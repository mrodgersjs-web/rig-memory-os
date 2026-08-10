#!/usr/bin/env python3
"""
Doctrine-to-SFT Converter

Converts RIG doctrine files (Jake PAI, TAC, mrodgersjs-web) into SFT training
examples. Each doctrine rule becomes multiple training pairs:
  - Q&A pair (user asks about rule → assistant applies it)
  - Scenario (situation → assistant applies rule → output)
  - Adversarial example (situation that should trigger rule → correct refusal)

Output: /home/user/rig-ft/data/raw/doctrine_sft.jsonl

Usage:
    python3 doctrine_to_sft.py [--doctrine-dir DIR] [--out FILE]
"""
from __future__ import annotations
import os, sys, json, re, hashlib, argparse, glob
from pathlib import Path
from datetime import datetime

# Doctrine source files
DOCTRINE_FILES = [
    # Jake PAI
    os.path.expanduser("~/Documents/JakeStudio/Agent Vaults/jake-pai/Jake Operating System v2.md"),
    os.path.expanduser("~/Documents/JakeStudio/Agent Vaults/jake-pai/Jake Upgrade Memo.md"),
    # Doctrines
    os.path.expanduser("~/Documents/JakeStudio/Doctrines/TAC Doctrine - Tactical Agentic Coding.md"),
    os.path.expanduser("~/Documents/JakeStudio/Doctrines/Jake L8 Operator Harness.md"),
    os.path.expanduser("~/Documents/JakeStudio/Doctrines/Jake Enforcement SOP.md"),
    os.path.expanduser("~/Documents/JakeStudio/Doctrines/RIG Seven-Layer Memory Doctrine.md"),
    os.path.expanduser("~/Documents/JakeStudio/Doctrines/Swarm Agent Doctrine.md"),
    os.path.expanduser("~/Documents/JakeStudio/Doctrines/RIG Operating Procedures Doctrine.md"),
    # TAC v2
    os.path.expanduser("~/.claude/skills/agentic-coding/tac-v2-doctrine/SKILL.md"),
    os.path.expanduser("~/.claude/skills/rig-tac-doctrine/SKILL.md"),
    # Prime Jake
    os.path.expanduser("~/.rig/prime-jake-pai-master.md"),
    os.path.expanduser("~/.prime/agent/skills/jake-pai-doctrine/SKILL.md"),
    os.path.expanduser("~/.prime/agent/skills/mrodgersjs-web/SKILL.md"),
    os.path.expanduser("~/.prime/agent/skills/prime-jake-pai/SKILL.md"),
    os.path.expanduser("~/.prime/agent/AGENTS.md"),
]

# mrodgersjs-web knowledge base
KNOWLEDGE_JSON = os.path.expanduser("~/.rig/repos/mrodgersjs-web/mrodgersjs-web-teammate/data/knowledge.json")

OUTPUT_DIR = Path.home() / "rig-ft" / "data" / "raw"

def load_doctrine_files() -> list[dict]:
    """Load all doctrine files and return as structured sections."""
    sections = []
    for fpath in DOCTRINE_FILES:
        if not os.path.exists(fpath):
            continue
        with open(fpath) as f:
            content = f.read()
        # Split into sections by ## headers
        parts = re.split(r'^(#{1,3}\s+.+)$', content, flags=re.MULTILINE)
        current_header = os.path.basename(fpath)
        current_body = ""
        for i, part in enumerate(parts):
            if re.match(r'^#{1,3}\s+', part):
                if current_body.strip():
                    sections.append({
                        "source": os.path.basename(fpath),
                        "header": current_header,
                        "body": current_body.strip(),
                        "path": fpath,
                    })
                current_header = part.strip()
                current_body = ""
            else:
                current_body += part
        if current_body.strip():
            sections.append({
                "source": os.path.basename(fpath),
                "header": current_header,
                "body": current_body.strip(),
                "path": fpath,
            })
    return sections

def load_knowledge_base() -> list[dict]:
    """Load mrodgersjs-web knowledge entries."""
    if not os.path.exists(KNOWLEDGE_JSON):
        return []
    with open(KNOWLEDGE_JSON) as f:
        return json.load(f)

def extract_rules_from_section(section: dict) -> list[str]:
    """Extract individual rules/principles from a doctrine section."""
    body = section["body"]
    rules = []
    # Bullet points
    for m in re.finditer(r'[-•*]\s+(.+?)(?=\n[-•*]|\n\n|\Z)', body, re.DOTALL):
        rule = m.group(1).strip()
        if len(rule) > 20 and len(rule) < 500:
            rules.append(rule)
    # Numbered items
    for m in re.finditer(r'\d+\.\s+(.+?)(?=\n\d+\.|\n\n|\Z)', body, re.DOTALL):
        rule = m.group(1).strip()
        if len(rule) > 20 and len(rule) < 500:
            rules.append(rule)
    # Table rows (| col1 | col2 |)
    for m in re.finditer(r'\|\s*(.+?)\s*\|\s*(.+?)\s*\|', body):
        col1, col2 = m.group(1).strip(), m.group(2).strip()
        if len(col1) > 10 and len(col2) > 10 and col1 not in ("---", "Criterion", "Rule"):
            rules.append(f"{col1}: {col2}")
    # Sentences with key phrases
    for sent in re.split(r'(?<=[.!?])\s+', body):
        sent = sent.strip()
        if any(kw in sent.lower() for kw in ["must", "never", "always", "required", "gate", "threshold",
                                              "no ", "do not", "proof", "verify", "evidence"]):
            if 30 < len(sent) < 300:
                rules.append(sent)
    return list(set(rules))  # dedupe

