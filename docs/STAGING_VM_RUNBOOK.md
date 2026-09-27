# Dedicated staging VM operator runbook (draft; do not execute without owner authorization)

Baseline: [plan](STAGING_VM_DEPLOYMENT_PLAN.md), [merged implementation](DEPLOYMENT_IMPLEMENTATION.md), [merged governance documentation (PR #40)](DEPLOYMENT_GOVERNANCE.md). Commands below are a **proposed** Ubuntu 24.04 procedure; owner supplies actual private VM addresses, repository SSH trust, disk device, authorized release SHA and token. Never paste secret values into a shell history, ticket or capture. Stop on any failed check. Do not run the CI disposable `real_compose_validation.py` against this VM. No command in this document has been executed on a staging host.

## 0. Decision gate and VM preflight

Record owner staging authorization separately before section 5. Confirm the owner-managed private HTTPS DNS, trusted certificate, VPN-restricted reverse proxy and matching CORS origin required by the plan are in place; no production route/customer data is accessible. On clean Ubuntu, an infrastructure administrator executes the following after checking the VM's assigned disk device and approved SSH/VPN CIDR; commands involving placeholders must be filled locally and reviewed before execution.

```bash
cat /etc/os-release
uname -m
lsblk -f
df -h / /var/lib/docker
ip route
sudo systemctl status ssh --no-pager
```

Provision/mount the approved **separate** database disk at `/var/lib/dcim-staging` using its UUID in `/etc/fstab`, with encrypted-at-rest storage per owner policy; do not format an unidentified device. Verify `findmnt /var/lib/dcim-staging` and `df -h /var/lib/dcim-staging`; stop if absent. Reserve separate backup destination `/var/backups/dcim` and arrange an off-VM encrypted copy. Check Docker bridge subnet 172.20.0.0/16 against host/VPN routes.

## 1. Docker and access foundation (infrastructure administrator)

Install Docker Engine and Compose plugin from the organization's approved signed Ubuntu Docker package repository (follow its current package instructions); install `git`, `python3`, `curl`, `util-linux`, `openssh-client`, `jq`, `ufw`, and `postgresql-client`. Do not use an unverified convenience installer. Verify:

```bash
docker version
docker compose version
docker info >/dev/null
command -v flock git python3 curl jq psql
sudo systemctl enable --now docker
sudo adduser --disabled-password --gecos '' dcim-deploy
sudo usermod -aG docker dcim-deploy
sudo install -d -o dcim-deploy -g dcim-deploy -m 0700 /opt/dcim /var/backups/dcim /var/log/dcim
sudo install -d -m 0700 /var/lib/dcim-staging/postgres
```

Use an approved operator public key in `~dcim-deploy/.ssh/authorized_keys` (0700 directory, 0600 file), and a separate read-only repository deploy key (0600) with pinned `github.com` host key. Docker group provides root-equivalent authority: only named trusted operators may assume this identity. `sudo -iu dcim-deploy` starts a fresh group session. The combined Compose file binds `${POSTGRES_DATA_PATH:-/var/lib/dcim/postgres}` to `/var/lib/postgresql/data` in `postgres:16`. For the **first empty** staging disk only, after confirming `findmnt /var/lib/dcim-staging` shows the dedicated filesystem and the image digest is recorded, the infrastructure administrator runs:

```bash
PG_UID="$(docker run --rm --entrypoint id postgres:16 -u postgres)"
PG_GID="$(docker run --rm --entrypoint id postgres:16 -g postgres)"
[[ "$PG_UID" =~ ^[0-9]+$ && "$PG_GID" =~ ^[0-9]+$ ]] || exit 1
test "$(stat -c %U:%G /var/lib/dcim-staging/postgres)" = root:root || exit 1
test -z "$(sudo find /var/lib/dcim-staging/postgres -mindepth 1 -maxdepth 1 -print -quit)" || exit 1
sudo chown -- "$PG_UID:$PG_GID" /var/lib/dcim-staging/postgres
sudo chmod 0700 /var/lib/dcim-staging/postgres
test "$(stat -c %u:%g:%a /var/lib/dcim-staging/postgres)" = "$PG_UID:$PG_GID:700"
```

Stop if this is an existing volume, the mount is missing, the image differs from the Compose release, or any check fails. Never recursively chown existing PostgreSQL files: restore and ownership assessment need a separate reviewed procedure. The numeric UID/GID and mode verification are evidence; no secret is printed.

Restrict inbound firewall before allowing administrative access: deny inbound by default; allow TCP 22 only from **approved bastion/VPN CIDRs**; deny 5432, 6379, 8000 and 8080 on external interfaces. Verify from a second SSH session before enabling firewall so operators do not lose access. Restrict outbound to approved DNS/NTP/apt/GitHub API/git/container registry destinations, and explicitly block production/customer/operational DCIM prefixes, including traffic forwarded from Docker bridge. Host UFW rules alone may not filter Docker forwarding: validate effective nftables/DOCKER-USER controls and perform an external negative connectivity test. Owner provisions the **private HTTPS ingress before login testing**: resolve `staging.<owner-domain>` on approved VPN clients, install a client-trusted certificate for that name, permit TCP 443 only from approved VPN CIDRs, and reverse-proxy both `/` and `/api/` to VM `http://127.0.0.1:8080` while forwarding Host and scheme headers. Verify `https://staging.<owner-domain>/` with ordinary TLS validation; do not use `curl -k`. No plain HTTP tunnel is an approved browser login path. No reverse-proxy installation command is provided because the host/proxy provider, certificate source and owner network policy have not been selected; this is a blocking infrastructure decision, not an already-implemented repository service.

## 2. Release selection and checkout (deployment operator)

In GitHub select the full 40-character lowercase commit ID from a **push-to-main** commit, inspect the exact SHA's CI and Deployment validation runs and all required job results. Confirm branch settings required checks and any applicable path-filtered Deployment Tools Test. Manually dispatch `.github/workflows/deploy.yml` with this SHA and `staging`; require the final validation record to pass. That dispatch does not deploy to the VM. Write the SHA, run IDs/attempts and approver to a restricted release record without secrets.

Clone using the separately authorized read-only SSH deploy key into an empty `/opt/dcim` owned by `dcim-deploy` (GitHub SSH host key preverified); the repository is private. As `dcim-deploy`:

```bash
cd /opt/dcim
git clone git@github.com:AhmedMahmoud2222/DCIM.git .
git fetch --prune origin main
git remote -v
git status --porcelain
git rev-parse --show-toplevel
git rev-parse origin/main
```

Set `RELEASE_SHA` to the reviewed exact SHA via `read -r RELEASE_SHA` (paste only SHA). Verify `[[ "$RELEASE_SHA" =~ ^[0-9a-f]{40}$ ]]`, `git cat-file -t "$RELEASE_SHA"` equals `commit`, and `git merge-base --is-ancestor "$RELEASE_SHA" origin/main`. Do not run the deployment script from an untrusted branch, modified tracked checkout or PR head. Preserve Git safe-directory ownership: do not run Git as root and do not set global `safe.directory '*'`.

## 3. Stage-only configuration (deployment operator, private terminal)

Set a restrictive umask (`umask 077`) and generate separate random secrets under an approved secret vault procedure. Generate URL-safe **alphanumeric** passwords for Postgres and app role; the init script inserts the app password into SQL and the Compose database URL without escaping. Generate Fernet via `python3 -c 'import base64,secrets; print(base64.urlsafe_b64encode(secrets.token_bytes(32)).decode())'` in the private secrets procedure. Keep its recovery copy with the database backup access policy. Never capture generator stdout in the evidence log. Create `/opt/dcim/.env` mode 0600 owned by deploy identity, with this template (replace placeholders only within an approved secret editor/vault transfer; do not commit):

```dotenv
COMPOSE_PROJECT_NAME=dcim-staging
POSTGRES_DB=dcim_staging
POSTGRES_DATA_PATH=/var/lib/dcim-staging/postgres
POSTGRES_PASSWORD=<stage-only-alphanumeric-secret>
DCIM_APP_PASSWORD=<distinct-stage-only-alphanumeric-secret>
JWT_SECRET_KEY=<stage-only-random-secret>
CREDENTIAL_ENCRYPTION_KEY=<stage-only-fernet-key>
CORS_ALLOWED_ORIGINS='["https://staging.<owner-domain>"]'
```

Replace `<owner-domain>` with the **same exact private DNS name** in the trusted TLS certificate, browser URL and `CORS_ALLOWED_ORIGINS`; the origin is case-sensitive for the scheme/host and uses implicit port 443 (no trailing slash). Frontend nginx proxies same-origin `/api/` to the backend; direct cross-origin API access is not part of this smoke. The production override deliberately keeps `ENVIRONMENT: production`: refresh and CSRF cookies therefore have `Secure` and `SameSite=Strict` and are scoped to `/api/v1/auth`. Do not set `ENVIRONMENT=development`, disable cookie security or use HTTP for login. The app role creation only runs on an **empty** PostgreSQL data directory; changing `DCIM_APP_PASSWORD` in `.env` afterward does not alter the stored role password. Plan a coordinated database role rotation and app update; never rotate the Fernet key by replacing `.env` alone.

Provide a short-lived fine-grained GitHub API token with Contents/Actions/Checks read. Enter through an approved private terminal `read -rs GITHUB_TOKEN; export GITHUB_TOKEN` without echo or tracing; confirm only presence (`test -n "${GITHUB_TOKEN:-}"`). The script uses it for API verification. Before invoking the script, load the file in the **same shell** using `set -a; . /opt/dcim/.env; set +a`. Because this executes shell syntax, only load a trusted, owner-controlled file with characters and quoting reviewed first; avoid arbitrary content. The template's JSON value must be shell quoted (e.g. `CORS_ALLOWED_ORIGINS='["https://staging.<owner-domain>"]'`) so quotes survive. Do not `cat .env` or publish environment or full Compose JSON. Prefer a reviewed vault injection process in a follow-up PR.

## 4. Non-disruptive preflight (deployment operator)

```bash
cd /opt/dcim
umask 077
test "$(stat -c %a .env)" = 600
test "$(stat -c %U .env)" = dcim-deploy
test -z "$(git status --porcelain --untracked-files=no)"
test -n "${GITHUB_TOKEN:-}"
set -a; . ./.env; set +a
for k in POSTGRES_PASSWORD DCIM_APP_PASSWORD JWT_SECRET_KEY CREDENTIAL_ENCRYPTION_KEY POSTGRES_DATA_PATH; do test -n "${!k:-}" || exit 1; done
test "${#CREDENTIAL_ENCRYPTION_KEY}" -eq 44
python3 -c 'import base64,os; k=os.environ["CREDENTIAL_ENCRYPTION_KEY"]; assert len(base64.urlsafe_b64decode(k))==32'
docker compose version --short | python3 -c 'import sys; v=tuple(map(int,sys.stdin.read().strip().lstrip("v").split(".")[:3])); assert v >= (2,24,4), v'
docker compose -f docker-compose.yml -f docker-compose.production.yml config --quiet
docker compose -f docker-compose.yml -f docker-compose.production.yml config --format json | jq '{ports: (.services | with_entries(select(.key | IN("postgres","redis","backend","frontend")) | .value = [.value.ports[] | {host_ip, published, target}])), database_mount: [.services.postgres.volumes[] | select(.target == "/var/lib/postgresql/data") | {type, source, target}]}'
python3 .github/scripts/verify_release_sha.py "$RELEASE_SHA" > /dev/null
findmnt /var/lib/dcim-staging
df -h / /var/lib/dcim-staging
ss -ltn
curl -sS -o /dev/null -w '%{http_code} TLS-verify=%{ssl_verify_result}\n' 'https://staging.<owner-domain>/'
```

Manually confirm Compose version >=2.24.4, at least 20 GiB and 20% free on build disk and 20% on data disk, exact `POSTGRES_DATA_PATH` mount, no external listeners or project name collision. To inspect effective bindings without disclosing other JSON fields, pipe `docker compose -f docker-compose.yml -f docker-compose.production.yml config --format json` through the narrowly selected `jq` filter above; no raw output or shell tracing. Confirm each of postgres/redis/backend/frontend binds 127.0.0.1 and database target is `/var/lib/postgresql/data`. The pre-deployment HTTPS curl above can return a proxy upstream error while the stack is stopped; TLS and private DNS must validate now, and HTTP 200 is required after deployment. Capture only filtered output. Pre-migration backup is mandatory on subsequent deployments; first empty database still needs a tested restoration path. Stop if release verifier fails, any data directory contains unexpected files, or backup cannot be verified.

## 5. First deployment (only after separate staging authorization)

Run under `dcim-deploy` from `/opt/dcim`, with token and `.env` variables already exported; operator confirms source SHA and host at the terminal:

```bash
cd /opt/dcim
bash scripts/deploy-docker-compose.sh "$RELEASE_SHA" staging
unset GITHUB_TOKEN POSTGRES_PASSWORD DCIM_APP_PASSWORD JWT_SECRET_KEY CREDENTIAL_ENCRYPTION_KEY
```

Do not redirect full output into a public artifact. Script fetches main, verifies exact release evidence again, detaches checkout to selected SHA, builds/starts combined Compose and waits up to 90 seconds by default for readiness and all eight states. It runs migration and privileged bootstrap via `up -d --build` dependency ordering; **do not run either manually again as part of normal deployment**. The script stores `.deployment-sha`, prior SHA in `.previous-deployment-sha`, and a private `.env` backup only when a previous successful release exists. On first failure, it attempts Compose down without `-v`; investigate manually and do not assume a recovered running service. On subsequent failure it tries rollback to previous SHA/config; a schema downgrade is not included.

## 6. Verification and sanitized evidence

```bash
cd /opt/dcim
docker compose -f docker-compose.yml -f docker-compose.production.yml ps -a
curl -fsS -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8000/api/v1/health/ready
curl -fsS -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8080/api/v1/health/ready
curl -fsS -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8080/
curl -fsS -o /dev/null -w '%{http_code} TLS-verify=%{ssl_verify_result}\n' 'https://staging.<owner-domain>/'
curl -fsS -o /dev/null -w '%{http_code} TLS-verify=%{ssl_verify_result}\n' 'https://staging.<owner-domain>/api/v1/health/ready'
docker compose -f docker-compose.yml -f docker-compose.production.yml exec -T postgres psql -U postgres -d dcim_staging -Atc 'SELECT version_num FROM alembic_version;'
git rev-parse HEAD
cat .deployment-sha
df -h / /var/lib/dcim-staging
```

Expect postgres/redis/backend `healthy`, worker/beat/frontend `running`, migrate/bootstrap `exited (0)`, all three local HTTP codes `200`, Git HEAD and state SHA equal. Inspect service logs locally with `docker compose ... logs --tail=100 <service>` **without exporting raw lines**. From an approved VPN browser, visit exactly `https://staging.<owner-domain>/` with a valid trusted certificate; same-origin frontend requests use `https://staging.<owner-domain>/api/v1/…`. Authenticate a synthetic test user provisioned by the application's reviewed admin-creation procedure, navigate inventory/catalog and verify refresh and logout. Browser developer tools should show `dcim_refresh_token` (HttpOnly) and `dcim_csrf_token` cookies with `Secure`, `SameSite=Strict`, path `/api/v1/auth`; refresh requests must send both cookies and `X-CSRF-Token`, return success without a CORS error, and no auth request may downgrade to HTTP. Capture only redacted cookie **attributes**, HTTP status, origin, certificate validation and smoke outcome, never cookie/header values or tokens. No admin is automatically created by Compose. Record timestamps, release SHA, masked run IDs, Docker/Compose versions, filtered states, migration revision, HTTPS and local HTTP codes, synthetic smoke outcomes, backup ID, reviewer and alert thresholds. Avoid `docker inspect` environment fields, shell `env`, full Compose output, secret-containing logs and database records. Configure host alert checks for readiness, container restart counts, disk thresholds, database backup age, and worker/beat liveness; test alert route.

## 7. Synthetic failure and recovery evidence checklist

Use only approved synthetic data, an isolated project/VM and a second authorized **main** SHA when testing rollback. The release verifier will reject ad hoc bad commits, so migration/startup fault injection belongs in a separate disposable CI fixture or a reviewed main test release; never corrupt the shared deployment checkout to bypass verification. For each case record precondition, exact command/reference, return code, sanitized status, SHA state, marker row count and stop decision.

| Rehearsal | Precondition and action | Expected result and evidence | Stop condition |
| --- | --- | --- | --- |
| Invalid/unauthorized SHA | No rollout active; pass malformed SHA, then a full non-main SHA to script/verifier. | Nonzero before checkout/Compose; current SHA and stack unchanged. | Any service or state changed. |
| Missing/malformed secret | Separate fresh shell with missing key or invalid Fernet key; run `config --quiet` and script preflight only. | Nonzero before `DEPLOY_ACTIVE`; no secret echoed; note that script checks key length, not Fernet decoding, hence explicit preflight. | Logs leak value or existing stack changed. |
| Lock contention | CI fixture or two controlled processes with `LOCK_TIMEOUT=2`; first holds same stable lock during authorized rollout. | Second fails `deployment lock unavailable`, inode unchanged; no concurrent Compose action. | First process unhealthy or lock identity changes. |
| Migration/boot failure | Disposable fixture's synthetic failing release; CI `real_compose_validation.py` already covers migration/bootstrap/backend failure. | Nonzero, prior SHA/config restored and services healthy, persistent marker survives. | Failed rollback, incompatible schema or data inconsistency. |
| Prior SHA recovery | Two successful main releases after verified backup; induce **only** a reviewed safe failure in disposable fixture or scheduled staging window. | `.deployment-sha` remains prior good SHA; checkout/config match; HTTP 200 and marker retained. | Recovery not healthy within bounded window. |
| Configuration rollback | Back up 0600 `.env`; controlled bad nonsecret config through reviewed procedure after successful prior release. | Prior private config restored by rollback, SHA unchanged, config hash matches protected prior hash (hash may be recorded). | Backup absent or secrets exposed. |
| Database preservation/restore | Create synthetic marker, logical backup before change; restore into a **different empty isolated** Postgres data directory/project first. | Row count and migration revision match backup; key recovery copy available without publishing key; `down` without `-v` preserves live bind mount. | Backup invalid, target not isolated, or rollback requires incompatible schema. |
| Deliberately failed rollback | Disposable fixture injects rollback Docker failure, as CI test does; staging-host fault only with explicit rehearsal authorization and recovery operator present. | Nonzero plus `ROLLBACK FAILED`, prior state remains a historical marker, service availability treated as unknown; operator stops writes, preserves logs/data, restores backup to isolated target and chooses reviewed recovery SHA. | Any temptation to retry blindly or delete volumes. |

Do not use `docker compose down --volumes`, run tests against customer data, or assume code rollback undoes migration. A failed rollback is an incident: stop traffic/writers, retain bind-mounted database, collect sanitized diagnostics, compare schema to backup, restore into isolated target, obtain reviewer sign-off, then resume only after full readiness/functional verification. Evidence and recovery runbook require independent review before staging acceptance. Production approval, secrets, ingress and restore objectives require a separate design and decision.
