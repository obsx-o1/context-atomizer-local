"""Offline conformance checks for the bundled Agent Plugins 1.0.0 package."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re
from typing import Any


PLUGIN_SCHEMA = "https://agent-plugins.org/schemas/1.0.0/plugin.schema.json"
MCP_SCHEMA = "https://agent-plugins.org/schemas/1.0.0/mcp.schema.json"
SCHEMA_ROOT = Path(__file__).resolve().parent / "schemas" / "agent-plugins" / "1.0.0"


def _object(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"{path.name} must contain an object")
    return value


def _resolve_ref(root: dict[str, Any], reference: str) -> dict[str, Any]:
    if not reference.startswith("#/"):
        raise ValueError(f"unsupported schema reference: {reference}")
    value: Any = root
    for raw_part in reference[2:].split("/"):
        part = raw_part.replace("~1", "/").replace("~0", "~")
        if not isinstance(value, dict) or part not in value:
            raise ValueError(f"unresolved schema reference: {reference}")
        value = value[part]
    if not isinstance(value, dict):
        raise ValueError(f"schema reference is not an object: {reference}")
    return value


def _matches_type(value: Any, expected: str | list[str]) -> bool:
    if isinstance(expected, list):
        return any(_matches_type(value, candidate) for candidate in expected)
    if expected == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if expected == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    return {
        "array": isinstance(value, list),
        "boolean": isinstance(value, bool),
        "null": value is None,
        "object": isinstance(value, dict),
        "string": isinstance(value, str),
    }.get(expected, False)


def _validate_schema(
    value: Any,
    schema: dict[str, Any],
    root: dict[str, Any],
    path: str,
) -> None:
    if "$ref" in schema:
        _validate_schema(value, _resolve_ref(root, schema["$ref"]), root, path)
        return
    if "not" in schema:
        try:
            _validate_schema(value, schema["not"], root, path)
        except ValueError:
            pass
        else:
            raise ValueError(f"{path} matches a forbidden schema")
    if "oneOf" in schema:
        matches = 0
        for candidate in schema["oneOf"]:
            try:
                _validate_schema(value, candidate, root, path)
            except ValueError:
                continue
            matches += 1
        if matches != 1:
            raise ValueError(f"{path} must match exactly one schema variant")
        return
    if "anyOf" in schema:
        for candidate in schema["anyOf"]:
            try:
                _validate_schema(value, candidate, root, path)
            except ValueError:
                continue
            break
        else:
            raise ValueError(f"{path} must match at least one schema variant")
        return
    if "enum" in schema and value not in schema["enum"]:
        raise ValueError(f"{path} is not an allowed value")
    if "const" in schema and value != schema["const"]:
        raise ValueError(f"{path} does not match the required constant")
    expected_type = schema.get("type")
    if expected_type is not None and not _matches_type(value, expected_type):
        raise ValueError(f"{path} must be a {expected_type}")
    if isinstance(value, str):
        if len(value) < schema.get("minLength", 0):
            raise ValueError(f"{path} is shorter than the schema minimum")
        if "maxLength" in schema and len(value) > schema["maxLength"]:
            raise ValueError(f"{path} is longer than the schema maximum")
        if "pattern" in schema and re.search(schema["pattern"], value) is None:
            raise ValueError(f"{path} does not match the schema pattern")
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if "minimum" in schema and value < schema["minimum"]:
            raise ValueError(f"{path} is below the schema minimum")
        if "maximum" in schema and value > schema["maximum"]:
            raise ValueError(f"{path} is above the schema maximum")
    if isinstance(value, list) and "items" in schema:
        for index, item in enumerate(value):
            _validate_schema(item, schema["items"], root, f"{path}[{index}]")
    if isinstance(value, dict):
        required = schema.get("required", [])
        missing = [key for key in required if key not in value]
        if missing:
            raise ValueError(f"{path} is missing required fields: {', '.join(missing)}")
        properties = schema.get("properties", {})
        additional = schema.get("additionalProperties", True)
        for key, item in value.items():
            if key in properties:
                _validate_schema(item, properties[key], root, f"{path}.{key}")
            elif additional is False:
                raise ValueError(f"{path} contains unknown field: {key}")
            elif isinstance(additional, dict):
                _validate_schema(item, additional, root, f"{path}.{key}")
        if "propertyNames" in schema:
            for key in value:
                _validate_schema(key, schema["propertyNames"], root, f"{path} key {key}")


def _validated_schema(filename: str) -> tuple[dict[str, Any], str]:
    provenance = _object(SCHEMA_ROOT / "PROVENANCE.json")
    record = provenance["schemas"][filename]
    schema_path = SCHEMA_ROOT / filename
    digest = hashlib.sha256(schema_path.read_bytes()).hexdigest()
    if digest != record["sha256"]:
        raise ValueError(f"vendored schema hash mismatch: {filename}")
    schema = _object(schema_path)
    if schema.get("$id") != record["canonical_url"]:
        raise ValueError(f"vendored schema identifier mismatch: {filename}")
    return schema, digest


def validate(plugin_root: Path) -> dict[str, Any]:
    root = Path(plugin_root).resolve()
    manifest = _object(root / "plugin.json")
    plugin_schema, plugin_digest = _validated_schema("plugin.schema.json")
    _validate_schema(manifest, plugin_schema, plugin_schema, "plugin.json")

    mcp = _object(root / "mcp.json")
    mcp_schema, mcp_digest = _validated_schema("mcp.schema.json")
    _validate_schema(mcp, mcp_schema, mcp_schema, "mcp.json")
    if manifest.get("$schema") != PLUGIN_SCHEMA or mcp.get("$schema") != MCP_SCHEMA:
        raise ValueError("portable manifests do not target Agent Plugins 1.0.0")
    servers = mcp.get("mcpServers")
    assert isinstance(servers, dict)
    for server_name, server in servers.items():
        if not server_name:
            raise ValueError("MCP server entry is invalid")
        command = server.get("command")
        if not isinstance(command, str) or not command or any(character.isspace() for character in command):
            raise ValueError("stdio command must be one executable token")
        if "/" in command and not command.startswith("./"):
            raise ValueError("stdio command paths must be plugin-relative")
        if any(key in server.get("env", {}) for key in ("PLUGIN_ROOT", "PLUGIN_DATA")):
            raise ValueError("reserved Agent Plugins variables cannot be configured")

    skills = sorted((root / "skills").glob("*/SKILL.md"))
    if not skills:
        raise ValueError("the portable Library skill is missing")
    for skill in skills:
        text = skill.read_text(encoding="utf-8")
        if not text.startswith("---\n") or "\nname:" not in text or "\ndescription:" not in text:
            raise ValueError(f"invalid skill frontmatter: {skill}")
    return {
        "passed": True,
        "agent_plugins_version": "1.0.0",
        "schema_receipts": {
            "mcp.schema.json": mcp_digest,
            "plugin.schema.json": plugin_digest,
        },
        "servers": sorted(servers),
        "skills": [path.parent.name for path in skills],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "plugin_root",
        nargs="?",
        type=Path,
        default=Path(__file__).resolve().parents[1]
        / "src"
        / "atomizer_local_client"
        / "portable_plugin",
    )
    try:
        result = validate(parser.parse_args().plugin_root)
    except (OSError, ValueError, json.JSONDecodeError) as error:
        print(json.dumps({"passed": False, "error": str(error)}, sort_keys=True))
        return 1
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
