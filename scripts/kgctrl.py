# scripts/kgctl.py
# Simple home CLI (no curl). Install deps: pip install typer requests rich
# Usage:
#   export GATEWAY=http://localhost:7000
#   export GATEWAY_API_KEY=...
#   python scripts/kgctl.py neighbors Run:demo --depth 1
#   python scripts/kgctl.py propose Run:demo USES Model:v2 --qual 0.95 --model-conf 0.9
#   python scripts/kgctl.py chat "reply with OK"

import os, json, typing as t
import requests
import typer
from rich import print

app = typer.Typer(add_completion=False)

GATEWAY=os.getenv("GATEWAY","http://localhost:7000")
API_KEY=os.getenv("GATEWAY_API_KEY","")

def _h():
    return {"X-API-Key": API_KEY} if API_KEY else {}

@app.command()
def health():
    r=requests.get(f"{GATEWAY}/health", timeout=10)
    r.raise_for_status()
    print(r.json())

@app.command()
def neighbors(entity_id: str, depth: int = 1, limit: int = 50):
    payload={"id": entity_id, "depth": depth, "limit": limit}
    # call kg-api through gateway’s pass-through to keep one entrypoint
    r=requests.post(f"{GATEWAY}/cypher", json={
        "query": f"MATCH (n:Entity {{id:$id}}) MATCH p=(n)-[*1..{depth}]-(m) RETURN p LIMIT $limit",
        "params": {"id": entity_id, "limit": limit}
    }, headers=_h(), timeout=20)
    r.raise_for_status()
    print(r.json())

@app.command()
def propose(subject_id: str, predicate: str, object_value: str,
           object_kind: str = typer.Option("entity", help="entity|literal"),
           qual: float = 0.9,
           model_conf: float = 0.9):
    payload = {
        "subject_id": subject_id,
        "predicate": predicate.upper(),
        "object_kind": object_kind,
        "object_value": object_value,
        "model_conf": model_conf,
        "evidence": [{
            "uri_or_blob_ref": f"log://{subject_id}",
            "source_type": "first_party_log",
            "quality_score": qual,
        }]
    }
    r=requests.post(f"{GATEWAY}/propose_claim", json=payload, headers=_h(), timeout=30)
    r.raise_for_status()
    print(r.json())

@app.command()
def chat(prompt: str):
    r=requests.post(f"{GATEWAY}/llm_chat", json=[{"role":"user","content":prompt}], timeout=30)
    r.raise_for_status()
    print(r.json().get("text",""))

if __name__ == "__main__":
    app()
