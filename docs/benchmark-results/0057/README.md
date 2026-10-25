# Recorded benchmark runs for migration 0057

All runs are development-machine measurements of synthetic data. **Rollout classification: NOT YET VALIDATED.**

| Run | Dataset / ingest | Notes |
| --- | --- | --- |
| `fk_index_benchmark_1000000_20261025T043738Z` | 1M, `raw` / `raw`, one run | Historical. Superuser, unmarked cluster, sampled build times, no host metrics. |
| `fk_index_benchmark_10000000_20261025T050647Z` | 10M, `raw` / `raw`, one run | Historical. Same limitations. |
| `fk_index_benchmark_1000000_20261025T090918Z` | 1M, `fk` / `service` (application `ingest_reading()`, FKs enforced, application role), S2 and S1/S5 x 2 repeats | Hardened framework: marked disposable cluster, non-superuser `fkbench_admin`, exact per-index times, host observations, full-definition acceptance check. |

## Caveat on the 2026-10-25 `fk`/`service` run

The two repeats of S1 disagree sharply and the difference is **unexplained**. Repeat 1: ingest p99 139.5 ms baseline, 92.8 ms
during the upgrade. Repeat 2 (the one whose detail the report prints): p99 156.3 ms baseline, but p50 1,214 ms and p99 1,958 ms
during the upgrade, and still p50 723 ms / p99 1,929 ms in the 30 s after it finished. Because latency stayed high after the
migration ended, these numbers cannot be attributed to the migration; they may reflect I/O or memory contention on the shared
VM after the previous repeat's clone and drop. The provisional ratio threshold (3.0) flagged run 2. Do not read either repeat as
the migration's ingest impact; the run shows only that results on this machine vary by an order of magnitude between repeats and
that more repeats, a quieter machine and a longer settle are needed before any ingest-impact claim.

Invariants held in every run, including all 22 indexes matching their complete expected definition after upgrade, downgrade
atomicity and re-upgrade.
