"""Operational power reports (Issue #102, area E).

A report is generated on the Celery `reports` queue from prepared aggregates: the newest closed-hour
`PowerUtilizationSnapshot` per scope, the forecast computed from those snapshots, protection devices'
current state and the rollup's redundancy scenarios. The result is kept as bounded JSON on the job row;
CSV is rendered from that JSON, so both formats always agree.

Job lifecycle: queued -> running -> completed | failed. A claim is a single atomic UPDATE that only wins
for a queued job, or a running job whose lease expired, so two workers never generate the same job and a
rerun after completion is a no-op. Failures store a fixed code, never exception text."""

import csv
import io
import uuid
from datetime import datetime, timedelta

from sqlalchemy import and_, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.power_forecast import forecast_scope
from app.application.power_rollup_loader import rollup_for_site
from app.domain.location.models import Site
from app.domain.power.analytics_models import PowerReportJob, PowerUtilizationSnapshot
from app.domain.power.models import PowerNode, ProtectionDevice

LEASE_SECONDS = 300
MAX_ATTEMPTS = 3
MAX_ROWS = 5000
REPORT_SCHEMA_VERSION = "1"


def _num(v) -> float | None:
    return None if v is None else float(v)


async def build_report(db: AsyncSession, site_id: uuid.UUID | None, now: datetime) -> dict:
    site_ids = [site_id] if site_id else list((await db.execute(select(Site.id).order_by(Site.id))).scalars())
    sites_out = []
    total_rows = 0
    for sid in site_ids:
        site = await db.get(Site, sid)
        if site is None:
            continue
        latest_bucket = (
            await db.execute(
                select(PowerUtilizationSnapshot.bucket_start)
                .where(PowerUtilizationSnapshot.site_id == sid, PowerUtilizationSnapshot.bucket_start < now)
                .order_by(PowerUtilizationSnapshot.bucket_start.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
        snaps = []
        if latest_bucket is not None:
            snaps = list(
                (
                    await db.execute(
                        select(PowerUtilizationSnapshot)
                        .where(
                            PowerUtilizationSnapshot.site_id == sid,
                            PowerUtilizationSnapshot.bucket_start == latest_bucket,
                        )
                        .order_by(PowerUtilizationSnapshot.scope_type, PowerUtilizationSnapshot.scope_id)
                        .limit(MAX_ROWS)
                    )
                ).scalars()
            )
        labels = (
            {
                n.id: n.label
                for n in (
                    await db.execute(
                        select(PowerNode).where(PowerNode.id.in_([s.scope_id for s in snaps if s.scope_type == "power_node"]))
                    )
                ).scalars()
            }
            if snaps
            else {}
        )
        utilization: list[dict] = []
        exceptions: list[dict] = []
        headroom: list[dict] = []
        warnings: list[dict] = []
        for s in snaps:
            label = site.name if s.scope_type == "site" else labels.get(s.scope_id, str(s.scope_id))
            row = {
                "scope_type": s.scope_type,
                "scope_id": str(s.scope_id),
                "label": label,
                "bucket_start": s.bucket_start.isoformat(),
                "load_kw": _num(s.load_kw),
                "load_basis": s.load_basis,
                "allocated_kw": _num(s.allocated_kw),
                "capacity_kw": _num(s.effective_capacity_kw),
                "headroom_kw": _num(s.headroom_kw),
                "utilization_pct": _num(s.utilization_pct),
                "quality": s.quality,
                "sample_count": s.sample_count,
                "coverage_ratio": _num(s.coverage_ratio),
            }
            utilization.append(row)
            if s.headroom_kw is not None:
                headroom.append({k: row[k] for k in ("scope_type", "scope_id", "label", "capacity_kw", "load_kw", "headroom_kw")})
            if s.headroom_kw is not None and float(s.headroom_kw) < 0:
                exceptions.append({**row, "code": "OVERLOAD", "severity": "critical"})
            elif s.utilization_pct is not None and float(s.utilization_pct) >= 95:
                exceptions.append({**row, "code": "NEAR_CAPACITY", "severity": "critical"})
            elif s.utilization_pct is not None and float(s.utilization_pct) >= 80:
                exceptions.append({**row, "code": "HIGH_UTILIZATION", "severity": "warning"})
            if s.quality != "measured":
                warnings.append(
                    {
                        "scope_id": str(s.scope_id),
                        "label": label,
                        "code": f"DATA_{s.quality.upper()}",
                        "detail": f"load basis {s.load_basis}, coverage {_num(s.coverage_ratio)}",
                    }
                )

        devices = (
            await db.execute(
                select(PowerNode, ProtectionDevice)
                .join(ProtectionDevice, ProtectionDevice.power_node_id == PowerNode.id)
                .where(ProtectionDevice.site_id == sid, PowerNode.retired_at.is_(None))
                .order_by(PowerNode.label, PowerNode.id)
            )
        ).all()
        protection = [
            {
                "device_id": str(n.id),
                "label": n.label,
                "state": d.state,
                "status": d.status,
                "rating_a": float(d.rating_a),
                "state_changed_at": d.state_changed_at.isoformat() if d.state_changed_at else None,
            }
            for n, d in devices
        ]
        for p in protection:
            if p["state"] in ("open", "tripped"):
                exceptions.append(
                    {
                        "scope_type": "power_node",
                        "scope_id": p["device_id"],
                        "label": p["label"],
                        "code": f"PROTECTION_{p['state'].upper()}",
                        "severity": "critical",
                    }
                )
            elif p["state"] == "unknown":
                warnings.append(
                    {
                        "scope_id": p["device_id"],
                        "label": p["label"],
                        "code": "PROTECTION_STATE_UNKNOWN",
                        "detail": "state not reported",
                    }
                )

        rollup = await rollup_for_site(db, sid, now)
        redundancy = [
            {
                "equipment_id": str(e.id),
                "label": e.label,
                "scenario": e.scenario,
                "live_inlets": e.live_inlets,
                "inlet_count": e.inlet_count,
                "served": e.served,
            }
            for e in sorted(rollup.equipment.values(), key=lambda x: (x.label, str(x.id)))
        ]
        for r in redundancy:
            if r["scenario"] in ("one_feed_failed", "one_feed_missing", "single_feed", "unserved"):
                exceptions.append(
                    {
                        "scope_type": "equipment",
                        "scope_id": r["equipment_id"],
                        "label": r["label"],
                        "code": f"REDUNDANCY_{r['scenario'].upper()}",
                        "severity": "critical" if r["scenario"] == "unserved" else "warning",
                    }
                )

        fc = await forecast_scope(db, "site", sid, now)
        forecast = {
            "status": fc.status,
            "no_forecast_reason": fc.no_forecast_reason,
            "method": fc.method,
            "current_load_kw": fc.current_load_kw,
            "capacity_kw": fc.capacity_kw,
            "headroom_kw": fc.headroom_kw,
            "exhaustion_date": fc.exhaustion_date.isoformat() if fc.exhaustion_date else None,
            "days_to_exhaustion": fc.days_to_exhaustion,
            "confidence": fc.confidence,
            "sample_count": fc.sample_count,
        }
        if fc.status in ("good", "already_over_capacity") and (fc.days_to_exhaustion is not None and fc.days_to_exhaustion <= 90):
            exceptions.append(
                {"scope_type": "site", "scope_id": str(sid), "label": site.name, "code": "FORECAST_RISK", "severity": "warning"}
            )
        sites_out.append(
            {
                "site_id": str(sid),
                "site_name": site.name,
                "utilization": utilization,
                "exceptions": exceptions,
                "redundancy": redundancy,
                "protection": protection,
                "headroom": headroom,
                "forecast": forecast,
                "data_quality": warnings,
            }
        )
        total_rows += sum(
            len(sites_out[-1][k]) for k in ("utilization", "exceptions", "redundancy", "protection", "headroom", "data_quality")
        )
    return {
        "schema_version": REPORT_SCHEMA_VERSION,
        "report_type": "operational",
        "generated_at": now.isoformat(),
        "metric": "power_kw",
        "unit": "kW",
        "sites": sites_out,
        "row_count": total_rows,
    }


CSV_COLUMNS = [
    "site_id",
    "section",
    "scope_id",
    "label",
    "code_or_scenario",
    "value_kw",
    "capacity_kw",
    "headroom_kw",
    "utilization_pct",
    "quality",
    "detail",
]


def render_csv(result: dict) -> str:
    """Flatten the stored JSON; cells that start with a formula character are neutralised."""
    out = io.StringIO()
    w = csv.writer(out, lineterminator="\n")
    w.writerow(CSV_COLUMNS)

    def safe(v):
        if isinstance(v, str) and v[:1] in ("=", "+", "-", "@", "\t", "\r"):
            return "'" + v
        return "" if v is None else v

    for site in result["sites"]:
        sid = site["site_id"]
        for r in site["utilization"]:
            w.writerow(
                [
                    sid,
                    "utilization",
                    r["scope_id"],
                    safe(r["label"]),
                    r["load_basis"],
                    r["load_kw"],
                    r["capacity_kw"],
                    r["headroom_kw"],
                    r["utilization_pct"],
                    r["quality"],
                    "",
                ]
            )
        for r in site["exceptions"]:
            w.writerow(
                [
                    sid,
                    "exceptions",
                    r["scope_id"],
                    safe(r["label"]),
                    r["code"],
                    r.get("load_kw"),
                    r.get("capacity_kw"),
                    r.get("headroom_kw"),
                    r.get("utilization_pct"),
                    r.get("quality", ""),
                    r["severity"],
                ]
            )
        for r in site["redundancy"]:
            w.writerow(
                [
                    sid,
                    "redundancy",
                    r["equipment_id"],
                    safe(r["label"]),
                    r["scenario"],
                    "",
                    "",
                    "",
                    "",
                    "",
                    f"live {r['live_inlets']}/{r['inlet_count']}",
                ]
            )
        for r in site["protection"]:
            w.writerow([sid, "protection", r["device_id"], safe(r["label"]), r["state"], "", "", "", "", "", r["status"]])
        for r in site["headroom"]:
            w.writerow(
                [
                    sid,
                    "headroom",
                    r["scope_id"],
                    safe(r["label"]),
                    "",
                    r["load_kw"],
                    r["capacity_kw"],
                    r["headroom_kw"],
                    "",
                    "",
                    "",
                ]
            )
        f = site["forecast"]
        w.writerow(
            [
                sid,
                "forecast",
                sid,
                safe(site["site_name"]),
                f["status"],
                f["current_load_kw"],
                f["capacity_kw"],
                f["headroom_kw"],
                "",
                f["confidence"],
                f["no_forecast_reason"] or f["exhaustion_date"] or "",
            ]
        )
        for r in site["data_quality"]:
            w.writerow([sid, "data_quality", r["scope_id"], safe(r["label"]), r["code"], "", "", "", "", "", r["detail"]])
    return out.getvalue()


async def claim_job(db: AsyncSession, job_id: uuid.UUID, now: datetime) -> PowerReportJob | None:
    """Atomic claim: wins for a queued job or a running job with an expired lease; otherwise None."""
    stmt = (
        update(PowerReportJob)
        .where(
            PowerReportJob.id == job_id,
            or_(
                PowerReportJob.status == "queued",
                and_(PowerReportJob.status == "running", PowerReportJob.lease_expires_at < now),
            ),
        )
        .values(
            status="running",
            attempts=PowerReportJob.attempts + 1,
            started_at=now,
            lease_expires_at=now + timedelta(seconds=LEASE_SECONDS),
        )
        .returning(PowerReportJob.id)
    )
    won = (await db.execute(stmt)).first()
    await db.commit()
    if won is None:
        return None
    return await db.get(PowerReportJob, job_id, populate_existing=True)


async def run_report_job(db: AsyncSession, job_id: uuid.UUID, now: datetime) -> str:
    """Generate one job. Returns completed | failed | skipped. Never raises for a generation failure."""
    job = await claim_job(db, job_id, now)
    if job is None:
        return "skipped"
    if job.attempts > MAX_ATTEMPTS:
        job.status, job.failure_code, job.finished_at, job.lease_expires_at = "failed", "ATTEMPTS_EXHAUSTED", now, None
        await db.commit()
        return "failed"
    try:
        result = await build_report(db, job.site_id, now)
        if result["row_count"] > MAX_ROWS:
            raise OverflowError
    except OverflowError:
        await db.rollback()
        return await _fail(db, job_id, "RESULT_TOO_LARGE", now)
    except Exception:
        await db.rollback()
        return await _fail(db, job_id, "GENERATION_FAILED", now)
    fresh = await db.get(PowerReportJob, job_id, populate_existing=True)
    assert fresh is not None
    fresh.status, fresh.result, fresh.row_count = "completed", result, result["row_count"]
    fresh.finished_at, fresh.lease_expires_at, fresh.failure_code = now, None, None
    await db.commit()
    return "completed"


async def _fail(db: AsyncSession, job_id: uuid.UUID, code: str, now: datetime) -> str:
    job = await db.get(PowerReportJob, job_id, populate_existing=True)
    assert job is not None
    job.status, job.failure_code, job.finished_at, job.lease_expires_at, job.result = "failed", code, now, None, None
    await db.commit()
    return "failed"
