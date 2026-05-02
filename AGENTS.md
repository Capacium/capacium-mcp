# Capacium MCP Server — Agents Guide

## Language

**English is REQUIRED for ALL Capacium content.**
READ.ME files, documentation, inline code comments, commit messages, PR descriptions, release notes — everything is English. No exceptions for published content.

## Resources (5)

- `search://{query}` — Exchange search
- `popular://` — Top capabilities
- `detail://{owner}/{name}` — Detail view
- `crawler://sources` — Crawler source list
- `crawler://status` — Crawler health + stats

## Tools (7)

- `cap_install` — Install capability
- `cap_verify` — Verify fingerprint
- `cap_status` — Check install status
- `crawler_source_enable` — Enable crawl source
- `crawler_source_disable` — Disable crawl source
- `crawler_doctor` — Crawler health check
- `crawler_cycle` — Trigger manual crawl cycle

## Pre-Commit Checklist

```bash
ruff check src/ --fix && pytest tests/ -q
```
