"""Thin remote client for OpenAgent operational history and search.

The server owns the database, authorization and search index.  This module only
speaks the authenticated gateway API and deliberately keeps capability/query
state in memory.  In particular, search text and opaque cursors are never
written to disk or included in exception messages.
"""

from __future__ import annotations

import copy
import re
from dataclasses import dataclass
from typing import Any, Mapping, Sequence

import aiohttp


ACTIVITY_KINDS = frozenset({
    "chat",
    "delegated_session",
    "workflow_run",
    "scheduled_run",
    "event_delivery",
})
SEARCH_SCOPES = frozenset({"chats", "tools", "workflows", "scheduled", "events"})
SEARCH_TARGET_KINDS = frozenset({
    "chat",
    "chat_message",
    "chat_tool",
    "workflow_definition",
    "workflow_run",
    "scheduled_definition",
    "scheduled_run",
    "event_definition",
    "event_delivery",
})
RUN_STATUSES = frozenset({
    "pending",
    "queued",
    "received",
    "running",
    "success",
    "failed",
    "cancelled",
    "rejected",
    "interrupted",
    "skipped",
    "timed_out",
})
PARENT_KINDS = frozenset({"session", "workflow", "scheduled_task", "event"})
SEARCH_SORTS = frozenset({"relevance", "recent"})
SEARCH_GROUPINGS = frozenset({"root", "match"})
SEARCH_ROOT_KINDS = frozenset({
    "chat",
    "delegated_session",
    "workflow_definition",
    "workflow_run",
    "scheduled_definition",
    "scheduled_run",
    "event_definition",
    "event_delivery",
})
ERROR_CODES = frozenset({
    "invalid_request",
    "unauthorized",
    "forbidden",
    "target_not_found",
    "cursor_stale",
    "request_too_large",
    "unprocessable_query",
    "rate_limited",
    "unsupported",
    "warming",
    "degraded",
    "internal_error",
})
_REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,128}$")


def _capabilities_are_transient(capabilities: Mapping[str, Any] | None) -> bool:
    """Return whether bootstrap/readiness state must not become sticky.

    A gateway can authenticate while canonical history or the derived search
    index is still warming.  Caching that response for the lifetime of a REPL
    connection would turn a temporary absence into permanent ``unsupported``.
    """
    if not isinstance(capabilities, Mapping):
        return False
    storage = capabilities.get("storage")
    if not isinstance(storage, Mapping):
        return False
    if storage.get("history_ready") is False:
        return True
    return storage.get("search_state") in {"warming", "degraded", "unavailable"}


class RemoteAPIError(RuntimeError):
    """Sanitized non-2xx gateway response.

    The response body is intentionally not retained.  A server error can echo
    user input; keeping it out of ``str(exc)`` prevents an operational-search
    query from leaking into CLI/process logs.
    """

    def __init__(
        self,
        *,
        status: int,
        code: str,
        retryable: bool = False,
        request_id: str | None = None,
        details: Mapping[str, Any] | None = None,
    ) -> None:
        self.status = int(status)
        self.code = str(code) if code in ERROR_CODES else "internal_error"
        self.retryable = bool(retryable)
        self.request_id = (
            request_id
            if isinstance(request_id, str) and _REQUEST_ID_RE.fullmatch(request_id)
            else None
        )
        # Retain only the numeric retry control.  Even a nominal ``reason``
        # field is free-form at this trust boundary and could echo the query.
        retry_after = details.get("retry_after_ms") if isinstance(details, Mapping) else None
        self.details = (
            {"retry_after_ms": retry_after}
            if isinstance(retry_after, int) and not isinstance(retry_after, bool) and retry_after >= 0
            else {}
        )
        super().__init__(f"gateway API returned {self.status} ({self.code})")

    @property
    def is_unsupported(self) -> bool:
        """Whether fallback to an explicitly limited legacy surface is safe."""
        return self.status in {404, 405} or (
            self.status == 501 and self.code == "unsupported"
        )


