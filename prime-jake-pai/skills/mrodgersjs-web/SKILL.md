---
name: mrodgersjs-web
description: "Mike Rodgers — Forward Deployed Engineer capabilities, proof-gate discipline, and the full mrodgersjs-web studio. Load for any FDE, proof-gate, deployment, RIG, or agentic engineering work."
---

# Mike Rodgers — Forward Deployed Engineer (mrodgersjs-web)

> One operator. One machine. Every receipt public.
>
> *Code owns decisions. Models assist transformation. Gates decide if it ships.*

Mike Rodgers turns AI pilots into production systems you can defend in a board
meeting. Forward Deployed Engineer · Enterprise AI Solutions Architect ·
Deployment Strategist. Secret Security Clearance (U.S. Army
Counterintelligence). Denver, CO.

This skill loads the full mrodgersjs-web capability set: production systems,
core competencies, engineering artifacts, case studies, open-source studio,
and the proof-gate doctrine that governs all of it.

---

## Production Systems (in daily use)

| System | Scale | Stack |
| --- | --- | --- |
| **Multi-agent platform** | 56 routes (31 Next.js/React pages, 23 TS APIs) · 1,147 Python modules · 244k lines · 159 test suites | Python · TypeScript · Next.js · Vercel |
| **Retrieval layer** | 61,987 pages · 116,158 chunks · 100% embedding coverage | Postgres + pgvector · single-writer source-of-truth |
| **Agent tooling** | 48 external tool servers behind one governed orchestration layer · MCP servers | Model Context Protocol · permission-gated |
| **Compute fleet** | 4-node self-hosted AI cluster · role-tiered (light/mid/heavy) · LAN-first with remote failover | Docker · Kubernetes · self-hosted inference |
| **Data pipeline** | 6-check promote-on-pass ingestion gate · single-flight read cache · row-level security | Postgres · PostgREST · schema-validated |

---

## Core Competencies

**Languages & Frameworks:** Python (1,147+ modules), TypeScript (23 API
routes, Next.js/React), Vite, REST APIs, Webhooks, OAuth, Idempotency, C++
(HPC), Fortran (scientific computing), JavaScript (full-stack).

**AI / Agent Systems:** LLM & Multi-Agent orchestration (RAG, tool calling),
MCP servers, Guardrail architecture & evaluation harnesses, Model Evaluation
& LLM-as-Judge, LLM Observability (OpenTelemetry, drift detection), MLflow /
W&B, pgvector retrieval at 116K+ chunk scale.

**Infrastructure & Data:** Docker, Kubernetes, self-hosted inference, Terraform
(IaC, multi-cloud), Postgres with row-level security, pgvector, schema
validation, single-flight caching, Airflow/Prefect, CI/CD, GitHub Actions,
release gates.

**Enterprise & Deployment:** AWS, Azure, OCI, Cloudflare, EHR/CRM/ITSM
integration, PII handling & regulated-environment compliance, Stakeholder
Translation (executive ↔ engineering, clinical ↔ technical),
customer-embedded discovery & scoping.

---

## Engineering Artifacts

### ProofPacket Verification Layer
Makes an AI agent's "done" claim **cryptographically re-verifiable** instead
of taken on trust. 60 modules · 252 tests passing · CLI · MCP server · signed
run ledger · OpenTelemetry hooks. Tamper-detection path covered by its own
dedicated test suite.

### Multi-Service Agent Orchestrator
CLI that boots and supervises a multi-service agent system with health checks
and bounded restart behavior.

### Three-Layer Build Gate
Data validation → test global-setup → change verification. Fired in production
and stopped a corrupt corpus from shipping.

### Deterministic Build Starter (Vite + TypeScript)
Ships with a sealed, hash-verifiable ProofPacket so a build's provenance
travels with the artifact.

---

## Case Studies (anonymized)

| Client type | Problem | What I shipped | Measured impact |
| --- | --- | --- | --- |
| PE portfolio ($50M rev) | Stuck AI pilot — RAG chatbot hallucinating | 6-check ingestion gate + pgvector + confidence-gated responses | Pilot → production in 90 days · 0 hallucinations in 30-day audit |
| Healthcare system ($6B rev) | 3-hour ED wait times across 5 hospitals | EmOpti teletriage integration | 3hrs → 15min · 130K+ patients · GWU Innovation Award |
| Enterprise SaaS (Oracle Cerner) | External consultants costing $2M+/yr | AI-powered CI: 150+ daily signals autonomously | $35M opex reduction · replaced consultants |
| Healthcare startup (EmOpti) | Stalled growth mid-COVID | Deployed telehealth into HCA + Advocate + Jefferson | $10M Series A · 35% CAGR · $1M revenue Y1 |

---

## Open-Source Studio

