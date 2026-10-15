---
title: Published organization pause and concurrency admission
status: shipped
sources:
  - src/treg/infra/org_flow_control.py
  - src/treg/infra/structured_logging.py
  - src/treg/infra/kv.py
  - src/treg/config.py
  - src/treg/routers/call.py
  - tests/test_org_flow_control.py
  - tests/test_org_flow_control_reader.py
  - tests/test_org_flow_control_redis.py
  - tests/fixtures/org-flow-control/generate.ts
related:
  - architecture/org-concurrency.md
  - architecture/money.md
  - architecture/proxy-model.md
  - ops/deploy.md
---

# Published organization pause and concurrency admission

`org_flow_control.check` reads operator-published Redis snapshots at
`routers.call.run_call_surface`, after trusted Caller/context resolution and before the optional
Org concurrency gate, money admission, body consumption or upstream work. It applies to the same
root surfaces as [Org concurrency](org-concurrency.md): `/call/`, `/catalog/call/`, `/table/`
and MCP adapters using them. Direct hub runs, provider-resource requests and application/background
calls remain outside this gate. Authentication queries happen before it.

One reader check returns the effective snapshot for both **pause and concurrency**. An active
pause rejects immediately; otherwise a non-null `concurrency_limit.max_in_flight` supplies the
shared slot guard's limit. Null means no concurrency limit and has no static fallback. Rate
fields remain validated for publication compatibility but do not change admission. A snapshot
observation is not confirmation that every field is enforced or every worker has applied it.

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
restore content; clearing a pause means publishing `pause_until: null` in a new full version.
Clearing concurrency means `concurrency_limit: null`; pausing expires at its absolute UTC time
independently of the concurrency field. A configuration save alone does not publish or activate
that version. Review existing non-null concurrency values before deploying workers that enforce
them with this switch enabled. See [concurrency migration](org-concurrency.md#configuration-migration-and-rollback)
for the removed environment settings and authorized rollout sequence.

The synthetic fixtures in `tests/fixtures/org-flow-control/` capture the reference publisher's
actual `publish` serialization, covering disabled controls, fractional-second UTC timestamps,
populated limits and a resume. The consumer's strict validation is unchanged.

## Admission and failures

A future `pause_until` returns HTTP 429, `Retry-After` rounded up from the remaining seconds,
and `detail.error=org_traffic_paused` with `config_version`. UTC expiry is checked on every
request, independently of refresh timing. Normal call-ID, error-header and refusal-audit handling
is retained; MCP call/media responses retain the retry guidance.

Only a valid, unexpired pause or contention under a valid published concurrency limit rejects
new roots. Concurrency contention returns HTTP 429 `org_concurrency_limit`, `Retry-After: 1`
and `config_version`; it never waits in a queue. Refresh failures follow these policies:

| Refresh result | Admission behavior |
| --- | --- |
| Redis connectivity, initialization or timeout failure | Allow, bypassing cached pause and concurrency until a subsequent refresh. Retain the snapshot for recovery and version comparison. |
| Healthy missing key | Clear the local snapshot and treat the Org as unconfigured version zero. |
| Invalid JSON, wrong type, oversized or undecodable data | Retain the previous valid snapshot, including its concurrency limit and original pause expiry; allow if none exists. |
| Lower version or changed content at the same version | Retain the previous valid snapshot, including its concurrency limit and original pause expiry. |

`kv.FlowControlReadFailure` distinguishes invalid data from transport unavailability; `None`
means missing and a string is a bounded payload. A cancelled read propagates cancellation without
changing shared snapshot, availability or refresh deadline. The next caller or waiting request
can refresh immediately; cancellation never becomes an HTTP 503.

Refreshes and Redis I/O have a 200 ms deadline. Per-Org single-flight and a 32-read semaphore
bound refresh work; failures are cached until the next interval. The worker has at most 4096
LRU entries, protecting active pauses, configured concurrency limits and entries with active
callers/waiters. If no entry can be evicted, the uncached Org is allowed without adding an entry
or reading Redis, with a bounded
capacity warning. No DB connection is held while Redis is read. Slot initialization, acquisition
and timeout failures also allow new work without a confirmed concurrency slot; only an explicit
busy result rejects. No Redis failure is converted into an HTTP 503 by these guards.

Already accepted work is never checked again: streams, settlement, release and cancellation
cleanup finish through their existing paths. Disabling the flag uses the normal deployment
process and does not cancel accepted work or erase Redis data.

## Evidence and recovery boundaries

Single-line `org_flow_control_version_observed`, `org_flow_control_paused` and
`org_flow_control_config_unavailable` records carry worker PID, trusted Org, call reference,
version, the compatibility field `capability=pause`, and `capabilities=["pause","concurrency"]`.
Version observations include `max_in_flight`, or null when disabled. These capabilities describe
the reader's supported admission checks, not successful slot acquisition or acknowledgment by
all workers. Records omit raw configuration, credentials and exception text.
They use the scoped plain handler described in [Org traffic logs](org-traffic.md), inherit
existing logger levels and preserve explicitly configured named handlers. Root logging is not
changed; root handler routing and filters do not apply to the default event sink.
Pause events and refresh warnings are each limited to one per Org every five seconds; the next
event reports `suppressed_since_last_log` for that event category, possibly spanning versions or
failure reasons. Counts remaining when an entry is evicted or a worker exits are lost. Capacity
warnings are limited to one per worker every five seconds. Ordinary request logs still describe
each request's outcome; sampled pause events are not a complete refusal count.
Observation is per worker and on demand, not a heartbeat or a complete worker-acknowledgement set.

Version protection is only as durable as the resident cache. Restart, eviction and a healthy
missing key forget history. A cold missing key cannot distinguish an unconfigured Org from lost
Redis data. Restore the
published keys before enabling/re-enabling workers; the reader cannot detect an older valid
Redis backup after losing its own watermark.
