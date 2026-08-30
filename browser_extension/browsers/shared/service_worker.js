(function (root) {
  "use strict";
  if (!root.AtomizerWebExtensionApi && typeof importScripts === "function") importScripts("api.js");
  const api = root.AtomizerWebExtensionApi;
  if (!api) return;

  const PROTOCOL_VERSION = "1";
  const BRIDGE_PORT = 43117;
  const BRIDGE_ORIGIN = "http://127.0.0.1:43117";
  const RUNTIME_PROOF_DOMAIN = "context-atomizer-local/runtime-proof/v1";
  const CAPTURE_REQUEST_DOMAIN = "context-atomizer-local/capture-request/v1";
  const PAIRING_DOMAIN = "context-atomizer-local/pairing/v1";
  const encoder = new TextEncoder();

  function bytesToHex(bytes) {
    return Array.from(bytes, (value) => value.toString(16).padStart(2, "0")).join("");
  }

  function randomNonce() {
    const bytes = new Uint8Array(32);
    root.crypto.getRandomValues(bytes);
    let binary = "";
    for (const value of bytes) binary += String.fromCharCode(value);
    return btoa(binary).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/g, "");
  }

  function constantTimeEqual(left, right) {
    if (typeof left !== "string" || typeof right !== "string" || left.length !== right.length) return false;
    let difference = 0;
    for (let index = 0; index < left.length; index += 1) {
      difference |= left.charCodeAt(index) ^ right.charCodeAt(index);
    }
    return difference === 0;
  }

  function sha256Hex(value) {
    return root.crypto.subtle.digest("SHA-256", encoder.encode(value)).then(
      (digest) => bytesToHex(new Uint8Array(digest)),
    );
  }

  function hmacHex(secret, material) {
    return root.crypto.subtle.importKey(
      "raw",
      encoder.encode(secret),
      { name: "HMAC", hash: "SHA-256" },
      false,
      ["sign"],
    ).then((key) => root.crypto.subtle.sign("HMAC", key, encoder.encode(material))).then(
      (signature) => bytesToHex(new Uint8Array(signature)),
    );
  }

  function recordRuntimeStatus(response) {
    const headers = response && response.headers;
    const protocol = headers && typeof headers.get === "function"
      ? headers.get("X-Atomizer-Protocol-Version")
      : null;
    const runtimeBuild = headers && typeof headers.get === "function"
      ? headers.get("X-Atomizer-Runtime-Build")
      : null;
    const restartHeader = headers && typeof headers.get === "function"
      ? headers.get("X-Atomizer-Restart-Required")
      : null;
    return api.storageSet({
      atomizerRuntimeHealth: {
        bridgeReachable: Boolean(response),
        protocolVersion: protocol,
        runtimeBuild,
        restartRequired: protocol !== PROTOCOL_VERSION || restartHeader === "true",
      },
    }).catch(() => undefined);
  }

  function recordBridgeUnavailable() {
    return api.storageSet({
      atomizerRuntimeHealth: {
        bridgeReachable: false,
        protocolVersion: null,
        runtimeBuild: null,
        restartRequired: false,
      },
    }).catch(() => undefined);
  }

  function pairedSettings() {
    return api.storageGet({ extensionSecret: "" }).then((settings) => (
      typeof settings.extensionSecret === "string" && settings.extensionSecret.length >= 43
        ? settings
        : null
    ));
  }

  function pair(pairingCode) {
    if (typeof pairingCode !== "string" || !pairingCode.trim()) {
      return Promise.resolve({ ok: false });
    }
    return fetch(BRIDGE_ORIGIN + "/v1/pair", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        protocolVersion: PROTOCOL_VERSION,
        pairingDomain: PAIRING_DOMAIN,
        pairingCode: pairingCode.trim(),
      }),
      cache: "no-store",
      credentials: "omit",
    }).then((response) => {
      if (!response.ok || typeof response.json !== "function") throw new Error("pairing rejected");
      return response.json().then((payload) => ({ response, payload }));
    }).then(({ response, payload }) => {
      if (
        !payload || payload.ok !== true || payload.protocolVersion !== PROTOCOL_VERSION ||
        payload.pairingDomain !== PAIRING_DOMAIN ||
        typeof payload.extensionSecret !== "string" || payload.extensionSecret.length < 43
      ) throw new Error("pairing response invalid");
      return api.storageSet({ extensionSecret: payload.extensionSecret }).then(() =>
        recordRuntimeStatus(response).then(() => ({ ok: true })),
      );
    }).catch(() => recordBridgeUnavailable().then(() => ({ ok: false })));
  }

  function proveRuntime(secret) {
    const challengeNonce = randomNonce();
    return fetch(BRIDGE_ORIGIN + "/v1/runtime-proof", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ protocolVersion: PROTOCOL_VERSION, challengeNonce }),
      cache: "no-store",
      credentials: "omit",
    }).then((response) => {
      if (!response.ok || typeof response.json !== "function") throw new Error("runtime proof rejected");
      return response.json().then((payload) => ({ response, payload }));
    }).then(({ response, payload }) => {
      if (
        !payload || payload.ok !== true || payload.protocolVersion !== PROTOCOL_VERSION ||
        payload.port !== BRIDGE_PORT || payload.challengeNonce !== challengeNonce ||
        typeof payload.proof !== "string"
      ) throw new Error("runtime proof invalid");
      const material = [
        RUNTIME_PROOF_DOMAIN,
        PROTOCOL_VERSION,
        challengeNonce,
        String(BRIDGE_PORT),
      ].join("\n");
      return hmacHex(secret, material).then((expected) => {
        if (!constantTimeEqual(expected, payload.proof)) throw new Error("runtime proof mismatch");
        return recordRuntimeStatus(response).then(() => true);
      });
    }).catch(() => recordBridgeUnavailable().then(() => false));
  }

  function forward(endpointPath, payload) {
    return pairedSettings().then((settings) => {
      if (!settings) return { ok: false };
      return proveRuntime(settings.extensionSecret).then((proved) => {
        if (!proved) return { ok: false };
        const body = JSON.stringify(payload);
        const nonce = randomNonce();
        const timestamp = String(Math.floor(Date.now() / 1000));
        return sha256Hex(body).then((bodySha256) => {
          const material = [
            CAPTURE_REQUEST_DOMAIN,
            PROTOCOL_VERSION,
            "POST",
            endpointPath,
            nonce,
            timestamp,
            bodySha256,
          ].join("\n");
          return hmacHex(settings.extensionSecret, material).then((signature) => fetch(
            BRIDGE_ORIGIN + endpointPath,
            {
              method: "POST",
              headers: {
                "Content-Type": "application/json",
                "X-Atomizer-Protocol": PROTOCOL_VERSION,
                "X-Atomizer-Nonce": nonce,
                "X-Atomizer-Timestamp": timestamp,
                "X-Atomizer-Content-SHA256": bodySha256,
                "X-Atomizer-Signature": signature,
              },
              body,
              cache: "no-store",
              credentials: "omit",
            },
          )).then((response) => recordRuntimeStatus(response).then(() => ({ ok: response.ok })));
        });
      });
    }).catch(() => recordBridgeUnavailable().then(() => ({ ok: false })));
  }

  function recoverOpenChatTabs() {
    const manifest = api.getManifest();
    const contentScripts = manifest && Array.isArray(manifest.content_scripts)
      ? manifest.content_scripts[0]
      : null;
    const matches = contentScripts && Array.isArray(contentScripts.matches) ? contentScripts.matches : [];
    const files = contentScripts && Array.isArray(contentScripts.js) ? contentScripts.js : [];
    if (!matches.length || !files.length) return Promise.resolve();
    return api.tabsQuery({ url: matches }).then((tabs) => {
      const candidates = (tabs || []).filter((tab) => Number.isInteger(tab.id));
      return Promise.all(candidates.map((tab) => api.executeScript({
        target: { tabId: tab.id },
        files,
      }).then(() => true, () => false))).then((results) => api.storageSet({
        atomizerContentRecovery: {
          attempted: candidates.length,
          succeeded: results.filter(Boolean).length,
          failed: results.filter((value) => !value).length,
        },
      }));
    }).catch(() => api.storageSet({
      atomizerContentRecovery: { attempted: 0, succeeded: 0, failed: 1 },
    }).catch(() => undefined));
  }

  if (api.onInstalled && typeof api.onInstalled.addListener === "function") {
    api.onInstalled.addListener(() => recoverOpenChatTabs());
  }
  if (api.onStartup && typeof api.onStartup.addListener === "function") {
    api.onStartup.addListener(() => pairedSettings().then((settings) => (
      settings ? proveRuntime(settings.extensionSecret) : false
    )));
  }

  api.onMessage.addListener((message) => {
    if (!message) return undefined;
    if (message.type === "atomizer.pair") return pair(message.pairingCode);
    if (message.type === "atomizer.runtime-proof") {
      return pairedSettings().then((settings) => (
        settings ? proveRuntime(settings.extensionSecret) : false
      )).then((ok) => ({ ok }));
    }
    if (message.type === "atomizer.capture" && message.event) {
      return forward("/v1/chat-events", message.event);
    }
    if (message.type === "atomizer.chat-titles" && Array.isArray(message.observations)) {
      return forward("/v1/chat-titles", { observations: message.observations });
    }
    return undefined;
  });
})(globalThis);
