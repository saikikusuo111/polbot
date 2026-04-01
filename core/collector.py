import aiohttp
import json
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

GAMMA_MARKETS_URL = "https://gamma-api.polymarket.com/markets"


def _parse_dt(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except Exception:
        return None


def _minutes_to(target: Optional[datetime]) -> Optional[float]:
    if not target:
        return None
    now = datetime.now(timezone.utc)
    return (target - now).total_seconds() / 60.0


def _normalize_list_field(value: Any) -> List[Any]:
    """
    Gamma sometimes returns:
    - real list: ["Yes", "No"]
    - JSON string: '["Yes","No"]'
    - empty / None
    """
    if value is None:
        return []

    if isinstance(value, list):
        return value

    if isinstance(value, str):
        raw = value.strip()
        if not raw:
            return []

        # try JSON list first
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, list):
                return parsed
        except Exception:
            pass

        # fallback: comma-separated string
        if "," in raw:
            return [x.strip() for x in raw.split(",") if x.strip()]

        return [raw]

    return []


async def fetch_markets(session: aiohttp.ClientSession, limit: int = 200) -> List[Dict[str, Any]]:
    params = {
        "active": "true",
        "closed": "false",
        "limit": str(limit),
    }

    async with session.get(GAMMA_MARKETS_URL, params=params, timeout=30) as resp:
        resp.raise_for_status()
        data = await resp.json()

    normalized = []
    for m in data:
        end_date = _parse_dt(m.get("endDate") or m.get("end_date"))
        minutes_to_end = _minutes_to(end_date)

        outcomes = _normalize_list_field(m.get("outcomes"))
        outcome_prices = _normalize_list_field(m.get("outcomePrices") or m.get("outcome_prices"))

        normalized.append({
            "market_id": m.get("id"),
            "question": m.get("question") or m.get("title") or "",
            "description": m.get("description") or "",
            "category": m.get("category") or "unknown",
            "active": m.get("active", False),
            "closed": m.get("closed", False),
            "end_date": end_date.isoformat() if end_date else None,
            "minutes_to_end": minutes_to_end,
            "liquidity": float(m.get("liquidity") or 0.0),
            "volume": float(m.get("volume") or 0.0),
            "outcomes": outcomes,
            "outcome_prices": outcome_prices,
            "raw": m,
        })

    return normalized


def extract_candidate_outcomes(market: Dict[str, Any]) -> List[Dict[str, Any]]:
    outcomes = market.get("outcomes") or []
    prices = market.get("outcome_prices") or []

    candidates = []
    for idx, name in enumerate(outcomes):
        try:
            price = float(prices[idx])
        except Exception:
            continue

        candidates.append({
            "market_id": market["market_id"],
            "question": market["question"],
            "description": market["description"],
            "category": market["category"],
            "minutes_to_end": market["minutes_to_end"],
            "liquidity": market["liquidity"],
            "volume": market["volume"],
            "outcome_name": str(name),
            "price": price,
            "end_date": market["end_date"],
        })

    return candidates