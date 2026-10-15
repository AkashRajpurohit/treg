"""The /showcase leaderboard: applications, review, weekly activity, the public board and votes.

treg is the only writer of every showcase table:

- makers (an owner or admin of the team) apply, edit and withdraw through `apply`, `edit`,
  `withdraw`. A revision is stored in `ShowcaseApp.pending` and is not public until approved; an
  edit to a live app leaves the live version in place until then.
- staff review through `approve`, `reject`, `set_hidden`, `staff_edit`, `staff_create` (the
  private admin calls them through the staff API). Approval publishes the revision's pictures
  first and only then points the row at them, so a page never names a missing file.
- `refresh_weeks` (the `treg-worker showcase weeks` cron) folds each approved app's team calls
  into one `ShowcaseWeek` row per UTC week, reading only weeks not stored yet plus the newest.
- `leaderboard` and `vote` are the public side; activity leaves only through
  `domain.showcase.public_entry`.

Every database step opens and closes its own session around picture storage I/O (AGENTS.md
rule 3). Each change appends a `ShowcaseAppEvent` under the next version, in the same commit.
"""
from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import date, datetime, timedelta

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from .. import ratestore
from ..domain import showcase as rules
from ..domain.catalog import store as catalog_store
from ..domain.identity.api_keys import is_machine_email
from ..domain.images import ImageRejected, parse_key
from ..infra.db import session_maker
from ..models import CallRecord, Membership, Org, ShowcaseApp, ShowcaseAppEvent, ShowcaseVote, ShowcaseWeek, User
from ..timeutil import utcnow_naive
from . import images

log = logging.getLogger("treg.showcase")

VOTE_NS = "showcase_vote"
VOTE_WINDOW_S = 3600
VOTE_MAX_PER_USER = 60
VOTE_MAX_PER_IP = 300
APPLY_NS = "showcase_apply"
APPLY_MAX_PER_USER = 20


class ShowcaseError(Exception):
    """A framework-neutral refusal translated by the HTTP router."""

    def __init__(self, kind: str, detail: str = ""):
        self.kind, self.detail = kind, detail
        super().__init__(kind)


# ---- shared helpers ------------------------------------------------------------------------------

def _insert(db: AsyncSession):
    if db.get_bind().dialect.name == "postgresql":
        from sqlalchemy.dialects.postgresql import insert
    else:
        from sqlalchemy.dialects.sqlite import insert
    return insert


def live_of(app: ShowcaseApp) -> dict:
    return {f: getattr(app, f) for f in rules.REVISION_FIELDS}


def _set_live(app: ShowcaseApp, rev: dict) -> None:
    for f in rules.REVISION_FIELDS:
        setattr(app, f, list(rev[f]) if f == "shot_keys" else rev[f])


def _event(db: AsyncSession, app: ShowcaseApp, action: str, changed: dict, *, actor: str, source: str,
           note: str = "") -> None:
    app.version += 1
    app.updated_at = utcnow_naive()
    db.add(ShowcaseAppEvent(app_id=app.id, version=app.version, action=action, changes=changed,
                            note=note, actor=actor, source=source))


def _check_keys(rev: dict) -> None:
    if rev["icon_key"] is not None:
        parsed = parse_key(rev["icon_key"])
        if parsed is None or parsed[0].name != "showcase_icon":
            raise ShowcaseError("invalid", "icon_key: unknown picture")
    for key in rev["shot_keys"]:
        parsed = parse_key(key)
        if parsed is None or parsed[0].name != "showcase_shot":
            raise ShowcaseError("invalid", "shot_keys: unknown picture")


def _clean(raw: dict) -> dict:
    try:
        rev = rules.clean_revision(raw)
    except rules.ApplicationInvalid as exc:
        raise ShowcaseError("invalid", f"{exc.field}: {exc.detail}") from None
    _check_keys(rev)
    return rev


def _shot_urls(key: str) -> dict:
    return {"url": images.public_url(key), "small_url": images.public_url(key.replace(".webp", "-640.webp"))}


