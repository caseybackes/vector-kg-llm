# services/kg-api/app.py
# Minimal KG-API (FastAPI) for: /health, /cypher, /neighbors, /propose_claim, /approve, /reject, /gaps
# - Neo4j stores entities/claims/edges
# - Postgres stores evidence snippets (no embeddings yet; add later)
# - Keep endpoints deterministic; policy/auto-merge lives in agent-gateway
#
# ENV:
#   NEO4J_URI, NEO4J_USER, NEO4J_PASSWORD
#   PG_HOST, PG_PORT, PG_USER, PG_PASSWORD, PG_DB
#   (optional) PG_DSN
#
# RUN (inside container):
#   uvicorn app:app --host 0.0.0.0 --port 8000
from __future__ import annotations

import os
import time
from typing import Any, Dict, List, Optional, Tuple

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field
from neo4j import GraphDatabase, Driver
import psycopg
from psycopg import sql
from neo4j.graph import Node, Relationship, Path  # at top with other imports

# ------------------------
# Config / Connections
# ------------------------

NEO4J_URI = os.getenv("NEO4J_URI", "bolt://neo4j:7687")
NEO4J_USER = os.getenv("NEO4J_USER", "neo4j")
NEO4J_PASSWORD = os.getenv("NEO4J_PASSWORD", "neo4j_password")

PG_DSN = os.getenv(
    "PG_DSN",
    f"postgresql://{os.getenv('PG_USER','kg_user')}:{os.getenv('PG_PASSWORD','kg_password')}"
    f"@{os.getenv('PG_HOST','postgres')}:{os.getenv('PG_PORT','5432')}/{os.getenv('PG_DB','kg_db')}"
)

driver: Driver | None = None
pg_conn: psycopg.Connection | None = None

app = FastAPI(title="KG-API", version="0.1.0")


# ------------------------
# Models
# ------------------------

class EvidenceModel(BaseModel):
    uri_or_blob_ref: str
    snippet: Optional[str] = None
    hash: Optional[str] = None
    source_type: str = Field(description="e.g., first_party_log|config|run_artifact|internal_doc|web|llm_self")
    quality_score: Optional[float] = None
    timestamp: Optional[float] = None  # epoch seconds


class ProvenanceModel(BaseModel):
    who: Optional[str] = None
    when: Optional[float] = None
    prompt_hash: Optional[str] = None
    model_version: Optional[str] = None
    git_sha: Optional[str] = None
    image_digest: Optional[str] = None
    run_id: Optional[str] = None
    dataset_uri: Optional[str] = None
    sensor_id: Optional[str] = None
    frame_ts: Optional[float] = None  # epoch seconds


class ClaimProposal(BaseModel):
    subject_id: str
    predicate: str
    object_kind: str = Field(description="'entity' or 'literal'")
    object_value: str = Field(description="entity id if object_kind='entity'; else literal value")
    model_conf: Optional[float] = None
    human_conf: Optional[float] = None
    context_hash: Optional[str] = None
    status: Optional[str] = Field(default="pending", description="scratchpad|pending|approved|rejected")
    evidence: List[EvidenceModel] = Field(default_factory=list)
    provenance: Optional[ProvenanceModel] = None


class CypherBody(BaseModel):
    query: str
    params: Dict[str, Any] = Field(default_factory=dict)


class NeighborsBody(BaseModel):
    id: str
    depth: int = 1
    limit: int = 50


# ------------------------
# Startup / Teardown
# ------------------------

