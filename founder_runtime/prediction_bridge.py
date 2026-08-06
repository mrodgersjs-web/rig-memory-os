#!/usr/bin/env python3
"""
Prediction Studio Bridge — wires Memory OS live signals into the
12-persona Brier-calibrated swarm (rig-prediction-studio-pro).

This is the outcome-prediction loop the council demanded:
  signals -> typed outcome questions -> swarm probability -> AUTO-RESOLVER
  -> Brier re-weighting -> calibration track record

Question types (all auto-resolvable — no vibes):
  session_testless   "Session {id} ends without a test run"
                     resolve: transcript idle >=30min -> outcome = (test_runs==0)
  project_rework     "Project {p} drift-flagged changes get reverted/forced
                     within 7d" — resolve: git log --force check (manual/semi)
  fleet_ratio        "Fleet read:edit ratio stays < 0.25 for the week"
                     resolve: Friday histogram recompute
  ci_result          "Session {id}'s changes pass CI within 24h"
                     resolve: .ci-pass marker or git log --grep=ci in mapped repo
  build_failure      "Session {id} ends with a failing build"
                     resolve: last 3 bash tool_results contain error/failed/Traceback
  revert_follow      "Project {p} changes get reverted within 7 days"
                     resolve: git log --grep=revert since created in mapped repo
  stale_session      "Session {id} becomes stale (no progress 30+ min) without completing"
                     resolve: idle>=30min AND last_phase!='test' AND files<2
  block_follow_revert "A secret-file override gets reverted within 48h"
                     resolve: wrong_rule override log then git revert on that path
  collision_conflict "Sessions A and B conflict on file F within 24h"
                     resolve: collision-log shows both edited F (1) or only A (0)

Dedup: one open prediction per (question_text) — checked against studio db.

Usage:
  PYTHONPATH=. RIG_MEMORY_OS_SECRET=test-universal-secret \
    .venv/bin/python -m founder_runtime.prediction_bridge [--resolve-only]
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, "/Users/rig128gb/Developer/RIGForge/repos/rig-prediction-studio-pro")

from runner.studio_v2 import PredictionStudioV2
from founder_runtime.jake_live_report import parse_session, CLAUDE_PROJECTS
from founder_runtime.prediction_math import (
    McpSurprisal, beta_posterior, ensemble_p, reweight_from_studio_db,
)

STATE = Path.home() / ".rig" / "state"
BRIDGE_STATE = STATE / "prediction-bridge.json"
IDLE_RESOLVE_SEC = 1800  # 30 min idle = session over
STUDIO_DB = "/Users/rig128gb/Developer/RIGForge/repos/rig-prediction-studio-pro/data/brier_calibration.db"

_surprisal_client = None


def _seal_resolution(p_true: float, outcome: int) -> dict | None:
    """Score the resolution with sealed surprisal via sympy MCP."""
    global _surprisal_client
    try:
        if _surprisal_client is None:
            _surprisal_client = McpSurprisal()
        # surprisal of the OBSERVED outcome's assigned probability
        p_observed = p_true if outcome == 1 else (1 - p_true)
        return _surprisal_client.surprisal(max(0.03, min(0.97, p_observed)))
    except Exception as e:
        return {"seal_error": str(e)}


def _prefix_tests(sig: dict, frac: float) -> int:
    """Test phases visible at frac through the session timeline."""
    n = max(1, int(len(sig["phases"]) * frac))
    return sum(1 for ph, _ in sig["phases"][:n] if ph == "test")


def empirical_base_rate() -> dict:
    """Feature-conditional Beta posterior for P(session ends testless),
    computed exactly from the full parsed history. Bucket: sessions with
    0 tests visible at the 50% timeline mark (matching live-prediction
    conditions)."""
    succ = fail = 0
    for p in CLAUDE_PROJECTS.rglob("*.jsonl"):
        if "subagents" in p.parts:
            continue
        try:
            sig = parse_session(p)
        except Exception:
            continue
        if len(sig["phases"]) < 4 or sig["duration_min"] < 2:
            continue
        if _prefix_tests(sig, 0.5) == 0:  # the conditional bucket
            if sig["test_runs"] == 0:
                succ += 1  # ended testless
            else:
                fail += 1    # recovered and tested
    return beta_posterior(succ, fail)


def load_bridge_state() -> dict:
    if BRIDGE_STATE.exists():
        try:
            return json.loads(BRIDGE_STATE.read_text())
        except Exception:
            pass
    return {"open": {}}  # question -> {prediction_id, resolver, payload, created}


def save_bridge_state(st: dict) -> None:
    import os as _os
    tmp = BRIDGE_STATE.with_name(BRIDGE_STATE.stem + f"-{_os.getpid()}.tmp")
    tmp.write_text(json.dumps(st, indent=2))
    tmp.replace(BRIDGE_STATE)


def active_sessions() -> list[dict]:
    now = time.time()
    out = []
    for p in CLAUDE_PROJECTS.rglob("*.jsonl"):
        if "subagents" in p.parts:
            continue
        try:
            if now - p.stat().st_mtime > 6 * 3600:
                continue
        except OSError:
            continue
        sig = parse_session(p)
        if len(sig["phases"]) >= 4:
            sig["_file"] = p
            out.append(sig)
    return out


def _iso_week() -> str:
    from datetime import datetime, timezone
    y, w, _ = datetime.now(timezone.utc).isocalendar()
    return f"{y}-W{w:02d}"


def _project_to_repo(project: str) -> Path | None:
    """Map Claude project dir encoding back to a repo path, tolerating
    dashes in directory names by longest-prefix existence checks."""
    raw = project.lstrip("-")
    if not raw:
        return None
    parts = raw.split("-")
    # greedy walk: at each level, join segments with '-' until a dir matches
    cur = Path("/")
    i = 0
    while i < len(parts):
        matched = None
        for j in range(len(parts), i, -1):
            cand = "-".join(parts[i:j])
            if (cur / cand).is_dir():
                matched = (cur / cand, j)
                break
        if matched is None:
            return None
        cur, i = matched
    return cur if (cur / ".git").exists() else None

OVERRIDES_LOG = STATE / "jake-overrides.jsonl"


def _repo_for_path(path: str | Path) -> Path | None:
    """Walk parents of a filesystem path until a .git dir is found."""
    try:
        cur = Path(path).expanduser().resolve()
    except Exception:
        return None
    if cur.is_file():
        cur = cur.parent
    for p in [cur, *cur.parents]:
        if (p / ".git").exists():
            return p
    return None


def _last_bash_outputs(session_file: Path, n: int = 3) -> list[str]:
    """Return text of the last n Bash tool_result payloads from a session jsonl."""
    bash_ids: dict[str, None] = {}
    results: list[tuple[int, str]] = []
    try:
        lines = session_file.read_text(errors="ignore").splitlines()
    except OSError:
        return []
    for idx, line in enumerate(lines):
        try:
            d = json.loads(line)
        except Exception:
            continue
        t = d.get("type")
        if t == "assistant":
            for c in (d.get("message", {}) or {}).get("content") or []:
                if isinstance(c, dict) and c.get("type") == "tool_use" and c.get("name") == "Bash":
                    tid = c.get("id")
                    if tid:
                        bash_ids[tid] = None
        elif t == "user":
            content = (d.get("message", {}) or {}).get("content")
            if not isinstance(content, list):
                continue
            for c in content:
                if not isinstance(c, dict) or c.get("type") != "tool_result":
                    continue
                if c.get("tool_use_id") not in bash_ids:
                    continue
                out = c.get("content")
                if isinstance(out, list):
                    txt = " ".join(
                        str(x.get("text", x) if isinstance(x, dict) else x) for x in out
                    )
                else:
                    txt = str(out or "")
                if c.get("is_error"):
                    txt = f"error {txt}"
                results.append((idx, txt))
    return [t for _, t in results[-n:]]


def _recent_wrong_rule_overrides(max_age_sec: float = 48 * 3600) -> list[dict]:
    """Load recent secret-file wrong_rule overrides from the Jake overrides log."""
    if not OVERRIDES_LOG.exists():
        return []
    now = time.time()
    out: list[dict] = []
    try:
        for line in OVERRIDES_LOG.read_text(errors="ignore").splitlines():
            try:
                d = json.loads(line)
            except Exception:
                continue
            if d.get("reason") != "wrong_rule":
                continue
            if d.get("outcome") == "blocked":
                continue
            ts = d.get("ts") or d.get("timestamp")
            try:
                from datetime import datetime
                if isinstance(ts, (int, float)):
                    tsv = float(ts)
                else:
                    tsv = datetime.fromisoformat(str(ts).replace("Z", "+00:00")).timestamp()
            except Exception:
                continue
            # Keep a little history beyond the 48h horizon for generation
            if now - tsv > max_age_sec * 2:
                continue
            d["_ts"] = tsv
            out.append(d)
    except OSError:
        return []
    return out



def generate_questions(studio: PredictionStudioV2, bridge: dict) -> list[dict]:
    """Create swarm predictions for active sessions that don't have one open."""
    made = []
    open_questions = set(bridge["open"].keys())
    base = empirical_base_rate()  # exact Beta posterior from full history

    # ---- fleet_ratio: one open question per ISO week ----
    week = _iso_week()
    fq = f"Fleet read:edit ratio stays below 0.25 in {week}"
    if fq not in open_questions:
        result = studio.predict(
            question=fq + ". Context: fleet-wide read+search phases vs edit phases "
                     f"across all coding sessions this ISO week.",
            horizon_days=7,
        )
        bridge["open"][fq] = {
            "prediction_id": result["prediction_id"],
            "p_true": result["p_true"],
            "resolver": "fleet_ratio",
            "payload": {"week": week},
            "created": time.time(),
        }
        made.append({"question": fq, "p_true": result["p_true"]})

    # ---- project_rework: drift-heavy projects, revert-check in 7d ----
    for sig in active_sessions():
        proj_dir = sig["project"]
        if not proj_dir or proj_dir == "-":
            continue
        repo = _project_to_repo(proj_dir)
        if repo is None:
            continue
        drift_sessions = [s for s in active_sessions()
                          if s["project"] == proj_dir
                          and len(s["files_modified"]) >= 4 and s["test_runs"] == 0]
        if len(drift_sessions) < 2:
            continue
        rq = f"Project {repo.name} changes from {week} get reverted/fixed within 7 days"
        if rq in bridge["open"]:  # live check, not the stale snapshot
            continue
        result = studio.predict(
            question=rq + f". Context: {len(drift_sessions)} sessions this week touched "
                          f"4+ files with zero test runs in repo {repo}.",
            horizon_days=7,
        )
        bridge["open"][rq] = {
            "prediction_id": result["prediction_id"],
            "p_true": result["p_true"],
            "resolver": "project_rework",
            "payload": {"repo": str(repo), "week": week},
            "created": time.time(),
        }
        made.append({"question": rq, "p_true": result["p_true"]})

    for sig in active_sessions():
        sid = sig["session_id"][:12]
        q = f"Session {sid} ends without a test run"
        if q in open_questions:
            continue

        # Only predict where there's real signal: >=5 min OR >=10 phases in
        if sig["duration_min"] < 5 and len(sig["phases"]) < 10:
            continue

        tests = sig["test_runs"]
        # CRITICAL: only ask "ends testless" while the outcome is actually open.
        # A session that already ran tests can only resolve outcome=0 — asking
        # manufactures a predetermined loss and poisons the calibration record.
        if tests > 0:
            continue
        files = len(sig["files_modified"])
        edits = sum(1 for ph, _ in sig["phases"] if ph == "edit")
        context = (
            f"Live coding session: {sig['duration_min']}min elapsed, "
            f"{len(sig['phases'])} tool calls, {files} files touched, "
            f"{edits} edits, {tests} test runs so far. "
            f"Project dir: {sig['project']}. "
            f"Empirical base rate for this bucket: {base['mean_exact']} "
            f"(95% CI {base['ci95'][0]:.2f}-{base['ci95'][1]:.2f}, n={base['n']})."
        )
        result = studio.predict(
            question=f"{q}. Context: {context}",
            horizon_days=1,
        )
        bridge["open"][q] = {
            "prediction_id": result["prediction_id"],
            "p_true": result["p_true"],
            "p_base_rate": base["mean"],  # exact empirical anchor
            "resolver": "session_testless",
            "payload": {"session_file": str(sig["_file"]), "session_id": sig["session_id"]},
            "created": time.time(),
            "context": context,
        }
        made.append({"question": q, "p_true": result["p_true"],
                     "p_base_rate": base["mean"],
                     "confidence": result.get("confidence")})

    # ---- ci_result: active sessions mapped to a CI-capable repo (24h) ----
    for sig in active_sessions():
        proj_dir = sig["project"]
        if not proj_dir or proj_dir == "-":
            continue
        repo = _project_to_repo(proj_dir)
        if repo is None:
            continue
        ci_capable = any((repo / p).exists() for p in (
            ".ci-pass", "ci.sh", "script/cibuild", ".github/workflows",
            "Makefile", "package.json", "pyproject.toml",
        ))
        if not ci_capable:
            continue
        sid = sig["session_id"][:12]
        cq = f"Session {sid}'s changes pass CI within 24h"
        if cq in bridge["open"] or cq in open_questions:
            continue
        if sig["duration_min"] < 3 and len(sig["phases"]) < 6:
            continue
        result = studio.predict(
            question=cq + f". Context: session in repo {repo.name}, "
                          f"{len(sig['files_modified'])} files touched, "
                          f"{sig['duration_min']}min elapsed.",
            horizon_days=1,
        )
        bridge["open"][cq] = {
            "prediction_id": result["prediction_id"],
            "p_true": result["p_true"],
            "resolver": "ci_result",
            "payload": {
                "session_file": str(sig["_file"]),
                "session_id": sig["session_id"],
                "repo": str(repo),
            },
            "created": time.time(),
        }
        made.append({"question": cq, "p_true": result["p_true"]})

    # ---- build_failure: sessions with bash activity ----
    for sig in active_sessions():
        bash_n = sum(1 for ph, _ in sig["phases"] if ph == "bash")
        if bash_n < 1:
            continue
        sid = sig["session_id"][:12]
        bq = f"Session {sid} ends with a failing build"
        if bq in bridge["open"] or bq in open_questions:
            continue
        if sig["duration_min"] < 3 and len(sig["phases"]) < 6:
            continue
        result = studio.predict(
            question=bq + f". Context: {bash_n} bash phases so far, "
                          f"{len(sig['files_modified'])} files touched, "
                          f"project {sig['project']}.",
            horizon_days=1,
        )
        bridge["open"][bq] = {
            "prediction_id": result["prediction_id"],
            "p_true": result["p_true"],
            "resolver": "build_failure",
            "payload": {
                "session_file": str(sig["_file"]),
                "session_id": sig["session_id"],
            },
            "created": time.time(),
        }
        made.append({"question": bq, "p_true": result["p_true"]})

    # ---- revert_follow: edited projects, 7d revert check ----
    for sig in active_sessions():
        proj_dir = sig["project"]
        if not proj_dir or proj_dir == "-":
            continue
        repo = _project_to_repo(proj_dir)
        if repo is None:
            continue
        edits = sum(1 for ph, _ in sig["phases"] if ph == "edit")
        if edits < 1 and len(sig["files_modified"]) < 1:
            continue
        rq2 = f"Project {repo.name} changes get reverted within 7 days"
        if rq2 in bridge["open"]:
            continue
        result = studio.predict(
            question=rq2 + f". Context: live session touched {len(sig['files_modified'])} "
                           f"files with {edits} edits in {repo}.",
            horizon_days=7,
        )
        bridge["open"][rq2] = {
            "prediction_id": result["prediction_id"],
            "p_true": result["p_true"],
            "resolver": "revert_follow",
            "payload": {"repo": str(repo), "project": proj_dir},
            "created": time.time(),
        }
        made.append({"question": rq2, "p_true": result["p_true"]})

    # ---- stale_session: low-progress live sessions ----
    for sig in active_sessions():
        files_n = len(sig["files_modified"])
        if files_n >= 2:
            continue  # already has progress; stale-abandon signal weak
        last_phase = sig["phases"][-1][0] if sig["phases"] else ""
        if last_phase == "test":
            continue  # completed toward test
        sid = sig["session_id"][:12]
        sq = f"Session {sid} becomes stale (no progress 30+ min) without completing"
        if sq in bridge["open"] or sq in open_questions:
            continue
        if len(sig["phases"]) < 4:
            continue
        result = studio.predict(
            question=sq + f". Context: {sig['duration_min']}min, {files_n} files, "
                          f"last_phase={last_phase}, {len(sig['phases'])} tool calls.",
            horizon_days=1,
        )
        bridge["open"][sq] = {
            "prediction_id": result["prediction_id"],
            "p_true": result["p_true"],
            "resolver": "stale_session",
            "payload": {
                "session_file": str(sig["_file"]),
                "session_id": sig["session_id"],
            },
            "created": time.time(),
        }
        made.append({"question": sq, "p_true": result["p_true"]})

    # ---- block_follow_revert: secret-file wrong_rule overrides ----
    for ov in _recent_wrong_rule_overrides():
        path = ov.get("target_path") or ov.get("path") or ""
        if not path:
            continue
        repo = _repo_for_path(path)
        oq = f"Secret-file override on {path} gets reverted within 48h"
        if oq in bridge["open"]:
            continue
        result = studio.predict(
            question=oq + f". Context: wrong_rule override session={ov.get('session_id')} "
                          f"detector={ov.get('detector_id')} repo={repo}.",
            horizon_days=2,
        )
        bridge["open"][oq] = {
            "prediction_id": result["prediction_id"],
            "p_true": result["p_true"],
            "resolver": "block_follow_revert",
            "payload": {
                "path": path,
                "repo": str(repo) if repo else None,
                "override_ts": ov.get("_ts"),
                "session_id": ov.get("session_id"),
            },
            "created": time.time(),
        }
        made.append({"question": oq, "p_true": result["p_true"]})


    if made:
        save_bridge_state(bridge)
    return made


