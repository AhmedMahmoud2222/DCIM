#!/usr/bin/env sh
# Verify that an Edge Collector appliance's durable queue (edge_collector/queue.py's
# SQLiteQueue, config.py's CollectorConfig.database_path) is stored on a dm-crypt/LUKS
# mapped block device, per docs/SEC06_EDGE_STORAGE_ENCRYPTION.md (Issue #38, SEC-06).
#
# Read-only. Performs no writes, no key material handling and no destructive operations.
#
# This is dependency-free POSIX sh rather than Python: it runs ON the edge appliance,
# which has no guaranteed backend Python environment, not in CI or the backend container.
# It only shells out to tools already required on any appliance that uses LUKS:
# findmnt/df, lsblk, cryptsetup and dd.
#
# Usage:
#   verify_edge_luks_encryption.sh <mountpoint>
#   verify_edge_luks_encryption.sh --self-test
#
# Exit codes:
#   0  compliant: mountpoint is backed by an active dm-crypt/LUKS device, and the
#      underlying raw (pre-decryption) device does not expose the plaintext SQLite header
#   1  usage error (bad or missing argument)
#   2  mountpoint is not backed by an active dm-crypt/LUKS mapped device
#   3  the underlying raw block device exposes the unencrypted SQLite magic header
#   4  required tooling is missing from PATH
#   5  self-test failure (only reachable via --self-test)

set -eu

SCRIPT_NAME=$(basename "$0")
SQLITE_MAGIC='SQLite format 3'

usage() {
    cat <<EOF
Usage: $SCRIPT_NAME <mountpoint>
       $SCRIPT_NAME --self-test

Checks, read-only, that <mountpoint> (e.g. the directory holding queue.db) is backed
by an active dm-crypt/LUKS mapped device, and that the underlying raw block device's
first 4KB do not contain the unencrypted SQLite magic header "SQLite format 3".

See docs/SEC06_EDGE_STORAGE_ENCRYPTION.md for the full runbook and compliance checklist.
EOF
}

fail() {
    # fail <exit-code> <message...>
    code=$1
    shift
    printf 'NON-COMPLIANT: %s\n' "$*" >&2
    exit "$code"
}

require_tool() {
    command -v "$1" >/dev/null 2>&1 || fail 4 "required tool '$1' is not on PATH"
}

# --- dm-crypt / LUKS detection -----------------------------------------------------

# resolve_source_device <mountpoint>
# Prints the block device backing <mountpoint>, using findmnt if available and
# falling back to `df`.
resolve_source_device() {
    mountpoint=$1
    if command -v findmnt >/dev/null 2>&1; then
        findmnt -n -o SOURCE --target "$mountpoint" 2>/dev/null && return 0
    fi
    df --output=source "$mountpoint" 2>/dev/null | tail -n 1
}

# parse_lsblk_type <device>
# Prints the lsblk TYPE column (e.g. "crypt", "part", "disk") for a device path.
lsblk_device_type() {
    lsblk -no TYPE "$1" 2>/dev/null | head -n 1
}

# is_active_luks_status <cryptsetup-status-output>
# Pure string check over the text `cryptsetup status <name>` would print. Factored
# out as a function over text (not a live command) so it can be unit-tested in
# --self-test without a real LUKS device.
is_active_luks_status() {
    status_text=$1
    case "$status_text" in
        *"is active"*) : ;;
        *) return 1 ;;
    esac
    case "$status_text" in
        *"type:"*"LUKS"*) return 0 ;;
        *) return 1 ;;
    esac
}

# underlying_device_from_status <cryptsetup-status-output>
# Extracts the value of the "device:" line cryptsetup status prints, e.g.
#   device:  /dev/sda2
# Pure text parsing, unit-tested in --self-test.
underlying_device_from_status() {
    printf '%s\n' "$1" | awk '/^[[:space:]]*device:/ {print $2; exit}'
}

