#!/usr/bin/env python3
"""reddit_scraper.py — scrape Reddit coding subreddits for SFT training pairs.

Uses the Reddit JSON API (https://www.reddit.com/r/SUBREDDIT/top.json?limit=100&t=week)
with no auth — just a descriptive User-Agent header. When the JSON endpoint is blocked
by Reddit's network security (returns a 403 HTML interstitial instead of JSON), the
script automatically falls back to the RSS feed (.rss) which provides the same post
listings and comment threads, albeit without score data (score threshold is relaxed
to "has comments" in fallback mode).

For each post with score >= 10 whose title is a question:
  - Fetches the post body + top comment (sorted by "best")
  - Builds an SFT pair: system/user/assistant in axolotl chat format

Quorum-sensing: only includes topics that appear in 2+ sources (Reddit + Stack Overflow
+ YouTube). Each scraper writes a topics manifest to data/raw/<source>_topics.jsonl.
This script reads all sibling manifests, builds a topic→sources map, and only emits
SFT pairs whose normalized topic appears in 2+ distinct sources.

Output: /home/user/rig-ft/data/raw/reddit_coding.jsonl
Topics: /home/user/rig-ft/data/raw/reddit_topics.jsonl

Usage:
  python3 reddit_scraper.py [--max 500] [--subreddits programming,coding]
"""

import argparse
import json
import os
import re
import sys
import time
import hashlib
import xml.etree.ElementTree as ET
from html import unescape as html_unescape
from urllib.parse import urljoin

import requests

# ── Constants ──────────────────────────────────────────────────────────────────

DEFAULT_SUBREDDITS = [
    "programming", "coding", "learnprogramming", "webdev",
    "python", "javascript", "rust", "golang",
    "MachineLearning", "LocalLLaMA",
]

USER_AGENT = "rig-ft-research-bot/1.0 (python:requests; purpose:fine-tuning-data-collection)"

JSON_BASE = "https://www.reddit.com"
RSS_BASE = "https://www.reddit.com"

OUTPUT_DIR = "/home/user/rig-ft/data/raw"
OUTPUT_FILE = os.path.join(OUTPUT_DIR, "reddit_coding.jsonl")
TOPICS_FILE = os.path.join(OUTPUT_DIR, "reddit_topics.jsonl")

SYSTEM_PROMPT = "You are an expert developer answering community questions with practical, accurate, and actionable advice."

MIN_SCORE = 10
RATE_LIMIT_DELAY = 2.0        # seconds between requests (JSON mode)
RSS_RATE_LIMIT_DELAY = 3.0    # seconds between RSS requests (more aggressive throttling)
MAX_RETRIES = 3
RETRY_BACKOFF = 5.0

# ── Topic normalization (shared quorum-sensing contract) ──────────────────────

STOPWORDS = {
    "a", "an", "the", "is", "are", "was", "were", "be", "been", "being",
    "have", "has", "had", "do", "does", "did", "will", "would", "could",
    "should", "may", "might", "must", "shall", "can", "need", "dare",
    "ought", "used", "to", "of", "in", "for", "on", "with", "at", "by",
    "from", "as", "into", "through", "during", "before", "after", "above",
    "below", "up", "down", "out", "off", "over", "under", "again", "further",
    "then", "once", "here", "there", "when", "where", "why", "how", "all",
    "each", "every", "both", "few", "more", "most", "other", "some", "such",
    "no", "nor", "not", "only", "own", "same", "so", "than", "too", "very",
    "just", "also", "but", "and", "or", "if", "because", "while", "about",
    "against", "between", "this", "that", "these", "those", "i", "you",
    "he", "she", "it", "we", "they", "me", "him", "her", "us", "them",
    "my", "your", "his", "its", "our", "their", "what", "which", "who",
    "whom", "whose", "my", "mine", "yours", "hers", "ours", "theirs",
}

FILLER_PHRASES = [
    "full course", "for beginners", "crash course", "step by step",
    "from scratch", "complete guide", "complete course", "ultimate guide",
    "beginners guide", "introduction to", "how to", "the basics",
    "deep dive", "tutorial", "explained", "you need to know",
    "in 2024", "in 2025", "in 2026", "in 2027",
]


