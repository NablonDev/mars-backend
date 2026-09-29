"""Turns an approved, review-ready extracted rule into a priceable rule, or a reason it cannot be.

Pure: no session, no network, no clock, no randomness.
"""

from datetime import date
from decimal import Decimal
from itertools import pairwise
from typing import Any

from app.services.penalties.rule_extraction.types import (
    PublishedRule,
    PublishedTier,
    RejectedPublication,
    RejectionReason,
    StagedFact,
    StagedRule,
)
from app.services.penalties.rule_extraction.vocabulary import (
    DURATION_APPLIES_PER,
    ROUNDING_CONVENTIONS,
    CategoryDefaults,
    tier_bands_are_contiguous,
)

_SUPPORTED_CALC_TYPES = {"PER_UNIT", "PERCENT_OF_PO", "FLAT_FEE", "TIERED"}
# `app.services.penalties.projection.types` has no real cost-of-goods, wholesale, retail,
# or invoice figure. `basis_type` only changes the math for SHORTFALL_VALUE (a fill-rate-floor
# calculation) and SHORTFALL_UNITS (a flat per-unit rate); every other accepted
# value, PO_VALUE and COST_OF_GOODS included, prices off the PO's own order_qty * unit_price.
# `basis_type` is stored verbatim: this set is only a validation gate, not a
# translation table, and the engine decides what it can use. UNIT_COST, WHOLESALE_PRICE,
# RETAIL_PRICE, and PRICE_DIFFERENTIAL each name a real, different price the data model
# doesn't have, so storing them would misrepresent what the rule actually prices against;
# they're rejected instead.
_SUPPORTED_BASIS_TYPES = {
    None,
    "NONE",
    "PO_VALUE",
    "COST_OF_GOODS",
    "SHORTFALL_VALUE",
    "SHORTFALL_UNITS",
}
# Families projection/mitigation actually select today (`ProjectionEngine.project`,
# `SHORTAGE_VIOLATION_TYPES`/`DELAY_VIOLATION_TYPES`). `is_engine_priceable` is a stored
# snapshot of this, not derived at read time: it does NOT update itself, so this set (and every
# `penalty_rule.is_engine_priceable` value already written) must be reviewed and potentially
# updated whenever engine capability changes, e.g. a future family gaining a real pricing model.
_ENGINE_PRICEABLE_FAMILIES = {"SHORTAGE", "DELAY"}
# A grace period is written straight into `penalty_rule.grace_period_days`, so any other
# unit would be read as days and understate the band by its own multiple.
_GRACE_PERIOD_UNITS = {"CALENDAR_DAYS", "BUSINESS_DAYS"}
# Every `DURATION_APPLIES_PER` value now accrues in `price_delay_penalty` (DAY
# multiplies by day count directly, WEEK/MONTH/QUARTER/YEAR convert through
# `rounding_convention`), so this set matches `DURATION_APPLIES_PER` itself rather than
# narrowing it.
_SUPPORTED_DURATION_APPLIES_PER = set(DURATION_APPLIES_PER)
# The fractional-period subset of _SUPPORTED_DURATION_APPLIES_PER: `price_delay_penalty`'s
# `_accrual_periods` raises NotImplementedError, uncaught, inside `ProjectionEngine.project`'s
# per-PO loop for one of these without a governed `rounding_convention`. A TIERED rule ignores
# `applies_per`/`rounding_convention` entirely (it bands directly on the measure), so the
# requirement below is calc_type-specific, not a blanket one.
_FRACTIONAL_PERIOD_APPLIES_PER = {"WEEK", "MONTH", "QUARTER", "YEAR"}
# Fallback for a tier band whose fact states no metric_code and whose rule carries no
# dominant one either. Matches the migration's own backfill value for tier rows written
# before `metric_code` existed, and every tiered rule the engine actually prices today is a
# shortfall-percentage ladder.
_DEFAULT_TIER_BASIS = "SHORTFALL_PCT"
# The metric names the measurement precisely; the PO flags only say which families the
# clause touches, and a category's governed defaults can legitimately raise both.
_VIOLATION_TYPE_BY_METRIC = {
    "FILL_RATE_PCT": "FILL_RATE",
    "SHORTFALL_PCT": "SHORT_SHIP",
    "SHORTFALL_QTY": "SHORT_SHIP",
    "OTIF_PCT": "OTIF_LATE",
}
# Fallback for a rule whose metric doesn't name a family: the category's own governed
# violation_type. Every admitted category (see `CategoryDefaults.engine_family_for`) gets an
# entry; a category with no entry here has nothing to fall back to and rejects instead of
# being assigned to whichever family its PO flags happen to raise. Categories whose
# engine_family is UNPRICEABLE (UNSPECIFIED_EXTERNAL/INTERNAL, UNMAPPED) never reach this map:
# `_check_admission` rejects them first.
#
# Every value below other than SHORT_SHIP/OTIF_LATE is deliberately a new violation_type,
# distinct from `projection.types.SHORTAGE_VIOLATION_TYPES`/`DELAY_VIOLATION_TYPES`:
# extraction/publication admission is de-restricted from engine pricing support, so admitting
# a category here does not by itself widen what projection/mitigation can price. Reusing an
# existing engine-recognized value here would silently route a category through pricing math
# built for a different shape, misrouting it under a new name.
_VIOLATION_TYPE_BY_CATEGORY = {
    "SHORT_SHIP": "SHORT_SHIP",
    "OTIF_LATE": "OTIF_LATE",
    "DELIVERY_WINDOW_VIOLATION": "DELIVERY_WINDOW_VIOLATION",
    "DELIVERY_ACCEPTANCE_COST_SHIFT": "DELIVERY_ACCEPTANCE_COST_SHIFT",
    "QUALITY_DEFECT_CHARGEBACK": "QUALITY_DEFECT",
    "NON_CONFORMANCE_COST_RECOVERY": "QUALITY_DEFECT",
    "DEFECT_RECTIFICATION_COST_SHIFT": "QUALITY_DEFECT",
    "RECALL_COST_RECOVERY": "QUALITY_DEFECT",
    "ALTERNATE_SOURCING_MARKUP": "COVER_PURCHASE",
    "MINIMUM_VOLUME_SHORTFALL": "VOLUME_SHORTFALL",
    "OVERAGE_CHARGEBACK": "OVERAGE_CHARGEBACK",
    "OVERAGE_NONPAYMENT": "OVERAGE_NONPAYMENT",
    "STORAGE_DURATION_FEE": "STORAGE_DURATION",
    "AGGREGATE_LIABILITY_CAP": "LIABILITY_CAP",
    "PRICE_PARITY_CLAWBACK": "FINANCIAL_ADJUSTMENT",
    "LATE_PAYMENT_INTEREST": "FINANCIAL_ADJUSTMENT",
    "EARLY_PAYMENT_DISCOUNT": "FINANCIAL_ADJUSTMENT",
    "AUDIT_FINDING_PENALTY": "FINANCIAL_ADJUSTMENT",
}


