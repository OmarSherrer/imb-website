"""Standard-library ASGI tests. No provider requests, tokens or installed SDK."""
import asyncio
import json
import sys
import types
import unittest
from unittest.mock import patch, AsyncMock
from typing import Any, Mapping

from platform_gateway import Generator, Message, Scope

from platform_gateway import CONTRACT, PlatformGateway, generate_html, legacy_authorized, parse_request, policy

ENV = {
    "IMB_PLATFORM_ENABLED": "1",
    "IMB_PLATFORM_GATEWAY_TOKEN": "fixture-only-" + "x" * 32,
    "IMB_PLATFORM_PRICING_VERIFIED": "1",
    "ANTHROPIC_API_KEY": "fixture-only-provider",
    "IMB_PLATFORM_MODEL": "claude-fixture-model",
    "IMB_PLATFORM_MAX_OUTPUT_TOKENS": "4096",
    "IMB_PLATFORM_INPUT_MICRO_USD_PER_MILLION_TOKENS": "3000000",
    "IMB_PLATFORM_OUTPUT_MICRO_USD_PER_MILLION_TOKENS": "15000000",
    "IMB_PLATFORM_RESERVE_MICRO_USD": "200000",
}
HTML = "<!doctype html><html><body>Fixture</body></html>"


class GatewayTests(unittest.IsolatedAsyncioTestCase):
    async def invoke(self, *, socket: bool = False, auth: bool = True, query: bytes = b"", path: str | None = None, body: str | None = None, env: Mapping[str,str] | None = None, generator: Generator | None = None, extra_headers: list[tuple[bytes,bytes]] | None = None) -> list[Message]:
        self.calls: list[str] = []

        async def generate(prompt: str, config: Mapping[str,str], limits: dict[str,Any]) -> str:
            self.calls.append(prompt)
            return HTML

        app = PlatformGateway(ENV if env is None else env, generator or generate)
        headers = [(b"authorization", ("Bearer " + ENV["IMB_PLATFORM_GATEWAY_TOKEN"]).encode())] if auth else []
        headers += extra_headers or []
        scope: Scope = {"type": "websocket" if socket else "http", "path": path or ("/platform/generate-code" if socket else "/platform/policy"), "headers": headers, "query_string": query, "method": "GET"}
        queue: asyncio.Queue[Message] = asyncio.Queue()
        if socket:
            await queue.put({"type": "websocket.connect"})
            await queue.put({"type": "websocket.receive", "text": json.dumps({"contract": CONTRACT, "prompt": "Build a demo"}) if body is None else body})
        events: list[Message] = []
        await app(scope, queue.get, lambda event: self.append(events, event))
        return events

    async def append(self, events: list[Message], event: Message) -> None:
        events.append(event)

    async def test_policy_requires_bearer_no_query_and_returns_no_secrets(self):
        cases: list[dict[str,Any]] = [{"auth": False}, {"query": b"token=x"}, {"extra_headers": [(b"authorization", b"Bearer duplicate")]}]
        for args in cases:
            events = await self.invoke(**args)
            self.assertEqual(events[0]["status"], 403)
            self.assertEqual(self.calls, [])
        events = await self.invoke()
        self.assertEqual(events[0]["status"], 200)
        result = json.loads(events[1]["body"])
        self.assertEqual(result["contract"], CONTRACT)
        self.assertEqual((result["variants"], result["max_calls"]), (1, 1))
        self.assertGreater(result["cost_bound_micro_usd"], 0)
        self.assertNotIn(ENV["ANTHROPIC_API_KEY"], repr(events))
        self.assertNotIn(ENV["IMB_PLATFORM_GATEWAY_TOKEN"], repr(events))

    async def test_disabled_unpriced_underreserved_or_invalid_limits_stay_closed(self):
        for key, value in (("IMB_PLATFORM_ENABLED", "0"), ("IMB_PLATFORM_PRICING_VERIFIED", "0"), ("IMB_PLATFORM_RESERVE_MICRO_USD", "1"), ("IMB_PLATFORM_MAX_OUTPUT_TOKENS", "9000"), ("IMB_PLATFORM_MODEL", "arbitrary"), ("ANTHROPIC_API_KEY", "")):
            env = {**ENV, key: value}
            with self.assertRaises(ValueError):
                policy(env)
            events = await self.invoke(env=env)
            self.assertEqual(events[0]["status"], 503)

    async def test_socket_denies_missing_bearer_and_unknown_route_before_provider(self):
        cases: list[dict[str,Any]] = [{"auth": False}, {"query": b"key=x"}, {"path": "/platform/other"}]
        for args in cases:
            events = await self.invoke(socket=True, **args)
            self.assertEqual(events, [{"type": "websocket.close", "code": 1008}])
            self.assertEqual(self.calls, [])

    async def test_one_complete_generation_with_original_websocket_output_contract(self):
        events = await self.invoke(socket=True)
        self.assertEqual(self.calls, ["Build a demo"])
        self.assertEqual(events[0]["type"], "websocket.accept")
        self.assertEqual(json.loads(events[1]["text"]), {"type": "setCode", "value": HTML, "variantIndex": 0})
        self.assertEqual(events[-1], {"type": "websocket.close", "code": 1000})

    async def test_untrusted_inputs_cannot_change_model_keys_tools_or_limit(self):
        invalid = ["not-json", "[]", json.dumps({"contract": CONTRACT, "prompt": "x", "openaiApiKey": "no"}), json.dumps({"contract": CONTRACT, "prompt": {"text": "x"}}), json.dumps({"contract": CONTRACT, "prompt": "é" * 2001}), json.dumps({"contract": CONTRACT, "prompt": " "}), "x" * 8001]
        for raw in invalid:
            with self.assertRaises((ValueError, TypeError)):
                parse_request(raw)
            events = await self.invoke(socket=True, body=raw)
            self.assertEqual(self.calls, [])
            self.assertEqual(json.loads(events[1]["text"])["type"], "error")

    async def test_provider_failure_and_partial_html_do_not_return_code_or_diagnostics(self):
        async def failed(*args: Any) -> str:
            raise RuntimeError("provider diagnostic fixture-secret")

        async def partial(*args: Any) -> str:
            return "<html>partial"

        for generate in (failed, partial):
            events = await self.invoke(socket=True, generator=generate)
            self.assertEqual(json.loads(events[1]["text"])["type"], "error")
            self.assertNotIn("fixture-secret", repr(events))
            self.assertNotIn("setCode", repr(events))

    async def test_disconnect_cancels_inflight_provider_without_a_retry(self):
        started = asyncio.Event()
        cancelled = asyncio.Event()

        async def generate(*args: Any) -> str:
            started.set()
            try:
                return await asyncio.Future[str]()
            finally:
                cancelled.set()

        queue: asyncio.Queue[Message] = asyncio.Queue()
        for event in ({"type": "websocket.connect"}, {"type": "websocket.receive", "text": json.dumps({"contract": CONTRACT, "prompt": "x"})}):
            await queue.put(event)
        events: list[Message] = []
        scope: Scope = {"type": "websocket", "path": "/generate-code", "headers": [(b"authorization", ("Bearer " + ENV["IMB_PLATFORM_GATEWAY_TOKEN"]).encode())]}
        task = asyncio.create_task(PlatformGateway(ENV, generate)(scope, queue.get, lambda event: self.append(events, event)))
        await started.wait()
        await queue.put({"type": "websocket.disconnect", "code": 1001})
        await asyncio.wait_for(task, 1)
        self.assertTrue(cancelled.is_set())
        self.assertFalse(any(event["type"] == "websocket.send" for event in events))

    async def test_sdk_request_has_one_call_fixed_model_tokens_no_retries_or_tools(self):
        observed: dict[str,Any] = {}

        class Client:
            def __init__(self, **kwargs: Any) -> None:
                observed["client"] = kwargs
                self.messages = self

            async def __aenter__(self) -> "Client":
                return self

            async def __aexit__(self, *args: Any) -> None:
                observed["closed"] = True

            async def create(self, **kwargs: Any) -> Any:
                observed["request"] = kwargs
                return types.SimpleNamespace(stop_reason="end_turn", content=[types.SimpleNamespace(type="text", text="```html\n" + HTML + "\n```")])

        with patch.dict(sys.modules, {"anthropic": types.SimpleNamespace(AsyncAnthropic=Client)}):
            result = await generate_html("brief", ENV, policy(ENV))
        self.assertEqual(result, HTML)
        self.assertEqual(observed["client"]["max_retries"], 0)
        self.assertEqual(observed["request"]["max_tokens"], 4096)
        self.assertEqual(observed["request"]["model"], ENV["IMB_PLATFORM_MODEL"])
        self.assertNotIn("tools", observed["request"])
        self.assertTrue(observed["closed"])

    async def test_actual_fastapi_mount_preserves_policy_and_socket_auth_contract(self):
        from fastapi.testclient import TestClient
        from starlette.websockets import WebSocketDisconnect
        from main import app
        from platform_gateway import gateway

        async def generate(prompt: str, config: Mapping[str,str], limits: dict[str,Any]) -> str:
            return HTML

        with patch.dict("os.environ", ENV), patch.object(gateway, "generator", generate), patch("preview_screenshot.probe_screenshot_preview", new=AsyncMock(return_value=None)):
            with TestClient(app) as client:
                self.assertEqual(client.get("/platform/policy").status_code, 403)
                headers={"Authorization":"Bearer " + ENV["IMB_PLATFORM_GATEWAY_TOKEN"]}
                result=client.get("/platform/policy",headers=headers)
                self.assertEqual(result.status_code, 200)
                self.assertEqual(result.json()["contract"], CONTRACT)
                with self.assertRaises(WebSocketDisconnect):
                    with client.websocket_connect("/platform/generate-code"):
                        pass
                with client.websocket_connect("/platform/generate-code",headers=headers) as socket:
                    socket.send_json({"contract":CONTRACT,"prompt":"Build a demo"})
                    self.assertEqual(socket.receive_json(),{"type":"setCode","value":HTML,"variantIndex":0})

    async def test_legacy_route_only_closes_when_bearer_is_configured(self):
        self.assertTrue(legacy_authorized([], {}))
        self.assertFalse(legacy_authorized([], ENV))
        self.assertFalse(legacy_authorized([], {"IMB_PLATFORM_GATEWAY_TOKEN": "short"}))
        self.assertTrue(legacy_authorized([(b"authorization", ("Bearer " + ENV["IMB_PLATFORM_GATEWAY_TOKEN"]).encode())], ENV))


if __name__ == "__main__":
    unittest.main()
