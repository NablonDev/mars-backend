"""Governed vocabularies for the penalty rule extraction schema.

Single source of truth for the value sets the extraction prompts, the LLM-facing
Pydantic models, and the database CHECK constraints all derive from, so a new
category cannot be added to one and forgotten in the others.
"""

from decimal import Decimal
from typing import ClassVar

PENALTY_CATEGORIES: tuple[str, ...] = (
    "SHORT_SHIP",
    "OTIF_LATE",
    "QUALITY_DEFECT_CHARGEBACK",
    "NON_CONFORMANCE_COST_RECOVERY",
    "DELIVERY_WINDOW_VIOLATION",
    "RECALL_COST_RECOVERY",
    "PRICE_PARITY_CLAWBACK",
    "LATE_PAYMENT_INTEREST",
    "OVERAGE_NONPAYMENT",
    "OVERAGE_CHARGEBACK",
    "MINIMUM_VOLUME_SHORTFALL",
    "DELIVERY_ACCEPTANCE_COST_SHIFT",
    "UNSPECIFIED_EXTERNAL",
    "UNSPECIFIED_INTERNAL",
    "ALTERNATE_SOURCING_MARKUP",
    "DEFECT_RECTIFICATION_COST_SHIFT",
    "AGGREGATE_LIABILITY_CAP",
    "EARLY_PAYMENT_DISCOUNT",
    "AUDIT_FINDING_PENALTY",
    "STORAGE_DURATION_FEE",
    "UNMAPPED",
)
CALC_TYPES: tuple[str, ...] = (
    "PER_UNIT",
    "PERCENT_OF_PO",
    "PERCENT_OF_INVOICE",
    "FLAT_FEE",
    "TIERED",
    "FORMULA_OTHER",
    "NON_MONETARY",
    "UNSPECIFIED",
    "LIMIT_ONLY",
)
ECONOMIC_EFFECT_TYPES: tuple[str, ...] = (
    "PENALTY",
    "CHARGEBACK",
    "ALLOWANCE",
    "DISCOUNT",
    "REFUND",
    "REIMBURSEMENT",
    "COST_RECOVERY",
    "INTEREST",
    "LIABILITY_CAP",
    "NON_MONETARY_REMEDY",
    "OTHER",
)
CONSEQUENCE_TYPES: tuple[str, ...] = (
    "DELIVERY_REFUSAL",
    "PRODUCT_REJECTION",
    "ORDER_SUSPENSION",
    "VENDOR_SUSPENSION",
    "TERMINATION_RIGHT",
    "REPLACEMENT_PURCHASE",
    "LOSS_OF_DISCOUNT",
    "REMEDIATION_REQUIRED",
    "SHORTFALL_QUANTITY_CANCELLED",
    "OTHER",
)
TAX_TREATMENTS: tuple[str, ...] = (
    "TAX_INCLUDED",
    "TAX_EXCLUDED",
    "TAX_ADDED",
    "NOT_SPECIFIED",
    "NOT_APPLICABLE",
)
SETTLEMENT_METHODS: tuple[str, ...] = (
    "INVOICE_DEDUCTION",
    "CREDIT_MEMO",
    "DIRECT_PAYMENT",
    "CHARGEBACK",
    "NETTING",
    "OTHER",
)
PARTY_SIDE_ROLES: tuple[str, ...] = ("RETAILER", "SUPPLIER", "EITHER")

SEGMENT_KEYS: tuple[str, ...] = (
    "product_classification",
    "fault_party",
    "occurrence_number",
    "termination_type",
    "defect_severity",
    "UNMAPPED",
)
CALENDAR_UNITS: tuple[str, ...] = ("DAYS", "WEEKS", "MONTHS", "QUARTERS", "YEARS")

ATTRIBUTE_ROLES: tuple[str, ...] = (
    "THRESHOLD",
    "RATE",
    "CAP",
    "FLOOR",
    "GRACE_PERIOD",
    "CURE_PERIOD",
    "TIME_WINDOW",
    "QUANTITY",
    "ESCALATION_FACTOR",
    "BASIS",
    "ROUNDING_RULE",
    "EXCLUSION_CONDITION",
    "OTHER",
)
METRIC_CODES: tuple[str, ...] = (
    "FILL_RATE_PCT",
    "OTIF_PCT",
    "SHORTFALL_PCT",
    "SHORTFALL_QTY",
    "OCCURRENCE_COUNT",
    "FORCE_MAJEURE",
    "DAMAGE_RATE_PCT",
    "NON_CONFORMANCE",
    "EXPIRED_UNSALABLE_PCT",
    "DELIVERY_FAILURE",
    "DELIVERY_WINDOW_VIOLATION",
    "RECALL",
    "PRICE_PARITY_BREACH",
    "LATE_PAYMENT",
    "OVERAGE",
    "PURCHASE_VOLUME",
    "DELIVERY_ACCEPTANCE",
    "DEFECT_RECTIFICATION",
    "EARLY_PAYMENT",
    "STORAGE_DURATION",
    "UNMAPPED",
)
# Ratio/percentage metrics for which "what population is this a percentage OF" is a
# different rule with a different dollar result: metric_denominator is required whenever
# metric_code is one of these, enforced both at the DB and in the Pydantic model.
RATIO_METRIC_CODES: tuple[str, ...] = (
    "FILL_RATE_PCT",
    "OTIF_PCT",
    "SHORTFALL_PCT",
    "DAMAGE_RATE_PCT",
    "EXPIRED_UNSALABLE_PCT",
)
METRIC_DENOMINATORS: tuple[str, ...] = (
    "ORDERED_QUANTITY",
    "CONFIRMED_QUANTITY",
    "INVOICED_QUANTITY",
    "RECEIVED_QUANTITY",
    "SHIPPED_QUANTITY",
    "INSPECTED_QUANTITY",
    "DELIVERED_QUANTITY",
    "COMMITTED_QUANTITY",
    "REQUESTED_QUANTITY",
    "ACCEPTED_QUANTITY",
    "ELIGIBLE_QUANTITY",
    "AFFECTED_QUANTITY",
    "OTHER",
)

