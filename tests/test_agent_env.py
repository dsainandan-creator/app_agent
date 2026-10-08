"""MCP servers must get the agent's environment, not mcp's minimal default (which let
each server re-read .env and post to the real Slack webhook during a 'disabled' run)."""

import agent


def test_servers_inherit_agent_environment(monkeypatch):
    monkeypatch.setenv("SLACK_WEBHOOK_URL", "")
    monkeypatch.setenv("LAYA_MODE", "advisory")
    for script in (agent.OBS_MCP_SERVER, agent.DT_MCP_SERVER, agent.LAYA_MCP_SERVER):
        params = agent._server_params(script)
        assert params.env is not None
        assert params.env["SLACK_WEBHOOK_URL"] == ""
        assert params.env["LAYA_MODE"] == "advisory"
