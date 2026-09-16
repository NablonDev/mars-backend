"""Tests for PoDeliveryChangeRequestService: create/accept/
counter/reject/expire, and one full-lifecycle test (create -> accept ->
PurchaseOrder.current_delivery_date updated -> a fresh PenaltyProjection
reflects the new date) against the SQLite test DB.

Was against `PoDeliveryChangeRequestService`/business-string `order_id`;
rewritten against `PoDeliveryChangeRequestService` and the
`common`/`penalties` repositories, keyed by the UUID surrogate
`purchase_order_id`.

Dates are computed relative to date.today() (not hardcoded, unlike the
seeded demo orders in app/services/seeding/, which are fixed in 2026-08
and would otherwise drift stale relative to the lead-time gate as real
time passes).
"""

from datetime import date, datetime, timedelta
from uuid import uuid4

import pytest

from app.core.exceptions import BusinessRuleError, ConflictError, NotFoundError, ValidationError
from app.services.penalties.delivery_change import PoDeliveryChangeRequestService
from app.services.penalties.projection.service import ProjectionService
from tests.conftest import make_retailer_agreement

_ORDER_QTY = 1000
_UNIT_PRICE = 10.0


def _build_service(repos) -> PoDeliveryChangeRequestService:
    projection_service = ProjectionService(
        purchase_orders=repos.purchase_orders,
        fulfillment=repos.fulfillment,
        rules=repos.penalty_rules,
        master_data=repos.master_data,
        projections=repos.penalty_projections,
    )
    return PoDeliveryChangeRequestService(
        purchase_orders=repos.purchase_orders,
        delivery_change_requests=repos.delivery_change_requests,
        projection_service=projection_service,
        master_data=repos.master_data,
    )


def _seed_order(
    repos,
    po_number: str,
    *,
    required_ship_date: date,
    requested_delivery_date: date,
    retailer_code: str = "RET-EXT",
    extension_min_lead_days: int = 2,
    extension_response_sla_hours: int = 48,
    extension_penalty_threshold: float = 0.0,
):
    """Every test gets a fresh function-scoped `repos`/`database` fixture
    (see tests/conftest.py), so this always seeds fresh reference data --
    no exists-check needed. material/plant codes are derived from
    retailer_code so tests that seed more than one retailer in a single
    test function (e.g. the per-retailer-policy and
    negotiation-status-lifecycle tests) don't collide on a shared natural
    key. Returns the purchase order's UUID id."""
    material_code = f"MAT-{retailer_code}"
    plant_code = f"PLANT-{retailer_code}"
    retailer = repos.master_data.add_retailer(
        retailer_code,
        "Extension Test Retailer",
        None,
        "SUM",
        None,
        extension_min_lead_days,
        extension_response_sla_hours,
        extension_penalty_threshold,
    )
    material = repos.master_data.add_material(material_code, None)
    plant = repos.master_data.add_plant(plant_code, None, None)
    repos.penalty_rules.add_rule(
        rule_code=f"RULE-{retailer_code}",
        retailer_id=retailer["id"],
        violation_type="OTIF_LATE",
        penalty_category="OTIF_LATE",
        retailer_agreement_id=make_retailer_agreement(repos, retailer["id"]),
        calc_type="FLAT_FEE",
        rate=25.0,
    )

    purchase_order = repos.purchase_orders.create_purchase_order(
        purchase_order_number=po_number,
        retailer_id=retailer["id"],
        order_date=date.today(),  # noqa: DTZ011 -- test date anchor, not a naive-datetime bug (see module docstring)
        requested_delivery_date=requested_delivery_date,
        required_ship_date=required_ship_date,
    )
    repos.purchase_orders.add_line(
        purchase_order_id=purchase_order["id"],
        line_number="10",
        ordered_quantity=_ORDER_QTY,
        unit_price=_UNIT_PRICE,
        material_id=material["id"],
        plant_id=plant["id"],
    )
    return purchase_order["id"]


