(function (root, factory) {
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  root.AtomizerChatDedupe = api;
})(globalThis, function () {
  "use strict";

  class DedupeWindow {
    constructor(limit) {
      this.limit = limit || 1000;
      this.values = new Set();
      this.order = [];
    }

    first(key) {
      if (this.values.has(key)) return false;
      this.values.add(key);
      this.order.push(key);
      while (this.order.length > this.limit) this.values.delete(this.order.shift());
      return true;
    }
  }

  class StableAssistantTracker {
    constructor(stableMilliseconds) {
      this.stableMilliseconds = stableMilliseconds || 700;
      this.states = new Map();
      this.emitted = new DedupeWindow(2000);
    }

    observe(message, now) {
      if (!message.visible || message.streaming || !message.content) return false;
      const key = message.reference;
      const prior = this.states.get(key);
      if (!prior || prior.content !== message.content) {
        this.states.set(key, { content: message.content, firstSeenAt: now });
        return false;
      }
      if (now - prior.firstSeenAt < this.stableMilliseconds) return false;
      return this.emitted.first(key + "\u001f" + message.content);
    }

    reset() {
      this.states.clear();
    }
  }

  return { DedupeWindow, StableAssistantTracker };
});

