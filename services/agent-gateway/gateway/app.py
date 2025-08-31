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

import os, json, re
from typing import Any, Dict, List, Optional

import httpx
from fastapi import FastAPI, HTTPException, Depends
from pydantic import BaseModel, Field

from . import (
    LLM_MODEL, LLM_API_KEY, KG_API_URL, LLM_BASE_URL,
    AUTO_TRUST, MIN_QUAL, AUTO_MERGE_PREDICATES, FIRST_PARTY,
    ALLOWED_READ_RELS, _ADD_RE, _NEI_RE, SYSTEM_PROMPT,
)

from .auth import require_key

app = FastAPI(title="agent-gateway", version="0.1.0")




# ------------------------
# Models
# ------------------------

class QueryIn(BaseModel):
    question: str
    max_steps: int = 4


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

async def _call_llm(messages: list[dict]) -> str:
    """Call LM Studio (OpenAI-compatible). Avoid features some models 400 on."""
    headers = {"Authorization": f"Bearer {LLM_API_KEY}"}
    payload = {
        "model": LLM_MODEL,        # e.g. "TheBloke/Mistral-7B…", "qwen2.5:7b", etc.
        "messages": messages,
        "temperature": 0.1,
        "stream": False,
        # DO NOT send response_format/tools to keep it broadly compatible
    }
    async with httpx.AsyncClient(base_url=LLM_BASE_URL, headers=headers, timeout=60.0) as client:
        r = await client.post("/chat/completions", json=payload)
        r.raise_for_status()  # will 400 if LM Studio rejects payload
        return r.json()["choices"][0]["message"]["content"]


def _extract_json(s: str) -> dict:
    """Extract first JSON object. If none, coerce to final answer.
    Also strips wrappers some LLMs add like [TOOL_RESULT]..."""
    s = re.sub(r'^\s*\[TOOL_RESULT\]\s*', '', s)
    s = re.sub(r'\s*\[END_TOOL_RESULT\]\s*$', '', s)
    m = re.search(r"\{.*\}", s, flags=re.DOTALL)
    if m:
        try:
            return json.loads(m.group(0))
        except Exception:
            pass
    return {"final": {"answer": s.strip() or ""}}

async def _dispatch_tool(req: dict) -> dict:
    """Map tool calls to kg-api endpoints."""
    tool = req.get("tool")
    args = req.get("args", {})
    if tool == "cypher":
        q = args.get("query","")
        if not _cypher_safe(q):
            raise HTTPException(400, f"Rejected unsafe/unknown Cypher: {q[:120]}")
    if tool == "neighbors":
        # args: {"id": "Entity:123", "depth": 1, "limit": 50}
        return await _kg_post("/neighbors", args)

    if tool == "propose_claim":
        # args must match kg-api ClaimProposal (object_kind in {"entity","literal"})
        return await _kg_post("/propose_claim", args)
    if tool == "cypher":
        return await _kg_post("/cypher", args)
    raise HTTPException(400, f"Unknown tool: {tool}")

def _maybe_route_neighbors(q: str) -> dict | None:
    m = _NEI_RE.search(q)
    if not m:
        return None
    ent, depth = m.group(1), max(1, min(2, int(m.group(2))))
    return {"tool":"neighbors","args":{"id":ent,"depth":depth,"limit":50}}

def _cypher_safe(query: str) -> bool:
    bad = ["name:", "HAS_KNOWLEDGE", "CREATE ", "MERGE ", "DELETE ", "SET "]
    return not any(b in query for b in bad)

def _maybe_route_add_claim(q: str) -> dict | None:
    m = _ADD_RE.search(q)
    if not m:
        return None
    subj, pred, obj, qual = m.groups()
    return {
        "tool": "propose_claim",
        "args": {
            "subject_id": subj,
            "predicate": pred.upper(),
            "object_kind": "entity",
            "object_value": obj,
            "model_conf": 0.9,
            "evidence": [{
                "uri_or_blob_ref": f"log://{subj}",
                "source_type": "first_party_log",
                "quality_score": float(qual),
            }],
            "provenance": {"who": "gateway", "when": __import__("time").time()}
        }
    }


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
async def propose_claim(claim: ClaimIn, _=Depends(require_key)) -> Dict[str, Any]:
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
async def cypher(body: CypherBody, _=Depends(require_key)) -> Dict[str, Any]:
    """Pass-through helper to KG for ad-hoc queries (use sparingly)."""
    return await _kg_post("/cypher", body.model_dump())


@app.post("/llm_chat")
async def llm_chat(messages: List[Dict[str, str]]) -> Dict[str, Any]:
    """
    Minimal LM Studio passthrough. Use the actual model id from __init__.py (LLM_MODEL);
    keeps payload minimal for widest compatibility.
    """
    headers = {"Authorization": f"Bearer {LLM_API_KEY}"}
    payload = {
        "model": LLM_MODEL,      # <- was hardcoded "local-model"
        "messages": messages,
        "temperature": 0.2,
        "stream": False,
    }
    async with httpx.AsyncClient(base_url=LLM_BASE_URL, headers=headers, timeout=60.0) as client:
        r = await client.post("/chat/completions", json=payload)
        r.raise_for_status()
        data = r.json()
        return {"text": data["choices"][0]["message"]["content"], "raw": data}


@app.post("/query")
async def query(body: QueryIn, _=Depends(require_key)) -> dict:
    messages = [
        {"role":"system","content": SYSTEM_PROMPT},
        {"role":"user","content": body.question},
    ]
    trace = []

    routed_add = _maybe_route_add_claim(body.question)
    if routed_add:
        result = await _dispatch_tool(routed_add)
        return {"ok": True, "answer": "", "trace": [{"assistant": json.dumps(routed_add)}, {"tool_result": result}]}


    # fast-path router for common asks
    routed = _maybe_route_neighbors(body.question)
    if routed:
        result = await _dispatch_tool(routed)
        trace += [{"assistant": json.dumps(routed)}, {"tool_result": result}]
        # give model one chance to summarize with a final
        messages += [{"role":"assistant","content": json.dumps(routed)},
                    {"role":"tool","content": json.dumps(result)}]
        assistant_text = await _call_llm(messages)
        trace.append({"assistant": assistant_text})
        obj = _extract_json(assistant_text)
        if "final" in obj:
            return {"ok": True, "answer": obj["final"].get("answer",""), "trace": trace}
        # if not finalized, just return the tool result
        return {"ok": True, "answer": "", "trace": trace, "data": result}

    # normal loop...
    for _ in range(max(1, body.max_steps)):
        assistant_text = await _call_llm(messages)
        trace.append({"assistant": assistant_text})
        obj = _extract_json(assistant_text)
        if "final" in obj:
            return {"ok": True, "answer": obj["final"].get("answer",""), "trace": trace}
        result = await _dispatch_tool(obj)
        trace.append({"tool_result": result})
        messages += [{"role":"assistant","content": assistant_text},
                     {"role":"tool","content": json.dumps(result)}]
    return {"ok": True, "answer":"(stopped: max_steps)", "trace": trace}
