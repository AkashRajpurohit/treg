"""Bounded published-rule reads against disposable loopback Redis."""
import asyncio
import json
import os
from datetime import datetime, timedelta, timezone
from urllib.parse import urlsplit
from uuid import uuid4

import pytest
from pydantic import ValidationError

from treg.config import Settings
from treg.infra import kv, org_flow_control as flow


def test_pause_settings_are_default_off_and_require_explicit_redis():
    assert not Settings(_env_file=None).org_flow_control_enabled
    for value in ("", "http://localhost", "redis://[invalid"):
        with pytest.raises(ValidationError, match="valid Redis kv_url"):
            Settings(_env_file=None, org_flow_control_enabled=True, kv_url=value)
    assert Settings(_env_file=None, org_flow_control_enabled=True,
                    kv_url="redis://localhost").org_flow_control_refresh_s == 2


@pytest.mark.parametrize("value", [0, 0.1, 11, float("inf"), float("nan")])
def test_pause_refresh_interval_is_bounded(value):
    with pytest.raises(ValidationError):
        Settings(_env_file=None, org_flow_control_refresh_s=value)


@pytest.fixture
async def redis_store():
    url = os.environ.get("TREG_TEST_KV_URL")
    if not url:
        pytest.skip("requires disposable loopback Redis via TREG_TEST_KV_URL")
    assert urlsplit(url).hostname in {"127.0.0.1", "localhost", "::1"}, "local Redis only"
    store = kv.RedisStore(url)
    org_id = uuid4().int % 2147483647 + 1
    key = f"treg:traffic-control:org:{org_id}"
    try:
        assert await store.ping()
        yield store, org_id, key
    finally:
        await store._client.delete(key)
        await store.aclose()


async def test_read_distinguishes_missing_empty_oversize_and_wrong_type(redis_store):
    store, org_id, key = redis_store
    assert await store.read_org_flow_control(org_id) is None
    await store._client.set(key, "")
    assert await store.read_org_flow_control(org_id) == ""
    await store._client.set(key, "x" * 32769)
    assert await store.read_org_flow_control(org_id) is False
    await store._client.delete(key)
    await store._client.lpush(key, "synthetic wrong type")
    assert await store.read_org_flow_control(org_id) is False


async def test_published_json_is_read_without_mutation_and_new_versions_resume(redis_store, monkeypatch):
    store, org_id, key = redis_store
    now = datetime.now(timezone.utc)
    config = {"schema_version": 1,
              "pause_until": (now + timedelta(minutes=30)).isoformat().replace("+00:00", "Z"),
              "rate_limit": None, "concurrency_limit": None}
    envelope = {"org_id": org_id, "version": 1, "config": config}
    raw = json.dumps(envelope)
    await store._client.set(key, raw)
    assert await store.read_org_flow_control(org_id) == raw
    assert await store._client.ttl(key) == -1
    monkeypatch.setattr(kv, "store", lambda: store)
    reader = flow.Reader()
    with pytest.raises(flow.PauseRejected) as failure:
        await reader.check(org_id, "synthetic-call-ref", 0)
    assert failure.value.detail["config_version"] == 1
    envelope["version"] = 2
    envelope["config"]["pause_until"] = None
    await store._client.set(key, json.dumps(envelope))
    assert await reader.check(org_id, "synthetic-resume-ref", 0) == 2
    assert await store._client.ttl(key) == -1


async def test_unavailable_and_local_backend_never_look_like_missing():
    store = kv.RedisStore("redis://127.0.0.1:1")
    try:
        assert await store.read_org_flow_control(42) is False
        assert await kv.LocalStore().read_org_flow_control(42) is False
    finally:
        await store.aclose()


async def test_read_timeout_and_error_do_not_expose_data_or_retry(monkeypatch):
    class Slow:
        async def eval(self, *_args):
            await asyncio.sleep(10)

    store = kv.RedisStore("redis://127.0.0.1:1")
    client = store._client
    try:
        monkeypatch.setattr(store, "_client", Slow())
        assert await asyncio.wait_for(store.read_org_flow_control(42), 1) is False
    finally:
        await client.aclose()
