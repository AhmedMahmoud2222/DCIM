"""Failure-path tests for the ClamAV and shared-volume runtime checks in compose_smoke.py.
Docker is replaced by in-memory fakes: these prove the gate fails (with a message naming the
failure class) when ClamAV is unhealthy/missing or the media volume is not shared."""
import contextlib
import importlib.util
import io
import re
import runpy
import subprocess
import types
import unittest
from pathlib import Path
from unittest import mock

import tomllib
import yaml

SCRIPT = Path(__file__).resolve().parents[2] / '.github' / 'scripts' / 'compose_smoke.py'
spec = importlib.util.spec_from_file_location('compose_smoke', SCRIPT)
smoke = importlib.util.module_from_spec(spec)
spec.loader.exec_module(smoke)


COMPOSE_CONFIG = yaml.safe_load((SCRIPT.parents[2] / 'docker-compose.yml').read_text())
BACKEND_CONFIG = tomllib.loads((SCRIPT.parents[2] / 'backend' / 'pyproject.toml').read_text())


def done(stdout='', stderr='', code=0):
    return subprocess.CompletedProcess([], code, stdout, stderr)


class ComposeHealthcheckConfigTests(unittest.TestCase):
    def test_worker_healthcheck_pings_its_celery_node(self):
        worker = COMPOSE_CONFIG['services']['celery-worker']
        healthcheck = worker['healthcheck']
        self.assertEqual(healthcheck['test'][0], 'CMD-SHELL')
        self.assertIn('inspect ping', healthcheck['test'][1])
        self.assertIn('celery@$$HOSTNAME', healthcheck['test'][1])
        self.assertIn('--timeout=3', healthcheck['test'][1])
        self.assertIn('grep -q pong', healthcheck['test'][1])
        self.assertEqual(healthcheck['interval'], '10s')
        self.assertEqual(healthcheck['timeout'], '15s')
        self.assertNotIn('-n', worker['command'])
        self.assertNotIn('--hostname', worker['command'])

    def test_beat_healthcheck_checks_the_beat_process(self):
        beat = COMPOSE_CONFIG['services']['celery-beat']
        healthcheck = beat['healthcheck']
        self.assertEqual(healthcheck['test'][0], 'CMD-SHELL')
        self.assertIn('--enable-remote-control', beat['command'])
        self.assertIn('inspect ping', healthcheck['test'][1])
        self.assertIn('celerybeat@$$HOSTNAME', healthcheck['test'][1])
        self.assertIn('--timeout=3', healthcheck['test'][1])
        self.assertIn('grep -q pong', healthcheck['test'][1])
        self.assertEqual(healthcheck['interval'], '10s')
        self.assertEqual(healthcheck['timeout'], '15s')
        self.assertIn('celery[redis]>=5.7.0', BACKEND_CONFIG['project']['dependencies'])

    def test_compose_smoke_requires_celery_healthchecks(self):
        healthy = {service: 'healthy' for service in smoke.HEALTH_CHECKED_SERVICES}
        self.assertTrue(smoke.healthchecks_healthy(healthy))
        for service in ('celery-worker', 'celery-beat'):
            unhealthy = dict(healthy, **{service: 'unhealthy'})
            self.assertFalse(smoke.healthchecks_healthy(unhealthy), service)

    def test_verify_rejects_unhealthy_worker_and_beat(self):
        states = {
            'migrate': 'exited',
            'bootstrap-privileges': 'exited',
            'backend': 'running',
            'celery-worker': 'running',
            'celery-beat': 'running',
            'frontend': 'running',
        }
        for service in ('celery-worker', 'celery-beat'):
            for status in ('starting', 'unhealthy'):
                with self.subTest(service=service, status=status):
                    health = {name: 'healthy' for name in smoke.HEALTH_CHECKED_SERVICES}
                    health[service] = status

                    def fake_state(container_id, _env, field, current_health=health, current_states=states):
                        if field == '.State.Health.Status':
                            return current_health.get(container_id, '<no value>')
                        if field == '.State.ExitCode':
                            return '0'
                        return current_states.get(container_id, 'running')

                    with mock.patch.object(smoke, 'context', return_value=([], {}, None, None)), \
                            mock.patch.object(smoke, 'container', side_effect=lambda _cmd, _env, service: service), \
                            mock.patch.object(smoke, 'state', side_effect=fake_state), \
                            mock.patch.object(smoke, 'http_ok', return_value=True), \
                            mock.patch.object(smoke.time, 'monotonic', side_effect=(0.0, 0.0, 181.0)), \
                            mock.patch.object(smoke.time, 'sleep'), \
                            self.assertRaisesRegex(RuntimeError, 'Timed out'):
                        smoke.verify()


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
                mock.patch.object(smoke, 'compose_exec', return_value=done(stdout='scanner: unreachable', code=12)), \
                self.assertRaisesRegex(RuntimeError, 'SCANNER NETWORK FAILURE'):
            smoke.verify_clamav()

    def test_verify_clamav_reports_host_clamd_impostor(self):
        with mock.patch.object(smoke, 'context', return_value=(['docker'], {}, None, 'p')), \
                mock.patch.object(smoke, 'wait_for_clamav_healthy', return_value='cid'), \
                mock.patch.object(smoke, 'container_ips', return_value=['10.0.0.5']), \
                mock.patch.object(smoke, 'compose_exec', return_value=done(code=11)), \
                self.assertRaisesRegex(RuntimeError, 'not the Compose clamav container'):
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
                              (24, 'Landlock filesystem policy cannot be enforced'), (25, "read its parent's process environment"),
                              (26, 'signal other processes or modify other files')):
            with self.assertRaisesRegex(RuntimeError, pattern):
                self.verify(code)

    def test_success_passes(self):
        self.verify(0)