def _preview(rev: dict | None, *, live: bool) -> dict | None:
    """A revision with picture URLs: live pictures from the public side, pending ones previewed from
    staging (they are public only once approved)."""
    if rev is None:
        return None
    url = images.public_url if live else (lambda k: images.staging_url(k) if k else None)
    return {**rev, "icon_url": url(rev["icon_key"]),
            "shot_urls": [_shot_urls(k)["url"] if live else images.staging_url(k) for k in rev["shot_keys"]]}


def app_view(app: ShowcaseApp, *, org: Org | None = None) -> dict:
    """The maker's and staff's view of one entry: status, the live version, the proposed one."""
    view = {
        "id": app.id, "org_id": app.org_id, "status": app.status, "version": app.version,
        "live": _preview(live_of(app), live=True) if app.approved_at else None,
        "pending": _preview(app.pending, live=False),
        "reject_reason": app.reject_reason, "approved_at": app.approved_at.isoformat() if app.approved_at else None,
        "created_at": app.created_at.isoformat(), "updated_at": app.updated_at.isoformat(),
    }
    if org is not None:
        view["team"] = {"id": org.id, "slug": org.slug, "name": org.name}
    return view


async def team_role(db: AsyncSession, user_id: int, org_id: int) -> str | None:
    return (await db.execute(select(Membership.role).where(
        Membership.user_id == user_id, Membership.org_id == org_id))).scalars().first()


async def _owned_app(db: AsyncSession, user: User, app_id: int) -> ShowcaseApp:
    app = await db.get(ShowcaseApp, app_id)
    if app is None:
        raise ShowcaseError("not_found")
    if await team_role(db, user.id, app.org_id) not in ("owner", "admin"):
        raise ShowcaseError("forbidden", "owner or admin role in this team is required")
    return app


# ---- makers -------------------------------------------------------------------------------------

async def my_teams(user: User) -> list[dict]:
    """The teams this person may apply for (owner or admin), with how many entries each has."""
    async with session_maker() as db:
        rows = (await db.execute(
            select(Org.id, Org.slug, Org.name)
            .join(Membership, Membership.org_id == Org.id)
            .where(Membership.user_id == user.id, Membership.role.in_(("owner", "admin")), Org.demo.is_(False))
            .order_by(Org.name))).all()
        counts = dict((await db.execute(
            select(ShowcaseApp.org_id, func.count()).where(ShowcaseApp.status != "rejected")
            .group_by(ShowcaseApp.org_id))).all())
    return [{"id": i, "slug": s, "name": n, "apps": counts.get(i, 0), "limit": rules.MAX_APPS_PER_TEAM}
            for i, s, n in rows]


async def team_apps(user: User, org_id: int) -> list[dict]:
    async with session_maker() as db:
        if await team_role(db, user.id, org_id) is None:
            raise ShowcaseError("forbidden", "you are not a member of this team")
        apps = (await db.execute(select(ShowcaseApp).where(ShowcaseApp.org_id == org_id)
                                 .order_by(ShowcaseApp.id))).scalars().all()
        return [app_view(a) for a in apps]


async def apply(user: User, org_id: int, raw: dict) -> dict:
    """A new application for `org_id`. Owner or admin; at most MAX_APPS_PER_TEAM entries per team
    (rejected ones do not count)."""
    rev = _clean(raw)
    async with session_maker() as db:
        if await team_role(db, user.id, org_id) not in ("owner", "admin"):
            raise ShowcaseError("forbidden", "owner or admin role in this team is required")
        org = await db.get(Org, org_id)
        if org is None or org.demo:
            raise ShowcaseError("forbidden", "this team cannot apply")
        await ratestore.sweep(db, APPLY_NS)
        if not await ratestore.rate_check(db, APPLY_NS, [(f"u:{user.id}", APPLY_MAX_PER_USER)], 86400):
            await db.commit()
            raise ShowcaseError("rate_limited", "too many applications today - try again tomorrow")
        count = (await db.execute(select(func.count()).where(
            ShowcaseApp.org_id == org_id, ShowcaseApp.status != "rejected"))).scalar_one()
        if count >= rules.MAX_APPS_PER_TEAM:
            await db.commit()
            raise ShowcaseError("limit", f"a team can list at most {rules.MAX_APPS_PER_TEAM} apps")
        app = ShowcaseApp(org_id=org_id, status="pending", pending=rev, submitted_by=user.id)
        db.add(app)
        await db.flush()
        _event(db, app, "applied", rules.changes({}, rev), actor=user.email, source="web")
        await db.commit()
        log.info("showcase applied app=%s team=%s", app.id, org_id)
        return app_view(app)


