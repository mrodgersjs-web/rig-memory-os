#!/usr/bin/env python3
"""
Prime Jake — Business Intelligence Data Collector

Collects 50GB+ of training data across gap areas not covered by the existing
vault. The vault has strong coverage of RIG/GTM/banking/healthcare/law but
is missing: psychology, startup growth, business frameworks, sales psychology,
negotiation, leadership, fundraising, operations, and more.

Data sources:
- HuggingFace datasets (business, psychology, instruction-following)
- YouTube transcripts (business coaching, startup, psychology)
- Reddit (r/startups, r/Entrepreneur, r/business, r/psychology)
- arXiv (business, decision science, organizational behavior)
- Web scraping (frameworks, playbooks, case studies)
- Podcast transcripts (business, startup, psychology)

Usage:
    python3 business_intel_collector.py [--topics all]
    python3 business_intel_collector.py --topics psychology,startup,sales
"""
from __future__ import annotations
import os, sys, json, subprocess, argparse, time, urllib.request, urllib.error
from pathlib import Path
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor, as_completed

OUTPUT_DIR = Path.home() / "rig-ft" / "data" / "business_intel"
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

# ── Topic Definitions ───────────────────────────────────────────────────────

TOPICS = {
    "psychology": {
        "description": "Cognitive psychology, behavioral economics, decision science",
        "hf_datasets": [
            ("Anthropic/hh-rlhf", "helpful"),  # Human preference data
            ("HuggingFaceH4/ultrachat_200k", "train_sft"),  # Conversational
        ],
        "reddit_subs": ["psychology", "cognitiveScience", "BehavioralEconomics", "decisiontheory"],
        "youtube_queries": ["cognitive bias decision making", "behavioral economics explained",
                           "psychology of persuasion", "negotiation psychology", "leadership psychology"],
        "arxiv_categories": ["cs.HC", "stat.AP", "econ.GN"],
    },
    "startup": {
        "description": "Startup growth, fundraising, product-market fit, scaling",
        "hf_datasets": [],
        "reddit_subs": ["startups", "Entrepreneur", "smallbusiness", "SaaS", "indiehackers"],
        "youtube_queries": ["startup growth strategy", "how to raise venture capital",
                           "product market fit", "startup scaling", "YC startup school",
                           "bootstrapped startup", "startup mistakes to avoid"],
        "arxiv_categories": ["econ.GN"],
    },
    "sales": {
        "description": "Sales psychology, negotiation, B2B outreach, closing techniques",
        "hf_datasets": [],
        "reddit_subs": ["sales", "salestechniques", "B2BSales", "coldemail"],
        "youtube_queries": ["B2B sales strategy", "cold email outreach", "sales psychology",
                           "negotiation tactics", "consultative selling", "SPIN selling"],
        "arxiv_categories": [],
    },
    "business_frameworks": {
        "description": "Business model frameworks, strategic planning, OKRs, operations",
        "hf_datasets": [],
        "reddit_subs": ["business", "management", "strategicmanagement"],
        "youtube_queries": ["business model canvas", "OKR framework", "Porter's five forces",
                           "lean startup methodology", "business strategy frameworks",
                           "operating model design", "organizational design"],
        "arxiv_categories": [],
    },
    "leadership": {
        "description": "Leadership, management, team building, organizational culture",
        "hf_datasets": [],
        "reddit_subs": ["leadership", "management", "humanresources", "organizationalbehavior"],
        "youtube_queries": ["leadership principles", "how to build a team", "management frameworks",
                           "organizational culture", "servant leadership", "high performance teams"],
        "arxiv_categories": [],
    },
    "finance": {
        "description": "Accounting, financial modeling, startup finance, unit economics",
        "hf_datasets": [],
        "reddit_subs": ["finance", "accounting", "FinancialAnalyst", "startupfinance"],
        "youtube_queries": ["startup financial model", "unit economics SaaS", "financial planning",
                           "burn rate calculation", "SaaS metrics", "revenue recognition"],
        "arxiv_categories": ["q-fin.CP", "q-fin.PM"],
    },
    "marketing": {
        "description": "Marketing strategy, content marketing, SEO, brand positioning",
        "hf_datasets": [],
        "reddit_subs": ["marketing", "digital_marketing", "SEO", "contentmarketing"],
        "youtube_queries": ["content marketing strategy", "SEO fundamentals", "brand positioning",
                           "marketing funnel", "growth hacking", "marketing automation"],
        "arxiv_categories": [],
    },
    "product": {
        "description": "Product management, UX, product strategy, user research",
        "hf_datasets": [],
        "reddit_subs": ["productmanagement", "ProductManagement", "UXDesign", "userresearch"],
        "youtube_queries": ["product management frameworks", "user research methods",
                           "product strategy", "PRD writing", "product roadmap",
                           "Jobs to be Done", "product discovery"],
        "arxiv_categories": ["cs.HC"],
    },
    "operations": {
        "description": "Operations, supply chain, process design, systems thinking",
        "hf_datasets": [],
        "reddit_subs": ["operations", "supplychain", "processimprovement"],
        "youtube_queries": ["operations management", "systems thinking", "process optimization",
                           "lean operations", "supply chain management", "six sigma"],
        "arxiv_categories": [],
    },
    "legal_formation": {
        "description": "Company formation, legal structures, contracts, compliance",
        "hf_datasets": [],
        "reddit_subs": ["legaladvice", "smallbusinesslaw", "corporatelaw"],
        "youtube_queries": ["how to incorporate a business", "LLC vs C Corp", "startup legal",
                           "founder agreements", "equity distribution", "IP protection"],
        "arxiv_categories": [],
    },
}

