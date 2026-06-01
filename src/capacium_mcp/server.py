"""Capacium MCP Server — exposes Capacium tools, resources, and crawler management to AI agents via MCP.

Implements the Model Context Protocol (MCP) over stdio transport
(stdin/stdout JSON-RPC) and SSE (Server-Sent Events over HTTP).

Resources:
  search://{query}       Search capabilities by query + filters
  popular://             List popular capabilities
  detail://{owner}/{name} Get capability details

Tools:
  search_capabilities    Marketplace discovery — search with filters (kind, trust, framework, personas)
  get_capability         View full capability detail including persona block
  install_capability     Install a capability into framework directories
  list_installed         List all locally installed capabilities
  verify_capability      Verify fingerprint and trust state
  cap_status             Check trust/install status of a capability

Crawler management (registered from crawler_tools.py):
  Resources: crawler://sources, crawler://status
  Tools: crawler_source_enable, crawler_source_disable, crawler_doctor, crawler_cycle

Crawler management (registered from crawler_tools.py):
  Resources: crawler://sources, crawler://status
  Tools: crawler_source_enable, crawler_source_disable, crawler_doctor, crawler_cycle

Usage:
  capacium-mcp                     # stdio mode (default)
  capacium-mcp --transport sse     # SSE mode on :9999
  capacium-mcp --port 9999         # custom SSE port
"""

from __future__ import annotations

import json
import os
import sys
import threading
import time
from datetime import datetime, timezone
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from typing import Any, Dict, List, Optional

import httpx

from .crawler_tools import register_crawler_tools

# ---------------------------------------------------------------------------
# Trust state badges
# ---------------------------------------------------------------------------

TRUST_BADGES = {
    "discovered": "discovered",
    "verified": "verified",
    "trusted": "trusted",
    "signed": "signed",
    "unknown": "unknown",
}

TRUST_RANK = {
    "discovered": 0,
    "verified": 1,
    "trusted": 2,
    "signed": 3,
}


def _trust_badge(state: str) -> str:
    return TRUST_BADGES.get(state, TRUST_BADGES["unknown"])


# ---------------------------------------------------------------------------
# MCP JSON-RPC helpers
# ---------------------------------------------------------------------------

JSONRPC_VERSION = "2.0"

_ERROR_PARSE = (-32700, "Parse error")
_ERROR_INVALID_REQUEST = (-32600, "Invalid Request")
_ERROR_METHOD_NOT_FOUND = (-32601, "Method not found")
_ERROR_INVALID_PARAMS = (-32602, "Invalid params")
_ERROR_INTERNAL = (-32603, "Internal error")

MCP_SERVER_INFO = {
    "name": "capacium-mcp",
    "version": "0.2.0",
}

MCP_CAPABILITIES = {
    "tools": {},
    "resources": {},
}


def jsonrpc_response(request_id: Any, result: Any) -> Dict[str, Any]:
    return {"jsonrpc": JSONRPC_VERSION, "id": request_id, "result": result}


def jsonrpc_error(request_id: Any, code: int, message: str, data: Any = None) -> Dict[str, Any]:
    err: Dict[str, Any] = {"code": code, "message": message}
    if data is not None:
        err["data"] = data
    return {"jsonrpc": JSONRPC_VERSION, "id": request_id, "error": err}


