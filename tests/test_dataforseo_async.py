"""DataForSEO Standard tasks on treg's shared key: a /task_post is an async submission whose tasks
the submitting team owns, read back through the family's free task_get, and settled at the charge
DataForSEO reported when the task was posted (it bills at task_post; task_get reports no cost)."""
from __future__ import annotations

import json

import pytest
from httpx import AsyncClient
from sqlmodel import select
from test_asynctasks import _response

from treg.application.call import service as call_service
from treg.config import get_settings
from treg.domain.catalog import store as catalog_store
from treg.infra.db import session_maker
from treg.models import AsyncResourceRecord, AsyncTaskRecord

POST = "dataforseo.x.business-data-google-reviews-task-post"
GET = "dataforseo.x.business-data-google-reviews-task-get"
TASK_A, TASK_B = "10151200-0000-0000-0000-00000000000a", "10151200-0000-0000-0000-00000000000b"


def _task(task_id: str, status: int, cost: float, result=None) -> dict:
    return {"id": task_id, "status_code": status, "status_message": "", "cost": cost,
            "result_count": len(result or []), "result": result}


def _envelope(tasks: list[dict]) -> dict:
    return {"status_code": 20000, "status_message": "Ok.", "cost": sum(t["cost"] for t in tasks),
            "tasks_count": len(tasks), "tasks_error": 0, "tasks": tasks}


@pytest.fixture
def dfs_platform(monkeypatch):
    monkeypatch.setenv("TREG_PLATFORM_KEY_DATAFORSEO", "login:password")
    monkeypatch.setenv("TREG_PLATFORM_PROVIDERS", "dataforseo")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def test_every_shared_key_task_submission_has_an_owned_reader():
    """No platform-eligible /task_post may take a team's money for a task nothing can read."""
    catalog = catalog_store.load()
    dead_ends = []
    for ep in catalog.endpoints:
        if not (ep.get("path", "").endswith("/task_post") and catalog.platform_eligible(ep)):
            continue
        produced = {item["kind"] for item in
                    (ep.get("resource_ownership") or {}).get("produces") or []}
        readers = [other for other in catalog.endpoints
                   if other.get("provider") == ep.get("provider")
                   and ((other.get("resource_ownership") or {}).get("requires") or {}).get("kind")
                   in produced and catalog.platform_eligible(other)]
        if not readers:
            dead_ends.append(ep["id"])
    assert dead_ends == []


async def test_a_task_post_is_owned_polled_and_settled_at_the_submission_charge(
    clients: AsyncClient, monkeypatch, dfs_platform,
):
    answers = [
        _response(200, _envelope([_task(TASK_A, 20100, 0.00375), _task(TASK_B, 20100, 0.00375)])),
        _response(200, _envelope([_task(TASK_B, 40602, 0)])),
        _response(200, _envelope([_task(TASK_A, 20000, 0, result=[{"items": []}])])),
    ]

    async def fake_relay(*args, **kwargs):
        return answers.pop(0)

    monkeypatch.setattr(call_service, "relay", fake_relay)
    submitted = await clients.post(f"/call/{POST}", json=[
        {"keyword": "hedonism wines", "location_name": "London,England,United Kingdom"},
        {"keyword": "berry bros", "location_name": "London,England,United Kingdom"},
    ])
    assert submitted.status_code == 200
    descriptor = json.loads(submitted.headers["X-Treg-Async"])
    assert descriptor["poll"]["endpoint"] == GET

    async with session_maker() as db:
        task = (await db.execute(select(AsyncTaskRecord))).scalar_one()
        owned = {r.resource_id for r in (await db.execute(select(AsyncResourceRecord))).scalars()}
    assert task.task_id == TASK_A and task.status == "pending"
    assert task.settlement_basis["submission_charge_micro"] == 7_500
    assert owned == {TASK_A, TASK_B}  # every task of the batch, not only the tracked first one

    # The second task of the batch is the team's too, and reading it costs nothing.
    second = await clients.get(f"/call/{GET}", params={"id": TASK_B})
    assert second.status_code == 200

    # The owner's own poll sees the tracked task finish and settles the hold at what DataForSEO
    # reported for the submission, not at the catalog estimate.
    finished = await clients.get(f"/call/{GET}", params={"id": TASK_A})
    assert finished.status_code == 200
    async with session_maker() as db:
        task = await db.get(AsyncTaskRecord, task.call_id)
    assert task.status == "settled" and task.settled_micro == 7_500

    other = await clients.post("/users", json={"email": "dfs-stranger@example.com"})
    stranger = {"X-Treg-Token": other.json()["token"]}

    async def must_not_relay(*args, **kwargs):
        raise AssertionError("another team's task reached treg's DataForSEO account")

    monkeypatch.setattr(call_service, "relay", must_not_relay)
    for task_id in (TASK_A, TASK_B):
        denied = await clients.get(f"/call/{GET}", params={"id": task_id}, headers=stranger)
        assert denied.status_code == 403
        assert denied.json()["detail"]["error"] == "async_resource_not_owned"


