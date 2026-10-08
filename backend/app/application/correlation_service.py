"""Deterministic event correlation (Issue #103, area A).

The engine reads alarms, collector transitions, protection-device state and the power topology, and writes
only `correlation_incident` / `correlation_member` rows that *reference* them. It never updates, deletes or
hides an alarm or a transition, never clears an alarm because an incident is resolved, and never runs inside
the ingestion path (it is a scheduled task over already-committed facts).

Rules, evaluated in this order; an alarm joins at most one incident:

1. collector_offline    a collector went offline (a recorded transition) and alarms opened on integrations
                        assigned to it within WINDOW_SECONDS of the detection. Cause: the transition. High
                        confidence when the alarm is an availability alarm, medium otherwise.
2. shared_power_cause   two or more alarms (or one alarm plus an alarm on the feeding asset) whose equipment
                        shares an upstream power node that is either a protection device in an interrupting
                        state or the subject of its own alarm. Cause: that node. High for a tripped/open
                        device, medium for an alarmed upstream asset. Evidence carries the path.
3. same_device          two or more alarms on one asset within the window. Cause: the earliest. Low.
4. same_integration     two or more alarms on one integration within the window. Cause: the earliest. Low.

Alarms that fit no rule stay unrelated and are never forced into an incident.

Identity: `dedup_key` is derived from the cause (transition id, protection device id plus the time of its
last state change, or the cause alarm id), so replaying the same facts, in any order, any number of times,
finds the same incident and adds no duplicate; members are unique per source row. Windows are measured on
source timestamps (`opened_at`, `detected_at`), never on processing time, so late or out-of-order arrival
does not change the grouping. A single advisory lock serialises runs; the unique constraints are the backstop.
"""

import hashlib
import uuid
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from sqlalchemy import select, text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.application import itsm_service
from app.application.audit_service import write_audit_log
from app.application.notification_service import enqueue_for_incident
from app.application.outbox_service import write_outbox_event
from app.application.power_graph import GraphTraversalBounded, get_upstream_node_ids
from app.domain.alarm.models import Alarm, AlarmRule
from app.domain.integration.models import CollectorAssignment, Integration
from app.domain.operations.models import (
    CollectorTransition,
    CorrelationIncident,
    CorrelationIncidentMember,
)
from app.domain.power.models import PowerNode, ProtectionDevice

METHOD_VERSION = "1"
WINDOW_SECONDS = 300
LOOKBACK_HOURS = 24
LOCK_KEY = 1_030_002
WINDOW = timedelta(seconds=WINDOW_SECONDS)


@dataclass
class _Member:
    kind: str  # alarm | collector_transition
    ref: uuid.UUID
    role: str
    time: datetime


@dataclass
class _Plan:
    rule: str
    dedup_key: str
    cause_type: str
    cause_ref: str
    cause_label: str
    cause_time: datetime
    confidence: str
    rationale: str
    evidence: list[dict]
    members: list[_Member]
    site_id: uuid.UUID | None
    causation_id: str


@dataclass
class CorrelationSummary:
    created: list[uuid.UUID] = field(default_factory=list)
    extended: list[uuid.UUID] = field(default_factory=list)
    unchanged: int = 0


def _key(*parts: object) -> str:
    raw = ":".join(str(p) for p in parts)
    return raw if len(raw) <= 150 else hashlib.sha256(raw.encode()).hexdigest()


def _iso(ts: datetime) -> str:
    return ts.astimezone(UTC).isoformat()


def _alarm_item(a: Alarm, rule: AlarmRule | None, role: str) -> dict:
    return {
        "type": "alarm",
        "role": role,
        "alarm_id": str(a.id),
        "subject_key": a.subject_key,
        "rule_name": rule.name if rule else None,
        "opened_at": _iso(a.opened_at),
        "status": a.status,
        "managed_asset_id": str(a.managed_asset_id) if a.managed_asset_id else None,
    }


