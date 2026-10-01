"""Repository for cmir.cmir_record, SCD2-versioned CMIR mapping history."""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models import CmirRecord, HumanAction, WorkflowThreadSubject
from app.models.enums import WorkflowThreadSubjectType
from app.schemas.cmir import MANDATORY_FIELDS
from app.services.identity import normalize_identity_key

logger = logging.getLogger(__name__)


class CmirVersionConflict(Exception):
    """Raised when the current record changed since the version being written was read."""


def _to_dict(row: CmirRecord) -> dict[str, Any]:
    """Project a `CmirRecord` row onto the content fields callers see, without SCD2 bookkeeping."""
    return {
        "id": row.id,
        "sender_type": row.sender_type,
        "customer_identity": row.customer_identity,
        "material_identity": row.material_identity,
        "intent_phrase": row.intent_phrase,
        "existing_cmir_ref": row.existing_cmir_ref,
        "brand": row.brand,
        "site": row.site,
        "target_grd_code": row.target_grd_code,
        "target_customer_material_ref": row.target_customer_material_ref,
        "effective_date": row.effective_date.isoformat() if row.effective_date else "",
        "reason": row.reason,
    }


class CmirRecordRepository:
    """SCD2-versioned CMIR mapping history.

    At most one `is_current` row exists per (customer_identity,
    target_customer_material_ref) pair; supersessions are recorded rather than
    overwritten in place.
    """

    def __init__(self, session: Session) -> None:
        self._session = session

    def find_latest_for_customer_material(
        self, customer_identity: str, target_customer_material_ref: str
    ) -> dict[str, Any] | None:
        """Return the current row for this customer/material pair, or None.

        Kept separate from `get_current` because PO validation needs only the four
        fields returned here, not the full content-field set.
        """
        row = self._session.scalars(
            select(CmirRecord).where(
                CmirRecord.customer_identity_key == normalize_identity_key(customer_identity),
                CmirRecord.target_customer_material_ref_key
                == normalize_identity_key(target_customer_material_ref),
                CmirRecord.is_current.is_(True),
            )
        ).first()
        if row is None:
            return None
        return {
            "id": row.id,
            "customer_identity": row.customer_identity,
            "material_identity": row.material_identity,
            "target_customer_material_ref": row.target_customer_material_ref,
        }

    def create_manual_mapping(
        self,
        *,
        customer_identity: str,
        material_identity: str,
        target_customer_material_ref: str,
        description: str = "",
    ) -> UUID:
        """Insert a CMIR mapping from a human-submitted PO line entry.

        Callers reach this only after `validate_against_cmir` found no current
        mapping, so "no current row" is a precondition. Routing through
        `supersede_and_insert` keeps the one-current-per-entity invariant, turning a
        concurrent insert into `CmirVersionConflict` instead of a second current row.
        """
        return self.supersede_and_insert(
            customer_identity=customer_identity,
            target_customer_material_ref=target_customer_material_ref,
            merged={
                "sender_type": "",
                "customer_identity": customer_identity,
                "material_identity": material_identity,
                "intent_phrase": None,
                "existing_cmir_ref": "",
                "brand": "",
                "site": "",
                "target_grd_code": "",
                "target_customer_material_ref": target_customer_material_ref,
                "effective_date": None,
                "reason": description or "",
            },
            expected_current_id=None,
        )

    def get_current(self, customer_identity: str, target_customer_material_ref: str) -> dict[str, Any] | None:
        """Return the current (is_current) cmir_record row for this entity, or None."""
        row = self._session.scalars(
            select(CmirRecord).where(
                CmirRecord.customer_identity_key == normalize_identity_key(customer_identity),
                CmirRecord.target_customer_material_ref_key
                == normalize_identity_key(target_customer_material_ref),
                CmirRecord.is_current.is_(True),
            )
        ).first()
        return _to_dict(row) if row is not None else None

    def list_current(self) -> list[CmirRecord]:
        """Every current (`is_current`) row, as raw ORM objects.

        Distinct from the dict-returning methods above: bulk consumers that
        need full attribute access to build something else from every row
        (e.g. the ontology materialize job's `OntologyBuilder`) work directly
        against the ORM objects here, the same way `get_health_snapshot`
        below already does over its own `current_rows` fetch, rather than
        paying for a narrower dict shape and then widening it back out.
        """
        return list(self._session.scalars(select(CmirRecord).where(CmirRecord.is_current.is_(True))).all())

    def get_health_snapshot(self, *, stale_days: int = 180, attention_limit: int = 50) -> dict[str, Any]:
        """Real, currently-computable CMIR table health.

        Ported from the old `PostgresCMIRRepository.get_health_snapshot`
        (`fix/cmir-uuid-servicebus-llm`); only the session acquisition
        changed (this repository now takes an injected `Session`, so there
        is no `with self._db.session()` wrapper to open here -- every other
        method on this class already works this way). The query/Python
        business logic is otherwise unchanged, including deliberately
        having no dormant-customer or deactivated-material category:
        cmir_record has no order-history relationship, and no link to
        MaterialMaster.discontinuation_indicator, so neither can be honestly
        derived from data that exists today. Health flags are computed in
        Python over one fetch of the (CSR-tool scale, not big-data)
        is_current row set, rather than SQL CASE expressions -- simpler to
        read and to keep in sync with MANDATORY_FIELDS.
        """
        stale_cutoff = datetime.now(UTC) - timedelta(days=stale_days)
        trend_cutoff = datetime.now(UTC) - timedelta(days=180)

        current_rows = self._session.scalars(select(CmirRecord).where(CmirRecord.is_current.is_(True))).all()

        # Reuse the same expression object for select/group_by/order_by --
        # three separate func.date_trunc(...) calls each bind "month" as a
        # distinct parameter, and Postgres then refuses to recognize the
        # GROUP BY/ORDER BY expressions as identical to the SELECT one.
        month_expr = func.date_trunc("month", CmirRecord.valid_from).label("month")
        trend_rows = self._session.execute(
            select(month_expr, func.count().label("count"))
            .where(
                CmirRecord.is_current.is_(True),
                CmirRecord.valid_from >= trend_cutoff,
            )
            .group_by(month_expr)
            .order_by(month_expr)
        ).all()

        key_counts: dict[tuple[str, str], int] = {}
        for row in current_rows:
            key = (row.customer_identity_key, row.target_customer_material_ref_key)
            key_counts[key] = key_counts.get(key, 0) + 1

        stale_count = 0
        missing_field_count = 0
        duplicate_count = 0
        healthy_count = 0
        attention: list[dict[str, Any]] = []

        for row in current_rows:
            reasons: list[str] = []
            if row.valid_from < stale_cutoff:
                reasons.append("stale_validation")
            if any(not (getattr(row, field) or "").strip() for field in MANDATORY_FIELDS):
                reasons.append("missing_required_field")
            key = (row.customer_identity_key, row.target_customer_material_ref_key)
            if key_counts[key] > 1:
                reasons.append("duplicate_row")

            if reasons:
                if "stale_validation" in reasons:
                    stale_count += 1
                if "missing_required_field" in reasons:
                    missing_field_count += 1
                if "duplicate_row" in reasons:
                    duplicate_count += 1
                attention.append(
                    {
                        "id": str(row.id),
                        "customer_identity": row.customer_identity,
                        "target_customer_material_ref": row.target_customer_material_ref,
                        "target_grd_code": row.target_grd_code,
                        "brand": row.brand,
                        "site": row.site,
                        "valid_from": row.valid_from.isoformat(),
                        "reasons": reasons,
                    }
                )
            else:
                healthy_count += 1

        attention.sort(key=lambda item: item["valid_from"])

        return {
            "total_current": len(current_rows),
            "healthy_current": healthy_count,
            "unhealthy_breakdown": [
                {"category": "stale_validation", "count": stale_count},
                {"category": "missing_required_field", "count": missing_field_count},
                {"category": "duplicate_row", "count": duplicate_count},
            ],
            "creation_trend": [
                {"month": row.month.strftime("%Y-%m"), "count": row.count} for row in trend_rows
            ],
            "needing_attention": attention[:attention_limit],
        }

    def get_health_trend(self, *, months: int = 6, stale_days: int = 180) -> list[dict[str, Any]]:
        """Point-in-time reconstruction of Total vs Healthy record counts for
        each of the last `months` month-ends, from `cmir_record`'s own SCD2
        `valid_from`/`valid_to` columns -- there is no stored trend table,
        and this is different from `get_health_snapshot`'s `creation_trend`
        (which counts new *writes* per month): this instead reconstructs
        what the whole table looked like as of each past month-end, for the
        "Table Health Trend" chart's Total-vs-Healthy lines.

        CSR-tool scale (see `get_health_snapshot`'s docstring) -- fetches
        every `cmir_record` row once (not just `is_current`) and does the
        health-check math in Python per boundary, rather than `months`
        separate SQL queries.
        """
        all_rows = self._session.scalars(select(CmirRecord)).all()

        now = datetime.now(UTC)
        year, month = now.year, now.month
        month_starts: list[datetime] = []
        for _ in range(months):
            month_starts.append(datetime(year, month, 1, tzinfo=UTC))
            month -= 1
            if month == 0:
                month, year = 12, year - 1
        month_starts.reverse()

        def _next_month_start(dt: datetime) -> datetime:
            return (
                datetime(dt.year + 1, 1, 1, tzinfo=UTC)
                if dt.month == 12
                else datetime(dt.year, dt.month + 1, 1, tzinfo=UTC)
            )

        trend: list[dict[str, Any]] = []
        for month_start in month_starts:
            as_of = min(_next_month_start(month_start), now)
            current_at_boundary = [
                row
                for row in all_rows
                if row.valid_from <= as_of and (row.valid_to is None or row.valid_to > as_of)
            ]

            key_counts: dict[tuple[str, str], int] = {}
            for row in current_at_boundary:
                key = (row.customer_identity_key, row.target_customer_material_ref_key)
                key_counts[key] = key_counts.get(key, 0) + 1

            stale_cutoff = as_of - timedelta(days=stale_days)
            healthy_count = 0
            for row in current_at_boundary:
                is_stale = row.valid_from < stale_cutoff
                missing_field = any(not (getattr(row, field) or "").strip() for field in MANDATORY_FIELDS)
                key = (row.customer_identity_key, row.target_customer_material_ref_key)
                is_duplicate = key_counts[key] > 1
                if not (is_stale or missing_field or is_duplicate):
                    healthy_count += 1

            trend.append(
                {
                    "month": month_start.strftime("%Y-%m"),
                    "total": len(current_at_boundary),
                    "healthy": healthy_count,
                }
            )
        return trend

    def get_housekeeping_audit_log(self, *, limit: int = 10) -> list[dict[str, Any]]:
        """Last `limit` cmir_record writes (create *and* refresh/supersede
        events, not just currently-active rows -- an audit log's whole point
        is showing history, so `is_current` is not filtered on here, unlike
        `get_health_snapshot`), for the CMIR Intelligence Module's
        "Housekeeping Audit Log" panel.

        Every column here is a real, traceable value -- no fabricated actor
        names or made-up outcomes:
        - ``action``: "Refresh" if some other row's `superseded_by_id`
          points at this one (i.e. this write followed a prior mapping for
          the same identity), else "Create" -- derived from real SCD2
          columns, not stored redundantly.
        - ``executed_by`` / ``outcome``: resolved by walking
          `cmir_record.purchase_order_line_id` / `.email_event_id` ->
          `workflow_thread_subject.workflow_thread_id` ->
          `human_action.actor` / `.decision` (most recent action on that
          thread). A cmir_record written on the touchless path (no human
          interrupt ever fired) has no matching human_action row -- both
          fields stay `None` rather than being guessed.
        """
        rows = self._session.scalars(
            select(CmirRecord).order_by(CmirRecord.valid_from.desc()).limit(limit)
        ).all()
        if not rows:
            return []

        row_ids = [row.id for row in rows]
        predecessor_of = set(
            self._session.scalars(
                select(CmirRecord.superseded_by_id).where(CmirRecord.superseded_by_id.in_(row_ids))
            ).all()
        )

        po_line_ids = [row.purchase_order_line_id for row in rows if row.purchase_order_line_id]
        email_event_ids = [row.email_event_id for row in rows if row.email_event_id]
        thread_id_by_po_line: dict[UUID, UUID] = {}
        thread_id_by_email_event: dict[UUID, UUID] = {}
        if po_line_ids or email_event_ids:
            # WorkflowThreadSubject carries a polymorphic subject_type/subject_id
            # pair, not a typed FK column per domain -- see
            # app/repositories/process/workflow.py's module docstring.
            subject_rows = self._session.execute(
                select(
                    WorkflowThreadSubject.workflow_thread_id,
                    WorkflowThreadSubject.subject_type,
                    WorkflowThreadSubject.subject_id,
                ).where(
                    (
                        (
                            WorkflowThreadSubject.subject_type
                            == WorkflowThreadSubjectType.PURCHASE_ORDER_LINE.value
                        )
                        & WorkflowThreadSubject.subject_id.in_(po_line_ids)
                    )
                    | (
                        (WorkflowThreadSubject.subject_type == WorkflowThreadSubjectType.EMAIL_EVENT.value)
                        & WorkflowThreadSubject.subject_id.in_(email_event_ids)
                    )
                )
            ).all()
            for subject in subject_rows:
                if subject.subject_type == WorkflowThreadSubjectType.PURCHASE_ORDER_LINE.value:
                    thread_id_by_po_line[subject.subject_id] = subject.workflow_thread_id
                elif subject.subject_type == WorkflowThreadSubjectType.EMAIL_EVENT.value:
                    thread_id_by_email_event[subject.subject_id] = subject.workflow_thread_id

        thread_ids = list({*thread_id_by_po_line.values(), *thread_id_by_email_event.values()})
        latest_action_by_thread: dict[UUID, dict[str, Any]] = {}
        if thread_ids:
            action_rows = self._session.execute(
                select(
                    HumanAction.workflow_thread_id,
                    HumanAction.actor,
                    HumanAction.decision,
                )
                .where(HumanAction.workflow_thread_id.in_(thread_ids))
                .order_by(HumanAction.responded_at.desc())
            ).all()
            for action in action_rows:
                # First hit per thread wins -- ordered by responded_at desc,
                # so this is always the most recent action on that thread.
                latest_action_by_thread.setdefault(
                    action.workflow_thread_id,
                    {"actor": action.actor, "decision": action.decision},
                )

        entries: list[dict[str, Any]] = []
        for row in rows:
            thread_id = (
                thread_id_by_po_line.get(row.purchase_order_line_id)
                if row.purchase_order_line_id
                else thread_id_by_email_event.get(row.email_event_id)
                if row.email_event_id
                else None
            )
            latest_action = latest_action_by_thread.get(thread_id) if thread_id else None
            entries.append(
                {
                    "id": str(row.id),
                    "timestamp": row.valid_from.isoformat(),
                    "action": "Refresh" if row.id in predecessor_of else "Create",
                    "customer_identity": row.customer_identity,
                    "target_grd_code": row.target_grd_code,
                    "target_customer_material_ref": row.target_customer_material_ref,
                    "executed_by": latest_action["actor"] if latest_action else None,
                    "outcome": latest_action["decision"] if latest_action else None,
                }
            )
        return entries

    def supersede_and_insert(
        self,
        *,
        customer_identity: str,
        target_customer_material_ref: str,
        merged: dict[str, Any],
        expected_current_id: UUID | None,
    ) -> UUID:
        """Retire the current row for this entity and insert `merged` as its replacement.

        `expected_current_id` must match the row that is actually current (`None`
        when none is expected yet); a mismatch raises `CmirVersionConflict` before
        anything is written. The old row is flushed to `is_current=False` first, so
        the partial unique index never sees two current rows for one entity; a
        concurrent writer that slips into that window hits the same index and also
        raises `CmirVersionConflict`. Returns the id of the new row.
        """
        customer_identity_key = normalize_identity_key(customer_identity)
        target_customer_material_ref_key = normalize_identity_key(target_customer_material_ref)

        current = self._session.scalars(
            select(CmirRecord).where(
                CmirRecord.customer_identity_key == customer_identity_key,
                CmirRecord.target_customer_material_ref_key == target_customer_material_ref_key,
                CmirRecord.is_current.is_(True),
            )
        ).first()
        current_id = current.id if current is not None else None
        if current_id != expected_current_id:
            raise CmirVersionConflict(
                f"Current record for customer_identity={customer_identity!r}, "
                f"target_customer_material_ref={target_customer_material_ref!r} changed: "
                f"expected current id={expected_current_id}, found id={current_id}."
            )

        if current is not None:
            # The partial unique index checks immediately rather than at commit, so
            # the old row must stop being current before the new one is inserted.
            # This flush forces the UPDATE ahead of the INSERT below, overriding
            # SQLAlchemy's default insert-before-update ordering.
            current.is_current = False
            current.valid_to = func.now()
            self._session.flush()

        new_record = CmirRecord(
            email_event_id=None,
            sender_type=merged["sender_type"],
            customer_identity=merged["customer_identity"],
            customer_identity_key=normalize_identity_key(merged["customer_identity"]),
            material_identity=merged["material_identity"],
            intent_phrase=merged.get("intent_phrase"),
            existing_cmir_ref=merged["existing_cmir_ref"],
            brand=merged["brand"],
            site=merged["site"],
            target_grd_code=merged["target_grd_code"],
            target_customer_material_ref=merged["target_customer_material_ref"],
            target_customer_material_ref_key=normalize_identity_key(merged["target_customer_material_ref"]),
            effective_date=merged.get("effective_date") or None,
            reason=merged.get("reason"),
            is_current=True,
        )
        self._session.add(new_record)
        try:
            self._session.flush()
        except IntegrityError as exc:
            # The partial unique index is the real correctness guarantee; the id
            # comparison above is a best-effort early exit for a clean error message.
            # A writer that won the race in between is caught here instead.
            self._session.rollback()
            raise CmirVersionConflict(
                f"Concurrent write detected for customer_identity={customer_identity!r}, "
                f"target_customer_material_ref={target_customer_material_ref!r} while "
                f"inserting the new version."
            ) from exc

        if current is not None:
            current.superseded_by_id = new_record.id
            self._session.flush()

        new_id = new_record.id
        logger.info(
            "Superseded cmir_record id=%s with new current id=%s for customer=%s material_ref=%s",
            current_id,
            new_id,
            customer_identity,
            target_customer_material_ref,
        )
        return new_id