class RemoteProtocolError(RuntimeError):
    """The gateway returned a success response that violates the page contract."""


class UnsupportedCapability(RuntimeError):
    """The authenticated server does not advertise a requested capability."""

    def __init__(self, feature: str) -> None:
        self.feature = feature
        super().__init__(f"server does not advertise {feature}")


def _unique(values: Sequence[str]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(str(value).strip() for value in values if str(value).strip()))


def _validate_values(name: str, values: Sequence[str], allowed: frozenset[str]) -> tuple[str, ...]:
    normalized = _unique(values)
    invalid = sorted(set(normalized) - allowed)
    if invalid:
        raise ValueError(f"invalid {name}: {', '.join(invalid)}")
    return normalized


@dataclass(frozen=True)
class HistoryQuery:
    """Validated query for ``GET /api/history``."""

    kinds: tuple[str, ...] = ()
    statuses: tuple[str, ...] = ()
    origin: str | None = None
    parent_type: str | None = None
    parent_id: str | None = None
    from_time: str | None = None
    to_time: str | None = None
    include_children: bool = False
    limit: int = 50
    cursor: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "kinds", _validate_values("activity kind", self.kinds, ACTIVITY_KINDS))
        object.__setattr__(self, "statuses", _validate_values("status", self.statuses, RUN_STATUSES))
        if self.parent_type is not None and self.parent_type not in PARENT_KINDS:
            raise ValueError(f"invalid parent type: {self.parent_type}")
        if bool(self.parent_type) != bool(self.parent_id):
            raise ValueError("parent_type and parent_id must be supplied together")
        if not 1 <= int(self.limit) <= 100:
            raise ValueError("limit must be between 1 and 100")

    def to_params(self, *, cursor: str | None = None) -> dict[str, str]:
        params: dict[str, str] = {
            "limit": str(int(self.limit)),
            "include_children": "true" if self.include_children else "false",
        }
        optional = {
            "kinds": ",".join(self.kinds) or None,
            "status": ",".join(self.statuses) or None,
            "origin": self.origin,
            "parent_type": self.parent_type,
            "parent_id": self.parent_id,
            "from": self.from_time,
            "to": self.to_time,
            "cursor": self.cursor if cursor is None else cursor,
        }
        params.update({key: str(value) for key, value in optional.items() if value is not None})
        return params


@dataclass(frozen=True)
class SearchQuery:
    """Validated body for ``POST /api/search``."""

    query: str
    scopes: tuple[str, ...]
    statuses: tuple[str, ...] = ()
    from_time: str | None = None
    to_time: str | None = None
    parent_type: str | None = None
    parent_id: str | None = None
    origin: str | None = None
    root_kind: str | None = None
    root_id: str | None = None
    sort: str = "relevance"
    grouping: str = "root"
    limit: int = 40
    cursor: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "scopes", _validate_values("search scope", self.scopes, SEARCH_SCOPES))
        object.__setattr__(self, "statuses", _validate_values("status", self.statuses, RUN_STATUSES))
        if not self.scopes:
            raise ValueError("at least one explicit search scope is required")
        if len(self.scopes) > 5:
            raise ValueError("at most five search scopes are supported")
        if len(self.query) > 4096:
            raise ValueError("query must not exceed 4096 characters")
        if self.sort not in SEARCH_SORTS:
            raise ValueError(f"invalid search sort: {self.sort}")
        if self.sort == "relevance" and not self.query:
            raise ValueError("a non-empty query is required for relevance sorting")
        if self.grouping not in SEARCH_GROUPINGS:
            raise ValueError(f"invalid search grouping: {self.grouping}")
        if self.parent_type is not None and self.parent_type not in PARENT_KINDS:
            raise ValueError(f"invalid parent type: {self.parent_type}")
        if bool(self.parent_type) != bool(self.parent_id):
            raise ValueError("parent_type and parent_id must be supplied together")
        if self.root_kind is not None and self.root_kind not in SEARCH_ROOT_KINDS:
            raise ValueError(f"invalid root kind: {self.root_kind}")
        if bool(self.root_kind) != bool(self.root_id):
            raise ValueError("root_kind and root_id must be supplied together")
        if not 1 <= int(self.limit) <= 100:
            raise ValueError("limit must be between 1 and 100")

    def to_body(self, *, cursor: str | None = None) -> dict[str, Any]:
        filters: dict[str, Any] = {}
        optional_filters = {
            "status": list(self.statuses) or None,
            "from": self.from_time,
            "to": self.to_time,
            "parent_type": self.parent_type,
            "parent_id": self.parent_id,
            "origin": self.origin,
        }
        filters.update({key: value for key, value in optional_filters.items() if value is not None})
        if self.root_kind and self.root_id:
            filters["root"] = {"kind": self.root_kind, "id": self.root_id}
        return {
            "query": self.query,
            "scopes": list(self.scopes),
            "filters": filters,
            "sort": self.sort,
            "grouping": self.grouping,
            "limit": int(self.limit),
            "cursor": self.cursor if cursor is None else cursor,
        }


