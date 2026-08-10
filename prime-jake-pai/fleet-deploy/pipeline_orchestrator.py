#!/usr/bin/env python3
"""
Prime Jake — Master Training Pipeline Orchestrator

Runs the complete fine-tuning pipeline end-to-end:
1. Run all scrapers (collect raw data from elite sources)
2. Run doctrine-to-SFT converter (convert RIG doctrine to training examples)
3. Run fungal fermenter (raw → tiered A/B/C SFT)
4. Run death trail detector (purge poison data)
5. Merge with existing traces
6. Update axolotl config with new dataset
7. Push to QNAP for backup

Usage:
    python3 pipeline_orchestrator.py [--step all] [--step scrape] [--step ferment] [--step detect]
    python3 pipeline_orchestrator.py --step all
"""
from __future__ import annotations
import os, sys, json, subprocess, argparse, time, hashlib
from pathlib import Path
from datetime import datetime

BASE_DIR = Path.home() / "rig-ft"
SCRAPERS_DIR = BASE_DIR / "scrapers"
DATA_DIR = BASE_DIR / "data"
RAW_DIR = DATA_DIR / "raw"
FERMENTED_DIR = DATA_DIR / "fermented"
QNAP_BRIDGE = Path.home() / ".rig" / "scripts" / "qnap-bridge.py"

def run_script(script_path: str, args: list[str] = None, timeout: int = 300) -> dict:
    """Run a Python script and return result."""
    cmd = ["python3", script_path] + (args or [])
    print(f"  $ {' '.join(cmd)}")
    t0 = time.time()
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, cwd=str(BASE_DIR))
        return {
            "success": result.returncode == 0,
            "stdout": result.stdout[-2000:] if len(result.stdout) > 2000 else result.stdout,
            "stderr": result.stderr[-1000:] if len(result.stderr) > 1000 else result.stderr,
            "duration_s": round(time.time() - t0, 1),
        }
    except subprocess.TimeoutExpired:
        return {"success": False, "error": "timeout", "duration_s": timeout}
    except Exception as e:
        return {"success": False, "error": str(e), "duration_s": round(time.time() - t0, 1)}

def count_jsonl(path: Path) -> int:
    """Count lines in a JSONL file."""
    if not path.exists():
        return 0
    with open(path) as f:
        return sum(1 for line in f if line.strip())

def step_scrape(args):
    """Step 1: Run all scrapers to collect raw data."""
    print("\n" + "=" * 60)
    print("STEP 1: SCRAPE — Collect raw data from elite sources")
    print("=" * 60)

    scrapers = [
        ("GitHub nit comments", str(SCRAPERS_DIR / "github_nit_scraper.py"), ["--max", "500"]),
        ("GitHub dotfiles", str(SCRAPERS_DIR / "github_dotfiles_scraper.py"), ["--max", "200"]),
        ("Stack Overflow", str(SCRAPERS_DIR / "stackoverflow_scraper.py"), ["--max", "1000"]),
        ("YouTube tutorials", str(SCRAPERS_DIR / "youtube_scraper.py"), ["--max", "50"]),
        ("arXiv papers", str(SCRAPERS_DIR / "arxiv_consensus_scraper.py"), ["--max", "200"]),
        ("Reddit coding", str(SCRAPERS_DIR / "reddit_scraper.py"), ["--max", "300"]),
    ]

    results = {}
    for name, script, scraper_args in scrapers:
        print(f"\n  Running {name}...")
        if not os.path.exists(script):
            print(f"  ⚠ {script} not found, skipping")
            results[name] = {"success": False, "error": "not found"}
            continue
        result = run_script(script, scraper_args, timeout=600)
        results[name] = result
        if result["success"]:
            print(f"  ✓ {name} completed ({result['duration_s']}s)")
        else:
            print(f"  ✗ {name} failed: {result.get('error', result.get('stderr', 'unknown')[:200])}")

    # Count collected data
    total_raw = sum(count_jsonl(f) for f in RAW_DIR.glob("*.jsonl"))
    print(f"\n  Total raw examples: {total_raw}")
    return results