def test_create_request_success(repos):
    today = date.today()  # noqa: DTZ011 -- test date anchor, not a naive-datetime bug (see module docstring)
    purchase_order_id = _seed_order(
        repos,
        "ORD-EXT-1",
        required_ship_date=today + timedelta(days=10),
        requested_delivery_date=today + timedelta(days=12),
    )
    service = _build_service(repos)

    row = service.create_request(
        purchase_order_id, "DELAY", today + timedelta(days=16), notes="ops requested more time"
    )

    assert row["status"] == "PENDING"
    assert row["purchase_order_id"] == purchase_order_id
    assert row["reason_code"] == "DELAY"
    assert row["proposed_delivery_date"] == today + timedelta(days=16)
    assert row["baseline_delivery_date"] == today + timedelta(days=12)
    assert row["expires_at"] - row["requested_at"] == timedelta(hours=48)
    # request_id is a server-generated external-system correlation key --
    # not API-visible (the surrogate `id: UUID` is the sole public
    # identifier), but still persisted for future reconciliation. Generated via
    # `new_id("ext")` in `PoDeliveryChangeRequestService.create_request`.
    assert row["request_id"].startswith("ext_")
    assert len(row["request_id"]) == len("ext_") + 12


def test_create_request_rejects_duplicate_active(repos):
    today = date.today()  # noqa: DTZ011 -- test date anchor, not a naive-datetime bug (see module docstring)
    purchase_order_id = _seed_order(
        repos,
        "ORD-EXT-2",
        required_ship_date=today + timedelta(days=10),
        requested_delivery_date=today + timedelta(days=12),
    )
    service = _build_service(repos)
    service.create_request(purchase_order_id, "DELAY", today + timedelta(days=16))

    with pytest.raises(ConflictError):
        service.create_request(purchase_order_id, "DELAY", today + timedelta(days=17))


def test_create_request_rejects_insufficient_lead_time(repos):
    today = date.today()  # noqa: DTZ011 -- test date anchor, not a naive-datetime bug (see module docstring)
    purchase_order_id = _seed_order(
        repos,
        "ORD-EXT-3",
        required_ship_date=today,  # 0 days lead, default extension_min_lead_days=2
        requested_delivery_date=today + timedelta(days=2),
    )
    service = _build_service(repos)

    with pytest.raises(BusinessRuleError):
        service.create_request(purchase_order_id, "SHORTAGE", today + timedelta(days=6))


def test_create_request_uses_per_retailer_policy(repos):
    """Two retailers with different extension_min_lead_days/response_sla_hours
    get different behavior from the service -- proves the policy is read
    per-retailer from Retailer via MasterDataRepository.get_extension_policy,
    not hardcoded/global (Settings has no such fields at all)."""
    today = date.today()  # noqa: DTZ011 -- test date anchor, not a naive-datetime bug (see module docstring)
    service = _build_service(repos)

    # Retailer A: relaxed policy -- 1 day lead time is enough, 24h SLA.
    purchase_order_a = _seed_order(
        repos,
        "ORD-EXT-POLICY-A",
        required_ship_date=today + timedelta(days=1),
        requested_delivery_date=today + timedelta(days=3),
        retailer_code="RET-POLICY-A",
        extension_min_lead_days=1,
        extension_response_sla_hours=24,
    )
    row_a = service.create_request(purchase_order_a, "DELAY", today + timedelta(days=7))
    assert row_a["expires_at"] - row_a["requested_at"] == timedelta(hours=24)

    # Retailer B: strict policy -- same 1 day lead time is rejected under a
    # 5-day minimum, and a compliant order gets a 96h SLA instead of 24h.
    purchase_order_b_reject = _seed_order(
        repos,
        "ORD-EXT-POLICY-B-REJECT",
        required_ship_date=today + timedelta(days=1),
        requested_delivery_date=today + timedelta(days=3),
        retailer_code="RET-POLICY-B",
        extension_min_lead_days=5,
        extension_response_sla_hours=96,
    )
    with pytest.raises(BusinessRuleError):
        service.create_request(purchase_order_b_reject, "DELAY", today + timedelta(days=7))

    retailer_b = repos.master_data.get_retailer_by_code("RET-POLICY-B")
    material_b = repos.master_data.get_material_by_code("MAT-RET-POLICY-B")
    plant_b = repos.master_data.get_plant_by_code("PLANT-RET-POLICY-B")
    purchase_order_b_ok = repos.purchase_orders.create_purchase_order(
        purchase_order_number="ORD-EXT-POLICY-B-OK",
        retailer_id=retailer_b["id"],
        order_date=today,
        requested_delivery_date=today + timedelta(days=10),
        required_ship_date=today + timedelta(days=8),
    )
    repos.purchase_orders.add_line(
        purchase_order_id=purchase_order_b_ok["id"],
        line_number="10",
        ordered_quantity=_ORDER_QTY,
        unit_price=_UNIT_PRICE,
        material_id=material_b["id"],
        plant_id=plant_b["id"],
    )
    row_b = service.create_request(purchase_order_b_ok["id"], "DELAY", today + timedelta(days=14))
    assert row_b["expires_at"] - row_b["requested_at"] == timedelta(hours=96)


