# v20.1 version copy
# v20.0 version copy
# v19.9.1 version copy
# v19.9 version copy
# v19.8 version copy
# v19.7 version copy
# v19.6 version copy
from typing import Dict, List
import re

CRYPTO_WORDS = {
    "bitcoin", "btc", "ethereum", "eth", "solana", "sol", "crypto",
    "etf", "token", "airdrop", "stablecoin", "polygon", "fdv",
    "market cap", "onchain", "mainnet", "layer 2", "staking"
}

POLITICS_WORDS = {
    "election", "president", "presidential", "senate", "house",
    "ceasefire", "war", "sanction", "prime minister", "nomination",
    "putin", "trump", "biden", "ukraine", "russia", "taiwan", "china",
    "tariff", "shutdown", "balance of power", "democratic", "republican",
    "congress", "governor", "nominee"
}

SPORTS_WORDS = {
    "nba", "nhl", "mlb", "nfl", "fifa", "ufc", "stanley cup",
    "world cup", "rookie of the year", "mvp", "heisman",
    "cy young", "finals", "playoffs", "ballon d'or", "golden boot",
    "win the award", "award", "masters", "champions league", "bundesliga",
    "premier league", "la liga", "serie a", "tournament", "open championship",
    "pga", "golf", "spieth", "schauffele", "aberg", "sporting", "bayern",
    "dortmund", "knicks", "lakers"
}

TECH_WORDS = {
    "openai", "consumer hardware", "hardware", "device", "gadget", "chip",
    "ai", "artificial intelligence", "nvidia", "apple", "google", "meta",
    "microsoft", "tesla", "software", "app", "platform", "launch",
    "consumer product", "robot", "model", "api", "gta", "video game",
    "game release", "consumer tech", "smartphone", "headset"
}

WEIRD_WORDS = {
    "earthquake", "alien", "ufo", "gta", "hack",
    "virus", "outage", "shutdown", "bankruptcy",
    "rihana", "rihanna", "playboi", "jesus christ",
    "convicted", "release", "before gta vi"
}

JUNK_WORDS = {
    "will win the",
    "championship",
    "world cup",
    "stanley cup",
    "nba finals",
    "super bowl",
}

FAR_FUTURE_WORDS = {
    "2028 democratic presidential nomination",
    "2028 republican presidential nomination",
    "2028 presidential nomination",
    "win the 2028 democratic presidential nomination",
    "win the 2028 republican presidential nomination",
    "2028 president",
    "presidential election in 2028",
}

SHORT_HORIZON_HINTS = {
    "this week",
    "this month",
    "by march",
    "by april",
    "by may",
    "by june",
    "by july",
    "by august",
    "by september",
    "by october",
    "by november",
    "by december",
    "before gta vi",
    "march 31, 2026",
    "april 2026",
    "q2 2026",
    "q1 2026",
    "one day after launch",
    "by end of 2026",
}

SPORTS_AWARD_WORDS = {
    "rookie of the year",
    "mvp",
    "cy young",
    "heisman",
    "ballon d'or",
    "golden boot",
    "sixth man of the year",
    "defensive player of the year",
    "most improved player",
    "coach of the year",
    "award",
}


def contains_any(text: str, words: set) -> bool:
    q = " ".join((text or "").lower().split())
    for w in words:
        term = " ".join((w or "").lower().split())
        if not term:
            continue
        pattern = r"(?<![a-z0-9])" + re.escape(term) + r"(?![a-z0-9])"
        if re.search(pattern, q):
            return True
    return False


def detect_theme(question: str) -> str:
    q = (question or "").lower()

    if contains_any(q, SPORTS_WORDS):
        return "sports"
    if contains_any(q, CRYPTO_WORDS):
        return "crypto"
    if contains_any(q, POLITICS_WORDS):
        return "politics"
    if contains_any(q, TECH_WORDS):
        return "tech"
    if contains_any(q, WEIRD_WORDS):
        return "weird"
    return "general"


def theme_bonus(question: str) -> float:
    theme = detect_theme(question)

    if theme == "weird":
        return 1.30
    if theme == "crypto":
        return 1.18
    if theme == "tech":
        return 1.14
    if theme == "politics":
        return 1.08
    if theme == "sports":
        return 0.92
    return 1.00


