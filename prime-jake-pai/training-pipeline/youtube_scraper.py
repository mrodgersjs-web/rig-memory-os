#!/usr/bin/env python3
"""
YouTube Tutorial Scraper — RIG Fine-Tuning Pipeline

Uses yt-dlp to fetch video metadata + auto-generated transcripts for coding
tutorial videos, then produces SFT pairs in the axolotl chat_template format.

Output: /home/user/rig-ft/data/raw/youtube_tutorials.jsonl
"""

import argparse
import json
import os
import re
import sys
import time
import urllib.request
from datetime import datetime

import yt_dlp

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

OUTPUT_PATH = "/home/user/rig-ft/data/raw/youtube_tutorials.jsonl"
TOPICS_PATH = "/home/user/rig-ft/data/raw/youtube_topics.jsonl"

DEFAULT_QUERIES = [
    "python tutorial 2024",
    "rust programming tutorial",
    "react tutorial",
    "docker tutorial",
    "kubernetes tutorial",
    "system design tutorial",
    "leetcode tutorial",
]

SYSTEM_PROMPT = (
    "You are a coding instructor explaining a concept clearly and concisely, "
    "with practical examples."
)

# Segment splitting thresholds
SEGMENT_GAP_THRESHOLD = 30.0   # seconds — a gap >= this starts a new segment
SEGMENT_MIN_DURATION = 20.0    # seconds — discard segments shorter than this
SEGMENT_MAX_DURATION = 600.0   # seconds — cap very long segments
SEGMENT_TARGET_DURATION = 120.0  # seconds — split continuous speech at this length
# Transcript length limits (chars) for SFT fields
MAX_ASSISTANT_CHARS = 6000
MAX_EXAMPLE_CHARS = 4000


# ---------------------------------------------------------------------------
# yt-dlp helpers
# ---------------------------------------------------------------------------

def _ydl_opts():
    """Base yt-dlp options — no download, metadata + subtitles only."""
    return {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "writesubtitles": True,
        "writeautomaticsub": True,
        "subtitleslangs": ["en", "en-US", "en-GB"],
        # Avoid geo / age gate noise
        "geo_bypass": True,
        "socket_timeout": 30,
        "retries": 3,
    }


def search_videos(query, limit):
    """Search YouTube and return a list of video metadata dicts."""
    opts = _ydl_opts()
    opts["extract_flat"] = "in_playlist"
    opts["playlistend"] = limit

    ydl = yt_dlp.YoutubeDL(opts)
    results = []
    try:
        info = ydl.extract_info(f"ytsearch{limit}:{query}", download=False)
        entries = info.get("entries") or []
        for e in entries:
            if not e:
                continue
            # flat extraction gives minimal info; capture what's available
            results.append({
                "id": e.get("id"),
                "url": e.get("url") or f"https://www.youtube.com/watch?v={e.get('id')}",
                "title": e.get("title", ""),
                "duration": e.get("duration"),
                "uploader": e.get("uploader") or e.get("channel") or "",
            })
    except Exception as exc:
        print(f"  [WARN] search failed for '{query}': {exc}", file=sys.stderr)
    return results


def fetch_full_metadata(video_url):
    """Fetch full metadata + subtitle data for a single video."""
    opts = _ydl_opts()
    ydl = yt_dlp.YoutubeDL(opts)
    try:
        return ydl.extract_info(video_url, download=False)
    except Exception as exc:
        print(f"  [WARN] metadata fetch failed for {video_url}: {exc}", file=sys.stderr)
        return None


# ---------------------------------------------------------------------------
# Transcript extraction + segmentation
# ---------------------------------------------------------------------------

