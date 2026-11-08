# SEC-06 Edge Collector durable queue storage encryption

Parent: [Phase 11 security backlog #38](https://github.com/AhmedMahmoud2222/DCIM/issues/38).
The owner approved **OS-level encryption** (LUKS/dm-crypt full-disk, or at minimum
full-partition, encryption) as a required production Edge Collector appliance
deployment control for this finding. **SQLCipher or any other application-level
encryption inside `edge_collector/queue.py` was explicitly considered and rejected
by the owner.** This document is the operator runbook for that control; it is not
a proposal, and nothing here should be read as reopening the SQLCipher option.

This is an operational/deployment document only. It does not change
`edge_collector/queue.py`, `edge_collector/config.py`, or any other application
code. `SQLiteQueue` has, and keeps, zero awareness of disk encryption: it opens
`CollectorConfig.database_path` exactly as before and the mapped, already-unlocked
filesystem underneath is the entire control.

## Why

`SQLiteQueue` (`edge_collector/queue.py`) persists unacknowledged telemetry
observations to a local SQLite WAL-mode database file at
`CollectorConfig.database_path` (`edge_collector/config.py`; operators set this via
the collector's `--database` flag, see `edge_collector/README.md` — the shipped
example is `/var/lib/dcim/queue.db`). Each queued row (`queue_records` in
`edge_collector/migrations/001_initial.sql`) stores a `record_id`, the observation's
`occurred_at` timestamp, its full `payload_json` (the telemetry reading itself) and
retry bookkeeping. Today the only protection on that file is host-OS directory
permissions (the collector's README asks operators to run it under a dedicated
service account and restrict the queue directory).

The risk this closes (SEC-06) is **physical or host-level access to the appliance's
disk** — a stolen, decommissioned, lost-in-transit, or improperly wiped appliance
whose storage medium (SSD/eMMC/HDD) is read directly, bypassing the host OS and its
directory permissions entirely. It is not a remote network exposure: an attacker
with network access to a running collector is already constrained by the existing
transport/auth controls (HMAC-signed requests to Central, SNMP credential handling
described in `edge_collector/README.md`), which this finding does not change.

## Decision

**Required production control:** full-disk, or at minimum full-partition, LUKS
(`dm-crypt`) encryption of the volume that backs `CollectorConfig.database_path`,
established as a mandatory step of Edge appliance provisioning.

**Rejected alternative:** application-level encryption (e.g. SQLCipher /
`pysqlcipher3` in place of the standard library `sqlite3` module used by
`SQLiteQueue`). The owner rejected this for SEC-06. Do not add a SQLCipher
dependency, change how `SQLiteQueue` opens or reads `queue.db`, or introduce an
in-application encryption key to satisfy this finding — that is explicitly out of
scope and was already decided against. OS-level encryption protects the same data
at rest with no code change, no in-process key material, and no per-row
performance or migration cost, because `SQLiteQueue` never has to know its
filesystem is encrypted.

## Deployment steps (initial appliance provisioning)

These steps provision the encrypted volume that `CollectorConfig.database_path`
(the directory containing `queue.db`) resolves under. Run them once, during
appliance imaging, before the Edge Collector package or service is installed.
Substitute your appliance's actual device/partition names; `/dev/sdX1` below is
illustrative.

1. **Partition** the target device, leaving a separate unencrypted boot partition
   (for the bootloader and, if used, a keyfile — see "Boot/unlock handling"
   below) and a partition for the encrypted data volume:
   ```sh
   parted /dev/sdX --script \
     mklabel gpt \
     mkpart boot 1MiB 512MiB \
     mkpart dcim-data 512MiB 100%
   ```

2. **Initialize LUKS** on the data partition. Use LUKS2 with a strong KDF; the
   passphrase/keyfile used here is consumed only at this step and during the
   boot-unlock step below — it is never read by the Edge Collector process:
   ```sh
   cryptsetup luksFormat --type luks2 --cipher aes-xts-plain64 --key-size 512 \
     --hash sha256 --pbkdf argon2id /dev/sdX2
   ```

3. **Open (unlock) the mapping** to get the decrypted block device, naming it
   something identifiable per-appliance:
   ```sh
   cryptsetup luksOpen /dev/sdX2 edge-queue
   ```
   This creates `/dev/mapper/edge-queue`, the device the rest of the OS (and this
   runbook's verification script) treats as the real disk.

4. **Create a filesystem** on the mapped (decrypted) device, never on the raw
   partition:
   ```sh
   mkfs.ext4 -L dcim-queue /dev/mapper/edge-queue
   ```

5. **Mount at the path `CollectorConfig.database_path` resolves under.** If the
   collector is started with `--database /var/lib/dcim/queue.db`, mount the
   decrypted filesystem at `/var/lib/dcim` (or a parent of it) before the
   collector service starts, and keep ownership/permissions matching the
   dedicated service account documented in `edge_collector/README.md`:
   ```sh
   mkdir -p /var/lib/dcim
   mount /dev/mapper/edge-queue /var/lib/dcim
   chown dcim-edge:dcim-edge /var/lib/dcim
   chmod 0700 /var/lib/dcim
   ```
   Add the corresponding `/etc/crypttab` and `/etc/fstab` entries (see boot
   handling below) so this mount is reconstructed identically on every boot,
   **before** the Edge Collector service unit starts (`systemd` `RequiresMountsFor=`
   or an equivalent ordering dependency on the appliance's init system).

6. **Confirm the Edge Collector process sees only the mounted, decrypted
   filesystem.** Nothing in `edge_collector/queue.py` or `config.py` changes:
   `SQLiteQueue.__init__` still just does
   `self.config.database_path.parent.mkdir(parents=True, exist_ok=True)` and opens
   the SQLite file at that path. The collector has no `cryptsetup`/LUKS
   awareness, no decryption key, and no code path that could accidentally bypass
   the mount — that separation of concerns is the point of choosing OS-level
   encryption over an application-level one.

## Boot/unlock handling

An appliance must unlock its encrypted data volume at boot without a human typing
a passphrase at the console every reboot (unattended/remote reboots and power-cycle
recovery need to work). Two supported options, in order of preference:

### Recommended: TPM-sealed key (appliances with a TPM 2.0)

Seal the LUKS key to the TPM so it only releases when boot measurements (PCRs)
match a known-good state, using `systemd-cryptenroll` (or `clevis` with a `tpm2`
pin, if that is the appliance's existing tooling):
```sh
systemd-cryptenroll /dev/sdX2 --tpm2-device=auto --tpm2-pcrs=7
```
This keeps an existing passphrase/recovery slot enrolled (step 2 above) in
addition to the TPM slot — do not remove it; it is the recovery path if the TPM
or its sealed state is ever unavailable (e.g. after a firmware update that
changes measured PCRs).

**What this protects against:** a disk removed from the appliance and read on any
other hardware — the TPM never releases the key off that specific, unmodified
appliance. **What this does not protect against:** an attacker with sustained
physical access to the *running, already-unlocked* appliance (the same is true of
every unattended-unlock design, including the keyfile fallback below) or with the
ability to extract the key from a compromised, still-booted OS.

### Fallback: keyfile on separate boot-time-only readable media (no TPM)

If the appliance model has no TPM, store a high-entropy keyfile (not a
human-memorable passphrase) on a storage location that is readable only during
early boot and is **not the encrypted volume it unlocks and not the same
filesystem exposed to normal appliance operation** — for example a small,
separately mounted boot partition that is unmounted (or remounted read-only and
access-restricted) once the system reaches multi-user state:
```sh
dd if=/dev/urandom of=/boot-keys/edge-queue.key bs=512 count=4
chmod 0400 /boot-keys/edge-queue.key
cryptsetup luksAddKey /dev/sdX2 /boot-keys/edge-queue.key
```
Reference it from `/etc/crypttab` so the init system unlocks the volume
automatically at boot using that keyfile.

**Do not** place this keyfile inside the encrypted volume it unlocks (that is
circular and unrecoverable) and do not leave it readable from the appliance's
normal unencrypted root filesystem at runtime. **What this protects against:**
casual disk removal and offline reading of the data partition alone.
**What this does not protect against:** anyone with access to the boot partition
or boot-time filesystem itself (unlike a TPM seal, a keyfile does not verify the
requesting hardware/firmware state) — document this explicitly as the accepted
tradeoff for non-TPM appliance models, and prefer the TPM path wherever hardware
support exists.

### If neither is acceptable: physical presence at boot

For appliance models/deployments where the threat model requires it, operators
may instead choose to require a human to type the LUKS passphrase at each boot
(no `crypttab` automation, no keyfile, no TPM enrollment). This is the strongest
option against automated/remote compromise of the unlock mechanism itself, at the
operational cost that unattended reboots will hang at the console waiting for
input. Document this choice explicitly per appliance class if selected; it is a
deliberate tradeoff, not an oversight.

## Reimage, replacement and recovery procedures

### Decommissioning or replacing an appliance (secure wipe)

Because the data partition is LUKS-encrypted, **cryptographic erasure is
sufficient** and is both faster and more certain than an overwrite-based wipe of
the full disk: destroying every LUKS key slot's header material makes the
underlying ciphertext permanently unrecoverable without ever reading or
overwriting the (potentially large) data partition itself.
```sh
cryptsetup luksErase /dev/sdX2
```
(Equivalently, `cryptsetup luksKillSlot` for all slots, or physically securely
erasing the TPM-sealed/keyfile key material if it is what's being retired rather
than the disk.) Follow this with your organization's standard hardware chain-of-
custody/decommissioning paperwork; a physical overwrite pass is not required to
consider the queued telemetry data destroyed, because without the key the
ciphertext is computationally unrecoverable.

### Provisioning a fresh replacement appliance

Re-run the "Deployment steps" above from a fresh device: new LUKS header, new
key material (TPM-sealed or new keyfile per that appliance), new filesystem.
Appliances are not expected to share LUKS key material; each gets its own.

### In-flight queued-but-unacknowledged telemetry on a lost/destroyed appliance

If an appliance is lost, destroyed, or has its LUKS key erased before its queued
records are acknowledged by Central, that data is gone. This is an **accepted,
bounded data-loss window**, consistent with the existing queue design rather than
a new risk introduced by this control:

- `SQLiteQueue` already bounds how much unacknowledged data can exist at all:
  `CollectorConfig.retention_seconds` (default 24 hours) expires records older
  than that on every `enqueue`/`list_due` call (`SQLiteQueue._expire`), and
  `CollectorConfig.max_records` (default 10,000) / `max_bytes` (default 64MiB)
  evict the oldest records once those caps are hit (`SQLiteQueue._evict_for`).
- So the worst-case loss from a destroyed appliance was already bounded, before
  this control, to at most `retention_seconds` of telemetry (or less, if volume
  hits `max_records`/`max_bytes` sooner) — the same bound documented operationally
  for routine delivery failures. LUKS encryption does not change this bound; it
  only ensures that *at-rest reading* of a lost appliance's disk cannot recover
  that same bounded, already-time-boxed data either.
- Record the loss window (appliance ID, approximate last-successful-ACK time, and
  the configured `retention_seconds`/`max_records` for that appliance) in your
  incident record per `docs/OPERATIONS.md`'s incident-response section.

## Verifiable checklist / evidence

Run this for every appliance before it is accepted into production, and again
after any reimage or disk replacement. Record the result in your deployment
change record alongside the rest of `docs/OPERATIONS.md`'s "Deployment evidence
register" checklist (see that document's final section — this is an additional
item for appliances, not a replacement for the existing list):

1. **Confirm the mount is live and backed by the mapped device**, not the raw
   partition:
   ```sh
   lsblk -o NAME,TYPE,MOUNTPOINT /dev/sdX2
   cryptsetup status edge-queue
   ```
2. **Run the automated verification script** (`backend/scripts/verify_edge_luks_encryption.sh`,
   described below) against the collector's queue mountpoint, from the
   appliance itself:
   ```sh
   backend/scripts/verify_edge_luks_encryption.sh /var/lib/dcim
   ```
   It must print `COMPLIANT: ...` and exit `0`. Record its full output (including
   the resolved underlying raw device) in the change record.
3. **Confirm unlock-at-boot works without manual intervention** (TPM or keyfile
   path), or that the chosen physical-presence tradeoff is documented for that
   appliance class, by rebooting the appliance (in a maintenance window) and
   confirming the collector service reaches a healthy state without console
   interaction (unless physical-presence unlock was explicitly chosen).
4. **Confirm the keyfile/TPM key material location** does not reside on the
   encrypted volume itself (`findmnt` the keyfile's path, if a keyfile is used,
   and confirm it resolves to a different device than `/dev/mapper/edge-queue`).
5. Record in the operations change record (`docs/OPERATIONS.md`'s "Deployment
   evidence register"): appliance identifier, LUKS device/mapping name, unlock
   method (TPM / keyfile / physical-presence), the verification script's full
   output and exit code, and the operator who performed the check.

### Verification script: `backend/scripts/verify_edge_luks_encryption.sh`

A dependency-free POSIX `sh` script (not Python) is used here deliberately: it
runs **on the edge appliance**, which is not guaranteed to have the backend
Python environment installed, rather than in CI or a backend container like
`backend/scripts/check_compose_settings.py`. It shells out only to tools any
LUKS-based appliance already has: `findmnt`/`df`, `lsblk`, `cryptsetup`, `dd` and
`grep`. It performs read-only checks and no destructive operations.

For a given mountpoint it checks, in order:

1. The device backing the mountpoint is an **active dm-crypt/LUKS mapped
   device** (via `lsblk` TYPE and `cryptsetup status`), not the raw underlying
   partition.
2. A raw read of the first 4KB of the **underlying, pre-decryption** block
   device (the one `cryptsetup status` reports as `device:`) does **not**
   contain the literal string `SQLite format 3` — the unencrypted SQLite file
   magic header. This is the concrete negative test from the original Phase 11
   plan's acceptance criteria for this finding.

It exits `0` with a `COMPLIANT:` message only if both checks pass, and non-zero
with a `NON-COMPLIANT:`/error message otherwise (see the script's `--help` output
for the full exit code table: usage error, not-dm-crypt-backed, plaintext header
exposed, or missing tooling, are all distinct codes).

**What was actually tested, and what wasn't (be aware of this limitation when
relying on this script):** the development/review environment for this script
has no root privileges and no loop/dm-crypt device support, so real
`cryptsetup luksFormat`/`luksOpen` and `lsblk`/`findmnt` against an actual LUKS
device could not be exercised here. Instead:

- The parsing/decision functions (`is_active_luks_status`,
  `underlying_device_from_status`) were unit-tested against canned
  `cryptsetup status` text for LUKS1, LUKS2, an inactive mapping, and an
  active-but-non-LUKS mapping.
- The plaintext-header check (`raw_device_exposes_sqlite_header`) was tested
  against real throwaway files using the script's actual `dd`/`grep` logic: one
  file written with a real `SQLite format 3\0` header (must be detected), one
  filled with random/non-SQLite bytes (must not be detected), and an empty file
  (must not be detected).
- Run `backend/scripts/verify_edge_luks_encryption.sh --self-test` to execute
  these checks; it prints each assertion's result and exits non-zero only if one
  fails.
- The live `cryptsetup`/`lsblk`/`findmnt` integration path (resolving a real
  mountpoint to a real mapped device) is **not exercised by `--self-test`** and
  has not been run against a real LUKS device as part of this change. Run it for
  real against a provisioned appliance (or a disposable `losetup` + `cryptsetup`
  loopback setup on a Linux host with root) as part of accepting that appliance,
  per the checklist above.