def jsonrpc_notification(method: str, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    msg: Dict[str, Any] = {"jsonrpc": JSONRPC_VERSION, "method": method}
    if params is not None:
        msg["params"] = params
    return msg


# ---------------------------------------------------------------------------
# Exchange API client
# ---------------------------------------------------------------------------

class ExchangeClient:
    """Lightweight Exchange API client using httpx."""

    def __init__(self, exchange_url: str = ""):
        self.base_url = (
            exchange_url
            or os.environ.get("CAPACIUM_EXCHANGE_API_URL", "")
            or os.environ.get("CAPACIUM_REGISTRY_URL", "")
            or "http://localhost:8000"
        ).rstrip("/")
        self._client = httpx.Client(
            timeout=httpx.Timeout(30.0),
            headers={"User-Agent": "capacium-mcp/0.1.0", "Accept": "application/json"},
        )

    def search(
        self,
        query: str,
        kind: Optional[str] = None,
        trust: Optional[str] = None,
        framework: Optional[str] = None,
        limit: int = 20,
    ) -> List[Dict[str, Any]]:
        params = {"query": query, "limit": str(limit)}
        if kind:
            params["kind"] = kind
        if trust:
            params["trust_state"] = trust
        if framework:
            params["framework"] = framework

        # P0-005: use correct Exchange v2 endpoint (/v2/search, not /api/v2/capabilities)
        try:
            resp = self._client.get(f"{self.base_url}/v2/search", params=params)
            resp.raise_for_status()
            data = resp.json()
        except Exception:
            return []

        if isinstance(data, dict):
            return data.get("listings", data.get("results", data.get("capabilities", [])))
        if isinstance(data, list):
            return data
        return []

    def popular(self, limit: int = 20) -> List[Dict[str, Any]]:
        # P0-005: use correct Exchange v2 endpoint
        try:
            resp = self._client.get(f"{self.base_url}/v2/search", params={
                "query": "",
                "sort": "installs",
                "limit": str(limit),
            })
            resp.raise_for_status()
            data = resp.json()
        except Exception:
            return []

        if isinstance(data, dict):
            return data.get("listings", data.get("results", data.get("capabilities", [])))
        if isinstance(data, list):
            return data
        return []

    def get_capability(self, owner: str, name: str) -> Optional[Dict[str, Any]]:
        # P0-005: correct endpoint is /v2/capabilities/{owner}/{name}
        try:
            resp = self._client.get(f"{self.base_url}/v2/capabilities/{owner}/{name}")
            resp.raise_for_status()
            return resp.json()
        except Exception:
            return None

    def download(self, owner: str, name: str, version: str = "latest") -> Optional[bytes]:
        # P0-005: correct download endpoint
        for path in (
            f"{self.base_url}/v2/capabilities/{owner}/{name}/download",
            f"{self.base_url}/v2/listings/{owner}/{name}/download",
        ):
            try:
                resp = self._client.get(path, params={"version": version})
                if resp.status_code == 404:
                    continue
                resp.raise_for_status()
                return resp.content
            except Exception:
                continue
        return None

    def check_trust(self, owner: str, name: str) -> Optional[str]:
        cap = self.get_capability(owner, name)
        if cap is None:
            return None
        return cap.get("trust_state", cap.get("trust", "discovered"))


# ---------------------------------------------------------------------------
# Local filesystem helpers
# ---------------------------------------------------------------------------

CAPACIUM_HOME = Path.home() / ".capacium"
ACTIVE_DIR = CAPACIUM_HOME / "active"

FRAMEWORK_DIRS = {
    "opencode": Path.home() / ".opencode" / "skills",
    "claude-code": Path.home() / ".claude" / "skills",
    "gemini-cli": Path.home() / ".gemini" / "skills",
    "cursor": Path.home() / ".cursor" / "skills",
    "continue": Path.home() / ".continue" / "skills",
}


def _compute_sha256(path: Path) -> str:
    import hashlib

    if not path.exists():
        return ""
    h = hashlib.sha256()
    if path.is_dir():
        for fp in sorted(path.rglob("*")):
            if fp.is_file() and ".cap-meta.json" not in str(fp):
                h.update(fp.read_bytes())
    else:
        h.update(path.read_bytes())
    return h.hexdigest()


def _find_cap_path(name: str) -> Optional[Path]:
    cap_path = ACTIVE_DIR / name
    if cap_path.exists():
        return cap_path
    for fw_dir in FRAMEWORK_DIRS.values():
        candidate = fw_dir / name
        if candidate.exists():
            return candidate.resolve()
    return None


# ---------------------------------------------------------------------------
# Resource definitions
# ---------------------------------------------------------------------------

RESOURCES = [
    {
        "uri": "search://{query}",
        "name": "Search Capabilities",
        "description": "Search Capacium exchange for capabilities by query and optional filters (kind, trust, framework)",
        "mimeType": "application/json",
    },
    {
        "uri": "popular://",
        "name": "Popular Capabilities",
        "description": "List popular/most installed capabilities from the Capacium exchange",
        "mimeType": "application/json",
    },
    {
        "uri": "detail://{owner}/{name}",
        "name": "Capability Details",
        "description": "Get detailed metadata for a specific capability by owner and name",
        "mimeType": "application/json",
    },
]


def _format_result(r: Dict[str, Any]) -> Dict[str, Any]:
    trust_state = r.get("trust_state", r.get("trust", "unknown"))
    frameworks = r.get("frameworks", r.get("target_frameworks", []))
    return {
        "owner": r.get("owner", r.get("publisher_id", "?")),
        "name": r.get("name", r.get("canonical_name", "?")),
        "version": r.get("version", "0.1.0"),
        "kind": r.get("kind", r.get("package_type", "skill")),
        "description": r.get("description", r.get("short_description", "")),
        "fingerprint": r.get("fingerprint", "")[:12],
        "trust_state": trust_state,
        "frameworks": frameworks,
        "tags": r.get("tags", []),
        "source_url": r.get("source_url", r.get("canonical_source_url", "")),
    }


# ---------------------------------------------------------------------------
# Resource + Tool registry (built at init time)
# ---------------------------------------------------------------------------

def _build_resource_list(exchange: ExchangeClient) -> List[Dict[str, Any]]:
    return RESOURCES


def _read_resource(exchange: ExchangeClient, uri: str, params: Optional[Dict[str, Any]] = None) -> str:
    if uri.startswith("search://"):
        raw = uri[len("search://"):]
        query = raw if raw else ""
        kind = params.get("kind") if params else None
        trust = params.get("trust") if params else None
        framework = params.get("framework") if params else None
        results = exchange.search(query or "", kind=kind, trust=trust, framework=framework)
        formatted = [_format_result(r) for r in results]
        return json.dumps({"query": query, "count": len(formatted), "results": formatted}, indent=2)

    if uri == "popular://":
        results = exchange.popular()
        formatted = [_format_result(r) for r in results]
        return json.dumps({"count": len(formatted), "results": formatted}, indent=2)

    if uri.startswith("detail://"):
        raw = uri[len("detail://"):]
        parts = raw.split("/")
        if len(parts) >= 2:
            owner, name = parts[0], parts[1]
            cap = exchange.get_capability(owner, name)
            if cap:
                return json.dumps(_format_result(cap), indent=2)
            return json.dumps({"error": f"capability not found: {owner}/{name}"})
        return json.dumps({"error": f"invalid detail URI: {uri}"})

    return json.dumps({"error": f"unknown resource: {uri}"})


TOOL_SCHEMAS = [
    {
        "name": "search_capabilities",
        "description": "Search the Capacium marketplace for capabilities by query, kind, trust state, framework, or target personas.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Free-text search query (e.g. 'code review', 'pdf parser')",
                },
                "kind": {
                    "type": "string",
                    "enum": ["skill", "mcp-server", "bundle", "tool", "prompt", "template", "workflow", "connector-pack", "resource"],
                    "description": "Filter by capability kind",
                },
                "trust": {
                    "type": "string",
                    "enum": ["discovered", "pending_review", "verified", "signed", "deprecated"],
                    "description": "Filter by minimum trust state",
                },
                "framework": {
                    "type": "string",
                    "description": "Filter by target framework (e.g. claude-desktop, opencode, cursor)",
                },
                "sort": {
                    "type": "string",
                    "enum": ["stars", "trust", "score", "updated"],
                    "description": "Sort order for results",
                },
                "limit": {
                    "type": "integer",
                    "description": "Maximum number of results (default: 10)",
                },
            },
        },
    },
    {
        "name": "get_capability",
        "description": "Get full detail for a capability including description, trust state, personas, value propositions, pricing, screenshots, and install command.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "owner": {
                    "type": "string",
                    "description": "Capability owner (publisher or org)",
                },
                "name": {
                    "type": "string",
                    "description": "Capability name",
                },
            },
            "required": ["owner", "name"],
        },
    },
    {
        "name": "install_capability",
        "description": "Install a capability from the Exchange into framework directories. Downloads the package and creates symlinks.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "owner": {
                    "type": "string",
                    "description": "Capability owner (publisher)",
                },
                "name": {
                    "type": "string",
                    "description": "Capability name",
                },
                "framework": {
                    "type": "string",
                    "description": "Target framework: opencode, claude-desktop, claude-code, gemini-cli, cursor, continue",
                },
            },
            "required": ["owner", "name"],
        },
    },
    {
        "name": "list_installed",
        "description": "List all locally installed Capacium capabilities with their versions, trust states, and frameworks.",
        "inputSchema": {
            "type": "object",
            "properties": {},
        },
    },
    {
        "name": "verify_capability",
        "description": "Verify a capability's SHA-256 fingerprint against the Exchange to detect tampering.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "owner": {
                    "type": "string",
                    "description": "Capability owner",
                },
                "name": {
                    "type": "string",
                    "description": "Capability name to verify",
                },
            },
            "required": ["owner", "name"],
        },
    },
]


