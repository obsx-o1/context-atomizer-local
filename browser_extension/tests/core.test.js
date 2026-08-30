"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const { ChatGPTCapture } = require("../core/capture.js");
const {
  canonicalProjectReference,
  isTemporaryNewChatReference,
  projectHomeDisplayName,
  routeIdentity,
} = require("../core/conversation.js");
const { DomCaptureAdapter } = require("../core/selectors.js");

const LIVE_PROJECT = "g-p-00000000000000001111111111111111";
const LIVE_PROJECT_SLUGGED = LIVE_PROJECT + "-atomizer-project-test";
const LIVE_CHAT_A = "6a771727-f250-83ea-ae5b-0e14abe188c6";
const LIVE_CHAT_B = "6a77178f-64a8-83ea-bdbc-3de93959a239";

class FakeAdapter {
  constructor() {
    this.composer = "";
    this.messages = [];
    this.submitIntent = null;
    this.submitCallback = null;
    this.domChanged = null;
  }

  readComposer() { return this.composer; }
  onSubmitIntent(callback) { this.submitIntent = callback; }
  onSubmit(callback) { this.submitCallback = callback; }
  onDomChanged(callback) { this.domChanged = callback; }
  listAssistantMessages() { return this.messages; }
  beginSubmit(kind) { if (this.submitIntent) this.submitIntent(kind); }
  finishSubmit(kind) { if (this.submitCallback) this.submitCallback(kind); }
  submit(kind) {
    this.beginSubmit(kind);
    this.finishSubmit(kind);
  }
}

function fixture(captureInstanceReference = "fixture-instance") {
  const adapter = new FakeAdapter();
  const events = [];
  const scheduled = [];
  let now = 1000;
  let route = {
    hostChatReference: "chat-one",
    hostProjectReference: null,
    projectDisplayName: null,
    chatDisplayName: "Visible Chat",
  };
  const capture = new ChatGPTCapture({
    adapter,
    routeProvider: () => route,
    send: (event) => events.push(event),
    now: () => now,
    schedule: (callback) => scheduled.push(callback),
    stableMilliseconds: 500,
    captureInstanceReference,
    pendingRoutePollLimit: 4,
  });
  capture.start();
  return {
    adapter,
    capture,
    events,
    setNow(value) { now = value; },
    setRoute(value) { route = value; },
    flush() { while (scheduled.length) scheduled.shift()(); },
  };
}

test("submission intent is observed on window capture before message capture", () => {
  const windowListeners = new Map();
  const documentListeners = new Map();
  const windowValue = {
    addEventListener(type, callback, capture) {
      windowListeners.set(type, { callback, capture });
    },
    removeEventListener() {},
  };
  const documentValue = {
    addEventListener(type, callback, capture) {
      documentListeners.set(type, { callback, capture });
    },
    removeEventListener() {},
  };
  const adapter = new DomCaptureAdapter(documentValue, windowValue);
  const observed = [];
  adapter.onSubmitIntent((kind) => observed.push("intent:" + kind));
  adapter.onSubmit((kind) => observed.push("capture:" + kind));

  assert.equal(windowListeners.get("pointerdown").capture, true);
  assert.equal(windowListeners.get("keydown").capture, true);
  assert.equal(windowListeners.get("submit").capture, true);
  assert.equal(documentListeners.get("click").capture, true);

  const sendTarget = { closest: (selector) => selector.includes("send-button") ? sendTarget : null };
  windowListeners.get("pointerdown").callback({ target: sendTarget });
  documentListeners.get("click").callback({ target: sendTarget });

  const composerTarget = { closest: (selector) => selector.includes("prompt-textarea") ? composerTarget : null };
  const enter = { key: "Enter", shiftKey: false, isComposing: false, target: composerTarget };
  windowListeners.get("keydown").callback(enter);
  documentListeners.get("keydown").callback(enter);

  assert.deepEqual(observed, [
    "intent:click", "capture:click", "intent:enter", "capture:enter",
  ]);
});

test("user click and Enter observation emits one normalized event", () => {
  const value = fixture();
  value.adapter.composer = "visible user prompt";
  value.adapter.submit("enter");
  value.adapter.submit("click");
  assert.equal(value.events.length, 1);
  assert.equal(value.events[0].role, "user");
  assert.equal(value.events[0].content, "visible user prompt");
});

test("identical text in a later real submission remains a distinct turn", () => {
  const value = fixture();
  value.adapter.composer = "repeatable prompt";
  value.adapter.submit("enter");
  value.setNow(2501);
  value.adapter.submit("enter");
  assert.equal(value.events.length, 2);
  assert.notEqual(value.events[0].host_turn_reference, value.events[1].host_turn_reference);
});

test("visible stable assistant response emits once after repeated DOM observation", () => {
  const value = fixture();
  value.adapter.messages = [{ reference: "assistant-1", content: "visible answer", visible: true, streaming: false }];
  value.capture.scanVisibleAssistantMessages();
  value.setNow(1600);
  value.capture.scanVisibleAssistantMessages();
  value.capture.scanVisibleAssistantMessages();
  assert.equal(value.events.length, 1);
  assert.equal(value.events[0].role, "assistant");
});

