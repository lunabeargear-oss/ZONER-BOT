import csv
import os
from datetime import datetime

try:
    from polymarket_us import PolymarketUS
except ImportError:
    PolymarketUS = None


class PolymarketExecutor:
    def __init__(self, cfg):
        self.cfg = cfg
        self.client = None

    def _ensure_client(self):
        if PolymarketUS is None or not self.cfg.keys_configured():
            self.client = None
            return None
        if self.client is None:
            self.client = PolymarketUS(
                key_id=self.cfg.polymarket_key_id,
                secret_key=self.cfg.polymarket_secret_key,
            )
        return self.client

    def reset_client(self):
        self.client = None

    def get_account_snapshot(self) -> dict:
        if PolymarketUS is None or not self.cfg.keys_configured():
            return {
                "connected": False,
                "cash_usd": 300.0 if self.cfg.dry_run else 0.0,
                "equity_usd": 300.0 if self.cfg.dry_run else 0.0,
                "positions": [],
                "error": "API keys missing" if not self.cfg.keys_configured() else "SDK missing",
            }
        try:
            self._ensure_client()
            bal_resp = self.client.account.balances() or {}
            balances = bal_resp.get("balances") if isinstance(bal_resp, dict) else bal_resp
            cash = 0.0
            equity = 0.0
            if isinstance(balances, list):
                for b in balances:
                    cur = str(b.get("currency") or "USD").upper()
                    if cur in ("USD", "USDC"):
                        cash += float(b.get("availableBalance") or b.get("buyingPower") or b.get("cashBalance") or b.get("balance") or 0)
                        equity += float(b.get("totalBalance") or b.get("portfolioValue") or b.get("balance") or cash)
            elif isinstance(balances, dict):
                cash = float(balances.get("availableBalance") or balances.get("buyingPower") or balances.get("cashBalance") or 0)
                equity = float(balances.get("totalBalance") or balances.get("portfolioValue") or cash)

            pos_resp = self.client.portfolio.positions() or {}
            raw_pos = pos_resp.get("positions") if isinstance(pos_resp, dict) else []
            return {
                "connected": True,
                "cash_usd": round(cash, 2),
                "equity_usd": round(max(cash, equity), 2),
                "positions": raw_pos if isinstance(raw_pos, list) else [],
                "error": "",
            }
        except Exception as exc:
            return {
                "connected": False,
                "cash_usd": 0.0,
                "equity_usd": 0.0,
                "positions": [],
                "error": str(exc),
            }

    def _get_live_bbo_price(self, slug: str, side: str, fallback_price: float) -> float:
        try:
            self._ensure_client()
            bbo = self.client.markets.bbo(slug) or {}
            best_ask = bbo.get("bestAsk") or bbo.get("ask") or {}
            best_bid = bbo.get("bestBid") or bbo.get("bid") or {}
            if isinstance(best_ask, dict):
                best_ask = best_ask.get("value") or 0
            if isinstance(best_bid, dict):
                best_bid = best_bid.get("value") or 0
            if side == "UP" and float(best_ask or 0) > 0:
                return round(float(best_ask), 2)
            if side == "DOWN" and float(best_bid or 0) > 0:
                return round(float(best_bid), 2)
        except Exception:
            pass
        return round(fallback_price if side == "UP" else (1.0 - fallback_price), 2)

    def place_order(self, slug: str, side: str, price: float, stake_usd: float, spot_price: float, manual: bool = False):
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        stake = min(float(stake_usd), float(self.cfg.max_stake_usd))
        shares = round(stake / max(0.05, price), 4)

        if self.cfg.dry_run:
            status = "MANUAL_PAPER" if manual else "PAPER_EXECUTED"
            self._append_csv("paper_trades.csv", [ts, slug, side, price, spot_price, stake, "PAPER", status])
            return {"ok": True, "mode": "PAPER", "status": status, "stake": stake, "price": price, "shares": shares}

        if PolymarketUS is None:
            return {"ok": False, "mode": "LIVE", "status": "FAILED: polymarket-us not installed"}
        if not self.cfg.keys_configured():
            return {"ok": False, "mode": "LIVE", "status": "FAILED: API keys missing"}
        self._ensure_client()

        intent = "ORDER_INTENT_BUY_LONG" if side == "UP" else "ORDER_INTENT_BUY_SHORT"
        order_price = self._get_live_bbo_price(slug, side, price)
        order_price = round(order_price + 0.01, 2) if side == "UP" else round(order_price - 0.01, 2)
        order_price = max(0.01, min(0.99, order_price))
        display_price = order_price if side == "UP" else round(1.0 - order_price, 2)
        qty = max(1, int(stake / max(0.05, display_price)))

        try:
            resp = self.client.orders.create({
                "marketSlug": slug,
                "intent": intent,
                "type": "ORDER_TYPE_LIMIT",
                "price": {"value": f"{order_price:.2f}", "currency": "USD"},
                "quantity": qty,
                "tif": "TIME_IN_FORCE_IMMEDIATE_OR_CANCEL",
            })
            order_obj = resp.get("order") or resp
            order_id = resp.get("id") or order_obj.get("id")
            executions = resp.get("executions") or order_obj.get("executions") or []
            order_state = order_obj.get("state") or order_obj.get("status") or ""
            cum_qty = float(order_obj.get("cumQuantity") or order_obj.get("cumQty") or order_obj.get("filledQuantity") or order_obj.get("filledQty") or 0)

            if order_id and (not executions and cum_qty <= 0 and "FILLED" not in str(order_state).upper()):
                try:
                    fetched = self.client.orders.retrieve(order_id) or {}
                    f_obj = fetched.get("order") or fetched
                    order_state = f_obj.get("state") or f_obj.get("status") or order_state
                    executions = fetched.get("executions") or f_obj.get("executions") or executions
                    cum_qty = max(cum_qty, float(f_obj.get("cumQuantity") or f_obj.get("cumQty") or f_obj.get("filledQuantity") or f_obj.get("filledQty") or 0))
                except Exception:
                    pass

            filled = bool(executions) or cum_qty > 0 or ("FILLED" in str(order_state).upper())
            if filled:
                order_state = "ORDER_STATE_FILLED"
            elif not order_state or "CANCELED" in str(order_state).upper():
                order_state = "ORDER_STATE_CANCELED (UNFILLED)"

            self._append_csv("live_trades.csv", [ts, slug, side, display_price, spot_price, stake, "LIVE", order_state])
            return {
                "ok": filled,
                "mode": "LIVE",
                "status": order_state,
                "stake": stake,
                "price": display_price,
                "shares": qty,
                "raw": resp,
            }
        except Exception as exc:
            status = f"FAILED: {exc}"
            self._append_csv("live_trades.csv", [ts, slug, side, display_price, spot_price, stake, "LIVE", status])
            return {"ok": False, "mode": "LIVE", "status": status}

    def close_scalp_position(
        self,
        slug: str,
        side: str,
        exit_price: float,
        quantity: float,
        entry_price: float,
        stake_usd: float,
        spot_price: float,
        reason: str = "TAKE_PROFIT",
    ):
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        pnl_usd = round(((exit_price - entry_price) / max(0.01, entry_price)) * stake_usd, 2)
        qty_int = max(1, int(round(quantity)))

        if self.cfg.dry_run:
            status = f"PAPER_SCALP_{reason} ({pnl_usd:+.2f})"
            self._append_csv("paper_trades.csv", [ts, slug, f"SELL_{side}", exit_price, spot_price, stake_usd, "PAPER", status])
            return {"ok": True, "mode": "PAPER", "status": status, "pnl_usd": pnl_usd, "exit_price": exit_price}

        if PolymarketUS is None or not self.cfg.keys_configured():
            return {"ok": False, "mode": "LIVE", "status": "FAILED: SDK/keys unavailable", "pnl_usd": 0.0}
        self._ensure_client()

        intent = "ORDER_INTENT_SELL_LONG" if side == "UP" else "ORDER_INTENT_SELL_SHORT"
        order_price = round(exit_price if side == "UP" else (1.0 - exit_price), 2)
        order_price = max(0.01, min(0.99, order_price))
        try:
            resp = self.client.orders.create({
                "marketSlug": slug,
                "intent": intent,
                "type": "ORDER_TYPE_LIMIT",
                "price": {"value": f"{order_price:.2f}", "currency": "USD"},
                "quantity": qty_int,
                "tif": "TIME_IN_FORCE_IMMEDIATE_OR_CANCEL",
            })
            order_state = resp.get("state") or resp.get("status") or "SCALP_CLOSED"
            if "CANCELED" in str(order_state).upper() and reason in ("STOP_LOSS", "MAX_HOLD", "MANUAL"):
                resp = self.client.orders.close_position({"marketSlug": slug})
                order_state = resp.get("state") or resp.get("status") or "SCALP_MARKET_CLOSED"
            status = f"LIVE_SCALP_{reason} ({pnl_usd:+.2f})"
            self._append_csv("live_trades.csv", [ts, slug, f"SELL_{side}", exit_price, spot_price, stake_usd, "LIVE", status])
            return {"ok": True, "mode": "LIVE", "status": status, "pnl_usd": pnl_usd, "exit_price": exit_price, "raw": resp}
        except Exception as exc:
            try:
                resp = self.client.orders.close_position({"marketSlug": slug})
                status = f"LIVE_SCALP_{reason}_CLOSE ({pnl_usd:+.2f})"
                self._append_csv("live_trades.csv", [ts, slug, f"SELL_{side}", exit_price, spot_price, stake_usd, "LIVE", status])
                return {"ok": True, "mode": "LIVE", "status": status, "pnl_usd": pnl_usd, "exit_price": exit_price, "raw": resp}
            except Exception as exc2:
                status = f"SCALP_FAILED: {exc} / {exc2}"
                self._append_csv("live_trades.csv", [ts, slug, f"SELL_{side}", exit_price, spot_price, stake_usd, "LIVE", status])
                return {"ok": False, "mode": "LIVE", "status": status, "pnl_usd": 0.0}

    @staticmethod
    def _append_csv(filename: str, row: list):
        base_dir = os.path.dirname(os.path.abspath(__file__))
        fpath = os.path.join(base_dir, filename)
        exists = os.path.exists(fpath)
        with open(fpath, "a", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            if not exists:
                w.writerow(["timestamp", "slug", "side", "price", "spot", "stake_usd", "mode", "status"])
            w.writerow(row)
