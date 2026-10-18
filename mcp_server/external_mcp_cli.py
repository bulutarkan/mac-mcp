"""`mac-mcp mcp`: add, list, test and call the owner's own MCP servers (Settings › MCP Servers)."""
from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import sys
from typing import Any, Callable, Dict, List, Tuple

from . import external_mcp, mcp_connectors  # noqa: F401  (registers how connected services sign in)

Request = Callable[..., Tuple[int, Dict[str, Any]]]


def _pairs(values: List[str] | None, flag: str) -> Dict[str, str]:
    out: Dict[str, str] = {}
    for pair in values or []:
        if "=" not in pair:
            raise external_mcp.ConfigError(f"{flag} takes NAME=VALUE: {pair}")
        key, value = pair.split("=", 1)
        out[key.strip()] = value
    return out


def register(sub: Any, request: Request) -> None:
    p_mcp = sub.add_parser("mcp", help="Your own MCP servers, offered to agents as <server>__<tool> tools.")
    mcp_sub = p_mcp.add_subparsers(dest="mcp_command", required=True)
    p_list = mcp_sub.add_parser("list", help="Servers, their status and tools (live when Mac MCP is running).")
    p_list.add_argument("--json", action="store_true")
    p_add = mcp_sub.add_parser("add", help="Add a server: --url URL for a remote one, or the command after --.")
    p_add.add_argument("name")
    p_add.add_argument("--url")
    p_add.add_argument("--header", action="append", metavar="NAME=VALUE")
    p_add.add_argument("--env", action="append", metavar="NAME=VALUE")
    p_add.add_argument("--hidden", action="store_true", help="Reach its tools only through tool_discover.")
    p_add.add_argument("server_command", nargs="*", help="after --: the command and args of a local server")
    p_import = mcp_sub.add_parser("import", help="Merge an {\"mcpServers\": …} JSON file (Claude Desktop, Cursor); - reads stdin.")
    p_import.add_argument("file")
    for verb in ("remove", "enable", "disable", "test"):
        mcp_sub.add_parser(verb, help=f"{verb.capitalize()} a server.").add_argument("name")
    p_call = mcp_sub.add_parser("call", help="Call one tool directly (no Mac MCP policy; for checking a server).")
    p_call.add_argument("name")
    p_call.add_argument("tool")
    p_call.add_argument("--args", default="{}", help="JSON object of arguments")
    p_mcp.set_defaults(func=lambda args: run(args, request))


def run(args: argparse.Namespace, request: Request) -> int:
    command = args.mcp_command
    try:
        if command == "list":
            return _list(args, request)
        if command == "add":
            entry: Dict[str, Any] = {}
            server_command = [part for part in args.server_command if part != "--"] if args.server_command else []
            if args.url:
                entry["url"] = args.url
                if args.header:
                    entry["headers"] = _pairs(args.header, "--header")
            elif server_command:
                entry["command"], entry["args"] = server_command[0], server_command[1:]
                if args.env:
                    entry["env"] = _pairs(args.env, "--env")
            else:
                raise external_mcp.ConfigError("give --url URL, or the command after --, e.g. -- npx -y exa-mcp-server")
            if args.hidden:
                entry["listed"] = False
            server = external_mcp.upsert_server(args.name, entry)
            print(f"Added {server.name} ({server.transport}). Mac MCP connects it within a few seconds; "
                  f"check with: mac-mcp mcp test {server.name}")
            return 0
        if command == "import":
            text = sys.stdin.read() if args.file == "-" else open(args.file, encoding="utf-8").read()
            names = external_mcp.import_servers(text)
            print("Imported " + ", ".join(names) + ".")
            return 0
        if command == "remove":
            removed = external_mcp.remove_server(args.name)
            print("Removed." if removed else f"No MCP server named {args.name}.")
            return 0 if removed else 1
        if command in {"enable", "disable"}:
            external_mcp.set_server_fields(args.name, enabled=command == "enable")
            print(f"{args.name}: {'on' if command == 'enable' else 'off'}.")
            return 0
        if command == "test":
            servers, _ = external_mcp.load_servers()
            if args.name not in servers:
                raise external_mcp.ConfigError(f"no MCP server named {args.name!r}")
            result = asyncio.run(external_mcp.probe(servers[args.name]))
            if not result["ok"]:
                print(f"{args.name}: could not connect ({result['elapsed_ms']} ms)\n{result['error']}")
                return 1
            info = result.get("server_info") or {}
            print(f"{args.name}: connected to {info.get('name')} {info.get('version') or ''} in {result['elapsed_ms']} ms")
            for tool in result["tools"]:
                print(f"  {tool['name']}  {tool['description'].splitlines()[0][:90] if tool['description'] else ''}")
            return 0
        if command == "call":
            return _call(args)
    except (external_mcp.ConfigError, OSError, ValueError) as exc:
        print(f"mac-mcp mcp {command}: {exc}", file=sys.stderr)
        return 1
    return 2


def _list(args: argparse.Namespace, request: Request) -> int:
    code, body = request("GET", "/dashboard/api/mcp-servers")
    if code != 0 or not body.get("ok"):
        servers, errors = external_mcp.load_servers()
        body = {"running": False, "config_path": str(external_mcp.config_path()), "config_errors": errors,
                "servers": [{**server.public(), "status": "not_started" if server.enabled else "disabled",
                             "tools": []} for server in servers.values()]}
    if args.json:
        print(json.dumps(body, ensure_ascii=False, indent=2))
        return 0
    if not body.get("servers"):
        print(f"No MCP servers yet. Add one with: mac-mcp mcp add NAME --url URL  (config: {body.get('config_path')})")
    for server in body.get("servers") or []:
        target = server.get("url") or " ".join([server.get("command") or ""] + list(server.get("args") or []))
        print(f"{server['name']}  {server['status']}  {len(server.get('tools') or [])} tools  {target}")
        if server.get("error"):
            print("  " + str(server["error"]).splitlines()[0])
    for name, error in (body.get("config_errors") or {}).items():
        print(f"{name}: not usable: {error}")
    return 0


def _call(args: argparse.Namespace) -> int:
    servers, _ = external_mcp.load_servers()
    if args.name not in servers:
        raise external_mcp.ConfigError(f"no MCP server named {args.name!r}")
    arguments = json.loads(args.args)
    if not isinstance(arguments, dict):
        raise external_mcp.ConfigError("--args must be a JSON object")

    async def once() -> Dict[str, Any]:
        from datetime import timedelta

        server = servers[args.name]
        async with contextlib.AsyncExitStack() as stack:
            session, _, _ = await asyncio.wait_for(external_mcp._open_session(stack, server), external_mcp.CONNECT_TIMEOUT_S)
            result = await session.call_tool(args.tool, arguments, read_timeout_seconds=timedelta(seconds=server.timeout_s))
        return external_mcp.result_payload(server.name, args.tool, result)

    payload = asyncio.run(once())
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0 if payload["ok"] else 1