test("hidden and streaming assistant content is not captured", () => {
  const value = fixture();
  value.adapter.messages = [
    { reference: "hidden", content: "hidden state", visible: false, streaming: false },
    { reference: "streaming", content: "partial state", visible: true, streaming: true },
  ];
  value.capture.scanVisibleAssistantMessages();
  value.setNow(2000);
  value.capture.scanVisibleAssistantMessages();
  assert.equal(value.events.length, 0);
});

test("SPA navigation updates conversation identity", () => {
  const value = fixture();
  value.adapter.composer = "first chat";
  value.adapter.submit("click");
  value.setNow(3000);
  value.setRoute({ hostChatReference: "chat-two", hostProjectReference: "g-p-two" });
  value.capture.routeChanged();
  value.adapter.composer = "second chat";
  value.adapter.submit("click");
  assert.deepEqual(value.events.map((event) => event.host_chat_reference), ["chat-one", "chat-two"]);
  assert.ok(value.events.every((event) => !event.rebind_from_host_chat_reference));
});

test("root submission is provisional and rebinds only to the newly observed stable route", () => {
  const value = fixture();
  value.setRoute({
    hostChatReference: "route:/",
    hostProjectReference: null,
    chatDisplayName: null,
    isNewChatRoute: true,
    hasStableChatReference: false,
  });
  value.adapter.composer = "new chat prompt";
  value.adapter.submit("click");
  const provisional = value.events[0].host_chat_reference;
  assert.match(provisional, /^provisional:new-chat:/);

  value.setRoute({
    hostChatReference: "conversation-new",
    hostProjectReference: null,
    chatDisplayName: "Reliable title",
    isNewChatRoute: false,
    hasStableChatReference: true,
  });
  value.capture.routeChanged();

  assert.equal(value.events[1].host_chat_reference, "conversation-new");
  assert.equal(value.events[1].rebind_from_host_chat_reference, provisional);
  assert.equal(value.events[1].host_turn_reference, value.events[0].host_turn_reference);
  assert.equal(value.events[1].content, "new chat prompt");
});

test("stable route polling rebinds before any assistant capture", () => {
  const value = fixture();
  value.setRoute({
    hostChatReference: "route:/",
    isNewChatRoute: true,
    hasStableChatReference: false,
  });
  value.adapter.composer = "route-first prompt";
  value.adapter.submit("click");
  const provisional = value.events[0].host_chat_reference;

  value.setRoute({
    hostChatReference: "route-first-stable",
    chatDisplayName: "Route First",
    hasStableChatReference: true,
  });
  value.flush();

  assert.equal(value.events.length, 2);
  assert.equal(value.events[1].host_chat_reference, "route-first-stable");
  assert.equal(value.events[1].rebind_from_host_chat_reference, provisional);
});

test("assistant scan carrying the first stable identity emits rebind before assistant", () => {
  const value = fixture();
  value.setRoute({
    hostChatReference: "route:/",
    isNewChatRoute: true,
    hasStableChatReference: false,
  });
  value.adapter.composer = "assistant-first prompt";
  value.adapter.submit("click");
  const provisional = value.events[0].host_chat_reference;

  value.setRoute({
    hostChatReference: "assistant-first-stable",
    chatDisplayName: "Assistant First",
    hasStableChatReference: true,
  });
  value.adapter.messages = [{
    reference: "assistant-first-message",
    content: "final assistant answer",
    visible: true,
    streaming: false,
  }];
  value.capture.scanVisibleAssistantMessages();
  value.setNow(1600);
  value.capture.scanVisibleAssistantMessages();

  assert.deepEqual(value.events.map((event) => event.role), ["user", "user", "assistant"]);
  assert.equal(value.events[1].rebind_from_host_chat_reference, provisional);
  assert.equal(value.events[2].host_chat_reference, "assistant-first-stable");
});

test("ChatGPT WEB UUID route is temporary and completes as one stable event flow", () => {
  const value = fixture();
  const temporaryReference = "WEB:fdfdf8ae-2284-4c79-921d-ddb8c617ba5b";
  const stableReference = "6a77047b-04a0-83ea-8aa8-76f83be1ced5";
  value.setRoute(routeIdentity(
    { pathname: "/c/" + temporaryReference },
    { title: "ChatGPT", querySelector: () => null }
  ));
  value.adapter.composer = "Reply with exactly: ATOMIZER CHATGPT LIVE TEST 003 PASSED";
  value.adapter.submit("click");
  const provisional = value.events[0].host_chat_reference;
  assert.match(provisional, /^provisional:new-chat:/);
  assert.notEqual(provisional, temporaryReference);

  value.setRoute(routeIdentity(
    { pathname: "/c/" + stableReference },
    { title: "ATOMIZER CHATGPT TEST", querySelector: () => null }
  ));
  value.adapter.messages = [{
    reference: "live-test-003-assistant",
    content: "ATOMIZER CHATGPT LIVE TEST 003 PASSED",
    visible: true,
    streaming: false,
  }];
  value.capture.scanVisibleAssistantMessages();
  value.setNow(1600);
  value.capture.scanVisibleAssistantMessages();

  assert.deepEqual(value.events.map((event) => event.role), ["user", "user", "assistant"]);
  assert.equal(value.events[1].rebind_from_host_chat_reference, provisional);
  assert.equal(value.events[1].host_chat_reference, stableReference);
  assert.equal(value.events[2].host_chat_reference, stableReference);
});

