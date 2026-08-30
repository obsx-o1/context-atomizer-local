(function (root, factory) {
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  root.AtomizerChatSelectors = api;
})(globalThis, function () {
  "use strict";

  const COMPOSER = '#prompt-textarea, textarea[data-testid="prompt-textarea"], [contenteditable="true"][data-testid="prompt-textarea"]';
  const SEND_BUTTON = '[data-testid="send-button"], button[aria-label*="Send"]';
  const ASSISTANT_MESSAGE = '[data-message-author-role="assistant"]';

  function isVisible(element) {
    if (!element || element.hidden || element.getAttribute("aria-hidden") === "true") return false;
    const view = element.ownerDocument && element.ownerDocument.defaultView;
    if (view && view.getComputedStyle) {
      const style = view.getComputedStyle(element);
      if (style.display === "none" || style.visibility === "hidden") return false;
    }
    return true;
  }

  function visibleText(element) {
    if (!isVisible(element)) return "";
    return (element.innerText || element.textContent || "").trim();
  }

  class DomCaptureAdapter {
    constructor(documentValue, windowValue) {
      this.documentValue = documentValue;
      this.windowValue = windowValue;
      this.cleanups = [];
    }

    readComposer() {
      const composer = this.documentValue.querySelector(COMPOSER);
      if (!isVisible(composer)) return "";
      return (composer.value || composer.innerText || composer.textContent || "").trim();
    }

    onSubmitIntent(callback) {
      const pointerdown = (event) => {
        if (event.target && event.target.closest && event.target.closest(SEND_BUTTON)) callback("click");
      };
      const keydown = (event) => {
        if (
          event.key === "Enter" && !event.shiftKey && !event.isComposing &&
          event.target && event.target.closest && event.target.closest(COMPOSER)
        ) callback("enter");
      };
      const submit = (event) => {
        if (event.target && event.target.querySelector && event.target.querySelector(COMPOSER)) callback("form");
      };
      this.windowValue.addEventListener("pointerdown", pointerdown, true);
      this.windowValue.addEventListener("keydown", keydown, true);
      this.windowValue.addEventListener("submit", submit, true);
      this.cleanups.push(() => this.windowValue.removeEventListener("pointerdown", pointerdown, true));
      this.cleanups.push(() => this.windowValue.removeEventListener("keydown", keydown, true));
      this.cleanups.push(() => this.windowValue.removeEventListener("submit", submit, true));
    }

    onSubmit(callback) {
      const click = (event) => {
        if (event.target && event.target.closest && event.target.closest(SEND_BUTTON)) callback("click");
      };
      const keydown = (event) => {
        if (
          event.key === "Enter" && !event.shiftKey && !event.isComposing &&
          event.target && event.target.closest && event.target.closest(COMPOSER)
        ) callback("enter");
      };
      const submit = (event) => {
        if (event.target && event.target.querySelector && event.target.querySelector(COMPOSER)) callback("form");
      };
      this.documentValue.addEventListener("click", click, true);
      this.documentValue.addEventListener("keydown", keydown, true);
      this.documentValue.addEventListener("submit", submit, true);
      this.cleanups.push(() => this.documentValue.removeEventListener("click", click, true));
      this.cleanups.push(() => this.documentValue.removeEventListener("keydown", keydown, true));
      this.cleanups.push(() => this.documentValue.removeEventListener("submit", submit, true));
    }

    listAssistantMessages() {
      return Array.from(this.documentValue.querySelectorAll(ASSISTANT_MESSAGE)).flatMap((element, index) => {
        const article = element.closest("article") || element;
        const reference = element.getAttribute("data-message-id") || article.id || "assistant:" + index;
        const contentElement = element.querySelector(".markdown");
        if (!contentElement) return [];
        const streaming = Boolean(article.querySelector('[data-testid="stop-button"], [data-is-streaming="true"]'));
        return [{ reference, content: visibleText(contentElement), visible: isVisible(element), streaming }];
      });
    }

    onDomChanged(callback) {
      const observer = new this.windowValue.MutationObserver(callback);
      observer.observe(this.documentValue.documentElement, { childList: true, subtree: true, characterData: true });
      this.cleanups.push(() => observer.disconnect());
    }

    dispose() {
      for (const cleanup of this.cleanups.splice(0)) cleanup();
    }
  }

  return { DomCaptureAdapter, isVisible, visibleText };
});