# check_dmcrypt_backed <mountpoint>
# Exits non-zero (via fail, code 2) unless <mountpoint> resolves to an active
# dm-crypt/LUKS mapped device. On success prints the underlying raw device path.
check_dmcrypt_backed() {
    mountpoint=$1
    device=$(resolve_source_device "$mountpoint") || true
    [ -n "$device" ] || fail 2 "could not resolve a block device backing '$mountpoint'"

    type=$(lsblk_device_type "$device") || true
    case "$device" in
        /dev/mapper/*) ;;
        *)
            [ "$type" = "crypt" ] || fail 2 "'$mountpoint' is backed by '$device' (lsblk TYPE='$type'), not a dm-crypt mapped device"
            ;;
    esac

    status_text=$(cryptsetup status "$device" 2>/dev/null) || fail 2 "'cryptsetup status $device' failed; device is not an active LUKS mapping"
    is_active_luks_status "$status_text" || fail 2 "'$device' is not reported as an active LUKS mapping by cryptsetup"

    underlying=$(underlying_device_from_status "$status_text")
    [ -n "$underlying" ] || fail 2 "could not determine the underlying raw device for '$device' from cryptsetup status"
    printf '%s\n' "$underlying"
}

# --- plaintext SQLite header detection ----------------------------------------------

# raw_device_exposes_sqlite_header <path>
# Reads the first 4KB of <path> (a raw block device, or in --self-test a regular
# file standing in for one) and returns success (0) if the unencrypted SQLite magic
# header is present in plaintext -- i.e. the negative compliance condition.
raw_device_exposes_sqlite_header() {
    path=$1
    dd if="$path" bs=4096 count=1 2>/dev/null | grep -aq "$SQLITE_MAGIC"
}

check_no_plaintext_header() {
    raw_device=$1
    if raw_device_exposes_sqlite_header "$raw_device"; then
        fail 3 "raw device '$raw_device' exposes the unencrypted SQLite header '$SQLITE_MAGIC' in its first 4KB"
    fi
}

# --- self-test: exercises parsing/decision logic without a real LUKS device --------
# losetup/cryptsetup require root and a real block device, unavailable in this
# sandbox. These tests instead unit-test the pure parsing/decision functions above
# with canned input, and exercise the real header check against throwaway files
# using the actual `dd`/`grep` this script runs in production.

self_test_failures=0

st_assert() {
    description=$1
    expected=$2
    actual=$3
    if [ "$expected" = "$actual" ]; then
        printf 'ok   - %s\n' "$description"
    else
        printf 'FAIL - %s (expected [%s], got [%s])\n' "$description" "$expected" "$actual"
        self_test_failures=$((self_test_failures + 1))
    fi
}

self_test() {
    echo "Running self-test (no real block devices or root privileges required)..."

    luks2_active="/dev/mapper/edge-queue is active and is in use.
  type:    LUKS2
  cipher:  aes-xts-plain64
  keysize: 512 bits
  key location: keyring
  device:  /dev/sda2
  sector size:  512
  offset:  32768 sectors
  size:    2097152 sectors
  mode:    read/write"
    if is_active_luks_status "$luks2_active"; then r=active; else r=inactive; fi
    st_assert "active LUKS2 status text is recognized as active" active "$r"
    st_assert "device: line parsed from LUKS2 status" "/dev/sda2" "$(underlying_device_from_status "$luks2_active")"

    luks1_active="/dev/mapper/edge-queue is active.
  type:    LUKS1
  cipher:  aes-cbc-essiv:sha256
  keysize: 256 bits
  device:  /dev/sdb1
  offset:  4096 sectors
  size:    1048576 sectors
  mode:    read/write"
    if is_active_luks_status "$luks1_active"; then r=active; else r=inactive; fi
    st_assert "active LUKS1 status text is recognized as active" active "$r"
    st_assert "device: line parsed from LUKS1 status" "/dev/sdb1" "$(underlying_device_from_status "$luks1_active")"

    inactive_status="/dev/mapper/edge-queue is inactive."
    if is_active_luks_status "$inactive_status"; then r=active; else r=inactive; fi
    st_assert "inactive status text is rejected" inactive "$r"

    not_luks_status="/dev/mapper/plain-dm is active.
  type:    (unknown)
  device:  /dev/sdc1"
    if is_active_luks_status "$not_luks_status"; then r=active; else r=inactive; fi
    st_assert "active-but-non-LUKS status text is rejected" inactive "$r"

    work_dir=$(mktemp -d)
    trap 'rm -rf "$work_dir"' EXIT

    plaintext_file="$work_dir/plaintext-sqlite.db"
    printf 'SQLite format 3\000' > "$plaintext_file"
    dd if=/dev/zero bs=1 count=4080 >> "$plaintext_file" 2>/dev/null
    if raw_device_exposes_sqlite_header "$plaintext_file"; then r=exposed; else r=hidden; fi
    st_assert "plaintext SQLite header is detected on an unencrypted file" exposed "$r"

    encrypted_like_file="$work_dir/encrypted-like.bin"
    if [ -r /dev/urandom ]; then
        dd if=/dev/urandom of="$encrypted_like_file" bs=4096 count=1 2>/dev/null
    else
        i=0
        : > "$encrypted_like_file"
        while [ "$i" -lt 4096 ]; do
            printf '\101' >> "$encrypted_like_file"
            i=$((i + 1))
        done
    fi
    if raw_device_exposes_sqlite_header "$encrypted_like_file"; then r=exposed; else r=hidden; fi
    st_assert "no SQLite header is detected on non-SQLite (encrypted-like) data" hidden "$r"

    empty_file="$work_dir/empty.bin"
    : > "$empty_file"
    if raw_device_exposes_sqlite_header "$empty_file"; then r=exposed; else r=hidden; fi
    st_assert "no SQLite header is detected on an empty file" hidden "$r"

    rm -rf "$work_dir"
    trap - EXIT

    echo "---"
    if [ "$self_test_failures" -eq 0 ]; then
        echo "Self-test PASSED: parsing/decision logic and the real header check are correct."
        echo "NOTE: --self-test does not exercise live cryptsetup/lsblk/findmnt against a real"
        echo "LUKS device; this sandbox has no root privileges or loop/dm-crypt device support."
        echo "Run this script for real against a provisioned appliance to verify the live checks."
        return 0
    fi
    echo "Self-test FAILED: $self_test_failures assertion(s) did not hold."
    return 1
}

# --- main ----------------------------------------------------------------------------

main() {
    if [ "$#" -eq 1 ] && { [ "$1" = "--self-test" ] || [ "$1" = "-self-test" ]; }; then
        self_test || exit 5
        exit 0
    fi
    if [ "$#" -eq 1 ] && { [ "$1" = "-h" ] || [ "$1" = "--help" ]; }; then
        usage
        exit 0
    fi
    [ "$#" -eq 1 ] || { usage >&2; fail 1 "expected exactly one <mountpoint> argument"; }

    mountpoint=$1
    [ -d "$mountpoint" ] || fail 1 "'$mountpoint' is not a directory"

    for tool in lsblk cryptsetup dd grep; do
        require_tool "$tool"
    done

    underlying=$(check_dmcrypt_backed "$mountpoint")
    echo "OK: '$mountpoint' is backed by an active dm-crypt/LUKS device (raw device: $underlying)"

    check_no_plaintext_header "$underlying"
    echo "OK: raw device '$underlying' does not expose an unencrypted SQLite header"

    echo "COMPLIANT: '$mountpoint' satisfies SEC-06 edge storage encryption checks."
}

main "$@"