def junk_penalty(question: str) -> float:
    q = (question or "").lower()

    if contains_any(q, JUNK_WORDS):
        return 0.60
    return 1.00


def far_future_penalty(question: str) -> float:
    q = (question or "").lower()

    if contains_any(q, FAR_FUTURE_WORDS):
        return 0.50

    if "2028" in q and (
        "nomination" in q or
        "president" in q or
        "presidential" in q or
        "election" in q
    ):
        return 0.55

    if "2027" in q and (
        "election" in q or
        "nomination" in q or
        "president" in q
    ):
        return 0.70

    return 1.00


def horizon_bonus(question: str, minutes_to_end) -> float:
    q = (question or "").lower()

    if contains_any(q, SHORT_HORIZON_HINTS):
        return 1.20

    if minutes_to_end is not None:
        try:
            m = float(minutes_to_end)
            if m <= 60 * 24 * 7:
                return 1.25
            if m <= 60 * 24 * 30:
                return 1.15
            if m <= 60 * 24 * 90:
                return 1.05
            if m >= 60 * 24 * 365:
                return 0.75
        except Exception:
            pass

    return 1.00


def catalyst_bonus(question: str) -> float:
    q = (question or "").lower()

    catalyst_words = {
        "ceasefire", "launch", "approval", "bankruptcy", "hack",
        "release", "convicted", "sec", "etf", "rate cut",
        "tariff", "shutdown", "earthquake", "consumer hardware",
        "one day after launch"
    }

    if contains_any(q, catalyst_words):
        return 1.15

    return 1.00


def sports_award_penalty(question: str) -> float:
    q = (question or "").lower()

    if contains_any(q, SPORTS_AWARD_WORDS):
        return 0.35

    return 1.00


def ultra_dust_penalty(price: float, question: str) -> float:
    q = (question or "").lower()

    if price < 0.002:
        if contains_any(q, SPORTS_AWARD_WORDS):
            return 0.25
        return 0.55

    if price < 0.005:
        if contains_any(q, SPORTS_AWARD_WORDS):
            return 0.50
        return 0.85

    return 1.00


def price_score_fn(price: float) -> float:
    if price < 0.002:
        return 0.20
    if price < 0.005:
        return 0.65
    if price < 0.02:
        return 1.00
    if price < 0.05:
        return 0.85
    if price < 0.10:
        return 0.70
    return 0.50


def liquidity_score_fn(liquidity: float) -> float:
    if liquidity <= 0:
        return 0.0
    return min(liquidity / 5000.0, 1.0)


def volatility_bonus(price_delta: float, price: float) -> float:
    abs_delta = abs(price_delta)

    if abs_delta < 0.003:
        return 1.00

    if price < 0.003 and abs_delta > 0.02:
        return 0.90

    if abs_delta < 0.01:
        return 1.05
    if abs_delta < 0.03:
        return 1.12
    if abs_delta < 0.07:
        return 1.20

    return 1.28


def direction_bonus(price_delta: float, price: float) -> float:
    if price_delta > 0.015 and price <= 0.10:
        return 1.06
    if price_delta < -0.03 and price <= 0.02:
        return 1.04
    return 1.00


def pre_momentum_bonus(price_delta_window: float, trend_strength: float, price: float, score_hint: float) -> float:
    abs_window = abs(price_delta_window)

    if price > 0.35:
        return 1.00

    if trend_strength >= 0.80 and abs_window >= 0.002 and score_hint >= 0.45:
        return 1.05

    if trend_strength >= 0.80 and abs_window >= 0.004 and score_hint >= 0.40:
        return 1.08

    if trend_strength >= 0.75 and abs_window >= 0.006 and score_hint >= 0.35:
        return 1.12

    return 1.00


def pressure_bonus(pressure_density: float, pressure_count: float, theme: str, price: float, score_hint: float) -> float:
    """
    V8:
    rewards markets that show repeated non-zero movement across the history window.
    """
    if price > 0.40:
        return 1.00

    if pressure_count >= 3 and pressure_density >= 0.50 and score_hint >= 0.40:
        return 1.06

    if pressure_count >= 4 and pressure_density >= 0.60 and score_hint >= 0.35:
        return 1.10

    if theme in {"crypto", "politics", "weird", "tech"} and pressure_count >= 2 and pressure_density >= 0.40 and score_hint >= 0.40:
        return 1.08

    return 1.00


