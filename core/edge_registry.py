# v21.7.0 patch
from typing import Dict

EDGE_REGISTRY_VERSION = "v21.7.8"

ROTATION_DISCIPLINE = {
    "min_open_positions_for_competition": 6,
    "competition_hard_open_positions": 4,
    "elite_survival_floor": 1.28,
    "elite_score_floor": 0.96,
    "competition_hard_survival_floor": 1.64,
    "competition_hard_score_floor": 1.07,
    "competition_hard_pressure_floor": 0.22,
    "market_type_score_floor": 1.02,
    "market_type_survival_floor": 1.34,
    "normal_regime_survival_floor": 1.52,
    "base_gap": 0.24,
    "dead_relief_gap": 0.18,
    "fresh_position_immunity_cycles": 4,
    "extended_immunity_cycles": 6,
    "fresh_position_pnl_floor": -0.04,
    "fresh_position_signal_density": 0.16,
    "fresh_position_signal_trend": 0.78,
    "fresh_position_signal_window": 0.0038,
    "score_elite_gap": 0.18,
    "family_heat_penalty_trigger": 2.8,
    "cluster_heat_penalty_trigger": 3.0,
}


def _safe_float(value, default=0.0):
    try:
        return float(value)
    except Exception:
        return float(default)


def _safe_int(value, default=0):
    try:
        return int(value)
    except Exception:
        return int(default)


def fresh_position_immunity(position: Dict) -> bool:
    age_cycles = _safe_int(position.get("age_cycles", 0), 0)
    if age_cycles > ROTATION_DISCIPLINE["fresh_position_immunity_cycles"]:
        return False

    pnl_pct = _safe_float(position.get("current_unrealized_pnl_pct", 0.0), 0.0)
    density = _safe_float(position.get("last_pressure_density", position.get("entry_pressure_density", 0.0)), 0.0)
    trend = _safe_float(position.get("last_trend_strength", position.get("entry_trend_strength", 0.0)), 0.0)
    delta_window = abs(_safe_float(position.get("last_window_delta", position.get("entry_window_delta", 0.0)), 0.0))
    silent_cycles = _safe_int(position.get("silent_cycles", 0), 0)
    dead_cycles = _safe_int(position.get("dead_cycles", 0), 0)

    if pnl_pct <= ROTATION_DISCIPLINE["fresh_position_pnl_floor"]:
        return False
    if silent_cycles >= 3 or dead_cycles >= 2:
        return False

    return (
        density >= ROTATION_DISCIPLINE["fresh_position_signal_density"]
        or trend >= ROTATION_DISCIPLINE["fresh_position_signal_trend"]
        or delta_window >= ROTATION_DISCIPLINE["fresh_position_signal_window"]
    )


def rotation_friction_penalty(position: Dict, cluster_heat: float = 0.0, family_heat: float = 0.0) -> float:
    age_cycles = _safe_int(position.get("age_cycles", 0), 0)
    silent_cycles = _safe_int(position.get("silent_cycles", 0), 0)
    dead_cycles = _safe_int(position.get("dead_cycles", 0), 0)
    pnl_pct = _safe_float(position.get("current_unrealized_pnl_pct", 0.0), 0.0)
    peak_pnl_pct = _safe_float(position.get("peak_unrealized_pnl_pct", pnl_pct), pnl_pct)

    penalty = 0.0
    if age_cycles <= ROTATION_DISCIPLINE["fresh_position_immunity_cycles"]:
        penalty += 0.16
    elif age_cycles <= ROTATION_DISCIPLINE["extended_immunity_cycles"]:
        penalty += 0.08

    if pnl_pct > -0.01:
        penalty += 0.04
    if peak_pnl_pct >= 0.05:
        penalty += 0.05

    if cluster_heat >= ROTATION_DISCIPLINE["cluster_heat_penalty_trigger"]:
        penalty -= 0.05
    if family_heat >= ROTATION_DISCIPLINE["family_heat_penalty_trigger"]:
        penalty -= 0.04

    if silent_cycles >= 5:
        penalty -= 0.05
    if dead_cycles >= 3:
        penalty -= 0.07

    return round(max(-0.12, penalty), 4)


