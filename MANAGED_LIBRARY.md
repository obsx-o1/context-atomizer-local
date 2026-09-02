# Managed Library access

Managed Library access is a narrow public boundary for a separately verified local manager. This repository does not contain or describe a private authority implementation.

The user explicitly selects one of three modes in the authenticated Library UI:

- `DIRECT_LOCAL`: the four read-only MCP tools are available to the direct frontier.
- `MANAGED_EXCLUSIVE`: direct MCP reads are denied; a verified manager may use the internal read path.
- `DISABLED`: both access paths are denied.

The mode is persisted outside the SQLite Library. A manager cannot select it, and lease loss or expiry cannot change it. Returning to direct access requires another explicit human UI action.

## Verified manager contract

The user pairs a trusted local manager with a one-time code from the authenticated Library UI. Pairing creates a dedicated OS-protected `managed-connector.bin` secret; it does not reuse the browser, capture, Library-session, or management credentials. Managed requests use purpose-separated HMAC-SHA256 authentication over a canonical method, path, nonce, timestamp, and body digest. Request nonces and lease capability identifiers have bounded replay state.

After the separate private authority boundary succeeds, the paired manager may present a short-lived `managed_library` lease. Local validates its HMAC and exact runtime-instance, runtime-build, manager-session, host-session, turn, scope, purpose, issue, expiry, and capability bindings. Successful activation creates a separate high-entropy, in-memory manager capability used for privileged reads and context completion. Expiry, request replay, lease replay, runtime restart, disconnect, scope drift, host-session drift, or turn drift fails closed. No MCP argument can claim manager privilege.

The HMAC establishes only that the request came from the locally paired manager. It does not independently prove private work authorization. The paired private adapter is responsible for minting a lease only after its existing verification boundary has passed; Local does not receive or reproduce that private verification policy.

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

The Library UI shows the current mode, pairing state, and managed-authority status; issues bounded one-time pairing codes; revokes the trusted manager; permits explicit mode changes; preserves project/chat/source inspection; revokes elected sources without deleting physical files; and exports canonical captured material as local JSON. Revocation closes managed access but never changes `MANAGED_EXCLUSIVE` into `DIRECT_LOCAL`. General chat delete/forget remains unavailable because no canonical retention operation exists for it in this version.
