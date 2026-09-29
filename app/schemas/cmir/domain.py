"""Internal graph-state and value-object shapes for the CMIR domain, not API contracts."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from pydantic import BaseModel, Field


class CmirStatus(str, Enum):
    """Review status of a CMIR record, derived from its content by `CmirValidator`."""

    PENDING_REVIEW = "pending_review"
    PENDING_HUMAN_ACTION = "pending_human_action"
    APPROVED = "approved"
    REJECTED = "rejected"


MANDATORY_FIELDS: tuple[str, ...] = (
    "sender_type",
    "customer_identity",
    "material_identity",
    "intent_phrase",
    "existing_cmir_ref",
    "brand",
    "site",
    "target_customer_material_ref",
)

# Every field a reviewer can edit and the SCD2 merge (app.services.cmir.merge) considers.
# Excludes `status` and `missing_fields`, which CmirValidator derives from this content.
CMIR_CONTENT_FIELDS: tuple[str, ...] = MANDATORY_FIELDS + (
    "target_grd_code",
    "effective_date",
    "reason",
)


class Cmir(BaseModel):
    """In-memory CMIR representation carried through LangGraph state, not an API contract."""

    sender_type: str = ""
    customer_identity: str = ""
    material_identity: str = Field(
        "",
        description=(
            "Internal/generic material identity mentioned in the email, not necessarily "
            "the customer's own material code."
        ),
    )
    intent_phrase: str = ""
    existing_cmir_ref: str = ""
    brand: str = ""
    site: str = ""
    target_grd_code: str = ""
    target_customer_material_ref: str = Field(
        "",
        description=(
            "The customer's own material number/reference as written in their email, "
            "e.g. a line labeled 'Customer Material', 'Cust Mat No', 'Customer Material "
            "Code', or similar."
        ),
    )
    effective_date: str = ""
    reason: str = ""
    status: str = ""
    missing_fields: list[str] = Field(default_factory=list)


@dataclass(frozen=True)
class EmailMessage:
    """Normalized representation of an inbound CMIR email."""

    imap_id: str
    sender: str
    subject: str
    body: str
    source_message_id: str | None = None
    mark_read: bool = True
