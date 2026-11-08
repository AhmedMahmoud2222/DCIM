"""Online rotation of stored credentials onto the current credential-encryption key version
(SEC-02, Issue #38). Safe to run while the application is serving, and safe to re-run.

    PYTHONPATH=. python scripts/rotate_credential_encryption_key.py --dry-run
    PYTHONPATH=. python scripts/rotate_credential_encryption_key.py

It uses the same settings as the application (DATABASE_URL, CREDENTIAL_ENCRYPTION_KEY,
CREDENTIAL_ENCRYPTION_KEYS). The current version is the highest one configured; every stored
credential on an older version is re-encrypted under it, in batches. Only counts per
table/column and the ids of rows that could not be decrypted are printed -- never plaintext or
ciphertext. Exits 1 when any row could not be decrypted (see docs/OPERATIONS.md).
"""

import argparse
import asyncio
import sys

from app.application.credential_key_rotation import rotate_credential_ciphertexts
from app.core.secrets import default_key_provider
from app.db.session import AsyncSessionLocal, engine


async def main(*, dry_run: bool, batch_size: int) -> int:
    current = default_key_provider().current_version()
    try:
        reports = await rotate_credential_ciphertexts(AsyncSessionLocal, batch_size=batch_size, dry_run=dry_run)
    finally:
        await engine.dispose()
    action = "would re-encrypt" if dry_run else "re-encrypted"
    print(f"Current key version: {current}{' (dry run, nothing written)' if dry_run else ''}")
    failed = 0
    for report in reports:
        versions = ", ".join(f"{v}={n}" for v, n in sorted(report.versions_before.items())) or "no values"
        line = f"{report.table}.{report.column}: before [{versions}]; {action} {report.rotated}"
        if report.changed_concurrently:
            line += f"; {report.changed_concurrently} changed by the application meanwhile (already current)"
        print(line)
        for row_id in report.undecryptable_ids:
            print(f"  UNDECRYPTABLE {report.table}.{report.column} id={row_id}", file=sys.stderr)
        failed += len(report.undecryptable_ids)
    if failed:
        print(f"{failed} value(s) could not be decrypted (corrupted, or their key version is not configured); "
              "they were left unchanged.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--dry-run", action="store_true", help="report what would change without writing")
    parser.add_argument("--batch-size", type=int, default=500)
    args = parser.parse_args()
    sys.exit(asyncio.run(main(dry_run=args.dry_run, batch_size=args.batch_size)))
