"""Published pause parsing, bounded refresh/cache behavior and failure recovery; no services."""
import asyncio
import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from treg.infra import org_flow_control as flow


def payload(org_id=42, version=1, pause_until=None, **config):
    return json.dumps({"org_id": org_id, "version": version, "config": {
        "schema_version": 1, "pause_until": pause_until,
        "rate_limit": None, "concurrency_limit": None, **config}})


@pytest.fixture
def env(monkeypatch):
    clock = [100.0]
    now = [datetime(2030, 1, 1, tzinfo=timezone.utc)]
    store = SimpleNamespace(read_org_flow_control=AsyncMock(return_value=None))
    monkeypatch.setattr(flow, "_clock", lambda: clock[0])
    monkeypatch.setattr(flow, "_utcnow", lambda: now[0])
    monkeypatch.setattr(flow.kv, "store", lambda: store)
    return flow.Reader(max_entries=3), store, clock, now


@pytest.mark.parametrize("raw", [
    "{", " " * 32769,
    '{"org_id":42,"org_id":42,"version":1,"config":{}}',
    payload(org_id=43), payload(org_id=True), payload(version=True), payload(version=0),
    payload(version=2147483648), payload(schema_version=True), payload(extra=True),
    payload(pause_until="2030-01-01T01:00:00+01:00"),
    payload(pause_until="2030-02-30T00:00:00Z"),
    payload(pause_until="2030-01-01T00:00:00"),
    payload(pause_until="2030-01-01T00:00Z"),
    payload(pause_until="0000-02-30T00:00:00Z"),
    payload(rate_limit={"rps": True, "burst": 1}),
    payload(rate_limit={"rps": 0, "burst": 1}),
    payload(rate_limit={"rps": float("nan"), "burst": 1}),
    payload(rate_limit={"rps": float("inf"), "burst": 1}),
    payload(rate_limit={"rps": 9007199254740992, "burst": 1}),
    payload(rate_limit={"rps": 1, "burst": 1.0}),
    payload(rate_limit={"rps": 1, "burst": 1, "extra": 2}),
    payload(concurrency_limit={"max_in_flight": False}),
    payload(concurrency_limit={"max_in_flight": 0}),
    payload(concurrency_limit={"max_in_flight": 1, "extra": 2}),
    '{"org_id":42,"version":1,"config":{"schema_version":1,"pause_until":null}}',
    '{"org_id":42,"version":1,"config":null}',
])
def test_strict_full_published_envelope(raw):
    with pytest.raises((ValueError, TypeError)):
        flow._parse(raw, 42)


async def test_disabled_guard_never_reads_redis(env, monkeypatch):
    _, store, _, _ = env
    monkeypatch.setattr(flow, "get_settings", lambda: SimpleNamespace(
        org_flow_control_enabled=False))
    assert await flow.check(42, "call-42") is None
    store.read_org_flow_control.assert_not_awaited()


async def test_healthy_missing_is_unconfigured_and_non_pause_fields_are_inert(env):
    reader, store, clock, _ = env
    assert await reader.check(42, "first", 2) == 0
    store.read_org_flow_control.return_value = payload(
        rate_limit={"rps": 0.01, "burst": 1}, concurrency_limit={"max_in_flight": 1})
    clock[0] += 3
    # This PR validates but never executes either other kind of limit.
    assert await reader.check(42, "second", 2) == 1
    assert await reader.check(42, "third", 2) == 1
    assert store.read_org_flow_control.await_count == 2


async def test_pause_expires_by_current_utc_without_another_refresh(env):
    reader, store, clock, now = env
    store.read_org_flow_control.return_value = payload(
        pause_until=(now[0] + timedelta(seconds=1.2)).isoformat().replace("+00:00", "Z"))
    with pytest.raises(flow.PauseRejected) as rejected:
        await reader.check(42, "paused", 2)
    assert rejected.value.status_code == 429
    assert rejected.value.retry_after_s == 2
    assert rejected.value.detail["config_version"] == 1
    clock[0] += 1.5
    now[0] += timedelta(seconds=1.5)
    assert await reader.check(42, "expired", 2) == 1
    store.read_org_flow_control.assert_awaited_once_with(42)


