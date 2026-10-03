"""Failure-path tests for the ClamAV and shared-volume runtime checks in compose_smoke.py.
Docker is replaced by in-memory fakes: these prove the gate fails (with a message naming the
failure class) when ClamAV is unhealthy/missing or the media volume is not shared."""
import importlib.util
import subprocess
import unittest
from pathlib import Path
from unittest import mock

SCRIPT = Path(__file__).resolve().parents[2] / '.github' / 'scripts' / 'compose_smoke.py'
spec = importlib.util.spec_from_file_location('compose_smoke', SCRIPT)
smoke = importlib.util.module_from_spec(spec)
spec.loader.exec_module(smoke)


def done(stdout='', stderr='', code=0):
    return subprocess.CompletedProcess([], code, stdout, stderr)


class ClamavHealthTests(unittest.TestCase):
    def wait(self, services='backend clamav', status='running', health='healthy', timeout=0.05):
        def fake_state(_cid, _env, field):
            return status if field == '.State.Status' else health
        with mock.patch.object(smoke, 'run', return_value=done(services)), \
                mock.patch.object(smoke, 'container', return_value='cid'), \
                mock.patch.object(smoke, 'state', side_effect=fake_state), \
                mock.patch.object(smoke.time, 'sleep'):
            return smoke.wait_for_clamav_healthy(['docker'], {}, timeout=timeout, poll=0)

    def test_healthy_passes(self):
        self.assertEqual(self.wait(), 'cid')

    def test_missing_service_fails(self):
        with self.assertRaisesRegex(RuntimeError, 'CLAMAV STARTUP FAILURE.*missing'):
            self.wait(services='backend celery-worker')

    def test_exited_container_fails(self):
        with self.assertRaisesRegex(RuntimeError, 'CLAMAV STARTUP FAILURE.*exited'):
            self.wait(status='exited')

    def test_unhealthy_fails(self):
        with self.assertRaisesRegex(RuntimeError, 'CLAMAV HEALTH FAILURE.*unhealthy'):
            self.wait(health='unhealthy')

    def test_missing_healthcheck_fails(self):
        with self.assertRaisesRegex(RuntimeError, 'no healthcheck'):
            self.wait(health='<no value>')

    def test_never_healthy_times_out(self):
        with self.assertRaisesRegex(RuntimeError, 'CLAMAV HEALTH FAILURE.*not healthy after'):
            self.wait(health='starting', timeout=0)


class FakeMedia:
    """Simulates exec into two containers; `shared=False` gives each its own filesystem."""
    def __init__(self, shared=True, deny_write=(), corrupt_read=False):
        self.shared, self.deny_write, self.corrupt_read = shared, set(deny_write), corrupt_read
        self.files = {}

    def key(self, service, name):
        return name if self.shared else (service, name)

    def write(self, _cmd, _env, service, name, token):
        if service in self.deny_write:
            return done(stderr='PermissionError: [Errno 13] Permission denied', code=1)
        self.files[self.key(service, name)] = token
        return done()

    def read(self, _cmd, _env, service, name):
        value = self.files.get(self.key(service, name))
        if value is None:
            return done(stderr='FileNotFoundError', code=1)
        return done(stdout=value + 'x' if self.corrupt_read else value)


class SharedMediaTests(unittest.TestCase):
    def transfer(self, fake):
        with mock.patch.object(smoke, 'media_write', fake.write), mock.patch.object(smoke, 'media_read', fake.read):
            smoke.media_transfer([], {}, 'backend', 'celery-worker', '.s', 'tok')

    def test_shared_volume_passes(self):
        self.transfer(FakeMedia())

    def test_isolated_volumes_fail(self):
        with self.assertRaisesRegex(RuntimeError, 'SHARED VOLUME FAILURE.*isolated'):
            self.transfer(FakeMedia(shared=False))

    def test_write_permission_failure(self):
        with self.assertRaisesRegex(RuntimeError, 'PERMISSION FAILURE.*backend cannot write'):
            self.transfer(FakeMedia(deny_write=['backend']))

    def test_content_mismatch_fails(self):
        with self.assertRaisesRegex(RuntimeError, 'SHARED VOLUME FAILURE.*differs'):
            self.transfer(FakeMedia(corrupt_read=True))


class ScannerMessageTests(unittest.TestCase):
    def test_every_probe_exit_code_has_a_named_failure(self):
        probe = (SCRIPT.parent / 'clamd_probe.py').read_text()
        for code in smoke.SCANNER_EXIT_MESSAGES:
            self.assertIn(f'= {code}\n', probe)
        self.assertTrue(all('SCANNER' in m for m in smoke.SCANNER_EXIT_MESSAGES.values()))

    def test_verify_clamav_reports_network_failure(self):
        with mock.patch.object(smoke, 'context', return_value=(['docker'], {}, None, 'p')), \
                mock.patch.object(smoke, 'wait_for_clamav_healthy', return_value='cid'), \
                mock.patch.object(smoke, 'container_ips', return_value=['10.0.0.5']), \
                mock.patch.object(smoke, 'compose_exec', return_value=done(stdout='scanner: unreachable', code=12)):
            with self.assertRaisesRegex(RuntimeError, 'SCANNER NETWORK FAILURE'):
                smoke.verify_clamav()

    def test_verify_clamav_reports_host_clamd_impostor(self):
        with mock.patch.object(smoke, 'context', return_value=(['docker'], {}, None, 'p')), \
                mock.patch.object(smoke, 'wait_for_clamav_healthy', return_value='cid'), \
                mock.patch.object(smoke, 'container_ips', return_value=['10.0.0.5']), \
                mock.patch.object(smoke, 'compose_exec', return_value=done(code=11)):
            with self.assertRaisesRegex(RuntimeError, 'not the Compose clamav container'):
                smoke.verify_clamav()


class OcrSandboxMessageTests(unittest.TestCase):
    def test_every_probe_exit_code_has_a_named_failure(self):
        probe = (SCRIPT.parent / 'ocr_sandbox_probe.py').read_text()
        for code in smoke.OCR_EXIT_MESSAGES:
            self.assertIn(f'sys.exit({code})', probe)
        self.assertTrue(all(m.startswith('OCR') for m in smoke.OCR_EXIT_MESSAGES.values()))

    def verify(self, code):
        with mock.patch.object(smoke, 'context', return_value=(['docker'], {}, None, 'p')), \
                mock.patch.object(smoke, 'container', return_value='cid'), \
                mock.patch.object(smoke, 'state', return_value='running'), \
                mock.patch.object(smoke, 'compose_exec', return_value=done(stdout='detail', code=code)):
            smoke.verify_ocr_sandbox()

    def test_each_failure_class_is_named(self):
        for code, pattern in ((20, 'not installed'), (21, 'seccomp network filter cannot be installed'),
                              (22, 'could still create a network socket'), (23, 'did not read a rendered scanned page'),
                              (24, 'Landlock filesystem policy cannot be enforced'), (25, "read its parent's process environment")):
            with self.assertRaisesRegex(RuntimeError, pattern):
                self.verify(code)

    def test_success_passes(self):
        self.verify(0)


if __name__ == '__main__':
    unittest.main()