def step_doctrine(args):
    """Step 2: Convert doctrine to SFT examples."""
    print("\n" + "=" * 60)
    print("STEP 2: DOCTRINE — Convert RIG doctrine to SFT training examples")
    print("=" * 60)

    script = str(BASE_DIR / "doctrine_to_sft.py")
    result = run_script(script, timeout=120)
    if result["success"]:
        doctrine_count = count_jsonl(RAW_DIR / "doctrine_sft.jsonl")
        print(f"  ✓ Doctrine SFT examples: {doctrine_count}")
    else:
        print(f"  ✗ Failed: {result.get('stderr', 'unknown')[:200]}")
    return result

def step_ferment(args):
    """Step 3: Ferment raw data into tiered SFT."""
    print("\n" + "=" * 60)
    print("STEP 3: FERMENT — Raw data → tiered A/B/C SFT")
    print("=" * 60)

    script = str(BASE_DIR / "fungal_fermenter.py")
    result = run_script(script, timeout=600)
    if result["success"]:
        a_count = count_jsonl(FERMENTED_DIR / "tier_a_sft.jsonl")
        b_count = count_jsonl(FERMENTED_DIR / "tier_b_dpo.jsonl")
        c_count = count_jsonl(FERMENTED_DIR / "tier_c_discarded.jsonl")
        print(f"  ✓ A-tier (training): {a_count}")
        print(f"  ✓ B-tier (DPO): {b_count}")
        print(f"  ✓ C-tier (discard): {c_count}")
    else:
        print(f"  ✗ Failed: {result.get('stderr', 'unknown')[:200]}")
    return result

def step_detect(args):
    """Step 4: Detect and purge death trails."""
    print("\n" + "=" * 60)
    print("STEP 4: DEATH TRAILS — Detect and purge poison data")
    print("=" * 60)

    script = str(BASE_DIR / "death_trail_detector.py")
    result = run_script(script, ["--batch-size", "50"], timeout=900)
    if result["success"]:
        print(f"  ✓ Death trail detection complete")
    else:
        print(f"  ✗ Failed: {result.get('stderr', 'unknown')[:200]}")
    return result

def step_merge(args):
    """Step 5: Merge fermented A-tier + existing traces + doctrine into final dataset."""
    print("\n" + "=" * 60)
    print("STEP 5: MERGE — Create final training dataset")
    print("=" * 60)

    output_file = DATA_DIR / "train_final.jsonl"
    seen_hashes = set()
    total = 0
    sources = {}

    with open(output_file, "w") as out:
        # 1. Existing traces (the 5,202)
        existing = DATA_DIR / "train_all_clean3.jsonl"
        if existing.exists():
            with open(existing) as f:
                for line in f:
                    if not line.strip():
                        continue
                    h = hashlib.sha256(line.strip().encode()).hexdigest()
                    if h not in seen_hashes:
                        seen_hashes.add(h)
                        try:
                            ex = json.loads(line)
                            ex["source"] = ex.get("source", "existing_traces")
                            out.write(json.dumps(ex) + "\n")
                            total += 1
                            src = ex["source"]
                            sources[src] = sources.get(src, 0) + 1
                        except:
                            pass
            print(f"  Existing traces: {sources.get('existing_traces', 0)}")

        # 2. Fermented A-tier
        a_file = FERMENTED_DIR / "tier_a_sft.jsonl"
        if a_file.exists():
            with open(a_file) as f:
                for line in f:
                    if not line.strip():
                        continue
                    h = hashlib.sha256(line.strip().encode()).hexdigest()
                    if h not in seen_hashes:
                        seen_hashes.add(h)
                        try:
                            ex = json.loads(line)
                            src = ex.get("source", "fermented_a")
                            out.write(json.dumps(ex) + "\n")
                            total += 1
                            sources[src] = sources.get(src, 0) + 1
                        except:
                            pass
            print(f"  Fermented A-tier: {sum(v for k, v in sources.items() if 'fermented' in k or k != 'existing_traces')}")

    print(f"\n  ✓ Final dataset: {total} examples")
    print(f"  Sources: {json.dumps(sources, indent=2)}")
    print(f"  Output: {output_file}")

    # Update axolotl config to point to new dataset
    update_axolotl_config(output_file)
    return {"success": True, "total": total, "sources": sources}

