(function (root, factory) {
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  root.AtomizerChatTitles = api;
})(globalThis, function () {
  "use strict";

  const ALLOWED_ORIGINS = new Set(["https://chatgpt.com", "https://chat.openai.com"]);
  const MAX_OBSERVATIONS = 200;
  const MAX_TITLE_LENGTH = 400;
  const PROJECT_ACCESSIBILITY_PHRASE = /, chat in project /i;

  function normalizedText(value) {
    return typeof value === "string" ? value.replace(/\s+/g, " ").trim() : "";
  }

  function boundedText(value) {
    const normalized = normalizedText(value);
    if (
      !normalized || normalized.length > MAX_TITLE_LENGTH ||
      /[\u0000-\u001f\u007f]/.test(normalized)
    ) return null;
    return normalized;
  }

  function cleanVisibleTitle(value) {
    const title = boundedText(value);
    if (
      !title || title.length > 200 ||
      /^chatgpt(?:\s+-|$)/i.test(title) ||
      PROJECT_ACCESSIBILITY_PHRASE.test(title)
    ) return null;
    return title;
  }

  function visible(element, windowValue) {
    if (!element || element.hidden || element.getAttribute("aria-hidden") === "true") return false;
    const style = windowValue.getComputedStyle(element);
    return style.display !== "none" && style.visibility !== "hidden";
  }

  function linkIdentity(href, baseHref, origin, canonicalProjectReference) {
    let url;
    try {
      url = new URL(href, baseHref);
    } catch (_) {
      return null;
    }
    if (!ALLOWED_ORIGINS.has(origin) || url.origin !== origin) return null;
    const chatMatch = url.pathname.match(/\/c\/([^/?#]+)/);
    if (!chatMatch) return null;
    const projectMatch = url.pathname.match(/\/g\/([^/?#]+)(?:\/[^/?#]+)*\/c\//);
    return {
      host_chat_reference: decodeURIComponent(chatMatch[1]),
      host_project_reference: projectMatch
        ? canonicalProjectReference(decodeURIComponent(projectMatch[1]))
        : null,
    };
  }

  function observationForLink(link, windowValue, canonicalProjectReference) {
    if (!visible(link, windowValue)) return null;
    const identity = linkIdentity(
      link.getAttribute("href") || link.href,
      windowValue.location.href,
      windowValue.location.origin,
      canonicalProjectReference
    );
    if (!identity) return null;
    const visibleTitle = cleanVisibleTitle(link.innerText || link.textContent || "");
    const ariaLabel = boundedText(link.getAttribute("aria-label"));
    if (!visibleTitle && !ariaLabel) return null;
    return {
      ...identity,
      visible_title: visibleTitle,
      aria_label: ariaLabel,
    };
  }

  function scanVisibleConversationLinks(documentValue, windowValue, canonicalProjectReference) {
    const grouped = new Map();
    for (const link of Array.from(documentValue.querySelectorAll("nav a[href], aside a[href]"))) {
      const observation = observationForLink(link, windowValue, canonicalProjectReference);
      if (!observation) continue;
      const values = grouped.get(observation.host_chat_reference) || [];
      values.push(observation);
      grouped.set(observation.host_chat_reference, values);
    }
    const observations = [];
    for (const values of grouped.values()) {
      const signatures = new Set(values.map((value) => JSON.stringify(value)));
      if (signatures.size !== 1) continue;
      observations.push(values[0]);
    }
    observations.sort((left, right) =>
      left.host_chat_reference.localeCompare(right.host_chat_reference)
    );
    return observations.slice(0, MAX_OBSERVATIONS);
  }

  class SidebarTitleReconciler {
    constructor(options) {
      this.documentValue = options.documentValue;
      this.windowValue = options.windowValue;
      this.canonicalProjectReference = options.canonicalProjectReference;
      this.send = options.send;
      this.schedule = options.schedule || ((callback) => this.windowValue.setTimeout(callback, 250));
      this.observer = null;
      this.scheduled = false;
      this.sentSignatures = new Map();
    }

    start() {
      this.scan();
      this.observer = new this.windowValue.MutationObserver(() => this.requestScan());
      this.observer.observe(this.documentValue.documentElement, {
        childList: true,
        subtree: true,
        characterData: true,
        attributes: true,
        attributeFilter: ["aria-label", "href", "hidden", "aria-hidden"],
      });
    }

    requestScan() {
      if (this.scheduled) return;
      this.scheduled = true;
      this.schedule(() => {
        this.scheduled = false;
        this.scan();
      });
    }

    scan() {
      const observations = scanVisibleConversationLinks(
        this.documentValue,
        this.windowValue,
        this.canonicalProjectReference
      );
      const changed = observations.filter((observation) => {
        const signature = JSON.stringify(observation);
        if (this.sentSignatures.get(observation.host_chat_reference) === signature) return false;
        this.sentSignatures.set(observation.host_chat_reference, signature);
        return true;
      });
      if (changed.length) Promise.resolve(this.send(changed)).catch(() => undefined);
    }

    stop() {
      if (this.observer) this.observer.disconnect();
      this.observer = null;
    }
  }

  return {
    SidebarTitleReconciler,
    cleanVisibleTitle,
    linkIdentity,
    normalizedText,
    observationForLink,
    scanVisibleConversationLinks,
  };
});
