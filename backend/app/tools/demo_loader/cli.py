# ruff: noqa: E501
"""Command line for the local demo loader.

    python -m app.tools.demo_loader plan
    python -m app.tools.demo_loader apply  --confirm-local-demo [--steps ...] [--history-hours 24]
    python -m app.tools.demo_loader refresh --confirm-local-demo [--watch]
    python -m app.tools.demo_loader verify
    python -m app.tools.demo_loader purge  --confirm-local-demo [--dry-run]

Mutating commands need `--confirm-local-demo` and refuse to run when ENVIRONMENT=production. Output is one JSON
document per command on stdout (progress goes to stderr); the exit code is 0 only when every check passed."""

import argparse
import asyncio
import contextlib
import importlib
import json
import sys
import uuid
from datetime import UTC, datetime

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.domain.auth.models import User
from app.domain.telemetry.models import IntegrationMetricMapping
from app.tools.demo_loader import LOADER_VERSION
from app.tools.demo_loader.constants import (
    DEFAULT_DEVICE_HISTORY_HOURS,
    DEFAULT_HISTORY_HOURS,
    DEMO_SITE_PREFIX,
    MAPPING_LABEL,
    SOURCE_PREFIX,
)
from app.tools.demo_loader.floorplans import calibrate_demo_plans
from app.tools.demo_loader.provision import ensure_device_mappings, ensure_sensors
from app.tools.demo_loader.purge import purge
from app.tools.demo_loader.selection import (
    LoaderError,
    Targets,
    check_mapping_ownership,
    discover,
    outside_fingerprint,
    validate_site_prefix,
)
from app.tools.demo_loader.series import Series, build_series, routes_by_integration, simulate
from app.tools.demo_loader.signals import next_slot_after
from app.tools.demo_loader.verify import VerifyOptions, verify

STEPS = ("existing", "sensors", "floorplans", "devices")
# One PostgreSQL advisory lock key for "a demo-loader process is writing". Writers (apply, refresh, including
# `refresh --watch`) hold it shared for their whole run; a real purge needs it exclusive, so it refuses to start while any
# writer is alive and no writer can start while a purge runs. Session-level, so it vanishes with the process.
WRITER_LOCK_KEY = 7_227_437_402_541_777
TAKE_EXCLUSIVE = text("SELECT pg_try_advisory_lock(:key)")
TAKE_SHARED = text("SELECT pg_try_advisory_lock_shared(:key)")
RELEASE_EXCLUSIVE = text("SELECT pg_advisory_unlock(:key)")
RELEASE_SHARED = text("SELECT pg_advisory_unlock_shared(:key)")


def log(message: str) -> None:
    print(message, file=sys.stderr, flush=True)


@contextlib.asynccontextmanager
async def writer_lock(*, exclusive: bool):
    from app.db.session import engine

    take = TAKE_EXCLUSIVE if exclusive else TAKE_SHARED
    release = RELEASE_EXCLUSIVE if exclusive else RELEASE_SHARED
    async with engine.connect() as connection:
        got = (await connection.execute(take, {"key": WRITER_LOCK_KEY})).scalar_one()
        if not got:
            what = "another demo-loader process is writing (an apply, or a refresh / demo-refresh service that is still running)" if exclusive \
                else "a purge is running"
            raise LoaderError(f"Cannot start: {what}. Stop it first (docker compose stop demo-refresh) and run this again.")
        try:
            yield
        finally:
            with contextlib.suppress(Exception):
                await connection.execute(release, {"key": WRITER_LOCK_KEY})


def guard(args: argparse.Namespace) -> None:
    validate_site_prefix(args.site_prefix)
    if get_settings().environment == "production":
        raise LoaderError("ENVIRONMENT is 'production'. The demo loader never runs against production.")
    if args.command in ("apply", "refresh", "purge") and not getattr(args, "dry_run", False) and not args.confirm_local_demo:
        raise LoaderError("This command changes data. Re-run with --confirm-local-demo to confirm this is the local demonstration database.")


async def resolve_actor(db: AsyncSession, email: str | None) -> uuid.UUID:
    stmt = select(User.id).where(User.is_active.is_(True)).order_by(User.created_at)
    if email:
        stmt = stmt.where(User.email == email)
    actor = (await db.execute(stmt.limit(1))).scalar_one_or_none()
    if actor is None:
        raise LoaderError("No active user to attribute floor-plan imports to" + (f" ({email})" if email else "") + "; pass --actor-email.")
    return actor