def extract_transcript(info):
    """
    Extract a list of (start_time, text) tuples from yt-dlp subtitle data.
    Tries manual subtitles first, then auto-generated.
    Returns [] if no transcript is available.
    """
    subs = info.get("subtitles") or {}
    auto_subs = info.get("automatic_captions") or {}

    # Prefer manual English subs, fall back to auto-generated
    candidate = None
    for lang_key in ("en", "en-US", "en-GB"):
        if subs.get(lang_key):
            candidate = subs[lang_key]
            break
    if candidate is None:
        for lang_key in ("en", "en-US", "en-GB"):
            if auto_subs.get(lang_key):
                candidate = auto_subs[lang_key]
                break

    if not candidate:
        return []

    # Pick the best subtitle format (prefer json, then vtt, then srt)
    fmt_priority = ("json3", "srv3", "vtt", "srt", "srv1", "srv2")
    chosen = None
    for fmt in fmt_priority:
        for track in candidate:
            if track.get("ext") == fmt:
                chosen = track
                break
        if chosen:
            break
    if chosen is None:
        chosen = candidate[0]

    # yt-dlp often leaves 'data' empty and only provides a 'url'.
    # Try inline data first, then fetch the URL.
    data = chosen.get("data")
    ext = chosen.get("ext", "")

    if isinstance(data, str) and data:
        parsed = _parse_subtitle_data(data, ext)
        if parsed:
            return parsed

    if isinstance(data, list):
        parsed = _parse_events(data)
        if parsed:
            return parsed

    # Fetch subtitle content from URL
    url = chosen.get("url")
    if url:
        try:
            req = urllib.request.Request(
                url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=30) as resp:
                content = resp.read().decode("utf-8", errors="replace")
            if content:
                parsed = _parse_subtitle_data(content, ext)
                if parsed:
                    return parsed
        except Exception as exc:
            print(f"    [WARN] subtitle fetch failed: {exc}",
                  file=sys.stderr)

    return []


def _parse_subtitle_data(text, ext):
    """Parse raw subtitle text into (start, text) tuples."""
    if not text:
        return []
    try:
        if ext in ("json3", "srv3"):
            return _parse_json3(text)
        elif ext == "vtt":
            return _parse_vtt(text)
        elif ext == "srt":
            return _parse_srt(text)
    except Exception:
        pass
    return []


def _parse_json3(text):
    """Parse YouTube json3 subtitle format."""
    obj = json.loads(text)
    events = obj.get("events") or []
    return _parse_events(events)


def _parse_events(events):
    """Parse the 'events' list common to json3/srv formats.

    json3 uses 'tStartMs' (milliseconds) and 'dDurationMs'.
    Some srv formats use 'start' / 'duration' in seconds.
    """
    out = []
    for ev in events:
        if not isinstance(ev, dict):
            continue
        # Try millisecond fields first (json3), then second fields (srv)
        start_ms = ev.get("tStartMs")
        if start_ms is None:
            start_s = ev.get("tStart") or ev.get("start") or 0.0
            start = float(start_s)
        else:
            start = float(start_ms) / 1000.0

        segs = ev.get("segs") or []
        parts = []
        for s in segs:
            if isinstance(s, dict):
                t = s.get("utf8") or s.get("text") or ""
                if t:
                    parts.append(t)
        line = "".join(parts).strip()
        # Filter pure noise / music tags
        line = _clean_caption_line(line)
        if line:
            out.append((start, line))
    return out


def _parse_vtt(text):
    """Parse WebVTT subtitle text."""
    return _parse_timecoded(text, vtt=True)


def _parse_srt(text):
    """Parse SubRip (.srt) subtitle text."""
    return _parse_timecoded(text, vtt=False)


def _parse_timecoded(text, vtt):
    """
    Shared parser for VTT/SRT — blocks separated by blank lines, each block:
        [index]            (srt only, optional)
        HH:MM:SS,mmm --> HH:MM:SS,mmm   (srt) or 00:00:00.000 --> ... (vtt)
        caption line(s)
    """
    out = []
    blocks = re.split(r"\n\s*\n", text.strip())
    for block in blocks:
        lines = [l for l in block.splitlines() if l.strip()]
        if not lines:
            continue
        # Find the timestamp line
        ts_idx = None
        for i, l in enumerate(lines):
            if "-->" in l:
                ts_idx = i
                break
        if ts_idx is None:
            continue
        ts_line = lines[ts_idx]
        match = re.match(
            r"(\d{1,2}:\d{2}:\d{2}[.,]\d{1,3})\s*-->\s*(\d{1,2}:\d{2}:\d{2}[.,]\d{1,3})",
            ts_line,
        )
        if not match:
            # try MM:SS form
            match = re.match(
                r"(\d{1,2}:\d{2}[.,]\d{1,3})\s*-->\s*(\d{1,2}:\d{2}[.,]\d{1,3})",
                ts_line,
            )
        if not match:
            continue
        start = _parse_timestamp(match.group(1))
        caption_lines = lines[ts_idx + 1:]
        line = " ".join(caption_lines).strip()
        line = _clean_caption_line(line)
        if line:
            out.append((start, line))
    return out


