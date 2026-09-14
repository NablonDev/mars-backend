"""Integration tests for `retailer_agreement`'s partial unique indexes against a real
Postgres instance: `uq_retailer_agreement_contract_code` and
`uq_retailer_agreement_document_sha256` (both `WHERE deleted_at IS NULL`) actually being
enforced at the DB level, and actually allowing a re-upload once the prior row is
soft-deleted. Neither is reachable from the SQLite unit suite
(tests/unit/repositories/test_rule_extraction.py), which builds its schema from
`Base.metadata.create_all()` rather than the migration these indexes live in only as raw
DDL (see docs/DATABASE.md's "Migration-only raw DDL constructs").

Skips cleanly (not an error) when `DATABASE_URL` isn't pointed at a reachable Postgres
instance -- same convention as tests/integration/test_job_queue_postgres.py.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError, OperationalError

from app.core.config import get_settings
from app.db.session import Database
from app.repositories.common.master_data import MasterDataRepository
from app.repositories.common.retailer_agreement import RetailerAgreementRepository


def _connect_or_none() -> Database | None:
    settings = get_settings()
    if not settings.database.url.startswith("postgresql"):
        return None

    db = Database(settings.database.url)
    try:
        with db.engine.connect() as conn:
            conn.execute(text("SELECT 1"))
    except OperationalError:
        db.dispose()
        return None
    return db


@pytest.fixture(scope="module")
def pg_database():
    db = _connect_or_none()
    if db is None:
        pytest.skip(
            "No reachable Postgres DATABASE_URL configured -- skipping retailer_agreement "
            "Postgres integration tests."
        )
    yield db
    db.dispose()


@pytest.fixture
def retailer_id(pg_database: Database):
    """A fresh retailer for one test, deleted (with any retailer_agreement rows) afterward."""
    session = pg_database.new_session()
    retailer = MasterDataRepository(session).add_retailer(
        f"RET-AGR-IT-{uuid.uuid4().hex[:8]}", "Retailer Agreement Integration Test", None, "SUM"
    )
    session.commit()
    session.close()

    yield retailer["id"]

    cleanup = pg_database.new_session()
    cleanup.execute(text("DELETE FROM retailer_agreement WHERE retailer_id = :id"), {"id": retailer["id"]})
    cleanup.execute(text("DELETE FROM retailer WHERE id = :id"), {"id": retailer["id"]})
    cleanup.commit()
    cleanup.close()


def test_partial_unique_index_rejects_a_live_duplicate_but_allows_after_soft_delete(
    pg_database: Database, retailer_id: uuid.UUID
):
    session = pg_database.new_session()
    contract_code, document_sha256 = "CONTRACT-REUPLOAD-IT", "ab" * 32

    try:
        repo = RetailerAgreementRepository(session)
        first = repo.add_retailer_agreement(
            retailer_id=retailer_id,
            contract_code=contract_code,
            title="Original upload",
            document_sha256=document_sha256,
            markdown_text="# Agreement v1",
        )
        session.commit()

        # Raw insert, deliberately bypassing the repository's own idempotency
        # check (RetailerAgreementRepository.get_by_sha256), to prove the DB-level
        # partial unique indexes themselves reject a second live row.
        with pytest.raises(IntegrityError) as exc_info:
            session.execute(
                text(
                    "INSERT INTO retailer_agreement "
                    "(id, retailer_id, contract_code, title, document_sha256) "
                    "VALUES (:id, :retailer_id, :contract_code, :title, :document_sha256)"
                ),
                {
                    "id": uuid.uuid4(),
                    "retailer_id": retailer_id,
                    "contract_code": contract_code,
                    "title": "Duplicate upload",
                    "document_sha256": document_sha256,
                },
            )
        assert "uq_retailer_agreement_contract_code" in str(exc_info.value) or (
            "uq_retailer_agreement_document_sha256" in str(exc_info.value)
        ), (
            "IntegrityError was not raised by either partial unique index on "
            f"retailer_agreement: {exc_info.value}"
        )
        session.rollback()

        session.execute(
            text("UPDATE retailer_agreement SET deleted_at = now() WHERE id = :id"), {"id": first["id"]}
        )
        session.commit()

        # Now that the only prior row for this code/hash is soft-deleted, a
        # re-upload under the same contract_code/document_sha256 is permitted.
        second = RetailerAgreementRepository(session).add_retailer_agreement(
            retailer_id=retailer_id,
            contract_code=contract_code,
            title="Re-uploaded after soft delete",
            document_sha256=document_sha256,
            markdown_text="# Agreement v2",
        )
        session.commit()
        assert second["id"] != first["id"]
    finally:
        session.close()
