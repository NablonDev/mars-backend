"""`InsertOperationProposal` -- the human-readable, pre-write intermediate
representation the ontology-insert POC agent produces before a human
approves anything. Sibling to `app.schemas.ontology.update_proposal`'s
`OperationProposal`, but shaped for a create (three new-or-reused entity
refs) rather than a single before/after relationship value -- see
`app/agents/ontology_insert/nodes.py::build_proposal`.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel

from app.schemas.ontology.update_proposal import ProposalRelationship

__all__ = ["InsertOperationProposal", "NewEntityRef", "ProposalRelationship"]


class NewEntityRef(BaseModel):
    """One of the three rows this operation creates -- business codes, not
    raw internals (no id yet exists until the write happens)."""

    material_code: str | None = None
    plant_code: str | None = None
    sap_material_number: str | None = None
    reused_existing: bool = False


class InsertOperationProposal(BaseModel):
    operation: Literal["INSERT"] = "INSERT"
    entity: str = "MaterialMaster"
    material: NewEntityRef
    plant: NewEntityRef
    material_master: NewEntityRef
    relationship: ProposalRelationship
    summary: str