async def existing_series(db: AsyncSession, targets: Targets) -> list[Series]:
    routes = routes_by_integration(targets)
    ownership = await check_mapping_ownership(db, targets)  # raises before any write if a mapping points at a foreign site
    mappings = list(
        (
            await db.execute(
                select(IntegrationMetricMapping)
                .where(IntegrationMetricMapping.integration_id.in_(list(routes)), ~IntegrationMetricMapping.source_identifier.startswith(SOURCE_PREFIX),
                       IntegrationMetricMapping.id.in_(ownership.allowed))
                .order_by(IntegrationMetricMapping.created_at, IntegrationMetricMapping.id)
            )
        ).scalars()
    )
    if ownership.unresolved:
        log(f"  skipped {len(ownership.unresolved)} existing mapping(s) whose asset site cannot be determined: {sorted(ownership.unresolved.values())[:5]}")
    return await build_series(db, mappings, routes, anchored=True)


async def loader_series(db: AsyncSession, targets: Targets) -> list[Series]:
    routes = routes_by_integration(targets)
    mappings = list(
        (
            await db.execute(
                select(IntegrationMetricMapping)
                .where(IntegrationMetricMapping.integration_id.in_(list(routes)), IntegrationMetricMapping.source_identifier.startswith(SOURCE_PREFIX),
                       IntegrationMetricMapping.label == MAPPING_LABEL)
                .order_by(IntegrationMetricMapping.source_identifier)
            )
        ).scalars()
    )
    return await build_series(db, mappings, routes)


def protect_prefix(args: argparse.Namespace) -> str:
    """Sites outside this prefix are fingerprinted. Fixed: a weaker value could hide a change, so it is not an option."""
    return DEMO_SITE_PREFIX


def clock(args: argparse.Namespace) -> datetime | None:
    """`--as-of` pins the sample clock (reproducible seeding, deterministic tests); default is the current time."""
    return getattr(args, "as_of", None)


def verify_options(args: argparse.Namespace, steps: list[str] | None = None) -> VerifyOptions:
    """`verify` after an `apply` checks only the steps that ran; a standalone `verify` checks everything."""
    chosen = frozenset(steps) if steps is not None else frozenset({"existing", "sensors", "devices", "floorplans"})
    return VerifyOptions(
        expected_racks=args.expect_racks, expected_devices=args.expect_devices, hours=args.history_hours,
        device_hours=args.device_history_hours, as_of=clock(args), require_sensors="sensors" in chosen,
        parts=chosen & {"existing", "sensors", "devices", "floorplans"},
    )


def parse_as_of(value: str) -> datetime:
    """Explicit, timezone-aware ISO 8601 only (for example 2026-11-13T12:00:00Z or ...+02:00), never in the future."""
    try:
        moment = datetime.fromisoformat(value.replace("Z", "+00:00") if value.endswith("Z") else value)
    except ValueError as error:
        raise argparse.ArgumentTypeError(f"{value!r} is not an ISO 8601 timestamp") from error
    if moment.tzinfo is None or moment.utcoffset() is None:
        raise argparse.ArgumentTypeError(f"{value!r} has no timezone; write an explicit offset such as 'Z' or '+00:00'")
    moment = moment.astimezone(UTC)
    if moment > datetime.now(UTC):
        raise argparse.ArgumentTypeError(f"{value!r} is in the future")
    return moment