async def test_new_version_can_resume_but_regression_or_mutation_is_unavailable(env):
    reader, store, clock, now = env
    store.read_org_flow_control.return_value = payload(
        pause_until=(now[0] + timedelta(hours=1)).isoformat().replace("+00:00", "Z"))
    with pytest.raises(flow.PauseRejected):
        await reader.check(42, None, 2)
    clock[0] += 3
    store.read_org_flow_control.return_value = payload(version=2)
    assert await reader.check(42, None, 2) == 2
    for conflicting in [payload(version=1),
                        payload(version=2, rate_limit={"rps": 1, "burst": 1})]:
        clock[0] += 3
        store.read_org_flow_control.return_value = conflicting
        with pytest.raises(flow.FlowControlUnavailable):
            await reader.check(42, None, 2)
        assert reader.entries[42].snapshot.version == 2
    clock[0] += 3
    store.read_org_flow_control.return_value = payload(version=2)
    assert await reader.check(42, None, 2) == 2


@pytest.mark.parametrize("bad", [None, False, "invalid", payload(org_id=7)])
async def test_known_snapshot_failure_refuses_new_work_and_recovers(env, bad):
    reader, store, clock, _ = env
    store.read_org_flow_control.return_value = payload()
    assert await reader.check(42, None, 2) == 1
    store.read_org_flow_control.return_value = bad
    # A fresh cached read is valid until its short refresh deadline.
    assert await reader.check(42, None, 2) == 1
    clock[0] += 3
    with pytest.raises(flow.FlowControlUnavailable) as error:
        await reader.check(42, "failed", 2)
    assert error.value.status_code == 503
    assert reader.entries[42].snapshot.version == 1
    with pytest.raises(flow.FlowControlUnavailable):
        await reader.check(42, "backoff", 2)
    assert store.read_org_flow_control.await_count == 2
    clock[0] += 3
    store.read_org_flow_control.return_value = payload()
    assert await reader.check(42, "recovered", 2) == 1


async def test_cold_redis_failure_and_initialization_error_are_sanitized(env, monkeypatch):
    reader, store, clock, _ = env
    store.read_org_flow_control.return_value = False
    with pytest.raises(flow.FlowControlUnavailable):
        await reader.check(42, None, 2)
    clock[0] += 3
    def broken_store():
        raise RuntimeError("private-redis-url")
    monkeypatch.setattr(flow.kv, "store", broken_store)
    with pytest.raises(flow.FlowControlUnavailable) as error:
        await reader.check(42, None, 2)
    assert "private-redis-url" not in str(error.value)


async def test_concurrent_cold_requests_share_one_read(env):
    reader, store, _, _ = env
    entered, release = asyncio.Event(), asyncio.Event()
    async def read(_org):
        entered.set()
        await release.wait()
        return payload()
    store.read_org_flow_control.side_effect = read
    tasks = [asyncio.create_task(reader.check(42, str(i), 2)) for i in range(20)]
    await entered.wait()
    assert reader.entries[42].users == 20
    release.set()
    assert await asyncio.gather(*tasks) == [1] * 20
    store.read_org_flow_control.assert_awaited_once_with(42)
    assert reader.entries[42].users == 0


async def test_cancelled_refresh_preserves_watermark_and_backoff(env):
    reader, store, clock, _ = env
    store.read_org_flow_control.return_value = payload(version=2)
    await reader.check(42, None, 2)
    clock[0] += 3
    entered = asyncio.Event()
    async def wait(_org):
        entered.set()
        await asyncio.Event().wait()
    store.read_org_flow_control.side_effect = wait
    task = asyncio.create_task(reader.check(42, "cancelled", 2))
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert reader.entries[42].users == 0
    assert reader.entries[42].snapshot.version == 2
    with pytest.raises(flow.FlowControlUnavailable):
        await reader.check(42, None, 2)
    assert store.read_org_flow_control.await_count == 2


