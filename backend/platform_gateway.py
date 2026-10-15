"""Opt-in, server-to-server static HTML generation. No client keys or tools.

The legacy pipeline remains separate. A configured gateway bearer also protects
its generation socket, so activating this adapter does not leave that bypass.
"""
import asyncio
import contextlib
import hmac
import json
import math
import os
import re
from typing import Any, Awaitable, Callable, Mapping

SYSTEM = (
    "Create a complete, polished static HTML document for the user's design brief. "
    "Return only the full HTML document, with inline CSS and optional inline JS. "
    "Do not use remote assets, CDNs, external scripts, fonts, packages, server code, "
    "credentials or tools. The document must work as one standalone index.html."
)
CONTRACT = "imb-static-design-v1"
MAX_INPUT_BYTES = 4000
MAX_REQUEST_BYTES = 8000


def bearer_matches(headers: list, env: Mapping[str, str]) -> bool:
    token = env.get("IMB_PLATFORM_GATEWAY_TOKEN", "")
    values = [v for k, v in headers if k.lower() == b"authorization"]
    return bool(
        len(token) >= 32
        and len(values) == 1
        and hmac.compare_digest(values[0], ("Bearer " + token).encode())
    )


def legacy_authorized(headers: list, env: Mapping[str, str] | None = None) -> bool:
    env = os.environ if env is None else env
    return not env.get("IMB_PLATFORM_GATEWAY_TOKEN") or bearer_matches(headers, env)


def integer(env: Mapping[str, str], key: str, minimum: int, maximum: int) -> int:
    value = env.get(key, "")
    if not value.isdecimal() or not minimum <= int(value) <= maximum:
        raise ValueError("Gateway configuration is incomplete.")
    return int(value)


def policy(env: Mapping[str, str]) -> dict[str, Any]:
    if (
        env.get("IMB_PLATFORM_ENABLED") != "1"
        or len(env.get("IMB_PLATFORM_GATEWAY_TOKEN", "")) < 32
        or env.get("IMB_PLATFORM_PRICING_VERIFIED") != "1"
        or not env.get("ANTHROPIC_API_KEY")
        or not re.fullmatch(r"claude-[a-z0-9.-]{3,90}", env.get("IMB_PLATFORM_MODEL", ""))
    ):
        raise ValueError("Gateway configuration is incomplete.")
    output = integer(env, "IMB_PLATFORM_MAX_OUTPUT_TOKENS", 256, 8192)
    input_price = integer(env, "IMB_PLATFORM_INPUT_MICRO_USD_PER_MILLION_TOKENS", 1, 1000000000)
    output_price = integer(env, "IMB_PLATFORM_OUTPUT_MICRO_USD_PER_MILLION_TOKENS", 1, 1000000000)
    # One request, no cache-write option or retry. A byte-count input allowance
    # plus 4096 tokens of protocol headroom intentionally over-reserves input.
    bound = math.ceil(((MAX_INPUT_BYTES + len(SYSTEM.encode()) + 4096) * input_price + output * output_price) / 1000000)
    reserve = integer(env, "IMB_PLATFORM_RESERVE_MICRO_USD", 1, 100000000)
    if reserve < bound:
        raise ValueError("Gateway reservation is too small.")
    return {
        "contract": CONTRACT,
        "profile": "static-html",
        "variants": 1,
        "max_calls": 1,
        "max_input_bytes": MAX_INPUT_BYTES,
        "max_output_tokens": output,
        "cost_bound_micro_usd": bound,
        "reservation_micro_usd": reserve,
    }


def parse_request(raw: str) -> str:
    if not isinstance(raw, str) or len(raw.encode()) > MAX_REQUEST_BYTES:
        raise ValueError("Invalid design request.")
    params = json.loads(raw)
    if not isinstance(params, dict) or set(params) != {"contract", "prompt"} or params["contract"] != CONTRACT:
        raise ValueError("Invalid design request.")
    prompt = params["prompt"]
    if not isinstance(prompt, str) or not prompt.strip() or len(prompt.encode()) > MAX_INPUT_BYTES:
        raise ValueError("Invalid design brief.")
    return prompt.strip()


