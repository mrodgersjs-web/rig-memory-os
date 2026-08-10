#!/usr/bin/env python3
"""
Stack Overflow Fast-Accepted Answer Scraper
============================================
Finds Stack Overflow Q&A pairs where an answer was accepted within 5 minutes
of the question being asked, then converts them to SFT (Supervised Fine-Tuning)
format for model training.

Uses the Stack Exchange API (no key required, but uses one if available).
Rate-limits to 100 requests/second (the anonymous API limit; 300/sec with key).

Output: /home/user/rig-ft/data/raw/stackoverflow_fast.jsonl
Topics: /home/user/rig-ft/data/raw/stackoverflow_topics.jsonl

Usage:
    python3 stackoverflow_scraper.py [--max 2000] [--tags python,javascript]
"""

import argparse
import json
import os
import re
import sys
import time
from html import unescape
from pathlib import Path

try:
    import requests
except ImportError:
    sys.stderr.write("Error: 'requests' package is required. Install with: pip install requests\n")
    sys.exit(1)


# ─── Constants ─────────────────────────────────────────────────────────────────

API_BASE = "https://api.stackexchange.com/2.3"
SITE = "stackoverflow"
DEFAULT_TAGS = [
    "python", "javascript", "typescript", "rust", "go",
    "react", "docker", "kubernetes", "sql", "git",
]
OUTPUT_DIR = Path("/home/user/rig-ft/data/raw")
OUTPUT_FILE = OUTPUT_DIR / "stackoverflow_fast.jsonl"
TOPICS_FILE = OUTPUT_DIR / "stackoverflow_topics.jsonl"

FAST_ACCEPT_THRESHOLD_SEC = 5 * 60  # 5 minutes
MIN_QUESTION_SCORE = 5
MIN_ANSWER_SCORE = 3
PAGE_SIZE = 100  # max items per API page
MAX_ANSWER_BATCH = 100  # max answer IDs per /answers/{ids} call
RATE_LIMIT_INTERVAL = 0.01  # 100 req/sec = 0.01s between requests

# Filter that includes body markdown for questions/answers
BODY_FILTER = "withbody"

# Stopwords for topic normalization (standard English, no external deps)
STOPWORDS = frozenset({
    "a", "an", "the", "and", "or", "but", "if", "then", "else", "when",
    "at", "by", "for", "with", "about", "against", "between", "into",
    "through", "during", "before", "after", "above", "below", "to", "from",
    "up", "down", "in", "out", "on", "off", "over", "under", "again",
    "further", "once", "here", "there", "all", "any", "both", "each",
    "few", "more", "most", "other", "some", "such", "no", "nor", "not",
    "only", "own", "same", "so", "than", "too", "very", "can", "will",
    "just", "don", "should", "now", "is", "are", "was", "were", "be",
    "been", "being", "have", "has", "had", "having", "do", "does", "did",
    "doing", "would", "could", "should", "ought", "i", "me", "my", "we",
    "our", "you", "your", "he", "him", "his", "she", "her", "it", "its",
    "they", "them", "their", "what", "which", "who", "whom", "this",
    "that", "these", "those", "of", "as", "how", "why", "get", "getting",
    "using", "use", "way", "make", "makeing", "file", "one", "two",
})


# ─── API Key ───────────────────────────────────────────────────────────────────

def get_api_key():
    """Return Stack Exchange API key from environment if available."""
    return os.environ.get("STACK_EXCHANGE_API_KEY") or os.environ.get("SE_API_KEY") or None


# ─── Rate Limiter ──────────────────────────────────────────────────────────────

class RateLimiter:
    """Simple token-bucket-style rate limiter for N requests per second."""

    def __init__(self, max_per_sec):
        self.interval = 1.0 / max_per_sec
        self.last_request = 0.0

    def wait(self):
        now = time.monotonic()
        elapsed = now - self.last_request
        if elapsed < self.interval:
            time.sleep(self.interval - elapsed)
        self.last_request = time.monotonic()


# ─── HTML Stripping ────────────────────────────────────────────────────────────

_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")


def strip_html(html_text):
    """Convert HTML to plain text: strip tags, unescape entities, collapse whitespace."""
    if not html_text:
        return ""
    text = html_text
    # Replace <br> and <p> with newlines for readability
    text = re.sub(r"<br\s*/?>", "\n", text, flags=re.IGNORECASE)
    text = re.sub(r"</p>", "\n\n", text, flags=re.IGNORECASE)
    text = re.sub(r"</?(?:div|h[1-6]|li|ul|ol|tr|td|th)\b[^>]*>", "\n", text, flags=re.IGNORECASE)
    # Strip all remaining tags
    text = _TAG_RE.sub("", text)
    # Unescape HTML entities (loop to handle double-encoding from SE API)
    prev = None
    while prev != text:
        prev = text
        text = unescape(text)
    # Collapse whitespace but preserve paragraph breaks
    text = _WS_RE.sub(" ", text).strip()
    # Restore some paragraph structure
    text = re.sub(r" {2,}", "\n", text)
    return text.strip()