async def correlate(db: AsyncSession, *, now: datetime | None = None, lookback_hours: int = LOOKBACK_HOURS) -> CorrelationSummary:
    now = now or datetime.now(UTC)
    await db.execute(text("SELECT pg_advisory_xact_lock(:k)"), {"k": LOCK_KEY})
    cutoff = now - timedelta(hours=lookback_hours)

    member_alarm_rows = await db.execute(
        select(CorrelationIncidentMember.alarm_id).where(CorrelationIncidentMember.alarm_id.is_not(None))
    )
    assigned_alarm_ids: set[uuid.UUID] = {i for i in member_alarm_rows.scalars() if i is not None}
    existing_keys = set(
        (
            await db.execute(select(CorrelationIncident.dedup_key).where(CorrelationIncident.opened_at >= cutoff - WINDOW))
        ).scalars()
    )
    alarms = [
        a
        for a in (
            await db.execute(
                select(Alarm).where(Alarm.opened_at >= cutoff, Alarm.opened_at <= now).order_by(Alarm.opened_at, Alarm.id)
            )
        ).scalars()
    ]
    pool = [a for a in alarms if a.id not in assigned_alarm_ids]
    rules = (
        {r.id: r for r in (await db.execute(select(AlarmRule).where(AlarmRule.id.in_({a.rule_id for a in alarms})))).scalars()}
        if alarms
        else {}
    )
    integrations = (
        {
            i.id: i
            for i in (
                await db.execute(select(Integration).where(Integration.id.in_({a.integration_id for a in alarms})))
            ).scalars()
        }
        if alarms
        else {}
    )
    collector_of = (
        dict(
            (
                await db.execute(
                    select(CollectorAssignment.integration_id, CollectorAssignment.collector_id).where(
                        CollectorAssignment.effective_to.is_(None), CollectorAssignment.integration_id.in_(list(integrations))
                    )
                )
            ).all()
        )
        if integrations
        else {}
    )

    plans: list[_Plan] = []
    taken: set[uuid.UUID] = set()  # alarm ids planned into an incident during this run
    transitions = list(
        (
            await db.execute(
                select(CollectorTransition)
                .where(CollectorTransition.to_state == "offline", CollectorTransition.detected_at >= cutoff - WINDOW)
                .order_by(CollectorTransition.detected_at, CollectorTransition.id)
            )
        ).scalars()
    )

    # Rule 1: collector offline
    for t in transitions:
        symptoms = [
            a
            for a in pool
            if a.id not in taken
            and collector_of.get(a.integration_id) == t.collector_id
            and abs(a.opened_at - t.detected_at) <= WINDOW
        ]
        if not symptoms:
            continue
        members = [_Member("collector_transition", t.id, "cause", t.detected_at)] + [
            _Member("alarm", a.id, "symptom", a.opened_at) for a in symptoms
        ]
        availability = [a for a in symptoms if rules.get(a.rule_id) and rules[a.rule_id].rule_type == "availability_unavailable"]
        evidence = [
            {
                "type": "collector_offline",
                "role": "cause",
                "transition_id": str(t.id),
                "collector_id": str(t.collector_id),
                "collector_name": t.collector_name,
                "detected_at": _iso(t.detected_at),
                "last_heartbeat_at": _iso(t.heartbeat_at) if t.heartbeat_at else None,
            }
        ] + [_alarm_item(a, rules.get(a.rule_id), "symptom") for a in symptoms]
        plans.append(
            _Plan(
                "collector_offline",
                _key("collector_offline", t.id),
                "collector_transition",
                str(t.id),
                f"Collector {t.collector_name} offline",
                t.detected_at,
                "high" if availability else "medium",
                f"Collector {t.collector_name} stopped sending heartbeats at {_iso(t.detected_at)}; {len(symptoms)} alarm(s) "
                f"on integrations it serves opened within {WINDOW_SECONDS} s of that.",
                evidence,
                members,
                t.site_id,
                str(t.id),
            )
        )
        taken.update(a.id for a in symptoms)

    # Rule 2: shared upstream power cause
    plans.extend(
        await _power_plans(
            db,
            [a for a in pool if a.id not in taken],
            alarms,
            rules,
            integrations,
            taken,
            existing_keys,
            assigned_alarm_ids,
        )
    )

    # Rules 3 and 4: same device / same integration
    seeds = await _weak_rule_seeds(db, cutoff)
    for rule_name, group_key in (("same_device", lambda a: a.managed_asset_id), ("same_integration", lambda a: a.integration_id)):
        groups: dict[uuid.UUID, list[Alarm]] = defaultdict(list)
        seeded = seeds.get(rule_name, {})
        for a in [*pool, *[s for s in seeded.values() if s.id not in taken]]:
            k = group_key(a)
            if a.id not in taken and k is not None:
                groups[k].append(a)
        for k in sorted(groups, key=str):
            for cluster in _chain(groups[k]):
                if len(cluster) < 2:
                    continue
                cause = next((a for a in cluster if a.id in seeded), cluster[0])
                cluster = [cause, *[a for a in cluster if a.id != cause.id]]
                members = [_Member("alarm", cause.id, "cause", cause.opened_at)] + [
                    _Member("alarm", a.id, "symptom", a.opened_at) for a in cluster[1:]
                ]
                subject = "asset" if rule_name == "same_device" else "integration"
                integ = integrations.get(cause.integration_id)
                plans.append(
                    _Plan(
                        rule_name,
                        _key(rule_name, cause.id),
                        "alarm",
                        str(cause.id),
                        f"First alarm: {cause.subject_key}",
                        cause.opened_at,
                        "low",
                        f"{len(cluster)} alarms on the same {subject} opened within {WINDOW_SECONDS} s of each other; the "
                        "earliest is the candidate cause. No topology link was established.",
                        [_alarm_item(cause, rules.get(cause.rule_id), "cause")]
                        + [_alarm_item(a, rules.get(a.rule_id), "symptom") for a in cluster[1:]],
                        members,
                        integ.site_id if integ else None,
                        str(cause.id),
                    )
                )
                taken.update(a.id for a in cluster)

    return await _persist(db, plans, now)


