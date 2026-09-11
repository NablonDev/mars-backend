"""Repositories for extracted_penalty_rule, its attributes, and rule_publication."""

from __future__ import annotations

from decimal import Decimal
from typing import Any
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.exceptions import ValidationError
from app.models import ExtractedPenaltyRule, ExtractedPenaltyRuleAttribute, RulePublication
from app.services.penalties.rule_extraction.types import PublishedRule, StagedFact, StagedRule

_REVIEW_STATUSES = ("APPROVED", "REJECTED")


def published_rule_insert_kwargs(published: PublishedRule, retailer_id: UUID) -> dict[str, Any]:
    """Shape a `PublishedRule` into `PenaltyRuleRepository.add_rule`'s keyword arguments.

    `retailer_id` is the retailer's surrogate id, not on `PublishedRule` itself (which
    carries only `retailer_code`, the business key the publisher had to work with).
    """
    tiers = (
        [
            {"band_min": float(t.band_min), "band_max": float(t.band_max), "rate": float(t.rate)}
            for t in published.tiers
        ]
        if published.tiers
        else None
    )
    return {
        "rule_code": published.rule_code,
        "retailer_id": retailer_id,
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
    }


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
        contract_id: UUID,
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
            contract_id=contract_id,
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

    def list_for_contract(
        self,
        contract_id: UUID,
        status: str | None = None,
        agent_run_id: UUID | None = None,
    ) -> list[ExtractedPenaltyRule]:
        """Return extracted rules for one contract, optionally narrowed by status and/or run."""
        stmt = select(ExtractedPenaltyRule).where(ExtractedPenaltyRule.contract_id == contract_id)
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

    def list_publishable(self, contract_id: UUID, agent_run_id: UUID) -> list[StagedRule]:
        """Return the contract's APPROVED rules from one run as pure `StagedRule` value objects.

        The boundary between the ORM and `PenaltyRulePublisher`
        (`app/services/penalties/rule_extraction/publisher.py`), which the publisher reads
        instead of the `ExtractedPenaltyRule`/`ExtractedPenaltyRuleAttribute` models directly.
        """
        rows = self._session.scalars(
            select(ExtractedPenaltyRule).where(
                ExtractedPenaltyRule.contract_id == contract_id,
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
                    contract_id=str(rule.contract_id),
                    clause_fingerprint=rule.clause_fingerprint,
                    penalty_category=rule.penalty_category,
                    calc_type=rule.calc_type,
                    po_shortage_flag=rule.po_shortage_flag,
                    po_delay_flag=rule.po_delay_flag,
                    pricing_readiness=rule.pricing_readiness,
                    status=rule.status,
                    facts=[_to_staged_fact(a) for a in attribute_rows],
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

    def list_for_contract(self, contract_id: UUID) -> list[RulePublication]:
        """Return every publication outcome for a contract's extracted rules, oldest first."""
        rows = self._session.scalars(
            select(RulePublication)
            .join(ExtractedPenaltyRule, RulePublication.extracted_rule_id == ExtractedPenaltyRule.id)
            .where(ExtractedPenaltyRule.contract_id == contract_id)
            .order_by(RulePublication.created_at.asc())
        ).all()
        return list(rows)

    def reason_histogram(self, contract_id: UUID) -> dict[str, int]:
        """Count rejection reasons for a contract; the signal for which penalty shape to build next."""
        rows = self._session.execute(
            select(RulePublication.reason_code, func.count())
            .join(ExtractedPenaltyRule, RulePublication.extracted_rule_id == ExtractedPenaltyRule.id)
            .where(
                ExtractedPenaltyRule.contract_id == contract_id,
                RulePublication.outcome == "REJECTED",
            )
            .group_by(RulePublication.reason_code)
        ).all()
        return {reason_code: count for reason_code, count in rows if reason_code is not None}