# ─── Topic Normalization ───────────────────────────────────────────────────────

def normalize_topic(title):
    """Normalize a question title into a topic keyphrase.

    Lowercase, strip HTML/punctuation, remove stopwords, keep remaining tokens.
    Falls back to raw lowercased title if all tokens are stopwords.
    """
    text = strip_html(title).lower()
    # Split on non-alphanumeric (keep hyphens within words)
    tokens = re.findall(r"[a-z0-9]+(?:-[a-z0-9]+)*", text)
    filtered = [t for t in tokens if t not in STOPWORDS and len(t) > 1]
    result = " ".join(filtered)
    # Fallback: if all tokens were stopwords/single-chars, use the raw tokens
    if not result and tokens:
        result = " ".join(tokens)
    return result


# ─── API Client ────────────────────────────────────────────────────────────────

class StackExchangeClient:
    """Thin wrapper around the Stack Exchange API with rate limiting and backoff."""

    def __init__(self, key=None):
        self.key = key
        self.session = requests.Session()
        self.session.headers.update({
            "Accept": "application/json",
            "Accept-Encoding": "gzip",
        })
        self.rate_limiter = RateLimiter(100 if not key else 300)
        self.quota_remaining = None
        self.request_count = 0

    def _build_params(self, extra=None):
        params = {"site": SITE}
        if self.key:
            params["key"] = self.key
        if extra:
            params.update(extra)
        return params

    def get(self, path, params=None):
        """Make a rate-limited GET request. Handles backoff from API."""
        url = f"{API_BASE}/{path.lstrip('/')}"
        full_params = self._build_params(params)

        while True:
            self.rate_limiter.wait()
            self.request_count += 1
            try:
                resp = self.session.get(url, params=full_params, timeout=30)
            except requests.RequestException as e:
                sys.stderr.write(f"  [warn] request error: {e}, retrying in 5s...\n")
                time.sleep(5)
                continue

            if resp.status_code == 429:
                sys.stderr.write("  [warn] rate limited (429), backing off 10s...\n")
                time.sleep(10)
                continue

            if resp.status_code != 200:
                sys.stderr.write(
                    f"  [error] HTTP {resp.status_code}: {resp.text[:200]}\n"
                )
                return {"items": [], "has_more": False}

            data = resp.json()
            self.quota_remaining = data.get("quota_remaining")

            # Check for backoff directive from API
            backoff = data.get("backoff")
            if backoff:
                sys.stderr.write(f"  [info] API requested backoff: {backoff}s\n")
                time.sleep(float(backoff) + 1)

            return data


# ─── Fetching Questions ────────────────────────────────────────────────────────

def fetch_questions(client, tag, min_score, max_results):
    """Fetch questions for a tag, paginating until max_results or exhaustion.

    Uses /search/advanced with accepted=True to pre-filter to questions
    that have an accepted answer. Sorts by votes to prioritize high-quality
    questions.
    """
    questions = []
    page = 1
    seen_ids = set()

    while len(questions) < max_results:
        remaining = max_results - len(questions)
        pagesize = min(PAGE_SIZE, remaining)

        params = {
            "order": "desc",
            "sort": "votes",
            "tagged": tag,
            "accepted": "True",
            "pagesize": pagesize,
            "page": page,
            "filter": BODY_FILTER,
        }

        data = client.get("search/advanced", params)

        items = data.get("items", [])
        if not items:
            break

        for q in items:
            qid = q.get("question_id")
            if qid is None or qid in seen_ids:
                continue
            seen_ids.add(qid)

            score = q.get("score", 0)
            if score < min_score:
                continue
            if not q.get("accepted_answer_id"):
                continue
            # Skip closed/deleted questions (deleted ones won't appear anyway,
            # but closed ones can; check for closed_date)
            if q.get("closed_date"):
                continue

            questions.append({
                "question_id": qid,
                "title": strip_html(q.get("title", "")),
                "body_html": q.get("body", ""),
                "body_text": strip_html(q.get("body", "")),
                "score": score,
                "creation_date": q.get("creation_date", 0),
                "accepted_answer_id": q["accepted_answer_id"],
                "tags": q.get("tags", []),
                "link": q.get("link", ""),
            })

        if not data.get("has_more"):
            break

        page += 1

        # Safety: don't paginate forever for a tag with low yield
        if page > 200:
            break

    return questions


