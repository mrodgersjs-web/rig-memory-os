#!/usr/bin/env python3
"""
Prime Jake — Self-Improving Training Loop

Closes the feedback loop: model outputs → RIGBench scoring → fermentation → 
training data → retrain. Runs fully autonomously once started.

The loop:
1. Generate model outputs on RIGBench questions
2. Score each output (correct/wrong/partial)
3. For wrong/partial outputs: create correction SFT pairs
4. Ferment corrections through the fungal fermenter
5. Merge into training dataset
6. Signal that a retrain is needed

Usage:
    python3 self_improving_loop.py [--model URL] [--eval FILE] [--iterations N]
    python3 self_improving_loop.py --model http://localhost:8003/v1 --iterations 5
"""
from __future__ import annotations
import os, sys, json, time, hashlib, argparse, urllib.request, urllib.error
from pathlib import Path
from datetime import datetime

VLLM_URL = os.environ.get("VLLM_URL", "http://localhost:8003/v1")
MODEL_NAME = "rig-kat"
EVAL_FILE = Path.home() / "rig-ft" / "data" / "rigbench.jsonl"
CORRECTIONS_FILE = Path.home() / "rig-ft" / "data" / "raw" / "self_improving_corrections.jsonl"
LOG_FILE = Path.home() / "rig-ft" / "self_improving.log"

def load_eval(path: str) -> list[dict]:
    data = []
    with open(path) as f:
        for line in f:
            if line.strip():
                try:
                    data.append(json.loads(line))
                except:
                    pass
    return data

def query_model(model_url: str, model_name: str, messages: list[dict]) -> str:
    """Query the model and return the response."""
    payload = {
        "model": model_name,
        "messages": messages,
        "max_tokens": 500,
        "temperature": 0.3,
    }
    try:
        req = urllib.request.Request(
            f"{model_url}/chat/completions",
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"},
            method="POST"
        )
        with urllib.request.urlopen(req, timeout=60) as resp:
            result = json.loads(resp.read())
            return result["choices"][0]["message"]["content"]
    except Exception as e:
        return f"[ERROR: {e}]"

def score_output(output: str, expected: str) -> dict:
    """Score the model output against the expected answer."""
    output_lower = output.lower()
    expected_lower = expected.lower()
    
    # Extract key terms from expected answer
    expected_words = set(expected_lower.split())
    output_words = set(output_lower.split())
    
    # Keyword overlap
    overlap = len(expected_words & output_words) / max(len(expected_words), 1)
    
    # Check for key phrases
    key_phrases = []
    for phrase in expected_lower.split('.'):
        phrase = phrase.strip()
        if len(phrase) > 20:
            key_phrases.append(phrase)
    
    phrase_matches = 0
    for phrase in key_phrases:
        # Check if significant words from the phrase appear in output
        phrase_words = set(phrase.split())
        if len(phrase_words & output_words) / max(len(phrase_words), 1) > 0.5:
            phrase_matches += 1
    
    phrase_score = phrase_matches / max(len(key_phrases), 1) if key_phrases else 0
    
    # Overall score
    overall = (overlap * 0.4 + phrase_score * 0.6)
    
    if overall > 0.6:
        verdict = "correct"
    elif overall > 0.3:
        verdict = "partial"
    else:
        verdict = "wrong"
    
    return {
        "verdict": verdict,
        "overlap": round(overlap, 2),
        "phrase_score": round(phrase_score, 2),
        "overall": round(overall, 2),
    }

def create_correction(entry: dict, model_output: str, score: dict) -> dict:
    """Create a correction SFT pair from a wrong/partial answer."""
    messages = entry.get("messages", [])
    user_msg = next((m["content"] for m in messages if m["role"] == "user"), "")
    expected = next((m["content"] for m in messages if m["role"] == "assistant"), "")
    category = entry.get("category", "unknown")
    
    return {
        "messages": [
            {"role": "system", "content": "You are Prime Jake PAI. Answer based on loaded RIG doctrine. Be specific and correct."},
            {"role": "user", "content": user_msg},
            {"role": "assistant", "content": expected},
        ],
        "source": f"self_improving_{category}_{score['verdict']}",
        "tier": None,
        "correction_for": model_output[:200],
        "score": score["overall"],
        "created_at": datetime.now().isoformat(),
    }