async def generate_html(prompt: str, env: Mapping[str, str], limits: dict) -> str:
    # Lazy import lets the authorization, policy and ASGI contract be tested
    # without installing the generation backend or making provider requests.
    from anthropic import AsyncAnthropic

    async with AsyncAnthropic(api_key=env["ANTHROPIC_API_KEY"], max_retries=0, timeout=180.0) as client:
        result = await client.messages.create(
            model=env["IMB_PLATFORM_MODEL"],
            max_tokens=limits["max_output_tokens"],
            system=SYSTEM,
            messages=[{"role": "user", "content": prompt}],
        )
    if result.stop_reason != "end_turn":
        raise ValueError("Generation did not complete.")
    html = "\n".join(block.text for block in result.content if block.type == "text").strip()
    if html.startswith("```"):
        html = re.sub(r"^```(?:html)?\s*\n?", "", html, count=1)
        html = re.sub(r"\n?```$", "", html).strip()
    return html


class PlatformGateway:
    def __init__(self, env=None, generator: Callable[..., Awaitable[str]] = generate_html):
        self.env = env
        self.generator = generator

    async def __call__(self, scope, receive, send):
        env = os.environ if self.env is None else self.env
        socket = scope["type"] == "websocket"
        if scope["type"] not in ("http", "websocket"):
            return

        async def deny(status: int):
            if socket:
                await send({"type": "websocket.close", "code": 1008})
            else:
                await send({"type": "http.response.start", "status": status, "headers": [(b"content-type", b"application/json"), (b"cache-control", b"no-store")]})
                await send({"type": "http.response.body", "body": b'{"error":"Design gateway unavailable."}'})

        if scope.get("query_string") or not bearer_matches(scope.get("headers", []), env):
            await deny(403)
            return
        try:
            limits = policy(env)
        except ValueError:
            await deny(503)
            return
        # Starlette versions differ in mounted scope.path normalization.
        path = scope["path"]
        if path.startswith("/platform/"):
            path = path[len("/platform"):]
        if not socket:
            if scope.get("method") != "GET" or path != "/policy":
                await deny(404)
                return
            await send({"type": "http.response.start", "status": 200, "headers": [(b"content-type", b"application/json"), (b"cache-control", b"no-store")]})
            await send({"type": "http.response.body", "body": json.dumps(limits).encode()})
            return
        if path != "/generate-code":
            await deny(404)
            return
        first = await receive()
        if first["type"] != "websocket.connect":
            return
        await send({"type": "websocket.accept"})
        generation = disconnected = None
        try:
            request = await asyncio.wait_for(receive(), 15)
            if request["type"] != "websocket.receive":
                return
            prompt = parse_request(request.get("text"))
            generation = asyncio.create_task(self.generator(prompt, env, limits))

            async def wait_disconnect():
                # Only one input frame is accepted. Further input closes the run.
                await receive()

            disconnected = asyncio.create_task(wait_disconnect())
            done, _ = await asyncio.wait({generation, disconnected}, timeout=210, return_when=asyncio.FIRST_COMPLETED)
            if generation not in done or disconnected in done:
                return
            html = generation.result()
            if not isinstance(html, str) or len(html.encode()) > 512 * 1024 or not re.search(r"<html[\s>]", html, re.I) or not re.search(r"</html\s*>", html, re.I):
                raise ValueError("Incomplete HTML.")
            await send({"type": "websocket.send", "text": json.dumps({"type": "setCode", "value": html, "variantIndex": 0})})
        except (Exception, asyncio.TimeoutError):
            # Provider errors may contain request metadata. Never echo them.
            await send({"type": "websocket.send", "text": '{"type":"error","value":"Design generation did not complete."}'})
        finally:
            for task in (generation, disconnected):
                if task is not None:
                    task.cancel()
                    with contextlib.suppress(asyncio.CancelledError, Exception):
                        await task
            with contextlib.suppress(Exception):
                await send({"type": "websocket.close", "code": 1000})


gateway = PlatformGateway()