def normalize_topic(text: str) -> str:
    """Normalize text into a canonical topic key for quorum-sensing cross-matching.

    Lowercases, strips URLs/brackets/parens, removes years, strips filler phrases,
    removes punctuation, removes stopwords, collapses whitespace, and keeps the
    first ~8 meaningful tokens.
    """
    if not text:
        return ""
    t = text.lower().strip()

    # Strip URLs
    t = re.sub(r"https?://\S+", "", t)
    # Strip bracketed/parenthesized content
    t = re.sub(r"[\[\(].*?[\]\)]", "", t)
    # Strip filler phrases
    for phrase in FILLER_PHRASES:
        t = t.replace(phrase, "")
    # Remove years (20XX)
    t = re.sub(r"\b20\d{2}\b", "", t)
    # Remove punctuation
    t = re.sub(r"[^\w\s]", " ", t)
    # Collapse whitespace
    t = re.sub(r"\s+", " ", t).strip()

    # Tokenize, remove stopwords, keep first ~8 meaningful tokens
    tokens = [w for w in t.split() if w and w not in STOPWORDS and len(w) > 1]
    tokens = tokens[:8]
    return " ".join(tokens)


# ── HTML / text cleaning ──────────────────────────────────────────────────────

HTML_TAG_RE = re.compile(r"<[^>]+>")
HTML_COMMENT_RE = re.compile(r"<!--.*?-->", re.S)
WHITESPACE_RE = re.compile(r"\s+")
REDDIT_ENTITY_RE = re.compile(r"&#\d+;")


def strip_html(raw: str) -> str:
    """Strip HTML tags, comments, entities, RSS metadata, and collapse whitespace."""
    if not raw:
        return ""
    text = HTML_COMMENT_RE.sub("", raw)
    text = HTML_TAG_RE.sub("", text)
    text = html_unescape(text)
    text = REDDIT_ENTITY_RE.sub(" ", text)
    # Remove Reddit RSS metadata patterns
    text = re.sub(r"submitted by\s+/u/\S+\s*", "", text)
    text = re.sub(r"\[link\]\s*", "", text)
    text = re.sub(r"\[comments\]\s*", "", text)
    text = re.sub(r"/u/\S+\s*on\s+", "", text)  # comment author prefix
    text = WHITESPACE_RE.sub(" ", text).strip()
    return text


# ── Question detection ────────────────────────────────────────────────────────

QUESTION_RE = re.compile(
    r"\b(how|what|why|when|where|which|who|can|should|is|are|do|does|did|"
    r"will|would|could|best|whats|howdo|howto)\b.*\?",
    re.I,
)


def is_question(title: str) -> bool:
    """Check if a post title is asking a question."""
    if not title:
        return False
    t = title.strip()
    if "?" in t and QUESTION_RE.search(t):
        return True
    # Also match titles that start with a question word even without '?'
    if re.match(r"^(how|what|why|when|where|which|who)\b", t, re.I):
        return True
    return False


# ── Secret scanning (reuse pattern from build_sft.py) ─────────────────────────

SECRET_RE = re.compile(
    r"(sk-[A-Za-z0-9]{20,}|sk-ant-[A-Za-z0-9_-]{20,}|gsk_[A-Za-z0-9]{20,}|"
    r"AKIA[0-9A-Z]{16}|hf_[A-Za-z0-9]{20,}|gh[ps]_[A-Za-z0-9]{20,}|"
    r"-----BEGIN [A-Z ]*PRIVATE KEY-----)"
)


def has_secret(text: str) -> bool:
    return bool(SECRET_RE.search(text or ""))


# ── HTTP session ──────────────────────────────────────────────────────────────

def make_session() -> requests.Session:
    s = requests.Session()
    s.headers.update({
        "User-Agent": USER_AGENT,
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "en-US,en;q=0.9",
    })
    return s