def _parse_timestamp(ts):
    """Parse HH:MM:SS,mmm / MM:SS.mmm / etc. into seconds (float)."""
    ts = ts.replace(",", ".").strip()
    parts = ts.split(":")
    try:
        nums = [float(p) for p in parts]
    except ValueError:
        return 0.0
    if len(nums) == 3:
        return nums[0] * 3600 + nums[1] * 60 + nums[2]
    elif len(nums) == 2:
        return nums[0] * 60 + nums[1]
    elif len(nums) == 1:
        return nums[0]
    return 0.0


_TAG_RE = re.compile(r"<[^>]+>")
_BRACKET_RE = re.compile(r"\[[^\]]*\]")
_WS_RE = re.compile(r"\s+")


def _clean_caption_line(line):
    """Strip HTML tags, [Music] tags, and collapse whitespace."""
    if not line:
        return ""
    line = _TAG_RE.sub("", line)
    line = _BRACKET_RE.sub("", line)
    line = line.replace("\n", " ")
    line = _WS_RE.sub(" ", line).strip()
    # Drop lines that are only noise
    if len(line) < 2:
        return ""
    return line


def segment_transcript(captions):
    """
    Split a list of (start, text) caption tuples into topic segments.

    Two splitting strategies:
    1. Gap-based: a timestamp gap >= SEGMENT_GAP_THRESHOLD starts a new
       segment (natural topic boundary — silence, scene change, etc.).
    2. Duration-based: if no gap occurs and the current segment exceeds
       SEGMENT_TARGET_DURATION, split at the next sentence boundary to
       keep segments focused on a single topic.

    Returns a list of dicts:
        {"text": "...", "start": float, "end": float}
    """
    if not captions:
        return []

    # Sort by start time
    captions = sorted(captions, key=lambda c: c[0])

    def _flush(lines, start_t, end_t):
        """Emit a segment if it passes duration + content checks."""
        seg_text = " ".join(lines).strip()
        duration = end_t - start_t
        if seg_text and duration >= SEGMENT_MIN_DURATION:
            segments.append({
                "text": seg_text,
                "start": start_t,
                "end": end_t,
            })

    segments = []
    current_lines = []
    current_start = captions[0][0]
    current_end = captions[0][0]
    prev_start = captions[0][0]

    for start, text in captions:
        gap = start - prev_start if prev_start else 0.0
        current_duration = current_end - current_start

        # Strategy 1: natural gap boundary
        if gap >= SEGMENT_GAP_THRESHOLD and current_lines:
            _flush(current_lines, current_start, current_end)
            current_lines = []
            current_start = start
        # Strategy 2: target duration reached — split at sentence end
        elif current_duration >= SEGMENT_TARGET_DURATION and current_lines:
            # Check if current text ends a sentence; if so, split here
            if current_lines[-1].rstrip().endswith((".", "!", "?")):
                _flush(current_lines, current_start, current_end)
                current_lines = []
                current_start = start
            # If we're way over target (2x), force a split regardless
            elif current_duration >= SEGMENT_TARGET_DURATION * 2:
                _flush(current_lines, current_start, current_end)
                current_lines = []
                current_start = start

        current_lines.append(text)
        current_end = start
        prev_start = start

    # Flush final segment
    if current_lines:
        _flush(current_lines, current_start, current_end)

    return segments


# ---------------------------------------------------------------------------
# Code example extraction
# ---------------------------------------------------------------------------

_CODE_FENCE_RE = re.compile(r"```[\w]*\n(.*?)```", re.DOTALL)
_INLINE_CODE_RE = re.compile(r"`([^`\n]{4,})`")


