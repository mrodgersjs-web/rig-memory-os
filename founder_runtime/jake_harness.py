#!/usr/bin/env python3
"""
Jake Mega-Harness — the orchestrator.

Jake is the overall agent. This harness is how he works:
  - A registry of CAPABILITIES (pattern detectors), each contributed by a
    council member domain: predictions, test discipline, git hygiene,
    debugging, scope, focus, verification, memory, security, ...
  - Each capability = measurable TRIGGER over session signals + an
    INTERVENTION Jake takes when it fires (block / warn / brief / require).
  - Jake evaluates all triggers against live session signals every cycle
    and emits the active intervention set, highest-severity first.

Surfaces (all three harnesses aligned):
  OMP / Claude Code / Hermes / Codex / OpenClaw
    -> MCP tool `memory_get_guidance` (already on the bus) includes harness
  Claude Code
    -> PreToolUse hook (jake-pushback.sh) blocks on `blocking` severity
  Cron (every 5 min)
    -> harness evaluation -> Obsidian guidance brief

Adding a capability = appending one dict to CAPABILITIES with a trigger
lambda over the SignalSet. No other wiring needed — Jake picks it up
on the next cycle.
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from founder_runtime.jake_live_report import parse_session, CLAUDE_PROJECTS

STATE = Path.home() / ".rig" / "state"
HARNESS_STATE = STATE / "jake-harness.json"


# ---------------------------------------------------------------- signals

@dataclass
class SignalSet:
    """Everything Jake can observe about the current coding fleet."""
    # fleet aggregates
    sessions_active: int = 0
    total_phases: int = 0
    phase_counts: dict[str, int] = field(default_factory=dict)
    files_touched: int = 0
    test_runs: int = 0
    read_edit_ratio: float = 1.0
    # per-session details (active window)
    sessions: list[dict] = field(default_factory=list)
    # git reality
    uncommitted_files: int = 0
    uncommitted_repos: list[str] = field(default_factory=list)
    # time
    hour_local: int = 12
    # prediction engine state
    forecast_accuracy: float = 0.5
    forecast_n: int = 0
    anti_calibrated: bool = False


def collect_signals() -> SignalSet:
    """Gather the live signal set from transcripts + git + prediction state."""
    now = time.time()
    sig = SignalSet(hour_local=time.localtime().tm_hour)

    sessions = []
    phase_counts: dict[str, int] = {}
    all_files: set[str] = set()
    tests = 0

    for p in CLAUDE_PROJECTS.rglob("*.jsonl"):
        if "subagents" in p.parts:
            continue
        try:
            if now - p.stat().st_mtime > 6 * 3600:
                continue
        except OSError:
            continue
        try:
            s = parse_session(p)
        except Exception:
            continue
        if not s["phases"]:
            continue
        sessions.append(s)
        for ph, _ in s["phases"]:
            phase_counts[ph] = phase_counts.get(ph, 0) + 1
        all_files.update(s["files_modified"])
        tests += s["test_runs"]

    sig.sessions_active = len(sessions)
    sig.sessions = sessions
    sig.phase_counts = phase_counts
    sig.total_phases = sum(phase_counts.values())
    sig.files_touched = len(all_files)
    sig.test_runs = tests
    reads = phase_counts.get("read", 0) + phase_counts.get("search", 0)
    edits = phase_counts.get("edit", 0)
    sig.read_edit_ratio = reads / edits if edits else 1.0

    # git state across known repos (fast, read-only)
    for repo in [Path.home() / "Developer"]:
        try:
            for gitdir in repo.glob("*/.git"):
                root = gitdir.parent
                try:
                    out = subprocess.run(
                        ["git", "-C", str(root), "status", "--porcelain"],
                        capture_output=True, timeout=3, text=True,
                    )
                    n = len([l for l in out.stdout.splitlines() if l.strip()])
                    if n > 0:
                        sig.uncommitted_files += n
                        sig.uncommitted_repos.append(f"{root.name}:{n}")
                except Exception:
                    continue
        except Exception:
            continue

    # prediction engine state
    bridge_path = STATE / "prediction-bridge.json"
    if bridge_path.exists():
        try:
            bridge = json.loads(bridge_path.read_text())
            sig.forecast_n = bridge.get("stats", {}).get("n", 0)
        except Exception:
            pass
    try:
        from founder_runtime.jake_guidance import _resolution_stats
        st = _resolution_stats()
        sig.forecast_accuracy = st.get("accuracy", 0.5)
        sig.forecast_n = st.get("n", 0)
        recent = st.get("recent", {})
        sig.anti_calibrated = (
            recent.get("n", 0) >= 10 and recent.get("accuracy", 1.0) < 0.4
        )
    except Exception:
        pass

    return sig


# ---------------------------------------------------------------- capabilities
# Each: (id, domain, severity, trigger(sig)->Optional[detail], intervention, why)
# Sources: Council of 20 nominations (2026-08-05), deduped + implemented.
# Capability 17 (mutation-veto gate) is the council-ratified containment layer
# for any self-modification of Jake's TCB — unanimous 2026-08-05.
# Capability 18 (generative_shipper) closes the generative loop: stigmergy
# candidates admitted by the mutation gate await capability promotion.
# Capability 19 (guidance_fatigue) kills repeated advice Mike has tuned out —
# any jake_advice line seen 3+ cycles without behavior change is stale.
# CAPABILITIES count: 18 existing + guidance_fatigue = 19 total expected.

Capability = tuple  # (id, domain, severity, trigger_fn, intervention, why)

SECRET_PATH_RE = re.compile(
    r"(\.env($|\.)|\.pem$|_rsa$|credentials\.json|secrets?/|\.aws/|\.ssh/|"
    r"id_ed25519|\.keystore|\.p12$|\.key$|token\.json)", re.I)


def _multi_file_no_tests(sig: "SignalSet", min_files: int) -> list[dict]:
    return [s for s in sig.sessions
            if len(s["files_modified"]) >= min_files and s["test_runs"] == 0]


def _longest_edit_streak(sig: dict) -> int:
    """Longest run of edit phases with no read/search interleaved."""
    best = cur = 0
    for ph, _ in sig["phases"]:
        if ph == "edit":
            cur += 1
            best = max(best, cur)
        elif ph in ("read", "search"):
            cur = 0
    return best


def _session_clusters(sig: dict) -> set[str]:
    clusters = set()
    for f in sig["files_modified"]:
        parts = [p for p in str(f).split("/") if p and p not in (".", "~")]
        clusters.add("/".join(parts[:4]) if len(parts) >= 4 else "/".join(parts))
    return clusters


def _file_edit_counts(sig: dict) -> dict[str, int]:
    """Edit counts per REAL file (skip bash: pseudo-paths from shell writes)."""
    real_files = [f for f in sig["files_modified"]
                  if not str(f).startswith("bash:")]
    counts: dict[str, int] = {}
    n_edits = sum(1 for ph, _ in sig["phases"] if ph == "edit")
    if not real_files or n_edits == 0:
        return counts
    # Attribute the session's edit phases across its real files proportionally:
    # a file only counts as "repeatedly fixed" when edits outnumber files 3:1
    per_file = n_edits / len(real_files)
    for f in real_files:
        counts[f] = int(per_file)
    return counts


def _colliding_pairs(sig: "SignalSet") -> list:
    pairs = []
    for i, a in enumerate(sig.sessions):
        for b in sig.sessions[i + 1:]:
            shared = set(a["files_modified"]) & set(b["files_modified"])
            if shared:
                pairs.append((a, b, shared))
    return pairs


CAPABILITIES: list[Capability] = [
    # ============ VERIFIED-BY-DATA CORE (8 council members converged) ============
    (
        "testless_multifile", "test-discipline", "blocking",
        lambda s: (f"{len(_multi_file_no_tests(s, 4))} sessions with 4+ files, 0 tests: "
                   + ", ".join(x['session_id'][:8] for x in _multi_file_no_tests(s, 4)[:4])
                   if _multi_file_no_tests(s, 4) else None),
        "Warn at 4 files/0 tests; BLOCK next edit at 7 files until a test phase runs. "
        "Council: Coda/Onyx/Iris/Esko/Delve/Quill/Brisk/Kest all nominated this.",
        "9 gtm-studio sessions touched 4-10 files with zero test runs — the single "
        "most-validated pattern in the fleet data.",
    ),
    # ============ READ-BEFORE-EDIT ENFORCEMENT (Gale, Tarn, Kest) ============
    (
        "blind_edit_streak", "harness-design", "blocking",
        lambda s: (f"longest no-read edit streak: {max((_longest_edit_streak(x) for x in s.sessions), default=0)}"
                   if max((_longest_edit_streak(x) for x in s.sessions), default=0) >= 5 else None),
        "Block the 6th consecutive edit with no read/search — force one read of the "
        "target file first. (Tarn: cheapest mechanical lever on 20:115 ratio.)",
        "Fleet edits 6x more than it reads; unbroken edit streaks are the direct "
        "precursor to drift and zero-test sessions.",
    ),
    (
        "context_starved_burst", "context-engineering", "warning",
        lambda s: (f"{sum(1 for x in s.sessions if len(x['files_modified']) >= 3 and x['phases'] and x['phases'][0][0] == 'edit')} "
                   "sessions opened with edits before any read/search"
                   if sum(1 for x in s.sessions if len(x["files_modified"]) >= 3
                          and x["phases"] and x["phases"][0][0] == "edit") >= 1 else None),
        "Warn: require the agent to state which conventions/patterns it read before "
        "editing further. (Gale.)",
        "Sessions that edit 3+ files with zero grounding reads are the acute form "
        "of the fleet's chronic read:edit imbalance.",
    ),
    (
        "read_edit_imbalance", "code-craft", "warning",
        lambda s: (f"read:edit = {s.read_edit_ratio:.2f} (reads {s.phase_counts.get('read',0)+s.phase_counts.get('search',0)}, edits {s.phase_counts.get('edit',0)})"
                   if s.phase_counts.get("edit", 0) > 20 and s.read_edit_ratio < 0.35 else None),
        "Brief: require a read/search phase before the next edit batch.",
        "Fleet measured at 20:115 read:edit — 6x more writing than grounding.",
    ),
    # ============ SCOPE & FOCUS (Lark, Mira, Sage) ============
    (
        "first_cluster_crossing", "scope-control", "warning",
        lambda s: (f"{sum(1 for x in s.sessions if len(_session_clusters(x)) >= 2)} sessions editing across unrelated clusters"
                   if sum(1 for x in s.sessions if len(_session_clusters(x)) >= 2) >= 1 else None),
        "Warn on FIRST out-of-cluster edit: 'this touches an unrelated area — confirm "
        "scope or split the session.' Cheap to act on at first crossing, expensive after. (Lark.)",
        "Path-cluster analysis flagged 16/24 active sessions for drift.",
    ),
    (
        "focus_fragmentation", "deep-work", "warning",
        lambda s: (f"{sum(1 for x in s.sessions if len(_session_clusters(x)) >= 3 and x['test_runs'] == 0)} sessions across 3+ clusters, 0 tests"
                   if sum(1 for x in s.sessions if len(_session_clusters(x)) >= 3
                          and x["test_runs"] == 0) >= 1 else None),
        "Warn before the 4th cluster opens; block at 5th if still no test/build. (Mira.)",
        "Fragmented multi-cluster editing with no verification is the observable "
        "signature of lost focus.",
    ),
    # ============ GIT HYGIENE (Hale) ============
    (
        "uncommitted_edit_streak", "git-hygiene", "warning",
        lambda s: (f"{s.uncommitted_files} uncommitted files across {len(s.uncommitted_repos)} repos: {', '.join(s.uncommitted_repos[:5])}"
                   if s.uncommitted_files > 30 else None),
        "Warn at the 4th uncommitted file: list modified files, require git status/diff "
        "review + checkpoint commit or test run. Block at 8. (Hale.)",
        "Multi-file edits with no commit and no test are unrecoverable without a "
        "diff to review — exactly the 9-session failure shape.",
    ),
    # ============ SECURITY (Juno) — hard block ============
    (
        "secret_file_guard", "security", "blocking",
        lambda s: (f"secret-path files touched: {[f for x in s.sessions for f in x['files_modified'] if SECRET_PATH_RE.search(str(f))][:5]}"
                   if any(SECRET_PATH_RE.search(str(f))
                          for x in s.sessions for f in x["files_modified"]) else None),
        "BLOCK outright. Secret-bearing files (.env, .pem, credentials.json, .ssh/, "
        ".aws/, keys) require Mike's explicit approval — one write, one approval, "
        "diff hash logged. (Juno.)",
        "Agent-authored changes to secret files are irreversible the moment they "
        "commit — the one class where advisory is insufficient.",
    ),
    # ============ CONCURRENCY (Fenn) ============
    (
        "session_collision", "agent-orchestration", "blocking",
        lambda s: (f"{len(_colliding_pairs(s))} session pairs editing same files: "
                   + "; ".join(f"{a['session_id'][:6]}&{b['session_id'][:6]}"
                               for a, b, _ in _colliding_pairs(s)[:3])
                   if _colliding_pairs(s) else None),
        "Warn with the colliding session ID + shared paths; block the second colliding "
        "edit until the later session reads the other's diff. (Fenn.)",
        "Two sessions converging on the same files with no coordination channel "
        "silently overwrite each other's work.",
    ),
    # ============ FAILURE RECOVERY (Nox) ============
    (
        "same_file_fix_loop", "failure-recovery", "blocking",
        lambda s: (lambda loops: (f"repeated-edit files: {list(loops)[:3]}" if loops else None))(
                       {f for x in s.sessions for f, c in _file_edit_counts(x).items() if c >= 4}),
        "Warn at 3rd consecutive edit to the SAME file without an intervening test; "
        "block the 4th: 'what's your hypothesis? verify before fixing blind.' (Nox.)",
        "Patching the same file repeatedly without re-verification is the silent-"
        "failure spiral — fixes that were never confirmed.",
    ),
    # ============ PREDICTION HYGIENE (Alder, Pike) ============
    (
        "confidence_accuracy_divergence", "calibration", "warning",
        lambda s: (f"accuracy {s.forecast_accuracy:.0%} over {s.forecast_n} resolutions while still emitting confident leans"
                   if s.forecast_n >= 30 and s.forecast_accuracy < 0.40 and not s.anti_calibrated else None),
        "Suppress high-confidence presentation; append 'LOW-CONFIDENCE (X% over last "
        "50)' to prediction guidance; recompute base rates. (Alder.)",
        "A 32%-accurate engine guiding interventions compounds false confidence "
        "into every downstream decision.",
    ),
    (
        "anti_calibration_fade", "calibration", "advisory",
        lambda s: (f"recent accuracy below 40% — fade leans, show base rates"
                   if s.anti_calibrated else None),
        "Brief: fade Jake's own leans; show base rates instead of ensemble votes.",
        "Live regime: 0% over the last 50 resolutions vs 32% all-time.",
    ),
    (
        "stale_prediction_loop", "learning-loops", "warning",
        lambda s: (f"accuracy {s.forecast_accuracy:.0%} stagnant across {s.forecast_n} resolutions — loop not learning"
                   if s.forecast_n >= 200 and 0.28 <= s.forecast_accuracy <= 0.40 else None),
        "Write 'model stalled' brief listing last 20 misses by category; gate new "
        "confidence-scored predictions until 5 are manually reviewed. (Pike.)",
        "317 resolutions at 32% means outcomes are recorded but never folded back — "
        "the resolve→relearn loop is open.",
    ),
    # ============ ABSTRACTION (Rune) ============
    (
        "abstraction_without_search", "skill-design", "warning",
        lambda s: (f"{sum(x['abstractions'] for x in s.sessions)} abstractions created; "
                   f"searches: {s.phase_counts.get('search', 0)}"
                   if sum(x["abstractions"] for x in s.sessions) >= 1
                   and s.phase_counts.get("search", 0) == 0
                   and s.phase_counts.get("read", 0) < 3 else None),
        "Warn: before creating a new skill/tool/wrapper, run a search for existing "
        "overlapping ones; block the second unchecked abstraction. (Rune.)",
        "New abstractions born from zero prior reads = skipped the reuse check.",
    ),
    # ============ ORCHESTRATION & TIMING ============
    (
        "delegate_without_read", "agent-orchestration", "warning",
        lambda s: (f"delegate calls {s.phase_counts.get('delegate',0)} vs reads {s.phase_counts.get('read',0)}"
                   if s.phase_counts.get("delegate", 0) > s.phase_counts.get("read", 0) + 5 else None),
        "Warn: subagents spawned without grounding reads produce unreviewable diffs.",
        "Delegation outpacing reading means agents are writing blind.",
    ),
    (
        "late_night_edits", "deep-work", "advisory",
        lambda s: (f"edit activity at {s.hour_local}:00 local"
                   if (s.hour_local >= 23 or s.hour_local < 5) and s.phase_counts.get("edit", 0) > 5 else None),
        "Brief: flag fatigue risk; suggest committing and stopping.",
        "Late-night sessions correlate with next-morning reverts.",
    ),
    (
        "mutation_gate_tamper", "security", "blocking",
        lambda s: _mutation_gate_status(),
        "BLOCK on any ledger tamper-detection or unsigned/unverifiable mutation "
        "verdict. All detector mutations must route through judge() with a valid "
        "Gate-D token. (Council-ratified containment layer, 2026-08-05.)",
        "The trust root must never self-modify untracked — the append-only "
        "hash-chained ledger makes any tampering detectable and any unsigned "
        "mutation inadmissible by construction.",
    ),
    (
        "generative_shipper", "generative", "advisory",
        lambda s: _generative_shipper_status(),
        "Promote admitted candidates to active detectors via mutation_gate.judge-"
        "verified diff; never auto-enable without a passing ledger entry.",
        "This is the generative loop closing — stigmergy surfaces candidates, "
        "the gate admits on evidence, and only then do new capabilities exist.",
    ),
    (
        "guidance_fatigue", "learning-loops", "advisory",
        lambda s: _guidance_fatigue_status(),
        "Rewrite or act on stale advice — repeating the same advisory 3x means "
        "it is not working.",
        "Jake advice lines that fire unchanged across 3 harness/guidance cycles "
        "are tuned out; staleness tracking in jake_guidance is the single source.",
    ),
]


def _mutation_gate_status() -> "str | None":
    """Capability 17 trigger: verify the mutation-gate ledger chain integrity.
    Returns a detail string only when tampering is detected (chain broken)."""
    try:
        from founder_runtime.mutation_gate import ledger_verify_chain
        result = ledger_verify_chain()
        if not result.get("ok"):
            return (f"LEDGER TAMPER at entry {result.get('entries')}: "
                    f"{result.get('error')} — mutation-gate chain broken")
    except Exception as e:
        return f"mutation-gate ledger unreadable (fail-closed treat as tamper): {e}"
    return None


def _generative_shipper_status() -> "str | None":
    """Capability 18 trigger: surface gate-admitted stigmergy candidates awaiting promotion."""
    pending_path = STATE / "capability-18-pending.json"
    if not pending_path.exists():
        return None
    try:
        data = json.loads(pending_path.read_text())
    except Exception:
        return None
    if not isinstance(data, list) or not data:
        return None
    ids: list[str] = []
    for item in data:
        if isinstance(item, dict):
            pid = item.get("proposal_id") or item.get("id") or item.get("surface")
            if pid:
                ids.append(str(pid))
        else:
            ids.append(str(item))
    n = len(data)
    id_str = ", ".join(ids[:8]) if ids else "(unidentified)"
    return (f"{n} candidates admitted through gate, awaiting capability "
            f"promotion: {id_str}")


def _guidance_fatigue_status() -> "str | None":
    """Capability 19 trigger: any advice line at cycles_seen >= 3."""
    path = STATE / "guidance-staleness.json"
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text())
    except Exception:
        return None
    if not isinstance(data, dict) or not data:
        return None
    stale = []
    for h, cycles in data.items():
        try:
            c = int(cycles)
        except (TypeError, ValueError):
            continue
        if c >= 3:
            stale.append((str(h), c))
    if not stale:
        return None
    stale.sort(key=lambda x: -x[1])
    parts = [f"{h[:8]}…×{c}" for h, c in stale[:5]]
    return (
        f"{len(stale)} advice line(s) hit 3+ cycles untouched: "
        + ", ".join(parts)
    )


# ---------------------------------------------------------------- evaluator

@dataclass
class Intervention:
    capability_id: str
    domain: str
    severity: str
    detail: str
    intervention: str
    why: str


SEVERITY_ORDER = {"blocking": 0, "warning": 1, "advisory": 2}


def evaluate(sig: SignalSet,
             capabilities: Optional[list[Capability]] = None) -> list[Intervention]:
    """Jake evaluates every capability's trigger against the live signals."""
    fired: list[Intervention] = []
    for cap in (capabilities or CAPABILITIES):
        cid, domain, severity, trigger_fn, intervention, why = cap
        try:
            detail = trigger_fn(sig)
        except Exception:
            continue
        if detail:
            fired.append(Intervention(cid, domain, severity, detail,
                                      intervention, why))
    fired.sort(key=lambda i: SEVERITY_ORDER.get(i.severity, 3))
    return fired


