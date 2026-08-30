"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const {
  SidebarTitleReconciler,
  cleanVisibleTitle,
  linkIdentity,
  scanVisibleConversationLinks,
} = require("../core/chat_titles.js");
const { canonicalProjectReference } = require("../core/conversation.js");

const PROJECT = "g-p-00000000000000001111111111111111";
const CHAT_A = "6a794dcb-ff9c-83ea-8177-7aef25bd782d";
const CHAT_B = "6a794dd3-a1ac-83ea-b651-545772a9f204";

function link(href, visibleTitle, ariaLabel = null) {
  return {
    hidden: false,
    innerText: visibleTitle,
    textContent: visibleTitle,
    getAttribute(name) {
      if (name === "href") return href;
      if (name === "aria-label") return ariaLabel;
      if (name === "aria-hidden") return null;
      return null;
    },
  };
}

function environment(links) {
  return {
    documentValue: {
      documentElement: {},
      querySelectorAll(selector) {
        assert.equal(selector, "nav a[href], aside a[href]");
        return links;
      },
    },
    windowValue: {
      location: new URL("https://chatgpt.com/"),
      getComputedStyle: () => ({ display: "block", visibility: "visible" }),
      MutationObserver: class {
        observe() {}
        disconnect() {}
      },
      setTimeout(callback) { callback(); },
    },
  };
}

test("exact same-origin IDs map only to their own visible titles", () => {
  const { documentValue, windowValue } = environment([
    link("/c/" + CHAT_A, "Ordinary Chat A", "Wrong aria fallback"),
    link("/g/" + PROJECT + "/c/" + CHAT_B, "Project Chat B", "Project Chat B, chat in project Context Atomizer"),
    link("https://example.com/c/" + CHAT_A, "External title", "External title"),
  ]);

  const observations = scanVisibleConversationLinks(
    documentValue,
    windowValue,
    canonicalProjectReference
  );

  assert.deepEqual(observations, [
    {
      host_chat_reference: CHAT_A,
      host_project_reference: null,
      visible_title: "Ordinary Chat A",
      aria_label: "Wrong aria fallback",
    },
    {
      host_chat_reference: CHAT_B,
      host_project_reference: PROJECT,
      visible_title: "Project Chat B",
      aria_label: "Project Chat B, chat in project Context Atomizer",
    },
  ]);
});

test("visible title rejects Project accessibility prose but preserves commas", () => {
  assert.equal(cleanVisibleTitle("Plans, Reviews, and Follow-up"), "Plans, Reviews, and Follow-up");
  assert.equal(
    cleanVisibleTitle("Project Chat, chat in project Context Atomizer"),
    null
  );
  assert.equal(cleanVisibleTitle("ChatGPT - Context Atomizer"), null);
});

test("unknown and conflicting link metadata is omitted as ambiguous", () => {
  const { documentValue, windowValue } = environment([
    link("/c/" + CHAT_A, "Title One", "Title One"),
    link("/c/" + CHAT_A, "Title Two", "Title Two"),
    link("/not-a-chat", "Unknown", "Unknown"),
  ]);

  assert.deepEqual(
    scanVisibleConversationLinks(documentValue, windowValue, canonicalProjectReference),
    []
  );
});

test("reconciler sends initial metadata and only sends a later rename once", async () => {
  let currentTitle = "Before Rename";
  const currentLink = link("/c/" + CHAT_A, currentTitle, currentTitle);
  Object.defineProperty(currentLink, "innerText", { get: () => currentTitle });
  Object.defineProperty(currentLink, "textContent", { get: () => currentTitle });
  const { documentValue, windowValue } = environment([currentLink]);
  const batches = [];
  const reconciler = new SidebarTitleReconciler({
    documentValue,
    windowValue,
    canonicalProjectReference,
    send: (observations) => batches.push(observations),
  });

  reconciler.start();
  reconciler.scan();
  currentTitle = "After Rename";
  reconciler.scan();
  reconciler.scan();

  assert.equal(batches.length, 2);
  assert.equal(batches[0][0].visible_title, "Before Rename");
  assert.equal(batches[1][0].visible_title, "After Rename");
});

test("link identity accepts supported Project route and rejects cross-origin links", () => {
  assert.deepEqual(
    linkIdentity(
      "/g/" + PROJECT + "-slug/c/" + CHAT_A,
      "https://chatgpt.com/",
      "https://chatgpt.com",
      canonicalProjectReference
    ),
    { host_chat_reference: CHAT_A, host_project_reference: PROJECT }
  );
  assert.equal(
    linkIdentity(
      "https://example.com/c/" + CHAT_A,
      "https://chatgpt.com/",
      "https://chatgpt.com",
      canonicalProjectReference
    ),
    null
  );
});