def fetch_json(session: requests.Session, url: str, params: dict = None) -> dict | None:
    """Fetch JSON from Reddit. Returns parsed dict or None if blocked/error."""
    for attempt in range(MAX_RETRIES):
        try:
            r = session.get(url, params=params, timeout=15)
            ct = r.headers.get("content-type", "")
            if r.status_code == 200 and "json" in ct:
                return r.json()
            if r.status_code == 429:
                wait = RETRY_BACKOFF * (attempt + 1)
                print(f"  [rate-limit] 429, waiting {wait:.0f}s...", file=sys.stderr)
                time.sleep(wait)
                continue
            # 403 with HTML = Reddit network security block
            if r.status_code == 403 and "html" in ct:
                return None  # signal fallback
            if r.status_code != 200:
                print(f"  [warn] {url} → HTTP {r.status_code}", file=sys.stderr)
                time.sleep(RATE_LIMIT_DELAY)
                continue
            # 200 but not JSON (HTML interstitial)
            if "html" in ct:
                return None
            # Try to parse anyway
            try:
                return r.json()
            except Exception:
                return None
        except requests.RequestException as e:
            print(f"  [error] {e} (attempt {attempt+1})", file=sys.stderr)
            time.sleep(RETRY_BACKOFF * (attempt + 1))
    return None


def fetch_rss(session: requests.Session, url: str, params: dict = None) -> ET.Element | None:
    """Fetch and parse an RSS feed from Reddit. Returns root Element or None."""
    for attempt in range(MAX_RETRIES):
        try:
            r = session.get(url, params=params, timeout=20)
            if r.status_code == 200 and r.text.strip():
                try:
                    return ET.fromstring(r.text)
                except ET.ParseError as e:
                    print(f"  [rss-parse-error] {e}", file=sys.stderr)
                    return None
            if r.status_code == 429:
                wait = RETRY_BACKOFF * (attempt + 1) + 30
                print(f"  [rss-rate-limit] 429, waiting {wait:.0f}s...", file=sys.stderr)
                time.sleep(wait)
                continue
            if r.status_code != 200:
                print(f"  [rss-warn] {url} → HTTP {r.status_code}", file=sys.stderr)
                time.sleep(RSS_RATE_LIMIT_DELAY)
        except requests.RequestException as e:
            print(f"  [rss-error] {e} (attempt {attempt+1})", file=sys.stderr)
            time.sleep(RETRY_BACKOFF * (attempt + 1))
    return None


# ── JSON API mode ─────────────────────────────────────────────────────────────

def json_get_top_posts(session, subreddit: str, limit: int = 100) -> list[dict]:
    """Fetch top posts from a subreddit via JSON API. Returns list of post dicts."""
    url = f"{JSON_BASE}/r/{subreddit}/top.json"
    data = fetch_json(session, url, params={"limit": str(limit), "t": "week"})
    if data is None:
        return []
    try:
        children = data["data"]["children"]
        return [c["data"] for c in children]
    except (KeyError, TypeError):
        return []


def json_get_post_and_comments(session, permalink: str) -> tuple[dict | None, list[dict]]:
    """Fetch a post and its comments via JSON API. Returns (post_data, comments_list).

    The .json endpoint for a permalink returns an array: [post_listing, comments_listing].
    """
    url = urljoin(JSON_BASE, permalink.rstrip("/") + ".json")
    data = fetch_json(session, url, params={"sort": "best", "limit": "30"})
    if data is None or not isinstance(data, list) or len(data) < 2:
        return None, []
    try:
        post = data[0]["data"]["children"][0]["data"]
    except (KeyError, IndexError, TypeError):
        post = None
    comments = _extract_comments_json(data[1])
    return post, comments


def _extract_comments_json(comment_data: dict) -> list[dict]:
    """Recursively extract comments from Reddit JSON comment listing."""
    result = []
    try:
        children = comment_data["data"]["children"]
    except (KeyError, TypeError):
        return result
    for child in children:
        if child.get("kind") != "t1":
            continue
        c = child.get("data", {})
        body = c.get("body", "")
        score = c.get("score", 0)
        if body and body not in ("[deleted]", "[removed]"):
            result.append({
                "body": body,
                "score": score,
                "author": c.get("author", ""),
            })
        # Recurse into replies
        replies = c.get("replies")
        if isinstance(replies, dict):
            result.extend(_extract_comments_json(replies))
    return result


