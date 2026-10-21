#!/usr/bin/env python3
"""Runs one mutated test suite as PID 1 of a private PID + mount namespace (ns_run v1.0).

    ns_run.py REPORT CWD TIMEOUT -- COMMAND...

The mutation runner starts this under `unshare --mount --pid --fork --mount-proc --propagation private`.
Because this process is PID 1 of the namespace, the kernel kills every process the suite left behind the
moment it exits, and every mount the suite made existed only in a private mount namespace that disappears
with it. Nothing the suite did can still be running, or mounted in the host namespace, when the runner
deletes the worktree. Before exiting it reports how many mounts the suite left below CWD (telemetry only).

Not collected by unittest (no test_ prefix).
"""
import json
import os
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import strict_cleanup  # noqa: E402


def root_is_private(table):
    """True when the namespace's root mount has no propagation tag (shared:, master:, unbindable). A shared root
    would let mounts made by the suite appear in the host, so the wrapper refuses to run the suite then."""
    for record in table.split('\n'):
        fields = record.split(' ')
        if len(fields) > 6 and fields[4] == '/':
            optional = fields[6:fields.index('-')] if '-' in fields else fields[6:]
            return not any(tag.startswith(('shared:', 'master:')) or tag == 'unbindable' for tag in optional)
    return False


def main(argv):
    if len(argv) < 6 or argv[4] != '--':
        print(__doc__, file=sys.stderr)
        return 2
    report, cwd, timeout, command = argv[1], argv[2], float(argv[3]), argv[5:]
    if not root_is_private(strict_cleanup.read_mountinfo()):
        print('ns_run: the root mount of this namespace is not private; refusing to run the suite', file=sys.stderr)
        return 3
    timed_out = False
    try:
        rc = subprocess.run(command, cwd=cwd, timeout=timeout).returncode
    except subprocess.TimeoutExpired:
        rc, timed_out = 1, True
    leaked = len(strict_cleanup.mounts_under(cwd))
    with open(report, 'w') as handle:
        json.dump({'rc': rc, 'timed_out': timed_out, 'leaked_mounts': leaked, 'pid': os.getpid()}, handle)
    return rc


if __name__ == '__main__':
    sys.exit(main(sys.argv))
