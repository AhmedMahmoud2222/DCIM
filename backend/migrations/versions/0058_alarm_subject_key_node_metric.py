"""Remove the integration identifier from persisted alarm subject keys.

Alarm.subject_key already belongs to one rule, and rules are scoped to an integration.
The display identity therefore needs only the node identifier and metric.
"""

from alembic import op

revision = "0058_alarm_subject_key_node_metric"
down_revision = "0057_fk_indexes"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        UPDATE alarm
        SET subject_key = substring(subject_key FROM char_length(integration_id::text) + 2)
        WHERE subject_key LIKE integration_id::text || ':%'
        """
    )


def downgrade() -> None:
    op.execute(
        """
        UPDATE alarm
        SET subject_key = integration_id::text || ':' || subject_key
        WHERE subject_key NOT LIKE integration_id::text || ':%'
        """
    )