async def run_apply(db: AsyncSession, args: argparse.Namespace) -> dict:
    factory = getattr(args, "session_factory", None)
    steps = [s for s in STEPS if s in args.steps]
    targets = await discover(db, prefix=args.site_prefix, overrides=dict(args.integration))
    before = await outside_fingerprint(db, protect_prefix(args))
    out: dict = {"loader_version": LOADER_VERSION, "steps": steps,
                 "targets": {"sites": [s.code for s in targets.sites], "racks": len(targets.racks), "devices": len(targets.devices)}}
    failed = False
    if "existing" in steps:
        log("Simulating history for the existing mappings ...")
        series = await existing_series(db, targets)
        report = await simulate(db, series, hours=args.history_hours, progress=log, now=clock(args), session_factory=factory)
        out["existing"] = {"series": len(series), **report.as_dict()}
        failed |= not report.clean
    if "sensors" in steps:
        log("Creating and commissioning the rack inlet sensors ...")
        sensors = await ensure_sensors(db, targets)
        out["sensors"] = {"racks": sensors.racks, "created": sensors.created, "commissioned": sensors.commissioned, "placed": sensors.placed,
                          "mappings_created": sensors.mappings_created, "problems": sensors.problems}
        failed |= bool(sensors.problems)
        series = await build_series(db, sensors.mappings, routes_by_integration(targets))
        report = await simulate(db, series, hours=args.history_hours, progress=log, now=clock(args), session_factory=factory)
        out["sensors"]["telemetry"] = {"series": len(series), **report.as_dict()}
        failed |= not report.clean
    if "floorplans" in steps:
        log("Calibrating the demo floor plans ...")
        actor = await resolve_actor(db, args.actor_email)
        plans = await calibrate_demo_plans(db, targets, actor, sandbox=not args.no_sandbox)
        out["floorplans"] = [{**p.__dict__, "plan_id": str(p.plan_id) if p.plan_id else None, "calibration_id": str(p.calibration_id) if p.calibration_id else None} for p in plans]
        failed |= any(p.state == "skipped" for p in plans)
    if "devices" in steps:
        log("Creating device mappings and simulating device telemetry ...")
        devices, mappings = await ensure_device_mappings(db, targets)
        series = await build_series(db, mappings, routes_by_integration(targets))
        report = await simulate(db, series, hours=args.device_history_hours, progress=log, now=clock(args), session_factory=factory)
        out["devices"] = {"devices": devices.devices, "mappings_created": devices.mappings_created, "mappings_total": devices.mappings_total,
                          "telemetry": {"series": len(series), **report.as_dict()}}
        failed |= not report.clean
    after = await outside_fingerprint(db, protect_prefix(args))
    out["outside_demo_sites"] = {"unchanged": before["digest"] == after["digest"], "before": before["digest"], "after": after["digest"], "counts": after["counts"]}
    failed |= before["digest"] != after["digest"]
    out["ok"] = not failed
    return out


async def run_refresh(db: AsyncSession, args: argparse.Namespace) -> dict:
    targets = await discover(db, prefix=args.site_prefix, overrides=dict(args.integration))
    last: dict = {}
    while True:
        series = await existing_series(db, targets) + await loader_series(db, targets)
        report = await simulate(db, series, hours=args.catch_up_hours, now=None, session_factory=getattr(args, "session_factory", None))  # refresh always uses the live clock
        last = {"at": datetime.now(UTC).isoformat(), "series": len(series), **report.as_dict(), "ok": report.clean}
        if not args.watch:
            return last
        print(json.dumps(last), flush=True)
        interval = min((s.interval for s in series), default=300)
        wake = next_slot_after(datetime.now(UTC), interval).timestamp() + 3
        await asyncio.sleep(max(1.0, wake - datetime.now(UTC).timestamp()))


async def run(args: argparse.Namespace) -> tuple[dict, int]:
    # Register every ORM model on the shared metadata first: a foreign key to a table whose model was never imported
    # fails at the first flush (NoReferencedTableError). The application does this by importing app.main; a CLI must too.
    importlib.import_module("app.db.models")
    from app.db.session import AsyncSessionLocal

    guard(args)
    args.session_factory = None if getattr(args, "sequential", False) else AsyncSessionLocal
    mutates = args.command in ("apply", "refresh") or (args.command == "purge" and not args.dry_run)
    async with contextlib.AsyncExitStack() as stack:
        if mutates:
            await stack.enter_async_context(writer_lock(exclusive=args.command == "purge"))
        db = await stack.enter_async_context(AsyncSessionLocal())
        return await dispatch(db, args)