def _handle_tool(exchange: ExchangeClient, name: str, arguments: Dict[str, Any]) -> Dict[str, Any]:
    if name == "search_capabilities":
        return _do_search_capabilities(exchange, arguments)
    if name == "get_capability":
        return _do_get_capability(exchange, arguments)
    if name == "install_capability":
        return _do_install(exchange, arguments)
    if name == "list_installed":
        return _do_list_installed(exchange, arguments)
    if name == "verify_capability":
        return _do_verify(exchange, arguments)

    return {"error": f"unknown tool: {name}"}


def _do_search_capabilities(exchange: ExchangeClient, args: Dict[str, Any]) -> Dict[str, Any]:
    query = args.get("query", "")
    kind = args.get("kind")
    trust = args.get("trust")
    framework = args.get("framework")
    sort = args.get("sort", "stars")
    limit = args.get("limit", 10)

    results = exchange.search(query or "", kind=kind, trust=trust, framework=framework, limit=limit)
    formatted = [_format_result_marketplace(r) for r in results]
    return {"results": formatted, "count": len(formatted), "query": query}


def _do_get_capability(exchange: ExchangeClient, args: Dict[str, Any]) -> Dict[str, Any]:
    owner = args.get("owner", "")
    name = args.get("name", "")
    if not owner or not name:
        return {"error": "owner and name are required"}

    cap = exchange.get_capability(owner, name)
    if not cap:
        return {"error": f"capability not found: {owner}/{name}"}

    return _format_result_marketplace(cap)


