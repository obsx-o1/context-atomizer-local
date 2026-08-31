(function (root, factory) {
  const api = factory(root.AtomizerChatTypes, root.AtomizerChatDedupe);
  if (typeof module === "object" && module.exports) {
    module.exports = factory(require("./types.js"), require("./dedupe.js"));
  } else {
    root.AtomizerChatCapture = api;
  }
})(globalThis, function (types, dedupe) {
  "use strict";

  class ChatGPTCapture {
    constructor(options) {
      this.adapter = options.adapter;
      this.routeProvider = options.routeProvider;
      this.send = options.send;
      this.host = options.host || "chatgpt_web";
      this.now = options.now || (() => Date.now());
      this.schedule = options.schedule || ((callback, delay) => setTimeout(callback, delay));
      const cryptoValue = options.crypto || globalThis.crypto;
      this.captureInstanceReference = options.captureInstanceReference || (
        cryptoValue && typeof cryptoValue.randomUUID === "function"
          ? cryptoValue.randomUUID()
          : types.stableHash(String(this.now()) + ":" + String(Math.random()))
      );
      this.assistantTracker = new dedupe.StableAssistantTracker(options.stableMilliseconds || 700);
      this.eventDedupe = new dedupe.DedupeWindow(2000);
      this.submitSequence = 0;
      this.recentSubmission = null;
      this.pendingNewChat = null;
      this.pendingRoutePollScheduled = false;
      this.pendingRoutePollRemaining = 0;
      this.pendingRoutePollMilliseconds = options.pendingRoutePollMilliseconds || 250;
      this.pendingRoutePollLimit = options.pendingRoutePollLimit || 120;
      this.recentProjectRouteMilliseconds = options.recentProjectRouteMilliseconds || 30000;
      this.recentProjectStableRoute = null;
      this.trustedProjectHome = null;
      this.deliveryTail = null;
      this.scanScheduled = false;
    }

    start() {
      if (typeof this.adapter.onSubmitIntent === "function") {
        this.adapter.onSubmitIntent(() => this.snapshotProjectHomeForSubmission());
      }
      this.adapter.onSubmit(() => this.captureUserSubmit());
      this.adapter.onDomChanged(() => this.scheduleScan());
      this.scheduleScan();
    }

    snapshotProjectHomeForSubmission() {
      this.trustedProjectHome = null;
      try {
        const route = this.routeProvider();
        if (!route || !route.isProjectHomeRoute) {
          return false;
        }
        this.observeTrustedProjectHome(route);
        return Boolean(this.trustedProjectHome);
      } catch (_) {
        return false;
      }
    }

    captureUserSubmit() {
      try {
        const content = this.adapter.readComposer();
        if (!content) return false;
        const observedRoute = this.routeProvider();
        this.observeTrustedProjectHome(observedRoute);
        const route = this.routeWithTrustedProjectHome(observedRoute);
        const now = this.now();
        let ignoredAssistantReferences = new Set();
        if (route.isProjectHomeRoute) {
          this.recentProjectStableRoute = null;
          try {
            ignoredAssistantReferences = new Set(
              this.adapter.listAssistantMessages().map((message) => message.reference)
            );
          } catch (_) {
            ignoredAssistantReferences = new Set();
          }
        }
        const signature = route.hostChatReference + "\u001f" + content;
        let messageReference;
        if (this.recentSubmission && this.recentSubmission.signature === signature && now - this.recentSubmission.at < 1000) {
          messageReference = this.recentSubmission.reference;
        } else {
          this.submitSequence += 1;
          messageReference = "submit:" + now + ":" + this.submitSequence;
          this.recentSubmission = { signature, at: now, reference: messageReference };
        }
        const captureRoute = route.isNewChatRoute
          ? {
              ...route,
              hostChatReference: "provisional:new-chat:" + this.captureInstanceReference + ":" + types.stableHash(messageReference),
              chatDisplayName: null,
            }
          : route;
        const event = types.makeChatEvent({
          host: this.host,
          ...captureRoute,
          role: "user",
          content,
          messageReference,
          capturedAt: now,
        });
        if (route.isNewChatRoute) {
          this.pendingNewChat = {
            event,
            provisionalReference: captureRoute.hostChatReference,
            hostProjectReference: event.host_project_reference || null,
            projectDisplayName: event.project_display_name || null,
            hostTurnReference: event.host_turn_reference,
            ignoredAssistantReferences,
            assistantCaptured: false,
          };
          this.pendingRoutePollRemaining = this.pendingRoutePollLimit;
        }
        const emitted = this.emit(event);
        if (emitted && event.project_display_name) this.trustedProjectHome = null;
        if (emitted && route.isNewChatRoute) this.schedulePendingRoutePoll();
        return emitted;
      } catch (_) {
        return false;
      }
    }

    scheduleScan() {
      if (this.scanScheduled) return;
      this.scanScheduled = true;
      this.schedule(() => {
        this.scanScheduled = false;
        this.scanVisibleAssistantMessages();
      }, 100);
    }

    scanVisibleAssistantMessages() {
      try {
        const route = this.routeProvider();
        this.observeTrustedProjectHome(route);
        this.clearRecentProjectRouteOutside(route);
        this.reconcilePendingNewChat(route);
        const now = this.now();
        const association = this.assistantAssociation(route, now);
        if (!association) return true;
        for (const message of this.adapter.listAssistantMessages()) {
          if (association.ignoredAssistantReferences.has(message.reference)) continue;
          if (!this.assistantTracker.observe(message, now)) continue;
          const emitted = this.emit(types.makeChatEvent({
            host: this.host,
            ...association.route,
            role: "assistant",
            content: message.content,
            messageReference: message.reference,
            capturedAt: now,
          }));
          if (
            emitted && association.pending &&
            association.pending === this.pendingNewChat
          ) {
            this.pendingNewChat.assistantCaptured = true;
          }
          if (
            emitted && this.recentProjectStableRoute &&
            association.route.hostChatReference ===
              this.recentProjectStableRoute.route.hostChatReference
          ) {
            this.recentProjectStableRoute = null;
          }
        }
      } catch (_) {
        return false;
      }
      return true;
    }

    routeChanged() {
      this.assistantTracker.reset();
      const route = this.routeProvider();
      this.observeTrustedProjectHome(route);
      this.clearRecentProjectRouteOutside(route);
      this.reconcilePendingNewChat(route);
      this.scheduleScan();
    }

    reconcilePendingNewChat(route) {
      if (!this.pendingNewChat || !route || !route.hasStableChatReference) return false;
      const pending = this.pendingNewChat;
      if ((route.hostProjectReference || null) !== pending.hostProjectReference) return false;
      const reboundRoute = pending.projectDisplayName
        ? { ...route, projectDisplayName: pending.projectDisplayName }
        : route;
      const rebound = types.makeChatEvent({
        host: this.host,
        ...reboundRoute,
        role: pending.event.role,
        content: pending.event.content,
        messageReference: pending.event.host_turn_reference,
        capturedAt: pending.event.captured_at,
        rebindFromHostChatReference: pending.provisionalReference,
      });
      if (!this.emit(rebound)) return false;
      if (pending.hostProjectReference && !pending.assistantCaptured) {
        this.recentProjectStableRoute = {
          route: { ...reboundRoute },
          hostProjectReference: pending.hostProjectReference,
          ignoredAssistantReferences: pending.ignoredAssistantReferences,
          expiresAt: this.now() + this.recentProjectRouteMilliseconds,
        };
      } else {
        this.recentProjectStableRoute = null;
      }
      this.pendingNewChat = null;
      this.pendingRoutePollRemaining = 0;
      return true;
    }

    assistantAssociation(route, now) {
      if (!route) return null;
      if (!route.isProjectHomeRoute) {
        return {
          route,
          ignoredAssistantReferences: new Set(),
          pending: null,
        };
      }
      const projectReference = route.hostProjectReference || null;
      const pending = this.pendingNewChat;
      if (pending && pending.hostProjectReference === projectReference) {
        return {
          route: {
            ...route,
            hostChatReference: pending.provisionalReference,
            projectDisplayName: pending.projectDisplayName || null,
            chatDisplayName: null,
          },
          ignoredAssistantReferences: pending.ignoredAssistantReferences,
          pending,
        };
      }
      const recent = this.recentProjectStableRoute;
      if (!recent) return null;
      if (recent.expiresAt < now) {
        this.recentProjectStableRoute = null;
        return null;
      }
      if (recent.hostProjectReference !== projectReference) return null;
      return {
        route: recent.route,
        ignoredAssistantReferences: recent.ignoredAssistantReferences,
        pending: null,
      };
    }

    observeTrustedProjectHome(route) {
      if (!route) return;
      const projectReference = route.hostProjectReference || null;
      if (route.isProjectHomeRoute) {
        this.trustedProjectHome = projectReference && route.projectDisplayName
          ? {
              hostProjectReference: projectReference,
              projectDisplayName: route.projectDisplayName,
            }
          : null;
        return;
      }
      if (
        this.trustedProjectHome &&
        projectReference !== this.trustedProjectHome.hostProjectReference
      ) {
        this.trustedProjectHome = null;
      }
    }

    routeWithTrustedProjectHome(route) {
      const trusted = this.trustedProjectHome;
      if (
        !route || route.projectDisplayName || !trusted ||
        (route.hostProjectReference || null) !== trusted.hostProjectReference
      ) return route;
      return { ...route, projectDisplayName: trusted.projectDisplayName };
    }

    clearRecentProjectRouteOutside(route) {
      const recent = this.recentProjectStableRoute;
      if (!recent || !route) return;
      if ((route.hostProjectReference || null) !== recent.hostProjectReference) {
        this.recentProjectStableRoute = null;
        return;
      }
      if (
        route.hasStableChatReference &&
        route.hostChatReference !== recent.route.hostChatReference
      ) {
        this.recentProjectStableRoute = null;
      }
    }

    schedulePendingRoutePoll() {
      if (
        !this.pendingNewChat || this.pendingRoutePollScheduled ||
        this.pendingRoutePollRemaining <= 0
      ) return;
      this.pendingRoutePollScheduled = true;
      this.schedule(() => {
        this.pendingRoutePollScheduled = false;
        if (!this.pendingNewChat) return;
        this.pendingRoutePollRemaining -= 1;
        let route = null;
        try {
          route = this.routeProvider();
        } catch (_) {
          route = null;
        }
        if (!this.reconcilePendingNewChat(route)) this.schedulePendingRoutePoll();
      }, this.pendingRoutePollMilliseconds);
    }

    emit(event) {
      if (!this.eventDedupe.first(event.event_id)) return false;
      const deliver = () => {
        try {
          return this.send(event);
        } catch (_) {
          return undefined;
        }
      };
      try {
        if (this.deliveryTail) {
          const tail = this.deliveryTail.then(deliver, deliver).catch(() => undefined);
          this.deliveryTail = tail;
          tail.finally(() => {
            if (this.deliveryTail === tail) this.deliveryTail = null;
          });
        } else {
          const result = deliver();
          if (result && typeof result.then === "function") {
            const tail = Promise.resolve(result).catch(() => undefined);
            this.deliveryTail = tail;
            tail.finally(() => {
              if (this.deliveryTail === tail) this.deliveryTail = null;
            });
          }
        }
      } catch (_) {
        return false;
      }
      return true;
    }
  }

  return { ChatGPTCapture, WebCapture: ChatGPTCapture };
});
