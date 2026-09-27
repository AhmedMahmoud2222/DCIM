from pathlib import Path
import yaml

path = Path('.github/workflows/deploy.yml')
data = yaml.safe_load(path.read_text())
assert data['permissions']['actions'] == 'read'
assert data['permissions']['checks'] == 'read'
for name, job in data['jobs'].items():
    for step in job['steps']:
        script = step.get('run', '')
        assert '${{ inputs.' not in script, (name, 'untrusted input embedded in shell')
        assert '${{ github.event.' not in script, (name, 'event data embedded in shell')
assert data['jobs']['verification-record']['needs'] == ['verify-release', 'validate-staging']
assert '"$STAGING_RESULT" != success' in data['jobs']['verification-record']['steps'][0]['run']
assert '"$VERIFY_RESULT" != success' in data['jobs']['verification-record']['steps'][0]['run']
assert all('upload-artifact@v3' not in str(job) for job in data['jobs'].values())
print('Workflow input handling and dependent record checks passed')
