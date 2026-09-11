// Bounded, opt-in diagnostics for browser observations. This module deliberately
// exposes facts about the browser surface, never page-controlled values.

const SECTIONS = ["runtime", "frames", "console", "network"];
const MAX_ROWS = 20;
const MAX_RUNTIME_MARKERS = 20;

export function inspectionRequest(request) {
  const value = request?.arguments?.inspection;
  if (value === undefined || value === null) return null;
  if (typeof value !== "object" || Array.isArray(value)) throw new Error("Browser inspection sections are invalid.");
  if (value.sections === undefined) return { sections: SECTIONS };
  if (!Array.isArray(value.sections) || value.sections.some((section) => !SECTIONS.includes(section))) {
    throw new Error("Browser inspection sections are invalid.");
  }
  const sections = [...new Set(value.sections)];
  return { sections };
}

export async function installInspection(page, request) {
  let config;
  try { config = inspectionRequest(request); } catch { throw new Error("Browser inspection request is invalid."); }
  if (!config) return { collector: null, snapshot: async () => null, cleanup: async () => {} };
  const rows = { console: [], network: [] };
  const omitted = { console: 0, network: 0 };
  const push = (kind, row) => {
    if (rows[kind].length < MAX_ROWS) rows[kind].push(row);
    else omitted[kind] += 1;
  };
  const listeners = [];
  if (config.sections.includes("console")) {
    const onConsole = (message) => push("console", {
      type: safeConsoleType(message?.type?.()),
      severity: consoleSeverity(message?.type?.()),
    });
    page.on("console", onConsole);
    listeners.push([page, "console", onConsole]);
  }
  if (config.sections.includes("network")) {
    const onResponse = (response) => push("network", {
      method: safeMethod(response?.request?.()?.method?.()),
      status: boundedStatus(response?.status?.()),
      resourceType: safeResourceType(response?.request?.()?.resourceType?.()),
      destination: safeOrigin(response?.url?.()),
    });
    page.on("response", onResponse);
    listeners.push([page, "response", onResponse]);
  }
  const snapshot = async () => inspectPage(page, request, { config, rows, omitted });
  const cleanup = async () => {
    for (const [target, event, listener] of listeners) target.off?.(event, listener);
  };
  return { collector: { config, rows, omitted }, snapshot, cleanup };
}

export async function inspectPage(page, request, collector = null) {
  let config;
  try { config = collector?.config || inspectionRequest(request); } catch (error) { return null; }
  if (!config) return null;
  const rows = collector?.rows || { console: [], network: [] };
  const omitted = collector?.omitted || { console: 0, network: 0 };
  const result = { requested: config.sections, scope: "call", observed: {}, omitted: {} };
  if (config.sections.includes("runtime")) {
    result.runtime = await page.evaluate(({ markerLimit }) => {
      const markerSelectors = [
        ["react", "[data-reactroot], [data-react]"],
        ["angular", "[ng-version]"],
        ["vue", "[data-vue-meta]"],
        ["svelte", "[data-svelte]"],
        ["next", "#__next"],
        ["root", "#root"],
        ["app", "#app"],
      ];
      const markers = markerSelectors
        .flatMap(([marker, selector]) => (document.querySelector(selector) ? [{ marker }] : []))
        .slice(0, markerLimit);
      return { userAgent: navigator.userAgent ? "present" : "absent", markers };
    }, { markerLimit: MAX_RUNTIME_MARKERS }).catch(() => ({ status: "unavailable", markers: [] }));
    result.observed.runtime = result.runtime.markers.length;
  }
  if (config.sections.includes("frames")) {
    const frames = page.frames?.() || [];
    result.frames = [];
    for (const frame of frames.slice(0, MAX_ROWS)) {
      let owner;
      try {
        const main = frame === page.mainFrame?.();
        owner = main ? null : await frame.frameElement?.();
        const box = await owner?.boundingBox?.();
        const frameUrl = frame.url?.();
        result.frames.push({ main, url: safeOrigin(frameUrl), crossOrigin: isCrossOrigin(page.url?.(), frameUrl), geometry: boundedGeometry(box) });
      } catch {
        result.frames.push({ main: false, url: "opaque", crossOrigin: true, geometry: null, status: "unavailable" });
      } finally {
        await owner?.dispose?.().catch?.(() => {});
      }
    }
    result.observed.frames = result.frames.length;
    result.omitted.frames = Math.max(0, frames.length - MAX_ROWS);
  }
  for (const section of ["console", "network"]) {
    if (config.sections.includes(section)) {
      result[section] = rows[section].slice(0, MAX_ROWS);
      result.observed[section] = result[section].length + omitted[section];
      result.omitted[section] = omitted[section];
    }
  }
  return result;
}

const METHODS = new Set(["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS", "CONNECT", "TRACE"]);
const RESOURCE_TYPES = new Set(["document", "stylesheet", "image", "media", "font", "script", "texttrack", "xhr", "fetch", "eventsource", "websocket", "manifest", "other"]);
const CONSOLE_TYPES = new Set(["debug", "dir", "dirxml", "error", "info", "log", "trace", "warning", "startGroup", "startGroupCollapsed", "endGroup"]);
function safeMethod(value) { return typeof value === "string" && METHODS.has(value.toUpperCase()) ? value.toUpperCase() : "OTHER"; }
function safeResourceType(value) { return typeof value === "string" && RESOURCE_TYPES.has(value) ? value : "other"; }
function safeConsoleType(value) { return typeof value === "string" && CONSOLE_TYPES.has(value) ? value : "other"; }
function consoleSeverity(type) {
  return ["error", "warning", "info", "debug"].includes(type) ? type : "log";
}
function boundedStatus(value) { return Number.isInteger(value) && value >= 100 && value <= 599 ? value : null; }
function safeOrigin(value) {
  try {
    const url = new URL(String(value));
    if (!["http:", "https:"].includes(url.protocol) || url.host.length > 256) return "opaque";
    return `${url.protocol}//${url.host}`;
  } catch { return "opaque"; }
}
function isCrossOrigin(pageUrl, frameUrl) {
  try { return new URL(pageUrl).origin !== new URL(frameUrl).origin; } catch { return true; }
}
function boundedGeometry(box) {
  if (!box || !["x", "y", "width", "height"].every((key) => Number.isFinite(box[key]))) return null;
  return { x: box.x, y: box.y, width: box.width, height: box.height };
}

export const __test = { inspectionRequest, safeOrigin, isCrossOrigin };
