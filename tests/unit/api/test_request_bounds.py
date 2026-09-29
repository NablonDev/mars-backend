"""
Two API-level acceptance paths that are easy to break from the schema layer:
a 100% cut confirmation, and a TIERED penalty rule whose bands are well
formed. Both go through the route (not the service directly), so a
request-model regression shows up here as a non-2xx.
"""

from __future__ import annotations

from uuid import UUID

from tests.conftest import make_retailer_agreement


def test_a_full_cut_confirmation_is_still_accepted(seeded_client):
    """confirmed_quantity=0 is a real 100% shortage, not bad input -- it
    must be accepted rather than treated as a missing or invalid quantity."""
    purchase_orders = seeded_client.get("/api/v1/purchase-orders").json()["data"]
    purchase_order = next(po for po in purchase_orders if po["purchase_order_number"] == "WMT-100234")
    line_id = purchase_order["lines"][0]["id"]

    resp = seeded_client.post(
        f"/api/v1/purchase-orders/{purchase_order['id']}/confirmations",
        json={
            "confirmation_number": "CONF-FULL-CUT",
            "confirmation_date": "2026-08-05T00:00:00",
            "lines": [{"purchase_order_line_id": line_id, "confirmed_quantity": 0}],
        },
    )
    assert resp.status_code == 201, resp.text


def test_an_ordered_tier_band_is_still_accepted(client, repos):
    retailer = client.post("/api/v1/retailers", json={"retailer_code": "RET-A", "retailer_name": "A"}).json()[
        "data"
    ]
    retailer_agreement_id = make_retailer_agreement(repos, UUID(retailer["id"]))

    resp = client.post(
        "/api/v1/penalties/rules",
        json={
            "rule_code": "RULE-TIERS-OK",
            "retailer_id": retailer["id"],
            "retailer_agreement_id": str(retailer_agreement_id),
            "penalty_category": "SHORT_SHIP",
            "violation_type": "SHORT_SHIP",
            "calc_type": "TIERED",
            "rate": 1.0,
            "tiers": [
                {"band_min": 0.0, "band_max": 0.10, "rate": 0.02},
                {"band_min": 0.10, "band_max": 1.01, "rate": 0.08},
            ],
        },
    )
    assert resp.status_code == 201, resp.text
