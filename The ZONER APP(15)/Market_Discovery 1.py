import re
import time
from datetime import datetime, timezone
import httpx

SUPPORTED_TIMEFRAMES = ("5m", "15m", "1h", "1d", "all")


def _parse_iso(ts_str):
    if not ts_str:
        return 0.0
    try:
        s = str(ts_str).strip()
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        return datetime.fromisoformat(s).astimezone(timezone.utc).timestamp()
    except Exception:
        return 0.0


def _classify_timeframe(slug: str, question: str = "", seconds_left: float = 0.0) -> str:
    s = (slug or "").lower()
    q = (question or "").lower()
    if re.search(r"(?<!\d)15m(?!\d)", s) or "15 min" in q or "15-minute" in q:
        return "15m"
    if re.search(r"(?<!\d)5m(?!\d)", s) or "5 min" in q or "5-minute" in q:
        return "5m"
    if re.search(r"(?<!\d)(1h|60m)(?!\d)", s) or "1 hour" in q or "hourly" in q:
        return "1h"
    if re.search(r"(?<!\d)1d(?!\d)", s) or "daily" in s or "1 day" in q or "today" in q or "end of day" in q:
        return "1d"
    if 0 < seconds_left <= 420:
        return "5m"
    if 420 < seconds_left <= 1200:
        return "15m"
    if 1200 < seconds_left <= 4500:
        return "1h"
    if seconds_left > 4500:
        return "1d"
    return "5m"


class MarketDiscovery:
    def __init__(self, gateway_host="https://gateway.polymarket.us"):
        self.gateway_host = gateway_host.rstrip("/")
        self._cache = {}
        self._all_cache = {"ts": 0.0, "markets": []}

    def find_all_btc_markets(self, min_seconds_left=15):
        now = time.time()
        if now - self._all_cache["ts"] < 12.0 and self._all_cache["markets"]:
            return self._all_cache["markets"]

        candidates = []
        seen_slugs = set()

        with httpx.Client(timeout=8.0) as client:
            for query in ("Bitcoin Up or Down", "BTC Up or Down", "Bitcoin"):
                try:
                    resp = client.get(
                        f"{self.gateway_host}/v1/search",
                        params={"query": query, "limit": 30},
                    )
                    if resp.status_code != 200:
                        continue
                    data = resp.json()
                    events = data.get("events", []) if isinstance(data, dict) else []
                    for ev in events:
                        if not ev.get("active", True) or ev.get("closed", False):
                            continue
                        markets = ev.get("markets", []) or []
                        for m in markets:
                            raw_slug = str(m.get("slug") or ev.get("slug") or "").strip()
                            if not raw_slug:
                                continue
                            low = raw_slug.lower()
                            question = str(m.get("question") or ev.get("title") or "")
                            q_low = question.lower()
                            if not (("btc" in low or "bitcoin" in low or "btc" in q_low or "bitcoin" in q_low) and ("updown" in low or "up or down" in q_low or "up/down" in q_low)):
                                continue
                            end_ts = _parse_iso(m.get("endDate") or ev.get("endDate"))
                            if end_ts <= now + min_seconds_left:
                                continue
                            slug = raw_slug if raw_slug.startswith("cpc-") else f"cpc-{raw_slug}"
                            if slug in seen_slugs:
                                continue
                            seen_slugs.add(slug)
                            tf = _classify_timeframe(slug, question, end_ts - now)
                            candidates.append({
                                "slug": slug,
                                "question": question or slug,
                                "timeframe": tf,
                                "end_ts": end_ts,
                                "seconds_left": round(end_ts - now, 1),
                            })
                except Exception:
                    continue

        candidates.sort(key=lambda x: x["end_ts"])
        self._all_cache = {"ts": now, "markets": candidates}
        return candidates

    def find_markets_by_timeframe(self, min_seconds_left=15):
        all_mkts = self.find_all_btc_markets(min_seconds_left=min_seconds_left)
        grouped = {"5m": None, "15m": None, "1h": None, "1d": None}
        for m in all_mkts:
            tf = m.get("timeframe", "5m")
            if tf in grouped and grouped[tf] is None:
                grouped[tf] = m
        return grouped, all_mkts

    def find_active_btc_market(self, timeframe="all", min_seconds_left=15):
        tf = str(timeframe or "all").strip().lower()
        if tf == "auto":
            tf = "all"
        now = time.time()
        cached = self._cache.get(tf)
        if cached and now - cached["ts"] < 10.0:
            return cached["market"]

        all_mkts = self.find_all_btc_markets(min_seconds_left=min_seconds_left)
        selected = None
        if tf in ("5m", "15m", "1h", "1d"):
            for m in all_mkts:
                if m.get("timeframe") == tf:
                    selected = m
                    break
        if selected is None and all_mkts:
            selected = all_mkts[0]

        self._cache[tf] = {"ts": now, "market": selected}
        return selected

    def fetch_book(self, slug):
        if not slug:
            return None
        try:
            with httpx.Client(timeout=6.0) as client:
                resp = client.get(f"{self.gateway_host}/v1/markets/{slug}/book")
                if resp.status_code != 200:
                    return None
                data = resp.json()
                book = data.get("book", data) if isinstance(data, dict) else {}
                asks = book.get("asks", []) or []
                bids = book.get("bids", []) or []

                best_ask = float(asks[0].get("price", 0.5)) if asks else 0.52
                best_bid = float(bids[0].get("price", 0.5)) if bids else 0.48
                ask_depth = sum(float(a.get("size", 0)) for a in asks[:5])

                up_ask = max(0.01, min(0.99, best_ask))
                down_ask = max(0.01, min(0.99, 1.0 - best_bid))
                spread = max(0.0, round(best_ask - best_bid, 4))

                return {
                    "up_ask": round(up_ask, 4),
                    "down_ask": round(down_ask, 4),
                    "best_ask": round(best_ask, 4),
                    "best_bid": round(best_bid, 4),
                    "spread": spread,
                    "ask_depth": int(ask_depth),
                }
        except Exception:
            return None

    def fetch_settlement(self, slug):
        if not slug:
            return None
        try:
            with httpx.Client(timeout=6.0) as client:
                resp = client.get(f"{self.gateway_host}/v1/markets/{slug}")
                if resp.status_code != 200:
                    return None
                data = resp.json()
                mkt = data.get("market", data) if isinstance(data, dict) else {}
                if not mkt.get("closed", False) and not mkt.get("resolved", False):
                    return None
                outcome = str(mkt.get("winningOutcome") or mkt.get("outcome") or "").strip().upper()
                if outcome in ("UP", "YES", "LONG"):
                    return "UP"
                if outcome in ("DOWN", "NO", "SHORT"):
                    return "DOWN"
                return None
        except Exception:
            return None