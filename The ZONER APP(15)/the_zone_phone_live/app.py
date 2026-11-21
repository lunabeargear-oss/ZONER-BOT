import json
import os
import threading
import time
from datetime import datetime

import httpx
from flask import Flask, jsonify, render_template, request

from config import BotConfig
from execution import TradeExecutor
from market_discovery import MarketDiscovery

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
RUNTIME_CONFIG_PATH = os.path.join(BASE_DIR, "runtime_config.json")
TRADES_STATE_PATH = os.path.join(BASE_DIR, "trades_state.json")
ENV_PATH = os.path.join(BASE_DIR, ".env")

app = Flask(__name__)
cfg = BotConfig.from_env(ENV_PATH)
discovery = MarketDiscovery(cfg)
executor = TradeExecutor(cfg)


def _parse_bool(val) -> bool:
    if isinstance(val, bool):
        return val
    return str(val).strip().lower() in ("1", "true", "yes", "on")


def load_runtime_config(target_cfg: BotConfig):
    if not os.path.exists(RUNTIME_CONFIG_PATH):
        return
    try:
        with open(RUNTIME_CONFIG_PATH, "r", encoding="utf-8") as f:
            saved = json.load(f)
        if "dry_run" in saved:
            target_cfg.dry_run = _parse_bool(saved["dry_run"])
        if "target_timeframe" in saved:
            target_cfg.target_timeframe = str(saved["target_timeframe"])
        if "sizing_mode" in saved:
            target_cfg.sizing_mode = str(saved["sizing_mode"])
        if "stake_usd" in saved:
            target_cfg.stake_usd = float(saved["stake_usd"])
        if "max_stake_usd" in saved:
            target_cfg.max_stake_usd = float(saved["max_stake_usd"])
        if "bankroll_pct" in saved:
            target_cfg.bankroll_pct = float(saved["bankroll_pct"])
        if "gain_scale_step_usd" in saved:
            target_cfg.gain_scale_step_usd = float(saved["gain_scale_step_usd"])
        if "gain_scale_mult" in saved:
            target_cfg.gain_scale_mult = float(saved["gain_scale_mult"])
        if "starting_bankroll_usd" in saved:
            target_cfg.starting_bankroll_usd = float(saved["starting_bankroll_usd"])
        if "predictor_min_score" in saved:
            target_cfg.predictor_min_score = float(saved["predictor_min_score"])
        if "min_spot_move_pct" in saved:
            target_cfg.min_spot_move_pct = float(saved["min_spot_move_pct"])
        if "max_spread" in saved:
            target_cfg.max_spread = float(saved["max_spread"])
        if "min_ask_depth_shares" in saved:
            target_cfg.min_ask_depth_shares = int(saved["min_ask_depth_shares"])
        if "min_seconds_left" in saved:
            target_cfg.min_seconds_left = int(saved["min_seconds_left"])
        if "max_daily_loss_usd" in saved:
            target_cfg.max_daily_loss_usd = float(saved["max_daily_loss_usd"])
        if "max_open_positions" in saved:
            target_cfg.max_open_positions = int(saved["max_open_positions"])
        if "max_consecutive_losses" in saved:
            target_cfg.max_consecutive_losses = int(saved["max_consecutive_losses"])
        if "scalp_enabled" in saved:
            target_cfg.scalp_enabled = _parse_bool(saved["scalp_enabled"])
        if "scalp_take_profit_pct" in saved:
            target_cfg.scalp_take_profit_pct = float(saved["scalp_take_profit_pct"])
        if "scalp_stop_loss_pct" in saved:
            target_cfg.scalp_stop_loss_pct = float(saved["scalp_stop_loss_pct"])
        if "scalp_trailing_stop_pct" in saved:
            target_cfg.scalp_trailing_stop_pct = float(saved["scalp_trailing_stop_pct"])
        if "scalp_min_hold_sec" in saved:
            target_cfg.scalp_min_hold_sec = int(saved["scalp_min_hold_sec"])
    except Exception:
        pass


