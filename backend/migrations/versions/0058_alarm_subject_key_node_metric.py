"""Remove the integration identifier from persisted alarm subject keys.

Alarm.subject_key already belongs to one rule, and rules are scoped to an integration.
The display identity therefore needs only the node identifier and metric.
"""

from alembic import op

revision = "0058_alarm_subject_key"
down_revision = "0057_fk_indexes"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Existing keys emitted by the alarm service were always
    # ``<integration UUID>:<node name>:<metric>``. Remove exactly that prefix.
    op.execute(
        """
        UPDATE alarm
        SET subject_key = substring(subject_key FROM char_length(integration_id::text) + 2)
        WHERE subject_key LIKE integration_id::text || ':%'
        """
    )


def downgrade() -> None:
    # Before this migration every alarm subject key included its integration
    # UUID, so restore that historical format for every row.
    op.execute(
        """
        UPDATE alarm
        SET subject_key = integration_id::text || ':' || subject_key
        """
    )
