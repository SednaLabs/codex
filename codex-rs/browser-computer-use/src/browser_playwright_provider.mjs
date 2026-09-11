import fs from "node:fs/promises";
import { createHash } from "node:crypto";
import { createRequire } from "node:module";
import { realpathSync } from "node:fs";
import os from "node:os";
import path from "node:path";
import { pathToFileURL } from "node:url";
import {
  actionSummary,
  captureBundle,
  pageResponseAfterFailure,
  pageState,
  responseForPage,
  restoreScroll,
  settleAfterAction,
  settleAfterActions,
} from "./browser_playwright_review.mjs";
import {
  installServiceHeaderRoute,
  serviceHeaderPlan,
} from "./browser_playwright_service_headers.mjs";
import { installInspection } from "./browser_playwright_inspection.mjs";

const TOOL_OBSERVE = "browser_observe";
const TOOL_STEP = "browser_step";
const DEFAULT_PROFILE_LOCK_TIMEOUT_MS = 120_000;
const PROFILE_LOCK_STALE_MS = 10 * 60_000;
const PROFILE_LOCK_POLL_MS = 250;
const ISOLATION_SHARED = "shared";
const ISOLATION_THREAD = "thread";
const ISOLATION_ENVIRONMENT = "environment";
const ISOLATION_CALL = "call";
const SESSION_RESTORE_PATH_SEGMENTS = [
  ["Current Session"],
  ["Current Tabs"],
  ["Last Session"],
  ["Last Tabs"],
  ["Sessions"],
  ["Default", "Current Session"],
  ["Default", "Current Tabs"],
  ["Default", "Last Session"],
  ["Default", "Last Tabs"],
  ["Default", "Sessions"],
];

if (isDirectExecution()) {
  main().catch((error) => {
    writeResponse({
      contentItems: [
        {
          type: "inputText",
          text: `Browser Playwright provider failed: ${error?.stack || error}`,
        },
      ],
      success: false,
      error: String(error?.message || error),
    });
    process.exitCode = 0;
  });
}

function isDirectExecution() {
  if (!process.argv[1]) {
    return false;
  }
  return import.meta.url === pathToFileURL(realpathSync(process.argv[1])).href;
}

async function main() {
  const request = JSON.parse(await readStdin());
  const { chromium } = loadPlaywright();
  const profile = await browserProfile(request);
  await withProfileLock(profile.stateDir, async () => {
    const headless = playwrightHeadless();
    const viewport = viewportFromRequest(request);
    const serviceHeaders = serviceHeaderPlan(request);
    await clearBrowserSessionRestore(profile.stateDir);
    const context = await chromium.launchPersistentContext(profile.stateDir, {
      ...launchOptions({ headless, viewport, profile }),
      headless,
      viewport,
    });

    try {
      await installServiceHeaderRoute(context, serviceHeaders);
      const page = await activePage(context);
      const inspectionRequested = request.arguments?.inspection != null;
      const inspection = await installInspection(page, request).catch(() => ({
        collector: null,
        snapshot: async () => (inspectionRequested ? { status: "unavailable" } : null),
        cleanup: async () => {},
      }));
      try {
        await restoreOrNavigate(page, request, profile.stateDir);

      const summaries = [];
      const actionTrail = [];
      if (request.tool === TOOL_STEP) {
        const actions = canonicalActions(request.arguments);
        if (actions.length === 0) {
          throw new Error("browser_step requires an action or non-empty actions array.");
        }
        for (const action of actions) {
          const before = await pageState(page);
          try {
            const summary = await runAction(page, action);
            const immediateAfter = await pageState(page);
            await settleAfterAction(page, action, request);
            const settledAfter = await pageState(page);
            summaries.push(summary);
            actionTrail.push({
              action: actionSummary(action),
              before,
              after: immediateAfter,
              settledAfter: isScrollAction(action) ? settledAfter : undefined,
              summary,
            });
          } catch (error) {
            actionTrail.push({
              action: actionSummary(action),
              before,
              after: await pageState(page),
              error: errorMessage(error),
            });
            await settleAfterActions(page, request);
            const inspectionSnapshot = await inspection.snapshot().catch(() => (inspectionRequested ? { status: "unavailable" } : null));
            const failureResponse = await pageResponseAfterFailure(
              page,
              summaries,
              profile,
              request,
              action,
              error,
              actionTrail,
              serviceHeaders,
              inspectionSnapshot,
            );
            await saveState(profile.stateDir, page);
            writeResponse(failureResponse);
            return;
          }
        }
      } else if (request.tool !== TOOL_OBSERVE) {
        throw new Error(`Unsupported browser tool ${request.tool}`);
      }

      await page.waitForLoadState("domcontentloaded", { timeout: 10_000 }).catch(() => {});
      await settleAfterActions(page, request);
      const screenshots = await captureBundle(page, request);
      const inspectionSnapshot = await inspection.snapshot().catch(() => (inspectionRequested ? { status: "unavailable" } : null));
      await saveState(profile.stateDir, page);
      writeResponse(
        await responseForPage(page, screenshots, summaries, profile, {
          request,
          actionTrail,
          serviceHeaders,
          success: true,
          inspection: inspectionSnapshot,
        }),
      );
      } finally {
        await inspection.cleanup().catch(() => {});
      }
    } finally {
      await context.close().catch(() => {});
    }
  });
}

