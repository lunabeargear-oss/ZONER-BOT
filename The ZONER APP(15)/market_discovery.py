import time
from datetime import datetime, timezone
import httpx


class MarketDiscovery:
    def __init__(self, *args, gateway_host: str = "https://api.polymarket.us"):
        # Accept optional positional args safely if called as MarketDiscovery(cfg)
        self.gateway_host = gateway_host.rstrip("/")

    @staticmethod
    def _parse_iso_ts(val) -> float:
        if not val:
            return 0.0
        if isinstance(val, (int, float)):
            return float(val)
        try:
            s = str(val).replace("Z", "+00:00")
            return datetime.fromisoformat(s).timestamp()
        except Exception:
            return 0.0

    @staticmethod
    def _extract_tf(slug: str, title: str) -> str:
        text = f"{slug} {title}".lower()
        if "5m" in text or "5-min" in text or "5 min" in text:
            return "5m"
        if "15m" in text or "15-min" in text or "15 min" in text:
            return "15m"
        if "1h" in text or "hourly" in text or "1 hour" in text:
            return "1h"
        if "1d" in text or "daily" in text or "24h" in text:
            return "1d"
        return "other"

    def find_active_btc_market(self, target_timeframe: str = "all") -> dict | None:
        tf_req = (target_timeframe or "all").lower()
        if tf_req == "auto":
            tf_req = "all"

        now = time.time()
        candidates = []
        try:
            with httpx.Client(timeout=6.0) as client:
                r = client.get(
                    f"{self.gateway_host}/v1/markets",
                    params={"closed": "false", "active": "true", "limit": 100},
                )
                if r.status_code != 200:
                    return None
                payload = r.json()
                markets = payload.get("markets") if isinstance(payload, dict) else payload
                if not isinstance(markets, list):
                    return None

                for m in markets:
                    slug = str(m.get("slug") or "")
                    title = str(m.get("question") or m.get("title") or slug)
                    combined = f"{slug} {title}".lower()
                    if "btc" not in combined and "bitcoin" not in combined:
                        continue
                    if "up" not in combined and "down" not in combined and "updown" not in combined:
                        continue

                    end_ts = self._parse_iso_ts(m.get("endDate") or m.get("windowEnd") or m.get("closeTime"))
                    if end_ts <= now + 5:
                        continue
                    tf = self._extract_tf(slug, title)
                    if tf_req != "all" and tf != tf_req:
                        continue

                    candidates.append({
                        "slug": slug,
                        "title": title,
                        "timeframe": tf,
                        "end_ts": end_ts,
                        "seconds_left": int(max(0, end_ts - now)),
                    })
        except Exception:
            return None

        if not candidates:
            return None

        # Prefer shortest active window with >= 30s left
        viable = [c for c in candidates if c["seconds_left"] >= 30] or candidates
        viable.sort(key=lambda x: x["seconds_left"])
        return viable[0]

    @staticmethod
    def _parse_level(lvl) -> tuple[float, float]:
        if isinstance(lvl, dict):
            px_raw = lvl.get("price") or lvl.get("px") or lvl.get("p") or 0
            if isinstance(px_raw, dict):
                px_raw = px_raw.get("value") or 0
            sz_raw = (
                lvl.get("size")
                or lvl.get("quantity")
                or lvl.get("qty")
                or lvl.get("amount")
                or lvl.get("shares")
                or 0
            )
            return float(px_raw or 0), float(sz_raw or 0)
        if isinstance(lvl, (list, tuple)) and len(lvl) >= 2:
            return float(lvl[0] or 0), float(lvl[1] or 0)
        return 0.0, 0.0

    def fetch_book(self, slug: str) -> dict:
        fallback = {
            "up_ask": 0.52,
            "up_bid": 0.48,
            "down_ask": 0.52,
            "down_bid": 0.48,
            "spread": 0.04,
            "ask_depth": 0.0,
            "bid_depth": 0.0,
            "book_source": "Fallback",
        }
        if not slug:
            return fallback

        try:
            with httpx.Client(timeout=5.0) as client:
                r = client.get(f"{self.gateway_host}/v1/markets/{slug}/book")
                if r.status_code != 200:
                    return fallback
                data = r.json()
                book = data.get("book") if isinstance(data, dict) and "book" in data else data
                bids = book.get("bids") or book.get("yesBids") or []
                asks = book.get("asks") or book.get("yesAsks") or []

                parsed_bids = [self._parse_level(b) for b in bids]
                parsed_asks = [self._parse_level(a) for a in asks]
                parsed_bids = [(p, s) for p, s in parsed_bids if 0 < p < 1]
                parsed_asks = [(p, s) for p, s in parsed_asks if 0 < p < 1]

                if not parsed_bids and not parsed_asks:
                    return fallback

                best_bid = max((p for p, _ in parsed_bids), default=0.48)
                best_ask = min((p for p, _ in parsed_asks), default=0.52)
                if best_ask < best_bid:
                    best_ask = min(0.99, round(best_bid + 0.02, 2))

                ask_depth = round(sum(s for _, s in parsed_asks[:5]), 2)
                bid_depth = round(sum(s for _, s in parsed_bids[:5]), 2)
                spread = round(max(0.0, best_ask - best_bid), 4)

                return {
                    "up_ask": round(best_ask, 2),
                    "up_bid": round(best_bid, 2),
                    "down_ask": round(max(0.01, 1.0 - best_bid), 2),
                    "down_bid": round(max(0.01, 1.0 - best_ask), 2),
                    "spread": spread,
                    "ask_depth": ask_depth,
                    "bid_depth": bid_depth,
                    "book_source": "Live Book",
                }
        except Exception:
            return fallback

    def fetch_settlement(self, slug: str) -> float | None:
        if not slug:
            return None
        try:
            with httpx.Client(timeout=5.0) as client:
                r = client.get(f"{self.gateway_host}/v1/markets/{slug}")
                if r.status_code != 200:
                    return None
                m = r.json()
                if isinstance(m, dict) and "market" in m:
                    m = m["market"]
                prices = m.get("outcomePrices")
                if isinstance(prices, list) and prices:
                    return float(prices[0])
        except Exception:
            pass
        return None
