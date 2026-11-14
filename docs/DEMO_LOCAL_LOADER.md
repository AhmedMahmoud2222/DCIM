# Local demonstration loader

| | |
| --- | --- |
| Loader version | **1.1.0** (`backend/app/tools/demo_loader/`) |
| Runbook revision | **3**, 2026-11-14. Revision 1: first draft on PR #351. Revision 2: owner review of `3579e3af` (backup and restore safety, image rebuild, rollback with a running refresher, site and ownership boundaries, stronger verification). Revision 3: Codacy AI review (catch-up before live verification, legacy calibration is never overwritten, mapping reuse is validated, equipment location is withheld from callers who cannot see the equipment). |
| Applies to | PR #351 branch `claude/elegant-dijkstra-qry27c` |

**Scope:** a local demonstration database only. The loader refuses to run when `ENVIRONMENT=production`, needs `--confirm-local-demo` for every command that changes data, and selects only sites whose code starts with `DEMO-SITE-`. It does not deploy anything and it does not touch the HRC site. Production deployment is not authorized by this document, and migration `0060` must not be run anywhere that is not a disposable or explicitly approved environment.

The loader is version-controlled code. It is idempotent: a second run finds every object it needs already present and writes nothing.

## What it does

| Step | Name | Result |
| --- | --- | --- |
| 1 | `existing` | 24 hours of readings (one per poll slot, 289 slots including both ends) for each mapping that already exists on a demo-site integration |
| 2 | `sensors` | One combined temperature/humidity rack-inlet sensor per demo rack (`DEMO-ENV-<rack tag>`), commissioned to `active`, mounted on a free front U of its rack, mapped to `temperature_c` and `humidity_percent`, with 24 hours of readings |
| 3 | `floorplans` | Each active demo plan that has no calibration gets a declared-units DXF import (millimetres) and an immutable `declared_units` calibration. Rack positions are validated first and never moved |
| 4 | `devices` | Per equipment record, a `power_kw` and a `load_percent` mapping with an asset-specific source identifier (`demo-sim:<asset tag>:<metric>`), and 24 hours of readings |

All readings go through the same function the collector endpoint calls (`ingest_collector_telemetry`): collector-assignment checks, pinned mapping revisions (`contract_evidence = pinned`), unit conversion (a mapping in °F receives °F numbers and stores °C), de-duplication on `(collector, dedup_key)` and alarm evaluation.

Every simulated reading carries `attributes = {"synthetic": true, "provenance": "demo_loader", "generator": "dcim-demo-loader", "generator_version": "1.1.0", "profile": "diurnal-v1"}` and a dedup key starting `demo-sim:v1:`. The existing mappings continue their real series: the loader reuses the external identifier of the latest real reading and anchors the simulated values on it.

`refresh` repeats the ingest for any slot that is missing in the last 24 hours. The heat maps treat a reading as fresh for three poll intervals (15 minutes at 300 s).

### Boundaries the loader enforces

- **Sites:** `--site-prefix` must start with `DEMO-SITE-` and contain only letters, digits and hyphens. `HRC`, an empty value and anything with a wildcard are refused before any query.
- **Routing:** each selected site needs exactly one enabled integration whose `site_id` is that site, or (only if it has no site) one that you name explicitly with `--integration SITE=NAME` or whose name contains this site's code and no other demo site's code. An integration that belongs to another site is refused even if you name it. One integration cannot serve two sites.
- **Ownership:** if any mapping on a routed integration points at an asset in a site that is not selected, the run stops before writing. A mapping whose asset's site cannot be determined is simulated only when its integration is owned by the demo site by `site_id`; otherwise it is skipped and listed.
- **HRC:** before and after every `apply`, the loader fingerprints everything outside `DEMO-SITE-*` (racks, placements, sensors, plans, integrations, mappings, readings, alarms) and fails the run if it changed. That is detection after the fact; the routing and ownership rules above are what prevent the write.
- **Writers:** `apply` and `refresh` (including `refresh --watch`) hold a shared PostgreSQL advisory lock; a real `purge` needs it exclusively, so a purge refuses to start while any loader or the `demo-refresh` service is still running, and no writer starts during a purge.

## Commands