function loadPlaywright() {
  const require = createRequire(import.meta.url);
  return require("playwright");
}

async function readStdin() {
  const chunks = [];
  for await (const chunk of process.stdin) {
    chunks.push(chunk);
  }
  return Buffer.concat(chunks).toString("utf8");
}

async function browserProfile(request) {
  const configured = process.env.CODEX_BROWSER_PLAYWRIGHT_STATE_DIR;
  const baseDir =
    configured && configured.trim()
      ? configured
      : path.join(os.homedir(), ".codex", "browser-computer-use-playwright");
  const isolation = browserIsolationMode();
  if (isolation === ISOLATION_SHARED) {
    await fs.mkdir(baseDir, { recursive: true });
    return {
      stateDir: baseDir,
      isolation,
      identity: ISOLATION_SHARED,
      label: ISOLATION_SHARED,
    };
  }

  const identity = browserProfileIdentity(request, isolation);
  const label =
    identity.source === isolation ? isolation : `${isolation}:${identity.source}`;
  const stateDir = path.join(baseDir, "profiles", identity.component);
  await fs.mkdir(stateDir, { recursive: true });
  return { stateDir, isolation, identity: identity.component, label };
}

function browserIsolationMode() {
  const raw = (process.env.CODEX_BROWSER_PLAYWRIGHT_ISOLATION || ISOLATION_THREAD)
    .trim()
    .toLowerCase();
  switch (raw) {
    case ISOLATION_SHARED:
      return ISOLATION_SHARED;
    case "session":
    case "agent":
    case ISOLATION_THREAD:
      return ISOLATION_THREAD;
    case "env":
    case ISOLATION_ENVIRONMENT:
      return ISOLATION_ENVIRONMENT;
    case "turn":
    case ISOLATION_CALL:
      return ISOLATION_CALL;
    default:
      return ISOLATION_THREAD;
  }
}

function browserProfileIdentity(request, isolation) {
  const source = browserProfileIdentitySource(request, isolation);
  return {
    source: source.name,
    component: safePathComponent(source.value),
  };
}

function browserProfileIdentitySource(request, isolation) {
  if (isolation === ISOLATION_ENVIRONMENT) {
    return firstIdentity([
      ["environment", request.environmentId],
      ["thread", request.threadId],
      ["call", request.callId],
    ]);
  }
  if (isolation === ISOLATION_CALL) {
    return firstIdentity([
      ["call", request.callId],
      ["turn", request.turnId],
      ["thread", request.threadId],
    ]);
  }
  return firstIdentity([
    ["thread", request.threadId],
    ["environment", request.environmentId],
    ["call", request.callId],
  ]);
}

