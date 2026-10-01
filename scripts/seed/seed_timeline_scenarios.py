"""Replay CLI for the fulfillment-timeline scenario simulator.

Standalone runnable: `python -m scripts.seed.seed_timeline_scenarios
[--end-date YYYY-MM-DD] [--days 21] [--reset]`. Writes against a real
`Database` built from `Settings().database.url`, seeding milestone types
first (idempotent, safe to run alongside the rest of the seed scripts).

Every replay is already a clean rebuild -- `TimelineSimulator.replay()` deletes
its own prior rows before recreating them, so it's safe to run repeatedly with
no `--reset` in between. `--reset` is a reset-only flag: it deletes every
`TL-`-prefixed scenario row and exits without replaying.
"""

from __future__ import annotations

import argparse
from datetime import date, timedelta

from app.core.config import Settings
from app.db.session import Database
from app.services.seeding.timeline.simulator import TimelineSimulator
from app.utils.clock import utc_today
from scripts.seed.seed_milestone_types import seed_milestone_types


def main() -> None:
    args = _parse_args()
    settings = Settings()  # type: ignore[call-arg]  # see app/core/config/__init__.py::get_settings
    database = Database(settings.database.url)

    with database.session() as session:
        seed_milestone_types(session)
        simulator = TimelineSimulator(session, args.end_date)
        if args.reset:
            simulator.reset()
            print("Reset every TL- scenario row.")
            return
        simulator.replay(start=args.end_date - timedelta(days=args.days), end=args.end_date)
        print(f"Replayed {args.days} day(s) of scenarios through {args.end_date.isoformat()}.")


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--end-date",
        type=date.fromisoformat,
        default=utc_today(),
        help="Replay end date T, ISO format (default: today).",
    )
    parser.add_argument("--days", type=int, default=21, help="Number of days to replay before T.")
    parser.add_argument(
        "--reset",
        action="store_true",
        help="Delete every TL- scenario row and exit, without replaying.",
    )
    return parser.parse_args()


if __name__ == "__main__":
    main()
