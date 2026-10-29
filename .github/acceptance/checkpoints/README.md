# Owner-reviewed history checkpoints

No active checkpoint is supplied. This is recovery configuration, not model output.

Before old review artifacts expire or a policy/adapter upgrade invalidates them,
the owner archives the prior records, preserves every unresolved blocking finding,
and commits `<PR>.json` on protected main through independent review. The latest
commit touching that file must be GitHub-verified (`true`, `valid`) and authored
by owner ID 285010264, with owner or GitHub web-flow committer. Agent publication
of this entire directory is prohibited.

Schema:

```json
{
  "version": 1,
  "pr": 211,
  "workflow_id": 123,
  "through_run_number": 42,
  "through_run_id": 456,
  "history_digest": "<SHA-256 of the archived canonical review history>",
  "outstanding_findings": [
    {"id": "F1", "blocking": true, "message": "Preserved unresolved finding"}
  ]
}
```

The numbers are placeholders. The gate authenticates the actual boundary run,
workflow ID, exact PR display title, owner actors, trusted source and run number;
it must be completed, but can be a failed preparation attempt caused by expired
history. A future cutoff cannot silently swallow later reviews. New reviews must
explicitly resolve the preserved finding IDs.

When history has already expired, an owner-requested review attempt gives a recent
authenticated failed preparation boundary. The owner then reconstructs the open
findings from the archived record, commits the reviewed checkpoint and requests
a new review. A checkpoint is never automatically generated, advanced or cleared.
Its boundary run must remain API-verifiable; refresh it before run retention
expires, or use a new authenticated failed preparation boundary. If the archive
cannot establish all open findings, escalate; do not manufacture an empty list.

This trusted-main configuration is part of the security trust root. An owner
signature alone is not permission for an autonomous agent to write it. Initial
bootstrap and changes require the separate owner authorization and independent
acceptance required for all security controls.
