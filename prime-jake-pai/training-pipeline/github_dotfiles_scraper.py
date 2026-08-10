#!/usr/bin/env python3
"""github_dotfiles_scraper.py — harvest shell history files from GitHub dotfiles repos.

Uses the GitHub code search API to find repos containing .bash_history / .zsh_history
files, filters to repos owned by high-signal developers (>N followers), downloads and
parses the history files into command sequences, clusters commands into problem-solving
episodes using temporal gaps and command-type transitions, and writes SFT training
entries in axolotl chat format.

When the GitHub REST code search API is degraded (returns total_count but 0 items — a
known platform issue), falls back to repository search + Git Trees API scanning to
discover repos with committed history files.

Usage:
    python3 github_dotfiles_scraper.py [--max 500] [--min-followers 500]

Output: /home/user/rig-ft/data/raw/github_dotfiles.jsonl
"""
import argparse
import json
import os
import re
import sys
import time

import requests

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

GITHUB_TOKEN = os.environ.get(
    "GITHUB_TOKEN", "os.environ.get("GITHUB_TOKEN", "")"
)
API_BASE = "https://api.github.com"
RAW_BASE = "https://raw.githubusercontent.com"
HEADERS = {
    "Authorization": f"token {GITHUB_TOKEN}",
    "Accept": "application/vnd.github+json",
    "User-Agent": "rig-ft-scraper/1.0",
}

OUTPUT_PATH = "/home/user/rig-ft/data/raw/github_dotfiles.jsonl"

HISTORY_FILENAMES = (".bash_history", ".zsh_history")
MIN_EPISODE_GAP_SEC = 30          # >30s gap between commands → new episode
MIN_INTERESTING_RATIO = 0.15      # skip files where <15% commands are interesting
MIN_EPISODE_COMMANDS = 3          # episodes with fewer commands are not useful
MIN_FILE_COMMANDS = 10            # skip tiny history files
MAX_FILE_BYTES = 5_000_000        # skip absurdly large files (5 MB)

# Commands that are trivial / repetitive and don't contribute to problem-solving.
TRIVIAL_COMMANDS = {
    "ls", "ll", "la", "l", "cd", "pwd", "clear", "cls", "exit", "quit",
    "history", "h", "echo", "true", "false", ":", "noop",
    "fg", "bg", "jobs", "disown", "wait",
    "alias", "unalias", "export", "unset", "set", "source", ".",
    "pushd", "popd", "dirs",
    "whoami", "hostname", "date", "cal", "uptime",
    "which", "whereis", "type", "hash",
    "man", "help", "info", "tldr",
    "git status", "git log", "git diff", "git branch", "git show",
    "git add .", "git add -A", "git add --all",
    "git commit -m", "git push", "git pull", "git fetch",
    "git checkout", "git switch", "git merge", "git rebase",
    "npm install", "npm i", "yarn", "pnpm install",
    "pip install", "pip3 install", "python", "python3",
    "node", "go run", "cargo run", "make",
}

# ---------------------------------------------------------------------------
# Rate-limiting
# ---------------------------------------------------------------------------

_last_call_time = 0.0

def _throttle(min_interval=1.2):
    """Sleep to respect GitHub secondary rate limits (max ~1 req/sec for search)."""
    global _last_call_time
    elapsed = time.time() - _last_call_time
    if elapsed < min_interval:
        time.sleep(min_interval - elapsed)
    _last_call_time = time.time()


def _check_rate_limit(response):
    """If rate-limited, sleep until reset."""
    remaining = response.headers.get("X-RateLimit-Remaining")
    reset = response.headers.get("X-RateLimit-Reset")
    resource = response.headers.get("X-RateLimit-Resource", "core")
    if remaining is not None and int(remaining) == 0 and reset:
        wait = int(reset) - int(time.time()) + 2
        if 0 < wait < 3700:
            print(f"  [{resource}] rate-limited, waiting {wait}s...", file=sys.stderr)
            time.sleep(wait)


