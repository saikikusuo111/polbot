# v21.7.0 patch
from typing import Dict, List

REGIME_DETECTOR_VERSION = "v21.7.8"

REGIME_THRESHOLDS = {
    "hot": {
        "strong_ratio": 0.03,
        "pressure_ratio": 0.08,
    },
    "normal": {
        "non_zero_ratio": 0.03,
        "pre_ratio": 0.06,
    },
}

REGIME_SETTINGS = {
    "hot": {
        "MAX_CYCLE_RISK_USD": 20.0,
        "MAX_THEME_POSITIONS_PER_CYCLE": 4,
        "MAX_CLUSTER_POSITIONS_PER_CYCLE": 3,
        "STAKE_MULTIPLIER": 1.20,
        "COMPETITION_AGGRESSION": 1.10,
        "EXPLORER_ALLOWANCE": 3,
    },
    "normal": {
        "MAX_CYCLE_RISK_USD": 16.0,
        "MAX_THEME_POSITIONS_PER_CYCLE": 3,
        "MAX_CLUSTER_POSITIONS_PER_CYCLE": 2,
        "STAKE_MULTIPLIER": 1.00,
        "COMPETITION_AGGRESSION": 1.00,
        "EXPLORER_ALLOWANCE": 2,
    },
    "calm": {
        "MAX_CYCLE_RISK_USD": 12.0,
        "MAX_THEME_POSITIONS_PER_CYCLE": 2,
        "MAX_CLUSTER_POSITIONS_PER_CYCLE": 1,
        "STAKE_MULTIPLIER": 0.85,
        "COMPETITION_AGGRESSION": 0.88,
        "EXPLORER_ALLOWANCE": 1,
    },
}


def _safe_float(value, default=0.0):
    try:
        return float(value)
    except Exception:
        return float(default)


def detect_market_regime(candidates: List[Dict]) -> Dict:
    non_zero_delta = 0
    strong_delta = 0
    pressure_like = 0
    pre_like = 0

    total = max(len(candidates), 1)

    for c in candidates:
        abs_delta = abs(_safe_float(c.get("price_delta", 0.0), 0.0))
        abs_window = abs(_safe_float(c.get("price_delta_window", 0.0), 0.0))
        density = _safe_float(c.get("pressure_density", 0.0), 0.0)

        if abs_delta > 0:
            non_zero_delta += 1
        if abs_delta >= 0.008 or abs_window >= 0.008:
            strong_delta += 1
        if density >= 0.25:
            pressure_like += 1
        if abs_window >= 0.002:
            pre_like += 1

    non_zero_ratio = float(non_zero_delta) / float(total)
    strong_ratio = float(strong_delta) / float(total)
    pressure_ratio = float(pressure_like) / float(total)
    pre_ratio = float(pre_like) / float(total)

    if strong_ratio >= REGIME_THRESHOLDS["hot"]["strong_ratio"] or pressure_ratio >= REGIME_THRESHOLDS["hot"]["pressure_ratio"]:
        regime = "hot"
        trigger = "strong_or_pressure"
    elif non_zero_ratio >= REGIME_THRESHOLDS["normal"]["non_zero_ratio"] or pre_ratio >= REGIME_THRESHOLDS["normal"]["pre_ratio"]:
        regime = "normal"
        trigger = "non_zero_or_pre"
    else:
        regime = "calm"
        trigger = "low_activity"

    return {
        "regime": regime,
        "non_zero_ratio": round(non_zero_ratio, 6),
        "strong_ratio": round(strong_ratio, 6),
        "pressure_ratio": round(pressure_ratio, 6),
        "pre_ratio": round(pre_ratio, 6),
        "trigger": trigger,
        "detector_version": REGIME_DETECTOR_VERSION,
        "competition_aggression": REGIME_SETTINGS[regime]["COMPETITION_AGGRESSION"],
        "explorer_allowance": REGIME_SETTINGS[regime]["EXPLORER_ALLOWANCE"],
    }


def regime_settings(regime: str) -> Dict:
    return dict(REGIME_SETTINGS.get(regime, REGIME_SETTINGS["calm"]))


def regime_trace_line(regime_info: Dict, settings: Dict) -> str:
    return (
        "TRACE | regime_audit | detector={} | regime={} | trigger={} | non_zero={:.3f} | strong={:.3f} | pressure={:.3f} | pre={:.3f} | "
        "cycle_risk=${:.2f} | theme_cap={} | cluster_cap={} | explorer_allowance={} | competition_aggr={:.2f}"
    ).format(
        regime_info.get("detector_version", REGIME_DETECTOR_VERSION),
        regime_info.get("regime", "unknown"),
        regime_info.get("trigger", "unknown"),
        float(regime_info.get("non_zero_ratio", 0.0) or 0.0),
        float(regime_info.get("strong_ratio", 0.0) or 0.0),
        float(regime_info.get("pressure_ratio", 0.0) or 0.0),
        float(regime_info.get("pre_ratio", 0.0) or 0.0),
        float(settings.get("MAX_CYCLE_RISK_USD", 0.0) or 0.0),
        int(settings.get("MAX_THEME_POSITIONS_PER_CYCLE", 0) or 0),
        int(settings.get("MAX_CLUSTER_POSITIONS_PER_CYCLE", 0) or 0),
        int(regime_info.get("explorer_allowance", settings.get("EXPLORER_ALLOWANCE", 0)) or 0),
        float(regime_info.get("competition_aggression", settings.get("COMPETITION_AGGRESSION", 1.0)) or 1.0),
    )
