#!/usr/bin/env python3
"""github_nit_scraper.py — harvest GitHub PR review comments containing "nit"
and convert them into axolotl SFT chat JSONL.

For each nit comment on a review comment with a code suggestion, extracts:
  - original code (the snippet being commented on)
  - suggested fix (the reviewer's proposed replacement, if any)
  - reviewer rationale (the comment body, stripped of "nit:" prefix)
  - repo + PR number (provenance)

Writes to /home/user/rig-ft/data/raw/github_nit.jsonl in the format:
  {"messages": [{"role":"system",...},{"role":"user",...},{"role":"assistant",...}],
   "source": "github-nit", "tier": null}

CLI: python3 github_nit_scraper.py [--max 1000] [--repos repo1,repo2]

Self-contained: only requests + json (plus stdlib argparse, os, sys, time, re).
"""
import argparse
import json
import os
import re
import sys
import time

import requests

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "os.environ.get("GITHUB_TOKEN", "")")
GITHUB_API = "https://api.github.com"
DEFAULT_REPOS = [
    "torvalds/linux",
    "microsoft/vscode",
    "facebook/react",
    "python/cpython",
    "rust-lang/rust",
    "golang/go",
    "nodejs/node",
    "kubernetes/kubernetes",
]
OUTPUT_PATH = "/home/user/rig-ft/data/raw/github_nit.jsonl"

# Rate limiting: 10 requests/sec -> min 0.1s between requests.
MIN_INTERVAL = 0.1
MAX_RETRIES = 5          # per request, on 403/429/5xx
RETRY_BACKOFF = 2.0      # exponential base (seconds)
PER_PAGE = 100           # GitHub max items per page for list endpoints.

SYSTEM_PROMPT = "You are an expert code reviewer who identifies nit-level issues and suggests precise fixes."

# ---------------------------------------------------------------------------
# HTTP layer with rate-limiting + retry
# ---------------------------------------------------------------------------

class RateLimiter:
    """Token-bucket-ish throttle: ensures >= MIN_INTERVAL between calls."""

    def __init__(self, interval=MIN_INTERVAL):
        self.interval = interval
        self._last = 0.0

    def wait(self):
        now = time.monotonic()
        delta = now - self._last
        if delta < self.interval:
            time.sleep(self.interval - delta)
        self._last = time.monotonic()


_LIMITER = RateLimiter()


def github_headers():
    return {
        "Authorization": f"token {GITHUB_TOKEN}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "rig-ft-nit-scraper",
    }


def github_get(url, params=None):
    """GET with rate-limit, retry on 403/429/5xx with exponential backoff."""
    last_exc = None
    for attempt in range(1, MAX_RETRIES + 1):
        _LIMITER.wait()
        try:
            resp = requests.get(url, headers=github_headers(), params=params, timeout=30)
        except requests.RequestException as exc:
            last_exc = exc
            time.sleep(RETRY_BACKOFF ** attempt)
            continue

        # Secondary rate limit / abuse detection -> back off harder.
        if resp.status_code in (403, 429):
            # Honor Retry-After if present.
            ra = resp.headers.get("Retry-After")
            sleep_for = float(ra) if ra and ra.isdigit() else RETRY_BACKOFF ** attempt
            # If this is a rate-limit (not abuse) and we're near exhausted, bail
            # gracefully rather than burn the retry budget.
            remaining = resp.headers.get("X-RateLimit-Remaining")
            if remaining == "0":
                reset = resp.headers.get("X-RateLimit-Reset")
                if reset and reset.isdigit():
                    wait_until = max(int(reset) - int(time.time()) + 2, 1)
                    print(f"  [rate-limit] exhausted, sleeping {wait_until}s until reset",
                          file=sys.stderr)
                    time.sleep(min(wait_until, 600))  # cap at 10 min
                    continue
            print(f"  [retry {attempt}/{MAX_RETRIES}] {resp.status_code} on {url} "
                  f"-> sleep {sleep_for:.1f}s", file=sys.stderr)
            time.sleep(sleep_for)
            continue

        if resp.status_code >= 500:
            print(f"  [retry {attempt}/{MAX_RETRIES}] {resp.status_code} on {url}",
                  file=sys.stderr)
            time.sleep(RETRY_BACKOFF ** attempt)
            continue

        resp.raise_for_status()
        return resp.json()

    raise RuntimeError(f"exhausted retries for {url}: {last_exc}")