def extract_code_example(text):
    """
    Try to find a code block or code-like content in the transcript
    segment. Returns a code example string or '' if none found.

    Strategies (in priority order):
    1. Fenced code blocks (```...```) — manual subs with formatting
    2. Line-by-line code detection — formatted transcripts
    3. Inline code (`...`) — manual subs
    4. Command-pattern extraction from prose — auto-generated captions
       where the instructor speaks commands like "docker run" or "print"
    """
    if not text:
        return ""

    # Strategy 1: Fenced code block
    fences = _CODE_FENCE_RE.findall(text)
    if fences:
        return _truncate(fences[0].strip(), MAX_EXAMPLE_CHARS)

    # Strategy 2: Lines that look like code (formatted transcripts)
    code_lines = []
    for line in text.split("\n"):
        stripped = line.strip()
        if not stripped:
            continue
        if _looks_like_code(stripped):
            code_lines.append(stripped)
            if len(code_lines) >= 8:
                break
    if code_lines:
        return _truncate("\n".join(code_lines), MAX_EXAMPLE_CHARS)

    # Strategy 3: Inline code
    inline = _INLINE_CODE_RE.findall(text)
    if inline:
        return _truncate(inline[0].strip(), MAX_EXAMPLE_CHARS)

    # Strategy 4: Extract command-like phrases from prose (auto-captions)
    commands = _extract_commands_from_prose(text)
    if commands:
        return _truncate("\n".join(commands), MAX_EXAMPLE_CHARS)

    return ""


# Patterns for extracting spoken commands from auto-generated captions.
# These match command + argument patterns commonly spoken in tutorials.
_COMMAND_PATTERNS = [
    # Shell/CLI commands with subcommands or flags:
    # "docker run hello-world", "kubectl get pods", "npm install express"
    re.compile(
        r"\b(docker|kubectl|npm|yarn|pnpm|pip|cargo|git)\s+"
        r"(run|build|pull|push|create|start|stop|rm|ps|get|apply|delete|"
        r"install|add|init|new|clone|commit|push|pull|build|run|test|"
        r"exec|logs|describe|rollout|config|secret|service|deploy|"
        r"images|tag|volume|network|compose|system|info|version)"
        r"(?:\s+[\w\-./:@=,]+){0,4}",
        re.IGNORECASE,
    ),
    # Python function calls with args: print("hello"), len(my_list)
    re.compile(
        r"\b(print|input|len|range|type|str|int|float|list|dict|set|"
        r"open|sorted|enumerate|zip|map|filter)\s*\("
        r"[\"']?[\w\-., ]{1,60}[\"']?\)",
    ),
    # Python definitions: def foo(), class Bar, import os, from x import y
    re.compile(
        r"\b(def|class)\s+\w+\s*\([^)]{0,60}\)\s*:",
        re.IGNORECASE,
    ),
    re.compile(
        r"\b(import\s+\w[\w.]*|from\s+\w[\w.]*\s+import\s+\w+)",
        re.IGNORECASE,
    ),
    # Kubernetes YAML keys with values: apiVersion: apps/v1, kind: Deployment
    re.compile(
        r"\b(apiVersion|kind|metadata|spec|replicas|containers|image|"
        r"namespace)\s*:\s*[\w./-]+",
        re.IGNORECASE,
    ),
    # Variable assignment with code-like RHS: x = 5, name = "hello"
    re.compile(
        r"\b\w+\s*=\s*(?:[\"'][\w\- .]{2,40}[\"']|\d+|True|False|None)",
    ),
]

# Prose phrases that should NOT be treated as code even if they match patterns
_PROSE_FILTERS = frozenset({
    "docker is", "docker allows", "docker called", "docker image",
    "docker engine", "docker service", "docker desktop",
    "make sure", "docker and", "docker so", "docker for",
    "docker to", "docker in", "docker on", "docker with",
})


