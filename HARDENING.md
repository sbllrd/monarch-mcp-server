# Samwise notes: hardening, audit, and deployment (Mordor)

This file is ours, not upstream's — kept separate from `README.md` (upstream's own, vendored) specifically so `git fetch upstream` never conflicts with our notes. See "Upstream source" below for how this directory relates to the actual `robcerda/monarch-mcp-server` project.

## Upstream source

- **Origin**: [`robcerda/monarch-mcp-server`](https://github.com/robcerda/monarch-mcp-server), vendored as a git submodule (see root `.gitmodules`) — full upstream git history is present in this directory.
- **Why this project, not building from scratch**: it already had real security-review history (documented audit findings fixed in its own PR history) before we started. That's inherited value — but every fix needed independent verification, not assumed trust. Full context in `docs/PROJECT_PLAN.md` under "MCP servers / integrations — hand-rolled or audited."
- **Working approach**: hardening work happens as commits directly on top of upstream's own history in this directory (not a separate clean copy) — `git log --oneline` here shows both upstream's commits and ours. This audit found no fixes were actually needed upstream (see checklist below), so there are currently no samwise-authored commits in this submodule's history — that may change later.
- **Pulling upstream updates**: since our own commits (if any) sit on top of a specific pinned commit, a plain `git submodule update --remote` would overwrite them by resetting to upstream's branch tip. Instead: `cd services/mcp-monarch && git fetch origin && git rebase origin/main` (rebase our commits onto the new upstream tip, resolving conflicts if any), then from the repo root `git add services/mcp-monarch && git commit` to record the new pinned commit. Re-run the audit checklist below after any upstream update — a rebase can silently change behavior the checklist relies on (e.g. `MUTATING_TOOLS` gaining a new entry).
- **Current pinned commit**: see `git submodule status` from the repo root, or `git -C services/mcp-monarch log -1`.

## Audit checklist before this is trusted with real data

- [x] **Read-only gate**: verified — `read_only.py` enforces by construction (`install()` wraps `mcp.tool()` so mutating tools in `MUTATING_TOOLS` are simply never registered when active, not filtered at call time). It's opt-in and OFF by default upstream (`MONARCH_MCP_READ_ONLY` env var, unset = full read/write tool registration) — set explicitly to `1` in whatever MCP client config runs this server (see Deployment below). Root `.env.example` documents it as a reference.
- [x] **Session storage**: resolved by deployment choice — see Deployment below. Running natively (not Docker) means `secure_session.py` uses the real macOS system keyring, confirmed live (`uv run monarch-mcp-server --help` logs `🔐 Using system keyring for token storage`), not the file-based fallback Docker's Linux VM would force.
- [x] **Dependency pinning**: confirmed clean across **every** documented install path — `uv sync --locked`, `pip install -r requirements-lock.txt --require-hashes`, `requirements.txt` (a pure `-r requirements-lock.txt` include; CI fails if it ever grows its own specifiers), and the `Dockerfile`'s two `RUN uv sync --locked` lines. All four resolve to the same hash-verified `uv.lock`. No unpinned path found — the previously-assumed "gap in a secondary route" is already fixed upstream.
- [x] **Tool registration**: confirmed 58 total tools, exactly 30 read-only / 28 in `MUTATING_TOOLS`, matching upstream's own README claim exactly. Well-tested: `tests/test_read_only.py::TestGateCannotSilentlyMissANewWriteTool` statically derives "tools that actually write" from source and asserts every one is gated, specifically to catch a future un-gated mutating tool.
- [x] **Permanent exclusions**: `delete_transaction` exists and is already in `MUTATING_TOOLS` (excluded whenever read-only mode is on). `delete_account` **does not exist as a tool in the current upstream codebase at all** — nothing to exclude; the original checklist assumption was stale.
- [x] **Remote exposure — reconsidered, not just "same pattern as everywhere else"**: Cloudflare Access does **not** work here. Two independent findings converged: (1) live audit of `app.py`/`server.py` plus `tests/test_transport.py` proved Monarch's HTTP transport has **zero request-level authentication** — `TransportSecuritySettings` only checks `Host`/`Origin` for DNS-rebinding protection, which is not authentication, exactly as the maintainer's own README states: *"Basic Host/origin checks protect against DNS rebinding; they do not authenticate callers."* (2) Cloudflare Access service-token headers (`CF-Access-Client-Id`/`Secret`) **cannot be sent by Claude's MCP connector at all** (confirmed via multiple open Anthropic GitHub issues) — so Access would either not gate the connector, or break it outright, and either way adds no real auth. **Decision: deploy local-only instead — see Deployment below.**

## Deployment: local-only via `uv run --locked` (not Docker, not HTTP)

Runs with **stdio transport (the default)** — zero network listener, so there's nothing for the auth gap above to expose. No port, no Host/Origin config, no Cloudflare tunnel, no `infra/docker-compose.yml` entry (Docker only mattered for the HTTP-transport path).

```
cd services/mcp-monarch
uv sync --locked
uv run --locked login_setup.py   # one-time, interactive, run by a human — never through an agent/chat
```

Then register with the MCP client that will use it (Claude Desktop, Claude Code):

```json
{ "mcpServers": { "Monarch Money": {
    "command": "uv", "args": ["run", "--locked", "--project", "/absolute/path/to/services/mcp-monarch", "monarch-mcp-server"],
    "env": { "MONARCH_MCP_READ_ONLY": "1" }
} } }
```

**This still works through the orchestrator once it exists.** "Local-only" means no network-exposed HTTP server — the orchestrator runs on the same machine (the Mac Mini) and is trusted first-party code, so it spawns Monarch as a local stdio subprocess exactly like Claude Desktop does: Telegram message → orchestrator → local stdio call to Monarch → response back over Telegram. No network hop, nothing for Cloudflare to gate, full remote reachability preserved. This was always the intended architecture (the orchestrator is the single mediator into the whole system), not a workaround for the auth gap.

## Future: remote access via an authenticating proxy (not built)

Only relevant if a *direct* claude.ai web/mobile custom connector to Monarch is ever wanted (bypassing the orchestrator entirely — the same pattern used for AnyList's connector). Not needed for Telegram/orchestrator-mediated access, which already works via the local stdio path above.

- Would require Monarch running in HTTP/`streamable-http` mode (Docker; a compose entry would need to come back) bound to `127.0.0.1`, with a small reverse proxy in front verifying a bearer token/API key on every request before forwarding — since Monarch has no request-level auth of its own, and Cloudflare Access is confirmed incompatible with Claude's connector for actual MCP calls.
- That proxy would be new, security-critical code in its own right — not a rubber-stamp addition, would need its own audit before trusting it with financial data.
- Getting Claude to actually send the token depends on its beta `static_headers` connector feature, which has documented reliability issues (reports of silently falling back to an OAuth attempt instead of sending the configured header — would fail outright against a server with no OAuth support) and, per Anthropic's own docs phrasing ("an organization administrator enters the credential"), may be an org/Team-plan-only feature — verify availability on an individual account before investing build effort.

## Testable now, no hardware needed

Everything above can be verified and built on any dev machine. Monarch account credentials needed for live testing — use a real account, treat the session/credentials with the same care as any other secret in `.env.example`.
