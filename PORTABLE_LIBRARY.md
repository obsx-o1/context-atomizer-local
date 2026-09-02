# Portable local Library access

This package exposes the existing Context Atomizer Local Library as a bounded, read-only local memory source. It does not add a second database, retrieval engine, capture path, network listener, model call, or write operation.

## Pinned contracts

- **Agent Plugins 1.0.0.** The canonical package is `src/atomizer_local_client/portable_plugin/`. Its required root manifest is `plugin.json`, its MCP declaration is the separate root `mcp.json`, and its one Agent Skill is an immediate child of `skills/`. These are the fixed locations and schemas required by the [published Agent Plugins 1.0.0 specification](https://agent-plugins.org/specification), [plugin schema](https://agent-plugins.org/schemas/1.0.0/plugin.schema.json), and [MCP schema](https://agent-plugins.org/schemas/1.0.0/mcp.schema.json).
- **MCP 2026-07-28.** `atomizer-local-mcp` is one newline-delimited JSON-RPC stdio server. It implements the modern `server/discover`, `tools/list`, and `tools/call` lifecycle. Every request must carry `_meta.io.modelcontextprotocol/protocolVersion = "2026-07-28"` and an object-valued `_meta.io.modelcontextprotocol/clientCapabilities`; `clientInfo` is accepted when present. There is no `initialize`/`initialized` handshake. Standard output contains protocol messages only and diagnostics use standard error. This follows the official [2026-07-28 release](https://blog.modelcontextprotocol.io/posts/2026-07-28/), [stdio transport contract](https://modelcontextprotocol.io/specification/draft/basic/transports), and [2026-07-28 SDK migration contract](https://ts.sdk.modelcontextprotocol.io/v2/migration/support-2026-07-28).

## Current client support checked on 2026-08-31

| Client surface | Support conclusion |
| --- | --- |
| ChatGPT desktop / Codex desktop host | Supported locally through the OpenAI `.codex-plugin/plugin.json` plus direct-map `openai.mcp.json` shim. It launches the same stdio executable. |
| Codex CLI | Supported through the same local MCP configuration and executable. |
| Codex IDE extension | Supported through the same local MCP configuration and executable. |
| ChatGPT Web | **Not supported by this local integration.** ChatGPT Web consumes remote MCP-backed plugin tools and does not read local Codex configuration. No remote service or tunnel is added here. |
| Claude Code | Supported through the `.claude-plugin/plugin.json` plus Claude's wrapped root `.mcp.json` shim. |
| Claude Desktop | Supported as a local plugin MCP on the machine through the same Claude shim and stdio executable. |
| claude.ai / Claude mobile | **Not supported by this local stdio integration.** Remote connectors require an internet-reachable MCP endpoint, which this package intentionally does not create. |

OpenAI's current native plugin layout uses `.codex-plugin/plugin.json` and `.mcp.json`, while local ChatGPT desktop, Codex CLI, and the IDE share Codex-host MCP configuration. See the official [OpenAI plugin packaging guide](https://developers.openai.com/plugins/build/plugins) and [Codex MCP guide](https://developers.openai.com/codex/extend/mcp). The dot-prefixed files are thin mappings; the Agent Plugins package remains the canonical portable representation.

Anthropic's current native plugin layout uses `.claude-plugin/plugin.json`, shared root `skills/`, and a wrapped root `.mcp.json` containing `mcpServers`. See the official [Claude plugin reference](https://code.claude.com/docs/en/plugins-reference), [Claude Code MCP guide](https://code.claude.com/docs/en/mcp), and [local versus remote connector support matrix](https://support.claude.com/en/articles/11725091-when-to-use-desktop-and-web-connectors). MCPB `0.3` remains an official Claude Desktop extension packaging route, but no duplicate MCPB payload is produced because current Claude plugins already carry local MCP servers for Claude Desktop and Claude Code. The [MCPB manifest specification](https://github.com/modelcontextprotocol/mcpb/blob/main/MANIFEST.md) was checked for this decision.

These support conclusions describe the officially documented package and local-transport surfaces. The cited client documents do not promise one MCP protocol revision for every shipped binary. This server intentionally accepts only the project-pinned modern `2026-07-28` contract and does not silently fall back to an `initialize` handshake. Its wire behavior is schema-validated below; a particular client build must speak that revision to connect.

The portable directory is a canonical configuration-and-skill package, not a self-contained executable payload. Its bare `atomizer-local-mcp` command uses the host's executable search rules and requires the installed Context Atomizer Local wheel/runtime. Installing that wheel creates the console executable. The canonical `mcp.json`, OpenAI direct-map shim, and Anthropic wrapped `.mcp.json` shim resolve the exact same command; they do not embed Python or a second server.

The offline conformance gate validates both manifests directly against exact vendored copies of the published Agent Plugins 1.0.0 schemas. A 2026-08-31 live download comparison matched byte-for-byte: `plugin.schema.json` SHA-256 `0a4aad95ce337878ad38802ebf0daa3fde76abe3f65400c86bcbb1ec0b3ab883`; `mcp.schema.json` SHA-256 `6539175bfcdf43085855183e86da40ea94b166547a72b47ae9a0a390516d3acb`. Provenance is pinned in `tools/schemas/agent-plugins/1.0.0/PROVENANCE.json`.

The wire conformance gate drives actual `server/discover`, `tools/list`, and `tools/call` frames through the server and validates both sides against the official immutable [`2026-07-28` tagged schema](https://github.com/modelcontextprotocol/modelcontextprotocol/blob/2026-07-28/schema/2026-07-28/schema.json), SHA-256 `ef70b61f99b6d2e5e3b46863822eab08dff6a45bedc7a08914e0e5b133f40203`. It also checks complete `resultType`, private cache fields, result server identity, notification silence, and rejection of the removed `initialize` method. The receipt is `tools/mcp_2026_wire_receipt.json`.

## Exposed read-only tools

- `search_library(query, project?, limit?)`
- `get_library_item(id)`
- `recent_library_context(project?, limit?)`
- `list_library_projects()`

Search calls the existing lexical retriever, local deterministic vector retriever, RRF fuser, and deterministic reranker. Results are capped at eight, item content is clipped, total service output is bounded, and provenance includes stable evidence/source IDs, source type, project, chat reference when available, and timestamp.

No tool accepts SQL, filesystem paths, write data, an administrator flag, a caller role, or a bypass parameter.

## Direct access boundary

`DirectLibraryAccessMode` has exactly three values:

- `DIRECT_LOCAL`: the fixed `DIRECT_FRONTIER` MCP caller may read bounded Library content.
- `MANAGED_EXCLUSIVE`: the frontier receives only `Direct Library access is delegated to a trusted manager for this session.`
- `DISABLED`: the frontier receives only a disabled status.

`ManagedAuthorityProvider` is a public content-free seam for a separately verified manager implementation. The internal `TRUSTED_MANAGER` caller is accepted only while that provider reports verified, unexpired authority. It is never an MCP argument. The human Library UI does not consult this gate and remains available in every mode.

The human-selected mode is persisted outside SQLite and loaded by normal MCP launches. In managed-exclusive mode the existing authenticated loopback management surface can host a generic verifier, a runtime/scope/expiry-bound lease, a high-entropy manager capability, the internal `LibraryQueryService` reader, and a bounded turn exchange. The public runtime configures a rejecting verifier by default; no payload can self-assert trusted status.

Codex and Claude Code `UserPromptSubmit` hooks capture the original prompt first and then, only under active managed-exclusive authority, request bounded context. They return the hosts' documented `hookSpecificOutput.additionalContext` shape. Standalone direct mode remains tool based. Browser managed reinjection is not supported because the browser capture surface has no equally safe pre-prompt context separation.

This boundary governs supported integration surfaces. It does not try to prevent arbitrary same-user malware or an unrestricted coding agent from opening the SQLite file independently.

## Licensing

This work adds no runtime dependency. The server uses the Python standard library plus existing project modules. Original portable-package code is covered by the repository's MPL-2.0 license. The two vendored Agent Plugins JSON Schemas are software material under Apache-2.0 according to the specification repository's [license allocation](https://github.com/agentplugins/agent-plugins-spec/blob/main/LICENSE.md); their source URLs and hashes are recorded beside them.