def gh_get(url, params=None, max_retries=3):
    """GET with retry, rate-limit handling, and throttling."""
    for attempt in range(max_retries):
        _throttle()
        try:
            r = requests.get(url, headers=HEADERS, params=params, timeout=30)
        except requests.RequestException as exc:
            print(f"  request error (attempt {attempt+1}): {exc}", file=sys.stderr)
            time.sleep(3 * (attempt + 1))
            continue

        if r.status_code == 200:
            return r

        if r.status_code in (403, 429):
            _check_rate_limit(r)
            # Secondary rate limit — back off longer
            retry_after = r.headers.get("Retry-After")
            wait = int(retry_after) if retry_after else 30 * (attempt + 1)
            print(f"  403/429 (attempt {attempt+1}), sleeping {wait}s...",
                  file=sys.stderr)
            time.sleep(wait)
            continue

        if r.status_code == 404:
            return r  # caller handles 404

        print(f"  HTTP {r.status_code} (attempt {attempt+1}): "
              f"{r.text[:200]}", file=sys.stderr)
        time.sleep(3 * (attempt + 1))

    return None


# ---------------------------------------------------------------------------
# Strategy 1: GitHub Code Search API
# ---------------------------------------------------------------------------

def search_code_for_history_files(max_results):
    """Use GitHub code search API to find .bash_history / .zsh_history files.

    Returns list of (repo_full_name, file_path, owner_login).
    """
    results = []
    seen_repos = set()
    per_page = 100

    for filename in HISTORY_FILENAMES:
        query = f"filename:{filename}"
        page = 1
        max_pages = max(1, max_results // per_page + 1)

        while page <= max_pages and len(results) < max_results:
            r = gh_get(
                f"{API_BASE}/search/code",
                params={"q": query, "per_page": per_page, "page": page},
            )
            if r is None:
                break

            data = r.json()
            total = data.get("total_count", 0)
            items = data.get("items", [])

            # GitHub's code search REST API has a known platform issue where it
            # returns total_count > 0 but items = [] for many queries. When this
            # happens we return what we have (possibly empty) and the caller will
            # fall back to repository search + trees scanning.
            if not items:
                if total > 0 and page == 1:
                    print(f"  [code-search] {filename}: total_count={total} "
                          f"but 0 items returned (GitHub platform issue)",
                          file=sys.stderr)
                break

            for item in items:
                repo_info = item.get("repository", {})
                repo_full = repo_info.get("full_name", "")
                owner = repo_info.get("owner", {}).get("login", "")
                path = item.get("path", "")
                key = (repo_full, path)
                if key not in seen_repos:
                    seen_repos.add(key)
                    results.append((repo_full, path, owner))
                    if len(results) >= max_results:
                        break

            if len(items) < per_page:
                break
            page += 1
            # code_search rate limit is 10/min
            time.sleep(7)

    return results


# ---------------------------------------------------------------------------
# Strategy 2 (fallback): Repository search + Git Trees API
# ---------------------------------------------------------------------------

REPO_SEARCH_QUERIES = [
    "dotfiles in:name stars:>5",
    "dotfiles bash stars:>5",
    "dotfiles zsh stars:>5",
    "dotfiles shell stars:>5",
    "my-config stars:>3",
    "linux-config stars:>3",
    "dotfiles terminal stars:>3",
    "shell-config stars:>3",
    "dotfiles history stars:>1",
    "bashrc zshrc stars:>3",
]


def search_repos(query, per_page=50, max_pages=3):
    """Search GitHub repositories, yield repo dicts."""
    for page in range(1, max_pages + 1):
        r = gh_get(
            f"{API_BASE}/search/repositories",
            params={
                "q": query,
                "sort": "stars",
                "order": "desc",
                "per_page": per_page,
                "page": page,
            },
        )
        if r is None or r.status_code != 200:
            break
        data = r.json()
        items = data.get("items", [])
        if not items:
            break
        for repo in items:
            yield repo
        if len(items) < per_page:
            break


def find_history_files_in_repo(repo_full, branch):
    """Use Git Trees API (recursive) to find .bash_history / .zsh_history files.

    Returns list of {path, size, sha} dicts.
    """
    r = gh_get(f"{API_BASE}/repos/{repo_full}/git/trees/{branch}?recursive=1")
    if r is None or r.status_code != 200:
        return []

    data = r.json()
    if data.get("truncated"):
        return []  # tree too large, skip

    found = []
    for item in data.get("tree", []):
        if item.get("type") != "blob":
            continue
        path = item.get("path", "")
        size = item.get("size", 0)
        if (path.endswith(HISTORY_FILENAMES)
                and MIN_FILE_COMMANDS <= size <= MAX_FILE_BYTES):
            found.append({
                "path": path,
                "size": size,
                "sha": item.get("sha"),
            })
    return found


def discover_via_repo_search(max_results, min_followers):
    """Discover repos with history files via repo search + trees scanning.

    Returns list of (repo_full_name, file_path, owner_login, branch, stars).
    """
    results = []
    seen_repos = set()

    for query in REPO_SEARCH_QUERIES:
        if len(results) >= max_results:
            break
        print(f"  [repo-search] {query}", file=sys.stderr)

        for repo in search_repos(query, per_page=50, max_pages=3):
            if len(results) >= max_results:
                break
            full = repo.get("full_name", "")
            if full in seen_repos:
                continue
            seen_repos.add(full)

            branch = repo.get("default_branch", "master")
            owner = repo.get("owner", {}).get("login", "")
            stars = repo.get("stargazers_count", 0)

            hist_files = find_history_files_in_repo(full, branch)
            if hist_files:
                for f in hist_files:
                    results.append((full, f["path"], owner, branch, stars))
                    print(f"    ✓ {full}/{f['path']} ({f['size']} bytes)",
                          file=sys.stderr)
                # Check owner followers — but we do this in the filter step

        # search rate limit is 30/min
        time.sleep(2)

    return results


# ---------------------------------------------------------------------------
# User filtering
# ---------------------------------------------------------------------------

_user_cache = {}


def get_user_info(login):
    """Fetch user info with caching."""
    if login in _user_cache:
        return _user_cache[login]
    r = gh_get(f"{API_BASE}/users/{login}")
    if r is None or r.status_code != 200:
        _user_cache[login] = {"followers": 0, "public_repos": 0}
        return _user_cache[login]
    data = r.json()
    info = {
        "followers": data.get("followers", 0),
        "public_repos": data.get("public_repos", 0),
        "following": data.get("following", 0),
    }
    _user_cache[login] = info
    return info


def passes_user_filter(login, min_followers):
    """Check if user passes the follower/contribution filter."""
    info = get_user_info(login)
    followers = info.get("followers", 0)
    public_repos = info.get("public_repos", 0)
    # >500 followers OR high contribution proxy (>200 public repos)
    return followers > min_followers or public_repos > 200


# ---------------------------------------------------------------------------
# History file parsing
# ---------------------------------------------------------------------------

# zsh extended history: ": <epoch>:<elapsed>;<command>"
ZSH_HISTORY_RE = re.compile(r"^:\s*(\d+):(\d+);(.*)$")

# bash history with HISTTIMEFORMAT: "#<epoch>\n<command>"
BASH_TS_RE = re.compile(r"^#(\d{9,11})$")

# Multi-line commands in zsh history start with a backslash-escaped newline
ZSH_CONT_RE = re.compile(r"\\\s*$")


def parse_history_file(content, filename):
    """Parse a shell history file into a list of command dicts.

    Each dict: {command, timestamp, elapsed}
    timestamp is epoch int or 0 if unknown.
    """
    commands = []
    lines = content.split("\n")

    i = 0
    pending_ts = 0

    while i < len(lines):
        raw = lines[i]
        line = raw.rstrip("\r")
        i += 1

        if not line.strip():
            continue

        # zsh extended history format
        m = ZSH_HISTORY_RE.match(line)
        if m:
            ts = int(m.group(1))
            elapsed = int(m.group(2))
            cmd = m.group(3)
            # Handle multi-line commands (continuation lines)
            while ZSH_CONT_RE.search(cmd) and i < len(lines):
                next_line = lines[i].rstrip("\r")
                i += 1
                cmd = cmd.rstrip("\\") + "\n" + next_line
            commands.append({"command": cmd, "timestamp": ts, "elapsed": elapsed})
            pending_ts = 0
            continue

        # bash timestamp marker
        m = BASH_TS_RE.match(line)
        if m:
            pending_ts = int(m.group(1))
            continue

        # plain command line (bash without timestamps, or unparseable)
        cmd = line.strip()
        if cmd and not cmd.startswith("#"):
            commands.append({
                "command": cmd,
                "timestamp": pending_ts,
                "elapsed": 0,
            })
            pending_ts = 0

    return commands


# ---------------------------------------------------------------------------
# Command classification
# ---------------------------------------------------------------------------

def get_command_root(cmd):
    """Extract the root command (first token or first few tokens for git/npm)."""
    cmd = cmd.strip()
    if not cmd:
        return ""
    # Strip leading env vars (FOO=bar cmd ...)
    while re.match(r"^\w+=\S+\s+", cmd):
        cmd = re.sub(r"^\w+=\S+\s+", "", cmd)
    # Strip leading sudo
    if cmd.startswith("sudo "):
        cmd = cmd[5:]
    # Strip leading time/nohup
    cmd = re.sub(r"^(time|nohup)\s+", "", cmd)
    parts = cmd.split()
    if not parts:
        return ""
    root = parts[0]
    # For compound commands (git, npm, docker, kubectl, etc.), include subcommand
    if root in ("git", "npm", "yarn", "pnpm", "docker", "kubectl",
                "pip", "pip3", "brew", "apt", "apt-get", "cargo", "go",
                "rustup", "gh", "aws", "gcloud", "az", "terraform",
                "ansible", "vagrant", "systemctl", "service"):
        if len(parts) > 1:
            return f"{root} {parts[1]}"
    return root


def is_trivial(cmd):
    """Check if a command is trivial/repetitive."""
    root = get_command_root(cmd)
    if not root:
        return True
    # Check exact matches
    cmd_stripped = cmd.strip()
    for trivial in TRIVIAL_COMMANDS:
        if cmd_stripped == trivial or cmd_stripped.startswith(trivial + " "):
            # But "git push origin main" is more interesting than just "git push"
            if trivial in ("git push", "git pull", "git fetch", "git add .",
                           "git add -A", "git add --all",
                           "npm install", "npm i", "pip install", "pip3 install"):
                # Only trivial if it's exactly that command with no args
                if cmd_stripped == trivial:
                    return True
                continue
            return True
    return False


def is_repetitive(cmd, recent_commands, window=5):
    """Check if a command repeats within the recent window."""
    cmd_norm = re.sub(r"\s+", " ", cmd.strip().lower())
    if not cmd_norm:
        return True
    for recent in recent_commands[-window:]:
        if re.sub(r"\s+", " ", recent.strip().lower()) == cmd_norm:
            return True
    return False


def compute_interesting_ratio(commands):
    """Compute ratio of non-trivial, non-repetitive commands."""
    if not commands:
        return 0.0
    recent = []
    interesting = 0
    for c in commands:
        cmd = c["command"]
        trivial = is_trivial(cmd)
        repetitive = is_repetitive(cmd, recent)
        recent.append(cmd)
        if not trivial and not repetitive:
            interesting += 1
    return interesting / len(commands)


# ---------------------------------------------------------------------------
# Episode clustering
# ---------------------------------------------------------------------------

def classify_command_type(cmd):
    """Classify a command into a type for transition detection."""
    root = get_command_root(cmd)
    if not root:
        return "empty"
    if root in ("git", "git push", "git pull", "git commit", "git checkout",
                "git merge", "git rebase", "git add", "git clone"):
        return "vcs"
    if root in ("npm", "yarn", "pnpm", "node", "npx", "bun", "deno"):
        return "js"
    if root in ("python", "python3", "pip", "pip3", "poetry", "pipenv",
                "conda", "uv"):
        return "python"
    if root in ("go", "cargo", "rustc", "rustup"):
        return "rust_go"
    if root in ("docker", "docker-compose", "kubectl", "helm", "oc"):
        return "container"
    if root in ("make", "cmake", "ninja"):
        return "build"
    if root in ("curl", "wget", "ssh", "scp", "rsync"):
        return "network"
    if root in ("cd", "ls", "pwd", "pushd", "popd"):
        return "navigation"
    if root in ("cat", "grep", "sed", "awk", "head", "tail", "less", "more",
                "vi", "vim", "nano", "emacs", "code"):
        return "edit_view"
    if root in ("mkdir", "rm", "cp", "mv", "touch", "chmod", "chown", "ln",
                "tar", "zip", "unzip"):
        return "filesystem"
    if root in ("sudo", "apt", "apt-get", "brew", "pacman", "yum", "dnf"):
        return "package"
    if root in ("ps", "kill", "top", "htop", "jobs", "bg", "fg"):
        return "process"
    if root in ("export", "source", "alias", "echo"):
        return "shell_config"
    if root in ("find", "locate", "which", "whereis", "type"):
        return "search"
    if root in ("echo", "printf", "tee"):
        return "output"
    if root in ("test", "[", "[[", "true", "false"):
        return "test"
    return "other"


def cluster_into_episodes(commands):
    """Cluster commands into problem-solving episodes.

    An episode boundary occurs when:
    1. There's a temporal gap > MIN_EPISODE_GAP_SEC between consecutive commands
       (only when timestamps are available).
    2. There's a major command-type transition (e.g., from "vcs" to "container"
       after a run of same-type commands).
    """
    if not commands:
        return []

    episodes = []
    current_episode = [commands[0]]
    current_type = classify_command_type(commands[0]["command"])
    type_run = 1

    for i in range(1, len(commands)):
        cmd = commands[i]
        prev = commands[i - 1]

        # Check temporal gap
        gap = None
        if (cmd["timestamp"] > 0 and prev["timestamp"] > 0):
            gap = cmd["timestamp"] - prev["timestamp"]

        # Check command type
        cmd_type = classify_command_type(cmd["command"])

        # Episode boundary conditions
        boundary = False
        if gap is not None and gap > MIN_EPISODE_GAP_SEC:
            boundary = True
        elif gap is None and cmd_type != current_type and type_run >= 3:
            # Type transition after a run of same-type commands (only when
            # no timestamps available)
            boundary = True

        if boundary:
            if len(current_episode) >= MIN_EPISODE_COMMANDS:
                episodes.append(current_episode)
            current_episode = [cmd]
            current_type = cmd_type
            type_run = 1
        else:
            current_episode.append(cmd)
            if cmd_type == current_type:
                type_run += 1
            else:
                current_type = cmd_type
                type_run = 1

    # Don't forget the last episode
    if len(current_episode) >= MIN_EPISODE_COMMANDS:
        episodes.append(current_episode)

    return episodes


# ---------------------------------------------------------------------------
# SFT entry generation
# ---------------------------------------------------------------------------

SYSTEM_PROMPT = (
    "You are an elite developer working in a terminal. You approach problems "
    "methodically, choosing the right tools for each task. You explain your "
    "reasoning and provide command sequences that solve the problem efficiently."
)


def infer_goal(commands):
    """Infer a natural-language goal from a sequence of commands."""
    types = [classify_command_type(c["command"]) for c in commands]
    roots = [get_command_root(c["command"]) for c in commands]

    # Count type frequencies
    type_counts = {}
    for t in types:
        type_counts[t] = type_counts.get(t, 0) + 1
    dominant_type = max(type_counts, key=type_counts.get)

    # Build goal description based on dominant activity
    goals = {
        "vcs": "manage my Git repository and handle version control tasks",
        "js": "set up and work with a JavaScript/Node.js project",
        "python": "set up and work with a Python project",
        "rust_go": "build and develop a Rust or Go project",
        "container": "work with containers and orchestrate deployments",
        "build": "build and compile a project",
        "network": "fetch resources or manage remote connections",
        "filesystem": "organize and manage files and directories",
        "package": "install and configure system packages",
        "process": "manage running processes",
        "edit_view": "inspect and edit files",
        "search": "search for files or content in the codebase",
    }

    base_goal = goals.get(dominant_type, "accomplish a development task")

    # Look for specific patterns to refine the goal
    cmd_text = " ".join(c["command"] for c in commands).lower()

    if "clone" in cmd_text and ("git" in cmd_text or "repo" in cmd_text):
        base_goal = "clone a repository and set up the development environment"
    elif "docker" in cmd_text and "build" in cmd_text:
        base_goal = "build and run a Docker container for my project"
    elif "deploy" in cmd_text or "kubectl" in cmd_text:
        base_goal = "deploy and manage my application"
    elif "test" in cmd_text or "pytest" in cmd_text or "jest" in cmd_text:
        base_goal = "run and debug tests for my project"
    elif "lint" in cmd_text or "eslint" in cmd_text or "flake8" in cmd_text:
        base_goal = "lint and fix code quality issues"
    elif "install" in cmd_text and "requirements" in cmd_text:
        base_goal = "set up Python project dependencies"
    elif "install" in cmd_text and "package.json" in cmd_text:
        base_goal = "set up Node.js project dependencies"
    elif "ssh" in cmd_text:
        base_goal = "connect to and manage a remote server"
    elif "grep" in cmd_text or "find" in cmd_text or "rg " in cmd_text:
        base_goal = "search through the codebase to find specific content"
    elif "chmod" in cmd_text or "chown" in cmd_text:
        base_goal = "fix file permissions"
    elif "systemctl" in cmd_text or "service" in cmd_text:
        base_goal = "manage system services"

    return f"I need to {base_goal}"


def explain_command(cmd):
    """Generate a brief explanation for a single command."""
    root = get_command_root(cmd)
    cmd_type = classify_command_type(cmd)

    explanations = {
        "git clone": "Clone the remote repository to get the code locally",
        "git add": "Stage the changes for commit",
        "git commit": "Commit the staged changes with a message",
        "git push": "Push the commits to the remote repository",
        "git pull": "Pull the latest changes from the remote",
        "git checkout": "Switch branches or restore files",
        "git branch": "List, create, or delete branches",
        "git merge": "Merge changes from another branch",
        "git rebase": "Rebase commits onto another branch",
        "git log": "View the commit history",
        "git diff": "Show unstaged changes",
        "npm install": "Install project dependencies from package.json",
        "npm run": "Run a script defined in package.json",
        "yarn install": "Install dependencies using Yarn",
        "pip install": "Install a Python package",
        "python": "Run a Python script or start the REPL",
        "docker build": "Build a Docker image from a Dockerfile",
        "docker run": "Run a command in a new Docker container",
        "docker-compose": "Manage multi-container Docker applications",
        "kubectl apply": "Apply a configuration to a Kubernetes cluster",
        "kubectl get": "Display one or many Kubernetes resources",
        "kubectl logs": "Print the logs of a container",
        "make": "Run a makefile target to build the project",
        "mkdir": "Create a new directory",
        "cd": "Change to a different working directory",
        "ls": "List directory contents",
        "cat": "Display the contents of a file",
        "grep": "Search for a pattern in file contents",
        "find": "Search for files matching criteria",
        "chmod": "Change file permissions",
        "chown": "Change file ownership",
        "curl": "Fetch data from a URL",
        "wget": "Download a file from the web",
        "ssh": "Connect to a remote host",
        "scp": "Copy files to/from a remote host",
        "sudo": "Run a command with elevated privileges",
        "apt": "Install or manage system packages (Debian/Ubuntu)",
        "brew": "Install or manage packages via Homebrew (macOS)",
        "systemctl": "Manage systemd services",
        "export": "Set an environment variable",
        "source": "Execute a file in the current shell context",
        "ps": "List running processes",
        "kill": "Send a signal to a process",
        "tar": "Create or extract a tar archive",
        "ln": "Create a symbolic or hard link",
        "sed": "Stream-edit text using patterns",
        "awk": "Process and transform text data",
        "head": "Show the first lines of a file",
        "tail": "Show the last lines of a file",
        "vi": "Open the file in the vi editor",
        "vim": "Open the file in the Vim editor",
        "code": "Open the file in VS Code",
        "pytest": "Run Python tests with pytest",
        "jest": "Run JavaScript tests with Jest",
        "eslint": "Lint JavaScript/TypeScript files",
        "flake8": "Lint Python files with flake8",
        "cargo": "Build or manage a Rust project",
        "go build": "Compile a Go project",
        "go run": "Compile and run a Go program",
        "terraform": "Manage infrastructure as code",
        "ansible": "Run automation playbooks",
    }

    explanation = explanations.get(root)
    if explanation:
        return f"`{cmd}` — {explanation}."

    # Generic explanation based on type
    type_explanations = {
        "vcs": "Handle version control operations",
        "js": "Work with the JavaScript/Node.js toolchain",
        "python": "Work with the Python toolchain",
        "rust_go": "Build or run a compiled-language project",
        "container": "Manage containers or orchestration",
        "build": "Build or compile the project",
        "network": "Perform a network operation",
        "navigation": "Navigate the filesystem",
        "edit_view": "View or edit file contents",
        "filesystem": "Manage files and directories",
        "package": "Install or manage system packages",
        "process": "Manage running processes",
        "shell_config": "Configure the shell environment",
        "search": "Search for content or files",
        "output": "Produce output",
        "test": "Run tests",
    }

    type_expl = type_explanations.get(cmd_type, "Perform a terminal operation")
    return f"`{cmd}` — {type_expl}."


def build_assistant_content(episode):
    """Build the assistant's response content from an episode's commands."""
    lines = ["Here's how to approach this:"]
    lines.append("")

    for cmd in episode:
        explanation = explain_command(cmd["command"].strip())
        lines.append(f"  $ {cmd['command'].strip()}")
        lines.append(f"  {explanation}")
        lines.append("")

    lines.append(
        "These commands work together to accomplish the task efficiently. "
        "Each step builds on the previous one, moving from setup through "
        "execution to verification."
    )

    return "\n".join(lines)


def episode_to_sft(episode):
    """Convert a problem-solving episode into an SFT entry."""
    user_content = infer_goal(episode)
    assistant_content = build_assistant_content(episode)

    return {
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_content},
            {"role": "assistant", "content": assistant_content},
        ],
        "source": "github-dotfiles",
        "tier": None,
    }


