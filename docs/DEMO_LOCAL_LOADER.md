# Local demonstration loader (v1.0.0)

**Scope:** a local demonstration database only. The loader refuses to run when `ENVIRONMENT=production`, needs `--confirm-local-demo` for every command that changes data, and selects only sites whose code starts with `DEMO-SITE-`. It does not deploy anything and it does not touch the HRC site. Production deployment is not authorized by this document.

The loader is version-controlled code in `backend/app/application/demo_loader/`. It is idempotent: a second run finds every object it needs already present and writes nothing.

## What it does

| Step | Name | Result |
| --- | --- | --- |
| 1 | `existing` | 24 hours of readings (one per poll slot, 288 per series) for each mapping that already exists on a demo-site integration |
| 2 | `sensors` | One combined temperature/humidity rack-inlet sensor per demo rack (`DEMO-ENV-<rack tag>`), commissioned to `active`, mounted on a free front U of its rack, mapped to `temperature_c` and `humidity_percent`, with 24 hours of readings |
| 3 | `floorplans` | Each active demo plan that has no calibration gets a declared-units DXF import (millimetres) and an immutable `declared_units` calibration. Rack positions are validated first and never moved |
| 4 | `devices` | Per equipment record, a `power_kw` and a `load_percent` mapping with an asset-specific source identifier (`demo-sim:<asset tag>:<metric>`), and 24 hours of readings |

All readings go through the same function the collector endpoint calls (`ingest_collector_telemetry`). That gives you collector-assignment checks (a record for an integration not assigned to the collector is rejected), pinned mapping revisions (`contract_evidence = pinned`), unit conversion (a mapping in °F receives °F numbers and stores °C), de-duplication on `(collector, dedup_key)` and alarm evaluation.

Every simulated reading carries `attributes = {"synthetic": true, "provenance": "demo_loader", "generator": "dcim-demo-loader", "generator_version": "1.0.0", "profile": "diurnal-v1"}` and a dedup key starting `demo-sim:v1:`. The existing nine mappings continue their real series: the loader reuses the external identifier of the latest real reading and anchors the simulated values on it.

`refresh` repeats the ingest for any slot newer than the last one stored. Run it with `--watch` (or the optional compose override below) to keep readings fresh; the heat maps treat a reading as fresh for three poll intervals (15 minutes at 300 s).

## Commands

```text
python -m app.application.demo_loader plan                       # read-only: sites, racks, devices, integration and collector per site
python -m app.application.demo_loader apply   --confirm-local-demo
python -m app.application.demo_loader refresh --confirm-local-demo [--watch]
python -m app.application.demo_loader verify
python -m app.application.demo_loader purge   --confirm-local-demo [--dry-run]
```

