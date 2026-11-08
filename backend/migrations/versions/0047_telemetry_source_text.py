"""Issue #128 / G3: keep the exact source numeral of every new telemetry reading.

Revision ID: 0047_telemetry_source_text
Revises: 0046_mapping_revisions

Additive. One nullable column, telemetry_reading.raw_value_text VARCHAR(64): the exact finite numeral the source sent
(for example '1234567890.123456789' or '-0.0'), written for readings ingested after this revision. NULL means "stored
before G3": the original text is unknown and nothing is invented for those rows (their raw_value is whatever
PostgreSQL rounded the float-parsed input to). No existing row is read, rewritten or backfilled.

The column is added with no default (a metadata-only change) and its CHECK constraint is added NOT VALID (no scan; every
existing row is NULL, which satisfies it, and it is enforced for every new or updated row).

Downgrade refuses while any reading carries a value in raw_value_text: dropping it would destroy the only record of the
source numeral.
"""

import sqlalchemy as sa
from alembic import op

revision = "0047_telemetry_source_text"
down_revision = "0046_mapping_revisions"
branch_labels = None
depends_on = None

CHECK = "ck_telemetry_reading_raw_value_text_numeral"


def upgrade() -> None:
    op.add_column("telemetry_reading", sa.Column("raw_value_text", sa.String(64), nullable=True))
    op.execute(f"""
        ALTER TABLE telemetry_reading ADD CONSTRAINT {CHECK}
        CHECK (raw_value_text IS NULL OR (char_length(raw_value_text) BETWEEN 1 AND 64 AND
               raw_value_text ~ '^[+-]?([0-9]+([.][0-9]*)?|[.][0-9]+)([eE][+-]?[0-9]+)?$')) NOT VALID
    """)


def downgrade() -> None:
    op.execute("LOCK TABLE telemetry_reading IN ACCESS EXCLUSIVE MODE")
    if op.get_bind().scalar(sa.text("SELECT EXISTS (SELECT 1 FROM telemetry_reading WHERE raw_value_text IS NOT NULL)")):
        raise RuntimeError(
            "Cannot downgrade 0047: telemetry readings carry their exact source numeral in raw_value_text. "
            "Dropping the column would destroy the only record of what the source sent."
        )
    op.drop_constraint(op.f(CHECK), "telemetry_reading", type_="check")
    op.drop_column("telemetry_reading", "raw_value_text")
