---
title: Published organization concurrent-request admission
status: shipped
sources:
  - src/treg/infra/org_concurrency.py
  - src/treg/infra/kv.py
  - src/treg/config.py
  - src/treg/routers/call.py
  - src/treg/mcp.py
  - tests/test_org_concurrency.py
  - tests/test_org_concurrency_redis.py
related:
  - architecture/proxy-model.md
  - architecture/mcp-oauth.md
  - architecture/money.md
  - ops/deploy.md
---

# Organization concurrent-request admission

`routers.call.run_call_surface` uses the published `concurrency_limit.max_in_flight` to admit one
root request after trusted `Caller` resolution and context creation, before `execute_call` can
reserve, enter money admission or call a provider. `/call/`, `/catalog/call/`, `/table/` and the
MCP adapters using these routes share the gate. Routing, verification, overflow and hub children
call the application directly, so they do
not consume additional root slots. Authentication DB work still precedes this gate; it cannot
protect the identity lookup from saturation. This is a root-request bound, not a bound on provider
fanout or database connections. It applies to configured orgs' own-key calls as well as shared-key
calls without changing their billing behavior.

**Excluded entry points:** direct `POST /hub/run` and `/orgs/{org_id}/provider-resources` requests
do not enter `run_call_surface`; both can perform provider work outside this guard. The MCP
`resources_list` tool uses the provider-resources route and is also excluded. Arena/background
work invoking `execute_call` directly is outside the guarded roots. This setting therefore does
not cap every Org Web request or all provider work. A hub invocation through `/call/` is covered
by its one root slot, while a direct `/hub/run` invocation is not. Expanding these boundaries is
a separate change.

`org_concurrency.acquire` returns immediately on contention with HTTP 429,
`Retry-After: 1`, and `detail.error=org_concurrency_limit`. The refusal includes the effective
`config_version`. Existing HTTP exception handling still adds `X-Treg-Error`, `X-Treg-Call-Id`
and an audit refusal. `mcp._call_impl` and
`mcp._call_media_impl` preserve the header as `retry_after` in their structured result; the
refusal body also includes integer `retry_after_s`. Retry-After is guidance, not a promise of an
available slot. There is no waiting queue. A configured [Org rate bucket](org-flow-control.md)
is checked atomically with this slot limit; the refusal consumes neither a token nor a slot.

Contention emits `org_concurrency rejected org_id=<trusted Caller.org_id> limit=<effective limit>
status=429 config_version=<effective version>`. Logging failure does not change the refusal. Process-local
[Org traffic logs](org-traffic.md) include this request in their arrival/completion counters.

The default-disabled [published flow-control reader](org-flow-control.md) returns one effective
snapshot for pause, rate and concurrency admission. An active pause rejects before the
rate/concurrency gate. A null concurrency field means no concurrency limit; there is no environment-configured
fallback. A non-null `rate_limit` is enforced even when concurrency is null; rate exhaustion
returns HTTP 429 `org_rate_limit` with token-refill retry guidance.

## Configuration, migration and rollback

The shared flow-control switch is disabled by default. Use the same Redis database as the
publisher on every Web worker/instance serving traffic.

```sh
TREG_ORG_FLOW_CONTROL_ENABLED=false
TREG_ORG_FLOW_CONTROL_REFRESH_S=2
# Set TREG_KV_URL privately before enabling.
```

In the full published configuration, use `"concurrency_limit":{"max_in_flight":8}` to set a
synthetic limit, or `"concurrency_limit":null` to remove it. Limits must be positive integers,
not strings, booleans or fractions. The lease is an internal 30-second constant; contention retry
guidance is an internal one-second constant. Redis read, initialization, command and timeout
failures allow new work without a confirmed slot. There is no process-local concurrency fallback.
Malformed or conflicting publications retain the previous valid snapshot; a healthy missing key
clears the effective local rule. See the reader fragment for bounded refresh and cache behavior.