async def _weak_rule_seeds(db: AsyncSession, cutoff: datetime) -> dict[str, dict[uuid.UUID, Alarm]]:
    """Cause alarms of existing same_device / same_integration incidents, so a late alarm can join them
    (their key is derived from that cause alarm and must not change)."""
    rows = (
        await db.execute(
            select(CorrelationIncident.rule, Alarm)
            .join(CorrelationIncidentMember, CorrelationIncidentMember.incident_id == CorrelationIncident.id)
            .join(Alarm, Alarm.id == CorrelationIncidentMember.alarm_id)
            .where(
                CorrelationIncident.rule.in_(("same_device", "same_integration")),
                CorrelationIncidentMember.role == "cause",
                CorrelationIncident.opened_at >= cutoff - WINDOW,
            )
        )
    ).all()
    out: dict[str, dict[uuid.UUID, Alarm]] = {"same_device": {}, "same_integration": {}}
    for rule, alarm in rows:
        out[rule][alarm.id] = alarm
    return out


def _chain(alarms: list[Alarm]) -> list[list[Alarm]]:
    """Split time-sorted alarms into clusters where consecutive gaps are within the window."""
    out: list[list[Alarm]] = []
    for a in sorted(alarms, key=lambda x: (x.opened_at, str(x.id))):
        if out and a.opened_at - out[-1][-1].opened_at <= WINDOW:
            out[-1].append(a)
        else:
            out.append([a])
    return out


