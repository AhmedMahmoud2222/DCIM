#!/usr/bin/env python3
"""Lockfile integrity for the pinned OpenCode runtime installation.

The native smoke workflow and the deployment runner both install the pinned
OpenCode CLI with `npm ci --ignore-scripts --no-audit --no-fund` from
`.github/opencode/runtime/`. `npm ci` fails closed (EUSAGE) when the committed
lockfile and package.json disagree — an earlier main lockfile had been
generated on a single platform and was therefore missing the platform-specific
optional packages that other npm majors refuse to install without ("Missing:
opencode-darwin-arm64@1.19.3 from lock file"), which defeated the diagnostic
before inference ever started.

These tests lock the shipping prerequisites of that install step, without any
network or subprocess access:

  1. package.json and the lockfile agree on the pinned `opencode-ai` version,
     the package name and the platform-optional manifests;
  2. every platform-specific optional package that `opencode-ai` declares has
     a complete lockfile entry (pinned version, registry-npmjs.org resolved
     URL, sha512 integrity) so `npm ci` cannot fail with EUSAGE on any runner
     platform or npm major — the failure classes observed in runs 39972207857
     and 40179971819 are each mapped to a dedicated assertion;
  3. the lockfile is closed: nothing else is pinned, so the runtime cannot
     drift by accumulating unrelated packages.

The clean installation itself (and the installed CLI reporting exactly
`1.19.3` before any credential is mapped) is asserted in the deployment
runner's prepare step of the deploy-tools workflow on every push/PR, plus by
the smoke workflow's own install step before its dispatch. These tests ensure
that installation can no longer fail for lockfile reasons.
"""
import json
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
RUNTIME = ROOT / '.github/opencode/runtime'

PINNED_VERSION = '1.19.3'
ROOT_PACKAGE = 'opencode-ai'
REGISTRY = 'https://registry.npmjs.org/'


def _load(path):
    return json.loads(path.read_text(encoding='utf-8'))


