from __future__ import annotations

import asyncio
import copy
import unittest

from openagent_cli.remote_api import (
    HistoryQuery,
    RemoteAPIClient,
    RemoteAPIError,
    RemoteProtocolError,
    SearchQuery,
)


class _Response:
    def __init__(self, status: int, payload):
        self.status = status
        self._payload = payload

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def json(self, *, content_type=None):
        if isinstance(self._payload, BaseException):
            raise self._payload
        return copy.deepcopy(self._payload)


class _Session:
    def __init__(self, *responses: _Response):
        self.responses = list(responses)
        self.requests: list[dict] = []

    def request(self, method: str, url: str, **kwargs):
        self.requests.append({"method": method, "url": url, **copy.deepcopy(kwargs)})
        if not self.responses:
            raise AssertionError("unexpected request")
        return self.responses.pop(0)


class _BlockingResponse(_Response):
    def __init__(self):
        super().__init__(200, None)
        self.started = asyncio.Event()
        self.exited = False

    async def json(self, *, content_type=None):
        self.started.set()
        await asyncio.Event().wait()

    async def __aexit__(self, exc_type, exc, tb):
        self.exited = True
        return False


def _client(session: _Session, account: str = "alice") -> RemoteAPIClient:
    return RemoteAPIClient(
        session=session,  # type: ignore[arg-type]
        base_url="http://127.0.0.1:8765",
        cache_scope=("http://127.0.0.1:8765", "network-1", account),
    )


class RemoteAPIClientTests(unittest.IsolatedAsyncioTestCase):
    async def test_capabilities_are_cached_only_inside_the_bound_client(self):
        first = _Session(_Response(200, {"api_revision": 2, "features": {}, "storage": {}}))
        api = _client(first, "alice")

        one = await api.capabilities()
        one["api_revision"] = 999
        two = await api.capabilities()

        self.assertEqual(two["api_revision"], 2)
        self.assertEqual(len(first.requests), 1)
        self.assertEqual(api.cache_scope[-1], "alice")

        second = _Session(_Response(200, {"api_revision": 3, "features": {}, "storage": {}}))
        bob = _client(second, "bob")
        self.assertEqual((await bob.capabilities())["api_revision"], 3)
        self.assertEqual(len(second.requests), 1)

    async def test_transient_capabilities_refresh_to_ready_on_same_client(self):
        warming = {
            "api_revision": 2,
            "features": {},
            "storage": {"schema_version": 2, "search_state": "warming"},
        }
        ready = RequestValidationTests._capabilities()
        ready["storage"] = {"schema_version": 2, "search_state": "ready"}
        session = _Session(_Response(200, warming), _Response(200, ready))
        api = _client(session)

        first = await api.capabilities()
        with self.assertRaises(RemoteAPIError) as caught:
            api.require_global_search_v1(first)
        self.assertEqual(caught.exception.code, "warming")

        second = await api.capabilities()
        api.require_global_search_v1(second)
        self.assertEqual(len(session.requests), 2)

    async def test_transient_embedded_capabilities_do_not_hide_ready_rest_state(self):
        warming = {
            "api_revision": 2,
            "features": {},
            "storage": {"schema_version": 2, "search_state": "unavailable"},
        }
        ready = RequestValidationTests._capabilities()
        ready["storage"] = {"schema_version": 2, "search_state": "ready"}
        session = _Session(_Response(200, ready))
        api = RemoteAPIClient(
            session=session,  # type: ignore[arg-type]
            base_url="http://127.0.0.1:8765",
            cache_scope=("http://127.0.0.1:8765", "network-1", "alice"),
            embedded_capabilities=warming,
        )

        capabilities = await api.capabilities()
        api.require_global_search_v1(capabilities)
        self.assertEqual(len(session.requests), 1)

    async def test_missing_capability_endpoint_is_explicit_absence(self):
        session = _Session(_Response(404, {"error": {
            "code": "target_not_found", "message": "missing", "retryable": False,
        }}))
        self.assertIsNone(await _client(session).capabilities())

    async def test_auth_failure_is_not_treated_as_capability_absence(self):
        session = _Session(_Response(401, {"error": {
            "code": "unauthorized", "message": "private input", "retryable": False,
        }}))
        with self.assertRaises(RemoteAPIError) as caught:
            await _client(session).capabilities()
        self.assertEqual(caught.exception.code, "unauthorized")
        self.assertNotIn("private input", str(caught.exception))

    async def test_error_code_request_id_and_details_cannot_echo_query(self):
        session = _Session(_Response(503, {"error": {
            "code": "database locked",
            "request_id": "request-database locked",
            "retryable": True,
            "details": {"reason": "database locked", "retry_after_ms": 250},
        }}))
        with self.assertRaises(RemoteAPIError) as caught:
            await _client(session).search_page(SearchQuery(
                query="database locked", scopes=("chats",),
            ))
        error = caught.exception
        self.assertEqual(error.code, "degraded")
        self.assertIsNone(error.request_id)
        self.assertEqual(error.details, {"retry_after_ms": 250})
        self.assertNotIn("database locked", str(error))

    async def test_search_drops_even_normalized_or_opaque_request_ids(self):
        session = _Session(_Response(429, {"error": {
            "code": "rate_limited",
            "request_id": "database-locked-7f8a",
            "retryable": True,
        }}))
        with self.assertRaises(RemoteAPIError) as caught:
            await _client(session).search_page(SearchQuery(
                query="database locked", scopes=("chats",),
            ))
        self.assertIsNone(caught.exception.request_id)

    async def test_history_all_follows_opaque_cursors_without_hidden_cap(self):
        first = {
            "items": [{"id": "one", "kind": "chat"}],
            "has_more": True,
            "next_cursor": "opaque/secret-1",
            "snapshot": {"snapshot_id": "snapshot-1"},
            "revision": "r1",
        }
        second = {
            "items": [{"id": "two", "kind": "workflow_run"}],
            "has_more": False,
            "next_cursor": None,
            "snapshot": {"snapshot_id": "snapshot-1"},
            "revision": "r1",
        }
        session = _Session(_Response(200, first), _Response(200, second))
        api = _client(session)
        result = await api.collect_history(
            HistoryQuery(kinds=("chat", "workflow_run"), statuses=("failed",), limit=1),
            all_pages=True,
        )

        self.assertEqual([item["id"] for item in result["items"]], ["one", "two"])
        self.assertFalse(result["has_more"])
        self.assertIsNone(result["next_cursor"])
        self.assertEqual(session.requests[0]["params"]["kinds"], "chat,workflow_run")
        self.assertEqual(session.requests[0]["params"]["status"], "failed")
        self.assertEqual(session.requests[1]["params"]["cursor"], "opaque/secret-1")

    async def test_repeated_cursor_fails_instead_of_looping(self):
        page = {"items": [], "has_more": True, "next_cursor": "same"}
        session = _Session(_Response(200, page), _Response(200, page))
        with self.assertRaises(RemoteProtocolError):
            await _client(session).collect_history(HistoryQuery(limit=1), all_pages=True)
        self.assertEqual(len(session.requests), 2)

    async def test_search_query_is_post_body_and_typed_target_is_unchanged(self):
        target = {
            "kind": "chat_tool",
            "session_id": "session-1",
            "message_id": "message-2",
            "tool_invocation_id": "tool-3",
        }
        page = {
            "items": [{"result_id": "result-1", "target": target}],
            "has_more": False,
            "next_cursor": None,
        }
        session = _Session(_Response(200, page))
        api = _client(session)
        result = await api.collect_search(SearchQuery(
            query="database locked",
            scopes=("chats", "tools"),
            statuses=("failed",),
            limit=40,
        ), all_pages=False)

        request = session.requests[0]
        self.assertEqual(request["method"], "POST")
        self.assertEqual(request["url"], "http://127.0.0.1:8765/api/search")
        self.assertNotIn("database", request["url"])
        self.assertEqual(request["json"]["query"], "database locked")
        self.assertEqual(result["items"][0]["target"], target)

    async def test_unknown_additive_response_values_are_preserved(self):
        page = {
            "items": [{"id": "future", "kind": "future_activity"}],
            "has_more": False,
            "next_cursor": None,
        }
        session = _Session(_Response(200, page))
        result = await _client(session).collect_history(HistoryQuery(), all_pages=False)
        self.assertEqual(result["items"][0]["kind"], "future_activity")

    async def test_cancellation_releases_the_aiohttp_response_context(self):
        response = _BlockingResponse()
        session = _Session(response)
        task = asyncio.create_task(_client(session).search_page(SearchQuery(
            query="private query", scopes=("chats",),
        )))
        await response.started.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertTrue(response.exited)


