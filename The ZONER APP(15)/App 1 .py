import json
import os
import threading
import time
import uuid
from datetime import datetime, timezone

import httpx
from flask import Flask, jsonify, render_template, request

from btc_analytics import BTCAnalytics
from config import BotConfig, RISK_PROFILES
from execution import TradeExecutor
from market_discovery import MarketDiscovery, SUPPORTED_TIMEFRAMES

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
RUNTIME_CFG_PATH = os.path.join(BASE_DIR, "runtime_config.json")
TRADES_STATE_PATH = os.path.join(BASE_DIR, "trades_state.json")

app = Flask(__name__, template_folder=os.path.join(BASE_DIR, "templates"))

cfg = BotConfig.from_env(os.path.join(BASE_DIR, ".env"))
discovery = MarketDiscovery()
executor = TradeExecutor(cfg)
analytics = BTCAnalytics()

state_lock = threading.Lock()

bot_state = {
    "spot_price": 0.0,
    "spot_updated_at": 0.0,
    "variation": {},
    "active_market": None,
    "markets_by_tf": {"5m": None, "15m": None, "1h": None, "1d": None},
    "all_markets": [],
    "book": None,
    "bankroll_usd": None,
    "bankroll_error": "",
    "last_bankroll_sync": 0.0,
    "paused": False,
    "pause_reason": "",
    "breaker_tripped": False,
    "breaker_reason": "",
    "daily_pnl": 0.0,
    "peak_daily_pnl": 0.0,
    "profit_lock_floor_usd": 0.0,
    "loss_streak": 0,
    "trades": [],
    "last_auto_trade_ts": 0.0,
    "last_loop_ts": 0.0,
    "signal": {
        "direction": "NEUTRAL",
        "score": 0.0,
        "edge_pct": 0.0,
        "confluence": False,
        "timeframe": "all",
        "reason": "Initializing...",
    },
}


def _load_persisted():
    if os.path.exists(RUNTIME_CFG_PATH):
        try:
            with open(RUNTIME_CFG_PATH, "r", encoding="utf-8") as f:
                saved = json.load(f)
            if isinstance(saved, dict):
                for k, v in saved.items():
                    if k == "paused":
                        bot_state["paused"] = bool(v)
                    elif hasattr(cfg, k) and k not in ("polymarket_key_id", "polymarket_secret_key"):
                        setattr(cfg, k, v)
        except Exception:
            pass

    if os.path.exists(TRADES_STATE_PATH):
        try:
            with open(TRADES_STATE_PATH, "r", encoding="utf-8") as f:
                st = json.load(f)
            if isinstance(st, dict):
                bot_state["trades"] = st.get("trades", [])[-150:]
                bot_state["daily_pnl"] = float(st.get("daily_pnl", 0.0))
                bot_state["peak_daily_pnl"] = float(st.get("peak_daily_pnl", bot_state["daily_pnl"]))
                bot_state["profit_lock_floor_usd"] = float(st.get("profit_lock_floor_usd", 0.0))
                bot_state["loss_streak"] = int(st.get("loss_streak", 0))
        except Exception:
            pass


