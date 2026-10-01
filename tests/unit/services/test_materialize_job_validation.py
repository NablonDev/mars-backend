"""Proves `materialize_job.rebuild()` never touches Postgres or swaps in a
new graph when the DB<->ontology mapping fails validation -- the "keep the
last known-good graph, never publish a broken one" behavior from
`docs/ontology/semantic-context-layer-design.md` §17, built entirely on the
existing `Container.refresh_ontology_graph` atomic swap (no new
state-management mechanism).

No live Postgres needed -- `validate_mapping` runs before any DB access, so
a validation failure here is caught before `container.ontology_repos()` is
ever entered.
"""

from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

from app.ontology.config.validation import MappingValidationError
from app.services.ontology import materialize_job


class RebuildValidationFailureTests(unittest.TestCase):
    def test_rebuild_raises_and_never_touches_the_db_or_the_graph_when_mapping_invalid(self) -> None:
        container = MagicMock()

        with (
            patch.object(
                materialize_job, "validate_mapping", side_effect=MappingValidationError("deliberately broken")
            ),
            self.assertRaises(MappingValidationError),
        ):
            materialize_job.rebuild(container)

        # No bulk read was attempted, and -- critically -- the container's
        # currently-published graph was never touched, so whatever it was
        # serving before this call keeps being served.
        container.ontology_repos.assert_not_called()
        container.refresh_ontology_graph.assert_not_called()


if __name__ == "__main__":
    unittest.main()
