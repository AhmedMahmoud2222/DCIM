"""PostgreSQL: v2 mappings pin semantics and batch rejection preserves valid peers."""
import uuid
from decimal import Decimal

from sqlalchemy import select

from app.domain.telemetry.models import IntegrationMetricMappingRevision, TelemetryReading
from tests.api._phase8_helpers import create_integration, register_collector
from tests.api.test_telemetry_invalid_numeric import _post


async def test_v2_mapping_ingest_range_rejection_and_unchanged_v1(client, auth_headers, db_session):
    headers = await auth_headers('DCIM Manager')
    collector = await register_collector(client, headers)
    await client.post(f"/api/v1/collectors/{collector['id']}/capabilities",json={'protocol_codes':['icmp']},headers=headers)
    integration = await create_integration(client,headers,integration_type='icmp')
    assigned = await client.post(f"/api/v1/collectors/{collector['id']}/assignments",json={'integration_id':integration['id']},headers=headers)
    assert assigned.status_code == 201, assigned.text
    mapping = await client.post('/api/v1/telemetry/mappings',headers=headers,json={
        'integration_id':integration['id'],'source_identifier':'sensor',
        'canonical_metric':'humidity_percent','unit':'%','registry_version':'2',
    })
    assert mapping.status_code == 201, mapping.text
    assert mapping.json()['registry_version'] == '2'
    revision = await db_session.get(IntegrationMetricMappingRevision,uuid.UUID(mapping.json()['current_revision_id']))
    assert revision.registry_version == '2'
    response = await _post(client,collector,integration,[('good','50'),('impossible','101'),('good2','100')])
    assert response.status_code == 200, response.text
    assert [(r['status'],r['error']) for r in response.json()['results']] == [
        ('accepted',None),('rejected','VALUE_OUT_OF_RANGE'),('accepted',None),
    ]
    rows = (await db_session.execute(select(TelemetryReading).order_by(TelemetryReading.dedup_key))).scalars().all()
    assert [(r.dedup_key,r.value,r.registry_version) for r in rows] == [('good',Decimal(50),'2'),('good2',Decimal(100),'2')]
    retry = await _post(client,collector,integration,[('impossible','101')])
    assert retry.json()['results'][0]['error'] == 'VALUE_OUT_OF_RANGE'
    # Neither a rejected sample nor a v2 declaration silently migrates an older mapping.
    rule = await client.post('/api/v1/alarms/rules',headers=headers,json={
        'integration_id':integration['id'],'metric':'humidity_percent','registry_version':'2',
        'rule_type':'threshold_high','threshold':90,'name':'v2 humidity',
    })
    assert rule.status_code == 201, rule.text
    old = await client.post('/api/v1/telemetry/mappings',headers=headers,json={
        'integration_id':integration['id'],'source_identifier':'v1-sensor','canonical_metric':'humidity_percent','unit':'%',
    })
    assert old.status_code == 201 and old.json()['registry_version'] == '1'
    import json
    from datetime import UTC, datetime

    from tests.api._phase8_helpers import sign_request
    raw=json.dumps({'records':[{'dedup_key':'v1-outside-v2','integration_id':integration['id'],
        'source_identifier':'v1-sensor','external_identifier':'ext','occurred_at':datetime.now(UTC).isoformat(),
        'value':150}]}).encode()
    signed=sign_request(secret=collector['secret'],collector_id=uuid.UUID(collector['id']),raw_body=raw)
    signed['Content-Type']='application/json'
    result=await client.post(f"/api/v1/collectors/{collector['id']}/telemetry",content=raw,headers=signed)
    assert result.status_code == 200 and result.json()['results'][0]['status'] == 'accepted'
    from app.domain.alarm.models import Alarm
    assert (await db_session.execute(select(Alarm))).scalars().all() == []


async def test_v2_alarm_threshold_and_state_validation(client,auth_headers):
    headers = await auth_headers('DCIM Manager')
    integration = await create_integration(client,headers,integration_type='icmp')
    base={'integration_id':integration['id'],'registry_version':'2','name':'v2 rule'}
    for fields in ({'metric':'humidity_percent','rule_type':'threshold_high','threshold':101},
                   {'metric':'availability','rule_type':'threshold_high','threshold':1}):
        response = await client.post('/api/v1/alarms/rules',headers=headers,json=base|fields)
        assert response.status_code == 422, response.text
    response=await client.post('/api/v1/alarms/rules',headers=headers,json=base|{
        'metric':'availability','rule_type':'availability_unavailable',
    })
    assert response.status_code == 201, response.text


async def test_v2_canonical_storage_overflow_retains_numeric_error(client, auth_headers, db_session):
    from tests.api.test_telemetry_invalid_numeric import _setup

    collector, integration = await _setup(
        client, auth_headers, db_session, metric="humidity_percent", unit="1", registry_version="2",
    )
    response = await _post(client, collector, integration, [("overflow", "9999999999"), ("domain", "1.01"), ("valid", "0.5")])
    assert response.status_code == 200, response.text
    assert [(r["status"], r["error"]) for r in response.json()["results"]] == [
        ("rejected", "INVALID_TELEMETRY_VALUE"), ("rejected", "VALUE_OUT_OF_RANGE"), ("accepted", None),
    ]
