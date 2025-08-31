# PATCH 2 — services/agent-gateway/__init__.py (tighten exports; drop unused imports)

import os, re

LLM_BASE_URL = os.getenv("LLM_BASE_URL", "http://host.docker.internal:1234/v1")
LLM_API_KEY  = os.getenv("LLM_API_KEY", "lm-studio")
LLM_MODEL    = os.getenv("LLM_MODEL", "llama-3.2-1b-instruct")

KG_API_URL = os.getenv("KG_API_URL", "http://kg-api:8000")

AUTO_TRUST = float(os.getenv("TIER_AUTO_TRUST_THRESHOLD", "0.85"))
MIN_QUAL   = float(os.getenv("TIER_MIN_EVIDENCE_QUALITY", "0.70"))

AUTO_MERGE_PREDICATES = {"USES", "INGESTS", "PRODUCES"}
FIRST_PARTY           = {"first_party_log", "config", "run_artifact"}
ALLOWED_READ_RELS     = {"USES","INGESTS","PRODUCES","VERSION_OF","MENTIONS","FIXED_BY","ORIGINATES_AT"}

_ADD_RE = re.compile(
    r"Add a claim:\s*`?([^`\s]+)`?\s+([A-Z_]+)\s+`?([^`\s]+)`?.*?quality\s+([0-9.]+)",
    re.I,
)
_NEI_RE = re.compile(r"neighbors.*`([^`]+)`.*depth\s+(\d+)", re.I)

SYSTEM_PROMPT = (
    "You are a tool-using assistant. Only respond with ONE JSON object per turn.\n"
    "SCHEMA:\n"
    '  {"tool":"neighbors","args":{"id":"<entity-id>","depth":1|2,"limit":<int>}}\n'
    '  {"tool":"cypher","args":{"query":"<READ-ONLY CYPHER>","params":{"id":"<id>","id2":"<id2>"}}}\n'
    '  {"tool":"propose_claim","args":{\n'
    '      "subject_id":"<id>","predicate":"<RELATION>",\n'
    '      "object_kind":"entity|literal","object_value":"<id-or-literal>",\n'
    '      "model_conf":<0..1>,\n'
    '      "evidence":[{"uri_or_blob_ref":"<uri>","source_type":"first_party_log|config|run_artifact|internal_doc|web|llm_self","quality_score":<0..1>}],\n'
    '      "provenance":{"who":"<agent>", "when":<epoch>}\n'
    '  }}\n'
    '  OR {"final":{"answer":"<text>","citations":[...]}}\n'
    "RULES:\n"
    "- Entities are identified by property **id** (NOT name).\n"
    "- Prefer **neighbors** for “list neighbors … depth N”.\n"
    f"- If using **cypher**, it must be READ-ONLY and only these rel types: {','.join(sorted(ALLOWED_READ_RELS))}\n"
    "  Use parameters (e.g., MATCH (e:Entity {id:$id}) ...).\n"
    "- Use **propose_claim** ONLY when the user explicitly asks to add/update knowledge.\n"
    '- If required fields are missing, return {"final":{"answer":"ask user for <field>"}}.\n'
    "EXAMPLES:\n"
    '1) Q: "List neighbors of Entity `Run:demo` depth 1."\n'
    '   A: {"tool":"neighbors","args":{"id":"Run:demo","depth":1,"limit":50}}\n'
    '2) Q: "Find path up to 2 hops between `A` and `B`."\n'
    '   A: {"tool":"cypher","args":{"query":"MATCH p=shortestPath((:Entity {id:$id})-[:USES|INGESTS|PRODUCES*..2]-(:Entity {id:$id2})) RETURN p","params":{"id":"A","id2":"B"}}}\n'
    '3) Q: "Add claim: Run:demo USES Model:v2 (first-party, qual=0.95)."\n'
    '   A: {"tool":"propose_claim","args":{\n'
    '         "subject_id":"Run:demo","predicate":"USES","object_kind":"entity","object_value":"Model:v2",\n'
    '         "model_conf":0.9,\n'
    '         "evidence":[{"uri_or_blob_ref":"log://run/demo","source_type":"first_party_log","quality_score":0.95}],\n'
    '         "provenance":{"who":"gateway","when":1690000000}\n'
    '      }}\n'
)

__all__ = [
    "LLM_BASE_URL",
    "LLM_API_KEY",
    "LLM_MODEL",
    "KG_API_URL",
    "AUTO_TRUST",
    "MIN_QUAL",
    "AUTO_MERGE_PREDICATES",
    "FIRST_PARTY",
    "ALLOWED_READ_RELS",
    "_ADD_RE",
    "_NEI_RE",
    "SYSTEM_PROMPT",
]
