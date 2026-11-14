//
// tests/layout/sidebar-transition.mjs
// Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
//

import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const repo = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "../..");

const css = (name) => readFileSync(path.join(repo, "app/static/css", name), "utf8");
const materialIcons = css("material-icons.css").replace(
  /url\('\/static\/fonts\/MaterialIcons-Regular\.ttf'\) format\('truetype'\)/,
  `url('data:font/ttf;base64,${readFileSync(path.join(repo, "app/static/fonts/MaterialIcons-Regular.ttf")).toString("base64")}') format('truetype')`,
);
const sidebarJs = readFileSync(path.join(repo, "app/static/js/sidebar.js"), "utf8");
const styles = [css("bootstrap.min.css"), materialIcons, css("gentelella-layout.css")].join("\n");
const navItems = (count) => Array.from({ length: count }, (_, i) =>
  `<li><a class="ju-nav-link" id="nav-extra-${i}" href="/extra-${i}"><span class="material-icons" aria-hidden="true">folder</span><span class="ju-nav-text">Extra entry number ${i}</span></a></li>`).join("");
const pageHtml = (extra) => `<!doctype html>
<html lang="en" data-bs-theme="light"><head><meta charset="utf-8"><style>${styles}</style>
<style>html,body{margin:0;min-height:100%;} .ju-shell{min-height:100vh;}</style></head><body>
<div class="ju-shell">
  <div class="ju-content" id="page-content"><header class="ju-topbar"><button class="btn ju-topbar-button ju-topbar-toggle" type="button" id="sidebar-toggle" aria-controls="app-sidebar" aria-expanded="true" aria-label="Toggle navigation" title="Toggle navigation"><span class="material-icons" aria-hidden="true">menu</span></button><a class="ju-topbar-brand" href="/" aria-label="justUp home"><span class="ju-logo">justUp!</span></a></header><main id="main-content">Dashboard</main></div>
  <nav class="ju-sidebar" id="app-sidebar" aria-label="Main navigation">
    <div class="ju-sidebar-header"><a class="ju-sidebar-brand" href="/" aria-label="justUp home"><span class="ju-logo">justUp!</span><span class="ju-brand-mark" aria-hidden="true">J</span></a><button type="button" class="ju-sidebar-close" id="sidebar-close" aria-label="Close navigation"><span class="material-icons" aria-hidden="true">close</span></button></div>
    <ul class="ju-nav">
      <li><a class="ju-nav-link" id="nav-dashboard" href="/"><span class="material-icons" aria-hidden="true">speed</span><span class="ju-nav-text">Dashboard</span></a></li>
      <li><button class="ju-nav-link ju-nav-group-toggle" id="nav-manage" type="button" aria-expanded="true" aria-controls="nav-group"><span class="material-icons" aria-hidden="true">dns</span><span class="ju-nav-text">Manage</span><span class="material-icons ju-nav-chevron" aria-hidden="true">expand_more</span></button>
        <ul class="ju-nav-sub" id="nav-group"><li><a class="ju-nav-link active" aria-current="page" id="nav-target" href="/targets"><span class="material-icons" aria-hidden="true">gps_fixed</span><span class="ju-nav-text">Targets</span></a></li></ul>
      </li>
      ${navItems(extra)}
    </ul>
    <div class="ju-sidebar-controls"><button type="button" class="ju-nav-link ju-sidebar-toggle" id="sidebar-collapse" aria-controls="app-sidebar" aria-expanded="true" aria-label="Collapse sidebar"><span class="material-icons" aria-hidden="true">menu_open</span><span class="ju-nav-text ju-sidebar-toggle-label">Collapse sidebar</span></button></div>
    <div class="ju-profile"><span class="ju-profile-avatar material-icons" id="profile-avatar" aria-hidden="true">person</span><div class="ju-profile-text"><span class="ju-profile-label">Signed in as</span><span class="ju-profile-name">Operator</span></div></div>
  </nav>
</div><script>${sidebarJs}</script></body></html>`;
const PAGE = pageHtml(0);

