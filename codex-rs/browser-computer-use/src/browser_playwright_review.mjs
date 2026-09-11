import fs from "node:fs/promises";
import { createHash } from "node:crypto";
import path from "node:path";

const OBSERVE_SCOPE_VIEWPORT_AND_PAGE = "viewport_and_page";
const ARTIFACT_POLICY_ALWAYS = "always";
const ARTIFACT_POLICY_FAILURE = "failure";
const CAPTURE_VIEWPORT = "viewport";
const CAPTURE_FULL_PAGE = "full_page";
const CAPTURE_BUNDLE_LIMIT = 4;
const INTERACTION_MAP_LIMIT = 24;
const ATTENTION_LIMIT = 12;

export async function restoreScroll(page, request) {
  const view = request.arguments?.view || {};
  if (typeof view.scrollY === "number") {
    await page.evaluate((scrollY) => window.scrollTo(window.scrollX, scrollY), view.scrollY);
  }
}

export async function captureBundle(page, request) {
  const captures = Array.isArray(request.arguments?.captures)
    ? request.arguments.captures.slice(0, CAPTURE_BUNDLE_LIMIT)
    : [];
  if (captures.length === 0) {
    const requestedViewport = page.viewportSize?.() || null;
    return [
      {
        label: null,
        requestedViewport,
        screenshot: await captureScreenshot(page),
        metadata: await captureViewportMetadata(page, requestedViewport),
      },
    ];
  }

  const originalRequestedViewport = page.viewportSize?.() || null;
  const original = await captureViewportMetadata(page, originalRequestedViewport);
  const results = [];
  try {
    for (const capture of captures) {
      const viewport = page.viewportSize?.();
      const width = numberOrDefault(capture.viewportWidth, viewport?.width || original.effectiveViewport.width);
      const height = numberOrDefault(capture.viewportHeight, viewport?.height || original.effectiveViewport.height);
      await page.setViewportSize({ width, height });
      await applyCaptureScroll(page, capture);
      await page.waitForTimeout(nonNegativeIntegerOrUndefined(capture.settle_ms) || 150);
      const metadata = await captureViewportMetadata(page, { width, height });
      results.push({
        label: capture.label || capture.scroll || `capture-${results.length + 1}`,
        screenshot: await captureScreenshot(page),
        requestedViewport: { width, height },
        metadata,
      });
    }
  } finally {
    await page.setViewportSize(originalRequestedViewport).catch(async () => {
      if (originalRequestedViewport === null) {
        await page
          .setViewportSize({
            width: original.effectiveViewport.width,
            height: original.effectiveViewport.height,
          })
          .catch(() => {});
      }
    });
    await page.evaluate((state) => window.scrollTo(state.scroll.x, state.scroll.y), original).catch(() => {});
  }
  results.restoredState = await captureViewportMetadata(page, originalRequestedViewport).catch(() => null);
  return results;
}

async function captureViewportMetadata(page, requestedViewport) {
  const metadata = await page.evaluate(() => ({
    effectiveViewport: { width: window.innerWidth, height: window.innerHeight },
    clientViewport: { width: document.documentElement.clientWidth, height: document.documentElement.clientHeight },
    document: {
      width: document.documentElement.scrollWidth,
      height: document.documentElement.scrollHeight,
    },
    scroll: { x: window.scrollX, y: window.scrollY },
    devicePixelRatio: window.devicePixelRatio,
    visualViewportScale: window.visualViewport?.scale ?? null,
  }));
  return { requestedViewport, ...metadata };
}

export async function captureScreenshot(page) {
  const errors = [];
  const fullPage = captureMode() === CAPTURE_FULL_PAGE;
  try {
    return {
      buffer: await page.screenshot({ type: "png", fullPage }),
      method: "page.screenshot",
    };
  } catch (error) {
    errors.push(`page.screenshot: ${errorMessage(error)}`);
  }

  for (const fromSurface of [true, false]) {
    let cdp = null;
    try {
      cdp = await page.context().newCDPSession(page);
      await cdp.send("Page.enable").catch(() => {});
      const result = await cdp.send("Page.captureScreenshot", {
        format: "png",
        fromSurface,
        captureBeyondViewport: fullPage,
      });
      return {
        buffer: Buffer.from(result.data, "base64"),
        method: `cdp.Page.captureScreenshot(fromSurface=${fromSurface})`,
        warning: compactCaptureErrors(errors),
      };
    } catch (error) {
      errors.push(
        `cdp.Page.captureScreenshot(fromSurface=${fromSurface}): ${errorMessage(error)}`,
      );
    } finally {
      if (cdp) {
        await cdp.detach().catch(() => {});
      }
    }
  }

  for (const selector of ["body", "html"]) {
    try {
      return {
        buffer: await page.locator(selector).screenshot({ type: "png" }),
        method: `locator(${selector}).screenshot`,
        warning: compactCaptureErrors(errors),
      };
    } catch (error) {
      errors.push(`locator(${selector}).screenshot: ${errorMessage(error)}`);
    }
  }

  throw new Error(`Unable to capture browser screenshot. ${compactCaptureErrors(errors)}`);
}

