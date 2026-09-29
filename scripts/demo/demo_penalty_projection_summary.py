"""
Calls the LLM-powered penalty-projection-summary endpoint
(`POST /penalties/projections/summary`) for one or every purchase order and
prints the free-text result -- the "why is this order's number what it is"
companion to `demo_daily_simulation.py`'s "what is the number."

Was `demo_fine_projection_summary.py` (`fine`/`fines` -> `penalty`/
`penalties` rename; `/orders/{id}/projection-summary` ->
`/purchase-orders/{id}/penalty-projections/summary` -> now flat,
`/penalties/projections/summary` with `purchase_order_id` in the body).
Every response now comes wrapped in the `{success, message, data, error}`
envelope (`app/core/envelope.py`) -- every `resp.json()` below reads
`["data"]`.

Generation is a background job: a cache miss (or --force-regenerate) gets
a `202` immediately, not a `200` with the summary already in it. This
script polls the dedicated `GET /penalties/projections/summary?
purchase_order_id=&as_of_date=` read route (shared with
`demo_penalty_mitigation_summary.py` via `_helpers._poll_until_ready`) until
`status` leaves `PENDING`. That status only ever changes once a worker
actually drains the queued regeneration job -- run
`python scripts/ops/run_daily_batch.py --drain-only` (or the full nightly
batch) in another terminal alongside this script.

Requires real Azure OpenAI credentials in `.env`
(AZURE_OPENAI_API_KEY/ENDPOINT/DEPLOYMENT_NAME) -- without them the
background job lands on a FAILED status with a clear "Penalty projection
summary generation failed upstream" message (see
app/services/penalties/projection/summary_service.py), which this script
prints per purchase order and moves on rather than treating as a script bug.

For each purchase order this explicitly looks up its latest existing
projection date via `GET /penalties/projections?purchase_order_id=` and
passes that as `as_of_date` -- it never omits `as_of_date` and relies on
the server's "defaults to today" behaviour, since that only happens to
produce a sensible answer while the mock scenario dates (Aug 2026) and the
real calendar date coincide. Summarizing a purchase order with no
projections yet is a clean skip, not a crash -- run
`demo_daily_simulation.py` (or `POST /penalties/projections`) first.

Usage:
    uvicorn app.main:app --reload &
    python scripts/demo/seed_master_data.py
    python scripts/demo/demo_daily_simulation.py
    python scripts/ops/run_daily_batch.py --drain-only &   # drains queued summary jobs

    python scripts/demo/demo_penalty_projection_summary.py                              # every purchase order on file
    python scripts/demo/demo_penalty_projection_summary.py --purchase-order-id <uuid>    # one purchase order
    python scripts/demo/demo_penalty_projection_summary.py --purchase-order-id <uuid> --force-regenerate
"""

import argparse
import sys
from datetime import UTC, datetime

import httpx
from _helpers import POLL_TIMEOUT_SECONDS, _auth_headers, _error_message, _poll_until_ready

_SUMMARY_PATH = "penalties/projections/summary"


def _latest_projection_date(base_url: str, purchase_order_id: str) -> str | None:
    """The most recent projection date that isn't in the future -- not
    just the most recent one that exists. `ProjectionSummaryService.
    get_or_schedule` rejects any `as_of_date` after today (see
    ValidationError(code="INVALID_AS_OF_DATE")), and the mock scenarios'
    hardcoded dates (Aug 2026) only sometimes fall entirely before "today"
    depending on when this actually runs -- three of the four routinely
    extend past it. Taking a blind `max()` over all history picks a
    future date for those and 422s every time."""
    resp = httpx.get(
        f"{base_url}/penalties/projections",
        params={"purchase_order_id": purchase_order_id},
        headers=_auth_headers(),
        timeout=30,
    )
    resp.raise_for_status()
    history = resp.json()["data"]
    if not history:
        return None
    today = datetime.now(UTC).date().isoformat()
    not_future = [row["projection_date"] for row in history if row["projection_date"] <= today]
    return max(not_future) if not_future else None


def _print_summary(purchase_order_id: str, body: dict) -> None:
    print(f"\n=== {purchase_order_id} -- as of {body['as_of_date']} (model={body['model_name']}) ===")
    print(body["summary"])


def _summarize_one(base_url: str, purchase_order_id: str, force_regenerate: bool) -> None:
    as_of_date = _latest_projection_date(base_url, purchase_order_id)
    if as_of_date is None:
        print(
            f"  [skip] {purchase_order_id}: no projection exists on or before today -- "
            "run demo_daily_simulation.py first, or this purchase order's whole scenario "
            "is still in the future"
        )
        return

    resp = httpx.post(
        f"{base_url}/{_SUMMARY_PATH}",
        json={
            "purchase_order_id": purchase_order_id,
            "as_of_date": as_of_date,
            "force_regenerate": force_regenerate,
        },
        headers=_auth_headers(),
        timeout=30,
    )
    if resp.status_code == 200:
        # Cache hit -- already the full summary, nothing to schedule or poll.
        _print_summary(purchase_order_id, resp.json()["data"]["summary"])
        return
    if resp.status_code != 202:
        print(f"  [failed] {purchase_order_id}: {resp.status_code} {_error_message(resp)}", file=sys.stderr)
        return

    row = _poll_until_ready(base_url, _SUMMARY_PATH, purchase_order_id, as_of_date)
    if row is None:
        print(
            f"  [timeout] {purchase_order_id}: still PENDING after {POLL_TIMEOUT_SECONDS:.0f}s -- "
            "is a worker draining the queue? (python scripts/ops/run_daily_batch.py --drain-only)",
            file=sys.stderr,
        )
        return
    if row["status"] == "FAILED":
        print(f"  [failed] {purchase_order_id}: generation failed upstream", file=sys.stderr)
        return
    _print_summary(purchase_order_id, row["summary"])


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--base-url", default="http://127.0.0.1:8000/api/v1")
    parser.add_argument(
        "--purchase-order-id", help="Summarize one purchase order only (default: every one on file)"
    )
    parser.add_argument(
        "--force-regenerate",
        action="store_true",
        help="Bypass the persisted-summary cache and call the LLM again",
    )
    args = parser.parse_args()

    if args.purchase_order_id:
        purchase_order_ids = [args.purchase_order_id]
    else:
        resp = httpx.get(f"{args.base_url}/purchase-orders", headers=_auth_headers(), timeout=30)
        resp.raise_for_status()
        purchase_order_ids = [po["id"] for po in resp.json()["data"]]
        if not purchase_order_ids:
            print("No purchase orders on file -- run scripts/demo/seed_master_data.py first", file=sys.stderr)
            sys.exit(1)

    for purchase_order_id in purchase_order_ids:
        _summarize_one(args.base_url, purchase_order_id, args.force_regenerate)


if __name__ == "__main__":
    main()
