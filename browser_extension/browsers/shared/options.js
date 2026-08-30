(function (root) {
  "use strict";
  const api = root.AtomizerWebExtensionApi;
  const state = document.getElementById("connection-state");
  const status = document.getElementById("status");
  function refresh() {
    return api.storageGet({ atomizerRuntimeHealth: null, extensionSecret: "" }).then((settings) => {
      const health = settings.atomizerRuntimeHealth;
      state.textContent = settings.extensionSecret
        ? (health && health.bridgeReachable ? "Paired with Atomizer Local" : "Paired; runtime not currently verified")
        : "Not paired";
    });
  }
  refresh();
  document.getElementById("pair").addEventListener("click", () => {
    const input = document.getElementById("pairing-code");
    status.value = "Pairing…";
    api.sendMessage({ type: "atomizer.pair", pairingCode: input.value }).then((result) => {
      status.value = result && result.ok ? "Paired." : "Pairing was rejected.";
      if (result && result.ok) input.value = "";
      return refresh();
    });
  });
  document.getElementById("reconnect").addEventListener("click", () => {
    status.value = "Verifying…";
    api.sendMessage({ type: "atomizer.runtime-proof" }).then((result) => {
      status.value = result && result.ok ? "Runtime verified." : "Runtime proof failed.";
      return refresh();
    });
  });
})(globalThis);
