#!/usr/bin/env python3
"""
Memory OS History Ingestion — one-time (then incremental) bulk load.

Sources:
1. ALL Claude/OMP chat transcripts (~/.claude/projects/**/*.jsonl)
   - phase transitions -> Predictor transition model (persisted)
   - session summaries -> goal-loop-memory.db (layer=episodic)
2. ALL Obsidian notes (~/Documents/JakeStudio/**/*.md)
   - note metadata (title/folder/tags/links/mtime) -> goal-loop-memory.db
     (layer=semantic)

Idempotent: INSERT OR IGNORE on UNIQUE(layer, key); re-runs only add new data.
Resumable: checkpoint file tracks per-source completion.

Usage:
  PYTHONPATH=. RIG_MEMORY_OS_SECRET=test-universal-secret \
    .venv/bin/python -m founder_runtime.ingest_history [--source all|chats|obsidian]
"""
from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from founder_runtime.runtime import MemoryOSRuntime
from founder_runtime.jake_live_report import parse_session, classify_tool

CLAUDE_PROJECTS = Path.home() / ".claude" / "projects"
OBSIDIAN_VAULT = Path.home() / "Documents" / "JakeStudio"
GOAL_LOOP_DB = Path.home() / ".rig" / "state" / "goal-loop-memory.db"
CHECKPOINT = Path.home() / ".rig" / "state" / "ingest-history-checkpoint.json"

BATCH = 2000

TAG_RE = re.compile(r"(?<!\w)#([A-Za-z][\w/-]{1,40})")
LINK_RE = re.compile(r"\[\[([^\[\]|]{1,120})(?:\|[^\[\]]*)?\]\]")
FM_RE = re.compile(r"\A---\s*\n(.*?)\n---", re.S)
FM_TAGS_RE = re.compile(r"^tags:\s*(?:\[(.*?)\]|(.*?))$", re.M | re.S)


def load_checkpoint() -> dict:
    if CHECKPOINT.exists():
        try:
            return json.loads(CHECKPOINT.read_text())
        except Exception:
            pass
    return {}


def save_checkpoint(cp: dict) -> None:
    CHECKPOINT.parent.mkdir(parents=True, exist_ok=True)
    import os as _os
    tmp = CHECKPOINT.with_name(CHECKPOINT.stem + f"-{_os.getpid()}.tmp")
    tmp.write_text(json.dumps(cp))
    tmp.replace(CHECKPOINT)


def db_connect() -> sqlite3.Connection:
    conn = sqlite3.connect(str(GOAL_LOOP_DB), timeout=30)
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


# ---------------------------------------------------------------- chats

def ingest_chats(rt: MemoryOSRuntime, conn: sqlite3.Connection,
                 since_mtime: float = 0.0) -> dict:
    files = sorted(CLAUDE_PROJECTS.rglob("*.jsonl"))
    total = len(files)
    sessions_rows = []
    n_transitions = 0
    n_sessions = 0
    n_skipped = 0
    max_mtime = since_mtime
    t0 = time.time()

    for i, p in enumerate(files, 1):
        if "subagents" in p.parts:
            continue
        try:
            mtime = p.stat().st_mtime
        except OSError:
            continue
        if mtime <= since_mtime:
            n_skipped += 1
            continue
        max_mtime = max(max_mtime, mtime)
        try:
            sig = parse_session(p)
        except Exception:
            continue
        if not sig["phases"]:
            continue

        # transitions
        seq = [ph for ph, _ in sig["phases"]]
        for a, b in zip(seq, seq[1:]):
            rt.record_transition(
                current_state=a, event_type="phase_advance", next_state=b,
                harness="omp", stage="coding", project=sig["project"],
            )
            n_transitions += 1

        # session summary memory (episodic)
        phase_hist = {}
        for ph in seq:
            phase_hist[ph] = phase_hist.get(ph, 0) + 1
        value = json.dumps({
            "project": sig["project"],
            "duration_min": sig["duration_min"],
            "phases": phase_hist,
            "files_touched": len(sig["files_modified"]),
            "test_runs": sig["test_runs"],
            "abstractions": sig["abstractions"],
            "user_msgs": sig["user_msgs"],
            "last_ts": sig["last_ts"],
        })
        sessions_rows.append((
            "episodic", f"chat-session:{sig['session_id']}", value,
            sig["project"], sig["session_id"], None,
        ))
        n_sessions += 1

        if len(sessions_rows) >= BATCH:
            conn.executemany(
                "INSERT OR IGNORE INTO memories (layer, key, value, goal_id, run_id, metadata) VALUES (?,?,?,?,?,?)",
                sessions_rows,
            )
            conn.commit()
            sessions_rows.clear()

        if i % 500 == 0:
            print(f"  chats: {i}/{total} files, {n_sessions} sessions, "
                  f"{n_transitions} transitions ({time.time()-t0:.0f}s)")

    if sessions_rows:
        conn.executemany(
            "INSERT OR IGNORE INTO memories (layer, key, value, goal_id, run_id, metadata) VALUES (?,?,?,?,?,?)",
            sessions_rows,
        )
        conn.commit()

    return {"sessions": n_sessions, "transitions": n_transitions,
            "skipped_unchanged": n_skipped, "max_mtime": max_mtime,
            "elapsed_s": round(time.time() - t0, 1)}


