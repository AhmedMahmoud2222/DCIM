import pytest

from app.application.concurrency import check_version_match, parse_if_match
from app.core.errors import ApiError, ConflictError


def test_stale_version_raises_conflict():
    with pytest.raises(ConflictError):
        check_version_match(expected=1, actual=2)


def test_matching_version_does_not_raise():
    check_version_match(expected=2, actual=2)


def test_parse_if_match_accepts_quoted_integer():
    assert parse_if_match('"3"') == 3


def test_parse_if_match_rejects_non_integer():
    with pytest.raises(ApiError):
        parse_if_match("not-a-number")


def test_parse_if_match_none_when_absent():
    assert parse_if_match(None) is None
