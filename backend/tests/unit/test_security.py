
import jwt
import pytest

from app.core.security import create_token, decode_token, hash_password, verify_password


def test_password_hash_is_not_plaintext_and_verifies():
    hashed = hash_password("correct horse battery staple")
    assert hashed != "correct horse battery staple"
    assert verify_password("correct horse battery staple", hashed)


def test_wrong_password_does_not_verify():
    hashed = hash_password("correct horse battery staple")
    assert not verify_password("wrong password", hashed)


def test_access_token_roundtrip():
    token, jti, _ = create_token(subject="user-123", token_type="access")
    payload = decode_token(token, expected_type="access")
    assert payload["sub"] == "user-123"
    assert payload["jti"] == jti


def test_token_type_confusion_is_rejected():
    """An access token must never be usable where a refresh token is expected, and
    vice versa (§31 Security Hardening)."""
    access_token, _, _ = create_token(subject="user-123", token_type="access")
    with pytest.raises(jwt.InvalidTokenError):
        decode_token(access_token, expected_type="refresh")


def test_tampered_token_is_rejected():
    token, _, _ = create_token(subject="user-123", token_type="access")
    tampered = token[:-1] + ("A" if token[-1] != "A" else "B")
    with pytest.raises(jwt.PyJWTError):
        decode_token(tampered, expected_type="access")