def save_runtime_config(target_cfg: BotConfig):
    payload = {
        "dry_run": target_cfg.dry_run,
        "target_timeframe": target_cfg.target_timeframe,
        "sizing_mode": target_cfg.sizing_mode,
        "stake_usd": target_cfg.stake_usd,
        "max_stake_usd": target_cfg.max_stake_usd,
        "bankroll_pct": target_cfg.bankroll_pct,
        "gain_scale_step_usd": target_cfg.gain_scale_step_usd,
        "gain_scale_mult": target_cfg.gain_scale_mult,
        "starting_bankroll_usd": target_cfg.starting_bankroll_usd,
        "predictor_min_score": target_cfg.predictor_min_score,
        "min_spot_move_pct": target_cfg.min_spot_move_pct,
        "max_spread": target_cfg.max_spread,
        "min_ask_depth_shares": target_cfg.min_ask_depth_shares,
        "min_seconds_left": target_cfg.min_seconds_left,
        "max_daily_loss_usd": target_cfg.max_daily_loss_usd,
        "max_open_positions": target_cfg.max_open_positions,
        "max_consecutive_losses": target_cfg.max_consecutive_losses,
        "scalp_enabled": target_cfg.scalp_enabled,
        "scalp_take_profit_pct": target_cfg.scalp_take_profit_pct,
        "scalp_stop_loss_pct": target_cfg.scalp_stop_loss_pct,
        "scalp_trailing_stop_pct": target_cfg.scalp_trailing_stop_pct,
        "scalp_min_hold_sec": target_cfg.scalp_min_hold_sec,
    }
    try:
        with open(RUNTIME_CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2)
    except Exception:
        pass


bot_state = {
    "paused": False,
    "session_start_ts": time.time(),
    "spot": 0.0,
    "spot_history": [],
    "market": None,
    "book": {"up_ask": 0.52, "up_bid": 0.50, "down_ask": 0.48, "down_bid": 0.46, "spread": 0.02, "ask_depth": 100},
    "gate": {"ready": False, "reason": "Starting up...", "score": 0.5},
    "daily_pnl": 0.0,
    "loss_streak": 0,
    "breaker_tripped": False,
    "open_positions": [],
    "trades": [],
    "last_trade_ts": 0.0,
    "account": None,
    "last_account_ts": 0.0,
}
state_lock = threading.Lock()


def _is_purchased_status(status: str) -> bool:
    s = str(status or "").upper()
    if any(bad in s for bad in ("CANCELED", "CANCELLED", "FAILED", "BLOCKED", "REJECTED")):
        return False
    return any(good in s for good in ("OPEN", "FILLED", "LIVE_PLACED", "PAPER_EXECUTED", "MANUAL_PAPER", "SETTLED_WIN", "SETTLED_LOSS"))


def _save_trades_state():
    try:
        with open(TRADES_STATE_PATH, "w", encoding="utf-8") as f:
            json.dump({
                "session_start_ts": bot_state["session_start_ts"],
                "daily_pnl": bot_state["daily_pnl"],
                "loss_streak": bot_state["loss_streak"],
                "trades": bot_state["trades"][:150],
            }, f, indent=2)
    except Exception:
        pass


def load_trade_history():
    if os.path.exists(TRADES_STATE_PATH):
        try:
            with open(TRADES_STATE_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data.get("trades"), list):
                bot_state["trades"] = data["trades"]
            if data.get("session_start_ts"):
                bot_state["session_start_ts"] = float(data["session_start_ts"])
            if data.get("daily_pnl") is not None:
                bot_state["daily_pnl"] = float(data["daily_pnl"])
            if data.get("loss_streak") is not None:
                bot_state["loss_streak"] = int(data["loss_streak"])
        except Exception:
            pass


def _compute_metrics(now: float):
    trades = bot_state["trades"]
    closed = [t for t in trades if str(t.get("status", "")).startswith("SETTLED_") or "SCALP" in str(t.get("status", ""))]
    wins = sum(1 for t in closed if float(t.get("pnl") or 0) > 0)
    losses = sum(1 for t in closed if float(t.get("pnl") or 0) < 0)
    gross_win = sum(float(t.get("pnl") or 0) for t in closed if float(t.get("pnl") or 0) > 0)
    gross_loss = abs(sum(float(t.get("pnl") or 0) for t in closed if float(t.get("pnl") or 0) < 0))
    net_pnl = round(sum(float(t.get("pnl") or 0) for t in closed), 2)
    win_rate = round((wins / len(closed)) * 100, 1) if closed else 0.0
    pf = round(gross_win / gross_loss, 2) if gross_loss > 0 else (round(gross_win, 2) if gross_win > 0 else 0.0)
    return {
        "net_pnl": net_pnl,
        "wins": wins,
        "losses": losses,
        "win_rate": win_rate,
        "open_count": len(bot_state["open_positions"]),
        "profit_factor": pf,
    }