# ── Collectors ──────────────────────────────────────────────────────────────

def collect_reddit(subreddit: str, max_posts: int = 100) -> list[dict]:
    """Collect posts from a subreddit."""
    examples = []
    try:
        url = f"https://www.reddit.com/r/{subreddit}/top.json?limit={max_posts}&t=month"
        req = urllib.request.Request(url, headers={"User-Agent": "RIG-Data-Collector/1.0"})
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read())
        
        for post in data.get("data", {}).get("children", []):
            d = post["data"]
            if d.get("score", 0) < 5:
                continue
            title = d.get("title", "")
            selftext = d.get("selftext", "")
            if not title or len(title) < 10:
                continue
            
            content = f"{title}\n\n{selftext}" if selftext else title
            examples.append({
                "messages": [
                    {"role": "system", "content": "You are a business intelligence assistant providing expert advice."},
                    {"role": "user", "content": title},
                    {"role": "assistant", "content": content[:4000]},
                ],
                "source": f"reddit-{subreddit}",
                "tier": None,
            })
    except Exception as e:
        print(f"  ✗ Reddit r/{subreddit}: {e}")
    return examples

def collect_youtube(query: str, max_videos: int = 10) -> list[dict]:
    """Collect YouTube transcripts for a search query."""
    examples = []
    try:
        result = subprocess.run(
            ["yt-dlp", "--dump-json", "--flat-playlist",
             f"ytsearch{max_videos}:{query}"],
            capture_output=True, text=True, timeout=60
        )
        if result.returncode != 0:
            return examples
        
        for line in result.stdout.strip().split("\n"):
            if not line:
                continue
            try:
                video = json.loads(line)
                video_id = video.get("id", "")
                title = video.get("title", "")
                if not video_id or not title:
                    continue
                
                # Get transcript
                transcript_result = subprocess.run(
                    ["yt-dlp", "--write-auto-sub", "--sub-lang", "en", "--skip-download",
                     "--output", "/tmp/yt_transcript", f"https://www.youtube.com/watch?v={video_id}"],
                    capture_output=True, text=True, timeout=30
                )
                
                # Read transcript
                import glob
                sub_files = glob.glob("/tmp/yt_transcript*.vtt") + glob.glob("/tmp/yt_transcript*.srt")
                if sub_files:
                    with open(sub_files[0]) as f:
                        transcript = f.read()
                    # Clean VTT/SRT format
                    import re
                    transcript = re.sub(r'\d{2}:\d{2}:\d{2}[.,]\d{3} --> \d{2}:\d{2}:\d{2}[.,]\d{3}', '', transcript)
                    transcript = re.sub(r'WEBVTT.*?\n', '', transcript)
                    transcript = re.sub(r'\n{3,}', '\n\n', transcript).strip()
                    
                    if len(transcript) > 100:
                        # Split into segments
                        segments = transcript.split('\n\n')
                        for i in range(0, len(segments), 5):
                            segment = '\n'.join(segments[i:i+5])
                            if len(segment) > 50:
                                examples.append({
                                    "messages": [
                                        {"role": "system", "content": "You are a business coach explaining concepts from video content."},
                                        {"role": "user", "content": f"Explain: {title}"},
                                        {"role": "assistant", "content": segment[:4000]},
                                    ],
                                    "source": f"youtube-{query[:30]}",
                                    "tier": None,
                                })
                    
                    # Clean up
                    for sf in sub_files:
                        os.remove(sf)
                
                time.sleep(1)  # Rate limit
            except:
                continue
    except Exception as e:
        print(f"  ✗ YouTube '{query}': {e}")
    return examples

def collect_arxiv(category: str, max_papers: int = 20) -> list[dict]:
    """Collect arXiv papers for a category."""
    examples = []
    try:
        import arxiv
        search = arxiv.Search(query=f"cat:{category}", max_results=max_papers, sort_by=arxiv.SortCriterion.Relevance)
        for paper in search.results():
            title = paper.title
            abstract = paper.summary
            examples.append({
                "messages": [
                    {"role": "system", "content": "You are a research assistant summarizing academic papers for business application."},
                    {"role": "user", "content": f"Summarize this paper: {title}"},
                    {"role": "assistant", "content": f"**{title}**\n\n{abstract}"},
                ],
                "source": f"arxiv-{category}",
                "tier": None,
            })
    except Exception as e:
        print(f"  ✗ arXiv {category}: {e}")
    return examples

