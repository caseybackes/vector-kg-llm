# services/scheduler/app.py
# Scheduler as a FastAPI app with a background task loop:
# - Periodically calls KG-API /gaps
# - For each gap, creates a minimal pending Claim via agent-gateway /propose_claim (T2 by default)
#
# ENV:
#   KG_API_URL (http://kg-api:8000)
#   GATEWAY_URL (http://agent-gateway:7000)
#   SCAN_INTERVAL_SECONDS (e.g., 600)
from __future__ import annotations

import asyncio
import os
import time
from typing import Any, Dict

import httpx
from fastapi import FastAPI

app = FastAPI(title="kg-scheduler", version="0.1.0")

KG_API_URL = os.getenv("KG_API_URL", "http://kg-api:8000")
GATEWAY_URL = os.getenv("GATEWAY_URL", "http://agent-gateway:7000")
SCAN_INTERVAL = int(os.getenv("SCAN_INTERVAL_SECONDS", "600"))

_run = True  # flip to False to stop gracefully


async def _fetch_gaps() -> Dict[str, Any]:
    async with httpx.AsyncClient(timeout=30.0) as client:
        r = await client.get(f"{KG_API_URL}/gaps", params={"limit": 20})
        r.raise_for_status()
        return r.json()


async def _propose_placeholder(entity_id: str) -> None:
    """Example: propose a low-risk alias/self relation for review (T2). Adjust for your real gaps."""
    payload = {
        "subject_id": entity_id,
        "predicate": "MENTIONS",   # harmless placeholder; change to a real predicate your policy expects
        "object_kind": "literal",
        "object_value": f"gap-noted-{int(time.time())}",
        "model_conf": 0.0,
        "human_conf": None,
        "context_hash": None,
        "evidence": [],  # no evidence → certainly T2
        "provenance": {"who": "scheduler", "when": time.time(), "model_version": "n/a"}
    }
    async with httpx.AsyncClient(timeout=30.0) as client:
        # go through gateway to hit policy (will be T2/pending)
        await client.post(f"{GATEWAY_URL}/propose_claim", json=payload)


async def loop_task() -> None:
    """Background loop: fetch gaps, create placeholder claims to surface them in review."""
    while _run:
        try:
            data = await _fetch_gaps()
            for rec in data.get("records", []):
                # Expect {"e": {"_type":"node","labels":["Entity"],"id":"...","id":"...","name":...}}
                node = rec.get("e") or {}
                props = {k: v for k, v in node.items() if k not in {"_type", "labels", "id"}}
                entity_id = props.get("id") or node.get("id")
                if entity_id:
                    await _propose_placeholder(entity_id)
        except Exception:
            # swallow to keep the loop running; rely on container logs for visibility
            pass
        await asyncio.sleep(SCAN_INTERVAL)


@app.on_event("startup")
async def _on_start() -> None:
    asyncio.create_task(loop_task())


@app.on_event("shutdown")
async def _on_stop() -> None:
    global _run
    _run = False


@app.get("/health")
async def health() -> Dict[str, Any]:
    return {"ok": True, "interval": SCAN_INTERVAL}