def _compute_dynamic_stake(metrics_net_pnl: float):
    base = max(1.0, float(cfg.stake_usd))
    max_s = max(base, float(cfg.max_stake_usd))
    acct = bot_state.get("account") or {}
    live_bal = acct.get("balance_usd")

    if cfg.starting_bankroll_usd <= 0 and live_bal is not None and live_bal > 0:
        cfg.starting_bankroll_usd = round(float(live_bal), 2)
        save_runtime_config(cfg)

    bankroll = float(live_bal) if (live_bal is not None and live_bal > 0) else (
        (cfg.starting_bankroll_usd if cfg.starting_bankroll_usd > 0 else 300.0) + metrics_net_pnl
    )

    mode = (cfg.sizing_mode or "scale_gains").lower()
    if mode == "flat":
        return round(min(base, max_s), 2), round(bankroll, 2), 0.0

    if mode == "pct_bankroll":
        raw = bankroll * max(0.005, float(cfg.bankroll_pct))
        scaled = max(base, min(max_s, raw))
        return round(scaled, 2), round(bankroll, 2), round(bankroll - (cfg.starting_bankroll_usd or bankroll), 2)

    # Default: "scale_gains" — scales up on positive gains, resets to base on active loss streak
    baseline = cfg.starting_bankroll_usd if cfg.starting_bankroll_usd > 0 else bankroll
    live_gain = round(bankroll - baseline, 2)
    effective_gain = max(live_gain, metrics_net_pnl)

    if bot_state["loss_streak"] > 0 or effective_gain <= 0:
        return round(min(base, max_s), 2), round(bankroll, 2), effective_gain

    steps = effective_gain / max(1.0, float(cfg.gain_scale_step_usd))
    mult = 1.0 + steps * max(0.0, float(cfg.gain_scale_mult))
    scaled = max(base, min(max_s, base * mult))
    return round(scaled, 2), round(bankroll, 2), effective_gain


def fetch_btc_spot():
    try:
        with httpx.Client(timeout=5.0) as client:
            r = client.get("https://api.exchange.coinbase.com/products/BTC-USD/ticker")
            if r.status_code == 200:
                return float(r.json()["price"])
    except Exception:
        pass
    return bot_state["spot"] or 91250.0


def _sync_live_positions(acct: dict, now: float):
    if cfg.dry_run or not acct or not acct.get("ok"):
        return
    raw_positions = acct.get("positions") or {}
    if not isinstance(raw_positions, dict):
        return
    tracked_slugs = {p.get("slug") for p in bot_state["open_positions"]}
    for slug, pdata in raw_positions.items():
        if not isinstance(pdata, dict):
            continue
        net_pos = float(pdata.get("netPosition") or pdata.get("netPositionDecimal") or 0)
        if abs(net_pos) < 0.5 or pdata.get("expired"):
            continue
        if slug in tracked_slugs:
            continue
        side = "UP" if net_pos > 0 else "DOWN"
        qty = abs(net_pos)
        avg_px = float((pdata.get("avgPx") or {}).get("value") or (pdata.get("costPerShare") or {}).get("value") or 0.5)
        cost_val = float((pdata.get("cost") or {}).get("value") or (qty * avg_px))
        bot_state["open_positions"].append({
            "trade_id": f"sync_{slug}",
            "slug": slug,
            "side": side,
            "entry_price": round(avg_px, 4),
            "peak_price": round(avg_px, 4),
            "current_bid": round(avg_px, 4),
            "pnl_pct": 0.0,
            "trail_armed": False,
            "held_sec": 0,
            "quantity": qty,
            "stake_usd": round(cost_val, 2),
            "opened_ts": now,
            "end_ts": now + 3600,
        })


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
        metrics = _compute_metrics(now)
        if metrics["net_pnl"] <= -cfg.max_daily_loss_usd or bot_state["loss_streak"] >= cfg.max_consecutive_losses:
            bot_state["breaker_tripped"] = True
        _save_trades_state()


