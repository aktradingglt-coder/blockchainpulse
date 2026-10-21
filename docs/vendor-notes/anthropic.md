# Anthropic — models, pricing, SDKs

Checked: 2026-10-21. Sources: bundled Claude API reference (model table cached 2026-10-06), code.claude.com/docs/en/agent-sdk/python (fetched 2026-10-21).

## Models and list prices (USD per million tokens, input / output)

| Model | ID | Input | Output | Notes |
| --- | --- | --- | --- | --- |
| Opus 5.5 | `claude-opus-5-5` | 4.00 | 20.00 | cache read 0.20; thinking cannot be disabled; effort default `medium`; forced `tool_choice` any/tool → 400 |
| Sonnet 5.5 | `claude-sonnet-5-5` | 2.00 | 10.00 | cache read 0.20; forced tool_choice → 400; `thinking: {type: "disabled"}` → 400 |
| Haiku 5.5 | `claude-haiku-5-5` | 0.10 | 0.50 | ≤100K-token prompts; 0.50 / 2.50 above 100K |

Matches PROMPT §7.1. Prices are stored only in `config/models.yaml`.

## Features relied on

- **Prompt caching**: prefix match tools → system → messages; max 4 breakpoints; verify with `usage.cache_read_input_tokens`.
- **Message Batches API**: 50% price, async; results keyed by `custom_id`. Used for non-time-critical scheduled work.
- **Structured outputs**: `output_config: {format: ...}` / `client.messages.parse()`; tools with `strict: true`.
- **No assistant prefill** on current models; **forced tool use is rejected** on Opus 5.5 / Sonnet 5.5 → use `tool_choice: auto` + `strict: true` + prompt instruction, or structured outputs.
- **Refusal stop reason**: always check `stop_reason` before reading content; server-side `fallbacks: "default"` with beta `server-side-fallback-2026-07-01` is available on Opus 5.5 / Sonnet 5.5.

## Claude Agent SDK for Python (`claude-agent-sdk`)

- It is the Claude Code harness packaged as a library and **spawns the Claude Code CLI as a subprocess** (`cli_path`, `CLINotFoundError`).
- Custom tools: `@tool(name, description, schema)` + `create_sdk_mcp_server(...)`, registered via `ClaudeAgentOptions.mcp_servers`.
- `allowed_tools` **auto-approves; it does not restrict**. Restriction needs `disallowed_tools` (bare names remove tools) and/or `PreToolUse` hooks.
- Subagents via `AgentDefinition`; **nested subagents are supported** in current versions (PROMPT §7.2's "[verify SDK constraint]" — the constraint does *not* hold by default; we must enforce "only the Director dispatches" ourselves).
- Highest version cited in docs: 0.2.158.

Decision D-007 (DECISIONS.md) records why the production agent runtime uses the Anthropic Python SDK (Messages API + our own tool loop) behind `LLMClient` instead of the Agent SDK.