async def edit(user: User, app_id: int, raw: dict, expected_version: int) -> dict:
    """Propose a new revision. A live app stays live as it is until staff approve the change; a
    rejected application goes back to review."""
    rev = _clean(raw)
    async with session_maker() as db:
        app = await _owned_app(db, user, app_id)
        if app.version != expected_version:
            raise ShowcaseError("conflict", "this app changed - reload it before saving")
        base = app.pending or (live_of(app) if app.approved_at else {})
        changed = rules.changes(base, rev)
        if not changed:
            raise ShowcaseError("invalid", "nothing changed")
        app.pending = rev
        if app.status == "rejected":
            app.status, app.reject_reason = "pending", ""
        _event(db, app, "edited", changed, actor=user.email, source="web")
        await db.commit()
        return app_view(app)


async def withdraw(user: User, app_id: int) -> None:
    """Withdraw a proposal: an application never approved is removed; a live app keeps its live
    version and drops the pending change."""
    async with session_maker() as db:
        app = await _owned_app(db, user, app_id)
        if app.approved_at is None:
            await db.delete(app)
        elif app.pending is not None:
            app.pending = None
            _event(db, app, "withdrawn", {}, actor=user.email, source="web")
        await db.commit()


# ---- staff --------------------------------------------------------------------------------------

async def queue(status: str | None = None) -> list[dict]:
    """Everything staff may act on, newest change first; `status=review` is what waits for them."""
    async with session_maker() as db:
        stmt = select(ShowcaseApp, Org).join(Org, Org.id == ShowcaseApp.org_id)
        if status == "review":
            stmt = stmt.where(ShowcaseApp.pending.is_not(None), ShowcaseApp.status != "rejected")
        elif status:
            stmt = stmt.where(ShowcaseApp.status == status)
        rows = (await db.execute(stmt.order_by(ShowcaseApp.updated_at.desc()))).all()
        return [app_view(a, org=o) for a, o in rows]


async def staff_get(app_id: int) -> dict:
    async with session_maker() as db:
        app = await db.get(ShowcaseApp, app_id)
        if app is None:
            raise ShowcaseError("not_found")
        org = await db.get(Org, app.org_id)
        events = (await db.execute(select(ShowcaseAppEvent).where(ShowcaseAppEvent.app_id == app_id)
                                   .order_by(ShowcaseAppEvent.version.desc()))).scalars().all()
        return {**app_view(app, org=org), "history": [
            {"version": e.version, "action": e.action, "changes": e.changes, "note": e.note,
             "actor": e.actor, "source": e.source, "created_at": e.created_at.isoformat()} for e in events]}


async def _publish_revision(rev: dict) -> None:
    for key in ([rev["icon_key"]] if rev["icon_key"] else []) + list(rev["shot_keys"]):
        try:
            await images.publish(key)
        except ImageRejected as exc:
            raise ShowcaseError("invalid", exc.detail) from None


async def _versioned(db: AsyncSession, app_id: int, expected_version: int) -> ShowcaseApp:
    app = await db.get(ShowcaseApp, app_id)
    if app is None:
        raise ShowcaseError("not_found")
    if app.version != expected_version:
        raise ShowcaseError("conflict", "this app changed - reload it before acting")
    return app


