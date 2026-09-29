"""
One-command tour of the whole system: seeds master data, replays all four
worked-example scenarios day by day, then asks the LLM-powered
penalty-projection-summary endpoint to summarize *why* each purchase
order's final number is what it is -- deterministic engine output feeding
a natural-language layer, in one run, from a clean DB to a client-readable
paragraph.

Not a new capability -- it is `seed_master_data.py` +
`demo_daily_simulation.py` + `demo_penalty_projection_summary.py` chained
together (was `demo_fine_projection_summary.py` -- `fine`/`fines` ->
`penalty`/`penalties` rename), using each scenario's own last simulated
day (taken straight from the simulate-daily-run response) as that
purchase order's `as_of_date`, so it needs no separate discovery call per
purchase order and can't drift out of the range
`ProjectionSummaryService.get_or_schedule` accepts. Prefer this script
for a from-scratch demo; use the three individual scripts when you want
to run or inspect one stage on its own (e.g. a bare engine replay with no
LLM cost, or re-summarizing one purchase order without re-seeding).

Every response now comes wrapped in the `{success, message, data, error}`
envelope (`app/core/envelope.py`), and `/orders/{order_id}/...` routes
became flat `/penalties/...` routes with `purchase_order_id` (`common.
purchase_order`'s UUID surrogate id, not a business `order_id` string) as
a query param or body field, not a path segment.

Penalty-projection-summary generation is a background job: a cache miss
(or --force-regenerate) gets a `202` immediately, not a `200` with the
summary already in it. This script polls the dedicated
`GET /penalties/projections/summary?purchase_order_id=&as_of_date=` read
route (`_helpers._poll_until_ready`, shared with
`demo_penalty_projection_summary.py`/`demo_penalty_mitigation_summary.py`)
until `status` leaves `PENDING`. That status only ever changes once a
worker actually drains the queued regeneration job -- this script starts
one itself (`python scripts/ops/run_daily_batch.py --drain-only`,
subprocess, best-effort) before polling.

The last stage needs real Azure OpenAI credentials in `.env`
(AZURE_OPENAI_API_KEY/ENDPOINT/DEPLOYMENT_NAME) -- without them this still
seeds and simulates successfully and just prints "Penalty projection
summary generation failed upstream" for each purchase order instead of
failing the whole run. Pass --skip-penalty-projection-summary to stop
after the deterministic stage on purpose.

Usage:
    uvicorn app.main:app --reload &
    python scripts/demo/run_end_to_end_demo.py
    python scripts/demo/run_end_to_end_demo.py --base-url http://localhost:9000/api/v1
    python scripts/demo/run_end_to_end_demo.py --skip-penalty-projection-summary
"""

import argparse
import subprocess
import sys
from datetime import UTC, datetime

import httpx
from _helpers import POLL_TIMEOUT_SECONDS, _auth_headers, _error_message, _poll_until_ready

_SUMMARY_PATH = "penalties/projections/summary"


def _seed(base_url: str) -> None:
    print("== Seeding master data ==")
    resp = httpx.post(f"{base_url}/admin/seed-master-data", headers=_auth_headers(), timeout=30)
    if resp.status_code != 200:
        print(f"Seeding failed: {resp.status_code} {_error_message(resp)}", file=sys.stderr)
        sys.exit(1)
    print("  (idempotent -- 0s below mean it was already there)")
    for key, value in resp.json()["data"].items():
        print(f"  {key:10s}: {value}")


def _simulate(base_url: str) -> list[dict]:
    print("\n== Replaying daily scenarios ==")
    resp = httpx.post(f"{base_url}/admin/simulate-daily-run", headers=_auth_headers(), timeout=60)
    if resp.status_code != 200:
        print(f"Simulation failed: {resp.status_code} {_error_message(resp)}", file=sys.stderr)
        sys.exit(1)
    scenarios = resp.json()["data"]["scenarios"]
    for scenario in scenarios:
        last_day = scenario["days"][-1]
        print(
            f"  {scenario['purchase_order_id']}: {len(scenario['days'])} days simulated, "
            f"final total=${last_day['total_expected_penalty_amount']:,.2f} on {last_day['projection_date']}"
        )
    return scenarios


def _start_drain_worker() -> subprocess.Popen | None:
    """Best-effort: drains queued penalty-projection-summary regeneration
    jobs in the background so this script's own polling below has
    something to wait on. Not fatal if it fails to start (e.g. no
    Postgres reachable from this shell) -- the poll loop below just times
    out and reports each purchase order as [timeout] instead."""
    try:
        return subprocess.Popen(
            [sys.executable, "scripts/ops/run_daily_batch.py", "--drain-only"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except OSError:
        return None


def _summarize_all(base_url: str, scenarios: list[dict], force_regenerate: bool) -> None:
    print("\n== Summarizing each purchase order's final number ==")
    today = datetime.now(UTC).date().isoformat()
    drain_worker = _start_drain_worker()
    try:
        for scenario in scenarios:
            purchase_order_id = scenario["purchase_order_id"]
            # The scenario's *last* simulated day, not necessarily today's:
            # the mock scenarios' hardcoded dates (Aug 2026) routinely extend
            # past the real calendar date, and get_or_schedule rejects any
            # `as_of_date` after today. Clamp to the latest day that isn't in
            # the future rather than assuming the last simulated day always is.
            not_future = [d["projection_date"] for d in scenario["days"] if d["projection_date"] <= today]
            if not not_future:
                print(
                    f"\n  [skip] {purchase_order_id}: this scenario's whole timeline is still in the future"
                )
                continue
            as_of_date = max(not_future)
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
                summary = resp.json()["data"]["summary"]
            elif resp.status_code == 202:
                row = _poll_until_ready(base_url, _SUMMARY_PATH, purchase_order_id, as_of_date)
                if row is None:
                    print(
                        f"\n  [timeout] {purchase_order_id}: still PENDING after "
                        f"{POLL_TIMEOUT_SECONDS:.0f}s -- try again later",
                        file=sys.stderr,
                    )
                    continue
                if row["status"] == "FAILED":
                    print(f"\n  [failed] {purchase_order_id}: generation failed upstream", file=sys.stderr)
                    continue
                summary = row["summary"]
            else:
                print(
                    f"\n  [failed] {purchase_order_id}: {resp.status_code} {_error_message(resp)}",
                    file=sys.stderr,
                )
                continue

            print(f"\n  {purchase_order_id} (as of {as_of_date}):")
            print(f"    {summary['summary']}")
    finally:
        if drain_worker is not None:
            drain_worker.terminate()

    print("\nFull per-purchase-order summary text: python scripts/demo/demo_penalty_projection_summary.py")


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--base-url", default="http://127.0.0.1:8000/api/v1")
    parser.add_argument(
        "--skip-penalty-projection-summary",
        action="store_true",
        help="Stop after the deterministic engine stage -- no LLM calls, no Azure OpenAI credentials needed",
    )
    parser.add_argument(
        "--force-regenerate",
        action="store_true",
        help="Bypass the persisted-summary cache and call the LLM again for every purchase order",
    )
    args = parser.parse_args()

    _seed(args.base_url)
    scenarios = _simulate(args.base_url)
    if not args.skip_penalty_projection_summary:
        _summarize_all(args.base_url, scenarios, args.force_regenerate)


if __name__ == "__main__":
    main()