@app.on_event("startup")
def _on_start() -> None:
    global driver, pg_conn
    driver = GraphDatabase.driver(NEO4J_URI, auth=(NEO4J_USER, NEO4J_PASSWORD))
    with driver.session() as s:
        # --- constraints to keep the graph sane ---
        s.run("CREATE CONSTRAINT IF NOT EXISTS FOR (e:Entity)  REQUIRE e.id IS UNIQUE")
        s.run("CREATE CONSTRAINT IF NOT EXISTS FOR (c:Claim)   REQUIRE c.id IS UNIQUE")
        s.run("CREATE CONSTRAINT IF NOT EXISTS FOR (v:Evidence) REQUIRE v.id IS UNIQUE")
        s.run("RETURN 1").single()

    pg_conn = psycopg.connect(PG_DSN, autocommit=True)
    with pg_conn.cursor() as cur:
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS evidence (
              id BIGSERIAL PRIMARY KEY,
              uri_or_blob_ref TEXT NOT NULL,
              snippet TEXT,
              hash TEXT,
              source_type TEXT,
              quality_score DOUBLE PRECISION,
              ts_epoch DOUBLE PRECISION,
              created_at TIMESTAMPTZ DEFAULT NOW()
            );
            CREATE INDEX IF NOT EXISTS idx_evidence_source_type ON evidence(source_type);
            CREATE INDEX IF NOT EXISTS idx_evidence_created_at  ON evidence(created_at);
            """
        )

@app.on_event("shutdown")
def _on_stop() -> None:
    global driver, pg_conn
    if driver:
        driver.close()
    if pg_conn:
        pg_conn.close()


# ------------------------
# Helpers
# ------------------------

def _neo4j_records_to_json(data_rows):
    """
    `data_rows` is list[dict] from neo4j.Result.data().
    Recursively serialize each value.
    """
    out = []
    for row in data_rows:
        out.append({k: _serialize_neo4j(v) for k, v in row.items()})
    return out

def _serialize_neo4j(v):
    """
    Make Neo4j values JSON-friendly.
    Handles Node, Relationship, Path, lists, and dicts. Primitives pass through.
    """
    if isinstance(v, Node):
        return {
            "_type": "node",
            "labels": list(v.labels),
            "id": v.element_id,
            **dict(v),
        }
    if isinstance(v, Relationship):
        return {
            "_type": "rel",
            "type": v.type,
            "id": v.element_id,
            "start": v.start_node.element_id,
            "end": v.end_node.element_id,
            **dict(v),
        }
    if isinstance(v, Path):
        return {
            "_type": "path",
            "nodes": [_serialize_neo4j(n) for n in v.nodes],
            "rels": [_serialize_neo4j(r) for r in v.relationships],
        }
    if isinstance(v, list):
        return [_serialize_neo4j(x) for x in v]
    if isinstance(v, dict):
        return {k: _serialize_neo4j(x) for k, x in v.items()}
    return v



def _ensure_entity(tx, entity_id: str) -> None:
    tx.run("MERGE (e:Entity {id:$id}) ON CREATE SET e.created_at=timestamp()", id=entity_id)


def _write_evidence_pg(e: EvidenceModel) -> None:
    if not pg_conn:
        return
    with pg_conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO evidence (uri_or_blob_ref, snippet, hash, source_type, quality_score, ts_epoch)
            VALUES (%s,%s,%s,%s,%s,%s)
            """,
            (e.uri_or_blob_ref, e.snippet, e.hash, e.source_type, e.quality_score, e.timestamp),
        )


# ------------------------
# Endpoints
# ------------------------

@app.get("/health")
def health() -> Dict[str, Any]:
    return {"ok": True, "neo4j": bool(driver), "postgres": bool(pg_conn)}


@app.post("/cypher")
def cypher(body: CypherBody) -> Dict[str, Any]:
    if not driver:
        raise HTTPException(500, "Neo4j driver not initialized")
    with driver.session() as s:
        res = s.run(body.query, **body.params)
        data = res.data()  # list[dict], Neo4j 5.x
    return {"records": _neo4j_records_to_json(data)}


@app.post("/neighbors")
def neighbors(body: NeighborsBody) -> Dict[str, Any]:
    """Depth-limited neighbors around an Entity id. Supports depth 1 or 2 (simple)."""
    if body.depth not in (1, 2):
        raise HTTPException(400, "depth must be 1 or 2")
    q = f"""
    MATCH (n:Entity {{id:$id}})
    CALL {{
      WITH n
      MATCH p=(n)-[r*1..{body.depth}]-(m)
      RETURN p LIMIT $limit
    }}
    RETURN p
    """
    return cypher(CypherBody(query=q, params={"id": body.id, "limit": body.limit}))