// Everything measured in one page.evaluate; rects are relative to the viewport.
const snapshot = () => {
  const rect = (el) => el.getBoundingClientRect();
  const sidebar = document.getElementById("app-sidebar");
  const nav = sidebar.querySelector(".ju-nav");
  const sr = rect(sidebar);
  const axis = sr.left + sr.width / 2;
  const centerOf = (selector) => { const r = rect(document.querySelector(selector)); return r.left + r.width / 2 - axis; };
  const icons = {
    brand: ".ju-brand-mark", toggle: "#sidebar-collapse .material-icons", dashboard: "#nav-dashboard .material-icons",
    group: "#nav-manage .material-icons:first-child", nested: "#nav-target .material-icons", avatar: "#profile-avatar",
  };
  const centers = Object.fromEntries(Object.entries(icons).map(([key, sel]) => [key, centerOf(sel)]));
  const controls = [...sidebar.querySelectorAll("a[href], button")]
    .filter((el) => getComputedStyle(el).display !== "none" && rect(el).width > 0);
  const nr = rect(nav);
  // Entries scrolled out of the nav are clipped by it and cannot overlap anything.
  const boxes = controls.map((el) => ({ id: el.id || el.className, r: rect(el), scrolled: nav.contains(el) }))
    .map((box) => box.scrolled ? { ...box, r: { left: box.r.left, right: box.r.right, top: Math.max(box.r.top, nr.top), bottom: Math.min(box.r.bottom, nr.bottom) } } : box)
    .filter(({ r }) => r.bottom > r.top + 1);
  const overlaps = [];
  boxes.forEach((a, i) => boxes.slice(i + 1).forEach((b) => {
    const w = Math.min(a.r.right, b.r.right) - Math.max(a.r.left, b.r.left);
    const h = Math.min(a.r.bottom, b.r.bottom) - Math.max(a.r.top, b.r.top);
    if (w > 1 && h > 1) overlaps.push(`${a.id} x ${b.id}`);
  }));
  const hasScroll = nav.scrollHeight > nav.clientHeight + 1;
  const outside = boxes.filter(({ id, r }) => {
    if (r.left < sr.left - 1 || r.right > sr.right + 1) return true;
    const inNav = !!nav.querySelector(`[id="${id}"]`);
    return !inNav && (r.top < sr.top - 1 || r.bottom > sr.bottom + 1);
  }).map(({ id }) => id);
  const avatar = rect(document.getElementById("profile-avatar"));
  const toggle = rect(document.getElementById("sidebar-collapse"));
  const collapseBtn = document.getElementById("sidebar-collapse");
  const rectOf = (selector) => rect(document.querySelector(selector));
  return {
    centers, overlaps, outside, hasScroll,
    navScrollW: nav.scrollWidth, navClientW: nav.clientWidth, sidebarScrollW: sidebar.scrollWidth, sidebarClientW: sidebar.clientWidth,
    docScrollW: document.documentElement.scrollWidth, docClientW: document.documentElement.clientWidth,
    sidebarRight: sr.right, sidebarLeft: sr.left, sidebarW: sr.width, contentLeft: rect(document.getElementById("page-content")).left,
    contentMargin: parseFloat(getComputedStyle(document.getElementById("page-content")).marginLeft),
    toggle: { left: toggle.left, top: toggle.top, w: toggle.width, h: toggle.height, iconLeft: rectOf("#sidebar-collapse .material-icons").left },
    vertical: { header: rectOf(".ju-sidebar-header").bottom, nav: nav.getBoundingClientRect().bottom, controlsTop: rectOf(".ju-sidebar-controls").top, controlsBottom: rectOf(".ju-sidebar-controls").bottom, profileTop: rectOf(".ju-profile").top, profileBottom: rectOf(".ju-profile").bottom, viewport: innerHeight },
    collapseLabel: collapseBtn.getAttribute("aria-label"), collapseExpanded: collapseBtn.getAttribute("aria-expanded"),
    brands: { topbar: getComputedStyle(document.querySelector(".ju-topbar-brand")).display, sidebarW: rectOf(".ju-sidebar-brand .ju-logo").width, markW: rectOf(".ju-brand-mark").width },
    topbarToggleDisplay: getComputedStyle(document.getElementById("sidebar-toggle")).display,
    avatarVisible: avatar.left >= sr.left && avatar.right <= sr.right && avatar.bottom <= innerHeight && avatar.top >= 0,
    textVisible: rect(document.querySelector("#nav-dashboard .ju-nav-text")).width > 8,
    titles: [...sidebar.querySelectorAll(".ju-nav-link")].map((el) => el.getAttribute("title")),
    groupExpanded: document.getElementById("nav-manage").getAttribute("aria-expanded"),
    toggleExpanded: document.getElementById("sidebar-toggle").getAttribute("aria-expanded"),
    collapsed: document.documentElement.classList.contains("ju-nav-sm"),
  };
};
// Wait for the started CSS transitions to finish instead of a wall-clock delay: the timeline only
// advances with rendered frames, so a slow first frame would otherwise sample a half-open sidebar.
const settle = (page) => page.evaluate(async () => {
  const running = document.getAnimations().filter((a) => a instanceof CSSTransition);
  const limit = new Promise((_, reject) => setTimeout(() => reject(new Error("sidebar transitions did not finish within 10s")), 10000));
  await Promise.race([Promise.allSettled(running.map((a) => a.finished)), limit]);
  await new Promise((resolve) => requestAnimationFrame(() => resolve()));
});
const TOL = 1;