def _evaluate_scalp_exits(market, book, spot, now):
    if not bot_state["open_positions"]:
        return
    slug_books = {}
    if market and book:
        slug_books[market["slug"]] = book

    remaining = []
    changed = False
    for pos in bot_state["open_positions"]:
        slug = pos.get("slug")
        pos_book = slug_books.get(slug)
        if pos_book is None:
            try:
                pos_book = discovery.fetch_book(slug)
                slug_books[slug] = pos_book
            except Exception:
                pos_book = book

        side = pos.get("side", "UP")
        entry_price = float(pos.get("entry_price") or 0.5)
        current_bid = float(pos_book.get("up_bid", 0.50) if side == "UP" else pos_book.get("down_bid", 0.46))
        peak_price = max(float(pos.get("peak_price") or entry_price), current_bid)
        pos["peak_price"] = peak_price
        pos["current_bid"] = current_bid

        held_sec = max(0, int(now - float(pos.get("opened_ts") or now)))
        pos["held_sec"] = held_sec

        pnl_pct = (current_bid - entry_price) / max(0.01, entry_price)
        drawdown_from_peak = (peak_price - current_bid) / max(0.01, peak_price)
        trail_armed = peak_price >= entry_price * (1.0 + cfg.scalp_take_profit_pct * 0.5)
        pos["pnl_pct"] = round(pnl_pct * 100, 2)
        pos["trail_armed"] = trail_armed

        if not cfg.scalp_enabled or held_sec < cfg.scalp_min_hold_sec:
            remaining.append(pos)
            continue

        reason = None
        if pnl_pct >= cfg.scalp_take_profit_pct:
            reason = "TAKE_PROFIT"
        elif trail_armed and drawdown_from_peak >= cfg.scalp_trailing_stop_pct:
            reason = "TRAIL_LOCK"
        elif pnl_pct <= -cfg.scalp_stop_loss_pct:
            reason = "STOP_LOSS"

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
        if not res.get("ok"):
            remaining.append(pos)
            continue

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
            "id": f"sclp_{int(now*1000)}",
            "ts": now,
            "time": datetime.now().strftime("%H:%M:%S"),
            "slug": slug,
            "side": f"SELL_{side}",
            "price": res.get("exit_price") or current_bid,
            "spot": spot,
            "window_start_spot": spot,
            "stake": float(pos.get("stake_usd") or cfg.stake_usd),
            "mode": res["mode"],
            "status": res["status"],
            "purchased": True,
            "pnl": pnl_usd,
            "settle_ts": 0,
        })
        changed = True

    bot_state["open_positions"] = remaining
    if changed:
        metrics = _compute_metrics(now)
        if metrics["net_pnl"] <= -cfg.max_daily_loss_usd or bot_state["loss_streak"] >= cfg.max_consecutive_losses:
            bot_state["breaker_tripped"] = True
        _save_trades_state()


def _record_entry_trade(market, side, price, spot, stake, res, now):
    raw_status = str(res.get("status", ""))
    purchased = bool(res.get("ok")) and _is_purchased_status(raw_status)
    display_status = f"OPEN ({raw_status})" if purchased else raw_status
    trade_id = f"tr_{int(now*1000)}_{len(bot_state['trades'])}"
    fill_price = float(res.get("price") or price)
    actual_stake = float(res.get("stake") or stake)
    trade = {
        "id": trade_id,
        "ts": now,
        "time": datetime.now().strftime("%H:%M:%S"),
        "slug": market["slug"],
        "side": side,
        "price": fill_price,
        "spot": spot,
        "window_start_spot": spot,
        "stake": actual_stake,
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
            "entry_price": fill_price,
            "peak_price": fill_price,
            "current_bid": fill_price,
            "pnl_pct": 0.0,
            "trail_armed": False,
            "held_sec": 0,
            "quantity": res.get("shares") or (actual_stake / max(0.05, fill_price)),
            "stake_usd": actual_stake,
            "opened_ts": now,
            "end_ts": market["end_ts"],
        })
    _save_trades_state()
    return trade