def competitive_gap_threshold(position: Dict, *, cluster_heat: float = 0.0, family_heat: float = 0.0, political_override_entry: bool = False, political_targeted_override: bool = False, balance_rescue_override: bool = False, cross_family_thesis_priority: bool = False, political_hold_window: bool = False) -> float:
    threshold = float(ROTATION_DISCIPLINE["base_gap"])

    silent_cycles = _safe_int(position.get("silent_cycles", 0), 0)
    dead_cycles = _safe_int(position.get("dead_cycles", 0), 0)
    reason = str(position.get("reason", "unknown") or "unknown")

    if dead_cycles >= 3 or silent_cycles >= 5:
        threshold = float(ROTATION_DISCIPLINE["dead_relief_gap"])

    if reason in {"score+pressure", "score+momentum", "multicycle_momentum_override", "score+pre_momentum"}:
        threshold += ROTATION_DISCIPLINE["score_elite_gap"]

    if political_override_entry and political_targeted_override:
        threshold += 0.18
    elif balance_rescue_override:
        threshold += 0.16
    elif cross_family_thesis_priority or political_hold_window:
        threshold += 0.14
    elif political_override_entry:
        threshold += 0.10

    threshold += rotation_friction_penalty(position, cluster_heat=cluster_heat, family_heat=family_heat)
    return round(max(0.16, threshold), 4)


def incoming_competition_gate(candidate: Dict, reason: str, current_regime: str, open_positions: int) -> Dict:
    score = _safe_float(candidate.get("score", 0.0), 0.0)
    survival = _safe_float(candidate.get("survival_priority", 0.0), 0.0)
    pressure = _safe_float(candidate.get("pressure_density", 0.0), 0.0)
    trend = _safe_float(candidate.get("price_trend_strength", 0.0), 0.0)
    market_type = candidate.get("market_type", "general_binary")
    source = str(candidate.get("_entry_source", candidate.get("entry_source", "unknown")) or "unknown")
    political_override = bool(candidate.get("political_override_active", False))

    if open_positions < ROTATION_DISCIPLINE["min_open_positions_for_competition"]:
        return {"allowed": False, "reason": "portfolio_too_small"}

    elite_reason = reason in {
        "score+pressure",
        "score+momentum",
        "multicycle_momentum_override",
        "momentum_override",
        "score+pre_momentum",
    }

    if open_positions >= 3 and not political_override:
        if market_type == "general_binary" and score < 1.00 and survival < 1.40 and pressure < 0.22:
            return {"allowed": False, "reason": "hot_slot_general_strict"}
        if market_type == "sports_award_longshot" and source == "explorer" and score < 0.99 and survival < 1.42:
            return {"allowed": False, "reason": "hot_slot_sports_explorer_strict"}
        if market_type == "narrative_long_tail" and score < 1.00 and survival < 1.30 and pressure < 0.18:
            return {"allowed": False, "reason": "hot_slot_narrative_strict"}

    if open_positions >= ROTATION_DISCIPLINE["competition_hard_open_positions"]:
        if elite_reason and survival >= ROTATION_DISCIPLINE["competition_hard_survival_floor"] and score >= ROTATION_DISCIPLINE["competition_hard_score_floor"] and (pressure >= ROTATION_DISCIPLINE["competition_hard_pressure_floor"] or trend >= 0.95):
            return {"allowed": True, "reason": "hard_elite_incoming"}

        if market_type in {"legal_resolution", "short_burst_catalyst", "speculative_hype"}:
            if survival >= max(ROTATION_DISCIPLINE["competition_hard_survival_floor"], 1.62) and score >= 1.05 and (pressure >= 0.24 or trend >= 0.98):
                return {"allowed": True, "reason": "hard_market_type_upgrade"}

        return {"allowed": False, "reason": "competition_hard_gate"}

    if elite_reason and survival >= ROTATION_DISCIPLINE["elite_survival_floor"] and score >= ROTATION_DISCIPLINE["elite_score_floor"]:
        return {"allowed": True, "reason": "elite_incoming"}

    if market_type in {"legal_resolution", "short_burst_catalyst", "speculative_hype"}:
        if score >= ROTATION_DISCIPLINE["market_type_score_floor"] and (pressure >= 0.22 or trend >= 0.97 or survival >= ROTATION_DISCIPLINE["market_type_survival_floor"]):
            return {"allowed": True, "reason": "market_type_upgrade"}

    if current_regime == "normal" and survival >= ROTATION_DISCIPLINE["normal_regime_survival_floor"] and score >= 0.97:
        return {"allowed": True, "reason": "normal_regime_elite"}

    return {"allowed": False, "reason": "incoming_edge_not_strong_enough"}
