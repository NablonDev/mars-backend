"""Repository for penalty_rule and penalty_rule_tier."""

from __future__ import annotations

from datetime import date
from uuid import UUID

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.core.exceptions import ValidationError
from app.models import PenaltyRule as PenaltyRuleModel
from app.models import PenaltyRuleTier as PenaltyRuleTierModel
from app.services.penalties.projection import CalcType
from app.services.penalties.projection import PenaltyRule as PenaltyRuleValue
from app.services.penalties.projection import PenaltyRuleTier as PenaltyRuleTierValue

_CALC_TYPE_MAP = {
    "PER_UNIT": CalcType.PER_UNIT,
    "PERCENT_OF_PO": CalcType.PERCENT_OF_PO,
    "FLAT_FEE": CalcType.FLAT_FEE,
    "TIERED": CalcType.TIERED,
}


def _rule_to_dict(r: PenaltyRuleModel) -> dict:
    """Serialize a PenaltyRuleModel row into a dict."""
    return {
        "id": r.id,
        "rule_code": r.rule_code,
        "retailer_id": r.retailer_id,
        "violation_type": r.violation_type,
        "calc_type": r.calc_type,
        "rate": float(r.rate),
        "threshold_pct": float(r.threshold_pct or 0.0),
        "cap_amount": float(r.cap_amount) if r.cap_amount is not None else None,
        "is_active": r.is_active,
        "grace_period_days": r.grace_period_days,
        "effective_start_date": r.effective_start_date,
        "effective_end_date": r.effective_end_date,
        "source_doc_reference": r.source_doc_reference,
        "basis_type": r.basis_type,
        "applies_per": r.applies_per,
        "currency_code": r.currency_code,
    }


