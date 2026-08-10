#!/usr/bin/env python3
"""
arxiv_consensus_scraper.py
==========================
Fetches recent arXiv papers across cs.SE / cs.AI / cs.CL / cs.LG and emits
axolotl-compatible SFT pairs (chat_template format) to JSONL.

Optionally enriches TL;DRs via the Consensus.app API when CONSENSUS_API_KEY
is present in the environment; otherwise runs arxiv-only.

Output: /home/user/rig-ft/data/raw/arxiv_papers.jsonl

Usage:
    python3 arxiv_consensus_scraper.py [--max 200] [--categories cs.SE,cs.AI]
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from pathlib import Path

import arxiv
import requests

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
DEFAULT_CATEGORIES = ["cs.SE", "cs.AI", "cs.CL", "cs.LG"]
DEFAULT_MAX = 200
OUTPUT_PATH = Path("/home/user/rig-ft/data/raw/arxiv_papers.jsonl")
CONSENSUS_API_URL = "https://api.consensus.app/v1/search"
CONSENSUS_API_KEY_ENV = "CONSENSUS_API_KEY"
ARXIV_DELAY_SECONDS = 3.0  # arXiv asks for >=3s between requests
SYSTEM_PROMPT = (
    "You are an AI research assistant that summarizes and explains recent "
    "research papers in software engineering, artificial intelligence, "
    "natural language processing, and machine learning."
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def split_sentences(text: str) -> list[str]:
    """Split text into sentences, robust to common abbreviations."""
    text = re.sub(r"\s+", " ", text.strip())
    if not text:
        return []
    # Split on . ! ? followed by whitespace + capital, keeping the delimiter.
    parts = re.split(r"(?<=[.!?])\s+(?=[A-Z0-9])", text)
    return [p.strip() for p in parts if p.strip()]


def make_tldr(abstract: str) -> str:
    """TL;DR = first two sentences of the abstract."""
    sentences = split_sentences(abstract)
    if not sentences:
        return ""
    if len(sentences) == 1:
        return sentences[0]
    return " ".join(sentences[:2])


def extract_key_findings(abstract: str) -> str:
    """
    Extract key findings from an abstract.

    Heuristic: prefer sentences containing result/contribution signal words;
    fall back to the last two sentences (typically results/conclusions in a
    structured abstract), and finally to the whole abstract if it is short.
    """
    sentences = split_sentences(abstract)
    if not sentences:
        return ""
    if len(sentences) <= 2:
        return " ".join(sentences)

    signal_words = (
        "result", "results", "show", "shows", "demonstrate", "demonstrates",
        "achieve", "achieves", "outperform", "outperforms", "improve",
        "improves", "find", "finds", "found", "propose", "proposes",
        "introduce", "introduces", "present", "presents", "conclude",
        "concludes", "enable", "enables", "yield", "yields", "reveal",
        "reveals", "observed", "observe", "state-of-the-art", "sota",
    )

    hits = [s for s in sentences if any(w in s.lower() for w in signal_words)]
    if hits:
        return " ".join(hits[:3])

    # Fallback: last two sentences (results/conclusions slot).
    return " ".join(sentences[-2:])


def format_authors(authors: list) -> str:
    """Format arxiv Author objects into a comma-separated string."""
    names = []
    for a in authors:
        name = getattr(a, "name", str(a))
        if name:
            names.append(name)
    return ", ".join(names)


# ---------------------------------------------------------------------------
# Consensus enrichment
# ---------------------------------------------------------------------------

def consensus_search(query: str, api_key: str, timeout: float = 15.0) -> dict | None:
    """
    Query the Consensus.app search API for a paper TL;DR.

    Returns the parsed JSON response (dict) on success, or None on any error.
    The endpoint is treated as best-effort enrichment — failures degrade
    gracefully to arxiv-only data.
    """
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    payload = {"query": query, "limit": 1}
    try:
        resp = requests.post(
            CONSENSUS_API_URL, headers=headers, json=payload, timeout=timeout
        )
        if resp.status_code != 200:
            return None
        data = resp.json()
        if isinstance(data, dict):
            return data
        return None
    except (requests.RequestException, ValueError):
        return None


def consensus_tldr(title: str, api_key: str) -> str | None:
    """
    Attempt to fetch a Consensus TL;DR for a paper title.

    Consensus returns a list of items under 'items' (or 'results'); each item
    may have a 'tldr' / 'abstract' / 'title' field. We look for the first
    non-empty TL;DR-like field.
    """
    data = consensus_search(title, api_key)
    if not data:
        return None
    items = data.get("items") or data.get("results") or []
    if not isinstance(items, list):
        return None
    for item in items:
        if not isinstance(item, dict):
            continue
        for key in ("tldr", "tl_dr", "summary", "takeaway", "abstract"):
            val = item.get(key)
            if isinstance(val, str) and val.strip():
                return val.strip()
    return None


# ---------------------------------------------------------------------------
# SFT pair construction
# ---------------------------------------------------------------------------

def build_sft_pair(
    title: str,
    abstract: str,
    authors: str,
    arxiv_id: str,
    categories: list[str],
    tldr_override: str | None = None,
) -> dict:
    """
    Build an axolotl chat_template SFT pair.

    Multi-turn:
      system -> user (summarize) -> assistant (TL;DR)
              -> user (key findings) -> assistant (findings)
    """
    tldr = (tldr_override or "").strip() or make_tldr(abstract)
    findings = extract_key_findings(abstract)

    pair = {
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": f"Summarize this paper: {title}"},
            {"role": "assistant", "content": tldr},
            {"role": "user", "content": "What are the key findings?"},
            {"role": "assistant", "content": findings},
        ],
        "source": "arxiv",
        "tier": None,
        # Extra metadata (non-load-bearing for training; useful for the
        # fermenter's A/B/C tiering pass).
        "metadata": {
            "arxiv_id": arxiv_id,
            "title": title,
            "authors": authors,
            "categories": categories,
            "consensus_enriched": tldr_override is not None,
        },
    }
    return pair


# ---------------------------------------------------------------------------
# arXiv fetching
# ---------------------------------------------------------------------------

def build_query(categories: list[str]) -> str:
    """Build an arXiv API query string for the given categories."""
    terms = [f"cat:{c}" for c in categories]
    return " OR ".join(terms)


def fetch_arxiv_papers(
    categories: list[str], max_results: int
) -> list[arxiv.Result]:
    """
    Fetch recent arXiv papers for the given categories, sorted by submission
    date (newest first). Paginates internally since the arXiv API caps page
    size; Client.results() handles pagination transparently.
    """
    query = build_query(categories)
    search = arxiv.Search(
        query=query,
        max_results=max_results,
        sort_by=arxiv.SortCriterion.SubmittedDate,
        sort_order=arxiv.SortOrder.Descending,
    )
    client = arxiv.Client(page_size=100, delay_seconds=ARXIV_DELAY_SECONDS, num_retries=3)
    results: list[arxiv.Result] = []
    try:
        for r in client.results(search):
            results.append(r)
            if len(results) >= max_results:
                break
    except Exception as exc:  # pragma: no cover - network resilience
        print(f"[warn] arxiv fetch interrupted: {exc}", file=sys.stderr)
    return results


# ---------------------------------------------------------------------------
# Main pipeline
# ---------------------------------------------------------------------------

def run(
    categories: list[str],
    max_results: int,
    output_path: Path,
    use_consensus: bool | None = None,
) -> int:
    """Run the scrape pipeline. Returns number of records written."""
    output_path.parent.mkdir(parents=True, exist_ok=True)

    api_key = os.environ.get(CONSENSUS_API_KEY_ENV, "").strip()
    if use_consensus is None:
        use_consensus = bool(api_key)
    if use_consensus and not api_key:
        use_consensus = False

    print(
        f"[arxiv_consensus_scraper] categories={categories} max={max_results} "
        f"consensus={'on' if use_consensus else 'off'} -> {output_path}",
        file=sys.stderr,
    )

    papers = fetch_arxiv_papers(categories, max_results)
    print(f"[arxiv_consensus_scraper] fetched {len(papers)} papers from arXiv", file=sys.stderr)

    written = 0
    skipped = 0
    with output_path.open("w", encoding="utf-8") as fh:
        for paper in papers:
            title = (paper.title or "").strip().replace("\n", " ")
            abstract = (paper.summary or "").strip().replace("\n", " ")
            if not title or not abstract:
                skipped += 1
                continue

            authors = format_authors(paper.authors)
            arxiv_id = paper.get_short_id()
            cats = paper.categories or [paper.primary_category]

            tldr_override = None
            if use_consensus:
                tldr_override = consensus_tldr(title, api_key)
                # Be polite to the Consensus API.
                time.sleep(0.5)

            pair = build_sft_pair(
                title=title,
                abstract=abstract,
                authors=authors,
                arxiv_id=arxiv_id,
                categories=cats,
                tldr_override=tldr_override,
            )
            fh.write(json.dumps(pair, ensure_ascii=False) + "\n")
            written += 1

    print(
        f"[arxiv_consensus_scraper] wrote {written} records "
        f"(skipped {skipped}) to {output_path}",
        file=sys.stderr,
    )
    return written


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Scrape arXiv papers into axolotl SFT pairs. "
        "Optionally enrich via Consensus.app API (CONSENSUS_API_KEY env)."
    )
    p.add_argument(
        "--max",
        type=int,
        default=DEFAULT_MAX,
        help=f"Maximum number of papers to fetch (default: {DEFAULT_MAX}).",
    )
    p.add_argument(
        "--categories",
        type=str,
        default=",".join(DEFAULT_CATEGORIES),
        help="Comma-separated arXiv categories (default: cs.SE,cs.AI,cs.CL,cs.LG).",
    )
    p.add_argument(
        "--output",
        type=str,
        default=str(OUTPUT_PATH),
        help=f"Output JSONL path (default: {OUTPUT_PATH}).",
    )
    p.add_argument(
        "--no-consensus",
        action="store_true",
        help="Disable Consensus enrichment even if CONSENSUS_API_KEY is set.",
    )
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    categories = [c.strip() for c in args.categories.split(",") if c.strip()]
    if not categories:
        print("[error] no categories provided", file=sys.stderr)
        return 2

    use_consensus = None if not args.no_consensus else False
    written = run(
        categories=categories,
        max_results=args.max,
        output_path=Path(args.output),
        use_consensus=use_consensus,
    )
    return 0 if written >= 0 else 1


if __name__ == "__main__":
    sys.exit(main())
