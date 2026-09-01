"""Escaped, accessible server-rendered HTML for the local Library."""

from __future__ import annotations

from html import escape
from typing import Any
from urllib.parse import urlencode


_STYLE = """
:root { color-scheme: light dark; font-family: system-ui, sans-serif; line-height: 1.5; }
body { margin: 0; background: #101318; color: #edf1f7; }
a { color: #8fc8ff; }
header { position: sticky; top: 0; background: #171c24; border-bottom: 1px solid #303846; padding: .8rem 1.2rem; z-index: 2; }
header nav { display: flex; gap: 1rem; align-items: center; flex-wrap: wrap; }
header form { margin-left: auto; display: flex; gap: .4rem; }
main { max-width: 1050px; margin: 0 auto; padding: 1.2rem; }
.grid { display: grid; gap: 1rem; grid-template-columns: repeat(auto-fit, minmax(250px, 1fr)); }
.card, .message, .panel { background: #191f29; border: 1px solid #303846; border-radius: .65rem; padding: 1rem; }
.card h2, .card h3 { margin-top: 0; }
.meta { color: #aeb8c8; font-size: .9rem; }
.pill { display: inline-block; padding: .1rem .5rem; border-radius: 999px; background: #2d3747; font-size: .82rem; }
.message { margin: .8rem 0; white-space: pre-wrap; }
.message.user { border-left: .35rem solid #63b3ff; }
.message.assistant { border-left: .35rem solid #a58aff; }
.message .role { font-weight: 700; text-transform: capitalize; }
input, select, button { font: inherit; padding: .55rem; border: 1px solid #536079; border-radius: .35rem; }
input[type=text] { min-width: 15rem; background: #0f1319; color: inherit; }
button { cursor: pointer; background: #276da8; color: white; }
button.danger { background: #8d3240; }
form.stack { display: grid; gap: .65rem; }
.status { background: #173924; border: 1px solid #2f8150; padding: .8rem; border-radius: .5rem; }
.error { background: #4a2026; border-color: #a54a58; }
pre.document { white-space: pre-wrap; overflow-wrap: anywhere; background: #0d1117; padding: 1rem; border-radius: .5rem; }
code.path { overflow-wrap: anywhere; }
.empty { color: #aeb8c8; font-style: italic; }
@media (max-width: 650px) { header form { margin-left: 0; width: 100%; } header form input { flex: 1; min-width: 0; } }
"""


def _host_label(host: str) -> str:
    return {
        "codex": "Codex",
        "chatgpt_web": "ChatGPT Web",
        "claude_code": "Claude Code",
        "claude_web": "Claude Web",
        "local": "Local",
    }.get(host, host)


def _url(path: str, **parameters: str) -> str:
    return path + ("?" + urlencode(parameters) if parameters else "")


def _page(title: str, content: str, *, search_query: str = "") -> bytes:
    return (
        "<!doctype html><html lang='en'><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width,initial-scale=1'>"
        f"<title>{escape(title)} · Context Atomizer Library</title><style>{_STYLE}</style>"
        "</head><body><header><nav><a href='/'><strong>Local Library</strong></a>"
        "<a href='/'>Projects</a>"
        "<a href='/permissions'>Permissions &amp; sources</a>"
        "<a href='/status'>Status</a>"
        f"<form action='/search' method='get' role='search'><label class='meta' for='global-search'>Search</label>"
        f"<input id='global-search' name='q' type='text' value='{escape(search_query, quote=True)}' required>"
        "<button type='submit'>Search</button></form></nav></header>"
        f"<main>{content}</main></body></html>"
    ).encode("utf-8")


