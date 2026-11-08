# SNMPv3 interoperability harness

The unit tests in `edge_collector/tests` speak real UDP and BER to an agent written for the tests. That agent shares
assumptions with the code it tests. This harness removes that dependence: it runs the real Edge Collector SNMPv3 stack
against **pysnmp**, an independent implementation, over loopback.

```sh
pip install ./edge_collector -r edge_collector/interop/requirements.txt
PYTHONPATH=. python -m edge_collector.interop.run_interop --json interop-results.json
```

It needs no privileges, no hardware and no network beyond `127.0.0.1`. It starts the reference agent itself, picks free
ports, and stops the agent when it finishes. Exit status is 0 only if every scenario behaved as expected. CI runs it in the
`edge-interop` job; it is not part of `pytest edge_collector/tests`, so the normal Edge job stays free of pysnmp.

## What it runs

| Group | Scenarios |
| --- | --- |
| `supported` | For each of the 12 authentication/privacy combinations the product accepts: one authenticated, encrypted GET, then a 25-row GETBULK walk that spans several pages. Values and order are compared with the agent's table. |
| `refused` | md5, des, noAuthNoPriv, and the SHA/AES pairs whose hash is shorter than the AES key. Each must be refused by the collector before any datagram is sent. |
| `failure` | Unknown user, wrong authentication secret, wrong privacy secret, agent configured with a different authentication algorithm, agent configured with a different privacy algorithm. Each must end in `SNMPv3AuthenticationError` with the fixed message and the right coarse reason. |
| `protocol` | A missing OID is reported as unknown, not guessed. Time-window resynchronisation: the collector's clock is pushed an hour ahead, the agent answers with an authenticated `notInTimeWindow` report, and the collector resynchronises and succeeds. |
| `discovery` | The product's LLDP and CDP profile specs are walked with the real SNMPv3 session against LLDP-MIB, CISCO-CDP-MIB and IF-MIB rows served by pysnmp, and decoded by the real adapters. Run for sha256 + aes128 and sha512 + aes256. |
| `wire` | A recording UDP relay sits between collector and agent. No authentication or privacy secret, master key, localized key, or plaintext value appears in any datagram; every request after engine discovery carries the auth and priv flags. A positive control confirms the scan can see the cleartext USM user name. The text of credentials, sessions and errors contains no secret. |

## What the result does and does not prove

Proves, for the algorithms listed and pysnmp 7.1.30: the collector's key localization, message authentication (including
the RFC 7860 truncations), AES-CFB privacy with 128, 192 and 256 bit keys, engine discovery, time-window handling, GETBULK
walking, and the LLDP/CDP table decoding all agree with an independent implementation, and that the failure modes fail
closed.

Does not prove: behavior of any vendor's firmware, vendor MIB quirks, large production tables, IPv6 targets, lossy networks
or SNMPv2c. No physical device took part. See `docs/implementation/ISSUE_101_SUPPORTED_PROFILES.md` for how these results
map to the supported-profile matrix and which entries remain unvalidated on hardware.

## Files

- `agent.py` - the pysnmp reference agent (also runnable by hand: `python -m edge_collector.interop.agent --port 16100`).
- `scenarios.py` - the algorithm matrix, fixed throwaway credentials, the MIB the agent serves, and the discovery plan.
  The LLDP and CDP specs here are a copy of the product templates; `backend/tests/unit/test_interop_profile_contract.py`
  fails if they differ, or if the matrix stops matching the algorithms the product accepts.
- `run_interop.py` - the orchestrator, relay and scenarios.
- `requirements.txt` - pins pysnmp. A newer pysnmp may change its API; update the pin deliberately.

The credentials in `scenarios.py` protect nothing: the agent listens on loopback for the duration of the run.
