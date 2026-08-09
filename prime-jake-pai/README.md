# Prime Jake PAI — 1000x Autonomous Operating System

Prime Jake PAI is Mike Rodgers's autonomous operating system running on Prime
Agent. It commands all 7 RIG fleet nodes, manages every running program,
terminal, and coding session, and operates with full Jake PAI + TAC +
mrodgersjs-web doctrine always active.

## What's Here

```
prime-jake-pai/
├── prime-jake-pai-master.md     # 1000x enhanced master doctrine
├── AGENTS.md                    # Prime Agent global context file
├── config/
│   ├── fleet-inventory.json     # All 7 fleet nodes + QNAP
│   ├── prime-agent-settings.json # Prime Agent config (model, skills)
│   ├── prime-agent-watchdog.service  # systemd service
│   └── prime-agent-watchdog.timer    # systemd timer (30s interval)
├── scripts/
│   ├── prime-jake-fleet.py      # Fleet control plane (all nodes)
│   ├── prime-jake-controller.py # Program/terminal/session controller
│   └── prime-agent-watchdog.sh  # Daemon watchdog script
└── skills/
    ├── jake-pai-doctrine/       # Full Jake PAI doctrine
    ├── mrodgersjs-web/          # FDE capabilities + proof-gate doctrine
    └── prime-jake-pai/          # Fleet control skill
```

## Fleet Nodes

| Node | Role | RAM | Status |
|------|------|-----|--------|
| blackwell ★ | Frontier GPU | 125GB | ONLINE |
| rig-96gb | Orchestrator | 96GB | ONLINE |
| rig-256gb | Heavy workers | 256GB | ONLINE |
| rig-36gb | Workers | 36GB | ONLINE |
| rig-128gb-mbp | Gateway | 128GB | ONLINE |
| rig-48gb | Workers | 48GB | OFFLINE |
| rig-28gb | Auditor | 28GB | OFFLINE |
| rig-qnap | Storage | — | ONLINE_LAN |

★ = local node where Prime Jake runs

## Installation

```bash
# Copy skills to Prime Agent
mkdir -p ~/.prime/agent/skills
cp -r skills/* ~/.prime/agent/skills/

# Copy config
cp config/prime-agent-settings.json ~/.prime/agent/settings.json
cp AGENTS.md ~/.prime/agent/AGENTS.md
cp config/fleet-inventory.json ~/.rig/mesh/fleet-inventory.json

# Copy scripts
cp scripts/prime-jake-fleet.py ~/.rig/scripts/
cp scripts/prime-jake-controller.py ~/.rig/scripts/
cp scripts/prime-agent-watchdog.sh ~/.rig/scripts/
chmod +x ~/.rig/scripts/prime-jake-*.py ~/.rig/scripts/prime-agent-watchdog.sh

# Copy master doctrine
cp prime-jake-pai-master.md ~/.rig/

# Install systemd watchdog
cp config/prime-agent-watchdog.service ~/.config/systemd/user/
cp config/prime-agent-watchdog.timer ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable prime-agent-watchdog.timer prime-agent-watchdog.service
systemctl --user start prime-agent-watchdog.timer
```

## Usage

```bash
# Fleet health
python3 ~/.rig/scripts/prime-jake-fleet.py status

# Run on all nodes
python3 ~/.rig/scripts/prime-jake-fleet.py exec "uptime"

# Full system health
python3 ~/.rig/scripts/prime-jake-controller.py health

# Prime Agent (always running via watchdog)
prime-agent -p "your question"
```

## Doctrine

- **Gate-D**: No outward action without typed human approval. Always armed.
- **AntiGenericForce ≥ 80**: No generic output. Mechanism or nothing.
- **TAC**: Build the system that builds the system. Stack leverage points.
- **Mike-Protection**: Focus, fitness, James, $10M ARR, decision fatigue.
- **Proof-First**: No proof, no completion.

## Version

v3.0 / 1000x — 2026-08-08