export async function pageResponseAfterFailure(
  page,
  summaries,
  profile,
  request,
  failedAction,
  error,
  actionTrail,
  serviceHeaders,
  inspection = null,
) {
  try {
    const requestedViewport = page.viewportSize?.() || null;
    const screenshots = [
      {
        label: "failure",
        screenshot: await captureScreenshot(page),
        requestedViewport,
        metadata: await captureViewportMetadata(page, requestedViewport),
      },
    ];
    return await responseForPage(page, screenshots, summaries, profile, {
      request,
      actionTrail,
      serviceHeaders,
      success: false,
      error: errorMessage(error),
      failedAction,
      inspection,
    });
  } catch (captureError) {
    const snapshot = await safePageSnapshot(page).catch(() => null);
    return {
      contentItems: [
        {
          type: "inputText",
          text: failureTextWithoutScreenshot(page, failedAction, error, captureError, snapshot),
        },
      ],
      success: false,
      error: errorMessage(error),
    };
  }
}

export async function responseForPage(page, screenshots, summaries, profile, options = {}) {
  const {
    request = { arguments: {} },
    actionTrail = [],
    serviceHeaders = null,
    success = true,
    error = null,
    failedAction = null,
    inspection = null,
  } = options;
  const snapshot = await pageSnapshot(page, interactionMapOptions(request));
  const lines = [success ? "Browser observation" : "Browser action failed", `url: ${pageUrl(page)}`];
  if (profile?.label) {
    lines.push(`profile: ${profile.label}`);
  }
  if (snapshot.title) {
    lines.push(`title: ${snapshot.title}`);
  }
  if (snapshot.viewport) {
    const documentSize = snapshot.document || { width: "unknown", height: "unknown" };
    lines.push(
      `viewport: ${snapshot.viewport.width}x${snapshot.viewport.height} scroll=${snapshot.scroll.x},${snapshot.scroll.y} document=${documentSize.width}x${documentSize.height}`,
    );
  }
  appendServiceHeaderSummary(lines, serviceHeaders);
  if (!success) {
    lines.push(`error: ${error || "unknown browser action error"}`);
  }
  if (failedAction) {
    lines.push(`failed_action: ${actionSummary(failedAction)}`);
  }
  if (summaries.length > 0) {
    lines.push("actions:");
    for (const summary of summaries) {
      lines.push(`- ${summary}`);
    }
  }
  appendNavigationSummary(lines, actionTrail);
  if (shouldIncludePageMetadata(request, success)) {
    appendStateMarkers(lines, snapshot);
    appendInteractionMap(lines, snapshot);
  }
  if (failedAction) {
    appendSelectorCandidates(lines, failedAction, snapshot);
  }
  for (const capture of screenshots) {
    const label = capture.label ? ` ${capture.label}` : "";
    lines.push(`capture${label}: ${capture.screenshot.method}`);
    appendCaptureMetadata(lines, capture);
    if (capture.screenshot.warning) {
      lines.push(`capture_fallback${label}: ${capture.screenshot.warning}`);
    }
  }
  if (screenshots.restoredState) {
    lines.push(`capture_restored: ${formatViewportMetadata(screenshots.restoredState)}`);
  }
  if (inspection) {
    lines.push(`inspection: ${JSON.stringify(inspection)}`);
  }

  const artifact = await maybeSaveArtifacts({
    page,
    screenshots,
    request,
    profile,
    snapshot,
    summaries,
    actionTrail,
    serviceHeaders,
    success,
    error,
    failedAction,
    inspection,
  }).catch((artifactError) => ({ error: errorMessage(artifactError) }));
  if (artifact?.manifestPath) {
    lines.push(`artifacts: ${artifact.manifestPath}`);
  } else if (artifact?.error) {
    lines.push(`artifacts_error: ${artifact.error}`);
  }

  const contentItems = [{ type: "inputText", text: lines.join("\n") }];
  for (const capture of screenshots) {
    contentItems.push({
      type: "inputImage",
      imageUrl: `data:image/png;base64,${capture.screenshot.buffer.toString("base64")}`,
      detail: "high",
    });
  }

  return {
    contentItems,
    success,
    error: success ? undefined : error || "browser action failed",
  };
}