def collect_hf_dataset(dataset_id: str, split: str, max_samples: int = 5000) -> list[dict]:
    """Download a HuggingFace dataset."""
    examples = []
    try:
        from datasets import load_dataset
        ds = load_dataset(dataset_id, split=split, streaming=True)
        for i, item in enumerate(ds):
            if i >= max_samples:
                break
            # Try to extract conversation/instruction format
            if "messages" in item:
                msgs = item["messages"]
                if isinstance(msgs, list) and len(msgs) >= 2:
                    clean = [{"role": m.get("role","user"), "content": str(m.get("content",""))[:4000]} for m in msgs]
                    examples.append({"messages": clean, "source": f"hf-{dataset_id}", "tier": None})
            elif "chosen" in item and "rejected" in item:
                # Preference data
                chosen = item["chosen"]
                if isinstance(chosen, list):
                    clean = [{"role": m.get("role","user"), "content": str(m.get("content",""))[:4000]} for m in chosen]
                    if len(clean) >= 2:
                        examples.append({"messages": clean, "source": f"hf-{dataset_id}-chosen", "tier": None})
            elif "instruction" in item and "output" in item:
                examples.append({
                    "messages": [
                        {"role": "user", "content": str(item["instruction"])[:4000]},
                        {"role": "assistant", "content": str(item["output"])[:4000]},
                    ],
                    "source": f"hf-{dataset_id}",
                    "tier": None,
                })
        print(f"  ✓ {dataset_id}: {len(examples)} samples")
    except Exception as e:
        print(f"  ✗ {dataset_id}: {e}")
    return examples

def collect_topic(topic_name: str) -> dict:
    """Collect all data for a single topic."""
    topic = TOPICS[topic_name]
    print(f"\n{'='*50}")
    print(f"Collecting: {topic_name} — {topic['description']}")
    print(f"{'='*50}")
    
    all_examples = []
    
    # Reddit
    for sub in topic["reddit_subs"]:
        print(f"  Reddit r/{sub}...")
        examples = collect_reddit(sub, max_posts=50)
        all_examples.extend(examples)
        print(f"    → {len(examples)} posts")
        time.sleep(2)
    
    # YouTube
    for query in topic["youtube_queries"]:
        print(f"  YouTube: {query}...")
        examples = collect_youtube(query, max_videos=5)
        all_examples.extend(examples)
        print(f"    → {len(examples)} segments")
        time.sleep(1)
    
    # arXiv
    for cat in topic["arxiv_categories"]:
        print(f"  arXiv: {cat}...")
        examples = collect_arxiv(cat, max_papers=20)
        all_examples.extend(examples)
        print(f"    → {len(examples)} papers")
    
    # HF datasets
    for ds_id, ds_split in topic["hf_datasets"]:
        print(f"  HF: {ds_id}...")
        examples = collect_hf_dataset(ds_id, ds_split, max_samples=5000)
        all_examples.extend(examples)
    
    # Write to file
    outfile = OUTPUT_DIR / f"{topic_name}.jsonl"
    with open(outfile, "w") as f:
        for ex in all_examples:
            f.write(json.dumps(ex) + "\n")
    
    print(f"\n  ✓ {topic_name}: {len(all_examples)} examples → {outfile}")
    return {"topic": topic_name, "count": len(all_examples), "file": str(outfile)}

def main():
    parser = argparse.ArgumentParser(description="Business Intelligence Data Collector")
    parser.add_argument("--topics", default="all", help="comma-separated topic names or 'all'")
    args = parser.parse_args()
    
    if args.topics == "all":
        topics = list(TOPICS.keys())
    else:
        topics = args.topics.split(",")
    
    print(f"Collecting data for {len(topics)} topics: {', '.join(topics)}")
    print(f"Output: {OUTPUT_DIR}")
    
    # Collect in parallel
    results = []
    with ThreadPoolExecutor(max_workers=3) as pool:
        futures = {pool.submit(collect_topic, t): t for t in topics}
        for f in as_completed(futures):
            results.append(f.result())
    
    # Summary
    print(f"\n{'='*60}")
    print(f"COLLECTION COMPLETE")
    print(f"{'='*60}")
    total = 0
    for r in results:
        print(f"  {r['topic']}: {r['count']} examples")
        total += r["count"]
    print(f"\n  Total: {total} examples across {len(results)} topics")
    print(f"  Output: {OUTPUT_DIR}")

if __name__ == "__main__":
    main()
