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
    # ``<integration UUID>:<node name>:<metric>``. Remove exactly the first
    # UUID prefix; colons inside node names remain part of the node identity.
    # This assumes rows at 0057 use the legacy format (not a mixed-format detector).
    op.execute(
        """
        UPDATE alarm
        SET subject_key = substring(subject_key FROM char_length(integration_id::text) + 2)
        WHERE subject_key LIKE integration_id::text || ':%'
        """
    )


def downgrade() -> None:
    # A legacy key can use at most 475 characters after adding the 37-character
    # UUID prefix. Fail before changing anything if post-migration data no longer
    # fits the old String(512) contract; silent truncation would corrupt identity.
    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM alarm WHERE char_length(subject_key) > 475) THEN
                RAISE EXCEPTION 'Cannot downgrade alarm subject keys longer than 475 characters';
            END IF;
            UPDATE alarm
            SET subject_key = integration_id::text || ':' || subject_key;
        END $$;
        """
    )
