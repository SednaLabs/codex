import assert from "node:assert/strict";
import { createRequire } from "node:module";
import fs from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import http from "node:http";
import { spawn } from "node:child_process";
import { fileURLToPath } from "node:url";
import test from "node:test";
import { captureBundle, pageResponseAfterFailure, responseForPage } from "./browser_playwright_review.mjs";

test("capture bundle records requested and effective responsive viewport evidence", async () => {
  const { chromium } = createRequire(import.meta.url)("playwright");

  const browser = await chromium.launch({ headless: true });
  try {
    const page = await browser.newPage({ viewport: { width: 1600, height: 1440 } });
    await page.route("https://example.invalid/**", (route) => route.fulfill({ body: "<p>frame fixture</p>" }));
    const responsiveMarkup = `
      <style>
        body { margin: 0; min-height: 2400px; }
        @media (max-width: 1539px) { body { background: rgb(1, 2, 3); } }
        @media (max-width: 600px) { body { background: rgb(4, 5, 6); } }
        @media (max-height: 1439px) { body { outline: 3px solid red; } }
      </style>
      <button aria-label="review">Review</button>
      <iframe title="external-frame" src="https://example.invalid/frame" style="width: 200px; height: 100px"></iframe>
    `;
    await page.setContent(responsiveMarkup);
    assert.equal(await page.evaluate(() => getComputedStyle(document.body).outlineStyle), "none");
    await page.setViewportSize({ width: 1600, height: 1439 });
    assert.equal(await page.evaluate(() => getComputedStyle(document.body).outlineStyle), "solid");
    await page.setViewportSize({ width: 1600, height: 1440 });
    const freshHeightBreakpoint = await browser.newPage({ viewport: { width: 1600, height: 1439 } });
    await freshHeightBreakpoint.route("https://example.invalid/**", (route) => route.fulfill({ body: "<p>frame fixture</p>" }));
    await freshHeightBreakpoint.setContent(responsiveMarkup);
    assert.equal(await freshHeightBreakpoint.evaluate(() => getComputedStyle(document.body).outlineStyle), "solid");
    await freshHeightBreakpoint.close();
    await page.evaluate(() => window.scrollTo(0, 320));

    const screenshots = await captureBundle(page, {
      arguments: {
        captures: [
          { label: "desktop", viewportWidth: 1600, viewportHeight: 1440, scroll: "top" },
          { label: "breakpoint", viewportWidth: 1539, viewportHeight: 900, scroll: "current" },
          { label: "mobile", viewportWidth: 390, viewportHeight: 844, scroll: "bottom" },
        ],
      },
    });

    assert.deepEqual(screenshots.map(({ label }) => label), ["desktop", "breakpoint", "mobile"]);
    assert.equal(screenshots[0].metadata.requestedViewport.width, 1600);
    assert.equal(screenshots[1].metadata.requestedViewport.width, 1539);
    assert.equal(screenshots[2].metadata.requestedViewport.width, 390);
    for (const capture of screenshots) {
      assert.ok(capture.metadata.clientViewport.width <= capture.metadata.effectiveViewport.width);
      assert.equal(typeof capture.metadata.devicePixelRatio, "number");
      assert.equal(typeof capture.metadata.scroll.y, "number");
      assert.ok(capture.screenshot.buffer.length > 0);
    }
    assert.equal(screenshots.restoredState.requestedViewport.width, 1600);
    assert.equal(screenshots.restoredState.effectiveViewport.width, 1600);
    assert.equal(screenshots.restoredState.scroll.y, 320);

    const response = await responseForPage(
      page,
      screenshots,
      ["scrolled browser viewport"],
      { label: "test", stateDir: "/tmp/unused" },
      { request: { arguments: { scope: "viewport_and_page" } }, actionTrail: [], success: true },
    );
    const text = response.contentItems[0].text;
    assert.match(text, /capture_metadata desktop/);
    assert.match(text, /capture_metadata breakpoint/);
    assert.match(text, /capture_metadata mobile/);
    assert.match(text, /capture_restored/);
    assert.match(text, /iframe "external-frame"/);
  } finally {
    await browser.close();
  }
});

