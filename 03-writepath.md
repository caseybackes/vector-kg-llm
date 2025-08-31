```mermaid
%% Diagram 3 — Write Path (Propose -> Gate -> Materialize)

sequenceDiagram
  autonumber
  actor User
  participant Agent as LLM Agent
  participant GW as agent-gateway (policy)
  participant API as kg-api
  participant GDB as GraphDB
  participant VDB as VectorDB
  participant UI as Review UI (optional)

  User->>Agent: Provide info / confirm intent
  Agent->>GW: tool.propose_claim(Claim,Evidence,Provenance)
  GW->>API: /propose (dedup + conflict check)
  API->>GDB: upsert Claim/Evidence (status=pending|auto)
  API->>VDB: index snippets (for dedup/search)
  API-->>GW: {conflicts, trust_score}

  alt Auto-merge (first-party, no conflicts, high trust)
    GW->>API: /materialize_edges(Claim.id)
    API->>GDB: set Claim.status=approved; create edges
    API->>VDB: re-embed impacted nodes/text
  else Human review (default)
    GW->>UI: enqueue review card
    UI->>API: approve/reject
    API->>GDB: apply decision (edges or close)
  end
``` 