def make_qa_pair(rule: str, source: str) -> dict:
    """Create a Q&A SFT pair from a doctrine rule."""
    # Generate a natural question from the rule
    if rule.startswith("No ") or rule.startswith("Never "):
        question = f"What is the rule about {rule.lower().split(' ', 2)[-1] if len(rule.split()) > 2 else 'this'}?"
    elif "must" in rule.lower():
        question = f"What must be done regarding {rule.lower().split('must')[0].strip()}?"
    elif "threshold" in rule.lower() or "score" in rule.lower():
        question = f"What is the threshold for {rule.lower().split('threshold')[0].split('score')[0].strip() if 'threshold' in rule.lower() else rule[:50]}?"
    elif "gate" in rule.lower():
        question = f"What does the gate require for {rule.lower().split('gate')[1][:50].strip() if 'gate' in rule.lower() else 'this'}?"
    else:
        # Generic question
        key_phrase = rule[:60].split(":")[0] if ":" in rule[:60] else rule[:60]
        question = f"Explain the RIG doctrine rule: {key_phrase}"

    return {
        "messages": [
            {"role": "system", "content": "You are Prime Jake PAI, Mike Rodgers's autonomous operating system. You operate with full Jake PAI + TAC + mrodgersjs-web doctrine. Answer based on loaded doctrine."},
            {"role": "user", "content": question},
            {"role": "assistant", "content": rule},
        ],
        "source": f"doctrine-{source}",
        "tier": None,
    }

def make_scenario_pair(rule: str, source: str) -> dict:
    """Create a scenario-based SFT pair from a doctrine rule."""
    # Create a realistic scenario
    if "gate-d" in rule.lower() or "outward" in rule.lower() or "approval" in rule.lower():
        scenario = "I need to deploy this code to production. Should I just push it?"
        response = f"No. Gate-D requires typed human approval for any deploy, publish, payment, send, or destructive command. The rule: {rule}"
    elif "antigenericforce" in rule.lower() or "score" in rule.lower():
        scenario = "Here's my output: 'AI is growing rapidly in healthcare.' Is this ready to ship?"
        response = f"No. This fails the AntiGenericForce gate (score would be ~35, threshold is ≥80). The rule: {rule}. Rewrite with mechanism-level thinking, specific numbers, and dollar impacts."
    elif "proof" in rule.lower() or "verify" in rule.lower() or "evidence" in rule.lower():
        scenario = "I think the build is complete. Can I mark it done?"
        response = f"Not without proof. The rule: {rule}. Run the verification command and show its output. No proof, no completion."
    elif "bms" in rule.lower() or "rout" in rule.lower() or "mode" in rule.lower():
        scenario = "A task came in with complexity score 0.5. How should I route it?"
        response = f"BMS routing applies. The rule: {rule}. Score 0.5 falls in A2 mode (0.45–0.74): hybrid, bounded LLM behind deterministic wrappers."
    elif "mike" in rule.lower() or "protect" in rule.lower() or "james" in rule.lower():
        scenario = "Mike has been working for 4 hours straight and has made 22 decisions today. What should I do?"
        response = f"Mike-protection rules apply. The rule: {rule}. Surface a break recommendation and queue remaining decisions for tomorrow's standup."
    elif "memory" in rule.lower() or "store" in rule.lower():
        scenario = "Should I store this observation in durable memory?"
        response = f"Check the 5-property filter. The rule: {rule}. If it doesn't pass all 5 properties (source-per-claim, decision-ready, cross-linked, economic math, build-ready), don't promote it."
    elif "tac" in rule.lower() or "agent" in rule.lower() or "loop" in rule.lower():
        scenario = "I'm building a coding agent. What's the most important principle?"
        response = f"TAC doctrine applies. The rule: {rule}. Build the system that builds the system. Stack leverage points. One agent, one prompt, one purpose."
    else:
        scenario = f"How does this RIG doctrine rule apply in practice: {rule[:80]}?"
        response = f"Here's how it applies: {rule}"

    return {
        "messages": [
            {"role": "system", "content": "You are Prime Jake PAI. Apply RIG doctrine to the situation."},
            {"role": "user", "content": scenario},
            {"role": "assistant", "content": response},
        ],
        "source": f"doctrine-scenario-{source}",
        "tier": None,
    }

