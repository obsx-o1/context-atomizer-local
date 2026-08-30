(function (root) {
  "use strict";
  const implementation = root.browser || root.chrome;
  if (!implementation || !implementation.runtime) return;

  function sendMessage(message) {
    try {
      const result = implementation.runtime.sendMessage(message);
      return result && typeof result.then === "function" ? result : Promise.resolve(result);
    } catch (error) {
      return Promise.reject(error);
    }
  }

  function storageGet(defaults) {
    const result = implementation.storage.local.get(defaults);
    if (result && typeof result.then === "function") return result;
    return new Promise((resolve) => implementation.storage.local.get(defaults, resolve));
  }

  function storageSet(values) {
    const result = implementation.storage.local.set(values);
    if (result && typeof result.then === "function") return result;
    return new Promise((resolve) => implementation.storage.local.set(values, resolve));
  }

  function tabsQuery(query) {
    if (!implementation.tabs || typeof implementation.tabs.query !== "function") {
      return Promise.resolve([]);
    }
    try {
      const result = implementation.tabs.query(query);
      return result && typeof result.then === "function" ? result : Promise.resolve(result || []);
    } catch (error) {
      return Promise.reject(error);
    }
  }

  function executeScript(details) {
    if (!implementation.scripting || typeof implementation.scripting.executeScript !== "function") {
      return Promise.reject(new Error("scripting API unavailable"));
    }
    try {
      const result = implementation.scripting.executeScript(details);
      return result && typeof result.then === "function" ? result : Promise.resolve(result);
    } catch (error) {
      return Promise.reject(error);
    }
  }

  root.AtomizerWebExtensionApi = {
    onMessage: implementation.runtime.onMessage,
    onInstalled: implementation.runtime.onInstalled,
    onStartup: implementation.runtime.onStartup,
    getManifest: () => implementation.runtime.getManifest(),
    sendMessage,
    storageGet,
    storageSet,
    tabsQuery,
    executeScript,
  };
})(globalThis);