def render_home(projects: list[dict[str, Any]], *, status: str | None = None) -> bytes:
    cards = []
    for project in projects:
        project_url = _url("/project", project_id=str(project["project_id"]))
        unassigned = project["display_name"] == "Unassigned"
        cards.append(
            "<article class='card'>"
            f"<h2><a href='{escape(project_url, quote=True)}'>{escape(str(project['display_label']))}</a></h2>"
            f"<p><span class='pill'>{escape(_host_label(str(project['host'])))}</span>"
            + (" <span class='pill'>No deterministic Project</span>" if unassigned else "")
            + "</p>"
            f"<p class='meta'>{project['chat_count']} chats · {project['document_count']} documents · {project['source_count']} authorized sources</p>"
            "</article>"
        )
    status_html = f"<p class='status'>{escape(status)}</p>" if status else ""
    content = (
        "<h1>Library</h1><p>Your local source-of-truth Projects, Chats, Messages, and automatically maintained Documents.</p>"
        + status_html
        + ("<section class='grid'>" + "".join(cards) + "</section>" if cards else "<p class='empty'>No Projects yet.</p>")
    )
    return _page("Library", content)


def render_runtime_status(health: dict[str, Any]) -> bytes:
    runtime = health["runtime"]
    database = health["database"]
    maintenance = health["source_maintenance"]
    derived = health["derived_state"]
    extension = health["extension"]
    restart = "Restart required" if runtime["restart_required"] else "Current build"
    browser = "Connected" if extension["state"] == "connected" else "Not seen yet"
    last_seen = extension.get("last_seen_at") or "Not available"
    content = (
        "<h1>Atomizer Local status</h1><section class='grid'>"
        "<article class='card'><h2>Runtime</h2><p><span class='pill'>Running</span></p>"
        f"<p class='meta'>Version {escape(str(runtime['runtime_version']))} · {escape(restart)}</p></article>"
        "<article class='card'><h2>Library</h2>"
        f"<p><span class='pill'>{escape(str(database['state']).capitalize())}</span></p>"
        f"<p class='meta'>Integrity {escape(str(database['integrity_check']))}</p></article>"
        "<article class='card'><h2>Sources</h2>"
        f"<p><span class='pill'>{escape(str(maintenance['state']).capitalize())}</span></p>"
        f"<p class='meta'>Last cycle errors: {escape(str(maintenance['last_cycle_error_count']))}</p></article>"
        "<article class='card'><h2>Enrichment</h2>"
        f"<p><span class='pill'>{escape(str(derived['convergence_state']).capitalize())}</span></p>"
        f"<p class='meta'>Pending: {escape(str(derived['pending_count']))} · "
        f"Backend {escape(str(derived['backend_version']))}</p></article>"
        "<article class='card'><h2>Browser</h2>"
        f"<p><span class='pill'>{escape(browser)}</span></p>"
        f"<p class='meta'>Last seen: {escape(str(last_seen))}</p></article>"
        "</section>"
    )
    return _page("Status", content)


