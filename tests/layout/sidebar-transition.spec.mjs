//
// tests/layout/sidebar-transition.spec.mjs
// Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
//

import assert from "node:assert/strict";
import { test } from "@playwright/test";
import { assertExpanded, focusRing, matrix, mobile, transition } from "./sidebar-transition.mjs";

test("sidebar transition preserves alignment throughout animation @sidebar", async ({ page }) => {
	const { initial, frames } = await transition(page);
	assertExpanded(initial, "initial");
	assert.equal(frames.length, 22, "both transitions were sampled at fixed progress points");
	for (const [index, frame] of frames.entries()) {
		assert.ok(Math.abs(frame.sidebarW - frame.contentMargin) <= 1.5, `content margin diverged from sidebar width at frame ${index}`);
		assert.ok(Math.abs(frame.contentLeft - frame.sidebarRight) <= 1.5, `content overlaps or detaches from the sidebar at frame ${index}`);
		assert.ok(Math.abs(frame.toggle.iconLeft - initial.toggle.iconLeft) <= 1 && Math.abs(frame.toggle.top - initial.toggle.top) <= 1,
			`toggle icon moved at frame ${index}: ${initial.toggle.iconLeft} -> ${frame.toggle.iconLeft}, top ${initial.toggle.top} -> ${frame.toggle.top}`);
	}
});

test("sidebar desktop layout remains stable across size, theme and rail states @sidebar", async ({ page }) => {
	await matrix(page);
});

test("sidebar keyboard focus and group controls remain operable @sidebar", async ({ page }) => {
	await focusRing(page);
});

test("mobile navigation drawer remains reachable and correctly labelled @sidebar", async ({ page }) => {
	await mobile(page);
});