def bot_loop():
    while True:
        try:
            spot = fetch_btc_spot()
            now = time.time()
            market = discovery.find_active_btc_market(cfg.target_timeframe)
            book = bot_state["book"]
            if market:
                try:
                    book = discovery.fetch_book(market["slug"])
                except Exception:
                    pass

            acct = None
            if now - bot_state["last_account_ts"] > 15:
                acct = executor.get_account_snapshot()

            with state_lock:
                if acct is not None:
                    bot_state["account"] = acct
                    bot_state["last_account_ts"] = now
                    _sync_live_positions(acct, now)

                bot_state["spot"] = spot
                bot_state["spot_history"].append({"ts": now, "price": spot})
                bot_state["spot_history"] = [p for p in bot_state["spot_history"] if now - p["ts"] <= 180]
                bot_state["market"] = market
                bot_state["book"] = book

                bot_state["open_positions"] = [p for p in bot_state["open_positions"] if now < p["end_ts"] + 90]
                _settle_expired_trades(spot, now)
                _evaluate_scalp_exits(market, book, spot, now)

                ref = bot_state["spot_history"][0]["price"] if bot_state["spot_history"] else spot
                move_pct = (spot - ref) / ref if ref > 0 else 0.0
                abs_move = abs(move_pct)
                score = min(0.95, 0.50 + (abs_move / max(0.0005, cfg.min_spot_move_pct)) * 0.20)

                if bot_state["paused"]:
                    gate = {"ready": False, "reason": "Bot paused", "score": 0.0}
                elif bot_state["breaker_tripped"]:
                    gate = {"ready": False, "reason": "Circuit breaker tripped", "score": 0.0}
                elif len(bot_state["open_positions"]) >= cfg.max_open_positions:
                    gate = {"ready": False, "reason": f"Max open positions ({len(bot_state['open_positions'])}/{cfg.max_open_positions})", "score": 0.0}
                elif not market:
                    gate = {"ready": False, "reason": f"No active {cfg.target_timeframe} BTC market", "score": score}
                elif market["seconds_left"] < cfg.min_seconds_left:
                    gate = {"ready": False, "reason": f"Window closing ({market['seconds_left']}s left)", "score": score}
                elif book["spread"] > cfg.max_spread:
                    gate = {"ready": False, "reason": f"Spread ${book['spread']:.2f} > ${cfg.max_spread:.2f}", "score": score}
                elif book["ask_depth"] < cfg.min_ask_depth_shares:
                    gate = {"ready": False, "reason": f"Depth {int(book['ask_depth'])} < {cfg.min_ask_depth_shares}sh", "score": score}
                elif abs_move < cfg.min_spot_move_pct:
                    gate = {"ready": False, "reason": f"2m BTC move {move_pct*100:+.3f}% below ±{cfg.min_spot_move_pct*100:.2f}%", "score": score}
                elif score < cfg.predictor_min_score:
                    gate = {"ready": False, "reason": f"Score {score:.2f} < {cfg.predictor_min_score:.2f}", "score": score}
                else:
                    side = "UP" if move_pct >= 0 else "DOWN"
                    gate = {"ready": True, "side": side, "reason": f"READY — {side} ({score:.2f})", "score": score}
                    if now - bot_state["last_trade_ts"] > 20:
                        bot_state["last_trade_ts"] = now
                        metrics = _compute_metrics(now)
                        dyn_stake, _, _ = _compute_dynamic_stake(metrics["net_pnl"])
                        price = book["up_ask"] if side == "UP" else book["down_ask"]
                        res = executor.place_order(market["slug"], side, price, dyn_stake, spot, manual=False)
                        _record_entry_trade(market, side, price, spot, dyn_stake, res, now)
                bot_state["gate"] = gate
        except Exception:
            pass
        time.sleep(4)


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/widget")
def widget():
    return render_template("widget.html")


@app.route("/health")
def health():
    return jsonify({
        "status": "ok",
        "keys_configured": cfg.keys_configured(),
        "dry_run": cfg.dry_run,
        "scalp_enabled": cfg.scalp_enabled,
        "sizing_mode": cfg.sizing_mode,
    })


