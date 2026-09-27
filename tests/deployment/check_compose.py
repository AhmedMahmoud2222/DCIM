import json
import subprocess

result = subprocess.run(['docker','compose','-f','docker-compose.yml','-f','docker-compose.production.yml','config','--format','json'],capture_output=True,text=True,check=True)
cfg = json.loads(result.stdout)
s = cfg['services']
assert set(s) == {'postgres','redis','migrate','bootstrap-privileges','backend','celery-worker','celery-beat','frontend'}
for service in ('postgres','redis','backend','frontend'):
    assert len(s[service]['ports']) == 1, (service, s[service]['ports'])
    assert s[service]['ports'][0]['host_ip'] == '127.0.0.1', service
assert any(m['target'] == '/var/lib/postgresql/data' and m['type'] == 'bind' for m in s['postgres']['volumes'])
assert s['migrate']['depends_on']['postgres']['condition'] == 'service_healthy'
assert s['bootstrap-privileges']['depends_on']['migrate']['condition'] == 'service_completed_successfully'
assert s['backend']['depends_on']['bootstrap-privileges']['condition'] == 'service_completed_successfully'
assert cfg['networks']['dcim-internal']['driver'] == 'bridge'
assert not cfg['networks']['dcim-internal'].get('internal', False)
keys = {s[x]['environment']['CREDENTIAL_ENCRYPTION_KEY'] for x in ('migrate','backend','celery-worker','celery-beat')}
assert len(keys) == 1
print('Effective bindings, storage, dependencies, network and shared key passed')