def render_permissions(
    integrations: dict[str, Any],
    extension: dict[str, Any],
    csrf_token: str,
    *,
    status: str | None = None,
    pairing_code: str | None = None,
) -> bytes:
    def integration_card(
        key: str, label: str, secondary_label: str, secondary_value: str
    ) -> str:
        permission = integrations[key]
        enabled = bool(permission.enabled)
        action = "Disable" if enabled else "Enable"
        new_value = "no" if enabled else "yes"
        return (
            "<article class='card'>"
            f"<h2>{escape(label)}</h2>"
            f"<p><span class='pill'>{'Enabled' if enabled else 'Disabled'}</span></p>"
            f"<p class='meta'>{escape(secondary_label)}: {escape(secondary_value)}</p>"
            "<form method='post' action='/integration/set'>"
            f"<input type='hidden' name='csrf_token' value='{escape(csrf_token, quote=True)}'>"
            f"<input type='hidden' name='integration' value='{escape(key, quote=True)}'>"
            f"<input type='hidden' name='enabled' value='{new_value}'>"
            f"<button type='submit'>{action}</button></form>"
            "<p class='meta'>Disabling stops future capture and keeps existing Library history and search results.</p>"
            "</article>"
        )

    paired = bool(extension.get("paired"))
    browser = "Paired" if paired else "Not paired"
    codex_hook = "Installed" if integrations["codex"].installed else "Not installed"
    claude_hook = (
        "Installed" if integrations["claude_code"].installed else "Not installed"
    )
    status_html = f"<p class='status'>{escape(status)}</p>" if status else ""
    pairing_code_html = (
        "<p>Paste this one-time code into the extension options page within five minutes:</p>"
        f"<p><code>{escape(pairing_code)}</code></p>"
        if pairing_code
        else ""
    )
    pairing_controls = (
        "<section class='panel'><h2>Browser extension pairing</h2>"
        f"<p><span class='pill'>{browser}</span></p>"
        + pairing_code_html
        + "<div class='grid'><form method='post' action='/extension/pairing-code'>"
        f"<input type='hidden' name='csrf_token' value='{escape(csrf_token, quote=True)}'>"
        "<button type='submit'>Create one-time pairing code</button></form>"
        "<form method='post' action='/extension/revoke'>"
        f"<input type='hidden' name='csrf_token' value='{escape(csrf_token, quote=True)}'>"
        "<button class='danger' type='submit'>Revoke browser pairing</button></form></div>"
        "<p class='meta'>The paired secret is never displayed here. Revocation stops future browser capture until you pair again.</p></section>"
    )
    content = (
        "<h1>Permissions &amp; sources</h1>"
        "<p>Permissions stay on this computer. Enable an integration once to capture supported future activity automatically. The ChatGPT Web browser permission also governs Claude Web capture through the same paired extension.</p>"
        + status_html
        + "<section class='grid'>"
        + integration_card("chatgpt_web", "ChatGPT Web", "Browser", browser)
        + integration_card("codex", "Codex", "Hook", codex_hook)
        + integration_card("claude_code", "Claude Code", "Hook", claude_hook)
        + "</section>"
        + pairing_controls
        + "<section class='panel'><h2>Local sources</h2>"
        "<p>Open a Project to manage its Authorized folders and Authorized files. Add a folder once; Atomizer watches eligible .txt, .md, and .markdown files inside it automatically.</p>"
        "<p>Removing a source never deletes the physical file or folder.</p></section>"
    )
    return _page("Permissions & sources", content)


