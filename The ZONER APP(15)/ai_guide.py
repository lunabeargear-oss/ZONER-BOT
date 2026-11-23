import time


def build_live_briefing(bot_state: dict, cfg, metrics: dict) -> dict:
    mode = "PAPER (Dry Run)" if cfg.dry_run else "LIVE (Real Money)"
    acct = bot_state.get("account") or {}
    connected = bool(acct.get("connected"))
    cash = float(acct.get("cash_usd") or 0.0)
    spot = float(bot_state.get("spot") or 0.0)
    market = bot_state.get("market")
    book = bot_state.get("book") or {}
    gate = bot_state.get("gate") or {}
    open_pos = bot_state.get("open_positions") or []
    matrix = bot_state.get("btc_matrix") or {}
    conf = matrix.get("confluence") or {}

    # 1. Headline status
    if not cfg.dry_run and not cfg.keys_configured():
        headline = "API keys are missing — add POLYMARKET_KEY_ID and POLYMARKET_SECRET_KEY in Render Environment."
        mood = "warn"
    elif bot_state.get("paused"):
        headline = "Bot is PAUSED. Auto-trading is off, though live prices and open positions still update."
        mood = "warn"
    elif bot_state.get("breaker_tripped"):
        headline = f"Safety Circuit Breaker tripped ({bot_state.get('breaker_reason') or 'Loss/Profit limit hit'}). Click Reset Breaker to resume."
        mood = "warn"
    elif gate.get("ready"):
        headline = f"Signal READY: {gate.get('side')} ({gate.get('reason')}). Auto-scalper is active in {mode}."
        mood = "good"
    else:
        headline = f"Watching market: {gate.get('reason') or 'Scanning BTC & Polymarket...'}"
        mood = "info"

    # 2. What's happening right now
    bullets = []
    bullets.append(
        f"Mode & Wallet: {mode} | API {'Connected' if connected else 'Disconnected'} | Cash: ${cash:.2f} | Profit: +${metrics.get('gross_profit', 0.0):.2f} | Loss: -${metrics.get('gross_loss', 0.0):.2f} | Net P&L: ${metrics.get('net_pnl', 0.0):+.2f}"
    )

    if market:
        bullets.append(
            f"Active Market: {market.get('title')} ({market.get('seconds_left', 0)}s left) | {book.get('book_source', 'Fallback')}: UP ${book.get('up_ask', 0):.2f} / DOWN ${book.get('down_ask', 0):.2f} | Depth: {int(book.get('ask_depth', 0))}sh | Spread: ${book.get('spread', 0):.2f}"
        )
    else:
        tf_desc = "5m, 15m, 1h, and 1d" if cfg.target_timeframe == "all" else cfg.target_timeframe.upper()
        bullets.append(f"Scanning {tf_desc} BTC Up/Down markets — waiting for an open window.")

    conf_dir = conf.get("direction", "NEUTRAL")
    conf_pct = int(conf.get("agreement_pct", 0))
    bullets.append(
        f"BTC Pulse: ${spot:,.2f} | Multi-timeframe trend: {conf_dir} ({conf_pct}% agreement across 5m/15m/1h/1d)."
    )

    if open_pos:
        pos_summaries = []
        now = time.time()
        for p in open_pos[:3]:
            side = p.get("side", "UP")
            entry = float(p.get("entry_price") or 0.5)
            bid = float(book.get("up_bid", entry) if side == "UP" else book.get("down_bid", entry))
            pnl_pct = ((bid - entry) / max(0.01, entry)) * 100.0
            held = int(now - float(p.get("opened_ts") or now))
            pos_summaries.append(f"{side} @{entry:.2f} ({pnl_pct:+.1f}%, {held}s)")
        bullets.append("Open Positions: " + " | ".join(pos_summaries))
    else:
        bullets.append(
            f"Triggers Armed: +{cfg.scalp_take_profit_pct*100:.0f}% TP, -{cfg.scalp_stop_loss_pct*100:.0f}% SL, {cfg.scalp_trailing_stop_pct*100:.1f}% Trail, +{cfg.scalp_breakeven_pct*100:.0f}% Breakeven, {cfg.scalp_max_hold_sec}s Max Hold."
        )

    # 3. Guided next step
    reason_lower = str(gate.get("reason") or "").lower()
    if not cfg.keys_configured() and not cfg.dry_run:
        guidance = "Next step: Add your Polymarket US API keys on Render, or switch Trading Mode to PAPER (Dry Run) to test safely."
    elif "depth" in reason_lower:
        guidance = f"Tip: Order book depth ({int(book.get('ask_depth', 0))}sh) is below your Min Book Depth ({cfg.min_ask_depth_shares}sh). Lower 'Min Book Depth (sh)' in Safety Guardrails if you want more frequent entries."
    elif "ev" in reason_lower or "edge" in reason_lower:
        guidance = "Tip: The bot is protecting your bankroll by skipping low-edge setups. Lower 'Min +EV Edge (%)' slightly only if you want more aggressive trades."
    elif "below" in reason_lower and "move" in reason_lower:
        guidance = "BTC is quiet right now. The bot is waiting for a sharper move before risking a trade."
    elif cfg.dry_run:
        guidance = "You are in PAPER mode (no real money). Once you see steady fills and triggers firing, switch Trading Mode to LIVE (Real Money) and click Apply."
    else:
        guidance = "You are LIVE. Keep an eye on Today's P&L and your Daily Loss Limit — the bot locks in profits and cuts losses automatically."

    return {
        "headline": headline,
        "mood": mood,
        "bullets": bullets,
        "guidance": guidance,
        "ts": time.time(),
    }