def _save_runtime_config():
    try:
        data = cfg.to_safe_dict()
        data.pop("keys_configured", None)
        data.pop("polymarket_key_id", None)
        data.pop("polymarket_secret_key", None)
        data["paused"] = bot_state["paused"]
        with open(RUNTIME_CFG_PATH, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
    except Exception:
        pass


def _save_trades_state():
    try:
        payload = {
            "trades": bot_state["trades"][-150:],
            "daily_pnl": round(bot_state["daily_pnl"], 2),
            "peak_daily_pnl": round(bot_state["peak_daily_pnl"], 2),
            "profit_lock_floor_usd": round(bot_state["profit_lock_floor_usd"], 2),
            "loss_streak": bot_state["loss_streak"],
        }
        with open(TRADES_STATE_PATH, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2)
    except Exception:
        pass


def fetch_btc_spot():
    try:
        with httpx.Client(timeout=4.5) as client:
            r = client.get("https://api.exchange.coinbase.com/products/BTC-USD/ticker")
            if r.status_code == 200:
                p = float(r.json().get("price", 0))
                if p > 0:
                    return p
    except Exception:
        pass
    return 0.0


def _compute_kelly_stake(bankroll: float, win_prob: float, market_price: float) -> float:
    p = max(0.05, min(0.95, float(win_prob)))
    price = max(0.05, min(0.95, float(market_price)))
    b = (1.0 - price) / price  # net odds on binary contract
    q = 1.0 - p
    full_kelly = (p * b - q) / max(0.01, b)
    if full_kelly <= 0:
        return 0.0
    frac_kelly = full_kelly * float(cfg.kelly_fraction)
    return max(1.0, bankroll * frac_kelly)


def _compute_dynamic_stake(win_prob=0.60, market_price=0.52):
    base = float(cfg.stake_usd)
    bankroll = bot_state.get("bankroll_usd") or cfg.starting_bankroll_usd or 100.0
    gains = max(0.0, float(bot_state.get("daily_pnl", 0.0)))
    compounded_base = base + (gains * float(cfg.auto_compound_pct) * 0.1)

    mode = cfg.sizing_mode
    if mode == "kelly":
        k_stake = _compute_kelly_stake(bankroll, win_prob, market_price)
        stake = k_stake if k_stake > 0 else compounded_base
    elif mode == "pct_bankroll" and bankroll > 0:
        stake = bankroll * float(cfg.bankroll_pct)
    elif mode == "scale_gains":
        steps = int(gains // max(1.0, float(cfg.gain_scale_step_usd)))
        stake = compounded_base * (1.0 + steps * float(cfg.gain_scale_mult))
    else:
        stake = compounded_base

    # Shrink stake by 50% after any consecutive loss to protect bankroll
    if bot_state.get("loss_streak", 0) >= 1:
        stake *= 0.5

    return round(max(1.0, min(float(cfg.max_stake_usd), stake)), 2)


def _current_triggers_from_cfg(overrides=None):
    ov = overrides or {}
    tp_pct = float(ov.get("take_profit_pct", cfg.scalp_take_profit_pct))
    sl_pct = float(ov.get("stop_loss_pct", cfg.scalp_stop_loss_pct))
    trail_pct = float(ov.get("trailing_stop_pct", cfg.scalp_trailing_stop_pct))
    be_pct = float(ov.get("breakeven_trigger_pct", cfg.breakeven_trigger_pct))
    max_hold = int(ov.get("max_hold_sec", cfg.scalp_max_hold_sec))
    return {
        "take_profit_pct": max(0.005, tp_pct),
        "stop_loss_pct": max(0.005, sl_pct),
        "trailing_stop_pct": max(0.002, trail_pct),
        "breakeven_trigger_pct": max(0.005, be_pct),
        "max_hold_sec": max(15, max_hold),
        "pre_expiry_exit_sec": int(ov.get("pre_expiry_exit_sec", cfg.pre_expiry_exit_sec)),
    }


def _check_confluence(variation: dict, direction: str) -> bool:
    d5 = (variation.get("5m") or {}).get("direction")
    d15 = (variation.get("15m") or {}).get("direction")
    d1h = (variation.get("1h") or {}).get("direction")
    matches = sum(1 for d in (d5, d15, d1h) if d == direction)
    return matches >= 2


def _check_safety_gates():
    if bot_state["paused"]:
        return False, bot_state.get("pause_reason") or "Bot is paused"
    if bot_state["breaker_tripped"]:
        return False, bot_state.get("breaker_reason") or "Circuit breaker tripped"

    pnl = float(bot_state["daily_pnl"])
    if pnl <= -abs(float(cfg.max_daily_loss_usd)):
        bot_state["breaker_tripped"] = True
        bot_state["breaker_reason"] = f"Daily loss limit reached (${pnl:.2f} <= -${cfg.max_daily_loss_usd:.2f})"
        return False, bot_state["breaker_reason"]

    if cfg.daily_profit_target_usd > 0 and pnl >= float(cfg.daily_profit_target_usd):
        bot_state["paused"] = True
        bot_state["pause_reason"] = f"Anti-greed daily profit target hit (+${pnl:.2f} >= +${cfg.daily_profit_target_usd:.2f})"
        return False, bot_state["pause_reason"]

    # Profit-Lock Ratchet: protects 60% of peak gains once peak >= $10
    if cfg.profit_lock_enabled and bot_state["peak_daily_pnl"] >= 10.0:
        floor = round(bot_state["peak_daily_pnl"] * float(cfg.profit_lock_floor_pct), 2)
        bot_state["profit_lock_floor_usd"] = max(bot_state["profit_lock_floor_usd"], floor)
        if pnl <= bot_state["profit_lock_floor_usd"]:
            bot_state["paused"] = True
            bot_state["pause_reason"] = f"Profit-Lock Ratchet triggered (${pnl:.2f} <= locked floor ${bot_state['profit_lock_floor_usd']:.2f})"
            return False, bot_state["pause_reason"]

    if bot_state["loss_streak"] >= int(cfg.max_consecutive_losses):
        bot_state["breaker_tripped"] = True
        bot_state["breaker_reason"] = f"Max consecutive losses ({bot_state['loss_streak']}) reached"
        return False, bot_state["breaker_reason"]

    bankroll = bot_state.get("bankroll_usd") or cfg.starting_bankroll_usd or 100.0
    drawdown = float(bot_state["peak_daily_pnl"]) - pnl
    if bankroll > 0 and (drawdown / bankroll) >= float(cfg.max_drawdown_pct):
        bot_state["breaker_tripped"] = True
        bot_state["breaker_reason"] = f"Max drawdown ({(drawdown / bankroll)*100:.1f}%) exceeded"
        return False, bot_state["breaker_reason"]

    open_cnt = sum(1 for t in bot_state["trades"] if t.get("status") == "OPEN")
    if open_cnt >= int(cfg.max_open_positions):
        return False, f"Max open positions ({open_cnt}/{cfg.max_open_positions}) active"

    return True, "OK"


def _record_closed_pnl(trade, exit_price, reason):
    entry = float(trade.get("entry_price", 0.5))
    shares = int(trade.get("shares", 1))
    stake = float(trade.get("stake_usd", entry * shares))
    gross = (float(exit_price) - entry) * shares
    fee = round(stake * 0.009, 2)
    net = round(gross - fee, 2)

    trade["status"] = "CLOSED"
    trade["exit_price"] = round(float(exit_price), 4)
    trade["exit_reason"] = reason
    trade["closed_at"] = datetime.now(timezone.utc).isoformat()
    trade["pnl_usd"] = net
    trade["pnl_pct"] = round(((float(exit_price) - entry) / max(0.01, entry)) * 100, 2)

    bot_state["daily_pnl"] = round(bot_state["daily_pnl"] + net, 2)
    if bot_state["daily_pnl"] > bot_state["peak_daily_pnl"]:
        bot_state["peak_daily_pnl"] = bot_state["daily_pnl"]
    if net < 0:
        bot_state["loss_streak"] += 1
    else:
        bot_state["loss_streak"] = 0


def _settle_expired_trades(spot_price):
    now = time.time()
    changed = False
    for t in bot_state["trades"]:
        if t.get("status") != "OPEN":
            continue
        settle_ts = float(t.get("settle_ts", 0))
        if settle_ts > 0 and now >= settle_ts:
            outcome = None
            if not cfg.dry_run:
                outcome = discovery.fetch_settlement(t.get("slug"))
            if outcome is None:
                start_spot = float(t.get("spot_price", spot_price))
                outcome = "UP" if spot_price >= start_spot else "DOWN"
            won = (outcome == t.get("side"))
            exit_p = 1.0 if won else 0.0
            _record_closed_pnl(t, exit_p, f"SETTLED_{outcome}")
            changed = True
    if changed:
        _save_trades_state()


def _evaluate_scalp_exits(spot_price):
    if not cfg.scalp_enabled:
        return
    now = time.time()
    changed = False
    book_cache = {}

    for t in bot_state["trades"]:
        if t.get("status") != "OPEN":
            continue
        opened_ts = float(t.get("opened_ts", now))
        elapsed = now - opened_ts
        if elapsed < int(cfg.scalp_min_hold_sec):
            continue

        slug = t.get("slug")
        if slug not in book_cache:
            book_cache[slug] = discovery.fetch_book(slug)
        bk = book_cache.get(slug)
        if not bk:
            continue

        side = t.get("side", "UP")
        cur_price = bk["up_ask"] if side == "UP" else bk["down_ask"]
        entry_price = float(t.get("entry_price", 0.5))
        t["current_price"] = round(cur_price, 4)
        t["live_pnl_pct"] = round(((cur_price - entry_price) / max(0.01, entry_price)) * 100, 2)

        peak_p = max(float(t.get("peak_price", entry_price)), cur_price)
        t["peak_price"] = round(peak_p, 4)

        trig = t.get("triggers") or _current_triggers_from_cfg()
        tp_pct = float(trig.get("take_profit_pct", cfg.scalp_take_profit_pct))
        sl_pct = float(trig.get("stop_loss_pct", cfg.scalp_stop_loss_pct))
        trail_pct = float(trig.get("trailing_stop_pct", cfg.scalp_trailing_stop_pct))
        be_pct = float(trig.get("breakeven_trigger_pct", cfg.breakeven_trigger_pct))
        max_hold_sec = int(trig.get("max_hold_sec", cfg.scalp_max_hold_sec))
        pre_expiry_sec = int(trig.get("pre_expiry_exit_sec", cfg.pre_expiry_exit_sec))

        gain_ratio = (cur_price - entry_price) / max(0.01, entry_price)
        peak_gain_ratio = (peak_p - entry_price) / max(0.01, entry_price)
        drop_from_peak = (peak_p - cur_price) / max(0.01, peak_p)

        # Arm Breakeven Stop once peak gain hits breakeven_trigger_pct
        if peak_gain_ratio >= be_pct and not t.get("breakeven_armed"):
            t["breakeven_armed"] = True
            t["sl_price"] = round(entry_price * 1.005, 4)

        reason = None
        if gain_ratio >= tp_pct:
            reason = "TAKE_PROFIT"
        elif t.get("breakeven_armed") and gain_ratio <= 0.002:
            reason = "BREAKEVEN_LOCK"
        elif peak_gain_ratio >= (tp_pct * 0.5) and drop_from_peak >= trail_pct:
            reason = "TRAIL_LOCK"
        elif gain_ratio <= -sl_pct:
            reason = "STOP_LOSS"
        elif elapsed >= max_hold_sec:
            reason = "MAX_HOLD"
        elif float(t.get("settle_ts", 0)) > 0 and (float(t["settle_ts"]) - now) <= pre_expiry_sec:
            reason = "PRE_EXPIRY"

        if reason:
            res = executor.close_scalp_position(
                slug=slug,
                side=side,
                exit_price=cur_price,
                quantity=t.get("shares", 1),
                entry_price=entry_price,
                stake_usd=t.get("stake_usd", 5.0),
                spot_price=spot_price,
                reason=reason,
            )
            if res.get("ok"):
                _record_closed_pnl(t, res.get("exit_price", cur_price), res.get("reason", reason))
                changed = True

    if changed:
        _save_trades_state()


def _create_trade_record(res, mkt, triggers, manual=False):
    entry = float(res["entry_price"])
    tp_pct = float(triggers["take_profit_pct"])
    sl_pct = float(triggers["stop_loss_pct"])
    return {
        "id": str(uuid.uuid4())[:8],
        "slug": res["slug"],
        "timeframe": mkt.get("timeframe", "5m"),
        "question": mkt.get("question", res["slug"]),
        "side": res["side"],
        "entry_price": round(entry, 4),
        "current_price": round(entry, 4),
        "peak_price": round(entry, 4),
        "shares": int(res["shares"]),
        "stake_usd": float(res["stake_usd"]),
        "spot_price": bot_state["spot_price"],
        "mode": res.get("mode", "PAPER"),
        "manual": manual,
        "status": "OPEN",
        "breakeven_armed": False,
        "opened_ts": time.time(),
        "opened_at": datetime.now(timezone.utc).isoformat(),
        "settle_ts": float(mkt.get("end_ts", time.time() + 300)),
        "triggers": triggers,
        "tp_price": round(min(0.99, entry * (1.0 + tp_pct)), 4),
        "sl_price": round(max(0.01, entry * (1.0 - sl_pct)), 4),
        "live_pnl_pct": 0.0,
    }


def bot_loop():
    while True:
        try:
            spot = fetch_btc_spot()
            if spot > 0:
                analytics.record_spot(spot)

            variation = analytics.get_multi_timeframe_variation(spot or bot_state["spot_price"])
            grouped_mkts, all_mkts = discovery.find_markets_by_timeframe(min_seconds_left=15)

            tf = cfg.target_timeframe if cfg.target_timeframe in SUPPORTED_TIMEFRAMES else "all"
            active_mkt = discovery.find_active_btc_market(timeframe=tf, min_seconds_left=15)
            book = discovery.fetch_book(active_mkt["slug"]) if active_mkt else None

            now = time.time()
            if now - bot_state["last_bankroll_sync"] >= 12.0:
                snap = executor.get_account_snapshot()
                with state_lock:
                    if snap.get("ok"):
                        bot_state["bankroll_usd"] = snap.get("balance_usd")
                        bot_state["bankroll_error"] = ""
                    else:
                        bot_state["bankroll_error"] = snap.get("error", "")
                    bot_state["last_bankroll_sync"] = now

            with state_lock:
                if spot > 0:
                    bot_state["spot_price"] = spot
                    bot_state["spot_updated_at"] = now
                bot_state["variation"] = variation
                bot_state["markets_by_tf"] = grouped_mkts
                bot_state["all_markets"] = all_mkts[:12]
                bot_state["active_market"] = active_mkt
                bot_state["book"] = book
                bot_state["last_loop_ts"] = now

                _settle_expired_trades(bot_state["spot_price"])
                _evaluate_scalp_exits(bot_state["spot_price"])

                v_tf = variation.get(tf) or variation.get("all") or {}
                sig_dir = v_tf.get("direction", "NEUTRAL")
                sig_score = float(v_tf.get("confidence", 0.0))
                spot_move = abs(float(v_tf.get("change_ratio", 0.0)))
                vol_ratio = float(v_tf.get("volatility_pct", 0.0)) / 100.0
                confluence_ok = _check_confluence(variation, sig_dir)

                entry_p = (book["up_ask"] if sig_dir == "UP" else book["down_ask"]) if book else 0.5
                edge_pct = round(sig_score - entry_p, 4)

                gate_ok, gate_reason = _check_safety_gates()
                sig_reason = "Ready"
                if not cfg.auto_scalp_enabled:
                    sig_reason = "Auto-scalp disabled"
                elif not gate_ok:
                    sig_reason = gate_reason
                elif not active_mkt or not book:
                    sig_reason = f"No active {tf.upper()} BTC market"
                elif active_mkt.get("seconds_left", 0) < cfg.min_seconds_left:
                    sig_reason = f"Waiting for next window ({active_mkt.get('seconds_left')}s left)"
                elif book["spread"] > cfg.max_spread:
                    sig_reason = f"Spread too wide ({book['spread']:.3f} > {cfg.max_spread:.3f})"
                elif book["ask_depth"] < cfg.min_ask_depth_shares:
                    sig_reason = f"Low depth ({book['ask_depth']} < {cfg.min_ask_depth_shares})"
                elif vol_ratio > cfg.max_volatility_pct:
                    sig_reason = f"Volatility spike guard ({vol_ratio*100:.2f}% > {cfg.max_volatility_pct*100:.2f}%)"
                elif spot_move < cfg.min_spot_move_pct:
                    sig_reason = f"Waiting for BTC move ({spot_move*100:.3f}% < {cfg.min_spot_move_pct*100:.3f}%)"
                elif cfg.confluence_required and not confluence_ok:
                    sig_reason = "Waiting for multi-timeframe confluence (5m/15m/1h)"
                elif sig_score < cfg.predictor_min_score:
                    sig_reason = f"Score {sig_score:.2f} < {cfg.predictor_min_score:.2f}"
                elif edge_pct < cfg.min_expected_edge_pct:
                    sig_reason = f"+EV edge too small ({edge_pct*100:.1f}% < {cfg.min_expected_edge_pct*100:.1f}%)"
                elif now - bot_state["last_auto_trade_ts"] < 20.0:
                    sig_reason = "Cooldown (20s)"
                else:
                    triggers = _current_triggers_from_cfg()
                    stake = _compute_dynamic_stake(win_prob=sig_score, market_price=entry_p)
                    res = executor.place_order(
                        slug=active_mkt["slug"],
                        side=sig_dir,
                        price=entry_p,
                        stake_usd=stake,
                        spot_price=bot_state["spot_price"],
                        triggers=triggers,
                        manual=False,
                    )
                    if res.get("ok"):
                        rec = _create_trade_record(res, active_mkt, triggers, manual=False)
                        bot_state["trades"].insert(0, rec)
                        bot_state["trades"] = bot_state["trades"][:150]
                        bot_state["last_auto_trade_ts"] = now
                        _save_trades_state()
                        sig_reason = f"Auto-bought {sig_dir} @ ${rec['entry_price']:.2f} (+EV {edge_pct*100:.1f}%)"
                    else:
                        sig_reason = f"Order error: {res.get('error', 'unknown')}"

                bot_state["signal"] = {
                    "direction": sig_dir,
                    "score": sig_score,
                    "edge_pct": round(edge_pct * 100, 1),
                    "confluence": confluence_ok,
                    "timeframe": tf,
                    "reason": sig_reason,
                }
        except Exception:
            pass
        time.sleep(4.0)


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/widget")
def widget():
    return render_template("widget.html")


@app.route("/api/state")
@app.route("/api/status")
def api_state():
    with state_lock:
        open_trades = [t for t in bot_state["trades"] if t.get("status") == "OPEN"]
        closed_trades = [t for t in bot_state["trades"] if t.get("status") == "CLOSED"]
        wins = sum(1 for t in closed_trades if float(t.get("pnl_usd", 0)) > 0)
        win_rate = round((wins / len(closed_trades)) * 100, 1) if closed_trades else 0.0
        sig = bot_state["signal"]
        bk = bot_state["book"] or {}
        entry_p = bk.get("up_ask", 0.52) if sig.get("direction") == "UP" else bk.get("down_ask", 0.52)
        return jsonify({
            "ok": True,
            "config": cfg.to_safe_dict(),
            "risk_profiles": RISK_PROFILES,
            "supported_timeframes": list(SUPPORTED_TIMEFRAMES),
            "spot_price": bot_state["spot_price"],
            "variation": bot_state["variation"],
            "active_market": bot_state["active_market"],
            "markets_by_tf": bot_state["markets_by_tf"],
            "all_markets": bot_state["all_markets"],
            "book": bot_state["book"],
            "bankroll_usd": bot_state["bankroll_usd"],
            "bankroll_error": bot_state["bankroll_error"],
            "paused": bot_state["paused"],
            "pause_reason": bot_state["pause_reason"],
            "breaker_tripped": bot_state["breaker_tripped"],
            "breaker_reason": bot_state["breaker_reason"],
            "daily_pnl": bot_state["daily_pnl"],
            "peak_daily_pnl": bot_state["peak_daily_pnl"],
            "profit_lock_floor_usd": bot_state["profit_lock_floor_usd"],
            "loss_streak": bot_state["loss_streak"],
            "win_rate": win_rate,
            "open_count": len(open_trades),
            "trades": bot_state["trades"][:50],
            "signal": sig,
            "next_stake_usd": _compute_dynamic_stake(sig.get("score", 0.6), entry_p),
        })


@app.route("/api/config", methods=["POST"])
def api_config():
    body = request.get_json(silent=True) or {}
    with state_lock:
        if "risk_profile" in body and body["risk_profile"] in RISK_PROFILES:
            cfg.apply_risk_profile(body["risk_profile"])
        if "paused" in body:
            bot_state["paused"] = bool(body["paused"])
            if not bot_state["paused"]:
                bot_state["pause_reason"] = ""
        if "target_timeframe" in body:
            tf = str(body["target_timeframe"]).strip().lower()
            if tf in SUPPORTED_TIMEFRAMES:
                cfg.target_timeframe = tf

        custom_touched = False
        numeric_fields = {
            "stake_usd": float,
            "max_stake_usd": float,
            "bankroll_pct": float,
            "kelly_fraction": float,
            "auto_compound_pct": float,
            "scalp_take_profit_pct": float,
            "scalp_stop_loss_pct": float,
            "scalp_trailing_stop_pct": float,
            "breakeven_trigger_pct": float,
            "scalp_max_hold_sec": int,
            "scalp_min_hold_sec": int,
            "pre_expiry_exit_sec": int,
            "predictor_min_score": float,
            "min_expected_edge_pct": float,
            "min_spot_move_pct": float,
            "max_volatility_pct": float,
            "max_spread": float,
            "min_ask_depth_shares": int,
            "min_seconds_left": int,
            "max_daily_loss_usd": float,
            "daily_profit_target_usd": float,
            "profit_lock_floor_pct": float,
            "max_open_positions": int,
            "max_consecutive_losses": int,
            "max_drawdown_pct": float,
        }
        for k, caster in numeric_fields.items():
            if k in body and body[k] is not None:
                setattr(cfg, k, caster(body[k]))
                if "risk_profile" not in body:
                    custom_touched = True

        for bfield in ("dry_run", "auto_scalp_enabled", "scalp_enabled", "require_pre_trade_triggers", "confluence_required", "profit_lock_enabled"):
            if bfield in body:
                setattr(cfg, bfield, bool(body[bfield]))

        if "sizing_mode" in body and body["sizing_mode"] in ("kelly", "scale_gains", "pct_bankroll", "flat"):
            cfg.sizing_mode = body["sizing_mode"]

        if custom_touched and "risk_profile" not in body:
            cfg.risk_profile = "custom"

        _save_runtime_config()
    return jsonify({"ok": True, "config": cfg.to_safe_dict()})


@app.route("/api/risk-profile", methods=["POST"])
def api_risk_profile():
    body = request.get_json(silent=True) or {}
    profile = str(body.get("profile", "")).strip().lower()
    with state_lock:
        if not cfg.apply_risk_profile(profile):
            return jsonify({"ok": False, "error": "Unknown risk profile"}), 400
        _save_runtime_config()
    return jsonify({"ok": True, "config": cfg.to_safe_dict()})


@app.route("/api/manual-trade", methods=["POST"])
def api_manual_trade():
    body = request.get_json(silent=True) or {}
    side = str(body.get("side", "UP")).strip().upper()
    if side not in ("UP", "DOWN"):
        return jsonify({"ok": False, "error": "Invalid side"}), 400

    with state_lock:
        mkt = bot_state["active_market"]
        bk = bot_state["book"]
        if not mkt or not bk:
            return jsonify({"ok": False, "error": "No active BTC market available right now"}), 400

        triggers = _current_triggers_from_cfg(body.get("triggers"))
        ok_trig, msg_trig = executor.validate_pre_trade_triggers(triggers)
        if not ok_trig:
            return jsonify({"ok": False, "error": msg_trig}), 400

        stake = float(body.get("stake_usd") or _compute_dynamic_stake())
        entry_p = bk["up_ask"] if side == "UP" else bk["down_ask"]
        res = executor.place_order(
            slug=mkt["slug"],
            side=side,
            price=entry_p,
            stake_usd=stake,
            spot_price=bot_state["spot_price"],
            triggers=triggers,
            manual=True,
        )
        if not res.get("ok"):
            return jsonify({"ok": False, "error": res.get("error", "Order failed")}), 400

        rec = _create_trade_record(res, mkt, triggers, manual=True)
        bot_state["trades"].insert(0, rec)
        bot_state["trades"] = bot_state["trades"][:150]
        _save_trades_state()
        return jsonify({"ok": True, "trade": rec})


@app.route("/api/cash-out", methods=["POST"])
def api_cash_out():
    body = request.get_json(silent=True) or {}
    trade_id = body.get("id")
    with state_lock:
        target = next((t for t in bot_state["trades"] if t.get("id") == trade_id and t.get("status") == "OPEN"), None)
        if not target:
            return jsonify({"ok": False, "error": "Open trade not found"}), 404
        bk = discovery.fetch_book(target["slug"]) or bot_state.get("book") or {}
        exit_p = (bk.get("up_ask") if target["side"] == "UP" else bk.get("down_ask")) or target.get("current_price") or target["entry_price"]
        res = executor.close_scalp_position(
            slug=target["slug"],
            side=target["side"],
            exit_price=exit_p,
            quantity=target["shares"],
            entry_price=target["entry_price"],
            stake_usd=target["stake_usd"],
            spot_price=bot_state["spot_price"],
            reason="MANUAL",
        )
        if not res.get("ok"):
            return jsonify({"ok": False, "error": res.get("error", "Cash-out failed")}), 400
        _record_closed_pnl(target, res.get("exit_price", exit_p), "MANUAL")
        _save_trades_state()
        return jsonify({"ok": True, "trade": target})


@app.route("/api/cash-out-all", methods=["POST"])
def api_cash_out_all():
    closed = 0
    errors = []
    with state_lock:
        for t in bot_state["trades"]:
            if t.get("status") != "OPEN":
                continue
            bk = discovery.fetch_book(t["slug"]) or {}
            exit_p = (bk.get("up_ask") if t["side"] == "UP" else bk.get("down_ask")) or t.get("current_price") or t["entry_price"]
            res = executor.close_scalp_position(
                slug=t["slug"],
                side=t["side"],
                exit_price=exit_p,
                quantity=t["shares"],
                entry_price=t["entry_price"],
                stake_usd=t["stake_usd"],
                spot_price=bot_state["spot_price"],
                reason="EMERGENCY_ALL",
            )
            if res.get("ok"):
                _record_closed_pnl(t, res.get("exit_price", exit_p), "EMERGENCY_ALL")
                closed += 1
            else:
                errors.append(res.get("error", "unknown"))
        bot_state["paused"] = True
        bot_state["pause_reason"] = "Paused after Emergency Cash-Out All"
        _save_trades_state()
        _save_runtime_config()
    return jsonify({"ok": True, "closed": closed, "errors": errors})


@app.route("/api/circuit-breaker/reset", methods=["POST"])
def api_breaker_reset():
    with state_lock:
        bot_state["breaker_tripped"] = False
        bot_state["breaker_reason"] = ""
        bot_state["loss_streak"] = 0
        bot_state["paused"] = False
        bot_state["pause_reason"] = ""
        _save_trades_state()
        _save_runtime_config()
    return jsonify({"ok": True})


@app.route("/api/sync-bankroll", methods=["POST"])
def api_sync_bankroll():
    snap = executor.get_account_snapshot()
    with state_lock:
        bot_state["last_bankroll_sync"] = time.time()
        if snap.get("ok"):
            bot_state["bankroll_usd"] = snap.get("balance_usd")
            bot_state["bankroll_error"] = ""
            return jsonify({"ok": True, "bankroll_usd": bot_state["bankroll_usd"]})
        bot_state["bankroll_error"] = snap.get("error", "Sync failed")
        return jsonify({"ok": False, "error": bot_state["bankroll_error"]}), 400


_load_persisted()
threading.Thread(target=bot_loop, daemon=True).start()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port, debug=False)