def test_record_response_accepted_shifts_dates_and_retriggers_projection(repos):
    # `now` is pinned and injected into both calls (record_response's own
    # `now` parameter is designed for exactly this, per its docstring) so the
    # test is anchored to a single fixed instant instead of two independent
    # wall-clock reads that can straddle a UTC/local day boundary.
    fixed_now = datetime(2026, 1, 15, 12, 0)  # noqa: DTZ001 (naive by design)
    today = fixed_now.date()
    original_delivery = today + timedelta(days=12)
    original_ship = today + timedelta(days=10)
    proposed_delivery = today + timedelta(days=16)  # +4 days

    purchase_order_id = _seed_order(
        repos,
        "ORD-EXT-4",
        required_ship_date=original_ship,
        requested_delivery_date=original_delivery,
    )
    service = _build_service(repos)
    request = service.create_request(purchase_order_id, "DELAY", proposed_delivery, now=fixed_now)

    updated = service.record_response(request["id"], "ACCEPTED", now=fixed_now)

    assert updated["status"] == "ACCEPTED"
    assert updated["retailer_response_date"] == today

    purchase_order = repos.purchase_orders.require_purchase_order(purchase_order_id)
    assert purchase_order["current_delivery_date"] == proposed_delivery
    assert purchase_order["current_required_ship_date"] == original_ship + timedelta(days=4)

    # Full-lifecycle assertion: a fresh PenaltyProjection row exists
    # reflecting the new (shifted) delivery date -- run_for_purchase_order
    # was re-triggered inline by record_response, using
    # ProjectionService.build_snapshot's COALESCE.
    history = repos.penalty_projections.list_history(purchase_order_id)
    assert history, "record_response should have re-triggered projection"
    assert all(row["days_to_delivery"] == (proposed_delivery - today).days for row in history)


def test_record_response_countered_shifts_by_countered_delta(repos):
    today = date.today()  # noqa: DTZ011 -- test date anchor, not a naive-datetime bug (see module docstring)
    original_delivery = today + timedelta(days=12)
    original_ship = today + timedelta(days=10)
    proposed_delivery = today + timedelta(days=16)
    countered_delivery = today + timedelta(days=14)  # strictly between original and proposed, +2 days

    purchase_order_id = _seed_order(
        repos,
        "ORD-EXT-5",
        required_ship_date=original_ship,
        requested_delivery_date=original_delivery,
    )
    service = _build_service(repos)
    request = service.create_request(purchase_order_id, "DELAY", proposed_delivery)

    updated = service.record_response(request["id"], "COUNTERED", countered_delivery_date=countered_delivery)

    assert updated["status"] == "COUNTERED"
    assert updated["countered_delivery_date"] == countered_delivery

    purchase_order = repos.purchase_orders.require_purchase_order(purchase_order_id)
    assert purchase_order["current_delivery_date"] == countered_delivery
    assert purchase_order["current_required_ship_date"] == original_ship + timedelta(days=2)


