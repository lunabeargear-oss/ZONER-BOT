import time
from datetime import datetime
import httpx


def _px(val, default=None):
    if val is None:
        return default
    if isinstance(val, dict):
        val = val.get("value") or val.get("price")
    try:
        return float(val)
    except Exception:
        return default


class MarketDiscovery:
    def __init__(self, gateway_host: str = "https://gateway.polymarket.us"):
        self.gateway_host = gateway_host.rstrip("/")
        self._cache = {}
        self._cache_ts = 0.0

    def find_active_btc_market(self, timeframe: str = "5m"):
        now = time.time()
        key = timeframe.lower()
        if key in self._cache and now - self._cache_ts < 15:
            cached = self._cache[key]
            if cached["end_ts"] > now:
                cached["seconds_left"] = int(cached["end_ts"] - now)
                return cached

        queries = ["Bitcoin Up or Down", "BTC Up or Down"]
        candidates = []
        with httpx.Client(timeout=8.0) as client:
            for q in queries:
                try:
                    r = client.get(f"{self.gateway_host}/v1/search", params={"query": q})
                    if r.status_code != 200:
                        continue
                    events = r.json().get("events") or []
                    for ev in events:
                        slug = ev.get("slug") or ""
                        if not ev.get("active") or ev.get("closed"):
                            continue
                        if "btc" not in slug.lower() or "updown" not in slug.lower():
                            continue
                        if key == "5m" and "-5m-" not in slug:
                            continue
                        if key == "15m" and "-15m-" not in slug:
                            continue
                        end_str = ev.get("endDate")
                        end_ts = self._parse_iso(end_str)
                        if end_ts and end_ts > now:
                            candidates.append({
                                "slug": slug if slug.startswith("cpc-") else f"cpc-{slug}",
                                "raw_slug": slug,
                                "title": ev.get("title", slug),
                                "end_ts": end_ts,
                                "seconds_left": int(end_ts - now),
                            })
                except Exception:
                    continue

        candidates.sort(key=lambda m: m["end_ts"])
        result = candidates[0] if candidates else None
        if result:
            self._cache[key] = result
            self._cache_ts = now
        return result

    def fetch_book(self, slug: str):
        with httpx.Client(timeout=6.0) as client:
            r = client.get(f"{self.gateway_host}/v1/markets/{slug}/book")
            r.raise_for_status()
            data = r.json()

        md = data.get("marketData") or data
        asks = md.get("offers") or (md.get("offers") or md.get("asks")) or data.get("yes", {}).get("asks") or []
        bids = md.get("bids") or data.get("yes", {}).get("bids") or []

        best_ask = _px(asks[0].get("px") if isinstance(asks[0], dict) and "px" in asks[0] else asks[0].get("price") if isinstance(asks[0], dict) else asks[0], 0.52) if asks else 0.52
        ask_depth = float(asks[0].get("qty") or asks[0].get("size") or 100) if asks and isinstance(asks[0], dict) else (100.0 if asks else 0.0)
        best_bid = _px(bids[0].get("px") if isinstance(bids[0], dict) and "px" in bids[0] else bids[0].get("price") if isinstance(bids[0], dict) else bids[0], max(0.01, best_ask - 0.02)) if bids else max(0.01, best_ask - 0.02)

        spread = round(max(0.0, best_ask - best_bid), 4)
        down_ask = round(max(0.01, min(0.99, 1.0 - best_bid)), 4)
        down_bid = round(max(0.01, min(0.99, 1.0 - best_ask)), 4)
        return {
            "up_ask": round(best_ask, 4),
            "up_bid": round(best_bid, 4),
            "down_ask": down_ask,
            "down_bid": down_bid,
            "spread": spread,
            "ask_depth": ask_depth,
            "book_valid": bool(asks or bids),
        }

    def fetch_settlement(self, slug: str):
        try:
            with httpx.Client(timeout=5.0) as client:
                r = client.get(f"{self.gateway_host}/v1/markets/{slug}/settlement")
                if r.status_code == 200:
                    data = r.json()
                    val = data.get("settlement")
                    if val is not None:
                        return float(val)
        except Exception:
            pass
        return None

    @staticmethod
    def _parse_iso(s: str):
        if not s:
            return None
        try:
            dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
            return dt.timestamp()
        except Exception:
            return None
