from __future__ import annotations

import asyncio
import contextlib
import json

import pytest

from headroom.cache import compression_store as compression_store_module
from headroom.cache.compression_store import (
    get_compression_store,
    reset_compression_store,
)
from tests._mcp_stub import import_module_with_mcp_stub

mcp_server = import_module_with_mcp_stub("headroom.ccr.mcp_server")


def test_shared_stats_work_without_fcntl(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(mcp_server, "_HAS_FCNTL", False)
    monkeypatch.setattr(mcp_server, "fcntl", None)
    monkeypatch.setattr(mcp_server, "SHARED_STATS_DIR", tmp_path)
    monkeypatch.setattr(mcp_server, "SHARED_STATS_FILE", tmp_path / "session_stats.jsonl")
    monkeypatch.setattr(mcp_server.os, "getpid", lambda: 4242)
    monkeypatch.setattr(mcp_server.time, "time", lambda: 1001.0)

    event = {"type": "compress", "timestamp": 1000.0}
    mcp_server._append_shared_event(event)

    raw_lines = mcp_server.SHARED_STATS_FILE.read_text(encoding="utf-8").splitlines()
    assert len(raw_lines) == 1
    assert json.loads(raw_lines[0]) == {"type": "compress", "timestamp": 1000.0, "pid": 4242}

    events = mcp_server._read_shared_events(window_seconds=60)
    assert events == [{"type": "compress", "timestamp": 1000.0, "pid": 4242}]


# --- Shared compression store wiring ---------------------------------------
# MCP's _get_local_store() must return the get_compression_store() singleton —
# the same instance the proxy and response_handler use — so content compressed
# on either side is retrievable in-process. These pin that wiring so a private
# store can't creep back.


@pytest.fixture
def fresh_store():
    reset_compression_store()
    yield
    reset_compression_store()


def test_mcp_uses_shared_singleton_store(fresh_store) -> None:
    """MCP's store is the global singleton, not a private instance."""
    server = mcp_server.HeadroomMCPServer(check_proxy=False)
    assert server._get_local_store() is get_compression_store()


def test_mcp_retrieves_proxy_stored_content(fresh_store) -> None:
    """Content stored via the singleton (as the proxy does) is retrievable
    through MCP's local-store path. The HTTP fallback is disabled so this
    passes only via the shared store."""
    original = '{"some": "original proxy-compressed content"}'
    hash_key = get_compression_store().store(original, '{"compressed": true}')

    server = mcp_server.HeadroomMCPServer(check_proxy=False)
    result = asyncio.run(server._retrieve_content(hash_key))

    assert result.get("source") == "local"
    assert result["original_content"] == original


def test_compress_savings_percent_tracks_token_counts(fresh_store) -> None:
    """``savings_percent`` must be the *removed* percentage derived from the
    token counts — never the retained percentage. Regression for the inversion
    where ``(1 - compression_ratio)`` reported a no-op (0% saved) as 100%."""
    pytest.importorskip("mcp", reason="MCP SDK required")
    server = mcp_server.HeadroomMCPServer(check_proxy=False)

    # Repetitive JSON array — the shape the engine actually compresses.
    content = json.dumps([{"id": i, "status": "ok", "kind": "run"} for i in range(40)])
    result = server._compress_content(content)

    orig = result["original_tokens"]
    comp = result["compressed_tokens"]
    expected = round((1 - comp / orig) * 100, 1) if orig > 0 else 0

    # Reported savings agrees with the token fields (and with tokens_saved).
    assert result["savings_percent"] == expected
    assert 0.0 <= result["savings_percent"] <= 100.0
    if result["tokens_saved"] == 0:
        assert result["savings_percent"] == 0.0  # not inverted to 100
    else:
        assert result["savings_percent"] > 0.0


def test_mcp_compress_surfaces_unreachable_proxy(fresh_store) -> None:
    server = mcp_server.HeadroomMCPServer(
        proxy_url="http://127.0.0.1:9",
        check_proxy=True,
    )

    response = asyncio.run(server._handle_compress({"content": "dead proxy check"}))
    payload = json.loads(response[0].kwargs["text"])

    assert payload["proxy"]["status"] == "unreachable"
    assert payload["proxy"]["url"] == "http://127.0.0.1:9"
    assert "unreachable" in payload["warning"].lower()


def test_mcp_stats_surfaces_unreachable_proxy() -> None:
    server = mcp_server.HeadroomMCPServer(
        proxy_url="http://127.0.0.1:9",
        check_proxy=True,
    )

    response = asyncio.run(server._handle_stats())
    payload = json.loads(response[0].kwargs["text"])

    assert payload["proxy"]["status"] == "unreachable"
    assert payload["proxy"]["url"] == "http://127.0.0.1:9"
    assert "unreachable" in payload["warning"].lower()


def test_mcp_proxy_probe_preserves_shared_proxy_client(monkeypatch: pytest.MonkeyPatch) -> None:
    class ProbeResponse:
        status_code = 200
        text = ""

        @staticmethod
        def json() -> dict[str, object]:
            return {"status": "healthy", "alive": True}

    class ProbeClient:
        def __init__(self, *, timeout: float) -> None:
            seen["timeout"] = timeout

        async def __aenter__(self) -> ProbeClient:
            return self

        async def __aexit__(self, *_args: object) -> None:
            seen["closed"] = True

        async def get(self, url: str) -> ProbeResponse:
            seen["url"] = url
            return ProbeResponse()

    seen: dict[str, object] = {}
    shared_client = object()
    monkeypatch.setattr(mcp_server.httpx, "AsyncClient", ProbeClient)

    server = mcp_server.HeadroomMCPServer(
        proxy_url="http://127.0.0.1:8765",
        check_proxy=True,
    )
    server._http_client = shared_client  # type: ignore[assignment]

    result = asyncio.run(server._probe_proxy_unreachable())

    assert result is None
    assert seen == {
        "timeout": 5.0,
        "url": "http://127.0.0.1:8765/livez",
        "closed": True,
    }
    assert server._http_client is shared_client


def test_mcp_local_mode_still_works_without_proxy_checking(fresh_store) -> None:
    server = mcp_server.HeadroomMCPServer(
        proxy_url="http://127.0.0.1:9",
        check_proxy=False,
    )

    response = asyncio.run(server._handle_compress({"content": "local mode stays available"}))
    payload = json.loads(response[0].kwargs["text"])

    assert "proxy" not in payload
    assert "warning" not in payload or "unreachable" not in payload["warning"].lower()


def test_mcp_retrieve_returns_full_content(fresh_store) -> None:
    """Retrieval is by hash: a stored, unexpired entry always returns its full
    original content (never empty, never a spurious "not found")."""
    original = "the the the the the the the the the the\n" * 5
    hash_key = get_compression_store().store(original, "<<small>>")

    server = mcp_server.HeadroomMCPServer(check_proxy=False)
    result = asyncio.run(server._retrieve_content(hash_key))

    assert "error" not in result
    assert result.get("source") == "local"
    assert result["original_content"] == original


def test_mcp_retrieve_expired_hash_returns_terminal_guidance(
    monkeypatch,
    fresh_store,
) -> None:
    """An expired local hash should say it expired and tell the agent to stop retrying."""
    current_time = [1000.0]

    def fake_time() -> float:
        return current_time[0]

    monkeypatch.setattr(mcp_server.time, "time", fake_time)
    monkeypatch.setattr(compression_store_module.time, "time", fake_time)

    store = get_compression_store()
    hash_key = store.store("expired content", "<<small>>", ttl=1)
    current_time[0] = 1002.0

    server = mcp_server.HeadroomMCPServer(check_proxy=False)
    result = asyncio.run(server._retrieve_content(hash_key))

    assert result["status"] == "expired"
    assert result["ttl_seconds"] == 1
    assert result["age_seconds"] == pytest.approx(2.0)
    assert "Entry expired" in result["error"]
    assert "do not retry the same hash" in result["error"].lower()
    assert "re-run the command" in result["hint"].lower()


def test_mcp_retrieve_hash_expiring_during_lookup_returns_terminal_guidance(
    monkeypatch,
    fresh_store,
) -> None:
    phase = "store"
    status_seen = False

    def fake_time() -> float:
        if phase == "store":
            return 1000.0
        return 1001.1 if status_seen else 1000.5

    monkeypatch.setattr(mcp_server.time, "time", fake_time)
    monkeypatch.setattr(compression_store_module.time, "time", fake_time)

    store = get_compression_store()
    hash_key = store.store("expired during retrieve", "<<small>>", ttl=1)
    phase = "retrieve"

    original_get_entry_status = store.get_entry_status
    original_retrieve = store.retrieve

    def get_entry_status_then_expire(*args, **kwargs):
        nonlocal status_seen
        result = original_get_entry_status(*args, **kwargs)
        status_seen = True
        return result

    monkeypatch.setattr(store, "get_entry_status", get_entry_status_then_expire)
    monkeypatch.setattr(store, "retrieve", original_retrieve)

    server = mcp_server.HeadroomMCPServer(check_proxy=False)
    result = asyncio.run(server._retrieve_content(hash_key))

    assert result["status"] == "expired"
    assert result["ttl_seconds"] == 1
    assert result["age_seconds"] == pytest.approx(1.1)
    assert "Entry expired" in result["error"]
    assert "do not retry the same hash" in result["error"].lower()


def test_mcp_retrieve_missing_local_hash_can_still_hit_proxy(
    monkeypatch,
    fresh_store,
) -> None:
    monkeypatch.setattr(mcp_server, "HTTPX_AVAILABLE", True)
    server = mcp_server.HeadroomMCPServer(check_proxy=True)

    async def retrieve_via_proxy(hash_key: str) -> dict[str, object]:
        return {"hash": hash_key, "original_content": "from proxy"}

    server._retrieve_via_proxy = retrieve_via_proxy

    result = asyncio.run(server._retrieve_content("proxy_hash"))

    assert result["source"] == "proxy"
    assert result["hash"] == "proxy_hash"
    assert result["original_content"] == "from proxy"


def test_mcp_retrieve_expired_local_hash_can_still_hit_proxy(
    monkeypatch,
    fresh_store,
) -> None:
    current_time = [1000.0]

    def fake_time() -> float:
        return current_time[0]

    monkeypatch.setattr(mcp_server, "HTTPX_AVAILABLE", True)
    monkeypatch.setattr(mcp_server.time, "time", fake_time)
    monkeypatch.setattr(compression_store_module.time, "time", fake_time)

    store = get_compression_store()
    hash_key = store.store("expired local content", "<<small>>", ttl=1)
    current_time[0] = 1002.0

    server = mcp_server.HeadroomMCPServer(check_proxy=True)

    async def retrieve_via_proxy(proxy_hash_key: str) -> dict[str, object]:
        return {"hash": proxy_hash_key, "original_content": "from proxy"}

    server._retrieve_via_proxy = retrieve_via_proxy

    result = asyncio.run(server._retrieve_content(hash_key))

    assert result["source"] == "proxy"
    assert result["hash"] == hash_key
    assert result["original_content"] == "from proxy"


def test_mcp_retrieve_missing_hash_still_errors(fresh_store) -> None:
    """A never-stored hash must stay on the generic missing path, not expired guidance."""
    server = mcp_server.HeadroomMCPServer(check_proxy=False)
    result = asyncio.run(server._retrieve_content("nonexistent_hash"))
    assert result.get("status") is None
    assert result["error"] == "Content not found. It may have expired or the hash may be incorrect."
    assert "do not retry the same hash" not in result.get("hint", "").lower()


def test_handle_stats_session_output_is_window_scoped() -> None:
    """window-scoped stats output should be explicitly labeled after this change."""

    async def fetch_stats() -> dict[str, object]:
        return {
            "summary": {
                "mode": "token",
                "api_requests": 3,
                "compression": {},
            }
        }

    server = mcp_server.HeadroomMCPServer(check_proxy=True)
    server._fetch_full_proxy_stats = fetch_stats
    response = asyncio.run(server._handle_stats())
    text = response[0].kwargs["text"]

    assert "Headroom Window-Scoped Session Summary" in text
    assert "Headroom Session Summary" not in text


def test_handle_stats_includes_lifetime_totals_from_persistent_savings() -> None:
    """Lifetime savings are appended from /stats persistent_savings.lifetime."""

    async def fetch_stats() -> dict[str, object]:
        return {
            "summary": {
                "mode": "token",
                "api_requests": 3,
                "compression": {},
            },
            "persistent_savings": {
                "lifetime": {"tokens_saved": 12345, "compression_savings_usd": 7.25}
            },
        }

    server = mcp_server.HeadroomMCPServer(check_proxy=True)
    server._fetch_full_proxy_stats = fetch_stats
    response = asyncio.run(server._handle_stats())
    text = response[0].kwargs["text"]

    assert "Lifetime Savings:" in text
    assert "Tokens saved: 12,345" in text
    assert "Compression savings: $7.25" in text


def test_handle_stats_falls_back_gracefully_without_persistent_lifetime() -> None:
    """Missing lifetime data should still return a valid session summary."""

    async def fetch_stats() -> dict[str, object]:
        return {
            "summary": {
                "mode": "token",
                "api_requests": 3,
                "compression": {},
            },
            "persistent_savings": {"lifetime": None},
        }

    server = mcp_server.HeadroomMCPServer(check_proxy=True)
    server._fetch_full_proxy_stats = fetch_stats
    response = asyncio.run(server._handle_stats())
    text = response[0].kwargs["text"]

    assert "Headroom Window-Scoped Session Summary" in text
    assert "Lifetime Savings:" not in text


def test_handle_stats_shows_zero_lifetime_totals_when_present() -> None:
    """A present lifetime payload should still render explicit zero totals."""

    async def fetch_stats() -> dict[str, object]:
        return {
            "summary": {
                "mode": "token",
                "api_requests": 3,
                "compression": {},
            },
            "persistent_savings": {"lifetime": {"tokens_saved": 0, "compression_savings_usd": 0.0}},
        }

    server = mcp_server.HeadroomMCPServer(check_proxy=True)
    server._fetch_full_proxy_stats = fetch_stats
    response = asyncio.run(server._handle_stats())
    text = response[0].kwargs["text"]

    assert "Lifetime Savings:" in text
    assert "Tokens saved: 0" in text
    assert "Compression savings: $0.00" in text


# --- Parent-death watchdog: reap orphaned `mcp serve` on client death --------
# When the launching MCP client is SIGKILLed, stdin EOF may never arrive and the
# SDK's blocking stdin reader wedges server.run() forever, orphaning this process
# under init/launchd. run_stdio() runs a watchdog that detects the reparent and
# forces shutdown. Refs headroomlabs-ai/headroom#2185 (secondary), #1761.


def test_parent_death_watchdog_fires_when_reparented(monkeypatch) -> None:
    """When ppid changes (client died), the watchdog resolves promptly."""
    server = mcp_server.HeadroomMCPServer(check_proxy=False)
    calls = {"n": 0}

    def fake_getppid() -> int:
        calls["n"] += 1
        return 500 if calls["n"] == 1 else 1  # captured live, then reparented

    monkeypatch.setattr(mcp_server.os, "getppid", fake_getppid)

    async def run() -> None:
        await asyncio.wait_for(server._await_parent_death(0.001), timeout=1.0)

    asyncio.run(run())  # returns => detected reparent; TimeoutError would fail


def test_parent_death_watchdog_stays_quiet_with_live_parent(monkeypatch) -> None:
    """A stable ppid must never trip the watchdog."""
    server = mcp_server.HeadroomMCPServer(check_proxy=False)
    monkeypatch.setattr(mcp_server.os, "getppid", lambda: 500)

    async def run() -> None:
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(server._await_parent_death(0.001), timeout=0.05)

    asyncio.run(run())


def test_run_stdio_reaps_process_on_parent_death(monkeypatch) -> None:
    """On reparent, run_stdio cleans up and calls os._exit(0) even though the
    (stubbed) server.run never returns — the orphan-reaper path."""
    server = mcp_server.HeadroomMCPServer(check_proxy=False)

    @contextlib.asynccontextmanager
    async def fake_stdio_server():
        yield (object(), object())

    monkeypatch.setattr(mcp_server, "stdio_server", fake_stdio_server)

    async def never_returns(*_args, **_kwargs) -> None:
        await asyncio.sleep(3600)  # emulate the wedged SDK reader

    # DummyServer (MCP SDK stub) has no `.run`; raising=False lets us add it.
    monkeypatch.setattr(server.server, "run", never_returns, raising=False)

    calls = {"n": 0}

    def fake_getppid() -> int:
        calls["n"] += 1
        return 500 if calls["n"] == 1 else 1

    monkeypatch.setattr(mcp_server.os, "getppid", fake_getppid)

    cleaned = {"done": False}

    async def fake_cleanup() -> None:
        cleaned["done"] = True

    monkeypatch.setattr(server, "cleanup", fake_cleanup)

    class _Exited(Exception):
        pass

    def fake_exit(code: int) -> None:
        raise _Exited(code)  # intercept so pytest survives

    monkeypatch.setattr(mcp_server.os, "_exit", fake_exit)

    with pytest.raises(_Exited) as excinfo:
        asyncio.run(server.run_stdio(parent_death_poll_interval=0.001))

    assert excinfo.value.args[0] == 0
    assert cleaned["done"] is True


# --- Proxy token authentication --------------------------------------------
# The sidecar gates /v1/* and /stats behind HEADROOM_PROXY_TOKEN. Without a
# token on the client the MCP server's retrieve and stats calls come back 401,
# and _fetch_full_proxy_stats swallowed that as "no proxy data", so a
# misconfigured token looked exactly like a proxy with nothing to report.


class _RecordingResponse:
    def __init__(self, status_code: int, payload: dict[str, object]) -> None:
        self.status_code = status_code
        self._payload = payload
        self.text = ""

    def json(self) -> dict[str, object]:
        return self._payload

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise AssertionError(f"unexpected HTTP {self.status_code}")


class _RecordingClient:
    """Stand-in for httpx.AsyncClient that records construction and calls."""

    instances: list[_RecordingClient] = []

    def __init__(self, *, timeout: float, headers: dict[str, str] | None = None) -> None:
        self.timeout = timeout
        self.headers = headers
        self.calls: list[tuple[str, str]] = []
        self.status_code = 200
        self.payload: dict[str, object] = {}
        _RecordingClient.instances.append(self)

    async def post(self, url: str, json: dict[str, str] | None = None) -> _RecordingResponse:
        self.calls.append(("POST", url))
        return _RecordingResponse(self.status_code, self.payload)

    async def get(self, url: str) -> _RecordingResponse:
        self.calls.append(("GET", url))
        return _RecordingResponse(self.status_code, self.payload)


@pytest.fixture
def recording_client(monkeypatch: pytest.MonkeyPatch):
    _RecordingClient.instances = []
    monkeypatch.setattr(mcp_server.httpx, "AsyncClient", _RecordingClient)
    return _RecordingClient


def test_proxy_token_from_env_is_sent_on_retrieve(
    monkeypatch: pytest.MonkeyPatch, recording_client
) -> None:
    monkeypatch.setenv("HEADROOM_PROXY_TOKEN", "s3cret-token")
    server = mcp_server.HeadroomMCPServer(proxy_url="http://headroom:8787", check_proxy=False)

    asyncio.run(server._retrieve_via_proxy("abc123"))

    client = recording_client.instances[-1]
    assert client.headers == {"X-Headroom-Proxy-Token": "s3cret-token"}
    assert client.calls == [("POST", "http://headroom:8787/v1/retrieve")]


def test_proxy_token_from_file_is_sent_on_stats(
    monkeypatch: pytest.MonkeyPatch, tmp_path, recording_client
) -> None:
    token_file = tmp_path / "proxy-token"
    token_file.write_text("file-token\n", encoding="utf-8")
    monkeypatch.delenv("HEADROOM_PROXY_TOKEN", raising=False)
    monkeypatch.setenv("HEADROOM_PROXY_TOKEN_FILE", str(token_file))
    server = mcp_server.HeadroomMCPServer(proxy_url="http://headroom:8787", check_proxy=False)

    asyncio.run(server._fetch_full_proxy_stats())

    client = recording_client.instances[-1]
    assert client.headers == {"X-Headroom-Proxy-Token": "file-token"}
    assert client.calls == [("GET", "http://headroom:8787/stats")]


def test_env_token_wins_over_token_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path, recording_client
) -> None:
    token_file = tmp_path / "proxy-token"
    token_file.write_text("file-token", encoding="utf-8")
    monkeypatch.setenv("HEADROOM_PROXY_TOKEN", "env-token")
    monkeypatch.setenv("HEADROOM_PROXY_TOKEN_FILE", str(token_file))
    server = mcp_server.HeadroomMCPServer(proxy_url="http://headroom:8787", check_proxy=False)

    asyncio.run(server._fetch_full_proxy_stats())

    assert recording_client.instances[-1].headers == {"X-Headroom-Proxy-Token": "env-token"}


