"""The /showcase leaderboard: teams apply, staff review, the public board shows only what was
approved and consented, and people upvote.

What these pin: an owner or admin applies for their team, at most five entries per team; nothing is
public before approval, and approval publishes the pictures before the row points at them; an edit
to a live entry leaves the live version until it is approved; a rejected application can be fixed
and resubmitted; the public JSON never carries a team, an exact call count or money; the weekly fold
counts exactly a team's upstream calls; one upvote per person per app; none of it on a self-hosted
registry.
"""
from __future__ import annotations

import json
from datetime import date, datetime, timedelta
from io import BytesIO

import pytest
from httpx import ASGITransport, AsyncClient
from PIL import Image
from sqlalchemy.exc import IntegrityError
from sqlmodel import func, select

from fake_media_store import MemoryMediaStore
from treg.api import app
from treg.application import images, showcase
from treg.config import get_settings
from treg.domain import showcase as rules
from treg.domain.identity import session
from treg.infra.db import session_maker
from treg.models import (CallRecord, Membership, Org, ShowcaseApp, ShowcaseAppEvent, ShowcaseCursor, ShowcaseVote,
                         ShowcaseWeek, User)
from treg.timeutil import utcnow_naive

TODAY = utcnow_naive().date()
NOW = datetime(TODAY.year, TODAY.month, TODAY.day, 12)
NEWEST = rules.last_complete_week(TODAY)
ADMIN = "showcase-staff-token-for-tests"


@pytest.fixture
def store():
    st = MemoryMediaStore()
    images.use_store(st)
    yield st
    images.use_store(None)


@pytest.fixture
def staff(monkeypatch):
    monkeypatch.setenv("TREG_ADMIN_TOKEN", ADMIN)
    get_settings.cache_clear()
    yield {"X-Treg-Token": ADMIN}
    get_settings.cache_clear()


def picture(w=300, h=300) -> bytes:
    out = BytesIO()
    Image.new("RGB", (w, h), (20, 120, 200)).save(out, "PNG")
    return out.getvalue()


async def _tim() -> tuple[User, int]:
    async with session_maker() as db:
        user = (await db.execute(select(User).where(User.email == "tim@superdesign.dev"))).scalar_one()
        org_id = (await db.execute(select(Membership.org_id).where(Membership.user_id == user.id))).scalars().first()
        return user, org_id


def _cookie(client, user: User) -> None:
    client.headers.pop("X-Treg-Token", None)
    client.cookies.set(session.COOKIE, session.make_session(user.id, token_version=user.token_version))


def fields(**over) -> dict:
    return {"name": "Example App", "url": "https://example.com", "tagline": "Does one thing well",
            "description": "A longer description.", "category": "ads", "maker_name": "Ada",
            "x_handle": "@ada", "show_calls": True, "show_streak": True, **over}


# ---- the rules, alone ---------------------------------------------------------------------------

def _week(start, calls, days=(), providers=(), categories=None):
    return rules.Week(week_start=start, calls=calls, active_days=sum(1 << d for d in days),
                      providers=frozenset(providers), categories=categories or {})


def test_tiers_streaks_growth_and_rising():
    prev = NEWEST - timedelta(days=7)
    act = rules.activity([_week(prev, 400, (4, 5, 6), {"a", "b"}, {"search": 400}),
                          _week(NEWEST, 900, (0, 1, 2, 3), {"b", "c"}, {"search": 100, "contacts": 800})], NEWEST)
    assert (act.calls, act.active_days, act.providers, act.streak_days) == (1300, 7, 3, 7)
    assert act.categories == ["contacts", "search"] and act.growth_pct == 125 and act.rising
    small = rules.activity([_week(prev, 10, (6,)), _week(NEWEST, 400, (0,))], NEWEST)
    assert small.growth_pct == 3900 and not small.rising


