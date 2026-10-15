"""What `treg call` sends and says for three failures reported from the field: `--data @file` sent
the literal text, a body mangled by shell quoting went out silently, and treg's own saturation 503
was called the provider's answer. Driven through `main()`."""
import io
import json
import sys

import httpx
import pytest

from treg import cli


def _serve(monkeypatch, response: httpx.Response) -> list[dict]:
    sent: list[dict] = []

    class FakeClient:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def request(self, method, url, params=None, content=None, headers=None):
            sent.append({"method": method, "content": content, "headers": headers})
            return response

    monkeypatch.setattr(cli, "_client", lambda cfg, **k: FakeClient())
    monkeypatch.setattr(cli, "_load_config", lambda: {"token": "t", "base_url": "http://x"})
    return sent


def test_data_at_path_sends_the_file(monkeypatch, tmp_path):
    body = tmp_path / "req.json"
    body.write_text('{"q":"café"}', encoding="utf-8")
    sent = _serve(monkeypatch, httpx.Response(200, json={}))
    cli.main(["call", "x.search", "--data", f"@{body}"])
    assert sent[0]["content"] == '{"q":"café"}'.encode()
    assert sent[0]["headers"]["content-type"] == "application/json"
    assert sent[0]["method"] == "POST"


def test_data_at_dash_reads_stdin(monkeypatch):
    sent = _serve(monkeypatch, httpx.Response(200, json={}))
    monkeypatch.setattr(sys, "stdin", io.TextIOWrapper(io.BytesIO(b'{"q":1}')))
    cli.main(["call", "x.search", "--data", "@-"])
    assert sent[0]["content"] == b'{"q":1}'


def test_data_at_missing_file_fails_before_any_request(monkeypatch, tmp_path):
    sent = _serve(monkeypatch, httpx.Response(200, json={}))
    with pytest.raises(SystemExit) as exc:
        cli.main(["call", "x.search", "--data", f"@{tmp_path / 'nope.json'}"])
    assert "no such file" in str(exc.value.code) and sent == []


def test_plain_data_is_unchanged(monkeypatch, capsys):
    sent = _serve(monkeypatch, httpx.Response(200, json={}))
    cli.main(["call", "x.say", "--data", "hello"])
    assert sent[0]["content"] == b"hello" and sent[0]["headers"] == {}
    assert "warning" not in capsys.readouterr().err


def test_json_looking_data_that_does_not_parse_warns(monkeypatch, capsys):
    # PowerShell hands '{"a":1}' over as {a:1}.
    sent = _serve(monkeypatch, httpx.Response(200, json={}))
    cli.main(["call", "x.search", "--data", "{a:1}"])
    assert sent[0]["content"] == b"{a:1}" and sent[0]["headers"] == {}
    assert "--data looks like JSON but does not parse" in capsys.readouterr().err


def _saturated() -> httpx.Response:
    return httpx.Response(
        503, json={"detail": "treg's database pool is saturated — retry in a moment", "treg_saturated": True},
        headers={"Retry-After": "2"}, request=httpx.Request("POST", "http://x/call/x.search"))


def test_saturation_503_is_treg_not_the_provider(monkeypatch, capsys):
    _serve(monkeypatch, _saturated())
    with pytest.raises(SystemExit) as exc:
        cli.main(["call", "x.search"])
    assert exc.value.code == 1
    err = capsys.readouterr().err
    assert "treg is overloaded, not the provider; retry in 2s" in err
    assert "the provider answered" not in err


def test_saturation_503_is_typed_in_the_json_envelope(monkeypatch, capsys):
    _serve(monkeypatch, _saturated())
    with pytest.raises(SystemExit):
        cli.main(["--json", "call", "x.search"])
    meta = json.loads(capsys.readouterr().out)["_treg"]
    assert meta == {"http_status": 503, "treg_saturated": True, "retry_after_seconds": 2}


def test_a_provider_503_is_still_the_providers(monkeypatch, capsys):
    _serve(monkeypatch, httpx.Response(503, json={"error": "busy"},
                                       request=httpx.Request("POST", "http://x/call/x.search")))
    with pytest.raises(SystemExit):
        cli.main(["call", "x.search"])
    assert "the provider answered; treg relayed it unchanged" in capsys.readouterr().err


def test_non_utf8_console_gets_utf8_instead_of_a_crash(monkeypatch):
    # A Windows pipe or Git Bash reports cp1252; `●` and non-Latin provider text raised before.
    raw = io.BytesIO()
    console = io.TextIOWrapper(raw, encoding="cp1252", errors="strict")
    _serve(monkeypatch, httpx.Response(200, json={"name": "株式会社 ●"}))
    monkeypatch.setattr(sys, "stdout", console)
    monkeypatch.setattr(sys, "stderr", io.TextIOWrapper(io.BytesIO(), encoding="cp1252"))
    cli.main(["--json", "call", "x.search"])
    console.flush()
    assert json.loads(raw.getvalue().decode("utf-8"))["result"] == {"name": "株式会社 ●"}