def _extract_commands_from_prose(text):
    """
    Extract command-like phrases from auto-generated caption prose.
    Returns a list of code-like strings, filtered to reduce false positives.
    """
    # Prose words that signal the command has ended and prose resumed
    _prose_breakers = re.compile(
        r"\s+(which|that|with|actually|basically|here|there|and|then|so|"
        r"now|we|you|this|if|but|also|just|like|will|can|should|would|"
        r"called|using|called|see|shown|takes|creates|stops|starts|"
        r"basically|basically|only|see|here|in|on|for|to|of|a|an|the)"
        r"\s.*$",
        re.IGNORECASE,
    )

    commands = []
    seen = set()
    for pattern in _COMMAND_PATTERNS:
        for match in pattern.finditer(text):
            cmd = match.group(0).strip().rstrip(".,;!?")
            # Strip trailing prose that leaked past the pattern
            cmd = _prose_breakers.sub("", cmd).strip()
            cmd = cmd.rstrip(".,;!?").strip()
            # Filter
            cmd_low = cmd.lower()
            if len(cmd) < 4:
                continue
            if any(cmd_low.startswith(pf) for pf in _PROSE_FILTERS):
                continue
            parts = cmd_low.split()
            if len(parts) <= 1:
                continue
            # Skip commands that are too long (likely prose, not code)
            if len(parts) > 6:
                continue
            if cmd_low not in seen:
                seen.add(cmd_low)
                commands.append(cmd)
            if len(commands) >= 8:
                break
        if len(commands) >= 8:
            break
    return commands


_CODE_INDICATORS = (
    "def ", "function ", "func ", "class ", "import ", "from ", "const ",
    "let ", "var ", "return ", "if (", "if(", "for (", "for(", "while (",
    "while(", "=>", "->", "==", "!=", "println!", "console.log", "print(",
    "fmt.", "use ", "pub fn", "pub async", "async function", "export ",
    "require(", "module.exports", "#!/", "docker", "kubectl", "apiVersion:",
    "kind:", "metadata:", "spec:", "npm ", "cargo ", "pip ", "git ",
)


def _looks_like_code(line):
    """Heuristic: does this line look like code rather than prose?"""
    if len(line) > 200:
        return False
    low = line.lower()
    for ind in _CODE_INDICATORS:
        if ind in low:
            return True
    # High symbol density
    symbols = sum(1 for c in line if c in "{}[]()=;<>:+-*/&|!")
    if len(line) > 10 and symbols / len(line) > 0.15:
        return True
    return False


# ---------------------------------------------------------------------------
# SFT pair construction
# ---------------------------------------------------------------------------

def _truncate(text, max_chars):
    if len(text) <= max_chars:
        return text
    cut = text[:max_chars]
    # Try to break on a word boundary
    last_space = cut.rfind(" ")
    if last_space > max_chars * 0.6:
        cut = cut[:last_space]
    return cut.rstrip() + "…"


def build_sft_pair(title, segment, code_example):
    """
    Build a single SFT pair dict from a transcript segment.
    """
    topic = _extract_topic(title)
    assistant_content = _truncate(segment["text"], MAX_ASSISTANT_CHARS)

    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": f"Explain {topic}"},
        {"role": "assistant", "content": assistant_content},
    ]

    if code_example:
        messages.append({"role": "user", "content": "Can you show me an example?"})
        messages.append({"role": "assistant", "content": code_example})
    else:
        # No code found — synthesize a brief example request/response so the
        # conversation still has the 5-message structure the spec asks for.
        messages.append({"role": "user", "content": "Can you show me an example?"})
        messages.append({
            "role": "assistant",
            "content": _synthesize_example(assistant_content, topic),
        })

    return {
        "messages": messages,
        "source": "youtube-tutorial",
        "tier": None,
    }

def _extract_topic(title):
    """
    Extract a clean topic phrase from a video title like:
        'Python Tutorial for Beginners 2024 - Full Course'
        -> 'python'
    """
    if not title:
        return "this coding concept"
    # Remove content in brackets/parens, emojis, URLs
    t = re.sub(r"\[[^\]]*\]", "", title)
    t = re.sub(r"\([^)]*\)", "", t)
    t = re.sub(r"https?://\S+", "", t)
    # Remove year + common filler (aligned with reddit-scraper contract)
    t = re.sub(r"\b(20\d{2})\b", "", t)
    t = re.sub(
        r"\b(full course|full tutorial|for beginners|crash course|"
        r"step by step|from scratch|complete guide|complete tutorial|"
        r"complete course|tutorial|explained|you need to know|"
        r"ultimate guide|beginners guide|introduction to|how to|"
        r"the basics|deep dive)\b",
        "", t, flags=re.IGNORECASE)
    # Strip trailing separators + whitespace
    t = re.sub(r"[-|–—:]\s*$", "", t.strip())
    t = re.sub(r"\s+", " ", t).strip()
    if not t:
        t = title.strip()
    # Lowercase for natural phrasing
    return t[:200].lower().strip()


