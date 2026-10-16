"""BlitzAPI shared-key pricing, own-key precedence, capacity and routed adapters."""

from __future__ import annotations

import json

import httpx
import pytest

from treg.application.call import service as call_service
from treg.application import arena
from treg.config import get_settings
from treg.domain.catalog import store as catalog_store
from treg.domain.capacity import collectors, policy

from test_marketplace_call import _balance, _fake_relay, _telemetry


@pytest.fixture
def blitzapi_on(monkeypatch):
    monkeypatch.setenv("TREG_PLATFORM_KEY_BLITZAPI", "PLATFORM-BLITZAPI-KEY")
    monkeypatch.setenv("TREG_PLATFORM_PROVIDERS", "blitzapi")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


async def test_free_key_info_supplies_exact_monthly_balance():
    def probe(request):
        assert request.method == "GET"
        assert str(request.url) == "https://api.blitz-api.ai/v2/account/key-info"
        assert request.headers["x-api-key"] == "test-key"
        return httpx.Response(200, json={"valid": True, "records_remaining": 199982,
                                         "next_reset_at": "2026-11-16T13:40:18Z",
                                         "max_requests_per_seconds": 10})

    async with httpx.AsyncClient(transport=httpx.MockTransport(probe)) as client:
        row = await collectors._blitzapi(client, "test-key")
    assert row["value"] == 199982 and row["unit"] == "records"
    assert "10 requests/s" in row["note"]
    assert policy.default_policy("blitzapi", has_key=True).capacity_type == "monthly_quota"


@pytest.mark.parametrize("body", [
    {"valid": False, "records_remaining": 1},
    {"valid": True, "records_remaining": None},
    {"valid": True, "records_remaining": True},
    {"valid": True, "records_remaining": "1"},
    {"valid": True, "records_remaining": -1},
])
async def test_key_info_rejects_invalid_balance(body):
    async with httpx.AsyncClient(transport=httpx.MockTransport(
            lambda _: httpx.Response(200, json=body))) as client:
        with pytest.raises(ValueError, match="no valid remaining-record balance"):
            await collectors._blitzapi(client, "test-key")


def test_seven_blitzapi_adapters_are_fixture_verified():
    catalog = catalog_store.load()
    ids = {"people.email.find", "people.phone.find", "people.enrich",
           "people.email.reverse", "companies.enrich", "people.search", "companies.search"}
    assert {f"blitzapi.{name}" for name in ids} <= {
        key for key, adapter in catalog.adapters.items() if adapter.verified
    }
    assert len([key for key in catalog.by_id if key.startswith("blitzapi.")]) == 18


def test_existing_arena_tasks_admit_the_matching_blitzapi_tools():
    exposed = {
        task["id"]: {item["endpoint_id"] for variant in task["provider_previews"]
                     for item in variant if item["provider"] == "blitzapi"}
        for task in arena.public_tasks()
    }
    assert "blitzapi.people.search" in exposed["people.search"]
    assert "blitzapi.people.email.find" in exposed["people.email.find"]
    assert {"blitzapi.people.enrich", "blitzapi.people.email.reverse"} <= exposed["people.enrich"]
    assert "blitzapi.companies.enrich" in exposed["companies.enrich"]
    assert "blitzapi.people.phone.find" in exposed["people.phone.find"]


async def test_platform_settles_reported_records_and_free_miss(clients, blitzapi_on, monkeypatch):
    endpoint = "/call/blitzapi.people.email.find"
    request = {"person_linkedin_url": "https://www.linkedin.com/in/alex-example"}
    before = await _balance(clients)
    monkeypatch.setattr(call_service, "relay", _fake_relay(200, json.dumps({
        "found": True, "email": "alex@example.com", "fair_usage": {"records_used": 1}
    }).encode()))
    hit = await clients.post(endpoint, json=request)
    assert hit.status_code == 200, hit.text
    charged = int(hit.headers["X-Treg-Cost-Micro"])
    assert charged > 0
    assert await _balance(clients) == before - charged
    assert (await _telemetry(clients))["credential_tier"] == "platform"

    monkeypatch.setattr(call_service, "relay", _fake_relay(200, json.dumps({
        "found": False, "email": None, "fair_usage": {"records_used": 0}
    }).encode()))
    miss = await clients.post(endpoint, json={"person_linkedin_url": "https://www.linkedin.com/in/unknown-example"})
    assert miss.status_code == 200, miss.text
    assert int(miss.headers.get("X-Treg-Cost-Micro", "0")) == 0
    assert await _balance(clients) == before - charged


async def test_own_key_wins_and_is_unmetered(clients, blitzapi_on, monkeypatch):
    await clients.post("/secrets", json={"name": "blitzapi", "value": "OWN-BLITZAPI-KEY"})
    monkeypatch.setattr(call_service, "relay", _fake_relay(200, json.dumps({
        "found": True, "email": "alex@example.com", "fair_usage": {"records_used": 1}
    }).encode()))
    before = await _balance(clients)
    response = await clients.post("/call/blitzapi.people.email.find", json={
        "person_linkedin_url": "https://www.linkedin.com/in/alex-example"})
    assert response.status_code == 200, response.text
    assert "X-Treg-Cost-Micro" not in response.headers
    assert await _balance(clients) == before
    assert (await _telemetry(clients))["credential_tier"] == "credential"


async def test_search_settles_returned_rows_below_requested_page(clients, blitzapi_on, monkeypatch):
    monkeypatch.setattr(call_service, "relay", _fake_relay(200, json.dumps({
        "results": [{"full_name": "Alex Example"}, {"full_name": "Jordan Example"}],
        "results_length": 2, "fair_usage": {"records_used": 2},
    }).encode()))
    before = await _balance(clients)
    response = await clients.post("/call/blitzapi.people.search", json={
        "people": {"job_title": {"include": ["Founder"]}}, "max_results": 5})
    assert response.status_code == 200, response.text
    charged = int(response.headers["X-Treg-Cost-Micro"])
    assert charged > 0
    assert await _balance(clients) == before - charged
    assert charged < 5 * 495 * 2  # the five-row hold was reduced to the two reported records


async def test_routed_email_find_can_choose_blitzapi(clients, blitzapi_on, monkeypatch):
    monkeypatch.setattr(call_service, "relay", _fake_relay(200, json.dumps({
        "found": True, "email": "alex@example.com", "fair_usage": {"records_used": 1}
    }).encode()))
    response = await clients.post("/call/treg.people.email.find", json={
        "linkedin_url": "https://www.linkedin.com/in/alex-example"})
    assert response.status_code == 200, response.text
    assert response.json()["_treg"]["provider"] == "blitzapi"