class CommandDispatchTests(unittest.TestCase):
    """Issue #331: the command line selects from an explicit table, never from module globals."""
    WORKFLOW_COMMANDS = ('prepare', 'config', 'start', 'verify', 'verify_clamav', 'verify_ocr_sandbox',
                         'verify_shared_media', 'diagnose', 'cleanup')

    def cli(self, *args):
        """Runs the script's own `__main__` block in this process; returns (exit code, stderr)."""
        stderr = io.StringIO()
        with mock.patch.object(smoke.sys, 'argv', [str(SCRIPT), *args]), contextlib.redirect_stderr(stderr), \
                self.assertRaises(SystemExit) as exit_:
            runpy.run_path(str(SCRIPT), run_name='__main__')
        return exit_.exception.code, stderr.getvalue()

    def test_table_holds_exactly_the_nine_workflow_commands(self):
        self.assertEqual(tuple(smoke.COMMANDS), self.WORKFLOW_COMMANDS)
        for name, function in smoke.COMMANDS.items():
            self.assertIs(function, getattr(smoke, name))

    def test_deployment_validation_workflow_only_uses_table_commands(self):
        workflow = (SCRIPT.parents[1] / 'workflows' / 'deployment-validation.yml').read_text()
        used = re.findall(r'compose_smoke\.py[ \t]+(\S+)', workflow)
        self.assertEqual(tuple(used), self.WORKFLOW_COMMANDS)

    def test_script_no_longer_dispatches_through_globals(self):
        self.assertNotIn('globals()', SCRIPT.read_text())

    def test_each_command_dispatches_to_its_own_function(self):
        for name in self.WORKFLOW_COMMANDS:
            with mock.patch.dict(smoke.COMMANDS, {name: mock.Mock()}):
                self.assertEqual(smoke.main(['compose_smoke.py', name]), 0)
                smoke.COMMANDS[name].assert_called_once_with()

    def test_internal_helpers_and_module_names_are_not_commands(self):
        helpers = sorted(
            name for name, value in vars(smoke).items()
            if name not in smoke.COMMANDS and (callable(value) or isinstance(value, types.ModuleType))
        )
        for expected in ('run', 'context', 'container', 'compose_exec', 'media_remove', 'main', 'subprocess', 'os'):
            self.assertIn(expected, helpers)
        main = smoke.main
        for name in helpers:
            with mock.patch.object(smoke, name) as helper, mock.patch.object(smoke.sys, 'stderr'):
                self.assertEqual(main(['compose_smoke.py', name]), 1, name)
            helper.assert_not_called()

    def test_command_failure_exits_nonzero(self):
        with mock.patch.dict(smoke.COMMANDS, {'verify': mock.Mock(side_effect=RuntimeError('boom'))}), \
                mock.patch.object(smoke.sys, 'stderr'):
            self.assertEqual(smoke.main(['compose_smoke.py', 'verify']), 1)

    def test_cli_rejects_unknown_helper_and_dunder_names(self):
        for name in ('run', 'context', 'exit', '__builtins__', 'COMMANDS', 'Verify', ''):
            code, stderr = self.cli(name)
            self.assertEqual(code, 1, name)
            self.assertIn('FAIL: expected exactly one command', stderr)

    def test_cli_rejects_missing_and_extra_arguments(self):
        for args in ((), ('verify', 'run')):
            code, stderr = self.cli(*args)
            self.assertEqual(code, 1, args)
            self.assertIn('FAIL: expected exactly one command', stderr)


if __name__ == '__main__':
    unittest.main()