async def approve(app_id: int, expected_version: int, *, actor: str, source: str = "admin") -> dict:
    """Make the pending revision live: publish its pictures, then point the row at them."""
    async with session_maker() as db:
        app = await _versioned(db, app_id, expected_version)
        if app.pending is None:
            raise ShowcaseError("invalid", "nothing is waiting for review")
        rev = dict(app.pending)
    await _publish_revision(rev)                     # storage I/O with no session open
    async with session_maker() as db:
        app = await _versioned(db, app_id, expected_version)
        first_approval = app.approved_at is None
        before = live_of(app) if app.approved_at else {}
        old_keys = [app.icon_key, *app.shot_keys] if app.approved_at else []
        _set_live(app, rev)
        app.pending, app.reject_reason = None, ""
        app.status = "approved" if app.status in ("pending", "rejected") else app.status
        app.approved_at = app.approved_at or utcnow_naive()
        _event(db, app, "approved", rules.changes(before, rev), actor=actor, source=source)
        await db.commit()
        view = app_view(app)
    await images.retire([k for k in old_keys if k not in ([rev["icon_key"], *rev["shot_keys"]])])
    if first_approval:
        # Count its activity now (four weeks back) instead of at the next weekly run, so an app with
        # real usage lands on the activity boards as soon as it is approved. Best effort: the
        # weekly run catches up if this fails.
        try:
            await refresh_weeks(app_ids=[app_id])
        except Exception:
            log.exception("showcase weeks on approval failed app=%s", app_id)
    return view


async def reject(app_id: int, expected_version: int, reason: str, *, actor: str, source: str = "admin") -> dict:
    """Refuse the pending revision with a reason the maker sees. A live app stays live."""
    reason = " ".join((reason or "").split())
    if not 1 <= len(reason) <= 500:
        raise ShowcaseError("invalid", "give a reason (1-500 characters)")
    async with session_maker() as db:
        app = await _versioned(db, app_id, expected_version)
        if app.pending is None:
            raise ShowcaseError("invalid", "nothing is waiting for review")
        app.reject_reason = reason
        if app.approved_at is None:
            app.status = "rejected"            # the maker may edit and resubmit
        else:
            app.pending = None                 # the live version stays
        _event(db, app, "rejected", {}, actor=actor, source=source, note=reason)
        await db.commit()
        return app_view(app)


async def set_hidden(app_id: int, expected_version: int, hidden: bool, *, actor: str, source: str = "admin") -> dict:
    async with session_maker() as db:
        app = await _versioned(db, app_id, expected_version)
        if app.approved_at is None:
            raise ShowcaseError("invalid", "only an approved app can be hidden or shown")
        target = "hidden" if hidden else "approved"
        if app.status == target:
            raise ShowcaseError("invalid", f"already {target}")
        before, app.status = app.status, target
        _event(db, app, "hidden" if hidden else "unhidden", {"status": [before, target]},
               actor=actor, source=source)
        await db.commit()
        return app_view(app)


async def staff_edit(app_id: int, expected_version: int, raw: dict, *, actor: str, source: str = "admin") -> dict:
    """Staff correct an entry. A pending revision is edited in place (it still needs approval); a
    live app without one is changed directly, its pictures published first."""
    rev = _clean(raw)
    async with session_maker() as db:
        app = await _versioned(db, app_id, expected_version)
        direct = app.pending is None and app.approved_at is not None
    if direct:
        await _publish_revision(rev)
    async with session_maker() as db:
        app = await _versioned(db, app_id, expected_version)
        old_keys: list = []
        if direct:
            before, old_keys = live_of(app), [app.icon_key, *app.shot_keys]
            _set_live(app, rev)
        else:
            before = app.pending or {}
            app.pending = rev
        changed = rules.changes(before, rev)
        if not changed:
            raise ShowcaseError("invalid", "nothing changed")
        _event(db, app, "edited", changed, actor=actor, source=source)
        await db.commit()
        view = app_view(app)
    if old_keys:
        await images.retire([k for k in old_keys if k not in ([rev["icon_key"], *rev["shot_keys"]])])
    return view


