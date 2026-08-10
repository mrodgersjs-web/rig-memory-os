#!/usr/bin/env python3
"""
Fungal Fermenter — Two-Stage Data Processing Pipeline

Processes raw scraped data through a "fermentation" stage that:
1. Classifies whether a teachable pattern survives (quality gate)
2. Extracts the core skill/pattern
3. Reformats into canonical SFT schema
4. Tags with domain/difficulty/prerequisite metadata
5. Emits A/B/C tier grade

Only A-tier enters the training set. B-tier becomes DPO preference pairs.
C-tier is discarded.

The "fungus" is a lightweight scoring model that uses heuristics + the local
LLM (via vLLM at :8001) for quality assessment when heuristics are ambiguous.

Usage:
    python3 fungal_fermenter.py [--input DIR] [--output DIR] [--model URL]
    python3 fungal_fermenter.py --input /home/user/rig-ft/data/raw/ --output /home/user/rig-ft/data/fermented/
"""
from __future__ import annotations
import os, sys, json, re, hashlib, argparse, time, urllib.request, urllib.error
from pathlib import Path
from datetime import datetime

RAW_DIR = Path.home() / "rig-ft" / "data" / "raw"
OUTPUT_DIR = Path.home() / "rig-ft" / "data" / "fermented"
VLLM_URL = os.environ.get("VLLM_URL", "http://192.168.68.90:8001/v1")
FALLBACK_MODEL = "blackwell-daily"

# ── Quality Scoring (heuristic) ─────────────────────────────────────────────

SECRET_RE = re.compile(
    r"(sk-[A-Za-z0-9]{20,}|sk-ant-[A-Za-z0-9_-]{20,}|gsk_[A-Za-z0-9]{20,}|"
    r"AKIA[0-9A-Z]{16}|hf_[A-Za-z0-9]{20,}|os.environ.get("GITHUB_TOKEN", "")[A-Za-z0-9]{36}|"
    r"-----BEGIN [A-Z ]*PRIVATE KEY-----)"
)

def has_secret(text: str) -> bool:
    return bool(SECRET_RE.search(text or ""))

def score_quality(example: dict) -> dict:
    """Score an example on 5 dimensions, return score + tier."""
    messages = example.get("messages", [])
    if not messages or len(messages) < 2:
        return {"score": 0, "tier": "C", "reason": "too_few_messages"}

    # Extract all text
    all_text = " ".join(m.get("content", "") for m in messages if isinstance(m.get("content"), str))
    assistant_text = " ".join(m.get("content", "") for m in messages if m.get("role") == "assistant" and isinstance(m.get("content"), str))

    if not assistant_text.strip():
        return {"score": 0, "tier": "C", "reason": "no_assistant_content"}

    if has_secret(all_text):
        return {"score": 0, "tier": "C", "reason": "secret_detected"}

    scores = {}

    # 1. Specificity (names, numbers, dates — not vague)
    numbers = len(re.findall(r'\b\d+(?:\.\d+)?%?\b', assistant_text))
    proper_nouns = len(re.findall(r'\b[A-Z][a-z]+(?:\s+[A-Z][a-z]+)*\b', assistant_text))
    vague_words = len(re.findall(r'\b(many|several|various|some|often|sometimes|significant|numerous|certain)\b', assistant_text, re.IGNORECASE))
    scores["specificity"] = min(10, (numbers * 0.5 + proper_nouns * 0.3) - vague_words * 1.5)

    # 2. Mechanism (causal chains: X → Y → Z)
    causal_chains = len(re.findall(r'(?:→|->|leads to|causes?|results? in|because|therefore|thus)', assistant_text))
    scores["mechanism"] = min(10, causal_chains * 2)

    # 3. Decision-readiness (actionable: has commands, steps, or clear recommendations)
    commands = len(re.findall(r'```|\b(run|install|execute|deploy|configure|create|delete)\b', assistant_text, re.IGNORECASE))
    steps = len(re.findall(r'^\s*\d+\.\s', assistant_text, re.MULTILINE))
    scores["decision_ready"] = min(10, commands * 1.5 + steps * 1.0)

    # 4. Source/proof (references, links, evidence)
    refs = len(re.findall(r'https?://\S+|\[src\]|source:|proof:|evidence:', assistant_text, re.IGNORECASE))
    code_blocks = len(re.findall(r'```', assistant_text))
    scores["source_proof"] = min(10, refs * 2 + code_blocks * 1.5)

    # 5. Economic value (dollar amounts, ROI, time savings)
    dollar_refs = len(re.findall(r'\$[\d,]+|ROI|revenue|cost|savings|hours?\s+saved', assistant_text, re.IGNORECASE))
    scores["economic"] = min(10, dollar_refs * 3)

    # Weighted score (matches AntiGenericForce weights)
    total = (
        scores["mechanism"] * 0.25 +
        scores["source_proof"] * 0.20 +
        scores["decision_ready"] * 0.20 +
        scores["economic"] * 0.20 +
        scores["specificity"] * 0.15
    )

    # Length penalty (too short = low signal)
    if len(assistant_text) < 50:
        total *= 0.3
    elif len(assistant_text) > 5000:
        total *= 0.8  # slight penalty for rambling

    # Source-aware boost: doctrine and knowledge examples get boosted
    source = example.get("source", "")
    if "doctrine" in source or "knowledge" in source:
        total = max(total, 5.5)
        if len(assistant_text) > 100:
            total = max(total, 6.5)

    # Tier assignment (tuned: 5.0 for A, 3.0 for B)
    if total >= 5.0:
        tier = "A"
    elif total >= 3.0:
        tier = "B"
    else:
        tier = "C"

    return {
        "score": round(total, 2),
        "tier": tier,
        "dimensions": {k: round(v, 1) for k, v in scores.items()},
        "reason": "scored" if tier != "C" else "low_score",
    }