test("two separate new-chat flows retain distinct provisional and stable identities", () => {
  const value = fixture();
  const rootRoute = {
    hostChatReference: "route:/",
    hostProjectReference: null,
    chatDisplayName: null,
    isNewChatRoute: true,
    hasStableChatReference: false,
  };
  value.setRoute(rootRoute);
  value.adapter.composer = "first separate prompt";
  value.adapter.submit("click");
  value.setRoute({ hostChatReference: "stable-one", hasStableChatReference: true });
  value.capture.routeChanged();

  value.setNow(3000);
  value.setRoute(rootRoute);
  value.capture.routeChanged();
  value.adapter.composer = "second separate prompt";
  value.adapter.submit("click");
  value.setRoute({ hostChatReference: "stable-two", hasStableChatReference: true });
  value.capture.routeChanged();

  assert.notEqual(value.events[0].host_chat_reference, value.events[2].host_chat_reference);
  assert.deepEqual(
    [value.events[1].host_chat_reference, value.events[3].host_chat_reference],
    ["stable-one", "stable-two"]
  );
  assert.notEqual(
    value.events[1].rebind_from_host_chat_reference,
    value.events[3].rebind_from_host_chat_reference
  );
});

test("simultaneous capture instances cannot share a provisional identity", () => {
  const provisionalFor = (captureInstanceReference) => {
    const adapter = new FakeAdapter();
    const events = [];
    const capture = new ChatGPTCapture({
      adapter,
      routeProvider: () => ({ hostChatReference: "route:/", isNewChatRoute: true }),
      send: (event) => events.push(event),
      now: () => 1000,
      schedule: () => undefined,
      captureInstanceReference,
    });
    capture.start();
    adapter.composer = "same simultaneous prompt";
    adapter.submit("click");
    return events[0].host_chat_reference;
  };

  assert.notEqual(provisionalFor("capture-a"), provisionalFor("capture-b"));
});

test("rapid root submissions can only bind the current pending provisional", () => {
  const value = fixture();
  const rootRoute = {
    hostChatReference: "route:/",
    isNewChatRoute: true,
    hasStableChatReference: false,
  };
  value.setRoute(rootRoute);
  value.adapter.composer = "first rapid prompt";
  value.adapter.submit("click");
  const firstProvisional = value.events[0].host_chat_reference;
  value.adapter.composer = "second rapid prompt";
  value.adapter.submit("click");
  const secondProvisional = value.events[1].host_chat_reference;

  value.setRoute({ hostChatReference: "rapid-stable", hasStableChatReference: true });
  value.flush();

  assert.notEqual(firstProvisional, secondProvisional);
  assert.equal(value.events[2].rebind_from_host_chat_reference, secondProvisional);
  assert.equal(value.events[2].content, "second rapid prompt");
  assert.notEqual(value.events[2].rebind_from_host_chat_reference, firstProvisional);
});

test("async bridge delivery preserves provisional-before-rebind order", async () => {
  const adapter = new FakeAdapter();
  const delivered = [];
  let releaseFirst;
  let route = {
    hostChatReference: "route:/",
    isNewChatRoute: true,
    hasStableChatReference: false,
  };
  const capture = new ChatGPTCapture({
    adapter,
    routeProvider: () => route,
    send: (event) => {
      delivered.push(event);
      if (delivered.length === 1) {
        return new Promise((resolve) => { releaseFirst = resolve; });
      }
      return Promise.resolve();
    },
    schedule: () => undefined,
    captureInstanceReference: "ordered-instance",
  });
  capture.start();
  adapter.composer = "ordered prompt";
  adapter.submit("click");
  route = { hostChatReference: "ordered-stable", hasStableChatReference: true };
  capture.routeChanged();
  assert.equal(delivered.length, 1);
  releaseFirst();
  await new Promise((resolve) => setImmediate(resolve));
  assert.deepEqual(
    delivered.map((event) => event.host_chat_reference),
    [delivered[0].host_chat_reference, "ordered-stable"]
  );
});

test("DOM replacement remains attached through the document-root observer", () => {
  const value = fixture();
  value.flush();
  value.adapter.messages = [{ reference: "replacement", content: "replacement answer", visible: true, streaming: false }];
  value.adapter.domChanged();
  value.flush();
  value.setNow(1700);
  value.adapter.domChanged();
  value.flush();
  assert.equal(value.events.length, 1);
});

test("missing project remains null for local Unassigned resolution", () => {
  const value = fixture();
  value.adapter.composer = "unassigned prompt";
  value.adapter.submit("click");
  assert.equal(value.events[0].host_project_reference, null);
});

