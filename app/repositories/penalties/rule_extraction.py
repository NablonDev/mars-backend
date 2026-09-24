"""Repositories for extracted penalty rules, publications, and reviewer revisions."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from app.core.exceptions import ValidationError
from app.models import ExtractedPenaltyRule, ExtractedPenaltyRuleAttribute, RulePublication
from app.models.penalties import ExtractedPenaltyRuleRevision
from app.services.penalties.rule_extraction.types import PublishedRule, StagedFact, StagedRule
from app.utils.sanitize import strip_nul_bytes

_REVIEW_STATUSES = ("APPROVED", "REJECTED")

_OPEN_REVISION_STATUSES = ("QUEUED", "RUNNING")

# The exact `ExtractedPenaltyRuleAttribute` columns a snapshot keeps, excluding the
# surrogate id, its FK back to the rule, and the timestamp columns from `TimestampMixin`.
_ATTRIBUTE_SNAPSHOT_COLUMNS = (
    "branch_no",
    "attribute_role",
    "metric_code",
    "metric_denominator",
    "operator",
    "value",
    "value_max",
    "value_unit",
    "value_status",
    "currency_code",
    "basis_type",
    "applies_per",
    "tier_application",
    "cap_scope",
    "source_text",
    "confidence",
)


def published_rule_insert_kwargs(published: PublishedRule) -> dict[str, Any]:
    """Shape a `PublishedRule` into `PenaltyRuleRepository.add_rule`'s keyword arguments."""
    tiers = (
        [
            {
                "band_min": float(t.band_min),
                "band_max": float(t.band_max) if t.band_max is not None else None,
                "rate": float(t.rate),
                "tier_application": t.tier_application,
                "tier_basis": t.tier_basis,
            }
            for t in published.tiers
        ]
        if published.tiers
        else None
    )
    return {
        "rule_code": published.rule_code,
        "violation_type": published.violation_type,
        "calc_type": published.calc_type,
        "rate": float(published.rate),
        "threshold_pct": float(published.threshold_pct),
        "cap_amount": float(published.cap_amount) if published.cap_amount is not None else None,
        "grace_period_days": published.grace_period_days,
        "effective_start_date": published.effective_start_date,
        "tiers": tiers,
        "basis_type": published.basis_type,
        "applies_per": published.applies_per,
        "currency_code": published.currency_code,
        "engine_family": published.engine_family,
        "penalty_category": published.penalty_category,
        "retailer_agreement_id": UUID(published.retailer_agreement_id),
        "extracted_rule_id": UUID(published.extracted_rule_id),
        "metric_code": published.metric_code,
        "metric_denominator": published.metric_denominator,
        "measurement_window_type": published.measurement_window_type,
        "measurement_window_length": published.measurement_window_length,
        "measurement_window_unit": published.measurement_window_unit,
        "rounding_convention": published.rounding_convention,
        "is_engine_priceable": published.is_engine_priceable,
    }


def _rule_to_dict(
    rule: ExtractedPenaltyRule, attribute_rows: Sequence[ExtractedPenaltyRuleAttribute]
) -> dict[str, Any]:
    """Shape an extracted rule and its attributes for the API response.

    `ExtractedPenaltyRule` has no ORM relationship to its attributes, so callers must
    provide the attribute rows explicitly.
    """
    return {
        "id": rule.id,
        "retailer_agreement_id": rule.retailer_agreement_id,
        "agent_run_id": rule.agent_run_id,
        "section": rule.section,
        "clause_text": rule.clause_text,
        "clause_fingerprint": rule.clause_fingerprint,
        "penalty_category": rule.penalty_category,
        "calc_type": rule.calc_type,
        "po_shortage_flag": rule.po_shortage_flag,
        "po_delay_flag": rule.po_delay_flag,
        "pricing_readiness": rule.pricing_readiness,
        "status": rule.status,
        "confidence": rule.confidence,
        "review_notes": rule.review_notes,
        "plain_explanation": (rule.extra or {}).get("plain_explanation"),
        "computed_summary": (rule.extra or {}).get("computed_summary"),
        "extra": dict(rule.extra or {}),
        "attributes": list(attribute_rows),
    }