def test_record_response_countered_out_of_range_rejected(repos):
    today = date.today()  # noqa: DTZ011 -- test date anchor, not a naive-datetime bug (see module docstring)
    purchase_order_id = _seed_order(
        repos,
        "ORD-EXT-6",
        required_ship_date=today + timedelta(days=10),
        requested_delivery_date=today + timedelta(days=12),
    )
    service = _build_service(repos)
    request = service.create_request(purchase_order_id, "DELAY", today + timedelta(days=16))

    with pytest.raises(ValidationError):
        # Not strictly between baseline_delivery_date (+12) and
        # proposed_delivery_date (+16).
        service.record_response(
            request["id"], "COUNTERED", countered_delivery_date=today + timedelta(days=20)
        )


def test_record_response_rejected_leaves_order_untouched(repos):
    # See test_record_response_accepted_shifts_dates_and_retriggers_projection:
    # `now` is pinned and injected into both calls for the same reason.
    fixed_now = datetime(2026, 1, 15, 12, 0)  # noqa: DTZ001 (naive by design)
    today = fixed_now.date()
    original_delivery = today + timedelta(days=12)
    original_ship = today + timedelta(days=10)

    purchase_order_id = _seed_order(
        repos,
        "ORD-EXT-7",
        required_ship_date=original_ship,
        requested_delivery_date=original_delivery,
    )
    service = _build_service(repos)
    request = service.create_request(purchase_order_id, "SHORTAGE", today + timedelta(days=16), now=fixed_now)

    updated = service.record_response(request["id"], "REJECTED", now=fixed_now)

    assert updated["status"] == "REJECTED"
    purchase_order = repos.purchase_orders.require_purchase_order(purchase_order_id)
    assert purchase_order["current_delivery_date"] is None
    assert purchase_order["current_required_ship_date"] is None

    # Projection is still re-triggered against the unchanged (original) date.
    history = repos.penalty_projections.list_history(purchase_order_id)
    assert history
    assert all(row["days_to_delivery"] == (original_delivery - today).days for row in history)


def test_record_response_twice_raises_invalid_response(repos):
    today = date.today()  # noqa: DTZ011 -- test date anchor, not a naive-datetime bug (see module docstring)
    purchase_order_id = _seed_order(
        repos,
        "ORD-EXT-8",
        required_ship_date=today + timedelta(days=10),
        requested_delivery_date=today + timedelta(days=12),
    )
    service = _build_service(repos)
    request = service.create_request(purchase_order_id, "DELAY", today + timedelta(days=16))
    service.record_response(request["id"], "REJECTED")

    with pytest.raises(ValidationError):
        service.record_response(request["id"], "ACCEPTED")


def test_record_response_unknown_id_raises_not_found(repos):
    service = _build_service(repos)
    with pytest.raises(NotFoundError):
        service.record_response(uuid4(), "ACCEPTED")


def test_expire_stale_transitions_pending_past_timeout_and_retriggers(repos):
    today = date.today()  # noqa: DTZ011 -- test date anchor, not a naive-datetime bug (see module docstring)
    original_delivery = today + timedelta(days=12)
    original_ship = today + timedelta(days=10)

    purchase_order_id = _seed_order(
        repos,
        "ORD-EXT-9",
        required_ship_date=original_ship,
        requested_delivery_date=original_delivery,
    )
    service = _build_service(repos)
    request = service.create_request(purchase_order_id, "DELAY", today + timedelta(days=16))

    # Still PENDING well before the default 48h SLA elapses.
    assert service.expire_stale(as_of=request["requested_at"] + timedelta(hours=1)) == []

    expire_as_of = request["requested_at"] + timedelta(hours=49)
    expired = service.expire_stale(as_of=expire_as_of)

    assert len(expired) == 1
    assert expired[0]["id"] == request["id"]
    assert expired[0]["status"] == "EXPIRED"

    # No active PENDING request remains, and the order's dates are untouched.
    assert repos.delivery_change_requests.find_active_for_purchase_order(purchase_order_id) is None
    purchase_order = repos.purchase_orders.require_purchase_order(purchase_order_id)
    assert purchase_order["current_delivery_date"] is None

    # The re-triggered projection anchors on `as_of`'s date, not wall-clock
    # "today" -- see PoDeliveryChangeRequestService.expire_stale's
    # projection_date=resolved_as_of.date() fix.
    history = repos.penalty_projections.list_history(purchase_order_id)
    assert history
    assert all(row["days_to_delivery"] == (original_delivery - expire_as_of.date()).days for row in history)


