"""API schemas for the CMIR <-> Material Master traceability endpoints
(`app/api/v1/ontology.py`) -- read-only responses over `OntologyGraphService`,
component B of the ontology plan (no reasoning/inference involved)."""

from __future__ import annotations

from pydantic import BaseModel


class TraceabilityCmirRecord(BaseModel):
    """One CMIR record found to reference the queried material."""

    cmir_record_id: str
    customer_identity: str
    target_customer_material_ref: str
    brand: str
    site: str


class TraceabilityResult(BaseModel):
    material_code: str
    cmir_records: list[TraceabilityCmirRecord]


class SuccessorChainLink(BaseModel):
    material_code: str
    discontinuation_indicator: str | None = None


class SuccessorChainResult(BaseModel):
    material_master_id: str
    chain: list[SuccessorChainLink]
