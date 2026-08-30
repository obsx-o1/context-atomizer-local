(function (root) {
  "use strict";
  if (!root.AtomizerChatCapture || !root.AtomizerChatTitles || !root.AtomizerWebExtensionApi) return;
  const CONTENT_RUNTIME_BUILD = "local-capture-v1";
  if (root.__ATOMIZER_LOCAL_CONTENT_RUNTIME__ === CONTENT_RUNTIME_BUILD) return;
  root.__ATOMIZER_LOCAL_CONTENT_RUNTIME__ = CONTENT_RUNTIME_BUILD;
  const adapter = new root.AtomizerChatSelectors.DomCaptureAdapter(document, window);
  const routeProvider = () => root.AtomizerChatConversation.routeIdentity(window.location, document);
  const capture = new root.AtomizerChatCapture.ChatGPTCapture({
    adapter,
    routeProvider,
    send: (event) => root.AtomizerWebExtensionApi.sendMessage({ type: "atomizer.capture", event }),
  });
  const watcher = new root.AtomizerChatConversation.RouteWatcher(window, () => capture.routeChanged());
  const titleReconciler = new root.AtomizerChatTitles.SidebarTitleReconciler({
    documentValue: document,
    windowValue: window,
    canonicalProjectReference: root.AtomizerChatConversation.canonicalProjectReference,
    send: (observations) => root.AtomizerWebExtensionApi.sendMessage({
      type: "atomizer.chat-titles",
      observations,
    }),
  });
  watcher.start();
  capture.start();
  titleReconciler.start();
})(globalThis);