test("capture failure does not throw or modify the prompt", () => {
  const adapter = new FakeAdapter();
  adapter.composer = "unchanged prompt";
  const capture = new ChatGPTCapture({
    adapter,
    routeProvider: () => ({ hostChatReference: "chat" }),
    send: () => { throw new Error("bridge unavailable"); },
    schedule: () => undefined,
  });
  capture.start();
  assert.doesNotThrow(() => adapter.submit("click"));
  assert.equal(adapter.composer, "unchanged prompt");
});

test("Project chat route keeps stable identity but defers its title to the sidebar", () => {
  const route = routeIdentity(
    { pathname: "/g/" + LIVE_PROJECT_SLUGGED + "/c/" + LIVE_CHAT_A },
    { title: "My Chat - ChatGPT", querySelector: () => null }
  );
  assert.equal(route.hostChatReference, LIVE_CHAT_A);
  assert.equal(route.hostProjectReference, LIVE_PROJECT);
  assert.equal(route.chatDisplayName, null);
  assert.equal(route.hasStableChatReference, true);
  assert.equal(route.isNewChatRoute, false);
});

test("live canonical and slugged Project routes share one canonical identity", () => {
  assert.equal(canonicalProjectReference(LIVE_PROJECT), LIVE_PROJECT);
  assert.equal(canonicalProjectReference(LIVE_PROJECT_SLUGGED), LIVE_PROJECT);
  assert.equal(
    canonicalProjectReference("G-P-00000000000000001111111111111111-PRESENTATION"),
    LIVE_PROJECT
  );
  assert.equal(canonicalProjectReference("g-p-not-a-proven-project-shape"), null);
  assert.equal(canonicalProjectReference("g-p-0000000000000000111111111111111"), null);
});

test("Project home is a project-scoped new-chat route", () => {
  const route = routeIdentity(
    { pathname: "/g/" + LIVE_PROJECT + "/project" },
    { title: "ChatGPT - ATOMIZER PROJECT TEST", querySelector: () => null }
  );
  assert.equal(route.hostProjectReference, LIVE_PROJECT);
  assert.equal(route.projectDisplayName, "ATOMIZER PROJECT TEST");
  assert.equal(route.chatDisplayName, null);
  assert.equal(route.hasStableChatReference, false);
  assert.equal(route.isProjectHomeRoute, true);
  assert.equal(route.isNewChatRoute, true);

  const unproven = routeIdentity(
    { pathname: "/g/g-p-not-a-proven-project-shape/project" },
    { title: "ChatGPT", querySelector: () => null }
  );
  assert.equal(unproven.hostProjectReference, null);
  assert.equal(unproven.isProjectHomeRoute, false);
  assert.equal(unproven.isNewChatRoute, false);
});

test("Project-home title parser is narrow and does not infer from routes or DOM labels", () => {
  assert.equal(projectHomeDisplayName("ChatGPT - ATOMIZER PROJECT TEST"), "ATOMIZER PROJECT TEST");
  assert.equal(projectHomeDisplayName("ChatGPT - ATOMIZER PROJECT TEST RENAMED"), "ATOMIZER PROJECT TEST RENAMED");
  assert.equal(projectHomeDisplayName("ATOMIZER PROJECT TEST - ChatGPT"), null);
  assert.equal(projectHomeDisplayName("ChatGPT | ATOMIZER PROJECT TEST"), null);
  assert.equal(projectHomeDisplayName("ChatGPT - "), null);
  assert.equal(projectHomeDisplayName("ChatGPT - invalid\nname"), null);

  const malformedHome = routeIdentity(
    { pathname: "/g/" + LIVE_PROJECT_SLUGGED + "/project" },
    {
      title: "Unexpected Project Page",
      querySelector: () => ({ textContent: "UNTRUSTED DOM PROJECT NAME" }),
    }
  );
  assert.equal(malformedHome.hostProjectReference, LIVE_PROJECT);
  assert.equal(malformedHome.projectDisplayName, null);
  assert.equal(malformedHome.chatDisplayName, null);
});

test("Project-home title is never authoritative on ordinary or Project chat pages", () => {
  const ordinary = routeIdentity(
    { pathname: "/c/ordinary-chat" },
    { title: "ChatGPT - NOT A PROJECT NAME", querySelector: () => null }
  );
  const projectChat = routeIdentity(
    { pathname: "/g/" + LIVE_PROJECT_SLUGGED + "/c/" + LIVE_CHAT_A },
    { title: "ChatGPT - NOT A PROJECT NAME", querySelector: () => null }
  );

  assert.equal(ordinary.projectDisplayName, null);
  assert.equal(ordinary.chatDisplayName, null);
  assert.equal(projectChat.projectDisplayName, null);
  assert.equal(projectChat.hostProjectReference, LIVE_PROJECT);
  assert.equal(projectChat.chatDisplayName, null);
});

test("Project-home submission carries the proven display name without changing identity", () => {
  const value = fixture("project-name-event");
  value.setRoute(routeIdentity(
    { pathname: "/g/" + LIVE_PROJECT_SLUGGED + "/project" },
    { title: "ChatGPT - ATOMIZER PROJECT TEST", querySelector: () => null }
  ));
  value.adapter.composer = "project display-name propagation prompt";
  value.adapter.submit("click");

  assert.equal(value.events[0].host_project_reference, LIVE_PROJECT);
  assert.equal(value.events[0].project_display_name, "ATOMIZER PROJECT TEST");
  assert.equal(value.events[0].chat_display_name, null);
});

