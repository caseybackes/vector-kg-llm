# Future Work — Learnable KG (Auto-Merge, No-Review) Plan

> Scope: Move the stack to **default auto-merge** with guardrails, while keeping explainability and fast rollback. No architecture changes required.

## 1) Goals & Non-Goals
- **Goals**
  - The model can **read from** and **write to** the KG without human confirmation by default.
  - Writes are **safe-by-design** via predicate-level policy, provenance, and conflict handling.
  - Everything is **explainable** and **reversible** (audit + undo).
- **Non-Goals**
  - Multi-tenant SaaS hardening.
  - Full-blown ontology management; we keep a light predicate allowlist.
  - Replacing LM Studio; it remains the local LLM backend.

## 2) UX Commitments
- Single entrypoint: `victor "..."` (CLI or minimal chat UI).
- Answers always check KG for entity/relationship queries.
- Writes happen automatically **only** when policy allows; no prompts unless requested.
- Quick commands:
  - `victor why "..."` — show paths/evidence + policy decision.
  - `victor what-changed --since 2h` — recent commits/diffs.
  - `victor undo <commit-id|last:N>` — roll back quickly.
  - Session modes: `--mode dry-run|auto|paranoid` (default **auto**).

## 3) Policy (Predicate- and Source-Aware)
- **Cardinality** per predicate: `functional` vs `set`.
- **Trust threshold** `T` per predicate and **source tier**.
- **Overwrite strategy** for functional predicates: `supersede | coexist | forbid`.
- **Source tiers**
  - Tier A (auto): `first_party_log`, `config`, `run_artifact` (+bonus).
  - Tier B (stricter T): `internal_doc`, `web` (domain allowlist + hash).
  - Tier C (never overwrite): `llm_self` (optional TTL/Shadow).

### Proposed defaults (can be tuned)
| Predicate        | Cardinality | Auto? | T (A/B) | Overwrite           |
|------------------|-------------|-------|---------|---------------------|
| `USES`           | set         | Yes   | .80/.90 | N/A                 |
| `INGESTS`        | set         | Yes   | .80/.90 | N/A                 |
| `PRODUCES`       | set         | Yes   | .85/.92 | N/A                 |
| `MENTIONS`       | set         | Yes   | .70/.85 | N/A                 |
| `FIXED_BY`       | set         | Yes   | .85/.92 | N/A                 |
| `ORIGINATES_AT`  | functional  | Yes   | .90/.95 | **supersede**       |
| `VERSION_OF`     | functional  | Yes   | .90/.95 | **supersede**       |

**Trust calculation (current):**
```
trust = clamp( 0.5*evidence_quality + 0.4*model_conf + (0.15 if all evidence is first-party else 0.0) )
```

## 4) Policy Config (file-based, reloadable)
Create `policy.yaml` (read by agent-gateway on startup; hot-reload optional):
```yaml
mode: auto   # auto | review | dry-run | shadow
predicates:
  USES:          {cardinality: set,        threshold: {A: 0.80, B: 0.90}, overwrite: coexist}
  INGESTS:       {cardinality: set,        threshold: {A: 0.80, B: 0.90}, overwrite: coexist}
  PRODUCES:      {cardinality: set,        threshold: {A: 0.85, B: 0.92}, overwrite: coexist}
  MENTIONS:      {cardinality: set,        threshold: {A: 0.70, B: 0.85}, overwrite: coexist}
  FIXED_BY:      {cardinality: set,        threshold: {A: 0.85, B: 0.92}, overwrite: coexist}
  ORIGINATES_AT: {cardinality: functional, threshold: {A: 0.90, B: 0.95}, overwrite: supersede}
  VERSION_OF:    {cardinality: functional, threshold: {A: 0.90, B: 0.95}, overwrite: supersede}
sources:
  first_party_log: {tier: A, bonus: 0.15, rate_per_min: 200}
  config:          {tier: A, bonus: 0.15, rate_per_min: 50}
  run_artifact:    {tier: A, bonus: 0.10, rate_per_min: 100}
  internal_doc:    {tier: B, bonus: 0.05, rate_per_min: 30, allow_domains: ["wiki.local"]}
  web:             {tier: B, bonus: 0.00, rate_per_min: 20, allow_domains: ["docs.vendor.com"]}
  llm_self:        {tier: C, bonus: 0.00, rate_per_min: 10, ttl_days: 14}
shadow:
  enabled: false
  label: Shadow
  promote_after_min: 0
limits:
  per_session_edges: 200
```

