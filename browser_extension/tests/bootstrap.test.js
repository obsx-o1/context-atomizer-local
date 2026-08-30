"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const crypto = require("node:crypto");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const { TextEncoder } = require("node:util");

const PACKAGE_ROOT = process.env.ATOMIZER_CHROMIUM_PACKAGE_ROOT
  ? path.resolve(process.env.ATOMIZER_CHROMIUM_PACKAGE_ROOT)
  : path.resolve(__dirname, "../dist/chromium-edge-live");
const SECRET = Buffer.alloc(32, 7).toString("base64url");
const PROOF_DOMAIN = "context-atomizer-local/runtime-proof/v1";
const CAPTURE_DOMAIN = "context-atomizer-local/capture-request/v1";
const PAIRING_DOMAIN = "context-atomizer-local/pairing/v1";

function load(context, relativePath) {
  const filename = path.join(PACKAGE_ROOT, relativePath);
  vm.runInContext(fs.readFileSync(filename, "utf8"), context, { filename });
}

function hmac(material) {
  return crypto.createHmac("sha256", Buffer.from(SECRET, "ascii")).update(material).digest("hex");
}

function response(payload, options = {}) {
  const values = {
    "X-Atomizer-Protocol-Version": "1",
    "X-Atomizer-Runtime-Build": "test-build",
    "X-Atomizer-Restart-Required": "false",
  };
  return {
    ok: options.ok !== false,
    status: options.status || 200,
    headers: { get: (name) => values[name] || null },
    json: () => Promise.resolve(payload),
  };
}

function serviceWorkerContext(fetchImplementation, initial = {}) {
  let listener = null;
  let installedListener = null;
  let startupListener = null;
  const stored = { ...initial };
  const manifest = JSON.parse(
    fs.readFileSync(path.join(PACKAGE_ROOT, "manifest.json"), "utf8"),
  );
  const sandbox = {
    Promise,
    JSON,
    Number,
    Array,
    Date,
    Uint8Array,
    TextEncoder,
    atob: (value) => Buffer.from(value, "base64").toString("binary"),
    btoa: (value) => Buffer.from(value, "binary").toString("base64"),
    crypto: crypto.webcrypto,
    fetch: fetchImplementation,
    chrome: {
      runtime: {
        onMessage: { addListener(callback) { listener = callback; } },
        onInstalled: { addListener(callback) { installedListener = callback; } },
        onStartup: { addListener(callback) { startupListener = callback; } },
        getManifest: () => manifest,
        sendMessage: () => Promise.resolve(),
      },
      storage: {
        local: {
          get(defaults) { return Promise.resolve({ ...defaults, ...stored }); },
          set(values) { Object.assign(stored, JSON.parse(JSON.stringify(values))); return Promise.resolve(); },
        },
      },
      tabs: { query: () => Promise.resolve([]) },
      scripting: { executeScript: () => Promise.resolve([]) },
    },
  };
  sandbox.window = sandbox;
  const context = vm.createContext(sandbox);
  load(context, "browsers/shared/api.js");
  load(context, "browsers/shared/service_worker.js");
  return {
    listener: () => listener,
    installedListener: () => installedListener,
    startupListener: () => startupListener,
    stored,
  };
}

test("packaged manifests grant only the fixed loopback bridge port", () => {
  for (const browser of ["chromium", "firefox"]) {
    const manifest = JSON.parse(fs.readFileSync(
      path.resolve(__dirname, "../browsers", browser, "manifest.json"),
      "utf8",
    ));
    assert.ok(manifest.host_permissions.includes("http://127.0.0.1:43117/*"));
    assert.ok(!manifest.host_permissions.includes("http://127.0.0.1/*"));
  }
});

test("one-time pairing stores only the returned extension secret", async () => {
  const requests = [];
  const harness = serviceWorkerContext((endpoint, options) => {
    requests.push({ endpoint, options });
    return Promise.resolve(response({
      ok: true,
      protocolVersion: "1",
      pairingDomain: PAIRING_DOMAIN,
      extensionSecret: SECRET,
    }));
  });
  const result = await harness.listener()({ type: "atomizer.pair", pairingCode: "one-time-code" });
  assert.equal(result.ok, true);
  assert.equal(harness.stored.extensionSecret, SECRET);
  assert.equal(requests.length, 1);
  assert.equal(requests[0].endpoint, "http://127.0.0.1:43117/v1/pair");
  const pairingRequest = JSON.parse(requests[0].options.body);
  assert.equal(pairingRequest.pairingCode, "one-time-code");
  assert.equal(pairingRequest.pairingDomain, PAIRING_DOMAIN);
  assert.ok(!JSON.stringify(harness.stored.atomizerRuntimeHealth).includes(SECRET));
});