test("submission intent snapshots Project home after a missed SPA notification", () => {
  const value = fixture("project-name-route-transition");
  // capture.start() ran on the fixture's ordinary chat route. This SPA change is
  // deliberately not followed by routeChanged(), a DOM scan, or scheduled work.
  value.setRoute(routeIdentity(
    { pathname: "/g/" + LIVE_PROJECT_SLUGGED + "/project" },
    { title: "ChatGPT - ATOMIZER PROJECT TEST", querySelector: () => null }
  ));
  value.adapter.composer = "project name route-transition prompt";
  value.adapter.beginSubmit("click");

  const temporaryReference = "WEB:fdfdf8ae-2284-4c79-921d-ddb8c617ba5b";
  value.setRoute(routeIdentity(
    { pathname: "/g/" + LIVE_PROJECT_SLUGGED + "/c/" + temporaryReference },
    { title: "ChatGPT", querySelector: () => null }
  ));
  value.adapter.finishSubmit("click");

  const provisional = value.events[0].host_chat_reference;
  assert.match(provisional, /^provisional:new-chat:project-name-route-transition:/);
  assert.equal(value.events[0].host_project_reference, LIVE_PROJECT);
  assert.equal(value.events[0].project_display_name, "ATOMIZER PROJECT TEST");
  assert.equal(value.capture.pendingNewChat.projectDisplayName, "ATOMIZER PROJECT TEST");

  value.setRoute(routeIdentity(
    { pathname: "/g/" + LIVE_PROJECT + "/c/" + LIVE_CHAT_A },
    { title: "Project Name Sync - ChatGPT", querySelector: () => null }
  ));
  value.capture.routeChanged();

  assert.equal(value.events[1].host_project_reference, LIVE_PROJECT);
  assert.equal(value.events[1].host_chat_reference, LIVE_CHAT_A);
  assert.equal(value.events[1].rebind_from_host_chat_reference, provisional);
  assert.equal(value.events[1].project_display_name, "ATOMIZER PROJECT TEST");
});

test("trusted Project-home name cannot cross Projects", () => {
  const value = fixture("project-name-cross-project");
  value.setRoute(routeIdentity(
    { pathname: "/g/" + LIVE_PROJECT_SLUGGED + "/project" },
    { title: "ChatGPT - ATOMIZER PROJECT TEST", querySelector: () => null }
  ));
  value.capture.scanVisibleAssistantMessages();

  const otherProject = "g-p-00000000000000002222222222222222";
  value.setRoute(routeIdentity(
    { pathname: "/g/" + otherProject + "/c/WEB:11111111-1111-1111-1111-111111111111" },
    { title: "ChatGPT", querySelector: () => null }
  ));
  value.adapter.composer = "other Project prompt";
  value.adapter.submit("click");

  assert.equal(value.events[0].host_project_reference, otherProject);
  assert.equal(value.events[0].project_display_name, null);
});

test("malformed Project-home title clears an older unconsumed trusted name", () => {
  const value = fixture("project-name-malformed-home");
  value.setRoute(routeIdentity(
    { pathname: "/g/" + LIVE_PROJECT_SLUGGED + "/project" },
    { title: "ChatGPT - ATOMIZER PROJECT TEST", querySelector: () => null }
  ));
  value.capture.scanVisibleAssistantMessages();
  value.setRoute(routeIdentity(
    { pathname: "/g/" + LIVE_PROJECT_SLUGGED + "/project" },
    { title: "Unexpected Project Page", querySelector: () => null }
  ));
  value.capture.scanVisibleAssistantMessages();
  value.setRoute(routeIdentity(
    { pathname: "/g/" + LIVE_PROJECT + "/c/WEB:22222222-2222-2222-2222-222222222222" },
    { title: "ChatGPT", querySelector: () => null }
  ));
  value.adapter.composer = "malformed Project-home prompt";
  value.adapter.submit("click");

  assert.equal(value.events[0].host_project_reference, LIVE_PROJECT);
  assert.equal(value.events[0].project_display_name, null);
});

test("Project-home submission rebinds only to a stable chat in the same Project", () => {
  const value = fixture("project-tab-a");
  value.setRoute(routeIdentity(
    { pathname: "/g/" + LIVE_PROJECT + "/project" },
    { title: "ChatGPT - ATOMIZER PROJECT TEST", querySelector: () => null }
  ));
  value.adapter.composer = "Reply with exactly: ATOMIZER PROJECT CHAT A";
  value.adapter.submit("click");
  const provisional = value.events[0].host_chat_reference;
  assert.match(provisional, /^provisional:new-chat:project-tab-a:/);
  assert.equal(value.events[0].host_project_reference, LIVE_PROJECT);

  const otherProject = "g-p-00000000000000002222222222222222";
  value.setRoute(routeIdentity(
    { pathname: "/g/" + otherProject + "/c/not-chat-a" },
    { title: "Other Project Chat - ChatGPT", querySelector: () => null }
  ));
  value.capture.routeChanged();
  assert.equal(value.events.length, 1);

  value.setRoute(routeIdentity(
    { pathname: "/g/" + LIVE_PROJECT_SLUGGED + "/c/" + LIVE_CHAT_A },
    { title: "Chat A - ChatGPT", querySelector: () => null }
  ));
  value.capture.routeChanged();
  assert.equal(value.events.length, 2);
  assert.equal(value.events[1].host_chat_reference, LIVE_CHAT_A);
  assert.equal(value.events[1].host_project_reference, LIVE_PROJECT);
  assert.equal(value.events[1].rebind_from_host_chat_reference, provisional);
  assert.equal(value.events[1].host_turn_reference, value.events[0].host_turn_reference);
});