# ---------------------------------------------------------------------------
# File download
# ---------------------------------------------------------------------------

def download_history_file(repo_full, branch, file_path):
    """Download a history file's raw content via raw.githubusercontent.com."""
    url = f"{RAW_BASE}/{repo_full}/{branch}/{file_path}"
    r = gh_get(url)
    if r is None or r.status_code != 200:
        # Try with the blobs API as fallback
        return None
    return r.text


# ---------------------------------------------------------------------------
# Main pipeline
# ---------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(
        description="Scrape GitHub dotfiles shell history → SFT training data")
    ap.add_argument("--max", type=int, default=500,
                    help="Max history files to process (default: 500)")
    ap.add_argument("--min-followers", type=int, default=500,
                    help="Minimum follower count for repo owner (default: 500)")
    ap.add_argument("--out", default=OUTPUT_PATH,
                    help=f"Output JSONL path (default: {OUTPUT_PATH})")
    a = ap.parse_args()

    os.makedirs(os.path.dirname(a.out), exist_ok=True)

    print(f"=== GitHub Dotfiles Scraper ===", file=sys.stderr)
    print(f"  max files: {a.max}", file=sys.stderr)
    print(f"  min followers: {a.min_followers}", file=sys.stderr)
    print(f"  output: {a.out}", file=sys.stderr)
    print(file=sys.stderr)

    # ---------------------------------------------------------------
    # Step 1: Discover history files via code search API
    # ---------------------------------------------------------------
    print("[1/6] Searching for .bash_history / .zsh_history files via "
          "code search API...", file=sys.stderr)
    code_search_results = search_code_for_history_files(a.max)
    print(f"  Found {len(code_search_results)} files via code search",
          file=sys.stderr)

    # ---------------------------------------------------------------
    # Step 2: Fallback — discover via repo search + trees if needed
    # ---------------------------------------------------------------
    repo_results = []
    if len(code_search_results) < a.max:
        needed = a.max - len(code_search_results)
        print(f"[2/6] Code search returned {len(code_search_results)} files "
              f"(need {needed} more). Falling back to repo search + trees...",
              file=sys.stderr)
        repo_results = discover_via_repo_search(needed, a.min_followers)
        print(f"  Found {len(repo_results)} files via repo search",
              file=sys.stderr)
    else:
        print(f"[2/6] Code search sufficient, skipping repo search fallback",
              file=sys.stderr)

    # Merge results: (repo_full, file_path, owner, branch, stars)
    all_files = []
    seen = set()
    for item in code_search_results:
        repo_full, file_path, owner = item[0], item[1], item[2]
        key = (repo_full, file_path)
        if key not in seen:
            seen.add(key)
            all_files.append((repo_full, file_path, owner, "master", 0))
            # We don't know the branch from code search; will resolve later

    for item in repo_results:
        repo_full, file_path, owner, branch, stars = (
            item[0], item[1], item[2], item[3], item[4])
        key = (repo_full, file_path)
        if key not in seen:
            seen.add(key)
            all_files.append((repo_full, file_path, owner, branch, stars))

    print(f"\n  Total unique files discovered: {len(all_files)}", file=sys.stderr)

    if not all_files:
        print("\nNo history files found. Exiting.", file=sys.stderr)
        # Write empty file so downstream knows we ran
        open(a.out, "w").close()
        return

    # ---------------------------------------------------------------
    # Step 3: Filter by user followers
    # ---------------------------------------------------------------
    print(f"\n[3/6] Filtering repos by owner followers > {a.min_followers}...",
          file=sys.stderr)
    filtered_files = []
    checked_users = set()

    for repo_full, file_path, owner, branch, stars in all_files:
        if owner in checked_users:
            # Already checked this user — get from cache
            if passes_user_filter(owner, a.min_followers):
                filtered_files.append(
                    (repo_full, file_path, owner, branch, stars))
            continue
        checked_users.add(owner)

        if passes_user_filter(owner, a.min_followers):
            info = get_user_info(owner)
            print(f"  ✓ {owner} ({info['followers']} followers, "
                  f"{info['public_repos']} repos)", file=sys.stderr)
            filtered_files.append((repo_full, file_path, owner, branch, stars))
        else:
            info = get_user_info(owner)
            print(f"  ✗ {owner} ({info['followers']} followers — below "
                  f"threshold)", file=sys.stderr)

    print(f"\n  Files after user filter: {len(filtered_files)}", file=sys.stderr)

    if not filtered_files:
        print("No files passed user filter. Exiting.", file=sys.stderr)
        open(a.out, "w").close()
        return

    # ---------------------------------------------------------------
    # Step 4: Download and parse history files
    # ---------------------------------------------------------------
    print(f"\n[4/6] Downloading and parsing history files...", file=sys.stderr)
    parsed_files = []  # list of (commands, repo_full, file_path)

    for repo_full, file_path, owner, branch, stars in filtered_files:
        # Resolve branch if we don't know it
        if branch == "master" and stars == 0:
            # Came from code search — get repo info to find default branch
            r = gh_get(f"{API_BASE}/repos/{repo_full}")
            if r and r.status_code == 200:
                branch = r.json().get("default_branch", "master")

        content = download_history_file(repo_full, branch, file_path)
        if content is None:
            print(f"  ✗ {repo_full}/{file_path} — download failed",
                  file=sys.stderr)
            continue

        commands = parse_history_file(content, file_path)
        if len(commands) < MIN_FILE_COMMANDS:
            print(f"  ✗ {repo_full}/{file_path} — only {len(commands)} "
                  f"commands (min {MIN_FILE_COMMANDS})", file=sys.stderr)
            continue

        print(f"  ✓ {repo_full}/{file_path} — {len(commands)} commands",
              file=sys.stderr)
        parsed_files.append((commands, repo_full, file_path))

    print(f"\n  Parsed {len(parsed_files)} history files", file=sys.stderr)

    # ---------------------------------------------------------------
    # Step 5: Compute interesting ratio, cluster episodes, build SFT
    # ---------------------------------------------------------------
    print(f"\n[5/6] Computing interesting ratio and clustering episodes...",
          file=sys.stderr)
    sft_entries = []
    skipped_low_ratio = 0
    total_episodes = 0

    for commands, repo_full, file_path in parsed_files:
        ratio = compute_interesting_ratio(commands)
        if ratio < MIN_INTERESTING_RATIO:
            skipped_low_ratio += 1
            print(f"  ✗ {repo_full}/{file_path} — interesting ratio "
                  f"{ratio:.1%} < {MIN_INTERESTING_RATIO:.0%}",
                  file=sys.stderr)
            continue

        episodes = cluster_into_episodes(commands)
        print(f"  ✓ {repo_full}/{file_path} — ratio {ratio:.1%}, "
              f"{len(episodes)} episodes", file=sys.stderr)

        for episode in episodes:
            entry = episode_to_sft(episode)
            sft_entries.append(entry)
            total_episodes += 1

    print(f"\n  Skipped {skipped_low_ratio} files (low interesting ratio)",
          file=sys.stderr)
    print(f"  Generated {total_episodes} SFT entries from "
          f"{len(parsed_files) - skipped_low_ratio} files", file=sys.stderr)

    # ---------------------------------------------------------------
    # Step 6: Write JSONL
    # ---------------------------------------------------------------
    print(f"\n[6/6] Writing {len(sft_entries)} entries to {a.out}...",
          file=sys.stderr)
    with open(a.out, "w") as f:
        for entry in sft_entries:
            f.write(json.dumps(entry) + "\n")

    print(f"\n=== Done ===", file=sys.stderr)
    print(f"  Files discovered:    {len(all_files)}", file=sys.stderr)
    print(f"  Files after filter:  {len(filtered_files)}", file=sys.stderr)
    print(f"  Files parsed:        {len(parsed_files)}", file=sys.stderr)
    print(f"  Files skipped (low ratio): {skipped_low_ratio}", file=sys.stderr)
    print(f"  SFT entries written: {len(sft_entries)}", file=sys.stderr)
    print(f"  Output:              {a.out}", file=sys.stderr)


if __name__ == "__main__":
    main()
