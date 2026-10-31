# DCIM orchestrator operating policy
version: 1
parameters:
  ask_spacing_minutes_per_sha: 60
  claude_open_ask_slots: 3
  codex_open_ask_slots: 3
  lease_minutes: 25
  receipt_frees_slot: true
procedures:
  P1: A Codex receipt ends the ask and frees the slot; it counts toward 3 asks per SHA.
  P2: Each Needs Ahmed item carries the exact line to paste and the mechanical checks already done.
  P3: Before any owner item for a dependency bump, verify each new pin against the upstream release tag and report changed lines outside uses: pins.
  P4: A failed required job whose diff cannot touch the failing code is classified infrastructure after one same-head rerun that passes, and the finding stays a separate issue.