OPERATORS: tuple[str, ...] = ("EQ", "GT", "GTE", "LT", "LTE", "BETWEEN", "ALWAYS")
VALUE_STATUSES: tuple[str, ...] = (
    "PRESENT",
    "NOT_APPLICABLE",
    "NOT_STATED",
    "REDACTED",
    "EXTERNAL_REFERENCE",
    "EXTRACTION_UNCERTAIN",
)
TRIGGER_LOGICS: tuple[str, ...] = ("AND", "OR", "NONE")

# HOURS/MINUTES cover appointment-window and other sub-day delay clauses ("must check
# in within 2 hours of the scheduled slot").
VALUE_UNITS: tuple[str, ...] = (
    "PERCENT",
    "BASIS_POINTS",
    "USD",
    "EUR",
    "GBP",
    "OTHER_CURRENCY",
    "CALENDAR_DAYS",
    "BUSINESS_DAYS",
    "HOURS",
    "MINUTES",
    "WEEKS",
    "MONTHS",
    "QUARTERS",
    "YEARS",
    "UNITS",
    "CASES",
    "PALLETS",
    "OCCURRENCES",
    "MULTIPLIER",
    "NONE",
)
APPLIES_PER: tuple[str, ...] = (
    "DAY",
    "WEEK",
    "MONTH",
    "QUARTER",
    "YEAR",
    "OCCURRENCE",
    "UNIT",
    "CASE",
    "PALLET",
    "TRAILER",
    "SHIPMENT",
    "PO",
    "PO_LINE",
    "SKU",
    "STORE",
    "DC",
    "LOCATION",
    "INVOICE",
    "INVOICE_LINE",
    "ASN",
    "TRUCKLOAD",
    "DELIVERY",
    "SQUARE_FOOT",
    "CUBIC_FOOT",
    "NONE",
)
# Fractional-period accrual units: a RATE row with one of these can land mid-period
# ("1% of Charges per week of delay" billed on 4 days) and needs a ROUNDING_RULE sibling
# to be mechanically computable; see extraction/consistency_checks.py.
DURATION_APPLIES_PER: tuple[str, ...] = ("DAY", "WEEK", "MONTH", "QUARTER", "YEAR")
BASIS_TYPES: tuple[str, ...] = (
    "PO_VALUE",
    "INVOICE_VALUE",
    "UNIT_COST",
    "WHOLESALE_PRICE",
    "RETAIL_PRICE",
    "SHORTFALL_UNITS",
    "SHORTFALL_VALUE",
    "COST_OF_GOODS",
    "PRICE_DIFFERENTIAL",
    "NONE",
    "OTHER",
)

TIER_APPLICATIONS: tuple[str, ...] = ("CLIFF", "MARGINAL", "NOT_APPLICABLE")
CAP_SCOPES: tuple[str, ...] = ("RATE_CEILING", "AMOUNT_CEILING", "DURATION_CEILING", "QUANTITY_CEILING")

WINDOW_TYPES: tuple[str, ...] = ("ROLLING", "FIXED_CALENDAR", "CONTRACT_YEAR", "ANNIVERSARY", "NONE")
EVENT_ANCHORS: tuple[str, ...] = (
    "SCHEDULED_DELIVERY_DATE",
    "REQUESTED_DELIVERY_DATE",
    "CONFIRMED_DELIVERY_DATE",
    "ACTUAL_DELIVERY_DATE",
    "RECEIPT_DATE",
    "INVOICE_DATE",
    "SHIPMENT_DATE",
    "PO_DATE",
    "ACCEPTANCE_DATE",
    "NOTIFICATION_DATE",
    "RECALL_DATE",
    "DEFECT_DISCOVERY_DATE",
    "PAYMENT_DUE_DATE",
    "OTHER",
)

