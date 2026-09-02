# Security

Context Atomizer Local is a local-only application. Its two loopback HTTP surfaces use separate, narrow authorities. The managed Library operations reuse the existing authenticated capture/management listener; they do not add a listener.

## Managed Library boundary

The access mode is stored in a small policy file outside SQLite. A missing file preserves `DIRECT_LOCAL`; an unreadable or invalid file fails closed. Selecting `MANAGED_EXCLUSIVE` immediately denies direct frontier MCP reads. Lease expiry, verifier failure, manager disconnect, runtime restart, and request timeout never change that policy or reopen direct access.

Managed activation is accepted only through a configured `ManagedAssertionVerifier`. The public default rejects every assertion. A verified result is bound to the exact runtime build, an opaque session reference, explicit Library scope references, and an expiry. Activation returns a high-entropy in-memory manager capability. Subsequent privileged reads and turn completion require that capability; public MCP schemas expose no manager, trusted, caller, or bypass field.

Hook requests bind the original host, host session, host turn, and hashed workspace scope. Context completion must match all bindings, must arrive before expiry, is size bounded, and is single-use. Prompt/context bodies are not included in request logs or diagnostics. These controls do not turn the generic public interface into proof of any particular private authority system.

## Library boundary

Library binds to `127.0.0.1` and accepts only the exact `Host` value `127.0.0.1:<actual-library-port>`. Content routes require a runtime-scoped browser session. The manager asks the runtime for a cryptographically random one-time launch capability, opens `/?launch=<capability>`, and Library consumes it once before setting an `HttpOnly`, `SameSite=Strict`, path-scoped session cookie and redirecting to a clean URL. Launch capabilities expire after 60 seconds; sessions expire after eight hours or when the runtime ends.

State-changing requests additionally require the exact Library `Origin`, a same-origin or absent `Sec-Fetch-Site` value, and the session's CSRF token. Cross-site fetch contexts are rejected. The unauthenticated `/health` response is deliberately limited to service availability and contains no Library content, permissions, CSRF token, or detailed integration state.

## Browser extension boundary

The capture bridge binds only to fixed endpoint `127.0.0.1:43117`. If that port is occupied, capture startup fails closed; there is no alternative-port probing or fallback.

Pairing is an explicit copy/paste action initiated in authenticated Library. The code is generated with a cryptographically secure random source, expires after five minutes, is single use, remains in runtime memory, and is limited to five failed attempts per rolling minute. Successful pairing establishes a high-entropy extension secret in browser-extension storage and Windows user-scoped protected runtime storage. Revocation invalidates the runtime secret and requires explicit re-pairing.

Before sending sensitive content, the extension submits a fresh challenge and verifies the runtime's HMAC-SHA-256 proof over this exact ASCII material:

```text
context-atomizer-local/runtime-proof/v1
1
<challenge_nonce>
43117
```

Every capture request has a fresh nonce and timestamp, the SHA-256 hash of the exact body, and an HMAC-SHA-256 over this exact ASCII material:

```text
context-atomizer-local/capture-request/v1
1
<HTTP method>
<operation path>
<request nonce>
<Unix timestamp>
<lowercase body SHA-256>
```

The HMAC key is the ASCII byte encoding of the stored paired-secret string. The runtime verifies the MAC and body hash before parsing content. It accepts each nonce once within a 120-second freshness window, permits at most 30 seconds of future clock skew, and keeps at most 4,096 replay entries with deterministic expiry. The persistent paired secret does not rotate per request.

The extension secret authorizes capture, title observations, and minimal runtime proof only. It cannot stop the runtime, create a Library session, change permissions, authorize sources, pair another extension, or perform lifecycle operations. Those actions use a separate Windows user-protected management credential that is never given to the extension or Library browser session.

## Threat boundary

These controls are intended to resist hostile web content, a fake loopback service that lacks the paired secret, unrelated local processes lacking user-owned secrets, and another local user lacking the owning user's protected material. They do not claim protection from arbitrary malware already executing with the full authority of the same Windows user.

Codex hook ownership is determined from the parsed executable identity. Unrelated hooks are preserved, owned entries are removed, and ambiguous true Atomizer-like commands are left untouched and reported without preventing core uninstall cleanup.

Technical-preview Windows installers may be unsigned and are published with SHA-256 checksums. Broad commercial Windows releases require Authenticode signing. An unsigned technical-preview installer is not, by itself, evidence that its contents are insecure; verify the published checksum and source provenance. macOS development archives are likewise unsigned and unnotarized.

## Reporting vulnerabilities

When this repository is published, private vulnerability reports are accepted through GitHub's **Security → Report a vulnerability** flow. Do not disclose suspected vulnerabilities through public issues before coordination. This setting is enabled only during final public-repository publication and is not claimed to be active on the current private candidate.
