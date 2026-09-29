"""Guards against Settings.job_queue_max_attempts and JobItem.max_attempts'
column default drifting apart again (see JobQueueRepository.enqueue/enqueue_many)."""

from app.core.config import Settings
from app.models.process import JobItem


def test_job_queue_max_attempts_setting_matches_column_default():
    assert Settings().job_queue.max_attempts == JobItem.__table__.c.max_attempts.default.arg
