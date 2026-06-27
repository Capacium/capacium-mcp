# Capacium MCP Server

> [!NOTE]
> **Public mirror.** The canonical repository is hosted on our self-hosted git.
> This GitHub copy is a read-only mirror kept in sync for visibility and
> installation. Bug reports are welcome via Issues; pull requests are applied
> upstream and synced back here.

Model Context Protocol (MCP) server exposing Capacium Exchange capabilities to AI agents.

## Resources

- `capacium://system/status` — System health and stats
- `capacium://{type}/{query}` — Search capabilities (e.g., `capacium://search/claude`)
- `capacium://popular` — Popular capabilities
- `capacium://capabilities/{owner}/{name}` — Detail view
- `capacium://crawler/sources` / `capacium://crawler/status` — Crawler management

## Tools

- `cap_install` — Install a capability
- `cap_verify` — Verify fingerprint
- `cap_status` — Check install status
- `crawler_source_enable` / `crawler_source_disable` — Control sources
- `crawler_doctor` — Health check
- `crawler_cycle` — Trigger crawl cycle

## Usage

```bash
pip install capacium-crawler
cap mcp start                  # stdio transport (default)
cap mcp start --transport sse --port 9999
```

Agents connect via MCP protocol to browse, search, install, and verify capabilities.