## 5) Gateway Changes (small)
- Read `policy.yaml`; cache per predicate/source settings.
- On `/propose_claim`:
  - Compute trust; map evidence sources → tier; pick threshold.
  - **Set-valued**: add edge if trust ≥ T.
  - **Functional**: if trust ≥ T and (new evidence fresher & higher trust than incumbent) → **supersede**; else set `conflict=true` and keep both.
  - If `mode=shadow`, write with `label=Shadow` (or `shadow=true`) and schedule promotion.
  - Always write with a `commit_id` (UUID) and `policy_decision` details.
- Add endpoints:
  - `POST /undo {commit_id}|{last:n}` — transactional rollback.
  - `GET /changes?since=ISO8601` — list commits with brief diffs.
  - `POST /promote_shadow?older_than=mins` — adopt shadow edges.

## 6) Data Model Additions
- Claim fields: `commit_id`, `policy_decision`, `conflict:bool`, `shadow:bool`, `valid_from:epoch`, `valid_to:epoch?`.
- For functional predicates, store incumbent’s `valid_to` when superseded.
- Consider relationship properties (on the edge) for `when`, `source_tier`, `commit_id`.

## 7) Scheduler Jobs
- **Corroboration**: find low-trust claims; if corroborated later, lift `conflict` or promote from shadow.
- **TTL cleanup**: expire Tier C (`llm_self`) or shadowed claims after `ttl_days`.
- **Dedup sweeps**: vector similarity → candidate merges (optional, still auto-add only if policy permits).
- **Freshness rebake**: re-embed/react on changed nodes.

## 8) Observability & Audit
- Emit structured logs: `{commit_id, subject, predicate, object, trust, tier, decision, conflict}`.
- Metrics: tool-call counts, decision rates, write rates per source, undo counts.
- `victor` subcommands:
  - `victor what-changed --since 2h`
  - `victor undo --last 1` or `victor undo --commit <uuid>`
  - `victor why "question"` — show paths + policy math.

## 9) CLI Additions (victor)
- `--mode` flag to set session behavior.
- `victor set mode auto|review|dry-run|shadow` — calls gateway to flip mode atomically.
- `victor policy show` — dumps effective policy as the gateway sees it.

## 10) MCP Adapter (optional, later)
- Implement an MCP server that exposes **the same tools**:
  - `neighbors(id, depth, limit)`
  - `cypher_readonly(query, params)`
  - `propose_claim(claim)`
  - `undo(commit_id|last:n)`
  - `changes(since)`
- No backend changes; adapter calls existing endpoints.

## 11) Testing & Acceptance
- Unit tests: trust calc, threshold routing, supersede logic, undo.
- Integration: end-to-end propose→materialize; conflict and supersede cases.
- Property-based tests on policy thresholds (fuzz evidence/tiers).
- Acceptance Criteria:
  - Fresh Tier A evidence for functional predicate **supersedes** older, lower-trust fact.
  - Conflicting facts **coexist** when trust is below overwrite threshold.
  - `undo` restores prior state exactly (idempotent).

## 12) Rollout Plan
1. Land `policy.yaml` + gateway reader (no behavior change; keep current defaults).
2. Add `commit_id`, `policy_decision` fields; store on writes.
3. Implement `undo` and `changes` (read-only until verified).
4. Flip `mode=auto`; monitor metrics; guard with per-session limits.
5. Optional: enable shadow for Tier C and/or web sources.

## 13) Risks & Mitigations
- **Model hallucination** → Tier C never overwrites; TTL; low write rate; shadow by default if desired.
- **Conflicting facts** → functional supersede rules; else mark conflict; answers disclose ambiguity.
- **Runaway writes** → rate limits, per-session caps, and commit-scoped undo.
- **Schema drift** → predicate allowlist; reject unknown rels at gateway.

---

### Status to Implement
- [ ] `policy.yaml` parser & hot-reload
- [ ] Per-predicate/source thresholds & cardinality enforcement
- [ ] Supersede logic (functional) + `valid_to`
- [ ] `commit_id` plumbing and `policy_decision` summaries
- [ ] `/undo`, `/changes`, `/promote_shadow`
- [ ] `victor` commands for mode/undo/changes/why
- [ ] Scheduler jobs: corroborate, TTL, dedup, freshness
- [ ] Metrics & structured logs