function firstIdentity(candidates) {
  for (const [name, value] of candidates) {
    if (typeof value === "string" && value.trim()) {
      return { name, value };
    }
  }
  return { name: "default", value: "default" };
}

function safePathComponent(value) {
  const text = String(value || "default");
  const slug = text
    .replace(/[^a-zA-Z0-9._-]+/g, "_")
    .replace(/^_+|_+$/g, "")
    .slice(0, 64) || "default";
  const hash = createHash("sha256").update(text).digest("hex").slice(0, 12);
  return `${slug}-${hash}`;
}

async function withProfileLock(stateDir, body) {
  const lockDir = path.join(stateDir, ".codex-provider.lock");
  const deadline = Date.now() + lockTimeoutMs();
  while (true) {
    try {
      await fs.mkdir(lockDir);
      await fs.writeFile(
        path.join(lockDir, "owner.json"),
        JSON.stringify({
          pid: process.pid,
          startedAt: new Date().toISOString(),
        }),
      );
      try {
        return await body();
      } finally {
        await fs.rm(lockDir, { recursive: true, force: true }).catch(() => {});
      }
    } catch (error) {
      if (error?.code !== "EEXIST") {
        throw error;
      }
      if (await removeStaleProfileLock(lockDir)) {
        continue;
      }
      if (Date.now() >= deadline) {
        throw new Error(
          `Timed out waiting for browser profile lock at ${lockDir}. Another native browser call may still be using the headed Chrome profile.`,
        );
      }
      await sleep(PROFILE_LOCK_POLL_MS);
    }
  }
}

async function removeStaleProfileLock(lockDir) {
  try {
    const stat = await fs.stat(lockDir);
    if (Date.now() - stat.mtimeMs < PROFILE_LOCK_STALE_MS) {
      return false;
    }
    await fs.rm(lockDir, { recursive: true, force: true });
    return true;
  } catch (error) {
    return error?.code === "ENOENT";
  }
}

function lockTimeoutMs() {
  return envNumber(
    "CODEX_BROWSER_PLAYWRIGHT_LOCK_TIMEOUT_MS",
    DEFAULT_PROFILE_LOCK_TIMEOUT_MS,
  );
}

function playwrightHeadless() {
  const raw = (process.env.CODEX_BROWSER_PLAYWRIGHT_HEADLESS || "1").toLowerCase();
  return !["0", "false", "no", "off"].includes(raw);
}

function launchOptions({ headless, viewport, profile }) {
  const options = {};
  const executablePath = trimmedEnv("CODEX_BROWSER_PLAYWRIGHT_EXECUTABLE_PATH");
  if (executablePath) {
    options.executablePath = executablePath;
  }
  const channel = trimmedEnv("CODEX_BROWSER_PLAYWRIGHT_CHANNEL");
  if (channel && !executablePath) {
    options.channel = channel;
  }
  if (!headless && viewport) {
    const position = windowPositionForProfile(profile);
    options.args = [
      `--window-position=${position.x},${position.y}`,
      `--window-size=${viewport.width},${viewport.height}`,
    ];
  }
  return options;
}

function windowPositionForProfile(profile) {
  if (!profile || profile.isolation === ISOLATION_SHARED) {
    return { x: 0, y: 0 };
  }
  const hash = createHash("sha256").update(profile.identity).digest();
  const slot = hash[0] % 9;
  return {
    x: (slot % 3) * envNumber("CODEX_BROWSER_PLAYWRIGHT_WINDOW_OFFSET_X", 48),
    y: Math.floor(slot / 3) * envNumber("CODEX_BROWSER_PLAYWRIGHT_WINDOW_OFFSET_Y", 36),
  };
}