Useful options: `--steps existing,sensors,floorplans,devices`, `--history-hours 24`, `--device-history-hours 24`, `--as-of 2026-11-13T12:00:00Z` (timezone-aware ISO 8601, not in the future, apply only; refresh always uses the live clock), `--sequential`, `--site-prefix`, `--protect-prefix`, `--integration DEMO-SITE-01="<integration name>"` (only when the loader cannot pick a site's integration by `site_id` or by the site code in its name), `--actor-email you@example.com` (user recorded on the floor-plan import and calibration; default is the oldest active user), `--expect-racks 100 --expect-devices 500`.

Every command prints one JSON document on stdout and exits 0 only when everything passed.

## Windows PowerShell

Run these from the repository root with Docker Desktop running. Replace the placeholders. The commands assume the Compose project in this repository (`postgres`, `backend`) and the values in your root `.env`.

### 1. Back up first

`>` redirection can corrupt a binary dump in Windows PowerShell 5.1, so dump inside the container and copy the file out.

```powershell
$stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
New-Item -ItemType Directory -Force backups | Out-Null
docker compose exec -T postgres sh -c 'pg_dump -U postgres -Fc -d "$POSTGRES_DB" -f /tmp/dcim-before-demo-loader.dump'
docker compose cp postgres:/tmp/dcim-before-demo-loader.dump ".\backups\dcim-before-demo-loader-$stamp.dump"
Get-Item ".\backups\dcim-before-demo-loader-$stamp.dump" | Select-Object Name, Length
```

Keep the existing `dcim-before-ems.dump` as well.

### 2. Install the code

```powershell
git fetch origin
git checkout claude/elegant-dijkstra-qry27c
git pull --ff-only
docker compose build backend frontend
docker compose run --rm migrate          # applies 0060_alarm_severity (adds alarm severity; nothing else)
docker compose up -d backend celery-worker celery-beat frontend
```

Migration `0060_alarm_severity` adds `severity` to `alarm_rule` and `alarm` (default `warning`). Existing rules and alarms are stamped `warning` as a placeholder; set the real severity per rule in **Alarm rules**. Review the migration before running it against data you care about, and keep the backup from step 1.

### 3. Check what the loader will use (read-only)

```powershell
docker compose exec backend python -m app.application.demo_loader plan
```

Confirm three `DEMO-SITE-0n` sites, 100 racks, 500 devices, one integration and one collector per site, and `poll_interval_seconds` 300. If a site's integration is ambiguous the command says so and tells you the `--integration` argument to add.

### 4. Seed

**Pilot first.** One site, two hours of device history, to prove the pipeline on your data before the long run (a few minutes):

```powershell
docker compose exec backend python -m app.application.demo_loader apply --confirm-local-demo --site-prefix DEMO-SITE-01 `
  --actor-email you@example.com --device-history-hours 2 --sequential
docker compose exec backend python -m app.application.demo_loader verify --site-prefix DEMO-SITE-01
```

A pilot leaves real, correctly flagged data in place; the full run below reuses it and fills the rest (the loader skips every slot already stored).

**Full run, 24 hours for every series.** The work is Python-CPU bound, so run one process per site; each changes only its own site, and the three are independent:

```powershell
$stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
$asOf  = (Get-Date).ToUniversalTime().ToString('yyyy-MM-ddTHH:mm:ssZ')     # one clock for all three, explicit UTC
$jobs = 'DEMO-SITE-01','DEMO-SITE-02','DEMO-SITE-03' | ForEach-Object {
  Start-Job -Name $_ -ArgumentList $_, $asOf, $stamp -ScriptBlock {
    param($site, $asOf, $stamp)
    Set-Location $using:PWD
    docker compose exec -T backend python -m app.application.demo_loader apply --confirm-local-demo --site-prefix $site `
      --actor-email you@example.com --as-of $asOf --sequential --no-verify 2>$null |
      Out-File -Encoding utf8 ".\backups\demo-loader-apply-$site-$stamp.json"
    $LASTEXITCODE
  }
}
$jobs | Wait-Job | Out-Null
$jobs | ForEach-Object { '{0}: exit {1}' -f $_.Name, ((Receive-Job $_) | Select-Object -Last 1) }
```

Every job must print `exit 0`. If one does not, read its JSON file (`failed_collectors` and `rejected` say what went wrong) and run that site's command again: finished slots are skipped, nothing is duplicated.

A single-process run (`apply --confirm-local-demo --actor-email ...`, no `--site-prefix`) does the same work in one process and is simpler; it is about three times slower.

**Measured throughput** (4-core Linux, PostgreSQL 16, same 18,297 readings in each mode, 0 rejected, 0 duplicates; see `backend/tests/integration/test_demo_loader_benchmark.py` and the table in the PR):

| Mode | Elapsed | Readings/s |
| --- | --- | --- |
| one process, one collector at a time | 169.7 s | 108 |
| one process, one session per collector | 128.8 s | 142 |
| three processes, one per site | 57.4 s | 319 |

The client Python process, not PostgreSQL, is the bottleneck (134 s of client CPU against 29 s of database CPU in the sequential run), which is why separate processes scale and extra sessions inside one process do not. A full seed is about 349,000 readings (1,000 device series + 200 sensor series + 9 existing series, 289 slots each): about **54 minutes** in one process or about **18 minutes** with three. These are projections from the measured rates, not a run against your database; your hardware and Docker Desktop's resource limits will change them.

### 5. Keep readings fresh

One catch-up cycle:

```powershell
docker compose exec backend python -m app.application.demo_loader refresh --confirm-local-demo
```

Continuous (leave the window open, or use the compose override):

```powershell
docker compose exec backend python -m app.application.demo_loader refresh --confirm-local-demo --watch
# or, detached:
docker compose -f docker-compose.yml -f docker-compose.demo.yml up -d demo-refresh
```

### 6. Verify

```powershell
docker compose exec backend python -m app.application.demo_loader verify --expect-racks 100 --expect-devices 500
if ($LASTEXITCODE -ne 0) { Write-Warning 'verification failed; read the checks above' }
```

The checks cover: rack and equipment counts unchanged; history has many points per series; no slot stored twice; every simulated reading flagged synthetic and pinned; every device has readings; one active combined sensor per rack, placed in a rack of its own site and room, with two mappings and fresh data; each demo plan calibrated with valid rack geometry; and non-empty temperature and humidity heat-map grids per room.

Row-level checks you can run yourself:

```powershell
docker compose exec -T postgres sh -c 'psql -U postgres -d "$POSTGRES_DB" -c "select s.code, count(*) from site s group by s.code order by 1;"'
docker compose exec -T postgres sh -c 'psql -U postgres -d "$POSTGRES_DB" -c "select (select count(*) from rack) racks, (select count(*) from equipment) equipment, (select count(*) from environmental_sensor) sensors;"'
```

Then open **Telemetry** for a device or sensor (the chart shows a day of points), **Floor plans → room → Heat map** (temperature and humidity), **Events** (severity chips) and **Equipment** (Site, Building, Floor, Room, Rack columns).

### 7. Roll back

Remove only what the loader created (readings, loader mappings, loader sensors and their alarms):

```powershell
docker compose exec backend python -m app.application.demo_loader purge --dry-run
docker compose exec backend python -m app.application.demo_loader purge --confirm-local-demo
```

Floor-plan calibrations are immutable by database trigger, and the audit log is append-only, so `purge` leaves both. To undo those, or to return to the exact earlier state (including removing migration `0060`), restore the backup:

```powershell
docker compose stop backend celery-worker celery-beat
docker compose cp ".\backups\dcim-before-demo-loader-$stamp.dump" postgres:/tmp/restore.dump
docker compose exec -T postgres sh -c 'dropdb -U postgres --if-exists "$POSTGRES_DB" && createdb -U postgres "$POSTGRES_DB" && pg_restore -U postgres -d "$POSTGRES_DB" --no-owner /tmp/restore.dump'
docker compose run --rm bootstrap-privileges
docker compose up -d backend celery-worker celery-beat
```

Restoring a dump taken before the migration also restores the earlier schema; check out the earlier commit and rebuild the images so the code matches.

## Safety properties (and how they are checked)

- **HRC and every non-demo site are untouched.** `apply` takes a digest of all racks, equipment placements, sensors, plans, calibrations, integrations and mappings outside the `DEMO-SITE-` sites before and after, prints both, and fails if they differ.
- **Counts do not change.** The loader creates no racks and no equipment, and `--expect-racks/--expect-devices` fail the run if they are not 100 and 500.
- **No duplicates.** Sensors and mappings are looked up before creation (unique asset tag and unique `(integration, source_identifier)`), and readings are de-duplicated by the collector contract; `verify` checks that no slot is stored twice.
- **Local only.** Refuses `ENVIRONMENT=production`; needs `--confirm-local-demo`.

## Known limits

- Simulated values are plausible, not measured. Do not use them for capacity or compliance decisions.
- Calibration declares the drawing's millimetres equal to room millimetres with the origin at the room corner. Existing rack positions keep their meaning because they are already room-local millimetres.
- A rack with no free front U gets no sensor; the loader reports it as a problem instead of moving equipment.
- Simulated readings can open alarms if an alarm rule's threshold is crossed; their severity is the rule's severity.
