# Managed Library access

Managed Library access is a narrow public boundary for a separately verified local manager. This repository does not contain or describe a private authority implementation.

The user explicitly selects one of three modes in the authenticated Library UI:

- `DIRECT_LOCAL`: the four read-only MCP tools are available to the direct frontier.
- `MANAGED_EXCLUSIVE`: direct MCP reads are denied; a verified manager may use the internal read path.
- `DISABLED`: both access paths are denied.

The mode is persisted outside the SQLite Library. A manager cannot select it, and lease loss or expiry cannot change it. Returning to direct access requires another explicit human UI action.

## Verified manager contract

A configured verifier may return a generic managed session bound to the current runtime build, an opaque session reference, explicit workspace scopes, and a short expiry. The public default verifier rejects every assertion. Successful activation creates a separate high-entropy, in-memory manager capability used for privileged reads and context completion. No MCP argument can claim manager privilege.

Privileged reads route only to the existing `LibraryQueryService` operations. They do not add SQL, writes, a second retrieval engine, or vendor-specific retrieval.

## Native context

For Codex and Claude Code, the `UserPromptSubmit` flow is:

1. capture the original human prompt through the existing ingestion path;
2. if managed-exclusive authority is active for the hashed workspace scope, request context for that exact host session and turn;
3. accept one bounded, fresh, matching result;
4. return it as the host's native `additionalContext` value.

Timeout, mismatch, replay, disconnect, verifier failure, and runtime restart inject nothing and do not fall back to direct MCP access. Prompt/context exchange bodies remain in memory and are omitted from diagnostics.

Browser managed reinjection is not supported. Existing ChatGPT Web and Claude Web capture behavior is preserved.

## Human controls

The Library UI shows the current mode and managed-authority status, permits explicit mode changes, preserves project/chat/source inspection, revokes elected sources without deleting physical files, and exports canonical captured material as local JSON. General chat delete/forget remains unavailable because no canonical retention operation exists for it in this version.
