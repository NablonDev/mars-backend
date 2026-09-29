"""
Replays all four worked-example scenarios day by day through the running
API and prints the same trace `simulate_daily_run.py` used to print
directly against SQLAlchemy sessions -- now going through
HTTP -> router -> service -> repository -> DB instead.

One HTTP call (`POST /admin/simulate-daily-run`) does the actual
day-by-day fact-writing and projecting server-side, inside one service
method (app/services/seeding/service.py::PenaltySeedingService.
simulate_daily_run) -- keeping that sequence atomic and consistent per
purchase order was more important than exposing per-day granularity over
HTTP for a demo script. This script's job is just to call it and render
the day-by-day trace from the response, which already carries every
day's numbers.

For every day where the projection carries exposure
(total_expected_penalty_amount > 0), this script also calls
`POST /api/v1/penalties/mitigations` (`projection_id` in the body) for that
same projection_date and prints the best-ranked option alongside that day's
projection line -- matching the real product flow's "if exposure
remains, a second calculation engine proposes mitigation options" (see
docs/DEMO.md SS3/SS4). The mitigations endpoint resolves
`(purchase_order_id, projection_date)` from one `penalty_projection` row's
own surrogate id (see app/api/v1/penalties/mitigations.py::
_resolve_projection), so this script first calls
`GET /penalties/projections?purchase_order_id=` once per scenario to build
a projection_date -> id map (multiple persisted rows share a date, one per
violation type -- any one of them resolves the same pair).

Unlike the projection replay, these are extra HTTP calls made from the
script rather than server-side: mitigation only applies to the subset of
days that actually have exposure, and by the time these calls fire, the
day's projection was already committed by the simulate-daily-run request
that returned it (see app/api/dependencies.py::get_session), so there's
nothing to race.

Every response now comes wrapped in the `{success, message, data, error}`
envelope (`app/core/envelope.py`), and each scenario is keyed by
`purchase_order_id` (was `order_id`) -- see
`app.schemas.penalties.admin.ScenarioSummary`.

Usage:
    uvicorn app.main:app --reload &
    python scripts/demo/seed_master_data.py
    python scripts/demo/demo_daily_simulation.py
"""

import argparse
import sys

import httpx
from _helpers import _auth_headers, _error_message


def _fetch_projection_ids(base_url: str, purchase_order_id: str) -> dict[str, str]:
    """Maps each projection_date (ISO string) to one `penalty_projection`
    row's own surrogate id for that date, via
    `GET /penalties/projections?purchase_order_id=`. Multiple persisted rows
    share a date (one per violation type), but any one of them resolves the
    same (purchase_order_id, projection_date) pair once passed as
    `projection_id` in the mitigations endpoint's request body (see
    app/api/v1/penalties/mitigations.py::_resolve_projection) -- the first
    row seen per date is kept."""
    resp = httpx.get(
        f"{base_url}/penalties/projections",
        params={"purchase_order_id": purchase_order_id},
        headers=_auth_headers(),
        timeout=30,
    )
    if resp.status_code != 200:
        print(
            f"    projection history fetch failed: {resp.status_code} {_error_message(resp)}",
            file=sys.stderr,
        )
        return {}

    ids_by_date: dict[str, str] = {}
    for row in resp.json()["data"]:
        ids_by_date.setdefault(row["projection_date"], row["id"])
    return ids_by_date


def _run_mitigation(base_url: str, projection_id: str) -> list[dict] | None:
    """Ranked mitigation options for one exposed day, or None on failure --
    printed and skipped rather than fatal, so one bad day doesn't stop the
    rest of the replay."""
    resp = httpx.post(
        f"{base_url}/penalties/mitigations",
        json={"projection_id": projection_id},
        headers=_auth_headers(),
        timeout=30,
    )
    if resp.status_code != 201:
        print(f"           mitigation failed: {resp.status_code} {_error_message(resp)}", file=sys.stderr)
        return None
    return resp.json()["data"]["options"]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:8000/api/v1")
    args = parser.parse_args()

    resp = httpx.post(
        f"{args.base_url}/admin/simulate-daily-run",
        headers=_auth_headers(),
        timeout=60,
    )
    if resp.status_code != 200:
        print(f"Simulation failed: {resp.status_code} {_error_message(resp)}", file=sys.stderr)
        sys.exit(1)

    days_with_exposure = 0
    mitigation_runs = 0
    accept_only_days = 0
    best_action_counts: dict[str, int] = {}
    total_net_saving = 0.0

    for scenario in resp.json()["data"]["scenarios"]:
        purchase_order_id = scenario["purchase_order_id"]
        print(f"\n=== {purchase_order_id} -- day-by-day projection trend ===")
        projection_ids = _fetch_projection_ids(args.base_url, purchase_order_id)

        for day in scenario["days"]:
            # Probability and the raw if-realized amount are the primary
            # framing for each violation type; total_expected_penalty_amount
            # (the blended, probability-weighted figure) is shown last and
            # labeled as a risk-adjusted estimate, never as the only number
            # -- see docs/API.md's response-shape note.
            print(
                f"  {day['projection_date']}  "
                f"shortage: {day['shortage_probability'] * 100:5.1f}% probability of a "
                f"${day['shortage_penalty_amount']:>7,.2f} penalty  "
                f"delay: {day['delay_probability'] * 100:5.1f}% probability of a "
                f"${day['delay_penalty_amount']:>7,.2f} penalty  "
                f"(risk-adjusted estimate: ${day['total_expected_penalty_amount']:>7,.2f})   {day['note']}"
            )
            if day["total_expected_penalty_amount"] <= 0:
                continue

            days_with_exposure += 1
            projection_id = projection_ids.get(day["projection_date"])
            if projection_id is None:
                print(
                    f"           mitigation skipped: no projection_id found for "
                    f"{purchase_order_id}/{day['projection_date']}",
                    file=sys.stderr,
                )
                continue
            options = _run_mitigation(args.base_url, projection_id)
            if options is None:
                continue

            mitigation_runs += 1
            # Options are already ranked by net_saving, descending.
            best = options[0]
            best_action_counts[best["action"]] = best_action_counts.get(best["action"], 0) + 1
            total_net_saving += best["net_saving"]
            if len(options) == 1:
                # ACCEPT was the only option -- likely missing cost data
                # for the other mitigation actions.
                accept_only_days += 1

            print(
                f"           mitigation: best={best['action']:<20} "
                f"net_saving=${best['net_saving']:>8,.2f} (estimated, risk-adjusted)  "
                f"({len(options)} option(s) considered)"
            )

    print("\n=== mitigation summary across all scenarios ===")
    print(f"  days with exposure (mitigation applicable): {days_with_exposure}")
    print(f"  mitigation runs completed:                  {mitigation_runs}")
    print(f"  days with no alternative to ACCEPT:         {accept_only_days} (missing cost data)")
    print(f"  best-action breakdown:                      {best_action_counts}")
    print(f"  total net saving captured (best/day, estimated): ${total_net_saving:,.2f}")


if __name__ == "__main__":
    main()
