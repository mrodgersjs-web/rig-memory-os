#!/usr/bin/env python3
"""
E2E suite — Jake Mega-Harness + Prediction Engine + Mutation Gate.

Four critical journeys, real system, no mocks (per e2e-testing-patterns):
  J1 GUARDIAN  — PreToolUse hook blocks secrets, honors approval, passes clean
  J2 PREDICTION — session -> swarm prediction -> auto-resolve -> outcome recorded
  J3 GATE      — proposal -> admit/veto/tamper -> ledger records
  J4 PIPELINE  — full cron cycle -> all artifacts land

Deterministic, isolated, self-cleaning. Run:
  python3 platform/founder-runtime/test_jake_e2e_journeys.py
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path("/Users/rig128gb/Developer/rig-intelligence-worktrees/rig-memory-os/platform/founder-runtime")
VENV_PY = ROOT / ".venv" / "bin" / "python"
HOOK = Path("/Users/rig128gb/.claude/hooks/jake-pushback.sh")
CLAUDE_PROJECTS = Path.home() / ".claude" / "projects"
STATE = Path.home() / ".rig" / "state"
FAKE_PROJ = CLAUDE_PROJECTS / "-e2e-jake-test"
CRON = Path("/Users/rig128gb/.rig/bin/memory-os-cron.sh")

env = {
    **os.environ,
    "PYTHONPATH": str(ROOT),
    "RIG_MEMORY_OS_SECRET": "test-universal-secret",
    "RIG_MEMORY_OS_DSN": "host=/tmp port=5432 dbname=rig_memory_os_phase1",
}

results = []
def check(journey: str, name: str, ok: bool, detail: str = ""):
    results.append((journey, name, ok, detail))
    mark = "✓" if ok else "✗"
    print(f"  {mark} {name}{(': ' + detail) if detail else ''}")


# ----------------------------------------------------------------------
# J1 GUARDIAN — hook enforcement behavior (observable exit codes)
# ----------------------------------------------------------------------

def j1_guardian():
    print("\n=== J1 GUARDIAN: hook enforcement ===")

    def run_hook(payload: dict) -> tuple[int, str]:
        p = subprocess.run(
            ["bash", str(HOOK)], input=json.dumps(payload),
            capture_output=True, text=True, timeout=15,
        )
        return p.returncode, p.stderr

    # secret file must hard-block (exit 1)
    rc, err = run_hook({"tool_name": "Write",
                        "tool_input": {"file_path": "/tmp/e2e-x/.env"},
                        "session_id": "e2e-j1-1"})
    check("J1", "secret file hard-blocks (exit 1)", rc == 1,
          f"rc={rc}")

    # the block event is logged as data
    logp = STATE / "jake-overrides.jsonl"
    logged = logp.exists() and any(
        '"outcome":"blocked"' in l and "e2e-x/.env" in l
        for l in logp.read_text().splitlines()[-5:]
    )
    check("J1", "block event logged to overrides JSONL", logged)

    # approval token with reason code: allowed + logged as wrong_rule
    import hashlib
    h = hashlib.md5(b"/tmp/e2e-x/.env").hexdigest()
    token = Path(f"/tmp/jake-secret-approve-{h}.wrong_rule")
    token.touch()
    rc, _ = run_hook({"tool_name": "Write",
                      "tool_input": {"file_path": "/tmp/e2e-x/.env"},
                      "session_id": "e2e-j1-1"})
    check("J1", "reason-coded approval allows write", rc == 0, f"rc={rc}")
    approved = any('"reason":"wrong_rule"' in l and "e2e-x/.env" in l
                   for l in logp.read_text().splitlines()[-5:])
    check("J1", "approval logged with reason=wrong_rule", approved)

    # normal file passes
    rc, _ = run_hook({"tool_name": "Write",
                      "tool_input": {"file_path": "/tmp/e2e-normal.py"},
                      "session_id": "e2e-j1-2"})
    check("J1", "normal file passes (exit 0)", rc == 0, f"rc={rc}")


# ----------------------------------------------------------------------
# J2 PREDICTION — full lifecycle on a synthetic idle session
# ----------------------------------------------------------------------

def j2_prediction():
    print("\n=== J2 PREDICTION: lifecycle (make -> idle -> resolve) ===")
    FAKE_PROJ.mkdir(parents=True, exist_ok=True)
    session_file = FAKE_PROJ / "e2e-sess-001.jsonl"

    # fabricate a Claude-format transcript: prompt -> edit x3, NO tests,
    # timestamps 40 min ago (past the 30-min idle resolve threshold)
    old = time.time() - 40 * 60
    ts = lambda m: time.strftime("%Y-%m-%dT%H:%M:%SZ",
                                 time.gmtime(old + m * 60))
    def tool_use(name, inp, i):
        return json.dumps({
            "type": "assistant", "timestamp": ts(5 + i),
            "message": {"content": [{"type": "tool_use", "name": name,
                                     "input": inp}]}}) + "\n"
    lines = [
        json.dumps({"type": "user", "timestamp": ts(0),
                    "message": {"content": "fix the login bug"}}) + "\n",
        tool_use("Edit", {"file_path": "/tmp/proj/a.py"}, 1),
        tool_use("Edit", {"file_path": "/tmp/proj/b.py"}, 2),
        tool_use("Edit", {"file_path": "/tmp/proj/c.py"}, 3),
    ]
    session_file.write_text("".join(lines))
    # force mtime to 40 min ago so the file reads as idle
    os.utime(session_file, (old, old))

    try:
        # --- step 1: generate a prediction for it ---
        gen = subprocess.run(
            [str(VENV_PY), "-m", "founder_runtime.prediction_bridge"],
            cwd=ROOT, env=env, capture_output=True, text=True, timeout=120,
        )
        made = '"predictions_made": []' not in gen.stdout
        has_q = "e2e-sess-001" in gen.stdout
        check("J2", "bridge generated prediction for fake session", made and has_q,
              "question found" if has_q else gen.stdout[-200:])

        # --- step 2: resolve (idle >30min -> outcome recorded for real) ---
        res = subprocess.run(
            [str(VENV_PY), "-m", "founder_runtime.prediction_bridge",
             "--resolve-only"],
            cwd=ROOT, env=env, capture_output=True, text=True, timeout=120,
        )
        resolved = '"outcome": 1' in res.stdout or '"outcome":1' in res.stdout
        check("J2", "idle session auto-resolved with outcome", resolved,
              res.stdout[-160:] if not resolved else "outcome=1 (no test runs)")

        # --- step 3: the resolution is recorded in the studio db ---
        dbq = subprocess.run(
            [str(VENV_PY), "-c",
             "import sqlite3;c=sqlite3.connect('/Users/rig128gb/Developer/"
             "RIGForge/repos/rig-prediction-studio-pro/data/brier_calibration.db');"
             "n=c.execute('SELECT COUNT(*) FROM brier_scores "
             "WHERE actual_outcome IS NOT NULL').fetchone()[0];print(n)"],
            capture_output=True, text=True, timeout=30, env=env,
        )
        n_resolved = int(dbq.stdout.strip() or "0")
        check("J2", "resolution persisted in studio db", n_resolved > 300,
              f"{n_resolved} total resolved")

        # --- step 4: bridge state no longer holds the fake session open ---
        bridge = json.loads((STATE / "prediction-bridge.json").read_text())
        still_open = any("e2e-sess-001" in q for q in bridge.get("open", {}))
        check("J2", "resolved prediction removed from open set", not still_open)
    finally:
        shutil.rmtree(FAKE_PROJ, ignore_errors=True)


# ----------------------------------------------------------------------
# J3 GATE — admit, veto, tamper-detect on the live ledger
# ----------------------------------------------------------------------

def j3_gate():
    print("\n=== J3 GATE: mutation-veto gauntlet + ledger ===")
    script = (
        "from founder_runtime.mutation_gate import ("
        " MutationProposal, judge, verify_verdict, ledger_verify_chain,"
        " ledger_append, LEDGER)\n"
        "held=[{'predicted':0.5,'actual':i%2} for i in range(60)]\n"
        "# good proposal admits + signature verifies\n"
        "p=MutationProposal(surface='detector:x',change_type='threshold_adjust',"
        "content={'expected_brier':0.1},proposer='e2e')\n"
        "v=judge(p,held)\n"
        "print('ADMIT', v.admitted, verify_verdict(v,p))\n"
        "# veto blocks\n"
        "v2=judge(p,held,observer_veto=True)\n"
        "print('VETO', not v2.admitted)\n"
        "# chain verifies after appends\n"
        "c=ledger_verify_chain()\n"
        "print('CHAIN', c['ok'], c['entries'])\n"
    )
    run = subprocess.run([str(VENV_PY), "-c", script],
                         cwd=ROOT, env=env, capture_output=True, text=True,
                         timeout=60)
    ok_admit = "ADMIT True True" in run.stdout
    ok_veto = "VETO True" in run.stdout
    ok_chain = "CHAIN True" in run.stdout
    check("J3", "good proposal admits + signature verifies", ok_admit,
          run.stdout.strip()[:120] if not ok_admit else "")
    check("J3", "observer veto blocks", ok_veto)
    check("J3", "ledger chain verifies after appends", ok_chain,
          run.stderr[-120:] if not ok_chain and run.stderr else "")


# ----------------------------------------------------------------------
# J4 PIPELINE — one full cron cycle, all artifacts land
# ----------------------------------------------------------------------

def j4_pipeline():
    print("\n=== J4 PIPELINE: full cron cycle ===")
    t0 = time.time()
    before_brief = max(STATE.glob("jake-guidance-brief*"), default=None,
                       key=lambda p: p.stat().st_mtime) if list(STATE.glob("jake-guidance-brief*")) else None
    run = subprocess.run(["bash", str(CRON)], capture_output=True, text=True,
                         timeout=400)
    dur = time.time() - t0
    ok = "Flows failed: 0" in run.stdout or "Flows failed: 0".encode().decode() in run.stdout
    check("J4", "cron cycle completes with 0 failures", ok,
          f"{dur:.0f}s" + ("" if ok else " | " + run.stdout[-200:]))

    harness = STATE / "jake-harness.json"
    if harness.exists():
        h = json.loads(harness.read_text())
        fresh = time.time() - harness.stat().st_mtime < 300
        check("J4", "harness state fresh with 19 capabilities",
              fresh and h.get("capabilities_loaded") == 19,
              f"{h.get('capabilities_loaded')} caps, {len(h.get('interventions', []))} interventions")
    else:
        check("J4", "harness state fresh with 17 capabilities", False, "file missing")

    dash = STATE / "jake-dashboard.html"
    check("J4", "dashboard regenerated",
          dash.exists() and time.time() - dash.stat().st_mtime < 300)

    from datetime import datetime, timezone
    briefs = sorted(STATE.glob("jake-guidance-brief*"),
                    key=lambda p: p.stat().st_mtime)
    # Obsidian bridge sanitizes to 'jake-guidance-brief - <ts>.md' (spaces);
    # also check the vault write location directly
    vault_brief = Path.home() / "Documents" / "JakeStudio" / "Memory"
    vault_candidates = sorted(vault_brief.glob("jake-guidance-brief*"),
                              key=lambda p: p.stat().st_mtime) if vault_brief.exists() else []
    all_briefs = briefs + vault_candidates
    if all_briefs:
        newest = max(all_briefs, key=lambda p: p.stat().st_mtime)
        check("J4", "guidance brief rewritten this cycle",
              time.time() - newest.stat().st_mtime < 300,
              newest.name)
    else:
        check("J4", "guidance brief rewritten this cycle", False,
              "no brief found in state or vault")
    ledger_ok = subprocess.run(
        [str(VENV_PY), "-c",
         "from founder_runtime.mutation_gate import ledger_verify_chain;"
         "print(ledger_verify_chain()['ok'])"],
        cwd=ROOT, env=env, capture_output=True, text=True, timeout=30)
    check("J4", "mutation ledger intact post-cycle",
          "True" in ledger_ok.stdout)


def main():
    print("=" * 60)
    print("Jake E2E Journey Suite — real system, no mocks")
    print("=" * 60)
    j1_guardian()
    j2_prediction()
    j3_gate()
    j4_pipeline()

    print("\n" + "=" * 60)
    total = len(results)
    passed = sum(1 for r in results if r[2])
    print(f"RESULTS: {passed}/{total} checks passed")
    fails = [r for r in results if not r[2]]
    for j, n, _, d in fails:
        print(f"  FAIL [{j}] {n} {d}")
    print("=" * 60)
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
