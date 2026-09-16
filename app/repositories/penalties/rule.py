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
        "engine_family": r.engine_family,
        "penalty_category": r.penalty_category,
        "metric_code": r.metric_code,
        "metric_denominator": r.metric_denominator,
        "retailer_agreement_id": r.retailer_agreement_id,
        "extracted_rule_id": r.extracted_rule_id,
        "measurement_window_type": r.measurement_window_type,
        "measurement_window_length": r.measurement_window_length,
        "measurement_window_unit": r.measurement_window_unit,
        "rounding_convention": r.rounding_convention,
        "is_engine_priceable": r.is_engine_priceable,
        "commitment_quantity": float(r.commitment_quantity) if r.commitment_quantity is not None else None,
        "commitment_value": float(r.commitment_value) if r.commitment_value is not None else None,
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
        penalty_category: str,
        retailer_agreement_id: UUID,
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
        engine_family: str | None = None,
        metric_code: str | None = None,
        metric_denominator: str | None = None,
        extracted_rule_id: UUID | None = None,
        measurement_window_type: str | None = None,
        measurement_window_length: int | None = None,
        measurement_window_unit: str | None = None,
        rounding_convention: str | None = None,
        is_engine_priceable: bool = True,
        commitment_quantity: float | None = None,
        commitment_value: float | None = None,
    ) -> dict:
        """Create a penalty rule, along with its penalty_rule_tier rows for a TIERED rule.

        Defaults `effective_start_date` to 2026-01-01 when not given. Each
        entry in `tiers` gets a generated `tier_code` ("TIER-00", "TIER-01",
        ...) in the order supplied. `retailer_agreement_id` must reference a real
        row: seed/demo data with no genuine uploaded contract points at a
        placeholder agreement instead of leaving this NULL.
        """
        rule = PenaltyRuleModel(
            rule_code=rule_code,
            retailer_id=retailer_id,
            violation_type=violation_type,
            calc_type=calc_type,
            rate=rate,
            penalty_category=penalty_category,
            retailer_agreement_id=retailer_agreement_id,
            threshold_pct=threshold_pct,
            cap_amount=cap_amount,
            grace_period_days=grace_period_days,
            effective_start_date=effective_start_date or date(2026, 1, 1),
            effective_end_date=effective_end_date,
            source_doc_reference=source_doc_reference,
            basis_type=basis_type,
            applies_per=applies_per,
            currency_code=currency_code,
            engine_family=engine_family,
            metric_code=metric_code,
            metric_denominator=metric_denominator,
            extracted_rule_id=extracted_rule_id,
            measurement_window_type=measurement_window_type,
            measurement_window_length=measurement_window_length,
            measurement_window_unit=measurement_window_unit,
            rounding_convention=rounding_convention,
            is_engine_priceable=is_engine_priceable,
            commitment_quantity=commitment_quantity,
            commitment_value=commitment_value,
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
                    tier_application=tier.get("tier_application", "CLIFF"),
                    tier_basis=tier.get("tier_basis", "SHORTFALL_PCT"),
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
        return [self._row_to_rule_value(r) for r in rows]

    def list_rules_for_agreement_by_family(
        self, retailer_agreement_id: UUID, engine_family: str
    ) -> list[PenaltyRuleValue]:
        """Fetch one retailer agreement's active rules in one `engine_family`, as pure engine values.

        Backs `ProjectionService.run_commitment_projection`: narrower than
        `list_rules_for_retailer`, which is retailer-wide and not agreement- or
        family-scoped.
        """
        rows = self._session.scalars(
            select(PenaltyRuleModel).where(
                PenaltyRuleModel.retailer_agreement_id == retailer_agreement_id,
                PenaltyRuleModel.engine_family == engine_family,
                PenaltyRuleModel.is_active.is_(True),
            )
        ).all()
        return [self._row_to_rule_value(r) for r in rows]

    def list_rules_effective_on(self, retailer_id: UUID, as_of_date: date) -> list[dict]:
        """Return the rules in force for a retailer on a specific historical date.

        Adjudicating a past charge needs the rule that was effective on the charge
        date, so `is_active` is deliberately not filtered: only the date range is
        checked. Returns plain dicts rather than `PenaltyRuleValue` because a
        caller matching on `violation_type` and tie-breaking on
        `effective_start_date` needs more than one candidate row at once; once a
        single rule id is settled on, `get_rule_value` converts it.
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

    def get_rule_value(self, rule_id: UUID) -> PenaltyRuleValue:
        """Fetch one rule by id as the engine's pure dataclass, tiers included for TIERED rules.

        The dispute engine's counterpart to `list_rules_for_retailer`'s per-row
        conversion, used once `list_rules_effective_on` and its tie-break have
        already settled on one rule id; sharing `_row_to_rule_value` is what
        keeps projection and dispute from disagreeing about a rule's fields (D1
        in `docs/architecture/extraction-engine-integration-plan.md`).
        """
        row = self._session.get(PenaltyRuleModel, rule_id)
        if row is None:
            raise ValueError(f"No penalty rule found with id={rule_id!r}")
        return self._row_to_rule_value(row)

    def get_tiers_for_rule(self, rule_id: UUID) -> list[PenaltyRuleTierValue]:
        """Return the pure-engine tier bands for one rule id."""
        tier_rows = self._session.scalars(
            select(PenaltyRuleTierModel).where(PenaltyRuleTierModel.rule_id == rule_id)
        ).all()
        return [
            PenaltyRuleTierValue(
                band_min=float(t.band_min),
                band_max=float(t.band_max) if t.band_max is not None else None,
                rate=float(t.rate),
                tier_application=t.tier_application,
                tier_basis=t.tier_basis,
            )
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

    def _row_to_rule_value(self, r: PenaltyRuleModel) -> PenaltyRuleValue:
        """Convert one ORM row into the engine's pure dataclass, tiers included for TIERED rules.

        Raises `ValidationError` when `calc_type` is not one of the known calc types.
        """
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
                    band_min=float(t.band_min),
                    band_max=float(t.band_max) if t.band_max is not None else None,
                    rate=float(t.rate),
                    tier_application=t.tier_application,
                    tier_basis=t.tier_basis,
                )
                for t in tier_rows
            ]

        return PenaltyRuleValue(
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
            currency_code=r.currency_code,
            grace_period_days=r.grace_period_days,
            engine_family=r.engine_family,
            rounding_convention=r.rounding_convention,
            measurement_window_type=r.measurement_window_type,
            measurement_window_length=r.measurement_window_length,
            measurement_window_unit=r.measurement_window_unit,
            commitment_quantity=float(r.commitment_quantity) if r.commitment_quantity is not None else None,
            commitment_value=float(r.commitment_value) if r.commitment_value is not None else None,
        )