class RequestValidationTests(unittest.TestCase):
    @staticmethod
    def _capabilities():
        return {
            "features": {
                "history": {
                    "version": 2,
                    "kinds": sorted({
                        "chat", "delegated_session", "workflow_run",
                        "scheduled_run", "event_delivery",
                    }),
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
        }

    def test_exact_history_and_search_capability_contracts(self):
        capabilities = self._capabilities()
        RemoteAPIClient.require_history_v2(capabilities)
        RemoteAPIClient.require_global_search_v1(capabilities)

    def test_partial_advertised_search_contract_fails_as_protocol_error(self):
        capabilities = self._capabilities()
        capabilities["features"]["global_search"]["targets"].pop()
        with self.assertRaisesRegex(RemoteProtocolError, "incompatible.*targets"):
            RemoteAPIClient.require_global_search_v1(capabilities)

    def test_history_ready_false_takes_precedence_over_search_state(self):
        capabilities = {
            "features": {},
            "storage": {"history_ready": False, "search_state": "ready"},
        }
        with self.assertRaises(RemoteAPIError) as caught:
            RemoteAPIClient.require_history_v2(capabilities)
        self.assertEqual(caught.exception.code, "warming")
        self.assertTrue(caught.exception.retryable)

    def test_search_requires_explicit_known_scope(self):
        with self.assertRaisesRegex(ValueError, "scope"):
            SearchQuery(query="x", scopes=())
        with self.assertRaisesRegex(ValueError, "scope"):
            SearchQuery(query="x", scopes=("vault",))

    def test_parent_and_root_filters_are_paired(self):
        with self.assertRaisesRegex(ValueError, "parent_type"):
            HistoryQuery(parent_type="session")
        with self.assertRaisesRegex(ValueError, "root_kind"):
            SearchQuery(query="x", scopes=("chats",), root_kind="chat")