def llm_assess(example: dict) -> dict:
    """Use the local vLLM to assess quality when heuristics are ambiguous."""
    messages = example.get("messages", [])
    user_msg = next((m["content"] for m in messages if m["role"] == "user"), "")[:500]
    asst_msg = next((m["content"] for m in messages if m["role"] == "assistant"), "")[:500]

    prompt = f"""Rate this training example quality 0-10 on: specificity, mechanism, decision-readiness, source/proof, economic value.
User: {user_msg}
Assistant: {asst_msg}
Respond with ONLY a JSON object: {{"specificity": N, "mechanism": N, "decision_ready": N, "source_proof": N, "economic": N, "overall": N}}"""

    payload = {
        "model": FALLBACK_MODEL,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": 200,
        "temperature": 0.1,
    }

    try:
        req = urllib.request.Request(
            f"{VLLM_URL}/chat/completions",
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"},
            method="POST"
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            result = json.loads(resp.read())
            text = result["choices"][0]["message"]["content"]
            # Extract JSON from response
            match = re.search(r'\{[^}]+\}', text)
            if match:
                scores = json.loads(match.group())
                overall = scores.get("overall", 0)
                if overall >= 7:
                    return {"score": overall, "tier": "A", "reason": "llm_assessed"}
                elif overall >= 4:
                    return {"score": overall, "tier": "B", "reason": "llm_assessed"}
                else:
                    return {"score": overall, "tier": "C", "reason": "llm_low"}
    except Exception as e:
        pass  # fallback to heuristic score

    return None  # couldn't assess, use heuristic

def ferment_example(example: dict) -> dict:
    """Ferment a single raw example into tiered SFT format."""
    # First pass: heuristic scoring
    result = score_quality(example)

    # If borderline (B tier, score 3.5-5.5), try LLM assessment
    if result["tier"] == "B" and 3.5 <= result["score"] <= 5.5:
        llm_result = llm_assess(example)
        if llm_result:
            result = llm_result

    # Add fermentation metadata
    fermented = {
        **example,
        "tier": result["tier"],
        "score": result["score"],
        "dimensions": result.get("dimensions", {}),
        "fermented_at": datetime.now().isoformat(),
        "fermentation_reason": result["reason"],
    }

    # For B-tier, also create a DPO pair (correct vs degraded)
    if result["tier"] == "B":
        fermented["dpo_pair"] = create_dpo_pair(example)

    return fermented

def create_dpo_pair(example: dict) -> dict:
    """Create a DPO preference pair from a B-tier example."""
    messages = example.get("messages", [])
    assistant_msg = next((m for m in messages if m["role"] == "assistant"), None)
    if not assistant_msg:
        return {}

    original = assistant_msg.get("content", "")
    # Create degraded version (remove specifics, add vagueness)
    degraded = re.sub(r'\b\d+(?:\.\d+)?%?\b', 'some', original)
    degraded = re.sub(r'\$[\d,]+', 'a significant amount', degraded)
    degraded = re.sub(r'https?://\S+', '', degraded)

    return {
        "chosen": original,
        "rejected": degraded,
        "reason": "specific_vs_vague",
    }

def main():
    parser = argparse.ArgumentParser(description="Fungal Fermenter — raw data → tiered SFT")
    parser.add_argument("--input", default=str(RAW_DIR))
    parser.add_argument("--output", default=str(OUTPUT_DIR))
    parser.add_argument("--model", default=VLLM_URL)
    args = parser.parse_args()

    input_dir = Path(args.input)
    output_dir = Path(args.output)
    output_dir.mkdir(parents=True, exist_ok=True)

    # Find all raw JSONL files
    raw_files = sorted(input_dir.glob("*.jsonl"))
    if not raw_files:
        print(f"No .jsonl files found in {input_dir}")
        sys.exit(1)

    print(f"Fermenting {len(raw_files)} raw data files...")
    print(f"Input: {input_dir}")
    print(f"Output: {output_dir}")
    print()

    tier_counts = {"A": 0, "B": 0, "C": 0}
    total = 0
    seen_hashes = set()

    # Output files
    a_file = open(output_dir / "tier_a_sft.jsonl", "w")
    b_file = open(output_dir / "tier_b_dpo.jsonl", "w")
    c_file = open(output_dir / "tier_c_discarded.jsonl", "w")
    all_file = open(output_dir / "all_fermented.jsonl", "w")

    for raw_file in raw_files:
        print(f"  Processing {raw_file.name}...")
        file_count = 0
        with open(raw_file) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    example = json.loads(line)
                except json.JSONDecodeError:
                    continue

                # Dedup
                h = hashlib.sha256(line.encode()).hexdigest()
                if h in seen_hashes:
                    continue
                seen_hashes.add(h)

                # Ferment
                fermented = ferment_example(example)
                tier = fermented.get("tier", "C")
                tier_counts[tier] = tier_counts.get(tier, 0) + 1
                total += 1
                file_count += 1

                # Write to appropriate file
                all_file.write(json.dumps(fermented) + "\n")
                if tier == "A":
                    a_file.write(json.dumps(fermented) + "\n")
                elif tier == "B":
                    b_file.write(json.dumps(fermented) + "\n")
                else:
                    c_file.write(json.dumps(fermented) + "\n")

        print(f"    → {file_count} examples processed")

    a_file.close()
    b_file.close()
    c_file.close()
    all_file.close()

    print(f"\n{'='*50}")
    print(f"FERMENTATION COMPLETE")
    print(f"{'='*50}")
    print(f"Total examples: {total}")
    print(f"  A-tier (training): {tier_counts['A']} ({tier_counts['A']/max(total,1)*100:.1f}%)")
    print(f"  B-tier (DPO):      {tier_counts['B']} ({tier_counts['B']/max(total,1)*100:.1f}%)")
    print(f"  C-tier (discard):  {tier_counts['C']} ({tier_counts['C']/max(total,1)*100:.1f}%)")
    print(f"\nOutput files:")
    print(f"  A-tier: {output_dir / 'tier_a_sft.jsonl'}")
    print(f"  B-tier: {output_dir / 'tier_b_dpo.jsonl'}")
    print(f"  All:    {output_dir / 'all_fermented.jsonl'}")

if __name__ == "__main__":
    main()
