    lunabeargear-oss
    ZONER-BOT

Repository navigation

    Code
    Issues
    Pull requests
    Agents
    Actions
    Projects
    Wiki
    Security and quality
    Insights
    Settings

    ZONER-BOT

/
in
The-ZONER-APP(15)

    Edit
    Preview

Indent mode
Indent size
Line wrap mode
Editing market_discovery.py file contents
  1
  2
  3
  4
  5
  6
  7
  8
  9
 10
 11
 12
 13
 14
 15
 16
 17
 18
 19
 20
 21
 22
 23
 24
 25
 26
 27
 28
 29
 30
 31
 32
 33
 34
 35
 36
 37
 38
 39
 40
 41
 42
 43
 44
 45
 46
 47
 48
 49
 50
 51
 52
 53
 54
 55
 56
 57
 58
 59
 60
 61
 62
 63
 64
 65
 66
 67
 68
 69
 70
 71
 72
 73
 74
 75
 76
 77
 78
 79
 80
 81
 82
 83
 84
import time
from datetime import datetime, timezone
import httpx

class MarketDiscovery:
    def __init__(self, gateway_host: str = "https://gateway.polymarket.us"):
        self.gateway_host = gateway_host.rstrip("/")
        self._cache = {}
        self._cache_ts = 0.0

    def find_active_btc_market(self, timeframe: str = "5m"):
        now = time.time()
        key = timeframe.lower()
        if key in self._cache and now - self._cache_ts < 15:
            return self._cache[key]

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
        asks = data.get("asks") or data.get("yes", {}).get("asks") or []
        bids = data.get("bids") or data.get("yes", {}).get("bids") or []
        best_ask = float(asks[0]["price"]) if asks else 0.52
        ask_depth = float(asks[0].get("size", 100)) if asks else 100.0
        best_bid = float(bids[0]["price"]) if bids else max(0.01, best_ask - 0.02)
        spread = round(max(0.0, best_ask - best_bid), 4)
        down_ask = round(max(0.01, min(0.99, 1.0 - best_bid)), 4)
        return {
            "up_ask": best_ask,
            "down_ask": down_ask,
            "spread": spread,
            "ask_depth": ask_depth,
        }

    @staticmethod
    def _parse_iso(s: str):
        if not s:
            return None
        try:
            dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
            return dt.timestamp()
        except Exception:
            return None

Use Control + Shift + m to toggle the tab key moving focus. Alternatively, use esc then tab to move to the next interactive element on the page.