def test_an_application_is_checked_field_by_field():
    clean = rules.clean_revision(fields(name="  Spaced   Name  ", x_handle="@ada"))
    assert clean["name"] == "Spaced Name" and clean["x_handle"] == "ada" and clean["shot_keys"] == []
    for bad in ({"url": "http://example.com"}, {"url": "javascript:alert(1)"}, {"category": "nope"},
                {"name": ""}, {"tagline": "x" * 81}, {"x_handle": "not a handle"},
                {"launch_post_url": "ftp://x"}, {"shot_keys": ["a"] * 5}, {"surprise": 1}):
        with pytest.raises(rules.ApplicationInvalid):
            rules.clean_revision(fields(**bad))


def test_public_entry_shows_only_consented_buckets():
    act = rules.Activity(calls=123_456, active_days=20, providers=12, categories=["search"],
                         streak_days=9, growth_pct=73, rising=True)
    assert rules.public_entry(act, rules.Consent(), 3) == {"upvotes": 3, "badges": []}
    full = rules.public_entry(act, rules.Consent(True, True, True, True), 0)
    assert full["calls_tier"] == "100k+" and full["growth_pct"] == 70 and "123456" not in json.dumps(full)


# ---- applying and review ------------------------------------------------------------------------

async def _upload(client, kind, data=None) -> str:
    r = await client.post(f"/images/{kind}", content=data or picture(1600 if kind == "showcase_shot" else 300,
                                                                     1000 if kind == "showcase_shot" else 300))
    assert r.status_code == 200, r.text
    return r.json()["key"]


async def test_apply_review_approve_and_nothing_public_before(clients, store, staff):
    user, org_id = await _tim()
    _cookie(clients, user)
    teams = (await clients.get("/showcase/my-teams")).json()
    assert any(t["id"] == org_id and t["limit"] == 5 for t in teams)
    icon, shot = await _upload(clients, "showcase_icon"), await _upload(clients, "showcase_shot")
    applied = await clients.post(f"/showcase/teams/{org_id}/apps", json=fields(icon_key=icon, shot_keys=[shot]))
    assert applied.status_code == 200, applied.text
    entry = applied.json()
    assert entry["status"] == "pending" and entry["live"] is None
    assert entry["pending"]["icon_url"] == f"/images/staging/{icon}"
    assert store.keys("public") == []
    assert (await clients.get("/showcase/leaderboard")).json()["apps"] == []

    queue = (await clients.get("/admin/showcase/apps?status=review", headers=staff)).json()
    assert [a["id"] for a in queue] == [entry["id"]] and queue[0]["team"]["id"] == org_id
    stale = await clients.post(f"/admin/showcase/apps/{entry['id']}/approve", headers=staff,
                               json={"expected_version": 0, "actor": "ops@superdesign.dev"})
    assert stale.status_code == 409
    approved = await clients.post(f"/admin/showcase/apps/{entry['id']}/approve", headers=staff,
                                  json={"expected_version": entry["version"], "actor": "ops@superdesign.dev"})
    assert approved.status_code == 200, approved.text
    assert approved.json()["status"] == "approved" and approved.json()["pending"] is None
    assert icon in store.keys("public") and shot in store.keys("public")
    assert shot.replace(".webp", "-640.webp") in store.keys("public")
    board = (await clients.get("/showcase/leaderboard")).json()
    (pub,) = board["apps"]
    assert pub["name"] == "Example App" and pub["icon_url"] == f"/media-files/{icon}"
    assert pub["shots"][0]["small_url"].endswith("-640.webp")
    async with session_maker() as db:   # approval counts nothing; the worker's next run does
        assert (await db.execute(select(ShowcaseWeek).where(ShowcaseWeek.app_id == entry["id"]))).scalars().all() == []
    # Once approved, nothing waits for review any more.
    assert (await clients.get("/admin/showcase/apps?status=review", headers=staff)).json() == []
    detail = (await clients.get(f"/admin/showcase/apps/{entry['id']}", headers=staff)).json()
    assert [h["action"] for h in detail["history"]] == ["approved", "applied"]
    assert detail["history"][0]["actor"] == "admin:ops@superdesign.dev"


