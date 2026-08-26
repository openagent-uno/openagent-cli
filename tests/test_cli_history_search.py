from __future__ import annotations

import json
import unittest
from unittest.mock import patch

from click.testing import CliRunner

from openagent_cli import __version__
from openagent_cli import main
from openagent_cli.remote_api import RemoteAPIClient, RemoteAPIError


CAPABILITIES = {
    "api_revision": 2,
    "features": {
        "history": {
            "version": 2,
            "kinds": [
                "chat", "delegated_session", "workflow_run",
                "scheduled_run", "event_delivery",
            ],
            "snapshot_pagination": True,
        },
        "global_search": {
            "version": 1,
            "scopes": ["chats", "tools", "workflows", "scheduled", "events"],
            "targets": [
                "chat", "chat_message", "chat_tool", "workflow_definition",
                "workflow_run", "scheduled_definition", "scheduled_run",
                "event_definition", "event_delivery",
            ],
            "snapshot_pagination": True,
        },
    },
    "storage": {"phase": "shadow", "search_state": "ready"},
}


class _API:
    def __init__(self, *, history=None, search=None, capabilities=CAPABILITIES):
        self.history = history
        self.search = search
        self.capability_payload = capabilities
        self.history_call = None
        self.search_call = None

    async def capabilities(self):
        return self.capability_payload

    @staticmethod
    def supports(capabilities, feature, minimum):
        value = (capabilities or {}).get("features", {}).get(feature, {})
        return isinstance(value.get("version"), int) and value["version"] >= minimum

    require_history_v2 = staticmethod(RemoteAPIClient.require_history_v2)
    require_global_search_v1 = staticmethod(RemoteAPIClient.require_global_search_v1)

    async def collect_history(self, query, *, all_pages):
        self.history_call = (query, all_pages)
        if isinstance(self.history, BaseException):
            raise self.history
        return self.history

    async def collect_search(self, query, *, all_pages):
        self.search_call = (query, all_pages)
        if isinstance(self.search, BaseException):
            raise self.search
        return self.search


class _Client:
    agent_name = "Friday"
    agent_version = "0.20.0-beta.1"
    agent_handle = "friday"
    network_id = "bluehost-network"
    target_handle = "friday"
    principal_handle = "codex-beta"

    def __init__(self, api):
        self.operational_api = api
        self.disconnected = False

    async def disconnect(self):
        self.disconnected = True


def _opener(client):
    async def open_gateway(network, password, agent, *, principal_handle=None, quiet):
        client.opened_principal = principal_handle
        return client, object()
    return open_gateway