The legacy `TREG_ORG_CONCURRENCY_*` settings no longer configure concurrency: their enable flag,
limits, exemptions, failure policy, lease and retry values no longer control this gate. Only the
old retry name remains as a compatibility alias for money admission's retry floor; use
`TREG_MONEY_ADMISSION_RETRY_AFTER_S` for that separate setting. There is no automatic conversion
of old concurrency environment values. Before an authorized rollout, an operator must
review and separately approve, save and publish an equivalent full snapshot for each intended
Org, then enable the shared flow-control flag. Saving alone does not activate a snapshot.
Review already published non-null rate and concurrency values before deployment: workers with
flow control enabled enforce both limits in addition to pauses. A null concurrency field leaves
concurrency unlimited even when legacy environment values are still present.

The implementation change itself neither publishes snapshots nor changes running deployment
configuration; those rollout actions require separate authorization.

To remove one Org's concurrency limit, publish a higher full configuration version with
`concurrency_limit:null`. To disable all published pause, rate and concurrency admission, set
`TREG_ORG_FLOW_CONTROL_ENABLED=false` and apply the normal deployment process. Already admitted
requests finish their renewal/cleanup lifecycle. No key deletion or DB migration is needed.
Lazy per-worker refresh and rolling enablement or disabling are not atomic across processes;
version observation does not acknowledge every worker. Lowering a limit stops new admissions
until occupancy falls below it; it does not evict accepted work.

## Ownership and lifetime

`kv.RedisStore.acquire_slot` uses one atomic Lua operation when no rate is configured; the combined
rate/slot operation is used otherwise. Both use the same sorted set per Org, shared across API
keys and workers. Each request owns a random token with a Redis-time expiry; the script
removes expired members, checks cardinality and inserts a new owner only below the published
limit. The existing `treg:org-concurrency:v1:<org_id>` namespace is reused across configuration
versions; a rule update never resets or replaces occupied slots. The key expires at its latest
owner's deadline, so a shorter new lease cannot expire another owner's slot. This namespace is
separate from money's admission leases.
When rate is configured, the script checks both limits before inserting the owner and debiting
one rate token. Publishing a new rate or concurrency version never replaces live owners.

`Slot._renew` runs every third of a lease. `renew_slot` extends only a still-live token and never
recreates one after expiry; `release_slot` removes only its own token. Ambiguous acquisition and
cancellation join the bounded Redis attempt and attempt owner-safe cleanup. A lost release reply
may leave occupancy until its lease expires, temporarily denying otherwise admissible requests.
Slot-only acquisition, renewal and release have one-second outer deadlines; combined rate/slot
admission has a 200 ms deadline. Both paths reuse the existing `kv.store()` client.
Rate tokens consumed by a successful admission are not refunded during this cleanup, including
failure, cancellation or an ambiguous acquisition reply.

The slot remains held through application execution, `/table/` conversion, stream transmission,
upstream close and synchronous financial cleanup. `_LeasedStreamingResponse` also releases on
send errors and disconnects before body iteration. Cleanup is joined through repeated task
cancellation and Starlette cancellation scopes. Ordinary completion, exceptions and cancellation
all release the same owner. An async provider submission releases its Web slot once that response
and cleanup end; the provider's remote job does not retain a Web slot between polls.

## Lease failure limits

Expiration is recovery for an unresponsive owner, **not proof the request finished**. A process
pause, Redis outage, eviction, restart, failover losing acknowledged writes, or network partition
can let a successor acquire while old work continues. There is no provider/database fencing here,
so a strict cap cannot be guaranteed under these conditions. Renewal uncertainty logs
`org_concurrency lease_uncertain` and explicitly reports that the cap is not guaranteed.
Acquire/release failures log `acquire_unavailable` /
`release_unavailable` without request contents, credentials or ownership tokens.

Accepted work and financial cleanup continue after lease loss; the guard neither cancels them
nor alters money locks, reserve or settlement.
Retry or emergency hard-fencing policies require separate design and approval.

`test_org_concurrency` covers policy, configuration, HTTP/MCP, streaming and cleanup.
`test_org_concurrency_redis` uses disposable loopback Redis via `TREG_TEST_KV_URL`, including
independent clients/processes, contention, expiry and replacement-owner safety. The Redis-backed
CI job includes both files. No production traffic or credentials are needed.