async def test_an_edit_to_a_live_app_waits_for_review(clients, store, staff):
    user, org_id = await _tim()
    _cookie(clients, user)
    entry = (await clients.post(f"/showcase/teams/{org_id}/apps", json=fields())).json()
    entry = (await clients.post(f"/admin/showcase/apps/{entry['id']}/approve", headers=staff,
                                json={"expected_version": entry["version"]})).json()
    edited = await clients.patch(f"/showcase/apps/{entry['id']}",
                                 json={"expected_version": entry["version"], "fields": fields(name="Renamed")})
    assert edited.status_code == 200
    assert edited.json()["live"]["name"] == "Example App" and edited.json()["pending"]["name"] == "Renamed"
    assert (await clients.get("/showcase/leaderboard")).json()["apps"][0]["name"] == "Example App"
    rejected = await clients.post(f"/admin/showcase/apps/{entry['id']}/reject", headers=staff,
                                  json={"expected_version": edited.json()["version"], "reason": "Name taken"})
    assert rejected.json()["status"] == "approved" and rejected.json()["pending"] is None
    assert rejected.json()["reject_reason"] == "Name taken"
    assert (await clients.get("/showcase/leaderboard")).json()["apps"][0]["name"] == "Example App"


async def test_a_rejected_application_can_be_fixed_and_resubmitted(clients, staff):
    user, org_id = await _tim()
    _cookie(clients, user)
    entry = (await clients.post(f"/showcase/teams/{org_id}/apps", json=fields())).json()
    assert (await clients.post(f"/admin/showcase/apps/{entry['id']}/reject", headers=staff,
                               json={"expected_version": entry["version"], "reason": ""})).status_code == 422
    rejected = (await clients.post(f"/admin/showcase/apps/{entry['id']}/reject", headers=staff,
                                   json={"expected_version": entry["version"], "reason": "Add a real link"})).json()
    assert rejected["status"] == "rejected"
    again = await clients.patch(f"/showcase/apps/{entry['id']}", json={"expected_version": rejected["version"],
                                                                         "fields": fields(url="https://example.org")})
    assert again.json()["status"] == "pending" and again.json()["reject_reason"] == ""


async def test_five_apps_per_team_and_only_owners_or_admins_apply(clients, staff):
    user, org_id = await _tim()
    _cookie(clients, user)
    for i in range(5):
        assert (await clients.post(f"/showcase/teams/{org_id}/apps", json=fields(name=f"App {i}"))).status_code == 200
    assert (await clients.post(f"/showcase/teams/{org_id}/apps", json=fields(name="App 6"))).status_code == 409
    assert (await clients.post(f"/admin/showcase/teams/{org_id}/apps", headers=staff,
                               json={"fields": fields(name="Staff 6")})).status_code == 409
    async with session_maker() as db:
        db.add(Org(name="Not mine", slug="not-mine"))
        await db.commit()
        other = (await db.execute(select(Org.id).where(Org.slug == "not-mine"))).scalar_one()
    assert (await clients.post(f"/showcase/teams/{other}/apps", json=fields())).status_code == 403
    assert (await clients.post(f"/showcase/teams/{org_id}/apps", json=fields(),
                               headers={"Origin": "https://evil.example"})).status_code == 403


async def test_staff_create_hide_and_the_board_follows(clients, store, staff):
    _, org_id = await _tim()
    icon = (await clients.post("/admin/images/showcase_icon", headers=staff, content=picture())).json()["key"]
    made = await clients.post(f"/admin/showcase/teams/{org_id}/apps", headers=staff,
                              json={"fields": fields(icon_key=icon), "approve": True, "actor": "ops@superdesign.dev"})
    assert made.status_code == 200 and made.json()["status"] == "approved"
    assert icon in store.keys("public")
    hidden = await clients.post(f"/admin/showcase/apps/{made.json()['id']}/hide", headers=staff,
                                json={"expected_version": made.json()["version"], "hidden": True})
    assert hidden.json()["status"] == "hidden"
    assert (await clients.get("/showcase/leaderboard")).json()["apps"] == []
    assert (await clients.get("/admin/showcase/apps", headers={"X-Treg-Token": "wrong"})).status_code == 403


# ---- the public board and the weekly fold ---------------------------------------------------------

