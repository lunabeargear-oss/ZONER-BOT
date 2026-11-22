import time
import httpx


class BTCAnalytics:
    COINBASE_TICKER = "https://api.exchange.coinbase.com/products/BTC-USD/ticker"
    COINBASE_CANDLES = "https://api.exchange.coinbase.com/products/BTC-USD/candles"

    def __init__(self):
        self._last_spot = 0.0
        self._last_matrix = {}
        self._last_matrix_ts = 0.0

    def fetch_spot(self) -> float:
        try:
            with httpx.Client(timeout=4.5) as client:
                r = client.get(self.COINBASE_TICKER)
                if r.status_code == 200:
                    price = float(r.json().get("price") or 0)
                    if price > 0:
                        self._last_spot = price
                        return price
        except Exception:
            pass
        return self._last_spot or 90000.0

    @staticmethod
    def _calc_rsi(closes: list[float], period: int = 14) -> float:
        if len(closes) < 3:
            return 50.0
        gains, losses = [], []
        for i in range(1, len(closes)):
            diff = closes[i] - closes[i - 1]
            gains.append(max(0.0, diff))
            losses.append(max(0.0, -diff))
        window = min(period, len(gains))
        avg_gain = sum(gains[-window:]) / window
        avg_loss = sum(losses[-window:]) / window
        if avg_loss == 0:
            return 75.0 if avg_gain > 0 else 50.0
        rs = avg_gain / avg_loss
        return round(100.0 - (100.0 / (1.0 + rs)), 1)

    def _fetch_timeframe(self, client: httpx.Client, granularity: int, lookback_bars: int) -> dict:
        try:
            r = client.get(self.COINBASE_CANDLES, params={"granularity": granularity})
            if r.status_code != 200:
                return {}
            rows = r.json()
            if not isinstance(rows, list) or len(rows) < 2:
                return {}
            # Coinbase returns [time, low, high, open, close, volume] newest first
            bars = list(reversed(rows[: max(20, lookback_bars + 2)]))
            recent = bars[-lookback_bars:]
            start_open = float(recent[0][3])
            end_close = float(recent[-1][4])
            high = max(float(b[2]) for b in recent)
            low = min(float(b[1]) for b in recent)
            closes = [float(b[4]) for b in bars]
            change_pct = ((end_close - start_open) / start_open) if start_open > 0 else 0.0
            range_pct = ((high - low) / low) if low > 0 else 0.0
            rsi = self._calc_rsi(closes)
            bias = "UP" if change_pct > 0.0004 else ("DOWN" if change_pct < -0.0004 else "FLAT")
            return {
                "change_pct": round(change_pct * 100.0, 3),
                "range_pct": round(range_pct * 100.0, 3),
                "high": round(high, 2),
                "low": round(low, 2),
                "rsi": rsi,
                "bias": bias,
            }
        except Exception:
            return {}

    def get_variation_matrix(self, force: bool = False) -> dict:
        now = time.time()
        if not force and self._last_matrix and (now - self._last_matrix_ts) < 20:
            return self._last_matrix

        matrix = {
            "5m": {"change_pct": 0.0, "range_pct": 0.0, "rsi": 50.0, "bias": "FLAT"},
            "15m": {"change_pct": 0.0, "range_pct": 0.0, "rsi": 50.0, "bias": "FLAT"},
            "1h": {"change_pct": 0.0, "range_pct": 0.0, "rsi": 50.0, "bias": "FLAT"},
            "1d": {"change_pct": 0.0, "range_pct": 0.0, "rsi": 50.0, "bias": "FLAT"},
        }
        try:
            with httpx.Client(timeout=5.0) as client:
                m5 = self._fetch_timeframe(client, 60, 5)
                m15 = self._fetch_timeframe(client, 300, 3)
                m1h = self._fetch_timeframe(client, 900, 4)
                m1d = self._fetch_timeframe(client, 3600, 24)
                if m5:
                    matrix["5m"] = m5
                if m15:
                    matrix["15m"] = m15
                if m1h:
                    matrix["1h"] = m1h
                if m1d:
                    matrix["1d"] = m1d
        except Exception:
            pass

        up_votes = sum(1 for tf in ("5m", "15m", "1h", "1d") if matrix[tf].get("bias") == "UP")
        down_votes = sum(1 for tf in ("5m", "15m", "1h", "1d") if matrix[tf].get("bias") == "DOWN")
        matrix["confluence"] = {
            "up_votes": up_votes,
            "down_votes": down_votes,
            "direction": "UP" if up_votes > down_votes else ("DOWN" if down_votes > up_votes else "NEUTRAL"),
            "agreement_pct": round(max(up_votes, down_votes) / 4.0 * 100.0, 0),
        }
        self._last_matrix = matrix
        self._last_matrix_ts = now
        return matrix

    @staticmethod
    def evaluate_signal(spot: float, spot_history: list[dict], matrix: dict, min_spot_move_pct: float) -> dict:
        ref = spot_history[0]["price"] if spot_history else spot
        move_pct = (spot - ref) / ref if ref > 0 else 0.0
        abs_move = abs(move_pct)
        side = "UP" if move_pct >= 0 else "DOWN"

        # Base momentum score + multi-timeframe confluence bonus
        base_score = 0.50 + min(0.32, (abs_move / max(0.0004, min_spot_move_pct)) * 0.14)
        conf = matrix.get("confluence", {})
        aligned = conf.get("up_votes", 0) if side == "UP" else conf.get("down_votes", 0)
        confluence_bonus = aligned * 0.035

        # Penalize extreme RSI exhaustion
        rsi_15m = float(matrix.get("15m", {}).get("rsi") or 50.0)
        rsi_adj = -0.04 if ((side == "UP" and rsi_15m > 74) or (side == "DOWN" and rsi_15m < 26)) else 0.0

        p_win = max(0.45, min(0.82, base_score + confluence_bonus + rsi_adj))
        return {
            "side": side,
            "move_pct": move_pct,
            "abs_move": abs_move,
            "score": round(p_win, 3),
            "p_win": round(p_win, 3),
            "aligned_timeframes": aligned,
            "rsi_15m": rsi_15m,
        }
