# Supported clients

| Client | Development status |
| --- | --- |
| Codex hooks | Capture plus managed `UserPromptSubmit` additional context; opt-in, automated coverage |
| Claude Code hooks | Capture plus managed `UserPromptSubmit` additional context; opt-in, automated coverage |
| ChatGPT Web on Chromium browsers | Automated package and capture coverage |
| ChatGPT Web on Firefox | Experimental manifest; not release-validated |
| Codex CLI/desktop/IDE local Library tools | One read-only MCP server through the OpenAI plugin/direct mapping |
| Claude Code/Desktop local Library tools | The same read-only MCP server through the Anthropic plugin mapping |
| ChatGPT desktop native capture | Not supported |
| Browser managed reinjection | Not supported; capture remains available where listed above |
| Other coding assistants | Not supported unless they can consume the portable local MCP contract |

ChatGPT page structure is not a stable public API. Browser capture may require maintenance when the host UI changes.

Standalone `DIRECT_LOCAL` memory is tool based and never auto-injected. Native automatic context is attempted only in `MANAGED_EXCLUSIVE` mode while a separately verified, unexpired, scope-bound manager lease is active. Loss or expiry of that lease fails closed and does not reopen direct MCP access. The public package defines the generic boundary but does not contain a private authority verifier.