# Stopwords for topic manifest normalization (aligned with reddit-scraper)
_STOPWORDS = frozenset({
    "a", "an", "the", "and", "or", "but", "in", "on", "at", "to", "for",
    "of", "with", "by", "from", "up", "about", "into", "through", "during",
    "before", "after", "above", "below", "between", "is", "are", "was",
    "were", "be", "been", "being", "have", "has", "had", "do", "does",
    "did", "will", "would", "should", "could", "can", "may", "might",
    "this", "that", "these", "those", "it", "its", "your", "you", "we",
    "they", "he", "she", "i", "me", "my", "our", "their", "his", "her",
    "what", "which", "who", "whom", "how", "why", "when", "where",
})


def normalize_topic_for_manifest(title):
    """
    Normalize a video title into a topic keyphrase for the cross-scraper
    topics manifest. Aligned with reddit-scraper:
      - lowercase
      - strip URLs, brackets, parens
      - remove years (20XX)
      - strip filler phrases (tutorial, explained, ultimate guide, etc.)
      - remove punctuation
      - remove stopwords
      - take first ~8 meaningful words
    """
    if not title:
        return ""
    t = title.lower()
    t = re.sub(r"https?://\S+", "", t)
    t = re.sub(r"\[[^\]]*\]", "", t)
    t = re.sub(r"\([^)]*\)", "", t)
    t = re.sub(r"\b(20\d{2})\b", "", t)
    t = re.sub(
        r"\b(full course|full tutorial|for beginners|crash course|"
        r"step by step|from scratch|complete guide|complete tutorial|"
        r"complete course|tutorial|explained|you need to know|"
        r"ultimate guide|beginners guide|introduction to|how to|"
        r"the basics|deep dive)\b",
        "", t, flags=re.IGNORECASE)
    # Remove punctuation (keep alphanumerics + spaces)
    t = re.sub(r"[^\w\s]", " ", t)
    # Tokenize, strip stopwords
    words = [w for w in t.split() if w and w not in _STOPWORDS]
    # Take first ~8 meaningful words
    return " ".join(words[:8]).strip()


def _synthesize_example(content, topic):
    """
    When no code block is found in the transcript, synthesize a minimal
    example response that references the topic, keeping the 5-turn shape.
    """
    # Pull a short illustrative sentence from the content if possible
    sentences = re.split(r"(?<=[.!?])\s+", content)
    snippet = ""
    for s in sentences:
        if len(s) > 30 and not s.endswith(":"):
            snippet = s[:300]
            break
    if snippet:
        return (
            f"Here's the key idea from the discussion on {topic}:\n\n"
            f"{snippet}\n\n"
            f"Try applying this concept in your own {topic} project to "
            f"reinforce the pattern."
        )
    return (
        f"Based on the {topic} explanation above, the best next step is "
        f"to implement a small example yourself — start with the simplest "
        f"case and build up from there."
    )


# ---------------------------------------------------------------------------
# Main pipeline
# ---------------------------------------------------------------------------