function appendCaptureMetadata(lines, capture) {
  if (!capture.metadata) return;
  lines.push(`capture_metadata${capture.label ? ` ${capture.label}` : ""}: ${formatViewportMetadata(capture.metadata)}`);
}

function formatViewportMetadata(metadata) {
  return JSON.stringify({
    requestedViewport: metadata.requestedViewport || null,
    effectiveViewport: metadata.effectiveViewport || null,
    clientViewport: metadata.clientViewport || null,
    document: metadata.document || null,
    scroll: metadata.scroll || null,
    devicePixelRatio: metadata.devicePixelRatio ?? null,
    visualViewportScale: metadata.visualViewportScale ?? null,
  });
}

export async function pageState(page) {
  return {
    url: pageUrl(page),
    title: await pageTitle(page),
    scroll: await page
      .evaluate(() => ({ x: window.scrollX, y: window.scrollY }))
      .catch(() => null),
  };
}

export const __test = {
  pageSnapshot,
  summarizeControlsForTest,
  captureViewportMetadata,
};

export async function settleAfterActions(page, request) {
  const delay = nonNegativeIntegerOrUndefined(request.arguments?.settle_ms);
  if (delay !== undefined) {
    await page.waitForTimeout(delay);
  }
}

export async function settleAfterAction(page, action, request = { arguments: {} }) {
  if (!["scroll", "mouse_wheel"].includes(action.type || action.action)) {
    return;
  }
  const timeout = nonNegativeIntegerOrUndefined(request.arguments?.settle_ms) ?? 100;
  await Promise.race([
    page.evaluate(
      () =>
        new Promise((resolve) => {
          requestAnimationFrame(() => requestAnimationFrame(resolve));
        }),
    ),
    page.waitForTimeout(Math.min(timeout, 250)),
  ]);
}

export function actionSummary(action) {
  const type = action.type || action.action || "unknown";
  if (action.selector) {
    return `${type} selector=${JSON.stringify(action.selector)}`;
  }
  if (action.url) {
    return `${type} url=${action.url}`;
  }
  if (typeof action.x === "number" && typeof action.y === "number") {
    return `${type} at ${action.x},${action.y}`;
  }
  return type;
}

async function applyCaptureScroll(page, capture) {
  if (typeof capture.scrollY === "number") {
    await page.evaluate((scrollY) => window.scrollTo(window.scrollX, scrollY), capture.scrollY);
    return;
  }
  switch (capture.scroll) {
    case "top":
      await page.evaluate(() => window.scrollTo(window.scrollX, 0));
      return;
    case "bottom":
      await page.evaluate(() => window.scrollTo(window.scrollX, document.documentElement.scrollHeight));
      return;
    case "current":
    case undefined:
      return;
    default:
      throw new Error("capture scroll must be current, top, bottom, or use scrollY");
  }
}

async function safePageSnapshot(page) {
  try {
    return await pageSnapshot(page, interactionMapOptions(null));
  } catch (error) {
    return {
      title: await pageTitle(page),
      viewport: page.viewportSize?.() || null,
      document: null,
      scroll: { x: 0, y: 0 },
      focused: null,
      controls: [],
      interactionTotal: 0,
      interactionOmitted: 0,
      attention: [
        {
          role: "status",
          text: `Page metadata unavailable: ${errorMessage(error)}`,
          box: null,
          offscreen: false,
          selectors: [],
        },
      ],
    };
  }
}

function interactionMapOptions(request) {
  const map = request?.arguments?.interaction_map;
  if (!map || map.scope !== "page") {
    return { offset: 0 };
  }
  return {
    offset: Number.isInteger(map.offset) && map.offset >= 0 ? map.offset : 0,
  };
}