def render_project(project: dict[str, Any], csrf_token: str, *, status: str | None = None) -> bytes:
    project_id = str(project["project_id"])
    automatic_maintenance = bool(project.get("automatic_maintenance", True))
    chats = []
    for chat in project["chats"]:
        chat_url = _url("/chat", chat_id=str(chat["chat_id"]))
        title_source = (
            "Host title"
            if chat["display_label_source"] == "stored-title"
            else "Local fallback"
        )
        chats.append(
            "<article class='card'>"
            f"<h3><a href='{escape(chat_url, quote=True)}'>{escape(str(chat['display_label']))}</a></h3>"
            f"<p><span class='pill'>{escape(_host_label(str(chat['host'])))}</span> "
            f"<span class='pill'>{title_source}</span></p>"
            f"<p class='meta'>{chat['message_count']} messages · Updated {escape(str(chat['updated_at_display']))}</p>"
            "</article>"
        )
    documents = []
    for document in project["documents"]:
        document_url = _url("/document", document_id=str(document["document_id"]))
        documents.append(
            "<article class='card'>"
            f"<h3><a href='{escape(document_url, quote=True)}'>{escape(str(document['display_name']))}</a></h3>"
            f"<p><span class='pill'>{escape(str(document['document_type']))}</span></p>"
            f"<p class='meta'>{document['file_size']} bytes · Updated {escape(str(document['updated_at_display']))}</p>"
            "</article>"
        )
    folders = []
    files = []
    for source in project["sources"]:
        availability = "Available" if source["available"] else "Missing"
        maintenance = "Watching automatically" if automatic_maintenance else "Automatic watching disabled"
        error = source.get("error")
        error_status = f"Error: {escape(str(error))}" if error else "No error"
        card = (
            "<article class='card'>"
            f"<h3>{escape(str(source['display_name']))}</h3>"
            f"<p><span class='pill'>{escape(str(source['source_kind']).title())}</span> "
            "<span class='pill'>Authorized</span> "
            f"<span class='pill'>{escape(maintenance)}</span> "
            f"<span class='pill'>{availability}</span> "
            f"<span class='pill'>{error_status}</span></p>"
            f"<p class='meta'><code class='path'>{escape(str(source['local_source_reference']))}</code></p>"
            f"<p class='meta'>{source['document_count']} active documents · Last synchronized {escape(str(source['last_synced_at_display']))}</p>"
            "<div class='grid'>"
            "<form method='post' action='/source/rescan'>"
            f"<input type='hidden' name='csrf_token' value='{escape(csrf_token, quote=True)}'>"
            f"<input type='hidden' name='project_id' value='{escape(project_id, quote=True)}'>"
            f"<input type='hidden' name='source_id' value='{escape(str(source['source_id']), quote=True)}'>"
            "<button type='submit'>Advanced: Rescan now</button></form>"
            "<form method='post' action='/source/revoke'>"
            f"<input type='hidden' name='csrf_token' value='{escape(csrf_token, quote=True)}'>"
            f"<input type='hidden' name='project_id' value='{escape(project_id, quote=True)}'>"
            f"<input type='hidden' name='source_id' value='{escape(str(source['source_id']), quote=True)}'>"
            "<label><input type='checkbox' name='confirm' value='yes' required> Confirm remove</label> "
            f"<button class='danger' type='submit'>Remove {'folder' if source['source_kind'] == 'DIRECTORY' else 'file'}</button></form></div>"
            "<p class='meta'>Removing stops automatic watching and removes active Library/search records no longer covered elsewhere. Physical content is never deleted.</p>"
            "</article>"
        )
        (folders if source["source_kind"] == "DIRECTORY" else files).append(card)
    status_html = f"<p class='status'>{escape(status)}</p>" if status else ""
    scoped_search = (
        "<form action='/search' method='get' class='stack' role='search'>"
        f"<input type='hidden' name='project_id' value='{escape(project_id, quote=True)}'>"
        "<label for='project-search'>Search this Project</label>"
        "<input id='project-search' name='q' type='text' required>"
        "<button type='submit'>Search Project</button></form>"
    )
    add_folder = (
        "<form method='post' action='/source/authorize' class='stack'>"
        f"<input type='hidden' name='csrf_token' value='{escape(csrf_token, quote=True)}'>"
        f"<input type='hidden' name='project_id' value='{escape(project_id, quote=True)}'>"
        "<input type='hidden' name='source_kind' value='DIRECTORY'>"
        "<label for='folder-path'>Folder path</label>"
        "<input id='folder-path' name='source_path' type='text' required>"
        "<button type='submit'>+ Add folder</button></form>"
    )
    add_file = (
        "<form method='post' action='/source/authorize' class='stack'>"
        f"<input type='hidden' name='csrf_token' value='{escape(csrf_token, quote=True)}'>"
        f"<input type='hidden' name='project_id' value='{escape(project_id, quote=True)}'>"
        "<input type='hidden' name='source_kind' value='FILE'>"
        "<label for='file-path'>Individual file path</label>"
        "<input id='file-path' name='source_path' type='text' required>"
        "<button type='submit'>+ Add file</button></form>"
    )
    content = (
        f"<p><a href='/'>← Library</a></p><h1>{escape(str(project['display_label']))}</h1>"
        f"<p><span class='pill'>{escape(_host_label(str(project['host'])))}</span></p>{status_html}"
        f"<section class='panel'><h2>Search</h2>{scoped_search}</section>"
        "<h2>Chats</h2>" + ("<section class='grid'>" + "".join(chats) + "</section>" if chats else "<p class='empty'>No Chats.</p>")
        + "<h2>Documents</h2>" + ("<section class='grid'>" + "".join(documents) + "</section>" if documents else "<p class='empty'>No active Documents.</p>")
        + "<h2>Authorized sources</h2><p>Add a folder once for automatic maintenance; individual files are optional.</p>"
        + "<h3>Authorized folders</h3>" + ("<section class='grid'>" + "".join(folders) + "</section>" if folders else "<p class='empty'>No authorized folders.</p>")
        + f"<section class='panel'><h2>Add folder</h2><p>Add once; eligible files inside are kept current automatically.</p>{add_folder}</section>"
        + "<h2>Authorized files</h2>" + ("<section class='grid'>" + "".join(files) + "</section>" if files else "<p class='empty'>No individually authorized files.</p>")
        + f"<section class='panel'><h2>Add individual file</h2><p>Optional secondary control for one supported file.</p>{add_file}</section>"
    )
    return _page(str(project["display_label"]), content)


