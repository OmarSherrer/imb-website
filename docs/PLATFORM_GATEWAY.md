# Platform HTML test gateway

This opt-in adapter provides the platform's first complete static HTML process. It is separate from the original multi-variant agent pipeline. It makes one Anthropic Messages call with no tools, retries, images, remote assets or client-supplied provider settings. A deadline cancels the local request; it does not promise a provider billing refund.

`GET /platform/policy` and the `WS /platform/generate-code` upgrade both require the same server-to-server Authorization bearer header. Queries and duplicate Authorization headers are refused. The platform verifies contract `imb-static-design-v1`, one variant/call and its reserved amount before opening the socket. Send exactly `{"contract":"imb-static-design-v1","prompt":"Your brief"}`. The socket emits the existing `setCode` format, variant 0, then closes. Partial output and raw provider diagnostics are never returned.

## Activation

The adapter remains disabled until all settings below are installed through Railway's secret/runtime controls. This commit does not set values, change subscriptions, start generation or prove a Railway deployment.

| Setting | Requirement |
|---|---|
| `IMB_PLATFORM_ENABLED` | `1` only after read-only deployment checks and the platform's durable budget migration |
| `IMB_PLATFORM_GATEWAY_TOKEN` | New random server-only bearer of at least 32 characters; matching platform `IMB_ENGINE_BEARER` |
| `ANTHROPIC_API_KEY` | Server-only key with owner-approved provider spending controls |
| `IMB_PLATFORM_MODEL` | Exact supported Claude API model, chosen and priced by the operator |
| `IMB_PLATFORM_PRICING_VERIFIED` | `1` only after current input/output rates are checked for that model |
| `IMB_PLATFORM_INPUT_MICRO_USD_PER_MILLION_TOKENS` | Current integer microUSD per million input tokens |
| `IMB_PLATFORM_OUTPUT_MICRO_USD_PER_MILLION_TOKENS` | Current integer microUSD per million output tokens |
| `IMB_PLATFORM_MAX_OUTPUT_TOKENS` | Integer 256 to 8192, e.g. 4096 |
| `IMB_PLATFORM_RESERVE_MICRO_USD` | Approved per-call reservation, at least the computed bound and no more than 100 USD |

The computed bound includes a conservative byte-based input allowance, 4096 tokens of protocol headroom, and the configured maximum output. It assumes the configured model/rates remain correct. It is a reservation estimate, not a provider-enforced dollar cap. The platform's per-owner daily quota and amount reservation apply before each engine request; this bearer must never be given to browser clients. Provider account limits remain the external control.

Configuring the gateway token also makes the legacy `/generate-code` socket require it. The existing browser UI cannot supply a server-only bearer and will stop generating through that socket. Coordinate that deliberate activation; leaving the token unset preserves its prior behavior. Other legacy engine routes retain their existing controls and must not be treated as a generally secured public backend.

## Verification

Use the repository's Poetry environment: `cd backend && poetry run pytest` and `poetry run pyright`, as required by AGENTS.md. The focused command is `poetry run python -m unittest discover -s tests -p test_platform_gateway.py -v`.

Nine standard-library tests cover the actual ASGI HTTP/socket contract, no-provider denial paths, bounded inputs, policy and cost configuration, completed/partial/error results, disconnect cancellation, SDK arguments and legacy gating. During preparation, these nine tests and compilation passed in the available system Python. Poetry, its backend virtualenv and the full dependency/source checkout were unavailable; the required full pytest/pyright gates did not run. Keep this change a draft until those repository gates pass. The fallback tests do not replace them or prove FastAPI mounting on the deployed runtime, real provider execution or pricing. Verify the exact deployed source and protected endpoints before enabling the platform.
