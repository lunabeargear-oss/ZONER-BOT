import json
import os
import threading
import time
from datetime import datetime
from flask import Flask, jsonify, render_template, request

from ai_guide import answer_user_question, build_live_briefing
from btc_analytics import BTCAnalytics
from config import BotConfig, RISK_PRESETS
from execution import PolymarketExecutor
from market_discovery import MarketDiscovery

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
RUNTIME_CONFIG_PATH = os.path.join(BASE_DIR, "runtime_config.json")
TRADES_STATE_PATH = os.path.join(BASE_DIR, "trades_state.json")

app = Flask(__name__, template_folder=os.path.join(BASE_DIR, "templates"))

cfg = BotConfig.from_env()
btc = BTCAnalytics()
discovery = MarketDiscovery()
executor = PolymarketExecutor(cfg)
state_lock = threading.Lock()

bot_state = {
    "paused": False,
    "breaker_tripped": False,
    "breaker_reason": "",
    "last_error": "",
    "spot": 90000.0,
    "spot_history": [],
    "btc_matrix": {},
    "signal": {"side": "UP", "score": 0.5, "p_win": 0.5, "move_pct": 0.0, "aligned_timeframes": 0, "rsi_15m": 50.0},
    "market": None,
    "book": {
        "up_ask": 0.52,
        "up_bid": 0.48,
        "down_ask": 0.52,
        "down_bid": 0.48,
        "spread": 0.04,
        "ask_depth": 0.0,
        "bid_depth": 0.0,
        "book_source": "Fallback",
    },
    "gate": {"ready": False, "side": "UP", "reason": "Starting up...", "score": 0.5, "ev_edge": 0.0},
    "account": {"connected": False, "cash_usd": 300.0 if cfg.dry_run else 0.0, "equity_usd": 300.0 if cfg.dry_run else 0.0, "positions": [], "error": ""},
    "last_account_ts": 0.0,
    "last_trade_ts": 0.0,
    "peak_equity_usd": 0.0,
    "peak_daily_pnl": 0.0,
    "loss_streak": 0,
    "next_stake_usd": cfg.stake_usd,
    "open_positions": [],
    "trades": [],
}


def _parse_bool(val) -> bool:
    if isinstance(val, bool):
        return val
    return str(val).strip().lower() in ("1", "true", "yes", "on")


def _load_runtime_config():
    if not os.path.exists(RUNTIME_CONFIG_PATH):
        return
    try:
        with open(RUNTIME_CONFIG_PATH, "r", encoding="utf-8") as f:
            saved = json.load(f)
        for k, v in saved.items():
            if hasattr(cfg, k):
                cur = getattr(cfg, k)
                if isinstance(cur, bool):
                    setattr(cfg, k, _parse_bool(v))
                elif isinstance(cur, int):
                    setattr(cfg, k, int(v))
                elif isinstance(cur, float):
                    setattr(cfg, k, float(v))
                else:
                    setattr(cfg, k, str(v))
    except Exception:
        pass


