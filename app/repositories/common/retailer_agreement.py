"""Repository for public.retailer_agreement."""

from __future__ import annotations

from datetime import date
from uuid import UUID

from sqlalchemy import delete, select
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
        "dispute_window_days": r.dispute_window_days,
    }


class RetailerAgreementRepository:
    """Access layer for public.retailer_agreement, the retailer agreement master data."""

    def __init__(self, session: Session) -> None:
        self._session = session

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

    def truncate_all(self) -> None:
        """Delete every retailer agreement; callers must first clear anything that FK-references it."""
        self._session.execute(delete(RetailerAgreement))
        self._session.flush()

    def get_effective_for_retailer(self, retailer_id: UUID, as_of_date: date) -> dict | None:
        """Return the retailer's effective agreement as of a date, or `None`.

        An agreement is effective when `effective_date` is on or before `as_of_date`
        and `expiration_date` is either unset or on or after `as_of_date`. If multiple
        agreements match, the one with the latest `effective_date` wins.
        """
        rows = self._session.scalars(
            select(RetailerAgreement)
            .where(
                RetailerAgreement.retailer_id == retailer_id,
                RetailerAgreement.effective_date.is_not(None),
                RetailerAgreement.effective_date <= as_of_date,
                (RetailerAgreement.expiration_date.is_(None))
                | (RetailerAgreement.expiration_date >= as_of_date),
            )
            .order_by(RetailerAgreement.effective_date.desc())
            .limit(1)
        ).all()
        return _retailer_agreement_to_dict(rows[0]) if rows else None