async function pageSnapshot(page, mapOptions = {}) {
  return page.evaluate(({ interactionLimit, attentionLimit, offset }) => {
    const viewport = { width: window.innerWidth, height: window.innerHeight };
    const documentSize = {
      width: document.documentElement.scrollWidth,
      height: document.documentElement.scrollHeight,
    };
    const allControls = Array.from(
      document.querySelectorAll(
        [
          "button",
          "a[href]",
          "input",
          "textarea",
          "select",
          "iframe",
          "[role]",
          "[tabindex]",
          "[contenteditable='true']",
          "[data-testid]",
          "[data-autosave-action]",
        ].join(","),
      ),
    )
      .map((element, index) => {
        if (isHidden(element)) return null;
        const summary = controlSummary(element, viewport);
        return summary ? { element, index, summary } : null;
      })
      .filter(Boolean);
    const attention = Array.from(
      document.querySelectorAll(
        [
          "[role='alert']",
          "[role='status']",
          "[aria-live]",
          ".alert",
          ".warning",
          ".error",
          ".status",
          ".banner",
          ".toast",
          ".modal",
          "[data-testid*='status' i]",
          "[data-testid*='warning' i]",
          "[data-testid*='error' i]",
        ].join(","),
      ),
    )
      .map((element) => attentionSummary(element, viewport))
      .filter(Boolean)
      .slice(0, attentionLimit);
    const activeElement = document.activeElement
      ? controlSummary(document.activeElement, viewport)
      : null;
    const rankedControls = allControls
      .map((entry) => ({
        ...entry,
        focused: entry.element === document.activeElement,
        modal: isActiveOverlay(entry.element),
      }))
      .sort(compareControlEntries);
    const boundedOffset = Math.min(offset, rankedControls.length);
    const controls = rankedControls.slice(boundedOffset, boundedOffset + interactionLimit).map((entry) => entry.summary);
    return {
      title: document.title || "",
      viewport,
      document: documentSize,
      scroll: { x: window.scrollX, y: window.scrollY },
      focused: activeElement,
      controls,
      interactionTotal: allControls.length,
      interactionOffset: boundedOffset,
      interactionOmitted: Math.max(0, allControls.length - controls.length),
      attention,
    };

    function attentionSummary(element, viewport) {
      const rect = element.getBoundingClientRect();
      if (!rect || rect.width <= 0 || rect.height <= 0) {
        return null;
      }
      const text = compactText(element.innerText || element.textContent || "");
      if (!text) {
        return null;
      }
      return {
        role: element.getAttribute("role") || implicitRole(element),
        text,
        box: roundBox(rect),
        offscreen:
          rect.bottom < 0 ||
          rect.right < 0 ||
          rect.top > viewport.height ||
          rect.left > viewport.width,
        selectors: selectorHints(element, element.getAttribute("role") || implicitRole(element), text),
      };
    }

    function controlSummary(element, viewport) {
      const rect = element.getBoundingClientRect();
      if (!rect || rect.width <= 0 || rect.height <= 0) {
        return null;
      }
      const role = element.getAttribute("role") || implicitRole(element);
      const text = controlName(element, role);
      const centerX = Math.max(0, Math.min(viewport.width - 1, rect.left + rect.width / 2));
      const centerY = Math.max(0, Math.min(viewport.height - 1, rect.top + rect.height / 2));
      const topElement = document.elementFromPoint(centerX, centerY);
      const covered = Boolean(topElement && topElement !== element && !element.contains(topElement));
      const disabled = Boolean(
        element.disabled ||
          element.getAttribute("aria-disabled") === "true" ||
          element.closest("[aria-disabled='true']"),
      );
      const offscreen =
        rect.bottom < 0 ||
        rect.right < 0 ||
        rect.top > viewport.height ||
        rect.left > viewport.width;
      return {
        role,
        name: text,
        disabled,
        covered,
        offscreen,
        box: roundBox(rect),
        selectors: selectorHints(element, role, text),
      };
    }

    function rankControlEntry(entry) {
      const control = entry.summary;
      return [
        entry.focused ? 1 : 0,
        entry.modal ? 1 : 0,
        control.offscreen ? 0 : 1,
        control.covered ? 0 : 1,
        control.disabled ? 0 : 1,
        control.role === "link" || control.role === "button" ? 1 : 0,
      ];
    }

    function compareControlEntries(left, right) {
      const leftRank = rankControlEntry(left);
      const rightRank = rankControlEntry(right);
      for (let index = 0; index < leftRank.length; index += 1) {
        if (leftRank[index] !== rightRank[index]) return rightRank[index] - leftRank[index];
      }
      return left.index - right.index;
    }

    function isHidden(element) {
      for (let current = element; current; current = current.parentElement) {
        if (current.hidden || current.inert || current.getAttribute("aria-hidden") === "true") return true;
        const style = window.getComputedStyle(current);
        if (style.display === "none" || style.visibility === "hidden" || style.visibility === "collapse") return true;
      }
      return false;
    }

    function isActiveOverlay(element) {
      const overlay = element.closest("dialog,[role='dialog'],[aria-modal='true'],[role='menu'],.modal");
      if (!overlay || overlay.hasAttribute("hidden") || overlay.hasAttribute("inert") || overlay.getAttribute("aria-hidden") === "true") return false;
      if (overlay.tagName.toLowerCase() === "dialog" && overlay.hasAttribute("closed")) return false;
      const rect = overlay.getBoundingClientRect();
      if (!rect || rect.width <= 0 || rect.height <= 0) return false;
      const style = window.getComputedStyle(overlay);
      return style.display !== "none" && style.visibility !== "hidden";
    }

    function controlName(element, role) {
      const tag = element.tagName.toLowerCase();
      const inputType = (element.getAttribute("type") || "").toLowerCase();
      if (
        tag === "textarea" ||
        tag === "iframe" ||
        element.isContentEditable ||
        element.getAttribute("contenteditable") === "true" ||
        role === "textbox" ||
        (tag === "input" && !["button", "submit", "reset", "checkbox", "radio"].includes(inputType))
      ) {
        return formControlLabel(element);
      }

      const explicitName =
        element.getAttribute("aria-label") ||
        element.innerText ||
        element.textContent ||
        element.getAttribute("title") ||
        element.getAttribute("placeholder") ||
        element.getAttribute("alt") ||
        "";
      if (explicitName) return compactText(explicitName);

      if (tag === "input" && ["button", "submit", "reset"].includes(inputType)) {
        return compactText(element.getAttribute("value") || "");
      }
      return "";
    }

    function formControlLabel(element) {
      return compactText(
        element.getAttribute("aria-label") ||
          element.getAttribute("title") ||
          element.getAttribute("placeholder") ||
          element.getAttribute("name") ||
          element.getAttribute("id") ||
          "",
      );
    }

    function implicitRole(element) {
      const tag = element.tagName.toLowerCase();
      if (tag === "button") return "button";
      if (tag === "a") return "link";
      if (tag === "textarea") return "textbox";
      if (tag === "select") return "combobox";
      if (element.isContentEditable || element.getAttribute("contenteditable") === "true") {
        return "textbox";
      }
      if (tag === "input") {
        const type = (element.getAttribute("type") || "text").toLowerCase();
        if (type === "checkbox") return "checkbox";
        if (type === "radio") return "radio";
        if (["submit", "button", "reset"].includes(type)) return "button";
        return "textbox";
      }
      return tag;
    }

    function selectorHints(element, role, text) {
      const hints = [];
      if (element.id) hints.push(`#${cssEscape(element.id)}`);
      for (const attribute of element.getAttributeNames()) {
        if (
          attribute === "data-testid" ||
          attribute === "data-test-id" ||
          attribute === "data-autosave-action" ||
          attribute.startsWith("data-action") ||
          attribute.startsWith("data-testid")
        ) {
          hints.push(`[${attribute}='${cssQuote(element.getAttribute(attribute))}']`);
        }
      }
      if (role && text) hints.push(`role=${role} name=${text}`);
      if (element.getAttribute("aria-label")) {
        hints.push(`[aria-label='${cssQuote(element.getAttribute("aria-label"))}']`);
      }
      if (element.name) hints.push(`${element.tagName.toLowerCase()}[name='${cssQuote(element.name)}']`);
      return hints.slice(0, 4);
    }

    function roundBox(rect) {
      return {
        x: Math.round(rect.x),
        y: Math.round(rect.y),
        width: Math.round(rect.width),
        height: Math.round(rect.height),
      };
    }

    function compactText(text) {
      return String(text || "").replace(/\s+/g, " ").trim().slice(0, 120);
    }

    function cssQuote(text) {
      return String(text || "").replace(/\\/g, "\\\\").replace(/'/g, "\\'");
    }

    function cssEscape(text) {
      if (window.CSS?.escape) return window.CSS.escape(text);
      return String(text || "").replace(/[^a-zA-Z0-9_-]/g, "\\$&");
    }
  }, {
    interactionLimit: INTERACTION_MAP_LIMIT,
    attentionLimit: ATTENTION_LIMIT,
    offset: Math.max(0, Number.isInteger(mapOptions.offset) ? mapOptions.offset : 0),
  });
}

function summarizeControlsForTest(documentLike, viewport, selector) {
  return Array.from(documentLike.querySelectorAll(selector))
    .map((element) => summarizeElementForTest(element, viewport))
    .filter(Boolean);
}

function summarizeElementForTest(element, viewport) {
  const rect = element.getBoundingClientRect();
  if (!rect || rect.width <= 0 || rect.height <= 0) {
    return null;
  }
  const role = element.getAttribute("role") || implicitRole(element);
  const text = controlName(element, role);
  const centerX = Math.max(0, Math.min(viewport.width - 1, rect.left + rect.width / 2));
  const centerY = Math.max(0, Math.min(viewport.height - 1, rect.top + rect.height / 2));
  const topElement = element.ownerDocument?.elementFromPoint?.(centerX, centerY);
  const covered = Boolean(topElement && topElement !== element && !element.contains(topElement));
  const disabled = Boolean(
    element.disabled ||
      element.getAttribute("aria-disabled") === "true" ||
      element.closest("[aria-disabled='true']"),
  );
  const offscreen =
    rect.bottom < 0 ||
    rect.right < 0 ||
    rect.top > viewport.height ||
    rect.left > viewport.width;
  return {
    role,
    name: text,
    disabled,
    covered,
    offscreen,
    box: roundBox(rect),
    selectors: selectorHints(element, role, text),
  };
}

function controlName(element, role) {
  const tag = element.tagName.toLowerCase();
  const inputType = (element.getAttribute("type") || "").toLowerCase();
  if (
    tag === "textarea" ||
    element.isContentEditable ||
    element.getAttribute("contenteditable") === "true" ||
    role === "textbox" ||
    (tag === "input" && !["button", "submit", "reset", "checkbox", "radio"].includes(inputType))
  ) {
    return formControlLabel(element);
  }

  const explicitName =
    element.getAttribute("aria-label") ||
    element.innerText ||
    element.textContent ||
    element.getAttribute("title") ||
    element.getAttribute("placeholder") ||
    element.getAttribute("alt") ||
    "";
  if (explicitName) return compactText(explicitName);

  if (tag === "input" && ["button", "submit", "reset"].includes(inputType)) {
    return compactText(element.getAttribute("value") || "");
  }
  return "";
}

function formControlLabel(element) {
  return compactText(
    element.getAttribute("aria-label") ||
      element.getAttribute("title") ||
      element.getAttribute("placeholder") ||
      element.getAttribute("name") ||
      element.getAttribute("id") ||
      "",
  );
}

function implicitRole(element) {
  const tag = element.tagName.toLowerCase();
  if (tag === "button") return "button";
  if (tag === "a") return "link";
  if (tag === "textarea") return "textbox";
  if (tag === "select") return "combobox";
  if (element.isContentEditable || element.getAttribute("contenteditable") === "true") {
    return "textbox";
  }
  if (tag === "input") {
    const type = (element.getAttribute("type") || "text").toLowerCase();
    if (type === "checkbox") return "checkbox";
    if (type === "radio") return "radio";
    if (["submit", "button", "reset"].includes(type)) return "button";
    return "textbox";
  }
  return tag;
}

function selectorHints(element, role, text) {
  const hints = [];
  if (element.id) hints.push(`#${cssEscape(element.id)}`);
  for (const attribute of element.getAttributeNames()) {
    if (
      attribute === "data-testid" ||
      attribute === "data-test-id" ||
      attribute === "data-autosave-action" ||
      attribute.startsWith("data-action") ||
      attribute.startsWith("data-testid")
    ) {
      hints.push(`[${attribute}='${cssQuote(element.getAttribute(attribute))}']`);
    }
  }
  if (role && text) hints.push(`role=${role} name=${text}`);
  if (element.getAttribute("aria-label")) {
    hints.push(`[aria-label='${cssQuote(element.getAttribute("aria-label"))}']`);
  }
  if (element.name) hints.push(`${element.tagName.toLowerCase()}[name='${cssQuote(element.name)}']`);
  return hints.slice(0, 4);
}

function roundBox(rect) {
  return {
    x: Math.round(rect.x),
    y: Math.round(rect.y),
    width: Math.round(rect.width),
    height: Math.round(rect.height),
  };
}

function compactText(text) {
  return String(text || "").replace(/\s+/g, " ").trim().slice(0, 120);
}

function cssQuote(text) {
  return String(text || "").replace(/\\/g, "\\\\").replace(/'/g, "\\'");
}

function cssEscape(text) {
  if (globalThis.CSS?.escape) return globalThis.CSS.escape(text);
  return String(text || "").replace(/[^a-zA-Z0-9_-]/g, "\\$&");
}

async function maybeSaveArtifacts({
  page,
  screenshots,
  request,
  profile,
  snapshot,
  summaries,
  actionTrail,
  serviceHeaders,
  success,
  error,
  failedAction,
  inspection,
}) {
  const policy = artifactPolicy();
  const explicit = request.arguments?.save_artifact === true;
  const shouldSave =
    explicit || policy === ARTIFACT_POLICY_ALWAYS || (!success && policy !== "off");
  if (!shouldSave) {
    return null;
  }
  const baseDir =
    trimmedEnv("CODEX_BROWSER_PLAYWRIGHT_ARTIFACT_DIR") ||
    path.join(profile.stateDir, "artifacts");
  const runDir = path.join(baseDir, artifactRunName(request, success));
  await fs.mkdir(runDir, { recursive: true });
  const screenshotFiles = [];
  for (let index = 0; index < screenshots.length; index += 1) {
    const capture = screenshots[index];
    const label = safePathComponent(capture.label || `capture-${index + 1}`);
    const fileName = `${String(index + 1).padStart(2, "0")}-${label}.png`;
    const filePath = path.join(runDir, fileName);
    await fs.writeFile(filePath, capture.screenshot.buffer);
    screenshotFiles.push({ label: capture.label || null, path: filePath, method: capture.screenshot.method });
  }
  const manifest = {
    createdAt: new Date().toISOString(),
    tool: request.tool,
    success,
    error: error || null,
    failedAction: failedAction ? actionSummary(failedAction) : null,
    url: pageUrl(page),
    title: snapshot.title,
    viewport: snapshot.viewport,
    scroll: snapshot.scroll,
    profile: profile.label,
    serviceActor: serviceHeaders?.actor || null,
    serviceProfile: serviceHeaders?.profileId || null,
    serviceHeaderNames: serviceHeaders?.headerNames || [],
    allowedHosts: serviceHeaders?.allowedHosts || [],
    actions: summaries,
    actionTrail,
    attention: snapshot.attention,
    interactionMap: snapshot.controls,
    screenshots: screenshotFiles,
    captureMetadata: screenshots.map((capture) => ({
      label: capture.label || null,
      metadata: capture.metadata || null,
    })),
    restoredState: screenshots.restoredState || null,
    inspection: inspection || null,
  };
  const manifestPath = path.join(runDir, "manifest.json");
  await fs.writeFile(manifestPath, JSON.stringify(manifest, null, 2));
  return { manifestPath, screenshotFiles };
}

function shouldIncludePageMetadata(request, success) {
  return !success || request.arguments?.scope === OBSERVE_SCOPE_VIEWPORT_AND_PAGE || request.arguments?.post_observe_scope === OBSERVE_SCOPE_VIEWPORT_AND_PAGE;
}

function appendServiceHeaderSummary(lines, plan) {
  if (!plan || plan.headerNames.length === 0) {
    return;
  }
  lines.push(
    `service_actor: ${plan.actor || "service account"} profile=${plan.profileId || "per-call"} headers=${plan.headerNames.map((name) => `${name}:<redacted>`).join(", ")} allowed_hosts=${plan.allowedHosts.join(",")}`,
  );
}

function appendNavigationSummary(lines, actionTrail) {
  const navigations = actionTrail.filter((entry) => entry.before?.url !== entry.after?.url);
  if (navigations.length === 0) {
    return;
  }
  lines.push("navigation:");
  for (const entry of navigations.slice(0, 6)) {
    lines.push(`- ${entry.before?.url || "unknown"} -> ${entry.after?.url || "unknown"} after ${entry.action}`);
  }
}

function appendStateMarkers(lines, snapshot) {
  if (snapshot.attention.length > 0) {
    lines.push("attention:");
    for (const item of snapshot.attention) {
      lines.push(`- ${item.role || "region"} "${item.text}" box=${boxText(item.box)}${item.offscreen ? " offscreen" : ""}`);
    }
  }
  if (snapshot.focused?.name || snapshot.focused?.role) {
    lines.push(`focused: ${controlText(snapshot.focused)}`);
  }
}

function appendInteractionMap(lines, snapshot) {
  const total = Number.isInteger(snapshot.interactionTotal)
    ? snapshot.interactionTotal
    : snapshot.controls.length;
  const omitted = Number.isInteger(snapshot.interactionOmitted)
    ? snapshot.interactionOmitted
    : Math.max(0, total - snapshot.controls.length);
  lines.push(`interaction_map (showing ${snapshot.controls.length} of ${total}; omitted ${omitted}):`);
  for (const control of snapshot.controls) {
    lines.push(`- ${controlText(control)}`);
  }
}

function appendSelectorCandidates(lines, failedAction, snapshot) {
  const selector = failedAction.selector;
  if (!selector || typeof selector === "string") {
    return;
  }
  const candidates = candidateControls(selector, snapshot.controls);
  if (candidates.length === 0) {
    return;
  }
  lines.push("selector_candidates:");
  for (const candidate of candidates.slice(0, 8)) {
    lines.push(`- ${controlText(candidate)}`);
  }
}

function candidateControls(selector, controls) {
  const wantedName = compactLower(selector.name || selector.text || selector.label || selector.placeholder || "");
  const wantedRole = selector.role ? String(selector.role).toLowerCase() : "";
  return controls.filter((control) => {
    const roleMatches = !wantedRole || String(control.role || "").toLowerCase() === wantedRole;
    const name = compactLower(control.name || "");
    const nameMatches = !wantedName || name.includes(wantedName) || wantedName.includes(name);
    return roleMatches && nameMatches;
  });
}

function controlText(control) {
  const flags = [
    control.disabled ? "disabled" : null,
    control.covered ? "covered" : null,
    control.offscreen ? "offscreen" : null,
  ]
    .filter(Boolean)
    .join(",");
  const suffix = flags ? ` ${flags}` : "";
  const selectors = control.selectors?.length ? ` selectors=${control.selectors.join(" | ")}` : "";
  return `${control.role || "element"} "${control.name || ""}" box=${boxText(control.box)}${suffix}${selectors}`;
}

function boxText(box) {
  if (!box) {
    return "unknown";
  }
  return `${box.x},${box.y},${box.width}x${box.height}`;
}

function compactLower(text) {
  return String(text || "").replace(/\s+/g, " ").trim().toLowerCase();
}

function failureTextWithoutScreenshot(page, failedAction, actionError, captureError, snapshot) {
  const lines = [
    "Browser action failed",
    `url: ${pageUrl(page)}`,
    `error: ${errorMessage(actionError)}`,
    `screenshot_error: ${errorMessage(captureError)}`,
    `failed_action: ${actionSummary(failedAction)}`,
  ];
  if (snapshot) {
    appendStateMarkers(lines, snapshot);
    appendInteractionMap(lines, snapshot);
    appendSelectorCandidates(lines, failedAction, snapshot);
  }
  return lines.join("\n");
}

function artifactPolicy() {
  const policy = (process.env.CODEX_BROWSER_PLAYWRIGHT_ARTIFACT_POLICY || ARTIFACT_POLICY_FAILURE)
    .trim()
    .toLowerCase();
  if (["off", "none", "never"].includes(policy)) {
    return "off";
  }
  if (policy === ARTIFACT_POLICY_ALWAYS) {
    return ARTIFACT_POLICY_ALWAYS;
  }
  return ARTIFACT_POLICY_FAILURE;
}

function captureMode() {
  const mode = (
    process.env.CODEX_BROWSER_PLAYWRIGHT_CAPTURE_MODE || CAPTURE_VIEWPORT
  ).toLowerCase();
  return mode === CAPTURE_FULL_PAGE ? CAPTURE_FULL_PAGE : CAPTURE_VIEWPORT;
}

function artifactRunName(request, success) {
  const label = request.arguments?.artifact_label || request.tool || "browser";
  const stamp = new Date().toISOString().replace(/[^0-9TZ]/g, "");
  const status = success ? "ok" : "failed";
  const id = request.callId || createHash("sha256").update(JSON.stringify(request.arguments || {})).digest("hex").slice(0, 12);
  return `${stamp}-${status}-${safePathComponent(label)}-${safePathComponent(id)}`;
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

function pageTitle(page) {
  return page
    .title()
    .then((title) => title)
    .catch(() => "");
}

function pageUrl(page) {
  try {
    return page.url();
  } catch {
    return "unknown";
  }
}

function numberOrDefault(value, fallback) {
  return typeof value === "number" && Number.isFinite(value) ? value : fallback;
}

function nonNegativeIntegerOrUndefined(value) {
  return Number.isInteger(value) && value >= 0 ? value : undefined;
}

function errorMessage(error) {
  return String(error?.message || error);
}

function compactCaptureErrors(errors) {
  return errors
    .map((error) => error.split("\n")[0])
    .join(" | ")
    .slice(0, 500);
}

function trimmedEnv(name) {
  const value = process.env[name];
  return value && value.trim() ? value.trim() : null;
}