def _do_list_installed(exchange: ExchangeClient, args: Dict[str, Any]) -> Dict[str, Any]:
    installed = []
    if ACTIVE_DIR.exists():
        for entry in sorted(ACTIVE_DIR.iterdir()):
            if not entry.is_dir():
                continue
            meta_path = entry / ".cap-meta.json"
            if not meta_path.exists():
                continue
            try:
                meta = json.loads(meta_path.read_text())
                installed.append({
                    "owner": meta.get("owner", "?"),
                    "name": meta.get("name", entry.name),
                    "version": meta.get("version", "?"),
                    "kind": meta.get("kind", "skill"),
                    "fingerprint": meta.get("fingerprint", "")[:12],
                    "installed_at": meta.get("installed_at", ""),
                    "framework": meta.get("framework", ""),
                })
            except Exception:
                installed.append({"name": entry.name, "error": "could not read metadata"})

    return {"installed": installed, "count": len(installed)}


def _format_result_marketplace(r: Dict[str, Any]) -> Dict[str, Any]:
    """Rich marketplace detail format — includes personas, screenshots, pricing, value props."""
    trust_state = r.get("trust_state", r.get("trust", "unknown"))
    frameworks = r.get("frameworks", [])
    return {
        "canonical": f"{r.get('owner', r.get('publisher_id', '?'))}/{r.get('name', r.get('canonical_name', '?'))}",
        "owner": r.get("owner", r.get("publisher_id", "?")),
        "name": r.get("name", r.get("canonical_name", "?")),
        "version": r.get("version", "0.1.0"),
        "kind": r.get("kind", r.get("package_type", "skill")),
        "description": r.get("description", r.get("short_description", "")),
        "long_description": r.get("long_description", ""),
        "fingerprint": r.get("fingerprint", "")[:12],
        "trust_state": trust_state,
        "quality_score": r.get("quality_score", 0),
        "install_count": r.get("install_count", r.get("installs", 0)),
        "frameworks": frameworks,
        "target_personas": r.get("target_personas", []),
        "value_propositions": r.get("value_propositions", []),
        "screenshots": r.get("screenshots", []),
        "pricing": r.get("pricing"),
        "license": r.get("github_license", r.get("license", "")),
        "tags": r.get("tags", []),
        "repository": r.get("canonical_source_url", r.get("source_url", "")),
        "install_command": f"cap install {r.get('owner', '?')}/{r.get('name', '?')}",
    }


