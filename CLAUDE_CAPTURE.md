# Claude capture coverage

Verified against Anthropic's official documentation on 2026-08-31:

- [Hooks reference](https://code.claude.com/docs/en/hooks): command hooks receive JSON on stdin. `UserPromptSubmit.prompt` is the submitted user text and `Stop.last_assistant_message` is the completed assistant text. The transcript can lag, so this integration does not read it. The same hook events run in terminal, IDE extensions, and Claude Code's Desktop Code surface. Local user settings do not carry into Claude Code web cloud sessions.
- [Claude Code Desktop](https://code.claude.com/docs/en/desktop): local Desktop Code sessions share `~/.claude/settings.json` hooks with the CLI.
- [Claude Code on the web](https://code.claude.com/docs/en/claude-code-on-the-web): the browser surface is hosted at `claude.ai/code`; browser-visible sessions may be captured by the existing extension when its conservative DOM selectors match.
- [Claude Desktop deep links](https://support.claude.com/en/articles/14729294-open-claude-desktop-with-a-link): ordinary chats and projects use `claude.ai/chat/{conversation-id}` and `claude.ai/project/{project-id}` identities.

Implemented coverage:

- Claude Code CLI, supported IDE integrations, and local Desktop Code sessions through the documented `UserPromptSubmit` and `Stop` hooks.
- User-visible `claude.ai` chat, project chat, and Claude Code web messages through the existing paired browser extension, with stable route rebinding and existing deduplication.

Claude Web uses the existing browser-capture permission and authenticated extension bridge; no bridge authentication or pairing behavior was changed.

Not claimed:

- Passive capture of ordinary Claude Desktop Chat. Anthropic documents shared hooks for Desktop Code, not a passive ordinary-chat hook.
- Claude mobile capture.
- Cloud hook installation from the local user settings file. Claude Code web uses repository or organization-managed hooks; the browser adapter is the local capture surface there.

The hook writes no decisions, prompt context, or message content to stdout. It does not read Claude transcript files, internal databases, cookies, browser credentials, or authentication tokens.
