from typing import Dict, List
from config.settings import MIN_LIQUIDITY, MIN_MINUTES_TO_END, MIN_PRICE, MAX_PRICE

BLACKLIST_KEYWORDS = {
    "celebrity death",
    "assassination",
}

ANTI_JUNK_KEYWORDS = {
    "will win the",
    "championship",
    "world cup",
    "stanley cup",
    "nba finals",
    "super bowl",
    "election winner",
}

FAR_FUTURE_POLITICS_KEYWORDS = {
    "2028 democratic presidential nomination",
    "2028 republican presidential nomination",
    "2028 presidential nomination",
    "win the 2028 democratic presidential nomination",
    "win the 2028 republican presidential nomination",
    "win the 2028 presidential election",
    "2028 president",
    "presidential election in 2028",
}

def is_blacklisted(question: str) -> bool:
    q = (question or "").lower()
    return any(k in q for k in BLACKLIST_KEYWORDS)

def is_junk_market(question: str) -> bool:
    q = (question or "").lower()
    return any(k in q for k in ANTI_JUNK_KEYWORDS)

def is_far_future_politics(question: str) -> bool:
    q = (question or "").lower()

    if any(k in q for k in FAR_FUTURE_POLITICS_KEYWORDS):
        return True

    # generic catch for distant political nomination/election markets
    if "2028" in q and (
        "nomination" in q or
        "presidential" in q or
        "president" in q or
        "election" in q
    ):
        return True

    return False

def filter_candidates(candidates: List[Dict]) -> List[Dict]:
    result = []

    reject_blacklist = 0
    reject_junk = 0
    reject_far_future = 0
    reject_price = 0
    reject_liquidity = 0
    reject_time = 0

    for c in candidates:
        try:
            price = float(c.get("price", 0))
        except Exception:
            price = 0.0

        try:
            liquidity = float(c.get("liquidity", 0))
        except Exception:
            liquidity = 0.0

        minutes_to_end = c.get("minutes_to_end")
        question = c.get("question", "")

        if is_blacklisted(question):
            reject_blacklist += 1
            continue

        if is_junk_market(question):
            reject_junk += 1
            continue

        if is_far_future_politics(question):
            reject_far_future += 1
            continue

        if not (MIN_PRICE <= price <= MAX_PRICE):
            reject_price += 1
            continue

        if liquidity < MIN_LIQUIDITY:
            reject_liquidity += 1
            continue

        if minutes_to_end is not None:
            try:
                if float(minutes_to_end) < MIN_MINUTES_TO_END:
                    reject_time += 1
                    continue
            except Exception:
                pass

        result.append(c)

    print(
        "FILTER DEBUG | "
        f"blacklist={reject_blacklist} "
        f"junk={reject_junk} "
        f"far_future={reject_far_future} "
        f"price={reject_price} "
        f"liquidity={reject_liquidity} "
        f"time={reject_time} "
        f"passed={len(result)}"
    )

    return result