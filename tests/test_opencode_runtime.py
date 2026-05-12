"""Tests for the OpenCode harness executor."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

import src.harness.opencode.executor as executor
from src.harness.sdk_config import set_sdk
from src.schemas import AgentResponse


@pytest.fixture(autouse=True)
def _reset_sdk():
    set_sdk("claude")
    yield
    set_sdk("claude")


@pytest.fixture(autouse=True)
def _reset_executor_state():
    executor._SERVER_PORTS.clear()
    executor._SERVER_PIDS.clear()
    executor._SPAWNED_THIS_RUN.clear()
    executor._SERVER_SIGNATURES.clear()
    executor._SERVER_LOG_HANDLES.clear()
    yield
    executor._SERVER_PORTS.clear()
    executor._SERVER_PIDS.clear()
    executor._SPAWNED_THIS_RUN.clear()
    executor._SERVER_SIGNATURES.clear()
    executor._SERVER_LOG_HANDLES.clear()


def _fake_httpx_response(json_data: object, status_code: int = 200):
    response = SimpleNamespace()
    response.status_code = status_code
    response._json_data = json_data
    response.headers = {
        "content-type": "application/json",
        "content-length": "0",
    }
    response.json = lambda: response._json_data
    response.text = json.dumps(json_data) if isinstance(json_data, (dict, list)) else str(json_data)
    response.content = response.text.encode("utf-8")
    response.raise_for_status = lambda: None
    return response


class _FakeStreamResponse:
    def __init__(
        self,
        body: bytes,
        *,
        status_code: int = 200,
        headers: dict[str, str] | None = None,
        chunks: list[bytes] | None = None,
    ) -> None:
        self.status_code = status_code
        self.headers = headers or {
            "content-type": "application/json",
            "transfer-encoding": "chunked",
        }
        self._chunks = chunks or [body]

    def raise_for_status(self) -> None:
        return None

    async def aiter_bytes(self):
        for chunk in self._chunks:
            yield chunk

    async def aclose(self) -> None:
        return None


class _FakeAsyncClient:
    def __init__(
        self,
        *,
        session_response,
        stream_response,
        messages_response,
        provider_response=None,
        message_post_response=None,
        message_poll_responses=None,
    ):
        self._session_response = session_response
        self._stream_response = stream_response
        self._messages_response = messages_response
        self._message_post_response = message_post_response or _fake_httpx_response({
            "info": {"role": "assistant"},
            "parts": [],
        })
        self._message_poll_responses = list(message_poll_responses or [])
        self._provider_response = provider_response or _fake_httpx_response({
            "all": [{
                "id": "openrouter",
                "models": {
                    "minimax/minimax-m2.7": {},
                    "claude-sonnet-4-6": {},
                    "llama3.2": {},
                },
            }],
        })
        self.sent_request = None

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def post(self, url: str, **kwargs):
        if url == "/session":
            return self._session_response
        if "/message" in url:
            return self._message_post_response
        raise AssertionError(f"unexpected POST {url}")

    def build_request(self, method: str, url: str, **kwargs):
        request = SimpleNamespace(method=method, url=url, kwargs=kwargs)
        self.sent_request = request
        return request

    async def send(self, request, *, stream: bool = False):
        assert stream is True
        return self._stream_response

    async def get(self, url: str, **kwargs):
        if url == "/provider":
            return self._provider_response
        if "/message" not in url:
            raise AssertionError(f"unexpected GET {url}")
        if self._message_poll_responses:
            return self._message_poll_responses.pop(0)
        return self._messages_response


def _make_server_payloads():
    session = _fake_httpx_response({"id": "ses-1"})
    chat_info = {
        "info": {
            "role": "assistant",
            "modelID": "minimax/minimax-m2.7",
            "providerID": "openrouter",
            "cost": 0.05,
            "tokens": {"input": 10, "output": 5},
            "structured": {"final_answer": "4", "reasoning": "basic arithmetic"},
            "stopReason": "end_turn",
        }
    }
    messages = _fake_httpx_response([
        {
            "info": {
                "role": "assistant",
                "cost": 0.05,
                "tokens": {"input": 10, "output": 5},
                "structured": {"final_answer": "4", "reasoning": "basic arithmetic"},
                "stopReason": "end_turn",
            },
            "parts": [{"type": "text", "text": "4"}],
        }
    ])
    return session, chat_info, messages


class TestExecuteQuery:
    def test_streams_message_and_writes_diagnostics(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ):
        set_sdk("opencode")
        monkeypatch.setenv("OPENROUTER_API_KEY", "test-openrouter-key")
        popen_calls = []
        session_resp, chat_info, msgs_resp = _make_server_payloads()
        stream_resp = _FakeStreamResponse(json.dumps(chat_info).encode("utf-8"))
        fake_client = _FakeAsyncClient(
            session_response=session_resp,
            stream_response=stream_resp,
            messages_response=msgs_resp,
            provider_response=_fake_httpx_response({
                "all": [{
                    "id": "openrouter",
                    "models": {"minimax/minimax-m2.7": {}},
                }],
            }),
        )

        monkeypatch.setattr(executor, "_find_free_port", lambda: 5555)
        monkeypatch.setattr(executor, "_kill_all_opencode_servers", lambda: None)
        monkeypatch.setattr(executor, "_push_provider_auth", lambda *args: None)
        monkeypatch.setattr(
            "subprocess.Popen",
            lambda *args, **kwargs: (popen_calls.append(kwargs), SimpleNamespace(pid=99))[1],
        )
        monkeypatch.setattr("time.sleep", lambda _: None)
        monkeypatch.setattr(executor, "_wait_for_port", lambda *args, **kwargs: None)
        monkeypatch.setattr(httpx, "AsyncClient", lambda *args, **kwargs: fake_client)

        options = {
            "system": "Answer questions.",
            "format": {"type": "json_schema", "schema": AgentResponse.model_json_schema()},
            "tools": {"read": True, "bash": True},
            "mode": "build",
            "provider_id": "openrouter",
            "model_id": "minimax/minimax-m2.7",
            "model": "openrouter/minimax/minimax-m2.7",
            "cwd": str(tmp_path),
        }

        result = asyncio.run(executor.execute_query(options, "What is 2+2?"))

        assert popen_calls
        assert popen_calls[0]["cwd"] == str(tmp_path)
        sent_body = fake_client.sent_request.kwargs["json"]
        assert sent_body["model"]["providerID"] == "openrouter"
        assert sent_body["model"]["modelID"] == "minimax/minimax-m2.7"

        fields = executor.parse_response(result, AgentResponse, lambda: options)
        assert fields["output"] is not None
        assert fields["output"].final_answer == "4"
        assert fields["total_cost_usd"] == 0.05
        assert fields["parse_error"] is None
        assert fields["diagnostics"]["provider_request_started"] is True
        assert fields["diagnostics"]["provider_http_headers_received"] is True
        assert fields["diagnostics"]["provider_first_chunk_received"] is True
        assert fields["diagnostics"]["provider_response_completed"] is True
        assert fields["diagnostics"]["message_poll_iterations"] == 1
        assert fields["diagnostics"]["assistant_output_tokens"] == 5
        assert fields["diagnostics"]["stop_reason"] == "end_turn"

        summary_path = tmp_path / ".evoskill" / "logs" / "opencode_message_diagnostics.jsonl"
        event_path = tmp_path / ".evoskill" / "logs" / "opencode_message_events.jsonl"
        assert summary_path.exists()
        assert event_path.exists()

        summary = json.loads(summary_path.read_text(encoding="utf-8").splitlines()[-1])
        assert summary["provider_request_started"] is True
        assert summary["provider_response_started"] is True
        assert summary["provider_response_completed"] is True
        assert summary["suspected_stall_stage"] == "unknown"

        events = [json.loads(line)["event"] for line in event_path.read_text(encoding="utf-8").splitlines()]
        assert "provider_http_request_start" in events
        assert "provider_http_headers_received" in events
        assert "provider_first_chunk_received" in events
        assert "provider_stream_complete" in events

    def test_records_provider_wait_exception(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ):
        set_sdk("opencode")
        monkeypatch.setenv("ANTHROPIC_API_KEY", "test-anthropic-key")

        class _FailingClient(_FakeAsyncClient):
            async def send(self, request, *, stream: bool = False):
                raise TimeoutError("provider stalled")

        session_resp, _chat_info, msgs_resp = _make_server_payloads()
        fake_client = _FailingClient(
            session_response=session_resp,
            stream_response=_FakeStreamResponse(b"{}"),
            messages_response=msgs_resp,
            provider_response=_fake_httpx_response({
                "all": [{
                    "id": "anthropic",
                    "models": {"claude-sonnet-4-6": {}},
                }],
            }),
        )

        monkeypatch.setattr(executor, "_find_free_port", lambda: 6666)
        monkeypatch.setattr(executor, "_kill_all_opencode_servers", lambda: None)
        monkeypatch.setattr(executor, "_push_provider_auth", lambda *args: None)
        monkeypatch.setattr("subprocess.Popen", lambda *args, **kwargs: SimpleNamespace(pid=100))
        monkeypatch.setattr("time.sleep", lambda _: None)
        monkeypatch.setattr(executor, "_wait_for_port", lambda *args, **kwargs: None)
        monkeypatch.setattr(httpx, "AsyncClient", lambda *args, **kwargs: fake_client)

        options = {
            "provider_id": "anthropic",
            "model_id": "claude-sonnet-4-6",
            "cwd": str(tmp_path),
        }

        with pytest.raises(TimeoutError, match="provider stalled"):
            asyncio.run(executor.execute_query(options, "hello"))

        summary_path = tmp_path / ".evoskill" / "logs" / "opencode_message_diagnostics.jsonl"
        summary = json.loads(summary_path.read_text(encoding="utf-8").splitlines()[-1])
        assert summary["provider_request_started"] is True
        assert summary["provider_http_headers_received"] is False
        assert summary["provider_exception_type"] == "TimeoutError"
        assert summary["suspected_stall_stage"] == "provider_wait"

    def test_rejects_missing_provider_model_before_message_post(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ):
        set_sdk("opencode")
        monkeypatch.setenv("OLLAMA_API_KEY", "local-test-key")

        fake_client = _FakeAsyncClient(
            session_response=_fake_httpx_response({"id": "ses-1"}),
            stream_response=_FakeStreamResponse(b"{}"),
            messages_response=_fake_httpx_response([]),
            provider_response=_fake_httpx_response({
                "all": [{
                    "id": "ollama-cloud",
                    "models": {"gpt-oss:20b": {}},
                }],
            }),
        )

        monkeypatch.setattr(executor, "_find_free_port", lambda: 7777)
        monkeypatch.setattr(executor, "_kill_all_opencode_servers", lambda: None)
        monkeypatch.setattr(executor, "_push_provider_auth", lambda *args: None)
        monkeypatch.setattr("subprocess.Popen", lambda *args, **kwargs: SimpleNamespace(pid=101))
        monkeypatch.setattr("time.sleep", lambda _: None)
        monkeypatch.setattr(executor, "_wait_for_port", lambda *args, **kwargs: None)
        monkeypatch.setattr(httpx, "AsyncClient", lambda *args, **kwargs: fake_client)

        options = {
            "provider_id": "ollama",
            "model_id": "qwen3-coder:30b",
            "cwd": str(tmp_path),
        }

        with pytest.raises(RuntimeError, match="Local Ollama is not registered"):
            asyncio.run(executor.execute_query(options, "hello"))

        summary_path = tmp_path / ".evoskill" / "logs" / "opencode_message_diagnostics.jsonl"
        summary = json.loads(summary_path.read_text(encoding="utf-8").splitlines()[-1])
        assert summary["provider_catalog_checked"] is True
        assert "ollama-cloud" in summary["provider_catalog_available_ids"]
        assert summary["provider_request_started"] is False

    def test_reuses_server_on_concurrent_calls(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ):
        set_sdk("opencode")
        monkeypatch.setenv("ANTHROPIC_API_KEY", "test-anthropic-key")
        popen_count = 0

        def fake_popen(*args, **kwargs):
            nonlocal popen_count
            popen_count += 1
            return SimpleNamespace(pid=100 + popen_count)

        monkeypatch.setattr(executor, "_find_free_port", lambda: 6666)
        monkeypatch.setattr(executor, "_kill_all_opencode_servers", lambda: None)
        monkeypatch.setattr(executor, "_push_provider_auth", lambda *args: None)
        monkeypatch.setattr("subprocess.Popen", fake_popen)
        monkeypatch.setattr("time.sleep", lambda _: None)
        monkeypatch.setattr(executor, "_wait_for_port", lambda *args, **kwargs: None)

        options = {
            "cwd": str(tmp_path),
            "provider_id": "anthropic",
            "model_id": "claude-sonnet-4-6",
        }

        url1 = executor._ensure_server(options)
        assert popen_count == 1

        url2 = executor._ensure_server(options)
        assert popen_count == 1
        assert url1 == url2


class TestShutdown:
    def test_shutdown_project_server_kills_pid(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ):
        key = str(tmp_path.resolve())
        executor._SERVER_PIDS[key] = 1234
        executor._SERVER_PORTS[key] = 7777
        executor._SPAWNED_THIS_RUN.add(key)

        kill_calls = []

        def fake_kill(pid, sig):
            kill_calls.append((pid, sig))
            if sig == 0:
                raise ProcessLookupError

        monkeypatch.setattr("os.kill", fake_kill)
        monkeypatch.setattr("time.sleep", lambda _: None)

        executor.shutdown_project_server(tmp_path)

        assert (1234, executor.signal.SIGTERM) in kill_calls
        assert key not in executor._SERVER_PIDS
        assert key not in executor._SERVER_PORTS
        assert key not in executor._SPAWNED_THIS_RUN

    def test_shutdown_all_servers(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
        key1 = str((tmp_path / "a").resolve())
        key2 = str((tmp_path / "b").resolve())
        executor._SERVER_PIDS[key1] = 111
        executor._SERVER_PIDS[key2] = 222
        executor._SERVER_PORTS[key1] = 8001
        executor._SERVER_PORTS[key2] = 8002
        executor._SPAWNED_THIS_RUN.update({key1, key2})

        killed = []

        def fake_kill(pid, sig):
            killed.append(pid)
            if sig == 0:
                raise ProcessLookupError

        monkeypatch.setattr("os.kill", fake_kill)
        monkeypatch.setattr("time.sleep", lambda _: None)

        executor.shutdown_all_servers()

        assert 111 in killed
        assert 222 in killed
        assert not executor._SERVER_PIDS
        assert not executor._SERVER_PORTS
        assert not executor._SPAWNED_THIS_RUN


class TestParseResponse:
    def test_parse_error_when_no_assistant_message(self):
        payload = {"session_id": "s1", "chat_info": {}, "messages": []}
        fields = executor.parse_response(
            [payload],
            AgentResponse,
            lambda: {"model": "test", "tools": {}},
        )
        assert fields["output"] is None
        assert fields["parse_error"] is not None

    def test_text_fallback_when_structured_is_invalid(self):
        payload = {
            "session_id": "s1",
            "chat_info": {},
            "messages": [{
                "info": {
                    "role": "assistant",
                    "structured": {"wrong": "fields"},
                    "cost": 0,
                    "tokens": {},
                },
                "parts": [{"type": "text", "text": '{"final_answer": "7", "reasoning": "fallback"}'}],
            }],
        }
        fields = executor.parse_response(
            [payload],
            AgentResponse,
            lambda: {"model": "test", "tools": {}},
        )
        assert fields["output"] is not None
        assert fields["output"].final_answer == "7"
        assert fields["parse_error"] is None

    def test_run_diagnostic_probe_reuses_provider_model_and_minimal_tools(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ):
        captured: dict[str, object] = {}

        async def fake_execute_query(options, query):
            captured["options"] = dict(options)
            captured["query"] = query
            return [{
                "session_id": "probe-1",
                "chat_info": {"info": {"role": "assistant"}},
                "messages": [{
                    "info": {
                        "role": "assistant",
                        "tokens": {"output": 2},
                        "structured": {"reply": "hello"},
                    },
                    "parts": [{"type": "text", "text": '{"reply":"hello"}'}],
                }],
                "diagnostics": {
                    "run_id": "probe-run",
                    "event_path": str(Path.cwd() / "events.jsonl"),
                    "summary_path": str(Path.cwd() / "summary.jsonl"),
                    "provider_request_started": True,
                    "provider_http_headers_received": True,
                    "provider_first_chunk_received": True,
                    "provider_response_completed": True,
                    "parse_failures": 0,
                },
            }]

        monkeypatch.setattr(executor, "execute_query", fake_execute_query)
        monkeypatch.setattr(executor, "_write_diagnostic_summary", lambda diagnostic: None)
        monkeypatch.setattr(executor, "_emit_diagnostic_event", lambda *args, **kwargs: None)

        result = asyncio.run(executor.run_diagnostic_probe(
            {
                "provider_id": "ollama",
                "model_id": "llama3.2",
                "cwd": str(Path.cwd()),
                "tools": {"bash": True},
            },
            include_minimal_tools=True,
        ))

        assert captured["query"] == "say hello"
        assert captured["options"]["provider_id"] == "ollama"
        assert captured["options"]["model_id"] == "llama3.2"
        assert captured["options"]["tools"] == {"read": True}
        assert result["output"] is not None
        assert result["output"].reply == "hello"

    def test_minimal_reply_smoke_polls_session_for_assistant_text(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ):
        set_sdk("opencode")
        monkeypatch.setenv("OLLAMA_API_KEY", "ollama")

        session_resp = _fake_httpx_response({"id": "ses-2"})
        empty_messages = _fake_httpx_response([
            {
                "info": {"role": "user"},
                "parts": [{"type": "text", "text": "this is a test give me a reply"}],
            },
            {
                "info": {"role": "assistant", "tokens": {"output": 0}},
                "parts": [],
            },
        ])
        final_messages = _fake_httpx_response([
            {
                "info": {"role": "user"},
                "parts": [{"type": "text", "text": "this is a test give me a reply"}],
            },
            {
                "info": {"role": "assistant", "tokens": {"output": 6}},
                "parts": [{"type": "text", "text": "This is a test reply."}],
            },
        ])

        fake_client = _FakeAsyncClient(
            session_response=session_resp,
            stream_response=_FakeStreamResponse(b"{}"),
            messages_response=final_messages,
            message_poll_responses=[empty_messages, final_messages],
            provider_response=_fake_httpx_response({
                "all": [{
                    "id": "ollama",
                    "models": {"qwen3-coder:30b": {}},
                }],
            }),
        )

        monkeypatch.setattr(executor, "_find_free_port", lambda: 8888)
        monkeypatch.setattr(executor, "_kill_all_opencode_servers", lambda: None)
        monkeypatch.setattr(executor, "_push_provider_auth", lambda *args: None)
        monkeypatch.setattr("subprocess.Popen", lambda *args, **kwargs: SimpleNamespace(pid=102))
        monkeypatch.setattr("time.sleep", lambda _: None)
        monkeypatch.setattr(executor, "_wait_for_port", lambda *args, **kwargs: None)
        monkeypatch.setattr(httpx, "AsyncClient", lambda *args, **kwargs: fake_client)

        result = asyncio.run(executor.run_minimal_reply_smoke(
            project_root=tmp_path,
            model="ollama/qwen3-coder:30b",
            timeout_seconds=5,
        ))

        assert result["reply"] == "This is a test reply."
        assert result["diagnostics"]["message_poll_iterations"] >= 2
        assert result["diagnostics"]["provider_request_started"] is True