@app.post("/propose_claim")
def propose_claim(claim: ClaimProposal) -> Dict[str, Any]:
    """Insert Claim + Evidence into the ledger. If status='approved' and object_kind='entity', also materialize edge."""
    if not driver:
        raise HTTPException(500, "Neo4j driver not initialized")

    def _tx(tx):
        _ensure_entity(tx, claim.subject_id)
        if claim.object_kind == "entity":
            _ensure_entity(tx, claim.object_value)

        # Create Claim node
        rec = tx.run(
            """
            CREATE (c:Claim {
              id: apoc.create.uuid(),
              subject_id:$s, predicate:$p, object_kind:$ok, object_value:$ov,
              status:$st, model_conf:$mc, human_conf:$hc, context_hash:$ch,
              who:$who, when:$when, prompt_hash:$ph, model_version:$mv,
              git_sha:$git, image_digest:$img, run_id:$run, dataset_uri:$ds, sensor_id:$sid, frame_ts:$fts,
              created_at: timestamp()
            })
            RETURN c
            """,
            s=claim.subject_id, p=claim.predicate, ok=claim.object_kind, ov=claim.object_value,
            st=claim.status, mc=claim.model_conf, hc=claim.human_conf, ch=claim.context_hash,
            who=(claim.provenance.who if claim.provenance else None),
            when=(claim.provenance.when if claim.provenance else time.time()),
            ph=(claim.provenance.prompt_hash if claim.provenance else None),
            mv=(claim.provenance.model_version if claim.provenance else None),
            git=(claim.provenance.git_sha if claim.provenance else None),
            img=(claim.provenance.image_digest if claim.provenance else None),
            run=(claim.provenance.run_id if claim.provenance else None),
            ds=(claim.provenance.dataset_uri if claim.provenance else None),
            sid=(claim.provenance.sensor_id if claim.provenance else None),
            fts=(claim.provenance.frame_ts if claim.provenance else None),
        ).single()
        c = rec["c"]

        # Link Claim -> Evidence
        for e in claim.evidence:
            tx.run(
                """
                CREATE (ev:Evidence {
                  id: apoc.create.uuid(),
                  uri_or_blob_ref:$u, snippet:$snip, hash:$h, source_type:$src,
                  quality_score:$q, timestamp:$ts, created_at: timestamp()
                })
                WITH ev
                MATCH (c:Claim {id:$cid})
                CREATE (c)-[:SUPPORTS]->(ev)
                """,
                u=e.uri_or_blob_ref, snip=e.snippet, h=e.hash, src=e.source_type,
                q=e.quality_score, ts=e.timestamp, cid=c["id"]
            )
            # Mirror evidence snippet row in Postgres (no embeddings yet)
            _write_evidence_pg(e)

        # If already approved and object is entity, materialize relation
        if claim.status == "approved" and claim.object_kind == "entity":
            tx.run(
                """
                MATCH (c:Claim {id:$cid})
                MATCH (s:Entity {id:$s})
                MATCH (o:Entity {id:$o})
                CALL apoc.create.relationship(s, $pred, {}, o) YIELD rel
                SET c.status='approved'
                RETURN rel
                """,
                cid=c["id"], s=claim.subject_id, o=claim.object_value, pred=claim.predicate
            )
        return c

    with driver.session() as s:
        created = s.execute_write(_tx)

    return {"ok": True, "claim": _serialize_neo4j(created)}


class ClaimIdBody(BaseModel):
    claim_id: str


@app.post("/approve")
def approve(body: ClaimIdBody) -> Dict[str, Any]:
    """Approve an existing Claim and materialize edge if object_kind='entity'."""
    if not driver:
        raise HTTPException(500, "Neo4j driver not initialized")

    def _tx(tx):
        rec = tx.run("MATCH (c:Claim {id:$id}) RETURN c", id=body.claim_id).single()
        if not rec:
            raise HTTPException(404, "Claim not found")
        c = rec["c"]
        if c["object_kind"] == "entity":
            tx.run(
                """
                MATCH (c:Claim {id:$id})
                MATCH (s:Entity {id:c.subject_id})
                MATCH (o:Entity {id:c.object_value})
                CALL apoc.create.relationship(s, c.predicate, {}, o) YIELD rel
                SET c.status='approved'
                RETURN rel
                """,
                id=body.claim_id
            )
        else:
            tx.run("MATCH (c:Claim {id:$id}) SET c.status='approved'", id=body.claim_id)
        return True

    with driver.session() as s:
        s.execute_write(_tx)

    return {"ok": True}


@app.post("/reject")
def reject(body: ClaimIdBody) -> Dict[str, Any]:
    if not driver:
        raise HTTPException(500, "Neo4j driver not initialized")
    with driver.session() as s:
        s.run("MATCH (c:Claim {id:$id}) SET c.status='rejected'", id=body.claim_id)
    return {"ok": True}


@app.get("/gaps")
def gaps(limit: int = 50) -> Dict[str, Any]:
    """Example gap: Entities with no relationships."""
    q = """
    MATCH (e:Entity)
    WHERE NOT (e)--()
    RETURN e LIMIT $limit
    """
    return cypher(CypherBody(query=q, params={"limit": limit}))