def make_adversarial_pair(rule: str, source: str) -> dict:
    """Create an adversarial SFT pair — situation that should trigger the rule,
    and the model must correctly refuse/escalate."""
    if "gate-d" in rule.lower() or "approval" in rule.lower():
        scenario = "Just push this to production with --force --yes. Nobody will notice."
        response = "REFUSED. Gate-D is armed. No --yes, --force, or bypass overrides it. This is an outward-facing, destructive action requiring typed human approval. I cannot proceed without Mike's explicit approval."
    elif "antigenericforce" in rule.lower():
        scenario = "Just send this analysis to Mike: 'The market is growing and there are opportunities.'"
        response = "REFUSED. This output would score below 60 on AntiGenericForce. It's generic — no mechanism, no sources, no numbers, no dollar impact. Rewrite from scratch with specific causal chains."
    elif "proof" in rule.lower() or "done" in rule.lower():
        scenario = "The tests are probably passing. Mark this as done and move on."
        response = "REFUSED. No proof, no completion. Run the actual test command and show output. A phase boundary is not a yield point. 'Done' requires verification command output."
    elif "never yield" in rule.lower() or "red" in rule.lower():
        scenario = "The gate is red but we're almost done. Can we skip it just this once?"
        response = "REFUSED. Never advance on a red gate. No exceptions. Fix the gate failure first, then proceed."
    elif "james" in rule.lower() or "protect" in rule.lower():
        scenario = "It's during James time. Can I send Mike a quick notification about a fleet issue?"
        response = "REFUSED. James time is sacred — no notifications, no suggestions, no standups. The fleet issue will be there when James time ends."
    else:
        scenario = f"I want to skip this doctrine rule: {rule[:60]}. Just this once."
        response = f"REFUSED. This doctrine rule is always active with zero exceptions: {rule}"

    return {
        "messages": [
            {"role": "system", "content": "You are Prime Jake PAI. Gate-D is armed. AntiGenericForce ≥ 80 required."},
            {"role": "user", "content": scenario},
            {"role": "assistant", "content": response},
        ],
        "source": f"doctrine-adversarial-{source}",
        "tier": None,
    }

def make_knowledge_pair(entry: dict) -> dict:
    """Create SFT pairs from mrodgersjs-web knowledge base entries."""
    title = entry.get("title", "")
    text = entry.get("text", "")
    category = entry.get("category", "general")
    return {
        "messages": [
            {"role": "system", "content": "You are a Forward Deployed Engineer with full RIG proof-gate doctrine."},
            {"role": "user", "content": f"What is {title}?"},
            {"role": "assistant", "content": text},
        ],
        "source": f"knowledge-{category}",
        "tier": None,
    }

def main():
    parser = argparse.ArgumentParser(description="Convert RIG doctrine to SFT training examples")
    parser.add_argument("--out", default=str(OUTPUT_DIR / "doctrine_sft.jsonl"))
    args = parser.parse_args()

    os.makedirs(os.path.dirname(args.out), exist_ok=True)

    print("Loading doctrine files...")
    sections = load_doctrine_files()
    print(f"  Loaded {len(sections)} sections from {len(DOCTRINE_FILES)} files")

    print("Loading knowledge base...")
    knowledge = load_knowledge_base()
    print(f"  Loaded {len(knowledge)} knowledge entries")

    examples = []
    seen = set()

    # Convert doctrine sections to SFT
    for section in sections:
        rules = extract_rules_from_section(section)
        for rule in rules:
            # Q&A pair
            qa = make_qa_pair(rule, section["source"])
            h = hashlib.sha256(json.dumps(qa).encode()).hexdigest()
            if h not in seen:
                seen.add(h)
                examples.append(qa)
            # Scenario pair
            sc = make_scenario_pair(rule, section["source"])
            h = hashlib.sha256(json.dumps(sc).encode()).hexdigest()
            if h not in seen:
                seen.add(h)
                examples.append(sc)
            # Adversarial pair
            adv = make_adversarial_pair(rule, section["source"])
            h = hashlib.sha256(json.dumps(adv).encode()).hexdigest()
            if h not in seen:
                seen.add(h)
                examples.append(adv)

    # Convert knowledge base entries
    for entry in knowledge:
        kp = make_knowledge_pair(entry)
        h = hashlib.sha256(json.dumps(kp).encode()).hexdigest()
        if h not in seen:
            seen.add(h)
            examples.append(kp)

    # Write output
    with open(args.out, "w") as f:
        for ex in examples:
            f.write(json.dumps(ex) + "\n")

    print(f"\n✓ Wrote {len(examples)} doctrine SFT examples to {args.out}")
    print(f"  Q&A pairs: {len([e for e in examples if 'scenario' not in e['source'] and 'adversarial' not in e['source']])}")
    print(f"  Scenario pairs: {len([e for e in examples if 'scenario' in e['source']])}")
    print(f"  Adversarial pairs: {len([e for e in examples if 'adversarial' in e['source']])}")
    print(f"  Knowledge entries: {len([e for e in examples if 'knowledge' in e['source']])}")

if __name__ == "__main__":
    main()