function assertCommon(m, label) {
  assert.ok(m.navScrollW <= m.navClientW, `${label}: horizontal scroll in navigation (${m.navScrollW} > ${m.navClientW})`);
  assert.ok(m.sidebarScrollW <= m.sidebarClientW, `${label}: sidebar overflows horizontally`);
  assert.ok(m.docScrollW <= m.docClientW, `${label}: document scrolls horizontally`);
  assert.deepEqual(m.outside, [], `${label}: interactive elements leave the sidebar`);
  assert.deepEqual(m.overlaps, [], `${label}: interactive elements overlap`);
  assert.ok(Math.abs(m.contentLeft - m.sidebarRight) <= TOL, `${label}: content starts at ${m.contentLeft}, sidebar ends at ${m.sidebarRight}`);
  assert.ok(m.toggle.h >= 44 && m.toggle.w >= 44, `${label}: toggle is smaller than 44px (${m.toggle.w}x${m.toggle.h})`);
  // The collapse control and the first-level icons share the rail axis (half the rail width, 36px)
  // in both states, so nothing slides sideways when the sidebar collapses.
  for (const name of ["toggle", "dashboard", "group"]) {
    assert.ok(Math.abs(m.centers[name] + m.sidebarW / 2 - 36) <= TOL, `${label}: ${name} icon is off the rail axis`);
  }
  const v = m.vertical;
  assert.ok(v.header <= v.nav + 1 && v.nav <= v.controlsTop + 1 && v.controlsBottom <= v.profileTop + 1, `${label}: header, nav, controls and profile overlap vertically`);
  assert.ok(v.profileBottom <= v.viewport + 1 && v.controlsTop >= 0, `${label}: controls or profile fall out of the viewport`);
  // DESIGN.md: the full wordmark appears once, in the top bar; the sidebar carries only the compact mark.
  assert.notEqual(m.brands.topbar, "none", `${label}: the wordmark must be shown in the top bar`);
  assert.equal(m.brands.sidebarW, 0, `${label}: the sidebar must not repeat the wordmark`);
  assert.ok(m.brands.markW > 0, `${label}: the sidebar shows the compact brand mark`);
  assert.equal(m.topbarToggleDisplay, "none", `${label}: the topbar toggle must not show next to the sidebar control on desktop`);
}

function assertRail(m, label) {
  assertCommon(m, label);
  assert.ok(m.collapsed && m.toggleExpanded === "false" && m.collapseExpanded === "false" && m.collapseLabel === "Expand sidebar", `${label}: rail state, aria-expanded or label wrong`);
  for (const [name, offset] of Object.entries(m.centers)) {
    assert.ok(Math.abs(offset) <= TOL, `${label}: ${name} icon center is ${offset.toFixed(1)}px off the rail axis`);
  }
  assert.ok(m.avatarVisible, `${label}: avatar is clipped`);
  assert.ok(m.titles.every(Boolean), `${label}: icon-only entries need a tooltip`);
  assert.equal(m.groupExpanded, "true", `${label}: group aria-expanded changed by collapsing`);
}