def _attribute_snapshot(attribute: ExtractedPenaltyRuleAttribute) -> dict[str, Any]:
    """Shape one attribute row into JSON-safe snapshot data: every typed column plus `extra`."""
    row: dict[str, Any] = {}
    for column in _ATTRIBUTE_SNAPSHOT_COLUMNS:
        value = getattr(attribute, column)
        row[column] = float(value) if isinstance(value, Decimal) else value
    row["extra"] = dict(attribute.extra or {})
    return row


def rule_snapshot(rule: dict[str, Any]) -> dict[str, Any]:
    """Shape an extracted rule into JSON-safe revision snapshot data.

    `rule["attributes"]` contains ORM attribute rows rather than plain dictionaries.
    """
    confidence = rule.get("confidence")
    return {
        "penalty_category": rule.get("penalty_category"),
        "calc_type": rule.get("calc_type"),
        "pricing_readiness": rule.get("pricing_readiness"),
        "confidence": float(confidence) if confidence is not None else None,
        "po_shortage_flag": rule.get("po_shortage_flag"),
        "po_delay_flag": rule.get("po_delay_flag"),
        "review_notes": rule.get("review_notes"),
        "extra": dict(rule.get("extra") or {}),
        "attributes": [_attribute_snapshot(a) for a in rule.get("attributes") or []],
    }


def _revision_to_dict(row: ExtractedPenaltyRuleRevision) -> dict[str, Any]:
    """Shape one `extracted_penalty_rule_revision` row into the plain dict callers read."""
    return {
        "id": row.id,
        "extracted_rule_id": row.extracted_rule_id,
        "revision_no": row.revision_no,
        "instruction": row.instruction,
        "requested_by": row.requested_by,
        "status": row.status,
        "agent_reply": row.agent_reply,
        "before_snapshot": row.before_snapshot,
        "after_snapshot": row.after_snapshot,
        "error": row.error,
        "started_at": row.started_at,
        "completed_at": row.completed_at,
        "created_at": row.created_at,
        "updated_at": row.updated_at,
    }


def _stale_cutoff(session: Session, older_than: datetime) -> datetime:
    """Normalize a stale-revision cutoff for the bound database dialect.

    SQLite stores `DateTime(timezone=True)` values without timezone information, so a
    timezone-aware cutoff must be made naive before binding.
    """
    if older_than.tzinfo is not None and session.get_bind().dialect.name == "sqlite":
        return older_than.replace(tzinfo=None)
    return older_than


def _to_staged_fact(a: ExtractedPenaltyRuleAttribute) -> StagedFact:
    """Convert one ORM attribute row into the publisher's pure `StagedFact` value object."""
    return StagedFact(
        branch_no=a.branch_no,
        attribute_role=a.attribute_role,
        metric_code=a.metric_code,
        metric_denominator=a.metric_denominator,
        operator=a.operator,
        value=Decimal(str(a.value)) if a.value is not None else None,
        value_max=Decimal(str(a.value_max)) if a.value_max is not None else None,
        value_unit=a.value_unit,
        value_status=a.value_status,
        currency_code=a.currency_code,
        basis_type=a.basis_type,
        applies_per=a.applies_per,
        tier_application=a.tier_application,
        cap_scope=a.cap_scope,
        extra=dict(a.extra or {}),
    )


