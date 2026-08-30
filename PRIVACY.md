# Privacy

Context Atomizer Local stores its Library in a SQLite database owned by the current Windows user. It does not synchronize Library content to a remote service, call a model provider, or send analytics remotely.

## What capture stores

When Codex capture is enabled, the `UserPromptSubmit` hook stores the prompt text and the `Stop` hook stores `last_assistant_message`. Supported content is stored locally verbatim. The raw working-directory path is not stored; the client keeps a local hash for project association.

When ChatGPT Web capture is enabled, the browser extension reads visible conversation messages. It also observes visible sidebar conversation titles and project associations so an existing local chat can receive its human-readable title. Captured content and retained title/project observations are sent only to the paired loopback runtime.

The current ChatGPT Web surface does not provide a stable, locally verified signal that reliably identifies Temporary Chat. When ChatGPT Web capture is enabled, a Temporary Chat may therefore be captured into the user's local Library; the product does not pretend otherwise or rely on fragile inference. Users who do not want a Temporary Chat retained locally should disable or pause ChatGPT Web capture before using it.

## Browser pairing

The extension is paired explicitly from an authenticated Library session with a high-entropy, short-lived, one-time code. Successful pairing establishes a separate local extension secret. That secret is stored in browser-extension storage and in Windows user-scoped protected storage for the runtime, and is used only to authenticate local capture traffic. It is not a management credential and is not sent during ordinary capture requests.

Revoking the pairing in Library deletes the runtime copy and future browser capture fails until the user pairs again. Malware already running with the full authority of the same Windows user may be able to read browser-profile state; this design does not claim to defend against that condition.

## Disable, indexes, and document history

Disabling an integration stops future capture for that integration. It does not delete already stored chats, documents, indexes, or search results.

The runtime builds local lexical and derived indexes, including semantic vectors, entities, claims, temporal state, contradictions, and verification state. These remain in the local SQLite Library.

For each elected document, the current version and at most the 10 newest historical revisions are retained. Older revisions are deterministically pruned, and retained revisions remain available to local derived indexing. Revoking the source deletes its retained history and reconciles derived rows away. User-configurable revision retention is planned for v1.1.

## Uninstall

Uninstall removes runtime startup registration, management and extension credentials, product configuration and permissions, runtime state and lock files, the Start Menu shortcut, runtime logs, `capture-errors.log`, application binaries, and Atomizer-owned Codex hooks that can be identified safely. Ambiguous Codex hooks are preserved and reported as partial cleanup; they do not block core product cleanup.

The SQLite Library database is preserved unconditionally by the product uninstaller. There is no uninstall-time choice that deletes Library data. Deleting that retained database is a separate user action.