async def test_cache_evicts_inactive_lru_but_protects_active_pauses(env):
    reader, store, _, now = env
    reader.max_entries = 2
    pause = (now[0] + timedelta(hours=1)).isoformat().replace("+00:00", "Z")
    store.read_org_flow_control.side_effect = lambda org: payload(
        org_id=org, pause_until=pause if org != 43 else None)
    with pytest.raises(flow.PauseRejected):
        await reader.check(42, None, 2)
    await reader.check(43, None, 2)
    with pytest.raises(flow.PauseRejected):
        await reader.check(44, None, 2)
    assert set(reader.entries) == {42, 44}
    with pytest.raises(flow.FlowControlUnavailable):
        await reader.check(45, None, 2)
    assert len(reader.entries) == 2
    now[0] += timedelta(hours=2)
    assert await reader.check(45, None, 2) == 1
    assert set(reader.entries) == {44, 45}


async def test_cache_cannot_evict_a_read_owner_or_waiter(env):
    reader, store, _, _ = env
    reader.max_entries = 1
    entered, release = asyncio.Event(), asyncio.Event()
    async def read(org):
        entered.set()
        await release.wait()
        return payload(org_id=org)
    store.read_org_flow_control.side_effect = read
    owner = asyncio.create_task(reader.check(42, None, 2))
    await entered.wait()
    waiter = asyncio.create_task(reader.check(42, None, 2))
    await asyncio.sleep(0)
    with pytest.raises(flow.FlowControlUnavailable):
        await reader.check(43, None, 2)
    assert reader.entries[42].users == 2
    release.set()
    assert await asyncio.gather(owner, waiter) == [1, 1]
    assert reader.entries[42].users == 0


@pytest.mark.parametrize("blocked_on_semaphore", [True, False])
async def test_read_and_semaphore_wait_have_a_total_deadline(env, blocked_on_semaphore):
    reader, store, _, _ = env
    if blocked_on_semaphore:
        reader.reads = asyncio.Semaphore(0)
    else:
        async def blocked(_org):
            await asyncio.Event().wait()
        store.read_org_flow_control.side_effect = blocked
    with pytest.raises(flow.FlowControlUnavailable):
        await asyncio.wait_for(reader.check(42, None, 2), timeout=1)
    assert store.read_org_flow_control.await_count == (0 if blocked_on_semaphore else 1)


async def test_logs_report_pause_capability_and_no_payload_or_exception(env, monkeypatch):
    reader, store, clock, _ = env
    records = []
    monkeypatch.setattr(flow.log, "info", lambda fmt, *args: records.append(fmt % args))
    store.read_org_flow_control.return_value = payload(version=2)
    await reader.check(42, "observed-ref", 2)
    assert "org_flow_control_version_observed" in records[0]
    parsed = json.loads(records[0].split(" ", 1)[1])
    assert parsed["capability"] == "pause" and parsed["config_version"] == 2
    assert parsed["org_id"] == 42 and parsed["call_ref"] == "observed-ref"
    assert parsed["worker_pid"] > 0
    store.read_org_flow_control.side_effect = RuntimeError("private-token-or-url")
    clock[0] += 3
    with pytest.raises(flow.FlowControlUnavailable):
        await reader.check(42, "failure-ref", 2)
    assert "private-token-or-url" not in "\n".join(records)
    assert "redis_unavailable" in records[-1]


async def test_same_version_key_order_is_not_a_conflict(env):
    reader, store, clock, _ = env
    original = payload(rate_limit={"rps": 1, "burst": 2})
    store.read_org_flow_control.return_value = original
    assert await reader.check(42, None, 2) == 1
    clock[0] += 3
    store.read_org_flow_control.return_value = json.dumps(
        json.loads(original), sort_keys=True, indent=2)
    assert await reader.check(42, None, 2) == 1


async def test_logging_failure_cannot_replace_pause_refusal(env, monkeypatch):
    reader, store, _, now = env
    store.read_org_flow_control.return_value = payload(
        pause_until=(now[0] + timedelta(minutes=30)).isoformat().replace("+00:00", "Z"))
    def broken_log(*_args):
        raise OSError("log sink unavailable")
    monkeypatch.setattr(flow.log, "info", broken_log)
    with pytest.raises(flow.PauseRejected):
        await reader.check(42, None, 2)


def test_valid_publisher_year_zero_is_already_expired():
    snapshot = flow._parse(payload(pause_until="0000-02-29T00:00:00Z"), 42)
    assert snapshot.pause_until == datetime.min.replace(tzinfo=timezone.utc)
