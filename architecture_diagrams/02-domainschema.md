```mermaid
%% Diagram 2 — Domain Schema (generic, project-agnostic)

classDiagram
  direction LR

  class Entity { 
    <<abstract>>
    +id: string
    +type: string
    +name: string
    +canonical_keys: json
    +created_at: datetime
    +updated_at: datetime
  }

  class Claim {
    +id: string
    +subject_id: string
    +predicate: string
    +object_ref: string|literal
    +status: enum  %% scratchpad|pending|approved|rejected
    +model_conf: float
    +human_conf: float
    +valid_from: datetime
    +valid_to: datetime?
    +who: string
    +when: datetime
    +prompt_hash: string
    +model_version: string
    +context_hash: string
  }

  class Evidence {
    +id: string
    +uri_or_blob_ref: string
    +snippet: text
    +hash: string
    +source_type: enum  %% first_party|internal|web|llm_self
    +quality_score: float
    +timestamp: datetime
  }

  %% Example concrete entities (extend later as needed)
  class Thing
  class Document
  class Person
  class Organization
  class Dataset
  class Model

  Entity <|-- Thing
  Entity <|-- Document
  Entity <|-- Person
  Entity <|-- Organization
  Entity <|-- Dataset
  Entity <|-- Model

  %% Ledger relations
  Claim "1" --> "0..*" Evidence : SUPPORTS/CONTRADICTS

  %% Materialized facts (approved claims only)
  Thing --> Thing : RELATION  %% e.g., "USES", "DEPENDS_ON"
  Document --> Thing : MENTIONS
  Model --> Model : VERSION_OF