def test_no_token_configured_sends_no_auth_header(
    monkeypatch: pytest.MonkeyPatch, recording_client
) -> None:
    monkeypatch.delenv("HEADROOM_PROXY_TOKEN", raising=False)
    monkeypatch.delenv("HEADROOM_PROXY_TOKEN_FILE", raising=False)
    server = mcp_server.HeadroomMCPServer(proxy_url="http://headroom:8787", check_proxy=False)

    asyncio.run(server._fetch_full_proxy_stats())

    assert recording_client.instances[-1].headers is None


def test_blank_token_file_is_treated_as_no_token(
    monkeypatch: pytest.MonkeyPatch, tmp_path, recording_client
) -> None:
    token_file = tmp_path / "proxy-token"
    token_file.write_text("   \n", encoding="utf-8")
    monkeypatch.delenv("HEADROOM_PROXY_TOKEN", raising=False)
    monkeypatch.setenv("HEADROOM_PROXY_TOKEN_FILE", str(token_file))
    server = mcp_server.HeadroomMCPServer(proxy_url="http://headroom:8787", check_proxy=False)

    asyncio.run(server._fetch_full_proxy_stats())

    assert recording_client.instances[-1].headers is None


@pytest.mark.parametrize(
    "content",
    [
        "qz9v-first-line\nsecond-line\n",
        "qz9v\rsplit\n",
        "qz9v\tsplit\n",
        "qz9v-caf\u00e9\n",
    ],
    ids=["two-lines", "embedded-cr", "embedded-tab", "non-ascii"],
)
def test_token_file_with_control_or_non_ascii_chars_is_refused(
    monkeypatch: pytest.MonkeyPatch, tmp_path, recording_client, caplog, content: str
) -> None:
    """A malformed token must fail closed, not travel.

    httpx refuses a header value with an embedded newline, and that exception
    can carry the offending value into a log line, which is the one route the
    token-file feature was written to close. So the read path rejects
    anything outside printable ASCII up front and logs only the path.
    """
    token_file = tmp_path / "proxy-token"
    token_file.write_text(content, encoding="utf-8")
    monkeypatch.delenv("HEADROOM_PROXY_TOKEN", raising=False)
    monkeypatch.setenv("HEADROOM_PROXY_TOKEN_FILE", str(token_file))

    with caplog.at_level("WARNING", logger=mcp_server.logger.name):
        server = mcp_server.HeadroomMCPServer(proxy_url="http://headroom:8787", check_proxy=False)
        asyncio.run(server._fetch_full_proxy_stats())

    assert server.proxy_token is None
    assert recording_client.instances[-1].headers is None
    logged = "\n".join(r.getMessage() for r in caplog.records)
    assert str(token_file) in logged
    assert "qz9v" not in logged


