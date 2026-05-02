"""Crawler management MCP Resources and Tools.

Resources:
  crawler://sources  — List all crawl sources
  crawler://status   — Crawler status (cycles, findings, errors, last run)

Tools:
  crawler_source_enable(source_id)   — Enable a source
  crawler_source_disable(source_id)  — Disable a source
  crawler_doctor()                   — Run comprehensive health check
  crawler_cycle(tier)                — Trigger manual crawl cycle (HOT/WARM/COLD)

Execution strategy:
  1. Try docker exec capacium-crawler python -m src.capacium_crawler.cli <cmd>
  2. Fall back to local python -m src.capacium_crawler.cli <cmd>
  3. Gracefully return an error message if neither is available
"""

from __future__ import annotations

import json
import shutil
import subprocess
from typing import Any, Dict, List, Optional, Tuple


# ---------------------------------------------------------------------------
# Execution helpers
# ---------------------------------------------------------------------------

DOCKER_CONTAINER = "capacium-crawler"
CRAWLER_WORKDIR: Optional[str] = None


def _find_crawler_workdir() -> Optional[str]:
    global CRAWLER_WORKDIR
    if CRAWLER_WORKDIR is not None:
        return CRAWLER_WORKDIR
    from pathlib import Path
    crawler_repo = Path(__file__).resolve().parent.parent.parent.parent / "capacium-crawler"
    if (crawler_repo / "src" / "capacium_crawler" / "cli.py").exists():
        CRAWLER_WORKDIR = str(crawler_repo)
    return CRAWLER_WORKDIR


def _docker_available() -> bool:
    return shutil.which("docker") is not None


def _container_running(name: str = DOCKER_CONTAINER) -> bool:
    if not _docker_available():
        return False
    try:
        r = subprocess.run(
            ["docker", "inspect", name, "--format", "{{.State.Running}}"],
            capture_output=True, text=True, timeout=5,
        )
        return r.stdout.strip() == "true"
    except Exception:
        return False


def _run_docker(script: str) -> Tuple[str, str, int]:
    return _run_cmd(
        ["docker", "exec", DOCKER_CONTAINER, "python", "-c", script],
        timeout=30,
    )


def _run_local(script: str) -> Tuple[str, str, int]:
    workdir = _find_crawler_workdir()
    return _run_cmd(
        ["python", "-c", script],
        timeout=30,
        cwd=workdir,
    )


def _run_cmd(cmd: List[str], timeout: int = 30, cwd: Optional[str] = None) -> Tuple[str, str, int]:
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, cwd=cwd)
        return r.stdout, r.stderr, r.returncode
    except FileNotFoundError:
        return "", "python not found", 1
    except subprocess.TimeoutExpired:
        return "", "command timed out", 1
    except Exception as exc:
        return "", str(exc), 1


def _exec_script(script: str) -> Tuple[str, str, int]:
    if _container_running():
        stdout, stderr, code = _run_docker(script)
        if code == 0:
            return stdout, stderr, code
    stdout, stderr, code = _run_local(script)
    return stdout, stderr, code


def _exec_json_script(script: str) -> Dict[str, Any]:
    stdout, stderr, code = _exec_script(script)
    if code != 0:
        return {"error": stderr.strip() or "command failed", "details": stdout.strip()}
    try:
        return json.loads(stdout.strip())
    except json.JSONDecodeError:
        return {"error": "failed to parse crawler output", "raw_output": stdout.strip()[:500]}


# ---------------------------------------------------------------------------
# Inline Python scripts (run inside crawler environment)
# ---------------------------------------------------------------------------

_SOURCE_LIST_SCRIPT = """\
import json
from src.capacium_crawler.engine import CrawlEngine
e = CrawlEngine(mode='rest')
sources = e.list_sources(enabled_only=False)
rows = []
for s in sources:
    rows.append({
        'id': s.id,
        'name': s.name,
        'type': s.source_type,
        'enabled': s.enabled,
        'url': s.url,
        'last_crawled': s.last_crawled_at,
    })
print(json.dumps(rows))
"""

