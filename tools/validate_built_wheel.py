"""Verify the portable Library payload in an already-built wheel."""

from __future__ import annotations

import argparse
import configparser
import io
import json
from pathlib import Path
import zipfile


PACKAGE = "atomizer_local_client"
SERVER_NAME = "context-atomizer-local-library"
EXECUTABLE = "atomizer-local-mcp"
ENTRY_POINT = "atomizer_local_client.mcp.server:main"
REQUIRED_PACKAGE_FILES = {
    f"{PACKAGE}/mcp/__init__.py",
    f"{PACKAGE}/mcp/contracts.py",
    f"{PACKAGE}/mcp/server.py",
    f"{PACKAGE}/mcp/tools.py",
    f"{PACKAGE}/memory_access/__init__.py",
    f"{PACKAGE}/memory_access/access_gate.py",
    f"{PACKAGE}/memory_access/contracts.py",
    f"{PACKAGE}/memory_access/formatting.py",
    f"{PACKAGE}/memory_access/query_service.py",
    f"{PACKAGE}/portable_plugin/.claude-plugin/plugin.json",
    f"{PACKAGE}/portable_plugin/.codex-plugin/plugin.json",
    f"{PACKAGE}/portable_plugin/.mcp.json",
    f"{PACKAGE}/portable_plugin/mcp.json",
    f"{PACKAGE}/portable_plugin/openai.mcp.json",
    f"{PACKAGE}/portable_plugin/plugin.json",
    f"{PACKAGE}/portable_plugin/skills/local-library/SKILL.md",
}


def _json(archive: zipfile.ZipFile, name: str) -> dict[str, object]:
    value = json.loads(archive.read(name).decode("utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"wheel member must contain an object: {name}")
    return value


def validate(wheel_path: Path) -> dict[str, object]:
    wheel = Path(wheel_path).resolve()
    with zipfile.ZipFile(wheel) as archive:
        names = set(archive.namelist())
        missing = sorted(REQUIRED_PACKAGE_FILES - names)
        if missing:
            raise ValueError(f"wheel is missing portable members: {', '.join(missing)}")
        entry_points = [name for name in names if name.endswith(".dist-info/entry_points.txt")]
        if len(entry_points) != 1:
            raise ValueError("wheel must contain exactly one dist-info entry_points.txt")
        parser = configparser.ConfigParser()
        parser.read_file(io.StringIO(archive.read(entry_points[0]).decode("utf-8")))
        if parser.get("console_scripts", EXECUTABLE, fallback=None) != ENTRY_POINT:
            raise ValueError("wheel console executable does not resolve to the MCP server")

        portable = _json(archive, f"{PACKAGE}/portable_plugin/mcp.json")
        claude = _json(archive, f"{PACKAGE}/portable_plugin/.mcp.json")
        codex = _json(archive, f"{PACKAGE}/portable_plugin/openai.mcp.json")
        portable_command = portable["mcpServers"][SERVER_NAME]["command"]
        claude_command = claude["mcpServers"][SERVER_NAME]["command"]
        codex_command = codex[SERVER_NAME]["command"]
        if {portable_command, claude_command, codex_command} != {EXECUTABLE}:
            raise ValueError("portable and vendor MCP mappings do not share the console executable")
    return {
        "passed": True,
        "wheel": wheel.name,
        "required_members": len(REQUIRED_PACKAGE_FILES),
        "console_executable": EXECUTABLE,
        "entry_point": ENTRY_POINT,
        "mcp_mappings": 3,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("wheel", type=Path)
    try:
        result = validate(parser.parse_args().wheel)
    except (KeyError, OSError, ValueError, zipfile.BadZipFile) as error:
        print(json.dumps({"passed": False, "error": str(error)}, sort_keys=True))
        return 1
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