def resolve_due(studio: PredictionStudioV2, bridge: dict) -> list[dict]:
    """Auto-resolve predictions whose outcome condition is now observable."""
    resolved = []
    now = time.time()

    for q, rec in list(bridge["open"].items()):
        rtype = rec["resolver"]

        if rtype == "ci_result":
            # Resolve at 24h horizon (or earlier if a pass marker already exists)
            repo = Path(rec["payload"].get("repo") or "")
            marker = repo / ".ci-pass" if str(repo) not in (".", "") else None
            early_pass = bool(marker and marker.exists())
            if not early_pass and now - rec["created"] < 24 * 3600:
                continue
            outcome = 0
            evidence = "no-ci-signal"
            if early_pass:
                outcome = 1
                evidence = str(marker)
            elif repo and repo.exists():
                import subprocess as _sp
                try:
                    since = time.strftime("%Y-%m-%d", time.gmtime(rec["created"]))
                    tag_out = _sp.run(
                        ["git", "-C", str(repo), "log", f"--since={since}",
                         "--grep=ci", "-i", "--oneline"],
                        capture_output=True, timeout=10, text=True,
                    )
                    lines = [l for l in tag_out.stdout.splitlines() if l.strip()]
                    if any(
                        k in l.lower()
                        for l in lines
                        for k in ("ci pass", "ci-pass", "ci green", "ci: pass", "passed ci")
                    ):
                        outcome = 1
                        evidence = "git-log-ci-pass"
                    elif lines:
                        evidence = "git-log-ci-mention"
                except Exception:
                    evidence = "git-log-error"
                if (repo / ".ci-pass").exists():
                    outcome = 1
                    evidence = str(repo / ".ci-pass")
            try:
                studio.record_outcome(
                    prediction_id=rec["prediction_id"], outcome=outcome,
                    notes=f"auto-resolved ci_result: {evidence}",
                )
            except TypeError:
                studio.record_outcome(rec["prediction_id"], outcome)
            seal = _seal_resolution(rec["p_true"], outcome)
            resolved.append({"question": q, "p_true": rec["p_true"], "outcome": outcome,
                             "evidence": evidence, "surprisal_proof": seal})
            del bridge["open"][q]
            continue

        if rtype == "build_failure":
            f = Path(rec["payload"]["session_file"])
            try:
                idle = now - f.stat().st_mtime
            except OSError:
                idle = IDLE_RESOLVE_SEC + 1
            if idle < IDLE_RESOLVE_SEC:
                continue  # session still live
            outputs = _last_bash_outputs(f, n=3)
            hit = False
            keys = ("error", "failed", "traceback")
            for txt in outputs:
                low = txt.lower()
                if any(k in low for k in keys):
                    hit = True
                    break
            outcome = 1 if hit else 0
            try:
                studio.record_outcome(
                    prediction_id=rec["prediction_id"], outcome=outcome,
                    notes=f"auto-resolved build_failure: hit={hit} bash_outputs={len(outputs)} idle={idle/60:.0f}min",
                )
            except TypeError:
                studio.record_outcome(rec["prediction_id"], outcome)
            seal = _seal_resolution(rec["p_true"], outcome)
            resolved.append({"question": q, "p_true": rec["p_true"], "outcome": outcome,
                             "bash_scanned": len(outputs), "surprisal_proof": seal})
            del bridge["open"][q]
            continue

        if rtype == "revert_follow":
            if now - rec["created"] < 7 * 86400:
                continue  # 7-day horizon not reached
            repo = Path(rec["payload"]["repo"])
            if not repo.exists():
                del bridge["open"][q]
                continue
            import subprocess as _sp
            try:
                out = _sp.run(
                    ["git", "-C", str(repo), "log",
                     f"--since={time.strftime('%Y-%m-%d', time.gmtime(rec['created']))}",
                     "--grep=revert", "-i", "--oneline"],
                    capture_output=True, timeout=10, text=True,
                )
                hits = len([l for l in out.stdout.splitlines() if l.strip()])
            except Exception:
                continue
            outcome = 1 if hits > 0 else 0
            try:
                studio.record_outcome(
                    prediction_id=rec["prediction_id"], outcome=outcome,
                    notes=f"auto-resolved revert_follow: {hits} revert commits in {repo.name}",
                )
            except TypeError:
                studio.record_outcome(rec["prediction_id"], outcome)
            seal = _seal_resolution(rec["p_true"], outcome)
            resolved.append({"question": q, "p_true": rec["p_true"], "outcome": outcome,
                             "revert_commits": hits, "surprisal_proof": seal})
            del bridge["open"][q]
            continue

        if rtype == "stale_session":
            f = Path(rec["payload"]["session_file"])
            try:
                mtime = f.stat().st_mtime
                idle = now - mtime
            except OSError:
                # file gone after creation window => abandoned
                idle = IDLE_RESOLVE_SEC + 1
            # Need 30+ min idle (no progress) — reuse session_testless idle pattern
            if idle < IDLE_RESOLVE_SEC:
                continue
            try:
                sig = parse_session(f)
            except Exception:
                sig = {"phases": [], "files_modified": []}
            last_phase = sig["phases"][-1][0] if sig.get("phases") else ""
            files = len(sig.get("files_modified") or [])
            # stale abandoned: idle, never reached test, almost no file progress
            outcome = 1 if (last_phase != "test" and files < 2) else 0
            try:
                studio.record_outcome(
                    prediction_id=rec["prediction_id"], outcome=outcome,
                    notes=f"auto-resolved stale_session: last_phase={last_phase} files={files} idle={idle/60:.0f}min",
                )
            except TypeError:
                studio.record_outcome(rec["prediction_id"], outcome)
            seal = _seal_resolution(rec["p_true"], outcome)
            resolved.append({"question": q, "p_true": rec["p_true"], "outcome": outcome,
                             "last_phase": last_phase, "files": files,
                             "surprisal_proof": seal})
            del bridge["open"][q]
            continue

        if rtype == "block_follow_revert":
            if now - rec["created"] < 48 * 3600:
                continue  # 48h horizon not reached
            path = rec["payload"].get("path") or ""
            repo_s = rec["payload"].get("repo")
            repo = Path(repo_s) if repo_s else _repo_for_path(path)
            hits = 0
            if repo and Path(repo).exists():
                import subprocess as _sp
                try:
                    since = time.strftime(
                        "%Y-%m-%d",
                        time.gmtime(rec["payload"].get("override_ts") or rec["created"]),
                    )
                    cmd = ["git", "-C", str(repo), "log", f"--since={since}",
                           "--grep=revert", "-i", "--oneline"]
                    try:
                        rel = str(Path(path).resolve().relative_to(Path(repo).resolve()))
                        cmd.extend(["--", rel])
                    except Exception:
                        pass
                    out = _sp.run(cmd, capture_output=True, timeout=10, text=True)
                    hits = len([l for l in out.stdout.splitlines() if l.strip()])
                except Exception:
                    hits = 0
            outcome = 1 if hits > 0 else 0
            try:
                studio.record_outcome(
                    prediction_id=rec["prediction_id"], outcome=outcome,
                    notes=f"auto-resolved block_follow_revert: path={path} hits={hits}",
                )
            except TypeError:
                studio.record_outcome(rec["prediction_id"], outcome)
            seal = _seal_resolution(rec["p_true"], outcome)
            resolved.append({"question": q, "p_true": rec["p_true"], "outcome": outcome,
                             "path": path, "revert_commits": hits,
                             "surprisal_proof": seal})
            del bridge["open"][q]
            continue


        if rtype == "fleet_ratio":
            # Resolve when the ISO week has ended
            if rec["payload"].get("week") == _iso_week():
                continue  # week still running
            # Aggregate the ended week's read:edit from all sessions seen then
            # (approximation: sessions modified during that week)
            reads = edits = 0
            from datetime import datetime, timezone
            for p in CLAUDE_PROJECTS.rglob("*.jsonl"):
                if "subagents" in p.parts:
                    continue
                try:
                    y, w, _ = datetime.fromtimestamp(
                        p.stat().st_mtime, timezone.utc).isocalendar()
                    if f"{y}-W{w:02d}" != rec["payload"]["week"]:
                        continue
                except OSError:
                    continue
                try:
                    sig = parse_session(p)
                except Exception:
                    continue
                for ph, _ in sig["phases"]:
                    if ph in ("read", "search"):
                        reads += 1
                    elif ph == "edit":
                        edits += 1
            if edits == 0:
                continue
            ratio = reads / edits
            outcome = 1 if ratio < 0.25 else 0
            try:
                studio.record_outcome(prediction_id=rec["prediction_id"], outcome=outcome,
                                      notes=f"auto-resolved: week {rec['payload']['week']} read:edit={ratio:.3f}")
            except TypeError:
                studio.record_outcome(rec["prediction_id"], outcome)
            seal = _seal_resolution(rec["p_true"], outcome)
            resolved.append({"question": q, "p_true": rec["p_true"], "outcome": outcome,
                             "ratio": round(ratio, 3), "surprisal_proof": seal})
            del bridge["open"][q]
            continue

        if rtype == "project_rework":
            if now - rec["created"] < 7 * 86400:
                continue  # 7-day horizon not reached
            repo = Path(rec["payload"]["repo"])
            if not repo.exists():
                del bridge["open"][q]
                continue
            import subprocess as _sp
            try:
                out = _sp.run(
                    ["git", "-C", str(repo), "log",
                     f"--since={time.strftime('%Y-%m-%d', time.gmtime(rec['created']))}",
                     "--grep=revert\\|fixup\\|oops\\|broken\\|hotfix", "-i",
                     "--oneline"],
                    capture_output=True, timeout=10, text=True,
                )
                hits = len([l for l in out.stdout.splitlines() if l.strip()])
            except Exception:
                continue
            outcome = 1 if hits > 0 else 0
            try:
                studio.record_outcome(prediction_id=rec["prediction_id"], outcome=outcome,
                                      notes=f"auto-resolved: {hits} revert/fix commits in {repo.name} since prediction")
            except TypeError:
                studio.record_outcome(rec["prediction_id"], outcome)
            seal = _seal_resolution(rec["p_true"], outcome)
            resolved.append({"question": q, "p_true": rec["p_true"], "outcome": outcome,
                             "revert_commits": hits, "surprisal_proof": seal})
            del bridge["open"][q]
            continue

        if rtype == "collision_conflict":
            # Question: "Sessions A and B conflict on file F within 24h"
            # outcome=1 if collision-log shows both sessions edited the same file
            # in the window; outcome=0 if only A edited. Pattern mirrors
            # session_testless (payload-driven, evidence from on-disk log).
            payload = rec.get("payload") or {}
            sid_a = str(payload.get("session_a") or payload.get("session_id") or "")
            sid_b = str(payload.get("session_b") or "")
            target = str(payload.get("file") or payload.get("shared_file") or "")
            created = float(rec.get("created") or 0)
            window_end = created + 24 * 3600 if created else now
            # Only resolve once the 24h observation window has closed,
            # unless the log already proves a bilateral hit.
            from founder_runtime.collision_gate import load_collision_log
            since = created - 3600 if created else now - 24 * 3600
            hits = load_collision_log(since_epoch=since)
            both = False
            only_a = False
            for h in hits:
                sa = str(h.get("session_a") or "")
                sb = str(h.get("session_b") or "")
                pair = {sa, sb}
                shared = [str(f) for f in (h.get("shared_files") or [])]
                if target and target not in shared and shared:
                    # if a specific file was predicted, require it in the log
                    continue
                if sid_a and sid_b and sid_a in pair and sid_b in pair:
                    both = True
                    break
                if sid_a and sid_a in pair and (not sid_b or sid_b not in pair):
                    only_a = True
            if both:
                outcome = 1
            elif only_a and (now >= window_end or created and now - created >= 24 * 3600):
                outcome = 0
            elif not both and not only_a and created and now - created >= 24 * 3600:
                # window closed, no bilateral evidence -> 0 (only A / no conflict)
                outcome = 0
            else:
                continue  # still open
            notes = (
                f"auto-resolved collision_conflict: both={both} only_a={only_a} "
                f"file={target or '*'} a={sid_a[:12]} b={sid_b[:12]}"
            )
            try:
                studio.record_outcome(
                    prediction_id=rec["prediction_id"], outcome=outcome, notes=notes,
                )
            except TypeError:
                studio.record_outcome(rec["prediction_id"], outcome)
            seal = _seal_resolution(rec["p_true"], outcome)
            resolved.append({
                "question": q,
                "p_true": rec["p_true"],
                "outcome": outcome,
                "correct": (rec["p_true"] > 0.5) == bool(outcome),
                "surprisal_proof": seal,
                "evidence": notes,
            })
            del bridge["open"][q]
            continue

        if rtype != "session_testless":
            continue
        f = Path(rec["payload"]["session_file"])
        try:
            idle = now - f.stat().st_mtime
        except OSError:
            idle = IDLE_RESOLVE_SEC + 1  # file gone = session over

        if idle < IDLE_RESOLVE_SEC:
            continue  # still live

        sig = parse_session(f)
        outcome = 1 if sig["test_runs"] == 0 else 0
        try:
            studio.record_outcome(
                prediction_id=rec["prediction_id"],
                outcome=outcome,
                notes=f"auto-resolved: test_runs={sig['test_runs']} after {idle/60:.0f}min idle",
            )
        except TypeError:
            studio.record_outcome(rec["prediction_id"], outcome)

        # Seal the score with exact surprisal via sympy MCP
        seal = _seal_resolution(rec["p_true"], outcome)

        resolved.append({
            "question": q,
            "p_true": rec["p_true"],
            "outcome": outcome,
            "correct": (rec["p_true"] > 0.5) == bool(outcome),
            "surprisal_proof": seal,
        })
        del bridge["open"][q]

    if resolved:
        save_bridge_state(bridge)
    return resolved


def main() -> int:
    studio = PredictionStudioV2()
    bridge = load_bridge_state()

    resolve_only = "--resolve-only" in sys.argv
    resolved = resolve_due(studio, bridge)
    made = [] if resolve_only else generate_questions(studio, bridge)

    out = {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "predictions_made": made,
        "predictions_resolved": resolved,
        "open_count": len(bridge["open"]),
    }
    print(json.dumps(out, indent=2))

    # Calibration snapshot if anything has ever resolved
    try:
        cal = studio.calibration_report()
        if cal:
            print("=== CALIBRATION ===")
            print(json.dumps(cal, indent=2, default=str)[:2000])
    except Exception:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