def run_cycle() -> dict:
    """One harness cycle: collect -> evaluate -> persist -> return."""
    sig = collect_signals()
    interventions = evaluate(sig)
    out = {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "signals": {
            "sessions_active": sig.sessions_active,
            "total_phases": sig.total_phases,
            "phase_counts": sig.phase_counts,
            "files_touched": sig.files_touched,
            "test_runs": sig.test_runs,
            "read_edit_ratio": round(sig.read_edit_ratio, 3),
            "uncommitted_files": sig.uncommitted_files,
            "uncommitted_repos": sig.uncommitted_repos[:10],
            "forecast_accuracy": sig.forecast_accuracy,
            "forecast_n": sig.forecast_n,
            "anti_calibrated": sig.anti_calibrated,
        },
        "capabilities_loaded": len(CAPABILITIES),
        "interventions": [
            {"id": i.capability_id, "domain": i.domain, "severity": i.severity,
             "detail": i.detail, "intervention": i.intervention, "why": i.why}
            for i in interventions
        ],
    }
    HARNESS_STATE.parent.mkdir(parents=True, exist_ok=True)
    import os as _os
    tmp = HARNESS_STATE.with_name(HARNESS_STATE.stem + f"-{_os.getpid()}.tmp")
    tmp.write_text(json.dumps(out, indent=2, default=str))
    tmp.replace(HARNESS_STATE)
    return out


def main() -> int:
    out = run_cycle()
    print(json.dumps(out, indent=2, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
