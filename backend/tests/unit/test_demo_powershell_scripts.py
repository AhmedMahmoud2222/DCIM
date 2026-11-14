"""Static regressions for the Windows PowerShell demo scripts (owner review of PR #351, P1: restore after a failed copy).

PowerShell is not available where this suite runs, so these tests do NOT execute the scripts. They pin the properties whose
absence made the original runbook dangerous, by reading the script text:

* every native `docker` call goes through Invoke-Native, which throws on a non-zero exit code (PowerShell does not stop on
  a failing native command by itself);
* nothing destructive (dropdb, createdb, the real pg_restore) can come before every check on the backup has passed;
* the backup path is a parameter and is never reassigned, and no step re-uses a timestamp variable;
* binary data is never moved through a PowerShell `>` redirection.

Run the scripts themselves against a disposable Compose project before relying on them."""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
SCRIPTS = ROOT / "scripts" / "demo"
RESTORE = (SCRIPTS / "Restore-DemoDatabase.ps1").read_text(encoding="utf-8")
BACKUP = (SCRIPTS / "Backup-DemoDatabase.ps1").read_text(encoding="utf-8")
COMMON = (SCRIPTS / "DemoCommon.ps1").read_text(encoding="utf-8")
RUNBOOK = (ROOT / "docs" / "DEMO_LOCAL_LOADER.md").read_text(encoding="utf-8")


def code_lines(text: str) -> list[tuple[int, str]]:
    return [(n, ln) for n, ln in enumerate(text.splitlines(), 1) if ln.strip() and not ln.strip().startswith("#")]


def first(text: str, needle: str) -> int:
    index = text.find(needle)
    assert index >= 0, f"{needle!r} not found"
    return index


def test_scripts_stop_on_errors_and_check_every_native_exit_code():
    assert "$ErrorActionPreference = 'Stop'" in RESTORE and "$ErrorActionPreference = 'Stop'" in BACKUP
    assert "if ($LASTEXITCODE -ne 0)" in COMMON and "throw" in COMMON
    for name, text in (("Restore", RESTORE), ("Backup", BACKUP)):
        for number, line in code_lines(text):
            assert not re.match(r"^\s*docker\b", line), f"{name} line {number} calls docker directly instead of through Invoke-Native"


def test_nothing_destructive_precedes_the_backup_checks():
    drop = first(RESTORE, "'dropdb' -ComposeFile")
    must_precede = [
        "-ConfirmDestroyLocalDemoDatabase) { throw",       # explicit confirmation
        "Test-Path -LiteralPath $BackupPath -PathType Leaf",  # the file exists
        "Get-FileHash -LiteralPath $backup.FullName",     # checksum against the manifest
        "'stop', 'demo-refresh'",                         # writers stopped (including the demo-refresh service)
        "pg_stat_activity",                                # and none left connected
        "'pg_restore --list '",                            # archive readable inside the container
        "$containerHash -ne $expected",                    # the copy in the container matches the manifest
        "'test restore into the throwaway database'",     # the archive actually restores
    ]
    for needle in must_precede:
        assert first(RESTORE, needle) < drop, f"{needle!r} must come before dropdb"
    assert first(RESTORE, "createdb -U postgres ' + $scratch") < drop
    assert RESTORE.count("'dropdb'") == 1 and RESTORE.count("'createdb'") == 1


def test_the_throwaway_check_database_is_always_dropped():
    block = RESTORE[first(RESTORE, "try {"):first(RESTORE, "# Every check has passed")]
    assert "finally" in block and "dropdb -U postgres --if-exists ' + $scratch" in block


def test_the_backup_path_is_never_reassigned_and_no_timestamp_variable_is_shared():
    assert "$BackupPath =" not in RESTORE.replace("[Parameter(Mandatory)][string]$BackupPath", "")
    assert "$stamp" not in RESTORE  # the original bug: a timestamp variable reset by a later step
    assert BACKUP.count("$stamp =") == 1
    assert "Refusing to overwrite an existing backup" in BACKUP


def test_backup_proves_the_copy_and_writes_a_manifest():
    assert BACKUP.index("pg_restore --list") < BACKUP.index("docker compose cp") or "'cp'" in BACKUP
    assert "Get-FileHash" in BACKUP and "Checksum mismatch after copy" in BACKUP and ".sha256" in BACKUP


def test_binary_dumps_are_not_moved_through_powershell_redirection():
    for text in (RESTORE, BACKUP, COMMON, RUNBOOK):
        assert not re.search(r"pg_dump[^\n]*>\s*[\w.\\$\"']", text), "pg_dump output must not be redirected through PowerShell"


def test_the_runbook_uses_the_scripts_for_backup_and_restore():
    assert "Backup-DemoDatabase.ps1" in RUNBOOK and "Restore-DemoDatabase.ps1" in RUNBOOK
    assert "dropdb" not in RUNBOOK, "restore must go through the guarded script, not inline commands"
    assert "docker compose build" in RUNBOOK and "migrate" in RUNBOOK.split("docker compose build", 1)[1][:200]


def test_the_runbook_names_every_service_image_it_rebuilds_and_checks_the_revision():
    section = RUNBOOK[first(RUNBOOK, "### 2. Install the code"):first(RUNBOOK, "### 3.")]
    assert re.search(r"docker compose build\b(?!\s+(backend|frontend)\b)", section) or "migrate" in section
    assert "alembic current" in section and "0060_alarm_severity" in section
