"""Pricing adapter: turns one projected `TimingRisk` into a penalty amount.

Pure, framework-free calculation: reuses `price_delay_penalty` and
`price_shortage_penalty` (`app.services.penalties.projection.delay`/
`.shortage`) rather than re-implementing rule pricing, so a timing or
shortfall risk is priced by exactly the same math as the legacy
probability-weighted engine, just without the probability weighting.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from app.services.penalties.projection.delay import price_delay_penalty
from app.services.penalties.projection.shortage import price_shortage_penalty
from app.services.penalties.projection.types import PenaltyRule

_TIMING_RULE_TYPES = {
    "LATE": (("OTIF_LATE",), ("DELIVERY_WINDOW_VIOLATION",)),
    "EARLY": (("DELIVERY_WINDOW_VIOLATION",), ()),
    "ASN_LATE": (("ASN_LATE",), ()),
}
_SHORTAGE_VIOLATION_TYPES = ("SHORT_SHIP", "FILL_RATE")
_SHORTAGE_RISK_TYPES = {"SHORT", "NOT_DELIVERED"}


@dataclass(frozen=True)
class PricingBasis:
    quantity: float
    unit_cost: float | None
    unit_price: float


@dataclass(frozen=True)
class RuleCharge:
    """One rule considered while pricing a risk, for UI explainability (`calculation_detail`).

    Built for every rule `_select_rules` considers, amount included even when
    it prices to 0.0 (e.g. a grace-absorbed delay), so the UI can show why a
    rule didn't charge as well as why one did. `rule_code` is always `None`
    here: this module only ever sees the pure `PenaltyRule` (no `rule_code`
    field), so the caller (the timeline service) resolves and fills it in
    when serializing this into `calculation_detail`.
    """

    rule_id: str
    violation_type: str
    calc_type: str
    rate: float
    basis_type: str | None
    unit_amount: float
    quantity: float
    shortfall_quantity: float | None
    days_off: int | None
    grace_period_days: int
    chargeable_days: int | None
    amount: float
    rule_code: str | None = None


@dataclass(frozen=True)
class PricedRisk:
    risk_type: str
    status: str
    penalty_amount: float
    priced_rule_ids: tuple[str, ...]
    rule_breakdown: tuple[RuleCharge, ...] = ()


@dataclass(frozen=True)
class _PricingContext:
    """Per-rule inputs shared by `_price_one_rule` and its `RuleCharge` record."""

    unit_amount: float
    order_qty: int
    is_shortage: bool
    shortfall: float | None
    chargeable_days: int | None


def price_risk(
    risk_type: str,
    status: str,
    days_off: int | None,
    shortfall_quantity: float | None,
    basis: PricingBasis,
    rules: Sequence[PenaltyRule],
    stacking_mode: str,
) -> PricedRisk:
    """Price one projected risk against the retailer's applicable `PenaltyRule` rows.

    `days_off`/`shortfall_quantity` select the timing- or shortage-pricing path;
    `NOT_DELIVERED` always prices the full `basis.quantity` as the shortfall,
    ignoring `shortfall_quantity`. A `PROJECTED_BREACH` risk that prices to
    0.0 (inside grace, or no applicable rule matched) is reported back as
    `SLIPPING`; a `BREACHED` risk that prices to 0.0 stays `BREACHED`.
    """
    applicable_rules = _select_rules(risk_type, rules)
    charges = []
    raw_amounts = []
    for rule in applicable_rules:
        ctx = _resolve_context(risk_type, rule, days_off, shortfall_quantity, basis)
        raw_amount = _price_one_rule(rule, ctx)
        raw_amounts.append(raw_amount)
        charges.append(_build_rule_charge(rule, ctx, days_off, raw_amount))

    total = _combine(raw_amounts, stacking_mode)
    priced_rule_ids = tuple(
        charge.rule_id for charge, raw_amount in zip(charges, raw_amounts, strict=True) if raw_amount > 0
    )

    downgraded_status = "SLIPPING" if total == 0.0 and status == "PROJECTED_BREACH" else status
    return PricedRisk(
        risk_type=risk_type,
        status=downgraded_status,
        penalty_amount=round(total, 2),
        priced_rule_ids=priced_rule_ids,
        rule_breakdown=tuple(charges),
    )


def _select_rules(risk_type: str, rules: Sequence[PenaltyRule]) -> list[PenaltyRule]:
    """Filter `rules` to the ones whose `violation_type` applies to `risk_type`.

    `LATE` prefers `OTIF_LATE` rules and only falls back to
    `DELIVERY_WINDOW_VIOLATION` ones when no `OTIF_LATE` rule exists at all.
    """
    if risk_type in _SHORTAGE_RISK_TYPES:
        return [r for r in rules if r.violation_type in _SHORTAGE_VIOLATION_TYPES]

    primary_types, fallback_types = _TIMING_RULE_TYPES[risk_type]
    primary = [r for r in rules if r.violation_type in primary_types]
    if primary or not fallback_types:
        return primary
    return [r for r in rules if r.violation_type in fallback_types]


def _resolve_context(
    risk_type: str,
    rule: PenaltyRule,
    days_off: int | None,
    shortfall_quantity: float | None,
    basis: PricingBasis,
) -> _PricingContext:
    """Resolve the shared inputs one rule prices a risk against, and records for its `RuleCharge`."""
    unit_amount = _unit_amount(rule, basis)
    order_qty = round(basis.quantity)

    if risk_type in _SHORTAGE_RISK_TYPES:
        shortfall = basis.quantity if risk_type == "NOT_DELIVERED" else (shortfall_quantity or 0.0)
        return _PricingContext(
            unit_amount=unit_amount,
            order_qty=order_qty,
            is_shortage=True,
            shortfall=shortfall,
            chargeable_days=None,
        )

    chargeable_days = max(0, abs(days_off or 0) - rule.grace_period_days)
    return _PricingContext(
        unit_amount=unit_amount,
        order_qty=order_qty,
        is_shortage=False,
        shortfall=None,
        chargeable_days=chargeable_days,
    )


def _price_one_rule(rule: PenaltyRule, ctx: _PricingContext) -> float:
    """Price one rule against the risk, dispatching on timing vs. shortage."""
    if ctx.is_shortage:
        return price_shortage_penalty(rule, ctx.order_qty, ctx.unit_amount, ctx.shortfall or 0.0)
    if ctx.chargeable_days == 0:
        return 0.0
    return price_delay_penalty(rule, ctx.order_qty, ctx.unit_amount, ctx.chargeable_days or 0)


def _build_rule_charge(
    rule: PenaltyRule, ctx: _PricingContext, days_off: int | None, raw_amount: float
) -> RuleCharge:
    """Record one rule's inputs and priced amount, for `calculation_detail`'s pricing.rules."""
    return RuleCharge(
        rule_id=rule.rule_id,
        violation_type=rule.violation_type,
        calc_type=rule.calc_type.value,
        rate=rule.rate,
        basis_type=rule.basis_type,
        unit_amount=ctx.unit_amount,
        quantity=ctx.order_qty,
        shortfall_quantity=ctx.shortfall,
        days_off=None if ctx.is_shortage else days_off,
        grace_period_days=rule.grace_period_days,
        chargeable_days=ctx.chargeable_days,
        amount=round(raw_amount, 2),
    )


def _unit_amount(rule: PenaltyRule, basis: PricingBasis) -> float:
    """Resolve the per-unit price a rule prices against.

    `PO_VALUE`-basis rules price off the PO line price; every other rule
    prices off standard cost when known, falling back to the PO line price
    otherwise.
    """
    if rule.basis_type == "PO_VALUE":
        return basis.unit_price
    return basis.unit_cost if basis.unit_cost is not None else basis.unit_price


def _combine(amounts: Sequence[float], stacking_mode: str) -> float:
    """Combine each applicable rule's priced amount per `stacking_mode`.

    Validates `stacking_mode` even when no rule priced anything, so an
    invalid mode fails loudly rather than being masked by a risk with no
    applicable rules.
    """
    if stacking_mode not in ("SUM", "MAX"):
        raise ValueError(f"Unrecognized stacking_mode={stacking_mode!r}; expected 'SUM' or 'MAX'")
    amounts = list(amounts)
    if not amounts:
        return 0.0
    return sum(amounts) if stacking_mode == "SUM" else max(amounts)