function assertExpanded(m, label) {
  assertCommon(m, label);
  assert.ok(!m.collapsed && m.toggleExpanded === "true" && m.collapseExpanded === "true" && m.collapseLabel === "Collapse sidebar", `${label}: expanded state, aria-expanded or label wrong`);
  assert.ok(m.textVisible, `${label}: expanded labels are not visible`);
  assert.ok(m.avatarVisible, `${label}: avatar is clipped`);
  assert.ok(m.centers.nested > m.centers.dashboard + 10, `${label}: nested entries lost their indent`);
  assert.ok(m.titles.every((title) => title === null), `${label}: expanded entries must not carry tooltips`);
}

// Intended alignment, not historical coordinates: icons are checked against the rail axis, and the
// toggle must keep one position through the whole transition.
async function transition(page) {
  await page.setViewportSize({ width: 1280, height: 800 });
  await page.setContent(PAGE, { waitUntil: "load" });
  await page.evaluate(() => document.fonts.ready);
  const result = await page.evaluate(async (snapshotSource) => {
    const snap = new Function(`return (${snapshotSource})`)();
    const toggle = document.getElementById("sidebar-collapse");
    const frames = [];
    // Pause every running transition and step it to fixed progress points, so the number and the
    // position of the sampled states never depend on frame rate or runner load.
    const STEPS = 10;
    const run = () => {
      const animations = document.getAnimations().filter((a) => a instanceof CSSTransition && a.transitionProperty !== "visibility");
      if (!animations.some((a) => a.transitionProperty === "width") || !animations.some((a) => a.transitionProperty === "margin-left")) {
        throw new Error(`sidebar and content transitions did not start: ${animations.map((a) => a.transitionProperty).join(",")}`);
      }
      for (const a of animations) a.pause();
      for (let step = 0; step <= STEPS; step++) {
        for (const a of animations) a.currentTime = (a.effect.getComputedTiming().duration * step) / STEPS;
        frames.push(snap());
      }
      for (const a of document.getAnimations()) a.finish();
    };
    const initial = snap();
    toggle.click();
    run();
    toggle.click();
    run();
    return { initial, frames };
  }, snapshot.toString());
  return result;
}

async function matrix(page) {
  let checks = 0;
  const viewports = [[1024, 768], [1280, 800], [1920, 1080], [1280, 360], [1920 / 1.5, 1080 / 1.5]];
  for (const [rawWidth, height] of viewports) {
    const width = Math.round(rawWidth);
    for (const theme of ["light", "dark"]) {
      for (const extra of [0, 30]) {
        await page.setViewportSize({ width, height: Math.round(height) });
        await page.setContent(pageHtml(extra), { waitUntil: "load" });
        await page.evaluate(() => document.fonts.ready);
        await page.evaluate((t) => document.documentElement.setAttribute("data-bs-theme", t), theme);
        const label = `${width}x${Math.round(height)} ${theme} extra=${extra}`;
        assertExpanded(await page.evaluate(snapshot), `expanded ${label}`);
        for (let cycle = 1; cycle <= 3; cycle++) {
          await page.locator("#sidebar-collapse").click();
          await settle(page);
          const rail = await page.evaluate(snapshot);
          assertRail(rail, `rail cycle ${cycle} ${label}`);
          if (extra) {
            assert.ok(rail.hasScroll, `rail ${label}: many entries must scroll vertically`);
            await page.locator("#nav-extra-29").scrollIntoViewIfNeeded();
            const last = await page.evaluate(() => { const r = document.getElementById("nav-extra-29").getBoundingClientRect(); const n = document.querySelector(".ju-nav").getBoundingClientRect(); return r.top >= n.top - 1 && r.bottom <= n.bottom + 1; });
            assert.ok(last, `rail ${label}: last entry is not reachable by scrolling`);
            await page.evaluate(() => document.querySelector(".ju-nav").scrollTo(0, 0));
          }
          await page.locator("#sidebar-collapse").click();
          await settle(page);
          assertExpanded(await page.evaluate(snapshot), `expanded cycle ${cycle} ${label}`);
          checks += 2;
        }
      }
    }
  }
  return checks;
}