_SOURCE_TOGGLE_SCRIPT = """\
from src.capacium_crawler.engine import CrawlEngine
e = CrawlEngine(mode='rest')
e.toggle_source('{source_id}', {enabled})
print(json.dumps({{"source_id": "{source_id}", "enabled": {enabled}, "ok": True}}))
"""

_CRAWLER_STATUS_SCRIPT = """\
import json
try:
    from src.capacium_crawler.metrics import METRICS
    cycles = METRICS._cycles.value
    findings = METRICS._findings.value
    errors = METRICS._errors.value
except Exception:
    cycles = -1
    findings = -1
    errors = -1
try:
    from src.capacium_crawler.engine import CrawlEngine
    e = CrawlEngine(mode='rest')
    source_count = len(e.list_sources(enabled_only=False))
    enabled_count = len(e.list_sources(enabled_only=True))
except Exception:
    source_count = -1
    enabled_count = -1
try:
    import subprocess
    r = subprocess.run(['docker', 'ps', '--format', '{{{{.Status}}}}', '--filter', 'name=capacium-crawler'], capture_output=True, text=True)
    container_status = r.stdout.strip() or 'not running'
except Exception:
    container_status = 'unknown'
print(json.dumps({
    'cycles_completed': cycles,
    'findings_total': findings,
    'errors': errors,
    'source_count': source_count,
    'enabled_source_count': enabled_count,
    'container': container_status,
}))
"""

_DOCTOR_SCRIPT = """\
import json, sys, os
results = {"checks": [], "healthy": True}
try:
    from src.capacium_crawler.engine import CrawlEngine
    e = CrawlEngine(mode='rest')
    sources = e.list_sources(enabled_only=False)
    results['checks'].append({"name": "database", "ok": True, "detail": f"{len(sources)} sources"})
except Exception as ex:
    results['checks'].append({"name": "database", "ok": False, "detail": str(ex)[:120]})
    results['healthy'] = False
try:
    from src.capacium_crawler.metrics import METRICS
    results['metrics'] = {
        'cycles': METRICS._cycles.value,
        'findings': METRICS._findings.value,
        'errors': METRICS._errors.value,
    }
except Exception:
    results['metrics'] = {"cycles": -1, "findings": -1, "errors": -1}
print(json.dumps(results))
"""

_CYCLE_SCRIPT = """\
import json, sys
tier = "{tier}"
interval = {interval}
try:
    from src.capacium_crawler.scheduler_staggered import _run_staggered_cycle
    _run_staggered_cycle(tier, interval)
    print(json.dumps({{"cycle": "ok", "tier": tier}}))
except Exception as ex:
    print(json.dumps({{"cycle": "error", "tier": tier, "error": str(ex)[:200]}}))
"""

_TIER_INTERVALS = {"HOT": 900, "WARM": 7200, "COLD": 86400}

# ---------------------------------------------------------------------------
# Resource definitions
# ---------------------------------------------------------------------------

CRAWLER_RESOURCES = [
    {
        "uri": "crawler://sources",
        "name": "Crawl Sources",
        "description": "Lists all configured crawl sources with their state, type, and last crawl time",
        "mimeType": "application/json",
    },
    {
        "uri": "crawler://status",
        "name": "Crawler Status",
        "description": "Crawler health: cycles completed, total findings, errors, source counts, container state",
        "mimeType": "application/json",
    },
]


def read_crawler_resource(uri: str) -> str:
    if uri == "crawler://sources":
        data = _exec_json_script(_SOURCE_LIST_SCRIPT)
        return json.dumps(data, indent=2)
    if uri == "crawler://status":
        data = _exec_json_script(_CRAWLER_STATUS_SCRIPT)
        return json.dumps(data, indent=2)
    return json.dumps({"error": f"unknown crawler resource: {uri}"})


# ---------------------------------------------------------------------------
# Tool definitions (JSON Schema)
# ---------------------------------------------------------------------------