def render_chat(chat: dict[str, Any]) -> bytes:
    messages = []
    for message in chat["messages"]:
        role = str(message["role"])
        messages.append(
            f"<article class='message {escape(role)}' id='message-{escape(str(message['message_id']), quote=True)}'>"
            f"<div><span class='role'>{escape(role)}</span> <span class='meta'>#{message['sequence_number']} · {escape(str(message['captured_at_display']))}</span></div>"
            f"<div>{escape(str(message['content']))}</div></article>"
        )
    project_url = _url("/project", project_id=str(chat["project_id"]))
    title_source = (
        "Host title"
        if chat["display_label_source"] == "stored-title"
        else "Local fallback"
    )
    content = (
        f"<p><a href='{escape(project_url, quote=True)}'>← {escape(str(chat['project_display_label']))}</a></p>"
        f"<h1>{escape(str(chat['display_label']))}</h1><p><span class='pill'>{escape(_host_label(str(chat['host'])))}</span> "
        f"<span class='pill'>{title_source}</span></p>"
        + ("".join(messages) if messages else "<p class='empty'>No Messages.</p>")
    )
    return _page(str(chat["display_label"]), content)


def render_document(document: dict[str, Any]) -> bytes:
    project_url = _url("/project", project_id=str(document["project_id"]))
    source_status = "".join(
        f"<li>{escape(str(source['display_name']))} · {escape(str(source['source_kind']).title())} · Last synced {escape(str(source['last_synced_at_display']))}</li>"
        for source in document["sources"]
    )
    content = (
        f"<p><a href='{escape(project_url, quote=True)}'>← {escape(str(document['project_display_label']))}</a></p>"
        f"<h1>{escape(str(document['display_name']))}</h1>"
        f"<p><span class='pill'>{escape(str(document['document_type']))}</span> "
        f"<span class='meta'>{document['file_size']} bytes · Updated {escape(str(document['updated_at_display']))}</span></p>"
        f"<pre class='document'>{escape(str(document['text_content']))}</pre>"
        f"<section class='panel'><h2>Active source status</h2><ul>{source_status}</ul></section>"
    )
    return _page(str(document["display_name"]), content)


def render_search(
    query: str,
    results: list[dict[str, Any]],
    *,
    project: dict[str, Any] | None = None,
    error: str | None = None,
) -> bytes:
    items = []
    for result in results:
        content = str(result["content"])
        snippet = content[:400] + ("…" if len(content) > 400 else "")
        role = f" · {escape(str(result['role']).title())}" if result.get("role") else ""
        items.append(
            "<article class='card'>"
            f"<h2><a href='{escape(str(result['destination']), quote=True)}'>{escape(str(result['source_name']))}</a></h2>"
            f"<p><span class='pill'>{escape(str(result['source_type']))}</span>{role}</p>"
            f"<p class='meta'>{escape(str(result['project_display_label']))}</p>"
            f"<p>{escape(snippet)}</p></article>"
        )
    scope = f" in {escape(str(project['display_label']))}" if project else ""
    error_html = f"<p class='status error'>{escape(error)}</p>" if error else ""
    content = (
        f"<h1>Search results{scope}</h1><p>Query: <strong>{escape(query)}</strong></p>{error_html}"
        + ("<section class='grid'>" + "".join(items) + "</section>" if items else "<p class='empty'>No matching local sources.</p>")
    )
    return _page("Search", content, search_query=query)


def render_error(status: int, message: str) -> bytes:
    return _page(f"Error {status}", f"<h1>Error {status}</h1><p class='status error'>{escape(message)}</p><p><a href='/'>Return to Library</a></p>")
