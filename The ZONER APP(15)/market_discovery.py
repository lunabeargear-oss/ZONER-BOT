import re
import time
from datetime import datetime
import httpx


class MarketDiscovery:
    def __init__(self, *args, gateway_host: str = "https://gateway.polymarket.us"):
        # Accept optional positional args safely if called as MarketDiscovery(cfg)
        self.gateway_host = gateway_host.rstrip("/")

    @staticmethod
    def _parse_iso_ts(val) -> float:
        if not val:
            return 0.0
        if isinstance(val, (int, float)):
            return float(val)
        try:
            s = str(val).strip()
            if s.endswith("Z"):
                s = s[:-1] + "+00:00"
            return datetime.fromisoformat(s).timestamp()
        except Exception:
            return 0.0

    @staticmethod
    def _extract_tf(slug: str, title: str, seconds_left: float = 0.0) -> str:
        text = f"{slug} {title}".lower()
        if re.search(r"(?<!\d)5m(?!\d)", text) or "5-min" in text or "5 min" in text:
            return "5m"
        if re.search(r"(?<!\d)15m(?!\d)", text) or "15-min" in text or "15 min" in text:
            return "15m"
        if re.search(r"(?<!\d)(1h|60m)(?!\d)", text) or "hourly" in text or "1 hour" in text:
            return "1h"
        if re.search(r"(?<!\d)1d(?!\d)", text) or "daily" in text or "24h" in text:
            return "1d"
        if 0 < seconds_left <= 420:
            return "5m"
        if 420 < seconds_left <= 1200:
            return "15m"
        if 1200 < seconds_left <= 4500:
            return "1h"
        if seconds_left > 4500:
            return "1d"
        return "other"

    def find_all_btc_markets(self) -> list[dict]:
        now = time.time()
        candidates = []
        seen_slugs = set()

        try:
            with httpx.Client(timeout=7.0) as client:
                for query in ("Bitcoin Up or Down", "BTC Up or Down", "Bitcoin"):
                    try:
                        r = client.get(
                            f"{self.gateway_host}/v1/search",
                            params={"query": query, "limit": 30},
                        )
                        if r.status_code != 200:
                            continue
                        payload = r.json()
                        events = payload.get("events", []) if isinstance(payload, dict) else []
                        for ev in events:
                            if not ev.get("active", True) or ev.get("closed", False):
                                continue
                            markets = ev.get("markets") or []
                            for m in markets:
                                if not m.get("active", True) or m.get("closed", False):
                                    continue
                                raw_slug = str(m.get("slug") or ev.get("slug") or "").strip()
                                if not raw_slug:
                                    continue
                                slug = raw_slug if raw_slug.startswith("cpc-") else f"cpc-{raw_slug}"
                                if slug in seen_slugs:
                                    continue

                                title = str(m.get("question") or m.get("title") or ev.get("title") or slug)
                                combined = f"{slug} {title}".lower()
                                if "btc" not in combined and "bitcoin" not in combined:
                                    continue
                                if "updown" not in combined and "up or down" not in combined and "up/down" not in combined:
                                    continue

                                end_ts = self._parse_iso_ts(
                                    m.get("endDate")
                                    or m.get("windowEnd")
                                    or m.get("closeTime")
                                    or ev.get("endDate")
                                )
                                if end_ts <= now + 5:
                                    continue

                                seen_slugs.add(slug)
                                seconds_left = int(max(0, end_ts - now))
                                tf = self._extract_tf(slug, title, seconds_left)
                                candidates.append({
                                    "slug": slug,
                                    "title": title,
                                    "timeframe": tf,
                                    "end_ts": end_ts,
                                    "seconds_left": seconds_left,
                                })
                    except Exception:
                        continue
        except Exception:
            return []

        candidates.sort(key=lambda x: x["seconds_left"])
        return candidates

    def find_active_btc_market(self, target_timeframe: str = "all", allow_fallback: bool = True) -> dict | None:
        return self.find_best_btc_market(target_timeframe=target_timeframe, allow_fallback=allow_fallback)

    def find_best_btc_market(self, target_timeframe: str = "all", allow_fallback: bool = True) -> dict | None:
        tf_req = (target_timeframe or "all").strip().lower()
        if tf_req == "auto":
            tf_req = "all"

        all_candidates = self.find_all_btc_markets()
        if not all_candidates:
            return None

        if tf_req != "all":
            matching = [c for c in all_candidates if c["timeframe"] == tf_req]
            pool = matching if matching else (all_candidates if allow_fallback else [])
        else:
            pool = all_candidates

        if not pool:
            return None

        # Prefer markets with >= 25s left
        viable = [c for c in pool if c["seconds_left"] >= 25] or pool

        # Score top candidates using live book liquidity & spread so we always pick the best live market
        tf_priority = {"5m": 0, "15m": 1, "1h": 2, "1d": 3, "other": 4}
        scored = []
        for idx, m in enumerate(viable[:6]):
            b = self.fetch_book(m["slug"])
            is_live = b.get("book_source") == "Live Book"
            total_depth = float(b.get("ask_depth", 0)) + float(b.get("bid_depth", 0))
            spread = float(b.get("spread", 1.0))
            # Rank: live book with depth first, then tight spread, then requested timeframe / shortest window
            scored.append((
                0 if (is_live and total_depth > 0) else (1 if is_live else 2),
                0 if spread <= 0.08 else 1,
                tf_priority.get(m["timeframe"], 4),
                spread,
                -total_depth,
                idx,
                m,
            ))

        scored.sort(key=lambda x: x[:6])
        return scored[0][-1]

    @staticmethod
    def _parse_level(lvl) -> tuple[float, float]:
        if isinstance(lvl, dict):
            px_raw = lvl.get("px") or lvl.get("price") or lvl.get("limitPrice") or lvl.get("p") or 0
            if isinstance(px_raw, dict):
                px_raw = px_raw.get("value") or px_raw.get("amount") or 0
            sz_raw = (
                lvl.get("qty")
                or lvl.get("size")
                or lvl.get("quantity")
                or lvl.get("amount")
                or lvl.get("shares")
                or lvl.get("remainingQuantity")
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
                book = (
                    data.get("marketData")
                    or data.get("book")
                    if isinstance(data, dict)
                    else data
                )
                if not isinstance(book, dict):
                    book = data if isinstance(data, dict) else {}

                state = str(book.get("state") or "").upper()
                if "EXPIRED" in state or "CLOSED" in state or "SETTLED" in state:
                    return fallback

                bids = book.get("bids") or book.get("yesBids") or []
                asks = book.get("offers") or book.get("asks") or book.get("yesAsks") or []

                parsed_bids = [self._parse_level(b) for b in bids]
                parsed_asks = [self._parse_level(a) for a in asks]
                parsed_bids = [(p, s) for p, s in parsed_bids if 0 < p < 1 and s > 0]
                parsed_asks = [(p, s) for p, s in parsed_asks if 0 < p < 1 and s > 0]

                if not parsed_bids and not parsed_asks:
                    return fallback

                best_bid = max((p for p, _ in parsed_bids), default=0.0)
                best_ask = min((p for p, _ in parsed_asks), default=0.0)
                if best_bid <= 0 and best_ask > 0:
                    best_bid = max(0.01, round(best_ask - 0.02, 2))
                if best_ask <= 0 and best_bid > 0:
                    best_ask = min(0.99, round(best_bid + 0.02, 2))
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
                    "ask_depth": ask_depth if ask_depth > 0 else bid_depth,
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
                r = client.get(f"{self.gateway_host}/v1/markets/{slug}/book")
                if r.status_code != 200:
                    return None
                data = r.json()
                md = data.get("marketData") if isinstance(data, dict) else None
                if not isinstance(md, dict):
                    return None
                state = str(md.get("state") or "").upper()
                if "EXPIRED" not in state and "SETTLED" not in state and "CLOSED" not in state:
                    return None
                stats = md.get("stats") or {}
                for key in ("settlementPx", "closePx", "lastTradePx"):
                    val = stats.get(key)
                    if isinstance(val, dict):
                        val = val.get("value")
                    if val is not None:
                        return float(val)
        except Exception:
            pass
        return None
