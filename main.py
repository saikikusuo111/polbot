# v21.7.3 patch
# v20.1 version copy
# v20.0 version copy
# v19.9.1 version copy
import asyncio
import aiohttp
import json
import os
import re
import time
import sys
import hashlib
import importlib
import importlib.util
from pathlib import Path

from collections import deque

from config.settings import DATA_DIR, SCAN_INTERVAL_SEC, MIN_PRICE, MAX_PRICE, MIN_LIQUIDITY, MIN_MINUTES_TO_END
from analytics.logger import ensure_dir, append_jsonl, utc_now_iso
from core.collector import fetch_markets, extract_candidate_outcomes
from core.filters import filter_candidates, is_blacklisted, is_junk_market, is_far_future_politics
from core.scorer import rank_candidates, detect_theme
MAIN_PATCH_VERSION = "v21.7.8"
_PATCH_SUFFIX = MAIN_PATCH_VERSION.replace(".", "_")
_SCRIPT_DIR = Path(__file__).resolve().parent if "__file__" in globals() else Path.cwd()


def _load_patch_module(base_name, package_candidates=()):
    local_candidates = [
        _SCRIPT_DIR / f"{base_name}_{_PATCH_SUFFIX}.py",
        _SCRIPT_DIR / f"{base_name}.py",
    ]
    for path in local_candidates:
        if path.exists():
            module_name = f"{base_name}_{_PATCH_SUFFIX}_runtime"
            spec = importlib.util.spec_from_file_location(module_name, str(path))
            if spec and spec.loader:
                module = importlib.util.module_from_spec(spec)
                sys.modules[module_name] = module
                spec.loader.exec_module(module)
                return module
    for dotted in package_candidates:
        try:
            return importlib.import_module(dotted)
        except Exception:
            continue
    raise ImportError(f"Unable to load patch module: {base_name}")


paper_engine_module = _load_patch_module("paper_engine", ("simulation.paper_engine", "paper_engine"))
PaperEngine = getattr(paper_engine_module, "PaperEngine")
PAPER_ENGINE_VERSION = getattr(paper_engine_module, "PAPER_ENGINE_VERSION", "unknown")

edge_registry_module = _load_patch_module("edge_registry", ("core.edge_registry", "edge_registry"))
EDGE_REGISTRY_VERSION = getattr(edge_registry_module, "EDGE_REGISTRY_VERSION", "unknown")
incoming_competition_gate = getattr(edge_registry_module, "incoming_competition_gate")

regime_detector_module = _load_patch_module("regime_detector", ("core.regime_detector", "regime_detector"))
REGIME_DETECTOR_VERSION = getattr(regime_detector_module, "REGIME_DETECTOR_VERSION", "unknown")
detect_market_regime = getattr(regime_detector_module, "detect_market_regime")
regime_settings = getattr(regime_detector_module, "regime_settings")
regime_trace_line = getattr(regime_detector_module, "regime_trace_line")


def _hash_file_short(path_like):
    try:
        path = Path(path_like)
        h = hashlib.sha256()
        with path.open("rb") as fh:
            for chunk in iter(lambda: fh.read(65536), b""):
                h.update(chunk)
        return h.hexdigest()[:12]
    except Exception:
        return "unknown"


def _module_runtime_manifest(label, module_obj, version_value, expected_version):
    raw_path = getattr(module_obj, "__file__", "unknown")
    path = Path(raw_path) if raw_path and raw_path != "unknown" else None
    basename = path.name if path else "unknown"
    allowed = {f"{label}.py", f"{label}_{_PATCH_SUFFIX}.py"}
    return {
        "label": label,
        "expected_version": expected_version,
        "version": str(version_value or "unknown"),
        "path": str(path) if path else "unknown",
        "basename": basename,
        "path_ok": basename in allowed,
        "hash": _hash_file_short(path) if path else "unknown",
    }


def enforce_runtime_integrity_lock():
    main_path = Path(__file__).resolve() if "__file__" in globals() else Path.cwd() / "main.py"
    main_allowed = {"main.py", f"main_{_PATCH_SUFFIX}.py"}
    manifest = {
        "main": {
            "label": "main",
            "expected_version": MAIN_PATCH_VERSION,
            "version": MAIN_PATCH_VERSION,
            "path": str(main_path),
            "basename": main_path.name,
            "path_ok": main_path.name in main_allowed,
            "hash": _hash_file_short(main_path),
        },
        "paper_engine": _module_runtime_manifest("paper_engine", paper_engine_module, PAPER_ENGINE_VERSION, MAIN_PATCH_VERSION),
        "edge_registry": _module_runtime_manifest("edge_registry", edge_registry_module, EDGE_REGISTRY_VERSION, MAIN_PATCH_VERSION),
        "regime_detector": _module_runtime_manifest("regime_detector", regime_detector_module, REGIME_DETECTOR_VERSION, MAIN_PATCH_VERSION),
    }
    mismatches = []
    for label, info in manifest.items():
        if info["version"] != info["expected_version"]:
            mismatches.append(f"{label}:version={info['version']}!=expected={info['expected_version']}")
        if not info["path_ok"]:
            mismatches.append(f"{label}:path={info['basename']}")

    if mismatches:
        print("ERROR | runtime_integrity_mismatch | {}".format(" ; ".join(mismatches)))
        for label, info in manifest.items():
            print(
                "TRACE | runtime_integrity_detail | label={} | version={} | expected={} | path={} | hash={} | path_ok={}".format(
                    label, info["version"], info["expected_version"], info["path"], info["hash"], int(bool(info["path_ok"]))
                )
            )
        raise RuntimeError("runtime_integrity_mismatch")

    print(
        "TRACE | runtime_integrity_ok | main={}@{}#{} | paper_engine={}@{}#{} | edge_registry={}@{}#{} | regime_detector={}@{}#{}".format(
            manifest["main"]["version"], manifest["main"]["basename"], manifest["main"]["hash"],
            manifest["paper_engine"]["version"], manifest["paper_engine"]["basename"], manifest["paper_engine"]["hash"],
            manifest["edge_registry"]["version"], manifest["edge_registry"]["basename"], manifest["edge_registry"]["hash"],
            manifest["regime_detector"]["version"], manifest["regime_detector"]["basename"], manifest["regime_detector"]["hash"],
        )
    )
    return manifest

SNAPSHOT_FILE = os.path.join(DATA_DIR, "market_snapshots.jsonl")
PAPER_TRADES_FILE = os.path.join(DATA_DIR, "paper_trades.jsonl")
STATE_FILE = os.path.join(DATA_DIR, "runtime_state.json")

ZERO_CHURN_MEMORY_EXIT_REASONS = {
    "no_follow_through_exit",
    "follow_through_compression_fail",
    "zero_peak_scout_cut",
    "normal_zero_peak_linger_cut",
    "sports_zero_peak_fire_exit",
    "zero_churn_guillotine_exit",
}

ZERO_CHURN_REALIZED_USD_THRESHOLD = 0.03


def calm_flex_admit(reason, combined_score, pressure_density, price_trend_strength, price_delta_window, pressure_count):
    try:
        score = float(combined_score or 0.0)
        pd = float(pressure_density or 0.0)
        trend = float(price_trend_strength or 0.0)
        win = abs(float(price_delta_window or 0.0))
        pcount = int(pressure_count or 0)
    except Exception:
        return False

    # v21.6.3: keep calm intelligence strict, use pressure-backed relief separately
    if pcount >= 3 and (win >= 0.0020 or pd >= 0.32) and score >= 0.82:
        return True
    if pcount >= 2 and pd >= 0.18 and (trend >= 0.90 or win >= 0.0030) and score >= 0.86:
        return True
    if reason in {"score+pressure", "pressure"} and pcount >= 2 and (win >= 0.0025 or pd >= 0.22):
        return True
    if reason in {"multicycle_momentum_override", "score+momentum", "momentum_override"} and (pcount >= 2 or trend >= 0.95) and win >= 0.0025 and score >= 0.88:
        return True
    if trend >= 0.95 and win >= 0.0030 and score >= 0.92:
        return True
    return False


def calm_pressure_backed_relief(reason, combined_score, pressure_density, price_trend_strength, price_delta_window, pressure_count, price_delta=0.0, delayed_memory=False):
    try:
        score = float(combined_score or 0.0)
        pd = float(pressure_density or 0.0)
        trend = float(price_trend_strength or 0.0)
        win = abs(float(price_delta_window or 0.0))
        delta1 = abs(float(price_delta or 0.0))
        pcount = int(pressure_count or 0)
        delayed = bool(delayed_memory)
    except Exception:
        return False

    strong_pressure = (pcount >= 2) or (pd >= 0.18) or (pcount >= 1 and (pd >= 0.12 or win >= 0.0010 or delta1 >= 0.0010))
    pressure_backed = (
        strong_pressure
        or trend >= 0.90
        or win >= 0.0015
        or delta1 >= 0.0015
        or (delayed and pcount >= 1)
    )
    if not pressure_backed:
        return False

    if reason in {"score+pressure", "pressure", "score+pre_momentum", "pre_momentum"}:
        if score >= 0.96 and (pd >= 0.10 or pcount >= 1) and (trend >= 0.86 or win >= 0.0010 or delta1 >= 0.0010 or delayed):
            return True

    if reason in {"multicycle_momentum_override", "momentum_override", "score+momentum"}:
        if score >= 0.92 and (pcount >= 1 or pd >= 0.10 or trend >= 0.90) and (win >= 0.0010 or delta1 >= 0.0010 or delayed):
            return True

    if reason == "score":
        if score >= 1.04 and pcount >= 1 and (pd >= 0.10 or trend >= 0.88 or win >= 0.0010 or delta1 >= 0.0010):
            return True
        if score >= 1.08 and strong_pressure and (trend >= 0.84 or win >= 0.0008 or delta1 >= 0.0008):
            return True
        if delayed and score >= 1.00 and (pcount >= 1 or pd >= 0.10) and (trend >= 0.86 or win >= 0.0015 or delta1 >= 0.0015):
            return True

    return False


def maybe_log_calm_pressure_relief_context(regime_name, reason, combined_score, pressure_density, price_trend_strength, price_delta_window, pressure_count, price_delta, delayed_memory, question):
    try:
        if regime_name != "calm":
            return False, False

        score = float(combined_score or 0.0)
        pd = float(pressure_density or 0.0)
        trend = float(price_trend_strength or 0.0)
        win = float(price_delta_window or 0.0)
        delta1 = float(price_delta or 0.0)
        pcount = int(pressure_count or 0)
        delayed = bool(delayed_memory)

        relief_allowed = calm_pressure_backed_relief(reason, score, pd, trend, win, pcount, delta1, delayed)
        borderline = (
            (pcount >= 1 and (pd >= 0.08 or abs(win) >= 0.0010 or abs(delta1) >= 0.0010))
            or (trend >= 0.86 and (abs(win) >= 0.0010 or abs(delta1) >= 0.0010))
            or (delayed and score >= 1.00)
        )

        if relief_allowed:
            print(
                "TRACE | calm_pressure_relief_check | verdict=admit | reason={} | score={:.3f} | pd={:.3f} | trend={:.3f} | delta1={:.3f} | win={:.3f} | pcount={} | delayed={} | {}".format(
                    reason, score, pd, trend, delta1, win, pcount, int(delayed), question
                )
            )
        elif borderline:
            print(
                "TRACE | calm_pressure_relief_reject | reason={} | score={:.3f} | pd={:.3f} | trend={:.3f} | delta1={:.3f} | win={:.3f} | pcount={} | delayed={} | {}".format(
                    reason, score, pd, trend, delta1, win, pcount, int(delayed), question
                )
            )

        return relief_allowed, borderline
    except Exception:
        return False, False

def maybe_log_calm_flex_context(regime_name, reason, combined_score, pressure_density, price_trend_strength, price_delta_window, pressure_count, question):
    try:
        if regime_name != "calm":
            return False, False

        score = float(combined_score or 0.0)
        pd = float(pressure_density or 0.0)
        trend = float(price_trend_strength or 0.0)
        win = float(price_delta_window or 0.0)
        pcount = int(pressure_count or 0)

        flex_allowed = calm_flex_admit(reason, score, pd, trend, win, pcount)
        borderline = (
            (pcount >= 2 and (pd >= 0.16 or abs(win) >= 0.0020))
            or (trend >= 0.85 and abs(win) >= 0.0020)
            or score >= 0.90
        )

        if flex_allowed:
            print(
                "TRACE | calm_flex_check | verdict=admit | reason={} | score={:.3f} | pd={:.3f} | trend={:.3f} | win={:.3f} | pcount={} | {}".format(
                    reason, score, pd, trend, win, pcount, question
                )
            )
        elif borderline:
            print(
                "TRACE | calm_flex_reject | reason={} | score={:.3f} | pd={:.3f} | trend={:.3f} | win={:.3f} | pcount={} | {}".format(
                    reason, score, pd, trend, win, pcount, question
                )
            )

        return flex_allowed, borderline
    except Exception:
        return False, False


def adaptive_calm_admission_relief_state(candidate, reason, current_regime, market_exit_memory=None):
    try:
        if current_regime != "calm":
            return {"considered": False, "active": False, "signal": None, "reject_reason": "regime_not_calm"}

        market_type = str(candidate.get("market_type", "general_binary") or "general_binary")
        theme = str(candidate.get("theme", "general") or "general")
        source = str(candidate.get("_universe_source") or "primary")
        score = float(candidate.get("score", 0.0) or 0.0)
        density = float(candidate.get("pressure_density", 0.0) or 0.0)
        trend = float(candidate.get("price_trend_strength", 0.0) or 0.0)
        delta1 = abs(float(candidate.get("price_delta", 0.0) or 0.0))
        win = abs(float(candidate.get("price_delta_window", 0.0) or 0.0))
        pcount = int(candidate.get("pressure_count", 0) or 0)
        delayed_confirmed = bool(candidate.get("_delayed_entry_confirmed", False) or candidate.get("_delayed_entry_light", False))
        already_open = bool(candidate.get("already_open", False))

        considered = source != "explorer"
        if market_type in {"sports_award_longshot"}:
            return {"considered": considered, "active": False, "signal": None, "reject_reason": "sports_excluded"}

        nonflat = (pcount >= 2) or (density >= 0.24) or (win >= 0.0045) or (delta1 >= 0.0035)
        strong = (
            score >= 1.08
            and (
                (pcount >= 2 and density >= 0.16)
                or density >= 0.28
                or (trend >= 0.90 and win >= 0.0030)
                or win >= 0.0065
                or delta1 >= 0.0060
            )
        )
        if not nonflat:
            return {"considered": considered, "active": False, "signal": None, "reject_reason": "structure_flat"}
        if not strong:
            return {"considered": considered, "active": False, "signal": None, "reject_reason": "score_too_low"}

        legalish = market_type == "legal_resolution"
        if reason == "score" and score < (1.14 if legalish else 1.10) and pcount < 2 and win < 0.0060:
            return {"considered": considered, "active": False, "signal": None, "reject_reason": "score_only_not_strong_enough"}

        signal = "adaptive_calm_admission_relief"
        cap = 0.62 if legalish else (0.68 if market_type in {"general_binary", "valuation_ladder", "narrative_long_tail"} else 0.74)
        if score >= 1.28 and pcount >= 2 and density >= 0.24 and win >= 0.0050:
            cap = max(cap, 0.82)
        force_delayed = not delayed_confirmed and not already_open
        micro_scout = True

        return {
            "considered": considered,
            "active": True,
            "signal": signal,
            "cap": round(float(cap), 4),
            "force_delayed": bool(force_delayed),
            "micro_scout": bool(micro_scout),
            "score": score,
            "density": density,
            "trend": trend,
            "delta": delta1,
            "window_delta": win,
            "pressure_count": pcount,
            "market_type": market_type,
            "theme": theme,
        }
    except Exception:
        return {"considered": False, "active": False, "signal": None, "reject_reason": "exception"}




def admission_budget_limit(current_regime, engine=None, opened_now=0):
    try:
        open_positions = int(len(getattr(engine, "open_positions", []) or [])) if engine is not None else 0
    except Exception:
        open_positions = 0
    if current_regime == "hot":
        return 0
    if current_regime == "normal":
        return 2 if open_positions <= 1 and int(opened_now or 0) == 0 else 1
    if current_regime == "calm":
        if open_positions == 0 and int(opened_now or 0) == 0:
            return 2
        return 1
    return 0


def adaptive_admission_budget_state(candidate, reason, current_regime, market_exit_memory=None, budget_used=0, budget_total=0, engine=None, opened_now=0):
    try:
        score = float(candidate.get("score", 0.0) or 0.0)
        density = float(candidate.get("pressure_density", 0.0) or 0.0)
        pressure_count = int(candidate.get("pressure_count", 0) or 0)
        trend = float(candidate.get("price_trend_strength", 0.0) or 0.0)
        raw_window_delta = float(candidate.get("price_delta_window", 0.0) or 0.0)
        window_delta = abs(raw_window_delta)
        raw_delta = float(candidate.get("price_delta", 0.0) or 0.0)
        delta = abs(raw_delta)
        market_type = str(candidate.get("market_type", "general_binary") or "general_binary")
        theme = str(candidate.get("theme", "general") or "general")
        family_key = str(candidate.get("family_key", "") or "")
        source = str(candidate.get("_universe_source") or "primary")
        delayed_memory = bool(candidate.get("_delayed_entry_memory_active", False) or candidate.get("delayed_entry_memory_active", False))
        delayed_watch = bool(candidate.get("_delayed_entry_watch_active", False) or candidate.get("delayed_entry_watch_active", False))
        delayed_confirmed = bool(candidate.get("_delayed_entry_confirmed", False))
    except Exception:
        return {"considered": False, "active": False, "signal": None, "reject_reason": "exception"}

    budget_total = int(budget_total or 0)
    budget_used = int(budget_used or 0)
    remaining = max(0, budget_total - budget_used)
    if budget_total <= 0:
        return {"considered": False, "active": False, "signal": None, "reject_reason": "budget_disabled", "remaining": 0}

    if source == "explorer":
        return {"considered": False, "active": False, "signal": None, "reject_reason": "explorer_excluded", "remaining": remaining}
    if market_type in {"sports_award_longshot"}:
        return {"considered": False, "active": False, "signal": None, "reject_reason": "sports_excluded", "remaining": remaining}

    key = build_market_key(candidate)
    memory = dict((market_exit_memory or {}).get(key, {}) or {}) if market_exit_memory is not None else {}
    family_memory = {}
    if family_key and market_exit_memory is not None:
        try:
            family_memory = dict((market_exit_memory or {}).get(build_family_memory_key(candidate), {}) or {})
        except Exception:
            family_memory = {}

    structure_votes = int(follow_through_structure_votes(candidate) or 0)
    reentry_votes = int(reentry_signal_votes(candidate) or 0)

    non_flat = bool(
        pressure_count >= 1
        or density >= 0.10
        or trend >= 0.84
        or window_delta >= 0.0012
        or delta >= 0.0012
    )
    strong_non_flat = bool(
        structure_votes >= 3
        or reentry_votes >= 4
        or pressure_count >= 2
        or density >= 0.24
        or trend >= 0.92
        or window_delta >= 0.0045
        or delta >= 0.0045
        or (score >= 1.18 and non_flat and structure_votes >= 2)
    )

    memory_pressure = int(memory.get("dead_exit_count", 0) or 0) + int(memory.get("stale_exit_count", 0) or 0) + int(memory.get("weak_failed_count", 0) or 0)
    family_pressure = int(family_memory.get("family_reopen_brake_count", 0) or 0) + int(family_memory.get("dead_money_exit_count", 0) or 0) + int(family_memory.get("zero_peak_family_count", 0) or 0)
    legal_pressure = int(memory.get("legal_false_pressure_quarantine_count", 0) or 0) + int(memory.get("legal_replay_exit_count", 0) or 0) + int(memory.get("legal_stale_loss_count", 0) or 0)

    considered = bool(non_flat and (delayed_memory or delayed_watch or memory_pressure >= 1 or family_pressure >= 1 or legal_pressure >= 1))
    if not considered:
        return {"considered": False, "active": False, "signal": None, "reject_reason": "no_memory_pressure", "remaining": remaining}
    if remaining <= 0:
        return {"considered": True, "active": False, "signal": None, "reject_reason": "budget_exhausted", "remaining": 0}
    if not strong_non_flat:
        return {"considered": True, "active": False, "signal": None, "reject_reason": "structure_too_weak", "remaining": remaining}

    legalish = market_type == "legal_resolution"
    if legalish and reason in {"score", "score+pressure", "pressure", "score+pre_momentum", "pre_momentum"} and score < 1.18 and pressure_count < 2 and density < 0.24 and window_delta < 0.0060:
        return {"considered": True, "active": False, "signal": None, "reject_reason": "legal_edge_not_strong_enough", "remaining": remaining}

    signal = "adaptive_admission_budget"
    if legalish:
        signal = "legal_admission_budget"
    elif market_type in {"valuation_ladder", "narrative_long_tail", "general_binary"}:
        signal = "memory_reopen_budget"

    base_cap = 0.76 if current_regime == "normal" else 0.68
    if legalish:
        base_cap = max(base_cap, 0.86)
    if score >= 1.42 and pressure_count >= 2 and density >= 0.24:
        base_cap += 0.10
    cap = min(1.20, max(base_cap, execution_micro_clamp_cap(candidate, "relief_escalation") or 0.0))
    force_delayed = bool(not delayed_confirmed)
    micro_scout = True

    return {
        "considered": True,
        "active": True,
        "signal": signal,
        "cap": round(float(cap), 4),
        "force_delayed": bool(force_delayed),
        "micro_scout": bool(micro_scout),
        "remaining": remaining,
        "allow_dead_market": True,
        "allow_stale_market": True,
        "allow_family_dead": True,
        "allow_family_reopen": True,
        "allow_calm_score": bool(current_regime == "calm" and strong_non_flat),
        "allow_legal_cooldown": bool(legalish and (pressure_count >= 2 or density >= 0.24 or delayed_confirmed)),
        "allow_legal_replay": bool(legalish and (pressure_count >= 2 or density >= 0.24 or window_delta >= 0.0060 or delayed_confirmed)),
        "allow_legal_false_pressure": bool(legalish and score >= 1.34 and (pressure_count >= 2 or density >= 0.28) and (window_delta >= 0.0060 or delta >= 0.0060 or delayed_confirmed)),
        "structure_votes": int(structure_votes),
        "reentry_votes": int(reentry_votes),
    }


def trace_adaptive_admission_budget_state(candidate, budget_state, reason):
    try:
        state = dict(budget_state or {})
        if bool(candidate.get("_adaptive_budget_route_traced", False)):
            return
        candidate["_adaptive_budget_route_traced"] = True
        if state.get("active", False):
            print(
                "TRACE | adaptive_admission_budget_seen | signal={} | remaining={} | cap={:.2f} | force_delayed={} | micro_scout={} | reason={} | score={:.3f} | pd={:.3f} | trend={:.3f} | delta1={:.3f} | win={:.3f} | votes={} | {}".format(
                    str(state.get("signal") or "adaptive_admission_budget"),
                    int(state.get("remaining", 0) or 0),
                    float(state.get("cap", 0.0) or 0.0),
                    int(bool(state.get("force_delayed", False))),
                    int(bool(state.get("micro_scout", False))),
                    reason,
                    float(candidate.get("score", 0.0) or 0.0),
                    float(candidate.get("pressure_density", 0.0) or 0.0),
                    float(candidate.get("price_trend_strength", 0.0) or 0.0),
                    abs(float(candidate.get("price_delta", 0.0) or 0.0)),
                    abs(float(candidate.get("price_delta_window", 0.0) or 0.0)),
                    int(state.get("structure_votes", 0) or 0),
                    (candidate.get("question") or "")[:96],
                )
            )
        elif state.get("considered", False):
            print(
                "TRACE | adaptive_admission_budget_reject | reason={} | reject_reason={} | remaining={} | score={:.3f} | pd={:.3f} | trend={:.3f} | delta1={:.3f} | win={:.3f} | {}".format(
                    reason,
                    str(state.get("reject_reason") or "activation_conditions_not_met"),
                    int(state.get("remaining", 0) or 0),
                    float(candidate.get("score", 0.0) or 0.0),
                    float(candidate.get("pressure_density", 0.0) or 0.0),
                    float(candidate.get("price_trend_strength", 0.0) or 0.0),
                    abs(float(candidate.get("price_delta", 0.0) or 0.0)),
                    abs(float(candidate.get("price_delta_window", 0.0) or 0.0)),
                    (candidate.get("question") or "")[:96],
                )
            )
    except Exception:
        return


def admission_budget_allows_block(state, block_name):
    state = dict(state or {})
    if not state.get("active", False):
        return False
    block = str(block_name or "")
    if block in {"dead_market_memory", "dead_reentry_cooldown"}:
        return bool(state.get("allow_dead_market", False))
    if block in {"stale_market_memory", "stale_reentry_cooldown"}:
        return bool(state.get("allow_stale_market", False))
    if block == "family_dead_cooldown":
        return bool(state.get("allow_family_dead", False))
    if block in {"family_reopen_brake", "family_reopen_memory_brake", "family_reopen_truth_gate"}:
        return bool(state.get("allow_family_reopen", False))
    if block in {"calm_authority_score_only", "primary_calm_gate", "primary_calm_long_tail", "primary_calm_speculative"}:
        return bool(state.get("allow_calm_score", False))
    if block == "legal_false_pressure_quarantine":
        return bool(state.get("allow_legal_false_pressure", False))
    if block in {"legal_cooldown_authority_gate", "legal_cooldown_memory_veto"}:
        return bool(state.get("allow_legal_cooldown", False))
    if block in {"legal_replay_quarantine", "legal_replay_memory", "legal_stale_loss_reentry_kill"}:
        return bool(state.get("allow_legal_replay", False))
    return False


def prime_admission_budget_route(candidate, budget_state, block_name):
    state = dict(budget_state or {})
    if not state.get("active", False):
        return candidate
    signal_root = str(state.get("signal") or "adaptive_admission_budget")
    signal = f"{signal_root}:{block_name}"
    cap = float(state.get("cap", execution_micro_clamp_cap(candidate, "adaptive_calm_relief")) or 0.0)
    candidate["_adaptive_budget_active"] = True
    candidate["_adaptive_budget_signal"] = signal
    candidate["_adaptive_budget_cap"] = round(float(cap or 0.0), 4)
    candidate["_adaptive_budget_force_delayed"] = bool(state.get("force_delayed", False))
    candidate["_adaptive_budget_micro_scout"] = bool(state.get("micro_scout", True) or state.get("force_delayed", False))
    candidate["_adaptive_budget_block"] = str(block_name or "unknown")
    candidate["_adaptive_budget_triggered"] = True
    print(
        "TRACE | adaptive_admission_budget_route | block={} | signal={} | cap={:.2f} | force_delayed={} | micro_scout={} | remaining={} | score={:.3f} | pd={:.3f} | trend={:.3f} | delta1={:.3f} | win={:.3f} | votes={} | {}".format(
            str(block_name or "unknown"),
            signal,
            float(cap or 0.0),
            int(bool(state.get("force_delayed", False))),
            int(bool(state.get("micro_scout", True))),
            int(state.get("remaining", 0) or 0),
            float(candidate.get("score", 0.0) or 0.0),
            float(candidate.get("pressure_density", 0.0) or 0.0),
            float(candidate.get("price_trend_strength", 0.0) or 0.0),
            abs(float(candidate.get("price_delta", 0.0) or 0.0)),
            abs(float(candidate.get("price_delta_window", 0.0) or 0.0)),
            int(state.get("structure_votes", 0) or 0),
            (candidate.get("question") or "")[:96],
        )
    )
    return candidate

def repair_political_mirror_state(candidate, delayed_entry_memory=None, delayed_entry_watch=None, delayed_entry_cooldown=None, stage="unknown", force=False):
    stake_model = dict(candidate.get("_stake_model", {}) or {})

    override_state = None
    try:
        override_state = political_family_override_state(
            candidate,
            delayed_entry_memory=delayed_entry_memory,
            delayed_entry_watch=delayed_entry_watch,
            delayed_entry_cooldown=delayed_entry_cooldown,
        )
    except Exception:
        override_state = None

    political_override_entry = bool(
        candidate.get("_political_family_override", stake_model.get("political_override_entry", False))
        or (override_state or {}).get("eligible", False)
    )
    targeted_override = bool(
        candidate.get("_political_targeted_override", stake_model.get("political_targeted_override", False))
        or (override_state or {}).get("targeted", False)
    )
    balance_rescue = bool(
        candidate.get("_balance_rescue_override", stake_model.get("balance_rescue_override", False))
        or bool((override_state or {}).get("balance_like", False) and (override_state or {}).get("eligible", False))
    )
    cross_family = bool(
        candidate.get("_cross_family_thesis_priority", stake_model.get("cross_family_thesis_priority", False))
        or (
            political_override_entry
            and (
                targeted_override
                or balance_rescue
                or bool(candidate.get("_delayed_entry_memory_active", False))
                or bool((override_state or {}).get("memory_active", False))
                or bool((override_state or {}).get("watch_active", False))
                or bool((override_state or {}).get("cooldown_active", False))
            )
        )
    )

    override_strength_hint = float(
        candidate.get("_political_override_hint_strength", stake_model.get("political_override_hint_strength", 0.0))
        or float((override_state or {}).get("hint_strength", 0.0) or 0.0)
    )
    override_strength = float(
        candidate.get("_political_override_strength", stake_model.get("political_override_strength", 0.0))
        or float((override_state or {}).get("strength", 0.0) or 0.0)
    )
    override_reason_hint = (
        candidate.get("_political_override_hint_reason")
        or stake_model.get("political_override_hint_reason")
        or (override_state or {}).get("hint_reason")
    )
    override_reason = (
        candidate.get("_political_override_reason")
        or stake_model.get("political_override_reason")
        or (override_state or {}).get("reason")
    )

    hold_window = bool(candidate.get("_political_hold_window", stake_model.get("political_hold_window", False)))
    hold_cycles = int(candidate.get("_political_hold_window_cycles", stake_model.get("political_hold_window_cycles", 0)) or 0)
    cross_cycles = int(candidate.get("_cross_family_priority_cycles", stake_model.get("cross_family_priority_cycles", 0)) or 0)
    corridor = bool(candidate.get("_override_survival_corridor", stake_model.get("override_survival_corridor", False)))
    mirror_expected = bool(political_override_entry or targeted_override or balance_rescue or cross_family)

    if mirror_expected:
        hold_window = True
        base_cycles = 8 if (targeted_override or balance_rescue) else 6
        if hold_cycles <= 0:
            hold_cycles = base_cycles
        if cross_family and cross_cycles <= 0:
            cross_cycles = max(hold_cycles, base_cycles)
        corridor = True

    if not political_override_entry and not mirror_expected:
        override_strength = 0.0
        override_reason = None

    before = (
        bool(candidate.get("_political_hold_window", False)),
        int(candidate.get("_political_hold_window_cycles", 0) or 0),
        int(candidate.get("_cross_family_priority_cycles", 0) or 0),
        bool(candidate.get("_override_survival_corridor", False)),
        bool(candidate.get("_flag_mirror_audit_expected", False)),
    )

    candidate["_political_family_override"] = bool(political_override_entry)
    candidate["_political_override_hint_strength"] = float(override_strength_hint or 0.0)
    candidate["_political_override_strength"] = float(override_strength or 0.0)
    candidate["_political_targeted_override"] = bool(targeted_override)
    candidate["_balance_rescue_override"] = bool(balance_rescue)
    candidate["_cross_family_thesis_priority"] = bool(cross_family)
    candidate["_political_hold_window"] = bool(hold_window)
    candidate["_political_hold_window_cycles"] = int(hold_cycles)
    candidate["_cross_family_priority_cycles"] = int(cross_cycles)
    candidate["_override_survival_corridor"] = bool(corridor)
    candidate["_flag_mirror_audit_expected"] = bool(mirror_expected)
    candidate["_political_override_hint_reason"] = override_reason_hint
    candidate["_political_override_reason"] = override_reason
    candidate["_political_override_applied"] = bool(political_override_entry)

    stake_model["political_override_entry"] = bool(political_override_entry)
    stake_model["political_override_hint_strength"] = float(override_strength_hint or 0.0)
    stake_model["political_override_strength"] = float(override_strength or 0.0)
    stake_model["political_targeted_override"] = bool(targeted_override)
    stake_model["balance_rescue_override"] = bool(balance_rescue)
    stake_model["cross_family_thesis_priority"] = bool(cross_family)
    stake_model["political_hold_window"] = bool(hold_window)
    stake_model["political_hold_window_cycles"] = int(hold_cycles)
    stake_model["cross_family_priority_cycles"] = int(cross_cycles)
    stake_model["override_survival_corridor"] = bool(corridor)
    stake_model["flag_mirror_audit_expected"] = bool(mirror_expected)
    stake_model["political_override_hint_reason"] = override_reason_hint
    stake_model["political_override_reason"] = override_reason
    candidate["_stake_model"] = dict(stake_model)

    candidate["political_override_active"] = bool(political_override_entry)
    candidate["political_override_strength"] = float(override_strength or 0.0)
    candidate["political_targeted_override"] = bool(targeted_override)
    candidate["balance_rescue_override"] = bool(balance_rescue)
    candidate["cross_family_thesis_priority"] = bool(cross_family)
    candidate["political_hold_window"] = bool(hold_window)
    candidate["political_hold_window_cycles"] = int(hold_cycles)
    candidate["cross_family_priority_cycles"] = int(cross_cycles)
    candidate["override_survival_corridor"] = bool(corridor)
    candidate["flag_mirror_audit_expected"] = bool(mirror_expected)
    if override_reason:
        candidate["political_override_reason"] = override_reason

    after = (
        bool(candidate.get("_political_hold_window", False)),
        int(candidate.get("_political_hold_window_cycles", 0) or 0),
        int(candidate.get("_cross_family_priority_cycles", 0) or 0),
        bool(candidate.get("_override_survival_corridor", False)),
        bool(candidate.get("_flag_mirror_audit_expected", False)),
    )

    changed = before != after
    if mirror_expected and (changed or force):
        print(
            "TRACE | political_mirror_repair | stage={} | hold={}({}) | cross={}({}) | corridor={} | mirror_expected={} | targeted={} | balance={} | strength={:.3f} | {}".format(
                stage,
                int(bool(hold_window)),
                int(hold_cycles),
                int(bool(cross_family)),
                int(cross_cycles),
                int(bool(corridor)),
                int(bool(mirror_expected)),
                int(bool(targeted_override)),
                int(bool(balance_rescue)),
                float(override_strength or 0.0),
                (candidate.get("question") or "")[:72],
            )
        )

    return candidate


def apply_preopen_political_synthesis(candidate):
    return repair_political_mirror_state(candidate, stage="preopen_synthesis", force=True)



def calm_probe_stake(stake_value, candidate, regime_name, opened_now=0):
    try:
        stake = float(stake_value or 0.0)
    except Exception:
        return stake_value

    if regime_name != "calm":
        return stake_value

    try:
        reason = str(candidate.get("reason", candidate.get("_entry_reason", "")) or "")
        pd = float(candidate.get("pressure_density", 0.0) or 0.0)
        trend = float(candidate.get("price_trend_strength", 0.0) or 0.0)
        win = abs(float(candidate.get("price_delta_window", 0.0) or 0.0))
        pcount = int(candidate.get("pressure_count", 0) or 0)
        score = float(candidate.get("score", 0.0) or 0.0)
        market_type = str(candidate.get("market_type", candidate.get("_market_type", "")) or "")
        delayed_memory = bool(candidate.get("delayed_entry_memory_active", False))
        political_override = bool(candidate.get("political_override_active", False))
    except Exception:
        return stake_value

    probe_candidate = False
    try:
        probe_candidate = calm_flex_admit(reason, score, pd, trend, win, pcount)
    except Exception:
        probe_candidate = False

    print(
        "TRACE | calm_probe_check | candidate={} | eligible={} | raw_stake={:.2f} | reason={} | pd={:.3f} | trend={:.3f} | win={:.3f} | pcount={}".format(
            (candidate.get("question") or "")[:72],
            int(bool(probe_candidate)),
            stake,
            reason,
            pd,
            trend,
            win,
            pcount,
        )
    )

    # v21.3.7.5: keep selective brake for fragile non-score calm entries
    selective_brake = (
        reason in {"pressure", "score+pressure", "pre_momentum", "score+pre_momentum"}
        and market_type in {"short_burst_catalyst", "legal_resolution", "valuation_ladder"}
    )
    if selective_brake:
        brake_cap = 1.25
        if market_type == "short_burst_catalyst":
            brake_cap = 1.20 if not delayed_memory else 1.35
        elif market_type == "legal_resolution":
            brake_cap = 1.15 if score < 1.15 else 1.25
        elif market_type == "valuation_ladder":
            brake_cap = 1.10 if not delayed_memory else 1.20

        if stake > brake_cap:
            print(
                "TRACE | calm_selective_stake_brake | old_stake={:.2f} | new_stake={:.2f} | reason={} | market_type={} | delayed_memory={} | score={:.3f} | {}".format(
                    stake,
                    brake_cap,
                    reason,
                    market_type,
                    int(bool(delayed_memory)),
                    score,
                    (candidate.get("question") or "")[:72],
                )
            )
            stake = brake_cap

    # v21.3.7.5: zero-peak scout mode for calm score entries
    scout_targets = {"short_burst_catalyst", "valuation_ladder", "narrative_long_tail"}
    scout_reasons = {"score", "score+pressure", "score+pre_momentum", "pre_momentum"}
    fake_trend = False
    promising_scout = False
    score_scout = (
        reason in scout_reasons
        and market_type in scout_targets
        and not political_override
    )
    if score_scout:
        fake_trend = (
            trend >= 0.95
            and pd <= 0.125
            and win <= 0.0015
            and pcount <= 1
        )

        promising_override = (
            market_type == "short_burst_catalyst"
            and reason in {"score", "score+pre_momentum", "pre_momentum"}
            and score >= 1.04
            and float(candidate.get("price", 1.0) or 1.0) <= 0.012
            and (
                delayed_memory
                or pd >= 0.125
                or win >= 0.0005
            )
        )

        base_promising_scout = (
            (
                reason in {"score+pressure", "score+pre_momentum", "pre_momentum"}
                and (
                    (market_type == "short_burst_catalyst" and score >= 1.35 and ((pd >= 0.16 and win >= 0.0015) or (pd >= 0.20) or (win >= 0.0030) or (trend >= 0.85 and not fake_trend))) or
                    (market_type == "valuation_ladder" and score >= 1.12 and ((pd >= 0.14 and win >= 0.0015) or (pd >= 0.18) or (win >= 0.0030) or (trend >= 0.85 and not fake_trend))) or
                    (market_type == "narrative_long_tail" and score >= 1.05 and ((pd >= 0.10 and win >= 0.0010) or (pd >= 0.14) or (win >= 0.0020) or (trend >= 0.85 and not fake_trend)))
                )
            )
            or (
                reason == "score"
                and (
                    (market_type == "short_burst_catalyst" and score >= 1.52 and (delayed_memory or pd >= 0.10 or win >= 0.0020)) or
                    (market_type == "valuation_ladder" and score >= 1.18 and (delayed_memory or pd >= 0.10 or win >= 0.0020)) or
                    (market_type == "narrative_long_tail" and score >= 1.08 and (delayed_memory or pd >= 0.08 or win >= 0.0015))
                )
            )
            or (delayed_memory and score >= (1.28 if market_type == "short_burst_catalyst" else 1.08) and not fake_trend)
        )

        promising_scout = bool(base_promising_scout or promising_override)

        scout_cap = 0.95
        if market_type == "short_burst_catalyst":
            scout_cap = 0.78 if not delayed_memory else 0.90
            if promising_scout:
                scout_cap = 0.98 if not delayed_memory else 1.08
            if promising_override:
                scout_cap = max(scout_cap, 1.02 if not delayed_memory else 1.12)
        elif market_type == "valuation_ladder":
            scout_cap = 0.82 if not delayed_memory else 0.92
            if promising_scout:
                scout_cap = 0.95 if not delayed_memory else 1.05
        elif market_type == "narrative_long_tail":
            scout_cap = 0.88 if not delayed_memory else 0.98
            if promising_scout:
                scout_cap = 1.00 if not delayed_memory else 1.10

        print(
            "TRACE | zero_peak_scout_arm | raw_stake={:.2f} | scout_cap={:.2f} | reason={} | market_type={} | delayed_memory={} | political_override={} | score={:.3f} | pd={:.3f} | trend={:.3f} | win={:.3f} | pcount={} | fake_trend={} | promising={} | override={} | {}".format(
                stake,
                scout_cap,
                reason,
                market_type,
                int(bool(delayed_memory)),
                int(bool(political_override)),
                score,
                pd,
                trend,
                win,
                pcount,
                int(bool(fake_trend)),
                int(bool(promising_scout)),
                int(bool(promising_override)),
                (candidate.get("question") or "")[:72],
            )
        )

        if stake > scout_cap:
            print(
                "TRACE | zero_peak_scout_stake | old_stake={:.2f} | new_stake={:.2f} | reason={} | market_type={} | delayed_memory={} | score={:.3f} | promising={} | override={} | {}".format(
                    stake,
                    scout_cap,
                    reason,
                    market_type,
                    int(bool(delayed_memory)),
                    score,
                    int(bool(promising_scout)),
                    int(bool(promising_override)),
                    (candidate.get("question") or "")[:72],
                )
            )
            stake = scout_cap

    if not probe_candidate:
        return round(stake, 4)

    probe_cap = 0.95 if int(opened_now or 0) == 0 else 1.10
    new_stake = min(stake, probe_cap)
    if new_stake < stake:
        print(
            "TRACE | calm_probe_cap | old_stake={:.2f} | new_stake={:.2f} | reason={} | pd={:.3f} | trend={:.3f} | win={:.3f} | pcount={} | {}".format(
                stake,
                new_stake,
                reason,
                pd,
                trend,
                win,
                pcount,
                (candidate.get("question") or "")[:72],
            )
        )
        return round(new_stake, 4)

    return round(stake, 4)



def apply_normal_entry_quality_gate(candidate, regime_name):
    if regime_name != "normal":
        return True, None

    try:
        reason = str(candidate.get("reason", candidate.get("_entry_reason", "")) or "")
        market_type = str(candidate.get("market_type", candidate.get("_market_type", "")) or "")
        score = float(candidate.get("score", 0.0) or 0.0)
        pd = float(candidate.get("pressure_density", 0.0) or 0.0)
        trend = float(candidate.get("price_trend_strength", 0.0) or 0.0)
        win = abs(float(candidate.get("price_delta_window", 0.0) or 0.0))
        pcount = int(candidate.get("pressure_count", 0) or 0)
        price = float(candidate.get("price", 1.0) or 1.0)
        delayed_memory = bool(candidate.get("delayed_entry_memory_active", False))
        political_override = bool(candidate.get("political_override_active", False))
    except Exception:
        return True, None

    if political_override:
        return True, None

    fake_trend = (
        trend >= 0.95
        and pd <= 0.125
        and win <= 0.0015
        and pcount <= 1
    )

    weak_score_only = (
        reason == "score"
        and market_type in {"short_burst_catalyst", "valuation_ladder", "narrative_long_tail"}
        and not delayed_memory
    )

    if weak_score_only:
        if market_type == "short_burst_catalyst":
            if fake_trend and score < 1.70 and price <= 0.020:
                print(
                    "TRACE | normal_entry_quality_gate | verdict=skip | signal=fake_trend_score_only | market_type={} | score={:.3f} | pd={:.3f} | trend={:.3f} | win={:.4f} | price={:.4f} | {}".format(
                        market_type, score, pd, trend, win, price, (candidate.get("question") or "")[:72]
                    )
                )
                return False, "normal_fake_trend_score_only"
            if pd < 0.10 and win < 0.0015 and score < 1.62 and price <= 0.015:
                print(
                    "TRACE | normal_entry_quality_gate | verdict=skip | signal=thin_score_only | market_type={} | score={:.3f} | pd={:.3f} | trend={:.3f} | win={:.4f} | price={:.4f} | {}".format(
                        market_type, score, pd, trend, win, price, (candidate.get("question") or "")[:72]
                    )
                )
                return False, "normal_thin_score_only"

        if market_type == "valuation_ladder":
            if fake_trend and score < 1.45:
                print(
                    "TRACE | normal_entry_quality_gate | verdict=skip | signal=fake_trend_valuation | market_type={} | score={:.3f} | pd={:.3f} | trend={:.3f} | win={:.4f} | {}".format(
                        market_type, score, pd, trend, win, (candidate.get("question") or "")[:72]
                    )
                )
                return False, "normal_fake_trend_valuation"
            if pd < 0.14 and win < 0.0020 and score < 1.30:
                print(
                    "TRACE | normal_entry_quality_gate | verdict=skip | signal=thin_valuation | market_type={} | score={:.3f} | pd={:.3f} | trend={:.3f} | win={:.4f} | {}".format(
                        market_type, score, pd, trend, win, (candidate.get("question") or "")[:72]
                    )
                )
                return False, "normal_thin_valuation"

        if market_type == "narrative_long_tail":
            if fake_trend and score < 1.18:
                print(
                    "TRACE | normal_entry_quality_gate | verdict=skip | signal=fake_trend_narrative | market_type={} | score={:.3f} | pd={:.3f} | trend={:.3f} | win={:.4f} | {}".format(
                        market_type, score, pd, trend, win, (candidate.get("question") or "")[:72]
                    )
                )
                return False, "normal_fake_trend_narrative"

    print(
        "TRACE | normal_entry_quality_gate | verdict=pass | market_type={} | reason={} | score={:.3f} | pd={:.3f} | trend={:.3f} | win={:.4f} | fake_trend={} | {}".format(
            market_type, reason, score, pd, trend, win, int(bool(fake_trend)), (candidate.get("question") or "")[:72]
        )
    )
    return True, None




def propagate_universal_reopen_lock(event, reason, now_ts, dead_reentry_cooldown, score_reentry_cooldown, family_dead_cooldown):
    try:
        pos_key = str(event.get("position_key", "") or event.get("key", "") or "")
        family_key = str(event.get("family_key", "") or "")
        market_type = str(event.get("market_type", "unknown") or "unknown")
        score = float(event.get("score", 0.0) or 0.0)
        question = (event.get("question") or "")[:72]
        if pos_key:
            dead_reentry_cooldown[pos_key] = now_ts
        if reason in {"score", "score+pressure", "score+pre_momentum", "pre_momentum", "multicycle_momentum_override", "pressure"} and pos_key:
            score_reentry_cooldown[pos_key] = now_ts
        if family_key:
            family_dead_cooldown[family_key] = now_ts
        print(
            "TRACE | universal_reopen_lock | reason={} | family={} | market_type={} | score={:.3f} | reopen_lock=1 | {}".format(
                reason,
                family_key[:72] if family_key else "unknown",
                market_type,
                score,
                question,
            )
        )
    except Exception:
        return



def propagate_failed_reentry_lock(event, reason, now_ts, dead_reentry_cooldown, score_reentry_cooldown, family_dead_cooldown):
    try:
        pos_key = str(event.get("position_key", "") or event.get("key", "") or "")
        family_key = str(event.get("family_key", "") or "")
        market_type = str(event.get("market_type", "unknown") or "unknown")
        score = float(event.get("score", 0.0) or 0.0)
        question = (event.get("question") or "")[:72]
        if pos_key:
            dead_reentry_cooldown[pos_key] = now_ts
        if reason in {"score", "score+pressure", "score+pre_momentum", "pre_momentum", "multicycle_momentum_override", "pressure"} and pos_key:
            score_reentry_cooldown[pos_key] = now_ts
        if family_key:
            family_dead_cooldown[family_key] = now_ts
        print(
            "TRACE | failed_reentry_lock | exit_reason={} | reason={} | family={} | market_type={} | score={:.3f} | reopen_lock=1 | {}".format(
                event.get("exit_reason", "unknown"),
                reason,
                family_key[:72] if family_key else "unknown",
                market_type,
                score,
                question,
            )
        )
    except Exception:
        return





def propagate_winner_reentry_lock(event, reason, now_ts, dead_reentry_cooldown, score_reentry_cooldown, family_dead_cooldown):
    try:
        pos_key = str(event.get("position_key", "") or event.get("key", "") or "")
        family_key = str(event.get("family_key", "") or "")
        market_type = str(event.get("market_type", "unknown") or "unknown")
        score = float(event.get("score", 0.0) or 0.0)
        realized_total = float(event.get("realized_pnl_usd_total_position", 0.0) or 0.0)
        partial_take_count = int(event.get("partial_take_count", 0) or 0)
        question = (event.get("question") or "")[:72]

        if partial_take_count <= 0 and realized_total <= 0.0:
            return

        if pos_key:
            dead_reentry_cooldown[pos_key] = now_ts
        if reason in {"score", "score+pressure", "score+pre_momentum", "pre_momentum", "multicycle_momentum_override", "pressure", "score+momentum", "momentum_override"} and pos_key:
            score_reentry_cooldown[pos_key] = now_ts
        if family_key:
            family_dead_cooldown[family_key] = now_ts

        print(
            "TRACE | winner_reentry_lock | exit_reason={} | reason={} | family={} | market_type={} | score={:.3f} | partials={} | realized_total={:.4f} | reopen_lock=1 | {}".format(
                str(event.get("exit_reason", "") or "unknown"),
                reason,
                family_key[:72] if family_key else "unknown",
                market_type,
                score,
                partial_take_count,
                realized_total,
                question,
            )
        )
    except Exception:
        return


def should_block_winner_reentry(candidate, reason, market_exit_memory):
    key = build_market_key(candidate)
    memory = dict(market_exit_memory.get(key, {}) or {})
    winner_exit_count = int(memory.get("winner_exit_count", 0) or 0)
    last_exit_reason = str(memory.get("last_exit_reason", "") or "")
    if winner_exit_count <= 0:
        return False, None

    try:
        score = float(candidate.get("score", 0.0) or 0.0)
        density = float(candidate.get("pressure_density", 0.0) or 0.0)
        pressure_count = int(candidate.get("pressure_count", 0) or 0)
        trend = float(candidate.get("price_trend_strength", 0.0) or 0.0)
        window_delta = abs(float(candidate.get("price_delta_window", 0.0) or 0.0))
        delta_1 = abs(float(candidate.get("price_delta", 0.0) or 0.0))
        market_type = str(candidate.get("market_type", "general_binary") or "general_binary")
    except Exception:
        score = 0.0
        density = 0.0
        pressure_count = 0
        trend = 0.0
        window_delta = 0.0
        delta_1 = 0.0
        market_type = "general_binary"

    elite_override = elite_recovery_override_state(candidate, reason, market_exit_memory)
    votes = reentry_signal_votes(candidate)
    if elite_override.get("active", False):
        return False, None
    sports_runner_block = (
        market_type == "sports_award_longshot"
        and votes < 5
        and (score < 1.08 or window_delta < 0.010 or density < 0.22)
    )
    general_runner_block = (
        market_type != "sports_award_longshot"
        and votes < 4
        and score < 1.10
        and density < 0.22
        and trend < 0.92
        and window_delta < 0.010
    )

    if sports_runner_block or general_runner_block:
        return True, "winner_reentry_discipline"

    thin_winner_reentry = (
        last_exit_reason in {"micro_profit_lock", "runner_protection_lock_exit", "profit_lock_decay_exit"}
        and market_type in {"short_burst_catalyst", "speculative_hype", "legal_resolution", "valuation_ladder"}
        and pressure_count <= 1
        and density < 0.18
        and trend < 0.96
        and window_delta < 0.0045
        and delta_1 < 0.0045
        and votes < 5
    )
    if thin_winner_reentry:
        return True, "winner_carry_reentry_brake"

    if last_exit_reason in {"runner_protection_lock_exit", "profit_lock_decay_exit", "micro_profit_lock"} and votes < 5:
        return True, "winner_reentry_memory"

    return False, None


def prime_recovery_router_context(candidate, delayed_entry_memory, delayed_entry_watch):
    try:
        key = build_market_key(candidate)
        family_key = candidate.get("family_key") or detect_market_family(candidate)
        market_key = "market::{}".format(key)
        family_memory_key = "family::{}".format(family_key) if family_key else ""
        candidate["_delayed_entry_memory_active"] = bool(
            market_key in (delayed_entry_memory or {})
            or (family_memory_key and family_memory_key in (delayed_entry_memory or {}))
        )
        candidate["_delayed_entry_watch_active"] = key in (delayed_entry_watch or {})
        return candidate
    except Exception:
        return candidate

def execution_micro_clamp_cap(candidate, clamp_kind="generic"):
    try:
        market_type = str(candidate.get("market_type", "general_binary") or "general_binary")
        score = float(candidate.get("score", 0.0) or 0.0)
        density = float(candidate.get("pressure_density", 0.0) or 0.0)
        pressure_count = int(candidate.get("pressure_count", 0) or 0)
        trend = float(candidate.get("price_trend_strength", 0.0) or 0.0)
        window_delta = abs(float(candidate.get("price_delta_window", 0.0) or 0.0))
        signal = str(candidate.get("_elite_recovery_signal") or candidate.get("_thin_pressure_truth_signal") or candidate.get("_dead_money_compression_signal") or "")
    except Exception:
        return 0.86

    base_caps = {
        "sports_award_longshot": 0.72,
        "speculative_hype": 0.78,
        "narrative_long_tail": 0.82,
        "valuation_ladder": 0.88,
        "general_binary": 0.86,
        "short_burst_catalyst": 0.94,
        "scheduled_binary_event": 0.98,
        "legal_resolution": 1.04,
    }
    cap = float(base_caps.get(market_type, 0.88))

    if clamp_kind == "elite_recovery":
        if market_type in {"short_burst_catalyst", "legal_resolution"}:
            cap += 0.08
        elif market_type in {"speculative_hype", "valuation_ladder"}:
            cap += 0.03
        if signal in {"elite_recovery_delayed_memory", "elite_recovery_watch_revival", "elite_recovery_delayed_confirmed"}:
            cap -= 0.10
    elif clamp_kind in {"follow_through_force_delayed", "thin_truth_force_delayed"}:
        cap -= 0.04
        if market_type in {"speculative_hype", "sports_award_longshot", "general_binary", "narrative_long_tail"}:
            cap -= 0.04
    elif clamp_kind == "relief_escalation":
        cap -= 0.06
        if market_type in {"valuation_ladder", "general_binary", "narrative_long_tail", "speculative_hype"}:
            cap -= 0.02
        if market_type in {"legal_resolution", "scheduled_binary_event"}:
            cap += 0.02

    if pressure_count >= 2 or density >= 0.24 or window_delta >= 0.0060 or score >= 1.40:
        cap += 0.08
    elif pressure_count >= 1 or density >= 0.18 or trend >= 0.92 or window_delta >= 0.0035:
        cap += 0.04

    if pressure_count <= 1 and density <= 0.12 and trend < 0.82 and window_delta <= 0.0025:
        cap -= 0.05

    return round(min(max(cap, 0.62), 1.18), 4)


def elite_recovery_clamp_state(candidate):
    try:
        active = bool(candidate.get("_elite_recovery_override", False))
        if not active:
            return {"active": False, "cap": 0.0, "force_delayed": False, "micro_scout": False, "signal": None}

        signal = str(candidate.get("_elite_recovery_signal") or "elite_recovery_override")
        density = float(candidate.get("pressure_density", 0.0) or 0.0)
        pressure_count = int(candidate.get("pressure_count", 0) or 0)
        trend = float(candidate.get("price_trend_strength", 0.0) or 0.0)
        window_delta = abs(float(candidate.get("price_delta_window", 0.0) or 0.0))
        structure_votes = int(follow_through_structure_votes(candidate) or 0)
        cap = execution_micro_clamp_cap(candidate, "elite_recovery")
        force_delayed = bool(
            signal in {"elite_recovery_delayed_memory", "elite_recovery_watch_revival"}
            and not (pressure_count >= 2 or density >= 0.28 or structure_votes >= 5 or window_delta >= 0.0065)
        )
        micro_scout = bool(cap <= 1.00 or force_delayed or structure_votes <= 3)
        if force_delayed:
            cap = min(cap, 0.94)
        if trend < 0.84 and density < 0.18 and pressure_count <= 1:
            cap = min(cap, 0.90)
        return {
            "active": True,
            "cap": round(min(max(cap, 0.62), 1.18), 4),
            "force_delayed": force_delayed,
            "micro_scout": micro_scout,
            "signal": signal,
        }
    except Exception:
        return {"active": False, "cap": 0.0, "force_delayed": False, "micro_scout": False, "signal": None}


def observable_recovery_router_state(candidate, reason, market_exit_memory):
    try:
        key = build_market_key(candidate)
        memory = dict((market_exit_memory or {}).get(key, {}) or {})
        family_memory_key = build_family_memory_key(candidate)
        family_memory = dict((market_exit_memory or {}).get(family_memory_key, {}) or {}) if family_memory_key else {}

        score = float(candidate.get("score", 0.0) or 0.0)
        density = float(candidate.get("pressure_density", 0.0) or 0.0)
        pressure_count = int(candidate.get("pressure_count", 0) or 0)
        trend = float(candidate.get("price_trend_strength", 0.0) or 0.0)
        window_delta = abs(float(candidate.get("price_delta_window", 0.0) or 0.0))
        delta = abs(float(candidate.get("price_delta", 0.0) or 0.0))
        votes = int(reentry_signal_votes(candidate) or 0)
        structure_votes = int(follow_through_structure_votes(candidate) or 0)
        delayed_confirmed = bool(candidate.get("_delayed_entry_confirmed", False))
        delayed_memory = bool(candidate.get("_delayed_entry_memory_active", False) or candidate.get("delayed_entry_memory_active", False))
        delayed_watch = bool(candidate.get("_delayed_entry_watch_active", False))
        market_type = str(candidate.get("market_type", "general_binary") or "general_binary")

        weak_failed = int(memory.get("weak_failed_count", 0) or 0)
        dead_hits = int(memory.get("dead_exit_count", 0) or 0)
        stale_hits = int(memory.get("stale_exit_count", 0) or 0)
        legal_replay_exit_count = int(memory.get("legal_replay_exit_count", 0) or 0)
        legal_stale_loss_count = int(memory.get("legal_stale_loss_count", 0) or 0)
        failed_runner_quarantine_count = int(memory.get("failed_runner_quarantine_count", 0) or 0)
        family_dead = int(family_memory.get("dead_money_exit_count", 0) or 0)
        family_follow = int(family_memory.get("follow_through_fail_count", 0) or 0)
        family_zero = int(family_memory.get("zero_peak_family_count", 0) or 0)
        family_brake = int(family_memory.get("family_reopen_brake_count", 0) or 0)
        family_failed_runner = int(family_memory.get("failed_runner_quarantine_count", 0) or 0)
        zero_churn_hits = int(memory.get("zero_churn_exit_count", 0) or 0)
        family_zero_churn_hits = int(family_memory.get("zero_churn_exit_count", 0) or 0)
        zero_churn_brake = int(memory.get("zero_churn_reopen_brake_count", 0) or 0)
        family_zero_churn_brake = int(family_memory.get("zero_churn_reopen_brake_count", 0) or 0)
        last_failed_score = float(memory.get("last_failed_score", family_memory.get("last_failed_score", 0.0)) or 0.0)
        last_failed_votes = int(memory.get("last_failed_structure_votes", family_memory.get("last_failed_structure_votes", 0)) or 0)

        memory_pressure = (weak_failed + dead_hits + stale_hits + legal_replay_exit_count + legal_stale_loss_count
            + min(failed_runner_quarantine_count, 2) + min(family_dead, 2) + min(family_follow, 2)
            + min(family_zero, 2) + min(family_brake, 2) + min(family_failed_runner, 2)
            + min(zero_churn_hits, 2) + min(family_zero_churn_hits, 2) + min(zero_churn_brake, 2) + min(family_zero_churn_brake, 2))
        considered = bool(memory_pressure > 0 or delayed_memory or delayed_confirmed or delayed_watch)
        if not considered:
            return {"considered": False, "active": False}

        elite_state = elite_recovery_override_state(candidate, reason, market_exit_memory)
        if elite_state.get("active", False):
            route = dict(elite_state)
            route["considered"] = True
            route["route"] = "activate"
            route["reject_reason"] = None
            return route

        score_floor = max(1.04, last_failed_score + (0.04 if (delayed_memory or delayed_watch) else 0.06))
        required_votes = max(3, last_failed_votes + (1 if memory_pressure >= 4 else 0))
        strong_structure = bool(pressure_count >= 2 or density >= 0.18 or (trend >= 0.90 and (window_delta >= 0.0025 or delta >= 0.0025))
            or window_delta >= 0.0045 or delta >= 0.0045 or structure_votes >= max(4, required_votes) or votes >= max(5, required_votes + 1))
        stronger_than_last = bool(score >= max(score_floor, last_failed_score + 0.05) and (structure_votes >= required_votes or votes >= required_votes + 1))

        reject_reason = "activation_conditions_not_met"
        if score < score_floor:
            reject_reason = "score_too_low"
        elif memory_pressure >= 7 and not strong_structure:
            reject_reason = "memory_too_toxic"
        elif (failed_runner_quarantine_count >= 1 or family_failed_runner >= 1) and not (strong_structure and score >= max(1.14, last_failed_score + 0.06)):
            reject_reason = "failed_runner_recovery_missing"
        elif (delayed_memory or delayed_watch) and not (delayed_confirmed or strong_structure or score >= score_floor + 0.03):
            reject_reason = "delayed_confirmation_missing"
        elif not stronger_than_last:
            reject_reason = "family_recovery_not_stronger"
        elif structure_votes < required_votes and votes < required_votes + 1:
            reject_reason = "structure_too_weak"

        return {
            "considered": True, "active": False, "route": "reject", "reject_reason": reject_reason,
            "score": score, "density": density, "pressure_count": pressure_count, "trend": trend,
            "window_delta": window_delta, "delta": delta, "votes": votes, "structure_votes": structure_votes,
            "memory_pressure": memory_pressure, "market_type": market_type,
            "last_failed_score": last_failed_score, "last_failed_votes": last_failed_votes,
        }
    except Exception:
        return {"considered": False, "active": False}


def should_block_failed_runner_quarantine(candidate, reason, market_exit_memory):
    try:
        market_type = str(candidate.get("market_type", "general_binary") or "general_binary")
        if market_type not in {"short_burst_catalyst", "speculative_hype", "valuation_ladder", "legal_resolution"}:
            return False, None

        key = build_market_key(candidate)
        memory = dict((market_exit_memory or {}).get(key, {}) or {})
        family_memory_key = build_family_memory_key(candidate)
        family_memory = dict((market_exit_memory or {}).get(family_memory_key, {}) or {}) if family_memory_key else {}

        runner_hits = int(memory.get("failed_runner_quarantine_count", 0) or 0)
        family_runner_hits = int(family_memory.get("failed_runner_quarantine_count", 0) or 0)
        if runner_hits <= 0 and family_runner_hits <= 0:
            return False, None

        score = float(candidate.get("score", 0.0) or 0.0)
        density = float(candidate.get("pressure_density", 0.0) or 0.0)
        pressure_count = int(candidate.get("pressure_count", 0) or 0)
        trend = float(candidate.get("price_trend_strength", 0.0) or 0.0)
        window_delta = abs(float(candidate.get("price_delta_window", 0.0) or 0.0))
        delta_1 = abs(float(candidate.get("price_delta", 0.0) or 0.0))
        votes = int(reentry_signal_votes(candidate) or 0)
        structure_votes = int(follow_through_structure_votes(candidate) or 0)
        delayed_confirmed = bool(candidate.get("_delayed_entry_confirmed", False))
        delayed_memory = bool(candidate.get("delayed_entry_memory_active", False) or candidate.get("_delayed_entry_memory_active", False))
        last_failed_score = float(memory.get("last_failed_score", family_memory.get("last_failed_score", 0.0)) or 0.0)
        last_failed_votes = int(memory.get("last_failed_structure_votes", family_memory.get("last_failed_structure_votes", 0)) or 0)

        elite_override = elite_recovery_override_state(candidate, reason, market_exit_memory)
        strong_recovery = bool(
            elite_override.get("active", False)
            or delayed_confirmed
            or (
                score >= max(1.24 if market_type == "short_burst_catalyst" else 1.16, last_failed_score + 0.08)
                and (
                    pressure_count >= 2
                    or density >= 0.20
                    or (trend >= 0.92 and (window_delta >= 0.0030 or delta_1 >= 0.0030))
                    or window_delta >= 0.0060
                )
                and structure_votes >= max(4, last_failed_votes + 1)
            )
        )

        weak_runner_retry = (
            reason in {"score", "pressure", "score+pressure", "pre_momentum", "score+pre_momentum", "multicycle_momentum_override", "score+momentum", "momentum_override"}
            and pressure_count <= 1
            and density < 0.18
            and trend < 0.92
            and window_delta < 0.0045
            and delta_1 < 0.0045
            and votes < 5
        )
        same_or_weaker = (
            structure_votes <= max(3, last_failed_votes)
            and score <= max(1.14 if market_type == "short_burst_catalyst" else 1.08, last_failed_score + 0.06)
        )

        if not strong_recovery and weak_runner_retry and same_or_weaker:
            return True, "failed_runner_quarantine"

        if not strong_recovery and (runner_hits >= 2 or family_runner_hits >= 2) and score < max(1.30 if market_type == "short_burst_catalyst" else 1.18, last_failed_score + 0.10):
            return True, "failed_runner_quarantine"

        if not strong_recovery and delayed_memory and votes < 5 and structure_votes < max(4, last_failed_votes + 1):
            return True, "failed_runner_quarantine"

        return False, None
    except Exception:
        return False, None


def propagate_legal_replay_quarantine(event, reason, now_ts, dead_reentry_cooldown, score_reentry_cooldown, family_dead_cooldown):
    try:
        market_type = str(event.get("market_type", "unknown") or "unknown")
        if market_type != "legal_resolution":
            return

        exit_reason = str(event.get("exit_reason", "") or "")
        realized_total = float(event.get("realized_pnl_usd_total_position", 0.0) or 0.0)
        quarantine_reasons = {
            "cluster_conflict_rotation_exit",
            "family_rotation_exit",
            "competitive_rotation_exit",
            "capital_rotation_exit",
            "time_stale_exit",
            "time_decay_exit",
            "pressure_decay_exit",
            "no_follow_through_exit",
            "dead_capital_decay",
            "idle_hard_exit",
            "opportunity_cost_decay",
            "early_hard_stop_compression_exit",
            "hard_stop_loss",
        }
        if exit_reason not in quarantine_reasons or realized_total >= -0.0001:
            return

        pos_key = str(event.get("position_key", "") or event.get("key", "") or "")
        family_key = str(event.get("family_key", "") or "")
        score = float(event.get("score", 0.0) or 0.0)
        question = (event.get("question") or "")[:72]

        if pos_key:
            dead_reentry_cooldown[pos_key] = now_ts
            score_reentry_cooldown[pos_key] = now_ts
        if family_key:
            family_dead_cooldown[family_key] = now_ts

        print(
            "TRACE | legal_replay_quarantine | exit_reason={} | reason={} | family={} | market_type={} | score={:.3f} | realized_total={:.4f} | quarantine=1 | {}".format(
                exit_reason,
                reason,
                family_key[:72] if family_key else "unknown",
                market_type,
                score,
                realized_total,
                question,
            )
        )
    except Exception:
        return


def should_block_legal_replay(candidate, reason, market_exit_memory, family_dead_cooldown, now_ts):
    try:
        market_type = str(candidate.get("market_type", "general_binary") or "general_binary")
        if market_type != "legal_resolution":
            return False, None

        key = build_market_key(candidate)
        memory = dict(market_exit_memory.get(key, {}) or {})
        legal_replay_exit_count = int(memory.get("legal_replay_exit_count", 0) or 0)
        legal_stale_loss_count = int(memory.get("legal_stale_loss_count", 0) or 0)
        last_loss_exit_reason = str(memory.get("legal_last_loss_exit_reason", "") or "")
        last_loss_realized = float(memory.get("legal_last_loss_realized", 0.0) or 0.0)
        if legal_replay_exit_count <= 0 and legal_stale_loss_count <= 0 and last_loss_realized >= -0.0001:
            return False, None

        score = float(candidate.get("score", 0.0) or 0.0)
        density = float(candidate.get("pressure_density", 0.0) or 0.0)
        trend = float(candidate.get("price_trend_strength", 0.0) or 0.0)
        window_delta = abs(float(candidate.get("price_delta_window", 0.0) or 0.0))
        votes = reentry_signal_votes(candidate)
        family_key = str(candidate.get("family_key", "") or "")
        family_locked = False
        if family_key:
            family_ts = family_dead_cooldown.get(family_key)
            if family_ts:
                try:
                    family_locked = (now_ts - family_ts).total_seconds() < 7200
                except Exception:
                    family_locked = False

        elite_override = elite_recovery_override_state(candidate, reason, market_exit_memory)
        elite_recovery = bool(
            elite_override.get("active", False)
            or votes >= 6
            or (score >= 1.44 and (density >= 0.20 or trend >= 0.95 or window_delta >= 0.012))
        )

        stale_loss_reason = last_loss_exit_reason in {
            "time_stale_exit",
            "time_decay_exit",
            "pressure_decay_exit",
            "no_follow_through_exit",
            "idle_hard_exit",
            "opportunity_cost_decay",
            "early_hard_stop_compression_exit",
            "hard_stop_loss",
        }

        if legal_stale_loss_count >= 1 and stale_loss_reason and not elite_recovery:
            return True, "legal_stale_loss_reentry_kill"

        if family_locked and legal_replay_exit_count >= 1 and not elite_recovery:
            return True, "legal_replay_quarantine"

        if legal_replay_exit_count >= 2 and votes < 6:
            return True, "legal_replay_memory"

        return False, None
    except Exception:
        return False, None




def should_block_legal_false_pressure_quarantine(candidate, reason, market_exit_memory):
    try:
        market_type = str(candidate.get("market_type", "general_binary") or "general_binary")
        if market_type != "legal_resolution":
            return False, None
        if reason not in {"score+pressure", "pressure", "score+pre_momentum", "pre_momentum", "score"}:
            return False, None

        key = build_market_key(candidate)
        memory = dict((market_exit_memory or {}).get(key, {}) or {})
        family_memory_key = build_family_memory_key(candidate)
        family_memory = dict((market_exit_memory or {}).get(family_memory_key, {}) or {}) if family_memory_key else {}
        legal_false_hits = int(memory.get("legal_false_pressure_quarantine_count", 0) or 0)
        family_false_hits = int(family_memory.get("legal_false_pressure_quarantine_count", 0) or 0)
        legal_replay_exit_count = int(memory.get("legal_replay_exit_count", 0) or 0)
        legal_stale_loss_count = int(memory.get("legal_stale_loss_count", 0) or 0)
        runner_hits = int(memory.get("failed_runner_quarantine_count", 0) or 0)
        family_runner_hits = int(family_memory.get("failed_runner_quarantine_count", 0) or 0)
        if legal_false_hits <= 0 and family_false_hits <= 0 and legal_replay_exit_count <= 0 and legal_stale_loss_count <= 0 and runner_hits <= 0 and family_runner_hits <= 0:
            return False, None

        score = float(candidate.get("score", 0.0) or 0.0)
        density = float(candidate.get("pressure_density", 0.0) or 0.0)
        pressure_count = int(candidate.get("pressure_count", 0) or 0)
        trend = float(candidate.get("price_trend_strength", 0.0) or 0.0)
        raw_window_delta = float(candidate.get("price_delta_window", 0.0) or 0.0)
        raw_delta = float(candidate.get("price_delta", 0.0) or 0.0)
        window_delta = abs(raw_window_delta)
        delta_1 = abs(raw_delta)
        structure_votes = int(follow_through_structure_votes(candidate) or 0)
        delayed_confirmed = bool(candidate.get("_delayed_entry_confirmed", False))
        delayed_memory = bool(candidate.get("_delayed_entry_memory_active", False) or candidate.get("delayed_entry_memory_active", False))
        last_failed_score = float(memory.get("last_failed_score", family_memory.get("last_failed_score", 0.0)) or 0.0)
        last_failed_votes = int(memory.get("last_failed_structure_votes", family_memory.get("last_failed_structure_votes", 0)) or 0)

        elite_override = elite_recovery_override_state(candidate, reason, market_exit_memory)
        strong_recovery = bool(
            elite_override.get("active", False)
            or delayed_confirmed
            or (
                score >= max(1.42, last_failed_score + 0.12)
                and pressure_count >= 3
                and density >= 0.38
                and raw_window_delta > 0.0
                and structure_votes >= max(4, last_failed_votes + 1)
            )
        )
        negative_or_stale_pressure = bool(
            raw_window_delta <= 0.0
            or (pressure_count <= 2 and density <= 0.36 and trend <= 0.82)
            or (trend <= 0.75 and delta_1 <= 0.0030)
            or (window_delta <= 0.010 and raw_delta <= 0.0)
        )
        same_or_weaker = bool(structure_votes <= max(4, last_failed_votes) or score <= max(1.34, last_failed_score + 0.08))
        pressure_replay_pressure = legal_false_hits + family_false_hits + legal_replay_exit_count + legal_stale_loss_count + runner_hits + family_runner_hits

        if not strong_recovery and negative_or_stale_pressure and same_or_weaker and pressure_replay_pressure >= 1:
            return True, "legal_false_pressure_quarantine"
        if not strong_recovery and delayed_memory and pressure_replay_pressure >= 2 and score < max(1.50, last_failed_score + 0.12):
            return True, "legal_false_pressure_family_brake"
        return False, None
    except Exception:
        return False, None






def should_block_weak_legal_override(candidate, reason, market_exit_memory):
    try:
        market_type = str(candidate.get("market_type", "general_binary") or "general_binary")
        if market_type != "legal_resolution":
            return False, None
        if reason not in {"multicycle_momentum_override", "momentum_override", "score+momentum", "score+pre_momentum", "pre_momentum"}:
            return False, None
        score = float(candidate.get("score", 0.0) or 0.0)
        density = float(candidate.get("pressure_density", 0.0) or 0.0)
        pressure_count = int(candidate.get("pressure_count", 0) or 0)
        trend = float(candidate.get("price_trend_strength", 0.0) or 0.0)
        window_delta = abs(float(candidate.get("price_delta_window", 0.0) or 0.0))
        delta_1 = abs(float(candidate.get("price_delta", 0.0) or 0.0))
        cooldown_active = bool(candidate.get("cooldown_active", False))
        score_reentry_cd = bool(candidate.get("score_reentry_cooldown_active", False))
        stale_reentry_cd = bool(candidate.get("stale_reentry_cooldown_active", False))
        delayed_memory = bool(candidate.get("delayed_entry_memory_active", False))
        key = build_market_key(candidate)
        memory = dict(market_exit_memory.get(key, {}) or {})
        stale_hits = int(memory.get("stale_exit_count", 0) or 0)
        weak_failed = int(memory.get("weak_failed_count", 0) or 0)
        legal_replay_exit_count = int(memory.get("legal_replay_exit_count", 0) or 0)
        legal_stale_loss_count = int(memory.get("legal_stale_loss_count", 0) or 0)

        strong_override_recovery = (
            score >= 0.96
            and (
                density >= 0.22
                or pressure_count >= 2
                or (trend >= 1.02 and window_delta >= 0.010)
                or delta_1 >= 0.010
            )
        )

        ultra_weak_authority = (
            score < 0.56
            and density <= 0.16
            and pressure_count <= 1
            and window_delta <= 0.018
            and delta_1 <= 0.018
        )

        fragile_override = (
            reason in {"multicycle_momentum_override", "momentum_override", "score+momentum"}
            and score < 0.72
            and density <= 0.18
            and pressure_count <= 1
            and window_delta <= 0.020
            and delta_1 <= 0.020
            and trend < 1.05
        )

        authority_memory_veto = (
            (
                cooldown_active
                or score_reentry_cd
                or stale_reentry_cd
                or delayed_memory
                or stale_hits >= 1
                or weak_failed >= 1
                or legal_replay_exit_count >= 1
                or legal_stale_loss_count >= 1
            )
            and peakless_legal_profile(candidate)
            and score < 0.84
            and density <= 0.20
            and pressure_count <= 1
            and window_delta <= 0.020
            and delta_1 <= 0.020
        )

        if (ultra_weak_authority or fragile_override) and not strong_override_recovery:
            return True, "weak_legal_override_authority_gate"
        if authority_memory_veto and not strong_override_recovery:
            return True, "weak_legal_override_memory_veto"
        return False, None
    except Exception:
        return False, None



def should_block_sports_longshot_churn(candidate, reason, current_regime, market_exit_memory):
    try:
        market_type = str(candidate.get("market_type", "general_binary") or "general_binary")
        if market_type != "sports_award_longshot":
            return False, None

        supported_reasons = {
            "score",
            "pressure",
            "score+pressure",
            "pre_momentum",
            "score+pre_momentum",
            "multicycle_momentum_override",
            "momentum_override",
            "score+momentum",
        }
        if reason not in supported_reasons:
            return False, None

        score = float(candidate.get("score", 0.0) or 0.0)
        density = float(candidate.get("pressure_density", 0.0) or 0.0)
        pressure_count = int(candidate.get("pressure_count", 0) or 0)
        trend = float(candidate.get("price_trend_strength", 0.0) or 0.0)
        window_delta = abs(float(candidate.get("price_delta_window", 0.0) or 0.0))
        delta_1 = abs(float(candidate.get("price_delta", 0.0) or 0.0))
        cooldown_active = bool(candidate.get("cooldown_active", False))
        stale_reentry_cd = bool(candidate.get("stale_reentry_cooldown_active", False))
        delayed_memory = bool(candidate.get("delayed_entry_memory_active", False) or candidate.get("_delayed_entry_memory_active", False))
        delayed_watch = bool(candidate.get("delayed_entry_watch_active", False) or candidate.get("_delayed_entry_watch_active", False))
        delayed_confirmed = bool(candidate.get("_delayed_entry_confirmed", False))

        key = build_market_key(candidate)
        memory = dict((market_exit_memory or {}).get(key, {}) or {})
        family_memory_key = build_family_memory_key(candidate)
        family_memory = dict((market_exit_memory or {}).get(family_memory_key, {}) or {}) if family_memory_key else {}

        stale_hits = int(memory.get("stale_exit_count", 0) or 0)
        weak_failed = int(memory.get("weak_failed_count", 0) or 0)
        dead_hits = int(memory.get("dead_exit_count", 0) or 0)
        sports_churn_count = int(memory.get("sports_longshot_churn_count", 0) or 0)
        sports_fire_count = int(memory.get("sports_zero_peak_fire_count", 0) or 0)
        sports_zombie_kill_count = int(memory.get("sports_zombie_kill_count", 0) or 0)

        family_dead = int(family_memory.get("dead_money_exit_count", 0) or 0)
        family_zero = int(family_memory.get("zero_peak_family_count", 0) or 0)
        family_follow = int(family_memory.get("follow_through_fail_count", 0) or 0)
        family_brake = int(family_memory.get("family_reopen_brake_count", 0) or 0)
        family_zombie = int(family_memory.get("sports_zombie_kill_count", 0) or 0)

        winner_safe_profile = (
            score >= 0.92
            and density >= 0.30
            and pressure_count >= 3
            and trend >= 0.98
            and window_delta >= 0.0012
        )
        elite_recovery = (
            delayed_confirmed
            or (
                score >= 1.16
                and (
                    (density >= 0.26 and pressure_count >= 2)
                    or (trend >= 0.98 and window_delta >= 0.006)
                    or window_delta >= 0.010
                    or delta_1 >= 0.008
                )
            )
        )
        if winner_safe_profile or elite_recovery:
            return False, None

        flat_zombie_score = (
            reason == "score"
            and score < (1.04 if current_regime == "normal" else 0.99)
            and density <= 0.02
            and pressure_count == 0
            and trend <= 0.10
            and window_delta <= 0.0005
            and delta_1 <= 0.0005
        )

        weak_score_probe = (
            reason in {"score", "pre_momentum", "score+pre_momentum"}
            and score < 0.99
            and density <= 0.14
            and pressure_count <= 1
            and trend < 0.92
            and window_delta <= 0.0045
            and delta_1 <= 0.0045
        )
        weak_pressure_probe = (
            reason in {"pressure", "score+pressure", "score+momentum"}
            and (
                (
                    score < 0.88
                    and density <= 0.28
                    and pressure_count <= 2
                    and trend < 0.90
                    and window_delta <= 0.0055
                    and delta_1 <= 0.0055
                )
                or (
                    density >= 0.45
                    and pressure_count >= 3
                    and trend < 0.82
                    and window_delta <= 0.0025
                    and delta_1 <= 0.0035
                    and score < 0.98
                )
            )
        )
        weak_override_probe = (
            reason in {"multicycle_momentum_override", "momentum_override"}
            and score < 0.78
            and density <= 0.22
            and pressure_count <= 1
            and trend < 0.92
            and window_delta <= 0.0065
            and delta_1 <= 0.0065
        )
        loser_target_probe = (
            reason in supported_reasons
            and score < 0.92
            and (density < 0.22 or pressure_count < 2 or trend < 0.98 or window_delta < 0.0012)
            and delta_1 <= 0.0040
        )

        zombie_memory_pressure = (
            stale_hits
            + weak_failed
            + dead_hits
            + sports_churn_count
            + sports_fire_count
            + sports_zombie_kill_count
            + min(family_dead, 2)
            + min(family_zero, 2)
            + min(family_follow, 2)
            + min(family_brake, 2)
            + min(family_zombie, 2)
        )
        poisoned_reentry_probe = (
            zombie_memory_pressure >= 1
            and score < 1.08
            and density <= 0.24
            and pressure_count <= 1
            and trend < 0.98
            and window_delta <= 0.008
            and delta_1 <= 0.008
        )
        delayed_zombie_probe = (
            (delayed_memory or delayed_watch or stale_reentry_cd or cooldown_active)
            and score < 1.06
            and density <= 0.22
            and pressure_count <= 1
            and trend < 0.96
            and window_delta <= 0.007
            and delta_1 <= 0.007
        )

        if flat_zombie_score or weak_pressure_probe:
            return True, "sports_zombie_guillotine"
        if weak_score_probe or weak_override_probe or loser_target_probe:
            return True, "sports_longshot_churn_kill"
        if poisoned_reentry_probe or delayed_zombie_probe:
            return True, "sports_zombie_memory_brake"
        return False, None
    except Exception:
        return False, None

def peakless_legal_profile(candidate):
    try:
        market_type = str(candidate.get("market_type", "general_binary") or "general_binary")
        if market_type != "legal_resolution":
            return False
        density = float(candidate.get("pressure_density", 0.0) or 0.0)
        pressure_count = int(candidate.get("pressure_count", 0) or 0)
        trend = float(candidate.get("price_trend_strength", 0.0) or 0.0)
        window_delta = abs(float(candidate.get("price_delta_window", 0.0) or 0.0))
        delta_1 = abs(float(candidate.get("price_delta", 0.0) or 0.0))
        score = float(candidate.get("score", 0.0) or 0.0)
        return (
            score < 1.86
            and density <= 0.36
            and pressure_count <= 2
            and trend <= 1.02
            and window_delta <= 0.018
            and delta_1 <= 0.018
        )
    except Exception:
        return False


def should_block_legal_pressure_admission(candidate, reason, market_exit_memory):
    try:
        market_type = str(candidate.get("market_type", "general_binary") or "general_binary")
        if market_type != "legal_resolution":
            return False, None

        if reason not in {"score+pressure", "pressure", "score+pre_momentum", "pre_momentum", "score"}:
            return False, None

        score = float(candidate.get("score", 0.0) or 0.0)
        density = float(candidate.get("pressure_density", 0.0) or 0.0)
        pressure_count = int(candidate.get("pressure_count", 0) or 0)
        trend = float(candidate.get("price_trend_strength", 0.0) or 0.0)
        window_delta = abs(float(candidate.get("price_delta_window", 0.0) or 0.0))
        delta_1 = abs(float(candidate.get("price_delta", 0.0) or 0.0))
        score_cd = bool(candidate.get("score_reentry_cooldown_active", False))
        stale_cd = bool(candidate.get("stale_reentry_cooldown_active", False))
        cool_cd = bool(candidate.get("cooldown_active", False))

        key = build_market_key(candidate)
        memory = dict(market_exit_memory.get(key, {}) or {})
        stale_hits = int(memory.get("stale_exit_count", 0) or 0)
        weak_failed = int(memory.get("weak_failed_count", 0) or 0)
        legal_replay_exit_count = int(memory.get("legal_replay_exit_count", 0) or 0)
        legal_stale_loss_count = int(memory.get("legal_stale_loss_count", 0) or 0)
        last_exit_reason = str(memory.get("last_exit_reason", "") or "")
        last_loss_reason = str(memory.get("legal_last_loss_exit_reason", "") or "")
        last_loss_realized = float(memory.get("legal_last_loss_realized", 0.0) or 0.0)

        votes = reentry_signal_votes(candidate)
        elite_override = elite_recovery_override_state(candidate, reason, market_exit_memory)
        elite_recovery = bool(
            elite_override.get("active", False)
            or votes >= 8
            or (
                score >= 1.82
                and (
                    density >= 0.42
                    or pressure_count >= 3
                    or trend >= 1.00
                    or window_delta >= 0.024
                    or delta_1 >= 0.020
                )
            )
        )

        legal_loss_reasons = {
            "time_stale_exit",
            "time_decay_exit",
            "pressure_decay_exit",
            "no_follow_through_exit",
            "dead_capital_decay",
            "idle_hard_exit",
            "opportunity_cost_decay",
            "early_hard_stop_compression_exit",
            "hard_stop_loss",
            "cluster_conflict_rotation_exit",
            "family_rotation_exit",
            "competitive_rotation_exit",
            "capital_rotation_exit",
        }

        pressure_like = reason in {"score+pressure", "pressure"}
        momentum_like = reason in {"score+pre_momentum", "pre_momentum", "score"}

        cooldown_authority_veto = (
            (score_cd or stale_cd or cool_cd)
            and not elite_recovery
            and (
                (
                    pressure_like
                    and score < 1.74
                    and density <= 0.46
                    and pressure_count <= 3
                    and window_delta <= 0.028
                    and delta_1 <= 0.022
                )
                or (
                    momentum_like
                    and score < 1.62
                    and density <= 0.34
                    and pressure_count <= 2
                    and window_delta <= 0.018
                    and delta_1 <= 0.016
                )
            )
        )

        memory_authority_veto = (
            (
                legal_stale_loss_count >= 1
                or legal_replay_exit_count >= 1
                or stale_hits >= 2
                or weak_failed >= 3
                or (last_exit_reason in legal_loss_reasons)
                or (last_loss_reason in legal_loss_reasons and last_loss_realized < -0.02)
            )
            and not elite_recovery
            and (
                (
                    pressure_like
                    and score < 1.78
                    and density <= 0.48
                    and pressure_count <= 3
                    and window_delta <= 0.028
                )
                or (
                    momentum_like
                    and score < 1.66
                    and density <= 0.36
                    and pressure_count <= 2
                    and window_delta <= 0.020
                )
            )
        )

        legal_zero_peak_history_veto = (
            (legal_stale_loss_count >= 1 or stale_hits >= 3 or weak_failed >= 4)
            and peakless_legal_profile(candidate)
            and score < 1.84
            and not elite_recovery
        )

        if cooldown_authority_veto:
            return True, "legal_cooldown_authority_gate"
        if memory_authority_veto or legal_zero_peak_history_veto:
            return True, "legal_cooldown_memory_veto"

        return False, None
    except Exception:
        return False, None


def family_reopen_locked(candidate, family_dead_cooldown, now_ts, lock_seconds=1800):
    try:
        family = str(candidate.get("family_key", "") or "")
        if not family:
            return False
        ts = family_dead_cooldown.get(family)
        if not ts:
            return False
        locked = (now_ts - ts).total_seconds() < float(lock_seconds)
        if locked:
            print(
                "TRACE | family_reopen_lock | family={} | lock_seconds={} | market_type={} | score={:.3f} | {}".format(
                    family[:72],
                    int(lock_seconds),
                    candidate.get("market_type", "unknown"),
                    float(candidate.get("score", 0.0) or 0.0),
                    (candidate.get("question") or "")[:72],
                )
            )
        return locked
    except Exception:
        return False


def apply_family_capital_priority(candidates, recently_cut_families=None):
    if not candidates:
        return candidates

    recently_cut_families = set(recently_cut_families or [])

    def _priority_key(c):
        try:
            family = str(c.get("family_key", "") or "")
            survival = float(c.get("survival_priority", 0.0) or 0.0)
            score = float(c.get("score", 0.0) or 0.0)
            pd = float(c.get("pressure_density", 0.0) or 0.0)
            win = abs(float(c.get("price_delta_window", 0.0) or 0.0))
            market_type = str(c.get("market_type", "") or "")
            family_penalty = -0.25 if family in recently_cut_families else 0.0
            market_bonus = 0.0
            if market_type in {"short_burst_catalyst", "legal_resolution"}:
                market_bonus = 0.08
            elif market_type in {"valuation_ladder", "narrative_long_tail"}:
                market_bonus = 0.03
            return survival + (score * 0.30) + (pd * 0.25) + (win * 8.0) + family_penalty + market_bonus
        except Exception:
            return 0.0

    ranked = sorted(candidates, key=_priority_key, reverse=True)
    for i, c in enumerate(ranked[:10]):
        try:
            print(
                "TRACE | family_capital_priority | rank={} | family={} | market_type={} | score={:.3f} | survival={:.3f} | {}".format(
                    i + 1,
                    (c.get("family_key") or "unknown")[:72],
                    c.get("market_type", "unknown"),
                    float(c.get("score", 0.0) or 0.0),
                    float(c.get("survival_priority", 0.0) or 0.0),
                    (c.get("question") or "")[:72],
                )
            )
        except Exception:
            pass
    return ranked


def _safe_int(value, default=0):
    try:
        return int(value)
    except Exception:
        return default



def apply_stake_concentration_guard(stake_value, candidate, regime_cfg):
    try:
        stake = float(stake_value or 0.0)
    except Exception:
        return stake_value

    try:
        cycle_cap = float(regime_cfg.get("cycle_risk_usd", regime_cfg.get("MAX_CYCLE_RISK_USD", 0.0)) or 0.0)
    except Exception:
        cycle_cap = 0.0

    if stake <= 0:
        return stake_value

    entry_reason = str(candidate.get("reason", candidate.get("_entry_reason", "")) or "")
    theme = str(candidate.get("theme", "") or "")
    cluster = str(candidate.get("cluster", "") or "")

    max_single_share = 0.38
    max_single_stake = cycle_cap * max_single_share if cycle_cap > 0 else 0.0

    print(
        "TRACE | stake_guard_check | cycle_cap={:.2f} | share_cap={:.2f} | raw_stake={:.2f} | reason={} | theme={} | cluster={} | {}".format(
            cycle_cap,
            max_single_share,
            stake,
            entry_reason,
            theme,
            cluster,
            (candidate.get("question") or "")[:72],
        )
    )

    if entry_reason in {"multicycle_momentum_override", "momentum_override", "score+momentum"}:
        max_single_stake = min(max_single_stake, 4.00) if max_single_stake > 0 else 4.00

    if max_single_stake > 0 and stake > max_single_stake:
        print(
            "TRACE | stake_concentration_cap | cycle_cap={:.2f} | share_cap={:.2f} | old_stake={:.2f} | new_stake={:.2f} | reason={} | theme={} | cluster={} | {}".format(
                cycle_cap,
                max_single_share,
                stake,
                max_single_stake,
                entry_reason,
                theme,
                cluster,
                (candidate.get("question") or "")[:72],
            )
        )
        return round(max_single_stake, 4)

    return stake_value



def _cap_intents_for_candidate(candidate):
    intents = []

    def _add(active, cap_value, signal, cap_kind, fallback_kind=None):
        if not bool(active):
            return
        try:
            cap = float(cap_value or 0.0)
        except Exception:
            cap = 0.0
        if cap <= 0 and fallback_kind:
            cap = float(execution_micro_clamp_cap(candidate, fallback_kind) or 0.0)
        intents.append({
            "signal": str(signal or cap_kind or "unknown"),
            "cap": round(float(cap or 0.0), 4),
            "kind": str(cap_kind or "generic"),
        })

    _add(candidate.get("_political_rescue_scout_demotion", False), candidate.get("_political_rescue_scout_cap", 0.0), candidate.get("_political_override_reason") or "political_rescue_scout", "political_rescue")
    _add(candidate.get("_thin_pressure_truth_active", False), candidate.get("_thin_pressure_truth_cap", 0.0), candidate.get("_thin_pressure_truth_signal") or "thin_pressure_truth_scout_cap", "thin_truth", fallback_kind="thin_truth_force_delayed" if bool(candidate.get("_thin_pressure_truth_force_delayed", False)) else None)
    _add(candidate.get("_elite_recovery_clamp_active", False), candidate.get("_elite_recovery_clamp_cap", 0.0), candidate.get("_elite_recovery_signal") or "elite_recovery_override", "elite_recovery", fallback_kind="elite_recovery" if bool(candidate.get("_elite_recovery_force_delayed", False) or candidate.get("_elite_recovery_micro_scout", False)) else None)
    _add(candidate.get("_dead_money_compression_active", False), candidate.get("_dead_money_compression_cap", 0.0), candidate.get("_dead_money_compression_signal") or "follow_through_compression", "follow_through", fallback_kind="follow_through_force_delayed" if bool(candidate.get("_follow_through_force_delayed", False)) else None)
    _add(candidate.get("_relief_escalation_active", False), candidate.get("_relief_escalation_cap", 0.0), candidate.get("_relief_escalation_signal") or "relief_escalation", "relief_escalation", fallback_kind="relief_escalation" if bool(candidate.get("_relief_escalation_force_delayed", False) or candidate.get("_relief_escalation_micro_scout", False)) else None)
    _add(candidate.get("_adaptive_calm_relief_active", False), candidate.get("_adaptive_calm_relief_cap", 0.0), candidate.get("_adaptive_calm_relief_signal") or "adaptive_calm_admission_relief", "adaptive_calm_relief", fallback_kind="adaptive_calm_relief" if bool(candidate.get("_adaptive_calm_relief_force_delayed", False) or candidate.get("_adaptive_calm_relief_micro_scout", False)) else None)
    _add(candidate.get("_adaptive_budget_active", False), candidate.get("_adaptive_budget_cap", 0.0), candidate.get("_adaptive_budget_signal") or "adaptive_admission_budget", "adaptive_budget", fallback_kind="adaptive_calm_relief" if bool(candidate.get("_adaptive_budget_force_delayed", False) or candidate.get("_adaptive_budget_micro_scout", False)) else None)
    _add(candidate.get("_narrative_full_size_brake", False), candidate.get("_narrative_full_size_brake_cap", 0.0), "narrative_full_size_brake", "narrative_brake")
    _add(candidate.get("_weak_sports_override_brake", False), candidate.get("_weak_sports_override_brake_cap", 0.0), "weak_sports_override_brake", "sports_brake")

    deduped = []
    seen = set()
    for intent in intents:
        key = (intent["signal"], intent["cap"], intent["kind"])
        if key in seen:
            continue
        seen.add(key)
        deduped.append(intent)
    return deduped


def canonical_cap_executor(candidate, stake_value, stake_meta, current_regime, opened_now, regime_cfg):
    try:
        raw_stake = float(stake_value or 0.0)
    except Exception:
        return stake_value, dict(stake_meta or {})
    if raw_stake <= 0:
        return stake_value, dict(stake_meta or {})

    stake_meta = dict(stake_meta or {})
    try:
        preexisting_stake = float(stake_meta.get("stake", raw_stake) or raw_stake)
    except Exception:
        preexisting_stake = raw_stake

    base_stake = min(raw_stake, preexisting_stake) if preexisting_stake > 0 else raw_stake
    intents = _cap_intents_for_candidate(candidate)
    for intent in intents:
        print(
            "TRACE | cap_intent_seen | signal={} | kind={} | cap={:.2f} | base={:.2f} | reason={} | market_type={} | {}".format(
                intent["signal"], intent["kind"], float(intent["cap"] or 0.0), base_stake,
                str(candidate.get("reason", candidate.get("_entry_reason", "")) or ""),
                str(candidate.get("market_type", "general_binary") or "general_binary"),
                (candidate.get("question") or "")[:72],
            )
        )

    selected_signal = "none"
    selected_cap = 0.0
    selectable = [i for i in intents if float(i.get("cap", 0.0) or 0.0) > 0]
    if selectable:
        selected = min(selectable, key=lambda x: float(x["cap"]))
        selected_signal = str(selected["signal"])
        selected_cap = float(selected["cap"] or 0.0)

    after_signal = min(base_stake, selected_cap) if selected_cap > 0 else base_stake
    print(
        "TRACE | cap_executor_choice | active={} | selected_signal={} | selected_cap={:.2f} | base={:.2f} | preexisting={:.2f} | after_signal={:.2f} | reason={} | market_type={} | {}".format(
            int(bool(intents)), selected_signal, selected_cap, raw_stake, preexisting_stake, after_signal,
            str(candidate.get("reason", candidate.get("_entry_reason", "")) or ""),
            str(candidate.get("market_type", "general_binary") or "general_binary"),
            (candidate.get("question") or "")[:72],
        )
    )

    after_guard = apply_stake_concentration_guard(after_signal, candidate, regime_cfg)
    after_probe = calm_probe_stake(after_guard, candidate, current_regime, opened_now)
    final_stake = float(after_probe or 0.0)
    if selected_cap > 0 and final_stake > (selected_cap + 1e-6):
        print(
            "WARN | cap_executor_mismatch | signal={} | selected_cap={:.2f} | computed_final={:.2f} | forcing_cap=1 | reason={} | market_type={} | {}".format(
                selected_signal, selected_cap, final_stake,
                str(candidate.get("reason", candidate.get("_entry_reason", "")) or ""),
                str(candidate.get("market_type", "general_binary") or "general_binary"),
                (candidate.get("question") or "")[:72],
            )
        )
        final_stake = selected_cap

    final_stake = round(max(0.5, float(final_stake or 0.0)), 2)
    stake_meta["stake"] = round(final_stake, 4)
    stake_meta["canonical_cap_active"] = bool(intents)
    stake_meta["canonical_cap_signal"] = selected_signal
    stake_meta["canonical_cap_value"] = round(selected_cap, 4) if selected_cap > 0 else 0.0
    stake_meta["canonical_cap_base_stake"] = round(raw_stake, 4)
    stake_meta["canonical_cap_preexisting_stake"] = round(preexisting_stake, 4)
    stake_meta["cap_intent_signals"] = [intent["signal"] for intent in intents]
    stake_meta["cap_intent_count"] = len(intents)
    stake_meta["cap_arbiter_active"] = bool(intents)
    stake_meta["cap_arbiter_signals"] = [intent["signal"] for intent in intents]
    stake_meta["cap_arbiter_hard_cap"] = round(selected_cap, 4) if selected_cap > 0 else 0.0
    stake_meta["cap_arbiter_base_stake"] = round(raw_stake, 4)
    stake_meta["cap_arbiter_pre_cap_stake"] = round(preexisting_stake, 4)

    print(
        "TRACE | cap_executor_apply | active={} | selected_signal={} | selected_cap={:.2f} | after_guard={:.2f} | final={:.2f} | reason={} | market_type={} | {}".format(
            int(bool(intents)), selected_signal, selected_cap, float(after_guard or 0.0), final_stake,
            str(candidate.get("reason", candidate.get("_entry_reason", "")) or ""),
            str(candidate.get("market_type", "general_binary") or "general_binary"),
            (candidate.get("question") or "")[:72],
        )
    )
    return final_stake, stake_meta


def unified_cap_arbiter(candidate, stake_value, stake_meta, current_regime, opened_now, regime_cfg):
    return canonical_cap_executor(candidate, stake_value, stake_meta, current_regime, opened_now, regime_cfg)

def _safe_float(value, default=0.0):
    try:
        return float(value)
    except Exception:
        return default


def load_runtime_state():
    if not os.path.exists(STATE_FILE):
        return {}
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            payload = json.load(f)
        return payload if isinstance(payload, dict) else {}
    except Exception as e:
        print("WARN | runtime_state_load_failed | {}".format(type(e).__name__))
        return {}


def hydrate_signal_memory(state):
    base = default_signal_memory()
    if not isinstance(state, dict):
        return base
    for key, value in state.items():
        if isinstance(value, dict):
            base[key] = {
                "seen": _safe_int(value.get("seen", 0), 0),
                "opened": _safe_int(value.get("opened", 0), 0),
            }
    return base


def hydrate_timestamp_map(state):
    result = {}
    if not isinstance(state, dict):
        return result
    for key, value in state.items():
        try:
            result[str(key)] = float(value)
        except Exception:
            continue
    return result


def hydrate_dict_map(state):
    result = {}
    if not isinstance(state, dict):
        return result
    for key, value in state.items():
        if isinstance(value, dict):
            result[str(key)] = value
    return result


def hydrate_price_history(state, history_window):
    result = {}
    if not isinstance(state, dict):
        return result
    for key, values in state.items():
        if not isinstance(values, list):
            continue
        cleaned = []
        for item in values[-history_window:]:
            try:
                cleaned.append(float(item))
            except Exception:
                continue
        if cleaned:
            result[str(key)] = deque(cleaned, maxlen=history_window)
    return result


def save_runtime_state(
    price_history,
    momentum_cooldown,
    score_reentry_cooldown,
    dead_reentry_cooldown,
    family_dead_cooldown,
    stale_reentry_cooldown,
    delayed_entry_watch,
    delayed_entry_cooldown,
    delayed_entry_memory,
    market_exit_memory,
    signal_memory,
):
    try:
        payload = {
            "ts": utc_now_iso(),
            "price_history": {k: list(v) for k, v in price_history.items() if v},
            "momentum_cooldown": momentum_cooldown,
            "score_reentry_cooldown": score_reentry_cooldown,
            "dead_reentry_cooldown": dead_reentry_cooldown,
            "family_dead_cooldown": family_dead_cooldown,
            "stale_reentry_cooldown": stale_reentry_cooldown,
            "delayed_entry_watch": delayed_entry_watch,
            "delayed_entry_cooldown": delayed_entry_cooldown,
            "delayed_entry_memory": delayed_entry_memory,
            "market_exit_memory": market_exit_memory,
            "signal_memory": signal_memory,
        }
        tmp = STATE_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False)
        os.replace(tmp, STATE_FILE)
    except Exception as e:
        print("WARN | runtime_state_save_failed | {}".format(type(e).__name__))



def deduplicate_by_question(candidates):
    seen = set()
    result = []

    for c in candidates:
        q = (c.get("question") or "").lower().strip()

        base = q
        base = base.replace("will ", "")
        base = base.replace(" win", "")

        if base in seen:
            continue

        seen.add(base)
        result.append(c)

    return result


def limit_per_theme(candidates, max_per_theme=3):
    result = []
    counter = {}

    for c in candidates:
        theme = c.get("theme", "unknown")

        if counter.get(theme, 0) >= max_per_theme:
            continue

        counter[theme] = counter.get(theme, 0) + 1
        result.append(c)

    return result


def diversify_by_cluster(candidates, max_per_cluster=2):
    result = []
    counter = {}

    for c in candidates:
        cluster = c.get("cluster", "unknown")
        if counter.get(cluster, 0) >= max_per_cluster:
            continue
        counter[cluster] = counter.get(cluster, 0) + 1
        result.append(c)

    return result




def reasonless_structure_is_weak(candidate):
    try:
        density = float(candidate.get("pressure_density", 0.0) or 0.0)
        pressure_count = int(candidate.get("pressure_count", 0) or 0)
        trend = float(candidate.get("price_trend_strength", 0.0) or 0.0)
        abs_window = abs(float(candidate.get("price_delta_window", 0.0) or 0.0))
        abs_delta = abs(float(candidate.get("price_delta", 0.0) or 0.0))
        score = float(candidate.get("score", 0.0) or 0.0)
    except Exception:
        return True

    return density < 0.18 and pressure_count < 2 and trend < 0.82 and abs_window < 0.006 and abs_delta < 0.006 and score < 0.98

def expanded_universe_candidates(candidates, top_n=14):
    explored = []
    for c in candidates:
        q = c.get("question", "")
        if is_blacklisted(q) or is_junk_market(q) or is_far_future_politics(q):
            continue

        try:
            price = float(c.get("price", 0.0) or 0.0)
            liquidity = float(c.get("liquidity", 0.0) or 0.0)
            minutes_to_end = c.get("minutes_to_end")
            if minutes_to_end is not None:
                minutes_to_end = float(minutes_to_end)
            density = float(c.get("pressure_density", 0.0) or 0.0)
            trend = float(c.get("price_trend_strength", 0.0) or 0.0)
            abs_window = abs(float(c.get("price_delta_window", 0.0) or 0.0))
            abs_delta = abs(float(c.get("price_delta", 0.0) or 0.0))
            score = float(c.get("score", 0.0) or 0.0)
        except Exception:
            continue

        market_type = c.get("market_type") or detect_market_type(c)
        theme = c.get("theme") or detect_theme(q)

        if price < 0.001 or price > 0.055:
            continue
        if liquidity < 160.0:
            continue
        if minutes_to_end is not None and minutes_to_end < 120:
            continue

        structure_votes = 0
        if density >= 0.14:
            structure_votes += 1
        if trend >= 0.74:
            structure_votes += 1
        if abs_window >= 0.0045:
            structure_votes += 1
        if abs_delta >= 0.0055:
            structure_votes += 1
        if score >= 0.78:
            structure_votes += 1

        if structure_votes < 2:
            continue

        if market_type in {"narrative_long_tail", "general_binary"} and theme in {"politics", "general"}:
            if structure_votes < 3 and score < 0.96:
                continue

        if market_type in {"sports_award_longshot", "valuation_ladder"} and reasonless_structure_is_weak(c):
            continue

        explored.append(c)

    ranked = rank_candidates(explored) if explored else []
    ranked = deduplicate_by_question(ranked)
    ranked = diversify_by_cluster(ranked, max_per_cluster=2)
    return ranked[:top_n]


def pick_universe_candidates(ranked, pulses, trends, pressures, explorers, top_main=18, top_explore=8):
    primary = merge_sources(ranked[:top_main], pulses[:8], trends[:8], pressures[:8])
    expanded = merge_sources(explorers[:top_explore], trends[:4], pressures[:4], [])
    return merge_sources(primary, expanded, [], [])


def post_merge_hygiene_reason(candidate):
    try:
        question = str(candidate.get("question", "") or "")
        if is_blacklisted(question):
            return "blacklist"
        if is_junk_market(question):
            return "junk_market"
        if is_far_future_politics(question):
            return "far_future_politics"

        price = float(candidate.get("price", 0.0) or 0.0)
        liquidity = float(candidate.get("liquidity", 0.0) or 0.0)
        minutes_to_end = candidate.get("minutes_to_end")
        if minutes_to_end is not None:
            minutes_to_end = float(minutes_to_end)

        if not (float(MIN_PRICE) <= price <= float(MAX_PRICE)):
            return "price_band"
        if liquidity < float(MIN_LIQUIDITY):
            return "liquidity_floor"
        if minutes_to_end is not None and minutes_to_end < float(MIN_MINUTES_TO_END):
            return "time_floor"
        return None
    except Exception:
        return "parse_error"


def apply_post_merge_hygiene_firewall(candidates, stage="combined"):
    clean = []
    blocked = {}
    for c in list(candidates or []):
        reason = post_merge_hygiene_reason(c)
        if reason:
            blocked[reason] = int(blocked.get(reason, 0) or 0) + 1
            try:
                print(
                    "SKIP | post_merge_hygiene_firewall | stage={} | reason={} | theme={} | cluster={} | price={:.4f} | score={:.3f} | {}".format(
                        stage,
                        reason,
                        str(c.get("theme", "") or ""),
                        str(c.get("cluster", "") or ""),
                        float(c.get("price", 0.0) or 0.0),
                        float(c.get("score", 0.0) or 0.0),
                        (c.get("question") or "")[:110],
                    )
                )
            except Exception:
                pass
            continue
        clean.append(c)

    if blocked:
        detail = ",".join("{}:{}".format(k, blocked[k]) for k in sorted(blocked.keys()))
        print(
            "TRACE | post_merge_hygiene_summary | stage={} | kept={} | removed={} | detail={}".format(
                stage,
                len(clean),
                max(len(list(candidates or [])) - len(clean), 0),
                detail,
            )
        )
    return clean


def build_market_key(candidate):
    return "{}::{}".format(
        candidate.get("market_id", ""),
        candidate.get("outcome_name", "")
    )


def build_family_memory_key(candidate_or_family):
    try:
        if isinstance(candidate_or_family, dict):
            family = str(candidate_or_family.get("family_key", "") or "")
        else:
            family = str(candidate_or_family or "")
        if not family:
            return ""
        return "family::{}".format(family)
    except Exception:
        return ""


def build_market_map(candidates):
    market_map = {}
    for c in candidates:
        market_map[build_market_key(c)] = c
    return market_map


def normalize_family_question(text):
    q = (text or '').lower()
    q = re.sub(r"\([^)]*\)", ' ', q)
    q = re.sub(r"[^a-z0-9$% ]+", ' ', q)
    q = re.sub(r"\b(2025|2026|2027|2028|jan|january|feb|february|mar|march|apr|april|may|jun|june|jul|july|aug|august|sep|sept|september|oct|october|nov|november|dec|december)\b", ' ', q)
    q = re.sub(r"\b(will|be|is|are|a|an|the|of|to|in|on|for|by|before|after|during|this|that|with|from|than|over|under|above|below|between|into|at)\b", ' ', q)
    q = re.sub(r"\b(yes|no|more|less|than|greater|fewer|under|over|above|below|between|range|odds|probability)\b", ' ', q)
    q = re.sub(r"\b(what|who|when|where|why|how)\b", ' ', q)
    q = re.sub(r"\s+", ' ', q).strip()
    return q


def detect_market_family(candidate):
    cluster = candidate.get('cluster', 'unknown')
    market_type = candidate.get('market_type') or detect_market_type(candidate)
    q = normalize_family_question(candidate.get('question', ''))
    tokens = []
    for token in q.split():
        if token.isdigit():
            continue
        if token in {'dollars', 'dollar', 'usd', 'cents', 'cent', 'market', 'cap', 'fdv', 'price'}:
            continue
        if len(token) <= 2 and not token.startswith('$'):
            continue
        tokens.append(token)
    if not tokens:
        return '{}::{}::{}'.format(cluster, market_type, build_market_key(candidate))
    return '{}::{}::{}'.format(cluster, market_type, '-'.join(tokens[:4]))


def merge_scored_into_candidates(raw_candidates, scored_candidates):
    """
    Берем raw candidates как основу market state,
    но если scorer дал score / другие enriched-поля — вплавляем их обратно по ключу.
    Это лечит баг, когда lifecycle видел raw объект без score.
    """
    scored_by_key = {}
    for c in scored_candidates:
        scored_by_key[build_market_key(c)] = c

    result = []
    for raw in raw_candidates:
        merged = dict(raw)
        scored = scored_by_key.get(build_market_key(raw))
        if scored:
            merged.update(scored)
        result.append(merged)
    return result


def top_movers(candidates, top_n=5):
    enriched = []
    for c in candidates:
        try:
            delta = float(c.get("price_delta", 0.0))
        except Exception:
            delta = 0.0
        item = dict(c)
        item["_delta"] = delta
        enriched.append(item)

    movers_up = sorted(enriched, key=lambda x: x["_delta"], reverse=True)[:top_n]
    movers_down = sorted(enriched, key=lambda x: x["_delta"])[:top_n]
    return movers_up, movers_down


def pulse_candidates(candidates, min_abs_delta=0.006, top_n=10):
    result = []
    for c in candidates:
        try:
            abs_delta = abs(float(c.get("price_delta", 0.0)))
            price = float(c.get("price", 0.0))
        except Exception:
            abs_delta = 0.0
            price = 0.0

        if abs_delta >= min_abs_delta and price <= 0.6:
            result.append(c)

    result.sort(key=lambda x: abs(float(x.get("price_delta", 0.0))), reverse=True)
    return result[:top_n]


def trend_candidates(candidates, min_abs_window_delta=0.008, top_n=10):
    result = []
    for c in candidates:
        try:
            abs_window = abs(float(c.get("price_delta_window", 0.0)))
            price = float(c.get("price", 0.0))
        except Exception:
            abs_window = 0.0
            price = 0.0

        if abs_window >= min_abs_window_delta and price <= 0.6:
            result.append(c)

    result.sort(key=lambda x: abs(float(x.get("price_delta_window", 0.0))), reverse=True)
    return result[:top_n]


def pressure_candidates(candidates, min_pressure_density=0.40, min_pressure_count=2, top_n=10):
    result = []
    for c in candidates:
        try:
            density = float(c.get("pressure_density", 0.0))
            count = int(c.get("pressure_count", 0))
            price = float(c.get("price", 0.0))
        except Exception:
            density = 0.0
            count = 0
            price = 0.0

        if density >= min_pressure_density and count >= min_pressure_count and price <= 0.6:
            result.append(c)

    result.sort(
        key=lambda x: (
            float(x.get("pressure_density", 0.0)),
            int(x.get("pressure_count", 0)),
            abs(float(x.get("price_delta_window", 0.0)))
        ),
        reverse=True
    )
    return result[:top_n]


def is_momentum_entry(candidate):
    try:
        price = float(candidate.get("price", 0.0))
        delta = float(candidate.get("price_delta", 0.0))
        score = float(candidate.get("score", 0.0))
    except Exception:
        return False

    theme = candidate.get("theme", "unknown")
    abs_delta = abs(delta)

    if price <= 0.15 and abs_delta >= 0.01 and score >= 0.50:
        return True

    if price <= 0.30 and abs_delta >= 0.01 and score >= 0.70:
        return True

    if abs_delta >= 0.02:
        return True

    if theme in {"crypto", "politics", "weird"} and abs_delta >= 0.008 and price <= 0.35:
        return True

    return False


def is_momentum_override(candidate):
    try:
        price = float(candidate.get("price", 0.0))
        delta = float(candidate.get("price_delta", 0.0))
        score = float(candidate.get("score", 0.0) or 0.0)
    except Exception:
        return False

    theme = candidate.get("theme", "unknown")
    abs_delta = abs(delta)

    if price <= 0.25 and abs_delta >= 0.01 and score >= 0.45:
        return True

    if theme in {"crypto", "politics", "weird"} and price <= 0.35 and abs_delta >= 0.008 and score >= 0.40:
        return True

    if price <= 0.50 and abs_delta >= 0.02:
        return True

    if price <= 0.35 and abs_delta >= 0.006 and score >= 0.35:
        return True

    return False


def is_multicycle_momentum_override(candidate):
    try:
        price = float(candidate.get("price", 0.0))
        delta_1 = float(candidate.get("price_delta", 0.0))
        delta_window = float(candidate.get("price_delta_window", 0.0))
        trend_strength = float(candidate.get("price_trend_strength", 0.0))
        score = float(candidate.get("score", 0.0) or 0.0)
    except Exception:
        return False

    theme = candidate.get("theme", "unknown")

    abs_delta_1 = abs(delta_1)
    abs_delta_window = abs(delta_window)

    if price <= 0.35 and abs_delta_window >= 0.01 and score >= 0.35:
        return True

    if theme in {"crypto", "politics", "weird"} and price <= 0.40 and abs_delta_window >= 0.008 and score >= 0.40:
        return True

    if price <= 0.35 and trend_strength >= 0.75 and abs_delta_window >= 0.006 and abs_delta_1 >= 0.003:
        return True

    if price <= 0.50 and abs_delta_window >= 0.02:
        return True

    return False


def is_pre_momentum(candidate):
    try:
        price = float(candidate.get("price", 0.0))
        score = float(candidate.get("score", 0.0) or 0.0)
        window_delta = abs(float(candidate.get("price_delta_window", 0.0)))
        trend = float(candidate.get("price_trend_strength", 0.0))
    except Exception:
        return False

    theme = candidate.get("theme", "unknown")

    if price <= 0.25 and window_delta >= 0.002 and trend >= 0.80 and score >= 0.50:
        return True

    if theme in {"crypto", "politics", "weird"} and price <= 0.35 and window_delta >= 0.002 and trend >= 0.75 and score >= 0.45:
        return True

    if price <= 0.40 and window_delta >= 0.004 and trend >= 0.70 and score >= 0.40:
        return True

    return False


def is_pressure_entry(candidate):
    try:
        price = float(candidate.get("price", 0.0))
        score = float(candidate.get("score", 0.0) or 0.0)
        density = float(candidate.get("pressure_density", 0.0))
        count = int(candidate.get("pressure_count", 0))
        window_delta = abs(float(candidate.get("price_delta_window", 0.0)))
        liquidity = float(candidate.get("liquidity", 0.0))
    except Exception:
        return False

    theme = candidate.get("theme", "unknown")

    if price > 0.50:
        return False

    if liquidity < 1000:
        return False

    if score < 0.35:
        return False

    if density >= 0.50 and count >= 3:
        return True

    if density >= 0.30 and count >= 1 and window_delta >= 0.003:
        return True

    if theme in {"crypto", "weird"}:
        if density >= 0.25 and count >= 1:
            return True
        if window_delta >= 0.002 and density > 0:
            return True

    if theme == "politics":
        if density >= 0.30 and count >= 1 and window_delta >= 0.002:
            return True

    if theme == "sports":
        if density >= 0.40 and count >= 2 and window_delta >= 0.005:
            return True

    return False


def merge_sources(ranked, pulses, trends, pressures):
    result = []
    seen = set()

    for group in (ranked, pulses, trends, pressures):
        for c in group:
            key = build_market_key(c)
            if key not in seen:
                seen.add(key)
                result.append(c)

    return result


def compute_history_features(history_deque, current_price):
    if not history_deque:
        return 0.0, 0.0, 0.0, 0.0, 0

    prev_price = history_deque[-1]
    delta_1 = current_price - prev_price

    oldest_price = history_deque[0]
    delta_window = current_price - oldest_price

    points = list(history_deque) + [current_price]
    directions = []
    non_zero_count = 0

    for i in range(1, len(points)):
        diff = points[i] - points[i - 1]
        if abs(diff) > 1e-12:
            non_zero_count += 1

        if diff > 0:
            directions.append(1)
        elif diff < 0:
            directions.append(-1)
        else:
            directions.append(0)

    non_zero_dirs = [d for d in directions if d != 0]
    if not non_zero_dirs:
        trend_strength = 0.0
    else:
        dominant = 1 if sum(non_zero_dirs) >= 0 else -1
        aligned = sum(1 for d in non_zero_dirs if d == dominant)
        trend_strength = float(aligned) / float(len(non_zero_dirs))

    total_steps = max(len(points) - 1, 1)
    pressure_density = float(non_zero_count) / float(total_steps)

    # v21.2.3: pressure quality guard
    # Boost only when multi-tick pressure has actual supporting movement.
    abs_window = abs(float(delta_window or 0.0))
    if non_zero_count >= 3:
        pressure_density *= 1.55
    elif non_zero_count >= 2 and abs_window >= 0.0015 and trend_strength > 0.0:
        pressure_density *= 1.30
    elif trend_strength <= 0.0 and abs_window <= 0.0:
        pressure_density *= 0.85

    if pressure_density > 1.0:
        pressure_density = 1.0

    return delta_1, delta_window, trend_strength, pressure_density, non_zero_count




def detect_market_type(candidate):
    q = (candidate.get("question") or "").lower()
    cluster = candidate.get("cluster", "unknown")
    theme = candidate.get("theme", "unknown")

    if any(x in q for x in ["sentenced", "sentence", "prison", "convicted", "trial", "charges", "acquitted", "guilty"]) or cluster == "legal_cases":
        return "legal_resolution"

    if "fdv" in q or "market cap" in q or ">$" in q or "<$" in q or cluster == "crypto_launch":
        return "valuation_ladder"

    if any(x in q for x in ["putin out", "xi jinping", "xi ", "ceasefire", "president of russia", "before 2027", "by december 31, 2026", "by december 31"]) or cluster == "geopolitics":
        return "narrative_long_tail"

    if any(x in q for x in ["consumer hardware", "hardware product", "launch a new", "airdrop", "product launch", "launch by", "launch before"]):
        return "short_burst_catalyst"

    if any(x in q for x in ["rookie of the year", "mvp", "stanley cup", "world cup", "qualify", "finals", "championship", "masters", "champions league", "bundesliga", "tournament"]):
        return "sports_award_longshot"

    if any(x in q for x in ["album", "rihanna", "playboi", "gta", "celebrity"]) or cluster == "music_release":
        return "speculative_hype"

    if any(x in q for x in ["before", "released before", "by march", "by april", "by may", "by june", "by july", "by august", "by september", "by october", "by november", "by december"]):
        return "scheduled_binary_event"

    return "general_binary"


def compute_market_type_multiplier(candidate, reason, current_regime):
    market_type = candidate.get("market_type") or detect_market_type(candidate)

    mult = 1.0

    if market_type == "short_burst_catalyst":
        mult *= 1.10 if reason in {"score+pressure", "score+momentum", "multicycle_momentum_override", "momentum_override"} else 1.04
    elif market_type == "legal_resolution":
        mult *= 1.03 if reason in {"score+pressure", "pressure", "pre_momentum", "multicycle_momentum_override"} else 0.99
    elif market_type == "scheduled_binary_event":
        mult *= 1.00 if reason in {"score", "score+pre_momentum"} else 0.97
    elif market_type == "valuation_ladder":
        mult *= 0.91
    elif market_type == "narrative_long_tail":
        mult *= 0.80
    elif market_type == "speculative_hype":
        mult *= 1.05 if reason in {"score+pressure", "score+momentum", "multicycle_momentum_override"} else 0.96
    elif market_type == "sports_award_longshot":
        mult *= 0.64
    elif market_type == "general_binary":
        mult *= 0.98

    if current_regime == "hot" and market_type in {"short_burst_catalyst", "speculative_hype"}:
        mult *= 1.04
    if current_regime == "calm" and market_type in {"narrative_long_tail", "sports_award_longshot"}:
        mult *= 0.95
    if current_regime == "normal" and market_type == "legal_resolution" and reason in {"score+pressure", "pressure"}:
        mult *= 1.02

    return round(clamp(mult, 0.58, 1.16), 4)

def detect_cluster(candidate):
    q = (candidate.get("question") or "").lower()

    if any(x in q for x in ["rihanna", "album", "playboi", "carti"]):
        return "music_release"
    if any(x in q for x in ["openai", "consumer hardware", "gta"]):
        return "tech_media"
    if any(x in q for x in ["ceasefire", "ukraine", "putin", "xi", "taiwan", "senate", "house", "balance of power"]):
        return "geopolitics"
    if any(x in q for x in ["weinstein", "convicted", "sentenced", "prison"]):
        return "legal_cases"
    if any(x in q for x in ["megaeth", "fdv", "crypto", "etf", "airdrop", "market cap"]):
        return "crypto_launch"
    if any(x in q for x in ["rookie of the year", "stanley cup", "world cup", "masters", "champions league", "bundesliga", "finals", "tournament"]):
        return "sports_outrights"

    theme = candidate.get("theme", "unknown")
    return "theme_{}".format(theme)


def calculate_stake(candidate, reason):
    base = 1.0

    if reason == "score":
        base = 1.0
    elif reason == "score+pre_momentum":
        base = 1.5
    elif reason == "pre_momentum":
        base = 1.35
    elif reason == "pressure":
        base = 1.2
    elif reason == "score+pressure":
        base = 2.0
    elif reason == "score+momentum":
        base = 2.2
    elif reason in ["momentum_override", "multicycle_momentum_override"]:
        base = 2.5

    try:
        score = float(candidate.get("score", 0.0) or 0.0)
    except Exception:
        score = 0.0

    if score > 1.2:
        base *= 1.2
    elif score > 1.0:
        base *= 1.1

    theme = candidate.get("theme", "")
    if theme in ["crypto", "weird"]:
        base *= 1.1

    try:
        price = float(candidate.get("price", 0.0))
    except Exception:
        price = 0.0

    if price > 0.4:
        base *= 0.7

    return round(min(base, 3.0), 2)


def portfolio_adjusted_stake(candidate, reason, engine, cycle_theme_counts, cycle_cluster_counts):
    stake = calculate_stake(candidate, reason)
    theme = candidate.get("theme", "unknown")
    cluster = candidate.get("cluster", "unknown")

    theme_exposure = engine.theme_exposure()
    cluster_exposure = engine.cluster_exposure()

    current_theme_exposure = float(theme_exposure.get(theme, 0.0))
    current_cluster_exposure = float(cluster_exposure.get(cluster, 0.0))

    if current_theme_exposure >= 4.0:
        stake *= 0.70
    elif current_theme_exposure >= 2.5:
        stake *= 0.85

    if current_cluster_exposure >= 3.5:
        stake *= 0.65
    elif current_cluster_exposure >= 2.0:
        stake *= 0.85

    if cycle_theme_counts.get(theme, 0) >= 2:
        stake *= 0.80

    if cycle_cluster_counts.get(cluster, 0) >= 1:
        stake *= 0.75

    politics_state = politics_concentration_state(engine, candidate)
    if politics_state.get("is_politics_like"):
        total_exp = float(politics_state.get("total_exposure", 0.0) or 0.0)
        family_exp = float(politics_state.get("family_exposure", 0.0) or 0.0)
        if total_exp >= 4.5:
            stake *= 0.74
        elif total_exp >= 3.2:
            stake *= 0.88

        if family_exp >= 2.4:
            stake *= 0.72
        elif family_exp >= 1.5:
            stake *= 0.86

        if int(politics_state.get("open_count", 0) or 0) >= 4:
            stake *= 0.86

    rebalance = concentration_rebalance_state(engine, candidate)
    if rebalance.get("dominant"):
        penalty = float(rebalance.get("penalty", 0.0) or 0.0)
        stake *= max(0.58, 1.0 - penalty)

    return round(max(0.5, min(stake, 3.0)), 2)


def candidate_is_politics_like(candidate):
    theme = candidate.get("theme", "unknown")
    cluster = candidate.get("cluster", "unknown")
    family_key = str(candidate.get("family_key", "") or "")
    market_type = candidate.get("market_type", "general_binary")
    q = (candidate.get("question") or "").lower()

    if theme == "politics":
        return True
    if cluster in {"geopolitics", "theme_politics"}:
        return True
    if family_key.startswith("geopolitics::") or family_key.startswith("theme_politics::"):
        return True

    politics_terms = {
        "ceasefire", "president", "prime minister", "senate", "house",
        "balance of power", "election", "ukraine", "russia", "taiwan",
        "china", "hungary", "putin", "xi", "jinping", "erdoğan", "erdogan",
        "congress", "shutdown", "tariff", "war"
    }
    if any(term in q for term in politics_terms):
        return True

    return market_type == "narrative_long_tail" and any(term in q for term in {"out by", "before 2027", "by december 31"})


def politics_concentration_state(engine, candidate):
    family_key = candidate.get("family_key")
    cluster = candidate.get("cluster", "unknown")

    total_exposure = 0.0
    cluster_exposure = 0.0
    family_exposure = 0.0
    open_count = 0
    cluster_count = 0
    family_count = 0

    for pos in engine.open_positions:
        pos_exposure = float(pos.get("stake_usd", pos.get("cost_basis_remaining", 0.0)) or 0.0)

        if candidate_is_politics_like(pos):
            total_exposure += pos_exposure
            open_count += 1

        if pos.get("cluster", "unknown") == cluster:
            cluster_exposure += pos_exposure
            cluster_count += 1

        if family_key and pos.get("family_key") == family_key:
            family_exposure += pos_exposure
            family_count += 1

    return {
        "is_politics_like": candidate_is_politics_like(candidate),
        "total_exposure": round(total_exposure, 4),
        "cluster_exposure": round(cluster_exposure, 4),
        "family_exposure": round(family_exposure, 4),
        "open_count": int(open_count),
        "cluster_count": int(cluster_count),
        "family_count": int(family_count),
    }


def is_high_conviction_reason(reason):
    return reason in [
        "multicycle_momentum_override",
        "momentum_override",
        "score+momentum",
        "score+pressure",
        "score+pre_momentum",
    ]


def score_survival_priority(candidate, reason, engine):
    score = float(candidate.get("score", 0.0) or 0.0)
    density = float(candidate.get("pressure_density", 0.0) or 0.0)
    window_delta = abs(float(candidate.get("price_delta_window", 0.0) or 0.0))
    trend = float(candidate.get("price_trend_strength", 0.0) or 0.0)
    theme = candidate.get("theme", "unknown")
    cluster = candidate.get("cluster", "unknown")

    priority = score
    if is_high_conviction_reason(reason):
        priority += 0.30
    if density >= 0.25:
        priority += 0.18
    if window_delta >= 0.006:
        priority += 0.12
    if trend >= 0.85:
        priority += 0.08
    if theme in {"crypto", "politics", "weird"}:
        priority += 0.04

    cluster_exp = float(engine.cluster_exposure().get(cluster, 0.0) or 0.0)
    if cluster_exp >= 5.0:
        priority -= 0.18
    elif cluster_exp >= 4.0:
        priority -= 0.10

    return round(priority, 6)


def detect_market_regime(candidates):
    non_zero_delta = 0
    strong_delta = 0
    pressure_like = 0
    pre_like = 0

    total = max(len(candidates), 1)

    for c in candidates:
        try:
            abs_delta = abs(float(c.get("price_delta", 0.0)))
        except Exception:
            abs_delta = 0.0

        try:
            abs_window = abs(float(c.get("price_delta_window", 0.0)))
        except Exception:
            abs_window = 0.0

        try:
            density = float(c.get("pressure_density", 0.0))
        except Exception:
            density = 0.0

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

    if strong_ratio >= 0.03 or pressure_ratio >= 0.08:
        regime = "hot"
    elif non_zero_ratio >= 0.03 or pre_ratio >= 0.06:
        regime = "normal"
    else:
        regime = "calm"

    return {
        "regime": regime,
        "non_zero_ratio": round(non_zero_ratio, 6),
        "strong_ratio": round(strong_ratio, 6),
        "pressure_ratio": round(pressure_ratio, 6),
        "pre_ratio": round(pre_ratio, 6),
    }


def regime_settings(regime):
    if regime == "hot":
        return {
            "MAX_CYCLE_RISK_USD": 20.0,
            "MAX_THEME_POSITIONS_PER_CYCLE": 4,
            "MAX_CLUSTER_POSITIONS_PER_CYCLE": 3,
            "STAKE_MULTIPLIER": 1.20,
        }

    if regime == "normal":
        return {
            "MAX_CYCLE_RISK_USD": 16.0,
            "MAX_THEME_POSITIONS_PER_CYCLE": 3,
            "MAX_CLUSTER_POSITIONS_PER_CYCLE": 2,
            "STAKE_MULTIPLIER": 1.00,
        }

    return {
        "MAX_CYCLE_RISK_USD": 12.0,
        "MAX_THEME_POSITIONS_PER_CYCLE": 2,
        "MAX_CLUSTER_POSITIONS_PER_CYCLE": 1,
        "STAKE_MULTIPLIER": 0.85,
    }


def default_signal_memory():
    return {
        "score": {"seen": 0, "opened": 0},
        "pre_momentum": {"seen": 0, "opened": 0},
        "pressure": {"seen": 0, "opened": 0},
        "score+pressure": {"seen": 0, "opened": 0},
        "score+pre_momentum": {"seen": 0, "opened": 0},
        "multicycle_momentum_override": {"seen": 0, "opened": 0},
        "momentum_override": {"seen": 0, "opened": 0},
        "score+momentum": {"seen": 0, "opened": 0},
        "momentum": {"seen": 0, "opened": 0},
    }


def signal_confidence(signal_memory, reason):
    stats = signal_memory.get(reason)
    if not stats:
        return 1.0

    seen = float(stats.get("seen", 0))
    opened = float(stats.get("opened", 0))

    if seen <= 0:
        return 1.0

    ratio = opened / seen

    if seen >= 5 and ratio >= 0.60:
        return 1.15
    if seen >= 3 and ratio >= 0.45:
        return 1.10
    if seen >= 2 and ratio >= 0.30:
        return 1.05
    if seen >= 5 and ratio <= 0.10:
        return 0.92

    return 1.0




def clamp(value, min_value, max_value):
    return max(min_value, min(value, max_value))


def compute_entry_quality_multiplier(candidate, reason, engine):
    score = float(candidate.get("score", 0.0) or 0.0)
    density = float(candidate.get("pressure_density", 0.0) or 0.0)
    trend = float(candidate.get("price_trend_strength", 0.0) or 0.0)
    window_delta = abs(float(candidate.get("price_delta_window", 0.0) or 0.0))
    survival = float(score_survival_priority(candidate, reason, engine))

    quality = 1.0

    if score >= 1.40:
        quality += 0.16
    elif score >= 1.10:
        quality += 0.10
    elif score < 0.45:
        quality -= 0.08

    if density >= 0.25:
        quality += 0.08
    elif density <= 0.0 and reason in {"score"}:
        quality -= 0.03

    if trend >= 0.95:
        quality += 0.06
    elif trend >= 0.80:
        quality += 0.03

    if window_delta >= 0.03:
        quality += 0.08
    elif window_delta >= 0.01:
        quality += 0.04

    if reason in {"score+pressure", "score+momentum", "multicycle_momentum_override", "score+pre_momentum"}:
        quality += 0.06
    elif reason in {"score"}:
        quality -= 0.02

    if survival >= 1.60:
        quality += 0.08
    elif survival >= 1.25:
        quality += 0.04
    elif survival < 0.60:
        quality -= 0.08

    return round(clamp(quality, 0.82, 1.35), 4)


def compute_regime_multiplier(current_regime, reason, quality_mult):
    mult = 1.0
    if current_regime == "hot":
        mult = 1.08 if quality_mult >= 1.02 else 0.96
    elif current_regime == "normal":
        mult = 1.00
    else:
        mult = 0.93 if reason == "score" else 0.97
    return round(clamp(mult, 0.88, 1.10), 4)


def compute_cluster_health_multiplier(candidate, engine, cycle_theme_counts, cycle_cluster_counts):
    theme = candidate.get("theme", "unknown")
    cluster = candidate.get("cluster", "unknown")

    theme_exposure = float(engine.theme_exposure().get(theme, 0.0) or 0.0)
    cluster_exposure = float(engine.cluster_exposure().get(cluster, 0.0) or 0.0)

    mult = 1.0

    if cluster_exposure >= 5.0:
        mult *= 0.70
    elif cluster_exposure >= 4.0:
        mult *= 0.82
    elif cluster_exposure >= 3.0:
        mult *= 0.92
    else:
        mult *= 1.03

    if theme_exposure >= 5.0:
        mult *= 0.82
    elif theme_exposure >= 3.5:
        mult *= 0.92

    if cycle_cluster_counts.get(cluster, 0) >= 1:
        mult *= 0.86
    if cycle_theme_counts.get(theme, 0) >= 2:
        mult *= 0.90

    return round(clamp(mult, 0.60, 1.08), 4)




def compute_cluster_risk_multiplier(cluster_exposure, confidence, reason, survival_priority):
    mult = 1.0

    if cluster_exposure >= 5.0:
        mult *= 0.25
    elif cluster_exposure >= 4.5:
        mult *= 0.40
    elif cluster_exposure >= 4.0:
        mult *= 0.55
    elif cluster_exposure >= 3.5:
        mult *= 0.72
    elif cluster_exposure >= 2.5:
        mult *= 0.88

    if confidence < 1.0 and cluster_exposure >= 3.5:
        mult *= 0.85

    if reason == "score" and cluster_exposure >= 3.5:
        mult *= 0.88

    if survival_priority >= 1.35 and cluster_exposure < 4.5:
        mult *= 1.05

    return round(clamp(mult, 0.25, 1.05), 4)

def compute_portfolio_heat_multiplier(engine):
    heat = portfolio_pressure_profile(engine)
    mult = 1.0
    if heat["hard_crowded"]:
        mult = 0.58
    elif heat["stressed"]:
        mult = 0.76
    elif heat["crowded"]:
        mult = 0.90
    return round(mult, 4), heat


def compute_profit_state_multiplier(candidate, reason, engine, quality_mult):
    summary = engine.summary()
    realized = float(summary.get("realized_pnl_total", 0.0) or 0.0)
    equity = float(summary.get("total_equity", engine.starting_balance) or engine.starting_balance)
    unrealized = float(summary.get("unrealized_pnl_total", 0.0) or 0.0)

    mult = 1.0

    if realized > 0.30 and equity > 100.20 and unrealized > -0.15:
        if quality_mult >= 1.05 and reason in {"score+pressure", "score+momentum", "multicycle_momentum_override", "score+pre_momentum"}:
            mult *= 1.08
        elif quality_mult >= 1.00:
            mult *= 1.04

    if realized > 0.60 and equity > 100.50 and quality_mult >= 1.10:
        mult *= 1.05

    if realized <= -0.30 and quality_mult < 1.12:
        mult *= 0.92

    return round(clamp(mult, 0.88, 1.16), 4)



def compute_capital_acceleration_multiplier(candidate, reason, engine, quality_mult, current_regime):
    summary = engine.summary()
    realized = float(summary.get("realized_pnl_total", 0.0) or 0.0)
    unrealized = float(summary.get("unrealized_pnl_total", 0.0) or 0.0)
    equity = float(summary.get("total_equity", engine.starting_balance) or engine.starting_balance)
    open_positions = int(summary.get("open_positions", 0) or 0)
    balance_free = float(summary.get("paper_balance_free", engine.starting_balance) or engine.starting_balance)
    free_ratio = balance_free / max(float(engine.starting_balance), 1.0)

    cluster = candidate.get("cluster", "unknown")
    cluster_exposure = float(engine.cluster_exposure().get(cluster, 0.0) or 0.0)
    survival = float(score_survival_priority(candidate, reason, engine))

    accel = 1.0

    elite_reason = reason in {
        "score+pressure",
        "score+momentum",
        "score+pre_momentum",
        "multicycle_momentum_override",
        "momentum_override",
    }

    healthy_book = (
        open_positions <= 9 and
        free_ratio >= 0.84 and
        cluster_exposure <= 2.8 and
        unrealized > -0.35
    )

    if healthy_book and quality_mult >= 1.12 and survival >= 1.35:
        accel *= 1.06

    if current_regime == "normal" and elite_reason and quality_mult >= 1.18 and survival >= 1.50 and cluster_exposure <= 2.6:
        accel *= 1.07

    if current_regime == "hot" and elite_reason and quality_mult >= 1.10 and survival >= 1.35 and cluster_exposure <= 2.4:
        accel *= 1.08

    if realized > 0.10 and equity >= 99.90 and healthy_book and elite_reason:
        accel *= 1.05

    if realized > 0.25 and equity >= 100.00 and quality_mult >= 1.18 and survival >= 1.55:
        accel *= 1.04

    if unrealized <= -0.28:
        accel *= 0.94

    if open_positions >= 10:
        accel *= 0.94

    return round(clamp(accel, 0.90, 1.24), 4)

def adaptive_stake_plan(candidate, reason, engine, cycle_theme_counts, cycle_cluster_counts, current_regime, confidence):
    base_stake = float(portfolio_adjusted_stake(candidate, reason, engine, cycle_theme_counts, cycle_cluster_counts))
    quality_mult = compute_entry_quality_multiplier(candidate, reason, engine)
    regime_mult = compute_regime_multiplier(current_regime, reason, quality_mult)
    cluster_mult = compute_cluster_health_multiplier(candidate, engine, cycle_theme_counts, cycle_cluster_counts)
    heat_mult, heat_profile = compute_portfolio_heat_multiplier(engine)
    profit_mult = compute_profit_state_multiplier(candidate, reason, engine, quality_mult)
    confidence_mult = round(clamp(float(confidence or 1.0), 0.85, 1.18), 4)

    cluster_name = candidate.get("cluster", "unknown")
    cluster_exposure = float(engine.cluster_exposure().get(cluster_name, 0.0) or 0.0)
    survival_priority = float(score_survival_priority(candidate, reason, engine))
    cluster_risk_mult = compute_cluster_risk_multiplier(
        cluster_exposure,
        confidence_mult,
        reason,
        survival_priority
    )
    accel_mult = compute_capital_acceleration_multiplier(
        candidate,
        reason,
        engine,
        quality_mult,
        current_regime
    )
    market_type = candidate.get("market_type") or detect_market_type(candidate)
    market_type_mult = compute_market_type_multiplier(candidate, reason, current_regime)

    raw_stake = base_stake * quality_mult * regime_mult * cluster_mult * cluster_risk_mult * heat_mult * profit_mult * confidence_mult * accel_mult * market_type_mult
    final_stake = round(clamp(raw_stake, 0.50, 4.20), 2)

    meta = {
        "base_stake": round(base_stake, 4),
        "quality_mult": quality_mult,
        "regime_mult": regime_mult,
        "cluster_mult": cluster_mult,
        "cluster_risk_mult": cluster_risk_mult,
        "cluster_exposure": round(cluster_exposure, 4),
        "politics_like": bool(candidate_is_politics_like(candidate)),
        "politics_exposure": round(float(politics_concentration_state(engine, candidate).get("total_exposure", 0.0) or 0.0), 4),
        "heat_mult": heat_mult,
        "profit_mult": profit_mult,
        "confidence_mult": confidence_mult,
        "accel_mult": accel_mult,
        "market_type": market_type,
        "market_type_mult": market_type_mult,
        "raw_stake": round(raw_stake, 4),
        "final_stake": final_stake,
        "heat_state": (
            "hard_crowded" if heat_profile["hard_crowded"] else
            "stressed" if heat_profile["stressed"] else
            "crowded" if heat_profile["crowded"] else
            "normal"
        ),
    }
    return final_stake, meta

def portfolio_pressure_profile(engine):
    summary = engine.summary()
    open_positions = int(summary.get("open_positions", 0) or 0)
    unrealized = float(summary.get("unrealized_pnl_total", 0.0) or 0.0)
    cluster_exposure = summary.get("cluster_exposure", {}) or {}
    largest_cluster = 0.0
    if cluster_exposure:
        try:
            largest_cluster = max(float(v or 0.0) for v in cluster_exposure.values())
        except Exception:
            largest_cluster = 0.0

    crowded = open_positions >= 9 or largest_cluster >= 3.9
    stressed = open_positions >= 11 or unrealized <= -0.15 or largest_cluster >= 4.8
    hard_crowded = open_positions >= 12 or unrealized <= -0.35 or largest_cluster >= 5.8

    return {
        "summary": summary,
        "open_positions": open_positions,
        "unrealized_pnl_total": unrealized,
        "largest_cluster_exposure": largest_cluster,
        "crowded": crowded,
        "stressed": stressed,
        "hard_crowded": hard_crowded,
    }




def reentry_signal_votes(candidate):
    try:
        score = float(candidate.get("score", 0.0) or 0.0)
        density = float(candidate.get("pressure_density", 0.0) or 0.0)
        pressure_count = int(candidate.get("pressure_count", 0) or 0)
        trend = float(candidate.get("price_trend_strength", 0.0) or 0.0)
        window_delta = abs(float(candidate.get("price_delta_window", 0.0) or 0.0))
        delta = abs(float(candidate.get("price_delta", 0.0) or 0.0))
    except Exception:
        return 0

    votes = 0
    if density >= 0.22:
        votes += 1
    if pressure_count >= 2:
        votes += 1
    if trend >= 0.86:
        votes += 1
    if window_delta >= 0.010:
        votes += 1
    if delta >= 0.007:
        votes += 1
    if score >= 0.97:
        votes += 1
    return votes


def has_strong_reentry_signal(candidate, reason):
    votes = reentry_signal_votes(candidate)
    try:
        score = float(candidate.get("score", 0.0) or 0.0)
        density = float(candidate.get("pressure_density", 0.0) or 0.0)
        trend = float(candidate.get("price_trend_strength", 0.0) or 0.0)
        window_delta = abs(float(candidate.get("price_delta_window", 0.0) or 0.0))
    except Exception:
        score = 0.0
        density = 0.0
        trend = 0.0
        window_delta = 0.0

    elite_reason = reason in {
        "score+pressure",
        "score+momentum",
        "multicycle_momentum_override",
        "momentum_override",
        "score+pre_momentum",
        "pressure",
        "pre_momentum",
        "momentum",
    }

    if votes >= 4:
        return True
    if elite_reason and votes >= 3:
        return True
    if score >= 1.02 and (density >= 0.20 or trend >= 0.90 or window_delta >= 0.012):
        return True
    return False



def selective_overblock_relief_state(candidate, reason, market_exit_memory=None, family_dead_cooldown=None, now_ts=None, current_regime="unknown"):
    try:
        score = float(candidate.get("score", 0.0) or 0.0)
        density = float(candidate.get("pressure_density", 0.0) or 0.0)
        pressure_count = int(candidate.get("pressure_count", 0) or 0)
        trend = float(candidate.get("price_trend_strength", 0.0) or 0.0)
        raw_window_delta = float(candidate.get("price_delta_window", 0.0) or 0.0)
        window_delta = abs(raw_window_delta)
        raw_delta = float(candidate.get("price_delta", 0.0) or 0.0)
        delta = abs(raw_delta)
        market_type = str(candidate.get("market_type", "general_binary") or "general_binary")
        family_key = str(candidate.get("family_key", "") or "")
        delayed_memory = bool(candidate.get("_delayed_entry_memory_active", False) or candidate.get("delayed_entry_memory_active", False))
        delayed_watch = bool(candidate.get("_delayed_entry_watch_active", False))
        delayed_confirmed = bool(candidate.get("_delayed_entry_confirmed", False))
    except Exception:
        return {"considered": False, "active": False, "signal": None, "allow_family_dead": False, "allow_family_reopen": False}

    key = build_market_key(candidate)
    memory = dict((market_exit_memory or {}).get(key, {}) or {}) if market_exit_memory is not None else {}
    structure_votes = int(follow_through_structure_votes(candidate) or 0)
    reentry_votes = int(reentry_signal_votes(candidate) or 0)

    political_override_entry = bool(candidate.get("_political_family_override", candidate.get("political_override_active", False)))
    targeted_override = bool(candidate.get("_political_targeted_override", candidate.get("political_targeted_override", False)))
    balance_rescue = bool(candidate.get("_balance_rescue_override", candidate.get("balance_rescue_override", False)))
    cross_family = bool(candidate.get("_cross_family_thesis_priority", candidate.get("cross_family_thesis_priority", False)))
    hold_window = bool(candidate.get("_political_hold_window", candidate.get("political_hold_window", False)))
    hold_cycles = int(candidate.get("_political_hold_window_cycles", candidate.get("political_hold_window_cycles", 0)) or 0)
    cross_cycles = int(candidate.get("_cross_family_priority_cycles", candidate.get("cross_family_priority_cycles", 0)) or 0)
    corridor = bool(candidate.get("_override_survival_corridor", candidate.get("override_survival_corridor", False)))
    mirror_expected = bool(candidate.get("_flag_mirror_audit_expected", candidate.get("flag_mirror_audit_expected", False)) or (political_override_entry and (targeted_override or balance_rescue or cross_family)))

    family_memory = {}
    if family_key and market_exit_memory is not None:
        try:
            family_memory = dict((market_exit_memory or {}).get(build_family_memory_key(candidate), {}) or {})
        except Exception:
            family_memory = {}

    family_dead = int(family_memory.get("dead_money_exit_count", 0) or 0)
    family_zero = int(family_memory.get("zero_peak_family_count", 0) or 0)
    family_follow = int(family_memory.get("follow_through_fail_count", 0) or 0)
    family_failed_runner = int(family_memory.get("failed_runner_quarantine_count", 0) or 0)
    legal_replay_exit_count = int(memory.get("legal_replay_exit_count", 0) or 0)
    legal_stale_loss_count = int(memory.get("legal_stale_loss_count", 0) or 0)
    failed_runner_quarantine_count = int(memory.get("failed_runner_quarantine_count", 0) or 0)
    legal_false_pressure_quarantine_count = int(memory.get("legal_false_pressure_quarantine_count", 0) or 0)
    family_legal_false_pressure_quarantine_count = int(family_memory.get("legal_false_pressure_quarantine_count", 0) or 0)

    non_flat = bool(
        density >= 0.10
        or pressure_count >= 1
        or trend >= 0.84
        or window_delta >= 0.0010
        or delta >= 0.0010
    )
    strong_non_flat = bool(
        structure_votes >= 3
        or reentry_votes >= 4
        or pressure_count >= 2
        or density >= 0.24
        or trend >= 0.92
        or window_delta >= 0.0045
        or delta >= 0.0045
        or (score >= 1.18 and non_flat and structure_votes >= 2)
    )
    mirror_ready = bool(
        (not mirror_expected)
        or (
            hold_window
            and hold_cycles > 0
            and corridor
            and (not cross_family or cross_cycles > 0)
        )
    )
    considered = bool(
        non_flat
        and (
            political_override_entry
            or delayed_memory
            or delayed_watch
            or family_dead >= 1
            or family_zero >= 1
            or family_follow >= 1
            or legal_replay_exit_count >= 1
            or legal_stale_loss_count >= 1
            or failed_runner_quarantine_count >= 1
            or family_failed_runner >= 1
            or legal_false_pressure_quarantine_count >= 1
            or family_legal_false_pressure_quarantine_count >= 1
        )
    )

    active = False
    signal = None
    if mirror_ready and non_flat and (
        (political_override_entry and (targeted_override or balance_rescue or cross_family) and (score >= 0.96 or strong_non_flat))
        or (
            market_type == "valuation_ladder"
            and score >= 1.52
            and trend >= 0.95
            and density >= 0.12
            and pressure_count >= 1
            and window_delta >= 0.0005
        )
        or (
            delayed_memory
            and market_type in {"general_binary", "narrative_long_tail", "legal_resolution"}
            and (family_dead >= 1 or family_zero >= 1 or family_follow >= 1)
            and strong_non_flat
        )
        or (
            market_type == "legal_resolution"
            and (legal_replay_exit_count >= 1 or legal_stale_loss_count >= 1 or failed_runner_quarantine_count >= 1 or family_failed_runner >= 1 or legal_false_pressure_quarantine_count >= 1 or family_legal_false_pressure_quarantine_count >= 1)
            and delayed_memory
            and strong_non_flat
            and score >= 1.10
            and (raw_window_delta > -0.010 or delayed_confirmed)
        )
    ):
        active = True
        if political_override_entry and (targeted_override or balance_rescue or cross_family):
            signal = "political_mirror_relief"
        elif market_type == "valuation_ladder":
            signal = "valuation_nonflat_relief"
        elif market_type == "legal_resolution":
            signal = "legal_false_pressure_relief"
        else:
            signal = "delayed_family_relief"

    escalation_ready = bool(
        active
        and mirror_ready
        and (
            strong_non_flat
            or (market_type == "valuation_ladder" and score >= 1.48 and trend >= 0.90)
            or (political_override_entry and score >= 0.96 and (targeted_override or balance_rescue or cross_family))
            or (market_type == "legal_resolution" and score >= 1.10 and (pressure_count >= 2 or density >= 0.22 or window_delta >= 0.0040 or delayed_confirmed))
        )
    )
    escalation_cap = float(execution_micro_clamp_cap(candidate, "relief_escalation") if escalation_ready else 0.0)
    force_delayed = bool(
        escalation_ready and not (
            pressure_count >= 2 or density >= 0.18 or structure_votes >= 4 or reentry_votes >= 5 or window_delta >= 0.0030 or delayed_confirmed
        )
    )
    micro_scout = bool(escalation_ready)

    reject_reason = None
    if considered and not active:
        if not mirror_ready:
            reject_reason = "mirror_not_ready"
        elif not non_flat:
            reject_reason = "flat_profile"
        elif market_type == "legal_resolution" and not delayed_memory and not delayed_watch:
            reject_reason = "legal_memory_missing"
        elif market_type == "legal_resolution" and not strong_non_flat:
            reject_reason = "legal_structure_too_weak"
        else:
            reject_reason = "activation_conditions_not_met"

    state = {
        "considered": bool(considered),
        "active": bool(active),
        "signal": signal,
        "reject_reason": reject_reason,
        "allow_family_dead": bool(active),
        "allow_family_reopen": bool(active),
        "allow_dead_market": bool(escalation_ready),
        "allow_stale_market": bool(escalation_ready and (structure_votes >= 2 or reentry_votes >= 4 or delayed_memory)),
        "allow_legal_cooldown": bool(escalation_ready and market_type == "legal_resolution" and strong_non_flat),
        "allow_legal_replay": bool(escalation_ready and market_type == "legal_resolution" and (structure_votes >= 3 or reentry_votes >= 4 or delayed_confirmed or delayed_memory)),
        "mirror_ready": bool(mirror_ready),
        "strong_non_flat": bool(strong_non_flat),
        "non_flat": bool(non_flat),
        "structure_votes": int(structure_votes),
        "reentry_votes": int(reentry_votes),
        "escalation_cap": round(escalation_cap, 4),
        "force_delayed": bool(force_delayed),
        "micro_scout": bool(micro_scout),
    }
    if state["active"]:
        candidate["_selective_overblock_relief_active"] = True
        candidate["_selective_overblock_relief_signal"] = signal
    return state

def prime_relief_escalation(candidate, relief_state, block_name):
    state = dict(relief_state or {})
    if not state.get("active", False):
        return candidate

    signal_root = str(state.get("signal") or "relief_escalation")
    signal = f"{signal_root}:{block_name}"
    cap = float(state.get("escalation_cap", execution_micro_clamp_cap(candidate, "relief_escalation")) or 0.0)
    force_delayed = bool(state.get("force_delayed", False))
    micro_scout = bool(state.get("micro_scout", True) or force_delayed)

    candidate["_selective_overblock_relief_active"] = True
    candidate["_selective_overblock_relief_signal"] = signal_root
    candidate["_relief_escalation_active"] = True
    candidate["_relief_escalation_signal"] = signal
    candidate["_relief_escalation_cap"] = round(float(cap or 0.0), 4)
    candidate["_relief_escalation_force_delayed"] = bool(force_delayed)
    candidate["_relief_escalation_micro_scout"] = bool(micro_scout)
    candidate["_relief_escalation_block"] = str(block_name or "unknown")

    print(
        "TRACE | relief_escalation_route | block={} | signal={} | cap={:.2f} | force_delayed={} | micro_scout={} | score={:.3f} | pd={:.3f} | trend={:.3f} | delta1={:.3f} | win={:.3f} | votes={} | {}".format(
            str(block_name or "unknown"),
            signal,
            float(cap or 0.0),
            int(bool(force_delayed)),
            int(bool(micro_scout)),
            float(candidate.get("score", 0.0) or 0.0),
            float(candidate.get("pressure_density", 0.0) or 0.0),
            float(candidate.get("price_trend_strength", 0.0) or 0.0),
            abs(float(candidate.get("price_delta", 0.0) or 0.0)),
            abs(float(candidate.get("price_delta_window", 0.0) or 0.0)),
            int(state.get("structure_votes", 0) or 0),
            (candidate.get("question") or "")[:96],
        )
    )
    return candidate


def trace_relief_router_state(candidate, relief_state, reason):
    try:
        if bool(candidate.get("_relief_route_traced", False)):
            return
        state = dict(relief_state or {})
        if state.get("active", False):
            print(
                "TRACE | relief_route_seen | reason={} | signal={} | score={:.3f} | pd={:.3f} | trend={:.3f} | delta1={:.3f} | win={:.3f} | votes={} | {}".format(
                    reason,
                    str(state.get("signal") or "relief_escalation"),
                    float(candidate.get("score", 0.0) or 0.0),
                    float(candidate.get("pressure_density", 0.0) or 0.0),
                    float(candidate.get("price_trend_strength", 0.0) or 0.0),
                    abs(float(candidate.get("price_delta", 0.0) or 0.0)),
                    abs(float(candidate.get("price_delta_window", 0.0) or 0.0)),
                    int(state.get("structure_votes", 0) or 0),
                    (candidate.get("question") or "")[:96],
                )
            )
        elif state.get("considered", False):
            print(
                "TRACE | relief_route_reject | reason={} | reject_reason={} | score={:.3f} | pd={:.3f} | trend={:.3f} | delta1={:.3f} | win={:.3f} | votes={} | {}".format(
                    reason,
                    str(state.get("reject_reason") or "activation_conditions_not_met"),
                    float(candidate.get("score", 0.0) or 0.0),
                    float(candidate.get("pressure_density", 0.0) or 0.0),
                    float(candidate.get("price_trend_strength", 0.0) or 0.0),
                    abs(float(candidate.get("price_delta", 0.0) or 0.0)),
                    abs(float(candidate.get("price_delta_window", 0.0) or 0.0)),
                    int(state.get("structure_votes", 0) or 0),
                    (candidate.get("question") or "")[:96],
                )
            )
        candidate["_relief_route_traced"] = True
    except Exception:
        return


def elite_recovery_override_state(candidate, reason, market_exit_memory):
    try:
        key = build_market_key(candidate)
        memory = dict((market_exit_memory or {}).get(key, {}) or {})
        family_memory_key = build_family_memory_key(candidate)
        family_memory = dict((market_exit_memory or {}).get(family_memory_key, {}) or {}) if family_memory_key else {}

        score = float(candidate.get("score", 0.0) or 0.0)
        density = float(candidate.get("pressure_density", 0.0) or 0.0)
        pressure_count = int(candidate.get("pressure_count", 0) or 0)
        trend = float(candidate.get("price_trend_strength", 0.0) or 0.0)
        window_delta = abs(float(candidate.get("price_delta_window", 0.0) or 0.0))
        delta = abs(float(candidate.get("price_delta", 0.0) or 0.0))
        market_type = str(candidate.get("market_type", "general_binary") or "general_binary")
        delayed_confirmed = bool(candidate.get("_delayed_entry_confirmed", False))
        delayed_memory = bool(candidate.get("delayed_entry_memory_active", False) or candidate.get("_delayed_entry_memory_active", False))
        delayed_watch = bool(candidate.get("_delayed_entry_watch_active", False))

        votes = int(reentry_signal_votes(candidate) or 0)
        structure_votes = int(follow_through_structure_votes(candidate) or 0)

        weak_failed = int(memory.get("weak_failed_count", 0) or 0)
        dead_hits = int(memory.get("dead_exit_count", 0) or 0)
        stale_hits = int(memory.get("stale_exit_count", 0) or 0)
        legal_replay_exit_count = int(memory.get("legal_replay_exit_count", 0) or 0)
        legal_stale_loss_count = int(memory.get("legal_stale_loss_count", 0) or 0)
        failed_runner_quarantine_count = int(memory.get("failed_runner_quarantine_count", 0) or 0)
        family_dead = int(family_memory.get("dead_money_exit_count", 0) or 0)
        family_follow = int(family_memory.get("follow_through_fail_count", 0) or 0)
        family_zero = int(family_memory.get("zero_peak_family_count", 0) or 0)
        family_brake = int(family_memory.get("family_reopen_brake_count", 0) or 0)
        family_failed_runner = int(family_memory.get("failed_runner_quarantine_count", 0) or 0)
        last_failed_score = float(memory.get("last_failed_score", family_memory.get("last_failed_score", 0.0)) or 0.0)
        last_failed_votes = int(memory.get("last_failed_structure_votes", family_memory.get("last_failed_structure_votes", 0)) or 0)
        last_exit_reason = str(memory.get("last_exit_reason", family_memory.get("last_exit_reason", "")) or "")

        memory_pressure = (
            weak_failed
            + dead_hits
            + stale_hits
            + legal_replay_exit_count
            + legal_stale_loss_count
            + min(failed_runner_quarantine_count, 2)
            + min(family_dead, 2)
            + min(family_follow, 2)
            + min(family_zero, 2)
            + min(family_brake, 2)
            + min(family_failed_runner, 2)
        )
        if memory_pressure <= 0 and not delayed_memory and not delayed_confirmed and not delayed_watch:
            return {"active": False}

        strong_core = (
            pressure_count >= 2
            or (density >= 0.16 and pressure_count >= 1)
            or density >= 0.20
            or (trend >= 0.88 and (window_delta >= 0.0025 or delta >= 0.0025))
            or window_delta >= 0.0038
            or delta >= 0.0038
        )
        elite_jump = (
            score >= max(1.10, last_failed_score + 0.08)
            and (
                structure_votes >= max(3, last_failed_votes + 1)
                or votes >= max(5, last_failed_votes + 2)
            )
        )
        legal_elite = (
            market_type == "legal_resolution"
            and score >= max(1.08, last_failed_score + 0.05)
            and (
                (density >= 0.16 and pressure_count >= 2)
                or (trend >= 0.90 and window_delta >= 0.0025)
                or window_delta >= 0.0065
                or delta >= 0.0040
                or (delayed_memory and score >= 1.18)
                or (delayed_confirmed and score >= 1.12)
            )
        )
        catalyst_elite = (
            market_type in {"short_burst_catalyst", "speculative_hype", "valuation_ladder"}
            and score >= max(1.12 if market_type != "short_burst_catalyst" else 1.16, last_failed_score + 0.06)
            and (
                pressure_count >= 2
                or density >= 0.18
                or (trend >= 0.90 and (window_delta >= 0.0025 or delta >= 0.0025))
                or window_delta >= 0.0050
                or delta >= 0.0050
                or delayed_confirmed
                or (delayed_memory and votes >= 4)
            )
        )
        failed_runner_recovery = (
            (failed_runner_quarantine_count >= 1 or family_failed_runner >= 1)
            and score >= max(1.14 if market_type != "short_burst_catalyst" else 1.18, last_failed_score + 0.05)
            and (
                pressure_count >= 2
                or density >= 0.20
                or (trend >= 0.92 and window_delta >= 0.0030)
                or window_delta >= 0.0060
                or delayed_confirmed
            )
            and votes >= 4
        )
        broad_elite = (
            score >= max(1.04, last_failed_score + 0.06)
            and votes >= 4
            and strong_core
        )
        watch_revival = (
            delayed_watch
            and score >= max(1.03, last_failed_score + 0.04)
            and votes >= 4
            and (pressure_count >= 1 or density >= 0.16 or window_delta >= 0.0030 or trend >= 0.90)
        )
        delayed_memory_revival = (
            delayed_memory
            and score >= max(1.02, last_failed_score + 0.04)
            and votes >= 3
            and strong_core
        )
        family_revival = (
            (family_dead >= 1 or family_follow >= 1 or family_zero >= 1 or family_brake >= 1 or family_zero_churn_hits >= 1)
            and score >= max(1.06, last_failed_score + 0.05)
            and (structure_votes >= max(3, last_failed_votes + 1) or (votes >= 4 and strong_core))
        )

        active = bool(delayed_confirmed or legal_elite or catalyst_elite or failed_runner_recovery or elite_jump or broad_elite or watch_revival or delayed_memory_revival or family_revival)
        if not active:
            return {"active": False}

        signal = "elite_recovery_override"
        if delayed_confirmed:
            signal = "elite_recovery_delayed_confirmed"
        elif legal_elite:
            signal = "elite_recovery_legal"
        elif failed_runner_recovery:
            signal = "elite_recovery_failed_runner"
        elif catalyst_elite:
            signal = "elite_recovery_catalyst"
        elif elite_jump:
            signal = "elite_recovery_jump"
        elif watch_revival:
            signal = "elite_recovery_watch_revival"
        elif delayed_memory_revival:
            signal = "elite_recovery_delayed_memory"
        elif family_revival:
            signal = "elite_recovery_family_revival"

        return {
            "active": True,
            "signal": signal,
            "score": score,
            "density": density,
            "pressure_count": pressure_count,
            "trend": trend,
            "window_delta": window_delta,
            "delta": delta,
            "votes": votes,
            "structure_votes": structure_votes,
            "memory_pressure": memory_pressure,
            "market_type": market_type,
            "last_failed_score": last_failed_score,
            "last_failed_votes": last_failed_votes,
            "last_exit_reason": last_exit_reason,
        }
    except Exception:
        return {"active": False}


def should_block_stale_reopen(candidate, reason, stale_reentry_cooldown, market_exit_memory):
    key = build_market_key(candidate)
    memory = dict(market_exit_memory.get(key, {}) or {})
    stale_hits = int(memory.get("stale_exit_count", 0) or 0)
    last_exit_reason = memory.get("last_exit_reason")

    if key in stale_reentry_cooldown and not has_strong_reentry_signal(candidate, reason):
        return True, "stale_reentry_cooldown"

    if last_exit_reason in {"time_decay_exit", "time_stale_exit", "idle_hard_exit", "opportunity_cost_decay"}:
        if reason == "score" and not has_strong_reentry_signal(candidate, reason):
            return True, "stale_reopen_score_block"

    if stale_hits >= 2 and reentry_signal_votes(candidate) < 4:
        return True, "stale_market_memory"

    return False, None


def should_block_speculative_hype_reopen(candidate, reason, market_exit_memory):
    try:
        market_type = str(candidate.get("market_type", "general_binary") or "general_binary")
        if market_type not in {"speculative_hype", "short_burst_catalyst"}:
            return False, None

        key = build_market_key(candidate)
        memory = dict(market_exit_memory.get(key, {}) or {})
        last_exit_reason = str(memory.get("last_exit_reason", "") or "")
        weak_failed = int(memory.get("weak_failed_count", 0) or 0)
        stale_hits = int(memory.get("stale_exit_count", 0) or 0)
        spec_brake = int(memory.get("speculative_reopen_brake_count", 0) or 0)

        score = float(candidate.get("score", 0.0) or 0.0)
        density = float(candidate.get("pressure_density", 0.0) or 0.0)
        pressure_count = int(candidate.get("pressure_count", 0) or 0)
        trend = float(candidate.get("price_trend_strength", 0.0) or 0.0)
        window_delta = abs(float(candidate.get("price_delta_window", 0.0) or 0.0))
        delta1 = abs(float(candidate.get("price_delta", 0.0) or 0.0))
        delayed = bool(candidate.get("_delayed_entry_memory_active", False) or candidate.get("delayed_entry_memory_active", False))

        elite_override = elite_recovery_override_state(candidate, reason, market_exit_memory)
        strong_recovery = bool(
            elite_override.get("active", False)
            or (
                score >= (1.44 if market_type == "short_burst_catalyst" else 1.26)
                and (
                    pressure_count >= 2
                    or density >= 0.20
                    or (trend >= 0.92 and (window_delta >= 0.0025 or delta1 >= 0.0025))
                    or window_delta >= 0.0040
                    or delta1 >= 0.0040
                    or delayed
                )
            )
        )
        flat_reopen = (
            pressure_count <= 1
            and density <= 0.14
            and trend < 0.90
            and window_delta <= 0.0015
            and delta1 <= 0.0015
        )

        toxic_last_exit = last_exit_reason in {"peak_zero_kill", "zero_peak_scout_cut", "time_stale_exit", "general_zero_peak_stall_cut", "follow_through_compression_fail", "delayed_admission_fail"}
        if toxic_last_exit and flat_reopen and not strong_recovery:
            return True, "speculative_hype_reopen_brake"

        if (spec_brake >= 1 or stale_hits >= 1 or weak_failed >= 2) and score < (1.50 if market_type == "short_burst_catalyst" else 1.30) and not strong_recovery:
            return True, "speculative_hype_memory_brake"

        return False, None
    except Exception:
        return False, None


def follow_through_structure_votes(candidate):
    try:
        score = float(candidate.get("score", 0.0) or 0.0)
        density = float(candidate.get("pressure_density", 0.0) or 0.0)
        pressure_count = int(candidate.get("pressure_count", 0) or 0)
        trend = float(candidate.get("price_trend_strength", 0.0) or 0.0)
        window_delta = abs(float(candidate.get("price_delta_window", 0.0) or 0.0))
        delta = abs(float(candidate.get("price_delta", 0.0) or 0.0))
    except Exception:
        return 0

    votes = 0
    if density >= 0.16:
        votes += 1
    if pressure_count >= 2 or (pressure_count >= 1 and density >= 0.20 and (window_delta >= 0.0030 or delta >= 0.0030)):
        votes += 1
    if trend >= 0.84:
        votes += 1
    if window_delta >= 0.0030:
        votes += 1
    if delta >= 0.0030:
        votes += 1
    if score >= 1.00:
        votes += 1
    return votes


def thin_pressure_truth_state(candidate, reason, current_regime, market_exit_memory):
    try:
        score = float(candidate.get("score", 0.0) or 0.0)
        density = float(candidate.get("pressure_density", 0.0) or 0.0)
        pressure_count = int(candidate.get("pressure_count", 0) or 0)
        trend = float(candidate.get("price_trend_strength", 0.0) or 0.0)
        window_delta = abs(float(candidate.get("price_delta_window", 0.0) or 0.0))
        delta = abs(float(candidate.get("price_delta", 0.0) or 0.0))
        market_type = str(candidate.get("market_type", "general_binary") or "general_binary")
        source = str(candidate.get("_universe_source") or candidate.get("_entry_source") or candidate.get("entry_source") or "primary")
    except Exception:
        return {"active": False, "signal": None}

    supported_reasons = {
        "pressure",
        "score+pressure",
        "score+momentum",
        "score",
        "pre_momentum",
        "score+pre_momentum",
        "multicycle_momentum_override",
        "momentum_override",
    }
    if reason not in supported_reasons:
        return {"active": False, "signal": None}

    structure_votes = int(follow_through_structure_votes(candidate) or 0)
    market_key = build_market_key(candidate)
    family_memory_key = build_family_memory_key(candidate)
    memory = dict((market_exit_memory or {}).get(market_key, {}) or {})
    family_memory = dict((market_exit_memory or {}).get(family_memory_key, {}) or {}) if family_memory_key else {}

    market_dead = int(memory.get("dead_money_exit_count", 0) or 0)
    market_follow = int(memory.get("follow_through_fail_count", 0) or 0)
    market_zero = int(memory.get("zero_peak_family_count", 0) or 0)
    family_dead = int(family_memory.get("dead_money_exit_count", 0) or 0)
    family_follow = int(family_memory.get("follow_through_fail_count", 0) or 0)
    family_zero = int(family_memory.get("zero_peak_family_count", 0) or 0)
    family_brake = int(family_memory.get("family_reopen_brake_count", 0) or 0)

    delayed_confirmed = bool(candidate.get("_delayed_entry_confirmed", False) or candidate.get("_delayed_entry_light", False))
    delayed_memory = bool(candidate.get("_delayed_entry_memory_active", False) or candidate.get("delayed_entry_memory_active", False))
    cooldown_context = bool(candidate.get("cooldown_active", False) or candidate.get("stale_reentry_cooldown_active", False) or candidate.get("score_reentry_cooldown_active", False))
    political_override = bool(candidate.get("_political_family_override", False))
    override_strength = float(candidate.get("_political_override_strength", 0.0) or 0.0)

    pressure_like = reason in {"pressure", "score+pressure", "score+momentum"}
    impulse_like = reason in {"score", "pre_momentum", "score+pre_momentum", "multicycle_momentum_override", "momentum_override"}
    risky_market = market_type in {
        "legal_resolution",
        "valuation_ladder",
        "scheduled_binary_event",
        "short_burst_catalyst",
        "speculative_hype",
        "sports_award_longshot",
        "narrative_long_tail",
    }

    truthless_pressure = (
        pressure_like
        and pressure_count <= 1
        and density <= 0.16
        and window_delta <= 0.0060
        and delta <= 0.0060
    )
    fake_pressure_burst = (
        pressure_like
        and trend >= 0.96
        and pressure_count <= 1
        and density <= 0.14
        and window_delta <= 0.0025
        and delta <= 0.0025
    )
    soft_pressure = (
        pressure_like
        and score < 1.10
        and pressure_count <= 1
        and density <= 0.20
        and trend < 0.92
        and window_delta <= 0.0055
        and delta <= 0.0055
    )
    thin_impulse = (
        impulse_like
        and pressure_count <= 1
        and density <= 0.14
        and trend >= 0.96
        and window_delta <= 0.0025
        and delta <= 0.0025
    )
    weak_pressure_loser = (
        score < 1.02
        and pressure_count <= 1
        and density <= 0.18
        and trend < 0.90
        and window_delta <= 0.0055
        and delta <= 0.0055
    )

    strong_truth = (
        delayed_confirmed
        or structure_votes >= 4
        or pressure_count >= 2
        or density >= 0.22
        or window_delta >= 0.0065
        or delta >= 0.0065
        or (trend >= 0.96 and (window_delta >= 0.0045 or delta >= 0.0045))
    )
    override_exception = political_override and override_strength >= 0.34 and strong_truth

    truth_risk = 0
    if risky_market:
        truth_risk += 1
    if pressure_like:
        truth_risk += 1
    if truthless_pressure:
        truth_risk += 2
    if fake_pressure_burst:
        truth_risk += 2
    if soft_pressure:
        truth_risk += 1
    if thin_impulse:
        truth_risk += 1
    if weak_pressure_loser:
        truth_risk += 1
    if current_regime == "calm":
        truth_risk += 1
    if cooldown_context or delayed_memory:
        truth_risk += 1
    if market_dead >= 1 or market_follow >= 1 or market_zero >= 1:
        truth_risk += 1
    if family_dead >= 2 or family_zero >= 1 or family_follow >= 1 or family_brake >= 1:
        truth_risk += 1

    if strong_truth or override_exception:
        truth_risk = max(0, truth_risk - 2)

    hard_block = (
        truth_risk >= 6
        and not strong_truth
        and not delayed_confirmed
        and not override_exception
    )

    can_delay = source != "explorer" and reason in {"pressure", "score+pressure", "score+momentum", "score", "pre_momentum", "score+pre_momentum"}
    force_delayed = (
        can_delay
        and not hard_block
        and truth_risk >= 3
        and not delayed_confirmed
    )

    scout_mode = bool((truth_risk >= 2 and not hard_block) or family_brake >= 1)
    signal = None
    cap = 0.0
    if hard_block:
        signal = "thin_pressure_truth_hard_block"
    elif force_delayed:
        signal = "thin_pressure_truth_force_delayed"
    elif scout_mode:
        signal = "thin_pressure_truth_scout_cap"
        if market_type == "sports_award_longshot":
            cap = 0.68 if current_regime != "hot" else 0.78
        elif market_type in {"legal_resolution", "scheduled_binary_event"}:
            cap = 0.82 if current_regime != "hot" else 0.92
        elif market_type in {"short_burst_catalyst", "speculative_hype", "valuation_ladder"}:
            cap = 0.86 if current_regime != "hot" else 0.96
        else:
            cap = 0.92 if current_regime != "hot" else 1.02
        if strong_truth:
            cap += 0.06
        cap = round(min(cap, 1.08), 4)

    return {
        "active": bool(signal and not hard_block),
        "hard_block": bool(hard_block),
        "force_delayed": bool(force_delayed),
        "scout_mode": bool(scout_mode and not hard_block),
        "signal": signal,
        "cap": float(cap or 0.0),
        "risk_score": int(truth_risk),
        "structure_votes": int(structure_votes),
        "family_dead_money": int(family_dead),
        "family_zero_peak": int(family_zero),
        "family_follow_fail": int(family_follow),
        "truthless_pressure": bool(truthless_pressure),
        "fake_pressure_burst": bool(fake_pressure_burst),
        "soft_pressure": bool(soft_pressure),
        "thin_impulse": bool(thin_impulse),
        "score": score,
        "density": density,
        "pressure_count": pressure_count,
        "trend": trend,
        "window_delta": window_delta,
        "delta": delta,
    }


def should_block_family_reopen_brake(candidate, reason, market_exit_memory):
    try:
        family_memory_key = build_family_memory_key(candidate)
        if not family_memory_key:
            return False, None
        family_memory = dict((market_exit_memory or {}).get(family_memory_key, {}) or {})
        if not family_memory:
            return False, None

        score = float(candidate.get("score", 0.0) or 0.0)
        density = float(candidate.get("pressure_density", 0.0) or 0.0)
        pressure_count = int(candidate.get("pressure_count", 0) or 0)
        trend = float(candidate.get("price_trend_strength", 0.0) or 0.0)
        window_delta = abs(float(candidate.get("price_delta_window", 0.0) or 0.0))
        delta = abs(float(candidate.get("price_delta", 0.0) or 0.0))
        market_type = str(candidate.get("market_type", "general_binary") or "general_binary")

        structure_votes = int(follow_through_structure_votes(candidate) or 0)
        family_dead = int(family_memory.get("dead_money_exit_count", 0) or 0)
        family_zero = int(family_memory.get("zero_peak_family_count", 0) or 0)
        family_follow = int(family_memory.get("follow_through_fail_count", 0) or 0)
        family_brake = int(family_memory.get("family_reopen_brake_count", 0) or 0)
        last_exit_reason = str(family_memory.get("last_exit_reason", "") or "")
        last_failed_votes = int(family_memory.get("last_failed_structure_votes", 0) or 0)
        last_failed_score = float(family_memory.get("last_failed_score", 0.0) or 0.0)

        toxic_last_exit = last_exit_reason in {
            "follow_through_compression_fail",
            "delayed_admission_fail",
            "no_follow_through_exit",
            "peak_zero_kill",
            "zero_peak_scout_cut",
            "normal_zero_peak_linger_cut",
            "general_zero_peak_stall_cut",
            "sports_zero_peak_fire_exit",
            "sports_longshot_churn_kill",
            "sports_zombie_guillotine_exit",
            "calm_zero_peak_general_cut",
            "calm_legal_zero_peak_cut",
            "political_pre_momentum_compression_exit",
            "time_decay_exit",
            "dead_capital_decay",
        }

        weak_reopen = (
            pressure_count <= 1
            and density <= 0.18
            and trend < 0.94
            and window_delta <= 0.0060
            and delta <= 0.0060
        )
        elite_override = elite_recovery_override_state(candidate, reason, market_exit_memory)
        strong_recovery = bool(
            elite_override.get("active", False)
            or bool(candidate.get("_delayed_entry_confirmed", False))
            or structure_votes >= max(4, last_failed_votes + 1)
            or pressure_count >= 2
            or density >= 0.24
            or window_delta >= 0.0065
            or delta >= 0.0065
            or (score >= max(1.18, last_failed_score + 0.10) and structure_votes >= 3)
        )
        same_or_weaker = (
            structure_votes <= max(2, last_failed_votes)
            and score <= max(1.10, last_failed_score + 0.06)
        )

        if toxic_last_exit and weak_reopen and same_or_weaker and not strong_recovery:
            return True, "family_reopen_brake"

        if (family_dead >= 3 or family_zero >= 2 or family_follow >= 2 or family_brake >= 2) and not strong_recovery and weak_reopen:
            return True, "family_reopen_memory_brake"

        if market_type in {"speculative_hype", "short_burst_catalyst", "valuation_ladder", "legal_resolution"} and family_brake >= 1 and not strong_recovery and score < max(1.16, last_failed_score + 0.08):
            return True, "family_reopen_truth_gate"

        return False, None
    except Exception:
        return False, None


def should_block_zero_churn_guillotine(candidate, reason, market_exit_memory):
    try:
        key = build_market_key(candidate)
        memory = dict((market_exit_memory or {}).get(key, {}) or {})
        family_memory_key = build_family_memory_key(candidate)
        family_memory = dict((market_exit_memory or {}).get(family_memory_key, {}) or {}) if family_memory_key else {}

        zero_hits = int(memory.get("zero_churn_exit_count", 0) or 0)
        family_zero_hits = int(family_memory.get("zero_churn_exit_count", 0) or 0)
        zero_brake = int(memory.get("zero_churn_reopen_brake_count", 0) or 0)
        family_zero_brake = int(family_memory.get("zero_churn_reopen_brake_count", 0) or 0)
        if zero_hits <= 0 and family_zero_hits <= 0 and zero_brake <= 0 and family_zero_brake <= 0:
            return False, None

        score = float(candidate.get("score", 0.0) or 0.0)
        density = float(candidate.get("pressure_density", 0.0) or 0.0)
        pressure_count = int(candidate.get("pressure_count", 0) or 0)
        trend = float(candidate.get("price_trend_strength", 0.0) or 0.0)
        window_delta = abs(float(candidate.get("price_delta_window", 0.0) or 0.0))
        delta = abs(float(candidate.get("price_delta", 0.0) or 0.0))
        votes = int(reentry_signal_votes(candidate) or 0)
        structure_votes = int(follow_through_structure_votes(candidate) or 0)
        delayed_confirmed = bool(candidate.get("_delayed_entry_confirmed", False))
        market_type = str(candidate.get("market_type", "general_binary") or "general_binary")
        last_zero_score = float(memory.get("last_zero_churn_score", family_memory.get("last_zero_churn_score", 0.0)) or 0.0)
        last_zero_votes = int(memory.get("last_zero_churn_votes", family_memory.get("last_zero_churn_votes", 0)) or 0)

        recovery_router = observable_recovery_router_state(candidate, reason, market_exit_memory)
        strong_recovery = bool(recovery_router.get("active", False) or delayed_confirmed or pressure_count >= 2 or density >= 0.22 or trend >= 0.94
            or window_delta >= 0.0055 or delta >= 0.0055 or structure_votes >= max(4, last_zero_votes + 1)
            or (score >= max(1.14, last_zero_score + 0.08) and votes >= max(4, last_zero_votes + 1)))
        weak_reopen = bool(reason in {"score", "pressure", "score+pressure", "pre_momentum", "score+pre_momentum", "multicycle_momentum_override", "score+momentum", "momentum_override"}
            and pressure_count <= 1 and density <= 0.18 and trend < 0.92 and window_delta <= 0.0045 and delta <= 0.0045 and votes < 5)
        same_or_weaker = bool(structure_votes <= max(3, last_zero_votes) and score <= max(1.10, last_zero_score + 0.06))

        if not strong_recovery and weak_reopen and same_or_weaker:
            return True, "zero_churn_guillotine"
        if not strong_recovery and (zero_hits >= 2 or family_zero_hits >= 2 or zero_brake >= 1 or family_zero_brake >= 2) and score < max(1.18, last_zero_score + 0.08):
            return True, "zero_churn_memory_brake"
        if market_type in {"general_binary", "narrative_long_tail", "valuation_ladder", "sports_award_longshot"} and not strong_recovery and family_zero_hits >= 1 and density <= 0.22 and pressure_count <= 1 and trend < 0.94:
            return True, "zero_churn_family_truth_gate"
        return False, None
    except Exception:
        return False, None


def follow_through_dead_money_state(candidate, reason, current_regime, market_exit_memory):
    try:
        score = float(candidate.get("score", 0.0) or 0.0)
        density = float(candidate.get("pressure_density", 0.0) or 0.0)
        pressure_count = int(candidate.get("pressure_count", 0) or 0)
        trend = float(candidate.get("price_trend_strength", 0.0) or 0.0)
        window_delta = abs(float(candidate.get("price_delta_window", 0.0) or 0.0))
        delta = abs(float(candidate.get("price_delta", 0.0) or 0.0))
        market_type = str(candidate.get("market_type", "general_binary") or "general_binary")
        source = str(candidate.get("_universe_source") or candidate.get("_entry_source") or candidate.get("entry_source") or "primary")
        political_override = bool(candidate.get("_political_family_override", False))
        targeted_override = bool(candidate.get("_political_targeted_override", False))
        balance_rescue = bool(candidate.get("_balance_rescue_override", False))
        override_strength = float(candidate.get("_political_override_strength", 0.0) or 0.0)
    except Exception:
        return {"active": False, "signal": None}

    supported_reasons = {
        "score",
        "pre_momentum",
        "score+pre_momentum",
        "pressure",
        "score+pressure",
        "multicycle_momentum_override",
        "momentum_override",
    }
    if reason not in supported_reasons:
        return {"active": False, "signal": None}

    structure_votes = int(follow_through_structure_votes(candidate) or 0)
    market_key = build_market_key(candidate)
    memory = dict((market_exit_memory or {}).get(market_key, {}) or {})
    family_memory_key = build_family_memory_key(candidate)
    family_memory = dict((market_exit_memory or {}).get(family_memory_key, {}) or {}) if family_memory_key else {}

    dead_money_exit_count = int(memory.get("dead_money_exit_count", 0) or 0)
    follow_through_fail_count = int(memory.get("follow_through_fail_count", 0) or 0)
    zero_peak_family_count = int(memory.get("zero_peak_family_count", 0) or 0)
    thin_impulse_fail_count = int(memory.get("thin_impulse_fail_count", 0) or 0)
    weak_failed_count = int(memory.get("weak_failed_count", 0) or 0)
    last_exit_reason = str(memory.get("last_exit_reason", "") or "")
    family_dead_money_exit_count = int(family_memory.get("dead_money_exit_count", 0) or 0)
    family_follow_through_fail_count = int(family_memory.get("follow_through_fail_count", 0) or 0)
    family_zero_peak_family_count = int(family_memory.get("zero_peak_family_count", 0) or 0)
    family_reopen_brake_count = int(family_memory.get("family_reopen_brake_count", 0) or 0)

    memory_pressure = (
        dead_money_exit_count
        + follow_through_fail_count
        + zero_peak_family_count
        + thin_impulse_fail_count
        + min(weak_failed_count, 2)
        + min(family_dead_money_exit_count, 2)
        + min(family_follow_through_fail_count, 2)
        + min(family_zero_peak_family_count, 2)
        + min(family_reopen_brake_count, 1)
    )

    thin_structure = (
        density < 0.18
        and pressure_count <= 1
        and trend < 0.90
        and window_delta < 0.0050
        and delta < 0.0050
    )
    fake_trend = (
        trend >= 0.94
        and density < 0.14
        and pressure_count <= 1
        and window_delta < 0.0025
        and delta < 0.0025
    )
    passive_pressure = (
        reason in {"pressure", "score+pressure"}
        and density >= 0.24
        and pressure_count >= 2
        and window_delta <= 0.0025
        and delta <= 0.0030
        and trend < 0.82
    )
    thin_impulse = (
        reason in {"score", "pre_momentum", "score+pre_momentum", "multicycle_momentum_override", "momentum_override"}
        and structure_votes <= 1
        and window_delta < 0.0035
        and delta < 0.0035
    )
    low_follow_through = (
        score < 1.12
        and density < 0.20
        and pressure_count <= 1
        and trend < 0.92
        and window_delta < 0.0065
    )

    promising = (
        structure_votes >= 3
        or density >= 0.20
        or pressure_count >= 2
        or window_delta >= 0.006
        or delta >= 0.006
        or (trend >= 0.90 and score >= 1.02)
    )
    elite_exception = (
        score >= 1.18
        and promising
        and (density >= 0.20 or trend >= 0.94 or window_delta >= 0.008)
    )
    override_exception = (
        political_override
        and (
            (targeted_override and override_strength >= 0.32)
            or (balance_rescue and override_strength >= 0.30)
            or override_strength >= 0.36
        )
        and promising
    )
    if elite_exception or override_exception:
        return {
            "active": False,
            "signal": None,
            "risk_score": 0,
            "structure_votes": structure_votes,
            "memory_pressure": memory_pressure,
        }

    risk_score = 0
    if market_type in {"general_binary", "narrative_long_tail", "valuation_ladder", "sports_award_longshot", "speculative_hype", "short_burst_catalyst"}:
        risk_score += 1
    if reason in {"score", "pre_momentum", "score+pre_momentum", "multicycle_momentum_override", "momentum_override"}:
        risk_score += 1
    if thin_structure:
        risk_score += 2
    if fake_trend:
        risk_score += 2
    if passive_pressure:
        risk_score += 2
    if thin_impulse:
        risk_score += 1
    if low_follow_through:
        risk_score += 1
    if current_regime == "calm":
        risk_score += 1
    elif current_regime == "normal" and market_type in {"general_binary", "narrative_long_tail", "sports_award_longshot"}:
        risk_score += 1
    if memory_pressure >= 2:
        risk_score += 1
    if memory_pressure >= 4:
        risk_score += 1
    if last_exit_reason in {
        "follow_through_compression_fail",
        "delayed_admission_fail",
        "no_follow_through_exit",
        "peak_zero_kill",
        "zero_peak_scout_cut",
        "normal_zero_peak_linger_cut",
        "general_zero_peak_stall_cut",
        "sports_zero_peak_fire_exit",
        "sports_longshot_churn_kill",
        "sports_zombie_guillotine_exit",
        "time_decay_exit",
        "dead_capital_decay",
    }:
        risk_score += 1

    hard_block = (
        memory_pressure >= 4
        and risk_score >= 7
        and not promising
        and not bool(candidate.get("_delayed_entry_confirmed", False))
    )

    delay_reasons = {
        "score",
        "pre_momentum",
        "score+pre_momentum",
        "pressure",
        "score+pressure",
        "multicycle_momentum_override",
        "momentum_override",
    }
    can_delay = source != "explorer" and reason in delay_reasons
    force_delayed = (
        can_delay
        and not hard_block
        and risk_score >= 4
        and not bool(candidate.get("_delayed_entry_confirmed", False))
        and not bool(candidate.get("_delayed_entry_light", False))
    )

    scout_mode = bool((risk_score >= 3 and not hard_block) or memory_pressure >= 3)
    cap = 0.0
    signal = None
    if hard_block:
        signal = "follow_through_dead_money_hard_block"
    elif force_delayed:
        signal = "follow_through_force_delayed"
    elif scout_mode:
        signal = "follow_through_scout_cap"
        if market_type == "sports_award_longshot":
            cap = 0.72 if current_regime != "hot" else 0.82
        elif market_type in {"narrative_long_tail", "general_binary"}:
            cap = 0.92 if current_regime != "hot" else 1.02
        elif market_type in {"valuation_ladder", "speculative_hype", "short_burst_catalyst"}:
            cap = 1.02 if current_regime != "hot" else 1.12
        else:
            cap = 1.05
        if promising:
            cap += 0.08
        cap = round(min(cap, 1.18), 4)
    elif risk_score >= 2:
        signal = "follow_through_compression"
        cap = 1.10 if current_regime != "hot" else 1.20
        if market_type == "sports_award_longshot":
            cap = 0.90 if current_regime != "hot" else 1.00
        cap = round(cap, 4)

    return {
        "active": bool(signal and not hard_block),
        "hard_block": bool(hard_block),
        "force_delayed": bool(force_delayed),
        "scout_mode": bool(scout_mode and not hard_block),
        "cap": float(cap or 0.0),
        "signal": signal,
        "risk_score": int(risk_score),
        "structure_votes": int(structure_votes),
        "memory_pressure": int(memory_pressure),
        "dead_money_exit_count": int(dead_money_exit_count),
        "follow_through_fail_count": int(follow_through_fail_count),
        "zero_peak_family_count": int(zero_peak_family_count),
        "thin_impulse_fail_count": int(thin_impulse_fail_count),
        "thin_structure": bool(thin_structure),
        "fake_trend": bool(fake_trend),
        "passive_pressure": bool(passive_pressure),
        "promising": bool(promising),
        "score": score,
        "density": density,
        "pressure_count": pressure_count,
        "trend": trend,
        "window_delta": window_delta,
        "delta": delta,
    }


def should_delay_normal_score_entry(candidate, current_regime, reason):
    if reason != "score" or current_regime != "normal":
        return False

    source = candidate.get("_universe_source") or "primary"
    if source == "explorer":
        return False

    market_type = candidate.get("market_type", "general_binary")
    if market_type not in {"general_binary", "narrative_long_tail", "valuation_ladder", "sports_award_longshot", "speculative_hype"}:
        return False

    try:
        score = float(candidate.get("score", 0.0) or 0.0)
        density = float(candidate.get("pressure_density", 0.0) or 0.0)
        pressure_count = int(candidate.get("pressure_count", 0) or 0)
        trend = float(candidate.get("price_trend_strength", 0.0) or 0.0)
        window_delta = abs(float(candidate.get("price_delta_window", 0.0) or 0.0))
        delta = abs(float(candidate.get("price_delta", 0.0) or 0.0))
    except Exception:
        return True

    strong_structure = (
        (density >= 0.18 and pressure_count >= 2) or
        (trend >= 0.88 and window_delta >= 0.006) or
        (window_delta >= 0.010) or
        (delta >= 0.008 and trend >= 0.80)
    )
    if strong_structure:
        return False

    if market_type in {"sports_award_longshot", "narrative_long_tail"} and score < 1.08:
        return True
    if market_type in {"general_binary", "valuation_ladder"} and score < 1.12:
        return True
    if market_type == "speculative_hype" and score < 1.16:
        return True

    weak_structure = density < 0.16 and pressure_count < 2 and trend < 0.82 and window_delta < 0.006 and delta < 0.006
    return weak_structure


def _delayed_entry_memory_key(candidate):
    family_key = candidate.get("family_key")
    if family_key:
        return "family::{}".format(family_key)
    return "market::{}".format(build_market_key(candidate))


def _delayed_entry_snapshot(candidate):
    try:
        return {
            "score": float(candidate.get("score", 0.0) or 0.0),
            "density": float(candidate.get("pressure_density", 0.0) or 0.0),
            "pressure_count": int(candidate.get("pressure_count", 0) or 0),
            "trend": float(candidate.get("price_trend_strength", 0.0) or 0.0),
            "window_delta": abs(float(candidate.get("price_delta_window", 0.0) or 0.0)),
            "delta": abs(float(candidate.get("price_delta", 0.0) or 0.0)),
        }
    except Exception:
        return {
            "score": 0.0,
            "density": 0.0,
            "pressure_count": 0,
            "trend": 0.0,
            "window_delta": 0.0,
            "delta": 0.0,
        }


def _record_delayed_entry_memory(candidate, delayed_entry_memory, watch, status, now_ts):
    key = build_market_key(candidate)
    memory_key = _delayed_entry_memory_key(candidate)
    prev = dict(delayed_entry_memory.get(memory_key, {}) or {})

    payload = {
        "market_key": key,
        "family_key": candidate.get("family_key", memory_key),
        "question": candidate.get("question", ""),
        "last_ts": now_ts,
        "last_status": status,
        "watch_count": int(prev.get("watch_count", 0) or 0) + 1,
        "confirm_count": int(prev.get("confirm_count", 0) or 0) + (1 if status in {"confirmed", "admitted_light"} else 0),
        "admit_count": int(prev.get("admit_count", 0) or 0) + (1 if status == "admitted_light" else 0),
        "fail_count": int(prev.get("fail_count", 0) or 0) + (1 if status in {"failed", "watch_expired"} else 0),
        "light_success_count": int(prev.get("light_success_count", 0) or 0),
        "light_fail_count": int(prev.get("light_fail_count", 0) or 0),
        "light_follow_count": int(prev.get("light_follow_count", 0) or 0),
        "max_seen_cycles": max(int(prev.get("max_seen_cycles", 0) or 0), int(watch.get("seen_cycles", 0) or 0)),
        "max_score": max(float(prev.get("max_score", 0.0) or 0.0), float(watch.get("max_score", 0.0) or 0.0)),
        "max_density": max(float(prev.get("max_density", 0.0) or 0.0), float(watch.get("max_density", 0.0) or 0.0)),
        "max_pressure_count": max(int(prev.get("max_pressure_count", 0) or 0), int(watch.get("max_pressure_count", 0) or 0)),
        "max_trend": max(float(prev.get("max_trend", 0.0) or 0.0), float(watch.get("max_trend", 0.0) or 0.0)),
        "max_window_delta": max(float(prev.get("max_window_delta", 0.0) or 0.0), float(watch.get("max_window_delta", 0.0) or 0.0)),
        "max_delta": max(float(prev.get("max_delta", 0.0) or 0.0), float(watch.get("max_delta", 0.0) or 0.0)),
        "max_structure_votes": max(int(prev.get("max_structure_votes", 0) or 0), int(watch.get("structure_votes", 0) or 0)),
        "max_improvement_votes": max(int(prev.get("max_improvement_votes", 0) or 0), int(watch.get("improvement_votes", 0) or 0)),
        "max_promotion_score": max(int(prev.get("max_promotion_score", 0) or 0), int(watch.get("promotion_score", 0) or 0)),
        "last_structure_votes": int(watch.get("structure_votes", 0) or 0),
        "last_improvement_votes": int(watch.get("improvement_votes", 0) or 0),
        "last_promotion_score": int(watch.get("promotion_score", 0) or 0),
        "stall_cycles": max(int(prev.get("stall_cycles", 0) or 0), int(watch.get("stall_cycles", 0) or 0)),
    }
    delayed_entry_memory[memory_key] = payload
    return payload


def _feedback_memory_key_from_event(event):
    family_key = event.get("family_key")
    if family_key:
        return "family::{}".format(family_key)
    return "market::{}".format(event.get("position_key"))


def apply_light_admission_feedback(delayed_entry_memory, event, now_ts):
    stake_model = dict(event.get("stake_model", {}) or {})
    delayed_mode = event.get("delayed_entry_mode") or stake_model.get("delayed_entry_mode")
    if delayed_mode != "light":
        return None

    memory_key = _feedback_memory_key_from_event(event)
    prev = dict(delayed_entry_memory.get(memory_key, {}) or {})

    exit_reason = event.get("exit_reason")
    realized = float(event.get("realized_pnl_usd", 0.0) or 0.0)
    peak_pnl = float(event.get("peak_unrealized_pnl_pct", 0.0) or 0.0)
    age_cycles = int(event.get("age_cycles", 0) or 0)

    success_reasons = {
        "micro_profit_lock",
        "profit_recycle_exit",
        "peak_decay_exit",
        "capital_rotation_exit",
        "competitive_rotation_exit",
        "family_rotation_exit",
        "family_slot_recycle",
    }
    fail_reasons = {
        "delayed_admission_fail",
        "follow_through_compression_fail",
        "dead_capital_decay",
        "time_decay_exit",
        "time_stale_exit",
        "idle_hard_exit",
        "no_follow_through_exit",
        "pressure_decay_exit",
        "thesis_invalidation",
        "market_missing_stale",
        "hard_stop_loss",
    }

    is_success = (
        exit_reason in success_reasons or
        realized >= 0.03 or
        peak_pnl >= 0.10
    )
    is_follow = (
        realized >= 0.015 or
        peak_pnl >= 0.06 or
        exit_reason in {"micro_profit_lock", "profit_recycle_exit", "peak_decay_exit"}
    )
    is_failure = (
        exit_reason in fail_reasons or
        (realized <= 0.0 and age_cycles <= 4 and peak_pnl <= 0.06)
    )

    prev["market_key"] = event.get("position_key")
    prev["family_key"] = event.get("family_key", prev.get("family_key"))
    prev["question"] = event.get("question", prev.get("question", ""))
    prev["last_ts"] = now_ts
    prev["last_light_outcome"] = "success" if is_success else ("fail" if is_failure else "neutral")
    prev["last_light_exit_reason"] = exit_reason
    prev["last_light_realized_pnl_usd"] = realized
    prev["last_light_peak_pnl_pct"] = peak_pnl
    prev["light_success_count"] = int(prev.get("light_success_count", 0) or 0) + (1 if is_success else 0)
    prev["light_fail_count"] = int(prev.get("light_fail_count", 0) or 0) + (1 if is_failure else 0)
    prev["light_follow_count"] = int(prev.get("light_follow_count", 0) or 0) + (1 if is_follow else 0)
    delayed_entry_memory[memory_key] = prev
    return prev





def _delayed_promotion_score(candidate, watch, memory, structure_votes: int, improvement_votes: int) -> int:
    snap = _delayed_entry_snapshot(candidate)
    score = float(snap.get("score", 0.0) or 0.0)
    density = float(snap.get("density", 0.0) or 0.0)
    pressure_count = int(snap.get("pressure_count", 0) or 0)
    trend = float(snap.get("trend", 0.0) or 0.0)
    window_delta = float(snap.get("window_delta", 0.0) or 0.0)
    delta = float(snap.get("delta", 0.0) or 0.0)

    market_type = candidate.get("market_type", "general_binary")
    base = 0

    if structure_votes >= 4:
        base += 4
    elif structure_votes >= 3:
        base += 3
    elif structure_votes >= 2:
        base += 2
    elif structure_votes >= 1:
        base += 1

    if improvement_votes >= 2:
        base += 3
    elif improvement_votes >= 1:
        base += 2

    if density >= 0.12:
        base += 1
    if pressure_count >= 1:
        base += 1
    if trend >= 0.84:
        base += 1
    if window_delta >= 0.0025:
        base += 1
    if delta >= 0.0025:
        base += 1
    if score >= 1.04:
        base += 1

    if int(memory.get("confirm_count", 0) or 0) >= 1:
        base += 1
    if int(memory.get("max_structure_votes", 0) or 0) >= 3:
        base += 1
    if int(watch.get("best_structure_votes", 0) or 0) >= 3:
        base += 1
    if int(memory.get("max_promotion_score", 0) or 0) >= 5:
        base += 1
    if int(watch.get("best_promotion_score", 0) or 0) >= 5:
        base += 1
    if int(memory.get("admit_count", 0) or 0) >= 1:
        base += 1
    if int(memory.get("light_success_count", 0) or 0) >= 1:
        base += 2
    if int(memory.get("light_follow_count", 0) or 0) >= 1:
        base += 1
    if int(memory.get("light_fail_count", 0) or 0) >= 2:
        base -= 1

    if market_type in {"general_binary", "narrative_long_tail", "valuation_ladder"} and structure_votes >= 2 and (trend >= 0.80 or density >= 0.12 or window_delta >= 0.0025):
        base += 1
    if market_type == "speculative_hype" and (trend >= 0.88 or window_delta >= 0.0035):
        base += 1
    if market_type == "sports_award_longshot" and trend >= 0.88 and pressure_count >= 1:
        base += 1

    return int(base)


def evaluate_delayed_entry(candidate, delayed_entry_watch, delayed_entry_cooldown, delayed_entry_memory, now_ts):
    key = build_market_key(candidate)
    memory_key = _delayed_entry_memory_key(candidate)
    watch = dict(delayed_entry_watch.get(key, {}) or {})
    memory = dict(delayed_entry_memory.get(memory_key, {}) or {})
    snap = _delayed_entry_snapshot(candidate)

    score = snap["score"]
    density = snap["density"]
    pressure_count = snap["pressure_count"]
    trend = snap["trend"]
    window_delta = snap["window_delta"]
    delta = snap["delta"]

    if key in delayed_entry_cooldown or memory_key in delayed_entry_cooldown:
        return False, "delayed_entry_cooldown", memory or None

    if not watch:
        prior_seen = int(memory.get("max_seen_cycles", 0) or 0)
        last_status = memory.get("last_status")
        start_seen = 1
        if last_status in {"watch", "watch_expired"} and prior_seen >= 2:
            start_seen = 2
        elif last_status == "failed" and int(memory.get("max_structure_votes", 0) or 0) >= 3 and int(memory.get("fail_count", 0) or 0) <= 1:
            start_seen = 2

        delayed_entry_watch[key] = {
            "market_key": key,
            "family_key": candidate.get("family_key", key),
            "question": candidate.get("question", ""),
            "first_ts": now_ts,
            "last_ts": now_ts,
            "seen_cycles": start_seen,
            "stall_cycles": 0,
            "best_structure_votes": int(memory.get("max_structure_votes", 0) or 0),
            "best_improvement_votes": int(memory.get("max_improvement_votes", 0) or 0),
            "best_promotion_score": int(memory.get("max_promotion_score", 0) or 0),
            "first_score": score,
            "first_density": density,
            "first_pressure_count": pressure_count,
            "first_trend": trend,
            "first_window_delta": window_delta,
            "first_delta": delta,
            "max_score": max(float(memory.get("max_score", 0.0) or 0.0), score),
            "max_density": max(float(memory.get("max_density", 0.0) or 0.0), density),
            "max_pressure_count": max(int(memory.get("max_pressure_count", 0) or 0), pressure_count),
            "max_trend": max(float(memory.get("max_trend", 0.0) or 0.0), trend),
            "max_window_delta": max(float(memory.get("max_window_delta", 0.0) or 0.0), window_delta),
            "max_delta": max(float(memory.get("max_delta", 0.0) or 0.0), delta),
            "memory_fail_count": int(memory.get("fail_count", 0) or 0),
            "memory_confirm_count": int(memory.get("confirm_count", 0) or 0),
            "memory_admit_count": int(memory.get("admit_count", 0) or 0),
            "memory_light_success_count": int(memory.get("light_success_count", 0) or 0),
            "memory_light_fail_count": int(memory.get("light_fail_count", 0) or 0),
            "structure_votes": 0,
            "improvement_votes": 0,
            "promotion_score": int(memory.get("last_promotion_score", 0) or 0),
        }
        return False, "delayed_entry_watch", delayed_entry_watch[key]

    watch["seen_cycles"] = int(watch.get("seen_cycles", 0) or 0) + 1
    watch["last_ts"] = now_ts
    watch["max_score"] = max(float(watch.get("max_score", score) or score), score)
    watch["max_density"] = max(float(watch.get("max_density", density) or density), density)
    watch["max_pressure_count"] = max(int(watch.get("max_pressure_count", pressure_count) or pressure_count), pressure_count)
    watch["max_trend"] = max(float(watch.get("max_trend", trend) or trend), trend)
    watch["max_window_delta"] = max(float(watch.get("max_window_delta", window_delta) or window_delta), window_delta)
    watch["max_delta"] = max(float(watch.get("max_delta", delta) or delta), delta)

    structure_votes = 0
    if density >= 0.16 or watch["max_density"] >= 0.18:
        structure_votes += 1
    if pressure_count >= 2 or watch["max_pressure_count"] >= 2:
        structure_votes += 1
    if trend >= 0.84 or watch["max_trend"] >= 0.88:
        structure_votes += 1
    if window_delta >= 0.006 or watch["max_window_delta"] >= 0.008:
        structure_votes += 1
    if delta >= 0.006 or watch["max_delta"] >= 0.007:
        structure_votes += 1
    if score >= 1.04 or watch["max_score"] >= 1.10:
        structure_votes += 1

    improvement_votes = 0
    if score >= float(watch.get("first_score", score) or score) + 0.03:
        improvement_votes += 1
    if density >= float(watch.get("first_density", density) or density) + 0.04:
        improvement_votes += 1
    if pressure_count >= int(watch.get("first_pressure_count", pressure_count) or pressure_count) + 1:
        improvement_votes += 1
    if trend >= float(watch.get("first_trend", trend) or trend) + 0.05:
        improvement_votes += 1
    if window_delta >= float(watch.get("first_window_delta", window_delta) or window_delta) + 0.002:
        improvement_votes += 1
    if delta >= float(watch.get("first_delta", delta) or delta) + 0.002:
        improvement_votes += 1

    watch["structure_votes"] = structure_votes
    watch["improvement_votes"] = improvement_votes

    best_structure = int(watch.get("best_structure_votes", 0) or 0)
    best_improvement = int(watch.get("best_improvement_votes", 0) or 0)
    if structure_votes > best_structure or improvement_votes > best_improvement:
        watch["stall_cycles"] = 0
    else:
        watch["stall_cycles"] = int(watch.get("stall_cycles", 0) or 0) + 1
    watch["best_structure_votes"] = max(best_structure, structure_votes)
    watch["best_improvement_votes"] = max(best_improvement, improvement_votes)

    promotion_score = _delayed_promotion_score(candidate, watch, memory, structure_votes, improvement_votes)
    best_promotion = int(watch.get("best_promotion_score", 0) or 0)
    if promotion_score > best_promotion:
        watch["stall_cycles"] = 0
    watch["promotion_score"] = promotion_score
    watch["best_promotion_score"] = max(best_promotion, promotion_score)

    delayed_entry_watch[key] = watch

    fail_count = int(memory.get("fail_count", 0) or 0)
    confirm_count = int(memory.get("confirm_count", 0) or 0)
    admit_count = int(memory.get("admit_count", 0) or 0)
    light_success_count = int(memory.get("light_success_count", 0) or 0)
    light_fail_count = int(memory.get("light_fail_count", 0) or 0)
    seen_cycles = int(watch.get("seen_cycles", 0) or 0)
    stall_cycles = int(watch.get("stall_cycles", 0) or 0)

    promoted_ready = (
        seen_cycles >= 2 and (
            promotion_score >= 6 or
            (promotion_score >= 5 and structure_votes >= 3) or
            (promotion_score >= 5 and improvement_votes >= 1 and structure_votes >= 2 and (density >= 0.12 or trend >= 0.80 or window_delta >= 0.003)) or
            (promotion_score >= 4 and improvement_votes >= 2 and (trend >= 0.80 or density >= 0.12 or window_delta >= 0.003)) or
            (promotion_score >= 4 and int(watch.get("best_promotion_score", 0) or 0) >= 5 and seen_cycles >= 3) or
            (promotion_score >= 4 and confirm_count >= 1 and structure_votes >= 2) or
            (promotion_score >= 4 and light_success_count >= 1 and structure_votes >= 2 and (trend >= 0.76 or density >= 0.10 or window_delta >= 0.0025))
        )
    )
    if promoted_ready:
        delayed_entry_watch.pop(key, None)
        mem = _record_delayed_entry_memory(candidate, delayed_entry_memory, watch, "confirmed", now_ts)
        watch["memory_fail_count"] = fail_count
        watch["memory_confirm_count"] = int(mem.get("confirm_count", 0) or 0)
        return True, "delayed_entry_promoted", watch

    confirm_ready = (
        structure_votes >= 4 or
        (structure_votes >= 3 and improvement_votes >= 1) or
        (structure_votes >= 3 and trend >= 0.84 and (window_delta >= 0.005 or density >= 0.16)) or
        (structure_votes >= 3 and confirm_count >= 1)
    )
    if seen_cycles >= 2 and confirm_ready:
        delayed_entry_watch.pop(key, None)
        mem = _record_delayed_entry_memory(candidate, delayed_entry_memory, watch, "confirmed", now_ts)
        watch["memory_fail_count"] = fail_count
        watch["memory_confirm_count"] = int(mem.get("confirm_count", 0) or 0)
        return True, "delayed_entry_confirmed", watch

    light_admission_ready = (
        seen_cycles >= 2 and (
            (promotion_score >= 4 and structure_votes >= 2 and (trend >= 0.78 or density >= 0.10 or window_delta >= 0.0020 or pressure_count >= 1)) or
            (promotion_score >= 3 and improvement_votes >= 1 and structure_votes >= 2 and (trend >= 0.80 or density >= 0.12 or window_delta >= 0.0025)) or
            (promotion_score >= 4 and int(watch.get("best_promotion_score", 0) or 0) >= 4 and stall_cycles <= 1 and structure_votes >= 1) or
            (promotion_score >= 3 and light_success_count >= 1 and structure_votes >= 1 and (trend >= 0.72 or density >= 0.10 or window_delta >= 0.0020)) or
            (promotion_score >= 3 and admit_count >= 1 and improvement_votes >= 1 and structure_votes >= 1)
        ) and light_fail_count <= 1 and fail_count <= 1
    )
    if light_admission_ready:
        delayed_entry_watch.pop(key, None)
        mem = _record_delayed_entry_memory(candidate, delayed_entry_memory, watch, "admitted_light", now_ts)
        watch["memory_fail_count"] = int(mem.get("fail_count", 0) or 0)
        watch["memory_confirm_count"] = int(mem.get("confirm_count", 0) or 0)
        watch["memory_admit_count"] = int(mem.get("admit_count", 0) or 0)
        return True, "delayed_admit_light", watch

    fail_fast = (
        (seen_cycles >= 3 and structure_votes <= 1 and improvement_votes == 0 and promotion_score <= 3 and light_success_count == 0) or
        (seen_cycles >= 3 and stall_cycles >= 2 and structure_votes < 3 and promotion_score <= 4 and light_success_count == 0) or
        (seen_cycles >= 2 and fail_count >= 1 and structure_votes <= 1 and improvement_votes == 0 and promotion_score <= 4 and light_success_count == 0) or
        (seen_cycles >= 4 and promotion_score <= 4 and light_success_count == 0) or
        (seen_cycles >= 3 and light_fail_count >= 2 and promotion_score <= 4 and improvement_votes == 0)
    )
    if fail_fast:
        delayed_entry_watch.pop(key, None)
        delayed_entry_cooldown[key] = now_ts
        delayed_entry_cooldown[memory_key] = now_ts
        mem = _record_delayed_entry_memory(candidate, delayed_entry_memory, watch, "failed", now_ts)
        watch["memory_fail_count"] = int(mem.get("fail_count", 0) or 0)
        watch["memory_confirm_count"] = int(mem.get("confirm_count", 0) or 0)
        return False, "delayed_entry_failed", watch

    _record_delayed_entry_memory(candidate, delayed_entry_memory, watch, "watch", now_ts)
    return False, "delayed_entry_watch", watch


def delayed_family_memory_strength(candidate, delayed_entry_memory):
    if not delayed_entry_memory:
        return 0.0

    market_key = build_market_key(candidate)
    family_key = candidate.get("family_key")
    memory = dict(delayed_entry_memory.get("market::{}".format(market_key), {}) or {})
    if family_key:
        family_memory = dict(delayed_entry_memory.get("family::{}".format(family_key), {}) or {})
        if len(family_memory) > len(memory):
            memory = family_memory

    if not memory:
        return 0.0

    bonus = 0.0
    bonus += min(int(memory.get("confirm_count", 0) or 0), 2) * 0.14
    bonus += min(int(memory.get("admit_count", 0) or 0), 2) * 0.08
    bonus += min(int(memory.get("light_success_count", 0) or 0), 2) * 0.10
    bonus += min(int(memory.get("light_follow_count", 0) or 0), 2) * 0.06
    if int(memory.get("max_structure_votes", 0) or 0) >= 3:
        bonus += 0.08
    if int(memory.get("max_promotion_score", 0) or 0) >= 5:
        bonus += 0.08
    if str(memory.get("last_light_outcome") or "") == "success":
        bonus += 0.06
    if str(memory.get("last_status") or "") in {"confirmed", "admitted_light"}:
        bonus += 0.05
    if int(memory.get("fail_count", 0) or 0) >= 2:
        bonus -= 0.08
    if int(memory.get("light_fail_count", 0) or 0) >= 2:
        bonus -= 0.08
    return round(bonus, 4)


def family_swap_outcome_state(engine, candidate):
    family_key = candidate.get("family_key")
    if not family_key:
        return {
            "weakest": None,
            "outcome_bonus": 0.0,
            "weak_count": 0,
            "family_count": 0,
            "family_heat": 0.0,
        }

    rows = []
    for pos in engine.open_positions:
        if pos.get("family_key") != family_key:
            continue

        hold = engine.compute_hold_score(pos, candidate)
        hold_score = float(hold.get("hold_score", 0.0) or 0.0)
        dead_cycles = int(pos.get("dead_cycles", 0) or 0)
        silent_cycles = int(pos.get("silent_cycles", 0) or 0)
        pnl_pct = float(pos.get("current_unrealized_pnl_pct", 0.0) or 0.0)
        peak_pnl_pct = float(pos.get("peak_unrealized_pnl_pct", pnl_pct) or pnl_pct)
        reason = str(pos.get("reason", "unknown") or "unknown")
        price_progress = abs(float(pos.get("last_window_delta", 0.0) or 0.0))
        family_heat = float(hold.get("family_heat", 0.0) or 0.0)

        weakness = 0.0
        if reason == "score":
            weakness += 0.04
        if hold_score < 1.02:
            weakness += 0.10
        elif hold_score < 1.10:
            weakness += 0.06
        if dead_cycles >= 2:
            weakness += min(0.16, 0.06 + dead_cycles * 0.03)
        if silent_cycles >= 4:
            weakness += min(0.12, 0.04 + (silent_cycles - 3) * 0.02)
        if peak_pnl_pct <= 0.04:
            weakness += 0.05
        if pnl_pct <= 0.01:
            weakness += 0.04
        if price_progress <= 0.0025:
            weakness += 0.03
        if family_heat >= 2.4:
            weakness += 0.03

        rows.append({
            "position_key": pos.get("position_key"),
            "hold_score": hold_score,
            "weakness": round(weakness, 4),
            "dead_cycles": dead_cycles,
            "silent_cycles": silent_cycles,
            "pnl_pct": round(pnl_pct, 4),
            "peak_pnl_pct": round(peak_pnl_pct, 4),
            "reason": reason,
        })

    if not rows:
        return {
            "weakest": None,
            "outcome_bonus": 0.0,
            "weak_count": 0,
            "family_count": 0,
            "family_heat": 0.0,
        }

    weakest = max(rows, key=lambda r: (r["weakness"], -r["hold_score"]))
    weak_count = sum(1 for r in rows if r["weakness"] >= 0.12)
    family_heat = float(engine.family_exposure().get(family_key, 0.0) or 0.0)

    outcome_bonus = float(weakest["weakness"])
    if weak_count >= 2:
        outcome_bonus += 0.05
    if family_heat >= 2.6:
        outcome_bonus += 0.03

    return {
        "weakest": weakest,
        "outcome_bonus": round(min(outcome_bonus, 0.32), 4),
        "weak_count": weak_count,
        "family_count": len(rows),
        "family_heat": round(family_heat, 4),
    }


def family_review_trigger_strength(engine, candidate, delayed_entry_memory=None):
    family_key = candidate.get("family_key")
    if not family_key:
        return {"trigger_bonus": 0.0, "memory_bonus": 0.0, "outcome_bonus": 0.0, "triggered": False}

    family_memory = dict((delayed_entry_memory or {}).get("family::{}".format(family_key), {}) or {})
    market_memory = dict((delayed_entry_memory or {}).get("market::{}".format(build_market_key(candidate)), {}) or {})
    memory = family_memory if len(family_memory) >= len(market_memory) else market_memory

    memory_bonus = 0.0
    memory_bonus += min(int(memory.get("confirm_count", 0) or 0), 2) * 0.08
    memory_bonus += min(int(memory.get("admit_count", 0) or 0), 2) * 0.05
    memory_bonus += min(int(memory.get("light_success_count", 0) or 0), 2) * 0.06
    memory_bonus += min(int(memory.get("light_follow_count", 0) or 0), 2) * 0.04
    if int(memory.get("max_promotion_score", 0) or 0) >= 5:
        memory_bonus += 0.05
    if int(memory.get("max_structure_votes", 0) or 0) >= 3:
        memory_bonus += 0.04
    if str(memory.get("last_status") or "") in {"confirmed", "admitted_light"}:
        memory_bonus += 0.04

    outcome_state = family_swap_outcome_state(engine, candidate)
    outcome_bonus = float(outcome_state.get("outcome_bonus", 0.0) or 0.0)
    weakest = dict(outcome_state.get("weakest") or {})
    weakness = float(weakest.get("weakness", 0.0) or 0.0)

    trigger_bonus = memory_bonus
    if weakness >= 0.12:
        trigger_bonus += 0.06
    if weakness >= 0.18:
        trigger_bonus += 0.05
    if int(outcome_state.get("weak_count", 0) or 0) >= 2:
        trigger_bonus += 0.04
    if float(outcome_state.get("family_heat", 0.0) or 0.0) >= 2.4:
        trigger_bonus += 0.03

    return {
        "trigger_bonus": round(min(trigger_bonus, 0.28), 4),
        "memory_bonus": round(memory_bonus, 4),
        "outcome_bonus": round(outcome_bonus, 4),
        "triggered": trigger_bonus >= 0.12,
        "weakest": weakest,
        "weak_count": int(outcome_state.get("weak_count", 0) or 0),
        "family_heat": float(outcome_state.get("family_heat", 0.0) or 0.0),
    }


def concentration_rebalance_state(engine, candidate):
    theme = candidate.get("theme", "unknown")
    cluster = candidate.get("cluster", "unknown")
    family_key = candidate.get("family_key")
    theme_exp = float(engine.theme_exposure().get(theme, 0.0) or 0.0)
    cluster_exp = float(engine.cluster_exposure().get(cluster, 0.0) or 0.0)
    family_exp = float(engine.family_exposure().get(family_key, 0.0) or 0.0) if family_key else 0.0

    dominant = False
    if family_exp >= 3.4:
        dominant = True
    if cluster_exp >= 4.2:
        dominant = True
    if theme_exp >= 4.8:
        dominant = True

    penalty = 0.0
    if family_exp >= 4.0:
        penalty += 0.14
    elif family_exp >= 3.0:
        penalty += 0.08
    if cluster_exp >= 4.5:
        penalty += 0.12
    elif cluster_exp >= 3.5:
        penalty += 0.07
    if theme_exp >= 5.0:
        penalty += 0.10
    elif theme_exp >= 4.0:
        penalty += 0.05

    return {
        "theme_exposure": round(theme_exp, 4),
        "cluster_exposure": round(cluster_exp, 4),
        "family_exposure": round(family_exp, 4),
        "dominant": dominant,
        "penalty": round(min(penalty, 0.26), 4),
    }



def should_escalate_delayed_family_review(engine, candidate, delayed_meta, delayed_entry_memory=None, current_regime="normal", reason="score"):
    family_key = candidate.get("family_key")
    if not family_key or not engine.has_open_family(family_key):
        return {"escalate": False}

    market_key = build_market_key(candidate)
    family_memory = dict((delayed_entry_memory or {}).get("family::{}".format(family_key), {}) or {})
    market_memory = dict((delayed_entry_memory or {}).get("market::{}".format(market_key), {}) or {})
    memory = family_memory if len(family_memory) >= len(market_memory) else market_memory

    seen = int((delayed_meta or {}).get("seen_cycles", 0) or 0)
    confirms = int((delayed_meta or {}).get("memory_confirm_count", 0) or 0)
    admits = int((delayed_meta or {}).get("memory_admit_count", 0) or 0)
    promotion_score = int((delayed_meta or {}).get("promotion_score", 0) or 0)
    structure_votes = int((delayed_meta or {}).get("structure_votes", 0) or 0)
    improvement_votes = int((delayed_meta or {}).get("improvement_votes", 0) or 0)

    mem_confirms = int(memory.get("confirm_count", 0) or 0)
    mem_admits = int(memory.get("admit_count", 0) or 0)
    mem_promotion = int(memory.get("max_promotion_score", 0) or 0)
    mem_structure = int(memory.get("max_structure_votes", 0) or 0)

    trigger_state = family_review_trigger_strength(engine, candidate, delayed_entry_memory=delayed_entry_memory)
    trigger_bonus = float(trigger_state.get("trigger_bonus", 0.0) or 0.0)
    outcome_bonus = float(trigger_state.get("outcome_bonus", 0.0) or 0.0)
    weakest = dict(trigger_state.get("weakest") or {})
    weak_hold = float(weakest.get("hold_score", 0.0) or 0.0)
    weak_silent = int(weakest.get("silent_cycles", 0) or 0)
    weak_dead = int(weakest.get("dead_cycles", 0) or 0)

    override = political_family_override_state(
        candidate,
        delayed_entry_memory=delayed_entry_memory,
        delayed_entry_watch=None,
        delayed_entry_cooldown=None,
    )
    effective_seen = max(seen, min(int(memory.get("max_seen_cycles", 0) or 0), 3))
    politics_like = bool(override.get("politics_like", False))
    targeted = bool(override.get("targeted", False))

    enough_signal = (
        promotion_score >= 4 or
        structure_votes >= 3 or
        improvement_votes >= 1 or
        confirms >= 1 or
        admits >= 1 or
        mem_confirms >= 1 or
        mem_admits >= 1 or
        mem_promotion >= 4 or
        mem_structure >= 2
    )
    weak_incumbent = (
        weak_hold <= 1.12 or
        weak_silent >= 1 or
        weak_dead >= 1 or
        trigger_bonus >= 0.08 or
        outcome_bonus >= 0.08
    )

    if targeted and (effective_seen >= 1 or mem_confirms >= 1 or mem_admits >= 1) and weak_incumbent:
        return {
            "escalate": True,
            "reason": "targeted_political_family_trigger",
            "trigger_bonus": round(max(trigger_bonus, 0.10), 4),
            "outcome_bonus": round(max(outcome_bonus, 0.08), 4),
            "weak_hold": round(weak_hold, 4),
            "weak_silent": weak_silent,
            "weak_dead": weak_dead,
            "promotion_score": max(promotion_score, mem_promotion, 4),
            "structure_votes": max(structure_votes, mem_structure, 2),
            "improvement_votes": max(improvement_votes, 1),
        }

    if politics_like and effective_seen >= 1 and enough_signal and weak_incumbent:
        return {
            "escalate": True,
            "reason": "delayed_family_trigger_hard",
            "trigger_bonus": round(max(trigger_bonus, 0.08), 4),
            "outcome_bonus": round(outcome_bonus, 4),
            "weak_hold": round(weak_hold, 4),
            "weak_silent": weak_silent,
            "weak_dead": weak_dead,
            "promotion_score": max(promotion_score, mem_promotion),
            "structure_votes": max(structure_votes, mem_structure),
            "improvement_votes": max(improvement_votes, 1 if politics_like else improvement_votes),
        }

    if effective_seen >= 2 and enough_signal and weak_incumbent:
        return {
            "escalate": True,
            "reason": "delayed_family_trigger",
            "trigger_bonus": round(trigger_bonus, 4),
            "outcome_bonus": round(outcome_bonus, 4),
            "weak_hold": round(weak_hold, 4),
            "weak_silent": weak_silent,
            "weak_dead": weak_dead,
            "promotion_score": max(promotion_score, mem_promotion),
            "structure_votes": max(structure_votes, mem_structure),
            "improvement_votes": improvement_votes,
        }

    return {"escalate": False}


def is_balance_of_power_candidate(candidate):
    question = (candidate.get("question") or "").lower()
    return "balance of power" in question or ("senate" in question and "house" in question)


def political_family_override_state(candidate, delayed_entry_memory=None, delayed_entry_watch=None, delayed_entry_cooldown=None):
    question = (candidate.get("question") or "").lower()
    theme = str(candidate.get("theme", "unknown") or "unknown")
    cluster = str(candidate.get("cluster", "unknown") or "unknown")
    family_key = candidate.get("family_key")
    market_key = build_market_key(candidate)

    politics_like = (
        theme in {"politics", "general"} or
        cluster in {"geopolitics", "theme_politics"} or
        any(term in question for term in {
            "hungary", "prime minister", "balance of power", "senate", "house",
            "election", "ukraine", "russia", "taiwan", "china", "putin", "xi"
        })
    )

    targeted_terms = {
        "balance of power", "next prime minister of hungary", "lászló toroczkai",
        "laszlo toroczkai", "péter magyar", "peter magyar", "viktor orbán", "viktor orban"
    }
    targeted = any(term in question for term in targeted_terms)

    if not politics_like or not family_key:
        return {
            "eligible": False,
            "strength": 0.0,
            "memory": {},
            "watch_active": False,
            "cooldown_active": False,
            "memory_active": False,
            "family_key": family_key,
            "market_key": market_key,
            "politics_like": politics_like,
            "targeted": False,
            "reason": None,
        }

    family_memory = dict((delayed_entry_memory or {}).get("family::{}".format(family_key), {}) or {})
    market_memory = dict((delayed_entry_memory or {}).get("market::{}".format(market_key), {}) or {})
    memory = family_memory if len(family_memory) >= len(market_memory) else market_memory

    watch_keys = delayed_entry_watch or {}
    cooldown_keys = delayed_entry_cooldown or {}
    watch_active = market_key in watch_keys
    cooldown_active = market_key in cooldown_keys or family_key in cooldown_keys or ("family::{}".format(family_key) in cooldown_keys)
    memory_active = bool(memory)

    confirm_count = int(memory.get("confirm_count", 0) or 0)
    admit_count = int(memory.get("admit_count", 0) or 0)
    promotion = int(memory.get("max_promotion_score", 0) or 0)
    structure = int(memory.get("max_structure_votes", 0) or 0)
    seen = int(memory.get("max_seen_cycles", 0) or 0)
    light_success = int(memory.get("light_success_count", 0) or 0)
    light_follow = int(memory.get("light_follow_count", 0) or 0)

    try:
        score = float(candidate.get("score", 0.0) or 0.0)
        density = float(candidate.get("pressure_density", 0.0) or 0.0)
        trend = float(candidate.get("price_trend_strength", 0.0) or 0.0)
        window_delta = abs(float(candidate.get("price_delta_window", 0.0) or 0.0))
        pressure_count = int(candidate.get("pressure_count", 0) or 0)
        market_type = str(candidate.get("market_type", "general_binary") or "general_binary")
    except Exception:
        score = 0.0
        density = 0.0
        trend = 0.0
        window_delta = 0.0
        pressure_count = 0
        market_type = "general_binary"

    strength = 0.0
    if memory_active:
        strength += 0.08
    if watch_active:
        strength += 0.05
    if confirm_count >= 1:
        strength += 0.08
    if admit_count >= 1:
        strength += 0.06
    if promotion >= 4:
        strength += 0.06
    if structure >= 2:
        strength += 0.05
    if seen >= 2:
        strength += 0.04
    if light_success >= 1 or light_follow >= 1:
        strength += 0.04
    if cooldown_active and (confirm_count >= 1 or promotion >= 4):
        strength += 0.03

    balance_like = is_balance_of_power_candidate(candidate)

    reason = "political_family_override"
    if targeted:
        strength += 0.08
        reason = "targeted_political_family_override"
        if memory_active:
            strength += 0.04
        if confirm_count >= 1 or admit_count >= 1:
            strength += 0.04
        if promotion >= 4 or structure >= 2:
            strength += 0.03
        if seen >= 1 or watch_active:
            strength += 0.03

    if balance_like:
        strength += 0.06
        if memory_active:
            strength += 0.04
        if promotion >= 3 or structure >= 2:
            strength += 0.03
        if confirm_count >= 1 or admit_count >= 1:
            strength += 0.03
        if watch_active or cooldown_active:
            strength += 0.02
        if reason == "political_family_override":
            reason = "balance_rescue_override"

    flat_narrative_override = (
        market_type == "narrative_long_tail"
        and density < 0.16
        and pressure_count < 2
        and trend < 0.82
        and window_delta < 0.006
        and score < 1.12
    )
    weak_general_override = (
        market_type == "general_binary"
        and theme in {"politics", "general"}
        and density < 0.18
        and pressure_count < 2
        and trend < 0.84
        and window_delta < 0.008
        and score < 0.96
    )

    if flat_narrative_override:
        strength -= 0.06
        print(
            "TRACE | political_rescue_narrowing | signal=flat_narrative_override | targeted={} | balance_like={} | score={:.3f} | pd={:.3f} | trend={:.3f} | win={:.3f} | {}".format(
                int(bool(targeted)), int(bool(balance_like)), score, density, trend, window_delta, (candidate.get("question") or "")[:72]
            )
        )

    if weak_general_override and not targeted:
        strength -= 0.04
        print(
            "TRACE | political_rescue_narrowing | signal=weak_general_override | targeted={} | balance_like={} | score={:.3f} | pd={:.3f} | trend={:.3f} | win={:.3f} | {}".format(
                int(bool(targeted)), int(bool(balance_like)), score, density, trend, window_delta, (candidate.get("question") or "")[:72]
            )
        )

    threshold = 0.10 if balance_like else (0.12 if targeted else 0.14)
    if market_type == "narrative_long_tail":
        threshold += 0.02
    if weak_general_override and not targeted:
        threshold += 0.02

    raw_strength = round(min(max(strength, 0.0), 0.38), 4)
    eligible = bool(raw_strength >= threshold)

    return {
        "eligible": eligible,
        "strength": raw_strength if eligible else 0.0,
        "hint_strength": raw_strength,
        "memory": memory,
        "watch_active": watch_active,
        "cooldown_active": cooldown_active,
        "memory_active": memory_active,
        "family_key": family_key,
        "market_key": market_key,
        "politics_like": politics_like,
        "targeted": targeted,
        "balance_like": balance_like,
        "reason": reason if eligible else None,
        "hint_reason": reason if raw_strength > 0 else None,
    }


def family_sibling_review_bonus(candidate, primary_effective, effective_attack, current_regime):
    diff = float(primary_effective - effective_attack)
    score = float(candidate.get("score", 0.0) or 0.0)
    density = float(candidate.get("pressure_density", 0.0) or 0.0)
    trend = float(candidate.get("price_trend_strength", 0.0) or 0.0)
    window_delta = abs(float(candidate.get("price_delta_window", 0.0) or 0.0))
    market_type = candidate.get("market_type", "general_binary")
    theme = candidate.get("theme", "unknown")
    bonus = 0.0

    if diff <= 0.18:
        bonus += 0.05
    if diff <= 0.12:
        bonus += 0.04
    if trend >= 0.82 or density >= 0.14 or window_delta >= 0.003:
        bonus += 0.04
    if score >= 1.02:
        bonus += 0.03
    if market_type in {"general_binary", "narrative_long_tail", "legal_resolution"}:
        bonus += 0.03
    if theme in {"politics", "general"} and current_regime != "calm":
        bonus += 0.03

    return round(min(bonus, 0.18), 4)


def family_admission_review(engine, candidate, reason, current_regime, delayed_entry_memory=None):
    family_key = candidate.get("family_key")
    if not family_key or not engine.has_open_family(family_key):
        return {"allow": True, "reason": None, "action": None, "gap": 0.0, "target": None, "incoming": 0.0, "effective_incoming": 0.0}

    incoming = family_attack_score(candidate, reason, current_regime, engine=engine)
    memory_bonus = delayed_family_memory_strength(candidate, delayed_entry_memory)
    trigger_state = family_review_trigger_strength(engine, candidate, delayed_entry_memory=delayed_entry_memory)
    outcome_state = family_swap_outcome_state(engine, candidate)
    outcome_bonus = float(outcome_state.get("outcome_bonus", 0.0) or 0.0)
    trigger_bonus = float(trigger_state.get("trigger_bonus", 0.0) or 0.0)
    rebalance_state = concentration_rebalance_state(engine, candidate)
    rebalance_penalty = float(rebalance_state.get("penalty", 0.0) or 0.0)
    effective_incoming = incoming + memory_bonus + outcome_bonus + trigger_bonus - (rebalance_penalty * 0.5)

    best_open = None
    weakest_open = None
    for pos in engine.open_positions:
        if pos.get("family_key") != family_key:
            continue
        hold = engine.compute_hold_score(pos, candidate)
        hold_score = float(hold.get("hold_score", 0.0) or 0.0)
        row = {"position_key": pos.get("position_key"), "hold_score": hold_score, "position": pos, "hold": hold}
        if best_open is None or hold_score > best_open["hold_score"]:
            best_open = row
        if weakest_open is None or hold_score < weakest_open["hold_score"]:
            weakest_open = row

    if best_open is None:
        return {"allow": True, "reason": None, "action": None, "gap": 0.0, "target": None, "incoming": incoming, "effective_incoming": effective_incoming}

    gap = float(best_open["hold_score"] - effective_incoming)
    delayed_signal = candidate.get("_delayed_entry_signal")
    delayed_confirmed = bool(candidate.get("_delayed_entry_confirmed", False))
    delayed_light = bool(candidate.get("_delayed_entry_light", False))
    selective = is_selective_aggression_candidate(candidate, reason, current_regime, engine=engine)

    forced_review = (
        bool(candidate.get("_family_force_review", False)) or
        bool(candidate.get("_political_targeted_override", False)) or
        float(candidate.get("_family_trigger_bonus", 0.0) or 0.0) >= 0.10
    )
    contender = (
        delayed_confirmed or
        delayed_light or
        forced_review or
        delayed_signal in {"delayed_entry_promoted", "delayed_entry_confirmed", "delayed_admit_light", "delayed_family_trigger", "targeted_political_family_trigger"} or
        memory_bonus >= 0.12 or
        outcome_bonus >= 0.12 or
        int((delayed_entry_memory or {}).get("family::{}".format(family_key), {}).get("confirm_count", 0) or 0) >= 1 or
        trigger_bonus >= 0.12
    )

    if weakest_open and effective_incoming >= float(weakest_open["hold_score"] - (0.06 if contender or trigger_bonus >= 0.12 else 0.0)):
        return {
            "allow": True,
            "reason": "family_admission_swap",
            "action": "swap",
            "gap": gap,
            "target": weakest_open,
            "incoming": incoming,
            "effective_incoming": effective_incoming,
            "outcome_bonus": outcome_bonus,
            "trigger_bonus": trigger_bonus,
            "rebalance_penalty": rebalance_penalty,
        }

    if contender and gap <= (0.16 + outcome_bonus + trigger_bonus + (0.06 if forced_review else 0.0)):
        return {
            "allow": True,
            "reason": "family_promotion_review",
            "action": "review",
            "gap": gap,
            "target": weakest_open,
            "incoming": incoming,
            "effective_incoming": effective_incoming,
            "outcome_bonus": outcome_bonus,
            "trigger_bonus": trigger_bonus,
            "rebalance_penalty": rebalance_penalty,
        }

    if (selective or forced_review) and gap <= (0.14 + (0.06 if forced_review else 0.0)):
        return {
            "allow": True,
            "reason": "family_selective_review",
            "action": "review",
            "gap": gap,
            "target": weakest_open,
            "incoming": incoming,
            "effective_incoming": effective_incoming,
            "outcome_bonus": outcome_bonus,
            "trigger_bonus": trigger_bonus,
            "rebalance_penalty": rebalance_penalty,
        }

    if (outcome_bonus >= 0.12 or trigger_bonus >= 0.12 or forced_review) and weakest_open and gap <= (0.24 + trigger_bonus + (0.08 if forced_review else 0.0)):
        return {
            "allow": True,
            "reason": "family_swap_review",
            "action": "swap_review",
            "gap": gap,
            "target": weakest_open,
            "incoming": incoming,
            "effective_incoming": effective_incoming,
            "outcome_bonus": outcome_bonus,
            "trigger_bonus": trigger_bonus,
            "rebalance_penalty": rebalance_penalty,
        }

    if forced_review and weakest_open and float(weakest_open.get("hold_score", 0.0) or 0.0) <= 1.08 and gap <= 0.30:
        return {
            "allow": True,
            "reason": "family_trigger_escalation",
            "action": "family_swap_review",
            "gap": gap,
            "target": weakest_open,
            "incoming": incoming,
            "effective_incoming": effective_incoming,
            "outcome_bonus": outcome_bonus,
            "trigger_bonus": trigger_bonus,
            "rebalance_penalty": rebalance_penalty,
        }

    if effective_incoming + 0.12 < float(best_open["hold_score"]) and not selective and not contender:
        return {
            "allow": False,
            "reason": "family_winner_preference",
            "action": None,
            "gap": gap,
            "target": weakest_open,
            "incoming": incoming,
            "effective_incoming": effective_incoming,
            "outcome_bonus": outcome_bonus,
            "trigger_bonus": trigger_bonus,
            "rebalance_penalty": rebalance_penalty,
        }

    return {
        "allow": True,
        "reason": None,
        "action": None,
        "gap": gap,
        "target": weakest_open,
        "incoming": incoming,
        "effective_incoming": effective_incoming,
        "outcome_bonus": outcome_bonus,
        "trigger_bonus": trigger_bonus,
        "rebalance_penalty": rebalance_penalty,
    }


def family_winner_guard(engine, candidate, reason, current_regime, delayed_entry_memory=None):
    review = family_admission_review(
        engine,
        candidate,
        reason,
        current_regime,
        delayed_entry_memory=delayed_entry_memory,
    )
    if review.get("allow"):
        candidate["_family_review_mode"] = review.get("action") or candidate.get("_family_review_mode")
        candidate["_family_review_gap"] = float(review.get("gap", 0.0) or 0.0)
        candidate["_family_review_reason"] = review.get("reason")
        target = review.get("target") or {}
        candidate["_family_review_target"] = target.get("position_key")
        candidate["_family_effective_attack"] = float(review.get("effective_incoming", 0.0) or 0.0)
        candidate["_family_outcome_bonus"] = float(review.get("outcome_bonus", 0.0) or 0.0)
        candidate["_family_trigger_bonus"] = float(review.get("trigger_bonus", 0.0) or 0.0)
        candidate["_family_rebalance_penalty"] = float(review.get("rebalance_penalty", 0.0) or 0.0)
        return True, None
    return False, review.get("reason") or "family_winner_preference"




def calm_pressure_quality_gate_state(candidate, reason, current_regime):
    try:
        score = float(candidate.get("score", 0.0) or 0.0)
        density = float(candidate.get("pressure_density", 0.0) or 0.0)
        pressure_count = int(candidate.get("pressure_count", 0) or 0)
        trend = float(candidate.get("price_trend_strength", 0.0) or 0.0)
        window_delta = abs(float(candidate.get("price_delta_window", 0.0) or 0.0))
    except Exception:
        score = 0.0
        density = 0.0
        pressure_count = 0
        trend = 0.0
        window_delta = 0.0

    market_type = str(candidate.get("market_type", "general_binary") or "general_binary")
    theme = candidate.get("theme") or detect_theme(candidate.get("question", ""))
    political_override = bool(candidate.get("_political_family_override", False))
    targeted_override = bool(candidate.get("_political_targeted_override", False))
    balance_rescue = bool(candidate.get("_balance_rescue_override", False))
    override_strength = float(candidate.get("_political_override_strength", 0.0) or 0.0)

    if current_regime != "calm" or reason not in {"score+pressure", "pressure"}:
        return {"block": False, "signal": None}

    weak_override_exception = (
        political_override
        and (
            (targeted_override and override_strength >= 0.24) or
            (balance_rescue and override_strength >= 0.22) or
            (override_strength >= 0.26)
        )
    )

    calm_pressure_thin_general = (
        market_type == "general_binary"
        and theme in {"politics", "general"}
        and score < 0.90
        and density < 0.40
        and pressure_count < 4
        and window_delta < 0.014
        and not weak_override_exception
    )
    calm_pressure_thin_narrative = (
        market_type == "narrative_long_tail"
        and theme in {"politics", "general"}
        and score < 1.04
        and density < 0.24
        and pressure_count < 3
        and trend < 0.92
        and window_delta < 0.012
        and not weak_override_exception
    )

    return {
        "block": bool(calm_pressure_thin_general or calm_pressure_thin_narrative),
        "signal": "calm_pressure_quality_gate" if (calm_pressure_thin_general or calm_pressure_thin_narrative) else None,
        "score": score,
        "density": density,
        "pressure_count": pressure_count,
        "trend": trend,
        "window_delta": window_delta,
        "market_type": market_type,
        "theme": theme,
        "political_override": political_override,
        "targeted_override": targeted_override,
        "balance_rescue": balance_rescue,
        "override_strength": override_strength,
    }




def calm_pressure_hard_block_state(candidate, reason, current_regime):
    state = calm_pressure_quality_gate_state(candidate, reason, current_regime)
    if current_regime != "calm" or reason not in {"score+pressure", "pressure"}:
        return {"block": False, "signal": None}

    try:
        score = float(state.get("score", candidate.get("score", 0.0)) or 0.0)
        density = float(state.get("density", candidate.get("pressure_density", 0.0)) or 0.0)
        pressure_count = int(state.get("pressure_count", candidate.get("pressure_count", 0)) or 0)
        trend = float(state.get("trend", candidate.get("price_trend_strength", 0.0)) or 0.0)
        window_delta = float(state.get("window_delta", abs(float(candidate.get("price_delta_window", 0.0) or 0.0))) or 0.0)
        market_type = str(state.get("market_type", candidate.get("market_type", "general_binary")) or "general_binary")
        theme = str(state.get("theme", candidate.get("theme") or detect_theme(candidate.get("question", ""))) or "general")
        political_override = bool(state.get("political_override", candidate.get("_political_family_override", False)))
        targeted_override = bool(state.get("targeted_override", candidate.get("_political_targeted_override", False)))
        balance_rescue = bool(state.get("balance_rescue", candidate.get("_balance_rescue_override", False)))
        override_strength = float(state.get("override_strength", candidate.get("_political_override_strength", 0.0)) or 0.0)
    except Exception:
        return {"block": False, "signal": None}

    strong_override_exception = (
        political_override and (
            (targeted_override and override_strength >= 0.32 and density >= 0.18) or
            (balance_rescue and override_strength >= 0.30 and density >= 0.16) or
            (override_strength >= 0.34 and pressure_count >= 3)
        )
    )

    hard_block_general = (
        market_type == "general_binary"
        and theme in {"politics", "general"}
        and score < 0.94
        and density < 0.44
        and pressure_count < 5
        and window_delta < 0.016
        and not strong_override_exception
    )
    hard_block_narrative = (
        market_type == "narrative_long_tail"
        and theme in {"politics", "general"}
        and score < 1.08
        and density < 0.28
        and pressure_count < 3
        and trend < 0.94
        and window_delta < 0.014
        and not strong_override_exception
    )

    return {
        "block": bool(hard_block_general or hard_block_narrative),
        "signal": "calm_pressure_hard_block" if (hard_block_general or hard_block_narrative) else None,
        "score": score,
        "density": density,
        "pressure_count": pressure_count,
        "trend": trend,
        "window_delta": window_delta,
        "market_type": market_type,
        "theme": theme,
        "political_override": political_override,
        "targeted_override": targeted_override,
        "balance_rescue": balance_rescue,
        "override_strength": override_strength,
    }


def political_rescue_scout_demotion_state(candidate, current_regime, reason=""):
    try:
        score = float(candidate.get("score", 0.0) or 0.0)
        density = float(candidate.get("pressure_density", 0.0) or 0.0)
        pressure_count = int(candidate.get("pressure_count", 0) or 0)
        trend = float(candidate.get("price_trend_strength", 0.0) or 0.0)
        window_delta = abs(float(candidate.get("price_delta_window", 0.0) or 0.0))
        market_type = str(candidate.get("market_type", "general_binary") or "general_binary")
        override_strength = float(candidate.get("_political_override_strength", 0.0) or 0.0)
        political_override = bool(candidate.get("_political_family_override", False))
        targeted_override = bool(candidate.get("_political_targeted_override", False))
        balance_rescue = bool(candidate.get("_balance_rescue_override", False))
        theme = str(candidate.get("theme") or detect_theme(candidate.get("question", "")) or "general")
    except Exception:
        return {"active": False}

    if current_regime not in {"calm", "normal"}:
        return {"active": False}
    if not political_override:
        return {"active": False}
    if market_type not in {"general_binary", "narrative_long_tail"}:
        return {"active": False}

    entry_reason = str(reason or candidate.get("reason", "") or "")
    politics_like = candidate_is_politics_like(candidate) or theme == "politics"

    flat_general = (
        market_type == "general_binary"
        and score < 0.95
        and density < 0.22
        and pressure_count < 3
        and trend < 0.92
        and window_delta < 0.012
    )
    flat_narrative = (
        market_type == "narrative_long_tail"
        and score < 1.10
        and density < 0.20
        and pressure_count < 2
        and trend < 0.90
        and window_delta < 0.010
    )
    targeted_narrative_force = (
        politics_like
        and targeted_override
        and market_type == "narrative_long_tail"
        and entry_reason in {"score+pre_momentum", "pre_momentum", "score+pressure", "pressure", "score"}
        and (
            density < 0.30 or
            pressure_count < 3 or
            trend < 0.96 or
            window_delta < 0.014
        )
    )
    balance_narrative_force = (
        politics_like
        and balance_rescue
        and market_type == "narrative_long_tail"
        and entry_reason in {"score+pre_momentum", "pre_momentum", "score", "score+pressure"}
        and (
            density < 0.26 or
            pressure_count < 2 or
            trend < 0.94 or
            window_delta < 0.012
        )
    )

    if not (flat_general or flat_narrative or targeted_narrative_force or balance_narrative_force):
        return {"active": False}

    cap = 0.95
    hold_cycles = 4
    cross_cycles = 4
    signal = "political_rescue_scout"

    if targeted_narrative_force:
        cap = 0.88 if current_regime == "calm" else 0.98
        if override_strength >= 0.30 and density >= 0.20:
            cap = 1.05
        hold_cycles = 4 if current_regime == "normal" else 3
        cross_cycles = 4 if current_regime == "normal" else 3
        signal = "targeted_political_scout_force"
    elif balance_narrative_force:
        cap = 0.84 if current_regime == "calm" else 0.94
        if override_strength >= 0.28 and density >= 0.18:
            cap = 1.00
        hold_cycles = 4
        cross_cycles = 4
        signal = "balance_rescue_scout_force"
    elif targeted_override:
        cap = 1.05 if override_strength >= 0.20 else 0.95
        hold_cycles = 5
        cross_cycles = 5
        signal = "targeted_political_rescue_scout"
    elif balance_rescue:
        cap = 0.90 if override_strength < 0.20 else 1.00
        hold_cycles = 4
        cross_cycles = 4
        signal = "balance_rescue_scout"
    else:
        cap = 0.82 if market_type == "general_binary" else 0.88
        hold_cycles = 3
        cross_cycles = 3

    return {
        "active": True,
        "cap": float(cap),
        "hold_cycles": int(hold_cycles),
        "cross_cycles": int(cross_cycles),
        "signal": signal,
        "score": score,
        "density": density,
        "pressure_count": pressure_count,
        "trend": trend,
        "window_delta": window_delta,
        "override_strength": override_strength,
    }




def pressure_decay_preentry_state(candidate, reason, current_regime):
    try:
        score = float(candidate.get("score", 0.0) or 0.0)
        density = float(candidate.get("pressure_density", 0.0) or 0.0)
        pressure_count = int(candidate.get("pressure_count", 0) or 0)
        trend = float(candidate.get("price_trend_strength", 0.0) or 0.0)
        window_delta = abs(float(candidate.get("price_delta_window", 0.0) or 0.0))
        delta_1 = abs(float(candidate.get("price_delta", 0.0) or 0.0))
        market_type = str(candidate.get("market_type", "general_binary") or "general_binary")
        theme = str(candidate.get("theme") or detect_theme(candidate.get("question", "")) or "general")
        political_override = bool(candidate.get("_political_family_override", False))
        targeted_override = bool(candidate.get("_political_targeted_override", False))
        balance_rescue = bool(candidate.get("_balance_rescue_override", False))
        override_strength = float(candidate.get("_political_override_strength", 0.0) or 0.0)
    except Exception:
        return {"block": False, "signal": None}

    if current_regime != "normal":
        return {"block": False, "signal": None}
    if reason not in {"pressure", "score+pressure"}:
        return {"block": False, "signal": None}

    strong_override_exception = (
        political_override and (
            (targeted_override and override_strength >= 0.32 and density >= 0.18) or
            (balance_rescue and override_strength >= 0.30 and density >= 0.16) or
            (override_strength >= 0.34 and pressure_count >= 3)
        )
    )

    general_flatline_risk = (
        market_type == "general_binary"
        and theme in {"politics", "general"}
        and score < 0.78
        and density >= 0.45
        and pressure_count >= 3
        and window_delta <= 0.0025
        and delta_1 <= 0.0035
        and trend < 0.78
        and not strong_override_exception
    )

    sports_pressure_churn_risk = (
        market_type == "sports_award_longshot"
        and score < 1.02
        and density >= 0.45
        and pressure_count >= 3
        and window_delta <= 0.0020
        and delta_1 <= 0.0035
        and trend < 0.80
    )

    return {
        "block": bool(general_flatline_risk or sports_pressure_churn_risk),
        "signal": "pressure_decay_preentry_gate" if (general_flatline_risk or sports_pressure_churn_risk) else None,
        "score": score,
        "density": density,
        "pressure_count": pressure_count,
        "trend": trend,
        "window_delta": window_delta,
        "delta_1": delta_1,
        "market_type": market_type,
        "theme": theme,
        "political_override": political_override,
        "override_strength": override_strength,
    }


def weak_sports_override_brake_state(candidate, current_regime, reason=""):
    try:
        score = float(candidate.get("score", 0.0) or 0.0)
        density = float(candidate.get("pressure_density", 0.0) or 0.0)
        pressure_count = int(candidate.get("pressure_count", 0) or 0)
        trend = float(candidate.get("price_trend_strength", 0.0) or 0.0)
        window_delta = abs(float(candidate.get("price_delta_window", 0.0) or 0.0))
        market_type = str(candidate.get("market_type", "general_binary") or "general_binary")
    except Exception:
        return {"active": False, "hard_block": False}

    if current_regime not in {"calm", "normal"}:
        return {"active": False, "hard_block": False}
    if market_type != "sports_award_longshot":
        return {"active": False, "hard_block": False}

    entry_reason = str(reason or candidate.get("reason", "") or "")
    if entry_reason not in {"multicycle_momentum_override", "momentum_override", "score+pressure"}:
        return {"active": False, "hard_block": False}

    ultra_weak_override = (
        entry_reason in {"multicycle_momentum_override", "momentum_override"}
        and score < 0.45
        and density < 0.40
        and pressure_count < 3
    )

    if ultra_weak_override:
        return {
            "active": True,
            "hard_block": True,
            "cap": 0.0,
            "signal": "weak_sports_override_hard_block",
            "score": score,
            "density": density,
            "pressure_count": pressure_count,
            "trend": trend,
            "window_delta": window_delta,
            "market_type": market_type,
        }

    weak_override = (
        (entry_reason in {"multicycle_momentum_override", "momentum_override"} and score < 0.90)
        or
        (entry_reason == "score+pressure" and score < 1.02 and density >= 0.45 and pressure_count >= 3 and window_delta <= 0.0020 and trend < 0.80)
    )

    if not weak_override:
        return {"active": False, "hard_block": False}

    cap = 1.05 if current_regime == "normal" else 0.92
    if score < 0.65:
        cap = 0.88 if current_regime == "normal" else 0.78

    return {
        "active": True,
        "hard_block": False,
        "cap": float(cap),
        "signal": "weak_sports_override_brake",
        "score": score,
        "density": density,
        "pressure_count": pressure_count,
        "trend": trend,
        "window_delta": window_delta,
        "market_type": market_type,
    }


def narrative_full_size_brake_state(candidate, current_regime, reason=""):
    try:
        score = float(candidate.get("score", 0.0) or 0.0)
        density = float(candidate.get("pressure_density", 0.0) or 0.0)
        pressure_count = int(candidate.get("pressure_count", 0) or 0)
        trend = float(candidate.get("price_trend_strength", 0.0) or 0.0)
        window_delta = abs(float(candidate.get("price_delta_window", 0.0) or 0.0))
        market_type = str(candidate.get("market_type", "general_binary") or "general_binary")
        override_strength = float(candidate.get("_political_override_strength", 0.0) or 0.0)
        political_override = bool(candidate.get("_political_family_override", False))
        targeted_override = bool(candidate.get("_political_targeted_override", False))
        balance_rescue = bool(candidate.get("_balance_rescue_override", False))
    except Exception:
        return {"active": False}

    if current_regime not in {"calm", "normal"}:
        return {"active": False}
    if market_type != "narrative_long_tail":
        return {"active": False}
    if not candidate_is_politics_like(candidate):
        return {"active": False}

    entry_reason = str(reason or candidate.get("reason", "") or "")
    if entry_reason not in {"score+pre_momentum", "pre_momentum", "score+pressure", "pressure", "score"}:
        return {"active": False}

    low_fuel = (
        density < 0.32
        and pressure_count < 3
        and trend < 0.97
        and window_delta < 0.015
    )
    if not low_fuel:
        return {"active": False}

    cap = 1.10 if current_regime == "normal" else 0.95
    signal = "narrative_full_size_brake"
    if targeted_override:
        cap = 1.00 if override_strength < 0.34 else 1.15
        signal = "targeted_narrative_full_size_brake"
    elif balance_rescue:
        cap = 0.92 if override_strength < 0.30 else 1.05
        signal = "balance_narrative_full_size_brake"
    elif political_override:
        cap = 0.90 if current_regime == "calm" else 1.00

    return {
        "active": True,
        "cap": float(cap),
        "signal": signal,
        "score": score,
        "density": density,
        "pressure_count": pressure_count,
        "trend": trend,
        "window_delta": window_delta,
        "override_strength": override_strength,
    }



def flat_signal_nullifier_state(candidate, reason, current_regime):
    try:
        score = float(candidate.get("score", 0.0) or 0.0)
        density = float(candidate.get("pressure_density", 0.0) or 0.0)
        pressure_count = int(candidate.get("pressure_count", 0) or 0)
        trend = abs(float(candidate.get("price_trend_strength", 0.0) or 0.0))
        window_delta = abs(float(candidate.get("price_delta_window", 0.0) or 0.0))
        delta = abs(float(candidate.get("price_delta", 0.0) or 0.0))
        market_type = str(candidate.get("market_type", "general_binary") or "general_binary")
    except Exception:
        return {"active": False}

    if reason != "score":
        return {"active": False}
    if current_regime not in {"calm", "normal"}:
        return {"active": False}
    if market_type not in {"general_binary", "narrative_long_tail", "valuation_ladder", "speculative_hype", "short_burst_catalyst", "sports_award_longshot"}:
        return {"active": False}

    delayed_confirmed = bool(candidate.get("_delayed_entry_confirmed", False))
    delayed_memory = bool(candidate.get("delayed_entry_memory_active", False) or candidate.get("_delayed_entry_memory_active", False))
    political_override = bool(candidate.get("_political_family_override", False))
    targeted_override = bool(candidate.get("_political_targeted_override", False))
    balance_rescue = bool(candidate.get("_balance_rescue_override", False))
    override_strength = float(candidate.get("_political_override_strength", 0.0) or 0.0)

    strong_override = bool(
        political_override and (
            (targeted_override and override_strength >= 0.34)
            or (balance_rescue and override_strength >= 0.32)
            or override_strength >= 0.40
        )
    )
    if delayed_confirmed or strong_override:
        return {"active": False}

    flat_zero = density <= 1e-9 and pressure_count == 0 and trend <= 1e-9 and window_delta <= 1e-9 and delta <= 1e-9
    micro_flat = density <= 0.02 and pressure_count == 0 and trend <= 0.10 and window_delta <= 0.0005 and delta <= 0.0005
    if not flat_zero and not micro_flat:
        return {"active": False}

    if market_type in {"general_binary", "narrative_long_tail"}:
        threshold = 1.18 if current_regime == "calm" else 1.12
    elif market_type == "valuation_ladder":
        threshold = 1.22 if current_regime == "calm" else 1.16
    elif market_type == "speculative_hype":
        threshold = 1.18 if current_regime == "calm" else 1.12
    elif market_type == "short_burst_catalyst":
        threshold = 1.30 if current_regime == "calm" else 1.24
    elif market_type == "sports_award_longshot":
        threshold = 1.02 if current_regime == "calm" else 0.98
    else:
        threshold = 1.14 if current_regime == "calm" else 1.08

    if delayed_memory:
        threshold += 0.08

    if score >= threshold:
        return {"active": False}

    return {
        "active": True,
        "signal": "flat_signal_nullifier",
        "threshold": float(round(threshold, 4)),
        "flat_zero": bool(flat_zero),
        "micro_flat": bool(micro_flat),
        "score": score,
        "density": density,
        "pressure_count": pressure_count,
        "trend": trend,
        "window_delta": window_delta,
        "delta": delta,
        "market_type": market_type,
    }


def entry_quality_gate(candidate, reason, current_regime, market_exit_memory):
    key = build_market_key(candidate)
    memory = dict(market_exit_memory.get(key, {}) or {})

    try:
        score = float(candidate.get("score", 0.0) or 0.0)
        density = float(candidate.get("pressure_density", 0.0) or 0.0)
        pressure_count = int(candidate.get("pressure_count", 0) or 0)
        trend = float(candidate.get("price_trend_strength", 0.0) or 0.0)
        window_delta = abs(float(candidate.get("price_delta_window", 0.0) or 0.0))
        delta = abs(float(candidate.get("price_delta", 0.0) or 0.0))
    except Exception:
        score = 0.0
        density = 0.0
        pressure_count = 0
        trend = 0.0
        window_delta = 0.0
        delta = 0.0

    market_type = candidate.get("market_type", "general_binary")
    theme = candidate.get("theme") or detect_theme(candidate.get("question", ""))
    source = candidate.get("_universe_source") or "primary"
    weak_structure = density < 0.16 and pressure_count < 2 and trend < 0.80 and window_delta < 0.008 and delta < 0.006
    long_tail_type = market_type in {"narrative_long_tail", "valuation_ladder", "speculative_hype", "general_binary"}

    structure_votes = 0
    if density >= 0.16:
        structure_votes += 1
    if pressure_count >= 2:
        structure_votes += 1
    if trend >= 0.82:
        structure_votes += 1
    if window_delta >= 0.006:
        structure_votes += 1
    if delta >= 0.006:
        structure_votes += 1
    if score >= 1.10:
        structure_votes += 1

    calm_flex_live, calm_flex_borderline = maybe_log_calm_flex_context(
        current_regime,
        reason,
        score,
        density,
        trend,
        window_delta,
        pressure_count,
        candidate.get("question", ""),
    )
    delayed_memory = bool(candidate.get("delayed_entry_memory_active", False))
    calm_pressure_relief, calm_pressure_relief_borderline = maybe_log_calm_pressure_relief_context(
        current_regime,
        reason,
        score,
        density,
        trend,
        window_delta,
        pressure_count,
        delta,
        delayed_memory,
        candidate.get("question", ""),
    )

    adaptive_calm_relief_state = adaptive_calm_admission_relief_state(
        candidate,
        reason,
        current_regime,
        market_exit_memory=market_exit_memory,
    )
    adaptive_calm_relief = bool(adaptive_calm_relief_state.get("active", False))
    if adaptive_calm_relief:
        candidate["_adaptive_calm_relief_active"] = True
        candidate["_adaptive_calm_relief_signal"] = adaptive_calm_relief_state.get("signal")
        candidate["_adaptive_calm_relief_cap"] = float(adaptive_calm_relief_state.get("cap", 0.0) or 0.0)
        candidate["_adaptive_calm_relief_force_delayed"] = bool(adaptive_calm_relief_state.get("force_delayed", False))
        candidate["_adaptive_calm_relief_micro_scout"] = bool(adaptive_calm_relief_state.get("micro_scout", False))
        print(
            "TRACE | adaptive_calm_admission_relief | signal={} | cap={:.2f} | force_delayed={} | micro_scout={} | reason={} | score={:.3f} | pd={:.3f} | trend={:.3f} | delta1={:.3f} | win={:.3f} | pcount={} | {}".format(
                str(candidate.get("_adaptive_calm_relief_signal") or "adaptive_calm_admission_relief"),
                float(candidate.get("_adaptive_calm_relief_cap", 0.0) or 0.0),
                int(bool(candidate.get("_adaptive_calm_relief_force_delayed", False))),
                int(bool(candidate.get("_adaptive_calm_relief_micro_scout", False))),
                reason,
                score,
                density,
                trend,
                delta,
                window_delta,
                pressure_count,
                candidate.get("question", ""),
            )
        )
        calm_pressure_relief = True

    political_override = bool(candidate.get("_political_family_override", False))
    targeted_override = bool(candidate.get("_political_targeted_override", False))
    balance_rescue = bool(candidate.get("_balance_rescue_override", False))
    override_strength = float(candidate.get("_political_override_strength", 0.0) or 0.0)

    flat_null_state = flat_signal_nullifier_state(candidate, reason, current_regime)
    if flat_null_state.get("active", False):
        print(
            "TRACE | flat_signal_nullifier | signal={} | market_type={} | score={:.3f} | thr={:.3f} | pd={:.3f} | trend={:.3f} | delta1={:.4f} | win={:.4f} | pcount={} | {}".format(
                flat_null_state.get("signal", "flat_signal_nullifier"),
                flat_null_state.get("market_type", market_type),
                float(flat_null_state.get("score", score) or score),
                float(flat_null_state.get("threshold", 0.0) or 0.0),
                float(flat_null_state.get("density", density) or density),
                float(flat_null_state.get("trend", trend) or trend),
                float(flat_null_state.get("delta", delta) or delta),
                float(flat_null_state.get("window_delta", window_delta) or window_delta),
                int(flat_null_state.get("pressure_count", pressure_count) or pressure_count),
                (candidate.get("question") or "")[:72]
            )
        )
        return False, flat_null_state.get("signal", "flat_signal_nullifier")

    calm_pressure_state = calm_pressure_quality_gate_state(candidate, reason, current_regime)
    if calm_pressure_state.get("block", False):
        print(
            "TRACE | calm_pressure_quality_gate | verdict=skip | market_type={} | reason={} | score={:.3f} | pd={:.3f} | trend={:.3f} | win={:.3f} | pcount={} | override={} | targeted={} | balance={} | strength={:.3f} | {}".format(
                calm_pressure_state.get("market_type", market_type),
                reason,
                float(calm_pressure_state.get("score", score) or score),
                float(calm_pressure_state.get("density", density) or density),
                float(calm_pressure_state.get("trend", trend) or trend),
                float(calm_pressure_state.get("window_delta", window_delta) or window_delta),
                int(calm_pressure_state.get("pressure_count", pressure_count) or pressure_count),
                int(bool(calm_pressure_state.get("political_override", political_override))),
                int(bool(calm_pressure_state.get("targeted_override", targeted_override))),
                int(bool(calm_pressure_state.get("balance_rescue", balance_rescue))),
                float(calm_pressure_state.get("override_strength", override_strength) or override_strength),
                (candidate.get("question") or "")[:72]
            )
        )
        return False, "calm_pressure_quality_gate"

    calm_pressure_hard_state = calm_pressure_hard_block_state(candidate, reason, current_regime)
    if calm_pressure_hard_state.get("block", False):
        print(
            "TRACE | calm_pressure_hard_block | verdict=skip | market_type={} | reason={} | score={:.3f} | pd={:.3f} | trend={:.3f} | win={:.3f} | pcount={} | override={} | targeted={} | balance={} | strength={:.3f} | {}".format(
                calm_pressure_hard_state.get("market_type", market_type),
                reason,
                float(calm_pressure_hard_state.get("score", score) or score),
                float(calm_pressure_hard_state.get("density", density) or density),
                float(calm_pressure_hard_state.get("trend", trend) or trend),
                float(calm_pressure_hard_state.get("window_delta", window_delta) or window_delta),
                int(calm_pressure_hard_state.get("pressure_count", pressure_count) or pressure_count),
                int(bool(calm_pressure_hard_state.get("political_override", political_override))),
                int(bool(calm_pressure_hard_state.get("targeted_override", targeted_override))),
                int(bool(calm_pressure_hard_state.get("balance_rescue", balance_rescue))),
                float(calm_pressure_hard_state.get("override_strength", override_strength) or override_strength),
                (candidate.get("question") or "")[:72]
            )
        )
        return False, "calm_pressure_hard_block"

    pressure_decay_state = pressure_decay_preentry_state(candidate, reason, current_regime)
    if pressure_decay_state.get("block", False):
        print(
            "TRACE | pressure_decay_preentry_gate | verdict=skip | market_type={} | reason={} | score={:.3f} | pd={:.3f} | trend={:.3f} | delta1={:.3f} | win={:.3f} | pcount={} | override={} | strength={:.3f} | {}".format(
                pressure_decay_state.get("market_type", market_type),
                reason,
                float(pressure_decay_state.get("score", score) or score),
                float(pressure_decay_state.get("density", density) or density),
                float(pressure_decay_state.get("trend", trend) or trend),
                float(pressure_decay_state.get("delta_1", delta) or delta),
                float(pressure_decay_state.get("window_delta", window_delta) or window_delta),
                int(pressure_decay_state.get("pressure_count", pressure_count) or pressure_count),
                int(bool(pressure_decay_state.get("political_override", False))),
                float(pressure_decay_state.get("override_strength", 0.0) or 0.0),
                (candidate.get("question") or "")[:72]
            )
        )
        return False, "pressure_decay_preentry_gate"

    if is_selective_aggression_candidate(candidate, reason, current_regime):
        if current_regime == "calm":
            if calm_flex_live:
                print("TRACE | calm_flex_final_admit | reason={} | score={:.3f} | pd={:.3f} | trend={:.3f} | win={:.3f} | pcount={} | {}".format(
                    reason, score, density, trend, window_delta, pressure_count, candidate.get("question", "")
                ))
            elif calm_pressure_relief:
                print("TRACE | calm_pressure_backed_re_admit | layer=selective | reason={} | score={:.3f} | pd={:.3f} | trend={:.3f} | delta1={:.3f} | win={:.3f} | pcount={} | delayed={} | {}".format(
                    reason, score, density, trend, delta, window_delta, pressure_count, int(delayed_memory), candidate.get("question", "")
                ))
            elif political_override:
                print("TRACE | calm_override_admit | layer=selective | reason={} | score={:.3f} | pd={:.3f} | trend={:.3f} | win={:.3f} | pcount={} | strength={:.3f} | targeted={} | balance_rescue={} | {}".format(
                    reason, score, density, trend, window_delta, pressure_count, override_strength, int(bool(targeted_override)), int(bool(balance_rescue)), candidate.get("question", "")
                ))
            else:
                print("TRACE | calm_base_admit | layer=selective | reason={} | score={:.3f} | pd={:.3f} | trend={:.3f} | win={:.3f} | pcount={} | structure_votes={} | {}".format(
                    reason, score, density, trend, window_delta, pressure_count, structure_votes, candidate.get("question", "")
                ))
        return True, None

    if current_regime == "calm" and not calm_flex_live and not calm_pressure_relief and not political_override and source != "explorer":
        pre_signal = bool(candidate.get("pre_momentum", False)) or bool(candidate.get("pressure_entry", False))
        if structure_votes == 0 and score < 1.42 and not bool(candidate.get("already_open", False)):
            return False, "calm_authority_gate"
        if reason == "score" and structure_votes <= 1 and score < 1.50 and not delayed_memory and not pre_signal and not bool(candidate.get("already_open", False)):
            return False, "calm_authority_score_only"

    override_strength = float(candidate.get("_political_override_strength", 0.0) or 0.0)

    if reason == "score":
        targeted_override = bool(candidate.get("_political_targeted_override", False))
        balance_rescue = bool(candidate.get("_balance_rescue_override", False))
        if current_regime == "calm" and weak_structure and score < 0.94 and not (calm_flex_live or calm_pressure_relief) and not (political_override and override_strength >= (0.10 if balance_rescue else (0.12 if targeted_override else 0.14))):
            return False, "score_gate_calm_noise"
        elif current_regime == "calm" and (calm_flex_live or calm_pressure_relief) and weak_structure and score < 0.94:
            print("TRACE | calm_flex_admit | reason={} | score={:.3f} | pd={:.3f} | trend={:.3f} | win={:.3f} | pcount={} | {}".format(
                reason, score, density, trend, window_delta, pressure_count, candidate.get("question", "")
            ))
        if current_regime == "calm" and long_tail_type and density < 0.18 and trend < 0.84 and window_delta < 0.010 and score < 0.99 and not (calm_flex_live or calm_pressure_relief) and not (political_override and override_strength >= (0.12 if balance_rescue else (0.14 if targeted_override else 0.16))):
            return False, "score_gate_calm_long_tail"
        if current_regime in {"calm", "normal"} and long_tail_type and weak_structure and score < 0.91:
            return False, "score_gate_weak_structure"
        if current_regime == "normal" and reason == "score":
            if market_type in {"general_binary", "narrative_long_tail", "valuation_ladder"} and weak_structure and score < 1.02:
                return False, "normal_score_gate"
            if market_type == "sports_award_longshot" and weak_structure and score < 0.96:
                return False, "normal_score_gate_sports"
            if market_type == "speculative_hype" and weak_structure and score < 1.08:
                return False, "normal_score_gate_speculative"

        if source != "explorer" and current_regime == "calm" and long_tail_type:
            targeted_override = bool(candidate.get("_political_targeted_override", False))
            balance_rescue = bool(candidate.get("_balance_rescue_override", False))
            if structure_votes < 2 and score < 1.18 and not (calm_flex_live or calm_pressure_relief) and not (political_override and override_strength >= (0.10 if balance_rescue else (0.12 if targeted_override else 0.14))):
                return False, "primary_calm_gate"
            if market_type in {"general_binary", "narrative_long_tail", "valuation_ladder"} and theme in {"general", "politics", "sports"} and score < 1.28 and not (calm_flex_live or calm_pressure_relief) and not (political_override and override_strength >= (0.12 if balance_rescue else (0.14 if targeted_override else 0.16))) and structure_votes < (2 if balance_rescue else 3):
                return False, "primary_calm_long_tail"
            if market_type == "speculative_hype" and theme != "tech" and structure_votes < 2 and score < 1.24 and not (calm_flex_live or calm_pressure_relief) and not (political_override and override_strength >= (0.14 if balance_rescue else (0.16 if targeted_override else 0.18))):
                return False, "primary_calm_speculative"

    weak_failed_count = int(memory.get("weak_failed_count", 0) or 0)
    dead_exit_count = int(memory.get("dead_exit_count", 0) or 0)
    stale_exit_count = int(memory.get("stale_exit_count", 0) or 0)

    political_override = bool(candidate.get("_political_family_override", False))
    targeted_override = bool(candidate.get("_political_targeted_override", False))
    override_strength = float(candidate.get("_political_override_strength", 0.0) or 0.0)

    balance_rescue = bool(candidate.get("_balance_rescue_override", False))
    zombie_exempt = political_override and (
        (balance_rescue and override_strength >= 0.12) or
        (targeted_override and override_strength >= 0.14) or
        (override_strength >= 0.18)
    )

    recovery_router_state = observable_recovery_router_state(candidate, reason, market_exit_memory)
    if recovery_router_state.get("considered", False):
        print(
            "TRACE | recovery_route_seen | market_type={} | reason={} | score={:.3f} | pd={:.3f} | trend={:.3f} | delta1={:.4f} | win={:.4f} | votes={} | mem={} | {}".format(
                recovery_router_state.get("market_type", market_type),
                reason,
                float(recovery_router_state.get("score", score) or score),
                float(recovery_router_state.get("density", density) or density),
                float(recovery_router_state.get("trend", trend) or trend),
                float(recovery_router_state.get("delta", delta) or delta),
                float(recovery_router_state.get("window_delta", window_delta) or window_delta),
                int(recovery_router_state.get("votes", reentry_signal_votes(candidate)) or reentry_signal_votes(candidate)),
                int(recovery_router_state.get("memory_pressure", 0) or 0),
                (candidate.get("question") or "")[:72],
            )
        )
    elite_recovery_state = recovery_router_state
    elite_recovery_active = bool(elite_recovery_state.get("active", False))
    if elite_recovery_active:
        candidate["_elite_recovery_override"] = True
        candidate["_elite_recovery_signal"] = elite_recovery_state.get("signal")
        clamp_state = elite_recovery_clamp_state(candidate)
        candidate["_elite_recovery_clamp_active"] = bool(clamp_state.get("active", False))
        candidate["_elite_recovery_clamp_cap"] = float(clamp_state.get("cap", 0.0) or 0.0)
        candidate["_elite_recovery_force_delayed"] = bool(clamp_state.get("force_delayed", False))
        candidate["_elite_recovery_micro_scout"] = bool(clamp_state.get("micro_scout", False))
        print(
            "TRACE | recovery_route_activate | signal={} | market_type={} | score={:.3f} | pd={:.3f} | trend={:.3f} | delta1={:.4f} | win={:.4f} | votes={} | mem={} | {}".format(
                elite_recovery_state.get("signal", "elite_recovery_override"),
                elite_recovery_state.get("market_type", market_type),
                float(elite_recovery_state.get("score", score) or score),
                float(elite_recovery_state.get("density", density) or density),
                float(elite_recovery_state.get("trend", trend) or trend),
                float(elite_recovery_state.get("delta", delta) or delta),
                float(elite_recovery_state.get("window_delta", window_delta) or window_delta),
                int(elite_recovery_state.get("votes", reentry_signal_votes(candidate)) or reentry_signal_votes(candidate)),
                int(elite_recovery_state.get("memory_pressure", 0) or 0),
                (candidate.get("question") or "")[:72],
            )
        )
        print(
            "TRACE | recovery_clamp_prime | active=1 | signal={} | cap={:.2f} | force_delayed={} | micro_scout={} | market_type={} | {}".format(
                str(candidate.get("_elite_recovery_signal") or "elite_recovery_override"),
                float(candidate.get("_elite_recovery_clamp_cap", 0.0) or 0.0),
                int(bool(candidate.get("_elite_recovery_force_delayed", False))),
                int(bool(candidate.get("_elite_recovery_micro_scout", False))),
                str(candidate.get("market_type", "general_binary") or "general_binary"),
                (candidate.get("question") or "")[:72],
            )
        )
    elif recovery_router_state.get("considered", False):
        print(
            "TRACE | recovery_route_reject | reason={} | market_type={} | score={:.3f} | pd={:.3f} | trend={:.3f} | delta1={:.4f} | win={:.4f} | votes={} | mem={} | {}".format(
                recovery_router_state.get("reject_reason", "activation_conditions_not_met"),
                recovery_router_state.get("market_type", market_type),
                float(recovery_router_state.get("score", score) or score),
                float(recovery_router_state.get("density", density) or density),
                float(recovery_router_state.get("trend", trend) or trend),
                float(recovery_router_state.get("delta", delta) or delta),
                float(recovery_router_state.get("window_delta", window_delta) or window_delta),
                int(recovery_router_state.get("votes", reentry_signal_votes(candidate)) or reentry_signal_votes(candidate)),
                int(recovery_router_state.get("memory_pressure", 0) or 0),
                (candidate.get("question") or "")[:72],
            )
        )

    relief_router_state = selective_overblock_relief_state(
        candidate,
        reason,
        market_exit_memory=market_exit_memory,
        current_regime=current_regime,
    )

    if weak_failed_count >= 2 and not has_strong_reentry_signal(candidate, reason) and not zombie_exempt and not elite_recovery_active:
        return False, "anti_zombie_memory"

    if dead_exit_count >= 2 and reentry_signal_votes(candidate) < 4 and not ((balance_rescue and override_strength >= 0.14) or (targeted_override and override_strength >= 0.18)) and not elite_recovery_active:
        if relief_router_state.get("active", False) and relief_router_state.get("allow_dead_market", False):
            candidate = prime_relief_escalation(candidate, relief_router_state, "dead_market_memory")
        else:
            return False, "dead_market_memory"

    if stale_exit_count >= 2 and reason == "score" and reentry_signal_votes(candidate) < 4 and not ((balance_rescue and override_strength >= 0.12) or (targeted_override and override_strength >= 0.16)) and not elite_recovery_active:
        if relief_router_state.get("active", False) and relief_router_state.get("allow_stale_market", False):
            candidate = prime_relief_escalation(candidate, relief_router_state, "stale_market_memory")
        else:
            return False, "stale_market_memory"

    if candidate.get("_universe_source") == "explorer":
        explorer_structure_votes = 0
        if density >= 0.16:
            explorer_structure_votes += 1
        if pressure_count >= 2:
            explorer_structure_votes += 1
        if trend >= 0.82:
            explorer_structure_votes += 1
        if window_delta >= 0.006:
            explorer_structure_votes += 1
        if delta >= 0.006:
            explorer_structure_votes += 1
        if score >= 0.88:
            explorer_structure_votes += 1

        if explorer_structure_votes < 2:
            return False, "explorer_quality_gate"

        if reason == "score" and current_regime == "calm" and explorer_structure_votes < 3:
            return False, "explorer_score_gate"

        if reason == "score" and market_type in {"general_binary", "narrative_long_tail", "sports_award_longshot"} and explorer_structure_votes < 4 and score < 0.96:
            return False, "explorer_quality_gate"

        if dead_exit_count >= 1 and not has_strong_reentry_signal(candidate, reason):
            return False, "explorer_dead_memory"

    if current_regime == "calm":
        if calm_flex_live:
            print("TRACE | calm_flex_final_admit | reason={} | score={:.3f} | pd={:.3f} | trend={:.3f} | win={:.3f} | pcount={} | {}".format(
                reason, score, density, trend, window_delta, pressure_count, candidate.get("question", "")
            ))
        elif calm_pressure_relief:
            print("TRACE | calm_pressure_backed_re_admit | layer=baseline | reason={} | score={:.3f} | pd={:.3f} | trend={:.3f} | delta1={:.3f} | win={:.3f} | pcount={} | delayed={} | {}".format(
                reason, score, density, trend, delta, window_delta, pressure_count, int(delayed_memory), candidate.get("question", "")
            ))
        elif political_override:
            print("TRACE | calm_override_admit | layer=baseline | reason={} | score={:.3f} | pd={:.3f} | trend={:.3f} | win={:.3f} | pcount={} | strength={:.3f} | targeted={} | balance_rescue={} | {}".format(
                reason, score, density, trend, window_delta, pressure_count, override_strength, int(bool(targeted_override)), int(bool(balance_rescue)), candidate.get("question", "")
            ))
        else:
            print("TRACE | calm_base_admit | layer=baseline | reason={} | score={:.3f} | pd={:.3f} | trend={:.3f} | win={:.3f} | pcount={} | structure_votes={} | {}".format(
                reason, score, density, trend, window_delta, pressure_count, structure_votes, candidate.get("question", "")
            ))
    return True, None


def should_attempt_family_replacement(engine, candidate, reason, current_regime):
    family_key = candidate.get("family_key")
    if not family_key:
        return False

    if not engine.has_open_family(family_key):
        return False

    score = float(candidate.get("score", 0.0) or 0.0)
    survival = float(score_survival_priority(candidate, reason, engine))
    pressure = float(candidate.get("pressure_density", 0.0) or 0.0)
    trend = float(candidate.get("price_trend_strength", 0.0) or 0.0)

    if reason in {"score+pressure", "score+momentum", "multicycle_momentum_override", "momentum_override", "score+pre_momentum"} and survival >= 1.05:
        return True

    if score >= 0.92 and (pressure >= 0.18 or trend >= 0.88 or survival >= 1.12):
        return True

    if current_regime in {"normal", "hot"} and survival >= 1.20 and score >= 0.88:
        return True

    return False


def family_attack_score(candidate, reason, current_regime, engine=None):
    score = float(candidate.get("score", 0.0) or 0.0)
    density = float(candidate.get("pressure_density", 0.0) or 0.0)
    trend = float(candidate.get("price_trend_strength", 0.0) or 0.0)
    window_delta = abs(float(candidate.get("price_delta_window", 0.0) or 0.0))
    price_delta = abs(float(candidate.get("price_delta", 0.0) or 0.0))
    pressure_count = int(candidate.get("pressure_count", 0) or 0)
    market_type = candidate.get("market_type", "general_binary")

    if engine is not None:
        survival = float(score_survival_priority(candidate, reason, engine))
    else:
        survival = score

    structure_votes = 0
    if density >= 0.18:
        structure_votes += 1
    if pressure_count >= 2:
        structure_votes += 1
    if trend >= 0.84:
        structure_votes += 1
    if window_delta >= 0.006:
        structure_votes += 1
    if price_delta >= 0.004:
        structure_votes += 1

    attack = survival + min(score, 1.6) * 0.18
    attack += structure_votes * 0.08
    attack += min(density, 0.5) * 0.32
    attack += min(trend, 1.0) * 0.16

    if reason in {"score+pressure", "score+momentum", "multicycle_momentum_override", "momentum_override", "score+pre_momentum"}:
        attack += 0.18
    elif reason in {"pre_momentum", "pressure", "momentum"}:
        attack += 0.11

    if current_regime == "calm" and market_type in {"legal_resolution", "short_burst_catalyst", "speculative_hype"} and structure_votes >= 2:
        attack += 0.08
    if current_regime == "calm" and market_type in {"narrative_long_tail", "valuation_ladder"} and structure_votes < 2:
        attack -= 0.12
    if market_type == "sports_award_longshot" and structure_votes >= 2:
        attack += 0.06

    return round(attack, 4)


def is_selective_aggression_candidate(candidate, reason, current_regime, engine=None):
    score = float(candidate.get("score", 0.0) or 0.0)
    density = float(candidate.get("pressure_density", 0.0) or 0.0)
    trend = float(candidate.get("price_trend_strength", 0.0) or 0.0)
    window_delta = abs(float(candidate.get("price_delta_window", 0.0) or 0.0))
    pressure_count = int(candidate.get("pressure_count", 0) or 0)
    market_type = candidate.get("market_type", "general_binary")

    strong_structure = (
        (density >= 0.12 and pressure_count >= 1 and trend >= 0.84) or
        (window_delta >= 0.006 and trend >= 0.84) or
        (pressure_count >= 2 and window_delta >= 0.004)
    )
    elite_reason = reason in {"score+pressure", "score+momentum", "multicycle_momentum_override", "momentum_override", "score+pre_momentum", "pre_momentum", "pressure"}
    survival = family_attack_score(candidate, reason, current_regime, engine=engine)

    if not strong_structure:
        return False
    if current_regime == "calm":
        if market_type == "general_binary" and elite_reason:
            if score < 0.90 and survival < 1.60:
                return False
            if score < 0.94 and density < 0.34 and window_delta < 0.010 and survival < 1.68:
                return False
        if market_type == "narrative_long_tail" and elite_reason:
            if score < 1.04 and density < 0.20 and trend < 0.90 and survival < 1.50:
                return False
        if elite_reason and survival >= 1.02:
            return True
        if market_type in {"legal_resolution", "short_burst_catalyst", "speculative_hype"} and score >= 0.82 and survival >= 0.98:
            return True
    if current_regime == "normal":
        if elite_reason and survival >= 0.94:
            return True
        if score >= 0.95 and strong_structure:
            return True
    return False


def resolve_family_conflicts(candidates, current_regime, engine=None, delayed_entry_memory=None):
    family_groups = {}
    ordered = []
    for c in candidates:
        family_key = c.get("family_key") or build_market_key(c)
        family_groups.setdefault(family_key, []).append(c)
        if family_key not in ordered:
            ordered.append(family_key)

    resolved = []
    for family_key in ordered:
        items = list(family_groups.get(family_key, []))
        if len(items) == 1:
            resolved.append(items[0])
            continue

        prelim = []
        for c in items:
            reason = c.get("_entry_reason") or "score"
            attack = family_attack_score(c, reason, current_regime, engine=engine)
            contender_bonus = delayed_family_memory_strength(c, delayed_entry_memory)
            force_bonus = 0.10 if c.get("_family_force_review") else 0.0
            base_effective = float(attack + contender_bonus + force_bonus)
            c["_family_attack_score"] = attack
            c["_family_contender_bonus"] = round(contender_bonus + force_bonus, 4)
            prelim.append((base_effective, attack, contender_bonus + force_bonus, c))

        prelim.sort(key=lambda x: x[0], reverse=True)
        provisional_effective, provisional_attack, provisional_bonus, provisional_primary = prelim[0]

        final_rows = []
        for base_effective, raw_attack, base_bonus, cand in prelim:
            if cand is provisional_primary:
                sibling_bonus = 0.0
                effective_attack = float(base_effective)
            else:
                sibling_bonus = family_sibling_review_bonus(cand, provisional_effective, base_effective, current_regime)
                effective_attack = float(base_effective + sibling_bonus)
            cand["_family_effective_attack"] = effective_attack
            cand["_family_contender_bonus"] = round(base_bonus + sibling_bonus, 4)
            final_rows.append((effective_attack, raw_attack, base_bonus + sibling_bonus, cand))

        final_rows.sort(key=lambda x: x[0], reverse=True)
        primary_effective, primary_attack, primary_bonus, primary = final_rows[0]
        primary_type = primary.get("market_type", "general_binary")
        resolved.append(primary)

        if primary is not provisional_primary:
            print("INFO | family_swap_sanity | family={} | new_primary_score={:.3f} | old_primary_score={:.3f} | {}".format(
                (family_key or "unknown")[:92],
                primary_effective,
                provisional_effective,
                (primary.get("question") or "")[:110]
            ))

        for effective_attack, raw_attack, contender_bonus, cand in final_rows[1:]:
            reason = cand.get("_entry_reason") or "score"
            structure_ok = is_selective_aggression_candidate(cand, reason, current_regime, engine=engine)
            diff = primary_effective - effective_attack
            contender_ready = (
                float(cand.get("_family_contender_bonus", 0.0) or 0.0) >= 0.08 or
                bool(cand.get("_delayed_entry_confirmed", False)) or
                bool(cand.get("_delayed_entry_light", False)) or
                bool(cand.get("_family_force_review", False)) or
                str(cand.get("_family_trigger_source") or "") in {"delayed_family_trigger", "delayed_family_trigger_hard", "political_family_override"}
            )

            allow_second = (
                primary_type not in {"legal_resolution", "valuation_ladder", "sports_award_longshot"}
                and (
                    (diff <= 0.16 and structure_ok) or
                    (diff <= 0.14 and contender_ready) or
                    (diff <= 0.12 and raw_attack >= 1.10) or
                    (diff <= 0.20 and cand.get("theme") in {"politics", "general"} and raw_attack >= 1.0) or
                    (diff <= 0.24 and bool(cand.get("_family_force_review", False)))
                )
            )

            if allow_second:
                if bool(cand.get("_family_force_review", False)):
                    cand["_family_review_mode"] = "family_swap_review"
                else:
                    cand["_family_review_mode"] = "family_swap_review" if cand.get("theme") in {"politics", "general"} else "peer_review"
                cand["_family_review_gap"] = round(diff, 4)
                resolved.append(cand)
            else:
                print("SKIP | family_conflict_resolver | family={} | kept_score={:.3f} | lost_score={:.3f} | bonus={:.3f} | {}".format(
                    (family_key or "unknown")[:92],
                    max(primary_effective, effective_attack),
                    min(primary_effective, effective_attack),
                    float(cand.get("_family_contender_bonus", 0.0) or 0.0),
                    (cand.get("question") or "")[:110]
                ))

    return resolved



def hot_slot_discipline_gate(engine, candidate, reason, current_regime):
    try:
        summary = engine.summary()
        open_positions = int(summary.get("open_positions", 0) or 0)
        score = float(candidate.get("score", 0.0) or 0.0)
        survival = float(score_survival_priority(candidate, reason, engine))
        pressure = float(candidate.get("pressure_density", 0.0) or 0.0)
        trend = float(candidate.get("price_trend_strength", 0.0) or 0.0)
        win = abs(float(candidate.get("price_delta_window", 0.0) or 0.0))
        market_type = str(candidate.get("market_type", "general_binary") or "general_binary")
        political_override = bool(candidate.get("political_override_active", False))
    except Exception:
        return True, None

    if political_override or open_positions < 2:
        return True, None

    # Do not spend hot slots on thin / passive candidates once portfolio has real occupancy
    if current_regime in {"calm", "normal", "hot"}:
        if market_type in {"general_binary", "narrative_long_tail", "sports_award_longshot"}:
            if score < (1.00 if current_regime != "hot" else 1.02) and survival < (1.24 if current_regime != "hot" else 1.34) and pressure < (0.18 if current_regime != "hot" else 0.20) and win < (0.0030 if current_regime != "hot" else 0.0060):
                print(
                    "TRACE | hot_slot_discipline | verdict=skip | signal=thin_hot_slot | open_positions={} | market_type={} | reason={} | score={:.3f} | survival={:.3f} | pressure={:.3f} | trend={:.3f} | win={:.4f} | {}".format(
                        open_positions, market_type, reason, score, survival, pressure, trend, win,
                        (candidate.get("question") or "")[:72],
                    )
                )
                return False, "thin_hot_slot"

        if open_positions >= (3 if current_regime != "hot" else 2) and market_type not in {"legal_resolution", "short_burst_catalyst", "speculative_hype", "valuation_ladder"}:
            if score < (0.99 if current_regime != "hot" else 1.01) and survival < (1.32 if current_regime != "hot" else 1.40) and pressure < (0.20 if current_regime != "hot" else 0.22):
                print(
                    "TRACE | hot_slot_discipline | verdict=skip | signal=portfolio_hot_slots_full | open_positions={} | market_type={} | reason={} | score={:.3f} | survival={:.3f} | pressure={:.3f} | trend={:.3f} | {}".format(
                        open_positions, market_type, reason, score, survival, pressure, trend,
                        (candidate.get("question") or "")[:72],
                    )
                )
                return False, "portfolio_hot_slots_full"

        if open_positions >= (2 if current_regime != "hot" else 1) and market_type == "sports_award_longshot" and str(candidate.get("_entry_source", candidate.get("entry_source", "unknown")) or "unknown") == "explorer":
            if score < (0.96 if current_regime != "hot" else 0.99) and survival < (1.36 if current_regime != "hot" else 1.42):
                print(
                    "TRACE | hot_slot_discipline | verdict=skip | signal=sports_hot_slot_strict | open_positions={} | market_type={} | reason={} | score={:.3f} | survival={:.3f} | pressure={:.3f} | {}".format(
                        open_positions, market_type, reason, score, survival, pressure,
                        (candidate.get("question") or "")[:72],
                    )
                )
                return False, "sports_hot_slot_strict"

    print(
        "TRACE | hot_slot_discipline | verdict=pass | open_positions={} | market_type={} | reason={} | score={:.3f} | survival={:.3f} | pressure={:.3f} | trend={:.3f} | win={:.4f} | {}".format(
            open_positions, market_type, reason, score, survival, pressure, trend, win,
            (candidate.get("question") or "")[:72],
        )
    )
    return True, None



def should_enforce_competition_gate_block(engine, candidate, reason, current_regime):
    try:
        summary = engine.summary()
        open_positions = int(summary.get("open_positions", 0) or 0)
        if open_positions < 3:
            return False, None

        candidate["survival_priority"] = float(score_survival_priority(candidate, reason, engine))
        gate = incoming_competition_gate(candidate, reason, current_regime, open_positions)
        gate_reason = gate.get("reason", "unknown")
        candidate["__competition_gate_reason"] = gate_reason

        hard_block_reasons = {
            "competition_hard_gate",
            "hot_slot_general_strict",
            "hot_slot_sports_explorer_strict",
            "hot_slot_narrative_strict",
        }
        if (not bool(gate.get("allowed", False))) and gate_reason in hard_block_reasons:
            print("TRACE | competition_gate_enforced | registry={} | gate={} | reason={} | {}".format(
                EDGE_REGISTRY_VERSION,
                gate_reason,
                reason,
                (candidate.get("question") or "")[:110],
            ))
            return True, gate_reason
        return False, gate_reason
    except Exception:
        return False, None



def warmup_slot_cap_gate(engine, candidate, reason, current_regime, opened_now):
    try:
        summary = engine.summary()
        open_positions = int(summary.get("open_positions", 0) or 0)
        market_type = str(candidate.get("market_type", "general_binary") or "general_binary")
        score = float(candidate.get("score", 0.0) or 0.0)
        survival = float(score_survival_priority(candidate, reason, engine))
        pressure = float(candidate.get("pressure_density", 0.0) or 0.0)
        trend = float(candidate.get("price_trend_strength", 0.0) or 0.0)
        source = str(candidate.get("_entry_source", candidate.get("entry_source", "unknown")) or "unknown")
        political_override = bool(candidate.get("political_override_active", False))
    except Exception:
        return False, None

    if current_regime != "hot" or political_override:
        return False, None

    # During the first hot burst, protect slots from medium-quality accumulation.
    if open_positions + int(opened_now or 0) >= 3:
        if market_type in {"general_binary", "narrative_long_tail"}:
            if score < 1.02 and survival < 1.42 and pressure < 0.20:
                print(
                    "TRACE | warmup_slot_cap | verdict=skip | signal=warmup_general_cap | open_positions={} | opened_now={} | market_type={} | reason={} | score={:.3f} | survival={:.3f} | pressure={:.3f} | trend={:.3f} | {}".format(
                        open_positions, int(opened_now or 0), market_type, reason, score, survival, pressure, trend,
                        (candidate.get("question") or "")[:72],
                    )
                )
                return True, "warmup_general_cap"

        if market_type == "sports_award_longshot":
            if score < 1.04 and survival < 1.48:
                print(
                    "TRACE | warmup_slot_cap | verdict=skip | signal=warmup_sports_cap | open_positions={} | opened_now={} | source={} | reason={} | score={:.3f} | survival={:.3f} | {}".format(
                        open_positions, int(opened_now or 0), source, reason, score, survival,
                        (candidate.get("question") or "")[:72],
                    )
                )
                return True, "warmup_sports_cap"

    if open_positions + int(opened_now or 0) >= 4:
        if market_type not in {"short_burst_catalyst", "valuation_ladder", "legal_resolution"}:
            if score < 1.06 and survival < 1.55:
                print(
                    "TRACE | warmup_slot_cap | verdict=skip | signal=warmup_portfolio_cap | open_positions={} | opened_now={} | market_type={} | reason={} | score={:.3f} | survival={:.3f} | {}".format(
                        open_positions, int(opened_now or 0), market_type, reason, score, survival,
                        (candidate.get("question") or "")[:72],
                    )
                )
                return True, "warmup_portfolio_cap"

    return False, None



def hard_slot_cap_gate(engine, candidate, reason, current_regime):
    try:
        summary = engine.summary()
        open_positions = int(summary.get("open_positions", 0) or 0)
        market_type = str(candidate.get("market_type", "general_binary") or "general_binary")
        score = float(candidate.get("score", 0.0) or 0.0)
        survival = float(score_survival_priority(candidate, reason, engine))
        pressure = float(candidate.get("pressure_density", 0.0) or 0.0)
        source = str(candidate.get("_entry_source", candidate.get("entry_source", "unknown")) or "unknown")
        political_override = bool(candidate.get("political_override_active", False))
    except Exception:
        return False, None

    if current_regime != "hot" or political_override:
        return False, None

    if open_positions >= 8:
        if market_type == "general_binary" and score < 1.04 and survival < 1.55:
            print("TRACE | hard_slot_cap | verdict=skip | signal=hot_general_cap | open_positions={} | reason={} | score={:.3f} | survival={:.3f} | {}".format(
                open_positions, reason, score, survival, (candidate.get("question") or "")[:72]
            ))
            return True, "hot_general_cap"
        if market_type == "sports_award_longshot" and (source == "explorer" or score < 1.00 or survival < 1.46):
            print("TRACE | hard_slot_cap | verdict=skip | signal=hot_sports_cap | open_positions={} | reason={} | score={:.3f} | survival={:.3f} | source={} | {}".format(
                open_positions, reason, score, survival, source, (candidate.get("question") or "")[:72]
            ))
            return True, "hot_sports_cap"
        if market_type == "narrative_long_tail" and score < 1.04 and survival < 1.34 and pressure < 0.20:
            print("TRACE | hard_slot_cap | verdict=skip | signal=hot_narrative_cap | open_positions={} | reason={} | score={:.3f} | survival={:.3f} | {}".format(
                open_positions, reason, score, survival, (candidate.get("question") or "")[:72]
            ))
            return True, "hot_narrative_cap"

    if open_positions >= 12 and market_type not in {"legal_resolution", "short_burst_catalyst", "valuation_ladder"}:
        print("TRACE | hard_slot_cap | verdict=skip | signal=portfolio_cap_full | open_positions={} | market_type={} | reason={} | {}".format(
            open_positions, market_type, reason, (candidate.get("question") or "")[:72]
        ))
        return True, "portfolio_cap_full"

    return False, None


def should_attempt_competitive_replacement(engine, candidate, reason, current_regime):
    summary = engine.summary()
    open_positions = int(summary.get("open_positions", 0) or 0)
    if open_positions < 6:
        return False

    candidate["survival_priority"] = float(score_survival_priority(candidate, reason, engine))
    gate = incoming_competition_gate(candidate, reason, current_regime, open_positions)
    candidate["__competition_gate_reason"] = gate.get("reason", "unknown")
    return bool(gate.get("allowed", False))

def log_lifecycle_event(event, current_regime):
    action = event.get("action")
    question = (event.get("question") or "")[:110]

    if action == "PARTIAL_CLOSE":
        print(
            "PARTIAL_CLOSE | regime={} | exit_reason={} | reason={} | type={} | {} | price={:.4f} | pnl={:.2%} | realized=${:.2f} | remaining_cost=${:.2f} | {}".format(
                current_regime,
                event.get("exit_reason"),
                event.get("reason"),
                event.get("market_type", "unknown"),
                event.get("outcome_name"),
                float(event.get("current_price", 0.0) or 0.0),
                float(event.get("unrealized_pnl_pct", 0.0) or 0.0),
                float(event.get("realized_pnl_usd", 0.0) or 0.0),
                float(event.get("remaining_cost_basis", 0.0) or 0.0),
                question
            )
        )
        return

    if action == "SCALE_IN":
        print(
            "SCALE_IN | regime={} | reason={} | {} | add=${:.2f} | new_entry={:.4f} | price={:.4f} | score={:.4f} | trend={:.2f} | pressure={:.2f} | {}".format(
                current_regime,
                event.get("reason"),
                event.get("outcome_name"),
                float(event.get("add_stake", 0.0) or 0.0),
                float(event.get("new_entry_price", 0.0) or 0.0),
                float(event.get("current_price", 0.0) or 0.0),
                float(event.get("score", 0.0) or 0.0),
                float(event.get("price_trend_strength", 0.0) or 0.0),
                float(event.get("pressure_density", 0.0) or 0.0),
                question
            )
        )
        return

    if action == "CLOSE":
        extra_fragments = []
        if event.get("exit_reason") == "trailing_momentum_exit":
            extra_fragments.append("peak={:.2%}".format(float(event.get("peak_pnl_pct", 0.0) or 0.0)))
            extra_fragments.append("retrace={:.2%}".format(float(event.get("retrace_from_peak_pct", 0.0) or 0.0)))
            extra_fragments.append("trail={}".format(event.get("trailing_signal")))
        if event.get("exit_reason") == "pressure_decay_exit":
            extra_fragments.append("p_decay={:.2%}".format(float(event.get("pressure_decay_ratio", 0.0) or 0.0)))
            extra_fragments.append("c_decay={:.2%}".format(float(event.get("pressure_count_decay_ratio", 0.0) or 0.0)))
            extra_fragments.append("flow={}".format(event.get("pressure_decay_signal")))
        if event.get("exit_reason") == "no_follow_through_exit":
            extra_fragments.append("follow={}".format(event.get("follow_through_signal")))
            extra_fragments.append("peak={:.2%}".format(float(event.get("peak_pnl_pct", 0.0) or 0.0)))
            extra_fragments.append("p_decay={:.2%}".format(float(event.get("pressure_decay_ratio", 0.0) or 0.0)))
        if event.get("exit_reason") == "time_decay_exit":
            extra_fragments.append("time={}".format(event.get("time_decay_signal")))
            extra_fragments.append("progress={:.2%}".format(float(event.get("price_progress", 0.0) or 0.0)))
            if event.get("type_bias_signal"):
                extra_fragments.append("type_bias={}".format(event.get("type_bias_signal")))
        if event.get("exit_reason") == "micro_profit_lock":
            extra_fragments.append("profit_locked={:.2%}".format(float(event.get("profit_locked", 0.0) or 0.0)))
            extra_fragments.append("peak={:.2%}".format(float(event.get("peak_pnl_pct", 0.0) or 0.0)))
            extra_fragments.append("retrace={:.2%}".format(float(event.get("retrace", 0.0) or 0.0)))
            if event.get("type_bias_signal"):
                extra_fragments.append("type_bias={}".format(event.get("type_bias_signal")))
        if event.get("exit_reason") == "peak_decay_exit":
            extra_fragments.append("peak={:.2%}".format(float(event.get("peak", 0.0) or 0.0)))
            extra_fragments.append("current={:.2%}".format(float(event.get("current", 0.0) or 0.0)))
            extra_fragments.append("retrace={:.2%}".format(float(event.get("retrace", 0.0) or 0.0)))
        if event.get("exit_reason") == "profit_recycle_exit":
            extra_fragments.append("capital={}".format(event.get("capital_signal")))
            extra_fragments.append("peak={:.2%}".format(float(event.get("peak", 0.0) or 0.0)))
            extra_fragments.append("current={:.2%}".format(float(event.get("current", 0.0) or 0.0)))
            extra_fragments.append("retrace={:.2%}".format(float(event.get("retrace", 0.0) or 0.0)))
        if event.get("exit_reason") in {"capital_rotation_exit", "competitive_rotation_exit"}:
            extra_fragments.append("capital={}".format(event.get("capital_signal")))
            extra_fragments.append("hold={:.2f}".format(float(event.get("hold_priority", 0.0) or 0.0)))
            extra_fragments.append("cluster_heat={:.2f}".format(float(event.get("cluster_heat", 0.0) or 0.0)))
            extra_fragments.append("peak={:.2%}".format(float(event.get("peak_pnl_pct", 0.0) or 0.0)))
            extra_fragments.append("progress={:.2%}".format(float(event.get("price_progress", 0.0) or 0.0)))
            if event.get("incoming_edge") is not None:
                extra_fragments.append("incoming={:.2f}".format(float(event.get("incoming_edge", 0.0) or 0.0)))
            if event.get("hold_gap") is not None:
                extra_fragments.append("gap={:.2f}".format(float(event.get("hold_gap", 0.0) or 0.0)))
            if event.get("replace_for"):
                extra_fragments.append("replace_for={}".format(event.get("replace_for")))
            if event.get("type_bias_signal"):
                extra_fragments.append("type_bias={}".format(event.get("type_bias_signal")))
        if event.get("exit_reason") in {"idle_hard_exit", "opportunity_cost_exit", "portfolio_pressure_exit"}:
            extra_fragments.append("capital={}".format(event.get("capital_signal")))
            extra_fragments.append("peak={:.2%}".format(float(event.get("peak_pnl_pct", 0.0) or 0.0)))
            extra_fragments.append("progress={:.2%}".format(float(event.get("price_progress", 0.0) or 0.0)))

        extra_tail = ""
        if extra_fragments:
            extra_tail = " | " + " | ".join(extra_fragments)

        print(
            "CLOSE | regime={} | exit_reason={} | reason={} | type={} | {} | price={:.4f} | realized=${:.2f} | pnl={:.2%} | age={}{} | {}".format(
                current_regime,
                event.get("exit_reason"),
                event.get("reason"),
                event.get("market_type", "unknown"),
                event.get("outcome_name"),
                float(event.get("current_price", 0.0) or 0.0),
                float(event.get("realized_pnl_usd", 0.0) or 0.0),
                float(event.get("unrealized_pnl_pct", 0.0) or 0.0),
                int(event.get("age_cycles", 0) or 0),
                extra_tail,
                question
            )
        )


async def run_loop() -> None:
    ensure_dir(DATA_DIR)
    engine = PaperEngine()

    HISTORY_WINDOW = 8
    runtime_state = load_runtime_state()

    price_history = hydrate_price_history(runtime_state.get("price_history"), HISTORY_WINDOW)

    momentum_cooldown = hydrate_timestamp_map(runtime_state.get("momentum_cooldown"))
    MOMENTUM_COOLDOWN_SEC = 60 * 20

    score_reentry_cooldown = hydrate_timestamp_map(runtime_state.get("score_reentry_cooldown"))
    SCORE_REENTRY_COOLDOWN_SEC = 60 * 45

    dead_reentry_cooldown = hydrate_timestamp_map(runtime_state.get("dead_reentry_cooldown"))
    DEAD_REENTRY_COOLDOWN_SEC = 60 * 90

    family_dead_cooldown = hydrate_timestamp_map(runtime_state.get("family_dead_cooldown"))
    FAMILY_DEAD_COOLDOWN_SEC = 60 * 90

    stale_reentry_cooldown = hydrate_timestamp_map(runtime_state.get("stale_reentry_cooldown"))
    STALE_REENTRY_COOLDOWN_SEC = 60 * 120

    delayed_entry_watch = hydrate_dict_map(runtime_state.get("delayed_entry_watch"))
    delayed_entry_cooldown = hydrate_timestamp_map(runtime_state.get("delayed_entry_cooldown"))
    delayed_entry_memory = hydrate_dict_map(runtime_state.get("delayed_entry_memory"))
    DELAYED_ENTRY_COOLDOWN_SEC = 60 * 90
    DELAYED_ENTRY_MEMORY_SEC = 60 * 60 * 8

    market_exit_memory = hydrate_dict_map(runtime_state.get("market_exit_memory"))
    MARKET_EXIT_MEMORY_SEC = 60 * 60 * 8

    signal_memory = hydrate_signal_memory(runtime_state.get("signal_memory"))

    timeout = aiohttp.ClientTimeout(total=60)
    headers = {
        "User-Agent": "anomaly-hunter-v1/0.1"
    }

    if runtime_state:
        print("INFO | runtime_state_restored | ts={} | histories={} | delayed_memory={} | delayed_watch={} | delayed_cooldown={} | signal_keys={} | market_exit_memory={} | momentum_cd={} | score_cd={} | dead_cd={}".format(
            runtime_state.get("ts", "unknown"),
            len(price_history),
            len(delayed_entry_memory),
            len(delayed_entry_watch),
            len(delayed_entry_cooldown),
            len(signal_memory),
            len(market_exit_memory),
            len(momentum_cooldown),
            len(score_reentry_cooldown),
            len(dead_reentry_cooldown),
        ))

    runtime_manifest = enforce_runtime_integrity_lock()
    print("TRACE | startup_audit | main_version={} | main_module={} | paper_engine={} | paper_module={} | edge_registry={} | edge_module={} | regime_detector={} | regime_module={} | cwd={}".format(
        MAIN_PATCH_VERSION,
        runtime_manifest.get("main", {}).get("path", getattr(sys.modules.get(__name__), "__file__", "unknown")),
        PAPER_ENGINE_VERSION,
        getattr(paper_engine_module, "__file__", "unknown"),
        EDGE_REGISTRY_VERSION,
        getattr(edge_registry_module, "__file__", "unknown"),
        REGIME_DETECTOR_VERSION,
        getattr(regime_detector_module, "__file__", "unknown"),
        os.getcwd(),
    ))

    async with aiohttp.ClientSession(timeout=timeout, headers=headers) as session:
        while True:
            try:
                now_ts = time.time()

                stale_keys = []
                for k, ts in momentum_cooldown.items():
                    if now_ts - ts > MOMENTUM_COOLDOWN_SEC:
                        stale_keys.append(k)
                for k in stale_keys:
                    del momentum_cooldown[k]

                stale_score_keys = []
                for k, ts in score_reentry_cooldown.items():
                    if now_ts - ts > SCORE_REENTRY_COOLDOWN_SEC:
                        stale_score_keys.append(k)
                for k in stale_score_keys:
                    del score_reentry_cooldown[k]

                stale_dead_keys = []
                for k, ts in dead_reentry_cooldown.items():
                    if now_ts - ts > DEAD_REENTRY_COOLDOWN_SEC:
                        stale_dead_keys.append(k)
                for k in stale_dead_keys:
                    del dead_reentry_cooldown[k]

                stale_family_keys = []
                for k, ts in family_dead_cooldown.items():
                    if now_ts - ts > FAMILY_DEAD_COOLDOWN_SEC:
                        stale_family_keys.append(k)
                for k in stale_family_keys:
                    del family_dead_cooldown[k]

                stale_reopen_keys = []
                for k, ts in stale_reentry_cooldown.items():
                    if now_ts - ts > STALE_REENTRY_COOLDOWN_SEC:
                        stale_reopen_keys.append(k)
                for k in stale_reopen_keys:
                    del stale_reentry_cooldown[k]

                stale_delayed_keys = []
                for k, ts in delayed_entry_cooldown.items():
                    if now_ts - ts > DELAYED_ENTRY_COOLDOWN_SEC:
                        stale_delayed_keys.append(k)
                for k in stale_delayed_keys:
                    del delayed_entry_cooldown[k]

                stale_watch_keys = []
                for k, payload in delayed_entry_watch.items():
                    if now_ts - float(payload.get("last_ts", 0.0) or 0.0) > (SCAN_INTERVAL_SEC * 6 + 40):
                        stale_watch_keys.append(k)
                for k in stale_watch_keys:
                    payload = dict(delayed_entry_watch.get(k, {}) or {})
                    if payload:
                        memory_key = "family::{}".format(payload.get("family_key")) if payload.get("family_key") else "market::{}".format(k)
                        prev_payload = dict(delayed_entry_memory.get(memory_key, {}) or {})
                        delayed_entry_memory[memory_key] = {
                            **prev_payload,
                            "market_key": k,
                            "family_key": payload.get("family_key", prev_payload.get("family_key", k)),
                            "question": payload.get("question", prev_payload.get("question", "")),
                            "last_ts": now_ts,
                            "last_status": "watch_expired",
                            "watch_count": int(prev_payload.get("watch_count", 0) or 0) + 1,
                            "fail_count": int(prev_payload.get("fail_count", 0) or 0) + 1,
                            "confirm_count": int(prev_payload.get("confirm_count", 0) or 0),
                            "max_seen_cycles": max(int(prev_payload.get("max_seen_cycles", 0) or 0), int(payload.get("seen_cycles", 0) or 0)),
                            "max_score": max(float(payload.get("max_score", 0.0) or 0.0), float(prev_payload.get("max_score", 0.0) or 0.0)),
                            "max_density": max(float(payload.get("max_density", 0.0) or 0.0), float(prev_payload.get("max_density", 0.0) or 0.0)),
                            "max_pressure_count": max(int(payload.get("max_pressure_count", 0) or 0), int(prev_payload.get("max_pressure_count", 0) or 0)),
                            "max_trend": max(float(payload.get("max_trend", 0.0) or 0.0), float(prev_payload.get("max_trend", 0.0) or 0.0)),
                            "max_window_delta": max(float(payload.get("max_window_delta", 0.0) or 0.0), float(prev_payload.get("max_window_delta", 0.0) or 0.0)),
                            "max_delta": max(float(payload.get("max_delta", 0.0) or 0.0), float(prev_payload.get("max_delta", 0.0) or 0.0)),
                            "max_structure_votes": max(int(prev_payload.get("max_structure_votes", 0) or 0), int(payload.get("structure_votes", 0) or 0)),
                            "max_improvement_votes": max(int(prev_payload.get("max_improvement_votes", 0) or 0), int(payload.get("improvement_votes", 0) or 0)),
                            "max_promotion_score": max(int(prev_payload.get("max_promotion_score", 0) or 0), int(payload.get("promotion_score", 0) or 0)),
                            "last_promotion_score": int(payload.get("promotion_score", 0) or 0),
                            "stall_cycles": max(int(prev_payload.get("stall_cycles", 0) or 0), int(payload.get("stall_cycles", 0) or 0)),
                        }
                        delayed_entry_cooldown[k] = now_ts
                        delayed_entry_cooldown[memory_key] = now_ts
                    del delayed_entry_watch[k]

                stale_delayed_memory_keys = []
                for k, payload in delayed_entry_memory.items():
                    if now_ts - float(payload.get("last_ts", 0.0) or 0.0) > DELAYED_ENTRY_MEMORY_SEC:
                        stale_delayed_memory_keys.append(k)
                for k in stale_delayed_memory_keys:
                    del delayed_entry_memory[k]

                stale_memory_keys = []
                for k, payload in market_exit_memory.items():
                    if now_ts - float(payload.get("last_ts", 0.0) or 0.0) > MARKET_EXIT_MEMORY_SEC:
                        stale_memory_keys.append(k)
                for k in stale_memory_keys:
                    del market_exit_memory[k]

                markets = await fetch_markets(session, limit=400)

                candidates = []
                for m in markets:
                    candidates.extend(extract_candidate_outcomes(m))

                for c in candidates:
                    key = build_market_key(c)
                    current_price = float(c.get("price", 0.0))

                    history_deque = price_history.get(key)
                    if history_deque is None:
                        history_deque = deque(maxlen=HISTORY_WINDOW)
                        price_history[key] = history_deque

                    delta_1, delta_window, trend_strength, pressure_density, pressure_count = compute_history_features(
                        history_deque,
                        current_price
                    )

                    c["price_delta"] = delta_1
                    c["price_delta_window"] = delta_window
                    c["price_trend_strength"] = trend_strength
                    c["pressure_density"] = pressure_density
                    c["pressure_count"] = pressure_count
                    c["cluster"] = detect_cluster(c)
                    c["market_type"] = detect_market_type(c)
                    c["family_key"] = detect_market_family(c)

                price_truth_map = build_market_map(candidates)

                filtered = filter_candidates(candidates)
                ranked = rank_candidates(filtered)

                ranked = deduplicate_by_question(ranked)
                ranked = limit_per_theme(ranked, max_per_theme=3)

                raw_pulses = pulse_candidates(candidates, min_abs_delta=0.006, top_n=10)
                raw_trends = trend_candidates(candidates, min_abs_window_delta=0.008, top_n=10)
                raw_pressures = pressure_candidates(candidates, min_pressure_density=0.40, min_pressure_count=2, top_n=10)

                pulses = rank_candidates(raw_pulses) if raw_pulses else []
                trends = rank_candidates(raw_trends) if raw_trends else []
                pressures = rank_candidates(raw_pressures) if raw_pressures else []

                explorers = expanded_universe_candidates(candidates, top_n=14)
                for c in explorers:
                    c["_universe_source"] = "explorer"

                combined = pick_universe_candidates(ranked, pulses, trends, pressures, explorers, top_main=18, top_explore=8)
                combined = apply_post_merge_hygiene_firewall(combined, stage="combined_pre_family")

                for c in combined:
                    c["cluster"] = detect_cluster(c)
                    c["market_type"] = detect_market_type(c)
                    c["family_key"] = detect_market_family(c)
                    c.setdefault("_universe_source", "primary")

                combined = resolve_family_conflicts(combined, current_regime=detect_market_regime(candidates)["regime"], engine=engine, delayed_entry_memory=delayed_entry_memory)
                combined = apply_post_merge_hygiene_firewall(combined, stage="combined_post_family")

                # ВАЖНО: lifecycle не должен смотреть на сырой candidates
                # Собираем единый scored/enriched market state.
                scored_state_candidates = merge_scored_into_candidates(
                    candidates,
                    merge_sources(ranked, pulses, trends, pressures)
                )
                for c in scored_state_candidates:
                    if not c.get("cluster"):
                        c["cluster"] = detect_cluster(c)
                    if not c.get("market_type"):
                        c["market_type"] = detect_market_type(c)
                    if not c.get("family_key"):
                        c["family_key"] = detect_market_family(c)

                regime_info = detect_market_regime(candidates)
                current_regime = regime_info["regime"]
                settings = regime_settings(current_regime)
                market_map = build_market_map(scored_state_candidates)

                lifecycle_events = engine.evaluate_positions(
                    market_map=market_map,
                    price_truth_map=price_truth_map,
                    now_ts=now_ts,
                    regime=current_regime
                )

                for event in lifecycle_events:
                    append_jsonl(PAPER_TRADES_FILE, {
                        "ts": utc_now_iso(),
                        "market_regime": current_regime,
                        **event
                    })
                    log_lifecycle_event(event, current_regime)

                    if event.get("action") == "CLOSE":
                        exit_reason = event.get("exit_reason")
                        reason = event.get("reason")
                        pos_key = event.get("position_key")

                        if exit_reason in {
                            "thesis_invalidation",
                            "time_stale_exit",
                            "time_decay_exit",
                            "no_follow_through_exit",
                            "hard_stop_loss",
                            "market_missing_stale",
                            "trailing_momentum_exit",
                            "pressure_decay_exit",
                            "idle_hard_exit",
                            "opportunity_cost_exit",
                            "micro_profit_lock",
                            "peak_decay_exit",
                            "profit_recycle_exit",
                            "capital_rotation_exit",
                            "competitive_rotation_exit",
                            "dead_capital_decay",
                            "family_slot_recycle",
                            "family_rotation_exit",
                            "early_hard_stop_compression_exit",
                            "sports_longshot_churn_kill",
                            "sports_zero_peak_fire_exit",
                        }:
                            momentum_cooldown[pos_key] = now_ts

                        if reason == "score" and exit_reason in {
                            "thesis_invalidation",
                            "time_stale_exit",
                            "time_decay_exit",
                            "hard_stop_loss",
                            "idle_hard_exit",
                            "opportunity_cost_exit",
                            "capital_rotation_exit",
                            "competitive_rotation_exit",
                            "dead_capital_decay",
                            "family_slot_recycle",
                            "family_rotation_exit",
                            "early_hard_stop_compression_exit",
                        }:
                            score_reentry_cooldown[pos_key] = now_ts

                        if exit_reason in {"dead_capital_decay", "family_slot_recycle"}:
                            dead_reentry_cooldown[pos_key] = now_ts
                            family_key = event.get("family_key")
                            if family_key:
                                family_dead_cooldown[family_key] = now_ts

                        if exit_reason in {"zero_peak_scout_cut", "calm_legal_zero_peak_cut", "seed_stall_compression_cut", "calm_zero_peak_general_cut", "normal_zero_peak_linger_cut", "override_stall_cut", "sports_longshot_churn_kill", "sports_zero_peak_fire_exit", "general_zero_peak_stall_cut", "political_pre_momentum_compression_exit", "follow_through_compression_fail"}:
                            propagate_universal_reopen_lock(
                                event,
                                reason,
                                now_ts,
                                dead_reentry_cooldown,
                                score_reentry_cooldown,
                                family_dead_cooldown,
                            )
                            print(
                                "TRACE | post_cut_capital_recycle | reason={} | family={} | market_type={} | score={:.3f} | recycle=1 | reopen_lock=1 | {}".format(
                                    reason,
                                    (event.get("family_key") or "unknown")[:72],
                                    event.get("market_type", "unknown"),
                                    float(event.get("score", 0.0) or 0.0),
                                    (event.get("question") or "")[:72],
                                )
                            )

                        if exit_reason in {"time_decay_exit", "time_stale_exit", "idle_hard_exit", "opportunity_cost_decay", "early_hard_stop_compression_exit", "sports_longshot_churn_kill", "sports_zero_peak_fire_exit", "general_zero_peak_stall_cut", "political_pre_momentum_compression_exit", "follow_through_compression_fail", "delayed_admission_fail"}:
                            stale_reentry_cooldown[pos_key] = now_ts

                        if exit_reason in {"no_follow_through_exit", "time_decay_exit", "time_stale_exit", "dead_capital_decay", "idle_hard_exit", "opportunity_cost_decay", "early_hard_stop_compression_exit", "sports_longshot_churn_kill", "sports_zero_peak_fire_exit", "general_zero_peak_stall_cut", "political_pre_momentum_compression_exit", "follow_through_compression_fail", "delayed_admission_fail"}:
                            propagate_failed_reentry_lock(
                                event,
                                reason,
                                now_ts,
                                dead_reentry_cooldown,
                                score_reentry_cooldown,
                                family_dead_cooldown,
                            )

                        propagate_legal_replay_quarantine(
                            event,
                            reason,
                            now_ts,
                            dead_reentry_cooldown,
                            score_reentry_cooldown,
                            family_dead_cooldown,
                        )

                        winner_exit = False
                        try:
                            partial_take_count = int(event.get("partial_take_count", 0) or 0)
                            realized_total_position = float(event.get("realized_pnl_usd_total_position", 0.0) or 0.0)
                            winner_exit = (
                                exit_reason in {"runner_protection_lock_exit", "profit_lock_decay_exit"}
                                or (
                                    partial_take_count > 0
                                    and exit_reason in {"peak_zero_kill", "pressure_decay_exit", "trailing_momentum_exit", "time_decay_exit", "no_follow_through_exit"}
                                )
                                or (exit_reason == "micro_profit_lock" and realized_total_position > 0.0)
                            )
                        except Exception:
                            winner_exit = False

                        if winner_exit:
                            propagate_winner_reentry_lock(
                                event,
                                reason,
                                now_ts,
                                dead_reentry_cooldown,
                                score_reentry_cooldown,
                                family_dead_cooldown,
                            )

                        try:
                            realized_total_position = float(event.get("realized_pnl_usd_total_position", 0.0) or 0.0)
                        except Exception:
                            realized_total_position = 0.0
                        zero_churn_exit = bool(exit_reason in ZERO_CHURN_MEMORY_EXIT_REASONS and abs(realized_total_position) <= ZERO_CHURN_REALIZED_USD_THRESHOLD)
                        if zero_churn_exit:
                            stale_reentry_cooldown[pos_key] = now_ts
                            if reason == "score":
                                score_reentry_cooldown[pos_key] = now_ts
                            family_key = event.get("family_key")
                            if family_key:
                                family_dead_cooldown[family_key] = now_ts

                        memory = dict(market_exit_memory.get(pos_key, {}) or {})
                        memory["last_ts"] = now_ts
                        memory["last_exit_reason"] = exit_reason
                        memory["last_score"] = float(event.get("score", 0.0) or 0.0)
                        if exit_reason in {"dead_capital_decay", "time_stale_exit", "time_decay_exit", "family_slot_recycle", "zero_peak_scout_cut", "early_hard_stop_compression_exit", "sports_longshot_churn_kill", "sports_zero_peak_fire_exit", "general_zero_peak_stall_cut", "political_pre_momentum_compression_exit", "follow_through_compression_fail"}:
                            memory["dead_exit_count"] = int(memory.get("dead_exit_count", 0) or 0) + 1
                        if exit_reason in {"dead_capital_decay", "time_stale_exit", "time_decay_exit", "no_follow_through_exit", "family_slot_recycle", "idle_hard_exit", "opportunity_cost_decay", "zero_peak_scout_cut", "early_hard_stop_compression_exit", "sports_longshot_churn_kill", "sports_zero_peak_fire_exit", "general_zero_peak_stall_cut", "political_pre_momentum_compression_exit", "follow_through_compression_fail", "delayed_admission_fail"}:
                            memory["weak_failed_count"] = int(memory.get("weak_failed_count", 0) or 0) + 1
                        if exit_reason in {"time_decay_exit", "time_stale_exit", "idle_hard_exit", "opportunity_cost_decay", "early_hard_stop_compression_exit", "sports_longshot_churn_kill", "sports_zero_peak_fire_exit", "general_zero_peak_stall_cut", "political_pre_momentum_compression_exit", "follow_through_compression_fail", "delayed_admission_fail"}:
                            memory["stale_exit_count"] = int(memory.get("stale_exit_count", 0) or 0) + 1
                        try:
                            event_market_type = str(event.get("market_type", "unknown") or "unknown")
                            if event_market_type in {"speculative_hype", "short_burst_catalyst"} and exit_reason in {"peak_zero_kill", "zero_peak_scout_cut", "time_stale_exit", "general_zero_peak_stall_cut"}:
                                memory["speculative_reopen_brake_count"] = int(memory.get("speculative_reopen_brake_count", 0) or 0) + 1
                            realized_total_position = float(event.get("realized_pnl_usd_total_position", 0.0) or 0.0)
                            legal_loss_replay_reason = exit_reason in {
                                "cluster_conflict_rotation_exit",
                                "family_rotation_exit",
                                "competitive_rotation_exit",
                                "capital_rotation_exit",
                                "time_stale_exit",
                                "time_decay_exit",
                                "pressure_decay_exit",
                                "no_follow_through_exit",
                                "dead_capital_decay",
                                "idle_hard_exit",
                                "opportunity_cost_decay",
                                "early_hard_stop_compression_exit",
                                "hard_stop_loss",
                            }
                            if event_market_type == "legal_resolution" and legal_loss_replay_reason and realized_total_position < -0.0001:
                                memory["legal_replay_exit_count"] = int(memory.get("legal_replay_exit_count", 0) or 0) + 1
                                memory["legal_last_loss_exit_reason"] = exit_reason
                                memory["legal_last_loss_realized"] = realized_total_position
                                if exit_reason in {"time_stale_exit", "time_decay_exit", "pressure_decay_exit", "no_follow_through_exit", "idle_hard_exit", "opportunity_cost_decay"}:
                                    memory["legal_stale_loss_count"] = int(memory.get("legal_stale_loss_count", 0) or 0) + 1
                            if event_market_type == "legal_resolution" and exit_reason in {"early_hard_stop_compression_exit", "no_follow_through_exit", "follow_through_compression_fail", "calm_legal_zero_peak_cut", "zero_peak_scout_cut", "peak_zero_kill"} and realized_total_position <= 0.02:
                                memory["legal_false_pressure_quarantine_count"] = int(memory.get("legal_false_pressure_quarantine_count", 0) or 0) + 1
                                memory["legal_false_pressure_last_exit_reason"] = exit_reason
                        except Exception:
                            pass
                        if event.get("market_type") == "sports_award_longshot" and exit_reason in {"sports_longshot_churn_kill", "sports_zero_peak_fire_exit", "sports_zombie_guillotine_exit"}:
                            memory["sports_longshot_churn_count"] = int(memory.get("sports_longshot_churn_count", 0) or 0) + 1
                            if exit_reason == "sports_zero_peak_fire_exit":
                                memory["sports_zero_peak_fire_count"] = int(memory.get("sports_zero_peak_fire_count", 0) or 0) + 1
                            if exit_reason == "sports_zombie_guillotine_exit":
                                memory["sports_zombie_kill_count"] = int(memory.get("sports_zombie_kill_count", 0) or 0) + 1

                        if event.get("market_type") == "general_binary" and exit_reason in {"general_zero_peak_stall_cut", "political_pre_momentum_compression_exit"}:
                            memory["general_zero_peak_stall_count"] = int(memory.get("general_zero_peak_stall_count", 0) or 0) + 1
                            if exit_reason == "political_pre_momentum_compression_exit":
                                memory["political_pre_momentum_compression_count"] = int(memory.get("political_pre_momentum_compression_count", 0) or 0) + 1
                        if exit_reason in {"follow_through_compression_fail", "delayed_admission_fail", "no_follow_through_exit", "failed_runner_quarantine_exit", "zero_peak_scout_cut", "peak_zero_kill", "normal_zero_peak_linger_cut", "general_zero_peak_stall_cut", "sports_zero_peak_fire_exit", "sports_longshot_churn_kill", "sports_zombie_guillotine_exit", "calm_zero_peak_general_cut", "calm_legal_zero_peak_cut", "political_pre_momentum_compression_exit", "time_decay_exit", "dead_capital_decay"}:
                            memory["dead_money_exit_count"] = int(memory.get("dead_money_exit_count", 0) or 0) + 1
                        if exit_reason in {"follow_through_compression_fail", "delayed_admission_fail", "no_follow_through_exit", "failed_runner_quarantine_exit"}:
                            memory["follow_through_fail_count"] = int(memory.get("follow_through_fail_count", 0) or 0) + 1
                            memory["last_follow_through_exit_reason"] = exit_reason
                        if event.get("market_type") in {"short_burst_catalyst", "speculative_hype", "valuation_ladder", "legal_resolution"} and exit_reason in {"no_follow_through_exit", "failed_runner_quarantine_exit", "peak_decay_exit", "profit_recycle_exit"}:
                            memory["failed_runner_quarantine_count"] = int(memory.get("failed_runner_quarantine_count", 0) or 0) + 1
                            memory["failed_runner_last_exit_reason"] = exit_reason
                        if exit_reason in {"peak_zero_kill", "zero_peak_scout_cut", "normal_zero_peak_linger_cut", "general_zero_peak_stall_cut", "sports_zero_peak_fire_exit", "sports_longshot_churn_kill", "sports_zombie_guillotine_exit", "calm_zero_peak_general_cut", "calm_legal_zero_peak_cut", "political_pre_momentum_compression_exit"}:
                            memory["zero_peak_family_count"] = int(memory.get("zero_peak_family_count", 0) or 0) + 1
                        if exit_reason in {"follow_through_compression_fail", "delayed_admission_fail", "peak_zero_kill", "zero_peak_scout_cut", "normal_zero_peak_linger_cut", "general_zero_peak_stall_cut", "sports_zero_peak_fire_exit", "sports_longshot_churn_kill", "sports_zombie_guillotine_exit", "calm_zero_peak_general_cut", "calm_legal_zero_peak_cut", "political_pre_momentum_compression_exit"}:
                            memory["thin_impulse_fail_count"] = int(memory.get("thin_impulse_fail_count", 0) or 0) + 1
                        if zero_churn_exit:
                            memory["zero_churn_exit_count"] = int(memory.get("zero_churn_exit_count", 0) or 0) + 1
                            memory["zero_churn_reopen_brake_count"] = int(memory.get("zero_churn_reopen_brake_count", 0) or 0) + 1
                            memory["last_zero_churn_exit_reason"] = exit_reason
                            memory["last_zero_churn_score"] = float(event.get("score", 0.0) or 0.0)
                            memory["last_zero_churn_votes"] = int(event.get("follow_through_structure_votes", memory.get("last_zero_churn_votes", 0)) or 0)

                        if winner_exit:
                            memory["winner_exit_count"] = int(memory.get("winner_exit_count", 0) or 0) + 1
                            memory["winner_last_exit_reason"] = exit_reason
                        market_exit_memory[pos_key] = memory

                        family_memory_key = build_family_memory_key(event)
                        if family_memory_key:
                            family_memory = dict(market_exit_memory.get(family_memory_key, {}) or {})
                            family_memory["last_ts"] = now_ts
                            family_memory["last_exit_reason"] = exit_reason
                            family_memory["last_score"] = float(event.get("score", 0.0) or 0.0)
                            family_memory["last_failed_structure_votes"] = int(event.get("follow_through_structure_votes", memory.get("last_failed_structure_votes", 0)) or 0)
                            family_memory["last_failed_score"] = float(event.get("score", 0.0) or 0.0)
                            family_memory["last_failed_density"] = float(event.get("pressure_density", event.get("last_pressure_density", 0.0)) or 0.0)
                            family_memory["last_failed_pressure_count"] = int(event.get("pressure_count", event.get("last_pressure_count", 0)) or 0)
                            if exit_reason in {"follow_through_compression_fail", "delayed_admission_fail", "no_follow_through_exit", "failed_runner_quarantine_exit", "zero_peak_scout_cut", "peak_zero_kill", "normal_zero_peak_linger_cut", "general_zero_peak_stall_cut", "sports_zero_peak_fire_exit", "sports_longshot_churn_kill", "sports_zombie_guillotine_exit", "calm_zero_peak_general_cut", "calm_legal_zero_peak_cut", "political_pre_momentum_compression_exit", "time_decay_exit", "dead_capital_decay"}:
                                family_memory["dead_money_exit_count"] = int(family_memory.get("dead_money_exit_count", 0) or 0) + 1
                            if exit_reason in {"follow_through_compression_fail", "delayed_admission_fail", "no_follow_through_exit", "failed_runner_quarantine_exit"}:
                                family_memory["follow_through_fail_count"] = int(family_memory.get("follow_through_fail_count", 0) or 0) + 1
                            if event.get("market_type") in {"short_burst_catalyst", "speculative_hype", "valuation_ladder", "legal_resolution"} and exit_reason in {"no_follow_through_exit", "failed_runner_quarantine_exit", "peak_decay_exit", "profit_recycle_exit"}:
                                family_memory["failed_runner_quarantine_count"] = int(family_memory.get("failed_runner_quarantine_count", 0) or 0) + 1
                            if event.get("market_type") == "legal_resolution" and exit_reason in {"early_hard_stop_compression_exit", "no_follow_through_exit", "follow_through_compression_fail", "calm_legal_zero_peak_cut", "zero_peak_scout_cut", "peak_zero_kill"} and realized_total_position <= 0.02:
                                family_memory["legal_false_pressure_quarantine_count"] = int(family_memory.get("legal_false_pressure_quarantine_count", 0) or 0) + 1
                            if exit_reason in {"peak_zero_kill", "zero_peak_scout_cut", "normal_zero_peak_linger_cut", "general_zero_peak_stall_cut", "sports_zero_peak_fire_exit", "sports_longshot_churn_kill", "sports_zombie_guillotine_exit", "calm_zero_peak_general_cut", "calm_legal_zero_peak_cut", "political_pre_momentum_compression_exit"}:
                                family_memory["zero_peak_family_count"] = int(family_memory.get("zero_peak_family_count", 0) or 0) + 1
                            if exit_reason in {"follow_through_compression_fail", "delayed_admission_fail", "no_follow_through_exit", "peak_zero_kill", "zero_peak_scout_cut", "normal_zero_peak_linger_cut", "general_zero_peak_stall_cut", "sports_zero_peak_fire_exit", "sports_longshot_churn_kill", "sports_zombie_guillotine_exit", "calm_zero_peak_general_cut", "calm_legal_zero_peak_cut", "political_pre_momentum_compression_exit", "time_decay_exit", "dead_capital_decay"}:
                                family_memory["family_reopen_brake_count"] = int(family_memory.get("family_reopen_brake_count", 0) or 0) + 1
                            if event.get("market_type") == "sports_award_longshot" and exit_reason == "sports_zombie_guillotine_exit":
                                family_memory["sports_zombie_kill_count"] = int(family_memory.get("sports_zombie_kill_count", 0) or 0) + 1
                            if zero_churn_exit:
                                family_memory["zero_churn_exit_count"] = int(family_memory.get("zero_churn_exit_count", 0) or 0) + 1
                                family_memory["zero_churn_reopen_brake_count"] = int(family_memory.get("zero_churn_reopen_brake_count", 0) or 0) + 1
                                family_memory["last_zero_churn_exit_reason"] = exit_reason
                                family_memory["last_zero_churn_score"] = float(event.get("score", 0.0) or 0.0)
                                family_memory["last_zero_churn_votes"] = int(event.get("follow_through_structure_votes", family_memory.get("last_zero_churn_votes", 0)) or 0)
                            market_exit_memory[family_memory_key] = family_memory

                        apply_light_admission_feedback(delayed_entry_memory, event, now_ts)

                print("DEBUG market regime:")
                print(regime_info)

                print("DEBUG signal memory:")
                print(signal_memory)

                print("DEBUG pressure candidates:")
                for x in pressures:
                    print({
                        "question": x.get("question"),
                        "outcome_name": x.get("outcome_name"),
                        "price": x.get("price"),
                        "pressure_density": x.get("pressure_density"),
                        "pressure_count": x.get("pressure_count"),
                        "price_delta_window": x.get("price_delta_window"),
                        "score": x.get("score"),
                        "theme": x.get("theme"),
                    })

                for idx, x in enumerate(combined):
                    combined[idx] = repair_political_mirror_state(
                        x,
                        delayed_entry_memory=delayed_entry_memory,
                        delayed_entry_watch=delayed_entry_watch,
                        delayed_entry_cooldown=delayed_entry_cooldown,
                        stage="combined_debug",
                        force=False,
                    )

                print("DEBUG combined entry candidates:")
                for x in combined[:16]:
                    key = build_market_key(x)
                    print({
                        "question": x.get("question"),
                        "outcome_name": x.get("outcome_name"),
                        "theme": x.get("theme"),
                        "cluster": x.get("cluster"),
                        "price": x.get("price"),
                        "price_delta": x.get("price_delta"),
                        "price_delta_window": x.get("price_delta_window"),
                        "price_trend_strength": x.get("price_trend_strength"),
                        "pressure_density": x.get("pressure_density"),
                        "pressure_count": x.get("pressure_count"),
                        "score": x.get("score"),
                        "family_key": x.get("family_key"),
                        "family_attack": x.get("_family_attack_score"),
                        "family_contender_bonus": x.get("_family_contender_bonus"),
                        "family_review_mode": x.get("_family_review_mode"),
                        "momentum_entry": is_momentum_entry(x),
                        "momentum_override": is_momentum_override(x),
                        "multicycle_override": is_multicycle_momentum_override(x),
                        "pre_momentum": is_pre_momentum(x),
                        "pressure_entry": is_pressure_entry(x),
                        "survival_priority": round(score_survival_priority(x, (
                            "score+pressure" if is_pressure_entry(x) and float(x.get("score", 0.0) or 0.0) >= 0.8 else
                            "score+pre_momentum" if is_pre_momentum(x) and float(x.get("score", 0.0) or 0.0) >= 0.8 else
                            "multicycle_momentum_override" if is_multicycle_momentum_override(x) and float(x.get("score", 0.0) or 0.0) < 0.8 else
                            "momentum_override" if is_momentum_override(x) and float(x.get("score", 0.0) or 0.0) < 0.8 else
                            "score"
                        ), engine), 4),
                        "cooldown_active": key in momentum_cooldown,
                        "score_reentry_cooldown_active": key in score_reentry_cooldown,
                        "stale_reentry_cooldown_active": key in stale_reentry_cooldown,
                        "delayed_entry_watch_active": key in delayed_entry_watch,
                        "delayed_entry_memory_active": ("market::{}".format(key) in delayed_entry_memory) or ("family::{}".format(x.get("family_key", "")) in delayed_entry_memory),
                        "political_override_active": bool(x.get("_political_family_override", False)),
                        "political_override_strength": float(x.get("_political_override_strength", 0.0) or 0.0),
                        "political_targeted_override": bool(x.get("_political_targeted_override", False)),
                        "balance_rescue_override": bool(x.get("_balance_rescue_override", False)),
                        "cross_family_thesis_priority": bool(x.get("_cross_family_thesis_priority", False)),
                        "cross_family_priority_cycles": int(x.get("_cross_family_priority_cycles", 0) or 0),
                        "political_override_reason": x.get("_political_override_reason"),
                        "override_survival_corridor": bool(x.get("_override_survival_corridor", False)),
                        "political_hold_window": bool(x.get("_political_hold_window", False)),
                        "political_hold_window_cycles": int(x.get("_political_hold_window_cycles", 0) or 0),
                        "flag_mirror_audit_expected": bool(x.get("_flag_mirror_audit_expected", False) or (x.get("_political_family_override", False) and (
                            x.get("_political_hold_window", False) or
                            x.get("_cross_family_thesis_priority", False) or
                            x.get("_override_survival_corridor", False)
                        ))),
                        "universe_source": x.get("_universe_source", "primary"),
                        "edge_registry_version": x.get("_edge_registry_version", EDGE_REGISTRY_VERSION),
                        "regime_detector_version": x.get("_regime_detector_version", REGIME_DETECTOR_VERSION),
                        "already_open": engine.has_open_position(key),
                    })

                snapshot = {
                    "ts": utc_now_iso(),
                    "markets_scanned": len(markets),
                    "outcomes_seen": len(candidates),
                    "outcomes_after_filter": len(filtered),
                    "ranked_after_dedupe": len(ranked),
                    "pulse_candidates": pulses,
                    "trend_candidates": trends,
                    "pressure_candidates": pressures,
                    "explorer_candidates": explorers[:12],
                    "combined_entry_candidates": combined[:16],
                    "market_regime": regime_info,
                    "signal_memory": signal_memory,
                    "runtime_state_counts": {
                        "price_history": len(price_history),
                        "delayed_entry_memory": len(delayed_entry_memory),
                        "delayed_entry_watch": len(delayed_entry_watch),
                    },
                    "lifecycle_events": lifecycle_events[:20],
                    "engine_summary": engine.summary(),
                }
                append_jsonl(SNAPSHOT_FILE, snapshot)

                print("=" * 80)
                print("[{}] scanned={} outcomes={} filtered={} ranked_after_dedupe={} pulses={} trends={} pressures={} explorers={} combined={} regime={} lifecycle_events={}".format(
                    snapshot["ts"],
                    len(markets),
                    len(candidates),
                    len(filtered),
                    len(ranked),
                    len(pulses),
                    len(trends),
                    len(pressures),
                    len(explorers),
                    len(combined),
                    current_regime,
                    len(lifecycle_events)
                ))
                print("engine={}".format(engine.summary()))

                MIN_SCORE = 0.8
                MAX_NEW_POSITIONS_PER_CYCLE = 10
                MAX_CYCLE_RISK_USD = settings["MAX_CYCLE_RISK_USD"]
                MAX_THEME_POSITIONS_PER_CYCLE = settings["MAX_THEME_POSITIONS_PER_CYCLE"]
                MAX_CLUSTER_POSITIONS_PER_CYCLE = settings["MAX_CLUSTER_POSITIONS_PER_CYCLE"]
                STAKE_MULTIPLIER = settings["STAKE_MULTIPLIER"]

                cycle_spend = 0.0
                cycle_theme_counts = {}
                cycle_cluster_counts = {}
                opened_now = 0
                adaptive_admission_budget_total = admission_budget_limit(current_regime, engine=engine, opened_now=opened_now)
                adaptive_admission_budget_used = 0
                print("TRACE | adaptive_admission_budget_cycle | regime={} | total={} | used={} | open_positions={}".format(current_regime, adaptive_admission_budget_total, adaptive_admission_budget_used, int(len(getattr(engine, "open_positions", []) or []))))

                book_pressure = portfolio_pressure_profile(engine)
                if book_pressure["hard_crowded"]:
                    MAX_NEW_POSITIONS_PER_CYCLE = min(MAX_NEW_POSITIONS_PER_CYCLE, 1)
                elif book_pressure["stressed"]:
                    MAX_NEW_POSITIONS_PER_CYCLE = min(MAX_NEW_POSITIONS_PER_CYCLE, 3)
                elif book_pressure["crowded"]:
                    MAX_NEW_POSITIONS_PER_CYCLE = min(MAX_NEW_POSITIONS_PER_CYCLE, 5)

                for candidate in combined:
                    candidate["market_type"] = detect_market_type(candidate)
                    key = build_market_key(candidate)
                    theme = candidate.get("theme", "unknown")
                    cluster = candidate.get("cluster", "unknown")
                    market_type = candidate.get("market_type", "general_binary")
                    score_value = float(candidate.get("score", 0.0) or 0.0)

                    if engine.has_open_position(key):
                        continue

                    score_ok = score_value >= MIN_SCORE
                    momentum_ok = is_momentum_entry(candidate)
                    override_ok = is_momentum_override(candidate)
                    multicycle_ok = is_multicycle_momentum_override(candidate)
                    pre_momentum_ok = is_pre_momentum(candidate)
                    pressure_ok = is_pressure_entry(candidate)

                    if (override_ok or multicycle_ok or pre_momentum_ok or pressure_ok) and key in momentum_cooldown:
                        override_ok = False
                        momentum_ok = False
                        multicycle_ok = False
                        pre_momentum_ok = False
                        pressure_ok = False

                    if not score_ok and not momentum_ok and not override_ok and not multicycle_ok and not pre_momentum_ok and not pressure_ok:
                        continue

                    if pressure_ok and score_ok:
                        reason = "score+pressure"
                    elif pre_momentum_ok and score_ok:
                        reason = "score+pre_momentum"
                    elif multicycle_ok and not score_ok:
                        reason = "multicycle_momentum_override"
                    elif override_ok and not score_ok:
                        reason = "momentum_override"
                    elif momentum_ok and not score_ok:
                        reason = "momentum"
                    elif pre_momentum_ok and not score_ok:
                        reason = "pre_momentum"
                    elif pressure_ok and not score_ok:
                        reason = "pressure"
                    elif (momentum_ok or override_ok or multicycle_ok) and score_ok:
                        reason = "score+momentum"
                    else:
                        reason = "score"

                    # Отдельный замок от reopen churn для score-входов
                    if reason == "score" and key in score_reentry_cooldown:
                        continue

                    relief_state = selective_overblock_relief_state(
                        candidate,
                        reason,
                        market_exit_memory=market_exit_memory,
                        family_dead_cooldown=family_dead_cooldown,
                        now_ts=now_ts,
                        current_regime=current_regime,
                    )
                    trace_relief_router_state(candidate, relief_state, reason)
                    adaptive_budget_state = {
                        "considered": False,
                        "active": False,
                        "signal": None,
                        "reject_reason": "not_initialized",
                    }

                    weak_legal_override_blocked, weak_legal_override_reason = should_block_weak_legal_override(
                        candidate,
                        reason,
                        market_exit_memory,
                    )
                    if weak_legal_override_blocked:
                        print("SKIP | {} | reason={} | score={:.3f} | pd={:.3f} | trend={:.3f} | delta1={:.3f} | win={:.3f} | pcount={} | {}".format(
                            weak_legal_override_reason, reason, float(candidate.get("score", 0.0) or 0.0), float(candidate.get("pressure_density", 0.0) or 0.0), float(candidate.get("price_trend_strength", 0.0) or 0.0), abs(float(candidate.get("price_delta", 0.0) or 0.0)), abs(float(candidate.get("price_delta_window", 0.0) or 0.0)), int(candidate.get("pressure_count", 0) or 0), (candidate.get("question") or "")[:110]
                        ))
                        continue

                    sports_longshot_blocked, sports_longshot_reason = should_block_sports_longshot_churn(
                        candidate,
                        reason,
                        current_regime,
                        market_exit_memory,
                    )
                    if sports_longshot_blocked:
                        print("SKIP | {} | reason={} | score={:.3f} | pd={:.3f} | trend={:.3f} | delta1={:.3f} | win={:.3f} | pcount={} | regime={} | {}".format(
                            sports_longshot_reason, reason, float(candidate.get("score", 0.0) or 0.0), float(candidate.get("pressure_density", 0.0) or 0.0), float(candidate.get("price_trend_strength", 0.0) or 0.0), abs(float(candidate.get("price_delta", 0.0) or 0.0)), abs(float(candidate.get("price_delta_window", 0.0) or 0.0)), int(candidate.get("pressure_count", 0) or 0), current_regime, (candidate.get("question") or "")[:110]
                        ))
                        continue

                    legal_false_pressure_blocked, legal_false_pressure_reason = should_block_legal_false_pressure_quarantine(
                        candidate,
                        reason,
                        market_exit_memory,
                    )
                    if legal_false_pressure_blocked:
                        if admission_budget_allows_block(adaptive_budget_state, legal_false_pressure_reason):
                            candidate = prime_admission_budget_route(candidate, adaptive_budget_state, legal_false_pressure_reason)
                        else:
                            print("SKIP | {} | reason={} | score={:.3f} | pd={:.3f} | trend={:.3f} | delta1={:.3f} | win={:.3f} | pcount={} | {}".format(
                                legal_false_pressure_reason,
                                reason,
                                float(candidate.get("score", 0.0) or 0.0),
                                float(candidate.get("pressure_density", 0.0) or 0.0),
                                float(candidate.get("price_trend_strength", 0.0) or 0.0),
                                abs(float(candidate.get("price_delta", 0.0) or 0.0)),
                                abs(float(candidate.get("price_delta_window", 0.0) or 0.0)),
                                int(candidate.get("pressure_count", 0) or 0),
                                (candidate.get("question") or "")[:110]
                            ))
                            continue

                    legal_pressure_blocked, legal_pressure_reason = should_block_legal_pressure_admission(
                        candidate,
                        reason,
                        market_exit_memory,
                    )
                    if legal_pressure_blocked:
                        if admission_budget_allows_block(adaptive_budget_state, legal_pressure_reason):
                            candidate = prime_admission_budget_route(candidate, adaptive_budget_state, legal_pressure_reason)
                        elif relief_state.get("active", False) and relief_state.get("allow_legal_cooldown", False) and legal_pressure_reason in {"legal_cooldown_authority_gate", "legal_cooldown_memory_veto"}:
                            candidate = prime_relief_escalation(candidate, relief_state, legal_pressure_reason)
                        else:
                            print("SKIP | {} | reason={} | score={:.3f} | pd={:.3f} | trend={:.3f} | delta1={:.3f} | win={:.3f} | pcount={} | score_cd={} | stale_cd={} | {}".format(
                                legal_pressure_reason,
                                reason,
                                float(candidate.get("score", 0.0) or 0.0),
                                float(candidate.get("pressure_density", 0.0) or 0.0),
                                float(candidate.get("price_trend_strength", 0.0) or 0.0),
                                abs(float(candidate.get("price_delta", 0.0) or 0.0)),
                                abs(float(candidate.get("price_delta_window", 0.0) or 0.0)),
                                int(candidate.get("pressure_count", 0) or 0),
                                int(bool(candidate.get("score_reentry_cooldown_active", False))),
                                int(bool(candidate.get("stale_reentry_cooldown_active", False))),
                                (candidate.get("question") or "")[:110]
                            ))
                            continue

                    legal_replay_blocked, legal_replay_reason = should_block_legal_replay(
                        candidate,
                        reason,
                        market_exit_memory,
                        family_dead_cooldown,
                        now_ts,
                    )
                    if legal_replay_blocked:
                        if admission_budget_allows_block(adaptive_budget_state, legal_replay_reason):
                            candidate = prime_admission_budget_route(candidate, adaptive_budget_state, legal_replay_reason)
                        elif relief_state.get("active", False) and relief_state.get("allow_legal_replay", False) and legal_replay_reason in {"legal_replay_quarantine", "legal_replay_memory", "legal_stale_loss_reentry_kill"}:
                            candidate = prime_relief_escalation(candidate, relief_state, legal_replay_reason)
                        else:
                            print("SKIP | {} | reason={} | score={:.3f} | pd={:.3f} | trend={:.3f} | win={:.3f} | {}".format(
                                legal_replay_reason,
                                reason,
                                float(candidate.get("score", 0.0) or 0.0),
                                float(candidate.get("pressure_density", 0.0) or 0.0),
                                float(candidate.get("price_trend_strength", 0.0) or 0.0),
                                abs(float(candidate.get("price_delta_window", 0.0) or 0.0)),
                                (candidate.get("question") or "")[:110]
                            ))
                            continue

                    prime_recovery_router_context(candidate, delayed_entry_memory, delayed_entry_watch)
                    relief_state = selective_overblock_relief_state(
                        candidate,
                        reason,
                        market_exit_memory=market_exit_memory,
                        family_dead_cooldown=family_dead_cooldown,
                        now_ts=now_ts,
                        current_regime=current_regime,
                    )
                    trace_relief_router_state(candidate, relief_state, reason)

                    winner_reentry_blocked, winner_reentry_reason = should_block_winner_reentry(candidate, reason, market_exit_memory)
                    if winner_reentry_blocked:
                        print("SKIP | {} | reason={} | score={:.3f} | pd={:.3f} | trend={:.3f} | win={:.3f} | {}".format(
                            winner_reentry_reason,
                            reason,
                            float(candidate.get("score", 0.0) or 0.0),
                            float(candidate.get("pressure_density", 0.0) or 0.0),
                            float(candidate.get("price_trend_strength", 0.0) or 0.0),
                            abs(float(candidate.get("price_delta_window", 0.0) or 0.0)),
                            (candidate.get("question") or "")[:110]
                        ))
                        continue

                    failed_runner_blocked, failed_runner_reason = should_block_failed_runner_quarantine(candidate, reason, market_exit_memory)
                    if failed_runner_blocked:
                        print("SKIP | {} | reason={} | score={:.3f} | pd={:.3f} | trend={:.3f} | delta1={:.3f} | win={:.3f} | pcount={} | {}".format(
                            failed_runner_reason,
                            reason,
                            float(candidate.get("score", 0.0) or 0.0),
                            float(candidate.get("pressure_density", 0.0) or 0.0),
                            float(candidate.get("price_trend_strength", 0.0) or 0.0),
                            abs(float(candidate.get("price_delta", 0.0) or 0.0)),
                            abs(float(candidate.get("price_delta_window", 0.0) or 0.0)),
                            int(candidate.get("pressure_count", 0) or 0),
                            (candidate.get("question") or "")[:110]
                        ))
                        continue

                    family_key = candidate.get("family_key")
                    candidate["_delayed_entry_memory_active"] = ("market::{}".format(key) in delayed_entry_memory) or ("family::{}".format(family_key or "") in delayed_entry_memory)
                    candidate["_delayed_entry_watch_active"] = key in delayed_entry_watch
                    candidate = repair_political_mirror_state(
                        candidate,
                        delayed_entry_memory=delayed_entry_memory,
                        delayed_entry_watch=delayed_entry_watch,
                        delayed_entry_cooldown=delayed_entry_cooldown,
                        stage="entry_seed",
                        force=False,
                    )
                    adaptive_budget_state = adaptive_admission_budget_state(
                        candidate,
                        reason,
                        current_regime,
                        market_exit_memory=market_exit_memory,
                        budget_used=adaptive_admission_budget_used,
                        budget_total=adaptive_admission_budget_total,
                        engine=engine,
                        opened_now=opened_now,
                    )
                    trace_adaptive_admission_budget_state(candidate, adaptive_budget_state, reason)
                    relief_state = selective_overblock_relief_state(
                        candidate,
                        reason,
                        market_exit_memory=market_exit_memory,
                        family_dead_cooldown=family_dead_cooldown,
                        now_ts=now_ts,
                        current_regime=current_regime,
                    )

                    if key in dead_reentry_cooldown and not has_strong_reentry_signal(candidate, reason):
                        if admission_budget_allows_block(adaptive_budget_state, "dead_reentry_cooldown"):
                            candidate = prime_admission_budget_route(candidate, adaptive_budget_state, "dead_reentry_cooldown")
                        else:
                            print("SKIP | dead_reentry_cooldown | reason={} | {}".format(
                                reason,
                                (candidate.get("question") or "")[:110]
                            ))
                            continue

                    if family_key in family_dead_cooldown and not has_strong_reentry_signal(candidate, reason):
                        if admission_budget_allows_block(adaptive_budget_state, "family_dead_cooldown"):
                            candidate = prime_admission_budget_route(candidate, adaptive_budget_state, "family_dead_cooldown")
                        elif relief_state.get("active", False) and relief_state.get("allow_family_dead", False):
                            candidate = prime_relief_escalation(candidate, relief_state, "family_dead_cooldown")
                            print("TRACE | selective_overblock_relief | block=family_dead_cooldown | signal={} | score={:.3f} | pd={:.3f} | trend={:.3f} | win={:.3f} | votes={} | {}".format(
                                relief_state.get("signal", "unknown"),
                                float(candidate.get("score", 0.0) or 0.0),
                                float(candidate.get("pressure_density", 0.0) or 0.0),
                                float(candidate.get("price_trend_strength", 0.0) or 0.0),
                                abs(float(candidate.get("price_delta_window", 0.0) or 0.0)),
                                int(relief_state.get("structure_votes", 0) or 0),
                                (candidate.get("question") or "")[:110]
                            ))
                        else:
                            print("SKIP | family_dead_cooldown | family={} | reason={} | {}".format(
                                (family_key or "unknown")[:96],
                                reason,
                                (candidate.get("question") or "")[:110]
                            ))
                            continue

                    speculative_reopen_blocked, speculative_reopen_reason = should_block_speculative_hype_reopen(candidate, reason, market_exit_memory)
                    if speculative_reopen_blocked:
                        print("SKIP | {} | reason={} | score={:.3f} | pd={:.3f} | trend={:.3f} | delta1={:.3f} | win={:.3f} | pcount={} | {}".format(
                            speculative_reopen_reason,
                            reason,
                            float(candidate.get("score", 0.0) or 0.0),
                            float(candidate.get("pressure_density", 0.0) or 0.0),
                            float(candidate.get("price_trend_strength", 0.0) or 0.0),
                            abs(float(candidate.get("price_delta", 0.0) or 0.0)),
                            abs(float(candidate.get("price_delta_window", 0.0) or 0.0)),
                            int(candidate.get("pressure_count", 0) or 0),
                            (candidate.get("question") or "")[:110]
                        ))
                        continue

                    family_reopen_blocked, family_reopen_reason = should_block_family_reopen_brake(candidate, reason, market_exit_memory)
                    if family_reopen_blocked:
                        if admission_budget_allows_block(adaptive_budget_state, family_reopen_reason):
                            candidate = prime_admission_budget_route(candidate, adaptive_budget_state, family_reopen_reason)
                        elif relief_state.get("active", False) and relief_state.get("allow_family_reopen", False) and family_reopen_reason in {"family_reopen_brake", "family_reopen_memory_brake", "family_reopen_truth_gate"}:
                            candidate = prime_relief_escalation(candidate, relief_state, family_reopen_reason)
                            print("TRACE | selective_overblock_relief | block={} | signal={} | score={:.3f} | pd={:.3f} | trend={:.3f} | delta1={:.3f} | win={:.3f} | votes={} | family={} | {}".format(
                                family_reopen_reason,
                                relief_state.get("signal", "unknown"),
                                float(candidate.get("score", 0.0) or 0.0),
                                float(candidate.get("pressure_density", 0.0) or 0.0),
                                float(candidate.get("price_trend_strength", 0.0) or 0.0),
                                abs(float(candidate.get("price_delta", 0.0) or 0.0)),
                                abs(float(candidate.get("price_delta_window", 0.0) or 0.0)),
                                int(relief_state.get("structure_votes", 0) or 0),
                                (candidate.get("family_key") or "unknown")[:72],
                                (candidate.get("question") or "")[:110]
                            ))
                        else:
                            print("SKIP | {} | reason={} | score={:.3f} | pd={:.3f} | trend={:.3f} | delta1={:.3f} | win={:.3f} | pcount={} | family={} | {}".format(
                                family_reopen_reason,
                                reason,
                                float(candidate.get("score", 0.0) or 0.0),
                                float(candidate.get("pressure_density", 0.0) or 0.0),
                                float(candidate.get("price_trend_strength", 0.0) or 0.0),
                                abs(float(candidate.get("price_delta", 0.0) or 0.0)),
                                abs(float(candidate.get("price_delta_window", 0.0) or 0.0)),
                                int(candidate.get("pressure_count", 0) or 0),
                                (candidate.get("family_key") or "unknown")[:72],
                                (candidate.get("question") or "")[:110]
                            ))
                            continue

                    zero_churn_blocked, zero_churn_reason = should_block_zero_churn_guillotine(candidate, reason, market_exit_memory)
                    if zero_churn_blocked:
                        print("SKIP | {} | reason={} | score={:.3f} | pd={:.3f} | trend={:.3f} | delta1={:.3f} | win={:.3f} | pcount={} | {}".format(
                            zero_churn_reason,
                            reason,
                            float(candidate.get("score", 0.0) or 0.0),
                            float(candidate.get("pressure_density", 0.0) or 0.0),
                            float(candidate.get("price_trend_strength", 0.0) or 0.0),
                            abs(float(candidate.get("price_delta", 0.0) or 0.0)),
                            abs(float(candidate.get("price_delta_window", 0.0) or 0.0)),
                            int(candidate.get("pressure_count", 0) or 0),
                            (candidate.get("question") or "")[:110]
                        ))
                        continue

                    stale_blocked, stale_reason = should_block_stale_reopen(candidate, reason, stale_reentry_cooldown, market_exit_memory)
                    if stale_blocked:
                        if admission_budget_allows_block(adaptive_budget_state, stale_reason):
                            candidate = prime_admission_budget_route(candidate, adaptive_budget_state, stale_reason)
                        else:
                            print("SKIP | {} | reason={} | {}".format(
                                stale_reason,
                                reason,
                                (candidate.get("question") or "")[:110]
                            ))
                            continue

                    gate_ok, gate_reason = entry_quality_gate(candidate, reason, current_regime, market_exit_memory)
                    if not gate_ok:
                        if admission_budget_allows_block(adaptive_budget_state, gate_reason):
                            candidate = prime_admission_budget_route(candidate, adaptive_budget_state, gate_reason)
                        else:
                            print("SKIP | {} | reason={} | score={:.3f} | pd={:.3f} | trend={:.3f} | win={:.3f} | {}".format(
                                gate_reason,
                                reason,
                                score_value,
                                float(candidate.get("pressure_density", 0.0) or 0.0),
                                float(candidate.get("price_trend_strength", 0.0) or 0.0),
                                abs(float(candidate.get("price_delta_window", 0.0) or 0.0)),
                                (candidate.get("question") or "")[:110]
                            ))
                            continue

                    pressure_gate_state = calm_pressure_quality_gate_state(candidate, reason, current_regime)
                    if pressure_gate_state.get("block", False):
                        print("SKIP | calm_pressure_quality_gate_wiring | reason={} | score={:.3f} | pd={:.3f} | trend={:.3f} | win={:.3f} | pcount={} | {}".format(
                            reason,
                            float(pressure_gate_state.get("score", score_value) or score_value),
                            float(pressure_gate_state.get("density", candidate.get("pressure_density", 0.0)) or 0.0),
                            float(pressure_gate_state.get("trend", candidate.get("price_trend_strength", 0.0)) or 0.0),
                            float(pressure_gate_state.get("window_delta", abs(float(candidate.get("price_delta_window", 0.0) or 0.0))) or 0.0),
                            int(pressure_gate_state.get("pressure_count", candidate.get("pressure_count", 0)) or 0),
                            (candidate.get("question") or "")[:110]
                        ))
                        candidate["skip_reason"] = "calm_pressure_quality_gate_wiring"
                        continue

                    pressure_hard_state = calm_pressure_hard_block_state(candidate, reason, current_regime)
                    if pressure_hard_state.get("block", False):
                        print("SKIP | calm_pressure_hard_block_wiring | reason={} | score={:.3f} | pd={:.3f} | trend={:.3f} | win={:.3f} | pcount={} | override={} | strength={:.3f} | {}".format(
                            reason,
                            float(pressure_hard_state.get("score", score_value) or score_value),
                            float(pressure_hard_state.get("density", candidate.get("pressure_density", 0.0)) or 0.0),
                            float(pressure_hard_state.get("trend", candidate.get("price_trend_strength", 0.0)) or 0.0),
                            float(pressure_hard_state.get("window_delta", abs(float(candidate.get("price_delta_window", 0.0) or 0.0))) or 0.0),
                            int(pressure_hard_state.get("pressure_count", candidate.get("pressure_count", 0)) or 0),
                            int(bool(pressure_hard_state.get("political_override", False))),
                            float(pressure_hard_state.get("override_strength", 0.0) or 0.0),
                            (candidate.get("question") or "")[:110]
                        ))
                        candidate["skip_reason"] = "calm_pressure_hard_block_wiring"
                        continue

                    pressure_decay_state = pressure_decay_preentry_state(candidate, reason, current_regime)
                    if pressure_decay_state.get("block", False):
                        print("SKIP | pressure_decay_preentry_gate_wiring | reason={} | score={:.3f} | pd={:.3f} | trend={:.3f} | delta1={:.3f} | win={:.3f} | pcount={} | {}".format(
                            reason,
                            float(pressure_decay_state.get("score", score_value) or score_value),
                            float(pressure_decay_state.get("density", candidate.get("pressure_density", 0.0)) or 0.0),
                            float(pressure_decay_state.get("trend", candidate.get("price_trend_strength", 0.0)) or 0.0),
                            float(pressure_decay_state.get("delta_1", abs(float(candidate.get("price_delta", 0.0) or 0.0))) or 0.0),
                            float(pressure_decay_state.get("window_delta", abs(float(candidate.get("price_delta_window", 0.0) or 0.0))) or 0.0),
                            int(pressure_decay_state.get("pressure_count", candidate.get("pressure_count", 0)) or 0),
                            (candidate.get("question") or "")[:110]
                        ))
                        candidate["skip_reason"] = "pressure_decay_preentry_gate_wiring"
                        continue

                    weak_legal_override_blocked, weak_legal_override_reason = should_block_weak_legal_override(
                        candidate,
                        reason,
                        market_exit_memory,
                    )
                    if weak_legal_override_blocked:
                        print("SKIP | {}_wiring | reason={} | score={:.3f} | pd={:.3f} | trend={:.3f} | delta1={:.3f} | win={:.3f} | pcount={} | {}".format(
                            weak_legal_override_reason, reason, float(candidate.get("score", 0.0) or 0.0), float(candidate.get("pressure_density", 0.0) or 0.0), float(candidate.get("price_trend_strength", 0.0) or 0.0), abs(float(candidate.get("price_delta", 0.0) or 0.0)), abs(float(candidate.get("price_delta_window", 0.0) or 0.0)), int(candidate.get("pressure_count", 0) or 0), (candidate.get("question") or "")[:110]
                        ))
                        candidate["skip_reason"] = "{}_wiring".format(weak_legal_override_reason)
                        continue

                    sports_longshot_blocked, sports_longshot_reason = should_block_sports_longshot_churn(
                        candidate,
                        reason,
                        current_regime,
                        market_exit_memory,
                    )
                    if sports_longshot_blocked:
                        print("SKIP | {}_wiring | reason={} | score={:.3f} | pd={:.3f} | trend={:.3f} | delta1={:.3f} | win={:.3f} | pcount={} | regime={} | {}".format(
                            sports_longshot_reason, reason, float(candidate.get("score", 0.0) or 0.0), float(candidate.get("pressure_density", 0.0) or 0.0), float(candidate.get("price_trend_strength", 0.0) or 0.0), abs(float(candidate.get("price_delta", 0.0) or 0.0)), abs(float(candidate.get("price_delta_window", 0.0) or 0.0)), int(candidate.get("pressure_count", 0) or 0), current_regime, (candidate.get("question") or "")[:110]
                        ))
                        candidate["skip_reason"] = "{}_wiring".format(sports_longshot_reason)
                        continue

                    legal_false_pressure_blocked, legal_false_pressure_reason = should_block_legal_false_pressure_quarantine(
                        candidate,
                        reason,
                        market_exit_memory,
                    )
                    if legal_false_pressure_blocked:
                        print("SKIP | {}_wiring | reason={} | score={:.3f} | pd={:.3f} | trend={:.3f} | delta1={:.3f} | win={:.3f} | pcount={} | {}".format(
                            legal_false_pressure_reason,
                            reason,
                            float(candidate.get("score", 0.0) or 0.0),
                            float(candidate.get("pressure_density", 0.0) or 0.0),
                            float(candidate.get("price_trend_strength", 0.0) or 0.0),
                            abs(float(candidate.get("price_delta", 0.0) or 0.0)),
                            abs(float(candidate.get("price_delta_window", 0.0) or 0.0)),
                            int(candidate.get("pressure_count", 0) or 0),
                            (candidate.get("question") or "")[:110]
                        ))
                        candidate["skip_reason"] = "{}_wiring".format(legal_false_pressure_reason)
                        continue

                    legal_cooldown_blocked, legal_cooldown_reason = should_block_legal_pressure_admission(
                        candidate,
                        reason,
                        market_exit_memory,
                    )
                    if legal_cooldown_blocked:
                        if relief_state.get("active", False) and relief_state.get("allow_legal_cooldown", False) and legal_cooldown_reason in {"legal_cooldown_authority_gate", "legal_cooldown_memory_veto"}:
                            candidate = prime_relief_escalation(candidate, relief_state, legal_cooldown_reason)
                        else:
                            print("SKIP | {}_wiring | reason={} | score={:.3f} | pd={:.3f} | trend={:.3f} | delta1={:.3f} | win={:.3f} | pcount={} | score_cd={} | stale_cd={} | {}".format(
                                legal_cooldown_reason,
                                reason,
                                float(candidate.get("score", 0.0) or 0.0),
                                float(candidate.get("pressure_density", 0.0) or 0.0),
                                float(candidate.get("price_trend_strength", 0.0) or 0.0),
                                abs(float(candidate.get("price_delta", 0.0) or 0.0)),
                                abs(float(candidate.get("price_delta_window", 0.0) or 0.0)),
                                int(candidate.get("pressure_count", 0) or 0),
                                int(bool(candidate.get("score_reentry_cooldown_active", False))),
                                int(bool(candidate.get("stale_reentry_cooldown_active", False))),
                                (candidate.get("question") or "")[:110]
                            ))
                            candidate["skip_reason"] = "{}_wiring".format(legal_cooldown_reason)
                            continue

                    sports_override_state = weak_sports_override_brake_state(candidate, current_regime, reason)
                    if sports_override_state.get("hard_block", False):
                        print("SKIP | weak_sports_override_hard_block_wiring | reason={} | score={:.3f} | pd={:.3f} | trend={:.3f} | win={:.3f} | pcount={} | {}".format(
                            reason,
                            float(sports_override_state.get("score", score_value) or score_value),
                            float(sports_override_state.get("density", candidate.get("pressure_density", 0.0)) or 0.0),
                            float(sports_override_state.get("trend", candidate.get("price_trend_strength", 0.0)) or 0.0),
                            float(sports_override_state.get("window_delta", abs(float(candidate.get("price_delta_window", 0.0) or 0.0))) or 0.0),
                            int(sports_override_state.get("pressure_count", candidate.get("pressure_count", 0)) or 0),
                            (candidate.get("question") or "")[:110]
                        ))
                        candidate["skip_reason"] = "weak_sports_override_hard_block_wiring"
                        continue

                    if candidate.get("_political_family_override") and current_regime == "calm" and family_key:
                        candidate["_family_force_review"] = True
                        candidate["_family_trigger_source"] = candidate.get("_political_override_reason") or "political_family_override"
                        candidate["_family_review_mode"] = candidate.get("_family_review_mode") or "family_swap_review"
                        candidate["_family_contender_bonus"] = max(
                            float(candidate.get("_family_contender_bonus", 0.0) or 0.0),
                            float(candidate.get("_political_override_strength", 0.0) or 0.0)
                        )
                        candidate["_family_attack_score"] = round(family_attack_score(candidate, reason, current_regime, engine=engine), 4)
                        candidate["_political_override_applied"] = True
                        candidate["_override_survival_corridor"] = True
                        candidate["_political_hold_window"] = True
                        candidate["_cross_family_thesis_priority"] = True
                        candidate["_political_hold_window_cycles"] = 8 if (candidate.get("_political_targeted_override") or candidate.get("_balance_rescue_override")) else 6
                        candidate["_cross_family_priority_cycles"] = 8 if candidate.get("_cross_family_thesis_priority") else 0
                        print("INFO | {} | strength={:.3f} | family={} | {}".format(
                            candidate.get("_political_override_reason") or "political_family_override",
                            float(candidate.get("_political_override_strength", 0.0) or 0.0),
                            (family_key or "unknown")[:96],
                            (candidate.get("question") or "")[:110]
                        ))
                        candidate = repair_political_mirror_state(
                            candidate,
                            delayed_entry_memory=delayed_entry_memory,
                            delayed_entry_watch=delayed_entry_watch,
                            delayed_entry_cooldown=delayed_entry_cooldown,
                            stage="family_force_review",
                            force=True,
                        )

                    scout_state = political_rescue_scout_demotion_state(candidate, current_regime, reason)
                    candidate["_political_rescue_scout_demotion"] = bool(scout_state.get("active", False))
                    candidate["_political_rescue_scout_cap"] = float(scout_state.get("cap", 0.0) or 0.0)
                    if scout_state.get("active", False):
                        candidate["_political_hold_window_cycles"] = min(
                            int(candidate.get("_political_hold_window_cycles", 0) or 0) if int(candidate.get("_political_hold_window_cycles", 0) or 0) > 0 else int(scout_state.get("hold_cycles", 4) or 4),
                            int(scout_state.get("hold_cycles", 4) or 4)
                        )
                        candidate["_cross_family_priority_cycles"] = min(
                            int(candidate.get("_cross_family_priority_cycles", 0) or 0) if int(candidate.get("_cross_family_priority_cycles", 0) or 0) > 0 else int(scout_state.get("cross_cycles", 4) or 4),
                            int(scout_state.get("cross_cycles", 4) or 4)
                        )
                        print("TRACE | political_rescue_scout_demotion | signal={} | cap={:.2f} | hold_cycles={} | cross_cycles={} | score={:.3f} | pd={:.3f} | trend={:.3f} | win={:.3f} | strength={:.3f} | {}".format(
                            scout_state.get("signal", "political_rescue_scout"),
                            float(scout_state.get("cap", 0.0) or 0.0),
                            int(candidate.get("_political_hold_window_cycles", 0) or 0),
                            int(candidate.get("_cross_family_priority_cycles", 0) or 0),
                            float(scout_state.get("score", 0.0) or 0.0),
                            float(scout_state.get("density", 0.0) or 0.0),
                            float(scout_state.get("trend", 0.0) or 0.0),
                            float(scout_state.get("window_delta", 0.0) or 0.0),
                            float(scout_state.get("override_strength", 0.0) or 0.0),
                            (candidate.get("question") or "")[:110]
                        ))
                        if str(scout_state.get("signal", "") or "").startswith("targeted_political_scout_force"):
                            print("TRACE | targeted_political_scout_activation | cap={:.2f} | hold_cycles={} | cross_cycles={} | score={:.3f} | pd={:.3f} | trend={:.3f} | win={:.3f} | strength={:.3f} | {}".format(
                                float(scout_state.get("cap", 0.0) or 0.0),
                                int(candidate.get("_political_hold_window_cycles", 0) or 0),
                                int(candidate.get("_cross_family_priority_cycles", 0) or 0),
                                float(scout_state.get("score", 0.0) or 0.0),
                                float(scout_state.get("density", 0.0) or 0.0),
                                float(scout_state.get("trend", 0.0) or 0.0),
                                float(scout_state.get("window_delta", 0.0) or 0.0),
                                float(scout_state.get("override_strength", 0.0) or 0.0),
                                (candidate.get("question") or "")[:110]
                            ))
                        candidate = repair_political_mirror_state(
                            candidate,
                            delayed_entry_memory=delayed_entry_memory,
                            delayed_entry_watch=delayed_entry_watch,
                            delayed_entry_cooldown=delayed_entry_cooldown,
                            stage="political_scout_sync",
                            force=False,
                        )

                    narrative_brake_state = narrative_full_size_brake_state(candidate, current_regime, reason)
                    candidate["_narrative_full_size_brake"] = bool(narrative_brake_state.get("active", False))
                    candidate["_narrative_full_size_brake_cap"] = float(narrative_brake_state.get("cap", 0.0) or 0.0)
                    if narrative_brake_state.get("active", False):
                        print("TRACE | narrative_full_size_brake | signal={} | cap={:.2f} | score={:.3f} | pd={:.3f} | trend={:.3f} | win={:.3f} | pcount={} | strength={:.3f} | {}".format(
                            narrative_brake_state.get("signal", "narrative_full_size_brake"),
                            float(narrative_brake_state.get("cap", 0.0) or 0.0),
                            float(narrative_brake_state.get("score", 0.0) or 0.0),
                            float(narrative_brake_state.get("density", 0.0) or 0.0),
                            float(narrative_brake_state.get("trend", 0.0) or 0.0),
                            float(narrative_brake_state.get("window_delta", 0.0) or 0.0),
                            int(narrative_brake_state.get("pressure_count", 0) or 0),
                            float(narrative_brake_state.get("override_strength", 0.0) or 0.0),
                            (candidate.get("question") or "")[:110]
                        ))

                    sports_override_state = weak_sports_override_brake_state(candidate, current_regime, reason)
                    candidate["_weak_sports_override_brake"] = bool(sports_override_state.get("active", False))
                    candidate["_weak_sports_override_hard_block"] = bool(sports_override_state.get("hard_block", False))
                    candidate["_weak_sports_override_brake_cap"] = float(sports_override_state.get("cap", 0.0) or 0.0)
                    if sports_override_state.get("active", False):
                        print("TRACE | weak_sports_override_brake | signal={} | hard_block={} | cap={:.2f} | score={:.3f} | pd={:.3f} | trend={:.3f} | win={:.3f} | pcount={} | reason={} | {}".format(
                            sports_override_state.get("signal", "weak_sports_override_brake"),
                            int(bool(sports_override_state.get("hard_block", False))),
                            float(sports_override_state.get("cap", 0.0) or 0.0),
                            float(sports_override_state.get("score", 0.0) or 0.0),
                            float(sports_override_state.get("density", 0.0) or 0.0),
                            float(sports_override_state.get("trend", 0.0) or 0.0),
                            float(sports_override_state.get("window_delta", 0.0) or 0.0),
                            int(sports_override_state.get("pressure_count", 0) or 0),
                            reason,
                            (candidate.get("question") or "")[:110]
                        ))

                    thin_pressure_state = thin_pressure_truth_state(
                        candidate,
                        reason,
                        current_regime,
                        market_exit_memory,
                    )
                    candidate["_thin_pressure_truth_active"] = bool(thin_pressure_state.get("active", False) or thin_pressure_state.get("force_delayed", False))
                    candidate["_thin_pressure_truth_signal"] = thin_pressure_state.get("signal")
                    candidate["_thin_pressure_truth_cap"] = float(thin_pressure_state.get("cap", 0.0) or 0.0)
                    candidate["_thin_pressure_truth_risk"] = int(thin_pressure_state.get("risk_score", 0) or 0)
                    candidate["_thin_pressure_truth_force_delayed"] = bool(thin_pressure_state.get("force_delayed", False))
                    if thin_pressure_state.get("hard_block", False):
                        print("SKIP | thin_pressure_truth_hard_block | reason={} | signal={} | risk={} | score={:.3f} | pd={:.3f} | trend={:.3f} | delta1={:.3f} | win={:.3f} | pcount={} | {}".format(
                            reason,
                            str(thin_pressure_state.get("signal") or "thin_pressure_truth_hard_block"),
                            int(thin_pressure_state.get("risk_score", 0) or 0),
                            float(thin_pressure_state.get("score", score_value) or score_value),
                            float(thin_pressure_state.get("density", candidate.get("pressure_density", 0.0)) or 0.0),
                            float(thin_pressure_state.get("trend", candidate.get("price_trend_strength", 0.0)) or 0.0),
                            float(thin_pressure_state.get("delta", abs(float(candidate.get("price_delta", 0.0) or 0.0))) or 0.0),
                            float(thin_pressure_state.get("window_delta", abs(float(candidate.get("price_delta_window", 0.0) or 0.0))) or 0.0),
                            int(thin_pressure_state.get("pressure_count", candidate.get("pressure_count", 0)) or 0),
                            (candidate.get("question") or "")[:110]
                        ))
                        candidate["skip_reason"] = "thin_pressure_truth_hard_block"
                        continue
                    if thin_pressure_state.get("active", False) or thin_pressure_state.get("force_delayed", False):
                        print("TRACE | thin_pressure_truth_gate | signal={} | reason={} | risk={} | force_delayed={} | scout={} | cap={:.2f} | score={:.3f} | pd={:.3f} | trend={:.3f} | delta1={:.3f} | win={:.3f} | pcount={} | {}".format(
                            str(thin_pressure_state.get("signal") or "thin_pressure_truth_gate"),
                            reason,
                            int(thin_pressure_state.get("risk_score", 0) or 0),
                            int(bool(thin_pressure_state.get("force_delayed", False))),
                            int(bool(thin_pressure_state.get("scout_mode", False))),
                            float(thin_pressure_state.get("cap", 0.0) or 0.0),
                            float(thin_pressure_state.get("score", score_value) or score_value),
                            float(thin_pressure_state.get("density", candidate.get("pressure_density", 0.0)) or 0.0),
                            float(thin_pressure_state.get("trend", candidate.get("price_trend_strength", 0.0)) or 0.0),
                            float(thin_pressure_state.get("delta", abs(float(candidate.get("price_delta", 0.0) or 0.0))) or 0.0),
                            float(thin_pressure_state.get("window_delta", abs(float(candidate.get("price_delta_window", 0.0) or 0.0))) or 0.0),
                            int(thin_pressure_state.get("pressure_count", candidate.get("pressure_count", 0)) or 0),
                            (candidate.get("question") or "")[:110]
                        ))

                    follow_through_state = follow_through_dead_money_state(
                        candidate,
                        reason,
                        current_regime,
                        market_exit_memory,
                    )
                    candidate["_dead_money_compression_active"] = bool(follow_through_state.get("active", False) or follow_through_state.get("force_delayed", False))
                    candidate["_dead_money_compression_signal"] = follow_through_state.get("signal")
                    candidate["_dead_money_compression_cap"] = float(follow_through_state.get("cap", 0.0) or 0.0)
                    candidate["_follow_through_risk_score"] = int(follow_through_state.get("risk_score", 0) or 0)
                    candidate["_follow_through_structure_votes"] = int(follow_through_state.get("structure_votes", 0) or 0)
                    candidate["_follow_through_memory_pressure"] = int(follow_through_state.get("memory_pressure", 0) or 0)
                    candidate["_follow_through_force_delayed"] = bool(follow_through_state.get("force_delayed", False))
                    candidate["_follow_through_scout_mode"] = bool(follow_through_state.get("scout_mode", False))
                    if follow_through_state.get("hard_block", False):
                        print("SKIP | follow_through_dead_money_hard_block | reason={} | signal={} | risk={} | sv={} | mem={} | score={:.3f} | pd={:.3f} | trend={:.3f} | delta1={:.3f} | win={:.3f} | pcount={} | {}".format(
                            reason,
                            str(follow_through_state.get("signal") or "follow_through_dead_money_hard_block"),
                            int(follow_through_state.get("risk_score", 0) or 0),
                            int(follow_through_state.get("structure_votes", 0) or 0),
                            int(follow_through_state.get("memory_pressure", 0) or 0),
                            float(follow_through_state.get("score", score_value) or score_value),
                            float(follow_through_state.get("density", candidate.get("pressure_density", 0.0)) or 0.0),
                            float(follow_through_state.get("trend", candidate.get("price_trend_strength", 0.0)) or 0.0),
                            float(follow_through_state.get("delta", abs(float(candidate.get("price_delta", 0.0) or 0.0))) or 0.0),
                            float(follow_through_state.get("window_delta", abs(float(candidate.get("price_delta_window", 0.0) or 0.0))) or 0.0),
                            int(follow_through_state.get("pressure_count", candidate.get("pressure_count", 0)) or 0),
                            (candidate.get("question") or "")[:110]
                        ))
                        candidate["skip_reason"] = "follow_through_dead_money_hard_block"
                        continue
                    if follow_through_state.get("active", False) or follow_through_state.get("force_delayed", False):
                        print("TRACE | follow_through_gate | signal={} | reason={} | risk={} | sv={} | mem={} | force_delayed={} | scout={} | cap={:.2f} | score={:.3f} | pd={:.3f} | trend={:.3f} | delta1={:.3f} | win={:.3f} | pcount={} | {}".format(
                            str(follow_through_state.get("signal") or "follow_through_compression"),
                            reason,
                            int(follow_through_state.get("risk_score", 0) or 0),
                            int(follow_through_state.get("structure_votes", 0) or 0),
                            int(follow_through_state.get("memory_pressure", 0) or 0),
                            int(bool(follow_through_state.get("force_delayed", False))),
                            int(bool(follow_through_state.get("scout_mode", False))),
                            float(follow_through_state.get("cap", 0.0) or 0.0),
                            float(follow_through_state.get("score", score_value) or score_value),
                            float(follow_through_state.get("density", candidate.get("pressure_density", 0.0)) or 0.0),
                            float(follow_through_state.get("trend", candidate.get("price_trend_strength", 0.0)) or 0.0),
                            float(follow_through_state.get("delta", abs(float(candidate.get("price_delta", 0.0) or 0.0))) or 0.0),
                            float(follow_through_state.get("window_delta", abs(float(candidate.get("price_delta_window", 0.0) or 0.0))) or 0.0),
                            int(follow_through_state.get("pressure_count", candidate.get("pressure_count", 0)) or 0),
                            (candidate.get("question") or "")[:110]
                        ))

                    thin_pressure_delay_needed = bool(thin_pressure_state.get("force_delayed", False))
                    follow_through_delay_needed = bool(follow_through_state.get("force_delayed", False))
                    relief_delay_needed = bool(candidate.get("_relief_escalation_force_delayed", False))
                    if should_delay_normal_score_entry(candidate, current_regime, reason) or follow_through_delay_needed or thin_pressure_delay_needed or relief_delay_needed:
                        allow_delayed, delayed_signal, delayed_meta = evaluate_delayed_entry(
                            candidate,
                            delayed_entry_watch,
                            delayed_entry_cooldown,
                            delayed_entry_memory,
                            now_ts
                        )
                        if not allow_delayed:
                            seen_cycles = int((delayed_meta or {}).get("seen_cycles", 0) or 0)
                            fail_count = int((delayed_meta or {}).get("memory_fail_count", 0) or 0)
                            confirm_count = int((delayed_meta or {}).get("memory_confirm_count", 0) or 0)
                            admit_count = int((delayed_meta or {}).get("memory_admit_count", 0) or 0)
                            light_success = int((delayed_meta or {}).get("memory_light_success_count", 0) or 0)
                            light_fail = int((delayed_meta or {}).get("memory_light_fail_count", 0) or 0)
                            structure_votes = int((delayed_meta or {}).get("structure_votes", 0) or 0)
                            improvement_votes = int((delayed_meta or {}).get("improvement_votes", 0) or 0)
                            promotion_score = int((delayed_meta or {}).get("promotion_score", 0) or 0)
                            stall_cycles = int((delayed_meta or {}).get("stall_cycles", 0) or 0)
                            escalation = should_escalate_delayed_family_review(
                                engine,
                                candidate,
                                delayed_meta,
                                delayed_entry_memory=delayed_entry_memory,
                                current_regime=current_regime,
                                reason=reason,
                            ) if delayed_signal == "delayed_entry_watch" else {"escalate": False}
                            if escalation.get("escalate"):
                                candidate["_family_force_review"] = True
                                candidate["_family_trigger_source"] = escalation.get("reason")
                                candidate["_family_review_mode"] = "family_swap_review"
                                candidate["_family_review_reason"] = escalation.get("reason")
                                candidate["_delayed_entry_signal"] = escalation.get("reason")
                                if candidate.get("_political_family_override"):
                                    candidate["_override_survival_corridor"] = True
                                    candidate["_political_hold_window"] = True
                                    candidate["_cross_family_thesis_priority"] = True
                                    candidate["_political_hold_window_cycles"] = 8 if (candidate.get("_political_targeted_override") or candidate.get("_balance_rescue_override")) else 6
                                    candidate["_cross_family_priority_cycles"] = 8 if candidate.get("_cross_family_thesis_priority") else 0
                                candidate["_delayed_entry_cycles"] = max(seen_cycles, 2)
                                candidate["_delayed_entry_promotion_score"] = int(escalation.get("promotion_score", promotion_score) or promotion_score)
                                candidate["_delayed_entry_structure_votes"] = int(escalation.get("structure_votes", structure_votes) or structure_votes)
                                candidate["_family_trigger_bonus"] = float(escalation.get("trigger_bonus", 0.0) or 0.0)
                                candidate["_family_outcome_bonus"] = float(escalation.get("outcome_bonus", 0.0) or 0.0)
                                candidate["_family_contender_bonus"] = round(float(escalation.get("trigger_bonus", 0.0) or 0.0) + float(escalation.get("outcome_bonus", 0.0) or 0.0), 4)
                                candidate["_family_attack_score"] = round(family_attack_score(candidate, reason, current_regime, engine=engine), 4)
                                print("ESCALATE | {} | seen={} | ps={} | sv={} | iv={} | trigger={:.3f} | outcome={:.3f} | weak_hold={:.3f} | weak_silent={} | weak_dead={} | family={} | {}".format(
                                    escalation.get("reason"),
                                    int(candidate.get("_delayed_entry_cycles", seen_cycles) or seen_cycles),
                                    int(candidate.get("_delayed_entry_promotion_score", promotion_score) or promotion_score),
                                    int(candidate.get("_delayed_entry_structure_votes", structure_votes) or structure_votes),
                                    improvement_votes,
                                    float(escalation.get("trigger_bonus", 0.0) or 0.0),
                                    float(escalation.get("outcome_bonus", 0.0) or 0.0),
                                    float(escalation.get("weak_hold", 0.0) or 0.0),
                                    int(escalation.get("weak_silent", 0) or 0),
                                    int(escalation.get("weak_dead", 0) or 0),
                                    (candidate.get("family_key") or "unknown")[:96],
                                    (candidate.get("question") or "")[:110]
                                ))
                            else:
                                print("SKIP | {} | reason={} | seen={} | fails={} | confirms={} | admits={} | ls={} | lf={} | sv={} | iv={} | ps={} | stall={} | score={:.3f} | pd={:.3f} | trend={:.3f} | win={:.3f} | {}".format(
                                    delayed_signal,
                                    reason,
                                    seen_cycles,
                                    fail_count,
                                    confirm_count,
                                    admit_count,
                                    light_success,
                                    light_fail,
                                    structure_votes,
                                    improvement_votes,
                                    promotion_score,
                                    stall_cycles,
                                    score_value,
                                    float(candidate.get("pressure_density", 0.0) or 0.0),
                                    float(candidate.get("price_trend_strength", 0.0) or 0.0),
                                    abs(float(candidate.get("price_delta_window", 0.0) or 0.0)),
                                    (candidate.get("question") or "")[:110]
                                ))
                                continue
                        candidate["_delayed_entry_confirmed"] = True
                        candidate["_delayed_entry_cycles"] = int((delayed_meta or {}).get("seen_cycles", 0) or 0)
                        candidate["_delayed_entry_signal"] = delayed_signal
                        candidate["_delayed_entry_structure_votes"] = int((delayed_meta or {}).get("structure_votes", 0) or 0)
                        candidate["_delayed_entry_promotion_score"] = int((delayed_meta or {}).get("promotion_score", 0) or 0)
                        candidate["_delayed_entry_light"] = delayed_signal == "delayed_admit_light"

                    family_guard_ok, family_guard_reason = family_winner_guard(
                        engine,
                        candidate,
                        reason,
                        current_regime,
                        delayed_entry_memory=delayed_entry_memory,
                    )
                    if not family_guard_ok:
                        print("SKIP | {} | reason={} | family={} | {}".format(
                            family_guard_reason,
                            reason,
                            (family_key or "unknown")[:96],
                            (candidate.get("question") or "")[:110]
                        ))
                        continue

                    selective_aggression = is_selective_aggression_candidate(candidate, reason, current_regime, engine=engine)
                    candidate["_selective_aggression"] = selective_aggression

                    if reason not in signal_memory:
                        signal_memory[reason] = {"seen": 0, "opened": 0}
                    signal_memory[reason]["seen"] += 1

                    high_conviction = is_high_conviction_reason(reason)
                    density_value = float(candidate.get("pressure_density", 0.0) or 0.0)
                    window_delta_value = abs(float(candidate.get("price_delta_window", 0.0) or 0.0))
                    trend_value = float(candidate.get("price_trend_strength", 0.0) or 0.0)
                    survival_priority = score_survival_priority(candidate, reason, engine)
                    cluster_exposure_now = float(engine.cluster_exposure().get(cluster, 0.0) or 0.0)
                    theme_exposure_now = float(engine.theme_exposure().get(theme, 0.0) or 0.0)
                    politics_state = politics_concentration_state(engine, candidate)
                    politics_total_exposure = float(politics_state.get("total_exposure", 0.0) or 0.0)
                    politics_cluster_exposure = float(politics_state.get("cluster_exposure", 0.0) or 0.0)
                    politics_family_exposure = float(politics_state.get("family_exposure", 0.0) or 0.0)

                    family_review_mode = str(candidate.get("_family_review_mode") or "")
                    force_family_review = bool(candidate.get("_family_force_review", False))
                    family_swapish = family_review_mode in {"swap", "swap_review", "family_swap_review", "review", "peer_review"} or force_family_review

                    targeted_override = bool(candidate.get("_political_targeted_override", False))
                    political_override = bool(candidate.get("_political_family_override", False))
                    override_strength = float(candidate.get("_political_override_strength", 0.0) or 0.0)
                    delayed_memory_active = bool(candidate.get("_delayed_entry_memory_active", False))
                    survival_corridor = (
                        (targeted_override and override_strength >= 0.12) or
                        (political_override and override_strength >= 0.18)
                    ) and (
                        family_swapish or delayed_memory_active
                    )
                    candidate["_override_survival_corridor"] = bool(survival_corridor)

                    if cluster_exposure_now > 4.5 and not selective_aggression and not family_swapish:
                        print("SKIP | cluster_overload | {} exposure={:.2f} | reason={} | {}".format(
                            cluster,
                            cluster_exposure_now,
                            reason,
                            (candidate.get("question") or "")[:110]
                        ))
                        continue

                    if cluster_exposure_now > 3.5 and not high_conviction and not selective_aggression and signal_confidence(signal_memory, reason) < 1.0 and not family_swapish:
                        print("SKIP | weak_signal_in_hot_cluster | {} exposure={:.2f} | reason={} | {}".format(
                            cluster,
                            cluster_exposure_now,
                            reason,
                            (candidate.get("question") or "")[:110]
                        ))
                        continue

                    if cluster_exposure_now >= 5.0 and not high_conviction and not selective_aggression and not family_swapish:
                        continue
                    if cluster_exposure_now >= 4.2 and reason == "score" and survival_priority < 1.28 and not selective_aggression and not family_swapish:
                        continue
                    if theme_exposure_now >= 4.2 and reason == "score" and survival_priority < 1.18 and not selective_aggression and not family_swapish:
                        continue

                    rebalance_state = concentration_rebalance_state(engine, candidate)
                    dominant_family_exp = float(rebalance_state.get("family_exposure", 0.0) or 0.0)
                    dominant_cluster_exp = float(rebalance_state.get("cluster_exposure", 0.0) or 0.0)
                    dominant_theme_exp = float(rebalance_state.get("theme_exposure", 0.0) or 0.0)
                    family_review_mode = str(candidate.get("_family_review_mode") or "")
                    if rebalance_state.get("dominant"):
                        if dominant_family_exp >= 4.0 and reason == "score" and family_review_mode not in {"swap", "swap_review", "family_swap_review", "review"} and not force_family_review and not survival_corridor and survival_priority < 1.34:
                            print("SKIP | family_concentration_rebalance | family_exp={:.2f} | reason={} | {}".format(
                                dominant_family_exp, reason, (candidate.get("question") or "")[:110]
                            ))
                            continue
                        if dominant_cluster_exp >= 4.5 and reason == "score" and not selective_aggression and family_review_mode not in {"swap", "swap_review", "family_swap_review"} and not force_family_review and not survival_corridor:
                            print("SKIP | cluster_concentration_rebalance | cluster={} | exposure={:.2f} | reason={} | {}".format(
                                cluster, dominant_cluster_exp, reason, (candidate.get("question") or "")[:110]
                            ))
                            continue
                        if dominant_theme_exp >= 5.0 and reason == "score" and not high_conviction and family_review_mode not in {"swap", "swap_review", "family_swap_review"} and not force_family_review and not survival_corridor:
                            print("SKIP | theme_concentration_rebalance | theme={} | exposure={:.2f} | reason={} | {}".format(
                                theme, dominant_theme_exp, reason, (candidate.get("question") or "")[:110]
                            ))
                            continue
                        if survival_corridor and reason == "score":
                            print("INFO | override_survival_corridor | family={} | strength={:.3f} | family_exp={:.2f} | cluster_exp={:.2f} | {}".format(
                                (family_key or "unknown")[:96],
                                override_strength,
                                dominant_family_exp,
                                dominant_cluster_exp,
                                (candidate.get("question") or "")[:110]
                            ))

                    if politics_state.get("is_politics_like"):
                        if politics_family_exposure >= 2.8 and not high_conviction and not selective_aggression and not (survival_corridor and politics_family_exposure < 3.6):
                            print("SKIP | politics_family_guard | family_exp={:.2f} | reason={} | {}".format(
                                politics_family_exposure,
                                reason,
                                (candidate.get("question") or "")[:110]
                            ))
                            continue

                        if politics_cluster_exposure >= 3.4 and reason == "score" and not high_conviction and not selective_aggression and not (survival_corridor and politics_cluster_exposure < 4.2):
                            print("SKIP | politics_cluster_guard | cluster={} | exposure={:.2f} | reason={} | {}".format(
                                cluster,
                                politics_cluster_exposure,
                                reason,
                                (candidate.get("question") or "")[:110]
                            ))
                            continue

                        if politics_total_exposure >= 4.8 and reason == "score" and survival_priority < 1.22 and not selective_aggression and not (survival_corridor and politics_total_exposure < 5.6):
                            print("SKIP | politics_concentration_guard | exposure={:.2f} | reason={} | {}".format(
                                politics_total_exposure,
                                reason,
                                (candidate.get("question") or "")[:110]
                            ))
                            continue

                        if int(politics_state.get("open_count", 0) or 0) >= 4 and not high_conviction and signal_confidence(signal_memory, reason) < 1.0 and not selective_aggression and not (survival_corridor and int(politics_state.get("open_count", 0) or 0) <= 5):
                            print("SKIP | politics_stack_guard | exposure={:.2f} | reason={} | {}".format(
                                politics_total_exposure,
                                reason,
                                (candidate.get("question") or "")[:110]
                            ))
                            continue

                    if book_pressure["hard_crowded"]:
                        if reason not in {"score+pressure", "score+momentum", "multicycle_momentum_override", "momentum_override", "score+pre_momentum"}:
                            continue
                        if survival_priority < 1.18:
                            continue
                        if not high_conviction and (density_value < 0.35 and window_delta_value < 0.006 and trend_value < 0.85):
                            continue
                    elif book_pressure["stressed"]:
                        if reason == "score" and survival_priority < 1.10 and not selective_aggression:
                            continue
                        if not high_conviction and not selective_aggression and density_value < 0.22 and window_delta_value < 0.003 and trend_value < 0.78:
                            continue
                    elif book_pressure["crowded"]:
                        if reason == "score" and survival_priority < 1.00 and not selective_aggression:
                            continue
                        if not high_conviction and not selective_aggression and density_value < 0.18 and window_delta_value < 0.0025 and trend_value < 0.72:
                            continue

                    if not high_conviction:
                        theme_limit = MAX_THEME_POSITIONS_PER_CYCLE + (1 if candidate.get("_universe_source") == "explorer" and selective_aggression else 0)
                        cluster_limit = MAX_CLUSTER_POSITIONS_PER_CYCLE + (1 if candidate.get("_universe_source") == "explorer" and selective_aggression else 0)
                        if cycle_theme_counts.get(theme, 0) >= theme_limit:
                            continue

                        if cycle_cluster_counts.get(cluster, 0) >= cluster_limit:
                            continue

                    cluster_pos_count = engine.cluster_position_count(cluster) if hasattr(engine, "cluster_position_count") else 0
                    if cluster in {"legal_cases", "sports_awards", "crypto_launch"} and cluster_pos_count >= 2 and not engine.has_open_position(key):
                        if selective_aggression:
                            cluster_replacement = engine.find_cluster_replacement_for_candidate(
                                candidate,
                                current_regime=current_regime,
                                incoming_reason=reason,
                                cluster_filter=cluster
                            ) if hasattr(engine, "find_cluster_replacement_for_candidate") else None
                            if cluster_replacement:
                                closed_event = engine.close_position(
                                    cluster_replacement["position_key"],
                                    "cluster_conflict_rotation_exit",
                                    now_ts=time.time()
                                )
                                if closed_event:
                                    closed_event.update({
                                        "capital_signal": "cluster_slot_upgrade",
                                        "hold_priority": cluster_replacement.get("hold_score", 0.0),
                                        "cluster_heat": cluster_replacement.get("cluster_heat", 0.0),
                                        "family_heat": cluster_replacement.get("family_heat", 0.0),
                                        "incoming_edge": cluster_replacement.get("incoming_edge", 0.0),
                                        "hold_gap": cluster_replacement.get("hold_gap", 0.0),
                                        "replace_for": (candidate.get("question") or "")[:80],
                                        "type_bias_signal": "family_cluster_conflict_profile",
                                    })
                                    log_lifecycle_event(closed_event, current_regime)
                        elif reason == "score" and not family_swapish:
                            print("SKIP | family_cluster_conflict | cluster={} | reason={} | {}".format(
                                cluster,
                                reason,
                                (candidate.get("question") or "")[:110]
                            ))
                            continue

                    candidate["_entry_reason"] = reason

                    confidence = signal_confidence(signal_memory, reason)
                    if book_pressure["hard_crowded"]:
                        confidence *= 0.82
                    elif book_pressure["stressed"]:
                        confidence *= 0.90
                    elif book_pressure["crowded"]:
                        confidence *= 0.96
                    if selective_aggression:
                        confidence *= 1.06

                    delayed_light_entry = bool(candidate.get("_delayed_entry_light", False))
                    if delayed_light_entry:
                        confidence *= 0.97

                    stake, stake_meta = adaptive_stake_plan(
                        candidate,
                        reason,
                        engine,
                        cycle_theme_counts,
                        cycle_cluster_counts,
                        current_regime,
                        confidence
                    )
                    if selective_aggression and not delayed_light_entry:
                        stake *= 1.08
                    delayed_admission_mult = 1.0
                    if delayed_light_entry:
                        delayed_admission_mult = 0.64
                        stake *= delayed_admission_mult
                    stake = round(max(0.5, min(stake * STAKE_MULTIPLIER, 4.60)), 2)
                    stake_meta["delayed_entry_mode"] = "light" if delayed_light_entry else ("confirmed" if candidate.get("_delayed_entry_confirmed") else "none")
                    stake_meta["delayed_admission_mult"] = delayed_admission_mult
                    stake_meta["delayed_entry_signal"] = candidate.get("_delayed_entry_signal")
                    stake_meta["family_review_mode"] = candidate.get("_family_review_mode")
                    stake_meta["family_trigger_source"] = candidate.get("_family_trigger_source")
                    stake_meta["family_outcome_bonus"] = float(candidate.get("_family_outcome_bonus", 0.0) or 0.0)
                    stake_meta["family_trigger_bonus"] = float(candidate.get("_family_trigger_bonus", 0.0) or 0.0)
                    stake_meta["family_rebalance_penalty"] = float(candidate.get("_family_rebalance_penalty", 0.0) or 0.0)
                    stake_meta["political_override_entry"] = bool(candidate.get("_political_override_applied", False))
                    stake_meta["political_override_active"] = bool(candidate.get("_political_family_override", False))
                    stake_meta["political_targeted_override"] = bool(candidate.get("_political_targeted_override", False))
                    stake_meta["balance_rescue_override"] = bool(candidate.get("_balance_rescue_override", False))
                    stake_meta["cross_family_thesis_priority"] = bool(candidate.get("_cross_family_thesis_priority", False))
                    stake_meta["cross_family_priority_cycles"] = int(candidate.get("_cross_family_priority_cycles", 0) or 0)
                    stake_meta["political_hold_window"] = bool(candidate.get("_political_hold_window", False))
                    stake_meta["political_hold_window_cycles"] = int(candidate.get("_political_hold_window_cycles", 0) or 0)
                    stake_meta["political_override_reason"] = candidate.get("_political_override_reason")
                    stake_meta["flag_mirror_audit_expected"] = bool(candidate.get("_flag_mirror_audit_expected", False))
                    stake_meta["override_survival_corridor"] = bool(candidate.get("_override_survival_corridor", False))
                    stake_meta["political_rescue_scout_demotion"] = bool(candidate.get("_political_rescue_scout_demotion", False))
                    stake_meta["political_rescue_scout_cap"] = float(candidate.get("_political_rescue_scout_cap", 0.0) or 0.0)
                    stake_meta["narrative_full_size_brake"] = bool(candidate.get("_narrative_full_size_brake", False))
                    stake_meta["narrative_full_size_brake_cap"] = float(candidate.get("_narrative_full_size_brake_cap", 0.0) or 0.0)
                    stake_meta["weak_sports_override_brake"] = bool(candidate.get("_weak_sports_override_brake", False))
                    stake_meta["weak_sports_override_brake_cap"] = float(candidate.get("_weak_sports_override_brake_cap", 0.0) or 0.0)
                    stake_meta["dead_money_compression_active"] = bool(candidate.get("_dead_money_compression_active", False))
                    stake_meta["dead_money_compression_signal"] = candidate.get("_dead_money_compression_signal")
                    stake_meta["dead_money_compression_cap"] = float(candidate.get("_dead_money_compression_cap", 0.0) or 0.0)
                    stake_meta["follow_through_risk_score"] = int(candidate.get("_follow_through_risk_score", 0) or 0)
                    stake_meta["follow_through_structure_votes"] = int(candidate.get("_follow_through_structure_votes", 0) or 0)
                    stake_meta["follow_through_memory_pressure"] = int(candidate.get("_follow_through_memory_pressure", 0) or 0)
                    stake_meta["follow_through_force_delayed"] = bool(candidate.get("_follow_through_force_delayed", False))
                    stake_meta["follow_through_scout_mode"] = bool(candidate.get("_follow_through_scout_mode", False))
                    stake_meta["thin_pressure_truth_active"] = bool(candidate.get("_thin_pressure_truth_active", False))
                    stake_meta["thin_pressure_truth_signal"] = candidate.get("_thin_pressure_truth_signal")
                    stake_meta["thin_pressure_truth_cap"] = float(candidate.get("_thin_pressure_truth_cap", 0.0) or 0.0)
                    stake_meta["thin_pressure_truth_risk"] = int(candidate.get("_thin_pressure_truth_risk", 0) or 0)
                    stake_meta["thin_pressure_truth_force_delayed"] = bool(candidate.get("_thin_pressure_truth_force_delayed", False))
                    stake_meta["elite_recovery_override"] = bool(candidate.get("_elite_recovery_override", False))
                    stake_meta["elite_recovery_signal"] = candidate.get("_elite_recovery_signal")
                    stake_meta["elite_recovery_clamp_active"] = bool(candidate.get("_elite_recovery_clamp_active", False))
                    stake_meta["elite_recovery_clamp_cap"] = float(candidate.get("_elite_recovery_clamp_cap", 0.0) or 0.0)
                    stake_meta["elite_recovery_force_delayed"] = bool(candidate.get("_elite_recovery_force_delayed", False))
                    stake_meta["elite_recovery_micro_scout"] = bool(candidate.get("_elite_recovery_micro_scout", False))
                    stake_meta["relief_escalation_active"] = bool(candidate.get("_relief_escalation_active", False))
                    stake_meta["relief_escalation_signal"] = candidate.get("_relief_escalation_signal")
                    stake_meta["relief_escalation_cap"] = float(candidate.get("_relief_escalation_cap", 0.0) or 0.0)
                    stake_meta["relief_escalation_force_delayed"] = bool(candidate.get("_relief_escalation_force_delayed", False))
                    stake_meta["relief_escalation_micro_scout"] = bool(candidate.get("_relief_escalation_micro_scout", False))
                    stake_meta["adaptive_calm_relief_active"] = bool(candidate.get("_adaptive_calm_relief_active", False))
                    stake_meta["adaptive_calm_relief_signal"] = candidate.get("_adaptive_calm_relief_signal")
                    stake_meta["adaptive_calm_relief_cap"] = float(candidate.get("_adaptive_calm_relief_cap", 0.0) or 0.0)
                    stake_meta["adaptive_calm_relief_force_delayed"] = bool(candidate.get("_adaptive_calm_relief_force_delayed", False))
                    stake_meta["adaptive_calm_relief_micro_scout"] = bool(candidate.get("_adaptive_calm_relief_micro_scout", False))
                    stake_meta["adaptive_budget_active"] = bool(candidate.get("_adaptive_budget_active", False))
                    stake_meta["adaptive_budget_signal"] = candidate.get("_adaptive_budget_signal")
                    stake_meta["adaptive_budget_cap"] = float(candidate.get("_adaptive_budget_cap", 0.0) or 0.0)
                    stake_meta["adaptive_budget_force_delayed"] = bool(candidate.get("_adaptive_budget_force_delayed", False))
                    stake_meta["adaptive_budget_micro_scout"] = bool(candidate.get("_adaptive_budget_micro_scout", False))
                    stake_meta["after_regime_cap_stake"] = stake
                    stake_meta["edge_registry_version"] = EDGE_REGISTRY_VERSION
                    stake_meta["stake"] = round(float(stake), 4)

                    try:
                        scout_cap = float(candidate.get("_political_rescue_scout_cap", 0.0) or 0.0)
                        if bool(candidate.get("_political_rescue_scout_demotion", False)) and scout_cap > 0:
                            original_stake = float(stake_meta.get("stake", 0.0) or 0.0)
                            if scout_cap < original_stake:
                                stake_meta["stake"] = round(scout_cap, 4)
                                print(
                                    "TRACE | political_rescue_scout_stake | old_stake={:.2f} | new_stake={:.2f} | signal={} | reason={} | market_type={} | strength={:.3f} | {}".format(
                                        original_stake,
                                        scout_cap,
                                        str(candidate.get("_political_override_reason") or "political_rescue_scout"),
                                        str(candidate.get("reason", "") or ""),
                                        str(candidate.get("market_type", "general_binary") or "general_binary"),
                                        float(candidate.get("_political_override_strength", 0.0) or 0.0),
                                        (candidate.get("question") or "")[:72],
                                    )
                                )
                    except Exception:
                        pass

                    try:
                        truth_cap = float(candidate.get("_thin_pressure_truth_cap", 0.0) or 0.0)
                        if bool(candidate.get("_thin_pressure_truth_active", False)) and truth_cap <= 0 and bool(candidate.get("_thin_pressure_truth_force_delayed", False)):
                            truth_cap = execution_micro_clamp_cap(candidate, "thin_truth_force_delayed")
                        if bool(candidate.get("_thin_pressure_truth_active", False)) and truth_cap > 0:
                            original_stake = float(stake_meta.get("stake", 0.0) or 0.0)
                            if truth_cap < original_stake:
                                stake_meta["stake"] = round(truth_cap, 4)
                                print(
                                    "TRACE | thin_pressure_truth_stake | old_stake={:.2f} | new_stake={:.2f} | signal={} | risk={} | reason={} | market_type={} | {}".format(
                                        original_stake,
                                        truth_cap,
                                        str(candidate.get("_thin_pressure_truth_signal") or "thin_pressure_truth_scout_cap"),
                                        int(candidate.get("_thin_pressure_truth_risk", 0) or 0),
                                        str(candidate.get("reason", "") or ""),
                                        str(candidate.get("market_type", "general_binary") or "general_binary"),
                                        (candidate.get("question") or "")[:72],
                                    )
                                )
                    except Exception:
                        pass

                    try:
                        recovery_cap = float(candidate.get("_elite_recovery_clamp_cap", 0.0) or 0.0)
                        if bool(candidate.get("_elite_recovery_clamp_active", False)) and recovery_cap > 0:
                            original_stake = float(stake_meta.get("stake", 0.0) or 0.0)
                            if recovery_cap < original_stake:
                                stake_meta["stake"] = round(recovery_cap, 4)
                                print(
                                    "TRACE | elite_recovery_clamp | old_stake={:.2f} | new_stake={:.2f} | signal={} | force_delayed={} | reason={} | market_type={} | {}".format(
                                        original_stake,
                                        recovery_cap,
                                        str(candidate.get("_elite_recovery_signal") or "elite_recovery_override"),
                                        int(bool(candidate.get("_elite_recovery_force_delayed", False))),
                                        str(candidate.get("reason", "") or ""),
                                        str(candidate.get("market_type", "general_binary") or "general_binary"),
                                        (candidate.get("question") or "")[:72],
                                    )
                                )
                    except Exception:
                        pass

                    try:
                        relief_cap = float(candidate.get("_relief_escalation_cap", 0.0) or 0.0)
                        if bool(candidate.get("_relief_escalation_active", False)) and relief_cap > 0:
                            original_stake = float(stake_meta.get("stake", 0.0) or 0.0)
                            if relief_cap < original_stake:
                                stake_meta["stake"] = round(relief_cap, 4)
                                print(
                                    "TRACE | relief_escalation_clamp | old_stake={:.2f} | new_stake={:.2f} | signal={} | force_delayed={} | reason={} | market_type={} | {}".format(
                                        original_stake,
                                        relief_cap,
                                        str(candidate.get("_relief_escalation_signal") or "relief_escalation"),
                                        int(bool(candidate.get("_relief_escalation_force_delayed", False))),
                                        str(candidate.get("reason", "") or ""),
                                        str(candidate.get("market_type", "general_binary") or "general_binary"),
                                        (candidate.get("question") or "")[:72],
                                    )
                                )
                    except Exception:
                        pass

                    try:
                        effective_stake = float(stake_meta.get("stake", stake) or stake)
                        scout_active = bool(
                            candidate.get("_political_rescue_scout_demotion", False)
                            or candidate.get("_thin_pressure_truth_active", False)
                            or candidate.get("_elite_recovery_clamp_active", False)
                            or candidate.get("_relief_escalation_active", False)
                            or candidate.get("_follow_through_force_delayed", False)
                        )
                        scout_cap_value = 0.0
                        scout_signal = "none"
                        if bool(candidate.get("_political_rescue_scout_demotion", False)):
                            scout_cap_value = float(candidate.get("_political_rescue_scout_cap", 0.0) or 0.0)
                            scout_signal = str(candidate.get("_political_override_reason") or "political_rescue_scout")
                        elif bool(candidate.get("_elite_recovery_clamp_active", False)):
                            scout_cap_value = float(candidate.get("_elite_recovery_clamp_cap", 0.0) or 0.0)
                            scout_signal = str(candidate.get("_elite_recovery_signal") or "elite_recovery_override")
                        elif bool(candidate.get("_thin_pressure_truth_active", False)):
                            scout_cap_value = float(candidate.get("_thin_pressure_truth_cap", 0.0) or 0.0)
                            if scout_cap_value <= 0 and bool(candidate.get("_thin_pressure_truth_force_delayed", False)):
                                scout_cap_value = execution_micro_clamp_cap(candidate, "thin_truth_force_delayed")
                            scout_signal = str(candidate.get("_thin_pressure_truth_signal") or "thin_pressure_truth_scout_cap")
                        elif bool(candidate.get("_relief_escalation_active", False)):
                            scout_cap_value = float(candidate.get("_relief_escalation_cap", 0.0) or 0.0)
                            if scout_cap_value <= 0 and bool(candidate.get("_relief_escalation_force_delayed", False)):
                                scout_cap_value = execution_micro_clamp_cap(candidate, "relief_escalation")
                            scout_signal = str(candidate.get("_relief_escalation_signal") or "relief_escalation")
                        elif bool(candidate.get("_follow_through_force_delayed", False)):
                            scout_cap_value = execution_micro_clamp_cap(candidate, "follow_through_force_delayed")
                            scout_signal = str(candidate.get("_dead_money_compression_signal") or "follow_through_force_delayed")
                        if scout_active:
                            print(
                                "TRACE | scout_demotion_wiring_audit | stage=pre_open | active=1 | signal={} | cap={:.2f} | stake_before={:.2f} | stake_after={:.2f} | hold_cycles={} | cross_cycles={} | reason={} | market_type={} | {}".format(
                                    scout_signal,
                                    scout_cap_value,
                                    float(stake),
                                    effective_stake,
                                    int(candidate.get("_political_hold_window_cycles", 0) or 0),
                                    int(candidate.get("_cross_family_priority_cycles", 0) or 0),
                                    reason,
                                    str(candidate.get("market_type", "general_binary") or "general_binary"),
                                    (candidate.get("question") or "")[:72],
                                )
                            )
                        else:
                            print(
                                "TRACE | scout_demotion_wiring_audit | stage=pre_open | active=0 | stake_before={:.2f} | stake_after={:.2f} | reason={} | market_type={} | {}".format(
                                    float(stake),
                                    effective_stake,
                                    reason,
                                    str(candidate.get("market_type", "general_binary") or "general_binary"),
                                    (candidate.get("question") or "")[:72],
                                )
                            )
                        if bool(candidate.get("_narrative_full_size_brake", False)):
                            brake_cap = float(candidate.get("_narrative_full_size_brake_cap", 0.0) or 0.0)
                            before_brake = float(effective_stake)
                            if brake_cap > 0 and brake_cap < effective_stake:
                                effective_stake = brake_cap
                            print(
                                "TRACE | narrative_full_size_brake_wiring | active=1 | cap={:.2f} | stake_before={:.2f} | stake_after={:.2f} | reason={} | market_type={} | {}".format(
                                    brake_cap,
                                    before_brake,
                                    effective_stake,
                                    reason,
                                    str(candidate.get("market_type", "general_binary") or "general_binary"),
                                    (candidate.get("question") or "")[:72],
                                )
                            )
                        if bool(candidate.get("_weak_sports_override_brake", False)):
                            sports_cap = float(candidate.get("_weak_sports_override_brake_cap", 0.0) or 0.0)
                            before_sports_brake = float(effective_stake)
                            if sports_cap > 0 and sports_cap < effective_stake:
                                effective_stake = sports_cap
                            print(
                                "TRACE | weak_sports_override_brake_wiring | active=1 | cap={:.2f} | stake_before={:.2f} | stake_after={:.2f} | reason={} | market_type={} | {}".format(
                                    sports_cap,
                                    before_sports_brake,
                                    effective_stake,
                                    reason,
                                    str(candidate.get("market_type", "general_binary") or "general_binary"),
                                    (candidate.get("question") or "")[:72],
                                )
                            )
                        if bool(candidate.get("_dead_money_compression_active", False)):
                            follow_cap = float(candidate.get("_dead_money_compression_cap", 0.0) or 0.0)
                            if follow_cap <= 0 and bool(candidate.get("_follow_through_force_delayed", False)):
                                follow_cap = execution_micro_clamp_cap(candidate, "follow_through_force_delayed")
                            before_follow_cap = float(effective_stake)
                            if follow_cap > 0 and follow_cap < effective_stake:
                                effective_stake = follow_cap
                            print(
                                "TRACE | follow_through_stake_compression | active=1 | signal={} | risk={} | sv={} | mem={} | cap={:.2f} | stake_before={:.2f} | stake_after={:.2f} | reason={} | market_type={} | {}".format(
                                    str(candidate.get("_dead_money_compression_signal") or "follow_through_compression"),
                                    int(candidate.get("_follow_through_risk_score", 0) or 0),
                                    int(candidate.get("_follow_through_structure_votes", 0) or 0),
                                    int(candidate.get("_follow_through_memory_pressure", 0) or 0),
                                    follow_cap,
                                    before_follow_cap,
                                    effective_stake,
                                    reason,
                                    str(candidate.get("market_type", "general_binary") or "general_binary"),
                                    (candidate.get("question") or "")[:72],
                                )
                            )
                        stake = round(max(0.5, effective_stake), 2)
                        stake_meta["stake"] = round(stake, 4)
                    except Exception:
                        pass
                    # v21.2.3: keep weird/legal pressure entries from overweighting
                    try:
                        entry_reason = str(candidate.get("reason", "") or "")
                        candidate_theme = str(candidate.get("theme", "") or "")
                        candidate_cluster = str(candidate.get("cluster", "") or "")
                        pressure_count = int(candidate.get("pressure_count", 0) or 0)
                        if pressure_count >= 2 and entry_reason in {"score+pressure", "pressure"} and (
                            candidate_theme in {"weird", "general"} or candidate_cluster == "legal_cases"
                        ):
                            original_stake = float(stake_meta.get("stake", 0.0) or 0.0)
                            capped_stake = min(original_stake, 3.25)
                            if capped_stake < original_stake:
                                stake_meta["stake"] = round(capped_stake, 4)
                                print(
                                    "TRACE | pressure_risk_cap | theme={} | cluster={} | pressure_count={} | old_stake={:.2f} | new_stake={:.2f} | {}".format(
                                        candidate_theme,
                                        candidate_cluster,
                                        pressure_count,
                                        original_stake,
                                        capped_stake,
                                        (candidate.get("question") or "")[:64],
                                    )
                                )
                    except Exception:
                        pass

                    # v21.2.4: stake concentration guard
                    try:
                        cycle_cap = float(regime_cfg.get("cycle_risk_usd", 0.0) or 0.0)
                        original_stake = float(stake_meta.get("stake", 0.0) or 0.0)
                        max_single_share = 0.38
                        max_single_stake = cycle_cap * max_single_share if cycle_cap > 0 else 0.0
                        entry_reason = str(candidate.get("reason", "") or "")
                        if entry_reason in {"multicycle_momentum_override", "momentum_override", "score+momentum"}:
                            max_single_stake = min(max_single_stake, 4.00) if max_single_stake > 0 else 4.00
                        if max_single_stake > 0 and original_stake > max_single_stake:
                            stake_meta["stake"] = round(max_single_stake, 4)
                            print(
                                "TRACE | stake_concentration_cap | cycle_cap={:.2f} | share_cap={:.2f} | old_stake={:.2f} | new_stake={:.2f} | reason={} | theme={} | cluster={} | {}".format(
                                    cycle_cap,
                                    max_single_share,
                                    original_stake,
                                    max_single_stake,
                                    entry_reason,
                                    str(candidate.get("theme", "") or ""),
                                    str(candidate.get("cluster", "") or ""),
                                    (candidate.get("question") or "")[:72],
                                )
                            )
                    except Exception:
                        pass

                    try:
                        final_effective_stake = float(stake_meta.get("stake", stake) or stake)
                        print(
                            "TRACE | scout_demotion_wiring_audit | stage=post_caps | active={} | narrative_brake={} | sports_override_brake={} | stake_before={:.2f} | stake_after={:.2f} | reason={} | market_type={} | {}".format(
                                int(bool(
                                    candidate.get("_political_rescue_scout_demotion", False)
                                    or candidate.get("_thin_pressure_truth_active", False)
                                    or candidate.get("_elite_recovery_clamp_active", False)
                                    or candidate.get("_follow_through_force_delayed", False)
                                    or candidate.get("_adaptive_calm_relief_active", False)
                                )),
                                int(bool(candidate.get("_narrative_full_size_brake", False))),
                                int(bool(candidate.get("_weak_sports_override_brake", False))),
                                float(stake),
                                final_effective_stake,
                                reason,
                                str(candidate.get("market_type", "general_binary") or "general_binary"),
                                (candidate.get("question") or "")[:72],
                            )
                        )
                        stake = round(max(0.5, final_effective_stake), 2)
                        stake_meta["stake"] = round(stake, 4)
                    except Exception:
                        pass

                    candidate["_stake_model"] = dict(stake_meta)
                    candidate = apply_preopen_political_synthesis(candidate)

                    if bool(candidate.get("political_override_active")) or bool(candidate.get("flag_mirror_audit_expected")):
                        print(
                            "TRACE | candidate_snapshot_aligned | question={} | hold={} | hold_cycles={} | cross_cycles={} | corridor={} | mirror_expected={} | registry={} | detector={}".format(
                                (candidate.get("question") or "")[:72],
                                int(bool(candidate.get("political_hold_window"))),
                                int(candidate.get("political_hold_window_cycles", 0) or 0),
                                int(candidate.get("cross_family_priority_cycles", 0) or 0),
                                int(bool(candidate.get("override_survival_corridor"))),
                                int(bool(candidate.get("flag_mirror_audit_expected"))),
                                candidate.get("edge_registry_version", EDGE_REGISTRY_VERSION),
                                candidate.get("regime_detector_version", REGIME_DETECTOR_VERSION),
                            )
                        )

                    family_key = candidate.get("family_key")
                    if family_key and engine.has_open_family(family_key):
                        family_review_mode = candidate.get("_family_review_mode")
                        family_review_target = candidate.get("_family_review_target")
                        family_review_gap = float(candidate.get("_family_review_gap", 0.0) or 0.0)
                        family_replacement = None

                        aggressive_family_review = family_review_mode in {"swap", "review", "peer_review", "swap_review", "family_swap_review"} and (
                            bool(candidate.get("_delayed_entry_confirmed", False)) or
                            bool(candidate.get("_delayed_entry_light", False)) or
                            str(candidate.get("_delayed_entry_signal") or "") in {"delayed_entry_promoted", "delayed_entry_confirmed", "delayed_admit_light"} or
                            float(candidate.get("_family_contender_bonus", 0.0) or 0.0) >= 0.10
                        )

                        if aggressive_family_review or should_attempt_family_replacement(engine, candidate, reason, current_regime):
                            family_replacement = engine.find_family_replacement_for_candidate(
                                candidate,
                                current_regime=current_regime,
                                incoming_reason=reason
                            )

                        if family_replacement:
                            target_key = family_review_target or family_replacement["position_key"]
                            closed_event = engine.close_position(
                                target_key,
                                "family_rotation_exit",
                                now_ts=time.time()
                            )
                            if closed_event:
                                closed_event.update({
                                    "capital_signal": "family_slot_upgrade",
                                    "hold_priority": family_replacement.get("hold_score", 0.0),
                                    "cluster_heat": family_replacement.get("cluster_heat", 0.0),
                                    "price_progress": family_replacement.get("price_progress", 0.0),
                                    "peak_pnl_pct": family_replacement.get("peak_pnl_pct", 0.0),
                                    "incoming_edge": family_replacement.get("incoming_edge", 0.0),
                                    "hold_gap": family_replacement.get("hold_gap", 0.0),
                                    "family_review_mode": family_review_mode,
                                    "family_review_gap": family_review_gap,
                                    "family_outcome_bonus": float(candidate.get("_family_outcome_bonus", 0.0) or 0.0),
                                    "family_trigger_bonus": float(candidate.get("_family_trigger_bonus", 0.0) or 0.0),
                                    "family_rebalance_penalty": float(candidate.get("_family_rebalance_penalty", 0.0) or 0.0),
                                    "replace_for": (candidate.get("question") or "")[:80],
                                    "type_bias_signal": "family_competition_profile",
                                })
                                log_lifecycle_event(closed_event, current_regime)
                        else:
                            if family_review_mode in {"review", "peer_review"}:
                                print("SKIP | family_promotion_review | family={} | gap={:.3f} | reason={} | {}".format(
                                    (family_key or "unknown")[:96],
                                    family_review_gap,
                                    reason,
                                    (candidate.get("question") or "")[:110]
                                ))
                            continue

                    hot_slot_ok, hot_slot_reason = hot_slot_discipline_gate(engine, candidate, reason, current_regime)
                    if not hot_slot_ok:
                        candidate["skip_reason"] = hot_slot_reason
                        continue

                    warmup_slot_block, warmup_slot_reason = warmup_slot_cap_gate(engine, candidate, reason, current_regime, opened_now)
                    if warmup_slot_block:
                        candidate["skip_reason"] = warmup_slot_reason
                        continue

                    hard_slot_block, hard_slot_reason = hard_slot_cap_gate(engine, candidate, reason, current_regime)
                    if hard_slot_block:
                        candidate["skip_reason"] = hard_slot_reason
                        continue

                    competition_gate_blocked, competition_gate_reason = should_enforce_competition_gate_block(engine, candidate, reason, current_regime)
                    if competition_gate_blocked:
                        candidate["skip_reason"] = competition_gate_reason
                        continue

                    if cycle_spend + stake > MAX_CYCLE_RISK_USD:
                        continue

                    if should_attempt_competitive_replacement(engine, candidate, reason, current_regime):
                        replacement_event = engine.find_recyclable_position_for_candidate(
                            candidate,
                            current_regime=current_regime,
                            incoming_reason=reason
                        )
                        if replacement_event:
                            closed_event = engine.close_position(
                                replacement_event["position_key"],
                                "competitive_rotation_exit",
                                now_ts=time.time()
                            )
                            if closed_event:
                                closed_event.update({
                                    "capital_signal": "incoming_superior",
                                    "hold_priority": replacement_event.get("hold_score", 0.0),
                                    "cluster_heat": replacement_event.get("cluster_heat", 0.0),
                                    "price_progress": replacement_event.get("price_progress", 0.0),
                                    "peak_pnl_pct": replacement_event.get("peak_pnl_pct", 0.0),
                                    "incoming_edge": replacement_event.get("incoming_edge", 0.0),
                                    "hold_gap": replacement_event.get("hold_gap", 0.0),
                                    "replace_for": (candidate.get("question") or "")[:80],
                                    "type_bias_signal": "competition_profile",
                                })
                                log_lifecycle_event(closed_event, current_regime)
                        else:
                            print("TRACE | competition_skip | registry={} | gate={} | no_recyclable_slot | reason={} | {}".format(
                                EDGE_REGISTRY_VERSION,
                                candidate.get("__competition_gate_reason", "unknown"),
                                reason,
                                (candidate.get("question") or "")[:110],
                            ))
                    elif candidate.get("__competition_gate_reason") and candidate.get("__competition_gate_reason") not in {"competition_hard_gate", "hot_slot_general_strict", "hot_slot_sports_explorer_strict", "hot_slot_narrative_strict"}:
                        print("TRACE | competition_gate_block | registry={} | gate={} | reason={} | {}".format(
                            EDGE_REGISTRY_VERSION,
                            candidate.get("__competition_gate_reason", "unknown"),
                            reason,
                            (candidate.get("question") or "")[:110],
                        ))

                    final_open_stake, stake_meta = unified_cap_arbiter(
                        candidate,
                        float(stake_meta.get("cap_arbiter_base_stake", stake) or stake),
                        stake_meta,
                        current_regime,
                        opened_now,
                        locals().get("regime_cfg") or locals().get("regime_settings_map") or locals().get("current_regime_settings") or locals().get("settings") or {}
                    )
                    print(
                        "TRACE | pre_open_gate_audit | pressure_gate_block={} | pressure_hard_block={} | pressure_decay_preentry={} | legal_cooldown_authority={} | sports_churn_block={} | weak_legal_override_authority={} | scout_demotion_active={} | narrative_brake_active={} | sports_override_brake_active={} | follow_through_active={} | follow_through_force_delayed={} | follow_through_risk={} | canonical_cap_active={} | canonical_cap_signal={} | canonical_cap_value={:.2f} | base_stake={:.2f} | final_open_stake={:.2f} | reason={} | market_type={} | {}".format(
                            int(bool(calm_pressure_quality_gate_state(candidate, reason, current_regime).get("block", False))),
                            int(bool(calm_pressure_hard_block_state(candidate, reason, current_regime).get("block", False))),
                            int(bool(pressure_decay_preentry_state(candidate, reason, current_regime).get("block", False))),
                            int(bool(should_block_legal_pressure_admission(candidate, reason, market_exit_memory)[0])),
                            int(bool(should_block_sports_longshot_churn(candidate, reason, current_regime, market_exit_memory)[0])),
                            int(bool(should_block_weak_legal_override(candidate, reason, market_exit_memory)[0])),
                            int(bool(candidate.get("_political_rescue_scout_demotion", False) or candidate.get("_thin_pressure_truth_active", False) or candidate.get("_elite_recovery_clamp_active", False) or candidate.get("_relief_escalation_active", False) or candidate.get("_adaptive_budget_active", False) or candidate.get("_follow_through_force_delayed", False))),
                            int(bool(candidate.get("_narrative_full_size_brake", False))),
                            int(bool(candidate.get("_weak_sports_override_brake", False))),
                            int(bool(candidate.get("_dead_money_compression_active", False))),
                            int(bool(candidate.get("_follow_through_force_delayed", False))),
                            int(candidate.get("_follow_through_risk_score", 0) or 0),
                            int(bool(stake_meta.get("canonical_cap_active", False))),
                            str(stake_meta.get("canonical_cap_signal", "none") or "none"),
                            float(stake_meta.get("canonical_cap_value", 0.0) or 0.0),
                            float(stake),
                            float(final_open_stake),
                            reason,
                            str(candidate.get("market_type", "general_binary") or "general_binary"),
                            (candidate.get("question") or "")[:72],
                        )
                    )

                    trade = engine.open_position(
                        candidate,
                        stake_override=final_open_stake,
                        now_ts=now_ts,
                        regime=current_regime,
                        confidence=confidence
                    )
                    if trade:
                        if trade.get("political_override_entry"):
                            print("TRACE | political_hold_activation | hold={} | hold_cycles={} | cross={} | cross_cycles={} | corridor={} | mirror_ok={} | reason={} | {}".format(
                                int(bool(trade.get("political_hold_window", False))),
                                int(trade.get("political_hold_window_cycles", 0) or 0),
                                int(bool(trade.get("cross_family_thesis_priority", False))),
                                int(trade.get("cross_family_priority_cycles", 0) or 0),
                                int(bool(trade.get("override_survival_corridor", False))),
                                int(bool(trade.get("position_flag_mirror_ok", False))),
                                trade.get("political_override_reason"),
                                (trade.get("question") or "")[:110],
                            ))
                        opened_now += 1
                        if bool(candidate.get("_adaptive_budget_active", False)) and bool(candidate.get("_adaptive_budget_triggered", False)):
                            adaptive_admission_budget_used += 1
                            print("TRACE | adaptive_admission_budget_consume | used={} | total={} | signal={} | block={} | {}".format(adaptive_admission_budget_used, adaptive_admission_budget_total, str(candidate.get("_adaptive_budget_signal") or "adaptive_admission_budget"), str(candidate.get("_adaptive_budget_block") or "unknown"), (candidate.get("question") or "")[:96]))
                        cycle_spend += final_open_stake
                        cycle_theme_counts[theme] = cycle_theme_counts.get(theme, 0) + 1
                        cycle_cluster_counts[cluster] = cycle_cluster_counts.get(cluster, 0) + 1
                        signal_memory[reason]["opened"] += 1

                        if reason in [
                            "pressure",
                            "pre_momentum",
                            "momentum",
                            "momentum_override",
                            "multicycle_momentum_override"
                        ]:
                            momentum_cooldown[key] = now_ts

                        append_jsonl(PAPER_TRADES_FILE, {
                            "ts": utc_now_iso(),
                            "action": "OPEN",
                            "reason": reason,
                            "trade": trade,
                            "price_delta": candidate.get("price_delta", 0.0),
                            "price_delta_window": candidate.get("price_delta_window", 0.0),
                            "price_trend_strength": candidate.get("price_trend_strength", 0.0),
                            "pressure_density": candidate.get("pressure_density", 0.0),
                            "pressure_count": candidate.get("pressure_count", 0),
                            "theme": theme,
                            "cluster": cluster,
                            "market_type": market_type,
                            "market_regime": current_regime,
                            "confidence": confidence,
                            "stake_model": stake_meta,
                            "delayed_entry_confirmed": bool(candidate.get("_delayed_entry_confirmed", False)),
                            "delayed_entry_cycles": int(candidate.get("_delayed_entry_cycles", 0) or 0),
                            "delayed_entry_signal": candidate.get("_delayed_entry_signal"),
                            "delayed_entry_structure_votes": int(candidate.get("_delayed_entry_structure_votes", 0) or 0),
                            "delayed_entry_promotion_score": int(candidate.get("_delayed_entry_promotion_score", 0) or 0),
                            "delayed_entry_mode": stake_meta.get("delayed_entry_mode"),
                        })

                        print(
                            "OPEN | regime={} | source={} | reason={} | type={} | confidence={:.2f} | delayed={} | dmode={} | theme={} | cluster={} | {} | price={:.4f} | delta={:.4f} | window_delta={:.4f} | trend={:.2f} | pressure={:.2f} | pcount={} | stake=${:.2f} | base={:.2f} | q={:.2f} | rg={:.2f} | cl={:.2f} | cl_risk={:.2f} | ht={:.2f} | pf={:.2f} | accel={:.2f} | mt={:.2f} | score={:.4f} | {}".format(
                                current_regime,
                                candidate.get("_universe_source", "primary"),
                                reason,
                                market_type,
                                confidence,
                                int(bool(candidate.get("_delayed_entry_confirmed", False))),
                                stake_meta.get("delayed_entry_mode", "none"),
                                theme,
                                cluster,
                                trade["outcome_name"],
                                trade["entry_price"],
                                candidate.get("price_delta", 0.0),
                                candidate.get("price_delta_window", 0.0),
                                candidate.get("price_trend_strength", 0.0),
                                candidate.get("pressure_density", 0.0),
                                candidate.get("pressure_count", 0),
                                trade["stake_usd"],
                                stake_meta.get("base_stake", 0.0),
                                stake_meta.get("quality_mult", 1.0),
                                stake_meta.get("regime_mult", 1.0),
                                stake_meta.get("cluster_mult", 1.0),
                                stake_meta.get("cluster_risk_mult", 1.0),
                                stake_meta.get("heat_mult", 1.0),
                                stake_meta.get("profit_mult", 1.0),
                                stake_meta.get("accel_mult", 1.0),
                                stake_meta.get("market_type_mult", 1.0),
                                candidate.get("score", 0.0),
                                trade["question"][:110]
                            )
                        )

                    if opened_now >= MAX_NEW_POSITIONS_PER_CYCLE:
                        break

                print("opened_now={} cycle_spend=${:.2f} regime={}".format(opened_now, cycle_spend, current_regime))

                for c in candidates:
                    key = build_market_key(c)
                    current_price = float(c.get("price", 0.0))
                    if key not in price_history:
                        price_history[key] = deque(maxlen=HISTORY_WINDOW)
                    price_history[key].append(current_price)

                save_runtime_state(
                    price_history,
                    momentum_cooldown,
                    score_reentry_cooldown,
                    dead_reentry_cooldown,
                    family_dead_cooldown,
                    stale_reentry_cooldown,
                    delayed_entry_watch,
                    delayed_entry_cooldown,
                    delayed_entry_memory,
                    market_exit_memory,
                    signal_memory,
                )

                await asyncio.sleep(SCAN_INTERVAL_SEC)

            except KeyboardInterrupt:
                save_runtime_state(
                    price_history,
                    momentum_cooldown,
                    score_reentry_cooldown,
                    dead_reentry_cooldown,
                    family_dead_cooldown,
                    stale_reentry_cooldown,
                    delayed_entry_watch,
                    delayed_entry_cooldown,
                    delayed_entry_memory,
                    market_exit_memory,
                    signal_memory,
                )
                raise
            except Exception as e:
                print("[ERROR] {}: {}".format(type(e).__name__, e))
                save_runtime_state(
                    price_history,
                    momentum_cooldown,
                    score_reentry_cooldown,
                    dead_reentry_cooldown,
                    family_dead_cooldown,
                    stale_reentry_cooldown,
                    delayed_entry_watch,
                    delayed_entry_cooldown,
                    delayed_entry_memory,
                    market_exit_memory,
                    signal_memory,
                )
                await asyncio.sleep(15)


if __name__ == "__main__":
    loop = asyncio.get_event_loop()
    loop.run_until_complete(run_loop())