def test_missing_token_file_does_not_raise(
    monkeypatch: pytest.MonkeyPatch, tmp_path, recording_client
) -> None:
    monkeypatch.delenv("HEADROOM_PROXY_TOKEN", raising=False)
    monkeypatch.setenv("HEADROOM_PROXY_TOKEN_FILE", str(tmp_path / "absent"))

    server = mcp_server.HeadroomMCPServer(proxy_url="http://headroom:8787", check_proxy=False)

    assert server.proxy_token is None


def test_explicit_proxy_token_argument_wins_over_environment(
    monkeypatch: pytest.MonkeyPatch, recording_client
) -> None:
    monkeypatch.setenv("HEADROOM_PROXY_TOKEN", "env-token")
    server = mcp_server.HeadroomMCPServer(
        proxy_url="http://headroom:8787",
        check_proxy=False,
        proxy_token="explicit-token",
    )

    asyncio.run(server._fetch_full_proxy_stats())

    assert recording_client.instances[-1].headers == {"X-Headroom-Proxy-Token": "explicit-token"}


def test_rejected_token_surfaces_in_stats_instead_of_silence(
    monkeypatch: pytest.MonkeyPatch, recording_client, fresh_store
) -> None:
    """A 401 from /stats must be visible, not an absent proxy section."""
    monkeypatch.setenv("HEADROOM_PROXY_TOKEN", "wrong-token")
    server = mcp_server.HeadroomMCPServer(proxy_url="http://headroom:8787", check_proxy=True)

    async def unauthorized_stats() -> dict[str, object] | None:
        server._proxy_auth_error = mcp_server._PROXY_UNAUTHORIZED
        return None

    server._fetch_full_proxy_stats = unauthorized_stats  # type: ignore[method-assign]

    response = asyncio.run(server._handle_stats())
    payload = json.loads(response[0].kwargs["text"])

    assert payload["proxy"]["status"] == "unauthorized"
    assert payload["proxy"]["url"] == "http://headroom:8787"
    assert "token" in payload["warning"].lower()
    assert "wrong-token" not in json.dumps(payload)


