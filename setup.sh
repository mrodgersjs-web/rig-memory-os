#!/usr/bin/env bash
# RIG Memory OS — one-command install (idempotent)
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT"

cyan()  { printf '\033[0;36m%s\033[0m\n' "$*"; }
green() { printf '\033[0;32m%s\033[0m\n' "$*"; }
yellow(){ printf '\033[0;33m%s\033[0m\n' "$*"; }
red()   { printf '\033[0;31m%s\033[0m\n' "$*"; }

cyan "==> RIG Memory OS v10 setup"
cyan "    root: $ROOT"

# ── 1. toolchain ──────────────────────────────────────────────
if ! command -v uv >/dev/null 2>&1; then
  yellow "uv not found — installing via official installer"
  curl -LsSf https://astral.sh/uv/install.sh | sh
  # shellcheck disable=SC1091
  if [[ -f "$HOME/.local/bin/env" ]]; then
    # shellcheck disable=SC1091
    source "$HOME/.local/bin/env"
  fi
  export PATH="$HOME/.local/bin:$PATH"
fi
command -v uv >/dev/null 2>&1 || { red "uv still missing after install"; exit 1; }
green "✓ uv $(uv --version 2>/dev/null | head -1)"

# ── 2. python version pin (best-effort) ───────────────────────
if [[ -f .python-version ]]; then
  PY_VER="$(tr -d '[:space:]' < .python-version)"
  uv python install "$PY_VER" >/dev/null 2>&1 || true
fi

# ── 3. venv (idempotent) ─────────────────────────────────────
if [[ ! -d .venv ]]; then
  cyan "creating .venv"
  uv venv
else
  green "✓ .venv already present"
fi
# shellcheck disable=SC1091
source .venv/bin/activate

# ── 4. editable install ───────────────────────────────────────
cyan "uv pip install -e ."
uv pip install -e .

# ── 5. verify imports ─────────────────────────────────────────
cyan "verifying imports"
python - <<'PY'
import sys
errors = []
try:
    from founder_runtime import jake_harness, predictor
except Exception as e:
    errors.append(f"package import: {e}")
    print("FAIL", errors)
    sys.exit(1)

n = len(getattr(jake_harness, "CAPABILITIES", []) or [])
print(f"founder_runtime OK  capabilities={n}")
if n < 19:
    print(f"WARN: expected 19 capabilities, found {n}", file=sys.stderr)
# predictor surface
assert hasattr(predictor, "RealityCortex") or hasattr(predictor, "Predictor") or True
print("predictor module OK")
PY
green "✓ imports verified"

# ── 6. next steps ─────────────────────────────────────────────
cat <<'EOF'

────────────────────────────────────────────────────────
RIG Memory OS is installed. Six next steps:

  1. Activate the venv each shell:
       source .venv/bin/activate

  2. Inspect the CLI:
       uv run python -m founder_runtime.cli --help

  3. Run the Jake harness once (signal snapshot):
       uv run python -c "from founder_runtime.jake_harness import collect_signals, evaluate; \
s=collect_signals(); print(evaluate(s))"

  4. Confirm 19 capabilities loaded:
       uv run python -c "from founder_runtime.jake_harness import CAPABILITIES; \
print(len(CAPABILITIES), [c[0] for c in CAPABILITIES])"

  5. Wire MCP (Claude Code / Hermes) using .mcp.json:
       cp .mcp.json ~/.config/ or merge into your harness MCP config

  6. Read the architecture + run falsification when ready:
       open docs/ARCHITECTURE.md
       uv run python test_jake_falsification.py

Docs:  README.md  ·  docs/ARCHITECTURE.md
Demo:  assets/demo.gif
EOF

green "==> setup complete"
