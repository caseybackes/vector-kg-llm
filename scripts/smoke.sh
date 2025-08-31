# OPTIONAL: quick smoke test (save as scripts/smoke.sh, run after 'docker compose up')
set -euo pipefail

# 1) create two entities
curl -s localhost:8000/cypher -X POST -H 'content-type: application/json' \
  -d '{"query":"MERGE (:Entity {id:$a}) MERGE (:Entity {id:$b}) RETURN 1","params":{"a":"Run:demo","b":"Model:v1"}}' >/dev/null

# 2) propose a first-party, high-trust claim (EXPECT auto-merge)
curl -s localhost:7000/propose_claim -X POST -H 'content-type: application/json' -d '{
  "subject_id":"Run:demo","predicate":"USES","object_kind":"entity","object_value":"Model:v1",
  "model_conf":0.95,
  "evidence":[{"uri_or_blob_ref":"log://run/demo","source_type":"first_party_log","quality_score":0.95}]
}' | jq .