function viewportFromRequest(request) {
  const view = request.arguments?.view || {};
  return {
    width: numberOrDefault(
      view.viewportWidth,
      envNumber("CODEX_BROWSER_PLAYWRIGHT_VIEWPORT_WIDTH", 1280),
    ),
    height: numberOrDefault(
      view.viewportHeight,
      envNumber("CODEX_BROWSER_PLAYWRIGHT_VIEWPORT_HEIGHT", 720),
    ),
  };
}

async function activePage(context) {
  const existing = context.pages().find((page) => !page.isClosed());
  return existing || context.newPage();
}

async function restoreOrNavigate(page, request, stateDir) {
  const firstAction = canonicalActions(request.arguments)[0];
  if ((firstAction?.type || firstAction?.action) === "navigate") {
    return;
  }
  const explicitUrl = request.arguments?.url;
  if (explicitUrl) {
    await page.goto(explicitUrl, { waitUntil: "domcontentloaded", timeout: timeoutMs(request) });
    await restoreScroll(page, request);
    return;
  }

  const statePath = path.join(stateDir, "state.json");
  const state = await readJsonOrNull(statePath);
  if (navigableUrl(state?.url) && pageUrl(page) === "about:blank") {
    await page.goto(state.url, { waitUntil: "domcontentloaded", timeout: timeoutMs(request) });
    if (!request.arguments?.view && typeof state.scrollY === "number") {
      await page.evaluate((scrollY) => window.scrollTo(window.scrollX, scrollY), state.scrollY);
    }
  }
  await restoreScroll(page, request);
}

async function clearBrowserSessionRestore(stateDir) {
  const profileRoot = path.resolve(stateDir);
  await Promise.all(
    browserSessionRestorePaths(profileRoot).map((entry) =>
      fs.rm(entry, { recursive: true, force: true }).catch(() => {}),
    ),
  );
}

function browserSessionRestorePaths(stateDir) {
  const profileRoot = path.resolve(stateDir);
  return SESSION_RESTORE_PATH_SEGMENTS.map((segments) => profilePath(profileRoot, segments));
}

function profilePath(profileRoot, segments) {
  const entry = path.resolve(profileRoot, ...segments);
  const relative = path.relative(profileRoot, entry);
  if (relative.startsWith("..") || path.isAbsolute(relative)) {
    throw new Error(`Refusing to remove browser session restore path outside profile: ${entry}`);
  }
  return entry;
}

function canonicalActions(argumentsValue) {
  if (Array.isArray(argumentsValue?.actions) && argumentsValue.actions.length > 0) {
    return argumentsValue.actions;
  }
  if (argumentsValue?.action || argumentsValue?.type) {
    return [argumentsValue];
  }
  return [];
}

function isScrollAction(action) {
  return ["scroll", "mouse_wheel"].includes(action.type || action.action);
}

async function runAction(page, action) {
  const type = action.type || action.action;
  switch (type) {
    case "navigate":
      await requireUrl(page, action);
      return `navigated to ${action.url}`;
    case "click":
      await click(page, action);
      return action.selector ? "clicked browser selector" : `clicked at ${action.x},${action.y}`;
    case "type":
      await typeText(page, action);
      return action.selector ? "typed into browser selector" : "typed into focused browser element";
    case "focus":
      await focusElement(page, action);
      return action.selector ? "focused browser selector" : "focused current browser element";
    case "clear":
      await clearElement(page, action);
      return action.selector ? "cleared browser selector" : "cleared focused browser element";
    case "keypress":
      await keypress(page, action);
      return "sent browser keypress";
    case "key_down":
      await keyDown(page, action);
      return "sent browser key down";
    case "key_up":
      await keyUp(page, action);
      return "sent browser key up";
    case "scroll":
    case "mouse_wheel":
      await mouseWheel(page, action);
      return "scrolled browser viewport";
    case "wait":
      await page.waitForTimeout(numberOrDefault(action.ms, 1000));
      return `waited ${numberOrDefault(action.ms, 1000)} ms`;
    case "select":
      await selectOption(page, action);
      return "selected browser option";
    case "drag":
      await drag(page, action);
      return "dragged in browser viewport";
    case "hover":
      await hover(page, action);
      return action.selector ? "hovered browser selector" : `hovered at ${action.x},${action.y}`;
    case "mouse_move":
      await mouseMove(page, action);
      return `moved browser mouse to ${action.x},${action.y}`;
    case "mouse_down":
      await mouseDown(page, action);
      return "sent browser mouse down";
    case "mouse_up":
      await mouseUp(page, action);
      return "sent browser mouse up";
    default:
      throw new Error(`Unsupported browser action ${type}`);
  }
}

