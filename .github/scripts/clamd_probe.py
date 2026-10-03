"""Runs INSIDE the backend container (`docker compose exec -T backend python - < this file`).

Exercises the production scanner path: Settings -> ClamdScanner -> scan_with_policy(mode="required")
against the endpoint the application is configured with. Exit codes are the contract with
compose_smoke.py; keep them in sync with SCANNER_EXIT_MESSAGES there."""

import os
import socket
import sys

EXIT_OK = 0
EXIT_CONFIG = 10
EXIT_DNS = 11
EXIT_UNAVAILABLE = 12
EXIT_CLEAN_REJECTED = 13
EXIT_EICAR_MISSED = 14

# Split so this file is not itself flagged by antivirus scanners.
EICAR = ("X5O!P%@AP[4\\PZX54(P^)7CC)7}$" + "EICAR-STANDARD-ANTIVIRUS-TEST-FILE!$H+H*").encode("ascii")
CLEAN_PDF = (
    b"%PDF-1.4\n1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj\n"
    b"2 0 obj<</Type/Pages/Kids[3 0 R]/Count 1>>endobj\n"
    b"3 0 obj<</Type/Page/Parent 2 0 R/MediaBox[0 0 200 200]>>endobj\n"
    b"trailer<</Root 1 0 R/Size 4>>\n%%EOF\n"
)


def main() -> int:
    from app.application.catalog_documents.malware_scan import (
        ClamdScanner,
        MalwareDetected,
        ScannerUnavailable,
        scan_with_policy,
    )
    from app.core.config import get_settings

    settings = get_settings()
    expected_host = os.environ.get("EXPECT_CLAMD_HOST", "clamav")
    if settings.catalog_pdf_scan_mode != "required" or settings.clamd_host != expected_host or settings.clamd_port != int(os.environ.get("EXPECT_CLAMD_PORT", "3310")):
        print(f"config: mode={settings.catalog_pdf_scan_mode} host={settings.clamd_host} port={settings.clamd_port}")
        return EXIT_CONFIG

    expected_ips = {ip for ip in os.environ.get("EXPECT_CLAMD_IPS", "").split(",") if ip}
    if expected_ips:
        try:
            resolved = {info[4][0] for info in socket.getaddrinfo(settings.clamd_host, settings.clamd_port, socket.AF_INET)}
        except OSError as exc:
            print(f"dns: {settings.clamd_host} did not resolve: {type(exc).__name__}")
            return EXIT_DNS
        if not resolved & expected_ips:
            print(f"dns: {settings.clamd_host} resolved to {sorted(resolved)}, Compose clamav container is {sorted(expected_ips)}")
            return EXIT_DNS

    scanner = ClamdScanner(settings.clamd_host, settings.clamd_port, settings.clamd_timeout_seconds)
    try:
        outcome = scan_with_policy(CLEAN_PDF, mode="required", scanner=scanner)
    except ScannerUnavailable:
        print("scanner: clamd unreachable or returned an error for the clean payload")
        return EXIT_UNAVAILABLE
    except MalwareDetected as exc:
        print(f"scanner: clean payload flagged as {exc.signature}")
        return EXIT_CLEAN_REJECTED
    if outcome.status != "clean" or outcome.engine != "clamd":
        print(f"scanner: clean payload outcome status={outcome.status} engine={outcome.engine}")
        return EXIT_CLEAN_REJECTED

    try:
        scan_with_policy(EICAR, mode="required", scanner=scanner)
    except MalwareDetected as exc:
        print(f"OK clean accepted; EICAR detected as {exc.signature}")
        return EXIT_OK
    except ScannerUnavailable:
        print("scanner: clamd became unreachable while scanning EICAR")
        return EXIT_UNAVAILABLE
    print("scanner: EICAR was accepted as clean")
    return EXIT_EICAR_MISSED


if __name__ == "__main__":
    sys.exit(main())
