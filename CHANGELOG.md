# Changelog — capacium-mcp

All notable changes to the Capacium MCP Server are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## capacium-mcp v0.3.1 — 2026-11-18

No change to the MCP server. Repository and release tooling only.

- The release object is written by the ops-engine on the canonical Forgejo and on the GitHub mirror; both destinations are declared in `.ops.yaml`.
- The Forgejo-to-GitHub mirror pushes only the triggering ref and no longer prunes with a wildcard.
- Development pointers in the README and AGENTS.md name the canonical origin.

## capacium-mcp v0.3.0 — 2026-06-04

First public-track release of the MCP server that exposes Capacium tools,
resources, and crawler management to AI coding agents via the Model
Context Protocol over stdio + SSE.

### Added

- **Phase C marketplace MCP tools** (#1, 71a16f5):
  - `search_capabilities` — marketplace discovery with kind / trust / framework / personas filters.
  - `get_capability` — full capability detail including persona block.
  - `install_capability` — install a capability into framework directories.
  - `list_installed` — list locally installed capabilities.
  - `verify_capability` — fingerprint + trust-state verification.
  - `cap_status` — per-canonical trust + install state.
- **Resources**:
  - `search://{query}` — search capabilities by query + filters.
  - `popular://` — list popular capabilities.
  - `detail://{owner}/{name}` — capability detail.
- **Crawler management subsystem** (`crawler_tools.py`):
  - Resources: `crawler://sources`, `crawler://status`.
  - Tools: `crawler_source_enable`, `crawler_source_disable`, `crawler_doctor`, `crawler_cycle`.
- **Claude Desktop auto-register flow** for one-command MCP installation.
- README (4bd62bf) documenting the resource and tool surface.

### Fixed

- **FIX-F-007 (#1, 096821c)** — `install_capability` now supports BOTH the
  symlink-style and copy-style install modes with auto-detection of the
  framework root (Voxel / Elementor / Bricks / generic). Removes the
  ambiguity that previously forced agents to know the framework layout.
- **P0-005** — `ExchangeClient` now targets the correct Exchange v2 routes
  (`/v2/search`, not `/api/v2/capabilities`); the wrong-base-path issue
  silently returned empty results in v0.1.x.

### Compatibility

- Talks to `capacium-exchange` over HTTP only — no direct dependency on
  `capacium-models` or the `capacium` CLI. Compatible with
  `capacium-exchange` v0.4.x.
- Requires Python ≥ 3.10. Only runtime dependency is `httpx>=0.24`.

### Install

```bash
pip install "capacium-mcp @ git+https://github.com/Capacium/capacium-mcp.git@v0.3.0"
```

PyPI publication deferred until the Capacium organization is approved by
PyPI. Primary distribution channel is git+https.

## capacium-mcp v0.1.1 — 2026-05-10

### Fixed

- **P0** — use correct Exchange `/v2/*` endpoints in `ExchangeClient`.
