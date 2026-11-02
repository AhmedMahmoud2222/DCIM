from app.application.pdu_catalog.validators import validate_pdu_revision


def test_valid_pdu():
    draft = {"pdu_phase": "single"}
    outlets = [{"stable_key": "o1", "outlet_number": 1, "outlet_type": "iec_c13", "phase": "single", "side": "front"}]
    s = validate_pdu_revision(draft, outlets)
    assert s.valid


def test_negative_voltage():
    draft = {"pdu_input_voltage_v": -1}
    outlets = []
    s = validate_pdu_revision(draft, outlets)
    assert not s.valid


def test_zero_current_invalid():
    draft = {"pdu_input_current_a": 0}
    outlets = []
    s = validate_pdu_revision(draft, outlets)
    assert not s.valid


def test_invalid_phase():
    draft = {"pdu_phase": "two"}
    outlets = []
    s = validate_pdu_revision(draft, outlets)
    assert not s.valid


def test_duplicate_outlet_numbers():
    outlets = [{"stable_key": "a", "outlet_number": 1}, {"stable_key": "b", "outlet_number": 1}]
    s = validate_pdu_revision({}, outlets)
    assert not s.valid


def test_invalid_outlet_type():
    outlets = [{"stable_key": "a", "outlet_number": 1, "outlet_type": "nema_9"}]
    s = validate_pdu_revision({}, outlets)
    assert not s.valid


def test_invalid_mounting():
    draft = {"mounting_orientation": "diagonal"}
    outlets = []
    s = validate_pdu_revision(draft, outlets)
    assert not s.valid


def test_invalid_outlet_count():
    outlets = [{"stable_key": "a", "outlet_number": 1, "outlet_count": 0}]
    s = validate_pdu_revision({}, outlets)
    assert not s.valid


def test_missing_stable_key_warns_via_error():
    outlets = [{"outlet_number": 1}]
    s = validate_pdu_revision({}, outlets)
    assert not s.valid