class RemoteAPIClient:
    """Authenticated operational API client bound to one server/account.

    One instance is created per live ``GatewayClient``.  Its capability cache
    cannot cross ``cache_scope`` (server URL, network id, principal handle), and
    disappears when the connection closes.
    """

    def __init__(
        self,
        *,
        session: aiohttp.ClientSession,
        base_url: str,
        cache_scope: tuple[str, str, str],
        embedded_capabilities: Mapping[str, Any] | None = None,
    ) -> None:
        self._session = session
        self.base_url = base_url.rstrip("/")
        self.cache_scope = cache_scope
        self._capabilities = (
            copy.deepcopy(dict(embedded_capabilities))
            if isinstance(embedded_capabilities, Mapping)
            else None
        )
        # An auth-handshake snapshot may be taken while the server bootstrap
        # worker is still constructing history/search.  Force the first REST
        # capability read, and subsequent reads while it remains transient.
        self._capabilities_checked = (
            embedded_capabilities is not None
            and not _capabilities_are_transient(self._capabilities)
        )

    async def _request_json(
        self,
        method: str,
        path: str,
        *,
        params: Mapping[str, str] | None = None,
        body: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        kwargs: dict[str, Any] = {}
        if params:
            kwargs["params"] = dict(params)
        if body is not None:
            kwargs["json"] = dict(body)
        async with self._session.request(method, f"{self.base_url}{path}", **kwargs) as response:
            try:
                payload = await response.json(content_type=None)
            except (aiohttp.ContentTypeError, ValueError, TypeError):
                payload = None

            if response.status >= 400:
                error = payload.get("error") if isinstance(payload, Mapping) else None
                error = error if isinstance(error, Mapping) else {}
                fallback_code = {
                    400: "invalid_request",
                    401: "unauthorized",
                    403: "forbidden",
                    404: "target_not_found",
                    409: "cursor_stale",
                    413: "request_too_large",
                    422: "unprocessable_query",
                    429: "rate_limited",
                    501: "unsupported",
                    503: "degraded",
                }.get(response.status, "internal_error")
                # A request id is server-controlled and may contain a normalized,
                # hashed, or truncated echo of the user's search text.  For search
                # requests, omit it unconditionally instead of trying to recognize
                # every possible transformation.
                request_id = (
                    None
                    if isinstance(body, Mapping) and "query" in body
                    else error.get("request_id")
                )
                raise RemoteAPIError(
                    status=response.status,
                    code=(error.get("code") if error.get("code") in ERROR_CODES else fallback_code),
                    retryable=bool(error.get("retryable", False)),
                    request_id=request_id,
                    details=error.get("details"),
                )

            if not isinstance(payload, dict):
                raise RemoteProtocolError("gateway returned a non-object JSON response")
            return payload

    async def capabilities(self, *, force: bool = False) -> dict[str, Any] | None:
        if self._capabilities_checked and not force:
            return copy.deepcopy(self._capabilities)
        try:
            capabilities = await self._request_json("GET", "/api/capabilities")
        except RemoteAPIError as exc:
            if not exc.is_unsupported:
                raise
            capabilities = None
        self._capabilities = copy.deepcopy(capabilities)
        self._capabilities_checked = not _capabilities_are_transient(capabilities)
        return copy.deepcopy(capabilities)

    @staticmethod
    def supports(capabilities: Mapping[str, Any] | None, feature: str, minimum: int) -> bool:
        features = capabilities.get("features") if isinstance(capabilities, Mapping) else None
        value = features.get(feature) if isinstance(features, Mapping) else None
        version = value.get("version") if isinstance(value, Mapping) else None
        return isinstance(version, int) and not isinstance(version, bool) and version >= minimum

    @staticmethod
    def _require_feature(
        capabilities: Mapping[str, Any] | None,
        feature_name: str,
        minimum_version: int,
    ) -> tuple[Mapping[str, Any], int]:
        features = capabilities.get("features") if isinstance(capabilities, Mapping) else None
        feature = features.get(feature_name) if isinstance(features, Mapping) else None
        if feature is None:
            storage = capabilities.get("storage") if isinstance(capabilities, Mapping) else None
            state = None
            if isinstance(storage, Mapping):
                if feature_name == "history" and storage.get("history_ready") is False:
                    # Optional additive readiness signal.  Older servers do not
                    # expose it, so search_state remains the bootstrap fallback.
                    state = "warming"
                elif feature_name != "history" or "history_ready" not in storage:
                    search_state = storage.get("search_state")
                    if search_state in {"warming", "degraded"}:
                        state = str(search_state)
                    elif search_state == "unavailable":
                        schema_version = storage.get("schema_version")
                        history_feature = (
                            features.get("history") if isinstance(features, Mapping) else None
                        )
                        history_version = (
                            history_feature.get("version")
                            if isinstance(history_feature, Mapping)
                            else None
                        )
                        canonical_v2 = (
                            isinstance(schema_version, int)
                            and not isinstance(schema_version, bool)
                            and schema_version >= 2
                        ) or (
                            isinstance(history_version, int)
                            and not isinstance(history_version, bool)
                            and history_version >= 2
                        )
                        if canonical_v2:
                            # A fresh canonical install can report unavailable
                            # until the background worker creates the derived FTS
                            # file.  Expose the stable retryable category rather
                            # than misclassifying it as unsupported.
                            state = "warming"
            if state is not None:
                raise RemoteAPIError(
                    status=503,
                    code=state,
                    retryable=True,
                )
            raise UnsupportedCapability(feature_name)
        if not isinstance(feature, Mapping):
            raise RemoteProtocolError(f"gateway advertised malformed {feature_name} capability")
        version = feature.get("version")
        if not isinstance(version, int) or isinstance(version, bool):
            raise RemoteProtocolError(f"gateway advertised malformed {feature_name} version")
        if version < minimum_version:
            raise UnsupportedCapability(feature_name)
        return feature, version

    @staticmethod
    def _require_members(
        feature: Mapping[str, Any],
        *,
        feature_name: str,
        key: str,
        expected: frozenset[str],
        exact: bool,
    ) -> None:
        values = feature.get(key)
        if (
            not isinstance(values, list)
            or any(not isinstance(value, str) for value in values)
            or len(values) != len(set(values))
        ):
            raise RemoteProtocolError(
                f"gateway advertised malformed {feature_name} {key}"
            )
        actual = set(values)
        valid = actual == expected if exact else expected.issubset(actual)
        if not valid:
            raise RemoteProtocolError(
                f"gateway advertised incompatible {feature_name} {key}"
            )

    @classmethod
    def require_history_v2(cls, capabilities: Mapping[str, Any] | None) -> None:
        """Validate the complete history-v2 contract before issuing requests."""
        feature, version = cls._require_feature(capabilities, "history", 2)
        cls._require_members(
            feature,
            feature_name="history",
            key="kinds",
            expected=ACTIVITY_KINDS,
            exact=version == 2,
        )
        if feature.get("snapshot_pagination") is not True:
            raise RemoteProtocolError("gateway history capability lacks snapshot pagination")

    @classmethod
    def require_global_search_v1(cls, capabilities: Mapping[str, Any] | None) -> None:
        """Validate the five-scope/nine-target search-v1 contract."""
        feature, version = cls._require_feature(capabilities, "global_search", 1)
        cls._require_members(
            feature,
            feature_name="global_search",
            key="scopes",
            expected=SEARCH_SCOPES,
            exact=version == 1,
        )
        cls._require_members(
            feature,
            feature_name="global_search",
            key="targets",
            expected=SEARCH_TARGET_KINDS,
            exact=version == 1,
        )
        if feature.get("snapshot_pagination") is not True:
            raise RemoteProtocolError("gateway search capability lacks snapshot pagination")

    async def history_page(self, query: HistoryQuery, *, cursor: str | None = None) -> dict[str, Any]:
        return await self._request_json(
            "GET", "/api/history", params=query.to_params(cursor=cursor)
        )

    async def search_page(self, query: SearchQuery, *, cursor: str | None = None) -> dict[str, Any]:
        return await self._request_json(
            "POST", "/api/search", body=query.to_body(cursor=cursor)
        )

    @staticmethod
    def _page_cursor(page: Mapping[str, Any]) -> tuple[list[Any], bool, str | None]:
        items = page.get("items")
        has_more = page.get("has_more")
        cursor = page.get("next_cursor")
        if not isinstance(items, list) or not isinstance(has_more, bool):
            raise RemoteProtocolError("gateway returned an invalid paginated response")
        if cursor is not None and not isinstance(cursor, str):
            raise RemoteProtocolError("gateway returned a non-string cursor")
        if has_more and not cursor:
            raise RemoteProtocolError("gateway claimed another page without a cursor")
        return items, has_more, cursor

    async def collect_history(self, query: HistoryQuery, *, all_pages: bool) -> dict[str, Any]:
        return await self._collect(self.history_page, query, all_pages=all_pages)

    async def collect_search(self, query: SearchQuery, *, all_pages: bool) -> dict[str, Any]:
        return await self._collect(self.search_page, query, all_pages=all_pages)

    async def _collect(self, fetch_page, query, *, all_pages: bool) -> dict[str, Any]:
        page = await fetch_page(query)
        items, has_more, cursor = self._page_cursor(page)
        if not all_pages or not has_more:
            return page

        combined = copy.deepcopy(page)
        combined_items = list(items)
        first_snapshot = combined.get("snapshot")
        seen = {query.cursor} if query.cursor else set()
        while has_more:
            if not cursor or cursor in seen:
                raise RemoteProtocolError("gateway returned a repeated pagination cursor")
            seen.add(cursor)
            next_page = await fetch_page(query, cursor=cursor)
            next_items, has_more, cursor = self._page_cursor(next_page)
            next_snapshot = next_page.get("snapshot")
            if (
                isinstance(first_snapshot, Mapping)
                and isinstance(next_snapshot, Mapping)
                and dict(first_snapshot) != dict(next_snapshot)
            ):
                raise RemoteProtocolError("gateway changed snapshot during pagination")
            combined_items.extend(next_items)
            # Coverage/revision can legitimately advance while a snapshot is
            # consumed; preserve the final page metadata but the first snapshot.
            combined.update(copy.deepcopy(next_page))
            if first_snapshot is not None:
                combined["snapshot"] = first_snapshot
        combined["items"] = combined_items
        combined["has_more"] = False
        combined["next_cursor"] = None
        return combined


def split_csv(values: Sequence[str]) -> tuple[str, ...]:
    """Expand repeatable Click options that may themselves be comma-separated."""
    expanded: list[str] = []
    for value in values:
        expanded.extend(part.strip() for part in str(value).split(","))
    return _unique(expanded)