class CLIHistorySearchTests(unittest.TestCase):
    def setUp(self):
        self.runner = CliRunner()

    def test_version_is_exact_package_version(self):
        result = self.runner.invoke(main.cli, ["--version"])
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertEqual(result.output, f"openagent-cli, version {__version__}\n")

    def test_history_json_and_all_are_forwarded(self):
        page = {
            "items": [{
                "id": "activity-1", "kind": "workflow_run", "resource_id": "run-1",
                "title": "Nightly", "occurred_at": "2026-08-26T02:00:00Z",
            }],
            "has_more": False,
            "next_cursor": None,
        }
        api = _API(history=page)
        client = _Client(api)
        with patch.object(main, "_open_gateway_for_rest", new=_opener(client)):
            result = self.runner.invoke(main.cli, [
                "history", "--type", "chat,workflow_run", "--status", "failed",
                "--limit", "25", "--cursor", "opaque", "--all",
                "--handle", "codex-beta", "--json",
            ])

        self.assertEqual(result.exit_code, 0, result.output)
        self.assertEqual(json.loads(result.output), page)
        query, all_pages = api.history_call
        self.assertEqual(query.kinds, ("chat", "workflow_run"))
        self.assertEqual(query.statuses, ("failed",))
        self.assertEqual(query.cursor, "opaque")
        self.assertEqual(query.limit, 25)
        self.assertTrue(all_pages)
        self.assertEqual(client.opened_principal, "codex-beta")
        self.assertTrue(client.disconnected)

    def test_search_json_preserves_canonical_target(self):
        target = {
            "kind": "workflow_run", "workflow_id": "workflow-1",
            "run_id": "run-2", "tool_invocation_id": "tool-3",
        }
        page = {
            "items": [{"result_id": "r", "target": target}],
            "has_more": False,
            "next_cursor": None,
        }
        api = _API(search=page)
        with patch.object(main, "_open_gateway_for_rest", new=_opener(_Client(api))):
            result = self.runner.invoke(main.cli, [
                "search", "database locked", "--scope", "tools,workflows", "--json",
            ])

        self.assertEqual(result.exit_code, 0, result.output)
        self.assertEqual(json.loads(result.output)["items"][0]["target"], target)
        query, _all_pages = api.search_call
        self.assertEqual(query.query, "database locked")
        self.assertEqual(query.scopes, ("tools", "workflows"))

    def test_no_match_has_documented_exit_one_and_valid_json(self):
        page = {"items": [], "has_more": False, "next_cursor": None}
        with patch.object(main, "_open_gateway_for_rest", new=_opener(_Client(_API(search=page)))):
            result = self.runner.invoke(main.cli, ["search", "nothing", "--json"])
        self.assertEqual(result.exit_code, 1, result.output)
        self.assertEqual(json.loads(result.output), page)

    def test_unsupported_is_not_relabelled_as_no_match(self):
        with patch.object(
            main, "_open_gateway_for_rest",
            new=_opener(_Client(_API(history={}, capabilities=None))),
        ):
            result = self.runner.invoke(main.cli, ["history", "--json"])
        self.assertEqual(result.exit_code, 4, result.output)
        self.assertEqual(json.loads(result.output)["error"]["code"], "unsupported")

    def test_bootstrap_feature_absence_is_warming_not_unsupported(self):
        capabilities = {
            "api_revision": 2,
            "features": {},
            "storage": {"phase": "shadow", "search_state": "warming"},
        }
        with patch.object(
            main, "_open_gateway_for_rest",
            new=_opener(_Client(_API(search={}, capabilities=capabilities))),
        ):
            result = self.runner.invoke(main.cli, ["search", "needle", "--json"])
        self.assertEqual(result.exit_code, 5, result.output)
        self.assertEqual(json.loads(result.output)["error"], {
            "code": "warming", "retryable": True,
        })

    def test_fresh_v2_unavailable_search_is_retryable_warming(self):
        capabilities = json.loads(json.dumps(CAPABILITIES))
        capabilities["features"].pop("global_search")
        capabilities["storage"].update({
            "schema_version": 2,
            "search_state": "unavailable",
            "search_ready": False,
        })
        with patch.object(
            main, "_open_gateway_for_rest",
            new=_opener(_Client(_API(search={}, capabilities=capabilities))),
        ):
            result = self.runner.invoke(main.cli, ["search", "needle", "--json"])
        self.assertEqual(result.exit_code, 5, result.output)
        self.assertEqual(json.loads(result.output)["error"], {
            "code": "warming", "retryable": True,
        })

    def test_503_does_not_fall_back_and_does_not_echo_server_message(self):
        error = RemoteAPIError(
            status=503, code="warming", retryable=True,
            request_id="request-1", details={"message": "database locked"},
        )
        api = _API(search=error)
        with patch.object(main, "_open_gateway_for_rest", new=_opener(_Client(api))):
            result = self.runner.invoke(main.cli, [
                "search", "database locked", "--json",
            ])
        self.assertEqual(result.exit_code, 5, result.output)
        self.assertNotIn("database locked", result.output)
        self.assertEqual(json.loads(result.output)["error"]["request_id"], "request-1")

    def test_invalid_relative_time_fails_before_connect(self):
        result = self.runner.invoke(main.cli, ["history", "--since", "yesterday"])
        self.assertEqual(result.exit_code, 2)
        self.assertIn("--since", result.output)

    def test_server_info_exposes_capabilities_without_database_access(self):
        api = _API()
        with patch.object(main, "_open_gateway_for_rest", new=_opener(_Client(api))):
            result = self.runner.invoke(main.cli, ["server-info", "--json"])
        payload = json.loads(result.output)
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertEqual(payload["capabilities"]["api_revision"], 2)
        self.assertEqual(payload["server"]["target_agent"], "friday")
        self.assertEqual(payload["server"]["principal_handle"], "codex-beta")

    def test_cli_has_no_local_self_updater_or_implicit_beta_channel(self):
        self.assertNotIn("self-update", main.cli.commands)
        self.assertNotIn("update", main.cli.commands)
        with main.console.capture() as capture:
            main._print_help()
        help_text = capture.get()
        self.assertIn("remote server", help_text)
        self.assertNotIn("Update this CLI", help_text)


class DurationTests(unittest.TestCase):
    def test_since_uses_utc(self):
        from datetime import datetime, timezone
        now = datetime(2026, 8, 26, 12, 0, tzinfo=timezone.utc)
        self.assertEqual(main._since_to_utc("7d", now=now), "2026-08-19T12:00:00Z")
