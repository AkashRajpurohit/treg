"""Pictures: only real PNG/JPEG/WebP of a sensible size get in, every one is re-drawn from its pixels
(so nothing hidden in the original survives), staged privately, published on demand, and served
inert. Avatars: a person sets their own, owners and admins their team's."""
from __future__ import annotations

from io import BytesIO

import pytest
from PIL import Image
from sqlmodel import select

from fake_media_store import MemoryMediaStore
from treg.application import images
from treg.domain import images as rules
from treg.domain.identity import session
from treg.infra.db import session_maker
from treg.infra.imaging import render
from treg.models import Membership, Org, User


def picture(w: int, h: int, fmt: str = "PNG", color=(200, 30, 30, 255), **save) -> bytes:
    out = BytesIO()
    img = Image.new("RGBA", (w, h), color)
    (img if fmt == "PNG" else img.convert("RGB")).save(out, fmt, **save)
    return out.getvalue()


@pytest.fixture
def store():
    st = MemoryMediaStore()
    images.use_store(st)
    yield st
    images.use_store(None)


def test_only_real_png_jpeg_webp_get_in():
    kind = rules.KINDS["showcase_icon"]
    assert rules.sniff(picture(10, 10)) == "png"
    assert rules.sniff(picture(10, 10, "JPEG")) == "jpeg"
    assert rules.sniff(picture(10, 10, "WEBP")) == "webp"
    for bad in (b"<svg xmlns='http://www.w3.org/2000/svg'></svg>", b"GIF89a....", b"", b"%PDF-1.7"):
        with pytest.raises(rules.ImageRejected):
            rules.check_upload(kind, bad)
    with pytest.raises(rules.ImageRejected) as too_small:
        render(kind, picture(64, 64))
    assert too_small.value.reason == "too_small"
    with pytest.raises(rules.ImageRejected) as corrupt:
        render(kind, picture(300, 300)[:200])
    assert corrupt.value.reason == "unreadable"


def test_a_redraw_drops_what_was_hidden_and_has_a_stable_name():
    kind = rules.KINDS["showcase_icon"]
    exif = Image.Exif()
    exif[0x010E] = "secret description"
    original = picture(500, 300, "JPEG", exif=exif.tobytes()) + b"<script>alert(1)</script>"
    out = render(kind, original)
    (key, body, content_type), = out.files
    assert key == f"showcase_icon/{out.sha256}.png" and content_type == "image/png"
    assert b"secret description" not in body and b"<script>" not in body
    with Image.open(BytesIO(body)) as redrawn:
        assert redrawn.size == (256, 256) and redrawn.mode == "RGBA"
        assert redrawn.getpixel((0, 0))[3] == 0          # fitted onto a transparent square, not stretched
    assert render(kind, original).sha256 == out.sha256    # same picture, same name


def test_screenshots_get_two_sizes_cropped_from_the_top():
    out = render(rules.KINDS["showcase_shot"], picture(1600, 1200, "PNG"))
    keys = [k for k, _, _ in out.files]
    assert keys == [f"showcase_shot/{out.sha256}.webp", f"showcase_shot/{out.sha256}-640.webp"]
    sizes = [Image.open(BytesIO(b)).size for _, b, _ in out.files]
    assert sizes == [(1280, 800), (640, 400)]


async def _tim() -> tuple[User, int]:
    async with session_maker() as db:
        user = (await db.execute(select(User).where(User.email == "tim@superdesign.dev"))).scalar_one()
        org_id = (await db.execute(select(Membership.org_id).where(Membership.user_id == user.id))).scalars().first()
        return user, org_id


def _cookie(client, user: User) -> None:
    client.headers.pop("X-Treg-Token", None)
    client.cookies.set(session.COOKIE, session.make_session(user.id, token_version=user.token_version))


async def test_upload_stages_privately_and_an_avatar_publishes(clients, store):
    user, org_id = await _tim()
    _cookie(clients, user)
    up = await clients.post("/images/user_avatar", content=picture(300, 300), headers={"content-type": "image/png"})
    assert up.status_code == 200, up.text
    key = up.json()["key"]
    assert store.keys("staging") == [key] and store.keys("public") == []
    preview = await clients.get(up.json()["preview_url"])
    assert preview.status_code == 200 and preview.headers["cache-control"] == "private, no-store"
    assert preview.headers["x-content-type-options"] == "nosniff"
    assert preview.headers["content-security-policy"] == "sandbox"

    set_ = await clients.put("/me/avatar", json={"key": key})
    assert set_.status_code == 200 and set_.json()["avatar_url"] == f"/media-files/{key}"
    assert store.keys("public") == [key]
    me = (await clients.get("/auth/me")).json()
    assert me["avatar_url"] == f"/media-files/{key}" and me["uploads"] is True
    served = await clients.get(f"/media-files/{key}")
    assert served.status_code == 200 and "immutable" in served.headers["cache-control"]

    # A new picture retires the old one from the public bucket.
    other = (await clients.post("/images/user_avatar", content=picture(300, 300, color=(0, 90, 0, 255)),
                                headers={"content-type": "image/png"})).json()["key"]
    await clients.put("/me/avatar", json={"key": other})
    assert store.keys("public") == [other]
    assert (await clients.put("/me/avatar", json={"key": None})).json() == {"avatar_url": None}
    assert store.keys("public") == []


async def test_avatar_keys_must_be_of_their_kind_and_teams_need_an_admin(clients, store):
    user, org_id = await _tim()
    _cookie(clients, user)
    icon = (await clients.post("/images/team_avatar", content=picture(300, 300),
                               headers={"content-type": "image/png"})).json()["key"]
    assert (await clients.put("/me/avatar", json={"key": icon})).status_code == 400   # a team picture is not a user's
    ok = await clients.put(f"/orgs/{org_id}/avatar", json={"key": icon})
    assert ok.status_code == 200
    teams = (await clients.get("/orgs")).json()
    assert any(t["org_id"] == org_id and t["avatar_url"] == f"/media-files/{icon}" for t in teams)
    async with session_maker() as db:
        db.add(Org(name="Elsewhere", slug="elsewhere"))
        await db.commit()
        other_id = (await db.execute(select(Org.id).where(Org.slug == "elsewhere"))).scalar_one()
    assert (await clients.put(f"/orgs/{other_id}/avatar", json={"key": icon})).status_code == 403


async def test_uploads_are_refused_without_storage_or_verification(clients):
    user, _ = await _tim()
    _cookie(clients, user)
    off = await clients.post("/images/user_avatar", content=picture(300, 300), headers={"content-type": "image/png"})
    assert off.status_code == 503
    assert (await clients.get("/auth/me")).json()["uploads"] is False
    async with session_maker() as db:
        u = await db.get(User, user.id)
        u.email_verified_at = None
        await db.commit()
    assert (await clients.post("/images/user_avatar", content=b"x")).status_code == 403


async def test_an_oversized_upload_is_cut_off(clients, store):
    user, _ = await _tim()
    _cookie(clients, user)
    big = b"\x89PNG\r\n\x1a\n" + b"0" * (rules.KINDS["user_avatar"].max_bytes + 1)
    assert (await clients.post("/images/user_avatar", content=big)).status_code == 413
    assert store.keys("staging") == []
