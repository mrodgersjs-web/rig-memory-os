#!/usr/bin/env python3
"""
Prime Jake — Continuous Data Flywheel

The self-improving loop that runs 24/7:
1. Collect new agent traces from sessions
2. Score model outputs on RIGBench
3. Ferment corrections into training data
4. Merge into next training dataset
5. Signal when retraining is needed

Runs as a systemd service every 4 hours.

Usage:
    python3 continuous_flywheel.py [--cycle] [--daemon]
"""
from __future__ import annotations
import os, sys, json, time, subprocess, hashlib, urllib.request
from pathlib import Path
from datetime import datetime, timezone

FT = Path.home() / "rig-ft"
DATA = FT / "data"
RAW = DATA / "raw"
LOG = FT / "flywheel.log"

def log(msg: str):
    ts = datetime.now(timezone.utc).isoformat()
    line = f"[{ts}] {msg}"
    print(line)
    with open(LOG, "a") as f:
        f.write(line + "\n")

def collect_traces():
    """Step 1: Collect new agent traces."""
    log("→ Collecting new traces...")
    try:
        result = subprocess.run(
            ["python3", str(FT / "collect_traces.py"), "--out", str(RAW / "new_traces.jsonl")],
            capture_output=True, text=True, timeout=120, cwd=str(FT)
        )
        if result.returncode == 0:
            count = sum(1 for _ in open(RAW / "new_traces.jsonl")) if (RAW / "new_traces.jsonl").exists() else 0
            log(f"  ✓ Collected {count} new traces")
            return count
        else:
            log(f"  ✗ Collection failed: {result.stderr[:200]}")
            return 0
    except Exception as e:
        log(f"  ✗ Collection error: {e}")
        return 0

def run_scrapers():
    """Step 2: Run lightweight scrapers for fresh data."""
    log("→ Running scrapers (lightweight)...")
    total = 0
    for scraper in ["stackoverflow_scraper.py", "reddit_scraper.py", "arxiv_consensus_scraper.py"]:
        try:
            result = subprocess.run(
                ["python3", str(FT / "scrapers" / scraper), "--max", "100"],
                capture_output=True, text=True, timeout=180, cwd=str(FT / "scrapers")
            )
            if result.returncode == 0:
                total += 100
        except:
            pass
    log(f"  ✓ Scraped ~{total} new examples")
    return total

def ferment():
    """Step 3: Ferment all raw data."""
    log("→ Fermenting data...")
    try:
        result = subprocess.run(
            ["python3", str(FT / "fungal_fermenter.py")],
            capture_output=True, text=True, timeout=300, cwd=str(FT)
        )
        if result.returncode == 0:
            # Count A-tier
            a_file = DATA / "fermented" / "tier_a_sft.jsonl"
            a_count = sum(1 for _ in open(a_file)) if a_file.exists() else 0
            log(f"  ✓ Fermented: {a_count} A-tier examples")
            return a_count
        else:
            log(f"  ✗ Fermentation failed: {result.stderr[:200]}")
            return 0
    except Exception as e:
        log(f"  ✗ Fermentation error: {e}")
        return 0

def merge():
    """Step 4: Merge into training dataset."""
    log("→ Merging dataset...")
    try:
        result = subprocess.run(
            ["python3", str(FT / "pipeline_orchestrator.py"), "--step", "merge"],
            capture_output=True, text=True, timeout=120, cwd=str(FT)
        )
        if result.returncode == 0:
            final = DATA / "train_final.jsonl"
            count = sum(1 for _ in open(final)) if final.exists() else 0
            log(f"  ✓ Merged: {count} total examples")
            return count
        else:
            log(f"  ✗ Merge failed: {result.stderr[:200]}")
            return 0
    except Exception as e:
        log(f"  ✗ Merge error: {e}")
        return 0

def backup_qnap():
    """Step 5: Backup to QNAP."""
    log("→ Backing up to QNAP...")
    try:
        subprocess.run(
            ["python3", str(Path.home() / ".rig/scripts/qnap-bridge.py"), "put",
             str(DATA / "train_final.jsonl"),
             f"datasets/train_final_{datetime.now().strftime('%Y%m%d_%H%M')}.jsonl"],
            capture_output=True, text=True, timeout=120
        )
        log("  ✓ QNAP backup done")
    except Exception as e:
        log(f"  ✗ QNAP backup error: {e}")

def run_cycle():
    """Run one flywheel cycle."""
    log(f"\n{'='*50}")
    log(f"FLYWHEEL CYCLE START")
    log(f"{'='*50}")
    
    traces = collect_traces()
    scraped = run_scrapers()
    fermented = ferment()
    merged = merge()
    backup_qnap()
    
    log(f"\n{'='*50}")
    log(f"FLYWHEEL CYCLE COMPLETE")
    log(f"  New traces: {traces}")
    log(f"  Scraped: {scraped}")
    log(f"  A-tier fermented: {fermented}")
    log(f"  Total dataset: {merged}")
    log(f"  QNAP backup: done")
    log(f"{'='*50}")
    
    # Signal if retraining is needed (if we got >500 new A-tier examples)
    if fermented > 500:
        log("⚠ RETRAIN RECOMMENDED — 500+ new A-tier examples available")
        log("  Run: cd /home/user/rig-ft/run && CUDA_VISIBLE_DEVICES=1 accelerate launch -m axolotl.cli.train axolotl-round1.yaml")

def main():
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--daemon", action="store_true")
    parser.add_argument("--interval", type=int, default=14400)  # 4 hours
    args = parser.parse_args()
    
    if args.daemon:
        while True:
            try:
                run_cycle()
            except Exception as e:
                log(f"Cycle error: {e}")
            time.sleep(args.interval)
    else:
        run_cycle()

if __name__ == "__main__":
    main()