async def test_a_task_that_fails_after_posting_still_bills_what_dataforseo_charged(
    clients: AsyncClient, monkeypatch, dfs_platform,
):
    answers = [
        _response(200, _envelope([_task(TASK_A, 20100, 0.00375)])),
        _response(200, _envelope([_task(TASK_A, 40102, 0)])),
    ]

    async def fake_relay(*args, **kwargs):
        return answers.pop(0)

    monkeypatch.setattr(call_service, "relay", fake_relay)
    submitted = await clients.post(f"/call/{POST}", json=[{"keyword": "nothing here"}])
    assert submitted.status_code == 200
    assert (await clients.get(f"/call/{GET}", params={"id": TASK_A})).status_code == 200
    async with session_maker() as db:
        task = (await db.execute(select(AsyncTaskRecord))).scalar_one()
    assert task.status == "settled" and task.settled_micro == 3_750


async def test_a_task_post_dataforseo_refused_settles_at_zero_without_a_task(
    clients: AsyncClient, monkeypatch, dfs_platform,
):
    async def fake_relay(*args, **kwargs):
        return _response(200, {**_envelope([_task(TASK_A, 40501, 0)]), "tasks_error": 1})

    monkeypatch.setattr(call_service, "relay", fake_relay)
    refused = await clients.post(f"/call/{POST}", json=[{"keyword": ""}])
    assert refused.status_code == 200
    assert refused.headers.get("X-Treg-Cost-Micro") == "0"
    async with session_maker() as db:
        assert (await db.execute(select(AsyncTaskRecord))).first() is None


async def test_an_on_page_crawl_is_readable_only_by_the_team_that_paid_for_it(
    clients: AsyncClient, monkeypatch, dfs_platform,
):
    answers = [
        _response(200, _envelope([_task(TASK_A, 20100, 0.018)])),
        _response(200, _envelope([_task(TASK_A, 20000, 0, result=[{"crawl_progress": "finished"}])])),
        _response(200, _envelope([_task(TASK_A, 20000, 0, result=[{"items": []}])])),
    ]

    async def fake_relay(*args, **kwargs):
        return answers.pop(0)

    monkeypatch.setattr(call_service, "relay", fake_relay)
    crawl = await clients.post("/call/dataforseo.x.on-page-task-post",
                               json=[{"target": "example.com", "max_crawl_pages": 10}])
    assert crawl.status_code == 200
    summary = await clients.get("/call/dataforseo.x.on-page-summary-id", params={"id": TASK_A})
    assert summary.status_code == 200
    pages = await clients.post("/call/dataforseo.x.on-page-pages", json=[{"id": TASK_A, "limit": 10}])
    assert pages.status_code == 200

    async def must_not_relay(*args, **kwargs):
        raise AssertionError("an unowned crawl reached treg's DataForSEO account")

    monkeypatch.setattr(call_service, "relay", must_not_relay)
    smuggled = await clients.post("/call/dataforseo.x.on-page-pages",
                                  json=[{"id": TASK_A}, {"id": TASK_B}])
    assert smuggled.status_code == 400
    other = await clients.post("/users", json={"email": "crawl-stranger@example.com"})
    denied = await clients.post("/call/dataforseo.x.on-page-pages", json=[{"id": TASK_A}],
                                headers={"X-Treg-Token": other.json()["token"]})
    assert denied.status_code == 403


def test_treg_call_await_follows_a_task_post_to_its_result():
    """`treg call <task_post> --await` reads the served descriptor: in the queue, then finished."""
    import httpx
    from test_aigc_pr_b import FakeClock

    from treg import cli

    descriptor = catalog_store.load().by_id[POST]["async"]
    polled = []
    replies = iter([_envelope([_task(TASK_A, 40602, 0)]),
                    _envelope([_task(TASK_A, 20000, 0, result=[{"items": [{"rating": 5}]}])])])

    def call_fn(target, params):
        polled.append((target, params))
        return httpx.Response(200, json=next(replies), request=httpx.Request("GET", "https://t"))

    submission = httpx.Response(200, json=_envelope([_task(TASK_A, 20100, 0.00375)]),
                                request=httpx.Request("POST", "https://t"))
    outcome = cli.await_async_task(descriptor, submission, call_fn, FakeClock(), 600)
    assert outcome["code"] == 0
    assert polled == [(GET, [("id", TASK_A)])] * 2
    assert outcome["recovery"] == f"treg call {GET} -p id={TASK_A}"
