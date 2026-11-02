//
// tests/layout/sidebar-transition.mjs
// Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
//

import assert from "node:assert/strict";
import { createRequire } from "node:module";
import { readFileSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const repo = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "../..");
const SKIP = 77;

function loadPlaywright() {
  for (const base of [process.cwd(), process.env.PLAYWRIGHT_DIR].filter(Boolean)) {
    try { return createRequire(path.join(path.resolve(base), "/"))("playwright"); } catch { /* try next */ }
  }
  return null;
}

const playwright = loadPlaywright();
if (!playwright) {
  console.log("SKIP: Playwright is not installed (set PLAYWRIGHT_DIR)");
  process.exit(SKIP);
}

const css = (name) => readFileSync(path.join(repo, "app/static/css", name), "utf8");
const materialIcons = css("material-icons.css").replace(
  /url\('\/static\/fonts\/MaterialIcons-Regular\.ttf'\) format\('truetype'\)/,
  `url('data:font/ttf;base64,${readFileSync(path.join(repo, "app/static/fonts/MaterialIcons-Regular.ttf")).toString("base64")}') format('truetype')`,
);
const sidebarJs = readFileSync(path.join(repo, "app/static/js/sidebar.js"), "utf8");
const styles = [css("bootstrap.min.css"), materialIcons, css("gentelella-layout.css"), css("sidebar-handle.css")].join("\n");
const PAGE = `<!doctype html>
<html lang="en" data-bs-theme="light"><head><meta charset="utf-8"><style>${styles}</style>
<style>html,body{margin:0;min-height:100%;} .ju-shell{min-height:100vh;}</style></head><body>
<div class="ju-shell">
  <div class="ju-content" id="page-content"><header class="ju-topbar"><button class="btn ju-sidebar-handle" type="button" id="sidebar-toggle" aria-controls="app-sidebar" aria-expanded="true" aria-label="Toggle navigation"><span class="material-icons" aria-hidden="true">menu</span></button></header><main id="main-content">Dashboard</main></div>
  <nav class="ju-sidebar" id="app-sidebar" aria-label="Main navigation">
    <div class="ju-sidebar-header"><a class="ju-sidebar-brand" href="/" aria-label="justUp home"><span class="ju-logo">justUp!</span><span class="ju-brand-mark" aria-hidden="true">J</span></a><button type="button" class="ju-sidebar-close" id="sidebar-close" aria-label="Close navigation"><span class="material-icons" aria-hidden="true">close</span></button></div>
    <ul class="ju-nav">
      <li><a class="ju-nav-link" id="nav-dashboard" href="/"><span class="material-icons" aria-hidden="true">speed</span><span class="ju-nav-text">Dashboard</span></a></li>
      <li><button class="ju-nav-link ju-nav-group-toggle" id="nav-manage" type="button" aria-expanded="true" aria-controls="nav-group"><span class="material-icons" aria-hidden="true">dns</span><span class="ju-nav-text">Manage</span><span class="material-icons ju-nav-chevron" aria-hidden="true">expand_more</span></button>
        <ul class="ju-nav-sub" id="nav-group"><li><a class="ju-nav-link" id="nav-target" href="/targets"><span class="material-icons" aria-hidden="true">gps_fixed</span><span class="ju-nav-text">Targets</span></a></li></ul>
      </li>
    </ul>
    <div class="ju-profile"><span class="ju-profile-avatar material-icons" id="profile-avatar" aria-hidden="true">person</span><div class="ju-profile-text"><span class="ju-profile-label">Signed in as</span><span class="ju-profile-name">Operator</span></div></div>
  </nav>
</div><script>${sidebarJs}</script></body></html>`;

const measured = async (browser) => {
  const page = await browser.newPage({ viewport: { width: 1280, height: 800 } });
  await page.setContent(PAGE, { waitUntil: "load" });
  await page.evaluate(() => document.fonts.ready);
  const result = await page.evaluate(async () => {
    const sidebar = document.getElementById("app-sidebar");
    const content = document.getElementById("page-content");
    const toggle = document.getElementById("sidebar-toggle");
    const selectors = [".ju-sidebar-brand", "#profile-avatar", "#nav-dashboard .material-icons", "#nav-manage .material-icons:first-child", "#nav-target .material-icons"];
    const sample = () => ({
      width: sidebar.getBoundingClientRect().width,
      margin: parseFloat(getComputedStyle(content).marginLeft),
      handleEdge: toggle.getBoundingClientRect().left - sidebar.getBoundingClientRect().right,
      icons: selectors.map((selector) => document.querySelector(selector).getBoundingClientRect().left),
    });
    const initial = sample();
    toggle.click();
    const frames = [];
    const started = performance.now();
    do {
      await new Promise(requestAnimationFrame);
      frames.push(sample());
    } while (performance.now() - started < 260);
    return { initial, frames, collapsed: document.documentElement.classList.contains("ju-nav-sm"), expanded: toggle.getAttribute("aria-expanded") };
  });
  await page.close();
  return result;
};

try {
  const browser = await playwright.chromium.launch();
  try {
    const { initial, frames, collapsed, expanded } = await measured(browser);
    assert.ok(collapsed, "the desktop toggle enters rail mode");
    assert.equal(expanded, "false", "aria-expanded follows the rail state");
    assert.ok(frames.length >= 10, "the transition was sampled across animation frames");
    for (const [index, frame] of frames.entries()) {
      assert.ok(Math.abs(frame.width - frame.margin) <= 1.5, `content margin diverged from sidebar width at frame ${index}`);
      assert.ok(Math.abs(frame.handleEdge) <= 1.5, `handle diverged from sidebar edge at frame ${index}: ${frame.handleEdge}`);
      frame.icons.forEach((left, iconIndex) => {
        assert.ok(Math.abs(left - initial.icons[iconIndex]) <= 1.5, `sidebar item ${iconIndex} shifted at frame ${index}: ${initial.icons[iconIndex]} -> ${left}`);
      });
    }
    const page = await browser.newPage({ viewport: { width: 390, height: 844 } });
    await page.setContent(PAGE, { waitUntil: "load" });
    const handle = page.getByRole("button", { name: "Toggle navigation" });
    const closed = await handle.boundingBox();
    const header = await page.locator(".ju-topbar").boundingBox();
    assert.equal(await handle.evaluate((button) => button.parentElement.classList.contains("ju-topbar")), true, "mobile toggle belongs to the topbar");
    assert.ok(closed && closed.x >= 0 && closed.x + closed.width <= 390, "mobile handle remains visible while the drawer is closed");
    assert.ok(closed.width >= 44 && closed.height >= 44, "mobile handle meets the minimum pointer target size");
    assert.ok(header && Math.abs((closed.y + closed.height / 2) - (header.y + header.height / 2)) <= 1.5, "mobile toggle is vertically centered in the topbar");
    await handle.click();
    await page.waitForFunction(() => document.documentElement.classList.contains("ju-nav-open"));
    assert.equal(await handle.getAttribute("aria-expanded"), "true", "mobile aria-expanded follows the open drawer");
    await page.getByRole("button", { name: "Close navigation" }).click();
    assert.equal(await handle.getAttribute("aria-expanded"), "false", "mobile handle closes the drawer");
    await page.close();
    console.log(`Sidebar transition: ${frames.length} Chromium frames sampled; desktop edge alignment, mobile topbar placement, and ${initial.icons.length} positions stayed aligned.`);
  } finally {
    await browser.close();
  }
} catch (error) {
  console.error(error);
  process.exitCode = 1;
}