async function requireUrl(page, action) {
  if (!action.url) {
    throw new Error("navigate requires url");
  }
  await page.goto(action.url, { waitUntil: "domcontentloaded", timeout: timeoutMs({ arguments: action }) });
}

async function click(page, action) {
  const locator = locatorFromAction(page, action);
  if (locator) {
    await withKeyboardModifiers(page, action, async () => {
      await locator.click({
        ...clickOptions(action),
        timeout: timeoutMs({ arguments: action }),
      });
    });
  } else {
    await withKeyboardModifiers(page, action, async () => {
      await moveMouseToActionPoint(page, action);
      await page.mouse.click(
        requiredNumber(action, "x"),
        requiredNumber(action, "y"),
        clickOptions(action),
      );
    });
  }
}

async function typeText(page, action) {
  const text = action.text || "";
  const locator = locatorFromAction(page, action);
  if (locator && textEntryMethod(action) === "fill") {
    await locator.fill(text, { timeout: timeoutMs({ arguments: action }) });
    return;
  }
  if (locator) {
    await locator.click({ timeout: timeoutMs({ arguments: action }) });
    if (action.replace !== false) {
      await selectAllAndClear(page);
    }
  }
  await page.keyboard.type(text, keyboardDelayOptions(action));
}

async function focusElement(page, action) {
  const locator = locatorFromAction(page, action);
  if (locator) {
    await locator.focus({ timeout: timeoutMs({ arguments: action }) });
    return;
  }
  if (typeof action.x === "number" && typeof action.y === "number") {
    await mouseMove(page, action);
  }
}

async function clearElement(page, action) {
  const locator = locatorFromAction(page, action);
  if (locator) {
    if (textEntryMethod(action) === "fill") {
      await locator.fill("", { timeout: timeoutMs({ arguments: action }) });
      return;
    }
    await locator.click({ timeout: timeoutMs({ arguments: action }) });
  }
  await selectAllAndClear(page);
}

async function keypress(page, action) {
  const key = Array.isArray(action.keys) && action.keys.length > 0 ? action.keys.join("+") : action.key;
  if (!key) {
    throw new Error("keypress requires key or keys");
  }
  await page.keyboard.press(key, keyboardDelayOptions(action));
}

async function keyDown(page, action) {
  const key = requiredKey(action, "key_down");
  await page.keyboard.down(key);
}

async function keyUp(page, action) {
  const key = requiredKey(action, "key_up");
  await page.keyboard.up(key);
}

async function mouseWheel(page, action) {
  await withKeyboardModifiers(page, action, async () => {
    await page.mouse.wheel(
      numberOrDefault(action.scroll_x, 0),
      numberOrDefault(action.scroll_y, 720),
    );
  });
}

async function selectOption(page, action) {
  const locator = locatorFromAction(page, action);
  if (!locator) {
    throw new Error("select requires selector");
  }
  await locator.selectOption(action.value || action.text || action.label || "");
}

async function drag(page, action) {
  await withKeyboardModifiers(page, action, async () => {
    await page.mouse.move(
      requiredNumber(action, "x1"),
      requiredNumber(action, "y1"),
      mouseMoveOptions(action),
    );
    await page.mouse.down(mouseButtonOptions(action));
    await page.mouse.move(
      requiredNumber(action, "x2"),
      requiredNumber(action, "y2"),
      mouseMoveOptions(action),
    );
    await page.mouse.up(mouseButtonOptions(action));
  });
}

