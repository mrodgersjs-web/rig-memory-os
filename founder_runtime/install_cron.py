"""Install Hermes cron jobs from config/schedules.yaml.

Idempotent: re-running updates existing jobs but does not duplicate.

Usage:
    uv run python -m founder_runtime.install_cron [--dry-run]
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

import yaml


HERMES_CRON = Path.home() / ".hermes" / "cron" / "jobs.json"


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--schedules", default="config/schedules.yaml")
    args = p.parse_args(argv)

    schedules = yaml.safe_load(Path(args.schedules).read_text())
    jobs = schedules.get("jobs", [])

    if HERMES_CRON.exists():
        raw = HERMES_CRON.read_text()
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            print(f"WARNING: {HERMES_CRON} is not valid JSON; skipping merge")
            parsed = {"jobs": []}
        if isinstance(parsed, list):
            existing = parsed
        else:
            existing = parsed.get("jobs", [])
    else:
        existing = []

    # Normalize: each entry should be a dict
    existing_dicts = [j for j in existing if isinstance(j, dict)]
    existing_strings = [j for j in existing if isinstance(j, str)]
    if existing_strings:
        print(f"  found {len(existing_strings)} non-dict entries in jobs.json — preserving as-is")

    existing_names = {j.get("name") for j in existing_dicts}
    added = 0
    for job in jobs:
        if job["name"] in existing_names:
            print(f"  = {job['name']} (exists)")
            continue
        existing_dicts.append({
            "name": job["name"],
            "schedule": job["schedule"],
            "agentic": job.get("agentic", False),
            "command": job.get("command", ""),
            "description": job.get("description", ""),
        })
        added += 1
        print(f"  + {job['name']}")

    if args.dry_run:
        print(f"\nDRY RUN: would add {added}, total {len(existing_dicts) + len(existing_strings)}")
        return 0

    HERMES_CRON.parent.mkdir(parents=True, exist_ok=True)
    merged = existing_strings + existing_dicts
    HERMES_CRON.write_text(json.dumps(merged, indent=2))
    print(f"\nWrote {len(merged)} jobs to {HERMES_CRON}")
    return 0


if __name__ == "__main__":
    sys.exit(main())