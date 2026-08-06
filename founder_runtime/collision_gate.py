#!/usr/bin/env python3
"""
Collision Gate — block concurrent sessions from overwriting the same files.

Fenn's detector already surfaces collision pairs; Jake only warned. This module
enforces the block and leaves an evidence trail for the prediction bridge.

Public API:
  collision_pairs(sessions) -> list[(session_a, session_b, shared_files)]
  should_block(session_id, files_to_write, active_sessions, blocking_enabled=True)
      -> (bool, reason)
  record_collision(a, b, shared) -> appends ~/.rig/state/collision-log.jsonl

CLI:
  python -m founder_runtime.collision_gate
  prints top 10 current collision pairs (session ids + shared file count).
"""
from __future__ import annotations

import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from founder_runtime.jake_live_report import (  # noqa: E402
    ACTIVE_WINDOW_SEC,
    CLAUDE_PROJECTS,
    parse_session,
)

STATE = Path.home() / ".rig" / "state"
COLLISION_LOG = STATE / "collision-log.jsonl"
ACTIVE_WINDOW = ACTIVE_WINDOW_SEC  # 6h


def _session_id(s: dict) -> str:
    return str(s.get("session_id") or s.get("id") or "")


def _files(s: dict) -> set[str]:
    raw = s.get("files_modified") or s.get("files") or []
    out: set[str] = set()
    for f in raw:
        fp = str(f)
        if not fp or fp.startswith("bash:"):
            continue
        out.add(fp)
    return out


def _is_active(s: dict, now: float | None = None, window: float = ACTIVE_WINDOW) -> bool:
    """Active if mtime or last_ts falls inside the window."""
    now = now if now is not None else time.time()
    mtime = s.get("mtime")
    if isinstance(mtime, (int, float)) and mtime > 0:
        return (now - float(mtime)) <= window
    last_ts = s.get("last_ts")
    if last_ts:
        try:
            ts = datetime.fromisoformat(str(last_ts).replace("Z", "+00:00"))
            return (now - ts.timestamp()) <= window
        except Exception:
            pass
    # Unknown freshness: treat as active (caller already filtered)
    return True


def collision_pairs(sessions: Iterable[dict]) -> list[tuple[dict, dict, list[str]]]:
    """Pairs of sessions both active within 6h that share modified files.

    Returns list of (session_a, session_b, shared_files) sorted by overlap
    count descending. shared_files is a sorted list for stable output.
    """
    now = time.time()
    active = [s for s in sessions if _is_active(s, now)]
    pairs: list[tuple[dict, dict, list[str]]] = []
    for i, a in enumerate(active):
        fa = _files(a)
        if not fa:
            continue
        for b in active[i + 1 :]:
            if _session_id(a) and _session_id(a) == _session_id(b):
                continue
            shared = fa & _files(b)
            if shared:
                pairs.append((a, b, sorted(shared)))
    pairs.sort(key=lambda t: len(t[2]), reverse=True)
    return pairs


def should_block(
    session_id: str,
    files_to_write: Iterable[str],
    active_sessions: Iterable[dict],
    blocking_enabled: bool = True,
) -> tuple[bool, str]:
    """Return (True, reason) if another active session already modified any target file."""
    if not blocking_enabled:
        return False, ""
    targets = {str(f) for f in files_to_write if f and not str(f).startswith("bash:")}
    if not targets:
        return False, ""
    sid = str(session_id or "")
    now = time.time()
    for other in active_sessions:
        oid = _session_id(other)
        if not oid or oid == sid:
            continue
        if not _is_active(other, now):
            continue
        hit = targets & _files(other)
        if hit:
            return True, f"collision with session {oid}"
    return False, ""


def record_collision(a: Any, b: Any, shared: Iterable[str]) -> dict:
    """Append a collision event to the evidence log. Returns the record written."""
    def _id(x: Any) -> str:
        if isinstance(x, dict):
            return _session_id(x)
        return str(x or "")

    rec = {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "epoch": time.time(),
        "session_a": _id(a),
        "session_b": _id(b),
        "shared_files": sorted({str(f) for f in shared if f}),
        "evidence": "both sessions list overlapping files_modified while active within 6h",
    }
    STATE.mkdir(parents=True, exist_ok=True)
    line = json.dumps(rec, separators=(",", ":")) + "\n"
    # atomic-ish append: write via per-PID tmp then append to log
    tmp = COLLISION_LOG.with_name(COLLISION_LOG.stem + f"-{os.getpid()}.tmp")
    try:
        tmp.write_text(line)
        with COLLISION_LOG.open("a") as fh:
            fh.write(tmp.read_text())
    finally:
        try:
            tmp.unlink(missing_ok=True)
        except TypeError:
            if tmp.exists():
                tmp.unlink()
    return rec


def load_collision_log(since_epoch: float | None = None) -> list[dict]:
    """Read collision-log.jsonl; optional lower bound on epoch/ts."""
    if not COLLISION_LOG.exists():
        return []
    out: list[dict] = []
    for line in COLLISION_LOG.open(errors="ignore"):
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except Exception:
            continue
        ep = rec.get("epoch")
        if ep is None and rec.get("ts"):
            try:
                ep = datetime.fromisoformat(
                    str(rec["ts"]).replace("Z", "+00:00")
                ).timestamp()
            except Exception:
                ep = None
        if since_epoch is not None and ep is not None and float(ep) < since_epoch:
            continue
        out.append(rec)
    return out


def load_active_sessions(window: float = ACTIVE_WINDOW) -> list[dict]:
    """Parse live Claude transcripts active within window (default 6h)."""
    now = time.time()
    out: list[dict] = []
    if not CLAUDE_PROJECTS.exists():
        return out
    for p in CLAUDE_PROJECTS.rglob("*.jsonl"):
        if "subagents" in p.parts:
            continue
        try:
            if now - p.stat().st_mtime > window:
                continue
        except OSError:
            continue
        try:
            sig = parse_session(p)
        except Exception:
            continue
        if not sig.get("files_modified"):
            continue
        sig["_file"] = str(p)
        out.append(sig)
    return out


def main() -> int:
    sessions = load_active_sessions()
    pairs = collision_pairs(sessions)
    top = pairs[:10]
    print(
        f"collision_gate: {len(sessions)} active sessions, "
        f"{len(pairs)} collision pairs (showing top {len(top)})"
    )
    for i, (a, b, shared) in enumerate(top, 1):
        sa = _session_id(a)[:12] or "?"
        sb = _session_id(b)[:12] or "?"
        sample = ", ".join(shared[:3])
        more = f" (+{len(shared) - 3})" if len(shared) > 3 else ""
        print(f"  {i:2d}. {sa} x {sb}  overlap={len(shared)}  files=[{sample}{more}]")
    if not top:
        print("  (none)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