const contrast = (a, b) => {
  const lum = (value) => {
    const [r, g, bl] = /rgba?\(([^)]+)\)/.exec(value)[1].split(/[ ,/]+/).filter(Boolean).slice(0, 3).map((n) => {
      const c = Number(n) / 255;
      return c <= 0.03928 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4;
    });
    return 0.2126 * r + 0.7152 * g + 0.0722 * bl;
  };
  const [hi, lo] = [lum(a), lum(b)].sort((x, y) => y - x);
  return (hi + 0.05) / (lo + 0.05);
};
async function focusRing(page) {
  await page.setViewportSize({ width: 1280, height: 800 });
  await page.setContent(PAGE, { waitUntil: "load" });
  await page.locator("#sidebar-collapse").click();
  await settle(page);
  for (const theme of ["light", "dark"]) {
    await page.evaluate((t) => document.documentElement.setAttribute("data-bs-theme", t), theme);
    for (const id of ["nav-dashboard", "nav-manage", "nav-target"]) {
      await page.focus(`#${id}`);
      await page.keyboard.press("Shift+Tab");
      await page.keyboard.press("Tab");
      const ring = await page.evaluate((target) => {
        const el = document.getElementById(target);
        const cs = getComputedStyle(el);
        const r = el.getBoundingClientRect();
        const sr = document.getElementById("app-sidebar").getBoundingClientRect();
        const probe = document.createElement("i");
        probe.style.cssText = "color:var(--ju-sidebar-ring);background:var(--ju-sidebar-bg);display:block;width:var(--ju-focus-outline-width)";
        document.body.append(probe);
        const token = getComputedStyle(probe);
        const tokens = { ring: token.color, bg: token.backgroundColor, width: probe.getBoundingClientRect().width };
        probe.remove();
        const bgOf = getComputedStyle(document.getElementById("app-sidebar")).backgroundColor;
        return { active: document.activeElement === el, width: parseFloat(cs.outlineWidth), style: cs.outlineStyle, color: cs.outlineColor, offset: parseFloat(cs.outlineOffset), bg: bgOf, tokens, inside: r.left >= sr.left && r.right <= sr.right };
      }, id);
      assert.ok(ring.active && ring.style !== "none" && ring.width > 0 && ring.inside, `${theme} ${id}: focus indicator is not visible inside the rail`);
      // DC-EX-FOCUS-2: the ring reads --ju-sidebar-ring, keeps the shared width, uses the V3 inset offset and reaches 3:1 on the sidebar.
      assert.equal(ring.color, ring.tokens.ring, `${theme} ${id}: ring colour is not --ju-sidebar-ring`);
      assert.equal(ring.width, ring.tokens.width, `${theme} ${id}: ring width is not --ju-focus-outline-width`);
      assert.equal(ring.offset, -ring.tokens.width, `${theme} ${id}: ring offset is not the V3 inset`);
      assert.ok(contrast(ring.color, ring.bg) >= 3, `${theme} ${id}: ring ${ring.color} on ${ring.bg} is below 3:1`);
    }
  }
  await page.focus("#sidebar-collapse");
  const toggleRing = await page.evaluate(() => { const cs = getComputedStyle(document.getElementById("sidebar-collapse")); return cs.outlineStyle !== "none" && parseFloat(cs.outlineWidth) > 0; });
  assert.ok(toggleRing, "sidebar control: focus indicator is not visible");
  await page.keyboard.press("Enter");
  assert.equal(await page.evaluate(() => document.documentElement.classList.contains("ju-nav-sm")), false, "keyboard activation expands the rail");
  await page.keyboard.press("Space");
  assert.equal(await page.evaluate(() => document.documentElement.classList.contains("ju-nav-sm")), true, "keyboard activation collapses the sidebar");
  // Collapsing the group in the rail hides its children and keeps aria-expanded truthful.
  await page.click("#nav-manage");
  assert.equal(await page.getAttribute("#nav-manage", "aria-expanded"), "false");
  assert.equal(await page.isVisible("#nav-target"), false);
}

