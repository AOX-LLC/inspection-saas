"""Settings that must stay consistent with the database functions they feed."""

import pytest
from pydantic import ValidationError

from app.config import Settings


def test_a_job_lock_cannot_outlast_what_the_claim_function_allows():
    # queue.jobs_claim clamps a lock to an hour; a longer handler timeout would let a
    # second worker take a job that is still running.
    with pytest.raises(ValidationError):
        Settings(job_lock_seconds=7200)


def test_the_overlap_must_leave_a_stride():
    with pytest.raises(ValidationError, match="TILE_OVERLAP"):
        Settings(tile_size=256, tile_overlap=256)


def test_abandoned_uploads_are_never_younger_than_a_presigned_posts_life():
    with pytest.raises(ValidationError):
        Settings(abandoned_upload_seconds=600)