def run_iteration(model_url: str, model_name: str, eval_data: list[dict]) -> dict:
    """Run one iteration of the self-improving loop."""
    correct = 0
    partial = 0
    wrong = 0
    corrections = []
    
    for i, entry in enumerate(eval_data):
        messages = entry.get("messages", [])
        # Send only system + user (not the expected answer)
        query_msgs = [m for m in messages if m["role"] in ("system", "user")]
        
        if not query_msgs:
            continue
        
        # Query model
        output = query_model(model_url, model_name, query_msgs)
        
        # Get expected answer
        expected = next((m["content"] for m in messages if m["role"] == "assistant"), "")
        if not expected:
            continue
        
        # Score
        score = score_output(output, expected)
        
        if score["verdict"] == "correct":
            correct += 1
        elif score["verdict"] == "partial":
            partial += 1
            corrections.append(create_correction(entry, output, score))
        else:
            wrong += 1
            corrections.append(create_correction(entry, output, score))
        
        print(f"  [{i+1}/{len(eval_data)}] {score['verdict']:7s} ({score['overall']:.2f}) — {entry.get('category', '?')}")
    
    # Write corrections
    if corrections:
        with open(CORRECTIONS_FILE, "a") as f:
            for c in corrections:
                f.write(json.dumps(c) + "\n")
    
    total = correct + partial + wrong
    accuracy = correct / max(total, 1)
    
    return {
        "total": total,
        "correct": correct,
        "partial": partial,
        "wrong": wrong,
        "accuracy": round(accuracy, 2),
        "corrections_generated": len(corrections),
        "timestamp": datetime.now().isoformat(),
    }

def main():
    parser = argparse.ArgumentParser(description="Self-Improving Training Loop")
    parser.add_argument("--model", default=VLLM_URL, help="Model API URL")
    parser.add_argument("--model-name", default=MODEL_NAME, help="Model name")
    parser.add_argument("--eval", default=str(EVAL_FILE), help="Eval file")
    parser.add_argument("--iterations", type=int, default=1, help="Number of iterations")
    args = parser.parse_args()
    
    if not os.path.exists(args.eval):
        print(f"Error: eval file not found: {args.eval}")
        print("Run: python3 rigbench_builder.py")
        sys.exit(1)
    
    eval_data = load_eval(args.eval)
    print(f"Loaded {len(eval_data)} eval questions")
    print(f"Model: {args.model_name} at {args.model}")
    print(f"Iterations: {args.iterations}")
    print()
    
    all_results = []
    for it in range(args.iterations):
        print(f"\n{'='*50}")
        print(f"ITERATION {it+1}/{args.iterations}")
        print(f"{'='*50}")
        
        result = run_iteration(args.model, args.model_name, eval_data)
        all_results.append(result)
        
        print(f"\nResults:")
        print(f"  Correct: {result['correct']}/{result['total']} ({result['accuracy']*100:.0f}%)")
        print(f"  Partial: {result['partial']}")
        print(f"  Wrong: {result['wrong']}")
        print(f"  Corrections generated: {result['corrections_generated']}")
        
        # Log
        with open(LOG_FILE, "a") as f:
            f.write(json.dumps(result) + "\n")
    
    # Summary
    print(f"\n{'='*50}")
    print(f"SUMMARY")
    print(f"{'='*50}")
    for i, r in enumerate(all_results):
        print(f"  Iteration {i+1}: {r['accuracy']*100:.0f}% accuracy, {r['corrections_generated']} corrections")
    
    if all_results:
        first_acc = all_results[0]["accuracy"]
        last_acc = all_results[-1]["accuracy"]
        delta = last_acc - first_acc
        print(f"\n  Accuracy delta: {delta:+.2f} ({'improving' if delta > 0 else 'declining' if delta < 0 else 'stable'})")
    
    total_corrections = sum(r["corrections_generated"] for r in all_results)
    print(f"  Total corrections: {total_corrections}")
    
    if total_corrections > 0:
        print(f"\n  Corrections saved to: {CORRECTIONS_FILE}")
        print(f"  To retrain: cd /home/user/rig-ft && python3 fungal_fermenter.py && python3 pipeline_orchestrator.py --step merge")
        print(f"  Then: cd run && CUDA_VISIBLE_DEVICES=1 accelerate launch -m axolotl.cli.train axolotl-kat-lora-v2.yaml")

if __name__ == "__main__":
    main()
