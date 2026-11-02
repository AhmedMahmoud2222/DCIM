"""Read-only rollout preflight: python scripts/sec_arch_02_preflight.py --output report.json."""

import argparse
import asyncio
import json
import sys
from pathlib import Path

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.application.access_control import active_administrator_ids  # noqa: E402
from app.application.access_preflight import access_change_report, write_report_file  # noqa: E402
from app.core.config import get_settings  # noqa: E402


async def report() -> dict:
    engine = create_async_engine(get_settings().database_url)
    try:
        async with AsyncSession(engine) as db:
            await db.execute(text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY"))
            return {
                "policy": "SEC-ARCH-02 Option A versus SEC-ARCH-01 flattened composition",
                "read_only": True,
                "changes": await access_change_report(db),
                "global_administrator_ids_after": sorted(map(str, await active_administrator_ids(db))),
            }
    finally:
        await engine.dispose()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, help="Report file (created 0600, must not exist); preferred over stdout.")
    args = parser.parse_args()
    try:
        payload = json.dumps(asyncio.run(report()), indent=2) + "\n"
        if args.output:
            write_report_file(args.output, payload)
        else:
            print("WARNING: the report below is confidential access-control data; prefer --output.", file=sys.stderr)
            print(payload, end="")
    except Exception as exc:
        # Class name only: tracebacks and messages can carry connection details or report fragments.
        print(f"preflight failed: {type(exc).__name__}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