# ------------------------------------------------------------- obsidian

def parse_note(path: Path) -> dict | None:
    try:
        head = path.read_text(errors="ignore")[:8192]  # frontmatter + lead
    except Exception:
        return None
    tags = set(TAG_RE.findall(head))
    fm = FM_RE.search(head)
    if fm:
        m = FM_TAGS_RE.search(fm.group(1))
        if m:
            raw = m.group(1) or m.group(2) or ""
            for t in re.split(r"[,\s]+", raw.replace("[", "").replace("]", "")):
                t = t.strip().strip('"\'')
                if t:
                    tags.add(t)
    links = LINK_RE.findall(head)
    return {
        "title": path.stem,
        "folder": str(path.parent.relative_to(OBSIDIAN_VAULT)) if path.parent != OBSIDIAN_VAULT else "",
        "tags": sorted(tags)[:20],
        "links": sorted(set(links))[:40],
        "size": path.stat().st_size,
        "mtime": path.stat().st_mtime,
    }


def ingest_obsidian(conn: sqlite3.Connection,
                    since_mtime: float = 0.0) -> dict:
    notes = sorted(OBSIDIAN_VAULT.rglob("*.md"))
    total = len(notes)
    rows = []
    n_notes = 0
    n_skipped = 0
    max_mtime = since_mtime
    t0 = time.time()

    for i, p in enumerate(notes, 1):
        if any(part.startswith(".") for part in p.parts):
            continue
        try:
            mtime = p.stat().st_mtime
        except OSError:
            continue
        if mtime <= since_mtime:
            n_skipped += 1
            continue
        max_mtime = max(max_mtime, mtime)
        meta = parse_note(p)
        if meta is None:
            continue
        rel = str(p.relative_to(OBSIDIAN_VAULT))
        # UPSERT so edited notes refresh in place
        rows.append((
            "semantic", f"obsidian-note:{rel}", json.dumps(meta),
            meta["folder"], None, None,
        ))
        n_notes += 1

        if len(rows) >= BATCH:
            conn.executemany(
                "INSERT OR REPLACE INTO memories (layer, key, value, goal_id, run_id, metadata) VALUES (?,?,?,?,?,?)",
                rows,
            )
            conn.commit()
            rows.clear()

        if i % 10000 == 0:
            print(f"  obsidian: {i}/{total} notes scanned ({time.time()-t0:.0f}s)")

    if rows:
        conn.executemany(
            "INSERT OR REPLACE INTO memories (layer, key, value, goal_id, run_id, metadata) VALUES (?,?,?,?,?,?)",
            rows,
        )
        conn.commit()

    return {"notes_updated": n_notes, "skipped_unchanged": n_skipped,
            "max_mtime": max_mtime, "elapsed_s": round(time.time() - t0, 1)}


# ---------------------------------------------------------------- main

def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--source", default="all",
                    choices=["all", "chats", "obsidian"])
    ap.add_argument("--full", action="store_true",
                    help="Ignore checkpoints and reprocess everything")
    args = ap.parse_args()

    cp = load_checkpoint()
    rt = MemoryOSRuntime.from_env()
    conn = db_connect()

    stats = {"started_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
    try:
        if args.source in ("all", "chats"):
            since = 0.0 if args.full else float(cp.get("chats_max_mtime", 0.0))
            mode = "FULL" if args.full else f"incremental (since {time.strftime('%m-%d %H:%M', time.localtime(since)) if since else 'never'})"
            print(f"=== Ingesting chat transcripts — {mode} ===")
            stats["chats"] = ingest_chats(rt, conn, since_mtime=since)
            print(f"  done: {stats['chats']}")
            cp["chats_max_mtime"] = stats["chats"]["max_mtime"]
            cp["chats_completed_at"] = stats["started_at"]
            save_checkpoint(cp)

        if args.source in ("all", "obsidian"):
            since = 0.0 if args.full else float(cp.get("obsidian_max_mtime", 0.0))
            print(f"=== Ingesting Obsidian notes — {'FULL' if args.full else 'incremental'} ===")
            stats["obsidian"] = ingest_obsidian(conn, since_mtime=since)
            print(f"  done: {stats['obsidian']}")
            cp["obsidian_max_mtime"] = stats["obsidian"]["max_mtime"]
            cp["obsidian_completed_at"] = stats["started_at"]
            save_checkpoint(cp)

        # model stats
        n_states = len(rt.predictor._transitions)
        n_trans = sum(sum(v.values()) for v in rt.predictor._transitions.values())
        stats["transition_model"] = {"states": n_states, "transitions": n_trans}

        # memory store counts
        cur = conn.execute("SELECT layer, COUNT(*) FROM memories GROUP BY layer")
        stats["memory_counts"] = dict(cur.fetchall())

        print(json.dumps(stats, indent=2))
        return 0
    finally:
        rt.close()  # persists transition model
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
