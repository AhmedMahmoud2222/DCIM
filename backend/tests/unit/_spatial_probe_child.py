"""Child used by test_spatial_import_sandbox.py. It enters the sandbox exactly as the importer worker does
(`worker.main`) and then reports what it can still reach."""

import json
import os
import socket
import sys

from app.application.catalog_documents.extraction.sandbox import enter_sandbox


def main() -> None:
    enter_sandbox(cpu_seconds=5, address_space_bytes=1024 * 1024 * 1024, require_seccomp=False, require_landlock=True)
    out: dict[str, object] = {}
    out["env_secret_keys"] = sorted(
        k for k in os.environ if any(t in k.upper() for t in ("DATABASE", "REDIS", "JWT", "CREDENTIAL", "SECRET", "PASSWORD", "TOKEN"))
    )
    try:
        socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        out["socket"] = "allowed"
    except Exception:  # noqa: BLE001
        out["socket"] = "blocked"
    for label, path, mode in (
        ("write_cwd", os.path.join(os.getcwd(), "spatial_probe_should_not_exist.txt"), "w"),
        ("write_tmp", "/tmp/spatial_probe_should_not_exist.txt", "w"),
        ("read_parent_environ", f"/proc/{os.getppid()}/environ", "rb"),
    ):
        try:
            with open(path, mode) as handle:
                if "r" in mode:
                    handle.read(16)
            out[label] = "allowed"
        except Exception:  # noqa: BLE001
            out[label] = "blocked"
    sys.stdout.write(json.dumps(out))


if __name__ == "__main__":
    main()