```text
python -m app.tools.demo_loader plan
python -m app.tools.demo_loader apply   --confirm-local-demo
python -m app.tools.demo_loader refresh --confirm-local-demo [--watch]
python -m app.tools.demo_loader verify
python -m app.tools.demo_loader purge   --confirm-local-demo [--dry-run]
```

Options: `--steps existing,sensors,floorplans,devices`, `--history-hours 24`, `--device-history-hours 24`, `--as-of 2026-11-13T12:00:00Z` (timezone-aware ISO 8601, not in the future; `apply` and `verify` only, `refresh` always uses the live clock), `--sequential`, `--site-prefix DEMO-SITE-01`, `--integration DEMO-SITE-01="<integration name>"`, `--actor-email you@example.com` (user recorded on the floor-plan import and calibration; default is the oldest active user), `--expect-racks 100 --expect-devices 500`.

Every command prints one JSON document on stdout and exits 0 only when everything passed.

## Windows PowerShell

### 0. Prerequisites

- Windows 10/11 with Docker Desktop running (Compose v2: `docker compose version`), Git, and PowerShell 5.1 or 7.
- The repository checked out, the stack previously started from it (a root `.env` with `POSTGRES_PASSWORD`, `DCIM_APP_PASSWORD`, `JWT_SECRET_KEY`, `CREDENTIAL_ENCRYPTION_KEY`), and Docker allowed at least 4 GB of memory and 5 GB of free disk.
- Allow the local scripts for this window only, from the repository root:

```powershell
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
docker compose version
docker compose ps
```

### 1. Back up first, and prove the backup

```powershell
$backup = .\scripts\demo\Backup-DemoDatabase.ps1
Get-Item $backup, "$backup.sha256" | Select-Object Name, Length
```

The script dumps inside the container (so PowerShell never re-encodes the binary), checks that the archive is readable and lists table data, copies it out, proves the copy is byte-identical by SHA-256, and writes `<file>.sha256`. It stops with an error on any failed step and never overwrites an earlier backup. Keep `$backup` for step 7, and keep your existing `dcim-before-ems.dump` as well.

### 2. Install the code

Rebuild **every** image from this commit. `migrate`, `celery-worker`, `celery-beat` and `demo-refresh` are separate Compose services; building only `backend` can leave an older migration set in `migrate` while the new backend expects the severity columns.

```powershell
git fetch origin
git checkout claude/elegant-dijkstra-qry27c
git pull --ff-only
git rev-parse HEAD                                   # record this SHA in your change record

$compose = '-f', 'docker-compose.yml', '-f', 'docker-compose.demo.yml'
docker compose @compose build                        # all services, one commit
if ($LASTEXITCODE -ne 0) { throw 'image build failed; nothing has been started' }

docker compose @compose run --rm migrate             # applies 0060_alarm_severity
if ($LASTEXITCODE -ne 0) { throw 'migration failed; do not start the new services' }

$revision = docker compose @compose run --rm --no-deps migrate alembic current
if ($LASTEXITCODE -ne 0 -or -not ($revision -match '0060_alarm_severity \(head\)')) { throw "database is not at 0060_alarm_severity (head): $revision" }

docker compose @compose up -d backend celery-worker celery-beat frontend
if ($LASTEXITCODE -ne 0) { throw 'service start failed' }
```

Migration `0060` adds `severity` to `alarm_rule` and `alarm` (default `warning`) and nothing else. Existing rules and alarms are stamped `warning` as a placeholder; set the real severity per rule in **Alarm rules → Edit**. The upgrade holds a strong table lock until it commits; run it only when nothing else is using the database, with the backup from step 1.

### 3. Check what the loader will use (read-only)

```powershell
docker compose @compose exec -T backend python -m app.tools.demo_loader plan
```

Confirm three `DEMO-SITE-0n` sites, 100 racks, 500 devices, one integration and one collector per site, and `poll_interval_seconds` 300. If a site's integration is ambiguous or belongs to another site the command says so and exits without writing.

### 4. Seed

**Pilot first.** One site, two hours of device history, in one process (a few minutes):

```powershell
docker compose @compose exec -T backend python -m app.tools.demo_loader apply --confirm-local-demo --site-prefix DEMO-SITE-01 `
  --actor-email you@example.com --device-history-hours 2 --sequential