class PlatformPackageLockfileTests(unittest.TestCase):
    """The lockfile the smoke workflow install step must be able to install."""

    def setUp(self):
        self.package_json = _load(RUNTIME / 'package.json')
        self.lockfile = _load(RUNTIME / 'package-lock.json')
        self.lock_packages = self.lockfile['packages']

    # -- 1. package.json and lockfile agree -------------------------------

    def test_package_json_pins_the_declared_version(self):
        deps = self.package_json['dependencies']
        self.assertEqual(deps, {ROOT_PACKAGE: PINNED_VERSION},
                         f'package.json must pin exactly '
                         f'{ROOT_PACKAGE}@{PINNED_VERSION}')

    def test_package_json_name_matches_the_lockfile_root(self):
        # `npm install` derives the lockfile name from package.json; a
        # mismatch would be rewritten on the next regeneration.
        self.assertEqual(self.package_json.get('name'),
                         self.lockfile.get('name'))

    def test_lockfile_root_declares_the_same_single_pinned_dependency(self):
        root = self.lock_packages['']
        self.assertEqual(root.get('dependencies'),
                         {ROOT_PACKAGE: PINNED_VERSION},
                         'lockfile root must match package.json')

    def test_lockfile_is_settled_format(self):
        self.assertEqual(self.lockfile['lockfileVersion'], 3)
        self.assertTrue(self.lockfile.get('requires', True))

    # -- 2. every declared platform package is installable ----------------

    def test_every_declared_platform_package_has_a_complete_lockfile_entry(
            self):
        declared = (self.lock_packages[f'node_modules/{ROOT_PACKAGE}']
                    .get('optionalDependencies'))
        self.assertTrue(declared,
                        'opencode-ai must declare platform optionals')
        for package, version in sorted(declared.items()):
            with self.subTest(package=package):
                self.assertEqual(version, PINNED_VERSION)
                entry = self.lock_packages[f'node_modules/{package}']
                # The four precise EUSAGE failure clauses, per platform
                # package: version, resolved URL, integrity, optionality.
                self.assertEqual(entry.get('version'), version,
                                 f'{package}: version pin missing')
                resolved = entry.get('resolved')
                self.assertEqual(
                    resolved,
                    REGISTRY + f'{package}/-/{package}-{version}.tgz',
                    f'{package}: resolved URL must be the exact registry '
                    'tarball')
                integrity = entry.get('integrity')
                self.assertIsInstance(integrity, str)
                self.assertTrue(
                    re.fullmatch(r'sha512-[A-Za-z0-9+/]{86}==', integrity),
                    f'{package}: sha512 integrity missing or malformed')
                self.assertTrue(entry.get('optional'),
                                f'{package}: must stay optional so npm ci '
                                'skips non-matching platforms rather than '
                                'erroring the install on them')
                # Platform-fitness metadata so npm can select per OS/CPU: at
                # least one of os/cpu/libc must be declared.
                self.assertTrue(entry.get('os') or entry.get('cpu')
                                or entry.get('libc'),
                                f'{package}: no os/cpu/libc selector')

    def test_platform_packages_are_not_filtered_out_for_the_smoke_platform(
            self):
        # The smoke workflow installs on linux/x64 glibc: those variants must
        # NOT be excluded by their own selectors (regression for a lockfile
        # whose own linux/x64 entries were absent).
        for package in ('opencode-linux-x64', 'opencode-linux-x64-musl'):
            entry = self.lock_packages[f'node_modules/{package}']
            self.assertIn('linux', entry.get('os', []),
                          f'{package}: linux selector missing')
            self.assertIn('x64', entry.get('cpu', []),
                          f'{package}: x64 selector missing')

    def test_main_package_entry_is_complete(self):
        entry = self.lock_packages[f'node_modules/{ROOT_PACKAGE}']
        self.assertEqual(entry.get('version'), PINNED_VERSION)
        self.assertEqual(entry.get('resolved'),
                         REGISTRY + f'{ROOT_PACKAGE}/-/'
                         f'{ROOT_PACKAGE}-{PINNED_VERSION}.tgz')
        self.assertTrue(re.fullmatch(
            r'sha512-[A-Za-z0-9+/]{86}==', entry.get('integrity', '')),
            'opencode-ai: sha512 integrity missing or malformed')
        self.assertTrue(entry.get('hasInstallScript'),
                        'opencode-ai must keep its install-script marker so '
                        'the npm ci wrapper reproduces previous installs')

    # -- 3. lockfile is closed ---------------------------------------------

    def test_lockfile_pins_nothing_outside_the_pinned_runtime(self):
        platform_optionals = set(
            self.lock_packages[f'node_modules/{ROOT_PACKAGE}']
            .get('optionalDependencies', {}))
        allowed = ({f'node_modules/{ROOT_PACKAGE}'}
                   | {f'node_modules/{package}' for package in platform_optionals})
        actual = {key for key in self.lock_packages if key != ''}
        self.assertEqual(actual, allowed,
                         'the runtime lockfile must pin exactly the pinned '
                         'runtime dependency and its platform optionals')

    # -- 4. the smoke workflow cannot be broken by the runtime layout -------

    def test_smoke_workflow_install_path_matches_the_pinned_selector(self):
        """The smoke workflow hardcodes the linux-x64 binary path; prove the
        lockfile keeps that package installable on the runner selectors.

        (The design of the two workflows' install steps themselves — `npm
        ci + exact --version equality before any credential is mapped — is
        asserted statically in the CI workflow tests.)"""
        workflow_text = (ROOT / '.github/workflows/'
                         'opencode-native-smoke.yml').read_text(encoding='utf-8')
        self.assertIn('npm ci --ignore-scripts --no-audit --no-fund',
                      workflow_text,
                      'the smoke install must run npm ci (never a mutable '
                      'install) from the committed lockfile')
        self.assertIn('node_modules/opencode-linux-x64/bin/opencode',
                      workflow_text,
                      'the smoke workflow must keep using the pinned '
                      'linux-x64 binary path')
