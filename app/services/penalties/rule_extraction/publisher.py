"""Turns an approved, review-ready extracted rule into a priceable rule, or a reason it cannot be.

Pure: no session, no network, no clock, no randomness.
"""

from datetime import date
from decimal import Decimal
from itertools import pairwise

from app.services.penalties.rule_extraction.types import (
    PublishedRule,
    PublishedTier,
    RejectedPublication,
    RejectionReason,
    StagedFact,
    StagedRule,
)
from app.services.penalties.rule_extraction.vocabulary import DURATION_APPLIES_PER, tier_bands_are_contiguous

_SUPPORTED_CALC_TYPES = {"PER_UNIT", "PERCENT_OF_PO", "FLAT_FEE", "TIERED"}
# `app.services.penalties.projection.types` prices only off COST_OF_GOODS, off
# SHORTFALL_VALUE, or off no basis at all; it has no concept of PO_VALUE, UNIT_COST, or
# SHORTFALL_UNITS. PO_VALUE and UNIT_COST both price off the order's full value in that
# single-unit-price engine, so they map onto COST_OF_GOODS unchanged. SHORTFALL_UNITS has
# no engine equivalent: mapping it onto SHORTFALL_VALUE would multiply a per-unit rate by
# unit_price a second time, so a rule carrying it is rejected rather than mispriced.
_BASIS_TYPE_MAP: dict[str | None, str | None] = {
    None: None,
    "NONE": None,
    "PO_VALUE": "COST_OF_GOODS",
    "UNIT_COST": "COST_OF_GOODS",
    "COST_OF_GOODS": "COST_OF_GOODS",
    "SHORTFALL_VALUE": "SHORTFALL_VALUE",
}
# A grace period is written straight into `penalty_rule.grace_period_days`, so any other
# unit would be read as days and understate the band by its own multiple.
_GRACE_PERIOD_UNITS = {"CALENDAR_DAYS", "BUSINESS_DAYS"}
# `app.services.penalties.projection.types.APPLIES_PER_DAY` is the only applies_per
# value the pricing engine accrues over: `price_delay_penalty` multiplies by day count
# only when it sees this exact value. Every other duration unit here (WEEK, MONTH,
# QUARTER, YEAR) would price as a single flat application instead of accruing, silently
# undercharging a per-week/month/quarter/year clause, so those stay rejected.
_SUPPORTED_DURATION_APPLIES_PER = {"DAY"}
# The metric names the measurement precisely; the PO flags only say which families the
# clause touches, and a category's governed defaults can legitimately raise both.
_VIOLATION_TYPE_BY_METRIC = {
    "FILL_RATE_PCT": "FILL_RATE",
    "SHORTFALL_PCT": "SHORT_SHIP",
    "SHORTFALL_QTY": "SHORT_SHIP",
    "OTIF_PCT": "OTIF_LATE",
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
        if not (staged.po_shortage_flag or staged.po_delay_flag):
            return RejectedPublication(
                RejectionReason.NOT_PO_SCOPED, "po_shortage_flag and po_delay_flag are both false"
            )
        return None

    def _build_rule_code(self, staged: StagedRule, retailer_code: str) -> str:
        """Deterministic from retailer, category, and clause fingerprint: same input, same code."""
        return f"{retailer_code}-{staged.penalty_category}-{staged.clause_fingerprint[:8]}"

    def _dominant_metric_code(self, facts: list[StagedFact]) -> str | None:
        """The rule-wide THRESHOLD's metric, else the lowest-branch fact that names one."""
        threshold = _find_fact(facts, "THRESHOLD", branch_no=0)
        if threshold is not None and threshold.metric_code is not None:
            return threshold.metric_code
        for fact in sorted(facts, key=lambda f: f.branch_no):
            if fact.metric_code is not None:
                return fact.metric_code
        return None

    def _check_mixed_currency(self, facts: list[StagedFact]) -> RejectedPublication | None:
        """A staged rule quoting more than one currency across its facts cannot be priced today."""
        currencies = {f.currency_code for f in facts if f.currency_code is not None}
        if len(currencies) > 1:
            return RejectedPublication(
                RejectionReason.MIXED_CURRENCY, f"facts carry currencies {sorted(currencies)}"
            )
        return None

    def _map_violation_type(self, staged: StagedRule) -> str:
        """The metric decides when it names one, since a category may raise both PO flags.

        Falling back to the flags would price a fill-rate clause as a delay whenever its
        category defaults both on, silently charging against the wrong measurement.
        """
        metric_code = self._dominant_metric_code(staged.facts)
        mapped = _VIOLATION_TYPE_BY_METRIC.get(metric_code or "")
        if mapped is not None:
            return mapped
        return "OTIF_LATE" if staged.po_delay_flag else "SHORT_SHIP"

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
        """RATE fact at one branch, converted, basis- and accrual-checked."""
        fact = _find_fact(facts, "RATE", branch_no=branch_no)
        if fact is None:
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
        basis_type = self._map_basis_type(fact.basis_type, calc_type)
        if isinstance(basis_type, RejectedPublication):
            return basis_type
        rate = _as_fraction(fact.value, fact.value_unit)
        return rate, basis_type, fact.currency_code, fact.applies_per

    def _map_basis_type(self, basis_type: str | None, calc_type: str) -> str | None | RejectedPublication:
        """Extraction basis onto the pricing engine's accepted set, or a rejection.

        A flat fee multiplies nothing, so `NONE` is its only coherent basis regardless
        of what else `_BASIS_TYPE_MAP` would otherwise accept.
        """
        if calc_type == "FLAT_FEE" and basis_type not in (None, "NONE"):
            return RejectedPublication(RejectionReason.UNSUPPORTED_BASIS, f"basis_type={basis_type!r}")
        if basis_type not in _BASIS_TYPE_MAP:
            return RejectedPublication(RejectionReason.UNSUPPORTED_BASIS, f"basis_type={basis_type!r}")
        return _BASIS_TYPE_MAP[basis_type]

    def _build_tiers(
        self, facts: list[StagedFact], rule_code: str, calc_type: str
    ) -> tuple[str | None, str | None, str | None, list[PublishedTier]] | RejectedPublication:
        """THRESHOLD facts at branch_no >= 1, ascending, into half-open bands with per-branch rates.

        A branch's THRESHOLD carries either an explicit upper bound (`operator=BETWEEN`,
        `value_max` set) or an open lower bound (`operator=GTE`) whose upper bound is the
        next branch's lower bound, or infinity for the last branch.
        """
        tier_facts = sorted(
            (f for f in facts if f.attribute_role == "THRESHOLD" and f.branch_no >= 1),
            key=lambda f: f.branch_no,
        )
        if not tier_facts:
            return RejectedPublication(
                RejectionReason.TIER_BAND_GAP, "calc_type=TIERED but no tier bands were staged"
            )

        bounds: list[tuple[Decimal, Decimal]] = []
        for i, fact in enumerate(tier_facts):
            if fact.tier_application == "MARGINAL":
                return RejectedPublication(
                    RejectionReason.MARGINAL_TIERS, f"branch_no={fact.branch_no}: tier_application=MARGINAL"
                )
            if fact.value is None:
                return RejectedPublication(
                    RejectionReason.NON_HALF_OPEN_TIERS, f"branch_no={fact.branch_no}: no band_min value"
                )
            band_min = _as_fraction(fact.value, fact.value_unit)
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
                    else Decimal("Infinity")
                )
            else:
                return RejectedPublication(
                    RejectionReason.NON_HALF_OPEN_TIERS,
                    f"branch_no={fact.branch_no}: operator={fact.operator!r} does not define a half-open band",
                )
            if band_max <= band_min:
                return RejectedPublication(
                    RejectionReason.NON_HALF_OPEN_TIERS,
                    f"branch_no={fact.branch_no}: band_max <= band_min",
                )
            bounds.append((band_min, band_max))

        for (_, prev_max), (next_min, _) in pairwise(bounds):
            if not tier_bands_are_contiguous(prev_max, next_min):
                return RejectedPublication(
                    RejectionReason.TIER_BAND_GAP, f"tier bands are not contiguous at {prev_max}"
                )

        basis_type: str | None = None
        currency_code: str | None = None
        applies_per: str | None = None
        tiers: list[PublishedTier] = []
        for fact, (band_min, band_max) in zip(tier_facts, bounds, strict=True):
            priced = self._map_rate(facts, branch_no=fact.branch_no, calc_type=calc_type)
            if isinstance(priced, RejectedPublication):
                return priced
            rate, basis_type, currency_code, applies_per = priced
            tiers.append(
                PublishedTier(
                    tier_code=f"{rule_code}-T{fact.branch_no}",
                    band_min=band_min,
                    band_max=band_max,
                    rate=rate,
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

        return PublishedRule(
            rule_code=rule_code,
            retailer_code=retailer_code,
            violation_type=self._map_violation_type(staged),
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
            tiers=tiers,
        )
