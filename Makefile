# Makefile
# "Home prod" shortcuts: one command per task; minimal deps; no curl needed.
# Usage:
#   cp .env.example .env  # edit secrets
#   make up               # bring stack up
#   make smoke            # end-to-end sanity
#   make backup           # dump neo4j + postgres
#   make restore          # restore from latest dumps
#   make logs             # follow logs
#   make down             # stop stack

SHELL := /bin/bash
.ONESHELL:

export $(shell sed 's/=.*//' .env 2>/dev/null)

up:
	docker compose up -d --remove-orphans

down:
	docker compose down

restart:
	docker compose restart agent-gateway kg-api

logs:
	docker compose logs -f agent-gateway kg-api neo4j postgres

smoke:
	# 1) gateway health
	curl -fsS http://localhost:7000/health | jq .
	# 2) LM Studio connectivity (from inside container via Python stdlib)
	docker compose exec -T agent-gateway python - <<'PY'
import os, json, urllib.request
base=os.environ["LLM_BASE_URL"].rstrip("/")
req=urllib.request.Request(base+"/models",headers={"Authorization":"Bearer "+os.environ["LLM_API_KEY"]})
print(json.dumps(json.loads(urllib.request.urlopen(req, timeout=10).read().decode()), indent=2))
PY
	# 3) seed entities
	curl -fsS localhost:8000/cypher -H 'content-type: application/json' -d '{"query":"MERGE (:Entity {id:$a}) MERGE (:Entity {id:$b}) RETURN 1","params":{"a":"Run:demo","b":"Model:v2"}}' | jq .
	# 4) write (auto-merge)
	curl -fsS http://localhost:7000/propose_claim -H 'content-type: application/json' -H "X-API-Key: $$GATEWAY_API_KEY" -d '{
	  "subject_id":"Run:demo","predicate":"USES","object_kind":"entity","object_value":"Model:v2",
	  "model_conf":0.9,
	  "evidence":[{"uri_or_blob_ref":"log://run/demo","source_type":"first_party_log","quality_score":0.95}]
	}' | jq .
	# 5) neighbors
	curl -fsS localhost:8000/neighbors -H 'content-type: application/json' -d '{"id":"Run:demo","depth":1,"limit":50}' | jq .

backup:
	# Postgres (pg_dump) and Neo4j (online copy)
	mkdir -p backups
	docker compose exec -T postgres pg_dump -U $$POSTGRES_USER -d $$POSTGRES_DB > backups/pg_`date +%F_%H%M%S`.sql
	# Neo4j: use cypher-shell for export (CSV) – simple & portable at home
	docker compose exec -T neo4j bash -lc 'cypher-shell -u neo4j -p $$NEO4J_PASSWORD "CALL apoc.export.csv.all(\"/data/export_`date +%F_%H%M%S`.csv\",{})" && ls -1 /data | tail -n 1'

restore:
	# Restore Postgres from newest dump
	ls -1 backups/pg_*.sql | tail -n 1 | xargs -I{} bash -lc 'cat {} | docker compose exec -T postgres psql -U $$POSTGRES_USER -d $$POSTGRES_DB'
	@echo "Neo4j restore: import CSV from /data via browser or custom script (varies by dump)."

.PHONY: up down restart logs smoke backup restore