def process_video(video_meta, seen_ids):
    """
    Process a single video: fetch metadata, extract transcript, segment,
    and build SFT pairs. Returns (pairs_list, topic_entry, info_dict).
    topic_entry is a dict for the topics manifest, or None if no video.
    """
    vid = video_meta.get("id")
    url = video_meta.get("url")
    if not url:
        return [], None, None
    if vid and vid in seen_ids:
        return [], None, None

    info = fetch_full_metadata(url)
    if not info:
        return [], None, None

    if vid:
        seen_ids.add(vid)

    title = info.get("title") or video_meta.get("title") or "coding tutorial"
    duration = info.get("duration") or 0

    # Build topic manifest entry (one per video, aligned with reddit-scraper)
    topic_entry = {
        "topic": normalize_topic_for_manifest(title),
        "source": "youtube",
        "title": title,
        "url": url,
    }

    # Skip very short videos (likely ads / shorts)
    if duration and duration < 60:
        return [], topic_entry, info

    captions = extract_transcript(info)
    if not captions:
        print(f"    [SKIP] no transcript: {title[:70]}", file=sys.stderr)
        return [], topic_entry, info

    segments = segment_transcript(captions)
    if not segments:
        print(f"    [SKIP] no usable segments: {title[:70]}", file=sys.stderr)
        return [], topic_entry, info

    pairs = []
    for seg in segments:
        code = extract_code_example(seg["text"])
        pair = build_sft_pair(title, seg, code)
        pairs.append(pair)

    return pairs, topic_entry, info


def run(queries, max_per_query, output_path, topics_path=TOPICS_PATH):
    os.makedirs(os.path.dirname(output_path), exist_ok=True)
    os.makedirs(os.path.dirname(topics_path), exist_ok=True)

    total_pairs = 0
    total_videos = 0
    total_topics = 0
    seen_ids = set()

    with open(output_path, "a", encoding="utf-8") as out_f, \
         open(topics_path, "a", encoding="utf-8") as topic_f:
        for qi, query in enumerate(queries, 1):
            print(f"\n[{qi}/{len(queries)}] Searching: '{query}' "
                  f"(max {max_per_query})", file=sys.stderr)

            videos = search_videos(query, max_per_query)
            print(f"  Found {len(videos)} videos", file=sys.stderr)

            for vi, vmeta in enumerate(videos, 1):
                title_preview = vmeta.get("title", "")[:60]
                print(f"  ({vi}/{len(videos)}) {title_preview}", file=sys.stderr)

                pairs, topic_entry, info = process_video(vmeta, seen_ids)

                # Write topic manifest entry for every video we fetched
                # metadata for (even if no transcript), so the cross-scraper
                # map has full coverage.
                if topic_entry:
                    topic_f.write(
                        json.dumps(topic_entry, ensure_ascii=False) + "\n")
                    topic_f.flush()
                    total_topics += 1

                if not pairs:
                    continue

                for pair in pairs:
                    out_f.write(json.dumps(pair, ensure_ascii=False) + "\n")
                out_f.flush()
                total_pairs += len(pairs)
                total_videos += 1
                print(f"    -> {len(pairs)} SFT pairs", file=sys.stderr)

                # Be gentle with YouTube between videos
                time.sleep(0.5)

    print(f"\n{'=' * 60}", file=sys.stderr)
    print(f"Done. Videos processed: {total_videos}", file=sys.stderr)
    print(f"SFT pairs written:     {total_pairs}", file=sys.stderr)
    print(f"Topic entries written: {total_topics}", file=sys.stderr)
    print(f"SFT output:   {output_path}", file=sys.stderr)
    print(f"Topics output: {topics_path}", file=sys.stderr)
    print(f"{'=' * 60}", file=sys.stderr)
    return total_pairs


def main():
    parser = argparse.ArgumentParser(
        description="Scrape YouTube coding tutorials into SFT pairs for fine-tuning."
    )
    parser.add_argument(
        "--max", type=int, default=100,
        help="Max videos per search query (default: 100)",
    )
    parser.add_argument(
        "--query", type=str, default=None,
        help="Single search query (overrides default query list)",
    )
    parser.add_argument(
        "--output", type=str, default=OUTPUT_PATH,
        help=f"Output JSONL path (default: {OUTPUT_PATH})",
    )
    parser.add_argument(
        "--topics", type=str, default=TOPICS_PATH,
        help=f"Topics manifest JSONL path (default: {TOPICS_PATH})",
    )
    args = parser.parse_args()

    if args.query:
        queries = [args.query]
    else:
        queries = DEFAULT_QUERIES

    run(queries, args.max, args.output, args.topics)


if __name__ == "__main__":
    main()