// DC-A11Y-1: the sidebar accent (active marker and icon, an indicator) reaches 3:1 on the sidebar surface; the link colour
// (text) reaches 4.5:1 on the panel and page surfaces. Both are read from the computed tokens in each theme.
async function accentContrast(page) {
  await page.setViewportSize({ width: 1280, height: 800 });
  await page.setContent(PAGE, { waitUntil: "load" });
  for (const theme of ["light", "dark"]) {
    await page.evaluate((t) => document.documentElement.setAttribute("data-bs-theme", t), theme);
    const colours = await page.evaluate(() => {
      const read = (color, background) => {
        const probe = document.createElement("i");
        probe.style.cssText = `color:${color};background:${background};display:block`;
        document.body.append(probe);
        const cs = getComputedStyle(probe);
        const out = { color: cs.color, background: cs.backgroundColor };
        probe.remove();
        return out;
      };
      return {
        accent: read("var(--ju-sidebar-accent)", "var(--ju-sidebar-bg)"),
        linkPanel: read("var(--bs-link-color)", "var(--ju-panel-bg)"),
        linkPage: read("var(--bs-link-color)", "var(--bs-body-bg)"),
      };
    });
    const accent = contrast(colours.accent.color, colours.accent.background);
    assert.ok(accent >= 3, `${theme}: sidebar accent ${colours.accent.color} on ${colours.accent.background} is ${accent.toFixed(2)}:1, below 3:1`);
    for (const surface of ["linkPanel", "linkPage"]) {
      const ratio = contrast(colours[surface].color, colours[surface].background);
      assert.ok(ratio >= 4.5, `${theme}: link ${colours[surface].color} on ${colours[surface].background} (${surface}) is ${ratio.toFixed(2)}:1, below 4.5:1`);
    }
  }
}

async function mobile(page) {
  await page.setViewportSize({ width: 390, height: 844 });
  await page.setContent(PAGE, { waitUntil: "load" });
  const handle = page.getByRole("button", { name: "Toggle navigation" });
  const closed = await handle.boundingBox();
  const header = await page.locator(".ju-topbar").boundingBox();
  assert.equal(await handle.evaluate((button) => button.parentElement.classList.contains("ju-topbar")), true, "mobile toggle belongs to the topbar");
  assert.ok(closed && closed.x >= 0 && closed.x + closed.width <= 390, "mobile handle remains visible while the drawer is closed");
  assert.ok(closed.width >= 44 && closed.height >= 44, "mobile handle meets the minimum pointer target size");
  assert.ok(header && Math.abs((closed.y + closed.height / 2) - (header.y + header.height / 2)) <= 1.5, "mobile toggle is vertically centered in the topbar");
  assert.equal(await handle.evaluate((button) => getComputedStyle(button).position), "static", "mobile toggle stays in flow");
  assert.equal(await page.locator(".ju-topbar-brand").isVisible(), true, "mobile shows the brand in the topbar");
  assert.equal(await page.locator("#sidebar-collapse").isVisible(), false, "the desktop collapse control is not part of the mobile drawer");
  await handle.click();
  await page.waitForFunction(() => document.documentElement.classList.contains("ju-nav-open"));
  assert.equal(await handle.getAttribute("aria-expanded"), "true", "mobile aria-expanded follows the open drawer");
  const drawer = await page.evaluate(() => { const m = document.querySelector("#nav-dashboard .ju-nav-text").getBoundingClientRect(); const n = document.querySelector(".ju-nav"); return { labelWidth: m.width, hscroll: n.scrollWidth > n.clientWidth, titled: document.getElementById("nav-dashboard").hasAttribute("title") }; });
  assert.ok(drawer.labelWidth > 8 && !drawer.hscroll && !drawer.titled, "mobile drawer shows full labels without scrolling or tooltips");
  await page.getByRole("button", { name: "Close navigation" }).click();
  assert.equal(await handle.getAttribute("aria-expanded"), "false", "mobile handle closes the drawer");
}

export { accentContrast, assertExpanded, focusRing, matrix, mobile, transition };