async def dispatch(db: AsyncSession, args: argparse.Namespace) -> tuple[dict, int]:
    if args.command == "plan":
        targets = await discover(db, prefix=args.site_prefix, overrides=dict(args.integration))
        result = {
            "loader_version": LOADER_VERSION, "sites": [s.code for s in targets.sites], "racks": len(targets.racks), "devices": len(targets.devices),
            "routes": {s.code: {"integration": targets.routes[s.id].integration.name, "collector": targets.routes[s.id].collector.name,
                                "poll_interval_seconds": targets.routes[s.id].integration.poll_interval_seconds} for s in targets.sites},
            "outside_demo_sites": await outside_fingerprint(db, protect_prefix(args)),
        }
        return result, 0
    if args.command == "apply":
        result = await run_apply(db, args)
        if not args.no_verify:
            if clock(args) is None:
                # A long seed outlives the freshness window. Catch up on the live clock first so the live-clock freshness check
                # judges the data a viewer would see now. With a pinned --as-of this is skipped on purpose: run `refresh`, then `verify`.
                catch_up = argparse.Namespace(
                    site_prefix=args.site_prefix, integration=args.integration, watch=False, session_factory=getattr(args, "session_factory", None),
                    catch_up_hours=max(args.history_hours, args.device_history_hours),
                )
                refreshed = await run_refresh(db, catch_up)
                result["catch_up"] = refreshed
                result["ok"] = bool(result["ok"]) and bool(refreshed["ok"])
            targets = await discover(db, prefix=args.site_prefix, overrides=dict(args.integration))
            report = await verify(db, targets, verify_options(args, steps=args.steps))
            result["verify"] = report.as_dict()
            result["ok"] = result["ok"] and report.ok
        return result, 0 if result["ok"] else 1
    if args.command == "refresh":
        result = await run_refresh(db, args)
        return result, 0 if result["ok"] else 1
    if args.command == "verify":
        targets = await discover(db, prefix=args.site_prefix, overrides=dict(args.integration))
        report = await verify(db, targets, verify_options(args))
        return report.as_dict(), 0 if report.ok else 1
    if args.command == "purge":
        targets = await discover(db, prefix=args.site_prefix, overrides=dict(args.integration))
        before = await outside_fingerprint(db, protect_prefix(args))
        purged = await purge(db, targets, dry_run=args.dry_run)
        after = await outside_fingerprint(db, protect_prefix(args))
        unchanged = before["digest"] == after["digest"]
        return {**purged.as_dict(), "outside_demo_sites_unchanged": unchanged}, 0 if unchanged else 1
    raise LoaderError(f"Unknown command {args.command!r}")


def parse_pair(value: str) -> tuple[str, str]:
    site, _, name = value.partition("=")
    if not site or not name:
        raise argparse.ArgumentTypeError("expected SITE_CODE=INTEGRATION_NAME")
    return site, name


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m app.tools.demo_loader", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("plan", "apply", "refresh", "verify", "purge"):
        p = sub.add_parser(name)
        p.add_argument("--site-prefix", default=DEMO_SITE_PREFIX, help="only sites whose code starts with this are touched (e.g. DEMO-SITE-01 for one site)")
        p.add_argument("--integration", action="append", default=[], type=parse_pair, metavar="SITE=NAME", help="choose a site's integration explicitly")
        if name in ("apply", "refresh", "purge"):
            p.add_argument("--confirm-local-demo", action="store_true", help="required: this database is a local demonstration database")
        if name in ("apply", "verify"):
            p.add_argument("--as-of", default=None, type=parse_as_of, metavar="ISO8601", help="pin the sample clock to a timezone-aware, non-future ISO 8601 time (default: the live clock)")
        if name in ("apply", "refresh"):
            p.add_argument("--sequential", action="store_true", help="ingest one collector at a time instead of one session per collector")
        if name in ("apply", "verify"):
            p.add_argument("--device-history-hours", type=float, default=DEFAULT_DEVICE_HISTORY_HOURS, help="history window for the device mappings (default 24)")
            p.add_argument("--expect-racks", type=int, default=None, help="fail unless exactly this many demo racks are in scope (the demo has 100)")
            p.add_argument("--expect-devices", type=int, default=None, help="fail unless exactly this many devices are in scope (the demo has 500)")
            p.add_argument("--history-hours", type=float, default=DEFAULT_HISTORY_HOURS)
        if name == "apply":
            p.add_argument("--steps", type=lambda v: [s.strip() for s in v.split(",")], default=list(STEPS), help=f"comma list of {','.join(STEPS)}")
            p.add_argument("--actor-email", default=None, help="user the floor-plan import/calibration is attributed to (default: oldest active user)")
            p.add_argument("--no-sandbox", action="store_true", help="parse the generated DXF in-process instead of the sandboxed child")
            p.add_argument("--no-verify", action="store_true")
        if name == "refresh":
            p.add_argument("--watch", action="store_true", help="keep running, one cycle per poll interval")
            p.add_argument("--catch-up-hours", type=float, default=DEFAULT_HISTORY_HOURS)
        if name == "purge":
            p.add_argument("--dry-run", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    unknown = [s for s in getattr(args, "steps", []) if s not in STEPS]
    if unknown:
        print(f"unknown step(s): {unknown}; choose from {list(STEPS)}", file=sys.stderr)
        return 2
    try:
        result, code = asyncio.run(run(args))
    except LoaderError as error:
        print(json.dumps({"ok": False, "error": str(error)}), flush=True)
        return 2
    print(json.dumps(result, indent=2, default=str), flush=True)
    return code
