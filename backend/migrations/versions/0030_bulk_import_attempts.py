"""SEC (Codex PR #50 review, ROUND 4, blocker 1): adds `bulk_import_job.commit_attempt_count`,
incremented atomically every time `run_commit` (app/application/bulk_import/service.py)
successfully claims the commit lease — whether that claim came from the original API
dispatch or from `requeue_stuck_bulk_import_commits`'s (app/infrastructure/tasks/
bulk_import.py) bounded sweeper re-dispatching a job whose lease expired without the prior
delivery ever finishing. Without a count, a job whose commit deterministically fails every
time (a bad row causing an uncaught exception before any lease renewal, a poisoned
downstream dependency) would have its lease simply expire and get reclaimed by the very
next sweep, forever, at the sweeper's fixed interval — an unbounded retry loop. See
`app/application/bulk_import/limits.py::BULK_IMPORT_COMMIT_MAX_ATTEMPTS` and
`service.py::run_commit` for how this column is used to convert that into a bounded number
of attempts before the job is finalized as `committed_with_errors` and no longer reselected
by the sweeper.

Revision ID: 0030_bulk_import_attempts
Revises: 0029_bulk_import_commit_lease
Create Date: 2026-09-29

Named `0030_bulk_import_attempts` rather than the more literal
`0030_bulk_import_commit_attempt_count` because `alembic_version.version_num` is
`VARCHAR(32)` (see every prior revision id in this directory, all ≤30 characters) — the
more literal name is 37 characters and overflows that column outright.
"""

import sqlalchemy as sa
from alembic import op

revision = "0030_bulk_import_attempts"
down_revision = "0029_bulk_import_commit_lease"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "bulk_import_job",
        sa.Column("commit_attempt_count", sa.Integer(), nullable=False, server_default="0"),
    )


def downgrade() -> None:
    op.drop_column("bulk_import_job", "commit_attempt_count")