def _save_runtime_config():
    try:
        with open(RUNTIME_CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(cfg.to_public_dict(), f, indent=2)
    except Exception:
        pass


def _load_trades_state():
    if not os.path.exists(TRADES_STATE_PATH):
        return
    try:
        with open(TRADES_STATE_PATH, "r", encoding="utf-8") as f:
            saved = json.load(f)
        bot_state["trades"] = saved.get("trades", [])[:150]
        bot_state["open_positions"] = saved.get("open_positions", [])
        bot_state["loss_streak"] = int(saved.get("loss_streak", 0))
    except Exception:
        pass


def _save_trades_state():
    try:
        with open(TRADES_STATE_PATH, "w", encoding="utf-8") as f:
            json.dump({
                "trades": bot_state["trades"][:150],
                "open_positions": bot_state["open_positions"],
                "loss_streak": bot_state["loss_streak"],
            }, f, indent=2)
    except Exception:
        pass


def _is_purchased_status(status: str) -> bool:
    s = str(status).upper()
    return ("FILLED" in s) or ("PAPER_EXECUTED" in s) or ("MANUAL_PAPER" in s)


def _compute_metrics() -> dict:
    closed = [t for t in bot_state["trades"] if str(t.get("status", "")).startswith(("SETTLED", "CLOSED_SCALP", "PAPER_SCALP", "LIVE_SCALP")) or str(t.get("side", "")).startswith("SELL_")]
    net_pnl = round(sum(float(t.get("pnl") or 0.0) for t in bot_state["trades"]), 2)
    wins = sum(1 for t in closed if float(t.get("pnl") or 0.0) > 0)
    losses = sum(1 for t in closed if float(t.get("pnl") or 0.0) < 0)
    win_rate = round((wins / max(1, wins + losses)) * 100.0, 1) if (wins + losses) else 0.0
    return {
        "net_pnl": net_pnl,
        "wins": wins,
        "losses": losses,
        "win_rate": win_rate,
        "open_count": len(bot_state["open_positions"]),
    }


def _compute_kelly_stake(p_win: float, ask_price: float) -> tuple[float, float]:
    price = max(0.05, min(0.95, float(ask_price)))
    ev_edge = round(p_win - price, 4)
    bankroll = float(bot_state["account"].get("cash_usd") or 0.0)
    if cfg.dry_run and bankroll <= 0:
        bankroll = 300.0

    if bankroll <= 0:
        return round(cfg.stake_usd, 2), ev_edge

    full_kelly = max(0.0, (p_win - price) / max(0.05, 1.0 - price))
    frac_kelly = full_kelly * cfg.kelly_fraction
    kelly_usd = bankroll * frac_kelly
    stake = max(cfg.stake_usd, kelly_usd) if ev_edge >= cfg.min_ev_edge else cfg.stake_usd
    stake = min(stake, cfg.max_stake_usd, max(1.0, bankroll * 0.15))
    return round(max(1.0, stake), 2), ev_edge


def _check_safety_breakers(metrics: dict):
    net_pnl = metrics["net_pnl"]
    bot_state["peak_daily_pnl"] = max(bot_state["peak_daily_pnl"], net_pnl)

    if net_pnl <= -cfg.max_daily_loss_usd:
        bot_state["breaker_tripped"] = True
        bot_state["breaker_reason"] = f"Daily loss limit (-${cfg.max_daily_loss_usd:.0f}) reached"
    elif cfg.anti_greed_profit_usd > 0 and net_pnl >= cfg.anti_greed_profit_usd:
        bot_state["breaker_tripped"] = True
        bot_state["breaker_reason"] = f"Anti-Greed Profit Pause (+${cfg.anti_greed_profit_usd:.0f}) reached"
    elif bot_state["peak_daily_pnl"] >= 10.0 and cfg.profit_lock_pct > 0:
        floor = bot_state["peak_daily_pnl"] * cfg.profit_lock_pct
        if net_pnl <= floor:
            bot_state["breaker_tripped"] = True
            bot_state["breaker_reason"] = f"Profit-Lock Floor (${floor:.2f}) triggered"
    elif bot_state["loss_streak"] >= cfg.max_consecutive_losses:
        bot_state["breaker_tripped"] = True
        bot_state["breaker_reason"] = f"Max loss streak ({cfg.max_consecutive_losses}) reached"


def _settle_expired_trades(spot: float, now: float):
    changed = False
    for t in bot_state["trades"]:
        if not t.get("purchased"):
            continue
        status = str(t.get("status", ""))
        settle_ts = float(t.get("settle_ts") or 0)
        if status.startswith("OPEN") and settle_ts > 0 and now >= settle_ts:
            side = str(t.get("side", "UP")).upper()
            won = None
            if str(t.get("mode", "")).upper() == "LIVE":
                s_val = discovery.fetch_settlement(t.get("slug", ""))
                if s_val is not None:
                    won = (s_val >= 0.5) if side == "UP" else (s_val < 0.5)
            if won is None:
                start_spot = float(t.get("window_start_spot") or t.get("spot") or spot)
                went_up = spot >= start_spot
                won = (side == "UP" and went_up) or (side == "DOWN" and not went_up)

            stake = float(t.get("stake") or 0.0)
            price = max(0.05, float(t.get("price") or 0.5))
            shares = stake / price
            fee = round(stake * 0.009, 2)
            pnl = round(shares - stake - fee, 2) if won else round(-stake, 2)
            t["pnl"] = pnl
            t["status"] = "SETTLED_WIN" if won else "SETTLED_LOSS"

            if won:
                bot_state["loss_streak"] = 0
            else:
                bot_state["loss_streak"] += 1
            changed = True

    if changed:
        bot_state["open_positions"] = [p for p in bot_state["open_positions"] if now < float(p.get("end_ts") or 0)]
        _check_safety_breakers(_compute_metrics())
        _save_trades_state()


def _evaluate_scalp_exits(market, book, spot, now):
    if not cfg.scalp_enabled or not bot_state["open_positions"]:
        return
    remaining = []
    changed = False
    for pos in bot_state["open_positions"]:
        slug = pos.get("slug")
        side = pos.get("side", "UP")
        entry_price = float(pos.get("entry_price") or 0.5)
        opened_ts = float(pos.get("opened_ts") or now)
        held_sec = now - opened_ts

        pos_book = book if (market and slug == market.get("slug")) else discovery.fetch_book(slug)
        current_bid = float(pos_book.get("up_bid", 0.50) if side == "UP" else pos_book.get("down_bid", 0.46))
        peak_price = max(float(pos.get("peak_price") or entry_price), current_bid)
        pos["peak_price"] = peak_price

        if held_sec < cfg.scalp_min_hold_sec:
            remaining.append(pos)
            continue

        pnl_pct = (current_bid - entry_price) / max(0.01, entry_price)
        drawdown_from_peak = (peak_price - current_bid) / max(0.01, peak_price)
        breakeven_armed = peak_price >= entry_price * (1.0 + cfg.scalp_breakeven_pct)
        trail_armed = peak_price >= entry_price * (1.0 + cfg.scalp_take_profit_pct * 0.5)

        reason = None
        if pnl_pct >= cfg.scalp_take_profit_pct:
            reason = "TAKE_PROFIT"
        elif trail_armed and drawdown_from_peak >= cfg.scalp_trailing_stop_pct:
            reason = "TRAIL_LOCK"
        elif breakeven_armed and current_bid <= entry_price:
            reason = "BREAKEVEN_LOCK"
        elif pnl_pct <= -cfg.scalp_stop_loss_pct:
            reason = "STOP_LOSS"
        elif cfg.scalp_max_hold_sec > 0 and held_sec >= cfg.scalp_max_hold_sec:
            reason = "MAX_HOLD"

        if not reason:
            remaining.append(pos)
            continue

        res = executor.close_scalp_position(
            slug=slug,
            side=side,
            exit_price=current_bid,
            quantity=float(pos.get("quantity") or 1),
            entry_price=entry_price,
            stake_usd=float(pos.get("stake_usd") or cfg.stake_usd),
            spot_price=spot,
            reason=reason,
        )
        pnl_usd = float(res.get("pnl_usd") or 0.0)
        if pnl_usd < 0:
            bot_state["loss_streak"] += 1
        elif pnl_usd > 0:
            bot_state["loss_streak"] = 0

        for t in bot_state["trades"]:
            if t.get("id") == pos.get("trade_id"):
                t["status"] = "CLOSED_SCALP"
                break

        bot_state["trades"].insert(0, {
            "id": f"sclp_{int(now * 1000)}",
            "ts": now,
            "time": datetime.now().strftime("%H:%M:%S"),
            "slug": slug,
            "side": f"SELL_{side}",
            "price": current_bid,
            "spot": spot,
            "window_start_spot": spot,
            "stake": float(pos.get("stake_usd") or cfg.stake_usd),
            "mode": res["mode"],
            "status": res["status"],
            "purchased": bool(res.get("ok")),
            "pnl": pnl_usd,
            "settle_ts": 0,
        })
        changed = True
        if not res.get("ok"):
            remaining.append(pos)

    bot_state["open_positions"] = remaining
    if changed:
        _check_safety_breakers(_compute_metrics())
        _save_trades_state()


def _record_entry_trade(market, side, price, spot, stake, res, now):
    raw_status = str(res.get("status", ""))
    purchased = bool(res.get("ok")) and _is_purchased_status(raw_status)
    display_status = f"OPEN ({raw_status})" if purchased else raw_status
    trade_id = f"tr_{int(now * 1000)}_{len(bot_state['trades'])}"
    trade = {
        "id": trade_id,
        "ts": now,
        "time": datetime.now().strftime("%H:%M:%S"),
        "slug": market["slug"],
        "side": side,
        "price": res.get("price") or price,
        "spot": spot,
        "window_start_spot": spot,
        "stake": res.get("stake") or stake,
        "mode": res["mode"],
        "status": display_status,
        "purchased": purchased,
        "pnl": 0.0,
        "settle_ts": market["end_ts"] if purchased else 0,
    }
    bot_state["trades"].insert(0, trade)
    if purchased:
        bot_state["open_positions"].append({
            "trade_id": trade_id,
            "slug": market["slug"],
            "side": side,
            "entry_price": float(res.get("price") or price),
            "peak_price": float(res.get("price") or price),
            "quantity": res.get("shares") or (stake / max(0.05, price)),
            "stake_usd": res.get("stake") or stake,
            "opened_ts": now,
            "end_ts": market["end_ts"],
        })
    _save_trades_state()
    return trade


def bot_loop():
    while True:
        try:
            now = time.time()
            spot = btc.fetch_spot()
            matrix = btc.get_variation_matrix()
            market = discovery.find_best_btc_market(cfg.target_timeframe, allow_fallback=True)
            book = discovery.fetch_book(market["slug"]) if market else bot_state["book"]

            acct = None
            if now - bot_state["last_account_ts"] > 15:
                acct = executor.get_account_snapshot()

            with state_lock:
                bot_state["last_error"] = ""
                if acct is not None:
                    bot_state["account"] = acct
                    bot_state["last_account_ts"] = now
                    eq = float(acct.get("equity_usd") or 0.0)
                    if eq > bot_state["peak_equity_usd"]:
                        bot_state["peak_equity_usd"] = eq

                bot_state["spot"] = spot
                bot_state["spot_history"].append({"ts": now, "price": spot})
                bot_state["spot_history"] = [p for p in bot_state["spot_history"] if now - p["ts"] <= 180]
                bot_state["btc_matrix"] = matrix
                bot_state["market"] = market
                bot_state["book"] = book

                _settle_expired_trades(spot, now)
                _evaluate_scalp_exits(market, book, spot, now)

                sig = btc.evaluate_signal(spot, bot_state["spot_history"], matrix, cfg.min_spot_move_pct)
                bot_state["signal"] = sig
                side = sig["side"]
                ask_price = book["up_ask"] if side == "UP" else book["down_ask"]
                next_stake, ev_edge = _compute_kelly_stake(sig["p_win"], ask_price)
                bot_state["next_stake_usd"] = next_stake

                if bot_state["paused"]:
                    gate = {"ready": False, "side": side, "reason": "Bot paused", "score": sig["score"], "ev_edge": ev_edge}
                elif bot_state["breaker_tripped"]:
                    gate = {"ready": False, "side": side, "reason": bot_state["breaker_reason"] or "Circuit breaker tripped", "score": sig["score"], "ev_edge": ev_edge}
                elif len(bot_state["open_positions"]) >= cfg.max_open_positions:
                    gate = {"ready": False, "side": side, "reason": f"Max open positions ({len(bot_state['open_positions'])}/{cfg.max_open_positions})", "score": sig["score"], "ev_edge": ev_edge}
                elif not market:
                    gate = {"ready": False, "side": side, "reason": ("Scanning 5m / 15m / 1h / 1d BTC markets..." if cfg.target_timeframe == "all" else f"Waiting for next {cfg.target_timeframe.upper()} BTC window"), "score": sig["score"], "ev_edge": ev_edge}
                elif market["seconds_left"] < cfg.min_seconds_left:
                    gate = {"ready": False, "side": side, "reason": f"Window closing ({market['seconds_left']}s left)", "score": sig["score"], "ev_edge": ev_edge}
                elif book["spread"] > cfg.max_spread:
                    gate = {"ready": False, "side": side, "reason": f"Spread ${book['spread']:.2f} > ${cfg.max_spread:.2f}", "score": sig["score"], "ev_edge": ev_edge}
                elif book["ask_depth"] < cfg.min_ask_depth_shares:
                    gate = {"ready": False, "side": side, "reason": f"Low depth ({int(book['ask_depth'])} < {cfg.min_ask_depth_shares}sh)", "score": sig["score"], "ev_edge": ev_edge}
                elif sig["abs_move"] < cfg.min_spot_move_pct:
                    gate = {"ready": False, "side": side, "reason": f"BTC move {sig['move_pct']*100:+.3f}% below ±{cfg.min_spot_move_pct*100:.2f}%", "score": sig["score"], "ev_edge": ev_edge}
                elif sig["score"] < cfg.predictor_min_score:
                    gate = {"ready": False, "side": side, "reason": f"Score {sig['score']:.2f} < {cfg.predictor_min_score:.2f}", "score": sig["score"], "ev_edge": ev_edge}
                elif ev_edge < cfg.min_ev_edge:
                    gate = {"ready": False, "side": side, "reason": f"+EV edge {ev_edge*100:+.1f}% < {cfg.min_ev_edge*100:.1f}%", "score": sig["score"], "ev_edge": ev_edge}
                else:
                    gate = {"ready": True, "side": side, "reason": f"READY — {side} ({sig['score']*100:.0f}%, +EV {ev_edge*100:.1f}%)", "score": sig["score"], "ev_edge": ev_edge}
                    if now - bot_state["last_trade_ts"] > 20:
                        bot_state["last_trade_ts"] = now
                        res = executor.place_order(market["slug"], side, ask_price, next_stake, spot, manual=False)
                        _record_entry_trade(market, side, ask_price, spot, next_stake, res, now)
                bot_state["gate"] = gate
        except Exception as exc:
            print(f"[bot_loop error] {exc}", flush=True)
            with state_lock:
                bot_state["last_error"] = str(exc)
        time.sleep(4)


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/widget")
def widget():
    return render_template("widget.html")


@app.route("/health")
def health():
    return jsonify({"status": "ok", "keys_configured": cfg.keys_configured(), "dry_run": cfg.dry_run, "scalp_enabled": cfg.scalp_enabled})


@app.route("/api/status")
def api_status():
    with state_lock:
        metrics = _compute_metrics()
        briefing = build_live_briefing(bot_state, cfg, metrics)
        return jsonify({
            "keys_configured": cfg.keys_configured(),
            "config": cfg.to_public_dict(),
            "presets": {k: v["label"] for k, v in RISK_PRESETS.items()},
            "paused": bot_state["paused"],
            "breaker_tripped": bot_state["breaker_tripped"],
            "breaker_reason": bot_state["breaker_reason"],
            "last_error": bot_state.get("last_error", ""),
            "spot": bot_state["spot"],
            "btc_matrix": bot_state["btc_matrix"],
            "signal": bot_state["signal"],
            "market": bot_state["market"],
            "book": bot_state["book"],
            "gate": bot_state["gate"],
            "account": bot_state["account"],
            "next_stake_usd": bot_state["next_stake_usd"],
            "metrics": metrics,
            "open_positions": bot_state["open_positions"],
            "trades": bot_state["trades"][:30],
            "ai_briefing": briefing,
        })


@app.route("/api/config", methods=["POST"])
def api_config():
    data = request.get_json(silent=True) or {}
    with state_lock:
        if "paused" in data:
            bot_state["paused"] = _parse_bool(data["paused"])
        for k, v in data.items():
            if k == "paused" or not hasattr(cfg, k):
                continue
            cur = getattr(cfg, k)
            if isinstance(cur, bool):
                setattr(cfg, k, _parse_bool(v))
            elif isinstance(cur, int):
                setattr(cfg, k, int(v))
            elif isinstance(cur, float):
                setattr(cfg, k, float(v))
            else:
                setattr(cfg, k, str(v))
        executor.cfg = cfg
        _save_runtime_config()
    return jsonify({"ok": True, "config": cfg.to_public_dict(), "paused": bot_state["paused"]})


@app.route("/api/preset", methods=["POST"])
def api_preset():
    data = request.get_json(silent=True) or {}
    profile = str(data.get("profile", "")).strip().lower()
    with state_lock:
        ok = cfg.apply_preset(profile)
        if not ok:
            return jsonify({"ok": False, "error": "Unknown preset"}), 400
        executor.cfg = cfg
        _save_runtime_config()
    return jsonify({"ok": True, "config": cfg.to_public_dict()})


@app.route("/api/manual-trade", methods=["POST"])
def api_manual_trade():
    data = request.get_json(silent=True) or {}
    side = str(data.get("side", "UP")).strip().upper()
    if side not in ("UP", "DOWN"):
        return jsonify({"ok": False, "error": f"Invalid side: {side}"}), 400

    with state_lock:
        market = bot_state["market"]
        stake = float(data.get("stake") or bot_state["next_stake_usd"] or cfg.stake_usd)
        tf = cfg.target_timeframe
        spot = bot_state["spot"]

    # Always resolve the best open BTC market on-demand if none is cached or window is nearly expired
    if not market or float(market.get("seconds_left") or 0) <= 10:
        market = discovery.find_best_btc_market(tf, allow_fallback=True)

    if not market:
        return jsonify({"ok": False, "error": "No open BTC Up/Down market available on Polymarket US right now."}), 400

    # Fetch fresh live orderbook right before execution so we hit the best market price
    fresh_book = discovery.fetch_book(market["slug"])
    price = fresh_book["up_ask"] if side == "UP" else fresh_book["down_ask"]
    now = time.time()

    with state_lock:
        bot_state["market"] = market
        bot_state["book"] = fresh_book
        res = executor.place_order(market["slug"], side, price, stake, spot, manual=True)
        trade = _record_entry_trade(market, side, price, spot, stake, res, now)

    if res.get("ok") and not cfg.dry_run:
        acct = executor.get_account_snapshot()
        with state_lock:
            bot_state["account"] = acct
            bot_state["last_account_ts"] = time.time()

    ok = bool(res.get("ok"))
    return jsonify({
        "ok": ok,
        "error": "" if ok else str(res.get("status") or "Order failed"),
        "market": market,
        "trade": trade,
        "result": res,
    })


@app.route("/api/close-position", methods=["POST"])
def api_close_position():
    data = request.get_json(silent=True) or {}
    trade_id = str(data.get("trade_id") or "")
    now = time.time()
    with state_lock:
        target = next((p for p in bot_state["open_positions"] if p.get("trade_id") == trade_id), None)
        if not target:
            return jsonify({"ok": False, "error": "Position not found"}), 404
        side = target.get("side", "UP")
        book = discovery.fetch_book(target["slug"])
        exit_price = float(book.get("up_bid", 0.50) if side == "UP" else book.get("down_bid", 0.48))
        res = executor.close_scalp_position(
            slug=target["slug"],
            side=side,
            exit_price=exit_price,
            quantity=float(target.get("quantity") or 1),
            entry_price=float(target.get("entry_price") or 0.5),
            stake_usd=float(target.get("stake_usd") or cfg.stake_usd),
            spot_price=bot_state["spot"],
            reason="MANUAL",
        )
        bot_state["open_positions"] = [p for p in bot_state["open_positions"] if p.get("trade_id") != trade_id]
        for t in bot_state["trades"]:
            if t.get("id") == trade_id:
                t["status"] = "CLOSED_SCALP"
                break
        bot_state["trades"].insert(0, {
            "id": f"sclp_{int(now * 1000)}",
            "ts": now,
            "time": datetime.now().strftime("%H:%M:%S"),
            "slug": target["slug"],
            "side": f"SELL_{side}",
            "price": exit_price,
            "spot": bot_state["spot"],
            "window_start_spot": bot_state["spot"],
            "stake": float(target.get("stake_usd") or cfg.stake_usd),
            "mode": res["mode"],
            "status": res["status"],
            "purchased": bool(res.get("ok")),
            "pnl": float(res.get("pnl_usd") or 0.0),
            "settle_ts": 0,
        })
        _save_trades_state()
        return jsonify({"ok": bool(res.get("ok")), "result": res})


@app.route("/api/sync-account", methods=["POST"])
def api_sync_account():
    acct = executor.get_account_snapshot()
    with state_lock:
        bot_state["account"] = acct
        bot_state["last_account_ts"] = time.time()
    return jsonify({"ok": acct.get("connected", False), "account": acct})


@app.route("/api/reset-breaker", methods=["POST"])
def api_reset_breaker():
    with state_lock:
        bot_state["breaker_tripped"] = False
        bot_state["breaker_reason"] = ""
        bot_state["loss_streak"] = 0
    return jsonify({"ok": True})


@app.route("/api/ai-guide", methods=["GET", "POST"])
def api_ai_guide():
    with state_lock:
        metrics = _compute_metrics()
        if request.method == "GET":
            return jsonify(build_live_briefing(bot_state, cfg, metrics))
        data = request.get_json(silent=True) or {}
        question = str(data.get("question") or "")
        reply = answer_user_question(question, bot_state, cfg, metrics)
        return jsonify({"reply": reply, "briefing": build_live_briefing(bot_state, cfg, metrics)})


_load_runtime_config()
_load_trades_state()
_loop_started = False


def start_background_loop():
    global _loop_started
    if not _loop_started:
        _loop_started = True
        t = threading.Thread(target=bot_loop, daemon=True)
        t.start()


start_background_loop()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