# ─── Fetching Answers ──────────────────────────────────────────────────────────

def fetch_answers_batch(client, answer_ids):
    """Fetch answers by ID in batches (up to 100 IDs per API call).

    Returns dict: answer_id -> answer dict with body, score, creation_date, is_accepted.
    """
    results = {}
    id_list = list(answer_ids)

    for i in range(0, len(id_list), MAX_ANSWER_BATCH):
        batch = id_list[i:i + MAX_ANSWER_BATCH]
        ids_str = ";".join(str(aid) for aid in batch)

        params = {
            "order": "desc",
            "sort": "creation",
            "pagesize": len(batch),
            "filter": BODY_FILTER,
        }

        data = client.get(f"answers/{ids_str}", params)

        for a in data.get("items", []):
            results[a["answer_id"]] = {
                "answer_id": a["answer_id"],
                "question_id": a.get("question_id"),
                "body_html": a.get("body", ""),
                "body_text": strip_html(a.get("body", "")),
                "score": a.get("score", 0),
                "creation_date": a.get("creation_date", 0),
                "is_accepted": a.get("is_accepted", False),
            }

    return results


# ─── SFT Record Building ───────────────────────────────────────────────────────

def build_sft_record(question, answer):
    """Build an SFT record from a question and its accepted answer."""
    # Combine title + body for user message
    user_content = f"{question['title']}\n\n{question['body_text']}".strip()

    return {
        "messages": [
            {
                "role": "system",
                "content": (
                    "You are an expert developer answering a technical question. "
                    "Provide a clear, accurate, and practical solution."
                ),
            },
            {
                "role": "user",
                "content": user_content,
            },
            {
                "role": "assistant",
                "content": answer["body_text"],
            },
        ],
        "source": "stackoverflow-fast",
        "tier": None,
    }


def build_topic_record(question):
    """Build a topic manifest record for cross-scraper quorum."""
    return {
        "topic": normalize_topic(question["title"]),
        "source": "stackoverflow",
        "title": strip_html(question["title"]),
        "url": question.get("link", ""),
    }


# ─── Main Pipeline ─────────────────────────────────────────────────────────────