def dead_market_penalty(pressure_density: float, abs_window_delta: float, price: float, liquidity: float) -> float:
    """
    Penalize completely dead markets unless they are very cheap, liquid, and otherwise strong.
    """
    if pressure_density == 0.0 and abs_window_delta == 0.0:
        if price > 0.12:
            return 0.90
        if liquidity < 1500:
            return 0.92
    return 1.00


def score_candidate(c: Dict) -> Dict:
    price = float(c["price"])
    liquidity = float(c.get("liquidity", 0))
    question = c.get("question", "")
    minutes_to_end = c.get("minutes_to_end")
    price_delta = float(c.get("price_delta", 0.0))
    price_delta_window = float(c.get("price_delta_window", 0.0))
    trend_strength = float(c.get("price_trend_strength", 0.0))
    pressure_density = float(c.get("pressure_density", 0.0))
    pressure_count = float(c.get("pressure_count", 0.0))

    price_score = price_score_fn(price)
    liquidity_score = liquidity_score_fn(liquidity)

    t_bonus = theme_bonus(question)
    h_bonus = horizon_bonus(question, minutes_to_end)
    c_bonus = catalyst_bonus(question)

    j_penalty = junk_penalty(question)
    f_penalty = far_future_penalty(question)
    s_penalty = sports_award_penalty(question)
    d_penalty = ultra_dust_penalty(price, question)

    v_bonus = volatility_bonus(price_delta, price)
    dir_bonus = direction_bonus(price_delta, price)

    base_score = (price_score * 0.58) + (liquidity_score * 0.42)

    pre_score_hint = (
        base_score
        * t_bonus
        * h_bonus
        * c_bonus
        * j_penalty
        * f_penalty
        * s_penalty
        * d_penalty
        * v_bonus
        * dir_bonus
    )

    pm_bonus = pre_momentum_bonus(price_delta_window, trend_strength, price, pre_score_hint)
    pr_bonus = pressure_bonus(pressure_density, pressure_count, detect_theme(question), price, pre_score_hint)
    dm_penalty = dead_market_penalty(pressure_density, abs(price_delta_window), price, liquidity)

    score = pre_score_hint * pm_bonus * pr_bonus * dm_penalty

    enriched = dict(c)
    enriched["theme"] = detect_theme(question)
    enriched["price_score"] = round(price_score, 6)
    enriched["liquidity_score"] = round(liquidity_score, 6)
    enriched["theme_bonus"] = round(t_bonus, 6)
    enriched["horizon_bonus"] = round(h_bonus, 6)
    enriched["catalyst_bonus"] = round(c_bonus, 6)
    enriched["junk_penalty"] = round(j_penalty, 6)
    enriched["far_future_penalty"] = round(f_penalty, 6)
    enriched["sports_award_penalty"] = round(s_penalty, 6)
    enriched["ultra_dust_penalty"] = round(d_penalty, 6)
    enriched["volatility_bonus"] = round(v_bonus, 6)
    enriched["direction_bonus"] = round(dir_bonus, 6)
    enriched["pre_momentum_bonus"] = round(pm_bonus, 6)
    enriched["pressure_bonus"] = round(pr_bonus, 6)
    enriched["dead_market_penalty"] = round(dm_penalty, 6)
    enriched["price_delta"] = round(price_delta, 6)
    enriched["abs_price_delta"] = round(abs(price_delta), 6)
    enriched["price_delta_window"] = round(price_delta_window, 6)
    enriched["abs_price_delta_window"] = round(abs(price_delta_window), 6)
    enriched["price_trend_strength"] = round(trend_strength, 6)
    enriched["pressure_density"] = round(pressure_density, 6)
    enriched["pressure_count"] = int(pressure_count)
    enriched["score"] = round(score, 6)

    return enriched


def rank_candidates(candidates: List[Dict]) -> List[Dict]:
    scored = [score_candidate(c) for c in candidates]
    scored.sort(key=lambda x: x["score"], reverse=True)
    return scored