def _do_install(exchange: ExchangeClient, args: Dict[str, Any]) -> Dict[str, Any]:
    owner = args.get("owner", "")
    name = args.get("name", "")
    framework = args.get("framework") or "opencode"

    if not owner or not name:
        return {"error": "owner and name are required"}

    cap_path = ACTIVE_DIR / name
    cap_path.mkdir(parents=True, exist_ok=True)

    cap_data = exchange.get_capability(owner, name)
    fingerprint = ""
    trust_state = "discovered"
    version = "0.1.0"
    kind = "skill"

    if cap_data:
        version = cap_data.get("version", version)
        kind = cap_data.get("kind", cap_data.get("package_type", kind))
        trust_state = cap_data.get("trust_state", cap_data.get("trust", trust_state))
        fingerprint = cap_data.get("fingerprint", "")

    content = exchange.download(owner, name, version)
    if content:
        import io
        import zipfile

        with zipfile.ZipFile(io.BytesIO(content)) as zf:
            for member in zf.namelist():
                member_info = zf.getinfo(member)
                if member_info.is_dir():
                    continue
                extracted = cap_path / member
                extracted.parent.mkdir(parents=True, exist_ok=True)
                extracted.write_bytes(zf.read(member))

    if not fingerprint:
        fingerprint = _compute_sha256(cap_path)

    meta = {
        "owner": owner,
        "name": name,
        "version": version,
        "kind": kind,
        "fingerprint": fingerprint,
        "installed_at": datetime.now(timezone.utc).isoformat(),
        "framework": framework,
    }
    (cap_path / ".cap-meta.json").write_text(json.dumps(meta, indent=2))

    fw_dir = FRAMEWORK_DIRS.get(framework)
    symlink_created = False
    if fw_dir:
        try:
            fw_dir.mkdir(parents=True, exist_ok=True)
            link_path = fw_dir / name
            if link_path.exists() or link_path.is_symlink():
                link_path.unlink()
            link_path.symlink_to(cap_path, target_is_directory=True)
            symlink_created = True
        except OSError:
            pass

    return {
        "installed": True,
        "owner": owner,
        "name": name,
        "version": version,
        "kind": kind,
        "fingerprint": fingerprint[:12],
        "trust_state": trust_state,
        "path": str(cap_path),
        "framework": framework,
        "symlink_created": symlink_created,
    }


