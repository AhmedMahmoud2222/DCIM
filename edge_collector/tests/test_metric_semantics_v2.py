from edge_collector.telemetry_contract import classify_telemetry_ack


def test_impossible_v2_reading_is_permanent():
    assert classify_telemetry_ack({'status':'rejected','error':'VALUE_OUT_OF_RANGE'}) == 'acknowledge'