async def _approved(org_id: int, **over) -> int:
    async with session_maker() as db:
        app_ = ShowcaseApp(org_id=org_id, status="approved", approved_at=datetime(2026, 9, 1), **{**{
            k: v for k, v in rules.clean_revision(fields()).items()}, **over})
        db.add(app_)
        await db.commit()
        return app_.id


async def _calls(org_id, at, n, *, provider="tikhub", endpoint="tikhub.tiktok.user.profile", refused_by=None):
    async with session_maker() as db:
        for _ in range(n):
            db.add(CallRecord(org_id=org_id, user_email="a@b.c", tool_name=endpoint, method="GET", path="/x",
                              status_code=200, endpoint_id=endpoint, provider=provider, refused_by=refused_by,
                              created_at=at))
        await db.commit()


async def test_the_fold_counts_only_new_upstream_calls_in_small_batches(clients):
    _, org_id = await _tim()
    app_id = await _approved(org_id)
    monday = datetime(NEWEST.year, NEWEST.month, NEWEST.day, 9)
    await _calls(org_id, rules.window_start(TODAY) - timedelta(days=3), 7)          # before the window
    await _calls(org_id, monday, 3)
    await _calls(org_id, monday + timedelta(days=2), 2, provider="anyapi", endpoint="anyapi.x.search.posts")
    await _calls(org_id, monday + timedelta(days=2), 4, refused_by="policy")
    first = await showcase.refresh_activity(now=NOW, batch_rows=2)
    assert first["caught_up"] and first["calls"] == 9      # refused calls move the cursor but are not activity
    async with session_maker() as db:
        row = await db.get(ShowcaseWeek, (app_id, NEWEST.isoformat()))
        assert (await db.execute(select(func.count()).select_from(ShowcaseWeek))).scalar_one() == 1
    assert (row.calls, row.active_days, row.providers) == (5, 0b101, ["anyapi", "tikhub"])
    assert sum(row.categories.values()) == 5

    # A second run reads only what arrived since; a call younger than the lag waits for the next.
    await _calls(org_id, monday + timedelta(days=3), 1)
    await _calls(org_id, NOW - timedelta(minutes=1), 1)
    again = await showcase.refresh_activity(now=NOW)
    assert again["calls"] == 1
    async with session_maker() as db:
        assert (await db.get(ShowcaseWeek, (app_id, NEWEST.isoformat()))).calls == 6
    assert (await showcase.refresh_activity(now=NOW))["calls"] == 0
    assert (await showcase.refresh_activity(now=NOW + timedelta(minutes=10)))["calls"] == 1


async def test_a_capped_run_resumes_where_it_stopped(clients):
    _, org_id = await _tim()
    app_id = await _approved(org_id)
    await _calls(org_id, datetime(NEWEST.year, NEWEST.month, NEWEST.day, 9), 5)
    first = await showcase.refresh_activity(now=NOW, max_rows=3, batch_rows=2)
    assert (first["calls"], first["caught_up"]) == (3, False)
    second = await showcase.refresh_activity(now=NOW, max_rows=3, batch_rows=2)
    assert (second["calls"], second["caught_up"]) == (2, True)
    async with session_maker() as db:
        assert (await db.get(ShowcaseWeek, (app_id, NEWEST.isoformat()))).calls == 5


async def test_weeks_that_leave_the_window_are_dropped(clients):
    _, org_id = await _tim()
    app_id = await _approved(org_id)
    async with session_maker() as db:
        old = (rules.window_start(TODAY) - timedelta(days=7)).isoformat()
        db.add(ShowcaseWeek(app_id=app_id, week_start=old, calls=9, active_days=1))
        await db.commit()
    await showcase.refresh_activity(now=NOW)
    async with session_maker() as db:
        assert await db.get(ShowcaseWeek, (app_id, old)) is None