class ExtractedPenaltyRuleRepository:
    """Access layer for extracted_penalty_rule and extracted_penalty_rule_attribute.

    The pending-review staging area a contract upload populates and a reviewer clears,
    before `list_publishable` hands the approved rows to the pure publisher.
    """

    def __init__(self, session: Session) -> None:
        self._session = session

    def add_extracted_rule(
        self,
        retailer_agreement_id: UUID,
        agent_run_id: UUID,
        clause_text: str,
        clause_fingerprint: str,
        penalty_category: str,
        calc_type: str,
        pricing_readiness: str,
        confidence: float,
        section: str | None = None,
        po_shortage_flag: bool = False,
        po_delay_flag: bool = False,
        status: str = "PENDING_REVIEW",
        review_notes: str | None = None,
        extra: dict[str, Any] | None = None,
        attributes: list[dict[str, Any]] | None = None,
    ) -> ExtractedPenaltyRule:
        """Insert one extracted rule plus its attribute rows in a single call.

        The two always arrive together from one clause, so there is no method that
        inserts either without the other.
        """
        rule = ExtractedPenaltyRule(
            retailer_agreement_id=retailer_agreement_id,
            agent_run_id=agent_run_id,
            section=section,
            clause_text=clause_text,
            clause_fingerprint=clause_fingerprint,
            penalty_category=penalty_category,
            calc_type=calc_type,
            po_shortage_flag=po_shortage_flag,
            po_delay_flag=po_delay_flag,
            pricing_readiness=pricing_readiness,
            status=status,
            confidence=confidence,
            review_notes=review_notes,
            extra=extra or {},
        )
        self._session.add(rule)
        self._session.flush()

        for attribute in attributes or []:
            self._session.add(ExtractedPenaltyRuleAttribute(extracted_rule_id=rule.id, **attribute))
        self._session.flush()

        return rule

    def get_with_attributes(self, extracted_rule_id: UUID) -> dict[str, Any] | None:
        """Return one extracted rule plus its attribute rows, or None if it doesn't exist."""
        rule = self._session.get(ExtractedPenaltyRule, extracted_rule_id)
        if rule is None:
            return None
        attribute_rows = self._session.scalars(
            select(ExtractedPenaltyRuleAttribute)
            .where(ExtractedPenaltyRuleAttribute.extracted_rule_id == rule.id)
            .order_by(ExtractedPenaltyRuleAttribute.branch_no.asc())
        ).all()
        return _rule_to_dict(rule, attribute_rows)

    def list_for_retailer_agreement(
        self,
        retailer_agreement_id: UUID,
        status: str | None = None,
        agent_run_id: UUID | None = None,
    ) -> list[ExtractedPenaltyRule]:
        """Return extracted rules for one retailer agreement, optionally narrowed by status and/or run."""
        stmt = select(ExtractedPenaltyRule).where(
            ExtractedPenaltyRule.retailer_agreement_id == retailer_agreement_id
        )
        if status is not None:
            stmt = stmt.where(ExtractedPenaltyRule.status == status)
        if agent_run_id is not None:
            stmt = stmt.where(ExtractedPenaltyRule.agent_run_id == agent_run_id)
        return list(self._session.scalars(stmt).all())

    def set_review_decision(
        self,
        extracted_rule_id: UUID,
        status: str,
        review_notes: str | None = None,
    ) -> ExtractedPenaltyRule:
        """Record a reviewer's decision on one extracted rule.

        Raises `ValidationError` unless `status` is `APPROVED` or `REJECTED`, and
        `ValueError` if `extracted_rule_id` doesn't exist.
        """
        if status not in _REVIEW_STATUSES:
            raise ValidationError(
                code="INVALID_REVIEW_STATUS",
                message=f"status must be one of {_REVIEW_STATUSES}, got {status!r}.",
            )

        rule = self._session.get(ExtractedPenaltyRule, extracted_rule_id)
        if rule is None:
            raise ValueError(f"No extracted penalty rule found with id={extracted_rule_id!r}")

        rule.status = status
        rule.review_notes = review_notes
        self._session.flush()
        return rule

    def apply_revision(
        self,
        extracted_rule_id: UUID,
        *,
        penalty_category: str,
        calc_type: str,
        pricing_readiness: str,
        confidence: float,
        po_shortage_flag: bool,
        po_delay_flag: bool,
        review_notes: str | None,
        extra: dict[str, Any],
        attributes: list[dict[str, Any]],
    ) -> ExtractedPenaltyRule:
        """Replace one rule's classification and attributes with a revision.

        The replacement resets `status` to `PENDING_REVIEW` so the revised rule is
        reviewed against its new facts.
        """
        rule = self._session.get(ExtractedPenaltyRule, extracted_rule_id)
        if rule is None:
            raise ValueError(f"No extracted penalty rule found with id={extracted_rule_id!r}")

        rule.penalty_category = penalty_category
        rule.calc_type = calc_type
        rule.pricing_readiness = pricing_readiness
        rule.confidence = confidence
        rule.po_shortage_flag = po_shortage_flag
        rule.po_delay_flag = po_delay_flag
        rule.review_notes = review_notes
        rule.extra = extra
        rule.status = "PENDING_REVIEW"

        self._session.execute(
            delete(ExtractedPenaltyRuleAttribute).where(
                ExtractedPenaltyRuleAttribute.extracted_rule_id == extracted_rule_id
            )
        )
        for attribute in attributes:
            self._session.add(ExtractedPenaltyRuleAttribute(extracted_rule_id=rule.id, **attribute))
        self._session.flush()
        return rule

    def list_publishable(self, retailer_agreement_id: UUID, agent_run_id: UUID) -> list[StagedRule]:
        """Return approved rules from one run as `StagedRule` values.

        This method is the ORM boundary for `PenaltyRulePublisher`.
        """
        rows = self._session.scalars(
            select(ExtractedPenaltyRule).where(
                ExtractedPenaltyRule.retailer_agreement_id == retailer_agreement_id,
                ExtractedPenaltyRule.status == "APPROVED",
                ExtractedPenaltyRule.agent_run_id == agent_run_id,
            )
        ).all()

        staged_rules = []
        for rule in rows:
            attribute_rows = self._session.scalars(
                select(ExtractedPenaltyRuleAttribute)
                .where(ExtractedPenaltyRuleAttribute.extracted_rule_id == rule.id)
                .order_by(ExtractedPenaltyRuleAttribute.branch_no.asc())
            ).all()
            staged_rules.append(
                StagedRule(
                    id=str(rule.id),
                    retailer_agreement_id=str(rule.retailer_agreement_id),
                    clause_fingerprint=rule.clause_fingerprint,
                    penalty_category=rule.penalty_category,
                    calc_type=rule.calc_type,
                    po_shortage_flag=rule.po_shortage_flag,
                    po_delay_flag=rule.po_delay_flag,
                    pricing_readiness=rule.pricing_readiness,
                    status=rule.status,
                    facts=[_to_staged_fact(a) for a in attribute_rows],
                    extra=dict(rule.extra or {}),
                )
            )
        return staged_rules