docker compose @compose exec -T backend python -m app.tools.demo_loader verify --site-prefix DEMO-SITE-01 --device-history-hours 2
```

A pilot leaves real, correctly flagged data in place. The full run below reuses it and fills the rest: the loader skips every slot that is already stored.

**Full run, 24 hours for every series.** The work is limited by the Python process, not by PostgreSQL, so run one process per site. Each changes only its own site and the three are independent:

```powershell
$runId = Get-Date -Format 'yyyyMMdd-HHmmss'
$asOf  = (Get-Date).ToUniversalTime().ToString('yyyy-MM-ddTHH:mm:ssZ')     # one clock for all three, explicit UTC
New-Item -ItemType Directory -Force backups | Out-Null
$jobs = 'DEMO-SITE-01', 'DEMO-SITE-02', 'DEMO-SITE-03' | ForEach-Object {
  Start-Job -Name $_ -ArgumentList $_, $asOf, $runId, $PWD.Path -ScriptBlock {
    param($site, $asOf, $runId, $dir)
    Set-Location $dir
    docker compose -f docker-compose.yml -f docker-compose.demo.yml exec -T backend python -m app.tools.demo_loader apply `
      --confirm-local-demo --site-prefix $site --actor-email you@example.com --as-of $asOf --sequential --no-verify 2>$null |
      Out-File -Encoding utf8 ".\backups\demo-loader-apply-$site-$runId.json"
    $LASTEXITCODE
  }
}
$jobs | Wait-Job | Out-Null
$jobs | ForEach-Object { '{0}: exit {1}' -f $_.Name, (Receive-Job $_ | Select-Object -Last 1) }
```

Every job must print `exit 0`. If one does not, read its JSON file (`failed_collectors`, `rejected` and `error` say what went wrong) and run that site's command again: completed slots are skipped and nothing is duplicated. The full run takes longer than the 15-minute freshness window, so catch up on the live clock and then check the whole result against the same seed clock:

```powershell
docker compose @compose exec -T backend python -m app.tools.demo_loader refresh --confirm-local-demo
docker compose @compose exec -T backend python -m app.tools.demo_loader verify --expect-racks 100 --expect-devices 500 --as-of $asOf
```

(A single-process `apply` without `--as-of` does this catch-up itself before its own verification. With `--as-of` it never replaces your clock with the live one, so run `refresh` and then `verify` as above.)

A single-process run (`apply --confirm-local-demo --actor-email ...` with no `--site-prefix`) does the same work in one process and takes about three times as long.

**Measured throughput** (4-core Linux, PostgreSQL 16, the same 18,297 readings in each mode, 0 rejected, 0 duplicates; reproducible with `backend/scripts/demo_loader_benchmark/run.sh`):

| Mode | Elapsed | Readings/s |
| --- | --- | --- |
| one process, one collector at a time | 169.7 s | 108 |
| one process, one session per collector | 128.8 s | 142 |
| three processes, one per site | 57.4 s | 319 |

The client Python process is the bottleneck (134 s of client CPU against 29 s of database CPU in the sequential run), which is why separate processes scale and extra sessions inside one process do not. A full seed is about 349,000 readings (1,000 device series + 200 sensor series + 9 existing series, 289 slots each): about **54 minutes** in one process or about **18 minutes** with three. **These are projections from the measured rates, not a run against your database or at 100 racks / 500 devices.** The benchmark fixture has no alarm rules, so it does not show the cost or the lock behaviour of an alarm-bearing workload; Docker Desktop's resource limits will also change the figures.

### 5. Keep readings fresh

One catch-up cycle:

```powershell
docker compose @compose exec -T backend python -m app.tools.demo_loader refresh --confirm-local-demo
```

Continuous, as a detached service from the demo override:

```powershell
docker compose @compose up -d demo-refresh
docker compose @compose logs --tail 20 demo-refresh
docker compose @compose stop demo-refresh        # stop it before any rollback (step 7)
```

`demo-refresh` holds the shared writer lock for as long as it runs. A purge will refuse to start until it is stopped.

### 6. Verify

```powershell
docker compose @compose exec -T backend python -m app.tools.demo_loader verify --expect-racks 100 --expect-devices 500
if ($LASTEXITCODE -ne 0) { Write-Warning 'verification failed; read the checks above' }
```

`verify` derives, for the selected sites, every series that should exist (each existing mapping, both metrics of each rack sensor, both metrics of each device) and checks that **every** slot of its window is stored under its loader key. It also checks that all simulated readings are flagged synthetic and pinned, rack and equipment counts, one active combined sensor per rack placed in a rack of its own site and room, both sensor metrics fresh on the live clock, each demo plan calibrated with valid rack geometry, and non-empty temperature and humidity heat-map grids per room.

Then open **Telemetry** for a device or sensor (a day of points), **Floor plans → room → Heat map**, **Events** (severity chips) and **Equipment** (Site, Building, Floor, Room, Rack columns).

### 7. Roll back

**A. Remove only what the loader created** (readings, loader mappings, loader sensors, and the alarms the loader opened). Stop the refresher first, then preview, then delete:

```powershell
docker compose @compose stop demo-refresh
docker compose @compose exec -T backend python -m app.tools.demo_loader purge --dry-run
docker compose @compose exec -T backend python -m app.tools.demo_loader purge --confirm-local-demo
```

`purge` is scoped to the selected `--site-prefix` sites and refuses to run while any loader process is writing. It deletes an alarm only if it carries the loader origin marker written when the alarm was opened by a simulated reading and its latest reading is still simulated. **A real alarm that simulated readings later retained or cleared is kept, with its acknowledgement history,** but its latest reading and last value were changed while the simulation ran and are not restored; only the backup restores that. Floor-plan calibrations are immutable by database trigger and the audit log is append-only, so `purge` leaves both.

**B. Return to the backup** (also removes calibrations and migration `0060`). The restore script stops every writer including `demo-refresh`, checks the backup file against its manifest, copies it into the container, re-checks the checksum there, test-restores it into a throwaway database, and only then drops and recreates the real one. Any failure before that point ends the script with your database untouched:

```powershell
.\scripts\demo\Restore-DemoDatabase.ps1 -BackupPath $backup -ConfirmDestroyLocalDemoDatabase
```

The script does not restart anything. A pre-`0060` dump needs pre-`0060` code. Check out the commit the backup was taken from, rebuild **all** images, and start them; do not start the new images against the restored schema:

```powershell
git checkout <the commit you ran before this PR>
docker compose @compose build
docker compose @compose up -d backend celery-worker celery-beat frontend
```

## What has and has not been tested

| Tested in CI and on a disposable PostgreSQL 16 (Linux) | Not tested here |
| --- | --- |
| The loader end to end: seed, rerun, refresh, purge, routing and ownership refusals, site boundary, failure and resume (parallel and sequential), `verify` gap detection, writer lock, strict `--as-of`, HRC unchanged | The PowerShell scripts and every PowerShell block above were **not executed** (no PowerShell or Docker daemon in the build environment). `backend/tests/unit/test_demo_powershell_scripts.py` checks their structure only: native exit codes are checked, nothing destructive precedes the backup checks, no path or timestamp variable is reused. Run them against a disposable Compose project first |
| Migration `0060` up, down, up on a clean database; `alembic check` | Upgrading an already-built previous stack; a rollback with `demo-refresh` already running on Docker Desktop (the writer lock is tested in-process) |
| Compose override merges and lists `demo-refresh` (`docker compose config`) | Your dataset: real plan geometry, real integration/site assignment, the full 100-rack / 500-device seed, alarm-bearing workloads |

## Known limits

- Simulated values are plausible, not measured. Do not use them for capacity or compliance decisions.
- Calibration declares that the drawing's millimetres equal room millimetres with the origin at the room corner. Existing rack positions keep their meaning because they are already room-local millimetres. A plan that already carries a legacy pixel scale (`calibration_scale_mm_per_px`) but no calibration record is skipped and reported, never overwritten.
- An existing mapping whose source identifier starts `demo-sim:` is reused only if it is provably this loader's (label, metric, canonical unit, scale 1, matching current revision); otherwise the run stops before writing.
- The API's equipment `move` endpoint checks the permission but not the caller's site scope. That predates this work and is not changed here; the new `location` field in its response is withheld unless the caller can see the equipment.
- A rack with no free front U gets no sensor; the loader reports it as a problem instead of moving equipment. A sensor tag too long for the 64-character column keeps its start and gains a hash suffix.
- Simulated readings can open alarms if a rule threshold is crossed; their severity is the rule's severity.
- Provisioning commits in chunks, so `apply` is not atomic. It is safe to interrupt and run again.
