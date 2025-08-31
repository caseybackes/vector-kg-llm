# services/agent-gateway/app.py
# Agent-Gateway (FastAPI): policy gate + thin façade over KG-API + (optional) LM Studio calls
# - Decides T1 (auto-merge) vs T2 (review) based on simple trust heuristic
# - For T1: writes claim with status='approved' to kg-api
# - For T2: writes claim with status='pending' (your review queue lives elsewhere)
#
# ENV:
#   KG_API_URL (e.g., http://kg-api:8000)
#   LLM_BASE_URL (e.g., http://host.docker.internal:1234/v1), LLM_API_KEY
#   TIER_AUTO_TRUST_THRESHOLD (e.g., 0.85) ; TIER_MIN_EVIDENCE_QUALITY (e.g., 0.7)
#
# RUN:
#   uvicorn app:app --host 0.0.0.0 --port 7000
from __future__ import annotations

import os
from typing import Any, Dict, List, Optional

import httpx
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

app = FastAPI(title="agent-gateway", version="0.1.0")

KG_API_URL = os.getenv("KG_API_URL", "http://kg-api:8000")
LLM_BASE_URL = os.getenv("LLM_BASE_URL", "http://host.docker.internal:1234/v1")
LLM_API_KEY = os.getenv("LLM_API_KEY", "lm-studio")

AUTO_TRUST = float(os.getenv("TIER_AUTO_TRUST_THRESHOLD", "0.85"))
MIN_QUAL = float(os.getenv("TIER_MIN_EVIDENCE_QUALITY", "0.70"))

AUTO_MERGE_PREDICATES = {"USES", "INGESTS", "PRODUCES"}  # tweak as needed
FIRST_PARTY = {"first_party_log", "config", "run_artifact"}


# ------------------------
# Models
# ------------------------

class Evidence(BaseModel):
    uri_or_blob_ref: str
    snippet: Optional[str] = None
    hash: Optional[str] = None
    source_type: str
    quality_score: Optional[float] = None
    timestamp: Optional[float] = None


class Provenance(BaseModel):
    who: Optional[str] = None
    when: Optional[float] = None
    prompt_hash: Optional[str] = None
    model_version: Optional[str] = None
    git_sha: Optional[str] = None
    image_digest: Optional[str] = None
    run_id: Optional[str] = None
    dataset_uri: Optional[str] = None
    sensor_id: Optional[str] = None
    frame_ts: Optional[float] = None


class ClaimIn(BaseModel):
    subject_id: str
    predicate: str
    object_kind: str = Field(description="'entity' or 'literal'")
    object_value: str
    model_conf: Optional[float] = None
    human_conf: Optional[float] = None
    context_hash: Optional[str] = None
    evidence: List[Evidence] = Field(default_factory=list)
    provenance: Optional[Provenance] = None


class CypherBody(BaseModel):
    query: str
    params: Dict[str, Any] = {}


# ------------------------
# Helpers
# ------------------------

async def _kg_post(path: str, json: Dict[str, Any]) -> Dict[str, Any]:
    async with httpx.AsyncClient(timeout=30.0) as client:
        r = await client.post(f"{KG_API_URL}{path}", json=json)
        if r.status_code >= 400:
            raise HTTPException(r.status_code, r.text)
        return r.json()


async def _kg_get(path: str, params: Dict[str, Any] | None = None) -> Dict[str, Any]:
    async with httpx.AsyncClient(timeout=30.0) as client:
        r = await client.get(f"{KG_API_URL}{path}", params=params)
        if r.status_code >= 400:
            raise HTTPException(r.status_code, r.text)
        return r.json()


def _trust_score(claim: ClaimIn) -> float:
    """Heuristic: evidence quality + model_conf + first-party bonus; clamp [0,1]."""
    qual = max((e.quality_score or 0.0) for e in claim.evidence) if claim.evidence else 0.0
    first_party_bonus = 0.15 if all((e.source_type in FIRST_PARTY) for e in claim.evidence) and claim.evidence else 0.0
    model_conf = claim.model_conf or 0.0
    raw = 0.5 * qual + 0.4 * model_conf + first_party_bonus
    return max(0.0, min(1.0, raw))


async def _has_conflicts(claim: ClaimIn) -> bool:
    """Naive conflict check: same subject+predicate exists with different object."""
    q = """
    MATCH (s:Entity {id:$sid})-[r]->(o)
    WHERE type(r) = $pred
    RETURN collect(distinct o.id) AS objs
    """
    try:
        res = await _kg_post("/cypher", {"query": q, "params": {"sid": claim.subject_id, "pred": claim.predicate}})
        objs = (res.get("records", [{}])[0] or {}).get("objs", [])
        # Conflict if some other object exists that's different than proposed (entity-only semantic)
        if claim.object_kind == "entity":
            return any(o != claim.object_value for o in objs)
        return False
    except Exception:
        # On failure, be conservative: route to review
        return True


# ------------------------
# Endpoints
# ------------------------

@app.get("/health")
async def health() -> Dict[str, Any]:
    try:
        kg = await _kg_get("/health")
    except Exception as e:
        kg = {"error": str(e)}
    return {"ok": True, "kg_api": kg}


@app.post("/propose_claim")
async def propose_claim(claim: ClaimIn) -> Dict[str, Any]:
    """Policy gate:
       - If predicate ∈ AUTO_MERGE_PREDICATES, object_kind=='entity', trust≥AUTO_TRUST, MIN_QUAL satisfied, and no conflicts → approve
       - Else → pending
    """
    trust = _trust_score(claim)
    min_qual_ok = all((e.quality_score or 0.0) >= MIN_QUAL for e in claim.evidence) if claim.evidence else False
    no_conflict = not await _has_conflicts(claim)

    auto_merge = (
        claim.predicate in AUTO_MERGE_PREDICATES and
        claim.object_kind == "entity" and
        trust >= AUTO_TRUST and
        min_qual_ok and
        no_conflict
    )

    status = "approved" if auto_merge else "pending"

    payload = claim.model_dump()
    payload["status"] = status

    created = await _kg_post("/propose_claim", payload)

    # If we forced approved but KG marked pending (edge case), try /approve
    if status == "approved":
        try:
            cid = created["claim"]["id"]
            await _kg_post("/approve", {"claim_id": cid})
        except Exception:
            pass

    return {
        "ok": True,
        "decision": "T1-auto-merge" if auto_merge else "T2-review",
        "trust": trust,
        "min_quality_ok": min_qual_ok,
        "no_conflict": no_conflict,
        "kg": created,
    }


@app.post("/cypher")
async def cypher(body: CypherBody) -> Dict[str, Any]:
    """Pass-through helper to KG for ad-hoc queries (use sparingly)."""
    return await _kg_post("/cypher", body.model_dump())


# Optional LLM passthrough (LM Studio). Keep simple; you can wire this into /query later.
@app.post("/llm_chat")
async def llm_chat(messages: List[Dict[str, str]]) -> Dict[str, Any]:
    headers = {"Authorization": f"Bearer {LLM_API_KEY}"}
    payload = {
        "model": "local-model",  # LM Studio ignores name by default; set your loaded model name if needed
        "messages": messages,
        "temperature": 0.2,
        "stream": False,
    }
    async with httpx.AsyncClient(base_url=LLM_BASE_URL, headers=headers, timeout=60.0) as client:
        r = await client.post("/chat/completions", json=payload)
        r.raise_for_status()
        data = r.json()
        return {"text": data["choices"][0]["message"]["content"], "raw": data}