# ---------------------------------------------------------------------------
# Extraction helpers
# ---------------------------------------------------------------------------

NIT_PREFIX_RE = re.compile(r"^\s*nit\s*[:\-]?\s*", re.IGNORECASE)
# GitHub suggestion blocks: ```suggestion\n ... \n```
SUGGESTION_RE = re.compile(
    r"```suggestion[^\n]*\n(.*?)```",
    re.DOTALL,
)
# Fenced code block (non-suggestion) capture for inline code refs.
CODE_FENCE_RE = re.compile(r"```[a-zA-Z0-9_+\-]*\n(.*?)```", re.DOTALL)


def strip_nit_prefix(body):
    """Remove leading 'nit:'/'Nit -' style prefix from a comment body."""
    return NIT_PREFIX_RE.sub("", body).strip()


def extract_suggestion(body):
    """Return the first ```suggestion``` block content, or None."""
    m = SUGGESTION_RE.search(body)
    if m:
        return m.group(1).strip()
    return None


def extract_inline_code(body):
    """Return the first non-suggestion fenced code block, for fallback code."""
    for m in CODE_FENCE_RE.finditer(body):
        block = m.group(0)
        if "```suggestion" in block:
            continue
        return m.group(1).strip()
    return None


def build_sft_record(original_code, suggested_fix, rationale, repo, pr_number):
    """Build one SFT row in the canonical axolotl chat format."""
    user_content = (
        "Review this code:\n\n```\n" + original_code.strip() + "\n```"
    )
    assistant_content = (
        f"Here's the issue: {rationale}. Suggested fix:\n\n```\n"
        f"{suggested_fix.strip()}\n```"
    )
    return {
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_content},
            {"role": "assistant", "content": assistant_content},
        ],
        "source": "github-nit",
        "tier": None,
    }


# ---------------------------------------------------------------------------
# Scraping
# ---------------------------------------------------------------------------

def search_nit_comments(repo, max_for_repo):
    """Fetch review comments for a repo and keep those mentioning 'nit'.

    Uses the bulk /repos/{owner}/{repo}/pulls/comments endpoint, which streams
    ALL review comments across the repo in one paginated series — far more
    efficient than per-PR fetching. Each comment object includes diff_hunk
    (the original code context) and may contain a ```suggestion``` block.

    We page through comments newest-first, filter for 'nit', and convert.
    """
    owner_repo = repo.strip()
    url = f"{GITHUB_API}/repos/{owner_repo}/pulls/comments"
    collected = []
    page = 1
    scanned = 0

    while len(collected) < max_for_repo:
        params = {"per_page": 100, "page": page,
                  "sort": "created", "direction": "desc"}
        try:
            comments = github_get(url, params=params)
        except RuntimeError as exc:
            print(f"  [warn] {owner_repo} comments page {page}: {exc}",
                  file=sys.stderr)
            break

        if not comments:
            break

        for c in comments:
            scanned += 1
            body = c.get("body") or ""
            if not is_nit_comment(body):
                continue
            # Extract PR number from pull_request_url field.
            pr_url = c.get("pull_request_url") or ""
            pr_number = pr_url.rstrip("/").split("/")[-1] if pr_url else "?"
            rec = comment_to_record(c, owner_repo, pr_number)
            if rec:
                collected.append(rec)
                if len(collected) >= max_for_repo:
                    break

        if len(comments) < 100:
            break
        page += 1
        # Safety cap: 50 pages = 5000 comments scanned per repo.
        if page > 50:
            break

    print(f"  {owner_repo}: scanned {scanned} comments, "
          f"collected {len(collected)} nit records", file=sys.stderr)
    return collected


def is_nit_comment(body):
    """A comment counts as a nit if it contains 'nit' as a word/prefix."""
    if not body:
        return False
    lowered = body.lower()
    # word-boundary-ish: 'nit' followed by ':' / '-' / space / newline / EOL
    return bool(re.search(r"\bnit\b[:\-\s]", lowered))