def answer_user_question(question: str, bot_state: dict, cfg, metrics: dict) -> str:
    q = (question or "").strip().lower()
    briefing = build_live_briefing(bot_state, cfg, metrics)
    acct = bot_state.get("account") or {}
    gate = bot_state.get("gate") or {}
    book = bot_state.get("book") or {}
    market = bot_state.get("market")
    trades = bot_state.get("trades") or []

    if not q or "going on" in q or "status" in q or "update" in q:
        return f"{briefing['headline']}\n\n" + "\n".join(f"• {b}" for b in briefing["bullets"]) + f"\n\n{briefing['guidance']}"

    if "why" in q and ("trad" in q or "buy" in q or "not" in q or "hold" in q or "stuck" in q):
        reason = gate.get("reason") or "Scanning market"
        return (
            f"Right now the Signal Gate says: **{reason}**.\n\n"
            f"• Active Market: {market['title'] if market else 'None open right now'}\n"
            f"• Book Source: {book.get('book_source', 'Fallback')} | Depth: {int(book.get('ask_depth', 0))}sh (Min required: {cfg.min_ask_depth_shares}sh) | Spread: ${book.get('spread', 0):.2f} (Max: ${cfg.max_spread:.2f})\n\n"
            f"{briefing['guidance']}"
        )

    if "paper" in q or "live" in q or "mode" in q or "real money" in q:
        mode = "PAPER (Dry Run — simulated fills, $0 risked)" if cfg.dry_run else "LIVE (Real Money — orders sent to Polymarket US)"
        return (
            f"You are currently in **{mode}**.\n\n"
            f"To switch modes anytime, scroll to **Bankroll Growth, Compounding & Safety Guardrails**, change **Trading Mode**, and click **Apply Growth & Safety Settings**. "
            f"Before running LIVE, confirm the top badge says **API CONNECTED** and your real cash balance (${float(acct.get('cash_usd') or 0):.2f}) is shown."
        )

    if "risk" in q or "trigger" in q or "sell" in q or "scalp" in q or "stop" in q or "profit" in q:
        return (
            f"Your active profile is **{cfg.risk_profile.upper()}** with pre-trade sell triggers locked before every entry:\n\n"
            f"• **Take-Profit:** +{cfg.scalp_take_profit_pct*100:.1f}% (auto-cashes out)\n"
            f"• **Stop-Loss:** -{cfg.scalp_stop_loss_pct*100:.1f}% (cuts losers fast)\n"
            f"• **Breakeven Lock:** moves stop to entry after +{cfg.scalp_breakeven_pct*100:.1f}% gain\n"
            f"• **Trailing Stop:** {cfg.scalp_trailing_stop_pct*100:.1f}% pullback lock once in profit\n"
            f"• **Hold Window:** {cfg.scalp_min_hold_sec}s min / {cfg.scalp_max_hold_sec}s max\n"
            f"• **Daily Limits:** Max Loss -${cfg.max_daily_loss_usd:.0f} | Anti-Greed Pause +${cfg.anti_greed_profit_usd:.0f} | Profit-Lock Floor {cfg.profit_lock_pct*100:.0f}%"
        )

    if "api" in q or "balance" in q or "book" in q or "connect" in q:
        return (
            f"API Status: **{'CONNECTED' if acct.get('connected') else 'DISCONNECTED'}** | "
            f"Cash: **${float(acct.get('cash_usd') or 0):.2f}** | "
            f"Book Feed: **{book.get('book_source', 'Fallback')}** (Depth: {int(book.get('ask_depth', 0))}sh, Spread: ${book.get('spread', 0):.2f}).\n\n"
            + (f"Last API note: {acct.get('error')}" if acct.get("error") else "Your Polymarket US connection looks healthy.")
        )

    if "last" in q or "trade" in q or "history" in q:
        if not trades:
            return "No trades have been logged in this session yet. Once a signal passes all gates (or you click Buy UP / Buy DOWN), it will appear in Recent Trades."
        t = trades[0]
        return (
            f"Most recent trade: **{t.get('side')}** on `{t.get('slug')}` at **${float(t.get('price') or 0):.2f}** "
            f"(Stake: ${float(t.get('stake') or 0):.2f}, Mode: {t.get('mode')}, Status: {t.get('status')}, P&L: ${float(t.get('pnl') or 0):+.2f})."
        )

    return f"{briefing['headline']}\n\n{briefing['guidance']}\n\nYou can ask me about: why it isn't trading, Paper vs Live mode, your sell triggers & risk limits, API/book health, or your last trade."
