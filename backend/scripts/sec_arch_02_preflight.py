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
from app.application.access_preflight import access_change_report  # noqa: E402
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


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, help="Security-sensitive report; protect it like an access-control export.")
    args = parser.parse_args()
    payload = json.dumps(asyncio.run(report()), indent=2) + "\n"
    if args.output:
        args.output.write_text(payload, encoding="utf-8")
    else:
        print(payload, end="")
