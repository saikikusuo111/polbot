# v21.7.3 patch
# v20.1 version copy
# v20.0 version copy
# v19.9.1 version copy
# v19.9 version copy
# v19.8 version copy
# v19.7 version copy
# v19.6 version copy
from typing import Dict, List, Optional
import sys
import importlib
import importlib.util
from pathlib import Path

PAPER_ENGINE_VERSION = "v21.7.8"
_PATCH_SUFFIX = PAPER_ENGINE_VERSION.replace(".", "_")
_SCRIPT_DIR = Path(__file__).resolve().parent if "__file__" in globals() else Path.cwd()


def _load_edge_registry_module():
    local_candidates = [
        _SCRIPT_DIR / f"edge_registry_{_PATCH_SUFFIX}.py",
        _SCRIPT_DIR / "edge_registry.py",
    ]
    for path in local_candidates:
        if path.exists():
            module_name = f"edge_registry_{_PATCH_SUFFIX}_paper_runtime"
            spec = importlib.util.spec_from_file_location(module_name, str(path))
            if spec and spec.loader:
                module = importlib.util.module_from_spec(spec)
                sys.modules[module_name] = module
                spec.loader.exec_module(module)
                return module
    for dotted in ("core.edge_registry", "edge_registry"):
        try:
            return importlib.import_module(dotted)
        except Exception:
            continue
    raise ImportError("Unable to load edge_registry module for paper_engine")


_edge_registry_module = _load_edge_registry_module()
EDGE_REGISTRY_VERSION = getattr(_edge_registry_module, "EDGE_REGISTRY_VERSION", "unknown")
fresh_position_immunity = getattr(_edge_registry_module, "fresh_position_immunity")
competitive_gap_threshold = getattr(_edge_registry_module, "competitive_gap_threshold")
rotation_friction_penalty = getattr(_edge_registry_module, "rotation_friction_penalty")


def _competition_audit_line(position: Dict, status: str, *, hold_score: float = 0.0, incoming_edge: float = 0.0, gap: float = 0.0, threshold: float = 0.0, immunity: int = 0, friction: float = 0.0, detail: str = "") -> str:
    return "{}:{} | age={} | pnl={:.3f} | hold={:.3f} | in={:.3f} | gap={:.3f} | thr={:.3f} | imm={} | fr={:.3f}{}".format(
        status,
        (position.get("outcome_name") or position.get("question") or "unknown")[:54],
        int(position.get("age_cycles", 0) or 0),
        float(position.get("current_unrealized_pnl_pct", 0.0) or 0.0),
        float(hold_score or 0.0),
        float(incoming_edge or 0.0),
        float(gap or 0.0),
        float(threshold or 0.0),
        int(immunity),
        float(friction or 0.0),
        (" | " + detail) if detail else "",
    )


MARKET_TYPE_EXIT_PROFILE = {
    "short_burst_catalyst": {
        "stale_age": 9,
        "idle_age": 11,
        "rotation_bonus": -0.03,
        "follow_through_patience": 0.90,
        "profit_lock_bias": 1.04,
    },
    "legal_resolution": {
        "stale_age": 8,
        "idle_age": 10,
        "rotation_bonus": 0.02,
        "follow_through_patience": 0.98,
        "profit_lock_bias": 1.02,
    },
    "scheduled_binary_event": {
        "stale_age": 8,
        "idle_age": 10,
        "rotation_bonus": 0.00,
        "follow_through_patience": 1.00,
        "profit_lock_bias": 1.00,
    },
    "valuation_ladder": {
        "stale_age": 7,
        "idle_age": 9,
        "rotation_bonus": 0.08,
        "follow_through_patience": 0.98,
        "profit_lock_bias": 0.98,
    },
    "narrative_long_tail": {
        "stale_age": 7,
        "idle_age": 9,
        "rotation_bonus": 0.07,
        "follow_through_patience": 1.06,
        "profit_lock_bias": 0.95,
    },
    "speculative_hype": {
        "stale_age": 7,
        "idle_age": 9,
        "rotation_bonus": 0.03,
        "follow_through_patience": 0.92,
        "profit_lock_bias": 1.06,
    },
    "sports_award_longshot": {
        "stale_age": 6,
        "idle_age": 8,
        "rotation_bonus": 0.10,
        "follow_through_patience": 0.94,
        "profit_lock_bias": 0.92,
    },
    "general_binary": {
        "stale_age": 8,
        "idle_age": 10,
        "rotation_bonus": 0.00,
        "follow_through_patience": 1.00,
        "profit_lock_bias": 1.00,
    },
}


ZERO_CHURN_MARKET_TYPES = {"general_binary", "narrative_long_tail", "valuation_ladder", "speculative_hype", "sports_award_longshot", "short_burst_catalyst"}

PROFIT_LOCK_SEED_PROFILE = {
    "short_burst_catalyst": {"trigger": 0.08, "fraction": 0.44, "min_age": 1},
    "legal_resolution": {"trigger": 0.08, "fraction": 0.38, "min_age": 1},
    "scheduled_binary_event": {"trigger": 0.12, "fraction": 0.32, "min_age": 1},
    "valuation_ladder": {"trigger": 0.10, "fraction": 0.36, "min_age": 1},
    "narrative_long_tail": {"trigger": 0.09, "fraction": 0.37, "min_age": 1},
    "speculative_hype": {"trigger": 0.08, "fraction": 0.42, "min_age": 1},
    "sports_award_longshot": {"trigger": 0.12, "fraction": 0.34, "min_age": 1},
    "general_binary": {"trigger": 0.12, "fraction": 0.32, "min_age": 1},
}