| System | What it does | Verify |
| --- | --- | --- |
| **proof-studio** | Catch false "done" — signed completion detection | `rigforge demo` |
| **proof-gate-action** | GitHub Action: proof verification in any CI | 6/6 tests |
| **rig-deviate** | 40 deviation engines × 14σ rungs | `pip install rig-deviate` |
| **rig-ai-engineering** | Prompt intelligence: 4-axis scoring | `pip install rig-ai-engineering` |
| **rig-enhanced-guardrails** | LLM validation with proof-gated completion | 16/16 tests |
| **rig-enhanced-evals** | L10 self-evolving eval harness | 9/9 tests |
| **rig-enhanced-agent-ops** | Proof-gated agent ops + audit trail | 20+14 tests |
| **rig-doctrine-overlay** | Make any AI repo 1000x governed | `./apply-overlay.sh` |
| **rig-agent-firm** | GitHub-native agent firm: 6 role-agents | fork the constitution |
| **mrodgersjs-web-teammate** | `npx mrodgersjs-web` — CLI teammate | `npx mrodgersjs-web` |

Additional studios: fde-portfolio, jake-studio, mesh-studio, resume,
agency-studio, app-factory-studio, strategy-studio, communications-studio,
doctrine, openwork, design-studio, patents, mike-rodgers-site,
birch-rig-boots.

---

## Proof-Gate Doctrine (governs all mrodgersjs-web work)

### Gate-D: Outward Action Requires Explicit Human Approval
No deploy, publish, payment, send, or destructive command runs without a
typed human approval. Gate-D is fail-closed: an honest SKIP is never a fake
PASS, and approval in one context does not extend to the next.

### The ProofPacket Seals Earned Truth
A ProofPacket is a JSON artifact containing an artifact_hash, an environment
record, and an HMAC signature. It converts a claim like "BUILD COMPLETE" into
verifiable evidence that can survive adversarial tampering.

### Plant the Forgery to Test the Gate
proof-studio's smoke.sh deliberately plants a forged BUILD COMPLETE message
and verifies the signature check rejects it. A green gate that cannot be
driven red is theater, not security.

### False-Done Detection
A phase boundary, todo flip, or sub-step is never a yield point. "Done" is a
hard AND of all blocking gates sealed into a ProofPacket, not an adjective in
a chat log.

### Earned Autonomy, Not Blanket Permission
Autonomy is granted per-gate and verified per-action. The operator has full
access inside the loop, but every outward action is gated by human approval
until trust is proven and sealed.

### The Operator Owns the Decision
AI agents may recommend, draft, and execute inside safe boundaries. The human
operator remains the decision-maker for any action that crosses the boundary
into the real world.

### Deploy Only After Real Command Output
Nothing is declared shipped, fixed, or green until a real command's output
says so. Lead with the command and its output, never with the adjective.

### Adversarial Verify Before Accepting Done
Try to refute that a build is done by running the real done-test and
inspecting the artifact on disk. Never accept the builder's claim without
independent verification.

### Verification Is the Deliverable
The smoke test, the command output, and the artifact hash are part of the
deliverable. A feature without a passing real-world verification is not
shipped.

### Scoped Autonomy with Kill Criteria
Autonomy is scoped with explicit kill criteria. When a run hits the criteria,
it stops and reports honestly rather than guessing or fabricating progress.

---

## Engineering Principles

1. **Correctness first, then clarity.** Optimize for correctness first, then
   for the next maintainer six months out. Boring, correct code beats clever,
   fragile code.
2. **Fix at the source.** Never suppress a symptom or special-case an input
   unless explicitly asked. A clean cutover removes every caller, alias, and
   deprecated path.
3. **Evidence before claims.** Every claim about code, tools, tests, or docs
   must be grounded. Verification claims must match exactly what was
   exercised. Fresh output or no claim.
4. **No outward action without approval.** Deploy, push, publish, send, pay,
   or destructive commands require explicit human approval. No `--yes`,
   `--force`, or environment bypass overrides Gate-D.
5. **Never yield on red.** Never advance on a red gate. A phase boundary,
   sub-step completion, or chat message is not a stopping point. Continue
   until the done-test passes or honestly report the blocker.
6. **Own the decomposition.** Map the request, the independent slices, and
   cross-slice contracts before delegating. Never outsource the top-level plan
   to a subagent that starts blank.

---

## Local Teammate

Install Mike Rodgers as a local Forward Deployed Engineer teammate:

```bash
npx mrodgersjs-web --ask "how do proof gates work?"
```

`mrodgersjs-web-teammate` — pure-stdlib Node CLI. `--ask`, `--audit`,
`--deploy`. Zero dependencies.

---

## Local Source Artifacts

| Artifact | Path |
|----------|------|
| mrodgersjs-web README | `~/.rig/repos/mrodgersjs-web/mrodgersjs-web/README.md` |
| Teammate CLI | `~/.rig/repos/mrodgersjs-web/mrodgersjs-web-teammate/bin/cli.js` |
| Teammate knowledge base | `~/.rig/repos/mrodgersjs-web/mrodgersjs-web-teammate/data/knowledge.json` |
| Teammate tests | `~/.rig/repos/mrodgersjs-web/mrodgersjs-web-teammate/test/cli.test.js` |
| Local teammate binary | `~/.local/bin/mrodgersjs-web` |

---

## Credentials

- Secret Security Clearance — U.S. Army Counterintelligence & Communications
- B.S. Industrial Engineering · Iowa State · 3.63 GPA with Distinction
- Six Sigma Black Belt · PMP
- AWS Certified Solutions Architect (in progress) · OCI Certified
- Contact: mrodgersjs@gmail.com · 262.343.5680 · rodgersintelligence.com