async function hover(page, action) {
  const locator = locatorFromAction(page, action);
  if (locator) {
    await locator.hover({ timeout: timeoutMs({ arguments: action }) });
  } else {
    await mouseMove(page, action);
  }
}

async function mouseMove(page, action) {
  await page.mouse.move(
    requiredNumber(action, "x"),
    requiredNumber(action, "y"),
    mouseMoveOptions(action),
  );
}

async function mouseDown(page, action) {
  await withKeyboardModifiers(page, action, async () => {
    await moveMouseIfActionPoint(page, action);
    await page.mouse.down(mouseButtonOptions(action));
    await delayAfterMouseEvent(page, action);
  });
}

async function mouseUp(page, action) {
  await withKeyboardModifiers(page, action, async () => {
    await moveMouseIfActionPoint(page, action);
    await page.mouse.up(mouseButtonOptions(action));
    await delayAfterMouseEvent(page, action);
  });
}

function locatorFromAction(page, action) {
  const selector = action.selector;
  if (!selector) {
    return null;
  }
  const maybeFirst = (locator) => selector.strict ? locator : locator.first();
  if (typeof selector === "string") {
    return page.locator(selector).first();
  }
  if (selector.css) {
    return maybeFirst(page.locator(selector.css));
  }
  if (selector.text) {
    return maybeFirst(page.getByText(selector.text, selectorOptions(selector)));
  }
  if (selector.label) {
    return maybeFirst(page.getByLabel(selector.label, selectorOptions(selector)));
  }
  if (selector.placeholder) {
    return maybeFirst(page.getByPlaceholder(selector.placeholder, selectorOptions(selector)));
  }
  if (selector.test_id || selector.testId) {
    return maybeFirst(page.getByTestId(selector.test_id || selector.testId));
  }
  if (selector.title) {
    return maybeFirst(page.getByTitle(selector.title, selectorOptions(selector)));
  }
  if (selector.alt_text || selector.altText) {
    return maybeFirst(
      page.getByAltText(selector.alt_text || selector.altText, selectorOptions(selector)),
    );
  }
  if (selector.role) {
    return maybeFirst(page.getByRole(selector.role, roleSelectorOptions(selector)));
  }
  return null;
}

function textEntryMethod(action) {
  return action.method === "fill" || action.input_method === "fill"
    ? "fill"
    : "keyboard";
}

async function selectAllAndClear(page) {
  const modifier = process.platform === "darwin" ? "Meta" : "Control";
  await page.keyboard.press(`${modifier}+A`);
  await page.keyboard.press("Backspace");
}

function requiredKey(action, actionName) {
  if (!action.key) {
    throw new Error(`${actionName} requires key`);
  }
  return action.key;
}

function clickOptions(action) {
  return compactOptions({
    ...mouseButtonOptions(action),
    ...keyboardDelayOptions(action),
    clickCount: positiveIntegerOrUndefined(action.click_count),
  });
}

function mouseButtonOptions(action) {
  return { button: mouseButton(action) };
}

function mouseButton(action) {
  const button = action.button || "left";
  if (!["left", "right", "middle"].includes(button)) {
    throw new Error("button must be left, right, or middle");
  }
  return button;
}

function mouseMoveOptions(action) {
  return compactOptions({ steps: positiveIntegerOrUndefined(action.steps) });
}

function keyboardDelayOptions(action) {
  return compactOptions({
    delay: nonNegativeIntegerOrUndefined(action.delay_ms),
  });
}

async function moveMouseToActionPoint(page, action) {
  await page.mouse.move(
    requiredNumber(action, "x"),
    requiredNumber(action, "y"),
    mouseMoveOptions(action),
  );
}

async function moveMouseIfActionPoint(page, action) {
  const hasX = typeof action.x === "number";
  const hasY = typeof action.y === "number";
  if (hasX !== hasY) {
    throw new Error(
      "mouse action requires both x and y when either coordinate is provided",
    );
  }
  if (hasX && hasY) {
    await page.mouse.move(action.x, action.y, mouseMoveOptions(action));
  }
}

