(function (root, factory) {
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  root.AtomizerChatTypes = api;
})(globalThis, function () {
  "use strict";

  function stableHash(value) {
    let first = 0x811c9dc5;
    let second = 0x9e3779b9;
    for (let index = 0; index < value.length; index += 1) {
      const code = value.charCodeAt(index);
      first = Math.imul(first ^ code, 0x01000193) >>> 0;
      second = Math.imul(second ^ code, 0x85ebca6b) >>> 0;
    }
    return first.toString(16).padStart(8, "0") + second.toString(16).padStart(8, "0");
  }

  function requiredText(value, fieldName) {
    if (typeof value !== "string" || value.trim() === "") {
      throw new Error(fieldName + " must be non-empty text");
    }
    return value.trim();
  }

  function makeChatEvent(input) {
    const hostChatReference = requiredText(input.hostChatReference, "hostChatReference");
    const role = requiredText(input.role, "role");
    if (role !== "user" && role !== "assistant") throw new Error("unsupported role");
    const content = requiredText(input.content, "content");
    const messageReference = requiredText(input.messageReference, "messageReference");
    const material = ["chatgpt_web", hostChatReference, messageReference, role, content].join("\u001f");
    const event = {
      event_id: "chatgpt-web-" + stableHash(material),
      host: "chatgpt_web",
      host_project_reference: input.hostProjectReference || null,
      host_chat_reference: hostChatReference,
      host_turn_reference: messageReference,
      role,
      content,
      captured_at: new Date(input.capturedAt || Date.now()).toISOString(),
      project_display_name: input.projectDisplayName || null,
      chat_display_name: input.chatDisplayName || null,
    };
    if (input.rebindFromHostChatReference) {
      event.rebind_from_host_chat_reference = requiredText(
        input.rebindFromHostChatReference,
        "rebindFromHostChatReference"
      );
    }
    return event;
  }

  return { makeChatEvent, requiredText, stableHash };
});
