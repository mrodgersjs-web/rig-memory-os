# Founder Console

A thin, local-first, read-only dashboard. Single HTML file, no build step.

## Run

```bash
uv run python -m http.server 8089 --bind 127.0.0.1
open http://127.0.0.1:8089/dashboard/index.html
```

## Views

1. **Today** — Jake's three priorities + decisions awaiting Mike + morning brief.
2. **Opportunity portfolio** — stage, scores, evidence freshness, next action.
3. **Fleet** — node status, heartbeat age, current work, concurrency.
4. **Work queue** — ready / leased / blocked / reopened / dead-lettered / completed.
5. **Evidence** — sources, artifacts, ProofPackets, verification verdicts.
6. **Learning** — what changed, killed assumptions, new reusable assets.

## Operator actions (Phase 5+)

- approve/reject a prepared outward action
- promote/hold/kill an opportunity
- change a priority
- pause a work type
- drain a node
- retry or reopen a failed mission
- inspect full evidence

The console reads and writes through the same durable database. It does not
maintain a second state system.