test("capture is sent only after a valid fresh runtime proof and is HMAC bound", async () => {
  const requests = [];
  const harness = serviceWorkerContext((endpoint, options) => {
    requests.push({ endpoint, options });
    if (endpoint.endsWith("/v1/runtime-proof")) {
      const challenge = JSON.parse(options.body).challengeNonce;
      return Promise.resolve(response({
        ok: true,
        protocolVersion: "1",
        port: 43117,
        challengeNonce: challenge,
        proof: hmac([PROOF_DOMAIN, "1", challenge, "43117"].join("\n")),
      }));
    }
    return Promise.resolve(response({ ok: true }));
  }, { extensionSecret: SECRET });
  const event = { event_id: "security-capture", content: "sensitive capture body" };

  const result = await harness.listener()({ type: "atomizer.capture", event });

  assert.equal(result.ok, true);
  assert.equal(requests.length, 2);
  assert.equal(requests[0].endpoint, "http://127.0.0.1:43117/v1/runtime-proof");
  assert.ok(!requests[0].options.body.includes("sensitive capture body"));
  assert.equal(requests[1].endpoint, "http://127.0.0.1:43117/v1/chat-events");
  const headers = requests[1].options.headers;
  const bodySha256 = crypto.createHash("sha256").update(requests[1].options.body).digest("hex");
  assert.equal(headers["X-Atomizer-Content-SHA256"], bodySha256);
  const material = [
    CAPTURE_DOMAIN,
    "1",
    "POST",
    "/v1/chat-events",
    headers["X-Atomizer-Nonce"],
    headers["X-Atomizer-Timestamp"],
    bodySha256,
  ].join("\n");
  assert.equal(headers["X-Atomizer-Signature"], hmac(material));
  assert.equal(headers.Authorization, undefined);
});

test("failed or altered runtime proof sends no capture body and has no fallback", async () => {
  for (const mutation of ["wrong-proof", "wrong-port", "wrong-nonce"]) {
    const requests = [];
    const harness = serviceWorkerContext((endpoint, options) => {
      requests.push({ endpoint, options });
      const challenge = JSON.parse(options.body).challengeNonce;
      return Promise.resolve(response({
        ok: true,
        protocolVersion: "1",
        port: mutation === "wrong-port" ? 43118 : 43117,
        challengeNonce: mutation === "wrong-nonce" ? challenge + "x" : challenge,
        proof: mutation === "wrong-proof"
          ? "0".repeat(64)
          : hmac([PROOF_DOMAIN, "1", challenge, "43117"].join("\n")),
      }));
    }, { extensionSecret: SECRET });

    const result = await harness.listener()({
      type: "atomizer.capture",
      event: { content: "must never leave after failed proof" },
    });
    assert.equal(result.ok, false, mutation);
    assert.equal(requests.length, 1, mutation);
    assert.equal(requests[0].endpoint, "http://127.0.0.1:43117/v1/runtime-proof");
    assert.ok(!JSON.stringify(requests).includes("must never leave after failed proof"));
  }
});

test("each authenticated request uses a different nonce and no alternate port probing", async () => {
  const requests = [];
  const harness = serviceWorkerContext((endpoint, options) => {
    requests.push({ endpoint, options });
    if (endpoint.endsWith("/v1/runtime-proof")) {
      const challenge = JSON.parse(options.body).challengeNonce;
      return Promise.resolve(response({
        ok: true,
        protocolVersion: "1",
        port: 43117,
        challengeNonce: challenge,
        proof: hmac([PROOF_DOMAIN, "1", challenge, "43117"].join("\n")),
      }));
    }
    return Promise.resolve(response({ ok: true }));
  }, { extensionSecret: SECRET });

  await harness.listener()({ type: "atomizer.capture", event: { event_id: "first" } });
  await harness.listener()({ type: "atomizer.chat-titles", observations: [{ visible_title: "second" }] });
  const captures = requests.filter((value) => !value.endpoint.endsWith("/v1/runtime-proof"));
  assert.equal(captures.length, 2);
  assert.notEqual(
    captures[0].options.headers["X-Atomizer-Nonce"],
    captures[1].options.headers["X-Atomizer-Nonce"],
  );
  assert.ok(requests.every((value) => value.endpoint.startsWith("http://127.0.0.1:43117/")));
  assert.ok(requests.every((value) => !value.endpoint.includes("/v1/bootstrap")));
});

test("extension update recovery remains manifest-owned and does not bootstrap credentials", async () => {
  const requests = [];
  const harness = serviceWorkerContext((endpoint, options) => {
    requests.push({ endpoint, options });
    return Promise.reject(new Error("unexpected network request"));
  });
  assert.equal(typeof harness.installedListener(), "function");
  await harness.installedListener()({ reason: "update" });
  assert.equal(requests.length, 0);
  assert.deepEqual(harness.stored.atomizerContentRecovery, {
    attempted: 0,
    succeeded: 0,
    failed: 0,
  });
});
