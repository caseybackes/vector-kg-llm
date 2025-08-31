# vector-kg-llm

Single-tenant, learnable knowledge-graph stack in Docker Compose.
LLM (LM Studio) proposes claims → policy gate → human review → Neo4j facts.
Graph for structure (Neo4j). Vectors/snippets for text search/dedup (Postgres+pgvector).
All FastAPI microservices. No multi-tenant fluff. Air-gappable.

## Why

Keep working/personal/org knowledge in a graph with provenance (who/when/source).

Let the model help, without letting it scribble over truth.
Answer with approved facts + citations; queue low-trust stuff for review.

## Services

- Neo4j (graph) — entities, claims, evidence, approved edges.
- Postgres + pgvector (vectors) — evidence/snippet store (embeddings later).
- kg-api (FastAPI) — /cypher, /neighbors, /propose_claim, /approve, /reject, /gaps.
- agent-gateway (FastAPI) — policy gate; decides T1 auto-merge vs T2 review; /propose_claim, /llm_chat.
- scheduler (FastAPI, optional) — background gap scans; surfaces missing/ stale facts as pending claims.

### Diagrams (Mermaid)

- 01-topology.md (stack)
- 02-domainschema.md (entities/claims/evidence)
- 03-writepath.md (propose → gate → materialize)
- 04-readpath.md (query → fuse → answer)
- 05-policy.md (T0/T1/T2)
- 06-research.md (optional background loop)

## quickstart  
0) Start LM Studio locally → Start Server on :1234 (OpenAI compat), set API key "lm-studio"

1) Bring up core stack
docker compose up -d neo4j postgres kg-api agent-gateway

2) (optional) scheduler
docker compose up -d scheduler

3) Health
curl -s http://localhost:8000/health | jq .   # kg-api
curl -s http://localhost:7000/health | jq .   # agent-gateway

4) Neo4j sanity
curl -s -X POST http://localhost:8000/cypher \
  -H 'content-type: application/json' -d '{"query":"RETURN 1 AS ok"}' | jq .

5) LM Studio connectivity (host)
curl -s -H 'Authorization: Bearer lm-studio' http://localhost:1234/v1/models | jq .
(from container)
docker compose exec agent-gateway bash -lc \
  "curl -s -H 'Authorization: Bearer lm-studio' http://host.docker.internal:1234/v1/models | jq ."

6) First claim (auto-merge if first-party, high trust, no conflict)
curl -s http://localhost:7000/propose_claim -H 'content-type: application/json' -d '{
  "subject_id":"Run:demo","predicate":"USES","object_kind":"entity","object_value":"Model:v1",
  "model_conf":0.95,
  "evidence":[{"uri_or_blob_ref":"log://run/demo","source_type":"first_party_log","quality_score":0.95}]
}' | jq .

## Policy (defaults)

- Auto-merge (T1) when: predicate ∈ {USES,INGESTS,PRODUCES} and object_kind=entity and trust ≥ 0.85 and evidence.quality ≥ 0.7 and no conflicts.
- Everything else → T2 review (status=pending).
- Trust heuristic = evidence quality + model confidence + first-party bonus. Tunable via env.

## Data model (essentials)

- Entity: {id, type, name, ...}
- Claim: {subject_id, predicate, object_kind, object_value, status, model_conf, human_conf, context_hash, provenance...}
- Evidence: {uri_or_blob_ref, snippet, hash, source_type, quality_score, timestamp}
- Approved claims materialize as edges between Entities; others live in the claim ledger.

## Configuration (env of interest)

- LLM_BASE_URL / LLM_API_KEY → LM Studio endpoint (OpenAI-compatible).
- NEO4J_URI/USER/PASSWORD
- PG_* or PG_DSN
- Policy knobs (gateway): TIER_AUTO_TRUST_THRESHOLD, TIER_MIN_EVIDENCE_QUALITY, AUTO_MERGE_PREDICATES.

## Ops notes

- Neo4j APOC enabled for UUID/relationship helpers.
- Startup creates unique constraints on Entity.id, Claim.id, Evidence.id.
- Volumes: neo4j_data, neo4j_logs, pg_data.
- Linux host: host.docker.internal is mapped via host-gateway.

## Non-goals

- Not multi-tenant. Not a crawler. No automatic writes from web sources.
- No background writes without provenance. Human stays in the loop.