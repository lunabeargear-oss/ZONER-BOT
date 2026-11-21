import csv
import os
import time
from datetime import datetime

try:
    from polymarket_us import PolymarketUS
except ImportError:
    PolymarketUS = None


def _px_val(val, default: float = 0.0) -> float:
    if isinstance(val, dict):
        val = val.get("value", default)
    try:
        return float(val)
    except Exception:
        return float(default)


class TradeExecutor:
    def __init__(self, cfg):
        self.cfg = cfg
        self.client = None

    def _ensure_client(self):
        if self.client is None and PolymarketUS is not None and self.cfg.keys_configured():
            self.client = PolymarketUS(
                key_id=self.cfg.polymarket_key_id,
                secret_key=self.cfg.polymarket_secret_key,
            )

    def get_account_snapshot(self):
        if self.cfg.dry_run:
            return {"ok": True, "balance_usd": None, "positions": {}}
        if PolymarketUS is None or not self.cfg.keys_configured():
            return {"ok": False, "balance_usd": None, "positions": {}}
        try:
            self._ensure_client()
            bal_resp = self.client.account.balances() or {}
            balances = bal_resp.get("balances") or []
            balance_usd = None
            if balances:
                balance_usd = _px_val(balances[0].get("currentBalance"), 0.0)
            pos_resp = self.client.portfolio.positions() or {}
            positions = pos_resp.get("positions") or {}
            return {"ok": True, "balance_usd": balance_usd, "positions": positions}
        except Exception:
            return {"ok": False, "balance_usd": None, "positions": {}}

    def _get_live_bbo_price(self, slug: str, side: str, fallback_price: float) -> float:
        try:
            self._ensure_client()
            if self.client is not None:
                bbo = self.client.markets.bbo(slug) or {}
                md = bbo.get("marketData") or bbo
                if side == "UP":
                    ask = _px_val(md.get("bestAsk"), 0.0)
                    if ask > 0:
                        return round(ask, 2)
                else:
                    bid = _px_val(md.get("bestBid"), 0.0)
                    if bid > 0:
                        return round(bid, 2)
        except Exception:
            pass
        return round(fallback_price if side == "UP" else (1.0 - fallback_price), 2)

    def _poll_order_fill(self, resp: dict, fallback_order_price: float):
        order_obj = resp.get("order") or resp
        order_id = resp.get("id") or order_obj.get("id")
        executions = resp.get("executions") or order_obj.get("executions") or []
        order_state = order_obj.get("state") or order_obj.get("status") or ""
        cum_qty = float(
            order_obj.get("cumQuantity")
            or order_obj.get("cumQty")
            or order_obj.get("filledQuantity")
            or order_obj.get("filledQty")
            or 0
        )
        avg_px = _px_val(order_obj.get("avgPx"), 0.0)

        # Polymarket US matching can take ~150-500ms to reflect in orders.retrieve()
        if order_id and not executions and cum_qty <= 0 and "FILLED" not in str(order_state).upper():
            for _ in range(3):
                time.sleep(0.25)
                try:
                    fetched = self.client.orders.retrieve(order_id) or {}
                    f_obj = fetched.get("order") or fetched
                    order_state = f_obj.get("state") or f_obj.get("status") or order_state
                    executions = fetched.get("executions") or f_obj.get("executions") or executions
                    cum_qty = max(
                        cum_qty,
                        float(
                            f_obj.get("cumQuantity")
                            or f_obj.get("cumQty")
                            or f_obj.get("filledQuantity")
                            or f_obj.get("filledQty")
                            or 0
                        ),
                    )
                    f_avg = _px_val(f_obj.get("avgPx"), 0.0)
                    if f_avg > 0:
                        avg_px = f_avg
                    if executions or cum_qty > 0 or "FILLED" in str(order_state).upper():
                        break
                except Exception:
                    break

        filled = bool(executions) or cum_qty > 0 or ("FILLED" in str(order_state).upper())
        if avg_px <= 0 and executions and isinstance(executions[0], dict):
            avg_px = _px_val(executions[0].get("lastPx") or executions[0].get("px"), 0.0)
        if avg_px <= 0:
            avg_px = fallback_order_price
        return filled, order_state, cum_qty, avg_px

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
        # 1-cent marketable buffer so IOC crosses live resting liquidity
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
            filled, order_state, cum_qty, avg_px = self._poll_order_fill(resp, order_price)
            fill_display_price = round(avg_px if side == "UP" else (1.0 - avg_px), 4)
            fill_shares = cum_qty if cum_qty > 0 else qty

            if filled:
                order_state = "ORDER_STATE_FILLED"
            elif not order_state or "CANCELED" in str(order_state).upper():
                order_state = "ORDER_STATE_CANCELED (UNFILLED)"

            self._append_csv("live_trades.csv", [ts, slug, side, fill_display_price, spot_price, stake, "LIVE", order_state])
            return {
                "ok": filled,
                "mode": "LIVE",
                "status": order_state,
                "stake": round(fill_shares * fill_display_price, 2) if filled else stake,
                "price": fill_display_price,
                "shares": fill_shares,
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
        base_order_price = exit_price if side == "UP" else (1.0 - exit_price)
        # 1-cent marketable buffer to ensure IOC sell fills immediately
        order_price = round(base_order_price - 0.01, 2) if side == "UP" else round(base_order_price + 0.01, 2)
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
            filled, order_state, cum_qty, avg_px = self._poll_order_fill(resp, order_price)
            if not filled:
                # Fallback to close_position for ANY unfilled IOC exit (not just STOP_LOSS)
                resp = self.client.orders.close_position({"marketSlug": slug})
                status = f"LIVE_SCALP_{reason}_CLOSE ({pnl_usd:+.2f})"
            else:
                actual_exit = round(avg_px if side == "UP" else (1.0 - avg_px), 4)
                pnl_usd = round(((actual_exit - entry_price) / max(0.01, entry_price)) * stake_usd, 2)
                exit_price = actual_exit
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