def test_stats_401_records_the_auth_error(
    monkeypatch: pytest.MonkeyPatch, recording_client
) -> None:
    monkeypatch.setenv("HEADROOM_PROXY_TOKEN", "wrong-token")
    server = mcp_server.HeadroomMCPServer(proxy_url="http://headroom:8787", check_proxy=False)
    recording_client.instances.clear()

    async def run() -> dict[str, object] | None:
        result = await server._fetch_full_proxy_stats()
        return result

    # First call constructs the client; set the status it will answer with.
    server._http_client = recording_client(timeout=15.0, headers=server._auth_headers() or None)
    server._http_client.status_code = 401

    assert asyncio.run(run()) is None
    assert server._proxy_auth_error == mcp_server._PROXY_UNAUTHORIZED


def test_retrieve_records_auth_rejection_instead_of_raising(
    monkeypatch: pytest.MonkeyPatch, recording_client
) -> None:
    monkeypatch.setenv("HEADROOM_PROXY_TOKEN", "wrong-token")
    server = mcp_server.HeadroomMCPServer(proxy_url="http://headroom:8787", check_proxy=True)
    client = recording_client(timeout=15.0, headers=server._auth_headers() or None)
    client.status_code = 401
    server._http_client = client  # type: ignore[assignment]

    result = asyncio.run(server._retrieve_via_proxy("abc123"))

    assert result["error"] == mcp_server._PROXY_UNAUTHORIZED
    assert server._proxy_auth_error == mcp_server._PROXY_UNAUTHORIZED


def test_retrieve_miss_after_auth_rejection_says_so(
    monkeypatch: pytest.MonkeyPatch, recording_client, fresh_store
) -> None:
    """A rejected token must not read as 'your content expired'."""
    monkeypatch.setenv("HEADROOM_PROXY_TOKEN", "wrong-token")
    monkeypatch.setattr(mcp_server, "HTTPX_AVAILABLE", True)
    server = mcp_server.HeadroomMCPServer(proxy_url="http://headroom:8787", check_proxy=True)
    client = recording_client(timeout=15.0, headers=server._auth_headers() or None)
    client.status_code = 401
    server._http_client = client  # type: ignore[assignment]

    result = asyncio.run(server._retrieve_content("absent_hash"))

    assert result["proxy"]["status"] == mcp_server._PROXY_UNAUTHORIZED
    assert "token" in result["error"].lower()
    assert "wrong-token" not in json.dumps(result)