async def test_the_public_board_never_leaks_the_team_or_exact_numbers(clients):
    _, org_id = await _tim()
    open_id = await _approved(org_id, name="Open")
    await _approved(org_id, name="Quiet", show_calls=False, show_streak=False)
    await _calls(org_id, datetime(NEWEST.year, NEWEST.month, NEWEST.day, 9), 1234)
    await showcase.refresh_activity(now=NOW)
    r = await clients.get("/showcase/leaderboard")
    assert r.headers["cache-control"] == "no-cache" and "1234" not in r.text
    again = await clients.get("/showcase/leaderboard", headers={"If-None-Match": r.headers["etag"]})
    assert again.status_code == 304 and again.content == b""
    by_name = {a["name"]: a for a in r.json()["apps"]}
    assert by_name["Open"]["calls_tier"] == "1k+" and "calls_tier" not in by_name["Quiet"]
    assert r.json()["boards"]["most_active"] == [open_id]
    for a in r.json()["apps"]:
        assert not {"org_id", "team", "calls", "pending", "submitted_by"} & set(a)


async def test_an_upvote_is_one_per_person_per_app_and_can_be_taken_back(clients):
    user, org_id = await _tim()
    app_id = await _approved(org_id)
    clients.headers.pop("X-Treg-Token", None)
    assert (await clients.post("/showcase/vote", json={"id": app_id, "up": True})).status_code == 401
    _cookie(clients, user)
    for _ in range(2):  # a second upvote changes nothing
        assert (await clients.post("/showcase/vote", json={"id": app_id, "up": True})).json() == \
            {"id": app_id, "up": True, "upvotes": 1}
    assert (await clients.get("/showcase/votes/me")).json()["upvoted"] == [app_id]
    assert (await clients.post("/showcase/vote", json={"id": app_id, "up": False})).json()["upvotes"] == 0
    assert (await clients.get("/showcase/votes/me")).json()["upvoted"] == []
    assert (await clients.post("/showcase/vote", json={"id": app_id, "value": -1})).status_code == 422
    assert (await clients.post("/showcase/vote", json={"id": 999999, "up": True})).status_code == 404
    assert (await clients.post("/showcase/vote", json={"id": app_id, "up": True},
                               headers={"Origin": "https://evil.example"})).status_code == 403
    async with session_maker() as db:
        db.add(ShowcaseVote(app_id=app_id, user_id=user.id))
        await db.commit()
    with pytest.raises(IntegrityError):
        async with session_maker() as db:
            db.add(ShowcaseVote(app_id=app_id, user_id=user.id))
            await db.commit()


async def test_deleting_a_team_removes_its_entries_and_everything_under_them(clients):
    from treg.domain.governance.teams import cascade_delete_org
    user, _ = await _tim()
    async with session_maker() as db:
        org = Org(name="Leaving", slug="leaving-team")
        db.add(org)
        await db.flush()
        org_id = org.id
        await db.commit()
    app_id = await _approved(org_id)
    async with session_maker() as db:
        db.add(ShowcaseWeek(app_id=app_id, week_start=NEWEST.isoformat(), calls=1, active_days=1))
        db.add(ShowcaseCursor(app_id=app_id, through_id=1))
        db.add(ShowcaseVote(app_id=app_id, user_id=user.id, value=1))
        db.add(ShowcaseAppEvent(app_id=app_id, version=1, action="applied", actor="t", source="api"))
        await db.commit()
        await cascade_delete_org(await db.get(Org, org_id), db)
        await db.commit()
    async with session_maker() as db:
        for model in (ShowcaseApp, ShowcaseWeek, ShowcaseCursor, ShowcaseVote, ShowcaseAppEvent):
            assert (await db.execute(select(model))).scalars().all() == [], model.__name__


async def test_the_leaderboard_does_not_exist_on_a_self_hosted_registry(monkeypatch):
    monkeypatch.setenv("TREG_PUBLIC_URL", "https://registry.example.internal")
    get_settings.cache_clear()
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://registry") as c:
            for method, path in (("GET", "/showcase/leaderboard"), ("GET", "/showcase/votes/me"),
                                 ("POST", "/showcase/vote")):
                r = await c.request(method, path, json={"id": 1, "up": True} if method == "POST" else None)
                assert r.status_code == 404, path
    finally:
        get_settings.cache_clear()