class RulePublicationRepository:
    """Access layer for rule_publication, the append-only publication audit trail."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def record(
        self,
        extracted_rule_id: UUID,
        agent_run_id: UUID,
        outcome: str,
        penalty_rule_id: UUID | None = None,
        reason_code: str | None = None,
        reason_detail: str | None = None,
    ) -> RulePublication:
        """Write one audit row for a published or rejected extracted rule."""
        row = RulePublication(
            extracted_rule_id=extracted_rule_id,
            penalty_rule_id=penalty_rule_id,
            agent_run_id=agent_run_id,
            outcome=outcome,
            reason_code=reason_code,
            reason_detail=reason_detail,
        )
        self._session.add(row)
        self._session.flush()
        return row


class ExtractedPenaltyRuleRevisionRepository:
    """Access layer for extracted_penalty_rule_revision, the reviewer revision audit trail.

    Every method returns plain dicts, not ORM rows: the background task that runs a
    revision (`app.services.penalties.rule_extraction.revision.run_rule_revision`) opens
    a fresh scoped session per step, so a caller must not hold a row across a session
    boundary where it would go detached.
    """

    def __init__(self, session: Session) -> None:
        self._session = session

    def create(
        self,
        extracted_rule_id: UUID,
        *,
        instruction: str,
        requested_by: str | None,
        before_snapshot: dict[str, Any],
    ) -> dict[str, Any]:
        """Open one new revision for a rule, numbering it one past the highest existing revision_no."""
        max_revision_no = self._session.scalar(
            select(func.max(ExtractedPenaltyRuleRevision.revision_no)).where(
                ExtractedPenaltyRuleRevision.extracted_rule_id == extracted_rule_id
            )
        )
        row = ExtractedPenaltyRuleRevision(
            extracted_rule_id=extracted_rule_id,
            revision_no=(max_revision_no or 0) + 1,
            instruction=instruction,
            requested_by=requested_by,
            status="QUEUED",
            before_snapshot=before_snapshot,
        )
        self._session.add(row)
        self._session.flush()
        return _revision_to_dict(row)

    def get(self, revision_id: UUID) -> dict[str, Any] | None:
        """Fetch one revision by id, or None if it doesn't exist."""
        row = self._session.get(ExtractedPenaltyRuleRevision, revision_id)
        return _revision_to_dict(row) if row is not None else None

    def list_for_rule(self, extracted_rule_id: UUID) -> list[dict[str, Any]]:
        """List every revision requested for one rule, oldest first."""
        rows = self._session.scalars(
            select(ExtractedPenaltyRuleRevision)
            .where(ExtractedPenaltyRuleRevision.extracted_rule_id == extracted_rule_id)
            .order_by(ExtractedPenaltyRuleRevision.revision_no.asc())
        ).all()
        return [_revision_to_dict(row) for row in rows]

    def latest_for_rules(self, rule_ids: Sequence[UUID]) -> dict[UUID, dict[str, Any]]:
        """Return each rule's highest-`revision_no` revision, keyed by `extracted_rule_id`, in one query."""
        if not rule_ids:
            return {}
        rows = self._session.scalars(
            select(ExtractedPenaltyRuleRevision)
            .where(ExtractedPenaltyRuleRevision.extracted_rule_id.in_(rule_ids))
            .order_by(
                ExtractedPenaltyRuleRevision.extracted_rule_id,
                ExtractedPenaltyRuleRevision.revision_no.desc(),
            )
        ).all()
        latest: dict[UUID, dict[str, Any]] = {}
        for row in rows:
            if row.extracted_rule_id not in latest:
                latest[row.extracted_rule_id] = _revision_to_dict(row)
        return latest

    def open_for_rule(self, extracted_rule_id: UUID) -> dict[str, Any] | None:
        """Return a rule's open (QUEUED or RUNNING) revision, the highest-numbered one if more than one."""
        row = self._session.scalars(
            select(ExtractedPenaltyRuleRevision)
            .where(
                ExtractedPenaltyRuleRevision.extracted_rule_id == extracted_rule_id,
                ExtractedPenaltyRuleRevision.status.in_(_OPEN_REVISION_STATUSES),
            )
            .order_by(ExtractedPenaltyRuleRevision.revision_no.desc())
        ).first()
        return _revision_to_dict(row) if row is not None else None

    def mark_running(self, revision_id: UUID) -> None:
        """Move one revision from QUEUED to RUNNING and stamp `started_at`."""
        row = self._session.get(ExtractedPenaltyRuleRevision, revision_id)
        if row is None:
            return
        row.status = "RUNNING"
        row.started_at = func.now()
        self._session.flush()

    def mark_completed(
        self, revision_id: UUID, *, agent_reply: str | None, after_snapshot: dict[str, Any]
    ) -> None:
        """Move one revision to COMPLETED, recording the agent's reply and the rule's new snapshot."""
        row = self._session.get(ExtractedPenaltyRuleRevision, revision_id)
        if row is None:
            return
        row.status = "COMPLETED"
        row.agent_reply = agent_reply
        row.after_snapshot = after_snapshot
        row.completed_at = func.now()
        self._session.flush()

    def mark_failed(self, revision_id: UUID, *, error: str) -> None:
        """Move one revision to FAILED, recording a sanitized, length-capped error message."""
        row = self._session.get(ExtractedPenaltyRuleRevision, revision_id)
        if row is None:
            return
        row.status = "FAILED"
        row.error = strip_nul_bytes(error)[:2000]
        row.completed_at = func.now()
        self._session.flush()

    def fail_stale(self, extracted_rule_id: UUID | None, *, older_than: datetime) -> int:
        """Fail every open revision (optionally scoped to one rule) idle since before `older_than`."""
        cutoff = _stale_cutoff(self._session, older_than)
        stmt = select(ExtractedPenaltyRuleRevision).where(
            ExtractedPenaltyRuleRevision.status.in_(_OPEN_REVISION_STATUSES),
            ExtractedPenaltyRuleRevision.updated_at < cutoff,
        )
        if extracted_rule_id is not None:
            stmt = stmt.where(ExtractedPenaltyRuleRevision.extracted_rule_id == extracted_rule_id)

        rows = self._session.scalars(stmt).all()
        for row in rows:
            row.status = "FAILED"
            row.error = "Revision timed out."
            row.completed_at = func.now()
        if rows:
            self._session.flush()
        return len(rows)