def update_axolotl_config(data_path: Path):
    """Update axolotl config to use the new merged dataset."""
    config_path = BASE_DIR / "axolotl-kat-lora.yaml"
    if not config_path.exists():
        print("  ⚠ axolotl config not found, skipping config update")
        return

    with open(config_path) as f:
        config = f.read()

    # Replace the dataset path
    import re
    new_config = re.sub(
        r'path: /home/user/rig-ft/data/[^\n]+',
        f'path: {data_path}',
        config
    )

    with open(config_path, "w") as f:
        f.write(new_config)
    print(f"  ✓ Updated axolotl config → {data_path}")

def step_backup(args):
    """Step 6: Backup to QNAP."""
    print("\n" + "=" * 60)
    print("STEP 6: BACKUP — Push to QNAP")
    print("=" * 60)

    files_to_backup = [
        ("data/train_final.jsonl", "datasets/train_final.jsonl"),
        ("data/fermented/tier_a_sft.jsonl", "datasets/tier_a_sft.jsonl"),
        ("data/fermented/tier_b_dpo.jsonl", "datasets/tier_b_dpo.jsonl"),
        ("data/rigbench.jsonl", "datasets/rigbench.jsonl"),
        ("axolotl-kat-lora.yaml", "configs/axolotl-kat-lora.yaml"),
    ]

    for local, remote in files_to_backup:
        local_path = BASE_DIR / local
        if local_path.exists():
            result = run_script(str(QNAP_BRIDGE), ["mkdir", "rig-ft"], timeout=10)
            result = run_script(str(QNAP_BRIDGE), ["put", str(local_path), f"rig-ft/{remote}"], timeout=60)
            if result["success"]:
                print(f"  ✓ {local} → qnap://rig-ft/{remote}")
            else:
                print(f"  ✗ {local} backup failed")
        else:
            print(f"  ⚠ {local} not found, skipping")

def main():
    parser = argparse.ArgumentParser(description="Prime Jake — Master Training Pipeline Orchestrator")
    parser.add_argument("--step", default="all",
                       choices=["all", "scrape", "doctrine", "ferment", "detect", "merge", "backup"],
                       help="Which step to run")
    args = parser.parse_args()

    print("=" * 60)
    print(f"PRIME JAKE — TRAINING PIPELINE")
    print(f"Started: {datetime.now().isoformat()}")
    print(f"Step: {args.step}")
    print("=" * 60)

    t0 = time.time()
    results = {}

    if args.step in ("all", "scrape"):
        results["scrape"] = step_scrape(args)
    if args.step in ("all", "doctrine"):
        results["doctrine"] = step_doctrine(args)
    if args.step in ("all", "ferment"):
        results["ferment"] = step_ferment(args)
    if args.step in ("all", "detect"):
        results["detect"] = step_detect(args)
    if args.step in ("all", "merge"):
        results["merge"] = step_merge(args)
    if args.step in ("all", "backup"):
        results["backup"] = step_backup(args)

    duration = round(time.time() - t0, 1)
    print("\n" + "=" * 60)
    print(f"PIPELINE COMPLETE — {duration}s")
    print("=" * 60)
    for step, result in results.items():
        status = "✓" if isinstance(result, dict) and result.get("success", False) else "✗"
        print(f"  {status} {step}")

    # Final dataset stats
    final = DATA_DIR / "train_final.jsonl"
    if final.exists():
        count = count_jsonl(final)
        print(f"\n  Final training dataset: {count} examples")
        print(f"  Location: {final}")
        print(f"\n  To start training:")
        print(f"    cd /home/user/rig-ft && accelerate launch -m axolotl.cli.train axolotl-kat-lora.yaml")

if __name__ == "__main__":
    main()