EXTERNAL_REFERENCE_TYPES: tuple[str, ...] = (
    "INPUT_VALUE",
    "RATE",
    "FORMULA",
    "RULE",
    "POLICY",
    "SCHEDULE",
    "OTHER",
)
RESOLUTION_DIFFICULTIES: tuple[str, ...] = ("LOW", "MEDIUM", "HIGH")
COMBINATORS: tuple[str, ...] = ("MAX", "MIN", "SUM", "SUBTRACT", "NONE")
ROUNDING_CONVENTIONS: tuple[str, ...] = (
    "ROUND_UP_TO_PERIOD",
    "ROUND_DOWN_TO_PERIOD",
    "PRORATE_EXACT",
    "NEAREST_PERIOD",
)

# (code, description, default_po_shortage_flag, default_po_delay_flag). The two flags must
# agree with the category's scope role in `extraction.po_scope.CATEGORY_SCOPE`: scope decides
# whether a rule's numbers get extracted, the flags decide what a KPI query can find, and a
# rule quantified as a delivery-failure rule must not be invisible to `where po_delay_flag =
# true`.
CATEGORY_LOOKUP_ROWS: list[tuple[str, str, bool, bool]] = [
    ("SHORT_SHIP", "Per-PO quantity shortfall chargeback", True, False),
    ("OTIF_LATE", "On-time-in-full delivery failure penalty", True, True),
    ("QUALITY_DEFECT_CHARGEBACK", "Damage, defect, or unsalable-goods allowance/chargeback", False, False),
    ("NON_CONFORMANCE_COST_RECOVERY", "Cost passthrough for handling non-conforming product", False, False),
    ("DELIVERY_WINDOW_VIOLATION", "Delivery outside a permitted timing window (early or late)", False, True),
    ("RECALL_COST_RECOVERY", "Reimbursement of recall-related costs", False, False),
    ("PRICE_PARITY_CLAWBACK", "Most-favored-nation / price-parity refund obligation", False, False),
    ("LATE_PAYMENT_INTEREST", "Interest charged on overdue payments", False, False),
    ("OVERAGE_NONPAYMENT", "Non-monetary consequence for over-delivered quantity", False, False),
    ("OVERAGE_CHARGEBACK", "A genuine monetary fee for over-delivered quantity", False, False),
    ("MINIMUM_VOLUME_SHORTFALL", "Aggregate purchase-volume commitment shortfall", True, False),
    (
        "DELIVERY_ACCEPTANCE_COST_SHIFT",
        "Cost shift triggered by a delivery-acceptance timing event",
        False,
        True,
    ),
    (
        "UNSPECIFIED_EXTERNAL",
        "Rule referenced but stated in a separate document outside this contract",
        False,
        False,
    ),
    (
        "UNSPECIFIED_INTERNAL",
        "Rule referenced but not stated anywhere, including no external pointer",
        False,
        False,
    ),
    (
        "ALTERNATE_SOURCING_MARKUP",
        "Non-performing party still earns its markup on substitute goods/services",
        True,
        True,
    ),
    (
        "DEFECT_RECTIFICATION_COST_SHIFT",
        "Bidirectional defect cost allocation keyed on causation",
        False,
        False,
    ),
    ("AGGREGATE_LIABILITY_CAP", "Ceiling on total liability, pair with calc_type = LIMIT_ONLY", False, False),
    ("EARLY_PAYMENT_DISCOUNT", "Discount for paying ahead of the standard due date", False, False),
    ("AUDIT_FINDING_PENALTY", "Fee/refund triggered by an audit finding of non-compliance", False, False),
    (
        "STORAGE_DURATION_FEE",
        "Fee for storage beyond a free period, typically tiered by day-count",
        False,
        True,
    ),
    ("UNMAPPED", "Escape value, pair with penalty_category_unmapped_desc", False, False),
]


class CategoryDefaults:
    """Governed `po_shortage_flag` / `po_delay_flag` defaults, keyed by `penalty_category`."""

    _BY_CATEGORY: ClassVar[dict[str, dict[str, bool]]] = {
        code: {"po_shortage_flag": shortage, "po_delay_flag": delay}
        for code, _desc, shortage, delay in CATEGORY_LOOKUP_ROWS
    }

    @classmethod
    def for_category(cls, category: str | None) -> dict[str, bool] | None:
        """The governed PO flag defaults for one category, or `None` if it carries no default."""
        if category is None:
            return None
        return cls._BY_CATEGORY.get(category)


def tier_bands_are_contiguous(upper_bound: Decimal | float, next_lower_bound: Decimal | float) -> bool:
    """Whether one tier band's upper edge exactly meets the next band's lower edge.

    The one predicate both `consistency_checks` (a reviewer-facing warning on
    `group_no`) and `publisher` (a publication-blocking rejection on `branch_no`)
    call, so the two can never disagree about what counts as a tier gap.
    """
    return upper_bound == next_lower_bound
