---
title: Published organization pause admission
status: shipped
sources:
  - src/treg/infra/org_flow_control.py
  - src/treg/infra/kv.py
  - src/treg/config.py
  - src/treg/routers/call.py
  - tests/test_org_flow_control.py
  - tests/test_org_flow_control_reader.py
  - tests/test_org_flow_control_redis.py
related:
  - architecture/org-concurrency.md
  - architecture/money.md
  - architecture/proxy-model.md
  - ops/deploy.md
---

# Published organization pause admission

`org_flow_control.check` reads operator-published Redis snapshots at
`routers.call.run_call_surface`, after trusted Caller/context resolution and before the optional
Org concurrency gate, money admission, body consumption or upstream work. It applies to the same
root surfaces as [Org concurrency](org-concurrency.md): `/call/`, `/catalog/call/`, `/table/`
and MCP adapters using them. Direct hub runs, provider-resource requests and application/background
calls remain outside this gate. Authentication queries happen before it.

This version enforces **pause only**. Rate and concurrency fields are validated for publication
compatibility but do not change admission here. Existing environment-configured Org concurrency
keeps its separate behavior. A snapshot observation is not confirmation that every control in
that version is enforced.

## Configuration and publication contract

Disabled by default; enabling requires an explicit valid Redis `TREG_KV_URL` pointing at the same
database as the publisher. There is no local-store substitute and no PostgreSQL read on this path.

```sh
TREG_ORG_FLOW_CONTROL_ENABLED=false
TREG_ORG_FLOW_CONTROL_REFRESH_S=2
# Set TREG_KV_URL privately before enabling.
```

The refresh interval is 0.2-10 seconds. Reads are lazy per active Org/worker; a quiet worker does
not poll or acknowledge a version. A request after the interval performs one bounded refresh;
there is no background monitor or publisher.

The key is `treg:traffic-control:org:<org_id>`, containing the complete JSON envelope:

```json
{
  "org_id": 42,
  "version": 1,
  "config": {
    "schema_version": 1,
    "pause_until": null,
    "rate_limit": null,
    "concurrency_limit": null
  }
}
```

All fields are required. Unknown fields, duplicate keys, wrong Org IDs, unsupported versions,
coerced/bool numbers, invalid limits and non-UTC pause timestamps are rejected. The reader does
not write, expire or delete published keys. A publisher must use a higher version to change or
restore content; clearing a pause means publishing `pause_until: null` in a new version.

## Admission and failures

A future `pause_until` returns HTTP 429, `Retry-After` rounded up from the remaining seconds,
and `detail.error=org_traffic_paused` with `config_version`. UTC expiry is checked on every
request, independently of refresh timing. Normal call-ID, error-header and refusal-audit handling
is retained; MCP call/media responses retain the retry guidance.

A failed/invalid refresh returns 503 with `org_traffic_control_unavailable` for new roots.
The last valid snapshot/watermark is retained for comparison, but is not treated as available
after that failure. Recovery requires a healthy acceptable refresh. A fresh cache entry with a
healthy absent key means unconfigured version zero; disappearance of an already observed
snapshot, lower versions and same-version changed content are unavailable.

Refreshes and Redis I/O have a 200 ms deadline. Per-Org single-flight and a 32-read semaphore
bound refresh work; failures are cached until the next interval. The worker has at most 4096
LRU entries, protecting active pauses and entries with active callers/waiters. If no entry can
be evicted, new untracked admission returns 503. No DB connection is held while Redis is read.

Already accepted work is never checked again: streams, settlement, release and cancellation
cleanup finish through their existing paths. Disabling the flag uses the normal deployment
process and does not cancel accepted work or erase Redis data.

## Evidence and recovery boundaries

Single-line `org_flow_control_version_observed`, `org_flow_control_paused` and
`org_flow_control_config_unavailable` records carry worker PID, trusted Org, call reference,
version and `capability=pause`. They omit raw configuration, credentials and exception text.
Observation is per worker and on demand, not a heartbeat or a complete worker-acknowledgement set.

Version protection is only as durable as the resident cache. Restart/eviction forgets history,
and a cold missing key cannot distinguish an unconfigured Org from lost Redis data. Restore the
published keys before enabling/re-enabling workers; the reader cannot detect an older valid
Redis backup after losing its own watermark.