async def staff_create(org_id: int, raw: dict, *, approve_now: bool, actor: str, source: str = "admin") -> dict:
    """Staff list an app for a team, pending or approved at once. The team limit still applies."""
    rev = _clean(raw)
    async with session_maker() as db:
        if await db.get(Org, org_id) is None:
            raise ShowcaseError("not_found", "no such team")
        count = (await db.execute(select(func.count()).where(
            ShowcaseApp.org_id == org_id, ShowcaseApp.status != "rejected"))).scalar_one()
        if count >= rules.MAX_APPS_PER_TEAM:
            raise ShowcaseError("limit", f"a team can list at most {rules.MAX_APPS_PER_TEAM} apps")
        app = ShowcaseApp(org_id=org_id, status="pending", pending=rev)
        db.add(app)
        await db.flush()
        _event(db, app, "applied", rules.changes({}, rev), actor=actor, source=source)
        await db.commit()
        app_id, version = app.id, app.version
    if approve_now:
        return await approve(app_id, version, actor=actor, source=source)
    return await staff_get(app_id)


# ---- weekly activity ----------------------------------------------------------------------------

def _categories_by_endpoint() -> dict[str, str]:
    return {e["id"]: (e.get("domain") or "other") for e in catalog_store.load().endpoints}


async def _week_row(db: AsyncSession, org_id: int, start: date, domains: dict[str, str]) -> dict:
    """One team's calls in the UTC week starting `start`, grouped small: by day, provider and tool.
    Refused calls never reached a provider, so they are not activity."""
    begin = datetime(start.year, start.month, start.day)
    rows = (await db.execute(
        select(func.date(CallRecord.created_at), CallRecord.provider, CallRecord.endpoint_id, func.count())
        .where(CallRecord.org_id == org_id, CallRecord.created_at >= begin,
               CallRecord.created_at < begin + timedelta(days=7), CallRecord.refused_by.is_(None))
        .group_by(func.date(CallRecord.created_at), CallRecord.provider, CallRecord.endpoint_id))).all()
    calls, active, providers, mix = 0, 0, set(), {}
    for day, provider, endpoint_id, n in rows:
        day = day if isinstance(day, date) else date.fromisoformat(str(day)[:10])
        calls += n
        active |= 1 << (day - start).days
        if provider:
            providers.add(provider)
        if endpoint_id:
            k = domains.get(endpoint_id, "other")
            mix[k] = mix.get(k, 0) + n
    return {"calls": calls, "active_days": active, "providers": sorted(providers), "categories": mix}


async def refresh_weeks(session_factory: Callable = session_maker, *, today: date | None = None,
                        app_ids: list[int] | None = None) -> dict:
    """Store every missing week (up to the window) for every approved app - or only `app_ids` - and
    recount the newest. Idempotent: re-running a week rewrites its row with the same numbers."""
    today = today or utcnow_naive().date()
    newest = rules.last_complete_week(today)
    wanted = [newest - timedelta(days=7 * i) for i in range(rules.WINDOW_WEEKS)]
    domains = _categories_by_endpoint()
    written = 0
    async with session_factory() as db:
        stmt = select(ShowcaseApp).where(ShowcaseApp.status == "approved")
        if app_ids is not None:
            stmt = stmt.where(ShowcaseApp.id.in_(app_ids))
        targets = [(a.id, a.org_id) for a in (await db.execute(stmt)).scalars().all()]
    for app_id, org_id in targets:
        async with session_factory() as db:
            have = set((await db.execute(
                select(ShowcaseWeek.week_start).where(ShowcaseWeek.app_id == app_id))).scalars().all())
            for start in wanted:
                key = start.isoformat()
                if key in have and start != newest:
                    continue
                row = await _week_row(db, org_id, start, domains)
                ins = _insert(db)(ShowcaseWeek).values(app_id=app_id, week_start=key, computed_at=utcnow_naive(), **row)
                await db.execute(ins.on_conflict_do_update(
                    index_elements=[ShowcaseWeek.app_id, ShowcaseWeek.week_start],
                    set_={**row, "computed_at": ins.excluded.computed_at}))
                written += 1
            await db.commit()
    log.info("showcase weeks: %d apps, %d rows written, newest week %s", len(targets), written, newest)
    return {"apps": len(targets), "rows_written": written, "newest_week": newest.isoformat()}


# ---- the public board and votes -----------------------------------------------------------------

async def _upvotes(db: AsyncSession, app_id: int | None = None) -> dict[int, int]:
    query = select(ShowcaseVote.app_id, func.count()).group_by(ShowcaseVote.app_id)
    if app_id is not None:
        query = query.where(ShowcaseVote.app_id == app_id)
    return {app: int(n) for app, n in (await db.execute(query)).all()}