async def _power_plans(
    db: AsyncSession,
    pool: list[Alarm],
    all_alarms: list[Alarm],
    rules: dict,
    integrations: dict,
    taken: set[uuid.UUID],
    existing_keys: set[str],
    assigned_alarm_ids: set[uuid.UUID],
) -> list[_Plan]:
    asset_alarms = [a for a in pool if a.managed_asset_id is not None]
    if len(asset_alarms) == 0:
        return []
    cause_source = [a for a in all_alarms if a.managed_asset_id is not None]
    asset_ids = {a.managed_asset_id for a in cause_source}
    nodes = list(
        (
            await db.execute(
                select(PowerNode).where(
                    PowerNode.retired_at.is_(None),
                    (PowerNode.managed_asset_id.in_(asset_ids)) | (PowerNode.owning_asset_id.in_(asset_ids)),
                )
            )
        ).scalars()
    )
    nodes_by_asset: dict[uuid.UUID, list[PowerNode]] = defaultdict(list)
    for n in nodes:
        nodes_by_asset[(n.managed_asset_id or n.owning_asset_id)].append(n)  # type: ignore[index]

    upstream: dict[uuid.UUID, set[uuid.UUID]] = {}  # alarm id -> upstream power node ids
    own_nodes: dict[uuid.UUID, set[uuid.UUID]] = {}
    for a in asset_alarms:
        ups: set[uuid.UUID] = set()
        for n in nodes_by_asset.get(a.managed_asset_id, []):  # type: ignore[arg-type]
            try:
                ups |= await get_upstream_node_ids(db, n.id)
            except GraphTraversalBounded:
                continue
        upstream[a.id] = ups
        own_nodes[a.id] = {n.id for n in nodes_by_asset.get(a.managed_asset_id, [])}  # type: ignore[arg-type]

    all_upstream = set().union(*upstream.values()) if upstream else set()
    if not all_upstream:
        return []
    devices = {
        d.power_node_id: d
        for d in (await db.execute(select(ProtectionDevice).where(ProtectionDevice.power_node_id.in_(all_upstream)))).scalars()
    }
    labels = {n.id: n for n in (await db.execute(select(PowerNode).where(PowerNode.id.in_(all_upstream)))).scalars()}
    # alarms on the feeding assets themselves
    cause_alarm_for_node: dict[uuid.UUID, Alarm] = {}
    for a in cause_source:
        for n in nodes_by_asset.get(a.managed_asset_id, []):  # type: ignore[arg-type]
            cause_alarm_for_node.setdefault(n.id, a)

    candidates: list[tuple[uuid.UUID, str, datetime, Alarm | None]] = []
    for nid in sorted(all_upstream, key=str):
        dev = devices.get(nid)
        if dev is not None and dev.state in ("open", "tripped") and dev.state_changed_at is not None:
            candidates.append((nid, "protection_device", dev.state_changed_at, None))
        elif nid in cause_alarm_for_node:
            found = cause_alarm_for_node[nid]
            candidates.append((nid, "alarm", found.opened_at, found))

    scored: list[tuple[int, str, tuple]] = []
    for cand in candidates:
        nid, kind, ctime, ca = cand
        syms = [
            a
            for a in asset_alarms
            if nid in upstream[a.id] and (ca is None or a.id != ca.id) and abs(a.opened_at - ctime) <= WINDOW
        ]
        scored.append((len(syms), str(nid), cand))
    scored.sort(key=lambda s: (-s[0], s[1]))

    plans: list[_Plan] = []
    for _count, _, (nid, _kind, ctime, ca) in scored:
        syms = [
            a
            for a in asset_alarms
            if a.id not in taken
            and nid in upstream[a.id]
            and (ca is None or a.id != ca.id)
            and abs(a.opened_at - ctime) <= WINDOW
        ]
        if ca is not None and ca.id in taken:
            continue
        if ca is not None:
            plan_key = _key("shared_power_cause", "alarm", ca.id)
        else:
            dev0 = devices.get(nid)
            plan_key = (
                _key("shared_power_cause", "device", nid, int(dev0.state_changed_at.timestamp()))
                if dev0 is not None and dev0.state_changed_at is not None
                else ""
            )
        exists = plan_key in existing_keys
        if ca is not None and ca.id in assigned_alarm_ids and not exists:
            continue  # the cause alarm already belongs to another incident
        needed = 1 if (exists or ca is not None) else 2
        if len(syms) < needed:
            continue
        node = labels.get(nid)
        node_label = node.label if node else str(nid)
        dev = devices.get(nid)
        evidence: list[dict] = []
        members: list[_Member] = []
        if ca is not None:
            members.append(_Member("alarm", ca.id, "cause", ca.opened_at))
            evidence.append(_alarm_item(ca, rules.get(ca.rule_id), "cause"))
            dedup = _key("shared_power_cause", "alarm", ca.id)
            confidence = "medium"
            cause_type, cause_ref = "alarm", str(ca.id)
            rationale = (
                f"{len(syms)} alarm(s) on equipment fed through {node_label}, which has its own alarm opened at "
                f"{_iso(ca.opened_at)}; treated as the shared upstream cause."
            )
        else:
            assert dev is not None and dev.state_changed_at is not None
            dedup = _key("shared_power_cause", "device", nid, int(dev.state_changed_at.timestamp()))
            confidence = "high"
            cause_type, cause_ref = "protection_device", str(nid)
            rationale = (
                f"{len(syms)} alarm(s) on equipment downstream of protection device {node_label}, which is {dev.state} "
                f"since {_iso(dev.state_changed_at)}; the interruption explains the downstream alarms."
            )
            evidence.append(
                {
                    "type": "protection_state",
                    "scope": "power",
                    "role": "cause",
                    "power_node_id": str(nid),
                    "label": node_label,
                    "state": dev.state,
                    "state_changed_at": _iso(dev.state_changed_at),
                }
            )
        for a in sorted(syms, key=lambda x: (x.opened_at, str(x.id))):
            members.append(_Member("alarm", a.id, "symptom", a.opened_at))
            evidence.append(_alarm_item(a, rules.get(a.rule_id), "symptom"))
            path_labels = sorted(labels[u].label for u in upstream[a.id] if u in labels and (u == nid))
            evidence.append(
                {
                    "type": "topology_path",
                    "scope": "power",
                    "alarm_id": str(a.id),
                    "through": node_label or path_labels,
                    "upstream_node_count": len(upstream[a.id]),
                }
            )
        site = None
        first = syms[0] if syms else ca
        integ = integrations.get(first.integration_id) if first else None
        if integ is not None:
            site = integ.site_id
        plans.append(
            _Plan(
                "shared_power_cause",
                dedup,
                cause_type,
                cause_ref,
                node_label,
                ctime,
                confidence,
                rationale,
                evidence,
                members,
                site,
                cause_ref,
            )
        )
        taken.update(a.id for a in syms)
        if ca is not None:
            taken.add(ca.id)
    return plans