def _do_verify(exchange: ExchangeClient, args: Dict[str, Any]) -> Dict[str, Any]:
    name = args.get("name", "")
    if not name:
        return {"error": "name is required"}

    result: Dict[str, Any] = {
        "name": name,
        "fingerprint_match": False,
        "trust_state": "unknown",
        "installed": False,
    }

    cap_path = _find_cap_path(name)
    if not cap_path:
        result["error"] = f"Capability '{name}' is not installed"
        return result

    meta_path = cap_path / ".cap-meta.json"
    if meta_path.exists():
        try:
            meta = json.loads(meta_path.read_text())
        except (json.JSONDecodeError, OSError):
            meta = {}
    else:
        meta = {}

    owner = meta.get("owner", "?")
    recorded_fingerprint = meta.get("fingerprint", "")
    current_fingerprint = _compute_sha256(cap_path)

    result["installed"] = True
    result["recorded_fingerprint"] = recorded_fingerprint[:12] if recorded_fingerprint else ""
    result["current_fingerprint"] = current_fingerprint[:12] if current_fingerprint else ""
    result["fingerprint_match"] = (
        recorded_fingerprint == current_fingerprint if recorded_fingerprint else None
    )

    if owner and owner != "?":
        trust_state = exchange.check_trust(owner, name)
        result["trust_state"] = trust_state or "unknown"
    else:
        result["trust_state"] = "unknown"

    if result["fingerprint_match"] is False:
        result["tampered"] = True
        result["warning"] = "Fingerprint mismatch — content may have been modified"
    elif result["fingerprint_match"] is True:
        result["tampered"] = False

    return result


def _do_status(exchange: ExchangeClient, args: Dict[str, Any]) -> Dict[str, Any]:
    name = args.get("name", "")
    if not name:
        return {"error": "name is required"}

    result: Dict[str, Any] = {
        "name": name,
        "installed": False,
        "trust_state": "unknown",
    }

    cap_path = _find_cap_path(name)
    if cap_path:
        result["installed"] = True
        result["path"] = str(cap_path)

        meta_path = cap_path / ".cap-meta.json"
        if meta_path.exists():
            try:
                meta = json.loads(meta_path.read_text())
                result["owner"] = meta.get("owner", "unknown")
                result["version"] = meta.get("version", "?")
                result["kind"] = meta.get("kind", "unknown")
                result["fingerprint"] = meta.get("fingerprint", "")[:12]
                result["installed_at"] = meta.get("installed_at", "")
                result["framework"] = meta.get("framework", "")

                owner = meta.get("owner", "?")
                if owner and owner != "?" and owner != "unknown":
                    trust_state = exchange.check_trust(owner, name)
                    result["trust_state"] = trust_state or "discovered"
            except (json.JSONDecodeError, OSError):
                pass

    return result


# ---------------------------------------------------------------------------
# MCP Server (stdio transport)
# ---------------------------------------------------------------------------

