(function (root, factory) {
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  root.AtomizerClaudeSelectors = api;
})(globalThis, function () {
  "use strict";
  const COMPOSER = '[data-testid="chat-input"][contenteditable="true"], .ProseMirror[contenteditable="true"], textarea[aria-label*="message" i]';
  const SEND = 'button[data-testid="send-button"], button[aria-label^="Send" i]';
  const ASSISTANT = '[data-testid="assistant-message"], [data-message-author-role="assistant"], .font-claude-response';
  function visible(element) {
    if (!element || element.hidden || element.getAttribute("aria-hidden") === "true") return false;
    const view = element.ownerDocument && element.ownerDocument.defaultView;
    const style = view && view.getComputedStyle ? view.getComputedStyle(element) : null;
    return !style || (style.display !== "none" && style.visibility !== "hidden");
  }
  class ClaudeDomCaptureAdapter {
    constructor(documentValue, windowValue) {
      this.documentValue = documentValue;
      this.windowValue = windowValue;
      this.cleanups = [];
    }
    readComposer() {
      const value = this.documentValue.querySelector(COMPOSER);
      return visible(value) ? (value.value || value.innerText || value.textContent || "").trim() : "";
    }
    onSubmitIntent(callback) { this._submits(this.windowValue, callback); }
    onSubmit(callback) { this._submits(this.documentValue, callback); }
    _submits(target, callback) {
      const click = (event) => event.target && event.target.closest && event.target.closest(SEND) && callback("click");
      const key = (event) => event.key === "Enter" && !event.shiftKey && !event.isComposing && event.target && event.target.closest && event.target.closest(COMPOSER) && callback("enter");
      target.addEventListener("click", click, true);
      target.addEventListener("keydown", key, true);
      this.cleanups.push(() => target.removeEventListener("click", click, true));
      this.cleanups.push(() => target.removeEventListener("keydown", key, true));
    }
    listAssistantMessages() {
      return Array.from(this.documentValue.querySelectorAll(ASSISTANT)).map((element, index) => {
        const container = element.closest("[data-message-id]") || element;
        return {
          reference: container.getAttribute("data-message-id") || element.id || "assistant:" + index,
          content: (element.innerText || element.textContent || "").trim(),
          visible: visible(element),
          streaming: container.getAttribute("data-is-streaming") === "true" || Boolean(this.documentValue.querySelector('button[aria-label*="Stop" i]')),
        };
      });
    }
    onDomChanged(callback) {
      const observer = new this.windowValue.MutationObserver(callback);
      observer.observe(this.documentValue.documentElement, { childList: true, subtree: true, characterData: true });
      this.cleanups.push(() => observer.disconnect());
    }
    dispose() { for (const cleanup of this.cleanups.splice(0)) cleanup(); }
  }
  return { ClaudeDomCaptureAdapter };
});
