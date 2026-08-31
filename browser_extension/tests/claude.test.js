"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const { routeIdentity } = require("../browsers/claude/conversation.js");
const { WebCapture } = require("../core/capture.js");

class Adapter {
  constructor() { this.composer = ""; this.messages = []; }
  onSubmitIntent(callback) { this.intent = callback; }
  onSubmit(callback) { this.submit = callback; }
  onDomChanged(callback) { this.changed = callback; }
  readComposer() { return this.composer; }
  listAssistantMessages() { return this.messages; }
}

function harness(pathname = "/new") {
  const adapter = new Adapter();
  const events = [];
  let now = 100;
  let route = routeIdentity({ pathname }, { title: "Claude" });
  const capture = new WebCapture({
    adapter, host: "claude_web", routeProvider: () => route,
    send: (event) => events.push(event), now: () => now,
    schedule: () => {}, captureInstanceReference: "fixture",
  });
  capture.start();
  return { adapter, capture, events, setRoute: (value) => { route = value; }, setNow: (value) => { now = value; } };
}

test("Claude routes expose stable chat, project, code, and trustworthy title associations", () => {
  const project = routeIdentity({ pathname: "/project/proj_1/chat/chat_1" }, { title: "Design review - Claude" });
  assert.equal(project.hostProjectReference, "project:proj_1");
  assert.equal(project.hostChatReference, "chat_1");
  assert.equal(project.chatDisplayName, "Design review");
  assert.equal(routeIdentity({ pathname: "/code/session_1" }, { title: "Claude Code" }).hostChatReference, "code:session_1");
});

test("Claude submit, assistant stabilization, dedupe, and new-chat rebinding reuse capture core", () => {
  const value = harness();
  value.adapter.composer = "hello Claude";
  value.adapter.submit("enter");
  assert.equal(value.events[0].host, "claude_web");
  const provisional = value.events[0].host_chat_reference;
  value.setRoute(routeIdentity({ pathname: "/chat/chat_1" }, { title: "Hello - Claude" }));
  value.capture.routeChanged();
  assert.equal(value.events[1].rebind_from_host_chat_reference, provisional);
  value.adapter.messages = [{ reference: "answer-1", content: "hello", visible: true, streaming: false }];
  value.capture.scanVisibleAssistantMessages();
  value.setNow(1000);
  value.capture.scanVisibleAssistantMessages();
  value.capture.scanVisibleAssistantMessages();
  assert.equal(value.events.filter((event) => event.role === "assistant").length, 1);
  assert.equal(value.events.at(-1).chat_display_name, "Hello");
});

test("manifests grant only the Claude origin and load the Claude adapter", () => {
  for (const browser of ["chromium", "firefox"]) {
    const manifest = JSON.parse(fs.readFileSync(path.join(__dirname, "../browsers", browser, "manifest.json")));
    assert.ok(manifest.host_permissions.includes("https://claude.ai/*"));
    assert.ok(!manifest.host_permissions.includes("<all_urls>"));
    const entry = manifest.content_scripts.find((item) => item.matches.includes("https://claude.ai/*"));
    assert.ok(entry.js.includes("browsers/claude/content_script.js"));
  }
});