# ── RSS fallback mode ─────────────────────────────────────────────────────────

def rss_get_top_posts(session, subreddit: str) -> list[dict]:
    """Fetch top posts from a subreddit via RSS feed. Returns list of post dicts."""
    url = f"{RSS_BASE}/r/{subreddit}/top/.rss"
    root = fetch_rss(session, url, params={"t": "week"})
    if root is None:
        return []
    ns = {"atom": "http://www.w3.org/2005/Atom"}
    posts = []
    for entry in root.findall("atom:entry", ns):
        title_el = entry.find("atom:title", ns)
        link_el = entry.find("atom:link", ns)
        content_el = entry.find("atom:content", ns)
        if title_el is None or link_el is None:
            continue
        title = title_el.text or ""
        permalink = link_el.get("href", "")
        content_html = content_el.text if content_el is not None else ""
        content_text = strip_html(content_html)

        # Extract post ID from permalink
        # Format: https://www.reddit.com/r/SUBREDDIT/comments/POST_ID/title/
        post_id = ""
        m = re.search(r"/comments/([a-z0-9]+)/", permalink)
        if m:
            post_id = m.group(1)

        # RSS doesn't provide score; use a heuristic: posts in the top feed are implicitly high-score
        # We set score=MIN_SCORE so the threshold check passes (relaxed mode)
        posts.append({
            "id": post_id,
            "title": title,
            "permalink": permalink,
            "selftext": "",
            "score": MIN_SCORE,  # unknown — relaxed threshold
            "num_comments": 0,
            "author": "",
            "url": permalink,
            "is_self": False,
            "_rss_content": content_text,
            "_rss_mode": True,
        })
    return posts


def rss_get_post_and_comments(session, permalink: str) -> tuple[dict | None, list[dict]]:
    """Fetch a post and its comments via RSS feed. Returns (post_data, comments_list)."""
    # Convert permalink to RSS URL
    # permalink: https://www.reddit.com/r/SUBREDDIT/comments/POST_ID/title/
    # rss:       https://www.reddit.com/r/SUBREDDIT/comments/POST_ID/.rss
    rss_path = re.sub(r"/comments/([a-z0-9]+)/[^/]*$", r"/comments/\1/.rss", permalink.rstrip("/"))
    rss_url = RSS_BASE + rss_path if rss_path.startswith("/") else rss_path

    root = fetch_rss(session, rss_url)
    if root is None:
        return None, []

    ns = {"atom": "http://www.w3.org/2005/Atom"}
    entries = root.findall("atom:entry", ns)
    if not entries:
        return None, []

    post_data = None
    comments = []

    for i, entry in enumerate(entries):
        title_el = entry.find("atom:title", ns)
        content_el = entry.find("atom:content", ns)
        title = title_el.text if title_el is not None else ""
        content_html = content_el.text if content_el is not None and content_el.text else ""
        content_text = strip_html(content_html)

        if i == 0:
            # First entry is the post itself
            post_data = {
                "title": title,
                "selftext": content_text,
                "score": MIN_SCORE,  # unknown in RSS
                "author": "",
            }
        else:
            # Comment entries: title starts with "/u/USERNAME on ..."
            body = content_text
            if body and body not in ("[deleted]", "[removed]"):
                # RSS doesn't provide score; use order as proxy (first = top)
                comments.append({
                    "body": body,
                    "score": len(comments) * -1 + 100,  # higher = earlier = better
                    "author": "",
                })
    return post_data, comments


# ── Comment selection ─────────────────────────────────────────────────────────

def get_best_comment(comments: list[dict], min_len: int = 50) -> dict | None:
    """Select the best comment from a list, sorted by score (descending).

    In JSON mode, comments have real scores. In RSS mode, earlier comments
    have higher proxy scores (Reddit sorts RSS by best by default).
    """
    if not comments:
        return None
    # Filter out very short comments
    candidates = [c for c in comments if len(c["body"]) >= min_len]
    if not candidates:
        candidates = comments
    # Sort by score descending
    candidates.sort(key=lambda c: c.get("score", 0), reverse=True)
    return candidates[0]