CRAWLER_TOOLS = [
    {
        "name": "crawler_source_enable",
        "description": "Enable a crawl source by its ID",
        "inputSchema": {
            "type": "object",
            "properties": {
                "source_id": {
                    "type": "string",
                    "description": "The ID of the crawl source to enable",
                },
            },
            "required": ["source_id"],
        },
    },
    {
        "name": "crawler_source_disable",
        "description": "Disable a crawl source by its ID",
        "inputSchema": {
            "type": "object",
            "properties": {
                "source_id": {
                    "type": "string",
                    "description": "The ID of the crawl source to disable",
                },
            },
            "required": ["source_id"],
        },
    },
    {
        "name": "crawler_doctor",
        "description": "Run a comprehensive crawler health check (database, metrics, source count)",
        "inputSchema": {
            "type": "object",
            "properties": {},
        },
    },
    {
        "name": "crawler_cycle",
        "description": "Trigger a manual crawl cycle for a specific tier (HOT, WARM, or COLD)",
        "inputSchema": {
            "type": "object",
            "properties": {
                "tier": {
                    "type": "string",
                    "description": "Crawl tier to execute: HOT, WARM, or COLD",
                    "enum": ["HOT", "WARM", "COLD"],
                },
            },
            "required": ["tier"],
        },
    },
]


def handle_crawler_tool(name: str, arguments: Dict[str, Any]) -> Dict[str, Any]:
    if name == "crawler_source_enable":
        return _toggle_source(arguments["source_id"], True)
    if name == "crawler_source_disable":
        return _toggle_source(arguments["source_id"], False)
    if name == "crawler_doctor":
        return _doctor()
    if name == "crawler_cycle":
        return _cycle(arguments["tier"])
    return {"error": f"unknown crawler tool: {name}"}


# ---------------------------------------------------------------------------
# Tool implementations
# ---------------------------------------------------------------------------

def _toggle_source(source_id: str, enabled: bool) -> Dict[str, Any]:
    state = str(enabled).lower()
    script = _SOURCE_TOGGLE_SCRIPT.format(source_id=source_id, enabled=state)
    data = _exec_json_script(script)
    if "error" in data and "ok" not in data:
        return data
    if data.get("ok") is True:
        return {
            "source_id": source_id,
            "enabled": enabled,
            "result": "enabled" if enabled else "disabled",
        }
    return {"error": "failed to toggle source", "details": data}


def _doctor() -> Dict[str, Any]:
    docker_ok = _container_running()
    result: Dict[str, Any] = {
        "docker_container_running": docker_ok,
        "checks": [],
        "healthy": docker_ok,
    }

    if docker_ok:
        data = _exec_json_script(_DOCTOR_SCRIPT)
        if "error" not in data:
            result["crawler_checks"] = data.get("checks", [])
            result["metrics"] = data.get("metrics", {})
            result["healthy"] = data.get("healthy", False)
        else:
            result["healthy"] = False
            result["error"] = data.get("error", "unknown crawler error")
    else:
        result["checks"].append({
            "name": "container",
            "ok": False,
            "detail": "capacium-crawler container is not running",
        })
        if _find_crawler_workdir():
            result["checks"].append({
                "name": "local_cli",
                "ok": True,
                "detail": "local crawler CLI found — run manually",
            })
        else:
            result["checks"].append({
                "name": "local_cli",
                "ok": False,
                "detail": "neither Docker container nor local CLI available",
            })

    return result


def _cycle(tier: str) -> Dict[str, Any]:
    tier = tier.upper()
    if tier not in _TIER_INTERVALS:
        return {"error": f"invalid tier: {tier}. Use HOT, WARM, or COLD"}

    if not _container_running():
        return {"error": "crawler container is not running — cannot trigger cycle"}

    interval = _TIER_INTERVALS[tier]
    script = _CYCLE_SCRIPT.format(tier=tier, interval=interval)
    data = _exec_json_script(script)
    return data


# ---------------------------------------------------------------------------
# Registration helper
# ---------------------------------------------------------------------------

def register_crawler_tools() -> Dict[str, Any]:
    """Return crawler resources and tools for registration in the MCP server.

    Returns:
        dict with keys: resources (list of resource definitions),
                        tools (list of tool schema definitions),
                        resource_handler (callable: uri -> text),
                        tool_handler (callable: name, args -> result dict)
    """
    return {
        "resources": CRAWLER_RESOURCES,
        "tools": CRAWLER_TOOLS,
        "resource_handler": read_crawler_resource,
        "tool_handler": handle_crawler_tool,
    }
