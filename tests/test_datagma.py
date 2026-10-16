from __future__ import annotations

import json

import httpx
import pytest

from treg import api as A
from treg.application.call import settle as call_settle
from treg.application.call.resolve import MarketplaceCall
from treg.domain.capacity import collectors, policy


def _mk(unit_micro=22_580, endpoint_id="datagma.people.email.find"):
    return MarketplaceCall(
        tool=None, upstream="https://gateway.datagma.net/api/ingress/v8/findEmail",
        consumed=set(), provider="datagma", endpoint_id=endpoint_id,
        tier="platform", estimate_micro=unit_micro, cost_type="per_success",
        unit_micro=unit_micro, reported_charge_unit_micro=22_580, request_data={},
    )


@pytest.mark.parametrize("raw,expected", [
    ("0", 0), ("1", 22_580), (30, 677_400), ("1.5", 33_870),
    (-1, None), (True, None), ("bad", None), (None, None),
])
def test_datagma_settles_from_reported_credit_burn(raw, expected):
    assert call_settle._observed_cost_micro(
        _mk(), json.dumps({"creditBurn": raw}).encode()) == expected


def test_datagma_phone_converts_credit_burn_at_one_credit_not_the_call_price():
    # Phone find costs 30 credits a call, so its unit is the whole call: `creditBurn: 30` once
    # settled as 30 x 30 credits ($20.32 for a $0.68 lookup).
    mk = _mk(unit_micro=677_400, endpoint_id="datagma.people.phone.find")
    assert call_settle._observed_cost_micro(mk, b'{"creditBurn": "30"}') == 677_400


async def test_datagma_internal_balance_exposes_only_credit_count(monkeypatch):
    monkeypatch.setenv("TREG_PLATFORM_KEY_DATAGMA", "private-test-key")
    collectors.get_settings.cache_clear()

    def reply(request):
        assert request.url.path == "/api/ingress/v1/mine"
        assert request.url.params["apiId"] == "private-test-key"
        return httpx.Response(200, json={
            "currentCredit": "3002", "email": "owner@example.test", "plan": "private",
        })

    try:
        async with httpx.AsyncClient(transport=httpx.MockTransport(reply)) as client:
            row = await collectors.provider_balance("datagma", client)
        assert row == {"provider": "datagma", "value": 3002, "unit": "credits",
                       "note": "Prepaid balance; replenished manually"}
        assert "owner" not in str(row) and "plan" not in str(row)
        capacity = policy.default_policy("datagma", has_key=True)
        assert capacity.funding_mode == "manual"
        assert capacity.rate_limit == {"limit": 10, "window_s": 1, "source": "docs"}
    finally:
        collectors.get_settings.cache_clear()


async def test_datagma_balance_failure_does_not_expose_query_key(monkeypatch):
    monkeypatch.setenv("TREG_PLATFORM_KEY_DATAGMA", "private-test-key")
    collectors.get_settings.cache_clear()
    try:
        async with httpx.AsyncClient(transport=httpx.MockTransport(
                lambda request: httpx.Response(401, json={"error": "bad"}, request=request))) as client:
            row = await collectors.provider_balance("datagma", client)
        assert row["value"] is None
        assert "private-test-key" not in str(row)
    finally:
        collectors.get_settings.cache_clear()


async def test_datagma_connection_probe_is_internal_and_generic(clients, monkeypatch):
    def probe(request):
        assert request.url.path == "/api/ingress/v1/mine"
        if request.url.params["apiId"] == "bad":
            return httpx.Response(401, json={"account": "must-not-leak"})
        return httpx.Response(200, json={"currentCredit": "3002", "email": "must-not-leak"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(probe)) as upstream:
        monkeypatch.setattr(A.app.state, "http", upstream)
        bad = await clients.post("/connections/token", json={"provider": "datagma", "token": "bad"})
        assert bad.status_code == 422 and "must-not-leak" not in bad.text
        good = await clients.post("/connections/token", json={"provider": "datagma", "token": "own"})
        assert good.status_code == 200 and "currentCredit" not in good.text and "must-not-leak" not in good.text


async def test_an_expansion_flag_declared_off_cannot_be_switched_on_with_treg_key(clients, monkeypatch):
    """people.enrich is priced at one credit with phoneFull/personFull off (`enum: [false]`). On
    treg's key a request that switches one on is refused before any money moves, whatever the
    spelling; an own key relays it unchanged, and leaving the flag off still works."""
    from treg.config import get_settings
    monkeypatch.setenv("TREG_PLATFORM_KEY_DATAGMA", "PLATFORM-DATAGMA")
    monkeypatch.setenv("TREG_PLATFORM_PROVIDERS", "datagma")
    get_settings.cache_clear()
    seen = []

    def upstream(request):
        seen.append(dict(request.url.params))
        return httpx.Response(200, headers={"content-type": "application/json"}, stream=httpx.ByteStream(
            json.dumps({"person": {"fullName": "Ada Example"}, "creditBurn": 1}).encode()))

    base = {"data": "acme.example", "fullName": "Ada Example", "companyPremium": "False",
            "companyFull": "False", "personFull": "False", "phoneFull": "False",
            "deepTraffic": "False", "debug": "False"}
    org_id = (await clients.get("/orgs")).json()[0]["org_id"]

    async def balance():
        return (await clients.get(f"/orgs/{org_id}/balance")).json()["balance_micro"]

    try:
        async with httpx.AsyncClient(transport=httpx.MockTransport(upstream)) as client:
            monkeypatch.setattr(A.app.state, "http", client)
            before = await balance()
            for spelling in ("True", "true", "1"):
                refused = await clients.get("/call/datagma.people.enrich",
                                            params={**base, "phoneFull": spelling})
                assert refused.status_code == 400, refused.text
                assert refused.json()["detail"]["parameter"] == "queryParams.phoneFull"
            assert seen == [] and await balance() == before

            served = await clients.get("/call/datagma.people.enrich", params=base)
            assert served.status_code == 200, served.text
            assert served.headers["X-Treg-Cost-Micro"] == "22580"
            assert before - await balance() == 22_580

            await clients.post("/secrets", json={"name": "datagma", "value": "OWN-DATAGMA"})
            own = await clients.get("/call/datagma.people.enrich", params={**base, "personFull": "true"})
            assert own.status_code == 200, own.text
            assert seen[-1]["personFull"] == "true" and seen[-1]["apiId"] == "OWN-DATAGMA"
    finally:
        get_settings.cache_clear()
