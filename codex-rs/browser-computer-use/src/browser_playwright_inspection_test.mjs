import assert from "node:assert/strict";
import test from "node:test";
import { __test, inspectPage, installInspection } from "./browser_playwright_inspection.mjs";

test("inspection is absent unless explicitly requested", async () => {
  const page = fakePage();
  const installed = await installInspection(page, { arguments: {} });
  assert.equal(installed.collector, null);
  assert.equal(await installed.snapshot(), null);
  assert.throws(
    () => __test.inspectionRequest({ arguments: { inspection: { sections: ["bogus"] } } }),
    /sections are invalid/,
  );
  assert.deepEqual(
    __test.inspectionRequest({ arguments: { inspection: { sections: ["runtime"], script: "secret" } } }),
    { sections: ["runtime"] },
  );
});

test("inspection caps event rows and strips hostile console/network data", async () => {
  const page = fakePage();
  const installed = await installInspection(page, { arguments: { inspection: {} } });
  for (let i = 0; i < 25; i += 1) {
    page.emit("console", { type: () => "error", text: () => "SECRET console payload" });
    page.emit("response", {
      status: () => 200,
      url: () => "https://example.test/path?token=SECRET#fragment",
      request: () => ({ method: () => "X-SECRET-METHOD", resourceType: () => "xhr", postData: () => "SECRET body" }),
    });
  }
  const result = await installed.snapshot();
  assert.equal(result.console.length, 20);
  assert.equal(result.network.length, 20);
  assert.equal(result.omitted.console, 5);
  assert.equal(result.omitted.network, 5);
  assert.equal(result.observed.console, 25);
  assert.equal(result.observed.network, 25);
  const text = JSON.stringify(result);
  assert(!text.includes("SECRET"));
  assert(!text.includes("/path"));
  assert(!text.includes("token"));
  assert.equal(result.network[0].destination, "https://example.test");
  assert.equal(result.network[0].method, "OTHER");
  await installed.cleanup();
  assert.equal(page.listenerCount("console"), 0);
  assert.equal(page.listenerCount("response"), 0);
});

test("frame inspection reports geometry and origin without reading contents", async () => {
  const page = fakePage();
  const main = page.mainFrame();
  const child = { url: () => "https://evil.test/frame?secret=1", frameElement: async () => ({ boundingBox: async () => ({ x: 1, y: 2, width: 3, height: 4 }) }) };
  page.frames = () => [main, child];
  const result = await inspectPage(page, { arguments: { inspection: { sections: ["frames"] } } });
  assert.deepEqual(result.frames, [
    { main: true, url: "https://example.test", crossOrigin: false, geometry: null },
    { main: false, url: "https://evil.test", crossOrigin: true, geometry: { x: 1, y: 2, width: 3, height: 4 } },
  ]);
  assert.equal(page.evaluateCalls, 0);
});

function fakePage() {
  const listeners = new Map();
  const main = { url: () => "https://example.test" };
  return {
    evaluateCalls: 0,
    on(event, listener) { listeners.set(event, listener); },
    off(event) { listeners.delete(event); },
    emit(event, value) { listeners.get(event)?.(value); },
    listenerCount(event) { return listeners.has(event) ? 1 : 0; },
    frames: () => [main],
    mainFrame: () => main,
    url: () => "https://example.test",
    async evaluate() { this.evaluateCalls += 1; return { userAgent: "present", markers: [] }; },
  };
}