def _public_app(app: ShowcaseApp) -> dict:
    return {
        "id": app.id, "name": app.name, "url": app.url, "tagline": app.tagline,
        "description": app.description, "category": app.category,
        "category_label": rules.CATEGORIES.get(app.category, "Other"),
        "maker": {"name": app.maker_name, "x_handle": app.x_handle} if (app.maker_name or app.x_handle) else None,
        "launch_post_url": app.launch_post_url or None,
        "icon_url": images.public_url(app.icon_key),
        "shots": [_shot_urls(k) for k in app.shot_keys],
    }


async def leaderboard(db: AsyncSession) -> dict:
    """The public leaderboard: approved apps only, activity through `rules.public_entry`."""
    apps = (await db.execute(select(ShowcaseApp).where(ShowcaseApp.status == "approved")
                             .order_by(ShowcaseApp.id))).scalars().all()
    ids = [a.id for a in apps]
    weeks = (await db.execute(select(ShowcaseWeek).where(ShowcaseWeek.app_id.in_(ids)))).scalars().all() if ids else []
    upvotes = await _upvotes(db)
    stored = [date.fromisoformat(w.week_start) for w in weeks]
    ref = max(stored) if stored else rules.last_complete_week(utcnow_naive().date())
    by_app: dict[int, list[rules.Week]] = {}
    for w in weeks:
        by_app.setdefault(w.app_id, []).append(rules.Week(
            week_start=date.fromisoformat(w.week_start), calls=w.calls, active_days=w.active_days,
            providers=frozenset(w.providers or []), categories=dict(w.categories or {})))
    folded: dict[int, tuple[rules.Activity, rules.Consent, int]] = {}
    entries = []
    for a in apps:
        consent = rules.Consent(calls=a.show_calls, streak=a.show_streak,
                                providers=a.show_providers, categories=a.show_categories)
        folded[a.id] = (rules.activity(by_app.get(a.id, []), ref), consent, upvotes.get(a.id, 0))
        entries.append({**_public_app(a), **rules.public_entry(*folded[a.id])})
    return {"week": ref.isoformat(), "apps": entries, "boards": rules.boards(folded)}


def may_vote(user: User) -> bool:
    """A real person with a proven email: not an agent or demo identity, not suspended."""
    return bool(user.email_verified_at) and not user.demo and not user.suspended \
        and not is_machine_email(user.email)


async def my_upvotes(db: AsyncSession, user: User) -> list[int]:
    return list((await db.execute(
        select(ShowcaseVote.app_id).where(ShowcaseVote.user_id == user.id)
        .order_by(ShowcaseVote.app_id))).scalars().all())


async def vote(db: AsyncSession, user: User, app_id: int, up: bool, client_ip: str) -> dict:
    """Upvote an approved app, or take this person's upvote back. Commits."""
    if not may_vote(user):
        raise ShowcaseError("not_allowed")
    app = await db.get(ShowcaseApp, app_id)
    if app is None or app.status != "approved":
        raise ShowcaseError("not_found")
    await ratestore.sweep(db, VOTE_NS)
    if not await ratestore.rate_check(
            db, VOTE_NS, [(f"u:{user.id}", VOTE_MAX_PER_USER), (f"i:{client_ip}", VOTE_MAX_PER_IP)],
            VOTE_WINDOW_S):
        await db.commit()
        raise ShowcaseError("rate_limited")
    if up:
        ins = _insert(db)(ShowcaseVote).values(app_id=app_id, user_id=user.id, created_at=utcnow_naive())
        await db.execute(ins.on_conflict_do_nothing(index_elements=[ShowcaseVote.app_id, ShowcaseVote.user_id]))
    else:
        await db.execute(delete(ShowcaseVote).where(ShowcaseVote.app_id == app_id, ShowcaseVote.user_id == user.id))
    await db.commit()
    return {"id": app_id, "up": up, "upvotes": (await _upvotes(db, app_id)).get(app_id, 0)}