test("artifact manifest agrees with native capture metadata", async () => {
  const { chromium } = createRequire(import.meta.url)("playwright");
  const stateDir = await fs.mkdtemp(path.join(os.tmpdir(), "codex-browser-evidence-"));
  const browser = await chromium.launch({ headless: true });
  try {
    const page = await browser.newPage({ viewport: { width: 800, height: 600 } });
    await page.setContent("<main style='height: 1800px'>evidence</main>");
    const screenshots = await captureBundle(page, {
      arguments: { captures: [{ label: "top", scroll: "top" }, { label: "bottom", scroll: "bottom" }] },
    });
    const response = await responseForPage(
      page,
      screenshots,
      [],
      { label: "test", stateDir },
      { request: { tool: "browser_observe", arguments: { save_artifact: true, artifact_label: "evidence" } } },
    );
    const manifestPath = response.contentItems[0].text.match(/^artifacts: (.+)$/m)?.[1];
    assert.ok(manifestPath);
    const manifest = JSON.parse(await fs.readFile(manifestPath, "utf8"));
    assert.deepEqual(manifest.captureMetadata.map(({ label }) => label), ["top", "bottom"]);
    assert.equal(manifest.restoredState.scroll.y, screenshots.restoredState.scroll.y);
    assert.equal(manifest.screenshots.length, 2);
    assert.equal(response.contentItems.filter(({ type }) => type === "inputImage").length, 2);
  } finally {
    await browser.close();
    await fs.rm(stateDir, { recursive: true, force: true });
  }
});

test("provider records selector and wheel settled trail in its manifest", async () => {
  const stateDir = await fs.mkdtemp(path.join(os.tmpdir(), "codex-browser-provider-"));
  const server = http.createServer((_request, response) => {
    response.end("<button aria-label='advance'>Advance</button><main style='height:2400px'></main>");
  });
  await new Promise((resolve) => server.listen(0, "127.0.0.1", resolve));
  const address = server.address();
  const request = {
    tool: "browser_step",
    arguments: {
      url: `http://127.0.0.1:${address.port}/`,
      save_artifact: true,
      actions: [
        { type: "click", selector: { role: "button", name: "Advance" } },
        { type: "mouse_wheel", scroll_y: 360 },
      ],
    },
  };
  const child = spawn(process.execPath, [fileURLToPath(new URL("./browser_playwright_provider.mjs", import.meta.url))], {
    env: { ...process.env, CODEX_BROWSER_PLAYWRIGHT_STATE_DIR: stateDir, CODEX_BROWSER_PLAYWRIGHT_ARTIFACT_POLICY: "always" },
    stdio: ["pipe", "pipe", "inherit"],
  });
  child.stdin.end(JSON.stringify(request));
  const output = await new Promise((resolve, reject) => {
    let text = "";
    child.stdout.on("data", (chunk) => { text += chunk; });
    child.on("error", reject);
    child.on("close", () => resolve(text));
  });
  try {
    const response = JSON.parse(output);
    const manifestPath = response.contentItems[0].text.match(/^artifacts: (.+)$/m)?.[1];
    assert.ok(manifestPath);
    const manifest = JSON.parse(await fs.readFile(manifestPath, "utf8"));
    assert.equal(manifest.actionTrail[0].action, 'click selector={"role":"button","name":"Advance"}');
    assert.equal(manifest.actionTrail[1].action, "mouse_wheel");
    assert.ok(manifest.actionTrail[1].settledAfter?.scroll?.y > 0);
    assert.deepEqual(manifest.actionTrail[1].settledAfter.scroll, manifest.scroll);
  } finally {
    server.close();
    await fs.rm(stateDir, { recursive: true, force: true });
  }
});