test("Project assistant captured before stable route stays on the pending provisional chat", () => {
  const value = fixture("assistant-before-stable");
  value.setRoute(routeIdentity(
    { pathname: "/g/" + LIVE_PROJECT_SLUGGED + "/project" },
    { title: "ChatGPT - ATOMIZER PROJECT TEST", querySelector: () => null }
  ));
  value.adapter.composer = "Reply with exactly: ATOMIZER PROJECT CHAT A";
  value.adapter.submit("click");
  const provisional = value.events[0].host_chat_reference;
  value.adapter.messages = [{
    reference: "assistant-before-stable-message",
    content: "ATOMIZER PROJECT CHAT A",
    visible: true,
    streaming: false,
  }];
  value.capture.scanVisibleAssistantMessages();
  value.setNow(1600);
  value.capture.scanVisibleAssistantMessages();

  assert.deepEqual(value.events.map((event) => event.role), ["user", "assistant"]);
  assert.equal(value.events[1].host_chat_reference, provisional);
  assert.ok(value.events.every((event) => !event.host_chat_reference.startsWith("route:/g/")));

  value.setRoute(routeIdentity(
    { pathname: "/g/" + LIVE_PROJECT + "/c/" + LIVE_CHAT_A },
    { title: "Chat A - ChatGPT", querySelector: () => null }
  ));
  value.capture.routeChanged();
  assert.equal(value.events[2].rebind_from_host_chat_reference, provisional);
  assert.equal(value.events[2].host_chat_reference, LIVE_CHAT_A);
  assert.equal(value.capture.pendingNewChat, null);
  assert.equal(value.capture.recentProjectStableRoute, null);
});

test("live Chat B order uses recent stable identity after returning to Project home", () => {
  const value = fixture("stable-then-home");
  const home = routeIdentity(
    { pathname: "/g/" + LIVE_PROJECT_SLUGGED + "/project" },
    { title: "ChatGPT - ATOMIZER PROJECT TEST", querySelector: () => null }
  );
  value.setRoute(home);
  value.adapter.composer = "Reply with exactly: ATOMIZER PROJECT CHAT B";
  value.adapter.submit("click");
  const provisional = value.events[0].host_chat_reference;
  value.setRoute(routeIdentity(
    { pathname: "/g/" + LIVE_PROJECT + "/c/" + LIVE_CHAT_B },
    { title: "Chat B - ChatGPT", querySelector: () => null }
  ));
  value.capture.routeChanged();
  value.capture.routeChanged();
  assert.equal(value.events.length, 2);
  assert.equal(value.events[1].rebind_from_host_chat_reference, provisional);

  value.adapter.messages = [{
    reference: "live-chat-b-assistant",
    content: "ATOMIZER PROJECT CHAT B",
    visible: true,
    streaming: false,
  }];
  value.setNow(1400);
  value.capture.scanVisibleAssistantMessages();
  value.setRoute(home);
  value.capture.routeChanged();
  value.capture.scanVisibleAssistantMessages();
  value.setNow(2000);
  value.capture.scanVisibleAssistantMessages();

  assert.equal(value.events.length, 3);
  assert.equal(value.events[2].role, "assistant");
  assert.equal(value.events[2].host_chat_reference, LIVE_CHAT_B);
  assert.ok(value.events.every((event) => !event.host_chat_reference.startsWith("route:/g/")));
  assert.equal(value.capture.recentProjectStableRoute, null);
});

test("Project-home assistant DOM without pending or recent state emits nothing", () => {
  const value = fixture("project-home-no-state");
  value.setRoute(routeIdentity(
    { pathname: "/g/" + LIVE_PROJECT_SLUGGED + "/project" },
    { title: "ChatGPT - ATOMIZER PROJECT TEST", querySelector: () => null }
  ));
  value.adapter.messages = [{
    reference: "stale-project-home-assistant",
    content: "previously rendered answer",
    visible: true,
    streaming: false,
  }];
  value.capture.scanVisibleAssistantMessages();
  value.setNow(2000);
  value.capture.scanVisibleAssistantMessages();
  assert.equal(value.events.length, 0);
});