def _find_fact(facts: list[StagedFact], role: str, branch_no: int | None = None) -> StagedFact | None:
    """First fact with the given role, optionally pinned to one branch."""
    for fact in facts:
        if fact.attribute_role == role and (branch_no is None or fact.branch_no == branch_no):
            return fact
    return None


def _as_fraction(value: Decimal, value_unit: str | None) -> Decimal:
    """A raw extracted number as a fraction: PERCENT divides by 100, everything else passes through."""
    if value_unit == "PERCENT":
        return value / Decimal(100)
    return value


class PenaltyRulePublisher:
    """Maps one `StagedRule` to a `PublishedRule` or a `RejectedPublication`. Holds no state."""

    def publish(
        self, staged: StagedRule, retailer_code: str, contract_effective_date: date
    ) -> PublishedRule | RejectedPublication:
        """Publish one approved, reviewed rule, or explain why it cannot be priced."""
        gate_rejection = self._check_admission(staged)
        if gate_rejection is not None:
            return gate_rejection
        return self._map(staged, retailer_code, contract_effective_date)

    def _check_admission(self, staged: StagedRule) -> RejectedPublication | None:
        """`agent_run_id` currency against the contract is the caller's job, not ours."""
        if staged.status != "APPROVED":
            return RejectedPublication(RejectionReason.NOT_APPROVED, f"status={staged.status!r}")
        if staged.pricing_readiness != "READY":
            return RejectedPublication(
                RejectionReason.NOT_READY, f"pricing_readiness={staged.pricing_readiness!r}"
            )
        engine_family = CategoryDefaults.engine_family_for(staged.penalty_category)
        if engine_family == "UNPRICEABLE":
            return RejectedPublication(
                RejectionReason.NOT_PO_SCOPED,
                f"penalty_category={staged.penalty_category!r} has engine_family=UNPRICEABLE",
            )
        return None

    def _build_rule_code(self, staged: StagedRule, retailer_code: str) -> str:
        """Deterministic from retailer, category, and clause fingerprint: same input, same code."""
        return f"{retailer_code}-{staged.penalty_category}-{staged.clause_fingerprint[:8]}"

    def _dominant_metric_fact(self, facts: list[StagedFact]) -> StagedFact | None:
        """The rule-wide THRESHOLD fact if it names a metric, else the lowest-branch fact that does."""
        threshold = _find_fact(facts, "THRESHOLD", branch_no=0)
        if threshold is not None and threshold.metric_code is not None:
            return threshold
        for fact in sorted(facts, key=lambda f: f.branch_no):
            if fact.metric_code is not None:
                return fact
        return None

    def _dominant_metric_code(self, facts: list[StagedFact]) -> str | None:
        """The rule-wide THRESHOLD's metric, else the lowest-branch fact that names one."""
        fact = self._dominant_metric_fact(facts)
        return fact.metric_code if fact is not None else None

    def _first_extra_value(self, facts: list[StagedFact], key: str) -> Any:
        """First non-None `extra[key]` across `facts`, in branch order; None if none is staged.

        `window_type`/`window_length`/`window_length_unit`/`rounding_convention` live in
        `StagedFact.extra` (see `ExtractedPenaltyRuleAttribute.extra`), not a typed column, and
        the extraction prompt does not pin them to one particular `attribute_role`.
        """
        for fact in sorted(facts, key=lambda f: f.branch_no):
            value = fact.extra.get(key)
            if value is not None:
                return value
        return None

    def _check_mixed_currency(self, facts: list[StagedFact]) -> RejectedPublication | None:
        """A staged rule quoting more than one currency across its facts cannot be priced today."""
        currencies = {f.currency_code for f in facts if f.currency_code is not None}
        if len(currencies) > 1:
            return RejectedPublication(
                RejectionReason.MIXED_CURRENCY, f"facts carry currencies {sorted(currencies)}"
            )
        return None

    def _map_violation_type(self, staged: StagedRule) -> str | RejectedPublication:
        """The metric decides when it names one; the category otherwise, only when it is itself a family.

        Falling back to the PO flags would price a fill-rate clause as a delay whenever its
        category defaults both on, silently charging against the wrong measurement; that is
        how MINIMUM_VOLUME_SHORTFALL and ALTERNATE_SOURCING_MARKUP were misrouted onto
        SHORT_SHIP and OTIF_LATE. Anything neither map recognizes rejects instead.
        """
        metric_code = self._dominant_metric_code(staged.facts)
        mapped = _VIOLATION_TYPE_BY_METRIC.get(metric_code or "")
        if mapped is not None:
            return mapped
        mapped = _VIOLATION_TYPE_BY_CATEGORY.get(staged.penalty_category)
        if mapped is not None:
            return mapped
        return RejectedPublication(
            RejectionReason.UNMAPPED_VIOLATION_TYPE,
            f"penalty_category={staged.penalty_category!r} metric_code={metric_code!r}",
        )

    def _map_threshold_pct(self, facts: list[StagedFact]) -> Decimal | RejectedPublication:
        """Rule-wide grace band from the branch-0 THRESHOLD fact; 0 when none is staged."""
        fact = _find_fact(facts, "THRESHOLD", branch_no=0)
        if fact is None or fact.value_status != "PRESENT" or fact.value is None:
            return Decimal(0)
        fraction = _as_fraction(fact.value, fact.value_unit)
        if not Decimal(0) <= fraction <= Decimal(1):
            return RejectedPublication(
                RejectionReason.THRESHOLD_OUT_OF_RANGE, f"threshold_pct={fraction} is outside [0, 1]"
            )
        return fraction

    def _map_cap(self, facts: list[StagedFact]) -> Decimal | None | RejectedPublication:
        """CAP fact's amount when it is an amount ceiling; None when there is no cap at all."""
        fact = _find_fact(facts, "CAP", branch_no=0)
        if fact is None:
            return None
        if fact.cap_scope != "AMOUNT_CEILING":
            return RejectedPublication(RejectionReason.NON_AMOUNT_CAP, f"cap_scope={fact.cap_scope!r}")
        if fact.value is None:
            return None
        return fact.value

    def _map_grace_period(self, facts: list[StagedFact]) -> int | RejectedPublication:
        """GRACE_PERIOD fact's day count; 0 when none is staged."""
        fact = _find_fact(facts, "GRACE_PERIOD", branch_no=0)
        if fact is None or fact.value_status != "PRESENT" or fact.value is None:
            return 0
        if fact.value_unit not in _GRACE_PERIOD_UNITS:
            return RejectedPublication(
                RejectionReason.UNSUPPORTED_ACCRUAL,
                f"grace period value_unit={fact.value_unit!r} is not a day count",
            )
        return int(fact.value)

    def _map_rate(
        self, facts: list[StagedFact], branch_no: int, calc_type: str
    ) -> tuple[Decimal, str | None, str | None, str | None] | RejectedPublication:
        """RATE fact at one branch, converted, basis- and accrual-checked.

        A clause can legitimately stage RATE facts on branches other than the expected one
        (e.g. per-category rates, each its own branch) when a rule-level number can't
        capture a rate that varies by an orthogonal dimension; that case rejects as
        `AMBIGUOUS_RATE_BRANCH` rather than the misleading `NO_RATE_VALUE`.
        """
        fact = _find_fact(facts, "RATE", branch_no=branch_no)
        if fact is None:
            other_branches = sorted({f.branch_no for f in facts if f.attribute_role == "RATE"})
            if other_branches:
                return RejectedPublication(
                    RejectionReason.AMBIGUOUS_RATE_BRANCH,
                    f"branch_no={branch_no}: no RATE fact there, but branch_no {other_branches} carry one",
                )
            return RejectedPublication(RejectionReason.NO_RATE_VALUE, f"branch_no={branch_no}: no RATE fact")
        if fact.value_status == "EXTERNAL_REFERENCE":
            return RejectedPublication(
                RejectionReason.EXTERNAL_FIGURE,
                f"branch_no={branch_no}: RATE value lives outside the contract",
            )
        if fact.value_status != "PRESENT" or fact.value is None:
            return RejectedPublication(
                RejectionReason.NO_RATE_VALUE, f"branch_no={branch_no}: RATE fact carries no number"
            )
        if (
            fact.applies_per in DURATION_APPLIES_PER
            and fact.applies_per not in _SUPPORTED_DURATION_APPLIES_PER
        ):
            return RejectedPublication(
                RejectionReason.UNSUPPORTED_ACCRUAL, f"applies_per={fact.applies_per!r}"
            )
        if (
            calc_type != "TIERED"
            and fact.applies_per in _FRACTIONAL_PERIOD_APPLIES_PER
            and self._first_extra_value(facts, "rounding_convention") not in ROUNDING_CONVENTIONS
        ):
            return RejectedPublication(
                RejectionReason.UNSUPPORTED_ACCRUAL,
                f"applies_per={fact.applies_per!r} has no supported rounding_convention",
            )
        basis_type = self._map_basis_type(fact.basis_type, calc_type)
        if isinstance(basis_type, RejectedPublication):
            return basis_type
        rate = _as_fraction(fact.value, fact.value_unit)
        return rate, basis_type, fact.currency_code, fact.applies_per

    def _map_basis_type(self, basis_type: str | None, calc_type: str) -> str | None | RejectedPublication:
        """Extraction basis stored verbatim once accepted, or a rejection.

        A flat fee multiplies nothing, so `NONE` is its only coherent basis regardless
        of what else `_SUPPORTED_BASIS_TYPES` would otherwise accept.
        """
        if calc_type == "FLAT_FEE" and basis_type not in (None, "NONE"):
            return RejectedPublication(RejectionReason.UNSUPPORTED_BASIS, f"basis_type={basis_type!r}")
        if basis_type not in _SUPPORTED_BASIS_TYPES:
            return RejectedPublication(RejectionReason.UNSUPPORTED_BASIS, f"basis_type={basis_type!r}")
        return basis_type

    def _build_tiers(
        self, facts: list[StagedFact], rule_code: str, calc_type: str
    ) -> tuple[str | None, str | None, str | None, list[PublishedTier]] | RejectedPublication:
        """THRESHOLD facts at branch_no >= 1, ascending, into half-open bands with per-branch rates.

        A branch's THRESHOLD carries either an explicit upper bound (`operator=BETWEEN`,
        `value_max` set) or an open lower bound (`operator=GTE`) whose upper bound is the
        next branch's lower bound, or `None` (unbounded) for the last branch.
        """
        tier_facts = sorted(
            (f for f in facts if f.attribute_role == "THRESHOLD" and f.branch_no >= 1),
            key=lambda f: f.branch_no,
        )
        if not tier_facts:
            return RejectedPublication(
                RejectionReason.TIER_BAND_GAP, "calc_type=TIERED but no tier bands were staged"
            )

        tier_applications = {fact.tier_application or "CLIFF" for fact in tier_facts}
        if len(tier_applications) > 1:
            return RejectedPublication(
                RejectionReason.MARGINAL_TIERS,
                f"tier bands mix tier_application values {sorted(tier_applications)}; "
                "shortage.price_tiered prices a whole rule under one value",
            )

        bounds: list[tuple[Decimal, Decimal | None]] = []
        for i, fact in enumerate(tier_facts):
            if fact.value is None:
                return RejectedPublication(
                    RejectionReason.NON_HALF_OPEN_TIERS, f"branch_no={fact.branch_no}: no band_min value"
                )
            band_min = _as_fraction(fact.value, fact.value_unit)
            band_max: Decimal | None
            if fact.operator == "BETWEEN":
                if fact.value_max is None:
                    return RejectedPublication(
                        RejectionReason.NON_HALF_OPEN_TIERS,
                        f"branch_no={fact.branch_no}: operator=BETWEEN with no value_max",
                    )
                band_max = _as_fraction(fact.value_max, fact.value_unit)
            elif fact.operator in ("GTE", "GT"):
                next_fact = tier_facts[i + 1] if i + 1 < len(tier_facts) else None
                band_max = (
                    _as_fraction(next_fact.value, next_fact.value_unit)
                    if next_fact is not None and next_fact.value is not None
                    else None
                )
            else:
                return RejectedPublication(
                    RejectionReason.NON_HALF_OPEN_TIERS,
                    f"branch_no={fact.branch_no}: operator={fact.operator!r} does not define a half-open band",
                )
            if band_max is not None and band_max <= band_min:
                return RejectedPublication(
                    RejectionReason.NON_HALF_OPEN_TIERS,
                    f"branch_no={fact.branch_no}: band_max <= band_min",
                )
            bounds.append((band_min, band_max))

        for (_, prev_max), (next_min, _) in pairwise(bounds):
            # Only the last band can be unbounded (band_max=None), and pairwise never
            # yields the last band as a `prev`, so `prev_max` is always a real bound here.
            assert prev_max is not None
            if not tier_bands_are_contiguous(prev_max, next_min):
                return RejectedPublication(
                    RejectionReason.TIER_BAND_GAP, f"tier bands are not contiguous at {prev_max}"
                )

        basis_type: str | None = None
        currency_code: str | None = None
        applies_per: str | None = None
        dominant_metric = self._dominant_metric_fact(facts)
        tiers: list[PublishedTier] = []
        for fact, (band_min, band_max) in zip(tier_facts, bounds, strict=True):
            priced = self._map_rate(facts, branch_no=fact.branch_no, calc_type=calc_type)
            if isinstance(priced, RejectedPublication):
                return priced
            rate, basis_type, currency_code, applies_per = priced
            tier_basis = (
                fact.metric_code
                or (dominant_metric.metric_code if dominant_metric is not None else None)
                or _DEFAULT_TIER_BASIS
            )
            tiers.append(
                PublishedTier(
                    tier_code=f"{rule_code}-T{fact.branch_no}",
                    band_min=band_min,
                    band_max=band_max,
                    rate=rate,
                    tier_application=fact.tier_application or "CLIFF",
                    tier_basis=tier_basis,
                )
            )
        return basis_type, currency_code, applies_per, tiers

    def _map(
        self, staged: StagedRule, retailer_code: str, contract_effective_date: date
    ) -> PublishedRule | RejectedPublication:
        calc_type = staged.calc_type
        if calc_type == "PERCENT_OF_INVOICE":
            return RejectedPublication(
                RejectionReason.PERCENT_OF_INVOICE, "no invoice value in the projection snapshot"
            )
        if calc_type not in _SUPPORTED_CALC_TYPES:
            return RejectedPublication(RejectionReason.UNSUPPORTED_CALC_TYPE, f"calc_type={calc_type!r}")

        violation_type = self._map_violation_type(staged)
        if isinstance(violation_type, RejectedPublication):
            return violation_type

        currency_rejection = self._check_mixed_currency(staged.facts)
        if currency_rejection is not None:
            return currency_rejection

        threshold_pct = self._map_threshold_pct(staged.facts)
        if isinstance(threshold_pct, RejectedPublication):
            return threshold_pct

        cap_amount = self._map_cap(staged.facts)
        if isinstance(cap_amount, RejectedPublication):
            return cap_amount

        grace_period_days = self._map_grace_period(staged.facts)
        if isinstance(grace_period_days, RejectedPublication):
            return grace_period_days

        rule_code = self._build_rule_code(staged, retailer_code)

        tiers: list[PublishedTier]
        if calc_type == "TIERED":
            tiered = self._build_tiers(staged.facts, rule_code, calc_type)
            if isinstance(tiered, RejectedPublication):
                return tiered
            basis_type, currency_code, applies_per, tiers = tiered
            rate = Decimal(0)
        else:
            priced = self._map_rate(staged.facts, branch_no=0, calc_type=calc_type)
            if isinstance(priced, RejectedPublication):
                return priced
            rate, basis_type, currency_code, applies_per = priced
            tiers = []

        engine_family = CategoryDefaults.engine_family_for(staged.penalty_category)
        metric_fact = self._dominant_metric_fact(staged.facts)

        return PublishedRule(
            rule_code=rule_code,
            retailer_code=retailer_code,
            violation_type=violation_type,
            calc_type=calc_type,
            rate=rate,
            threshold_pct=threshold_pct,
            cap_amount=cap_amount,
            grace_period_days=grace_period_days,
            basis_type=basis_type,
            currency_code=currency_code or "USD",
            applies_per=applies_per,
            effective_start_date=contract_effective_date,
            extracted_rule_id=staged.id,
            engine_family=engine_family,
            penalty_category=staged.penalty_category,
            retailer_agreement_id=staged.retailer_agreement_id,
            metric_code=metric_fact.metric_code if metric_fact is not None else None,
            metric_denominator=metric_fact.metric_denominator if metric_fact is not None else None,
            measurement_window_type=self._first_extra_value(staged.facts, "window_type"),
            measurement_window_length=self._first_extra_value(staged.facts, "window_length"),
            measurement_window_unit=self._first_extra_value(staged.facts, "window_length_unit"),
            rounding_convention=self._first_extra_value(staged.facts, "rounding_convention"),
            is_engine_priceable=engine_family in _ENGINE_PRICEABLE_FAMILIES,
            tiers=tiers,
        )
