"""/start: the paid-traffic landing page. It is an ad destination, so it must stay out of search,
exist only where its claims (the $1 credit, vendor plans) are true, be measurable, and bring a
visitor who signs in back to the job and agent they had picked."""

from __future__ import annotations

import re

from httpx import ASGITransport, AsyncClient

from treg import email as email_sender
from treg.api import app
from treg.config import Settings, get_settings

PATH = "/start"


async def test_start_is_served_noindex_and_measurable(clients: AsyncClient):
    r = await clients.get(PATH)
    assert r.status_code == 200
    assert r.headers.get("x-robots-tag") == "noindex"
    html = r.text
    assert '<meta name="robots" content="noindex">' in html
    assert "{BASE}" not in html and "{ENDPOINTS}" not in html
    assert 'data-page="start"' in html
    for script in ("/sitetrack.js", "/adtrack.js"):
        assert f'<script src="{script}"></script>' in html
    # An ad destination is not a search page: no canonical, not in the sitemap.
    assert 'rel="canonical"' not in html
    assert f"{PATH}<" not in (await clients.get("/sitemap.xml")).text


async def test_start_copy_keeps_the_public_promises(clients: AsyncClient):
    """The wording rules for treg.to copy, checked where a regression would ship them to ad traffic."""
    html = (await clients.get(PATH)).text
    text = re.sub(r"<[^>]+>", " ", html.split("<script>")[0])
    lowered = text.lower()
    assert "misses are free" not in lowered and "nothing for a miss" not in lowered
    assert "monid" not in html.lower()
    # The page only sells: one action, and the per-call prices it quotes come from real runs.
    assert len(re.findall(r'<button[^>]* data-cta="', html)) == 3
    assert "$0.00015" in html and "$471" in html


async def test_a_phone_visitor_is_emailed_the_link_to_open_on_a_computer(clients: AsyncClient, monkeypatch):
    """A phone signup lands on a setup screen nobody can use, so a phone gets the link by email.
    It mails strangers' addresses unauthenticated: the address is checked and every inbox capped."""
    sent: list[tuple[str, str, str]] = []

    async def fake_send(email, link, ask=""):
        sent.append((email, link, ask))
        return True

    monkeypatch.setattr(email_sender, "send_setup_link", fake_send)
    monkeypatch.setattr(Settings, "expose_dev_code", property(lambda self: False))
    r = await clients.post(f"{PATH}/send-link", json={"email": " Ana@Example.com ", "job": "leads"})
    assert r.status_code == 200 and r.json() == {"sent": True}
    email, link, ask = sent[0]
    assert email == "ana@example.com"
    assert link.endswith("/start?job=leads&utm_source=start-email&utm_medium=email")
    assert ask.startswith("Find dentists in Sydney")
    # An unknown job is dropped from the link rather than echoed into an email.
    await clients.post(f"{PATH}/send-link", json={"email": "bo@example.com", "job": "<x>"})
    assert sent[1][1].endswith("/start?utm_source=start-email&utm_medium=email") and sent[1][2] == ""

    assert (await clients.post(f"{PATH}/send-link", json={"email": "not-an-email"})).status_code == 400
    await clients.post(f"{PATH}/send-link", json={"email": "ana@example.com"})
    assert (await clients.post(f"{PATH}/send-link", json={"email": "ana@example.com"})).status_code == 429
    assert len(sent) == 3


async def test_a_failed_send_is_not_reported_as_sent(clients: AsyncClient, monkeypatch):
    async def fail(*a, **k):
        return False

    monkeypatch.setattr(email_sender, "send_setup_link", fail)
    monkeypatch.setattr(Settings, "expose_dev_code", property(lambda self: False))
    assert (await clients.post(f"{PATH}/send-link", json={"email": "cy@example.com"})).status_code == 502


async def test_start_404s_on_a_self_hosted_registry(monkeypatch):
    monkeypatch.setenv("TREG_PUBLIC_URL", "https://registry.example.internal")
    get_settings.cache_clear()
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://registry") as c:
            assert (await c.get(PATH)).status_code == 404
            assert (await c.post(f"{PATH}/send-link", json={"email": "a@example.com"})).status_code == 404
    finally:
        get_settings.cache_clear()