test("recent stable Project association expires without creating a route chat", () => {
  const value = fixture("recent-project-timeout");
  const home = routeIdentity(
    { pathname: "/g/" + LIVE_PROJECT + "/project" },
    { title: "ChatGPT - ATOMIZER PROJECT TEST", querySelector: () => null }
  );
  value.setRoute(home);
  value.adapter.composer = "bounded association prompt";
  value.adapter.submit("click");
  value.setRoute(routeIdentity(
    { pathname: "/g/" + LIVE_PROJECT + "/c/" + LIVE_CHAT_A },
    { title: "Bounded Chat - ChatGPT", querySelector: () => null }
  ));
  value.capture.routeChanged();
  value.setRoute(home);
  value.capture.routeChanged();
  value.adapter.messages = [{
    reference: "expired-project-assistant",
    content: "late answer",
    visible: true,
    streaming: false,
  }];
  value.setNow(32000);
  value.capture.scanVisibleAssistantMessages();
  value.setNow(33000);
  value.capture.scanVisibleAssistantMessages();
  assert.equal(value.events.length, 2);
  assert.equal(value.capture.recentProjectStableRoute, null);
  assert.ok(value.events.every((event) => !event.host_chat_reference.startsWith("route:/g/")));
});

test("two consecutive Project-home chats in one tab reset state and ignore stale prior DOM", () => {
  const value = fixture("two-project-chats-one-tab");
  const home = routeIdentity(
    { pathname: "/g/" + LIVE_PROJECT_SLUGGED + "/project" },
    { title: "ChatGPT - ATOMIZER PROJECT TEST", querySelector: () => null }
  );
  const stableA = routeIdentity(
    { pathname: "/g/" + LIVE_PROJECT + "/c/" + LIVE_CHAT_A },
    { title: "Chat A - ChatGPT", querySelector: () => null }
  );
  const stableB = routeIdentity(
    { pathname: "/g/" + LIVE_PROJECT + "/c/" + LIVE_CHAT_B },
    { title: "Chat B - ChatGPT", querySelector: () => null }
  );
  const assistantA = {
    reference: "two-flow-assistant-a",
    content: "ATOMIZER PROJECT CHAT A",
    visible: true,
    streaming: false,
  };
  const assistantB = {
    reference: "two-flow-assistant-b",
    content: "ATOMIZER PROJECT CHAT B",
    visible: true,
    streaming: false,
  };

  value.setRoute(home);
  value.adapter.composer = "Reply with exactly: ATOMIZER PROJECT CHAT A";
  value.adapter.submit("click");
  const provisionalA = value.events[0].host_chat_reference;
  value.setRoute(stableA);
  value.capture.routeChanged();
  value.adapter.messages = [assistantA];
  value.capture.scanVisibleAssistantMessages();
  value.setNow(1600);
  value.capture.scanVisibleAssistantMessages();
  assert.deepEqual(
    value.events.slice(0, 3).map((event) => [event.role, event.host_chat_reference]),
    [["user", provisionalA], ["user", LIVE_CHAT_A], ["assistant", LIVE_CHAT_A]]
  );
  assert.equal(value.capture.pendingNewChat, null);
  assert.equal(value.capture.recentProjectStableRoute, null);

  value.setRoute(home);
  value.capture.routeChanged();
  value.adapter.messages = [assistantA];
  value.setNow(2300);
  value.capture.scanVisibleAssistantMessages();
  assert.equal(value.events.length, 3);

  value.adapter.composer = "Reply with exactly: ATOMIZER PROJECT CHAT B";
  value.adapter.submit("click");
  const provisionalB = value.events[3].host_chat_reference;
  assert.notEqual(provisionalB, provisionalA);
  value.adapter.messages = [assistantA, assistantB];
  value.capture.scanVisibleAssistantMessages();
  value.setNow(2900);
  value.capture.scanVisibleAssistantMessages();
  assert.equal(value.events[4].role, "assistant");
  assert.equal(value.events[4].host_chat_reference, provisionalB);
  assert.equal(value.events.filter((event) => event.content === assistantA.content).length, 1);

  value.setRoute(stableB);
  value.capture.routeChanged();
  value.capture.routeChanged();
  assert.equal(value.events[5].rebind_from_host_chat_reference, provisionalB);
  assert.equal(value.events[5].host_chat_reference, LIVE_CHAT_B);
  assert.equal(value.capture.pendingNewChat, null);
  assert.equal(value.capture.recentProjectStableRoute, null);
  assert.ok(value.events.every((event) => !event.host_chat_reference.startsWith("route:/g/")));
});

test("Project route appearing during assistant stability polling binds final answer stably", () => {
  const value = fixture("route-during-stability");
  value.setRoute(routeIdentity(
    { pathname: "/g/" + LIVE_PROJECT + "/project" },
    { title: "ChatGPT - ATOMIZER PROJECT TEST", querySelector: () => null }
  ));
  value.adapter.composer = "fast project prompt";
  value.adapter.submit("click");
  value.adapter.messages = [{
    reference: "fast-project-assistant",
    content: "fast exact answer",
    visible: true,
    streaming: false,
  }];
  value.capture.scanVisibleAssistantMessages();
  value.setRoute(routeIdentity(
    { pathname: "/g/" + LIVE_PROJECT_SLUGGED + "/c/" + LIVE_CHAT_A },
    { title: "Fast Chat - ChatGPT", querySelector: () => null }
  ));
  value.capture.routeChanged();
  value.capture.scanVisibleAssistantMessages();
  value.setNow(1600);
  value.capture.scanVisibleAssistantMessages();
  assert.equal(value.events.at(-1).role, "assistant");
  assert.equal(value.events.at(-1).host_chat_reference, LIVE_CHAT_A);
});

