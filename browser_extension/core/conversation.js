(function (root, factory) {
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  root.AtomizerChatConversation = api;
})(globalThis, function () {
  "use strict";

  const TEMPORARY_NEW_CHAT_REFERENCE = /^WEB:[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
  const CANONICAL_PROJECT_REFERENCE = /^g-p-([0-9a-f]{32})(?:-[^/?#]+)?$/i;

  function isTemporaryNewChatReference(value) {
    return typeof value === "string" && TEMPORARY_NEW_CHAT_REFERENCE.test(value);
  }

  function canonicalProjectReference(value) {
    if (typeof value !== "string") return null;
    const match = value.match(CANONICAL_PROJECT_REFERENCE);
    return match ? "g-p-" + match[1].toLowerCase() : null;
  }

  function projectHomeDisplayName(documentTitle) {
    if (typeof documentTitle !== "string" || !documentTitle.startsWith("ChatGPT - ")) {
      return null;
    }
    const displayName = documentTitle.slice("ChatGPT - ".length).trim();
    if (!displayName || displayName.length > 200 || /[\u0000-\u001f\u007f]/.test(displayName)) {
      return null;
    }
    return displayName;
  }

  function routeIdentity(locationValue, documentValue) {
    const pathname = (locationValue && locationValue.pathname) || "/";
    const chatMatch = pathname.match(/\/c\/([^/?#]+)/);
    const chatReference = chatMatch ? chatMatch[1] : null;
    const temporaryNewChat = isTemporaryNewChatReference(chatReference);
    const projectMatch = pathname.match(/\/(?:g|project)\/([^/?#]+)/);
    const projectSegment = projectMatch ? projectMatch[1] : null;
    const canonicalProject = canonicalProjectReference(projectSegment);
    const legacyProject = projectSegment && /^proj_[^/?#]+$/.test(projectSegment)
      ? projectSegment
      : null;
    const hostProjectReference = canonicalProject || legacyProject;
    const projectHomeMatch = pathname.match(/^\/g\/([^/?#]+)\/project\/?$/);
    const projectHomeReference = projectHomeMatch
      ? canonicalProjectReference(projectHomeMatch[1])
      : null;
    const isProjectHomeRoute = Boolean(
      projectHomeReference && projectHomeReference === hostProjectReference
    );
    const documentTitle = documentValue && typeof documentValue.title === "string"
      ? documentValue.title
      : "";
    const projectDisplayName = isProjectHomeRoute
      ? projectHomeDisplayName(documentTitle)
      : null;
    const title = documentTitle.replace(/\s*[|\-]\s*ChatGPT\s*$/i, "").trim();
    const reliableTitle = (
      !hostProjectReference && title && title.toLowerCase() !== "chatgpt" &&
      !/^chatgpt\s*-/i.test(title)
    )
      ? title
      : null;
    return {
      hostChatReference: chatReference || "route:" + pathname,
      hostProjectReference,
      projectDisplayName,
      chatDisplayName: reliableTitle,
      hasStableChatReference: Boolean(chatReference) && !temporaryNewChat,
      isProjectHomeRoute,
      isNewChatRoute: temporaryNewChat || isProjectHomeRoute || (!chatReference && pathname === "/"),
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
      const history = this.windowValue.history;
      for (const methodName of ["pushState", "replaceState"]) {
        const original = history[methodName];
        this.originals.push([methodName, original]);
        history[methodName] = (...argumentsValue) => {
          const result = original.apply(history, argumentsValue);
          this.callback();
          return result;
        };
      }
      this.windowValue.addEventListener("popstate", this.onPopState);
    }

    stop() {
      for (const [methodName, original] of this.originals) {
        this.windowValue.history[methodName] = original;
      }
      this.originals = [];
      this.windowValue.removeEventListener("popstate", this.onPopState);
    }
  }

  return {
    RouteWatcher,
    canonicalProjectReference,
    isTemporaryNewChatReference,
    projectHomeDisplayName,
    routeIdentity,
  };
});