class PaperEngine:
    def __init__(self, starting_balance: float = 100.0, default_stake: float = 1.0):
        self.starting_balance = float(starting_balance)
        self.balance_free = float(starting_balance)
        self.default_stake = float(default_stake)

        self.open_positions: List[Dict] = []
        self.closed_positions: List[Dict] = []

        self.paper_spent_total = 0.0
        self.open_cost_basis = 0.0
        self.realized_pnl_total = 0.0

        self._open_keys = set()

    def _build_key(self, candidate: Dict) -> str:
        return "{}::{}".format(
            candidate.get("market_id", ""),
            candidate.get("outcome_name", "")
        )

    def has_open_position(self, key: str) -> bool:
        return key in self._open_keys

    def _safe_float(self, value, default: float = 0.0) -> float:
        try:
            return float(value)
        except Exception:
            return float(default)

    def _find_position(self, key: str) -> Optional[Dict]:
        for pos in self.open_positions:
            if pos.get("position_key") == key:
                return pos
        return None

    def _ensure_political_position_flags(self, position: Dict) -> Dict:
        stake_model = dict(position.get("stake_model", {}) or {})

        position["political_override_entry"] = bool(position.get("political_override_entry", stake_model.get("political_override_entry", False)))
        position["political_targeted_override"] = bool(position.get("political_targeted_override", stake_model.get("political_targeted_override", False)))
        position["balance_rescue_override"] = bool(position.get("balance_rescue_override", stake_model.get("balance_rescue_override", False)))
        position["cross_family_thesis_priority"] = bool(position.get("cross_family_thesis_priority", stake_model.get("cross_family_thesis_priority", False)))
        position["cross_family_priority_cycles"] = int(position.get("cross_family_priority_cycles", stake_model.get("cross_family_priority_cycles", 0)) or 0)
        position["political_hold_window"] = bool(position.get("political_hold_window", stake_model.get("political_hold_window", False)))
        position["political_hold_window_cycles"] = int(position.get("political_hold_window_cycles", stake_model.get("political_hold_window_cycles", 0)) or 0)
        position["override_survival_corridor"] = bool(position.get("override_survival_corridor", stake_model.get("override_survival_corridor", False)))
        position["political_override_reason"] = position.get("political_override_reason", stake_model.get("political_override_reason"))
        position["flag_mirror_audit_expected"] = bool(position.get("flag_mirror_audit_expected", stake_model.get("flag_mirror_audit_expected", False)))

        if position["political_override_entry"]:
            base_cycles = 8 if (position["political_targeted_override"] or position["balance_rescue_override"]) else 6

            if position["political_hold_window"] and position["political_hold_window_cycles"] <= 0:
                position["political_hold_window_cycles"] = base_cycles
            if position["cross_family_thesis_priority"] and position["cross_family_priority_cycles"] <= 0:
                position["cross_family_priority_cycles"] = max(position["political_hold_window_cycles"], base_cycles)

            if (
                not position["override_survival_corridor"]
                and (
                    position["political_targeted_override"]
                    or position["balance_rescue_override"]
                    or position["political_hold_window"]
                    or position["cross_family_thesis_priority"]
                )
            ):
                position["override_survival_corridor"] = True

        mirror_ok = True
        if position["political_override_entry"]:
            if (position["political_targeted_override"] or position["balance_rescue_override"] or position["political_hold_window"]) and position["political_hold_window_cycles"] <= 0:
                mirror_ok = False
            if position["cross_family_thesis_priority"] and position["cross_family_priority_cycles"] <= 0:
                mirror_ok = False
            if (position["political_targeted_override"] or position["balance_rescue_override"] or position["cross_family_thesis_priority"]) and not position["override_survival_corridor"]:
                mirror_ok = False

        position["position_flag_mirror_ok"] = bool(mirror_ok)
        return position

    def open_position(self, candidate: Dict, stake_override=None, now_ts=None, regime: str = "unknown", confidence: float = 1.0):
        key = self._build_key(candidate)

        if key in self._open_keys:
            return None

        try:
            stake = float(stake_override) if stake_override is not None else float(self.default_stake)
        except Exception:
            stake = float(self.default_stake)

        if stake <= 0:
            return None

        if self.balance_free < stake:
            return None

        entry_price = self._safe_float(candidate.get("price", 0.0), 0.0)
        if entry_price <= 0:
            return None

        qty_shares = stake / entry_price
        entry_score = self._safe_float(candidate.get("score", 0.0), 0.0)

        position = {
            "position_key": key,
            "market_id": candidate.get("market_id"),
            "question": candidate.get("question"),
            "outcome_name": candidate.get("outcome_name"),
            "entry_price": entry_price,
            "current_price": entry_price,
            "current_price_source": "entry",
            "last_price_truth": entry_price,
            "last_price_candidate": entry_price,
            "max_price_seen": entry_price,
            "min_price_seen": entry_price,
            "stake_usd": float(stake),
            "initial_stake_usd": float(stake),
            "qty_shares": float(qty_shares),
            "cost_basis_remaining": float(stake),
            "market_value": float(stake),
            "score": entry_score,
            "entry_score": entry_score,
            "category": candidate.get("theme", candidate.get("category", "unknown")),
            "theme": candidate.get("theme", "unknown"),
            "cluster": candidate.get("cluster", "unknown"),
            "market_type": candidate.get("market_type", "general_binary"),
            "family_key": candidate.get("family_key", key),
            "reason": candidate.get("_entry_reason", "unknown"),
            "opened_at_ts": now_ts,
            "last_seen_ts": now_ts,
            "age_cycles": 0,
            "cycles_since_scale_in": 0,
            "missing_cycles": 0,
            "silent_cycles": 0,
            "dead_cycles": 0,
            "peak_unrealized_pnl_pct": 0.0,
            "current_unrealized_pnl_pct": 0.0,
            "current_unrealized_pnl_usd": 0.0,
            "realized_pnl_usd": 0.0,
            "scale_in_count": 0,
            "partial_take_count": 0,
            "thesis_weaken_count": 0,
            "partial_tp_1_done": False,
            "partial_tp_2_done": False,
            "profit_lock_seed_done": False,
            "last_action": "OPEN",
            "last_action_ts": now_ts,
            "open_regime": regime,
            "last_regime": regime,
            "confidence_at_entry": float(confidence),
            "last_score": entry_score,
            "prev_score": entry_score,
            "entry_pressure_density": self._safe_float(candidate.get("pressure_density", 0.0), 0.0),
            "entry_pressure_count": int(candidate.get("pressure_count", 0) or 0),
            "last_pressure_density": self._safe_float(candidate.get("pressure_density", 0.0), 0.0),
            "prev_pressure_density": self._safe_float(candidate.get("pressure_density", 0.0), 0.0),
            "max_pressure_density_seen": self._safe_float(candidate.get("pressure_density", 0.0), 0.0),
            "last_pressure_count": int(candidate.get("pressure_count", 0) or 0),
            "prev_pressure_count": int(candidate.get("pressure_count", 0) or 0),
            "max_pressure_count_seen": int(candidate.get("pressure_count", 0) or 0),
            "entry_trend_strength": self._safe_float(candidate.get("price_trend_strength", 0.0), 0.0),
            "entry_delta": self._safe_float(candidate.get("price_delta", 0.0), 0.0),
            "entry_window_delta": self._safe_float(candidate.get("price_delta_window", 0.0), 0.0),
            "last_trend_strength": self._safe_float(candidate.get("price_trend_strength", 0.0), 0.0),
            "prev_trend_strength": self._safe_float(candidate.get("price_trend_strength", 0.0), 0.0),
            "last_delta": self._safe_float(candidate.get("price_delta", 0.0), 0.0),
            "prev_delta": self._safe_float(candidate.get("price_delta", 0.0), 0.0),
            "last_window_delta": self._safe_float(candidate.get("price_delta_window", 0.0), 0.0),
            "prev_window_delta": self._safe_float(candidate.get("price_delta_window", 0.0), 0.0),
            "stake_model": dict(candidate.get("_stake_model", {}) or {}),
            "edge_registry_version": dict(candidate.get("_stake_model", {}) or {}).get("edge_registry_version", EDGE_REGISTRY_VERSION),
            "political_override_entry": bool(dict(candidate.get("_stake_model", {}) or {}).get("political_override_entry", False)),
            "political_targeted_override": bool(dict(candidate.get("_stake_model", {}) or {}).get("political_targeted_override", False)),
            "balance_rescue_override": bool(dict(candidate.get("_stake_model", {}) or {}).get("balance_rescue_override", False)),
            "cross_family_thesis_priority": bool(dict(candidate.get("_stake_model", {}) or {}).get("cross_family_thesis_priority", False)),
            "cross_family_priority_cycles": int(dict(candidate.get("_stake_model", {}) or {}).get("cross_family_priority_cycles", 0) or 0),
            "political_hold_window": bool(dict(candidate.get("_stake_model", {}) or {}).get("political_hold_window", False)),
            "political_hold_window_cycles": int(dict(candidate.get("_stake_model", {}) or {}).get("political_hold_window_cycles", 0) or 0),
            "override_survival_corridor": bool(dict(candidate.get("_stake_model", {}) or {}).get("override_survival_corridor", False)),
            "political_override_reason": dict(candidate.get("_stake_model", {}) or {}).get("political_override_reason"),
            "political_override_hint_reason": dict(candidate.get("_stake_model", {}) or {}).get("political_override_hint_reason"),
            "political_override_hint_strength": float(dict(candidate.get("_stake_model", {}) or {}).get("political_override_hint_strength", 0.0) or 0.0),
            "flag_mirror_audit_expected": bool(dict(candidate.get("_stake_model", {}) or {}).get("flag_mirror_audit_expected", False)),
            "relief_escalation_active": bool(dict(candidate.get("_stake_model", {}) or {}).get("relief_escalation_active", False)),
            "relief_escalation_signal": dict(candidate.get("_stake_model", {}) or {}).get("relief_escalation_signal"),
            "relief_escalation_cap": float(dict(candidate.get("_stake_model", {}) or {}).get("relief_escalation_cap", 0.0) or 0.0),
            "relief_escalation_force_delayed": bool(dict(candidate.get("_stake_model", {}) or {}).get("relief_escalation_force_delayed", False)),
            "relief_escalation_micro_scout": bool(dict(candidate.get("_stake_model", {}) or {}).get("relief_escalation_micro_scout", False)),
            "adaptive_calm_relief_active": bool(dict(candidate.get("_stake_model", {}) or {}).get("adaptive_calm_relief_active", False)),
            "adaptive_calm_relief_signal": dict(candidate.get("_stake_model", {}) or {}).get("adaptive_calm_relief_signal"),
            "adaptive_calm_relief_cap": self._safe_float(dict(candidate.get("_stake_model", {}) or {}).get("adaptive_calm_relief_cap", 0.0), 0.0),
            "adaptive_calm_relief_force_delayed": bool(dict(candidate.get("_stake_model", {}) or {}).get("adaptive_calm_relief_force_delayed", False)),
            "adaptive_calm_relief_micro_scout": bool(dict(candidate.get("_stake_model", {}) or {}).get("adaptive_calm_relief_micro_scout", False)),
            "dead_money_compression_active": bool(dict(candidate.get("_stake_model", {}) or {}).get("dead_money_compression_active", False)),
            "dead_money_compression_signal": dict(candidate.get("_stake_model", {}) or {}).get("dead_money_compression_signal"),
            "dead_money_compression_cap": self._safe_float(dict(candidate.get("_stake_model", {}) or {}).get("dead_money_compression_cap", 0.0), 0.0),
            "follow_through_risk_score": int(dict(candidate.get("_stake_model", {}) or {}).get("follow_through_risk_score", 0) or 0),
            "follow_through_structure_votes": int(dict(candidate.get("_stake_model", {}) or {}).get("follow_through_structure_votes", 0) or 0),
            "follow_through_memory_pressure": int(dict(candidate.get("_stake_model", {}) or {}).get("follow_through_memory_pressure", 0) or 0),
            "follow_through_force_delayed": bool(dict(candidate.get("_stake_model", {}) or {}).get("follow_through_force_delayed", False)),
            "follow_through_scout_mode": bool(dict(candidate.get("_stake_model", {}) or {}).get("follow_through_scout_mode", False)),
            "thin_pressure_truth_active": bool(dict(candidate.get("_stake_model", {}) or {}).get("thin_pressure_truth_active", False)),
            "thin_pressure_truth_signal": dict(candidate.get("_stake_model", {}) or {}).get("thin_pressure_truth_signal"),
            "thin_pressure_truth_cap": self._safe_float(dict(candidate.get("_stake_model", {}) or {}).get("thin_pressure_truth_cap", 0.0), 0.0),
            "thin_pressure_truth_risk": int(dict(candidate.get("_stake_model", {}) or {}).get("thin_pressure_truth_risk", 0) or 0),
            "thin_pressure_truth_force_delayed": bool(dict(candidate.get("_stake_model", {}) or {}).get("thin_pressure_truth_force_delayed", False)),
            "elite_recovery_override": bool(dict(candidate.get("_stake_model", {}) or {}).get("elite_recovery_override", False)),
            "elite_recovery_signal": dict(candidate.get("_stake_model", {}) or {}).get("elite_recovery_signal"),
            "elite_recovery_clamp_active": bool(dict(candidate.get("_stake_model", {}) or {}).get("elite_recovery_clamp_active", False)),
            "elite_recovery_clamp_cap": self._safe_float(dict(candidate.get("_stake_model", {}) or {}).get("elite_recovery_clamp_cap", 0.0), 0.0),
            "elite_recovery_force_delayed": bool(dict(candidate.get("_stake_model", {}) or {}).get("elite_recovery_force_delayed", False)),
            "elite_recovery_micro_scout": bool(dict(candidate.get("_stake_model", {}) or {}).get("elite_recovery_micro_scout", False)),
        }

        position = self._ensure_political_position_flags(position)

        self.balance_free -= stake
        self.paper_spent_total += stake
        self.open_cost_basis += stake

        self.open_positions.append(position)
        self._open_keys.add(key)

        return dict(position)

    def _mark_to_market(self, position: Dict, candidate: Dict, now_ts=None, regime: str = "unknown") -> None:
        prev_price = self._safe_float(position.get("current_price", position.get("entry_price", 0.0)), position.get("entry_price", 0.0))
        candidate_price = self._safe_float(candidate.get("price", prev_price), prev_price)
        truth_price = self._safe_float(candidate.get("_truth_price", candidate_price), candidate_price)
        current_price = truth_price if truth_price > 0 else candidate_price
        price_source = "truth_map" if candidate.get("_truth_price") is not None else ("market_map" if candidate.get("price") is not None else "position_fallback")
        position["current_price"] = current_price
        position["current_price_source"] = price_source
        position["last_price_truth"] = truth_price
        position["last_price_candidate"] = candidate_price
        position["max_price_seen"] = max(self._safe_float(position.get("max_price_seen", current_price), current_price), current_price)
        position["min_price_seen"] = min(self._safe_float(position.get("min_price_seen", current_price), current_price), current_price)
        if abs(current_price - prev_price) > 1e-9:
            print("TRACE | mark_to_market_update | source={} | prev_price={:.6f} | current_price={:.6f} | candidate_price={:.6f} | truth_price={:.6f} | age={} | {}".format(
                price_source, prev_price, current_price, candidate_price, truth_price, int(position.get("age_cycles", 0) or 0), (position.get("question") or "")[:72]
            ))
        elif abs(candidate_price - truth_price) > 1e-9:
            print("TRACE | price_truth_mismatch | source={} | current_price={:.6f} | candidate_price={:.6f} | truth_price={:.6f} | age={} | {}".format(
                price_source, current_price, candidate_price, truth_price, int(position.get("age_cycles", 0) or 0), (position.get("question") or "")[:72]
            ))

        qty_shares = self._safe_float(position.get("qty_shares", 0.0), 0.0)
        cost_basis_remaining = self._safe_float(position.get("cost_basis_remaining", 0.0), 0.0)
        market_value = qty_shares * current_price

        candidate_score = candidate.get("score", None)
        if candidate_score is None:
            score_value = self._safe_float(position.get("last_score", position.get("score", 0.0)), position.get("score", 0.0))
        else:
            score_value = self._safe_float(candidate_score, self._safe_float(position.get("last_score", position.get("score", 0.0)), position.get("score", 0.0)))

        prev_score = self._safe_float(position.get("last_score", position.get("score", score_value)), position.get("score", score_value))
        prev_pressure_density = self._safe_float(position.get("last_pressure_density", 0.0), 0.0)
        prev_pressure_count = int(position.get("last_pressure_count", 0) or 0)
        prev_trend_strength = self._safe_float(position.get("last_trend_strength", 0.0), 0.0)
        prev_delta = self._safe_float(position.get("last_delta", 0.0), 0.0)
        prev_window_delta = self._safe_float(position.get("last_window_delta", 0.0), 0.0)

        current_density = self._safe_float(candidate.get("pressure_density", prev_pressure_density), prev_pressure_density)
        current_count = int(candidate.get("pressure_count", prev_pressure_count) or 0)
        current_trend = self._safe_float(candidate.get("price_trend_strength", prev_trend_strength), prev_trend_strength)
        current_delta = self._safe_float(candidate.get("price_delta", prev_delta), prev_delta)
        current_window_delta = self._safe_float(candidate.get("price_delta_window", prev_window_delta), prev_window_delta)

        position["market_value"] = market_value
        position["stake_usd"] = cost_basis_remaining
        position["score"] = score_value
        position["prev_score"] = prev_score
        position["last_score"] = score_value
        position["prev_pressure_density"] = prev_pressure_density
        position["last_pressure_density"] = current_density
        position["max_pressure_density_seen"] = max(
            self._safe_float(position.get("max_pressure_density_seen", current_density), current_density),
            current_density
        )
        position["prev_pressure_count"] = prev_pressure_count
        position["last_pressure_count"] = current_count
        position["max_pressure_count_seen"] = max(int(position.get("max_pressure_count_seen", current_count) or 0), current_count)
        position["prev_trend_strength"] = prev_trend_strength
        position["last_trend_strength"] = current_trend
        position["prev_delta"] = prev_delta
        position["last_delta"] = current_delta
        position["prev_window_delta"] = prev_window_delta
        position["last_window_delta"] = current_window_delta
        position["last_seen_ts"] = now_ts
        position["last_regime"] = regime
        position["age_cycles"] = int(position.get("age_cycles", 0)) + 1
        position["cycles_since_scale_in"] = int(position.get("cycles_since_scale_in", 0)) + 1
        position["missing_cycles"] = 0

        meaningful_activity = (
            abs(current_delta) >= 0.0045 or
            abs(current_window_delta) >= 0.0065 or
            current_density >= 0.18 or
            current_count >= 2 or
            current_trend >= 0.82
        )
        if meaningful_activity:
            position["silent_cycles"] = 0
        else:
            position["silent_cycles"] = int(position.get("silent_cycles", 0) or 0) + 1

        if cost_basis_remaining > 0:
            unrealized_pnl_usd = market_value - cost_basis_remaining
            unrealized_pnl_pct = (market_value / cost_basis_remaining) - 1.0
        else:
            unrealized_pnl_usd = 0.0
            unrealized_pnl_pct = 0.0

        score_improving = score_value >= max(prev_score + 0.06, prev_score * 1.06 if prev_score > 0 else 0.06)
        weak_state = (
            not meaningful_activity and
            not score_improving and
            unrealized_pnl_pct <= 0.03 and
            self._safe_float(position.get("peak_unrealized_pnl_pct", 0.0), 0.0) <= 0.06
        )
        if weak_state:
            position["dead_cycles"] = int(position.get("dead_cycles", 0) or 0) + 1
        else:
            position["dead_cycles"] = max(0, int(position.get("dead_cycles", 0) or 0) - 1)

        prev_peak_unrealized_pnl_pct = self._safe_float(position.get("peak_unrealized_pnl_pct", unrealized_pnl_pct), unrealized_pnl_pct)
        position["prev_peak_unrealized_pnl_pct"] = prev_peak_unrealized_pnl_pct
        position["current_unrealized_pnl_usd"] = unrealized_pnl_usd
        position["current_unrealized_pnl_pct"] = unrealized_pnl_pct
        position["peak_unrealized_pnl_pct"] = max(
            prev_peak_unrealized_pnl_pct,
            unrealized_pnl_pct
        )
        if prev_peak_unrealized_pnl_pct <= 0.0001 and position["peak_unrealized_pnl_pct"] > 0.0001:
            position["post_first_peak_protect_until_age"] = max(
                int(position.get("age_cycles", 0) or 0) + 2,
                int(position.get("post_first_peak_protect_until_age", 0) or 0)
            )
            print(
                "TRACE | post_first_peak_protect_arm | age={} | protect_until_age={} | pnl={:.4f} | peak={:.4f} | {}".format(
                    int(position.get("age_cycles", 0) or 0),
                    int(position.get("post_first_peak_protect_until_age", 0) or 0),
                    unrealized_pnl_pct,
                    position["peak_unrealized_pnl_pct"],
                    (position.get("question") or position.get("outcome_name") or "unknown")[:72],
                )
            )
        if position["peak_unrealized_pnl_pct"] <= 0.0001:
            position["zero_peak_cycles"] = int(position.get("zero_peak_cycles", 0) or 0) + 1
        else:
            position["zero_peak_cycles"] = 0

    def _build_event(self, action: str, position: Dict, extra: Optional[Dict] = None) -> Dict:
        position = self._ensure_political_position_flags(position)
        event = {
            "action": action,
            "position_key": position.get("position_key"),
            "market_id": position.get("market_id"),
            "question": position.get("question"),
            "outcome_name": position.get("outcome_name"),
            "reason": position.get("reason"),
            "theme": position.get("theme"),
            "cluster": position.get("cluster"),
            "market_type": position.get("market_type", "general_binary"),
            "family_key": position.get("family_key", position.get("position_key")),
            "entry_price": round(self._safe_float(position.get("entry_price", 0.0), 0.0), 6),
            "current_price": round(self._safe_float(position.get("current_price", 0.0), 0.0), 6),
            "current_price_source": position.get("current_price_source", "unknown"),
            "last_price_truth": round(self._safe_float(position.get("last_price_truth", position.get("current_price", 0.0)), position.get("current_price", 0.0)), 6),
            "last_price_candidate": round(self._safe_float(position.get("last_price_candidate", position.get("current_price", 0.0)), position.get("current_price", 0.0)), 6),
            "cost_basis_remaining": round(self._safe_float(position.get("cost_basis_remaining", 0.0), 0.0), 6),
            "market_value": round(self._safe_float(position.get("market_value", 0.0), 0.0), 6),
            "qty_shares": round(self._safe_float(position.get("qty_shares", 0.0), 0.0), 6),
            "age_cycles": int(position.get("age_cycles", 0) or 0),
            "scale_in_count": int(position.get("scale_in_count", 0) or 0),
            "partial_take_count": int(position.get("partial_take_count", 0) or 0),
            "unrealized_pnl_pct": round(self._safe_float(position.get("current_unrealized_pnl_pct", 0.0), 0.0), 6),
            "unrealized_pnl_usd": round(self._safe_float(position.get("current_unrealized_pnl_usd", 0.0), 0.0), 6),
            "realized_pnl_usd_total_position": round(self._safe_float(position.get("realized_pnl_usd", 0.0), 0.0), 6),
            "peak_unrealized_pnl_pct": round(self._safe_float(position.get("peak_unrealized_pnl_pct", 0.0), 0.0), 6),
            "score": round(self._safe_float(position.get("score", 0.0), 0.0), 6),
            "pressure_density": round(self._safe_float(position.get("last_pressure_density", 0.0), 0.0), 6),
            "pressure_count": int(position.get("last_pressure_count", 0) or 0),
            "price_delta": round(self._safe_float(position.get("last_delta", 0.0), 0.0), 6),
            "price_delta_window": round(self._safe_float(position.get("last_window_delta", 0.0), 0.0), 6),
            "price_trend_strength": round(self._safe_float(position.get("last_trend_strength", 0.0), 0.0), 6),
            "silent_cycles": int(position.get("silent_cycles", 0) or 0),
            "dead_cycles": int(position.get("dead_cycles", 0) or 0),
            "last_regime": position.get("last_regime", "unknown"),
            "delayed_entry_mode": dict(position.get("stake_model", {}) or {}).get("delayed_entry_mode", "none"),
            "delayed_entry_signal": dict(position.get("stake_model", {}) or {}).get("delayed_entry_signal"),
            "political_override_entry": bool(position.get("political_override_entry", False)),
            "political_targeted_override": bool(position.get("political_targeted_override", False)),
            "balance_rescue_override": bool(position.get("balance_rescue_override", False)),
            "cross_family_thesis_priority": bool(position.get("cross_family_thesis_priority", False)),
            "cross_family_priority_cycles": int(position.get("cross_family_priority_cycles", 0) or 0),
            "political_hold_window": bool(position.get("political_hold_window", False)),
            "political_hold_window_cycles": int(position.get("political_hold_window_cycles", 0) or 0),
            "override_survival_corridor": bool(position.get("override_survival_corridor", False)),
            "position_flag_mirror_ok": bool(position.get("position_flag_mirror_ok", False)),
            "stake_model": dict(position.get("stake_model", {}) or {}),
        }
        if extra:
            event.update(extra)
        return event

    def _realize_fraction(self, position: Dict, close_fraction: float, exit_price: Optional[float] = None) -> Optional[Dict]:
        close_fraction = max(0.0, min(float(close_fraction), 1.0))
        if close_fraction <= 0:
            return None

        qty_before = self._safe_float(position.get("qty_shares", 0.0), 0.0)
        cost_before = self._safe_float(position.get("cost_basis_remaining", 0.0), 0.0)
        if qty_before <= 0 or cost_before <= 0:
            return None

        px = self._safe_float(exit_price, self._safe_float(position.get("current_price", 0.0), 0.0))
        if px <= 0:
            return None

        qty_sold = qty_before * close_fraction
        cost_released = cost_before * close_fraction
        proceeds = qty_sold * px
        realized_pnl = proceeds - cost_released

        position["qty_shares"] = max(0.0, qty_before - qty_sold)
        position["cost_basis_remaining"] = max(0.0, cost_before - cost_released)
        position["stake_usd"] = position["cost_basis_remaining"]
        position["market_value"] = self._safe_float(position.get("qty_shares", 0.0), 0.0) * px
        position["current_unrealized_pnl_usd"] = position["market_value"] - position["cost_basis_remaining"]

        if position["cost_basis_remaining"] > 0:
            position["current_unrealized_pnl_pct"] = (position["market_value"] / position["cost_basis_remaining"]) - 1.0
        else:
            position["current_unrealized_pnl_pct"] = 0.0

        position["realized_pnl_usd"] = self._safe_float(position.get("realized_pnl_usd", 0.0), 0.0) + realized_pnl
        self.realized_pnl_total += realized_pnl
        self.balance_free += proceeds
        self.open_cost_basis = max(0.0, self.open_cost_basis - cost_released)

        return {
            "close_fraction": close_fraction,
            "qty_sold": qty_sold,
            "cost_released": cost_released,
            "proceeds": proceeds,
            "realized_pnl_usd": realized_pnl,
            "exit_price": px,
            "remaining_qty": position["qty_shares"],
            "remaining_cost_basis": position["cost_basis_remaining"],
        }

    def partial_close_position(self, key: str, close_fraction: float, exit_reason: str, now_ts=None) -> Optional[Dict]:
        position = self._find_position(key)
        if not position:
            return None

        result = self._realize_fraction(position, close_fraction, exit_price=position.get("current_price"))
        if not result:
            return None

        position["partial_take_count"] = int(position.get("partial_take_count", 0)) + 1
        position["last_action"] = "PARTIAL_CLOSE"
        position["last_action_ts"] = now_ts

        return self._build_event("PARTIAL_CLOSE", position, {
            "exit_reason": exit_reason,
            "close_fraction": round(result["close_fraction"], 6),
            "qty_sold": round(result["qty_sold"], 6),
            "cost_released": round(result["cost_released"], 6),
            "proceeds": round(result["proceeds"], 6),
            "realized_pnl_usd": round(result["realized_pnl_usd"], 6),
            "exit_price_truth": round(result["exit_price"], 6),
            "exit_price_source": position.get("current_price_source", "unknown"),
            "remaining_qty": round(result["remaining_qty"], 6),
            "remaining_cost_basis": round(result["remaining_cost_basis"], 6),
        })

    def close_position(self, key: str, exit_reason: str, now_ts=None) -> Optional[Dict]:
        position = self._find_position(key)
        if not position:
            return None

        result = self._realize_fraction(position, 1.0, exit_price=position.get("current_price"))
        if not result:
            return None

        closed_snapshot = dict(position)
        closed_snapshot["closed_at_ts"] = now_ts
        closed_snapshot["close_reason"] = exit_reason
        self.closed_positions.append(closed_snapshot)

        self.open_positions = [p for p in self.open_positions if p.get("position_key") != key]
        self._open_keys.discard(key)

        return self._build_event("CLOSE", closed_snapshot, {
            "exit_reason": exit_reason,
            "close_fraction": 1.0,
            "qty_sold": round(result["qty_sold"], 6),
            "cost_released": round(result["cost_released"], 6),
            "proceeds": round(result["proceeds"], 6),
            "realized_pnl_usd": round(result["realized_pnl_usd"], 6),
            "remaining_qty": 0.0,
            "remaining_cost_basis": 0.0,
        })

    def scale_in_position(self, key: str, add_stake: float, candidate: Dict, now_ts=None) -> Optional[Dict]:
        position = self._find_position(key)
        if not position:
            return None

        try:
            add_stake = float(add_stake)
        except Exception:
            return None

        if add_stake <= 0:
            return None

        if self.balance_free < add_stake:
            return None

        add_price = self._safe_float(candidate.get("price", position.get("current_price", 0.0)), position.get("current_price", 0.0))
        if add_price <= 0:
            return None

        add_qty = add_stake / add_price

        self.balance_free -= add_stake
        self.paper_spent_total += add_stake
        self.open_cost_basis += add_stake

        old_qty = self._safe_float(position.get("qty_shares", 0.0), 0.0)
        old_cost = self._safe_float(position.get("cost_basis_remaining", 0.0), 0.0)

        new_qty = old_qty + add_qty
        new_cost = old_cost + add_stake

        position["qty_shares"] = new_qty
        position["cost_basis_remaining"] = new_cost
        position["stake_usd"] = new_cost
        position["entry_price"] = (new_cost / new_qty) if new_qty > 0 else add_price
        position["scale_in_count"] = int(position.get("scale_in_count", 0)) + 1
        position["cycles_since_scale_in"] = 0
        position["last_action"] = "SCALE_IN"
        position["last_action_ts"] = now_ts

        self._mark_to_market(position, candidate, now_ts=now_ts, regime=position.get("last_regime", "unknown"))

        return self._build_event("SCALE_IN", position, {
            "add_stake": round(add_stake, 6),
            "add_price": round(add_price, 6),
            "add_qty": round(add_qty, 6),
            "new_entry_price": round(self._safe_float(position.get("entry_price", 0.0), 0.0), 6),
        })

    def _should_trailing_momentum_exit(self, position: Dict, candidate: Dict) -> Optional[Dict]:
        reason = position.get("reason", "unknown")
        if reason not in {"momentum", "momentum_override", "multicycle_momentum_override", "score+momentum", "pre_momentum", "score+pre_momentum"}:
            return None

        age_cycles = int(position.get("age_cycles", 0) or 0)
        if age_cycles < 2:
            return None

        pnl_pct = self._safe_float(position.get("current_unrealized_pnl_pct", 0.0), 0.0)
        peak_pnl_pct = self._safe_float(position.get("peak_unrealized_pnl_pct", pnl_pct), pnl_pct)
        retrace_from_peak = peak_pnl_pct - pnl_pct
        delta_1 = self._safe_float(candidate.get("price_delta", position.get("last_delta", 0.0)), position.get("last_delta", 0.0))
        delta_window = self._safe_float(candidate.get("price_delta_window", position.get("last_window_delta", 0.0)), position.get("last_window_delta", 0.0))
        trend = self._safe_float(candidate.get("price_trend_strength", position.get("last_trend_strength", 0.0)), position.get("last_trend_strength", 0.0))
        score = self._safe_float(candidate.get("score", position.get("last_score", 0.0)), position.get("last_score", 0.0))
        entry_score = self._safe_float(position.get("entry_score", score), score)

        heavy_rollover = delta_1 <= -0.006 or delta_window <= -0.003
        weak_followthrough = delta_1 <= 0.0 and delta_window <= 0.0015 and trend < 0.62
        score_fade = score < max(0.32, entry_score * 0.72)

        if peak_pnl_pct >= 0.22 and retrace_from_peak >= 0.12 and (heavy_rollover or weak_followthrough):
            return {
                "exit_reason": "trailing_momentum_exit",
                "peak_pnl_pct": peak_pnl_pct,
                "retrace_from_peak_pct": retrace_from_peak,
                "trailing_signal": "soft_rollover",
            }

        if peak_pnl_pct >= 0.35 and retrace_from_peak >= 0.16 and (heavy_rollover or score_fade or trend < 0.55):
            return {
                "exit_reason": "trailing_momentum_exit",
                "peak_pnl_pct": peak_pnl_pct,
                "retrace_from_peak_pct": retrace_from_peak,
                "trailing_signal": "lock_gain",
            }

        if peak_pnl_pct >= 0.55 and retrace_from_peak >= 0.20:
            return {
                "exit_reason": "trailing_momentum_exit",
                "peak_pnl_pct": peak_pnl_pct,
                "retrace_from_peak_pct": retrace_from_peak,
                "trailing_signal": "hard_trail",
            }

        return None

    def _should_pressure_decay_exit(self, position: Dict, candidate: Dict) -> Optional[Dict]:
        reason = position.get("reason", "unknown")
        if reason not in {"pressure", "score+pressure"}:
            return None

        age_cycles = int(position.get("age_cycles", 0) or 0)
        if age_cycles < 1:
            return None

        pnl_pct = self._safe_float(position.get("current_unrealized_pnl_pct", 0.0), 0.0)
        density = self._safe_float(candidate.get("pressure_density", position.get("last_pressure_density", 0.0)), position.get("last_pressure_density", 0.0))
        prev_density = self._safe_float(position.get("prev_pressure_density", density), density)
        peak_density = max(
            self._safe_float(position.get("max_pressure_density_seen", density), density),
            self._safe_float(position.get("entry_pressure_density", density), density),
            density,
        )
        count = int(candidate.get("pressure_count", position.get("last_pressure_count", 0)) or 0)
        prev_count = int(position.get("prev_pressure_count", count) or 0)
        peak_count = max(int(position.get("max_pressure_count_seen", count) or 0), int(position.get("entry_pressure_count", count) or 0), count)
        delta_window = self._safe_float(candidate.get("price_delta_window", position.get("last_window_delta", 0.0)), position.get("last_window_delta", 0.0))
        delta_1 = self._safe_float(candidate.get("price_delta", position.get("last_delta", 0.0)), position.get("last_delta", 0.0))
        trend = self._safe_float(candidate.get("price_trend_strength", position.get("last_trend_strength", 0.0)), position.get("last_trend_strength", 0.0))
        score = self._safe_float(candidate.get("score", position.get("last_score", 0.0)), position.get("last_score", 0.0))
        entry_score = self._safe_float(position.get("entry_score", score), score)

        density_drop_ratio = 0.0
        if peak_density > 0:
            density_drop_ratio = 1.0 - (density / peak_density)

        count_drop_ratio = 0.0
        if peak_count > 0:
            count_drop_ratio = 1.0 - (float(count) / float(peak_count))

        flow_fading = density <= (prev_density + 0.01) and count <= prev_count
        impulse_gone = delta_window <= 0.0025 and delta_1 <= 0.0035 and trend < 0.72
        score_soft = score < max(0.30, entry_score * 0.86)
        early_dead_flow = age_cycles >= 2 and peak_density >= 0.35 and density <= max(0.18, peak_density * 0.50) and count <= max(1, int(round(peak_count * 0.6)))

        if density <= 0.12 and count <= 1 and impulse_gone and pnl_pct <= 0.15:
            return {
                "exit_reason": "pressure_decay_exit",
                "pressure_decay_signal": "pressure_flatlined",
                "pressure_decay_ratio": density_drop_ratio,
                "pressure_count_decay_ratio": count_drop_ratio,
            }

        if density_drop_ratio >= 0.40 and flow_fading and impulse_gone and pnl_pct <= 0.18:
            return {
                "exit_reason": "pressure_decay_exit",
                "pressure_decay_signal": "flow_collapsed",
                "pressure_decay_ratio": density_drop_ratio,
                "pressure_count_decay_ratio": count_drop_ratio,
            }

        if density_drop_ratio >= 0.30 and flow_fading and score_soft and delta_window <= 0.001 and pnl_pct <= 0.10:
            return {
                "exit_reason": "pressure_decay_exit",
                "pressure_decay_signal": "thesis_evaporated",
                "pressure_decay_ratio": density_drop_ratio,
                "pressure_count_decay_ratio": count_drop_ratio,
            }

        if early_dead_flow and abs(delta_window) <= 0.0025 and trend < 0.76 and pnl_pct <= 0.15:
            return {
                "exit_reason": "pressure_decay_exit",
                "pressure_decay_signal": "soft_decay_exit",
                "pressure_decay_ratio": density_drop_ratio,
                "pressure_count_decay_ratio": count_drop_ratio,
            }

        if age_cycles >= 3 and peak_density >= 0.35 and density <= max(0.16, peak_density * 0.40) and count <= 1 and abs(delta_window) <= 0.002 and trend < 0.70 and pnl_pct <= 0.20:
            return {
                "exit_reason": "pressure_decay_exit",
                "pressure_decay_signal": "continuation_died",
                "pressure_decay_ratio": density_drop_ratio,
                "pressure_count_decay_ratio": count_drop_ratio,
            }

        return None


    def _should_delayed_light_exit(self, position: Dict, candidate: Dict) -> Optional[Dict]:
        stake_model = dict(position.get("stake_model", {}) or {})
        delayed_mode = stake_model.get("delayed_entry_mode")
        compression_active = bool(
            stake_model.get("dead_money_compression_active", False)
            or stake_model.get("follow_through_scout_mode", False)
            or stake_model.get("thin_pressure_truth_active", False)
            or position.get("dead_money_compression_active", False)
            or position.get("thin_pressure_truth_active", False)
        )
        thin_truth_active = bool(
            stake_model.get("thin_pressure_truth_active", False)
            or position.get("thin_pressure_truth_active", False)
        )
        if delayed_mode != "light" and not compression_active:
            return None

        age_cycles = int(position.get("age_cycles", 0) or 0)
        if age_cycles < 2 or age_cycles > 7:
            return None

        pnl_pct = self._safe_float(position.get("current_unrealized_pnl_pct", 0.0), 0.0)
        peak_pnl_pct = self._safe_float(position.get("peak_unrealized_pnl_pct", pnl_pct), pnl_pct)
        density = self._safe_float(candidate.get("pressure_density", position.get("last_pressure_density", 0.0)), position.get("last_pressure_density", 0.0))
        count = int(candidate.get("pressure_count", position.get("last_pressure_count", 0)) or 0)
        trend = self._safe_float(candidate.get("price_trend_strength", position.get("last_trend_strength", 0.0)), position.get("last_trend_strength", 0.0))
        delta_window = abs(self._safe_float(candidate.get("price_delta_window", position.get("last_window_delta", 0.0)), position.get("last_window_delta", 0.0)))
        delta_1 = abs(self._safe_float(candidate.get("price_delta", position.get("last_delta", 0.0)), position.get("last_delta", 0.0)))
        score = self._safe_float(candidate.get("score", position.get("last_score", 0.0)), position.get("last_score", 0.0))
        entry_score = self._safe_float(position.get("entry_score", score), score)

        exit_reason = "follow_through_compression_fail" if compression_active and delayed_mode != "light" else "delayed_admission_fail"
        promotion_mode = "compression" if compression_active and delayed_mode != "light" else "light"

        if compression_active and delayed_mode != "light":
            if age_cycles >= 2 and pnl_pct <= 0.025 and peak_pnl_pct <= 0.04 and density < 0.16 and count <= 1 and delta_window <= 0.0025 and delta_1 <= 0.0030 and trend < 0.82:
                return {
                    "exit_reason": exit_reason,
                    "admission_signal": "compression_no_confirmation",
                    "peak_pnl_pct": peak_pnl_pct,
                    "promotion_mode": promotion_mode,
                }

            if age_cycles >= 3 and pnl_pct <= 0.04 and peak_pnl_pct <= 0.06 and score <= max(0.48, entry_score * 0.98) and density < 0.20 and delta_window <= 0.0032 and trend < 0.84:
                return {
                    "exit_reason": exit_reason,
                    "admission_signal": "compression_thesis_never_built",
                    "peak_pnl_pct": peak_pnl_pct,
                    "promotion_mode": promotion_mode,
                }
        else:
            if age_cycles >= 2 and pnl_pct <= 0.03 and peak_pnl_pct <= 0.05 and density < 0.18 and count <= 1 and delta_window <= 0.0028 and delta_1 <= 0.0032 and trend < 0.80:
                return {
                    "exit_reason": exit_reason,
                    "admission_signal": "no_confirmation_after_light_admit",
                    "peak_pnl_pct": peak_pnl_pct,
                    "promotion_mode": promotion_mode,
                }

            if age_cycles >= 3 and pnl_pct <= 0.05 and peak_pnl_pct <= 0.08 and score <= max(0.45, entry_score * 0.97) and density < 0.22 and delta_window <= 0.0038 and trend < 0.83:
                return {
                    "exit_reason": exit_reason,
                    "admission_signal": "thesis_never_built_after_light_admit",
                    "peak_pnl_pct": peak_pnl_pct,
                    "promotion_mode": promotion_mode,
                }

        return None

    def _should_failed_runner_quarantine_exit(self, position: Dict, candidate: Dict) -> Optional[Dict]:
        position = self._ensure_political_position_flags(position)
        market_type = str(position.get("market_type", "general_binary") or "general_binary")
        if market_type not in {"short_burst_catalyst", "speculative_hype", "valuation_ladder", "legal_resolution"}:
            return None

        reason = str(position.get("reason", "unknown") or "unknown")
        if reason not in {"score", "pressure", "score+pressure", "pre_momentum", "score+pre_momentum", "multicycle_momentum_override", "score+momentum", "momentum_override"}:
            return None
        if bool(position.get("partial_tp_1_done", False)):
            return None

        age_cycles = int(position.get("age_cycles", 0) or 0)
        if age_cycles < 1 or age_cycles > 4:
            return None

        stake_model = dict(position.get("stake_model", {}) or {})
        pnl_pct = self._safe_float(position.get("current_unrealized_pnl_pct", 0.0), 0.0)
        peak_pnl_pct = self._safe_float(position.get("peak_unrealized_pnl_pct", pnl_pct), pnl_pct)
        score = self._safe_float(candidate.get("score", position.get("last_score", 0.0)), position.get("last_score", 0.0))
        density = self._safe_float(candidate.get("pressure_density", position.get("last_pressure_density", 0.0)), position.get("last_pressure_density", 0.0))
        pressure_count = int(candidate.get("pressure_count", position.get("last_pressure_count", 0)) or 0)
        trend = self._safe_float(candidate.get("price_trend_strength", position.get("last_trend_strength", 0.0)), position.get("last_trend_strength", 0.0))
        delta_window = abs(self._safe_float(candidate.get("price_delta_window", position.get("last_window_delta", 0.0)), position.get("last_window_delta", 0.0)))
        delta_1 = abs(self._safe_float(candidate.get("price_delta", position.get("last_delta", 0.0)), position.get("last_delta", 0.0)))
        entry_density = self._safe_float(position.get("entry_pressure_density", density), density)
        entry_count = int(position.get("entry_pressure_count", pressure_count) or pressure_count)
        entry_trend = self._safe_float(position.get("entry_trend_strength", trend), trend)
        entry_window = abs(self._safe_float(position.get("entry_window_delta", delta_window), delta_window))
        entry_score = self._safe_float(position.get("entry_score", score), score)
        zero_peak_cycles = int(position.get("zero_peak_cycles", age_cycles) or age_cycles)
        delayed_mode = str(stake_model.get("delayed_entry_mode", "none") or "none")
        forced_delayed = bool(stake_model.get("follow_through_force_delayed", False) or stake_model.get("thin_pressure_truth_force_delayed", False))
        elite_recovery_entry = bool(stake_model.get("elite_recovery_override", False))

        if elite_recovery_entry and age_cycles <= 2 and pnl_pct > -0.08:
            return None

        late_runner_profile = bool(
            delayed_mode in {"confirmed", "light"}
            or forced_delayed
            or entry_density <= 0.20
            or entry_count <= 1
            or entry_trend < 0.90
            or entry_window <= 0.0030
        )
        structure_never_built = bool(
            pressure_count <= max(1, entry_count)
            and density <= max(0.18, entry_density * 0.82)
            and trend < max(0.84, entry_trend)
            and delta_window <= max(0.0035, entry_window + 0.0015)
            and delta_1 <= 0.0045
        )
        expensive_false_runner = bool(
            entry_score >= (1.24 if market_type == "short_burst_catalyst" else 1.10)
            and late_runner_profile
            and structure_never_built
            and peak_pnl_pct <= 0.035
        )

        if expensive_false_runner and age_cycles >= 2 and pnl_pct <= -0.055:
            return {
                "exit_reason": "failed_runner_quarantine_exit",
                "follow_through_signal": "false_runner_never_confirmed",
                "peak_pnl_pct": peak_pnl_pct,
                "pressure_decay_ratio": (1.0 - (density / max(entry_density, 0.0001))) if entry_density > 0 else 0.0,
            }

        if expensive_false_runner and age_cycles >= 3 and pnl_pct <= 0.015 and density <= 0.18 and pressure_count <= 1 and trend < 0.82 and delta_window <= max(0.0030, entry_window + 0.0010):
            return {
                "exit_reason": "failed_runner_quarantine_exit",
                "follow_through_signal": "late_runner_air_pocket",
                "peak_pnl_pct": peak_pnl_pct,
                "pressure_decay_ratio": (1.0 - (density / max(entry_density, 0.0001))) if entry_density > 0 else 0.0,
            }

        return None

    def _should_zero_churn_guillotine_exit(self, position: Dict, candidate: Dict) -> Optional[Dict]:
        position = self._ensure_political_position_flags(position)
        market_type = str(position.get("market_type", "general_binary") or "general_binary")
        if market_type not in ZERO_CHURN_MARKET_TYPES:
            return None

        reason = str(position.get("reason", "unknown") or "unknown")
        if reason not in {"score", "pressure", "score+pressure", "pre_momentum", "score+pre_momentum", "multicycle_momentum_override", "score+momentum", "momentum_override"}:
            return None
        if bool(position.get("partial_tp_1_done", False)):
            return None

        age_cycles = int(position.get("age_cycles", 0) or 0)
        if age_cycles < 2 or age_cycles > 7:
            return None

        stake_model = dict(position.get("stake_model", {}) or {})
        pnl_pct = self._safe_float(position.get("current_unrealized_pnl_pct", 0.0), 0.0)
        peak_pnl_pct = self._safe_float(position.get("peak_unrealized_pnl_pct", pnl_pct), pnl_pct)
        score = self._safe_float(candidate.get("score", position.get("last_score", 0.0)), position.get("last_score", 0.0))
        density = self._safe_float(candidate.get("pressure_density", position.get("last_pressure_density", 0.0)), position.get("last_pressure_density", 0.0))
        pressure_count = int(candidate.get("pressure_count", position.get("last_pressure_count", 0)) or 0)
        trend = self._safe_float(candidate.get("price_trend_strength", position.get("last_trend_strength", 0.0)), position.get("last_trend_strength", 0.0))
        delta_window = abs(self._safe_float(candidate.get("price_delta_window", position.get("last_window_delta", 0.0)), position.get("last_window_delta", 0.0)))
        delta_1 = abs(self._safe_float(candidate.get("price_delta", position.get("last_delta", 0.0)), position.get("last_delta", 0.0)))
        entry_density = self._safe_float(position.get("entry_pressure_density", density), density)
        entry_count = int(position.get("entry_pressure_count", pressure_count) or pressure_count)
        entry_trend = self._safe_float(position.get("entry_trend_strength", trend), trend)
        entry_window = abs(self._safe_float(position.get("entry_window_delta", delta_window), delta_window))
        entry_score = self._safe_float(position.get("entry_score", score), score)
        zero_peak_cycles = int(position.get("zero_peak_cycles", age_cycles) or age_cycles)
        delayed_mode = str(stake_model.get("delayed_entry_mode", "none") or "none")
        compression_active = bool(stake_model.get("dead_money_compression_active", False) or stake_model.get("follow_through_scout_mode", False) or position.get("dead_money_compression_active", False))
        thin_truth_active = bool(stake_model.get("thin_pressure_truth_active", False) or position.get("thin_pressure_truth_active", False))
        forced_delayed = bool(stake_model.get("follow_through_force_delayed", False) or stake_model.get("thin_pressure_truth_force_delayed", False))
        elite_recovery_entry = bool(stake_model.get("elite_recovery_override", False))

        if elite_recovery_entry and age_cycles <= 2 and pnl_pct > -0.06:
            return None

        weak_live = bool(pressure_count <= max(1, entry_count) and density <= max(0.20, entry_density * 0.84) and trend < max(0.84, entry_trend) and delta_window <= max(0.0035, entry_window + 0.0015) and delta_1 <= 0.0040)
        flat_progress = bool(pnl_pct <= 0.025 and peak_pnl_pct <= 0.045)
        zero_churn_profile = bool(flat_progress and weak_live and (forced_delayed or compression_active or thin_truth_active or delayed_mode in {"confirmed", "light"} or market_type in {"general_binary", "narrative_long_tail", "valuation_ladder", "sports_award_longshot", "speculative_hype", "short_burst_catalyst"}))
        late_zombie = bool(
            (age_cycles >= 4 and zero_peak_cycles >= 4 and peak_pnl_pct <= 0.015 and pnl_pct <= 0.01 and pressure_count <= 1 and density <= max(0.16, entry_density * 0.82) and delta_window <= max(0.0040, entry_window + 0.0015))
            or (age_cycles >= 6 and zero_peak_cycles >= 5 and peak_pnl_pct <= 0.030 and pnl_pct <= 0.015 and pressure_count <= 1 and density <= max(0.18, entry_density * 0.88) and delta_window <= max(0.0050, entry_window + 0.0025))
        )

        if zero_churn_profile and age_cycles >= 2 and (score <= max(1.04, entry_score * 0.99) or late_zombie or (zero_peak_cycles >= 4 and peak_pnl_pct <= 0.010 and pnl_pct <= 0.005)):
            print(
                "TRACE | zero_churn_guillotine_exit | market_type={} | reason={} | age={} | zero_peak_cycles={} | late_zombie={} | pnl={:.4f} | peak={:.4f} | score={:.3f} | pd={:.3f} | trend={:.3f} | win={:.4f} | {}".format(
                    market_type, reason, age_cycles, zero_peak_cycles, int(bool(late_zombie)), pnl_pct, peak_pnl_pct, score, density, trend, delta_window, (position.get("question") or "")[:72],
                )
            )
            return {
                "exit_reason": "zero_churn_guillotine_exit",
                "follow_through_signal": "zero_progress_airlock",
                "peak_pnl_pct": peak_pnl_pct,
                "pressure_decay_ratio": (1.0 - (density / max(entry_density, 0.0001))) if entry_density > 0 else 0.0,
            }

        return None


    def _should_no_follow_through_exit(self, position: Dict, candidate: Dict) -> Optional[Dict]:
        position = self._ensure_political_position_flags(position)
        reason = position.get("reason", "unknown")
        stake_model = dict(position.get("stake_model", {}) or {})
        compression_active = bool(
            stake_model.get("dead_money_compression_active", False)
            or stake_model.get("follow_through_scout_mode", False)
            or position.get("dead_money_compression_active", False)
        )
        thin_truth_active = bool(
            stake_model.get("thin_pressure_truth_active", False)
            or position.get("thin_pressure_truth_active", False)
        )
        if reason not in {"pressure", "score+pressure", "pre_momentum", "score+pre_momentum"} and not compression_active and not thin_truth_active:
            return None

        age_cycles = int(position.get("age_cycles", 0) or 0)
        if age_cycles < 2 or age_cycles > 6:
            return None

        political_override_entry = bool(position.get("political_override_entry", stake_model.get("political_override_entry", False)))
        political_targeted_override = bool(position.get("political_targeted_override", stake_model.get("political_targeted_override", False)))
        balance_rescue_override = bool(position.get("balance_rescue_override", stake_model.get("balance_rescue_override", False)))
        cross_family_thesis_priority = bool(position.get("cross_family_thesis_priority", stake_model.get("cross_family_thesis_priority", False)))
        cross_family_priority_cycles = int(position.get("cross_family_priority_cycles", stake_model.get("cross_family_priority_cycles", 0)) or 0)
        political_hold_window = bool(position.get("political_hold_window", stake_model.get("political_hold_window", False)))
        political_hold_window_cycles = int(position.get("political_hold_window_cycles", stake_model.get("political_hold_window_cycles", 0)) or 0)
        override_survival_corridor = bool(position.get("override_survival_corridor", stake_model.get("override_survival_corridor", False)))

        pnl_pct = self._safe_float(position.get("current_unrealized_pnl_pct", 0.0), 0.0)
        peak_pnl_pct = self._safe_float(position.get("peak_unrealized_pnl_pct", pnl_pct), pnl_pct)
        density = self._safe_float(candidate.get("pressure_density", position.get("last_pressure_density", 0.0)), position.get("last_pressure_density", 0.0))
        peak_density = max(
            self._safe_float(position.get("max_pressure_density_seen", density), density),
            self._safe_float(position.get("entry_pressure_density", density), density),
            density,
        )
        count = int(candidate.get("pressure_count", position.get("last_pressure_count", 0)) or 0)
        peak_count = max(int(position.get("max_pressure_count_seen", count) or 0), int(position.get("entry_pressure_count", count) or 0), count)
        entry_count = int(position.get("entry_pressure_count", 0) or 0)
        delta_window = self._safe_float(candidate.get("price_delta_window", position.get("last_window_delta", 0.0)), position.get("last_window_delta", 0.0))
        delta_1 = self._safe_float(candidate.get("price_delta", position.get("last_delta", 0.0)), position.get("last_delta", 0.0))
        trend = self._safe_float(candidate.get("price_trend_strength", position.get("last_trend_strength", 0.0)), position.get("last_trend_strength", 0.0))
        entry_window = self._safe_float(position.get("entry_window_delta", 0.0), 0.0)

        hold_cycles = max(
            political_hold_window_cycles,
            cross_family_priority_cycles,
            5 if (political_targeted_override or balance_rescue_override) else 0
        )
        if political_override_entry and (political_targeted_override or balance_rescue_override or cross_family_thesis_priority or political_hold_window):
            if age_cycles <= max(hold_cycles, 5) and peak_pnl_pct <= 0.12 and pnl_pct > -0.12:
                return None
        elif political_override_entry and override_survival_corridor:
            if age_cycles <= 4 and peak_pnl_pct <= 0.10 and pnl_pct > -0.10:
                return None

        weak_profit = pnl_pct <= 0.10 and peak_pnl_pct <= 0.12
        no_flow_extension = count <= max(1, entry_count) and peak_count <= max(2, entry_count + 1)
        pressure_never_built = peak_density < 0.55 or density <= peak_density * 0.80
        action_flat = delta_window <= max(0.0025, entry_window + 0.0020) and delta_1 <= 0.004
        trend_not_expanding = trend < 0.80

        if thin_truth_active and age_cycles >= 2 and weak_profit and density <= 0.22 and count <= max(1, entry_count) and abs(delta_window) <= max(0.0035, entry_window + 0.0025) and abs(delta_1) <= 0.0040 and trend < 0.82:
            return {
                "exit_reason": "no_follow_through_exit",
                "follow_through_signal": "thin_pressure_truth_never_confirmed",
                "peak_pnl_pct": peak_pnl_pct,
                "pressure_decay_ratio": (1.0 - (density / peak_density)) if peak_density > 0 else 0.0,
            }

        if compression_active and age_cycles >= 2 and weak_profit and density <= 0.24 and count <= max(1, entry_count) and abs(delta_window) <= max(0.0030, entry_window + 0.0020) and abs(delta_1) <= 0.0035 and trend < 0.78:
            return {
                "exit_reason": "no_follow_through_exit",
                "follow_through_signal": "compression_flow_never_confirmed",
                "peak_pnl_pct": peak_pnl_pct,
                "pressure_decay_ratio": (1.0 - (density / peak_density)) if peak_density > 0 else 0.0,
            }

        if weak_profit and no_flow_extension and action_flat and trend_not_expanding and pressure_never_built:
            return {
                "exit_reason": "no_follow_through_exit",
                "follow_through_signal": "initial_burst_failed",
                "peak_pnl_pct": peak_pnl_pct,
                "pressure_decay_ratio": (1.0 - (density / peak_density)) if peak_density > 0 else 0.0,
            }

        if age_cycles >= 3 and weak_profit and density <= 0.30 and count <= 1 and abs(delta_window) <= 0.0025 and trend < 0.75:
            return {
                "exit_reason": "no_follow_through_exit",
                "follow_through_signal": "flow_never_confirmed",
                "peak_pnl_pct": peak_pnl_pct,
                "pressure_decay_ratio": (1.0 - (density / peak_density)) if peak_density > 0 else 0.0,
            }

        return None

    def _should_time_decay_exit(self, position: Dict, candidate: Dict) -> Optional[Dict]:
        position = self._ensure_political_position_flags(position)
        reason = position.get("reason", "unknown")
        age_cycles = int(position.get("age_cycles", 0) or 0)
        pnl_pct = self._safe_float(position.get("current_unrealized_pnl_pct", 0.0), 0.0)
        peak_pnl_pct = self._safe_float(position.get("peak_unrealized_pnl_pct", pnl_pct), pnl_pct)
        score = self._safe_float(candidate.get("score", position.get("last_score", 0.0)), position.get("last_score", 0.0))
        entry_score = self._safe_float(position.get("entry_score", score), score)
        density = self._safe_float(candidate.get("pressure_density", position.get("last_pressure_density", 0.0)), position.get("last_pressure_density", 0.0))
        trend = self._safe_float(candidate.get("price_trend_strength", position.get("last_trend_strength", 0.0)), position.get("last_trend_strength", 0.0))
        delta_window = self._safe_float(candidate.get("price_delta_window", position.get("last_window_delta", 0.0)), position.get("last_window_delta", 0.0))
        current_price = self._safe_float(candidate.get("price", position.get("entry_price", 0.0)), position.get("entry_price", 0.0))
        entry_price = self._safe_float(position.get("entry_price", 0.0), 0.0)

        if entry_price > 0:
            price_progress = abs((current_price / entry_price) - 1.0)
        else:
            price_progress = 0.0

        stake_model = dict(position.get("stake_model", {}) or {})
        political_override_entry = bool(position.get("political_override_entry", stake_model.get("political_override_entry", False)))
        political_targeted_override = bool(position.get("political_targeted_override", stake_model.get("political_targeted_override", False)))
        balance_rescue_override = bool(position.get("balance_rescue_override", stake_model.get("balance_rescue_override", False)))
        override_survival_corridor = bool(position.get("override_survival_corridor", stake_model.get("override_survival_corridor", False)))
        cross_family_thesis_priority = bool(position.get("cross_family_thesis_priority", stake_model.get("cross_family_thesis_priority", False)))
        cross_family_priority_cycles = int(position.get("cross_family_priority_cycles", stake_model.get("cross_family_priority_cycles", 0)) or 0)
        political_hold_window = bool(position.get("political_hold_window", stake_model.get("political_hold_window", False)))
        political_hold_window_cycles = int(position.get("political_hold_window_cycles", stake_model.get("political_hold_window_cycles", 0)) or 0)

        hold_cycles = max(
            political_hold_window_cycles,
            cross_family_priority_cycles,
            7 if (political_targeted_override or balance_rescue_override) else 0
        )
        if political_override_entry and (political_targeted_override or balance_rescue_override or cross_family_thesis_priority or political_hold_window):
            if age_cycles <= max(hold_cycles, 7) and peak_pnl_pct <= 0.12 and pnl_pct > -0.14:
                return None
        elif political_override_entry and override_survival_corridor:
            if age_cycles <= 5 and peak_pnl_pct <= 0.10 and pnl_pct > -0.10:
                return None

        if reason in {"pressure", "score+pressure", "pre_momentum", "score+pre_momentum"}:
            if age_cycles >= 3 and -0.08 <= pnl_pct <= 0.07 and peak_pnl_pct <= 0.12 and price_progress <= 0.030 and density < 0.35 and abs(delta_window) <= 0.0040 and trend < 0.82:
                return {
                    "exit_reason": "time_decay_exit",
                    "time_decay_signal": "stale_recycle",
                    "price_progress": price_progress,
                }

        if reason == "score":
            if age_cycles >= 5 and -0.08 <= pnl_pct <= 0.05 and peak_pnl_pct <= 0.07 and price_progress <= 0.022 and score <= max(0.56, entry_score * 0.97) and density < 0.18 and abs(delta_window) <= 0.0025:
                return {
                    "exit_reason": "time_decay_exit",
                    "time_decay_signal": "idle_score_recycle",
                    "price_progress": price_progress,
                }

        return None

    def compute_hold_score(self, position: Dict, candidate: Dict = None) -> Dict:
        market_type = position.get("market_type", "general_binary")
        type_profile = self._market_type_exit_profile(market_type)

        pnl_pct = self._safe_float(position.get("current_unrealized_pnl_pct", 0.0), 0.0)
        peak_pnl_pct = self._safe_float(position.get("peak_unrealized_pnl_pct", pnl_pct), pnl_pct)
        age_cycles = int(position.get("age_cycles", 0) or 0)
        cluster = position.get("cluster", "unknown")
        cluster_heat = self._cluster_heat(cluster)
        family_heat = self._family_heat(position.get("family_key", position.get("position_key", "unknown")))
        score = self._safe_float(position.get("last_score", position.get("entry_score", 0.0)), 0.0)
        price_progress = self._safe_float(position.get("price_progress", 0.0), 0.0)
        hold_priority = self._hold_priority(position, candidate or {})

        score_value = 1.0
        score_value += pnl_pct * 2.5
        score_value += min(peak_pnl_pct, 0.20) * 1.25
        score_value += (hold_priority - 1.0) * 0.9
        score_value += min(score, 1.5) * 0.18
        score_value -= max(cluster_heat - 2.5, 0.0) * 0.12
        score_value -= max(age_cycles - int(type_profile.get("stale_age", 8) or 8), 0) * 0.05
        score_value += min(price_progress, 0.10) * 0.5

        return {
            "hold_score": round(score_value, 4),
            "cluster_heat": round(cluster_heat, 4),
            "family_heat": round(family_heat, 4),
            "price_progress": round(price_progress, 4),
            "peak_pnl_pct": round(peak_pnl_pct, 4),
            "market_type": market_type,
        }

    def compute_incoming_edge(self, candidate: Dict, current_regime: str = "normal", incoming_reason: str = "score") -> float:
        market_type = candidate.get("market_type", "general_binary")
        score = self._safe_float(candidate.get("score", 0.0), 0.0)
        density = self._safe_float(candidate.get("pressure_density", 0.0), 0.0)
        trend = self._safe_float(candidate.get("price_trend_strength", 0.0), 0.0)
        survival = self._safe_float(candidate.get("survival_priority", 0.0), 0.0)

        edge = 1.0
        edge += min(score, 1.6) * 0.28
        edge += min(survival, 1.8) * 0.20
        edge += min(density, 0.5) * 0.45
        edge += min(trend, 1.0) * 0.18

        if incoming_reason in {"score+pressure", "score+momentum", "multicycle_momentum_override", "momentum_override"}:
            edge += 0.18

        if current_regime == "normal":
            edge += 0.05

        if market_type in {"short_burst_catalyst", "legal_resolution", "speculative_hype"}:
            edge += 0.06
        elif market_type in {"narrative_long_tail", "sports_award_longshot"}:
            edge -= 0.06

        return round(edge, 4)

    def find_recyclable_position_for_candidate(self, candidate: Dict, current_regime: str = "normal", incoming_reason: str = "score") -> Dict:
        if not self.open_positions:
            candidate["__competition_audit"] = ["portfolio_empty"]
            return None

        for pos in self.open_positions:
            self._ensure_political_position_flags(pos)

        incoming_edge = self.compute_incoming_edge(candidate, current_regime=current_regime, incoming_reason=incoming_reason)

        incoming_family = candidate.get("family_key")
        weakest = None
        audit = []
        for position in self.open_positions:
            key = position.get("position_key")
            if not key:
                continue
            if incoming_family and position.get("family_key") == incoming_family:
                audit.append(_competition_audit_line(position, "skip_same_family", incoming_edge=incoming_edge))
                continue

            stake_model = dict(position.get("stake_model", {}) or {})
            political_override_entry = bool(position.get("political_override_entry", stake_model.get("political_override_entry", False)))
            political_targeted_override = bool(position.get("political_targeted_override", stake_model.get("political_targeted_override", False)))
            balance_rescue_override = bool(position.get("balance_rescue_override", stake_model.get("balance_rescue_override", False)))
            cross_family_thesis_priority = bool(position.get("cross_family_thesis_priority", stake_model.get("cross_family_thesis_priority", False)))
            cross_family_priority_cycles = int(position.get("cross_family_priority_cycles", stake_model.get("cross_family_priority_cycles", 0)) or 0)
            political_hold_window = bool(position.get("political_hold_window", stake_model.get("political_hold_window", False)))
            political_hold_window_cycles = int(position.get("political_hold_window_cycles", stake_model.get("political_hold_window_cycles", 0)) or 0)
            override_survival_corridor = bool(position.get("override_survival_corridor", stake_model.get("override_survival_corridor", False)))
            age_cycles = int(position.get("age_cycles", 0) or 0)
            silent_cycles = int(position.get("silent_cycles", 0) or 0)
            dead_cycles = int(position.get("dead_cycles", 0) or 0)
            pnl_pct = float(position.get("current_unrealized_pnl_pct", 0.0) or 0.0)

            incoming_theme = candidate.get("theme", "unknown")
            incoming_cluster = candidate.get("cluster", "unknown")
            incoming_politics_like = incoming_theme in {"politics", "general"} or incoming_cluster in {"geopolitics", "theme_politics"}
            incoming_cross_priority = bool(candidate.get("_cross_family_thesis_priority", False))

            if political_override_entry:
                protected_cycles = max(cross_family_priority_cycles, political_hold_window_cycles, 0)
                if political_targeted_override and age_cycles <= max(protected_cycles, 7) and pnl_pct > -0.14 and silent_cycles < 6 and dead_cycles < 4:
                    if not (incoming_cross_priority or incoming_politics_like):
                        audit.append(_competition_audit_line(position, "protect_targeted_override", incoming_edge=incoming_edge, detail="cross_required"))
                        continue
                if balance_rescue_override and age_cycles <= max(protected_cycles, 7) and pnl_pct > -0.14 and silent_cycles < 6 and dead_cycles < 4:
                    if not (incoming_cross_priority or incoming_politics_like):
                        audit.append(_competition_audit_line(position, "protect_balance_rescue", incoming_edge=incoming_edge, detail="cross_required"))
                        continue
                if cross_family_thesis_priority and age_cycles <= max(protected_cycles, 8) and pnl_pct > -0.14 and silent_cycles < 6 and dead_cycles < 4:
                    if not (incoming_cross_priority or incoming_politics_like):
                        audit.append(_competition_audit_line(position, "protect_cross_priority", incoming_edge=incoming_edge, detail="cross_required"))
                        continue
                if political_hold_window and age_cycles <= max(protected_cycles, 8) and pnl_pct > -0.14 and silent_cycles < 6 and dead_cycles < 4:
                    if not (incoming_cross_priority or incoming_politics_like):
                        audit.append(_competition_audit_line(position, "protect_hold_window", incoming_edge=incoming_edge, detail="cross_required"))
                        continue
                if override_survival_corridor and age_cycles <= 5 and pnl_pct > -0.10 and silent_cycles < 4 and dead_cycles < 2:
                    audit.append(_competition_audit_line(position, "protect_override_corridor", incoming_edge=incoming_edge))
                    continue

            hold = self.compute_hold_score(position, candidate)
            hold_score = float(hold.get("hold_score", 0.0) or 0.0)
            cluster_heat = float(hold.get("cluster_heat", 0.0) or 0.0)
            family_heat = float(hold.get("family_heat", 0.0) or 0.0)

            immunity_active = fresh_position_immunity(position)
            friction = rotation_friction_penalty(
                position,
                cluster_heat=cluster_heat,
                family_heat=family_heat,
            )

            hold_gap = incoming_edge - hold_score
            threshold = competitive_gap_threshold(
                position,
                cluster_heat=cluster_heat,
                family_heat=family_heat,
                political_override_entry=political_override_entry,
                political_targeted_override=political_targeted_override,
                balance_rescue_override=balance_rescue_override,
                cross_family_thesis_priority=cross_family_thesis_priority,
                political_hold_window=political_hold_window,
            )

            print(
                "TRACE | competition_battle | held={} | incoming={} | hold={:.3f} | in={:.3f} | gap={:.3f} | thr={:.3f} | imm={} | fr={:.3f}".format(
                    (position.get("question") or "")[:32],
                    (candidate.get("question") or "")[:32],
                    hold_score,
                    incoming_edge,
                    hold_gap,
                    threshold,
                    int(immunity_active),
                    friction,
                )
            )

            if immunity_active:
                audit.append(_competition_audit_line(position, "DEFEND_IMMUNITY", hold_score=hold_score, incoming_edge=incoming_edge, gap=hold_gap, threshold=threshold, immunity=1, friction=friction))
                continue

            if hold_gap < threshold:
                audit.append(_competition_audit_line(position, "DEFEND_GAP", hold_score=hold_score, incoming_edge=incoming_edge, gap=hold_gap, threshold=threshold, immunity=0, friction=friction))
                continue

            reason = position.get("reason", "unknown")
            if reason in {"score+pressure", "score+momentum", "multicycle_momentum_override", "score+pre_momentum"} and hold_gap < max(0.50, threshold + 0.08):
                elite_threshold = max(0.50, threshold + 0.08)
                audit.append(_competition_audit_line(position, "DEFEND_ELITE", hold_score=hold_score, incoming_edge=incoming_edge, gap=hold_gap, threshold=elite_threshold, immunity=0, friction=friction, detail="elite_hold"))
                continue

            candidate_info = {
                "position_key": key,
                "hold_score": hold_score,
                "cluster_heat": cluster_heat,
                "price_progress": hold.get("price_progress", 0.0),
                "peak_pnl_pct": hold.get("peak_pnl_pct", 0.0),
                "incoming_edge": incoming_edge,
                "hold_gap": round(hold_gap, 4),
                "rotation_threshold": threshold,
                "family_heat": family_heat,
                "silent_cycles": int(position.get("silent_cycles", 0) or 0),
                "dead_cycles": int(position.get("dead_cycles", 0) or 0),
                "edge_registry_version": position.get("edge_registry_version", EDGE_REGISTRY_VERSION),
                "audit_preview": _competition_audit_line(position, "REPLACE", hold_score=hold_score, incoming_edge=incoming_edge, gap=hold_gap, threshold=threshold, immunity=0, friction=friction),
            }

            audit.append(_competition_audit_line(position, "REPLACE", hold_score=hold_score, incoming_edge=incoming_edge, gap=hold_gap, threshold=threshold, immunity=0, friction=friction))
            if weakest is None or hold_score < weakest["hold_score"]:
                weakest = candidate_info

        candidate["__competition_audit"] = audit[:12]
        if weakest is not None:
            weakest["audit"] = audit[:12]
            weakest["audit_preview"] = " | ".join(audit[:3])
        return weakest

    def has_open_family(self, family_key: str) -> bool:
        if not family_key:
            return False
        for pos in self.open_positions:
            if pos.get("family_key") == family_key:
                return True
        return False

    def family_exposure(self) -> Dict[str, float]:
        result = {}
        for pos in self.open_positions:
            family_key = pos.get("family_key", pos.get("position_key", "unknown"))
            result[family_key] = result.get(family_key, 0.0) + self._safe_float(pos.get("cost_basis_remaining", 0.0), 0.0)
        return result

    def family_position_count(self, family_key: str) -> int:
        if not family_key:
            return 0
        return sum(1 for pos in self.open_positions if pos.get("family_key") == family_key)

    def cluster_position_count(self, cluster: str) -> int:
        if not cluster:
            return 0
        return sum(1 for pos in self.open_positions if pos.get("cluster") == cluster)

    def _family_heat(self, family_key: str) -> float:
        exposures = self.family_exposure()
        return self._safe_float(exposures.get(family_key, 0.0), 0.0)

    def find_family_replacement_for_candidate(self, candidate: Dict, current_regime: str = "normal", incoming_reason: str = "score") -> Dict:
        family_key = candidate.get("family_key")
        if not family_key:
            return None

        incoming_edge = self.compute_incoming_edge(candidate, current_regime=current_regime, incoming_reason=incoming_reason)
        review_mode = str(candidate.get("_family_review_mode") or "")
        weakest = None
        for position in self.open_positions:
            if position.get("family_key") != family_key:
                continue
            hold = self.compute_hold_score(position, candidate)
            hold_score = float(hold.get("hold_score", 0.0) or 0.0)
            silent_cycles = int(position.get("silent_cycles", 0) or 0)
            dead_cycles = int(position.get("dead_cycles", 0) or 0)
            peak_pnl_pct = float(position.get("peak_unrealized_pnl_pct", 0.0) or 0.0)
            pnl_pct = float(position.get("current_unrealized_pnl_pct", 0.0) or 0.0)

            threshold = 0.16
            if position.get("reason") in {"score+pressure", "score+momentum", "multicycle_momentum_override", "score+pre_momentum"}:
                threshold = 0.28
            if position.get("market_type") in {"legal_resolution", "valuation_ladder", "sports_award_longshot"}:
                threshold += 0.06
            if review_mode in {"swap_review", "family_swap_review", "review", "peer_review", "swap"}:
                threshold -= 0.10
            if fresh_position_immunity(position):
                threshold += 0.18
            if dead_cycles >= 2:
                threshold -= 0.08
            if silent_cycles >= 4:
                threshold -= 0.05
            if dead_cycles >= 1 and silent_cycles >= 3:
                threshold -= 0.04
            if peak_pnl_pct <= 0.04:
                threshold -= 0.03
            if pnl_pct <= 0.01:
                threshold -= 0.02
            if position.get("reason") == "score" and pnl_pct <= 0.01:
                threshold -= 0.03

            threshold = max(0.06, threshold)
            hold_gap = incoming_edge - hold_score
            if hold_gap < threshold:
                continue

            info = {
                "position_key": position.get("position_key"),
                "hold_score": hold_score,
                "cluster_heat": hold.get("cluster_heat", 0.0),
                "family_heat": hold.get("family_heat", 0.0),
                "price_progress": hold.get("price_progress", 0.0),
                "peak_pnl_pct": hold.get("peak_pnl_pct", 0.0),
                "incoming_edge": incoming_edge,
                "hold_gap": round(hold_gap, 4),
                "silent_cycles": silent_cycles,
                "dead_cycles": dead_cycles,
                "threshold_used": round(threshold, 4),
            }
            if weakest is None or hold_score < weakest["hold_score"]:
                weakest = info
        return weakest

    def find_cluster_replacement_for_candidate(self, candidate: Dict, current_regime: str = "normal", incoming_reason: str = "score", cluster_filter: str = None) -> Dict:
        target_cluster = cluster_filter or candidate.get("cluster")
        if not target_cluster:
            return None

        incoming_edge = self.compute_incoming_edge(candidate, current_regime=current_regime, incoming_reason=incoming_reason)
        weakest = None
        for position in self.open_positions:
            if position.get("cluster") != target_cluster:
                continue
            hold = self.compute_hold_score(position, candidate)
            hold_score = float(hold.get("hold_score", 0.0) or 0.0)
            hold_gap = incoming_edge - hold_score
            threshold = 0.26
            if position.get("reason") in {"score+pressure", "score+momentum", "multicycle_momentum_override"}:
                threshold = 0.40
            if position.get("family_key") == candidate.get("family_key"):
                threshold -= 0.06
            if hold_gap < threshold:
                continue
            info = {
                "position_key": position.get("position_key"),
                "hold_score": hold_score,
                "cluster_heat": hold.get("cluster_heat", 0.0),
                "family_heat": hold.get("family_heat", 0.0),
                "price_progress": hold.get("price_progress", 0.0),
                "peak_pnl_pct": hold.get("peak_pnl_pct", 0.0),
                "incoming_edge": incoming_edge,
                "hold_gap": round(hold_gap, 4),
            }
            if weakest is None or hold_score < weakest["hold_score"]:
                weakest = info
        return weakest

    def _market_type_exit_profile(self, market_type: str) -> Dict:
        return dict(MARKET_TYPE_EXIT_PROFILE.get(market_type or "general_binary", MARKET_TYPE_EXIT_PROFILE["general_binary"]))

    def _cluster_heat(self, cluster: str) -> float:
        exposures = self.cluster_exposure()
        return self._safe_float(exposures.get(cluster, 0.0), 0.0)

    def _hold_priority(self, position: Dict, candidate: Dict) -> float:
        reason = position.get("reason", "unknown")
        score = self._safe_float(candidate.get("score", position.get("last_score", 0.0)), position.get("last_score", 0.0))
        density = self._safe_float(candidate.get("pressure_density", position.get("last_pressure_density", 0.0)), position.get("last_pressure_density", 0.0))
        trend = self._safe_float(candidate.get("price_trend_strength", position.get("last_trend_strength", 0.0)), position.get("last_trend_strength", 0.0))
        delta_window = abs(self._safe_float(candidate.get("price_delta_window", position.get("last_window_delta", 0.0)), position.get("last_window_delta", 0.0)))
        pnl_pct = self._safe_float(position.get("current_unrealized_pnl_pct", 0.0), 0.0)
        peak_pnl_pct = self._safe_float(position.get("peak_unrealized_pnl_pct", pnl_pct), pnl_pct)
        entry_score = self._safe_float(position.get("entry_score", score), score)
        cluster_heat = self._cluster_heat(position.get("cluster", candidate.get("cluster", "unknown")))
        family_heat = self._family_heat(position.get("family_key", position.get("position_key", "unknown")))
        silent_cycles = int(position.get("silent_cycles", 0) or 0)
        dead_cycles = int(position.get("dead_cycles", 0) or 0)

        priority = score
        if reason in {"score+pressure", "score+momentum", "score+pre_momentum", "multicycle_momentum_override", "momentum_override"}:
            priority += 0.28
        elif reason in {"pressure", "pre_momentum", "momentum", "score"}:
            priority += 0.10
        if density >= 0.25:
            priority += 0.14
        if trend >= 0.85:
            priority += 0.10
        if delta_window >= 0.006:
            priority += 0.08
        if peak_pnl_pct >= 0.06:
            priority += 0.18
        if pnl_pct > 0.02:
            priority += 0.07
        if score >= max(1.15, entry_score * 1.03):
            priority += 0.10
        if cluster_heat >= 4.8:
            priority -= 0.18
        elif cluster_heat >= 4.0:
            priority -= 0.10
        if family_heat >= 2.8:
            priority -= 0.10
        if family_heat >= 4.0:
            priority -= 0.10
        if silent_cycles >= 4:
            priority -= 0.08
        if dead_cycles >= 3:
            priority -= 0.14
        return priority


    def _winner_carry_state(self, position: Dict, candidate: Dict) -> Dict:
        try:
            market_type = str(position.get("market_type", "general_binary") or "general_binary")
            if market_type not in {"short_burst_catalyst", "legal_resolution", "speculative_hype", "valuation_ladder", "sports_award_longshot", "narrative_long_tail"}:
                return {"active": False}

            age_cycles = int(position.get("age_cycles", 0) or 0)
            pnl_pct = self._safe_float(position.get("current_unrealized_pnl_pct", 0.0), 0.0)
            peak_pnl_pct = self._safe_float(position.get("peak_unrealized_pnl_pct", pnl_pct), pnl_pct)
            density = self._safe_float(candidate.get("pressure_density", position.get("last_pressure_density", 0.0)), position.get("last_pressure_density", 0.0))
            pressure_count = int(candidate.get("pressure_count", position.get("last_pressure_count", 0)) or 0)
            trend = self._safe_float(candidate.get("price_trend_strength", position.get("last_trend_strength", 0.0)), position.get("last_trend_strength", 0.0))
            delta_window = abs(self._safe_float(candidate.get("price_delta_window", position.get("last_window_delta", 0.0)), position.get("last_window_delta", 0.0)))
            delta_1 = abs(self._safe_float(candidate.get("price_delta", position.get("last_delta", 0.0)), position.get("last_delta", 0.0)))
            score = self._safe_float(candidate.get("score", position.get("last_score", 0.0)), position.get("last_score", 0.0))
            locked_floor = self._safe_float(position.get("runner_locked_floor_pct", 0.0), 0.0)
            winner_seed_realized = self._safe_float(position.get("winner_seed_realized_usd", 0.0), 0.0)
        except Exception:
            return {"active": False}

        if age_cycles < 3:
            return {"active": False}
        if peak_pnl_pct < 0.028:
            return {"active": False}
        if not (bool(position.get("partial_tp_1_done", False)) or bool(position.get("profit_lock_seed_done", False)) or winner_seed_realized > 0.0):
            return {"active": False}

        strong_follow = bool(
            pressure_count >= 2
            or density >= 0.22
            or (trend >= 0.94 and (delta_window >= 0.0025 or delta_1 >= 0.0025))
            or delta_window >= 0.0040
            or delta_1 >= 0.0040
            or score >= 1.18
        )
        if not strong_follow:
            return {"active": False}

        carry_floor = max(locked_floor, 0.0)
        if market_type == "short_burst_catalyst":
            carry_floor = max(carry_floor, min(0.16, peak_pnl_pct * 0.36))
        elif market_type == "speculative_hype":
            carry_floor = max(carry_floor, min(0.14, peak_pnl_pct * 0.34))
        elif market_type == "legal_resolution":
            carry_floor = max(carry_floor, min(0.12, peak_pnl_pct * 0.30))
        elif market_type == "sports_award_longshot":
            carry_floor = max(carry_floor, min(0.10, peak_pnl_pct * 0.26))
        else:
            carry_floor = max(carry_floor, min(0.12, peak_pnl_pct * 0.28))

        hold_full_exit = (
            market_type in {"short_burst_catalyst", "speculative_hype", "legal_resolution"}
            and peak_pnl_pct >= 0.055
            and pnl_pct >= max(0.010, carry_floor * 0.80)
        )

        return {
            "active": True,
            "market_type": market_type,
            "carry_floor_pct": round(carry_floor, 6),
            "hold_full_exit": bool(hold_full_exit),
            "strong_follow": bool(strong_follow),
            "peak_pnl_pct": round(peak_pnl_pct, 6),
            "pnl_pct": round(pnl_pct, 6),
            "density": round(density, 6),
            "trend": round(trend, 6),
            "window_delta": round(delta_window, 6),
            "pressure_count": int(pressure_count),
            "score": round(score, 6),
        }


    def _should_micro_profit_exit(self, position: Dict, candidate: Dict) -> Optional[Dict]:
        reason = position.get("reason", "unknown")
        if reason not in {
            "score+pressure",
            "pressure",
            "score+momentum",
            "momentum",
            "momentum_override",
            "multicycle_momentum_override",
            "pre_momentum",
            "score+pre_momentum",
            "score",
        }:
            return None

        market_type = position.get("market_type", "general_binary")
        type_profile = self._market_type_exit_profile(market_type)
        profit_lock_bias = float(type_profile.get("profit_lock_bias", 1.0) or 1.0)

        age_cycles = int(position.get("age_cycles", 0) or 0)
        if age_cycles < 2:
            return None

        pnl = self._safe_float(position.get("current_unrealized_pnl_pct", 0.0), 0.0)
        peak = self._safe_float(position.get("peak_unrealized_pnl_pct", pnl), pnl)

        delta = self._safe_float(candidate.get("price_delta", position.get("last_delta", 0.0)), position.get("last_delta", 0.0))
        delta_window = self._safe_float(candidate.get("price_delta_window", position.get("last_window_delta", 0.0)), position.get("last_window_delta", 0.0))
        trend = self._safe_float(candidate.get("price_trend_strength", position.get("last_trend_strength", 0.0)), position.get("last_trend_strength", 0.0))
        density = self._safe_float(candidate.get("pressure_density", position.get("last_pressure_density", 0.0)), position.get("last_pressure_density", 0.0))

        retrace = peak - pnl
        winner_carry = self._winner_carry_state(position, candidate)

        micro_trigger = 0.015 / max(profit_lock_bias, 0.75)
        peak_trigger = 0.015 / max(profit_lock_bias, 0.75)
        if pnl >= micro_trigger and peak >= peak_trigger:
            if winner_carry.get("active", False) and winner_carry.get("hold_full_exit", False):
                print(
                    "TRACE | winner_carry_micro_hold | market_type={} | age={} | pnl={:.4f} | peak={:.4f} | floor={:.4f} | density={:.3f} | trend={:.3f} | win={:.4f} | {}".format(
                        winner_carry.get("market_type", market_type),
                        age_cycles,
                        pnl,
                        peak,
                        float(winner_carry.get("carry_floor_pct", 0.0) or 0.0),
                        density,
                        trend,
                        delta_window,
                        (position.get("question") or position.get("outcome_name") or "unknown")[:72],
                    )
                )
                return None
            if delta <= 0.0 and delta_window <= 0.001 and trend < 0.85:
                return {
                    "exit_reason": "micro_profit_lock",
                    "profit_locked": pnl,
                    "peak_pnl_pct": peak,
                    "retrace": retrace,
                    "type_bias_signal": "profit_lock_profile",
                }

        if peak >= 0.03 and retrace >= 0.015:
            if winner_carry.get("active", False) and pnl >= float(winner_carry.get("carry_floor_pct", 0.0) or 0.0) and winner_carry.get("strong_follow", False):
                return None
            if delta <= 0.0 or delta_window <= 0.0 or trend < 0.70:
                return {
                    "exit_reason": "peak_decay_exit",
                    "peak": peak,
                    "current": pnl,
                    "retrace": retrace,
                    "pressure_density": density,
                }

        if peak >= 0.05 and pnl > 0.0 and retrace >= 0.02 and trend < 0.78:
            if winner_carry.get("active", False) and pnl >= float(winner_carry.get("carry_floor_pct", 0.0) or 0.0):
                return None
            return {
                "exit_reason": "profit_recycle_exit",
                "capital_signal": "reallocated",
                "peak": peak,
                "current": pnl,
                "retrace": retrace,
            }

        return None


    def _should_opportunity_cost_governor_exit(self, position: Dict, candidate: Dict, portfolio_open_count: int) -> Optional[Dict]:
        position = self._ensure_political_position_flags(position)
        age_cycles = int(position.get("age_cycles", 0) or 0)
        if age_cycles < 4:
            return None

        pnl_pct = self._safe_float(position.get("current_unrealized_pnl_pct", 0.0), 0.0)
        peak_pnl_pct = self._safe_float(position.get("peak_unrealized_pnl_pct", pnl_pct), pnl_pct)
        score = self._safe_float(candidate.get("score", position.get("last_score", 0.0)), position.get("last_score", 0.0))
        entry_score = self._safe_float(position.get("entry_score", score), score)
        density = self._safe_float(candidate.get("pressure_density", position.get("last_pressure_density", 0.0)), position.get("last_pressure_density", 0.0))
        trend = self._safe_float(candidate.get("price_trend_strength", position.get("last_trend_strength", 0.0)), position.get("last_trend_strength", 0.0))
        delta_window = abs(self._safe_float(candidate.get("price_delta_window", position.get("last_window_delta", 0.0)), position.get("last_window_delta", 0.0)))
        cluster = position.get("cluster", candidate.get("cluster", "unknown"))
        cluster_heat = self._cluster_heat(cluster)
        family_key = position.get("family_key", position.get("position_key", "unknown"))
        family_heat = self._family_heat(family_key)
        silent_cycles = int(position.get("silent_cycles", 0) or 0)
        dead_cycles = int(position.get("dead_cycles", 0) or 0)
        hold_priority = self._hold_priority(position, candidate)
        market_type = position.get("market_type", "general_binary")

        stake_model = dict(position.get("stake_model", {}) or {})
        political_override_entry = bool(position.get("political_override_entry", stake_model.get("political_override_entry", False)))
        political_targeted_override = bool(position.get("political_targeted_override", stake_model.get("political_targeted_override", False)))
        balance_rescue_override = bool(position.get("balance_rescue_override", stake_model.get("balance_rescue_override", False)))
        override_survival_corridor = bool(position.get("override_survival_corridor", stake_model.get("override_survival_corridor", False)))
        cross_family_thesis_priority = bool(position.get("cross_family_thesis_priority", stake_model.get("cross_family_thesis_priority", False)))
        cross_family_priority_cycles = int(position.get("cross_family_priority_cycles", stake_model.get("cross_family_priority_cycles", 0)) or 0)
        political_hold_window = bool(position.get("political_hold_window", stake_model.get("political_hold_window", False)))
        political_hold_window_cycles = int(position.get("political_hold_window_cycles", stake_model.get("political_hold_window_cycles", 0)) or 0)

        hold_cycles = max(
            political_hold_window_cycles,
            cross_family_priority_cycles,
            8 if (political_targeted_override or balance_rescue_override) else 0
        )
        if political_override_entry and (political_targeted_override or balance_rescue_override or cross_family_thesis_priority or political_hold_window):
            if age_cycles <= max(hold_cycles, 8) and dead_cycles < 4 and silent_cycles < 6 and peak_pnl_pct <= 0.14 and pnl_pct > -0.14:
                return None
        elif political_override_entry and override_survival_corridor:
            if age_cycles <= 6 and dead_cycles < 3 and silent_cycles < 5 and pnl_pct > -0.10:
                return None

        low_signal = density < 0.16 and delta_window <= 0.0032 and trend < 0.74
        score_soft = score <= max(0.52, entry_score * 0.95)

        if dead_cycles >= 4 and age_cycles >= 4 and pnl_pct <= 0.03 and peak_pnl_pct <= 0.06 and low_signal and hold_priority < 1.12:
            return {
                "exit_reason": "dead_capital_decay",
                "capital_signal": "dead_capital_redeploy",
                "hold_priority": hold_priority,
                "cluster_heat": cluster_heat,
                "family_heat": family_heat,
                "silent_cycles": silent_cycles,
                "dead_cycles": dead_cycles,
            }

        long_tail = market_type in {"narrative_long_tail", "valuation_ladder", "sports_award_longshot"}
        if silent_cycles >= (5 if long_tail else 6) and age_cycles >= 5 and pnl_pct <= 0.04 and peak_pnl_pct <= 0.08 and low_signal and hold_priority < 1.18:
            if portfolio_open_count >= 7 or cluster_heat >= 3.0 or family_heat >= 2.8:
                return {
                    "exit_reason": "opportunity_cost_decay",
                    "capital_signal": "silent_capital_redeploy",
                    "hold_priority": hold_priority,
                    "cluster_heat": cluster_heat,
                    "family_heat": family_heat,
                    "silent_cycles": silent_cycles,
                    "dead_cycles": dead_cycles,
                }

        if family_heat >= 2.8 and age_cycles >= 4 and pnl_pct <= 0.05 and peak_pnl_pct <= 0.08 and score_soft and low_signal and hold_priority < 1.20:
            return {
                "exit_reason": "family_slot_recycle",
                "capital_signal": "family_crowding_relief",
                "hold_priority": hold_priority,
                "cluster_heat": cluster_heat,
                "family_heat": family_heat,
                "silent_cycles": silent_cycles,
                "dead_cycles": dead_cycles,
            }

        return None


    def _should_capital_rotation_exit(self, position: Dict, candidate: Dict, portfolio_open_count: int) -> Optional[Dict]:
        age_cycles = int(position.get("age_cycles", 0) or 0)
        if age_cycles < 4:
            return None

        reason = position.get("reason", "unknown")
        cluster = position.get("cluster", candidate.get("cluster", "unknown"))

        pnl_pct = self._safe_float(position.get("current_unrealized_pnl_pct", 0.0), 0.0)
        peak_pnl_pct = self._safe_float(position.get("peak_unrealized_pnl_pct", pnl_pct), pnl_pct)
        score = self._safe_float(candidate.get("score", position.get("last_score", 0.0)), position.get("last_score", 0.0))
        entry_score = self._safe_float(position.get("entry_score", score), score)
        density = self._safe_float(candidate.get("pressure_density", position.get("last_pressure_density", 0.0)), position.get("last_pressure_density", 0.0))
        trend = self._safe_float(candidate.get("price_trend_strength", position.get("last_trend_strength", 0.0)), position.get("last_trend_strength", 0.0))
        delta_window = self._safe_float(candidate.get("price_delta_window", position.get("last_window_delta", 0.0)), position.get("last_window_delta", 0.0))
        price_delta = self._safe_float(candidate.get("price_delta", position.get("last_delta", 0.0)), position.get("last_delta", 0.0))
        current_price = self._safe_float(candidate.get("price", position.get("current_price", 0.0)), position.get("current_price", 0.0))
        entry_price = self._safe_float(position.get("entry_price", current_price), current_price)

        price_progress = 0.0
        if entry_price > 0:
            price_progress = abs((current_price / entry_price) - 1.0)

        cluster_heat = self._cluster_heat(cluster)
        hold_priority = self._hold_priority(position, candidate)
        market_type = position.get("market_type", "general_binary")
        type_profile = self._market_type_exit_profile(market_type)
        rotation_bonus = float(type_profile.get("rotation_bonus", 0.0) or 0.0)

        hold_priority -= rotation_bonus

        weak_rotation_candidate = (
            score <= max(0.72, entry_score * 0.92) and
            density <= 0.12 and
            abs(delta_window) <= 0.0035 and
            trend < 0.72
        )

        stale_positive = (
            pnl_pct > 0.0 and
            peak_pnl_pct <= max(0.08, pnl_pct + 0.03) and
            abs(price_delta) <= 0.003 and
            abs(delta_window) <= 0.004
        )

        rotation_needed = cluster_heat >= 3.4 or portfolio_open_count >= 10
        if market_type in {"valuation_ladder", "sports_award_longshot"} and cluster_heat >= 2.8:
            rotation_needed = True
        weak_priority = hold_priority <= 1.05

        if rotation_needed and weak_rotation_candidate and weak_priority and age_cycles >= 5:
            return {
                "exit_reason": "capital_rotation_exit",
                "capital_signal": "cluster_rotation",
                "hold_priority": hold_priority,
                "cluster_heat": cluster_heat,
                "price_progress": price_progress,
                "peak_pnl_pct": peak_pnl_pct,
                "type_bias_signal": "rotation_profile",
            }

        if stale_positive and weak_priority and age_cycles >= 6 and (portfolio_open_count >= 9 or cluster_heat >= 3.0):
            return {
                "exit_reason": "capital_rotation_exit",
                "capital_signal": "stale_positive_redeploy",
                "hold_priority": hold_priority,
                "cluster_heat": cluster_heat,
                "price_progress": price_progress,
                "peak_pnl_pct": peak_pnl_pct,
                "type_bias_signal": "rotation_profile",
            }

        if reason == "score" and pnl_pct <= 0.02 and age_cycles >= 8 and cluster_heat >= 2.8 and weak_priority and abs(delta_window) <= 0.0025:
            return {
                "exit_reason": "capital_rotation_exit",
                "capital_signal": "score_slot_recycle",
                "hold_priority": hold_priority,
                "cluster_heat": cluster_heat,
                "price_progress": price_progress,
                "peak_pnl_pct": peak_pnl_pct,
                "type_bias_signal": "rotation_profile",
            }

        return None

    def _should_capital_discipline_exit(self, position: Dict, candidate: Dict, portfolio_open_count: int) -> Optional[Dict]:
        position = self._ensure_political_position_flags(position)
        reason = position.get("reason", "unknown")
        age_cycles = int(position.get("age_cycles", 0) or 0)
        pnl_pct = self._safe_float(position.get("current_unrealized_pnl_pct", 0.0), 0.0)
        peak_pnl_pct = self._safe_float(position.get("peak_unrealized_pnl_pct", pnl_pct), pnl_pct)
        score = self._safe_float(candidate.get("score", position.get("last_score", 0.0)), position.get("last_score", 0.0))
        entry_score = self._safe_float(position.get("entry_score", score), score)
        density = self._safe_float(candidate.get("pressure_density", position.get("last_pressure_density", 0.0)), position.get("last_pressure_density", 0.0))
        trend = self._safe_float(candidate.get("price_trend_strength", position.get("last_trend_strength", 0.0)), position.get("last_trend_strength", 0.0))
        delta_window = self._safe_float(candidate.get("price_delta_window", position.get("last_window_delta", 0.0)), position.get("last_window_delta", 0.0))
        current_price = self._safe_float(candidate.get("price", position.get("entry_price", 0.0)), position.get("entry_price", 0.0))
        entry_price = self._safe_float(position.get("entry_price", 0.0), 0.0)
        cluster = position.get("cluster", candidate.get("cluster", "unknown"))
        cluster_heat = self._cluster_heat(cluster)
        hold_priority = self._hold_priority(position, candidate)

        if entry_price > 0:
            price_progress = abs((current_price / entry_price) - 1.0)
        else:
            price_progress = 0.0

        if reason in {"pressure", "score+pressure", "pre_momentum", "score+pre_momentum", "momentum", "score+momentum", "momentum_override", "multicycle_momentum_override"}:
            if age_cycles >= 3 and pnl_pct <= 0.03 and peak_pnl_pct <= 0.08 and density < 0.20 and abs(delta_window) <= 0.0025 and trend < 0.68 and price_progress <= 0.020 and hold_priority < 0.95:
                return {
                    "exit_reason": "idle_hard_exit",
                    "capital_signal": "dead_impulse_recycle",
                    "peak_pnl_pct": peak_pnl_pct,
                    "price_progress": price_progress,
                    "hold_priority": hold_priority,
                }

        if reason == "score":
            if age_cycles >= 7 and pnl_pct <= 0.02 and peak_pnl_pct <= 0.05 and score <= max(0.46, entry_score * 0.92) and density < 0.12 and abs(delta_window) <= 0.0015 and price_progress <= 0.018 and hold_priority < 1.08:
                return {
                    "exit_reason": "idle_hard_exit",
                    "capital_signal": "dead_score_recycle",
                    "peak_pnl_pct": peak_pnl_pct,
                    "price_progress": price_progress,
                    "hold_priority": hold_priority,
                }

        if portfolio_open_count >= 8:
            early_crowded_weak = age_cycles >= 3 and pnl_pct <= 0.02 and peak_pnl_pct <= 0.07 and density < 0.22 and abs(delta_window) <= 0.0030
            score_soft = score <= max(0.50, entry_score * 0.93)
            if early_crowded_weak and (score_soft or reason != "score") and hold_priority < 1.05:
                signal = "early_book_relief"
                if cluster_heat >= 4.2:
                    signal = "cluster_relief"
                return {
                    "exit_reason": "portfolio_pressure_exit",
                    "capital_signal": signal,
                    "peak_pnl_pct": peak_pnl_pct,
                    "price_progress": price_progress,
                    "hold_priority": hold_priority,
                }

        if portfolio_open_count >= 10:
            crowded_weak = age_cycles >= 3 and pnl_pct <= 0.01 and peak_pnl_pct <= 0.06 and density < 0.18 and abs(delta_window) <= 0.0025
            score_soft = score <= max(0.44, entry_score * 0.91)
            if crowded_weak and (score_soft or reason != "score") and hold_priority < 1.12:
                signal = "crowded_book_recycle"
                if cluster_heat >= 4.8:
                    signal = "cluster_trim"
                return {
                    "exit_reason": "opportunity_cost_exit",
                    "capital_signal": signal,
                    "peak_pnl_pct": peak_pnl_pct,
                    "price_progress": price_progress,
                    "hold_priority": hold_priority,
                }

        if portfolio_open_count >= 11 and age_cycles >= 2 and pnl_pct <= 0.00 and peak_pnl_pct <= 0.05 and density < 0.16 and abs(delta_window) <= 0.0022 and hold_priority < 0.98:
            signal = "book_heat_dump"
            if cluster_heat >= 4.8:
                signal = "cluster_heat_dump"
            return {
                "exit_reason": "portfolio_pressure_exit",
                "capital_signal": signal,
                "peak_pnl_pct": peak_pnl_pct,
                "price_progress": price_progress,
                "hold_priority": hold_priority,
            }

        if portfolio_open_count >= 12 and age_cycles >= 2 and pnl_pct < 0.0 and peak_pnl_pct <= 0.04 and density < 0.15 and abs(delta_window) <= 0.002 and hold_priority < 0.92:
            signal = "weakest_recycle"
            if cluster_heat >= 5.2:
                signal = "cluster_weakest_recycle"
            return {
                "exit_reason": "opportunity_cost_exit",
                "capital_signal": signal,
                "peak_pnl_pct": peak_pnl_pct,
                "price_progress": price_progress,
                "hold_priority": hold_priority,
            }

        return None

    def _position_should_weaken(self, position: Dict, candidate: Dict) -> bool:
        reason = position.get("reason", "unknown")

        score = self._safe_float(candidate.get("score", position.get("last_score", position.get("score", 0.0))), position.get("last_score", position.get("score", 0.0)))
        density = self._safe_float(candidate.get("pressure_density", position.get("last_pressure_density", 0.0)), position.get("last_pressure_density", 0.0))
        count = int(candidate.get("pressure_count", position.get("last_pressure_count", 0)) or 0)
        trend = self._safe_float(candidate.get("price_trend_strength", position.get("last_trend_strength", 0.0)), position.get("last_trend_strength", 0.0))
        delta_1 = self._safe_float(candidate.get("price_delta", position.get("last_delta", 0.0)), position.get("last_delta", 0.0))
        delta_window = self._safe_float(candidate.get("price_delta_window", position.get("last_window_delta", 0.0)), position.get("last_window_delta", 0.0))

        entry_price = self._safe_float(position.get("entry_price", 0.0), 0.0)
        current_price = self._safe_float(candidate.get("price", position.get("current_price", entry_price)), position.get("current_price", entry_price))
        age_cycles = int(position.get("age_cycles", 0) or 0)

        price_drawdown = 0.0
        if entry_price > 0:
            price_drawdown = (current_price / entry_price) - 1.0

        # universal deep damage
        if score < 0.22 and price_drawdown <= -0.12:
            return True

        if reason in {"pressure", "score+pressure"}:
            if age_cycles < 2:
                return False
            if density < 0.10 and count <= 0 and delta_window <= -0.001:
                return True
            if price_drawdown <= -0.12 and density < 0.18:
                return True

        if reason in {"pre_momentum", "score+pre_momentum"}:
            if age_cycles < 2:
                return False
            if trend < 0.40 and delta_window <= 0.0 and price_drawdown <= -0.05:
                return True
            if price_drawdown <= -0.11 and trend < 0.52:
                return True

        if reason in {"momentum", "momentum_override", "multicycle_momentum_override", "score+momentum"}:
            if age_cycles < 2:
                return False
            if delta_1 <= -0.012 and delta_window < 0:
                return True
            if trend < 0.35 and delta_window < -0.002:
                return True
            if price_drawdown <= -0.14:
                return True

        if reason == "score":
            entry_score = self._safe_float(position.get("entry_score", score), score)
            last_score = self._safe_float(position.get("last_score", score), score)
            effective_score = max(score, last_score)

            # score-позиции должны жить дольше и не дохнуть на пустом месте
            if age_cycles < 4:
                return False

            if effective_score < max(0.35, entry_score * 0.50) and price_drawdown <= -0.06:
                return True

            if age_cycles >= 6 and effective_score < max(0.30, entry_score * 0.42) and price_drawdown <= -0.03:
                return True

            if price_drawdown <= -0.20:
                return True

            return False

        return False





    def _should_zero_peak_scout_cut(self, position: Dict, candidate: Dict) -> Optional[Dict]:
        position = self._ensure_political_position_flags(position)
        age_cycles = int(position.get("age_cycles", 0) or 0)
        if age_cycles < 2:
            return None

        reason = str(position.get("reason", "unknown") or "unknown")
        market_type = str(position.get("market_type", "general_binary") or "general_binary")
        open_regime = str(position.get("open_regime", position.get("last_regime", "unknown")) or "unknown")
        pnl_pct = self._safe_float(position.get("current_unrealized_pnl_pct", 0.0), 0.0)
        peak_pnl_pct = self._safe_float(position.get("peak_unrealized_pnl_pct", pnl_pct), pnl_pct)
        zero_peak_cycles = int(position.get("zero_peak_cycles", age_cycles) or age_cycles)
        density = self._safe_float(candidate.get("pressure_density", position.get("last_pressure_density", 0.0)), 0.0)
        trend = self._safe_float(candidate.get("price_trend_strength", position.get("last_trend_strength", 0.0)), 0.0)
        delta_window = abs(self._safe_float(candidate.get("price_delta_window", position.get("last_window_delta", 0.0)), 0.0))
        score = self._safe_float(candidate.get("score", position.get("last_score", 0.0)), position.get("last_score", 0.0))

        scout_reasons = {"score", "score+pressure", "score+pre_momentum", "pre_momentum"}
        if reason not in scout_reasons:
            return None
        if market_type not in {"short_burst_catalyst", "valuation_ladder", "narrative_long_tail"}:
            return None
        if bool(position.get("partial_tp_1_done", False)):
            return None
        if bool(position.get("political_hold_window", False)) or bool(position.get("override_survival_corridor", False)):
            return None

        fake_trend = (
            (trend >= 0.95 and density <= 0.125 and delta_window <= 0.0015)
            or (market_type == "valuation_ladder" and trend >= 0.95 and density <= 0.135 and delta_window <= 0.0025)
        )

        delayed_memory = bool(position.get("delayed_entry_memory_active", False))
        entry_price = float(position.get("entry_price", 1.0) or 1.0)
        promising_override = (
            market_type == "short_burst_catalyst"
            and reason in {"score", "score+pre_momentum", "pre_momentum"}
            and score >= 1.04
            and entry_price <= 0.012
            and (
                delayed_memory
                or density >= 0.125
                or delta_window >= 0.0005
            )
        )

        base_promising_scout = (
            (
                reason in {"score+pressure", "score+pre_momentum", "pre_momentum"}
                and (
                    (market_type == "short_burst_catalyst" and score >= 1.35 and ((density >= 0.16 and delta_window >= 0.0015) or density >= 0.20 or delta_window >= 0.0030 or (trend >= 0.85 and not fake_trend))) or
                    (market_type == "valuation_ladder" and score >= 1.12 and ((density >= 0.14 and delta_window >= 0.0015) or density >= 0.18 or delta_window >= 0.0030 or (trend >= 0.85 and not fake_trend))) or
                    (market_type == "narrative_long_tail" and score >= 1.05 and ((density >= 0.10 and delta_window >= 0.0010) or density >= 0.14 or delta_window >= 0.0020 or (trend >= 0.85 and not fake_trend)))
                )
            )
            or (
                reason == "score"
                and (
                    (market_type == "short_burst_catalyst" and score >= 1.52 and (delayed_memory or density >= 0.10 or delta_window >= 0.0020)) or
                    (market_type == "valuation_ladder" and score >= 1.18 and (delayed_memory or density >= 0.10 or delta_window >= 0.0020)) or
                    (market_type == "narrative_long_tail" and score >= 1.08 and (delayed_memory or density >= 0.08 or delta_window >= 0.0015))
                )
            )
            or (delayed_memory and score >= (1.28 if market_type == "short_burst_catalyst" else 1.08) and not fake_trend)
        )

        promising_scout = bool(base_promising_scout or promising_override)

        age_cut = 2 if open_regime == "calm" else 3
        if market_type == "narrative_long_tail":
            age_cut += 1
        if promising_scout:
            age_cut += 1

        print(
            "TRACE | zero_peak_scout_check | market_type={} | reason={} | age={} | zero_peak_cycles={} | age_cut={} | pnl={:.4f} | peak={:.4f} | density={:.3f} | trend={:.3f} | win={:.4f} | score={:.3f} | fake_trend={} | promising={} | override={} | {}".format(
                market_type, reason, age_cycles, zero_peak_cycles, age_cut, pnl_pct, peak_pnl_pct, density, trend, delta_window, score, int(bool(fake_trend)), int(bool(promising_scout)), int(bool(promising_override)),
                (position.get("question") or position.get("outcome_name") or "unknown")[:72],
            )
        )

        if peak_pnl_pct > 0.0001:
            return None

        if fake_trend:
            print(
                "TRACE | fake_trend_filter | market_type={} | reason={} | age={} | zero_peak_cycles={} | density={:.3f} | trend={:.3f} | win={:.4f} | score={:.3f} | {}".format(
                    market_type, reason, age_cycles, zero_peak_cycles, density, trend, delta_window, score,
                    (position.get("question") or position.get("outcome_name") or "unknown")[:72],
                )
            )

        dead_scout = (((density < 0.18 and trend < 0.90 and delta_window <= 0.0045) or fake_trend) and not promising_override)
        if market_type == "valuation_ladder" and fake_trend and zero_peak_cycles >= max(2, age_cut - 1) and pnl_pct <= 0.0:
            print(
                "TRACE | zero_peak_scout_cut | market_type={} | reason={} | age={} | zero_peak_cycles={} | pnl={:.4f} | score={:.3f} | promising={} | override={} | fake_trend=1 | {}".format(
                    market_type, reason, age_cycles, zero_peak_cycles, pnl_pct, score, int(bool(promising_scout)), int(bool(promising_override)),
                    (position.get("question") or position.get("outcome_name") or "unknown")[:72],
                )
            )
            return {
                "exit_reason": "zero_peak_scout_cut",
                "bleed_signal": "score_zero_peak_scout_fake_trend",
                "peak_pnl_pct": peak_pnl_pct,
                "market_type": market_type,
                "score": score,
                "family_key": position.get("family_key"),
                "question": position.get("question"),
                "reason": reason,
            }

        if zero_peak_cycles >= age_cut and pnl_pct <= 0.0 and dead_scout:
            print(
                "TRACE | zero_peak_scout_cut | market_type={} | reason={} | age={} | zero_peak_cycles={} | pnl={:.4f} | score={:.3f} | promising={} | override={} | {}".format(
                    market_type, reason, age_cycles, zero_peak_cycles, pnl_pct, score, int(bool(promising_scout)), int(bool(promising_override)),
                    (position.get("question") or position.get("outcome_name") or "unknown")[:72],
                )
            )
            return {
                "exit_reason": "zero_peak_scout_cut",
                "bleed_signal": "score_zero_peak_scout",
                "peak_pnl_pct": peak_pnl_pct,
                "market_type": market_type,
                "score": score,
                "family_key": position.get("family_key"),
                "question": position.get("question"),
                "reason": reason,
            }

        print(
            "TRACE | zero_peak_scout_hold | market_type={} | reason={} | age={} | zero_peak_cycles={} | age_cut={} | pnl={:.4f} | density={:.3f} | trend={:.3f} | win={:.4f} | score={:.3f} | promising={} | override={} | {}".format(
                market_type, reason, age_cycles, zero_peak_cycles, age_cut, pnl_pct, density, trend, delta_window, score, int(bool(promising_scout)), int(bool(promising_override)),
                (position.get("question") or position.get("outcome_name") or "unknown")[:72],
            )
        )
        return None




    def _should_calm_legal_zero_peak_cut(self, position: Dict, candidate: Dict) -> Optional[Dict]:
        position = self._ensure_political_position_flags(position)
        age_cycles = int(position.get("age_cycles", 0) or 0)
        if age_cycles < 3:
            return None

        reason = str(position.get("reason", "unknown") or "unknown")
        market_type = str(position.get("market_type", "general_binary") or "general_binary")
        open_regime = str(position.get("open_regime", position.get("last_regime", "unknown")) or "unknown")
        pnl_pct = self._safe_float(position.get("current_unrealized_pnl_pct", 0.0), 0.0)
        peak_pnl_pct = self._safe_float(position.get("peak_unrealized_pnl_pct", pnl_pct), pnl_pct)
        zero_peak_cycles = int(position.get("zero_peak_cycles", age_cycles) or age_cycles)
        blocked_count = int(position.get("seed_blocked_zero_peak_count", 0) or 0)
        density = self._safe_float(candidate.get("pressure_density", position.get("last_pressure_density", 0.0)), 0.0)
        trend = self._safe_float(candidate.get("price_trend_strength", position.get("last_trend_strength", 0.0)), 0.0)
        delta_window = abs(self._safe_float(candidate.get("price_delta_window", position.get("last_window_delta", 0.0)), 0.0))
        score = self._safe_float(candidate.get("score", position.get("last_score", 0.0)), position.get("last_score", 0.0))

        if open_regime != "calm":
            return None
        if market_type != "legal_resolution":
            return None
        if reason not in {"score+pressure", "score", "pressure"}:
            return None
        if bool(position.get("partial_tp_1_done", False)):
            return None
        if bool(position.get("political_hold_window", False)) or bool(position.get("override_survival_corridor", False)):
            return None

        print(
            "TRACE | calm_legal_zero_peak_check | market_type={} | reason={} | age={} | zero_peak_cycles={} | blocked_count={} | pnl={:.4f} | peak={:.4f} | density={:.3f} | trend={:.3f} | win={:.4f} | score={:.3f} | {}".format(
                market_type, reason, age_cycles, zero_peak_cycles, blocked_count, pnl_pct, peak_pnl_pct, density, trend, delta_window, score,
                (position.get("question") or position.get("outcome_name") or "unknown")[:72],
            )
        )

        if peak_pnl_pct > 0.0001:
            return None

        if age_cycles >= 3 and zero_peak_cycles >= 3 and pnl_pct <= 0.0 and density <= 0.24 and delta_window <= 0.0080:
            print(
                "TRACE | calm_legal_zero_peak_cut | signal=legal_zero_peak | age={} | blocked_count={} | pnl={:.4f} | score={:.3f} | {}".format(
                    age_cycles, blocked_count, pnl_pct, score,
                    (position.get("question") or position.get("outcome_name") or "unknown")[:72],
                )
            )
            return {
                "exit_reason": "calm_legal_zero_peak_cut",
                "bleed_signal": "calm_legal_zero_peak",
                "peak_pnl_pct": peak_pnl_pct,
                "market_type": market_type,
                "score": score,
                "family_key": position.get("family_key"),
                "question": position.get("question"),
                "reason": reason,
            }

        if blocked_count >= 2 and age_cycles >= 4 and pnl_pct <= 0.0 and density <= 0.28 and delta_window <= 0.0100:
            print(
                "TRACE | seed_stall_compression_cut | signal=legal_seed_stall | age={} | blocked_count={} | pnl={:.4f} | score={:.3f} | {}".format(
                    age_cycles, blocked_count, pnl_pct, score,
                    (position.get("question") or position.get("outcome_name") or "unknown")[:72],
                )
            )
            return {
                "exit_reason": "seed_stall_compression_cut",
                "bleed_signal": "legal_seed_stall",
                "peak_pnl_pct": peak_pnl_pct,
                "market_type": market_type,
                "score": score,
                "family_key": position.get("family_key"),
                "question": position.get("question"),
                "reason": reason,
            }

        return None

    def _should_calm_zero_peak_general_cut(self, position: Dict, candidate: Dict) -> Optional[Dict]:
        position = self._ensure_political_position_flags(position)
        age_cycles = int(position.get("age_cycles", 0) or 0)
        if age_cycles < 2:
            return None

        reason = str(position.get("reason", "unknown") or "unknown")
        market_type = str(position.get("market_type", "general_binary") or "general_binary")
        open_regime = str(position.get("open_regime", position.get("last_regime", "unknown")) or "unknown")
        pnl_pct = self._safe_float(position.get("current_unrealized_pnl_pct", 0.0), 0.0)
        peak_pnl_pct = self._safe_float(position.get("peak_unrealized_pnl_pct", pnl_pct), pnl_pct)
        zero_peak_cycles = int(position.get("zero_peak_cycles", age_cycles) or age_cycles)
        density = self._safe_float(candidate.get("pressure_density", position.get("last_pressure_density", 0.0)), 0.0)
        trend = self._safe_float(candidate.get("price_trend_strength", position.get("last_trend_strength", 0.0)), 0.0)
        delta_window = abs(self._safe_float(candidate.get("price_delta_window", position.get("last_window_delta", 0.0)), 0.0))
        score = self._safe_float(candidate.get("score", position.get("last_score", 0.0)), position.get("last_score", 0.0))

        if open_regime != "calm":
            return None
        if reason not in {"pre_momentum", "score+pre_momentum"}:
            return None
        if market_type != "general_binary":
            return None
        if bool(position.get("partial_tp_1_done", False)):
            return None
        if bool(position.get("political_hold_window", False)) or bool(position.get("override_survival_corridor", False)):
            return None

        print(
            "TRACE | calm_zero_peak_general_check | market_type={} | reason={} | age={} | zero_peak_cycles={} | pnl={:.4f} | peak={:.4f} | density={:.3f} | trend={:.3f} | win={:.4f} | score={:.3f} | {}".format(
                market_type, reason, age_cycles, zero_peak_cycles, pnl_pct, peak_pnl_pct, density, trend, delta_window, score,
                (position.get("question") or position.get("outcome_name") or "unknown")[:72],
            )
        )

        if peak_pnl_pct > 0.0001:
            return None

        if zero_peak_cycles >= 2 and pnl_pct <= 0.0 and density <= 0.125 and delta_window <= 0.0055 and score < 1.05:
            print(
                "TRACE | calm_zero_peak_general_cut | market_type={} | reason={} | age={} | zero_peak_cycles={} | pnl={:.4f} | score={:.3f} | {}".format(
                    market_type, reason, age_cycles, zero_peak_cycles, pnl_pct, score,
                    (position.get("question") or position.get("outcome_name") or "unknown")[:72],
                )
            )
            return {
                "exit_reason": "calm_zero_peak_general_cut",
                "bleed_signal": "calm_general_pre_momentum_zero_peak",
                "peak_pnl_pct": peak_pnl_pct,
                "market_type": market_type,
                "score": score,
                "family_key": position.get("family_key"),
                "question": position.get("question"),
                "reason": reason,
            }

        return None



    def _should_sports_zombie_guillotine_exit(self, position: Dict, candidate: Dict) -> Optional[Dict]:
        position = self._ensure_political_position_flags(position)
        market_type = str(position.get("market_type", "general_binary") or "general_binary")
        if market_type != "sports_award_longshot":
            return None

        reason = str(position.get("reason", "unknown") or "unknown")
        if reason not in {"score", "pressure", "score+pressure", "pre_momentum", "score+pre_momentum", "multicycle_momentum_override", "momentum_override", "score+momentum"}:
            return None
        if bool(position.get("partial_tp_1_done", False)):
            return None

        age_cycles = int(position.get("age_cycles", 0) or 0)
        if age_cycles < 1 or age_cycles > 4:
            return None

        stake_model = dict(position.get("stake_model", {}) or {})
        pnl_pct = self._safe_float(position.get("current_unrealized_pnl_pct", 0.0), 0.0)
        peak_pnl_pct = self._safe_float(position.get("peak_unrealized_pnl_pct", pnl_pct), pnl_pct)
        density = self._safe_float(candidate.get("pressure_density", position.get("last_pressure_density", 0.0)), position.get("last_pressure_density", 0.0))
        pressure_count = int(candidate.get("pressure_count", position.get("last_pressure_count", 0)) or 0)
        trend = self._safe_float(candidate.get("price_trend_strength", position.get("last_trend_strength", 0.0)), position.get("last_trend_strength", 0.0))
        delta_window = abs(self._safe_float(candidate.get("price_delta_window", position.get("last_window_delta", 0.0)), position.get("last_window_delta", 0.0)))
        delta_1 = abs(self._safe_float(candidate.get("price_delta", position.get("last_delta", 0.0)), position.get("last_delta", 0.0)))
        score = self._safe_float(candidate.get("score", position.get("last_score", 0.0)), position.get("last_score", 0.0))

        entry_density = self._safe_float(position.get("entry_pressure_density", density), density)
        entry_count = int(position.get("entry_pressure_count", pressure_count) or pressure_count)
        entry_trend = self._safe_float(position.get("entry_trend_strength", trend), trend)

        zombie_profile = bool(
            stake_model.get("weak_sports_override_brake", False)
            or stake_model.get("dead_money_compression_active", False)
            or stake_model.get("follow_through_scout_mode", False)
            or stake_model.get("thin_pressure_truth_active", False)
            or entry_density <= 0.18
            or entry_count <= 1
            or entry_trend < 0.90
        )

        winner_safe_profile = (
            score >= 0.92
            and density >= 0.30
            and pressure_count >= 3
            and trend >= 0.98
            and delta_window >= 0.0012
            and pnl_pct >= -0.0015
        )
        if winner_safe_profile and age_cycles <= 2:
            return None

        fake_pressure_zombie = (
            age_cycles >= 1
            and peak_pnl_pct <= 0.0001
            and pnl_pct <= 0.0001
            and density >= 0.45
            and pressure_count >= 3
            and trend < 0.82
            and delta_window <= 0.0025
            and delta_1 <= 0.0035
            and score < 0.98
        )
        flat_zombie_no_pop = (
            age_cycles >= 1
            and zombie_profile
            and peak_pnl_pct <= 0.0001
            and pnl_pct <= 0.0001
            and density <= 0.18
            and pressure_count <= 1
            and trend < 0.90
            and delta_window <= 0.0040
            and delta_1 <= 0.0040
            and score < 1.00
        )
        zombie_bleed_no_follow = (
            age_cycles >= 2
            and zombie_profile
            and peak_pnl_pct <= 0.0015
            and pnl_pct <= -0.0010
            and density <= 0.22
            and pressure_count <= 1
            and trend < 0.94
            and delta_window <= 0.0060
            and delta_1 <= 0.0060
            and score < 1.04
        )

        if fake_pressure_zombie or flat_zombie_no_pop or zombie_bleed_no_follow:
            if fake_pressure_zombie:
                signal = "sports_zombie_fake_pressure"
            elif flat_zombie_no_pop:
                signal = "sports_zombie_flat_no_pop"
            else:
                signal = "sports_zombie_bleed_no_follow"
            print("TRACE | sports_zombie_guillotine | signal={} | age={} | pnl={:.4f} | peak={:.4f} | density={:.3f} | trend={:.3f} | win={:.4f} | pcount={} | score={:.3f} | {}".format(
                signal, age_cycles, pnl_pct, peak_pnl_pct, density, trend, delta_window, pressure_count, score, (position.get("question") or position.get("outcome_name") or "unknown")[:72]
            ))
            return {
                "exit_reason": "sports_zombie_guillotine_exit",
                "bleed_signal": signal,
                "peak_pnl_pct": peak_pnl_pct,
                "market_type": market_type,
                "score": score,
                "family_key": position.get("family_key"),
                "question": position.get("question"),
                "reason": reason,
            }
        return None


    def _should_sports_longshot_churn_kill(self, position: Dict, candidate: Dict) -> Optional[Dict]:
        position = self._ensure_political_position_flags(position)
        age_cycles = int(position.get("age_cycles", 0) or 0)
        if age_cycles < 2:
            return None
        market_type = str(position.get("market_type", "general_binary") or "general_binary")
        if market_type != "sports_award_longshot":
            return None
        reason = str(position.get("reason", "unknown") or "unknown")
        if reason not in {"score", "score+pressure", "pre_momentum", "score+pre_momentum", "multicycle_momentum_override", "momentum_override", "score+momentum"}:
            return None
        if bool(position.get("partial_tp_1_done", False)):
            return None
        pnl_pct = self._safe_float(position.get("current_unrealized_pnl_pct", 0.0), 0.0)
        peak_pnl_pct = self._safe_float(position.get("peak_unrealized_pnl_pct", pnl_pct), pnl_pct)
        zero_peak_cycles = int(position.get("zero_peak_cycles", age_cycles) or age_cycles)
        density = self._safe_float(candidate.get("pressure_density", position.get("last_pressure_density", 0.0)), 0.0)
        pressure_count = int(candidate.get("pressure_count", position.get("last_pressure_count", 0)) or 0)
        trend = self._safe_float(candidate.get("price_trend_strength", position.get("last_trend_strength", 0.0)), 0.0)
        delta_window = abs(self._safe_float(candidate.get("price_delta_window", position.get("last_window_delta", 0.0)), 0.0))
        score = self._safe_float(candidate.get("score", position.get("last_score", 0.0)), position.get("last_score", 0.0))
        print("TRACE | sports_longshot_churn_check | reason={} | age={} | zero_peak_cycles={} | pnl={:.4f} | peak={:.4f} | density={:.3f} | trend={:.3f} | win={:.4f} | pcount={} | score={:.3f} | {}".format(reason, age_cycles, zero_peak_cycles, pnl_pct, peak_pnl_pct, density, trend, delta_window, pressure_count, score, (position.get("question") or position.get("outcome_name") or "unknown")[:72]))
        if peak_pnl_pct > 0.0001:
            return None

        winner_safe_profile = (
            score >= 0.90
            and density >= 0.22
            and pressure_count >= 2
            and trend >= 0.98
            and delta_window >= 0.0008
            and pnl_pct >= -0.0015
        )
        if winner_safe_profile and age_cycles <= 3:
            print("TRACE | sports_winner_safe_bypass | reason={} | age={} | zero_peak_cycles={} | pnl={:.4f} | score={:.3f} | {}".format(
                reason, age_cycles, zero_peak_cycles, pnl_pct, score, (position.get("question") or position.get("outcome_name") or "unknown")[:72]
            ))
            return None

        print("TRACE | sports_zero_peak_fire_check | reason={} | age={} | zero_peak_cycles={} | pnl={:.4f} | peak={:.4f} | density={:.3f} | trend={:.3f} | win={:.4f} | pcount={} | score={:.3f} | {}".format(reason, age_cycles, zero_peak_cycles, pnl_pct, peak_pnl_pct, density, trend, delta_window, pressure_count, score, (position.get("question") or position.get("outcome_name") or "unknown")[:72]))

        loser_target = (
            score < 0.90
            or density < 0.22
            or pressure_count < 2
            or trend < 0.98
            or delta_window < 0.0012
        )

        zero_peak_fire = (
            age_cycles >= 3
            and zero_peak_cycles >= 3
            and pnl_pct <= -0.0025
            and loser_target
            and density <= 0.32
            and pressure_count <= 2
            and delta_window <= 0.0045
        )
        zero_peak_compression = (
            age_cycles >= 4
            and zero_peak_cycles >= 4
            and pnl_pct <= 0.0001
            and loser_target
            and density <= 0.30
            and pressure_count <= 2
            and delta_window <= 0.0035
            and score < 0.92
        )
        stale_loser_fire = (
            age_cycles >= 5
            and zero_peak_cycles >= 5
            and pnl_pct <= 0.0001
            and not winner_safe_profile
            and density <= 0.28
            and pressure_count <= 2
        )

        if zero_peak_fire or zero_peak_compression or stale_loser_fire:
            if zero_peak_fire:
                fire_signal = "sports_zero_peak_fire"
            elif zero_peak_compression:
                fire_signal = "sports_zero_peak_compression"
            else:
                fire_signal = "sports_loser_stale_fire"
            print("TRACE | sports_zero_peak_fire | signal={} | age={} | zero_peak_cycles={} | pnl={:.4f} | peak={:.4f} | score={:.3f} | {}".format(fire_signal, age_cycles, zero_peak_cycles, pnl_pct, peak_pnl_pct, score, (position.get("question") or position.get("outcome_name") or "unknown")[:72]))
            return {"exit_reason": "sports_zero_peak_fire_exit", "bleed_signal": fire_signal, "peak_pnl_pct": peak_pnl_pct, "market_type": market_type, "score": score, "family_key": position.get("family_key"), "question": position.get("question"), "reason": reason}

        weak_probe = (age_cycles >= 2 and zero_peak_cycles >= 2 and pnl_pct <= 0.0005 and density <= 0.14 and pressure_count <= 1 and trend < 0.90 and delta_window <= 0.0035 and score < 1.00)
        weak_override = (reason in {"multicycle_momentum_override", "momentum_override"} and age_cycles >= 2 and zero_peak_cycles >= 2 and pnl_pct <= 0.0001 and density <= 0.18 and pressure_count <= 1 and trend < 0.92 and delta_window <= 0.0050 and score < 0.82)
        stale_runner = (age_cycles >= 3 and zero_peak_cycles >= 3 and pnl_pct <= -0.0080 and density <= 0.22 and pressure_count <= 1 and trend < 0.94 and delta_window <= 0.0070 and score < 1.06)
        fire_signal = None
        if weak_override:
            fire_signal = "weak_override_zero_peak"
        elif weak_probe:
            fire_signal = "sports_probe_zero_peak"
        elif stale_runner:
            fire_signal = "sports_stale_runner"
        if fire_signal:
            print("TRACE | sports_longshot_churn_kill | signal={} | age={} | zero_peak_cycles={} | pnl={:.4f} | peak={:.4f} | score={:.3f} | {}".format(fire_signal, age_cycles, zero_peak_cycles, pnl_pct, peak_pnl_pct, score, (position.get("question") or position.get("outcome_name") or "unknown")[:72]))
            return {"exit_reason": "sports_longshot_churn_kill", "bleed_signal": fire_signal, "peak_pnl_pct": peak_pnl_pct, "market_type": market_type, "score": score, "family_key": position.get("family_key"), "question": position.get("question"), "reason": reason}
        return None

    def _should_general_zero_peak_stall_cut(self, position: Dict, candidate: Dict) -> Optional[Dict]:
        position = self._ensure_political_position_flags(position)
        age_cycles = int(position.get("age_cycles", 0) or 0)
        if age_cycles < 3:
            return None

        reason = str(position.get("reason", "unknown") or "unknown")
        market_type = str(position.get("market_type", "general_binary") or "general_binary")
        open_regime = str(position.get("open_regime", position.get("last_regime", "unknown")) or "unknown")
        theme = str(position.get("theme", candidate.get("theme", "general")) or "general")
        cluster = str(position.get("cluster", candidate.get("cluster", "unknown")) or "unknown")
        pnl_pct = self._safe_float(position.get("current_unrealized_pnl_pct", 0.0), 0.0)
        peak_pnl_pct = self._safe_float(position.get("peak_unrealized_pnl_pct", pnl_pct), pnl_pct)
        zero_peak_cycles = int(position.get("zero_peak_cycles", age_cycles) or age_cycles)
        density = self._safe_float(candidate.get("pressure_density", position.get("last_pressure_density", 0.0)), 0.0)
        pressure_count = int(candidate.get("pressure_count", position.get("last_pressure_count", 0)) or 0)
        trend = self._safe_float(candidate.get("price_trend_strength", position.get("last_trend_strength", 0.0)), 0.0)
        delta_window = abs(self._safe_float(candidate.get("price_delta_window", position.get("last_window_delta", 0.0)), 0.0))
        score = self._safe_float(candidate.get("score", position.get("last_score", 0.0)), position.get("last_score", 0.0))

        if market_type != "general_binary":
            return None
        if open_regime not in {"calm", "normal"}:
            return None
        if bool(position.get("partial_tp_1_done", False)):
            return None
        if bool(position.get("political_hold_window", False)) or bool(position.get("override_survival_corridor", False)):
            return None

        print(
            "TRACE | general_zero_peak_stall_check | theme={} | cluster={} | reason={} | age={} | zero_peak_cycles={} | pnl={:.4f} | peak={:.4f} | density={:.3f} | trend={:.3f} | win={:.4f} | pcount={} | score={:.3f} | {}".format(
                theme, cluster, reason, age_cycles, zero_peak_cycles, pnl_pct, peak_pnl_pct, density, trend, delta_window, pressure_count, score,
                (position.get("question") or position.get("outcome_name") or "unknown")[:72],
            )
        )

        if peak_pnl_pct > 0.0001:
            return None

        strong_recovery = (
            score >= 1.26
            and (density >= 0.22 or pressure_count >= 2 or trend >= 1.04 or delta_window >= 0.010)
        )
        if strong_recovery:
            return None

        political_pre_momentum_stall = (
            theme == "politics"
            and reason in {"pre_momentum", "score+pre_momentum"}
            and age_cycles >= 4
            and zero_peak_cycles >= 4
            and pnl_pct <= 0.0001
            and density <= 0.18
            and pressure_count <= 1
            and delta_window <= 0.0065
        )
        general_flat_stall = (
            theme in {"general", "politics"}
            and reason in {"score", "pre_momentum", "score+pre_momentum", "score+pressure", "pressure"}
            and age_cycles >= 3
            and zero_peak_cycles >= 3
            and pnl_pct <= 0.0001
            and density <= 0.14
            and pressure_count <= 1
            and delta_window <= 0.0045
            and trend <= 1.02
            and score < 1.22
        )

        if political_pre_momentum_stall:
            print(
                "TRACE | political_pre_momentum_compression | age={} | zero_peak_cycles={} | pnl={:.4f} | score={:.3f} | {}".format(
                    age_cycles, zero_peak_cycles, pnl_pct, score,
                    (position.get("question") or position.get("outcome_name") or "unknown")[:72],
                )
            )
            return {
                "exit_reason": "political_pre_momentum_compression_exit",
                "bleed_signal": "political_pre_momentum_zero_peak_stall",
                "peak_pnl_pct": peak_pnl_pct,
                "market_type": market_type,
                "score": score,
                "family_key": position.get("family_key"),
                "question": position.get("question"),
                "reason": reason,
            }

        if general_flat_stall:
            print(
                "TRACE | general_zero_peak_stall_cut | age={} | zero_peak_cycles={} | pnl={:.4f} | score={:.3f} | {}".format(
                    age_cycles, zero_peak_cycles, pnl_pct, score,
                    (position.get("question") or position.get("outcome_name") or "unknown")[:72],
                )
            )
            return {
                "exit_reason": "general_zero_peak_stall_cut",
                "bleed_signal": "general_zero_peak_stall",
                "peak_pnl_pct": peak_pnl_pct,
                "market_type": market_type,
                "score": score,
                "family_key": position.get("family_key"),
                "question": position.get("question"),
                "reason": reason,
            }

        return None


    def _should_normal_zero_peak_linger_cut(self, position: Dict, candidate: Dict) -> Optional[Dict]:
        position = self._ensure_political_position_flags(position)
        age_cycles = int(position.get("age_cycles", 0) or 0)
        if age_cycles < 2:
            return None

        reason = str(position.get("reason", "unknown") or "unknown")
        market_type = str(position.get("market_type", "general_binary") or "general_binary")
        open_regime = str(position.get("open_regime", position.get("last_regime", "unknown")) or "unknown")
        pnl_pct = self._safe_float(position.get("current_unrealized_pnl_pct", 0.0), 0.0)
        peak_pnl_pct = self._safe_float(position.get("peak_unrealized_pnl_pct", pnl_pct), pnl_pct)
        zero_peak_cycles = int(position.get("zero_peak_cycles", age_cycles) or age_cycles)
        density = self._safe_float(candidate.get("pressure_density", position.get("last_pressure_density", 0.0)), 0.0)
        trend = self._safe_float(candidate.get("price_trend_strength", position.get("last_trend_strength", 0.0)), 0.0)
        delta_window = abs(self._safe_float(candidate.get("price_delta_window", position.get("last_window_delta", 0.0)), 0.0))
        score = self._safe_float(candidate.get("score", position.get("last_score", 0.0)), position.get("last_score", 0.0))

        if open_regime != "normal":
            return None
        if reason not in {"pre_momentum", "score+pre_momentum"}:
            return None
        if market_type not in {"general_binary", "sports_award_longshot", "scheduled_binary_event"}:
            return None
        if bool(position.get("partial_tp_1_done", False)):
            return None
        if bool(position.get("political_hold_window", False)) or bool(position.get("override_survival_corridor", False)):
            return None

        print(
            "TRACE | normal_zero_peak_linger_check | market_type={} | reason={} | age={} | zero_peak_cycles={} | pnl={:.4f} | peak={:.4f} | density={:.3f} | trend={:.3f} | win={:.4f} | score={:.3f} | {}".format(
                market_type, reason, age_cycles, zero_peak_cycles, pnl_pct, peak_pnl_pct, density, trend, delta_window, score,
                (position.get("question") or position.get("outcome_name") or "unknown")[:72],
            )
        )

        if peak_pnl_pct > 0.0001:
            return None

        if zero_peak_cycles >= 3 and pnl_pct <= 0.0 and density < 0.16 and delta_window <= 0.0025:
            print(
                "TRACE | normal_zero_peak_linger_cut | market_type={} | reason={} | age={} | zero_peak_cycles={} | pnl={:.4f} | score={:.3f} | {}".format(
                    market_type, reason, age_cycles, zero_peak_cycles, pnl_pct, score,
                    (position.get("question") or position.get("outcome_name") or "unknown")[:72],
                )
            )
            return {
                "exit_reason": "normal_zero_peak_linger_cut",
                "bleed_signal": "normal_pre_momentum_zero_peak",
                "peak_pnl_pct": peak_pnl_pct,
                "market_type": market_type,
                "score": score,
                "family_key": position.get("family_key"),
                "question": position.get("question"),
                "reason": reason,
            }

        return None


    def _should_override_stall_cut(self, position: Dict, candidate: Dict) -> Optional[Dict]:
        position = self._ensure_political_position_flags(position)
        age_cycles = int(position.get("age_cycles", 0) or 0)
        if age_cycles < 3:
            return None

        reason = str(position.get("reason", "unknown") or "unknown")
        market_type = str(position.get("market_type", "general_binary") or "general_binary")
        open_regime = str(position.get("open_regime", position.get("last_regime", "unknown")) or "unknown")
        pnl_pct = self._safe_float(position.get("current_unrealized_pnl_pct", 0.0), 0.0)
        peak_pnl_pct = self._safe_float(position.get("peak_unrealized_pnl_pct", pnl_pct), pnl_pct)
        zero_peak_cycles = int(position.get("zero_peak_cycles", age_cycles) or age_cycles)
        blocked_count = int(position.get("seed_blocked_zero_peak_count", 0) or 0)
        density = self._safe_float(candidate.get("pressure_density", position.get("last_pressure_density", 0.0)), 0.0)
        trend = self._safe_float(candidate.get("price_trend_strength", position.get("last_trend_strength", 0.0)), 0.0)
        delta_window = abs(self._safe_float(candidate.get("price_delta_window", position.get("last_window_delta", 0.0)), 0.0))
        score = self._safe_float(candidate.get("score", position.get("last_score", 0.0)), position.get("last_score", 0.0))

        if open_regime != "normal":
            return None
        if bool(position.get("partial_tp_1_done", False)):
            return None
        if bool(position.get("political_hold_window", False)) or bool(position.get("override_survival_corridor", False)):
            return None

        target_reason = reason in {"multicycle_momentum_override", "score+pressure", "pressure"}
        target_market = market_type in {"general_binary", "speculative_hype"}
        if not (target_reason and target_market):
            return None

        print(
            "TRACE | override_stall_check | market_type={} | reason={} | age={} | zero_peak_cycles={} | blocked_count={} | pnl={:.4f} | peak={:.4f} | density={:.3f} | trend={:.3f} | win={:.4f} | score={:.3f} | {}".format(
                market_type, reason, age_cycles, zero_peak_cycles, blocked_count, pnl_pct, peak_pnl_pct, density, trend, delta_window, score,
                (position.get("question") or position.get("outcome_name") or "unknown")[:72],
            )
        )

        if peak_pnl_pct > 0.0001:
            return None

        if reason == "multicycle_momentum_override":
            if age_cycles >= 3 and zero_peak_cycles >= 3 and pnl_pct <= 0.0 and density <= 0.20 and delta_window <= 0.0060:
                print(
                    "TRACE | override_stall_cut | signal=override_zero_peak | market_type={} | age={} | blocked_count={} | pnl={:.4f} | {}".format(
                        market_type, age_cycles, blocked_count, pnl_pct,
                        (position.get("question") or position.get("outcome_name") or "unknown")[:72],
                    )
                )
                return {
                    "exit_reason": "override_stall_cut",
                    "bleed_signal": "override_zero_peak",
                    "peak_pnl_pct": peak_pnl_pct,
                    "market_type": market_type,
                    "score": score,
                    "family_key": position.get("family_key"),
                    "question": position.get("question"),
                    "reason": reason,
                }

        if reason in {"score+pressure", "pressure"}:
            if age_cycles >= 4 and blocked_count >= 2 and pnl_pct <= 0.0 and density <= 0.24 and delta_window <= 0.0065:
                print(
                    "TRACE | override_stall_cut | signal=pressure_zero_peak_stall | market_type={} | age={} | blocked_count={} | pnl={:.4f} | {}".format(
                        market_type, age_cycles, blocked_count, pnl_pct,
                        (position.get("question") or position.get("outcome_name") or "unknown")[:72],
                    )
                )
                return {
                    "exit_reason": "override_stall_cut",
                    "bleed_signal": "pressure_zero_peak_stall",
                    "peak_pnl_pct": peak_pnl_pct,
                    "market_type": market_type,
                    "score": score,
                    "family_key": position.get("family_key"),
                    "question": position.get("question"),
                    "reason": reason,
                }

        return None

    def _should_peak_zero_kill(self, position: Dict, candidate: Dict) -> Optional[Dict]:
        position = self._ensure_political_position_flags(position)
        age_cycles = int(position.get("age_cycles", 0) or 0)
        if age_cycles < 3:
            return None

        reason = str(position.get("reason", "unknown") or "unknown")
        market_type = str(position.get("market_type", "general_binary") or "general_binary")
        pnl_pct = self._safe_float(position.get("current_unrealized_pnl_pct", 0.0), 0.0)
        peak_pnl_pct = self._safe_float(position.get("peak_unrealized_pnl_pct", pnl_pct), pnl_pct)
        prev_peak_pnl_pct = self._safe_float(position.get("prev_peak_unrealized_pnl_pct", peak_pnl_pct), peak_pnl_pct)
        zero_peak_cycles = int(position.get("zero_peak_cycles", age_cycles) or age_cycles)
        density = self._safe_float(candidate.get("pressure_density", position.get("last_pressure_density", 0.0)), 0.0)
        trend = self._safe_float(candidate.get("price_trend_strength", position.get("last_trend_strength", 0.0)), 0.0)
        delta_window = abs(self._safe_float(candidate.get("price_delta_window", position.get("last_window_delta", 0.0)), 0.0))
        score = self._safe_float(candidate.get("score", position.get("last_score", 0.0)), position.get("last_score", 0.0))
        open_regime = str(position.get("open_regime", position.get("last_regime", "unknown")) or "unknown")

        if bool(position.get("partial_tp_1_done", False)):
            return None
        if bool(position.get("political_hold_window", False)) or bool(position.get("override_survival_corridor", False)):
            return None

        post_first_peak_protect_until_age = int(position.get("post_first_peak_protect_until_age", 0) or 0)
        if post_first_peak_protect_until_age and age_cycles <= post_first_peak_protect_until_age and peak_pnl_pct > 0.0001 and pnl_pct > -0.015:
            print(
                "TRACE | post_first_peak_protect_hold | age={} | until_age={} | pnl={:.4f} | peak={:.4f} | {}".format(
                    age_cycles, post_first_peak_protect_until_age, pnl_pct, peak_pnl_pct,
                    (position.get("question") or position.get("outcome_name") or "unknown")[:72],
                )
            )
            return None

        post_bridge_protect_until_age = int(position.get("post_bridge_protect_until_age", 0) or 0)
        if post_bridge_protect_until_age and age_cycles <= post_bridge_protect_until_age and pnl_pct > -0.020:
            print(
                "TRACE | post_bridge_protect_hold | age={} | until_age={} | pnl={:.4f} | peak={:.4f} | {}".format(
                    age_cycles, post_bridge_protect_until_age, pnl_pct, peak_pnl_pct,
                    (position.get("question") or position.get("outcome_name") or "unknown")[:72],
                )
            )
            return None

        target_types = {"sports_award_longshot", "legal_resolution", "short_burst_catalyst", "speculative_hype", "valuation_ladder"}
        target_reasons = {"pressure", "score+pressure", "pre_momentum", "score+pre_momentum", "multicycle_momentum_override", "score+momentum", "momentum_override"}

        if market_type not in target_types and reason not in target_reasons:
            return None

        print(
            "TRACE | peak_zero_kill_check | market_type={} | reason={} | age={} | zero_peak_cycles={} | pnl={:.4f} | peak={:.4f} | prev_peak={:.4f} | density={:.3f} | trend={:.3f} | win={:.4f} | score={:.3f} | {}".format(
                market_type, reason, age_cycles, zero_peak_cycles, pnl_pct, peak_pnl_pct, prev_peak_pnl_pct, density, trend, delta_window, score,
                (position.get("question") or position.get("outcome_name") or "unknown")[:72],
            )
        )

        selective_grace = (
            market_type in {"short_burst_catalyst", "legal_resolution", "narrative_long_tail"}
            and age_cycles <= 4
            and score >= 1.08
            and (trend >= 0.80 or density >= 0.10 or delta_window >= 0.0020 or pnl_pct > -0.012)
        )
        if selective_grace:
            position["peak_zero_repair_grace_age"] = age_cycles
            position["peak_zero_repair_grace_until_age"] = max(age_cycles + 2, int(position.get("peak_zero_repair_grace_until_age", 0) or 0))
            position["peak_zero_repair_grace_hits"] = int(position.get("peak_zero_repair_grace_hits", 0) or 0) + 1
            print(
                "TRACE | peak_zero_repair_grace | market_type={} | age={} | zero_peak_cycles={} | pnl={:.4f} | score={:.3f} | until_age={} | {}".format(
                    market_type, age_cycles, zero_peak_cycles, pnl_pct, score,
                    int(position.get("peak_zero_repair_grace_until_age", 0) or 0),
                    (position.get("question") or position.get("outcome_name") or "unknown")[:72],
                )
            )
            return None

        if peak_pnl_pct <= 0.0001:
            if open_regime in {"calm", "normal"} and zero_peak_cycles >= 3 and pnl_pct <= -0.002 and density < 0.30 and trend < 0.95 and delta_window <= 0.010:
                if int(position.get("peak_zero_repair_grace_until_age", 0) or 0) > 0:
                    print("TRACE | grace_expired_to_kill | signal=zero_peak_dead_money | age={} | zero_peak_cycles={} | pnl={:.4f} | {}".format(
                        age_cycles, zero_peak_cycles, pnl_pct, (position.get("question") or position.get("outcome_name") or "unknown")[:72]
                    ))
                print("TRACE | peak_zero_kill | signal=zero_peak_dead_money | age={} | pnl={:.4f} | {}".format(
                    age_cycles, pnl_pct, (position.get("question") or position.get("outcome_name") or "unknown")[:72]
                ))
                return {"exit_reason": "peak_zero_kill", "bleed_signal": "zero_peak_dead_money", "peak_pnl_pct": peak_pnl_pct}

            if zero_peak_cycles >= 5 and pnl_pct <= 0.008 and density < 0.26 and trend < 0.90:
                if int(position.get("peak_zero_repair_grace_until_age", 0) or 0) > 0:
                    print("TRACE | grace_expired_to_kill | signal=long_no_peak_recycle | age={} | zero_peak_cycles={} | pnl={:.4f} | {}".format(
                        age_cycles, zero_peak_cycles, pnl_pct, (position.get("question") or position.get("outcome_name") or "unknown")[:72]
                    ))
                print("TRACE | peak_zero_kill | signal=long_no_peak_recycle | age={} | pnl={:.4f} | {}".format(
                    age_cycles, pnl_pct, (position.get("question") or position.get("outcome_name") or "unknown")[:72]
                ))
                return {"exit_reason": "peak_zero_kill", "bleed_signal": "long_no_peak_recycle", "peak_pnl_pct": peak_pnl_pct}

        return None


    def _should_early_hard_stop_compression(self, position: Dict, candidate: Dict) -> Optional[Dict]:
        position = self._ensure_political_position_flags(position)
        age_cycles = int(position.get("age_cycles", 0) or 0)
        if age_cycles < 4:
            return None

        market_type = str(position.get("market_type", "general_binary") or "general_binary")
        if market_type != "legal_resolution":
            return None

        reason = str(position.get("reason", "unknown") or "unknown")
        if reason not in {"score+pressure", "pressure", "score+pre_momentum", "pre_momentum", "score"}:
            return None

        if bool(position.get("partial_tp_1_done", False)):
            return None

        pnl_pct = self._safe_float(position.get("current_unrealized_pnl_pct", 0.0), 0.0)
        peak_pnl_pct = self._safe_float(position.get("peak_unrealized_pnl_pct", pnl_pct), pnl_pct)
        density = self._safe_float(candidate.get("pressure_density", position.get("last_pressure_density", 0.0)), 0.0)
        pressure_count = int(candidate.get("pressure_count", position.get("last_pressure_count", 0)) or 0)
        trend = self._safe_float(candidate.get("price_trend_strength", position.get("last_trend_strength", 0.0)), 0.0)
        delta_window = abs(self._safe_float(candidate.get("price_delta_window", position.get("last_window_delta", 0.0)), 0.0))
        score = self._safe_float(candidate.get("score", position.get("last_score", 0.0)), position.get("last_score", 0.0))
        entry_score = self._safe_float(position.get("entry_score", position.get("score", 0.0)), position.get("score", 0.0))
        zero_peak_cycles = int(position.get("zero_peak_cycles", age_cycles) or age_cycles)

        print(
            "TRACE | early_hard_stop_compression_check | market_type={} | reason={} | age={} | zero_peak_cycles={} | pnl={:.4f} | peak={:.4f} | density={:.3f} | trend={:.3f} | win={:.4f} | pcount={} | score_now={:.3f} | entry_score={:.3f} | {}".format(
                market_type, reason, age_cycles, zero_peak_cycles, pnl_pct, peak_pnl_pct, density, trend, delta_window, pressure_count, score, entry_score,
                (position.get("question") or position.get("outcome_name") or "unknown")[:72],
            )
        )

        legal_zero_peak_compression = (
            age_cycles >= 4
            and zero_peak_cycles >= 4
            and pnl_pct <= -0.0550
            and peak_pnl_pct <= 0.0010
            and density <= 0.36
            and pressure_count <= 2
            and delta_window <= 0.0160
        )

        legal_zero_peak_stall = (
            age_cycles >= 5
            and zero_peak_cycles >= 5
            and pnl_pct <= -0.0300
            and peak_pnl_pct <= 0.0001
            and density <= 0.28
            and trend < 1.00
            and delta_window <= 0.0120
            and score <= max(entry_score + 0.10, 1.54)
        )

        severe_zero_peak_loss = (
            age_cycles >= 4
            and pnl_pct <= -0.1800
            and peak_pnl_pct <= 0.0050
            and density <= 0.38
            and pressure_count <= 2
            and delta_window <= 0.0220
        )

        stale_red_runner = (
            age_cycles >= 5
            and pnl_pct <= -0.1400
            and peak_pnl_pct <= 0.0001
            and density <= 0.32
            and trend < 1.02
            and delta_window <= 0.0220
            and score <= max(entry_score + 0.18, 1.58)
        )

        fire_signal = None
        if legal_zero_peak_compression:
            fire_signal = "legal_zero_peak_compression"
        elif legal_zero_peak_stall:
            fire_signal = "legal_zero_peak_stall"
        elif severe_zero_peak_loss or stale_red_runner:
            fire_signal = "legal_red_runner"

        if fire_signal:
            print(
                "TRACE | early_hard_stop_compression_fire | signal={} | age={} | zero_peak_cycles={} | pnl={:.4f} | peak={:.4f} | score={:.3f} | entry_score={:.3f} | {}".format(
                    fire_signal, age_cycles, zero_peak_cycles, pnl_pct, peak_pnl_pct, score, entry_score,
                    (position.get("question") or position.get("outcome_name") or "unknown")[:72],
                )
            )
            return {
                "exit_reason": "early_hard_stop_compression_exit",
                "bleed_signal": fire_signal,
                "peak_pnl_pct": peak_pnl_pct,
                "market_type": market_type,
                "score": score,
                "family_key": position.get("family_key"),
                "question": position.get("question"),
                "reason": reason,
            }

        return None


    def _should_early_bleed_cut(self, position: Dict, candidate: Dict) -> Optional[Dict]:
        position = self._ensure_political_position_flags(position)
        reason = position.get("reason", "unknown")
        if reason not in {
            "pressure", "score+pressure", "pre_momentum", "score+pre_momentum",
            "multicycle_momentum_override", "score+momentum", "momentum_override"
        }:
            return None

        if bool(position.get("partial_tp_1_done", False)):
            return None

        age_cycles = int(position.get("age_cycles", 0) or 0)
        post_first_peak_protect_until_age = int(position.get("post_first_peak_protect_until_age", 0) or 0)
        post_bridge_protect_until_age = int(position.get("post_bridge_protect_until_age", 0) or 0)
        if age_cycles < 2:
            return None

        pnl_pct = self._safe_float(position.get("current_unrealized_pnl_pct", 0.0), 0.0)
        peak_pnl_pct = self._safe_float(position.get("peak_unrealized_pnl_pct", pnl_pct), pnl_pct)
        score = self._safe_float(candidate.get("score", position.get("last_score", 0.0)), position.get("last_score", 0.0))
        entry_score = self._safe_float(position.get("entry_score", score), score)
        density = self._safe_float(candidate.get("pressure_density", position.get("last_pressure_density", 0.0)), position.get("last_pressure_density", 0.0))
        trend = self._safe_float(candidate.get("price_trend_strength", position.get("last_trend_strength", 0.0)), position.get("last_trend_strength", 0.0))
        delta_window = self._safe_float(candidate.get("price_delta_window", position.get("last_window_delta", 0.0)), position.get("last_window_delta", 0.0))
        delta_1 = self._safe_float(candidate.get("price_delta", position.get("last_delta", 0.0)), position.get("last_delta", 0.0))
        open_regime = str(position.get("open_regime", position.get("last_regime", "unknown")) or "unknown")

        # leave political protected positions alone early
        if bool(position.get("political_hold_window", False)) or bool(position.get("override_survival_corridor", False)):
            if age_cycles <= 6 and pnl_pct > -0.12:
                return None

        if post_first_peak_protect_until_age and age_cycles <= post_first_peak_protect_until_age and peak_pnl_pct > 0.0001 and pnl_pct > -0.015:
            print(
                "TRACE | post_first_peak_protect_hold | age={} | until_age={} | pnl={:.4f} | peak={:.4f} | {}".format(
                    age_cycles, post_first_peak_protect_until_age, pnl_pct, peak_pnl_pct,
                    (position.get("question") or position.get("outcome_name") or "unknown")[:72],
                )
            )
            return None

        if post_bridge_protect_until_age and age_cycles <= post_bridge_protect_until_age and pnl_pct > -0.020:
            print(
                "TRACE | post_bridge_protect_hold | age={} | until_age={} | pnl={:.4f} | peak={:.4f} | {}".format(
                    age_cycles, post_bridge_protect_until_age, pnl_pct, peak_pnl_pct,
                    (position.get("question") or position.get("outcome_name") or "unknown")[:72],
                )
            )
            return None

        print(
            "TRACE | early_bleed_check | reason={} | age={} | pnl={:.4f} | peak={:.4f} | density={:.3f} | trend={:.3f} | win={:.4f} | score_now={:.3f} | entry_score={:.3f} | {}".format(
                reason,
                age_cycles,
                pnl_pct,
                peak_pnl_pct,
                density,
                trend,
                delta_window,
                score,
                entry_score,
                (position.get("question") or position.get("outcome_name") or "unknown")[:72],
            )
        )

        # failed impulse never really paid
        if age_cycles >= 2 and pnl_pct <= -0.012 and peak_pnl_pct <= 0.025 and density < 0.36 and trend < 0.95 and abs(delta_window) <= 0.0095:
            print("TRACE | early_bleed_cut | signal=impulse_never_paid | age={} | pnl={:.4f} | peak={:.4f} | {}".format(
                age_cycles, pnl_pct, peak_pnl_pct, (position.get("question") or position.get("outcome_name") or "unknown")[:72]
            ))
            return {
                "exit_reason": "early_bleed_cut",
                "bleed_signal": "impulse_never_paid",
                "peak_pnl_pct": peak_pnl_pct,
                "entry_score": entry_score,
                "score_now": score,
            }

        # score collapsed quickly after entry
        if age_cycles >= 2 and pnl_pct <= -0.010 and score <= max(0.52, entry_score * 0.88) and density < 0.36 and abs(delta_window) <= 0.0090 and abs(delta_1) <= 0.0110:
            print("TRACE | early_bleed_cut | signal=score_decay_after_entry | age={} | pnl={:.4f} | peak={:.4f} | {}".format(
                age_cycles, pnl_pct, peak_pnl_pct, (position.get("question") or position.get("outcome_name") or "unknown")[:72]
            ))
            return {
                "exit_reason": "early_bleed_cut",
                "bleed_signal": "score_decay_after_entry",
                "peak_pnl_pct": peak_pnl_pct,
                "entry_score": entry_score,
                "score_now": score,
            }

        # in calm, bleed faster on non-protected impulse positions
        if open_regime == "calm" and age_cycles >= 2 and pnl_pct <= -0.006 and peak_pnl_pct <= 0.020 and density < 0.30 and trend < 0.90:
            print("TRACE | early_bleed_cut | signal=calm_bleed_recycle | age={} | pnl={:.4f} | peak={:.4f} | {}".format(
                age_cycles, pnl_pct, peak_pnl_pct, (position.get("question") or position.get("outcome_name") or "unknown")[:72]
            ))
            return {
                "exit_reason": "early_bleed_cut",
                "bleed_signal": "calm_bleed_recycle",
                "peak_pnl_pct": peak_pnl_pct,
                "entry_score": entry_score,
                "score_now": score,
            }

        if age_cycles >= 2 and pnl_pct < 0.0 and peak_pnl_pct <= 0.05:
            print(
                "TRACE | bleed_cut_blocked_by_threshold | reason={} | age={} | pnl={:.4f} | peak={:.4f} | density={:.3f} | trend={:.3f} | win={:.4f} | {}".format(
                    reason,
                    age_cycles,
                    pnl_pct,
                    peak_pnl_pct,
                    density,
                    trend,
                    delta_window,
                    (position.get("question") or position.get("outcome_name") or "unknown")[:72],
                )
            )

        return None



    def _should_runner_protection_lock_exit(self, position: Dict, candidate: Dict) -> Optional[Dict]:
        position = self._ensure_political_position_flags(position)
        if not bool(position.get("partial_tp_1_done", False)):
            return None
        if not bool(position.get("profit_lock_seed_done", False)):
            return None

        age_cycles = int(position.get("age_cycles", 0) or 0)
        if age_cycles < 3:
            return None

        market_type = str(position.get("market_type", "general_binary") or "general_binary")
        if market_type not in {"sports_award_longshot", "legal_resolution", "short_burst_catalyst", "valuation_ladder", "speculative_hype", "narrative_long_tail"}:
            return None

        pnl_pct = self._safe_float(position.get("current_unrealized_pnl_pct", 0.0), 0.0)
        peak_pnl_pct = self._safe_float(position.get("peak_unrealized_pnl_pct", pnl_pct), pnl_pct)
        density = self._safe_float(candidate.get("pressure_density", position.get("last_pressure_density", 0.0)), position.get("last_pressure_density", 0.0))
        trend = self._safe_float(candidate.get("price_trend_strength", position.get("last_trend_strength", 0.0)), position.get("last_trend_strength", 0.0))
        delta_window = abs(self._safe_float(candidate.get("price_delta_window", position.get("last_window_delta", 0.0)), position.get("last_window_delta", 0.0)))
        score = self._safe_float(candidate.get("score", position.get("last_score", 0.0)), position.get("last_score", 0.0))
        entry_density = self._safe_float(position.get("entry_pressure_density", 0.0), 0.0)

        runner_protect_until_age = int(position.get("runner_protect_until_age", 0) or 0)
        if runner_protect_until_age and age_cycles <= runner_protect_until_age and pnl_pct > -0.012:
            print(
                "TRACE | runner_protection_hold | market_type={} | age={} | until_age={} | pnl={:.4f} | peak={:.4f} | {}".format(
                    market_type, age_cycles, runner_protect_until_age, pnl_pct, peak_pnl_pct,
                    (position.get("question") or position.get("outcome_name") or "unknown")[:72],
                )
            )
            return None

        if peak_pnl_pct < 0.028:
            return None

        winner_carry = self._winner_carry_state(position, candidate)
        locked_floor_pct = self._safe_float(position.get("runner_locked_floor_pct", 0.0), 0.0)
        if locked_floor_pct <= 0.0:
            locked_floor_pct = max(0.008, min(0.020, peak_pnl_pct * 0.22))
        if winner_carry.get("active", False):
            locked_floor_pct = max(locked_floor_pct, float(winner_carry.get("carry_floor_pct", 0.0) or 0.0))

        weak_follow = (
            delta_window < 0.008
            and (density < max(0.34, entry_density + 0.08) or trend < 0.90)
        )
        exhausted_runner = (
            peak_pnl_pct >= 0.040
            and pnl_pct <= locked_floor_pct
            and weak_follow
        )
        hard_giveback = (
            peak_pnl_pct >= 0.030
            and pnl_pct <= -0.002
        )

        print(
            "TRACE | runner_protection_lock_check | market_type={} | age={} | pnl={:.4f} | peak={:.4f} | floor={:.4f} | density={:.3f} | trend={:.3f} | win={:.4f} | score={:.3f} | partials={} | carry={} | {}".format(
                market_type, age_cycles, pnl_pct, peak_pnl_pct, locked_floor_pct, density, trend, delta_window, score,
                int(position.get("partial_take_count", 0) or 0),
                int(bool(winner_carry.get("active", False))),
                (position.get("question") or position.get("outcome_name") or "unknown")[:72],
            )
        )
        if winner_carry.get("active", False) and pnl_pct >= locked_floor_pct and winner_carry.get("strong_follow", False):
            print(
                "TRACE | winner_carry_runner_hold | market_type={} | age={} | pnl={:.4f} | peak={:.4f} | floor={:.4f} | {}".format(
                    market_type, age_cycles, pnl_pct, peak_pnl_pct, locked_floor_pct,
                    (position.get("question") or position.get("outcome_name") or "unknown")[:72],
                )
            )
            return None

        if exhausted_runner or hard_giveback:
            print(
                "TRACE | runner_protection_lock_fire | market_type={} | age={} | pnl={:.4f} | peak={:.4f} | floor={:.4f} | weak_follow={} | {}".format(
                    market_type, age_cycles, pnl_pct, peak_pnl_pct, locked_floor_pct, int(bool(weak_follow)),
                    (position.get("question") or position.get("outcome_name") or "unknown")[:72],
                )
            )
            return {
                "exit_reason": "runner_protection_lock_exit",
                "peak_pnl_pct": round(peak_pnl_pct, 6),
                "runner_floor_pct": round(locked_floor_pct, 6),
                "market_type": market_type,
                "score": round(score, 6),
                "family_key": position.get("family_key"),
                "question": position.get("question"),
                "reason": position.get("reason", "unknown"),
            }

        return None


    def _profit_lock_seed_config(self, position: Dict) -> Dict:
        market_type = position.get("market_type", "general_binary")
        base = dict(PROFIT_LOCK_SEED_PROFILE.get(market_type, PROFIT_LOCK_SEED_PROFILE["general_binary"]))
        open_regime = str(position.get("open_regime", position.get("last_regime", "unknown")) or "unknown")

        if open_regime == "calm":
            base["trigger"] = max(0.04, float(base.get("trigger", 0.24)) - 0.07)
            base["fraction"] = min(0.50, float(base.get("fraction", 0.25)) + 0.10)
        elif open_regime == "hot":
            base["trigger"] = max(0.06, float(base.get("trigger", 0.24)) + 0.00)

        profile = MARKET_TYPE_EXIT_PROFILE.get(market_type, MARKET_TYPE_EXIT_PROFILE["general_binary"])
        bias = self._safe_float(profile.get("profit_lock_bias", 1.0), 1.0)

        trigger = max(0.04, float(base.get("trigger", 0.24)) * bias)
        fraction = min(0.60, max(0.18, float(base.get("fraction", 0.25))))
        min_age = max(1, int(base.get("min_age", 1) or 1))

        return {
            "trigger": round(trigger, 4),
            "fraction": round(fraction, 4),
            "min_age": min_age,
        }

    def evaluate_positions(self, market_map: Dict[str, Dict], price_truth_map: Optional[Dict[str, Dict]] = None, now_ts=None, regime: str = "unknown") -> List[Dict]:
        events: List[Dict] = []
        if not self.open_positions:
            return events

        positions_snapshot = list(self.open_positions)

        for position in positions_snapshot:
            key = position.get("position_key")
            if not key:
                continue

            candidate = market_map.get(key)
            truth_candidate = (price_truth_map or {}).get(key) if price_truth_map else None
            if not candidate and truth_candidate:
                candidate = dict(truth_candidate)
                candidate["_price_truth_only"] = True
            elif candidate and truth_candidate:
                merged_candidate = dict(candidate)
                for fld in ("price", "price_delta", "price_delta_window", "price_trend_strength", "pressure_density", "pressure_count", "minutes_to_end", "liquidity", "volume", "end_date"):
                    if fld in truth_candidate and truth_candidate.get(fld) is not None:
                        merged_candidate[f"_truth_{fld}"] = truth_candidate.get(fld)
                if truth_candidate.get("price") is not None:
                    merged_candidate["_truth_price"] = truth_candidate.get("price")
                candidate = merged_candidate
            if not candidate:
                position["missing_cycles"] = int(position.get("missing_cycles", 0)) + 1
                if position["missing_cycles"] >= 5:
                    close_event = self.close_position(key, "market_missing_stale", now_ts=now_ts)
                    if close_event:
                        events.append(close_event)
                continue

            self._mark_to_market(position, candidate, now_ts=now_ts, regime=regime)

            pnl_pct = self._safe_float(position.get("current_unrealized_pnl_pct", 0.0), 0.0)
            age_cycles = int(position.get("age_cycles", 0) or 0)
            score = self._safe_float(candidate.get("score", position.get("last_score", 0.0)), position.get("last_score", 0.0))
            density = self._safe_float(candidate.get("pressure_density", position.get("last_pressure_density", 0.0)), position.get("last_pressure_density", 0.0))
            trend = self._safe_float(candidate.get("price_trend_strength", position.get("last_trend_strength", 0.0)), position.get("last_trend_strength", 0.0))
            current_price = self._safe_float(candidate.get("_truth_price", candidate.get("price", position.get("entry_price", 0.0))), position.get("entry_price", 0.0))
            entry_price = self._safe_float(position.get("entry_price", 0.0), 0.0)

            weaken = self._position_should_weaken(position, candidate)
            if weaken:
                position["thesis_weaken_count"] = int(position.get("thesis_weaken_count", 0)) + 1
            else:
                position["thesis_weaken_count"] = max(0, int(position.get("thesis_weaken_count", 0)) - 1)

            early_hard_stop_exit = self._should_early_hard_stop_compression(position, candidate)
            if early_hard_stop_exit:
                close_event = self.close_position(key, early_hard_stop_exit["exit_reason"], now_ts=now_ts)
                if close_event:
                    close_event.update(early_hard_stop_exit)
                    events.append(close_event)
                continue

            if pnl_pct <= -0.45:
                close_event = self.close_position(key, "hard_stop_loss", now_ts=now_ts)
                if close_event:
                    events.append(close_event)
                continue

            # score-позициям больше терпения, импульсным меньше
            reason = position.get("reason", "unknown")
            weaken_limit = 3 if reason == "score" else 2

            if int(position.get("thesis_weaken_count", 0)) >= weaken_limit:
                close_event = self.close_position(key, "thesis_invalidation", now_ts=now_ts)
                if close_event:
                    events.append(close_event)
                continue

            profit_exit = self._should_micro_profit_exit(position, candidate)
            if profit_exit:
                close_event = self.close_position(key, profit_exit["exit_reason"], now_ts=now_ts)
                if close_event:
                    close_event.update(profit_exit)
                    events.append(close_event)
                continue

            delayed_light_exit = self._should_delayed_light_exit(position, candidate)
            if delayed_light_exit:
                close_event = self.close_position(key, delayed_light_exit["exit_reason"], now_ts=now_ts)
                if close_event:
                    close_event.update(delayed_light_exit)
                    events.append(close_event)
                continue

            zero_peak_scout_exit = self._should_zero_peak_scout_cut(position, candidate)
            if zero_peak_scout_exit:
                close_event = self.close_position(key, zero_peak_scout_exit["exit_reason"], now_ts=now_ts)
                if close_event:
                    close_event.update(zero_peak_scout_exit)
                    events.append(close_event)
                continue

            calm_legal_zero_peak_exit = self._should_calm_legal_zero_peak_cut(position, candidate)
            if calm_legal_zero_peak_exit:
                close_event = self.close_position(key, calm_legal_zero_peak_exit["exit_reason"], now_ts=now_ts)
                if close_event:
                    close_event.update(calm_legal_zero_peak_exit)
                    events.append(close_event)
                continue

            calm_zero_peak_general_exit = self._should_calm_zero_peak_general_cut(position, candidate)
            if calm_zero_peak_general_exit:
                close_event = self.close_position(key, calm_zero_peak_general_exit["exit_reason"], now_ts=now_ts)
                if close_event:
                    close_event.update(calm_zero_peak_general_exit)
                    events.append(close_event)
                continue

            sports_zombie_exit = self._should_sports_zombie_guillotine_exit(position, candidate)
            if sports_zombie_exit:
                close_event = self.close_position(key, sports_zombie_exit["exit_reason"], now_ts=now_ts)
                if close_event:
                    close_event.update(sports_zombie_exit)
                    events.append(close_event)
                continue

            sports_longshot_churn_exit = self._should_sports_longshot_churn_kill(position, candidate)
            if sports_longshot_churn_exit:
                close_event = self.close_position(key, sports_longshot_churn_exit["exit_reason"], now_ts=now_ts)
                if close_event:
                    close_event.update(sports_longshot_churn_exit)
                    events.append(close_event)
                continue

            general_zero_peak_stall_exit = self._should_general_zero_peak_stall_cut(position, candidate)
            if general_zero_peak_stall_exit:
                close_event = self.close_position(key, general_zero_peak_stall_exit["exit_reason"], now_ts=now_ts)
                if close_event:
                    close_event.update(general_zero_peak_stall_exit)
                    events.append(close_event)
                continue

            normal_zero_peak_linger_exit = self._should_normal_zero_peak_linger_cut(position, candidate)
            if normal_zero_peak_linger_exit:
                close_event = self.close_position(key, normal_zero_peak_linger_exit["exit_reason"], now_ts=now_ts)
                if close_event:
                    close_event.update(normal_zero_peak_linger_exit)
                    events.append(close_event)
                continue

            override_stall_exit = self._should_override_stall_cut(position, candidate)
            if override_stall_exit:
                close_event = self.close_position(key, override_stall_exit["exit_reason"], now_ts=now_ts)
                if close_event:
                    close_event.update(override_stall_exit)
                    events.append(close_event)
                continue

            peak_zero_exit = self._should_peak_zero_kill(position, candidate)
            if peak_zero_exit:
                close_event = self.close_position(key, peak_zero_exit["exit_reason"], now_ts=now_ts)
                if close_event:
                    close_event.update(peak_zero_exit)
                    events.append(close_event)
                continue

            early_bleed_exit = self._should_early_bleed_cut(position, candidate)
            if early_bleed_exit:
                close_event = self.close_position(key, early_bleed_exit["exit_reason"], now_ts=now_ts)
                if close_event:
                    close_event.update(early_bleed_exit)
                    events.append(close_event)
                continue

            failed_runner_exit = self._should_failed_runner_quarantine_exit(position, candidate)
            if failed_runner_exit:
                close_event = self.close_position(key, failed_runner_exit["exit_reason"], now_ts=now_ts)
                if close_event:
                    close_event.update(failed_runner_exit)
                    events.append(close_event)
                continue

            zero_churn_exit = self._should_zero_churn_guillotine_exit(position, candidate)
            if zero_churn_exit:
                close_event = self.close_position(key, zero_churn_exit["exit_reason"], now_ts=now_ts)
                if close_event:
                    close_event.update(zero_churn_exit)
                    events.append(close_event)
                continue

            no_follow_exit = self._should_no_follow_through_exit(position, candidate)
            if no_follow_exit:
                close_event = self.close_position(key, no_follow_exit["exit_reason"], now_ts=now_ts)
                if close_event:
                    close_event.update(no_follow_exit)
                    events.append(close_event)
                continue

            time_decay_exit = self._should_time_decay_exit(position, candidate)
            if time_decay_exit:
                close_event = self.close_position(key, time_decay_exit["exit_reason"], now_ts=now_ts)
                if close_event:
                    close_event.update(time_decay_exit)
                    events.append(close_event)
                continue

            governor_exit = self._should_opportunity_cost_governor_exit(position, candidate, len(self.open_positions))
            if governor_exit:
                close_event = self.close_position(key, governor_exit["exit_reason"], now_ts=now_ts)
                if close_event:
                    close_event.update(governor_exit)
                    events.append(close_event)
                continue

            capital_exit = self._should_capital_discipline_exit(position, candidate, len(self.open_positions))
            if capital_exit:
                close_event = self.close_position(key, capital_exit["exit_reason"], now_ts=now_ts)
                if close_event:
                    close_event.update(capital_exit)
                    events.append(close_event)
                continue

            rotation_exit = self._should_capital_rotation_exit(position, candidate, len(self.open_positions))
            if rotation_exit:
                close_event = self.close_position(key, rotation_exit["exit_reason"], now_ts=now_ts)
                if close_event:
                    close_event.update(rotation_exit)
                    events.append(close_event)
                continue

            price_progress = 0.0
            if entry_price > 0:
                price_progress = abs((current_price / entry_price) - 1.0)

            # stale-exit отдельно и мягче для score
            if reason == "score":
                if age_cycles >= 12 and price_progress <= 0.035 and score <= max(0.35, self._safe_float(position.get("entry_score", score), score) * 0.70) and density < 0.18:
                    close_event = self.close_position(key, "time_stale_exit", now_ts=now_ts)
                    if close_event:
                        events.append(close_event)
                    continue
            else:
                if age_cycles >= 10 and price_progress <= 0.05 and score <= (self._safe_float(position.get("entry_score", score), score) + 0.05) and density < 0.20:
                    close_event = self.close_position(key, "time_stale_exit", now_ts=now_ts)
                    if close_event:
                        events.append(close_event)
                    continue

            seed_cfg = self._profit_lock_seed_config(position)
            peak_pnl_pct = self._safe_float(position.get("peak_unrealized_pnl_pct", pnl_pct), pnl_pct)
            prev_peak_pnl_pct = self._safe_float(position.get("prev_peak_unrealized_pnl_pct", peak_pnl_pct), peak_pnl_pct)
            market_type = str(position.get("market_type", "general_binary") or "general_binary")
            seed_ready = age_cycles >= seed_cfg["min_age"]
            grace_until_age = int(position.get("peak_zero_repair_grace_until_age", 0) or 0)
            grace_active = bool(grace_until_age and age_cycles <= grace_until_age)
            monetization_bias = market_type in {
                "short_burst_catalyst", "legal_resolution", "valuation_ladder",
                "speculative_hype", "sports_award_longshot", "narrative_long_tail"
            }
            trigger_now = float(seed_cfg["trigger"])
            early_floor = 0.014 if monetization_bias else 0.02
            seed_fire = (
                (not bool(position.get("partial_tp_1_done", False)))
                and seed_ready
                and (
                    (peak_pnl_pct >= trigger_now and pnl_pct >= max(trigger_now - 0.06, early_floor))
                    or (monetization_bias and peak_pnl_pct >= max(trigger_now - 0.012, 0.040) and pnl_pct >= max(trigger_now - 0.085, early_floor))
                    or (market_type == "legal_resolution" and age_cycles >= 2 and peak_pnl_pct >= 0.025 and pnl_pct >= 0.015)
                    or (market_type == "narrative_long_tail" and age_cycles >= 2 and peak_pnl_pct >= 0.035 and pnl_pct >= 0.018)
                    or (monetization_bias and age_cycles >= 2 and peak_pnl_pct >= 0.04 and pnl_pct >= 0.02)
                )
            )

            print(
                "TRACE | profit_lock_seed_check | market_type={} | ready={} | trigger={:.4f} | fraction={:.2f} | age={} | pnl={:.4f} | peak={:.4f} | fire={} | {}".format(
                    market_type,
                    int(bool(seed_ready)),
                    trigger_now,
                    seed_cfg["fraction"],
                    age_cycles,
                    pnl_pct,
                    peak_pnl_pct,
                    int(bool(seed_fire)),
                    (position.get("question") or position.get("outcome_name") or "unknown")[:72],
                )
            )

            grace_bridge_candidate = (
                grace_active
                and (not bool(position.get("partial_tp_1_done", False)))
                and market_type in {"short_burst_catalyst", "legal_resolution", "valuation_ladder", "narrative_long_tail"}
                and age_cycles >= 2
                and prev_peak_pnl_pct <= 0.0001
                and (
                    pnl_pct >= (0.008 if market_type == "narrative_long_tail" else 0.010)
                    or peak_pnl_pct >= (0.010 if market_type == "narrative_long_tail" else 0.012)
                )
            )

            bridge_fired = False
            if grace_bridge_candidate:
                print(
                    "TRACE | grace_bridge_candidate | market_type={} | age={} | pnl={:.4f} | peak={:.4f} | prev_peak={:.4f} | until_age={} | {}".format(
                        market_type, age_cycles, pnl_pct, peak_pnl_pct, prev_peak_pnl_pct, grace_until_age,
                        (position.get("question") or position.get("outcome_name") or "unknown")[:72],
                    )
                )
                partial_event = self.partial_close_position(key, min(0.22, seed_cfg["fraction"]), "grace_bridge_micro", now_ts=now_ts)
                if partial_event:
                    print(
                        "TRACE | grace_bridge_fire | market_type={} | age={} | pnl={:.4f} | peak={:.4f} | fraction={:.2f} | {}".format(
                            market_type, age_cycles, pnl_pct, peak_pnl_pct, min(0.22, seed_cfg["fraction"]),
                            (position.get("question") or position.get("outcome_name") or "unknown")[:72],
                        )
                    )
                    position["partial_tp_1_done"] = True
                    position["profit_lock_seed_done"] = True
                    position["peak_zero_repair_grace_until_age"] = 0
                    position["post_bridge_protect_until_age"] = max(age_cycles + 2, int(position.get("post_bridge_protect_until_age", 0) or 0))
                    position["runner_protect_until_age"] = max(age_cycles + (3 if market_type == "sports_award_longshot" else 2), int(position.get("runner_protect_until_age", 0) or 0))
                    position["runner_lock_trigger_age"] = age_cycles
                    position["runner_locked_floor_pct"] = max(
                        float(position.get("runner_locked_floor_pct", 0.0) or 0.0),
                        max(0.008, min(0.020, peak_pnl_pct * (0.24 if market_type == "sports_award_longshot" else 0.22)))
                    )
                    position["winner_seed_realized_usd"] = float(position.get("winner_seed_realized_usd", 0.0) or 0.0) + float(partial_event.get("realized_pnl_usd", 0.0) or 0.0)
                    position["winner_carry_until_age"] = max(age_cycles + (5 if market_type == "short_burst_catalyst" else 4), int(position.get("winner_carry_until_age", 0) or 0))
                    position["winner_carry_anchor_peak_pct"] = max(float(position.get("winner_carry_anchor_peak_pct", 0.0) or 0.0), peak_pnl_pct)
                    print(
                        "TRACE | runner_protection_lock_arm | signal=grace_bridge | market_type={} | age={} | until_age={} | floor={:.4f} | realized={:.4f} | {}".format(
                            market_type,
                            age_cycles,
                            int(position.get("runner_protect_until_age", 0) or 0),
                            float(position.get("runner_locked_floor_pct", 0.0) or 0.0),
                            float(position.get("winner_seed_realized_usd", 0.0) or 0.0),
                            (position.get("question") or position.get("outcome_name") or "unknown")[:72],
                        )
                    )
                    bridge_fired = True
                    events.append(partial_event)

            micro_peak_harvest = (
                (not bool(position.get("partial_tp_1_done", False)))
                and seed_ready
                and (not seed_fire)
                and market_type in {"short_burst_catalyst", "legal_resolution", "valuation_ladder", "narrative_long_tail"}
                and age_cycles >= 2
                and prev_peak_pnl_pct <= 0.0001
                and pnl_pct >= (
                    0.010 if market_type == "narrative_long_tail"
                    else 0.011 if market_type == "legal_resolution"
                    else 0.012
                )
            )

            if micro_peak_harvest:
                print(
                    "TRACE | peak_harvest_micro_fire | market_type={} | age={} | pnl={:.4f} | peak={:.4f} | prev_peak={:.4f} | {}".format(
                        market_type, age_cycles, pnl_pct, peak_pnl_pct, prev_peak_pnl_pct,
                        (position.get("question") or position.get("outcome_name") or "unknown")[:72],
                    )
                )
                partial_event = self.partial_close_position(key, min(0.30, seed_cfg["fraction"]), "peak_harvest_micro", now_ts=now_ts)
                if partial_event:
                    position["partial_tp_1_done"] = True
                    position["profit_lock_seed_done"] = True
                    position["peak_zero_repair_grace_until_age"] = 0
                    position["runner_protect_until_age"] = max(age_cycles + (3 if market_type == "sports_award_longshot" else 2), int(position.get("runner_protect_until_age", 0) or 0))
                    position["runner_lock_trigger_age"] = age_cycles
                    position["runner_locked_floor_pct"] = max(
                        float(position.get("runner_locked_floor_pct", 0.0) or 0.0),
                        max(0.008, min(0.020, peak_pnl_pct * (0.24 if market_type == "sports_award_longshot" else 0.22)))
                    )
                    position["winner_seed_realized_usd"] = float(position.get("winner_seed_realized_usd", 0.0) or 0.0) + float(partial_event.get("realized_pnl_usd", 0.0) or 0.0)
                    position["winner_carry_until_age"] = max(age_cycles + (5 if market_type == "short_burst_catalyst" else 4), int(position.get("winner_carry_until_age", 0) or 0))
                    position["winner_carry_anchor_peak_pct"] = max(float(position.get("winner_carry_anchor_peak_pct", 0.0) or 0.0), peak_pnl_pct)
                    print(
                        "TRACE | runner_protection_lock_arm | signal=seed_or_micro | market_type={} | age={} | until_age={} | floor={:.4f} | realized={:.4f} | {}".format(
                            market_type,
                            age_cycles,
                            int(position.get("runner_protect_until_age", 0) or 0),
                            float(position.get("runner_locked_floor_pct", 0.0) or 0.0),
                            float(position.get("winner_seed_realized_usd", 0.0) or 0.0),
                            (position.get("question") or position.get("outcome_name") or "unknown")[:72],
                        )
                    )
                    events.append(partial_event)

            if (not seed_fire) and (not micro_peak_harvest) and peak_pnl_pct <= 0.0001 and seed_ready and pnl_pct <= 0.0001:
                if grace_active:
                    print(
                        "TRACE | grace_bridge_candidate | market_type={} | age={} | pnl={:.4f} | peak={:.4f} | prev_peak={:.4f} | until_age={} | status=waiting_zero_peak | {}".format(
                            market_type, age_cycles, pnl_pct, peak_pnl_pct, prev_peak_pnl_pct, grace_until_age,
                            (position.get("question") or position.get("outcome_name") or "unknown")[:72],
                        )
                    )
                print(
                    "TRACE | seed_blocked_by_peak_zero | market_type={} | age={} | pnl={:.4f} | peak={:.4f} | prev_peak={:.4f} | {}".format(
                        market_type,
                        age_cycles,
                        pnl_pct,
                        peak_pnl_pct,
                        prev_peak_pnl_pct,
                        (position.get("question") or position.get("outcome_name") or "unknown")[:72],
                    )
                )

            if seed_fire and bridge_fired:
                print(
                    "TRACE | bridge_dedup_skip_seed | market_type={} | age={} | pnl={:.4f} | peak={:.4f} | {}".format(
                        market_type, age_cycles, pnl_pct, peak_pnl_pct,
                        (position.get("question") or position.get("outcome_name") or "unknown")[:72],
                    )
                )

            if seed_fire and (not bridge_fired):
                partial_event = self.partial_close_position(key, seed_cfg["fraction"], "profit_lock_seed", now_ts=now_ts)
                if partial_event:
                    print(
                        "TRACE | profit_lock_seed_fire | market_type={} | fraction={:.2f} | pnl={:.4f} | peak={:.4f} | {}".format(
                            market_type,
                            seed_cfg["fraction"],
                            pnl_pct,
                            peak_pnl_pct,
                            (position.get("question") or position.get("outcome_name") or "unknown")[:72],
                        )
                    )
                    position["partial_tp_1_done"] = True
                    position["profit_lock_seed_done"] = True
                    position["peak_zero_repair_grace_until_age"] = 0
                    position["winner_seed_realized_usd"] = float(position.get("winner_seed_realized_usd", 0.0) or 0.0) + float(partial_event.get("realized_pnl_usd", 0.0) or 0.0)
                    position["winner_carry_until_age"] = max(age_cycles + (5 if market_type == "short_burst_catalyst" else 4), int(position.get("winner_carry_until_age", 0) or 0))
                    position["winner_carry_anchor_peak_pct"] = max(float(position.get("winner_carry_anchor_peak_pct", 0.0) or 0.0), peak_pnl_pct)
                    events.append(partial_event)

            if key in self._open_keys and (not bool(position.get("partial_tp_2_done", False))) and pnl_pct >= 0.55:
                partial_event = self.partial_close_position(key, 0.35, "partial_take_profit_2", now_ts=now_ts)
                if partial_event:
                    position["partial_tp_2_done"] = True
                    events.append(partial_event)

            if key not in self._open_keys:
                continue

            if bool(position.get("partial_tp_1_done", False)):
                retrace_floor = 0.040 if str(position.get("open_regime", regime) or regime) == "calm" else 0.050
                peak_pnl_pct = self._safe_float(position.get("peak_unrealized_pnl_pct", pnl_pct), pnl_pct)
                winner_carry = self._winner_carry_state(position, candidate)
                if winner_carry.get("active", False):
                    retrace_floor = max(retrace_floor, float(winner_carry.get("carry_floor_pct", 0.0) or 0.0))
                if peak_pnl_pct >= max(seed_cfg["trigger"] + 0.00, 0.08) and pnl_pct <= retrace_floor and age_cycles >= 2:
                    if winner_carry.get("active", False) and pnl_pct >= float(winner_carry.get("carry_floor_pct", 0.0) or 0.0) and winner_carry.get("strong_follow", False):
                        print(
                            "TRACE | winner_carry_decay_hold | market_type={} | peak={:.4f} | pnl={:.4f} | floor={:.4f} | {}".format(
                                winner_carry.get("market_type", position.get("market_type", "general_binary")),
                                peak_pnl_pct,
                                pnl_pct,
                                float(winner_carry.get("carry_floor_pct", 0.0) or 0.0),
                                (position.get("question") or position.get("outcome_name") or "unknown")[:72],
                            )
                        )
                    else:
                        print(
                            "TRACE | profit_lock_decay_check | fire=1 | peak={:.4f} | pnl={:.4f} | floor={:.4f} | {}".format(
                                peak_pnl_pct,
                                pnl_pct,
                                retrace_floor,
                                (position.get("question") or position.get("outcome_name") or "unknown")[:72],
                            )
                        )
                        close_event = self.close_position(key, "profit_lock_decay_exit", now_ts=now_ts)
                        if close_event:
                            close_event.update({
                                "peak_pnl_pct": round(peak_pnl_pct, 6),
                                "retrace_floor": round(retrace_floor, 6),
                                "seed_trigger": round(seed_cfg["trigger"], 6),
                            })
                            events.append(close_event)
                        continue

            runner_lock_exit = self._should_runner_protection_lock_exit(position, candidate)
            if runner_lock_exit:
                close_event = self.close_position(key, runner_lock_exit["exit_reason"], now_ts=now_ts)
                if close_event:
                    close_event.update(runner_lock_exit)
                    events.append(close_event)
                continue

            trailing_exit = self._should_trailing_momentum_exit(position, candidate)
            if trailing_exit:
                close_event = self.close_position(key, trailing_exit["exit_reason"], now_ts=now_ts)
                if close_event:
                    close_event.update(trailing_exit)
                    events.append(close_event)
                continue

            pressure_decay_exit = self._should_pressure_decay_exit(position, candidate)
            if pressure_decay_exit:
                close_event = self.close_position(key, pressure_decay_exit["exit_reason"], now_ts=now_ts)
                if close_event:
                    close_event.update(pressure_decay_exit)
                    events.append(close_event)
                continue

            if int(position.get("scale_in_count", 0)) < 1 and age_cycles >= 3 and int(position.get("cycles_since_scale_in", 0)) >= 2:
                entry_score = self._safe_float(position.get("entry_score", score), score)
                peak_pnl_pct = self._safe_float(position.get("peak_unrealized_pnl_pct", pnl_pct), pnl_pct)
                entry_density = self._safe_float(position.get("entry_pressure_density", density), density)
                entry_count = int(position.get("entry_pressure_count", 0) or 0)
                peak_density_seen = self._safe_float(position.get("max_pressure_density_seen", density), density)
                current_window = self._safe_float(candidate.get("price_delta_window", position.get("last_window_delta", 0.0)), position.get("last_window_delta", 0.0))

                score_improved = score >= max(entry_score + 0.22, entry_score * 1.15)
                structure_votes = 0
                if trend >= 0.82:
                    structure_votes += 1
                if density >= max(0.55, entry_density + 0.12):
                    structure_votes += 1
                if current_window >= 0.014:
                    structure_votes += 1
                structure_ok = structure_votes >= 2
                validation_gate = (
                    peak_pnl_pct >= 0.06
                    or (pnl_pct >= 0.025 and int(candidate.get("pressure_count", position.get("last_pressure_count", 0)) or 0) >= entry_count + 1)
                    or (peak_density_seen >= 0.55 and density >= peak_density_seen * 0.92 and current_window >= 0.012)
                )
                price_not_too_far = current_price <= max(entry_price * 1.24, entry_price + 0.05) if entry_price > 0 else True
                not_too_red = pnl_pct >= 0.005
                portfolio_not_crowded = len(self.open_positions) < 8 and self._cluster_heat(position.get("cluster", candidate.get("cluster", "unknown"))) < 4.2
                add_stake = min(
                    self.default_stake,
                    max(0.5, self._safe_float(position.get("cost_basis_remaining", 0.0), 0.0) * 0.40)
                )

                if score_improved and structure_ok and validation_gate and price_not_too_far and not_too_red and portfolio_not_crowded and self.balance_free >= add_stake:
                    scale_event = self.scale_in_position(key, add_stake, candidate, now_ts=now_ts)
                    if scale_event:
                        events.append(scale_event)

        return events

    def theme_exposure(self) -> Dict[str, float]:
        result = {}
        for pos in self.open_positions:
            theme = pos.get("theme", "unknown")
            result[theme] = result.get(theme, 0.0) + self._safe_float(pos.get("cost_basis_remaining", 0.0), 0.0)
        return result

    def cluster_exposure(self) -> Dict[str, float]:
        result = {}
        for pos in self.open_positions:
            cluster = pos.get("cluster", "unknown")
            result[cluster] = result.get(cluster, 0.0) + self._safe_float(pos.get("cost_basis_remaining", 0.0), 0.0)
        return result

    def summary(self) -> Dict:
        current_market_value = 0.0
        for pos in self.open_positions:
            current_market_value += self._safe_float(pos.get("market_value", 0.0), 0.0)

        unrealized_pnl_total = current_market_value - self.open_cost_basis
        total_equity = self.balance_free + current_market_value

        return {
            "paper_balance_free": round(self.balance_free, 4),
            "open_positions": len(self.open_positions),
            "closed_positions": len(self.closed_positions),
            "paper_spent_total": round(self.paper_spent_total, 4),
            "open_cost_basis": round(self.open_cost_basis, 4),
            "current_market_value": round(current_market_value, 4),
            "unrealized_pnl_total": round(unrealized_pnl_total, 4),
            "realized_pnl_total": round(self.realized_pnl_total, 4),
            "total_equity": round(total_equity, 4),
            "theme_exposure": {k: round(v, 4) for k, v in self.theme_exposure().items()},
            "cluster_exposure": {k: round(v, 4) for k, v in self.cluster_exposure().items()},
            "family_exposure": {k: round(v, 4) for k, v in self.family_exposure().items()},
        }