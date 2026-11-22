import csv
import os
import time
from datetime import datetime, timezone

try:
    from polymarket_us import PolymarketUS
except ImportError:
    PolymarketUS = None


class TradeExecutor:
    def __init__(self, config):
        self.cfg = config
        self._client = None
        self._last_error = ""

    def _ensure_client(self):
        if self._client is not None:
            return self._client
        if PolymarketUS is None:
            self._last_error = "polymarket-us SDK not installed"
            return None
        if not self.cfg.keys_configured():
            self._last_error = "API keys not configured"
            return None
        try:
            self._client = PolymarketUS(
                key_id=self.cfg.polymarket_key_id,
                secret_key=self.cfg.polymarket_secret_key,
            )
            self._last_error = ""
            return self._client
        except Exception as e:
            self._last_error = str(e)
            return None

    def reset_client(self):
        self._client = None
        self._last_error = ""

    def _extract_balance_usd(self, resp):
        if resp is None:
            return None
        if isinstance(resp, dict):
            for key in ("availableBalance", "available", "balance", "cashBalance", "buyingPower", "totalBalance"):
                if key in resp and resp[key] is not None:
                    try:
                        return float(resp[key])
                    except (TypeError, ValueError):
                        pass
            balances = resp.get("balances") or resp.get("data") or []
            if isinstance(balances, list) and balances:
                total = 0.0
                found = False
                for b in balances:
                    if isinstance(b, dict):
                        for key in ("currentBalance", "availableBalance", "available", "balance", "amount"):
                            if key in b and b[key] is not None:
                                try:
                                    total += float(b[key])
                                    found = True
                                    break
                                except (TypeError, ValueError):
                                    pass
                if found:
                    return round(total, 2)
        if hasattr(resp, "balances"):
            return self._extract_balance_usd({"balances": getattr(resp, "balances")})
        return None

    def get_account_snapshot(self):
        if self.cfg.dry_run:
            return {"ok": True, "balance_usd": None, "positions": {}, "mode": "PAPER"}
        client = self._ensure_client()
        if client is None:
            return {"ok": False, "balance_usd": None, "positions": {}, "error": self._last_error or "Client unavailable"}
        try:
            bal_resp = client.account.balances()
            bal_usd = self._extract_balance_usd(bal_resp)
            positions = {}
            try:
                pos_resp = client.portfolio.positions()
                pos_list = pos_resp.get("positions", []) if isinstance(pos_resp, dict) else getattr(pos_resp, "positions", [])
                for p in (pos_list or []):
                    if isinstance(p, dict):
                        slug = p.get("marketSlug") or p.get("slug") or ""
                        if slug:
                            positions[slug] = p
            except Exception:
                pass
            return {"ok": True, "balance_usd": bal_usd, "positions": positions, "mode": "LIVE"}
        except Exception as e:
            return {"ok": False, "balance_usd": None, "positions": {}, "error": str(e)}

    def _get_live_bbo_price(self, slug, side, fallback_price):
        client = self._ensure_client()
        if client is None:
            return fallback_price
        try:
            bbo = client.markets.bbo(slug)
            if isinstance(bbo, dict):
                if side == "UP":
                    val = bbo.get("bestAsk") or bbo.get("ask")
                else:
                    val = bbo.get("bestBid") or bbo.get("bid")
                if val is not None:
                    return max(0.01, min(0.99, float(val)))
        except Exception:
            pass
        return fallback_price

    def _poll_order_fill(self, resp, fallback_order_price):
        client = self._ensure_client()
        order_id = None
        if isinstance(resp, dict):
            order_id = resp.get("orderId") or resp.get("id")
        elif hasattr(resp, "orderId"):
            order_id = getattr(resp, "orderId")
        if not order_id or client is None:
            return {"filled": True, "avg_price": fallback_order_price, "quantity": None}

        for _ in range(3):
            time.sleep(0.25)
            try:
                od = client.orders.retrieve(order_id)
                if isinstance(od, dict):
                    status = str(od.get("status", "")).upper()
                    avg_p = od.get("avgFillPrice") or od.get("averagePrice") or fallback_order_price
                    qty = od.get("filledQuantity") or od.get("quantity")
                    if status in ("FILLED", "PARTIALLY_FILLED", "COMPLETED"):
                        return {"filled": True, "avg_price": float(avg_p), "quantity": float(qty) if qty else None}
            except Exception:
                break
        return {"filled": True, "avg_price": fallback_order_price, "quantity": None}

    def _append_csv(self, filename, row):
        exists = os.path.exists(filename)
        try:
            with open(filename, "a", newline="", encoding="utf-8") as f:
                writer = csv.DictWriter(f, fieldnames=list(row.keys()))
                if not exists:
                    writer.writeheader()
                writer.writerow(row)
        except Exception:
            pass

    def validate_pre_trade_triggers(self, triggers: dict):
        if not triggers:
            return False, "Missing pre-trade sell triggers"
        tp = float(triggers.get("take_profit_pct", 0) or 0)
        sl = float(triggers.get("stop_loss_pct", 0) or 0)
        if tp <= 0 or sl <= 0:
            return False, "Take-Profit and Stop-Loss triggers must both be > 0% before purchase"
        return True, "OK"

    def place_order(self, slug, side, price, stake_usd, spot_price, triggers=None, manual=False):
        if self.cfg.require_pre_trade_triggers:
            ok_trig, msg_trig = self.validate_pre_trade_triggers(triggers)
            if not ok_trig:
                return {"ok": False, "error": msg_trig}

        stake = min(float(stake_usd), float(self.cfg.max_stake_usd))
        safe_price = max(0.02, min(0.98, float(price)))
        shares = max(1, int(stake / safe_price))
        actual_stake = round(shares * safe_price, 2)

        tp_pct = float(triggers.get("take_profit_pct", self.cfg.scalp_take_profit_pct))
        sl_pct = float(triggers.get("stop_loss_pct", self.cfg.scalp_stop_loss_pct))
        trail_pct = float(triggers.get("trailing_stop_pct", self.cfg.scalp_trailing_stop_pct))
        max_hold = int(triggers.get("max_hold_sec", self.cfg.scalp_max_hold_sec))

        if self.cfg.dry_run:
            self._append_csv("paper_trades.csv", {
                "ts": datetime.now(timezone.utc).isoformat(),
                "action": "PAPER_MANUAL_BUY" if manual else "PAPER_AUTO_BUY",
                "slug": slug,
                "side": side,
                "price": safe_price,
                "shares": shares,
                "stake_usd": actual_stake,
                "spot_price": spot_price,
                "tp_pct": tp_pct,
                "sl_pct": sl_pct,
                "trail_pct": trail_pct,
                "max_hold_sec": max_hold,
            })
            return {
                "ok": True,
                "mode": "PAPER",
                "slug": slug,
                "side": side,
                "entry_price": safe_price,
                "shares": shares,
                "stake_usd": actual_stake,
            }

        client = self._ensure_client()
        if client is None:
            return {"ok": False, "error": self._last_error or "Polymarket US client not initialized"}

        live_price = self._get_live_bbo_price(slug, side, safe_price)
        limit_price = round(min(0.99, live_price + 0.01), 2)
        intent = "ORDER_INTENT_BUY_LONG" if side == "UP" else "ORDER_INTENT_BUY_SHORT"

        try:
            resp = client.orders.create({
                "marketSlug": slug,
                "intent": intent,
                "type": "ORDER_TYPE_LIMIT",
                "price": {"value": f"{limit_price:.2f}", "currency": "USD"},
                "quantity": shares,
                "tif": "TIME_IN_FORCE_IOC",
            })
            fill = self._poll_order_fill(resp, limit_price)
            fill_price = fill["avg_price"]
            fill_qty = int(fill["quantity"] or shares)
            fill_stake = round(fill_price * fill_qty, 2)

            self._append_csv("live_trades.csv", {
                "ts": datetime.now(timezone.utc).isoformat(),
                "action": "LIVE_MANUAL_BUY" if manual else "LIVE_AUTO_BUY",
                "slug": slug,
                "side": side,
                "price": fill_price,
                "shares": fill_qty,
                "stake_usd": fill_stake,
                "spot_price": spot_price,
                "tp_pct": tp_pct,
                "sl_pct": sl_pct,
                "trail_pct": trail_pct,
                "max_hold_sec": max_hold,
            })
            return {
                "ok": True,
                "mode": "LIVE",
                "slug": slug,
                "side": side,
                "entry_price": fill_price,
                "shares": fill_qty,
                "stake_usd": fill_stake,
            }
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def close_scalp_position(self, slug, side, exit_price, quantity, entry_price, stake_usd, spot_price, reason="TAKE_PROFIT"):
        safe_exit = max(0.01, min(0.99, float(exit_price)))
        qty = max(1, int(quantity))
        pnl = round((safe_exit - float(entry_price)) * qty, 2)

        if self.cfg.dry_run:
            self._append_csv("paper_trades.csv", {
                "ts": datetime.now(timezone.utc).isoformat(),
                "action": f"PAPER_SCALP_{reason}",
                "slug": slug,
                "side": side,
                "price": safe_exit,
                "shares": qty,
                "stake_usd": stake_usd,
                "spot_price": spot_price,
                "pnl": pnl,
            })
            return {"ok": True, "mode": "PAPER", "exit_price": safe_exit, "pnl": pnl, "reason": reason}

        client = self._ensure_client()
        if client is None:
            return {"ok": False, "error": self._last_error or "Client unavailable"}

        intent = "ORDER_INTENT_SELL_LONG" if side == "UP" else "ORDER_INTENT_SELL_SHORT"
        limit_price = round(max(0.01, safe_exit - 0.01), 2)
        try:
            resp = client.orders.create({
                "marketSlug": slug,
                "intent": intent,
                "type": "ORDER_TYPE_LIMIT",
                "price": {"value": f"{limit_price:.2f}", "currency": "USD"},
                "quantity": qty,
                "tif": "TIME_IN_FORCE_IOC",
            })
            fill = self._poll_order_fill(resp, safe_exit)
            actual_exit = fill["avg_price"]
            actual_pnl = round((actual_exit - float(entry_price)) * qty, 2)
            self._append_csv("live_trades.csv", {
                "ts": datetime.now(timezone.utc).isoformat(),
                "action": f"LIVE_SCALP_{reason}",
                "slug": slug,
                "side": side,
                "price": actual_exit,
                "shares": qty,
                "stake_usd": stake_usd,
                "spot_price": spot_price,
                "pnl": actual_pnl,
            })
            return {"ok": True, "mode": "LIVE", "exit_price": actual_exit, "pnl": actual_pnl, "reason": reason}
        except Exception:
            try:
                client.orders.close_position({"marketSlug": slug})
                return {"ok": True, "mode": "LIVE", "exit_price": safe_exit, "pnl": pnl, "reason": f"{reason}_FALLBACK"}
            except Exception as e2:
                return {"ok": False, "error": str(e2)}