test("two tabs starting chats in one Project cannot cross-bind", () => {
  const first = fixture("project-tab-first");
  const second = fixture("project-tab-second");
  const home = routeIdentity(
    { pathname: "/g/" + LIVE_PROJECT_SLUGGED + "/project" },
    { title: "ChatGPT - ATOMIZER PROJECT TEST", querySelector: () => null }
  );
  first.setRoute(home);
  second.setRoute(home);
  first.adapter.composer = "Reply with exactly: ATOMIZER PROJECT CHAT A";
  second.adapter.composer = "Reply with exactly: ATOMIZER PROJECT CHAT B";
  first.adapter.submit("click");
  second.adapter.submit("click");
  assert.notEqual(first.events[0].host_chat_reference, second.events[0].host_chat_reference);

  first.setRoute(routeIdentity(
    { pathname: "/g/" + LIVE_PROJECT + "/c/" + LIVE_CHAT_A },
    { title: "Chat A - ChatGPT", querySelector: () => null }
  ));
  second.setRoute(routeIdentity(
    { pathname: "/g/" + LIVE_PROJECT + "/c/" + LIVE_CHAT_B },
    { title: "Chat B - ChatGPT", querySelector: () => null }
  ));
  first.capture.routeChanged();
  second.capture.routeChanged();
  assert.equal(first.events[1].host_chat_reference, LIVE_CHAT_A);
  assert.equal(second.events[1].host_chat_reference, LIVE_CHAT_B);
  assert.equal(
    first.events[1].rebind_from_host_chat_reference,
    first.events[0].host_chat_reference
  );
  assert.equal(
    second.events[1].rebind_from_host_chat_reference,
    second.events[0].host_chat_reference
  );
});

test("ordinary non-Project stable chat exposes no Project identity", () => {
  const route = routeIdentity(
    { pathname: "/c/non-project-conversation" },
    { title: "Ordinary Chat - ChatGPT", querySelector: () => null }
  );
  assert.equal(route.hostProjectReference, null);
  assert.equal(route.hostChatReference, "non-project-conversation");
  assert.equal(route.hasStableChatReference, true);
  assert.equal(route.isNewChatRoute, false);
});

test("root route has no reliable title and is explicitly marked new-chat", () => {
  const route = routeIdentity(
    { pathname: "/" },
    { title: "ChatGPT", querySelector: () => null }
  );
  assert.equal(route.hostChatReference, "route:/");
  assert.equal(route.chatDisplayName, null);
  assert.equal(route.hasStableChatReference, false);
  assert.equal(route.isNewChatRoute, true);
});

test("only WEB UUID route segments are classified as temporary", () => {
  const temporary = routeIdentity(
    { pathname: "/c/WEB:fdfdf8ae-2284-4c79-921d-ddb8c617ba5b" },
    { title: "ChatGPT", querySelector: () => null }
  );
  const unrelated = routeIdentity(
    { pathname: "/c/WEB:not-a-uuid" },
    { title: "Existing", querySelector: () => null }
  );
  assert.equal(isTemporaryNewChatReference(temporary.hostChatReference), true);
  assert.equal(temporary.hasStableChatReference, false);
  assert.equal(temporary.isNewChatRoute, true);
  assert.equal(isTemporaryNewChatReference(unrelated.hostChatReference), false);
  assert.equal(unrelated.hasStableChatReference, true);
  assert.equal(unrelated.isNewChatRoute, false);
});

test("assistant status without a markdown message body is excluded structurally", () => {
  const styleDocument = {
    defaultView: { getComputedStyle: () => ({ display: "block", visibility: "visible" }) },
  };
  const content = (text) => ({
    hidden: false,
    ownerDocument: styleDocument,
    innerText: text,
    getAttribute: () => null,
  });
  const assistant = (id, markdownContent) => {
    const article = { id, querySelector: () => null };
    return {
      hidden: false,
      ownerDocument: styleDocument,
      getAttribute: () => null,
      closest: () => article,
      querySelector: (selector) => selector === ".markdown" ? markdownContent : null,
    };
  };
  const transient = assistant("transient", null);
  const legitimate = assistant("legitimate", content("I was thinking about the exact answer."));
  const adapter = new DomCaptureAdapter(
    { querySelectorAll: () => [transient, legitimate] },
    {}
  );

  assert.deepEqual(adapter.listAssistantMessages().map((message) => message.content), [
    "I was thinking about the exact answer.",
  ]);
});

test("a completed assistant response containing thinking is still emitted", () => {
  const value = fixture();
  value.adapter.messages = [{
    reference: "legitimate-thinking",
    content: "I was thinking about the exact answer.",
    visible: true,
    streaming: false,
  }];
  value.capture.scanVisibleAssistantMessages();
  value.setNow(1600);
  value.capture.scanVisibleAssistantMessages();
  assert.equal(value.events.length, 1);
  assert.equal(value.events[0].content, "I was thinking about the exact answer.");
});
