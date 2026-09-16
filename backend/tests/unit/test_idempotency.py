from app.application.idempotency import hash_request_body


def test_same_body_hashes_identically_regardless_of_key_order():
    a = hash_request_body({"asset_type": "rack", "asset_tag": "R-1"})
    b = hash_request_body({"asset_tag": "R-1", "asset_type": "rack"})
    assert a == b


def test_different_body_hashes_differently():
    a = hash_request_body({"asset_tag": "R-1"})
    b = hash_request_body({"asset_tag": "R-2"})
    assert a != b