test("provider lets a first navigate action replace stale persisted state", async () => {
  const stateDir = await fs.mkdtemp(path.join(os.tmpdir(), "codex-browser-provider-navigate-"));
  const server = http.createServer((_request, response) => {
    response.end("<main>healthy destination</main>");
  });
  await new Promise((resolve) => server.listen(0, "127.0.0.1", resolve));
  const address = server.address();
  const healthyUrl = `http://127.0.0.1:${address.port}/healthy`;
  await fs.writeFile(path.join(stateDir, "state.json"), JSON.stringify({ url: "http://127.0.0.1:1/stale" }));
  const request = {
    tool: "browser_step",
    arguments: { actions: [{ type: "navigate", url: healthyUrl }] },
  };
  const child = spawn(process.execPath, [fileURLToPath(new URL("./browser_playwright_provider.mjs", import.meta.url))], {
    env: {
      ...process.env,
      CODEX_BROWSER_PLAYWRIGHT_STATE_DIR: stateDir,
      CODEX_BROWSER_PLAYWRIGHT_ISOLATION: "shared",
    },
    stdio: ["pipe", "pipe", "inherit"],
  });
  child.stdin.end(JSON.stringify(request));
  const output = await new Promise((resolve, reject) => {
    let text = "";
    child.stdout.on("data", (chunk) => { text += chunk; });
    child.on("error", reject);
    child.on("close", () => resolve(text));
  });
  try {
    const response = JSON.parse(output);
    assert.equal(response.success, true);
    assert.match(response.contentItems[0].text, new RegExp(`url: ${healthyUrl}`));
  } finally {
    server.close();
    await fs.rm(stateDir, { recursive: true, force: true });
  }
});

test("failed page metadata retains a captured native screenshot", async () => {
  let evaluateCount = 0;
  const screenshot = Buffer.from("png-fixture");
  const page = {
    url: () => "http://fixture.invalid/",
    title: async () => "Fixture",
    viewportSize: () => ({ width: 800, height: 600 }),
    screenshot: async () => screenshot,
    evaluate: async () => {
      evaluateCount += 1;
      if (evaluateCount === 1) {
        return {
          effectiveViewport: { width: 800, height: 600 },
          clientViewport: { width: 800, height: 600 },
          document: { width: 800, height: 600 },
          scroll: { x: 0, y: 0 },
          devicePixelRatio: 1,
          visualViewportScale: 1,
        };
      }
      throw new Error("metadata evaluation failed");
    },
  };
  const response = await pageResponseAfterFailure(
    page,
    [],
    { label: "test" },
    { arguments: { interaction_map: { scope: "page", offset: 3 } } },
    { type: "click" },
    new Error("click failed"),
    [],
    null,
  );
  assert.equal(response.success, false);
  assert.equal(response.contentItems.filter(({ type }) => type === "inputImage").length, 1);
  assert.match(response.contentItems[0].text, /Page metadata unavailable/);
});

test("response retains supplied native screenshot when page metadata fails", async () => {
  const page = {
    url: () => "http://fixture.invalid/",
    title: async () => "Fixture",
    viewportSize: () => ({ width: 800, height: 600 }),
    evaluate: async () => { throw new Error("metadata evaluation failed"); },
  };
  const response = await responseForPage(
    page,
    [{
      label: "failure",
      screenshot: { buffer: Buffer.from("png-fixture"), method: "fixture" },
      requestedViewport: { width: 800, height: 600 },
    }],
    [],
    { label: "test" },
    { request: { arguments: { interaction_map: { scope: "page", offset: 3 } } }, success: false, error: "click failed" },
  );
  assert.equal(response.success, false);
  assert.equal(response.contentItems.filter(({ type }) => type === "inputImage").length, 1);
  assert.match(response.contentItems[0].text, /Page metadata unavailable/);
});
