(function (root) {
  "use strict";
  if (!root.AtomizerChatCapture || !root.AtomizerClaudeConversation || !root.AtomizerClaudeSelectors || !root.AtomizerWebExtensionApi) return;
  if (root.__ATOMIZER_LOCAL_CLAUDE_RUNTIME__) return;
  root.__ATOMIZER_LOCAL_CLAUDE_RUNTIME__ = "claude-capture-v1";
  const adapter = new root.AtomizerClaudeSelectors.ClaudeDomCaptureAdapter(document, window);
  const routeProvider = () => root.AtomizerClaudeConversation.routeIdentity(window.location, document);
  const capture = new root.AtomizerChatCapture.WebCapture({
    adapter,
    routeProvider,
    host: "claude_web",
    send: (event) => root.AtomizerWebExtensionApi.sendMessage({ type: "atomizer.capture", event }),
  });
  const watcher = new root.AtomizerClaudeConversation.RouteWatcher(window, () => capture.routeChanged());
  watcher.start();
  capture.start();
})(globalThis);
