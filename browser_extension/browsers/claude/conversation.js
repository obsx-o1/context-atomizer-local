(function (root, factory) {
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  root.AtomizerClaudeConversation = api;
})(globalThis, function () {
  "use strict";

  function clean(value) {
    return typeof value === "string" && /^[A-Za-z0-9_-]+$/.test(value) ? value : null;
  }

  function routeIdentity(locationValue, documentValue) {
    const pathname = (locationValue && locationValue.pathname) || "/";
    const parts = pathname.split("/").filter(Boolean);
    let project = null;
    let chat = null;
    let code = false;
    if (parts[0] === "project") {
      project = clean(parts[1]);
      const chatIndex = parts.indexOf("chat", 2);
      if (chatIndex >= 0) chat = clean(parts[chatIndex + 1]);
    } else if (parts[0] === "chat") {
      chat = clean(parts[1]);
    } else if (parts[0] === "code") {
      code = true;
      chat = clean(parts[1]);
    }
    const title = documentValue && typeof documentValue.title === "string"
      ? documentValue.title.replace(/\s*[|\-]\s*Claude(?: Code)?\s*$/i, "").trim()
      : "";
    const reliableTitle = title && !/^Claude(?: Code)?$/i.test(title) ? title : null;
    const stable = Boolean(chat);
    return {
      hostChatReference: stable ? (code ? "code:" : "") + chat : "route:" + pathname,
      hostProjectReference: project ? "project:" + project : null,
      projectDisplayName: null,
      chatDisplayName: reliableTitle,
      hasStableChatReference: stable,
      isProjectHomeRoute: Boolean(project && !chat),
      isNewChatRoute: !stable,
    };
  }

  class RouteWatcher {
    constructor(windowValue, callback) {
      this.windowValue = windowValue;
      this.callback = callback;
      this.originals = [];
      this.onPopState = () => this.callback();
    }
    start() {
      for (const name of ["pushState", "replaceState"]) {
        const original = this.windowValue.history[name];
        this.originals.push([name, original]);
        this.windowValue.history[name] = (...args) => {
          const result = original.apply(this.windowValue.history, args);
          this.callback();
          return result;
        };
      }
      this.windowValue.addEventListener("popstate", this.onPopState);
    }
    stop() {
      for (const [name, original] of this.originals) this.windowValue.history[name] = original;
      this.windowValue.removeEventListener("popstate", this.onPopState);
    }
  }

  return { RouteWatcher, routeIdentity };
});
