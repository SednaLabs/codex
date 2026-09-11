import assert from "node:assert/strict";
import { createRequire } from "node:module";
import test from "node:test";
import { __test } from "./browser_playwright_review.mjs";

const require = createRequire(import.meta.url);
const { chromium } = require("playwright");

let browser;

test.before(async () => {
  browser = await chromium.launch({ headless: true });
});

test.after(async () => {
  await browser?.close();
});

async function withPage(html, callback) {
  const page = await browser.newPage({ viewport: { width: 900, height: 600 } });
  try {
    await page.setContent(html);
    return await callback(page);
  } finally {
    await page.close();
  }
}

function pageMarkup(extra = "") {
  const header = Array.from({ length: 45 }, (_, index) =>
    `<a href="#header-${index}" data-testid="header-${index}">Header ${index}</a>`,
  ).join("");
  const bottom = Array.from({ length: 5 }, (_, index) =>
    `<a href="#bottom-${index}" data-testid="bottom-${index}">Bottom ${index}</a>`,
  ).join("");
  return `<!doctype html><style>
    body { margin: 0; min-height: 2600px; font: 14px sans-serif; }
    header { padding: 12px; display: flex; flex-wrap: wrap; gap: 4px; }
    #bottom { position: absolute; top: 2200px; padding: 20px; }
    #fixed { position: fixed; right: 12px; bottom: 12px; z-index: 3; }
    #menu { display: none; position: fixed; inset: 80px auto auto 20px; z-index: 8; background: white; padding: 20px; }
    #covered { position: fixed; left: 20px; top: 120px; z-index: 1; }
    #cover { position: fixed; left: 20px; top: 120px; width: 180px; height: 50px; z-index: 9; }
  </style>
  <header>${header}</header>
  <button id="fixed">Fixed action</button>
  <button id="covered">Covered action</button><div id="cover"></div>
  <nav id="menu" role="menu"><a href="#menu-a" data-testid="menu-a">Menu A</a><a href="#menu-b" data-testid="menu-b">Menu B</a></nav>
  <section id="bottom">${bottom}<input id="offscreen-editor" aria-label="Offscreen editor"><button id="disabled" disabled>Disabled action</button><button aria-label="Duplicate" data-testid="duplicate-a">Duplicate</button><button aria-label="Duplicate" data-testid="duplicate-b">Duplicate</button></section>
  ${extra}`;
}

test("page snapshot caps and counts the complete long-page result at top and bottom", async () => {
  await withPage(pageMarkup(), async (page) => {
    const top = await __test.pageSnapshot(page);
    assert.equal(top.controls.length, 24);
    assert.equal(top.interactionTotal, 56);
    assert.equal(top.interactionOmitted, 32);
    assert(top.controls.every((control) => control.selectors.length > 0));

    await page.evaluate(() => window.scrollTo(0, document.documentElement.scrollHeight));
    const bottom = await __test.pageSnapshot(page);
    assert.equal(bottom.controls.length, 24);
    assert.equal(bottom.interactionTotal, top.interactionTotal);
    assert(bottom.controls.some((control) => control.offscreen));
    assert(bottom.controls.some((control) => control.name.startsWith("Bottom ")));

    const offset = await __test.pageSnapshot(page, { offset: 24 });
    assert.equal(offset.interactionOffset, 24);
    assert.equal(offset.controls.length, 24);
    assert.equal(offset.interactionOmitted, 32);
  });
});

test("page snapshot discovers a newly visible menu without retaining hidden controls", async () => {
  await withPage(pageMarkup(), async (page) => {
    const hidden = await __test.pageSnapshot(page);
    assert(!hidden.controls.some((control) => control.name === "Menu A"));

    await page.locator("#menu").evaluate((menu) => { menu.style.display = "block"; });
    const visible = await __test.pageSnapshot(page);
    assert(visible.controls.some((control) => control.name === "Menu A"));
    assert(visible.controls.some((control) => control.name === "Menu B"));
    assert.equal(visible.controls[0].name, "Menu A");
  });
});

test("page snapshot prioritizes a focused offscreen control and preserves duplicate selectors", async () => {
  await withPage(pageMarkup(), async (page) => {
    await page.evaluate(() => document.querySelector("#offscreen-editor").focus({ preventScroll: true }));
    const snapshot = await __test.pageSnapshot(page);
    assert.equal(snapshot.focused.name, "Offscreen editor");
    assert.equal(snapshot.controls[0].name, "Offscreen editor");
    assert(snapshot.controls[0].offscreen);

    const discovered = [];
    for (const offset of [0, 24, 48]) {
      discovered.push(...(await __test.pageSnapshot(page, { offset })).controls);
    }
    const duplicates = discovered.filter((control) => control.name === "Duplicate");
    assert.equal(duplicates.length, 2);
    assert.notDeepEqual(duplicates[0].selectors, duplicates[1].selectors);
  });
});

test("page snapshot retains disabled, covered, and offscreen geometry flags", async () => {
  await withPage(pageMarkup(), async (page) => {
    const discovered = [];
    for (const offset of [0, 24, 48]) {
      discovered.push(...(await __test.pageSnapshot(page, { offset })).controls);
    }
    const covered = discovered.find((control) => control.name === "Covered action");
    const disabled = discovered.find((control) => control.name === "Disabled action");
    const offscreen = discovered.find((control) => control.name === "Bottom 0");
    assert.equal(covered?.covered, true);
    assert.equal(disabled?.disabled, true);
    assert.equal(offscreen?.offscreen, true);
  });
});