class CapaciumMCPServer:
    """MCP server over stdio (stdin/stdout JSON-RPC)."""

    def __init__(self, exchange_url: str = ""):
        self.exchange = ExchangeClient(exchange_url)
        self._initialized = False

        crawler = register_crawler_tools()
        self._all_resources = RESOURCES + crawler["resources"]
        self._all_tools = TOOL_SCHEMAS + crawler["tools"]
        self._crawler_resource_handler = crawler["resource_handler"]
        self._crawler_tool_handler = crawler["tool_handler"]

    def run(self) -> None:
        for line in sys.stdin:
            line = line.strip()
            if not line:
                continue
            try:
                request = json.loads(line)
            except json.JSONDecodeError:
                error_resp = jsonrpc_error(
                    None, _ERROR_PARSE[0], _ERROR_PARSE[1],
                    data={"raw": line[:200]},
                )
                self._write_response(error_resp)
                continue

            response = self._dispatch(request)
            if response is not None:
                self._write_response(response)

    def _dispatch(self, request: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        req_id = request.get("id")
        method = request.get("method", "")
        params = request.get("params", {}) or {}

        if method == "initialize":
            self._initialized = True
            return jsonrpc_response(req_id, {
                "protocolVersion": "2024-11-05",
                "serverInfo": MCP_SERVER_INFO,
                "capabilities": MCP_CAPABILITIES,
            })

        if method == "notifications/initialized":
            return None

        if method == "ping":
            return jsonrpc_response(req_id, {})

        if not self._initialized:
            return jsonrpc_error(req_id, _ERROR_INTERNAL[0], "Server not initialized")

        if method == "resources/list":
            return jsonrpc_response(req_id, {"resources": self._all_resources})

        if method == "resources/read":
            uri = params.get("uri", "")
            req_params = params.get("params")
            return self._handle_resource_read(req_id, uri, req_params)

        if method == "tools/list":
            return jsonrpc_response(req_id, {"tools": self._all_tools})

        if method == "tools/call":
            tool_name = params.get("name", "")
            tool_args = params.get("arguments", {}) or {}
            return self._call_tool(req_id, tool_name, tool_args)

        return jsonrpc_error(req_id, _ERROR_METHOD_NOT_FOUND[0],
                             f"Method not found: {method}")

    def _handle_resource_read(self, req_id: Any, uri: str, req_params: Optional[Dict[str, Any]]) -> Dict[str, Any]:
        try:
            if uri.startswith("crawler://"):
                text = self._crawler_resource_handler(uri)
            else:
                text = _read_resource(self.exchange, uri, req_params)
            return jsonrpc_response(req_id, {
                "contents": [
                    {"uri": uri, "mimeType": "application/json", "text": text},
                ],
            })
        except Exception as exc:
            return jsonrpc_error(req_id, _ERROR_INTERNAL[0],
                                 f"Resource read error: {exc}",
                                 data={"uri": uri})

    def _call_tool(self, req_id: Any, tool_name: str, args: Dict[str, Any]) -> Dict[str, Any]:
        try:
            if tool_name.startswith("crawler_"):
                result = self._crawler_tool_handler(tool_name, args)
            else:
                result = _handle_tool(self.exchange, tool_name, args)

            return jsonrpc_response(req_id, {
                "content": [
                    {"type": "text", "text": json.dumps(result, indent=2)},
                ],
            })
        except Exception as exc:
            return jsonrpc_error(req_id, _ERROR_INTERNAL[0],
                                 f"Tool error: {exc}",
                                 data={"tool": tool_name})

    @staticmethod
    def _write_response(response: Dict[str, Any]) -> None:
        sys.stdout.write(json.dumps(response) + "\n")
        sys.stdout.flush()


# ---------------------------------------------------------------------------
# SSE transport
# ---------------------------------------------------------------------------

class _SSERequestHandler(BaseHTTPRequestHandler):
    server_instance: "MCPSSEServer" = None  # type: ignore

    def log_message(self, format, *args):
        pass

    def do_GET(self):
        if self.path == "/sse" or self.path == "/":
            self._handle_sse()
        elif self.path == "/health" or self.path == "/healthz":
            self._json(200, {"status": "ok", "mode": "sse"})
        else:
            self.send_response(404)
            self.send_header("Content-Type", "text/plain")
            self.end_headers()
            self.wfile.write(b"Not Found")

    def do_POST(self):
        if self.path in ("/message", "/rpc"):
            self._handle_message()
        else:
            self.send_response(404)
            self.send_header("Content-Type", "text/plain")
            self.end_headers()
            self.wfile.write(b"Not Found")

    def _handle_sse(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()

        endpoint = f"http://localhost:{self.server.server_address[1]}/message"
        event = json.dumps({
            "jsonrpc": JSONRPC_VERSION,
            "method": "endpoint",
            "params": {"uri": endpoint},
        })
        self.wfile.write(f"data: {event}\n\n".encode())
        self.wfile.flush()

        while True:
            try:
                time.sleep(1)
                self.wfile.write(b": heartbeat\n\n")
                self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError, OSError):
                break

    def _handle_message(self):
        content_length = int(self.headers.get("Content-Length", "0"))
        if content_length == 0:
            self._json(400, {"error": "Empty body"})
            return

        body = self.rfile.read(content_length)
        try:
            request = json.loads(body)
        except json.JSONDecodeError:
            self._json(400, jsonrpc_error(None, _ERROR_PARSE[0], _ERROR_PARSE[1]))
            return

        response = self.server.server_instance._dispatch(request)
        if response is not None:
            self._json(200, response)
        else:
            self._json(204, {})

    def do_OPTIONS(self):
        self.send_response(200)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def _json(self, status: int, data: Any):
        body = json.dumps(data).encode("utf-8") if not isinstance(data, bytes) else data
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)