def run(tags, max_results, output_file, topics_file):
    """Main pipeline: fetch, filter, transform, write."""
    client = StackExchangeClient(key=get_api_key())
    key_status = "with key" if client.key else "anonymous (no key)"
    sys.stderr.write(f"Stack Exchange API: {key_status}\n")
    sys.stderr.write(f"Tags: {', '.join(tags)}\n")
    sys.stderr.write(f"Max results: {max_results}\n")
    sys.stderr.write(f"Output: {output_file}\n\n")

    output_file.parent.mkdir(parents=True, exist_ok=True)

    total_collected = 0
    total_api_calls = 0
    seen_question_ids = set()  # dedupe across tags

    # Open output files in append mode (so re-runs accumulate)
    # But first, load existing IDs to avoid duplicates
    if output_file.exists():
        with open(output_file, "r", encoding="utf-8") as f:
            for line in f:
                try:
                    rec = json.loads(line)
                    # Extract question_id from user content hash — not stored directly,
                    # so we use the URL or a content hash. Simpler: just track by
                    # a hash of the user message.
                    msg = rec.get("messages", [])
                    if len(msg) >= 2:
                        seen_question_ids.add(hash(msg[1].get("content", "")))
                except (json.JSONDecodeError, KeyError, IndexError):
                    pass
        sys.stderr.write(f"Loaded {len(seen_question_ids)} existing records for dedup\n")

    with open(output_file, "a", encoding="utf-8") as sft_out, \
         open(topics_file, "a", encoding="utf-8") as topics_out:

        for tag in tags:
            if total_collected >= max_results:
                break

            remaining = max_results - total_collected
            sys.stderr.write(f"\n{'─' * 60}\n")
            sys.stderr.write(f"Tag: {tag} (need {remaining} more)\n")
            sys.stderr.write(f"{'─' * 60}\n")

            # Fetch candidate questions with accepted answers
            sys.stderr.write(f"  Fetching questions for [{tag}]...\n")
            # Over-fetch questions since most won't be fast-accepted
            # Fetch up to 5x the remaining target, capped at a reasonable max
            fetch_target = min(remaining * 10, 5000)
            questions = fetch_questions(client, tag, MIN_QUESTION_SCORE, fetch_target)
            calls_before = client.request_count
            sys.stderr.write(
                f"  Got {len(questions)} candidate questions "
                f"(score >= {MIN_QUESTION_SCORE}, has accepted answer) "
                f"in {client.request_count - calls_before} API calls\n"
            )

            if not questions:
                sys.stderr.write(f"  No questions found for [{tag}]\n")
                continue

            # Fetch accepted answers in batches
            answer_ids = [q["accepted_answer_id"] for q in questions]
            sys.stderr.write(f"  Fetching {len(answer_ids)} accepted answers...\n")
            calls_before = client.request_count
            answers = fetch_answers_batch(client, answer_ids)
            sys.stderr.write(
                f"  Got {len(answers)} answers "
                f"in {client.request_count - calls_before} API calls\n"
            )

            # Filter for fast-accepted + quality
            tag_collected = 0
            for q in questions:
                if total_collected >= max_results:
                    break

                aid = q["accepted_answer_id"]
                if aid not in answers:
                    continue

                a = answers[aid]

                # Filter: answer score >= 3
                if a["score"] < MIN_ANSWER_SCORE:
                    continue

                # Filter: accepted within 5 minutes
                delta = a["creation_date"] - q["creation_date"]
                if delta < 0 or delta > FAST_ACCEPT_THRESHOLD_SEC:
                    continue

                # Filter: must be the accepted answer
                if not a["is_accepted"]:
                    continue

                # Dedup by content hash
                content_hash = hash(
                    f"{q['title']}\n\n{q['body_text']}"
                )
                if content_hash in seen_question_ids:
                    continue
                seen_question_ids.add(content_hash)

                # Build and write SFT record
                sft_record = build_sft_record(q, a)
                sft_out.write(json.dumps(sft_record, ensure_ascii=False) + "\n")
                sft_out.flush()

                # Build and write topic record
                topic_record = build_topic_record(q)
                topics_out.write(json.dumps(topic_record, ensure_ascii=False) + "\n")
                topics_out.flush()

                total_collected += 1
                tag_collected += 1

                sys.stderr.write(
                    f"  [{total_collected}/{max_results}] "
                    f"qid={q['question_id']} "
                    f"q_score={q['score']} a_score={a['score']} "
                    f"delta={delta}s "
                    f"title={q['title'][:60]}...\n"
                )

            sys.stderr.write(f"\n  Tag [{tag}]: collected {tag_collected} fast-accepted Q&A\n")

            if client.quota_remaining is not None:
                sys.stderr.write(f"  API quota remaining: {client.quota_remaining}\n")

            total_api_calls = client.request_count

    sys.stderr.write(f"\n{'═' * 60}\n")
    sys.stderr.write(f"DONE: {total_collected} SFT records written to {output_file}\n")
    sys.stderr.write(f"      {total_collected} topic records written to {topics_file}\n")
    sys.stderr.write(f"      Total API calls: {total_api_calls}\n")
    if client.quota_remaining is not None:
        sys.stderr.write(f"      API quota remaining: {client.quota_remaining}\n")
    sys.stderr.write(f"{'═' * 60}\n")

    return total_collected


# ─── CLI ───────────────────────────────────────────────────────────────────────

def parse_args():
    parser = argparse.ArgumentParser(
        description="Scrape Stack Overflow fast-accepted answers for SFT training data.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  python3 stackoverflow_scraper.py\n"
            "  python3 stackoverflow_scraper.py --max 2000\n"
            "  python3 stackoverflow_scraper.py --tags python,javascript --max 500\n"
            "\n"
            "Environment:\n"
            "  STACK_EXCHANGE_API_KEY  Optional API key (raises rate limit to 300/sec)\n"
        ),
    )
    parser.add_argument(
        "--max",
        type=int,
        default=2000,
        help="Maximum number of SFT records to collect (default: 2000)",
    )
    parser.add_argument(
        "--tags",
        type=str,
        default=None,
        help=(
            "Comma-separated list of tags to scrape "
            f"(default: {','.join(DEFAULT_TAGS)})"
        ),
    )
    return parser.parse_args()


def main():
    args = parse_args()

    tags = (
        [t.strip() for t in args.tags.split(",") if t.strip()]
        if args.tags
        else DEFAULT_TAGS
    )

    if not tags:
        sys.stderr.write("Error: no tags specified\n")
        sys.exit(1)

    count = run(tags, args.max, OUTPUT_FILE, TOPICS_FILE)
    sys.stderr.write(f"\nCollected {count} records total.\n")


if __name__ == "__main__":
    main()
