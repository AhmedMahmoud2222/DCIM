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


# Artifact action pins: every workflow that uploads and downloads must use an approved pair.
# An unknown pin, or a pairing outside this table, fails the check so a bump of one action
# cannot silently leave its counterpart on an incompatible major version.
ARTIFACT_PIN_PAIRS = {
    # (upload-artifact SHA, download-artifact SHA)
    ('cf430e030ddbb5b0abf93d22962f4752f3646cd9', '9000827ccba6bdab643e8b6fd33ac0654aef8333'),  # v7.0.2 + v8.0.2
    # Version comments only, not independently verified upstream. Used by claude-agent.yml.
    ('043fb46d1a93c77aae656e7c1c64a875d1fc6a0a', 'd3f86a106a0bac45b974a628896c90dbdf5c8093'),  # v4 + v4.3.0
}
APPROVED_UPLOADS = {upload for upload, _ in ARTIFACT_PIN_PAIRS}
APPROVED_DOWNLOADS = {download for _, download in ARTIFACT_PIN_PAIRS}


def collect_artifact_pins(node, found):
    if isinstance(node, dict):
        ref = node.get('uses')
        if isinstance(ref, str):
            match = re.fullmatch(r'actions/(upload-artifact|download-artifact)@([0-9a-f]{40})', ref)
            if match:
                found[match.group(1)].add(match.group(2))
        for value in node.values():
            collect_artifact_pins(value, found)
    elif isinstance(node, list):
        for value in node:
            collect_artifact_pins(value, found)


for workflow in sorted(Path('.github/workflows').glob('*.y*ml')):
    found = {'upload-artifact': set(), 'download-artifact': set()}
    collect_artifact_pins(yaml.safe_load(workflow.read_text()), found)
    uploads, downloads = found['upload-artifact'], found['download-artifact']
    assert uploads <= APPROVED_UPLOADS, (
        f'artifact pin mismatch: {workflow} has an unapproved upload-artifact pin', sorted(uploads - APPROVED_UPLOADS)
    )
    assert downloads <= APPROVED_DOWNLOADS, (
        f'artifact pin mismatch: {workflow} has an unapproved download-artifact pin', sorted(downloads - APPROVED_DOWNLOADS)
    )
    for upload in uploads:
        for download in downloads:
            assert (upload, download) in ARTIFACT_PIN_PAIRS, (
                f'artifact pin mismatch: {workflow} pairs upload and download pins that are not an approved pair',
                upload, download,
            )
print('Artifact upload and download pins form approved pairs in every workflow')