# ── Quorum-sensing ────────────────────────────────────────────────────────────

def load_all_topics(data_dir: str) -> dict[str, set[str]]:
    """Load topic manifests from all scrapers. Returns {topic: {source1, source2, ...}}."""
    topic_map: dict[str, set[str]] = {}
    # Match any *_topics.jsonl file in the raw data dir
    for fname in os.listdir(data_dir):
        if not fname.endswith("_topics.jsonl"):
            continue
        fpath = os.path.join(data_dir, fname)
        source_name = fname.replace("_topics.jsonl", "")
        try:
            with open(fpath) as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rec = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    topic = normalize_topic(rec.get("topic", ""))
                    source = rec.get("source", source_name)
                    if topic:
                        topic_map.setdefault(topic, set()).add(source)
        except OSError:
            continue
    return topic_map


def topic_meets_quorum(topic: str, topic_map: dict[str, set[str]], min_sources: int = 2) -> bool:
    """Check if a topic appears in 2+ sources. Does fuzzy matching as fallback."""
    if not topic:
        return False
    # Exact match
    sources = topic_map.get(topic, set())
    if len(sources) >= min_sources:
        return True
    # Fuzzy: check if our topic is a substring of any known topic or vice versa
    for known_topic, known_sources in topic_map.items():
        if not known_topic:
            continue
        # Check token overlap (at least 60% of our tokens appear in the known topic)
        our_tokens = set(topic.split())
        known_tokens = set(known_topic.split())
        if not our_tokens:
            continue
        overlap = our_tokens & known_tokens
        if len(overlap) >= 1 and len(overlap) / len(our_tokens) >= 0.6:
            combined = sources | known_sources
            if len(combined) >= min_sources:
                return True
    return False


# ── SFT pair construction ─────────────────────────────────────────────────────

def build_sft_pair(title: str, body: str, answer: str, permalink: str) -> dict | None:
    """Build an SFT pair from a Reddit Q&A. Returns None if invalid."""
    # Construct user content: title + body
    user_parts = [title.strip()]
    body = body.strip() if body else ""
    if body and body != title.strip():
        user_parts.append(body)
    user_content = "\n\n".join(user_parts)

    assistant_content = answer.strip()

    # Validation
    if not user_content or not assistant_content:
        return None
    if len(assistant_content) < 20:
        return None
    if has_secret(user_content) or has_secret(assistant_content):
        return None
    # Skip if answer is just a link with no explanation
    if len(assistant_content) < 50 and assistant_content.startswith("http"):
        return None

    return {
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_content},
            {"role": "assistant", "content": assistant_content},
        ],
        "source": "reddit-coding",
        "tier": None,
    }


# ── Deduplication ─────────────────────────────────────────────────────────────

