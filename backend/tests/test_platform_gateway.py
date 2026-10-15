"""Standard-library ASGI tests. No provider requests, tokens or installed SDK."""
import asyncio
import json
import sys
import types
import unittest
from unittest.mock import patch

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
    async def invoke(self, *, socket=False, auth=True, query=b"", path=None, body=None, env=None, generator=None, extra_headers=None):
        self.calls = []

        async def generate(prompt, config, limits):
            self.calls.append(prompt)
            return HTML

        app = PlatformGateway(ENV if env is None else env, generator or generate)
        headers = [(b"authorization", ("Bearer " + ENV["IMB_PLATFORM_GATEWAY_TOKEN"]).encode())] if auth else []
        headers += extra_headers or []
        scope = {"type": "websocket" if socket else "http", "path": path or ("/platform/generate-code" if socket else "/platform/policy"), "headers": headers, "query_string": query, "method": "GET"}
        queue = asyncio.Queue()
        if socket:
            await queue.put({"type": "websocket.connect"})
            await queue.put({"type": "websocket.receive", "text": json.dumps({"contract": CONTRACT, "prompt": "Build a demo"}) if body is None else body})
        events = []
        await app(scope, queue.get, lambda event: self.append(events, event))
        return events

    async def append(self, events, event):
        events.append(event)

    async def test_policy_requires_bearer_no_query_and_returns_no_secrets(self):
        for args in ({"auth": False}, {"query": b"token=x"}, {"extra_headers": [(b"authorization", b"Bearer duplicate")]}):
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
        for args in ({"auth": False}, {"query": b"key=x"}, {"path": "/platform/other"}):
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
        async def failed(*args):
            raise RuntimeError("provider diagnostic fixture-secret")

        async def partial(*args):
            return "<html>partial"

        for generate in (failed, partial):
            events = await self.invoke(socket=True, generator=generate)
            self.assertEqual(json.loads(events[1]["text"])["type"], "error")
            self.assertNotIn("fixture-secret", repr(events))
            self.assertNotIn("setCode", repr(events))

    async def test_disconnect_cancels_inflight_provider_without_a_retry(self):
        started = asyncio.Event()
        cancelled = asyncio.Event()

        async def generate(*args):
            started.set()
            try:
                await asyncio.Future()
            finally:
                cancelled.set()

        queue = asyncio.Queue()
        for event in ({"type": "websocket.connect"}, {"type": "websocket.receive", "text": json.dumps({"contract": CONTRACT, "prompt": "x"})}):
            await queue.put(event)
        events = []
        scope = {"type": "websocket", "path": "/generate-code", "headers": [(b"authorization", ("Bearer " + ENV["IMB_PLATFORM_GATEWAY_TOKEN"]).encode())]}
        task = asyncio.create_task(PlatformGateway(ENV, generate)(scope, queue.get, lambda event: self.append(events, event)))
        await started.wait()
        await queue.put({"type": "websocket.disconnect", "code": 1001})
        await asyncio.wait_for(task, 1)
        self.assertTrue(cancelled.is_set())
        self.assertFalse(any(event["type"] == "websocket.send" for event in events))

    async def test_sdk_request_has_one_call_fixed_model_tokens_no_retries_or_tools(self):
        observed = {}

        class Client:
            def __init__(self, **kwargs):
                observed["client"] = kwargs
                self.messages = self

            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                observed["closed"] = True

            async def create(self, **kwargs):
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

    async def test_legacy_route_only_closes_when_bearer_is_configured(self):
        self.assertTrue(legacy_authorized([], {}))
        self.assertFalse(legacy_authorized([], ENV))
        self.assertFalse(legacy_authorized([], {"IMB_PLATFORM_GATEWAY_TOKEN": "short"}))
        self.assertTrue(legacy_authorized([(b"authorization", ("Bearer " + ENV["IMB_PLATFORM_GATEWAY_TOKEN"]).encode())], ENV))


if __name__ == "__main__":
    unittest.main()
