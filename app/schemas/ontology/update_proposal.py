"""`OperationProposal` -- the human-readable, pre-write intermediate
representation the ontology-update POC agent produces before a human
approves anything. Never contains executable SQL; describes what would
change in business terms, including the *current* value being replaced so
a reviewer sees the full before/after. See
`docs/ontology/semantic-context-layer-design.md` §23 and
`app/agents/ontology_update/nodes.py::build_proposal`.
"""

from __future__ import annotations

from pydantic import BaseModel


class ProposalMaterialRef(BaseModel):
    """A material/material-master reference as shown to a human -- business
    codes, not raw internals. `material_master_id` is only populated for
    the `target` reference; `current_value`/`new_value` reference a
    `Material` by code/id."""

    material_code: str | None = None
    material_id: str | None = None
    material_master_id: str | None = None


class ProposalRelationship(BaseModel):
    name: str
    target_entity: str
    kind: str


class OperationProposal(BaseModel):
    operation: str
    entity: str
    target: ProposalMaterialRef
    relationship: ProposalRelationship
    current_value: ProposalMaterialRef
    new_value: ProposalMaterialRef
    summary: str