def content_hash(pair: dict) -> str:
    """Hash the user content for deduplication."""
    user_msg = next((m for m in pair["messages"] if m["role"] == "user"), {})
    return hashlib.sha256(json.dumps(user_msg.get("content", "")).encode()).hexdigest()


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser(
        description="Scrape Reddit coding subreddits for SFT training pairs.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python3 reddit_scraper.py
  python3 reddit_scraper.py --max 500
  python3 reddit_scraper.py --subreddits programming,coding
  python3 reddit_scraper.py --max 200 --subreddits python,rust
        """,
    )
    ap.add_argument("--max", type=int, default=500,
                    help="Maximum number of SFT pairs to output (default: 500)")
    ap.add_argument("--subreddits", type=str, default=None,
                    help="Comma-separated list of subreddits (default: all 10)")
    ap.add_argument("--out", type=str, default=OUTPUT_FILE,
                    help=f"Output JSONL file (default: {OUTPUT_FILE})")
    ap.add_argument("--no-quorum", action="store_true",
                    help="Skip quorum-sensing check (include all question posts)")
    ap.add_argument("--min-score", type=int, default=MIN_SCORE,
                    help=f"Minimum post score (default: {MIN_SCORE})")
    ap.add_argument("--delay", type=float, default=None,
                    help="Override rate-limit delay between requests (seconds)")
    args = ap.parse_args()

    # Parse subreddits
    if args.subreddits:
        subreddits = [s.strip() for s in args.subreddits.split(",") if s.strip()]
    else:
        subreddits = DEFAULT_SUBREDDITS

    # Ensure output directory exists
    os.makedirs(os.path.dirname(args.out), exist_ok=True)
    os.makedirs(OUTPUT_DIR, exist_ok=True)

    # Load quorum topic map
    topic_map = {}
    if not args.no_quorum:
        topic_map = load_all_topics(OUTPUT_DIR)
        print(f"[quorum] Loaded {len(topic_map)} topics from {OUTPUT_DIR}/*_topics.jsonl", file=sys.stderr)
        # Count how many have 2+ sources
        multi = sum(1 for s in topic_map.values() if len(s) >= 2)
        print(f"[quorum] {multi} topics have 2+ source coverage", file=sys.stderr)

    session = make_session()

    # ── Detect mode: try JSON API first, fall back to RSS ─────────────────────
    use_rss = False
    print(f"[probe] Testing JSON API on r/{subreddits[0]}...", file=sys.stderr)
    # Quick single-attempt probe — don't waste rate-limit budget on retries
    try:
        r = session.get(f"{JSON_BASE}/r/{subreddits[0]}/top.json",
                        params={"limit": "1", "t": "week"}, timeout=10)
        ct = r.headers.get("content-type", "")
        if r.status_code == 200 and "json" in ct:
            test_posts = r.json().get("data", {}).get("children", [])
        else:
            test_posts = []
    except requests.RequestException:
        test_posts = []

    if test_posts:
        print(f"[probe] JSON API works — using JSON mode", file=sys.stderr)
        delay = args.delay or RATE_LIMIT_DELAY
    else:
        print(f"[probe] JSON API blocked (403 interstitial) — falling back to RSS mode", file=sys.stderr)
        use_rss = True
        delay = args.delay or RSS_RATE_LIMIT_DELAY
        # Wait before RSS probe to avoid cascading rate-limit from the JSON attempt
        print(f"[probe] Waiting {delay:.0f}s before RSS probe...", file=sys.stderr)
        time.sleep(delay)
        test_rss = rss_get_top_posts(session, subreddits[0])
        if not test_rss:
            print(f"[error] Both JSON and RSS failed for r/{subreddits[0]}. "
                  f"Reddit may be blocking this host.", file=sys.stderr)
            sys.exit(1)
        print(f"[probe] RSS works — {len(test_rss)} posts from r/{subreddits[0]}", file=sys.stderr)

    # ── Scrape ────────────────────────────────────────────────────────────────
    all_pairs = []
    all_topics = []  # for topic manifest
    seen_hashes = set()
    stats = {
        "subreddits_scraped": 0,
        "posts_fetched": 0,
        "question_posts": 0,
        "comments_fetched": 0,
        "pairs_before_quorum": 0,
        "pairs_after_quorum": 0,
        "dropped_dup": 0,
        "dropped_secret": 0,
        "dropped_short": 0,
        "dropped_no_comment": 0,
        "dropped_no_quorum": 0,
        "rss_mode": use_rss,
    }

    for sub in subreddits:
        print(f"\n[r/{sub}] Fetching top posts...", file=sys.stderr)
        time.sleep(delay)

        if use_rss:
            posts = rss_get_top_posts(session, sub)
        else:
            posts = json_get_top_posts(session, sub, limit=100)

        print(f"[r/{sub}] {len(posts)} posts found", file=sys.stderr)
        stats["subreddits_scraped"] += 1

        for post in posts:
            stats["posts_fetched"] += 1

            # Score check (skipped in RSS mode since score is unknown)
            score = post.get("score", 0)
            if not use_rss and score < args.min_score:
                continue

            title = post.get("title", "")
            body = post.get("selftext", "") or post.get("_rss_content", "")
            permalink = post.get("permalink", post.get("url", ""))

            # Must be a question
            if not is_question(title):
                continue
            stats["question_posts"] += 1

            # Fetch post + comments
            time.sleep(delay)
            if use_rss:
                post_data, comments = rss_get_post_and_comments(session, permalink)
            else:
                post_data, comments = json_get_post_and_comments(session, permalink)

            # Use fetched post data if available (has better body text)
            if post_data:
                if post_data.get("selftext"):
                    body = post_data["selftext"]

            stats["comments_fetched"] += len(comments)

            # Get best comment
            best = get_best_comment(comments)
            if not best:
                stats["dropped_no_comment"] += 1
                continue

            answer = best["body"]

            # Build SFT pair
            pair = build_sft_pair(title, body, answer, permalink)
            if not pair:
                stats["dropped_short"] += 1
                continue

            # Secret check
            if has_secret(json.dumps(pair)):
                stats["dropped_secret"] += 1
                continue

            # Dedup
            h = content_hash(pair)
            if h in seen_hashes:
                stats["dropped_dup"] += 1
                continue
            seen_hashes.add(h)

            stats["pairs_before_quorum"] += 1

            # Record topic for manifest
            topic = normalize_topic(title)
            if topic:
                all_topics.append({
                    "topic": topic,
                    "source": "reddit",
                    "title": title,
                    "url": permalink,
                })

            # Quorum-sensing check
            if not args.no_quorum:
                if not topic_meets_quorum(topic, topic_map, min_sources=2):
                    stats["dropped_no_quorum"] += 1
                    continue

            stats["pairs_after_quorum"] += 1
            all_pairs.append(pair)

            if len(all_pairs) >= args.max:
                print(f"\n[limit] Reached --max {args.max} pairs, stopping.", file=sys.stderr)
                break

        if len(all_pairs) >= args.max:
            break

    # ── Write output ──────────────────────────────────────────────────────────
    with open(args.out, "w") as f:
        for pair in all_pairs:
            f.write(json.dumps(pair, ensure_ascii=False) + "\n")

    # Write topics manifest (always, even if empty — signals to other scrapers)
    with open(TOPICS_FILE, "w") as f:
        for t in all_topics:
            f.write(json.dumps(t, ensure_ascii=False) + "\n")

    # ── Report ────────────────────────────────────────────────────────────────
    print(f"\n{'═' * 60}", file=sys.stderr)
    print(f"Reddit Scraper — {'RSS fallback' if use_rss else 'JSON API'} mode", file=sys.stderr)
    print(f"{'═' * 60}", file=sys.stderr)
    print(f"Subreddits scraped:  {stats['subreddits_scraped']}", file=sys.stderr)
    print(f"Posts fetched:       {stats['posts_fetched']}", file=sys.stderr)
    print(f"Question posts:      {stats['question_posts']}", file=sys.stderr)
    print(f"Comments fetched:    {stats['comments_fetched']}", file=sys.stderr)
    print(f"Pairs (pre-quorum):  {stats['pairs_before_quorum']}", file=sys.stderr)
    print(f"Pairs (post-quorum): {stats['pairs_after_quorum']}", file=sys.stderr)
    print(f"Dropped (dup):       {stats['dropped_dup']}", file=sys.stderr)
    print(f"Dropped (secret):    {stats['dropped_secret']}", file=sys.stderr)
    print(f"Dropped (short):     {stats['dropped_short']}", file=sys.stderr)
    print(f"Dropped (no comment):{stats['dropped_no_comment']}", file=sys.stderr)
    print(f"Dropped (no quorum): {stats['dropped_no_quorum']}", file=sys.stderr)
    print(f"Topics manifest:     {len(all_topics)} entries → {TOPICS_FILE}", file=sys.stderr)
    print(f"Output:              {len(all_pairs)} pairs → {args.out}", file=sys.stderr)
    print(f"{'═' * 60}", file=sys.stderr)


if __name__ == "__main__":
    main()
