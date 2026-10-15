"""The team MCP `call` tool must not lose what the caller sent.

An argument the tool does not declare (`data`, the CLI's flag name) was silently dropped by the
argument model, so a POST went out with an empty body and the provider answered "POST Data Is Empty".
"""

from __future__ import annotations

async def _own_echo_tool(clients) -> None:
    sid = (await clients.post("/secrets", json={"name": "echo-k", "value": "SEK"})).json()["id"]
    r = await clients.post("/tools", json={"name": "edge", "base_url": "https://api.edge.example",
                                           "secret_id": sid})
    assert r.status_code == 200, r.text


async def test_unknown_call_argument_is_refused_by_name(clients):
    from test_mcp_directory import _call_tool, paired_mcp_session

    await _own_echo_tool(clients)
    async with paired_mcp_session() as session:
        out = await _call_tool(session, "call",
                               {"endpoint_id": "edge/echo", "data": {"keywords": ["coffee"]}},
                               clients.headers["X-Treg-Token"], path="/mcp/")
    assert "data" in out["error"], out
    assert "body" in out["hint"] and "params" in out["hint"], out
    assert "status" not in out  # nothing was relayed


async def test_known_arguments_still_pass(clients):
    from test_mcp_directory import _call_tool, paired_mcp_session

    await _own_echo_tool(clients)
    async with paired_mcp_session() as session:
        out = await _call_tool(session, "call",
                               {"endpoint_id": "edge/echo", "body": {"keywords": ["coffee"]}},
                               clients.headers["X-Treg-Token"], path="/mcp/")
    assert out["status"] == 200, out


async def test_directory_call_tools_refuse_unknown_arguments_too(clients):
    from test_mcp_directory import _call_tool, paired_mcp_session

    async with paired_mcp_session() as session:
        out = await _call_tool(session, "catalog_call_write",
                               {"endpoint_id": "dataforseo.google.keywords.volume",
                                "data": [{"keywords": ["coffee"]}]},
                               clients.headers["X-Treg-Token"], path="/mcp/v2/")
    assert "data" in out.get("error", ""), out
