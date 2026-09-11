"""Repository for public.retailer_agreement."""

from __future__ import annotations

from datetime import date
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import RetailerAgreement


def _retailer_agreement_to_dict(r: RetailerAgreement) -> dict:
    """Serialize a RetailerAgreement row into a dict."""
    return {
        "id": r.id,
        "retailer_id": r.retailer_id,
        "contract_code": r.contract_code,
        "title": r.title,
        "document_sha256": r.document_sha256,
        "source_uri": r.source_uri,
        "markdown_text": r.markdown_text,
        "effective_date": r.effective_date,
        "expiration_date": r.expiration_date,
    }


class RetailerAgreementRepository:
    """Access layer for public.retailer_agreement, the retailer agreement master data."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def get_by_sha256(self, document_sha256: str) -> dict | None:
        """Return the retailer agreement matching a document's sha256, or None; backs upload idempotency."""
        row = self._session.scalars(
            select(RetailerAgreement).where(RetailerAgreement.document_sha256 == document_sha256)
        ).first()
        return _retailer_agreement_to_dict(row) if row is not None else None

    def add_retailer_agreement(
        self,
        retailer_id: UUID,
        contract_code: str,
        title: str,
        document_sha256: str,
        source_uri: str | None = None,
        markdown_text: str | None = None,
        effective_date: date | None = None,
        expiration_date: date | None = None,
    ) -> dict:
        """Insert a new retailer agreement row."""
        row = RetailerAgreement(
            retailer_id=retailer_id,
            contract_code=contract_code,
            title=title,
            document_sha256=document_sha256,
            source_uri=source_uri,
            markdown_text=markdown_text,
            effective_date=effective_date,
            expiration_date=expiration_date,
        )
        self._session.add(row)
        self._session.flush()
        return _retailer_agreement_to_dict(row)

    def get(self, retailer_agreement_id: UUID) -> dict | None:
        """Return the retailer agreement `retailer_agreement_id`, or None if it doesn't exist."""
        row = self._session.get(RetailerAgreement, retailer_agreement_id)
        return _retailer_agreement_to_dict(row) if row is not None else None

    def list_for_retailer(self, retailer_id: UUID) -> list[dict]:
        """Return every retailer agreement for one retailer."""
        rows = self._session.scalars(
            select(RetailerAgreement).where(RetailerAgreement.retailer_id == retailer_id)
        ).all()
        return [_retailer_agreement_to_dict(r) for r in rows]