@app.route("/api/status")
def api_status():
    now = time.time()
    with state_lock:
        metrics = _compute_metrics(now)
        dyn_stake, bankroll_usd, effective_gain = _compute_dynamic_stake(metrics["net_pnl"])
        ref = bot_state["spot_history"][0]["price"] if bot_state["spot_history"] else bot_state["spot"]
        move_pct = ((bot_state["spot"] - ref) / ref * 100) if ref > 0 else 0.0
        return jsonify({
            "keys_configured": cfg.keys_configured(),
            "config": {
                "dry_run": cfg.dry_run,
                "target_timeframe": cfg.target_timeframe,
                "sizing_mode": cfg.sizing_mode,
                "stake_usd": cfg.stake_usd,
                "max_stake_usd": cfg.max_stake_usd,
                "bankroll_pct": cfg.bankroll_pct,
                "gain_scale_step_usd": cfg.gain_scale_step_usd,
                "gain_scale_mult": cfg.gain_scale_mult,
                "starting_bankroll_usd": cfg.starting_bankroll_usd,
                "predictor_min_score": cfg.predictor_min_score,
                "min_spot_move_pct": cfg.min_spot_move_pct,
                "max_spread": cfg.max_spread,
                "min_ask_depth_shares": cfg.min_ask_depth_shares,
                "min_seconds_left": cfg.min_seconds_left,
                "max_daily_loss_usd": cfg.max_daily_loss_usd,
                "max_open_positions": cfg.max_open_positions,
                "max_consecutive_losses": cfg.max_consecutive_losses,
                "scalp_enabled": cfg.scalp_enabled,
                "scalp_take_profit_pct": cfg.scalp_take_profit_pct,
                "scalp_stop_loss_pct": cfg.scalp_stop_loss_pct,
                "scalp_trailing_stop_pct": cfg.scalp_trailing_stop_pct,
                "scalp_min_hold_sec": cfg.scalp_min_hold_sec,
            },
            "paused": bot_state["paused"],
            "breaker_tripped": bot_state["breaker_tripped"],
            "spot": bot_state["spot"],
            "move_2m_pct": round(move_pct, 3),
            "market": bot_state["market"],
            "book": bot_state["book"],
            "gate": bot_state["gate"],
            "daily_pnl": metrics["net_pnl"],
            "wins": metrics["wins"],
            "losses": metrics["losses"],
            "win_rate": metrics["win_rate"],
            "profit_factor": metrics["profit_factor"],
            "loss_streak": bot_state["loss_streak"],
            "open_count": metrics["open_count"],
            "open_positions": bot_state["open_positions"],
            "trades": bot_state["trades"][:40],
            "bankroll_usd": bankroll_usd,
            "current_stake_usd": dyn_stake,
            "effective_gain_usd": effective_gain,
            "account": bot_state["account"],
        })


@app.route("/api/config", methods=["POST"])
def api_config():
    data = request.get_json(silent=True) or {}
    with state_lock:
        if "dry_run" in data:
            cfg.dry_run = _parse_bool(data["dry_run"])
        if "target_timeframe" in data:
            cfg.target_timeframe = str(data["target_timeframe"])
        if "sizing_mode" in data:
            cfg.sizing_mode = str(data["sizing_mode"])
        if "stake_usd" in data:
            cfg.stake_usd = float(data["stake_usd"])
        if "max_stake_usd" in data:
            cfg.max_stake_usd = float(data["max_stake_usd"])
        if "bankroll_pct" in data:
            cfg.bankroll_pct = float(data["bankroll_pct"])
        if "gain_scale_step_usd" in data:
            cfg.gain_scale_step_usd = float(data["gain_scale_step_usd"])
        if "gain_scale_mult" in data:
            cfg.gain_scale_mult = float(data["gain_scale_mult"])
        if "starting_bankroll_usd" in data:
            cfg.starting_bankroll_usd = float(data["starting_bankroll_usd"])
        if "predictor_min_score" in data:
            cfg.predictor_min_score = float(data["predictor_min_score"])
        if "min_spot_move_pct" in data:
            cfg.min_spot_move_pct = float(data["min_spot_move_pct"])
        if "max_spread" in data:
            cfg.max_spread = float(data["max_spread"])
        if "min_ask_depth_shares" in data:
            cfg.min_ask_depth_shares = int(data["min_ask_depth_shares"])
        if "min_seconds_left" in data:
            cfg.min_seconds_left = int(data["min_seconds_left"])
        if "max_daily_loss_usd" in data:
            cfg.max_daily_loss_usd = float(data["max_daily_loss_usd"])
        if "max_open_positions" in data:
            cfg.max_open_positions = int(data["max_open_positions"])
        if "max_consecutive_losses" in data:
            cfg.max_consecutive_losses = int(data["max_consecutive_losses"])
        if "scalp_enabled" in data:
            cfg.scalp_enabled = _parse_bool(data["scalp_enabled"])
        if "scalp_take_profit_pct" in data:
            cfg.scalp_take_profit_pct = float(data["scalp_take_profit_pct"])
        if "scalp_stop_loss_pct" in data:
            cfg.scalp_stop_loss_pct = float(data["scalp_stop_loss_pct"])
        if "scalp_trailing_stop_pct" in data:
            cfg.scalp_trailing_stop_pct = float(data["scalp_trailing_stop_pct"])
        if "scalp_min_hold_sec" in data:
            cfg.scalp_min_hold_sec = int(data["scalp_min_hold_sec"])
        if "paused" in data:
            bot_state["paused"] = _parse_bool(data["paused"])
        executor.cfg = cfg
        save_runtime_config(cfg)
    return jsonify({"ok": True, "dry_run": cfg.dry_run, "target_timeframe": cfg.target_timeframe, "scalp_enabled": cfg.scalp_enabled, "sizing_mode": cfg.sizing_mode})


