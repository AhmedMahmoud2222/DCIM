import uuid
from datetime import UTC, datetime

import pytest

from app.api.v1.alarms import _decode_cursor, _encode_cursor
from app.domain.alarm.models import Alarm


def test_alarm_history_cursor_preserves_composite_ordering_key():
    opened_at = datetime(2026, 9, 19, 2, tzinfo=UTC)
    alarm = Alarm(id=uuid.uuid4(), opened_at=opened_at)
    cursor = _encode_cursor(alarm)
    assert _decode_cursor(cursor) == (opened_at, alarm.id)


@pytest.mark.parametrize("cursor", ["not-base64", "e30="])
def test_alarm_history_cursor_rejects_invalid_values(cursor):
    with pytest.raises(Exception, match="Cursor is malformed"):
        _decode_cursor(cursor)
