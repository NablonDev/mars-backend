"""Pure scenario definitions for the fulfillment-timeline simulator.

Every date field is an offset in days relative to the replay's end date `T`
(negative for a day before `T`), so the whole scenario catalog is anchored
once, by `simulator.replay`, rather than baked to fixed calendar dates.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class ScenarioLine:
    material_code: str
    plant_code: str
    quantity: float
    unit_price: float


@dataclass(frozen=True)
class ScenarioProductionOrder:
    material_code: str
    plant_code: str
    quantity: float
    planned_end_offset: int


@dataclass(frozen=True)
class ScenarioQualityLot:
    material_code: str
    plant_code: str
    quantity: float
    inspection_start_offset: int
    planned_release_offset: int


@dataclass(frozen=True)
class Disruption:
    day_offset: int
    kind: str
    reason_code: str
    milestone_code: str | None = None
    shift_days: int = 0


@dataclass(frozen=True)
class ScenarioDefinition:
    code: str
    retailer_code: str
    freight_term: str
    order_offset: int
    window_start_offset: int
    window_end_offset: int
    planned_transit_days: int
    lines: tuple[ScenarioLine, ...]
    initial_on_hand: dict[tuple[str, str], float] = field(default_factory=dict)
    production_orders: tuple[ScenarioProductionOrder, ...] = ()
    quality_lots: tuple[ScenarioQualityLot, ...] = ()
    disruptions: tuple[Disruption, ...] = ()
    cancel_date_offset: int | None = None


MILESTONE_REPLAN = "MILESTONE_REPLAN"
PRODUCTION_REPLAN = "PRODUCTION_REPLAN"
QA_HOLD = "QA_HOLD"
TRANSIT_DELAY = "TRANSIT_DELAY"

_PEDIGREE = "TL-MAT-PEDIGREE"
_TEMPTATIONS = "TL-MAT-TEMPTATIONS"
_ROYAL_CANIN = "TL-MAT-ROYALCANIN"
_GREENIES = "TL-MAT-GREENIES"
_CESAR = "TL-MAT-CESAR"
_SHEBA = "TL-MAT-SHEBA"
_JOPLIN = "TL-JOP"
_COLUMBUS = "TL-COL"

_AMAZON = "TL-AMAZON"
_WALMART = "TL-WALMART"
_COSTCO = "TL-COSTCO"
_TARGET = "TL-TARGET"

_PREPAID = "PREPAID"
_COLLECT = "COLLECT"


def _normal_prepaid(code: str, retailer_code: str, material_code: str, plant_code: str) -> ScenarioDefinition:
    return ScenarioDefinition(
        code=code,
        retailer_code=retailer_code,
        freight_term=_PREPAID,
        order_offset=-10,
        window_start_offset=-2,
        window_end_offset=2,
        planned_transit_days=2,
        lines=(ScenarioLine(material_code, plant_code, 100.0, 20.0),),
        initial_on_hand={(material_code, plant_code): 200.0},
    )


SCENARIOS: tuple[ScenarioDefinition, ...] = (
    _normal_prepaid("TL-S01", _AMAZON, _PEDIGREE, _JOPLIN),
    ScenarioDefinition(
        code="TL-S02",
        retailer_code=_AMAZON,
        freight_term=_PREPAID,
        order_offset=-10,
        window_start_offset=-2,
        window_end_offset=2,
        planned_transit_days=2,
        lines=(ScenarioLine(_PEDIGREE, _JOPLIN, 2000.0, 20.0),),
        initial_on_hand={(_PEDIGREE, _JOPLIN): 4000.0},
        disruptions=(
            Disruption(
                day_offset=-5,
                kind=MILESTONE_REPLAN,
                reason_code="WAVE_NOT_RELEASED",
                milestone_code="PICKED",
                shift_days=3,
            ),
        ),
    ),
    ScenarioDefinition(
        code="TL-S03",
        retailer_code=_WALMART,
        freight_term=_PREPAID,
        order_offset=-15,
        window_start_offset=5,
        window_end_offset=6,
        planned_transit_days=4,
        lines=(ScenarioLine(_TEMPTATIONS, _JOPLIN, 100.0, 18.0),),
        initial_on_hand={(_TEMPTATIONS, _JOPLIN): 200.0},
        disruptions=tuple(
            Disruption(
                day_offset=-10,
                kind=MILESTONE_REPLAN,
                reason_code="LOAD_PULLED_FORWARD",
                milestone_code=milestone_code,
                shift_days=-4,
            )
            for milestone_code in (
                "PICKED",
                "LOADED",
                "GOODS_ISSUED",
                "ASN_SENT",
                "APPOINTMENT_CONFIRMED",
                "DELIVERED",
            )
        ),
    ),
    ScenarioDefinition(
        code="TL-S04",
        retailer_code=_WALMART,
        freight_term=_PREPAID,
        order_offset=-19,
        window_start_offset=-2,
        window_end_offset=2,
        planned_transit_days=2,
        lines=(ScenarioLine(_TEMPTATIONS, _COLUMBUS, 100.0, 18.0),),
        initial_on_hand={(_TEMPTATIONS, _COLUMBUS): 0.0},
        quality_lots=(
            ScenarioQualityLot(
                material_code=_TEMPTATIONS,
                plant_code=_COLUMBUS,
                quantity=40.0,
                inspection_start_offset=-18,
                planned_release_offset=-16,
            ),
        ),
        disruptions=(
            Disruption(
                day_offset=-17,
                kind=QA_HOLD,
                reason_code="QA_HOLD",
                shift_days=20,
            ),
        ),
    ),
    ScenarioDefinition(
        code="TL-S05",
        retailer_code=_AMAZON,
        freight_term=_PREPAID,
        order_offset=-16,
        window_start_offset=-2,
        window_end_offset=2,
        planned_transit_days=2,
        lines=(ScenarioLine(_ROYAL_CANIN, _COLUMBUS, 100.0, 55.0),),
        initial_on_hand={(_ROYAL_CANIN, _COLUMBUS): 0.0},
        production_orders=(
            ScenarioProductionOrder(
                material_code=_ROYAL_CANIN, plant_code=_COLUMBUS, quantity=100.0, planned_end_offset=-8
            ),
        ),
        disruptions=(
            Disruption(
                day_offset=-14,
                kind=PRODUCTION_REPLAN,
                reason_code="PRODUCTION_DELAY",
                shift_days=6,
            ),
        ),
    ),
    ScenarioDefinition(
        code="TL-S06",
        retailer_code=_TARGET,
        freight_term=_PREPAID,
        order_offset=-10,
        window_start_offset=-2,
        window_end_offset=2,
        planned_transit_days=2,
        lines=(ScenarioLine(_GREENIES, _JOPLIN, 100.0, 28.0),),
        initial_on_hand={(_GREENIES, _JOPLIN): 200.0},
        disruptions=(
            Disruption(
                day_offset=-9,
                kind=MILESTONE_REPLAN,
                reason_code="TENDER_REJECTED",
                milestone_code="TENDER_ACCEPTED",
                shift_days=7,
            ),
        ),
    ),
    ScenarioDefinition(
        code="TL-S07",
        retailer_code=_WALMART,
        freight_term=_COLLECT,
        order_offset=-10,
        window_start_offset=-2,
        window_end_offset=2,
        planned_transit_days=2,
        lines=(ScenarioLine(_PEDIGREE, _COLUMBUS, 2000.0, 19.5),),
        initial_on_hand={(_PEDIGREE, _COLUMBUS): 4000.0},
        disruptions=(
            Disruption(
                day_offset=-5,
                kind=MILESTONE_REPLAN,
                reason_code="WAVE_NOT_RELEASED",
                milestone_code="PICKED",
                shift_days=3,
            ),
        ),
    ),
    ScenarioDefinition(
        code="TL-S08",
        retailer_code=_COSTCO,
        freight_term=_PREPAID,
        order_offset=-16,
        window_start_offset=-5,
        window_end_offset=-3,
        planned_transit_days=2,
        lines=(ScenarioLine(_PEDIGREE, _JOPLIN, 100.0, 19.5),),
        initial_on_hand={(_PEDIGREE, _JOPLIN): 200.0},
        disruptions=(
            Disruption(
                day_offset=-4,
                kind=MILESTONE_REPLAN,
                reason_code="WEATHER_DELAY",
                milestone_code="DELIVERED",
                shift_days=3,
            ),
        ),
    ),
    ScenarioDefinition(
        code="TL-S09",
        retailer_code=_AMAZON,
        freight_term=_PREPAID,
        order_offset=-10,
        window_start_offset=-2,
        window_end_offset=2,
        planned_transit_days=2,
        lines=(ScenarioLine(_PEDIGREE, _JOPLIN, 100.0, 19.5),),
        initial_on_hand={(_PEDIGREE, _JOPLIN): 200.0},
        disruptions=(
            Disruption(
                day_offset=-6,
                kind=MILESTONE_REPLAN,
                reason_code="NO_APPOINTMENT_SLOT",
                milestone_code="APPOINTMENT_CONFIRMED",
                shift_days=3,
            ),
        ),
    ),
    ScenarioDefinition(
        code="TL-S10A",
        retailer_code=_TARGET,
        freight_term=_PREPAID,
        order_offset=-10,
        window_start_offset=-2,
        window_end_offset=2,
        planned_transit_days=2,
        lines=(ScenarioLine(_GREENIES, _COLUMBUS, 60.0, 28.0),),
        initial_on_hand={(_GREENIES, _COLUMBUS): 60.0},
    ),
    ScenarioDefinition(
        code="TL-S10B",
        retailer_code=_TARGET,
        freight_term=_PREPAID,
        order_offset=-10,
        window_start_offset=-2,
        window_end_offset=2,
        planned_transit_days=2,
        lines=(ScenarioLine(_GREENIES, _COLUMBUS, 60.0, 28.0),),
        initial_on_hand={},
    ),
    ScenarioDefinition(
        code="TL-S11",
        retailer_code=_COSTCO,
        freight_term=_PREPAID,
        order_offset=-10,
        window_start_offset=-2,
        window_end_offset=10,
        planned_transit_days=2,
        lines=(ScenarioLine(_TEMPTATIONS, _JOPLIN, 100.0, 18.0),),
        initial_on_hand={(_TEMPTATIONS, _JOPLIN): 200.0},
        disruptions=(
            Disruption(
                day_offset=-5,
                kind=MILESTONE_REPLAN,
                reason_code="MINOR_PICK_DELAY",
                milestone_code="PICKED",
                shift_days=1,
            ),
        ),
    ),
    _normal_prepaid("TL-S12", _WALMART, _ROYAL_CANIN, _JOPLIN),
    ScenarioDefinition(
        code="TL-S13",
        retailer_code=_AMAZON,
        freight_term=_PREPAID,
        order_offset=-10,
        window_start_offset=-2,
        window_end_offset=2,
        planned_transit_days=2,
        lines=(ScenarioLine(_PEDIGREE, _JOPLIN, 100.0, 19.5),),
        initial_on_hand={(_PEDIGREE, _JOPLIN): 200.0},
        disruptions=(
            Disruption(
                day_offset=-3,
                kind=MILESTONE_REPLAN,
                reason_code="ASN_SUBMISSION_DELAY",
                milestone_code="ASN_SENT",
                shift_days=5,
            ),
        ),
    ),
    ScenarioDefinition(
        code="TL-S14",
        retailer_code=_TARGET,
        freight_term=_PREPAID,
        order_offset=-18,
        window_start_offset=-5,
        window_end_offset=25,
        cancel_date_offset=28,
        planned_transit_days=3,
        lines=(ScenarioLine(_CESAR, _COLUMBUS, 200.0, 22.0),),
        initial_on_hand={(_CESAR, _COLUMBUS): 500.0},
        disruptions=(
            Disruption(
                day_offset=-8,
                kind=MILESTONE_REPLAN,
                reason_code="WAVE_NOT_RELEASED",
                milestone_code="PICKED",
                shift_days=18,
            ),
        ),
    ),
    ScenarioDefinition(
        code="TL-S15",
        retailer_code=_COSTCO,
        freight_term=_PREPAID,
        order_offset=-15,
        window_start_offset=-2,
        window_end_offset=13,
        planned_transit_days=2,
        lines=(ScenarioLine(_SHEBA, _COLUMBUS, 100.0, 24.0),),
        initial_on_hand={(_SHEBA, _COLUMBUS): 300.0},
        disruptions=(
            Disruption(
                day_offset=-6,
                kind=MILESTONE_REPLAN,
                reason_code="TENDER_REJECTED",
                milestone_code="TENDER_ACCEPTED",
                shift_days=11,
            ),
        ),
    ),
    ScenarioDefinition(
        code="TL-S16",
        retailer_code=_AMAZON,
        freight_term=_PREPAID,
        order_offset=-10,
        window_start_offset=-2,
        window_end_offset=5,
        planned_transit_days=2,
        lines=(ScenarioLine(_CESAR, _JOPLIN, 150.0, 22.0),),
        initial_on_hand={(_CESAR, _JOPLIN): 500.0},
        disruptions=(
            Disruption(
                day_offset=-5,
                kind=MILESTONE_REPLAN,
                reason_code="NO_APPOINTMENT_SLOT",
                milestone_code="APPOINTMENT_CONFIRMED",
                shift_days=6,
            ),
        ),
    ),
    ScenarioDefinition(
        code="TL-S17",
        retailer_code=_WALMART,
        freight_term=_PREPAID,
        order_offset=-10,
        window_start_offset=2,
        window_end_offset=3,
        planned_transit_days=2,
        lines=(ScenarioLine(_SHEBA, _JOPLIN, 100.0, 24.0),),
        initial_on_hand={(_SHEBA, _JOPLIN): 300.0},
        disruptions=(
            Disruption(
                day_offset=-3,
                kind=MILESTONE_REPLAN,
                reason_code="TENDER_REJECTED",
                milestone_code="TENDER_ACCEPTED",
                shift_days=3,
            ),
        ),
    ),
)


def assert_unique_codes(scenarios: tuple[ScenarioDefinition, ...] = SCENARIOS) -> None:
    """Raise `ValueError` if two scenarios share a `code`."""
    codes = [s.code for s in scenarios]
    if len(codes) != len(set(codes)):
        duplicates = sorted({c for c in codes if codes.count(c) > 1})
        raise ValueError(f"Duplicate scenario codes: {duplicates}")