async def _persist(db: AsyncSession, plans: list[_Plan], now: datetime) -> CorrelationSummary:
    summary = CorrelationSummary()
    for plan in plans:
        corr = f"incident:{hashlib.sha256(plan.dedup_key.encode()).hexdigest()[:24]}"
        opened_at = min(m.time for m in plan.members)
        last_at = max(m.time for m in plan.members)
        new_id = uuid.uuid4()
        inserted = (
            await db.execute(
                insert(CorrelationIncident)
                .values(
                    id=new_id,
                    dedup_key=plan.dedup_key,
                    rule=plan.rule,
                    cause_type=plan.cause_type,
                    cause_ref=plan.cause_ref,
                    cause_label=plan.cause_label[:256],
                    confidence=plan.confidence,
                    rationale=plan.rationale,
                    evidence=plan.evidence,
                    status="open",
                    site_id=plan.site_id,
                    opened_at=opened_at,
                    last_member_at=last_at,
                    correlation_id=corr,
                    causation_id=plan.causation_id,
                    method_version=METHOD_VERSION,
                    version=1,
                )
                .on_conflict_do_nothing(constraint="uq_correlation_incident_dedup_key")
                .returning(CorrelationIncident.id)
            )
        ).first()
        if inserted is not None:
            incident_id = inserted[0]
            created = True
        else:
            incident_id = (
                await db.execute(select(CorrelationIncident.id).where(CorrelationIncident.dedup_key == plan.dedup_key))
            ).scalar_one()
            created = False
        added = 0
        for m in plan.members:
            values = {
                "id": uuid.uuid4(),
                "incident_id": incident_id,
                "member_type": m.kind,
                "role": m.role,
                "source_time": m.time,
                "added_at": now,
                "alarm_id": m.ref if m.kind == "alarm" else None,
                "transition_id": m.ref if m.kind == "collector_transition" else None,
            }
            ins = (
                insert(CorrelationIncidentMember)
                .values(**values)
                .on_conflict_do_nothing()
                .returning(CorrelationIncidentMember.id)
            )
            if (await db.execute(ins)).first() is not None:
                added += 1
        if created:
            summary.created.append(incident_id)
            incident = await db.get(CorrelationIncident, incident_id)
            assert incident is not None
            await write_audit_log(
                db,
                actor_user_id=None,
                action="correlation.incident.open",
                entity_type="correlation_incident",
                entity_id=incident_id,
                request_id=None,
                correlation_id=corr,
                source="system",
                after={"rule": plan.rule, "confidence": plan.confidence, "members": added, "cause": plan.cause_label},
            )
            await write_outbox_event(
                db,
                event_type="IncidentOpened",
                aggregate_type="correlation_incident",
                aggregate_id=incident_id,
                payload={"rule": plan.rule, "confidence": plan.confidence, "members": added},
                correlation_id=corr,
                causation_id=plan.causation_id,
            )
            await enqueue_for_incident(db, incident, event_type="incident.opened")
            await itsm_service.queue_for_incident(db, incident)
        elif added:
            incident = await db.get(CorrelationIncident, incident_id, with_for_update=True, populate_existing=True)
            assert incident is not None
            incident.last_member_at = max(incident.last_member_at, last_at)
            incident.evidence = plan.evidence
            incident.rationale = plan.rationale
            incident.version += 1
            await db.flush()
            summary.extended.append(incident_id)
            await write_audit_log(
                db,
                actor_user_id=None,
                action="correlation.incident.extend",
                entity_type="correlation_incident",
                entity_id=incident_id,
                request_id=None,
                correlation_id=corr,
                source="system",
                after={"added_members": added},
            )
            await enqueue_for_incident(db, incident, event_type="incident.updated", suffix=f":v{incident.version}")
        else:
            summary.unchanged += 1
    await db.commit()
    return summary
