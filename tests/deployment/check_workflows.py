from pathlib import Path
import re
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

# The deployment gate must keep exercising ClamAV and the shared media volume at runtime.
gate = yaml.safe_load(Path('.github/workflows/deployment-validation.yml').read_text())
gate_runs = ' '.join(step.get('run', '') for step in gate['jobs']['compose-smoke']['steps'])
for command in ('compose_smoke.py verify_clamav', 'compose_smoke.py verify_shared_media', 'compose_smoke.py verify_ocr_sandbox'):
    assert command in gate_runs, f'deployment validation no longer runs {command}'
print('Deployment validation runs the ClamAV and shared-media runtime checks')

# Apply the supply-chain policy to step actions and reusable workflow jobs.
def check_action_refs(node, workflow):
    if isinstance(node, dict):
        if 'uses' in node:
            ref = node['uses']
            assert isinstance(ref, str), (workflow, 'invalid action reference')
            if not ref.startswith('./'):
                assert re.fullmatch(r'[^@\s]+@[0-9a-f]{40}', ref), (
                    workflow, 'external action must use a full commit SHA', ref
                )
        for value in node.values():
            check_action_refs(value, workflow)
    elif isinstance(node, list):
        for value in node:
            check_action_refs(value, workflow)


for workflow in sorted(Path('.github/workflows').glob('*.y*ml')):
    config = yaml.safe_load(workflow.read_text())
    check_action_refs(config, str(workflow))
print('All external workflow actions use full commit SHAs')


# Artifact action pins must be one compatible upload/download pair across every workflow.
# A workflow that mixes pins can upload with one major version and download with another.
ARTIFACT_PIN_PAIRS = {
    # upload-artifact SHA (v7.0.2) -> download-artifact SHA (v8.0.2)
    'cf430e030ddbb5b0abf93d22962f4752f3646cd9': '9000827ccba6bdab643e8b6fd33ac0654aef8333',
}
artifact_pins = {'upload-artifact': set(), 'download-artifact': set()}


def collect_artifact_pins(node):
    if isinstance(node, dict):
        ref = node.get('uses')
        if isinstance(ref, str):
            match = re.fullmatch(r'actions/(upload-artifact|download-artifact)@([0-9a-f]{40})', ref)
            if match:
                artifact_pins[match.group(1)].add(match.group(2))
        for value in node.values():
            collect_artifact_pins(value)
    elif isinstance(node, list):
        for value in node:
            collect_artifact_pins(value)


for workflow in sorted(Path('.github/workflows').glob('*.y*ml')):
    collect_artifact_pins(yaml.safe_load(workflow.read_text()))
for action, shas in artifact_pins.items():
    assert len(shas) <= 1, f'artifact pin mismatch: {action} uses several pins: {sorted(shas)}'
uploads, downloads = artifact_pins['upload-artifact'], artifact_pins['download-artifact']
if uploads and downloads:
    assert {ARTIFACT_PIN_PAIRS.get(sha) for sha in uploads} == downloads, (
        'artifact pin mismatch: upload and download pins are not an approved pair'
    )
print('Artifact upload and download pins form an approved pair')