def comment_to_record(comment, repo, pr_number):
    """Convert one GitHub review-comment object into an SFT record, or None.

    Fields of interest from the API:
      - body: the comment text (may contain ```suggestion``` block)
      - path: file path (provenance only)
      - line / original_line: target line
      - diff_hunk: the diff context around the comment (contains original code)
      - original_commit_id: provenance
      - in_reply_to_id: skip reply chains (less signal)
    """
    body = comment.get("body") or ""
    if comment.get("in_reply_to_id"):
        # Replies rarely carry the original-code/suggestion signal we want.
        return None

    suggested_fix = extract_suggestion(body)
    rationale = strip_nit_prefix(body)

    # Original code: prefer the diff_hunk's '-' / context lines around the
    # comment. The diff_hunk is a unified-diff fragment; extract added/removed
    # code lines as the "original code" context.
    diff_hunk = comment.get("diff_hunk") or ""
    original_code = extract_code_from_diff(diff_hunk)

    # Fallback: if no suggestion block, try a plain fenced code block in body.
    if not suggested_fix:
        suggested_fix = extract_inline_code(body)

    # Need at least a rationale + either original code or suggested fix to be
    # a useful training example.
    if not rationale:
        return None
    if not original_code and not suggested_fix:
        return None
    # If we have no suggested fix, synthesize one from the original so the
    # assistant turn still has the required structure.
    if not suggested_fix:
        suggested_fix = original_code or "(no inline suggestion provided)"

    # Truncate absurdly long fields so we don't blow the context budget.
    original_code = truncate(original_code, 4000)
    suggested_fix = truncate(suggested_fix, 4000)
    rationale = truncate(rationale, 2000)

    return build_sft_record(original_code, suggested_fix, rationale, repo, pr_number)


def extract_code_from_diff(diff_hunk):
    """Pull the relevant code lines out of a unified-diff hunk.

    Prefer removed lines (the 'before' state the reviewer is commenting on);
    fall back to context lines. Returns a compact code snippet.
    """
    if not diff_hunk:
        return None
    lines = diff_hunk.splitlines()
    # Collect removed + context lines, strip the leading '+/-/ ' marker.
    removed = []
    context = []
    for ln in lines:
        if ln.startswith("-") and not ln.startswith("---"):
            removed.append(ln[1:])
        elif ln.startswith("+") and not ln.startswith("+++"):
            continue  # the 'after' code; we want original
        elif ln.startswith(" "):
            context.append(ln[1:])
        elif ln.startswith("@@"):
            continue
    code = removed if removed else context
    if not code:
        return None
    return "\n".join(code).strip()


def truncate(text, limit):
    if not text:
        return text
    if len(text) <= limit:
        return text
    return text[:limit].rsplit("\n", 1)[0] + "\n…"


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(
        description="Scrape GitHub PR nit comments into SFT JSONL.")
    ap.add_argument("--max", type=int, default=1000,
                    help="Max total nit records to collect (default 1000).")
    ap.add_argument("--repos", type=str, default=None,
                    help="Comma-separated list of owner/repo overrides.")
    ap.add_argument("--out", type=str, default=OUTPUT_PATH,
                    help="Output JSONL path.")
    ap.add_argument("--per-repo", type=int, default=None,
                    help="Optional per-repo cap (default: --max / num repos).")
    a = ap.parse_args()

    if a.repos:
        repos = [r.strip() for r in a.repos.split(",") if r.strip()]
    else:
        repos = DEFAULT_REPOS

    per_repo = a.per_repo or (a.max // max(len(repos), 1))
    # Never exceed the global max.
    per_repo = min(per_repo, a.max)

    os.makedirs(os.path.dirname(a.out), exist_ok=True)

    total = 0
    seen_keys = set()

    with open(a.out, "w") as out_fh:
        for repo in repos:
            if total >= a.max:
                break
            remaining = a.max - total
            this_cap = min(per_repo, remaining)
            print(f"[{repo}] fetching up to {this_cap} nit comments…",
                  file=sys.stderr)
            try:
                records = search_nit_comments(repo, this_cap)
            except Exception as exc:
                print(f"  [error] {repo}: {exc}", file=sys.stderr)
                continue

            for rec in records:
                if total >= a.max:
                    break
                # Dedup on a content hash of (user code + rationale).
                key = json.dumps(rec["messages"][1]["content"]) + "::" + \
                      json.dumps(rec["messages"][2]["content"])
                if key in seen_keys:
                    continue
                seen_keys.add(key)
                out_fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
                total += 1

            out_fh.flush()
            print(f"  -> {total} total records so far", file=sys.stderr)

    print(f"\nDone. Wrote {total} records to {a.out}", file=sys.stderr)


if __name__ == "__main__":
    main()