class MCPSSEServer:
    """MCP server over SSE (Server-Sent Events over HTTP)."""

    def __init__(self, exchange_url: str = ""):
        self.exchange = ExchangeClient(exchange_url)
        self._initialized = True

        crawler = register_crawler_tools()
        self._all_resources = RESOURCES + crawler["resources"]
        self._all_tools = TOOL_SCHEMAS + crawler["tools"]
        self._crawler_resource_handler = crawler["resource_handler"]
        self._crawler_tool_handler = crawler["tool_handler"]

    def _dispatch(self, request: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        method = request.get("method", "")
        params = request.get("params", {}) or {}
        req_id = request.get("id")

        if method == "initialize":
            return jsonrpc_response(req_id, {
                "protocolVersion": "2024-11-05",
                "serverInfo": MCP_SERVER_INFO,
                "capabilities": MCP_CAPABILITIES,
            })
        if method == "notifications/initialized":
            return None
        if method == "ping":
            return jsonrpc_response(req_id, {})
        if method == "resources/list":
            return jsonrpc_response(req_id, {"resources": self._all_resources})
        if method == "resources/read":
            uri = params.get("uri", "")
            req_params = params.get("params")
            return self._handle_resource_read(req_id, uri, req_params)
        if method == "tools/list":
            return jsonrpc_response(req_id, {"tools": self._all_tools})
        if method == "tools/call":
            tool_name = params.get("name", "")
            tool_args = params.get("arguments", {}) or {}
            return self._call_tool(req_id, tool_name, tool_args)
        return jsonrpc_error(req_id, _ERROR_METHOD_NOT_FOUND[0],
                             f"Method not found: {method}")

    def _handle_resource_read(self, req_id: Any, uri: str, req_params: Optional[Dict[str, Any]]) -> Dict[str, Any]:
        try:
            if uri.startswith("crawler://"):
                text = self._crawler_resource_handler(uri)
            else:
                text = _read_resource(self.exchange, uri, req_params)
            return jsonrpc_response(req_id, {
                "contents": [
                    {"uri": uri, "mimeType": "application/json", "text": text},
                ],
            })
        except Exception as exc:
            return jsonrpc_error(req_id, _ERROR_INTERNAL[0],
                                 f"Resource read error: {exc}",
                                 data={"uri": uri})

    def _call_tool(self, req_id: Any, tool_name: str, args: Dict[str, Any]) -> Dict[str, Any]:
        try:
            if tool_name.startswith("crawler_"):
                result = self._crawler_tool_handler(tool_name, args)
            else:
                result = _handle_tool(self.exchange, tool_name, args)
            return jsonrpc_response(req_id, {
                "content": [
                    {"type": "text", "text": json.dumps(result, indent=2)},
                ],
            })
        except Exception as exc:
            return jsonrpc_error(req_id, _ERROR_INTERNAL[0],
                                 f"Tool error: {exc}",
                                 data={"tool": tool_name})

    def start(self, port: int = 9999) -> HTTPServer:
        _SSERequestHandler.server_instance = self
        server = HTTPServer(("0.0.0.0", port), _SSERequestHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        return server


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def main():
    import argparse

    parser = argparse.ArgumentParser(description="Capacium MCP Server")
    parser.add_argument(
        "--transport",
        choices=["stdio", "sse"],
        default="stdio",
        help="Transport mode (default: stdio)",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=0,
        help="Port for SSE transport (default: 9999)",
    )
    parser.add_argument(
        "--exchange-url",
        default="",
        help="Exchange API base URL (default: $CAPACIUM_EXCHANGE_API_URL or http://localhost:8000)",
    )
    args = parser.parse_args()

    if args.transport == "stdio":
        server = CapaciumMCPServer(exchange_url=args.exchange_url)
        server.run()
    elif args.transport == "sse":
        port = args.port or 9999
        server = MCPSSEServer(exchange_url=args.exchange_url)
        httpd = server.start(port=port)
        print(f"Capacium MCP SSE server listening on http://localhost:{port}/sse")
        print(f"  POST http://localhost:{port}/message for JSON-RPC")
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            httpd.shutdown()


if __name__ == "__main__":
    main()