class PenaltyRuleRepository:
    """Access layer for penalty_rule and penalty_rule_tier.

    Holds the retailer-specific penalty pricing rules the projection,
    mitigation, and dispute engines price violations against.
    """

    def __init__(self, session: Session) -> None:
        self._session = session

    def add_rule(
        self,
        rule_code: str,
        retailer_id: UUID,
        violation_type: str,
        calc_type: str,
        rate: float,
        threshold_pct: float = 0.0,
        cap_amount: float | None = None,
        grace_period_days: int = 0,
        effective_start_date: date | None = None,
        effective_end_date: date | None = None,
        source_doc_reference: str | None = None,
        tiers: list[dict] | None = None,
        basis_type: str | None = None,
        applies_per: str | None = None,
        currency_code: str = "USD",
    ) -> dict:
        """Create a penalty rule, along with its penalty_rule_tier rows for a TIERED rule.

        Defaults `effective_start_date` to 2026-01-01 when not given. Each
        entry in `tiers` gets a generated `tier_code` ("TIER-00", "TIER-01",
        ...) in the order supplied.
        """
        rule = PenaltyRuleModel(
            rule_code=rule_code,
            retailer_id=retailer_id,
            violation_type=violation_type,
            calc_type=calc_type,
            rate=rate,
            threshold_pct=threshold_pct,
            cap_amount=cap_amount,
            grace_period_days=grace_period_days,
            effective_start_date=effective_start_date or date(2026, 1, 1),
            effective_end_date=effective_end_date,
            source_doc_reference=source_doc_reference,
            basis_type=basis_type,
            applies_per=applies_per,
            currency_code=currency_code,
        )
        self._session.add(rule)
        self._session.flush()

        for i, tier in enumerate(tiers or []):
            self._session.add(
                PenaltyRuleTierModel(
                    rule_id=rule.id,
                    tier_code=f"TIER-{i:02d}",
                    band_min=tier["band_min"],
                    band_max=tier["band_max"],
                    rate=tier["rate"],
                )
            )

        self._session.flush()
        return _rule_to_dict(rule)

    def get_by_rule_code(self, rule_code: str) -> dict | None:
        """Return the penalty rule with this `rule_code`, or None; backs publish's republish guard."""
        row = self._session.scalars(
            select(PenaltyRuleModel).where(PenaltyRuleModel.rule_code == rule_code)
        ).first()
        return _rule_to_dict(row) if row is not None else None

    def list_rules_for_retailer(self, retailer_id: UUID) -> list[PenaltyRuleValue]:
        """Fetch a retailer's active rules as pure-engine `PenaltyRuleValue` objects.

        Tier bands are loaded for TIERED rules. Raises `ValidationError` when a rule's
        `calc_type` is not one of the known calc types.
        """
        rows = self._session.scalars(
            select(PenaltyRuleModel).where(
                PenaltyRuleModel.retailer_id == retailer_id,
                PenaltyRuleModel.is_active.is_(True),
            )
        ).all()

        rules: list[PenaltyRuleValue] = []

        for r in rows:
            calc_type = _CALC_TYPE_MAP.get(r.calc_type)
            if calc_type is None:
                raise ValidationError(
                    code="INVALID_PENALTY_RULE_DATA",
                    message=(
                        f"Rule {r.rule_code!r} has calc_type={r.calc_type!r}, which is not one of "
                        f"{sorted(_CALC_TYPE_MAP)}. Fix the row in penalty_rule."
                    ),
                )

            tiers = None
            if calc_type == CalcType.TIERED:
                tier_rows = self._session.scalars(
                    select(PenaltyRuleTierModel).where(PenaltyRuleTierModel.rule_id == r.id)
                ).all()

                tiers = [
                    PenaltyRuleTierValue(
                        band_min=float(t.band_min), band_max=float(t.band_max), rate=float(t.rate)
                    )
                    for t in tier_rows
                ]

            rules.append(
                PenaltyRuleValue(
                    # Must be the surrogate `penalty_rule.id`, not `rule_code`:
                    # `PenaltyProjectionRepository.save_result` writes this value
                    # straight into `penalty_projection.rule_id`, a UUID FK. The
                    # human-legible `rule_code` stays available on the dict rows.
                    rule_id=str(r.id),
                    violation_type=r.violation_type,
                    calc_type=calc_type,
                    rate=float(r.rate),
                    threshold_pct=float(r.threshold_pct or 0.0),
                    cap_amount=float(r.cap_amount) if r.cap_amount is not None else None,
                    tiers=tiers,
                    basis_type=r.basis_type,
                    applies_per=r.applies_per,
                )
            )
        return rules

    def list_rules_effective_on(self, retailer_id: UUID, as_of_date: date) -> list[dict]:
        """Return the rules in force for a retailer on a specific historical date.

        Adjudicating a past charge needs the rule that was effective on the charge
        date, so `is_active` is deliberately not filtered: only the date range is
        checked. Returns plain dicts rather than `PenaltyRuleValue` because the
        dispute engine also needs `grace_period_days`.
        """
        rows = self._session.scalars(
            select(PenaltyRuleModel).where(
                PenaltyRuleModel.retailer_id == retailer_id,
                PenaltyRuleModel.effective_start_date <= as_of_date,
                (PenaltyRuleModel.effective_end_date.is_(None))
                | (PenaltyRuleModel.effective_end_date >= as_of_date),
            )
        ).all()
        return [_rule_to_dict(r) for r in rows]

    def get_tiers_for_rule(self, rule_id: UUID) -> list[PenaltyRuleTierValue]:
        """Return the pure-engine tier bands for one rule id."""
        tier_rows = self._session.scalars(
            select(PenaltyRuleTierModel).where(PenaltyRuleTierModel.rule_id == rule_id)
        ).all()
        return [
            PenaltyRuleTierValue(band_min=float(t.band_min), band_max=float(t.band_max), rate=float(t.rate))
            for t in tier_rows
        ]

    def list_rules(self, retailer_id: UUID | None = None) -> list[dict]:
        """List penalty rules, optionally filtered to one retailer."""
        stmt = select(PenaltyRuleModel)
        if retailer_id:
            stmt = stmt.where(PenaltyRuleModel.retailer_id == retailer_id)

        rows = self._session.scalars(stmt).all()
        return [_rule_to_dict(r) for r in rows]

    def truncate_all(self) -> None:
        """Delete every rule and its tiers; penalty_projection must be cleared first."""
        self._session.execute(delete(PenaltyRuleTierModel))
        self._session.execute(delete(PenaltyRuleModel))
        self._session.flush()