def test_expire_stale_defaults_as_of_to_now(repos):
    service = _build_service(repos)
    assert service.expire_stale() == []


def test_create_request_requires_existing_order(repos):
    today = date.today()  # noqa: DTZ011 -- test date anchor, not a naive-datetime bug (see module docstring)
    service = _build_service(repos)
    with pytest.raises(NotFoundError):
        service.create_request(uuid4(), "OTHER", today + timedelta(days=5))


def test_negotiation_status_transitions_through_full_lifecycle(repos):
    """PurchaseOrder.negotiation_status is a denormalized, single-writer
    column (see the comment on PurchaseOrder.negotiation_status and
    PurchaseOrderRepository.update_negotiation_status) -- this asserts
    every transition PoDeliveryChangeRequestService is
    responsible for, checked after each service call rather than only at
    the DB level: create -> PENDING, accept -> ACCEPTED, counter ->
    COUNTERED, reject -> REJECTED, and the sweep path -> EXPIRED. No other
    service in this codebase calls
    PurchaseOrderRepository.update_negotiation_status."""
    today = date.today()  # noqa: DTZ011 -- test date anchor, not a naive-datetime bug (see module docstring)
    original_delivery = today + timedelta(days=12)
    original_ship = today + timedelta(days=10)
    proposed_delivery = today + timedelta(days=16)
    service = _build_service(repos)

    # A brand-new order starts at NONE.
    purchase_order_1 = _seed_order(
        repos,
        "ORD-EXT-NEG-1",
        required_ship_date=original_ship,
        requested_delivery_date=original_delivery,
    )
    assert repos.purchase_orders.require_purchase_order(purchase_order_1)["negotiation_status"] == "NONE"

    # create_request -> PENDING
    request = service.create_request(purchase_order_1, "DELAY", proposed_delivery)
    assert repos.purchase_orders.require_purchase_order(purchase_order_1)["negotiation_status"] == "PENDING"

    # record_response(ACCEPTED) -> ACCEPTED
    service.record_response(request["id"], "ACCEPTED")
    assert repos.purchase_orders.require_purchase_order(purchase_order_1)["negotiation_status"] == "ACCEPTED"

    # A second order, walked through COUNTERED.
    purchase_order_2 = _seed_order(
        repos,
        "ORD-EXT-NEG-2",
        required_ship_date=original_ship,
        requested_delivery_date=original_delivery,
        retailer_code="RET-EXT-NEG-2",
    )
    request_2 = service.create_request(purchase_order_2, "DELAY", proposed_delivery)
    assert repos.purchase_orders.require_purchase_order(purchase_order_2)["negotiation_status"] == "PENDING"
    service.record_response(request_2["id"], "COUNTERED", countered_delivery_date=today + timedelta(days=14))
    assert repos.purchase_orders.require_purchase_order(purchase_order_2)["negotiation_status"] == "COUNTERED"

    # A third order, walked through REJECTED.
    purchase_order_3 = _seed_order(
        repos,
        "ORD-EXT-NEG-3",
        required_ship_date=original_ship,
        requested_delivery_date=original_delivery,
        retailer_code="RET-EXT-NEG-3",
    )
    request_3 = service.create_request(purchase_order_3, "DELAY", proposed_delivery)
    service.record_response(request_3["id"], "REJECTED")
    assert repos.purchase_orders.require_purchase_order(purchase_order_3)["negotiation_status"] == "REJECTED"

    # A fourth order, walked through the sweep -> EXPIRED path.
    purchase_order_4 = _seed_order(
        repos,
        "ORD-EXT-NEG-4",
        required_ship_date=original_ship,
        requested_delivery_date=original_delivery,
        retailer_code="RET-EXT-NEG-4",
    )
    request_4 = service.create_request(purchase_order_4, "DELAY", proposed_delivery)
    assert repos.purchase_orders.require_purchase_order(purchase_order_4)["negotiation_status"] == "PENDING"
    service.expire_stale(as_of=request_4["requested_at"] + timedelta(hours=49))
    assert repos.purchase_orders.require_purchase_order(purchase_order_4)["negotiation_status"] == "EXPIRED"
