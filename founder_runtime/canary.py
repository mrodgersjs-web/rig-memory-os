#!/usr/bin/env python3
"""
Jake Canary — capability promotion harness.

New detectors enter as severity='shadow': they evaluate against live
signals and log predicted blocks, but never intervene. After >=7 days
in shadow with precision >= 0.80 they may promote to 'warning'.

CAPABILITIES tuple format (jake_harness.py):
  (id, domain, severity, trigger_fn, intervention, why)

Usage:
  PYTHONPATH=. python -m founder_runtime.canary --cycle
  PYTHONPATH=. python -m founder_runtime.canary --promote <id>
  PYTHONPATH=. python -m founder_runtime.canary --status
  PYTHONPATH=. python -m founder_runtime.canary --register-synthetic
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

STATE = Path.home() / ".rig" / "state"
REGISTRY_PATH = STATE / "canary-registry.json"
HARNESS_SRC = Path(__file__).resolve().parent / "jake_harness.py"

# Promotion gates
MIN_DAYS_IN_SHADOW = 7
MIN_PRECISION = 0.80

# Built-in trigger factories for registry-persisted canaries (capability 19+).
# Callables cannot be JSON-serialized; we store a kind string and rebuild.
TRIGGER_KINDS: dict[str, Callable] = {}


def _trigger_always_true(sig: Any) -> str:
    n = getattr(sig, "sessions_active", 0) or len(getattr(sig, "sessions", []) or [])
    return f"synthetic always-true over {n} sessions"


def _trigger_never(sig: Any) -> None:
    return None


TRIGGER_KINDS["always_true"] = _trigger_always_true
TRIGGER_KINDS["never"] = _trigger_never


def _rebuild_trigger(kind: str | None) -> Callable:
    if kind and kind in TRIGGER_KINDS:
        return TRIGGER_KINDS[kind]
    # default safe: never fires (won't inflate precision)
    return _trigger_never



# ---------------------------------------------------------------- data

@dataclass
class CanaryCapability:
    """A detector living in the shadow stage before it can block."""
    id: str
    domain: str
    trigger_fn: Callable
    severity: str = "shadow"
    created_at: str = ""
    shadow_log: list = field(default_factory=list)

    def __post_init__(self) -> None:
        if not self.created_at:
            self.created_at = _now_iso()


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _today_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


# ---------------------------------------------------------------- registry I/O

def _empty_entry(created_at: str | None = None) -> dict:
    return {
        "days_in_shadow": 0,
        "predicted_blocks": 0,
        "actual_blocked": 0,
        "precision": 0.0,
        "promoted": False,
        "created_at": created_at or _now_iso(),
        "first_seen_day": _today_utc(),
        "last_cycle_at": None,
        "shadow_log": [],
        "domain": "",
        "severity": "shadow",
        "trigger_kind": None,
        "intervention": "",
        "why": "",
    }


def load_registry() -> dict:
    """SHADOW_REGISTRY: dict id -> {days_in_shadow, predicted_blocks,
    actual_blocked, precision, promoted, ...}"""
    if REGISTRY_PATH.exists():
        try:
            data = json.loads(REGISTRY_PATH.read_text())
            if isinstance(data, dict):
                return data
        except Exception:
            pass
    return {}


def save_registry(reg: dict) -> None:
    STATE.mkdir(parents=True, exist_ok=True)
    tmp = REGISTRY_PATH.with_name(REGISTRY_PATH.stem + f"-{os.getpid()}.tmp")
    tmp.write_text(json.dumps(reg, indent=2, default=str))
    tmp.replace(REGISTRY_PATH)


# module-level registry handle (reloaded on each public entry)
SHADOW_REGISTRY: dict = {}


def _sync_registry() -> dict:
    global SHADOW_REGISTRY
    SHADOW_REGISTRY = load_registry()
    return SHADOW_REGISTRY


# ---------------------------------------------------------------- outcomes

def _session_clusters(session: dict) -> dict[str, int]:
    """Path-prefix clusters — same heuristic as pattern_extractor scope_creep."""
    clusters: dict[str, int] = {}
    for f in session.get("files_modified", []) or []:
        parts = [p for p in str(f).split("/") if p and p not in (".", "~")]
        key = "/".join(parts[:4]) if len(parts) >= 4 else "/".join(parts)
        if not key:
            continue
        clusters[key] = clusters.get(key, 0) + 1
    return clusters


def session_ended_badly(session: dict) -> tuple[bool, list[str]]:
    """Reuse jake_live_report / jake_predictions outcomes.

    Bad = ended_without_tests (with real edit work) OR path-cluster drift.
    Returns (is_bad, reasons).
    """
    reasons: list[str] = []
    try:
        from founder_runtime.jake_predictions import session_outcome
        outcome = session_outcome(session)
        ended_without = bool(outcome.get("ended_without_tests"))
    except Exception:
        ended_without = session.get("test_runs", 0) == 0

    n_files = len(session.get("files_modified", []) or [])
    # idle/read-only sessions aren't "ended badly"
    if ended_without and n_files >= 2:
        reasons.append("ended_without_tests")

    clusters = _session_clusters(session)
    if len(clusters) > 1:
        counts = sorted(clusters.values(), reverse=True)
        main_cluster, drift = counts[0], sum(counts[1:])
        if drift >= 2 and drift >= main_cluster * 0.34:
            reasons.append("drift")

    return (len(reasons) > 0, reasons)


def fleet_has_bad_outcome(sessions: list[dict]) -> tuple[bool, dict]:
    """Aggregate session outcomes across the live signal set."""
    bad_ids: list[str] = []
    reason_counts: dict[str, int] = {}
    for s in sessions:
        is_bad, reasons = session_ended_badly(s)
        if is_bad:
            bad_ids.append(str(s.get("session_id", "?"))[:12])
            for r in reasons:
                reason_counts[r] = reason_counts.get(r, 0) + 1
    evidence = {
        "bad_session_count": len(bad_ids),
        "bad_session_ids": bad_ids[:8],
        "reason_counts": reason_counts,
        "sessions_scored": len(sessions),
    }
    return (len(bad_ids) > 0, evidence)


# ---------------------------------------------------------------- shadow discovery

def shadow_capabilities(capabilities: list | None = None) -> list[tuple]:
    """CAPABILITIES entries whose severity == 'shadow'.

    Also rehydrates registry-persisted canaries (19+) into the live list so
    CLI processes started after --register still evaluate them.
    """
    from founder_runtime import jake_harness as jh
    if capabilities is None:
        rehydrate_registered_canaries()
        caps = jh.CAPABILITIES
    else:
        caps = capabilities
    out = []
    for cap in caps:
        try:
            cid, domain, severity, trigger_fn, intervention, why = cap
        except Exception:
            continue
        if severity == "shadow":
            out.append(cap)
    return out


def rehydrate_registered_canaries() -> int:
    """Inject non-promoted registry canaries into jake_harness.CAPABILITIES.

    Returns number of caps added/refreshed. Source-file shadow caps already
    present in CAPABILITIES are left alone; registry-only (19+) are rebuilt
    from trigger_kind.
    """
    from founder_runtime import jake_harness as jh
    reg = load_registry()
    existing: dict[str, int] = {}
    for i, cap in enumerate(jh.CAPABILITIES):
        try:
            existing[cap[0]] = i
        except Exception:
            continue
    added = 0
    for cid, entry in reg.items():
        if entry.get("promoted"):
            continue
        if entry.get("severity", "shadow") not in ("shadow", None, ""):
            continue
        kind = entry.get("trigger_kind")
        # Only rehydrate entries we know how to rebuild (registered canaries).
        # Source-file shadows have no trigger_kind and already live in CAPABILITIES.
        if not kind:
            continue
        domain = entry.get("domain") or "canary"
        trigger_fn = _rebuild_trigger(kind)
        intervention = entry.get("intervention") or (
            "SHADOW ONLY — predicted blocks logged, no intervention."
        )
        why = entry.get("why") or "Registry-persisted canary capability."
        shadow_tuple = (cid, domain, "shadow", trigger_fn, intervention, why)
        if cid in existing:
            cur = jh.CAPABILITIES[existing[cid]]
            if len(cur) >= 3 and cur[2] == "shadow":
                jh.CAPABILITIES[existing[cid]] = shadow_tuple
                added += 1
        else:
            jh.CAPABILITIES.append(shadow_tuple)
            existing[cid] = len(jh.CAPABILITIES) - 1
            added += 1
    return added


def _ensure_registry_entry(reg: dict, cap_id: str, created_at: str | None = None) -> dict:
    if cap_id not in reg:
        reg[cap_id] = _empty_entry(created_at)
    return reg[cap_id]


def _recompute_days(entry: dict) -> int:
    """days_in_shadow from first_seen_day (calendar days, UTC)."""
    first = entry.get("first_seen_day") or _today_utc()
    try:
        d0 = datetime.strptime(first, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        delta = (datetime.now(timezone.utc).date() - d0.date()).days
        days = max(0, int(delta))
    except Exception:
        days = int(entry.get("days_in_shadow") or 0)
    entry["days_in_shadow"] = days
    return days


def _recompute_precision(entry: dict) -> float:
    pred = int(entry.get("predicted_blocks") or 0)
    # precision = correct_predictions / total_predictions
    # We store actual_blocked as the count of predictions that matched a
    # real bad outcome (i.e. correct positives). False positives = predicted
    # without a bad outcome.
    correct = int(entry.get("actual_blocked") or 0)
    if pred <= 0:
        prec = 0.0
    else:
        prec = correct / pred
    entry["precision"] = round(prec, 4)
    return entry["precision"]


# ---------------------------------------------------------------- cycle

def run_shadow_cycle(sig=None) -> dict:
    """One shadow evaluation pass.

    For each shadow capability: fire trigger against live signals; if it
    would block, log a predicted_block WITHOUT intervening; score against
    session outcomes (ended_without_tests / drift).
    """
    from founder_runtime.jake_harness import collect_signals

    reg = _sync_registry()
    if sig is None:
        sig = collect_signals()

    fleet_bad, outcome_evidence = fleet_has_bad_outcome(list(sig.sessions or []))
    shadows = shadow_capabilities()
    cycle_at = _now_iso()
    results: list[dict] = []

    for cap in shadows:
        cid, domain, severity, trigger_fn, intervention, why = cap
        entry = _ensure_registry_entry(reg, cid)
        if entry.get("promoted"):
            results.append({
                "id": cid, "skipped": True, "reason": "already_promoted",
            })
            continue

        detail = None
        trigger_error = None
        try:
            detail = trigger_fn(sig)
        except Exception as e:
            trigger_error = str(e)

        fired = bool(detail) and trigger_error is None
        log_row = {
            "at": cycle_at,
            "fired": fired,
            "detail": str(detail)[:240] if detail else None,
            "fleet_bad": fleet_bad,
            "outcome_evidence": outcome_evidence,
            "trigger_error": trigger_error,
        }

        if fired:
            entry["predicted_blocks"] = int(entry.get("predicted_blocks") or 0) + 1
            # correct prediction iff the fleet actually had a bad outcome
            if fleet_bad:
                entry["actual_blocked"] = int(entry.get("actual_blocked") or 0) + 1
                log_row["correct"] = True
            else:
                log_row["correct"] = False

        _recompute_days(entry)
        _recompute_precision(entry)
        entry["last_cycle_at"] = cycle_at
        logs = list(entry.get("shadow_log") or [])
        logs.append(log_row)
        # keep last 50 cycle rows
        entry["shadow_log"] = logs[-50:]

        results.append({
            "id": cid,
            "domain": domain,
            "fired": fired,
            "detail": str(detail)[:240] if detail else None,
            "correct": log_row.get("correct"),
            "predicted_blocks": entry["predicted_blocks"],
            "actual_blocked": entry["actual_blocked"],
            "precision": entry["precision"],
            "days_in_shadow": entry["days_in_shadow"],
            "evidence": {
                "trigger_detail": str(detail)[:240] if detail else None,
                "fleet_bad": fleet_bad,
                "outcome": outcome_evidence,
            },
        })

    save_registry(reg)
    _sync_registry()

    out = {
        "generated_at": cycle_at,
        "sessions_active": getattr(sig, "sessions_active", len(sig.sessions or [])),
        "fleet_bad_outcome": fleet_bad,
        "outcome_evidence": outcome_evidence,
        "shadow_count": len(shadows),
        "results": results,
    }
    return out


# ---------------------------------------------------------------- promote

def _edit_harness_severity(cap_id: str, old: str = "shadow", new: str = "warning") -> bool:
    """Carefully rewrite severity in jake_harness.CAPABILITIES source via regex.

    Matches the tuple head:
      ( "cap_id", "domain", "shadow",
    and rewrites the severity token only.
    """
    if not HARNESS_SRC.exists():
        return False
    src = HARNESS_SRC.read_text()
    # Allow optional whitespace/newlines between tuple fields.
    pat = re.compile(
        rf'(\(\s*"{re.escape(cap_id)}"\s*,\s*"[^"]*"\s*,\s*)"{re.escape(old)}"',
        re.MULTILINE,
    )
    new_src, n = pat.subn(rf'\1"{new}"', src, count=1)
    if n != 1:
        return False
    # atomic write
    tmp = HARNESS_SRC.with_name(HARNESS_SRC.stem + f"-{os.getpid()}.tmp")
    tmp.write_text(new_src)
    tmp.replace(HARNESS_SRC)
    return True


def _patch_inmemory_severity(cap_id: str, new_severity: str = "warning") -> bool:
    """Update the live CAPABILITIES list so the process sees the promotion."""
    from founder_runtime import jake_harness as jh
    for i, cap in enumerate(jh.CAPABILITIES):
        try:
            cid, domain, severity, trigger_fn, intervention, why = cap
        except Exception:
            continue
        if cid == cap_id:
            jh.CAPABILITIES[i] = (cid, domain, new_severity, trigger_fn, intervention, why)
            return True
    return False


def _ledger_promotion(cap_id: str, entry: dict) -> dict:
    """Append an admitted-promotion record to the mutation-gate ledger."""
    from founder_runtime.mutation_gate import ledger_append
    payload = {
        "kind": "canary_promotion",
        "admitted": True,
        "capability_id": cap_id,
        "from_severity": "shadow",
        "to_severity": "warning",
        "days_in_shadow": entry.get("days_in_shadow"),
        "precision": entry.get("precision"),
        "predicted_blocks": entry.get("predicted_blocks"),
        "actual_blocked": entry.get("actual_blocked"),
        "ts": _now_iso(),
        "evidence": {
            "gate": f"days>={MIN_DAYS_IN_SHADOW} AND precision>={MIN_PRECISION}",
            "registry_path": str(REGISTRY_PATH),
        },
    }
    return ledger_append(payload)


def promote_if_ready(cap_id: str, force: bool = False) -> bool:
    """Promote shadow -> warning when days_in_shadow >= 7 AND precision >= 0.80.

    Edits jake_harness.CAPABILITIES source + in-memory list, logs to the
    mutation-gate ledger. Returns True on successful promotion.
    """
    # Ensure registry-only canaries are present in the live list first.
    rehydrate_registered_canaries()
    reg = _sync_registry()
    entry = reg.get(cap_id)
    if entry is None:
        # try to seed from live CAPABILITIES
        for cap in shadow_capabilities():
            if cap[0] == cap_id:
                entry = _ensure_registry_entry(reg, cap_id)
                break
    if entry is None:
        return False
    if entry.get("promoted"):
        return False

    _recompute_days(entry)
    _recompute_precision(entry)
    days = int(entry.get("days_in_shadow") or 0)
    prec = float(entry.get("precision") or 0.0)

    ready = force or (days >= MIN_DAYS_IN_SHADOW and prec >= MIN_PRECISION)
    if not ready:
        save_registry(reg)
        return False

    # 1) source edit
    src_ok = _edit_harness_severity(cap_id, "shadow", "warning")
    # 2) in-memory (always — even if source already warning / synthetic-only)
    mem_ok = _patch_inmemory_severity(cap_id, "warning")
    if not src_ok and not mem_ok:
        # nothing to promote (cap not present)
        return False

    entry["promoted"] = True
    entry["promoted_at"] = _now_iso()
    entry["promoted_to"] = "warning"
    save_registry(reg)
    _sync_registry()

    try:
        _ledger_promotion(cap_id, entry)
    except Exception:
        # promotion still counts; ledger failure is non-fatal for the boolean
        pass
    return True


# ---------------------------------------------------------------- register

def register_canary(cap_tuple: tuple, trigger_kind: str | None = None) -> dict:
    """Add a new shadow capability (capability 19+).

    cap_tuple: (id, domain, severity, trigger_fn, intervention, why)
    Severity is forced to 'shadow' regardless of the incoming value.
    trigger_kind: optional registry key for cross-process rehydration
      (e.g. 'always_true'). Inferred from known TRIGGER_KINDS when omitted.
    """
    from founder_runtime import jake_harness as jh

    if not isinstance(cap_tuple, tuple) or len(cap_tuple) != 6:
        raise ValueError(
            "cap_tuple must be (id, domain, severity, trigger_fn, intervention, why)"
        )
    cid, domain, _sev, trigger_fn, intervention, why = cap_tuple
    shadow_tuple = (cid, domain, "shadow", trigger_fn, intervention, why)

    # Infer trigger_kind from identity of known factories when not given.
    kind = trigger_kind
    if kind is None:
        for k, fn in TRIGGER_KINDS.items():
            if trigger_fn is fn:
                kind = k
                break

    # replace existing id or append
    replaced = False
    for i, cap in enumerate(jh.CAPABILITIES):
        try:
            existing_id = cap[0]
        except Exception:
            continue
        if existing_id == cid:
            jh.CAPABILITIES[i] = shadow_tuple
            replaced = True
            break
    if not replaced:
        jh.CAPABILITIES.append(shadow_tuple)

    reg = _sync_registry()
    entry = _ensure_registry_entry(reg, cid)
    entry["domain"] = domain
    entry["severity"] = "shadow"
    entry["trigger_kind"] = kind
    entry["intervention"] = intervention
    entry["why"] = why
    if entry.get("promoted"):
        # re-arm if re-registered
        entry["promoted"] = False
        entry["promoted_at"] = None
    save_registry(reg)
    _sync_registry()

    return {
        "id": cid,
        "domain": domain,
        "severity": "shadow",
        "trigger_kind": kind,
        "replaced": replaced,
        "capabilities_loaded": len(jh.CAPABILITIES),
        "registry_entry": {
            k: entry[k] for k in (
                "days_in_shadow", "predicted_blocks", "actual_blocked",
                "precision", "promoted", "created_at", "trigger_kind",
            ) if k in entry
        },
    }


def register_synthetic_always_true(cap_id: str = "canary_synthetic_always") -> dict:
    """Acceptance helper: always-true shadow capability for precision smoke."""
    return register_canary(
        (
            cap_id,
            "canary-test",
            "shadow",
            _trigger_always_true,
            "SHADOW ONLY — never blocks; used to validate the canary precision path.",
            "Acceptance fixture: trigger always fires so precision = P(fleet_bad).",
        ),
        trigger_kind="always_true",
    )


# ---------------------------------------------------------------- status / CLI

def status() -> dict:
    reg = _sync_registry()
    shadows = shadow_capabilities()
    shadow_ids = {c[0] for c in shadows}
    rows = []
    for cid, entry in sorted(reg.items()):
        _recompute_days(entry)
        _recompute_precision(entry)
        rows.append({
            "id": cid,
            "in_live_shadow": cid in shadow_ids,
            "days_in_shadow": entry.get("days_in_shadow"),
            "predicted_blocks": entry.get("predicted_blocks"),
            "actual_blocked": entry.get("actual_blocked"),
            "precision": entry.get("precision"),
            "promoted": entry.get("promoted"),
            "last_cycle_at": entry.get("last_cycle_at"),
            "ready": (
                not entry.get("promoted")
                and int(entry.get("days_in_shadow") or 0) >= MIN_DAYS_IN_SHADOW
                and float(entry.get("precision") or 0.0) >= MIN_PRECISION
            ),
        })
    # also surface live shadow caps missing from registry
    for cap in shadows:
        if cap[0] not in reg:
            rows.append({
                "id": cap[0],
                "in_live_shadow": True,
                "days_in_shadow": 0,
                "predicted_blocks": 0,
                "actual_blocked": 0,
                "precision": 0.0,
                "promoted": False,
                "last_cycle_at": None,
                "ready": False,
                "note": "live shadow, not yet cycled",
            })
    return {
        "generated_at": _now_iso(),
        "registry_path": str(REGISTRY_PATH),
        "gates": {"min_days": MIN_DAYS_IN_SHADOW, "min_precision": MIN_PRECISION},
        "live_shadow_count": len(shadows),
        "registry_count": len(reg),
        "capabilities": rows,
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="founder_runtime.canary",
                                 description="Jake capability canary / promotion harness")
    ap.add_argument("--cycle", action="store_true",
                    help="run one shadow evaluation against live signals")
    ap.add_argument("--promote", metavar="ID",
                    help="attempt promotion of a shadow capability")
    ap.add_argument("--force-promote", action="store_true",
                    help="with --promote, skip days/precision gates (debug only)")
    ap.add_argument("--status", action="store_true",
                    help="print shadow registry")
    ap.add_argument("--register-synthetic", action="store_true",
                    help="register always-true shadow cap for acceptance smoke")
    ap.add_argument("--register-synthetic-id", default="canary_synthetic_always",
                    help="id used by --register-synthetic")
    args = ap.parse_args(argv)

    if not any([args.cycle, args.promote, args.status, args.register_synthetic]):
        ap.print_help()
        return 2

    if args.register_synthetic:
        out = register_synthetic_always_true(args.register_synthetic_id)
        print(json.dumps({"action": "register_synthetic", **out}, indent=2, default=str))

    if args.cycle:
        out = run_shadow_cycle()
        print(json.dumps({"action": "cycle", **out}, indent=2, default=str))
        # one-line result for acceptance grepping
        n_fire = sum(1 for r in out.get("results", []) if r.get("fired"))
        print(
            f"canary_cycle ok shadows={out.get('shadow_count', 0)} "
            f"fired={n_fire} fleet_bad={out.get('fleet_bad_outcome')} "
            f"sessions={out.get('sessions_active')}"
        )

    if args.promote:
        ok = promote_if_ready(args.promote, force=bool(args.force_promote))
        reg = _sync_registry()
        entry = reg.get(args.promote, {})
        print(json.dumps({
            "action": "promote",
            "id": args.promote,
            "promoted": ok,
            "days_in_shadow": entry.get("days_in_shadow"),
            "precision": entry.get("precision"),
            "gates": {"min_days": MIN_DAYS_IN_SHADOW, "min_precision": MIN_PRECISION},
            "force": bool(args.force_promote),
        }, indent=2, default=str))
        print(f"canary_promote id={args.promote} ok={ok}")

    if args.status:
        out = status()
        print(json.dumps({"action": "status", **out}, indent=2, default=str))
        print(
            f"canary_status registry={out.get('registry_count')} "
            f"live_shadow={out.get('live_shadow_count')}"
        )

    return 0


if __name__ == "__main__":
    sys.exit(main())
