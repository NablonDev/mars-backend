"""Retailer agreement master data, `public.retailer_agreement`."""

from datetime import date
from uuid import UUID

from sqlalchemy import CHAR, Date, ForeignKey, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import UUID_PK, Base, TimestampMixin, generate_uuid7


class RetailerAgreement(Base, TimestampMixin):
    """Retailer agreement document; source of penalty rule extraction.

    `document_sha256` makes re-upload idempotent.
    """

    __tablename__ = "retailer_agreement"

    id: Mapped[UUID] = mapped_column(UUID_PK, primary_key=True, default=generate_uuid7)
    retailer_id: Mapped[UUID] = mapped_column(UUID_PK, ForeignKey("retailer.id"), index=True)
    contract_code: Mapped[str] = mapped_column(String(100), index=True)
    title: Mapped[str] = mapped_column(String(300))
    document_sha256: Mapped[str] = mapped_column(CHAR(64), index=True)
    source_uri: Mapped[str | None] = mapped_column(String(500), nullable=True)
    markdown_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    effective_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    expiration_date: Mapped[date | None] = mapped_column(Date, nullable=True)
