import math
import time
import httpx


def _compute_rsi(closes, period=14):
    if len(closes) < 3:
        return 50.0
    deltas = [closes[i] - closes[i - 1] for i in range(1, len(closes))]
    recent = deltas[-period:] if len(deltas) >= period else deltas
    gains = [d for d in recent if d > 0]
    losses = [-d for d in recent if d < 0]
    avg_gain = sum(gains) / max(1, len(recent))
    avg_loss = sum(losses) / max(1, len(recent))
    if avg_loss == 0:
        return 100.0 if avg_gain > 0 else 50.0
    rs = avg_gain / avg_loss
    return round(100.0 - (100.0 / (1.0 + rs)), 1)


def _summarize_candles(candles_asc, lookback_bars, tf_label):
    if not candles_asc or len(candles_asc) < 2:
        return None
    window = candles_asc[-lookback_bars:] if len(candles_asc) >= lookback_bars else candles_asc
    start_price = float(window[0][3] or window[0][4])
    end_price = float(window[-1][4])
    high_price = max(float(c[2]) for c in window)
    low_price = min(float(c[1]) for c in window)
    if start_price <= 0 or low_price <= 0:
        return None
    change_usd = end_price - start_price
    change_pct = change_usd / start_price
    range_pct = (high_price - low_price) / low_price
    closes = [float(c[4]) for c in window]
    rets = [(closes[i] - closes[i - 1]) / closes[i - 1] for i in range(1, len(closes)) if closes[i - 1] > 0]
    if rets:
        mean_r = sum(rets) / len(rets)
        var_r = sum((r - mean_r) ** 2 for r in rets) / len(rets)
        vol_pct = math.sqrt(var_r)
    else:
        vol_pct = 0.0
    rsi = _compute_rsi([float(c[4]) for c in candles_asc])
    abs_move = abs(change_pct)
    scale_map = {"5m": 0.0020, "15m": 0.0035, "1h": 0.0070, "1d": 0.0180}
    denom = scale_map.get(tf_label, 0.003)
    raw_conf = min(0.96, 0.42 + (abs_move / denom) * 0.45)
    if rsi > 65 and change_pct > 0:
        raw_conf = min(0.98, raw_conf + 0.06)
    elif rsi < 35 and change_pct < 0:
        raw_conf = min(0.98, raw_conf + 0.06)
    direction = "UP" if change_pct >= 0 else "DOWN"
    return {
        "timeframe": tf_label,
        "change_pct": round(change_pct * 100, 3),
        "change_ratio": round(change_pct, 6),
        "change_usd": round(change_usd, 2),
        "high": round(high_price, 2),
        "low": round(low_price, 2),
        "range_pct": round(range_pct * 100, 3),
        "volatility_pct": round(vol_pct * 100, 3),
        "rsi": rsi,
        "direction": direction,
        "confidence": round(raw_conf, 3),
    }


class BTCAnalytics:
    def __init__(self):
        self._cache_ts = 0.0
        self._cached_variation = {}
        self.spot_history = []

    def record_spot(self, price: float):
        now = time.time()
        if price and price > 0:
            self.spot_history.append((now, float(price)))
            cutoff = now - 86400
            self.spot_history = [(t, p) for t, p in self.spot_history if t >= cutoff][-2000:]

    def _fetch_coinbase_candles(self, granularity_sec: int):
        url = "https://api.exchange.coinbase.com/products/BTC-USD/candles"
        with httpx.Client(timeout=5.0) as client:
            resp = client.get(url, params={"granularity": granularity_sec})
            if resp.status_code != 200:
                return []
            data = resp.json()
            if not isinstance(data, list):
                return []
            data.sort(key=lambda row: row[0])
            return data

    def _fallback_from_spot_history(self, window_sec: int, tf_label: str, current_spot: float):
        if not self.spot_history or current_spot <= 0:
            return {
                "timeframe": tf_label, "change_pct": 0.0, "change_ratio": 0.0,
                "change_usd": 0.0, "high": round(current_spot, 2), "low": round(current_spot, 2),
                "range_pct": 0.0, "volatility_pct": 0.0, "rsi": 50.0,
                "direction": "UP", "confidence": 0.45,
            }
        now = time.time()
        pts = [p for t, p in self.spot_history if t >= now - window_sec]
        if not pts:
            pts = [self.spot_history[0][1], current_spot]
        start_p = pts[0]
        high_p = max(pts + [current_spot])
        low_p = min(pts + [current_spot])
        chg_usd = current_spot - start_p
        chg_pct = chg_usd / start_p if start_p > 0 else 0.0
        rng_pct = (high_p - low_p) / low_p if low_p > 0 else 0.0
        return {
            "timeframe": tf_label, "change_pct": round(chg_pct * 100, 3),
            "change_ratio": round(chg_pct, 6), "change_usd": round(chg_usd, 2),
            "high": round(high_p, 2), "low": round(low_p, 2),
            "range_pct": round(rng_pct * 100, 3), "volatility_pct": round(rng_pct * 35, 3),
            "rsi": 50.0, "direction": "UP" if chg_pct >= 0 else "DOWN",
            "confidence": round(min(0.90, 0.45 + abs(chg_pct) * 120), 3),
        }

    def get_multi_timeframe_variation(self, current_spot: float):
        now = time.time()
        if now - self._cache_ts < 20.0 and self._cached_variation:
            return self._cached_variation
        variation = {}
        try:
            candles_1m = self._fetch_coinbase_candles(60)
            candles_5m = self._fetch_coinbase_candles(300)
            candles_1h = self._fetch_coinbase_candles(3600)
        except Exception:
            candles_1m, candles_5m, candles_1h = [], [], []
        v_5m = _summarize_candles(candles_1m, 5, "5m") or self._fallback_from_spot_history(300, "5m", current_spot)
        v_15m = _summarize_candles(candles_1m, 15, "15m") or self._fallback_from_spot_history(900, "15m", current_spot)
        v_1h = _summarize_candles(candles_5m, 12, "1h") or self._fallback_from_spot_history(3600, "1h", current_spot)
        v_1d = _summarize_candles(candles_1h, 24, "1d") or self._fallback_from_spot_history(86400, "1d", current_spot)
        variation["5m"] = v_5m
        variation["15m"] = v_15m
        variation["1h"] = v_1h
        variation["1d"] = v_1d
        avg_conf = sum(v["confidence"] for v in variation.values()) / 4.0
        weighted_chg = (v_5m["change_ratio"] * 0.40 + v_15m["change_ratio"] * 0.30 + v_1h["change_ratio"] * 0.20 + v_1d["change_ratio"] * 0.10)
        variation["all"] = {
            "timeframe": "all", "change_pct": round(weighted_chg * 100, 3),
            "change_ratio": round(weighted_chg, 6), "change_usd": v_1h["change_usd"],
            "high": v_1d["high"], "low": v_1d["low"], "range_pct": v_1d["range_pct"],
            "volatility_pct": round((v_5m["volatility_pct"] + v_15m["volatility_pct"] + v_1h["volatility_pct"]) / 3.0, 3),
            "rsi": v_15m["rsi"], "direction": "UP" if weighted_chg >= 0 else "DOWN",
            "confidence": round(avg_conf, 3),
        }
        self._cached_variation = variation
        self._cache_ts = now
        return variation
        return 50.0