@app.route("/api/manual-trade", methods=["POST"])
def api_manual_trade():
    data = request.get_json(silent=True) or {}
    side = str(data.get("side", "UP")).upper()
    now = time.time()
    with state_lock:
        market = bot_state["market"]
        if not market:
            return jsonify({"ok": False, "error": "No active BTC Up/Down market found"}), 400
        metrics = _compute_metrics(now)
        dyn_stake, _, _ = _compute_dynamic_stake(metrics["net_pnl"])
        stake_to_use = float(data.get("stake_usd") or dyn_stake)
        price = bot_state["book"]["up_ask"] if side == "UP" else bot_state["book"]["down_ask"]
        res = executor.place_order(market["slug"], side, price, stake_to_use, bot_state["spot"], manual=True)
        _record_entry_trade(market, side, price, bot_state["spot"], stake_to_use, res, now)
        return jsonify(res)


@app.route("/api/cash-out", methods=["POST"])
def api_cash_out():
    data = request.get_json(silent=True) or {}
    target_id = data.get("trade_id")
    target_slug = data.get("slug")
    now = time.time()
    with state_lock:
        remaining = []
        closed_res = None
        for pos in bot_state["open_positions"]:
            if closed_res is None and (
                (target_id and pos.get("trade_id") == target_id)
                or (target_slug and pos.get("slug") == target_slug)
                or (not target_id and not target_slug)
            ):
                side = pos.get("side", "UP")
                exit_px = float(pos.get("current_bid") or pos.get("entry_price") or 0.5)
                res = executor.close_scalp_position(
                    slug=pos["slug"],
                    side=side,
                    exit_price=exit_px,
                    quantity=float(pos.get("quantity") or 1),
                    entry_price=float(pos.get("entry_price") or 0.5),
                    stake_usd=float(pos.get("stake_usd") or cfg.stake_usd),
                    spot_price=bot_state["spot"],
                    reason="MANUAL",
                )
                closed_res = res
                if not res.get("ok"):
                    remaining.append(pos)
                else:
                    pnl_usd = float(res.get("pnl_usd") or 0.0)
                    for t in bot_state["trades"]:
                        if t.get("id") == pos.get("trade_id"):
                            t["status"] = "CLOSED_SCALP"
                            break
                    bot_state["trades"].insert(0, {
                        "id": f"man_{int(now*1000)}",
                        "ts": now,
                        "time": datetime.now().strftime("%H:%M:%S"),
                        "slug": pos["slug"],
                        "side": f"SELL_{side}",
                        "price": res.get("exit_price") or exit_px,
                        "spot": bot_state["spot"],
                        "window_start_spot": bot_state["spot"],
                        "stake": float(pos.get("stake_usd") or cfg.stake_usd),
                        "mode": res["mode"],
                        "status": res["status"],
                        "purchased": True,
                        "pnl": pnl_usd,
                        "settle_ts": 0,
                    })
                    _save_trades_state()
            else:
                remaining.append(pos)
        bot_state["open_positions"] = remaining
        if closed_res is None:
            return jsonify({"ok": False, "error": "Position not found"}), 404
        return jsonify(closed_res)


@app.route("/api/circuit-breaker/reset", methods=["POST"])
def api_reset_breaker():
    with state_lock:
        bot_state["breaker_tripped"] = False
        bot_state["loss_streak"] = 0
    return jsonify({"ok": True})


load_runtime_config(cfg)
load_trade_history()
executor.cfg = cfg

if __name__ == "__main__":
    t = threading.Thread(target=bot_loop, daemon=True)
    t.start()
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
