"""
Calls the LLM-powered penalty-mitigation-summary endpoint
(`POST /penalties/mitigations/summary`) for one or every purchase order and
prints the free-text result -- the "which action should we take, and why"
companion to `demo_penalty_projection_summary.py`'s "why is this order's
number what it is."

Was `demo_fine_mitigation_summary.py` (`fine`/`fines` -> `penalty`/
`penalties` rename; `/orders/{id}/mitigation-options` ->
`/penalty-mitigations?projection_id=...` -> now flat,
`/penalties/mitigations/summary` with `purchase_order_id`+`as_of_date`
directly in the body -- no `projection_id` resolution needed here any
more).

Ranked mitigation options are the prerequisite here, not projections
directly -- generating a summary needs `penalties.mitigation_option` rows
to already exist for the resolved `(purchase_order_id, projection_date)`,
and raises `NO_MITIGATION_OPTIONS_EXIST` (`422`, `BusinessRuleError`) for a
purchase order that has none. Those in turn need a projection for the same
date first, so the full prerequisite chain is
projections -> mitigation-options -> this script -- run
`scripts/ops/run_mitigation_cli.py --purchase-order-id <id>` (or
`POST /penalties/mitigations`) first.

Generation is a background job: a cache miss (or --force-regenerate) gets
a `202` immediately. This script polls the dedicated
`GET /penalties/mitigations/summary?purchase_order_id=&as_of_date=` read
route (shared with `demo_penalty_projection_summary.py` via
`_helpers._poll_until_ready`) until `status` leaves `PENDING`. That status
only ever changes once a worker actually drains the queued regeneration
job -- run `python scripts/ops/run_daily_batch.py --drain-only` (or the
full nightly batch) in another terminal alongside this script.

Requires real Azure OpenAI credentials in `.env`
(AZURE_OPENAI_API_KEY/ENDPOINT/DEPLOYMENT_NAME) -- without them the background job
lands on a FAILED status with a clear "Penalty mitigation summary
generation failed upstream" message (see
app/services/penalties/mitigation/summary_service.py), which this script
prints per purchase order and moves on rather than treating as a script
bug.

Usage:
    uvicorn app.main:app --reload &
    python scripts/demo/seed_master_data.py
    python scripts/demo/demo_daily_simulation.py
    python scripts/ops/run_mitigation_cli.py --all-open
    python scripts/ops/run_daily_batch.py --drain-only &   # drains queued summary jobs

    python scripts/demo/demo_penalty_mitigation_summary.py                              # every purchase order on file
    python scripts/demo/demo_penalty_mitigation_summary.py --purchase-order-id <uuid>    # one purchase order
    python scripts/demo/demo_penalty_mitigation_summary.py --purchase-order-id <uuid> --force-regenerate
"""

import argparse
import sys

import httpx
from _helpers import POLL_TIMEOUT_SECONDS, _auth_headers, _error_message, _poll_until_ready

_SUMMARY_PATH = "penalties/mitigations/summary"


def _latest_projection_date(base_url: str, purchase_order_id: str) -> str | None:
    """The purchase order's latest projection date, via its penalty-exposure
    snapshot, or None if no projection has ever run, or it ran with zero
    violations (nothing to mitigate)."""
    resp = httpx.get(
        f"{base_url}/penalties/exposure",
        params={"purchase_order_id": purchase_order_id},
        headers=_auth_headers(),
        timeout=30,
    )
    if resp.status_code == 404:
        return None
    resp.raise_for_status()
    body = resp.json()["data"]
    if not body["violations"]:
        return None
    return body["projection_date"]


def _mitigation_options_exist(base_url: str, purchase_order_id: str, projection_date: str) -> bool:
    resp = httpx.get(
        f"{base_url}/penalties/mitigations",
        params={"purchase_order_id": purchase_order_id, "projection_date": projection_date},
        headers=_auth_headers(),
        timeout=30,
    )
    resp.raise_for_status()
    return bool(resp.json()["data"]["options"])


def _print_summary(purchase_order_id: str, body: dict) -> None:
    print(f"\n=== {purchase_order_id} -- as of {body['as_of_date']} (model={body['model_name']}) ===")
    print(body["summary"])


def _summarize_one(base_url: str, purchase_order_id: str, force_regenerate: bool) -> None:
    projection_date = _latest_projection_date(base_url, purchase_order_id)
    if projection_date is None:
        print(
            f"  [skip] {purchase_order_id}: no penalty exposure on file -- "
            "run demo_daily_simulation.py first, or this purchase order has no violations to mitigate"
        )
        return

    if not _mitigation_options_exist(base_url, purchase_order_id, projection_date):
        print(
            f"  [skip] {purchase_order_id}: no mitigation options exist yet -- "
            "run scripts/ops/run_mitigation_cli.py first"
        )
        return

    resp = httpx.post(
        f"{base_url}/{_SUMMARY_PATH}",
        json={
            "purchase_order_id": purchase_order_id,
            "as_of_date": projection_date,
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

    row = _poll_until_ready(base_url, _SUMMARY_PATH, purchase_order_id, projection_date)
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