async function delayAfterMouseEvent(page, action) {
  const delay = nonNegativeIntegerOrUndefined(action.delay_ms);
  if (delay !== undefined) {
    await page.waitForTimeout(delay);
  }
}

async function withKeyboardModifiers(page, action, body) {
  const modifiers = Array.isArray(action.modifiers) ? action.modifiers : [];
  for (const modifier of modifiers) {
    if (!["Alt", "Control", "Meta", "Shift"].includes(modifier)) {
      throw new Error("modifiers must contain only Alt, Control, Meta, or Shift");
    }
  }
  for (const modifier of modifiers) {
    await page.keyboard.down(modifier);
  }
  try {
    return await body();
  } finally {
    for (const modifier of modifiers.slice().reverse()) {
      await page.keyboard.up(modifier).catch(() => {});
    }
  }
}

function selectorOptions(selector) {
  return selector.exact === undefined ? undefined : { exact: Boolean(selector.exact) };
}

function roleSelectorOptions(selector) {
  const options = selector.name ? { name: selector.name } : {};
  if (selector.exact !== undefined) {
    options.exact = Boolean(selector.exact);
  }
  return Object.keys(options).length > 0 ? options : undefined;
}

function compactOptions(options) {
  return Object.fromEntries(
    Object.entries(options).filter(([, value]) => value !== undefined),
  );
}

async function saveState(stateDir, page) {
  const viewport = page.viewportSize?.() || null;
  const url = pageUrl(page);
  const scroll = await page
    .evaluate(() => ({ x: window.scrollX, y: window.scrollY }))
    .catch(() => ({ x: 0, y: 0 }));
  if (!navigableUrl(url)) {
    return;
  }
  await fs.writeFile(
    path.join(stateDir, "state.json"),
    JSON.stringify({
      url,
      scrollX: scroll.x,
      scrollY: scroll.y,
      viewportWidth: viewport?.width,
      viewportHeight: viewport?.height,
      updatedAt: new Date().toISOString(),
    }),
  );
}

function pageUrl(page) {
  try {
    return page.url();
  } catch {
    return "unknown";
  }
}

function navigableUrl(value) {
  if (typeof value !== "string" || !value.trim() || value === "unknown") {
    return false;
  }
  try {
    const url = new URL(value);
    return url.protocol === "http:" || url.protocol === "https:" || url.protocol === "file:";
  } catch {
    return false;
  }
}

async function readJsonOrNull(file) {
  try {
    return JSON.parse(await fs.readFile(file, "utf8"));
  } catch {
    return null;
  }
}

function timeoutMs(request) {
  return numberOrDefault(request.arguments?.timeout_secs, 30) * 1000;
}

function requiredNumber(value, field) {
  if (typeof value[field] !== "number") {
    throw new Error(`${field} is required`);
  }
  return value[field];
}

function numberOrDefault(value, fallback) {
  return typeof value === "number" && Number.isFinite(value) ? value : fallback;
}

function positiveIntegerOrUndefined(value) {
  return Number.isInteger(value) && value > 0 ? value : undefined;
}

function nonNegativeIntegerOrUndefined(value) {
  return Number.isInteger(value) && value >= 0 ? value : undefined;
}

function errorMessage(error) {
  return String(error?.message || error);
}

function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

function envNumber(name, fallback) {
  const raw = process.env[name];
  if (!raw) {
    return fallback;
  }
  const parsed = Number(raw);
  return Number.isFinite(parsed) && parsed > 0 ? parsed : fallback;
}

function trimmedEnv(name) {
  const value = process.env[name];
  return value && value.trim() ? value.trim() : null;
}

function writeResponse(response) {
  process.stdout.write(JSON.stringify(response));
}

export const __test = {
  browserSessionRestorePaths,
  clearBrowserSessionRestore,
  profilePath,
};
