import { test } from "./fixtures.mjs";
const VPS = [[1280, 720], [1024, 768], [390, 844], [320, 640]];
test("edit probe", async ({ browser, app, adminState }) => {
	test.setTimeout(170000);
	for (const [w, h] of VPS) {
		const context = await browser.newContext({ viewport: { width: w, height: h }, storageState: adminState });
		const page = await context.newPage();
		await page.goto(app.base + "/ui/targets/1/edit", { waitUntil: "load" });
		await page.waitForTimeout(800);
		for (const tab of ["general", "http", "network", "alerts"]) {
			await page.click(`#target-${tab}-tab`);
			await page.waitForTimeout(500);
			// expand every disclosure of the active pane
			await page.evaluate(() => {
				document.querySelectorAll(".tab-pane.active details").forEach((d) => { d.open = true; });
				document.querySelectorAll(".tab-pane.active .collapse:not(.show)").forEach((c) => c.classList.add("show"));
				document.querySelectorAll(".tab-pane.active .check-card input[role=switch]").forEach((i) => { if (!i.checked) i.click(); });
			});
			await page.waitForTimeout(400);
			const m = await page.evaluate(() => {
				const r = (e) => e && Math.round(e.getBoundingClientRect().bottom);
				const main = document.querySelector("main") || document.getElementById("main-content");
				const pane = document.querySelector(".tab-pane.active");
				const footer = document.querySelector(".ju-footer, footer");
				const de = document.documentElement;
				return { vh: innerHeight, docScroll: de.scrollHeight, docClient: de.clientHeight, bodyScroll: document.body.scrollHeight,
					mainScroll: main && main.scrollHeight, mainClient: main && main.clientHeight, mainOverflowY: main && getComputedStyle(main).overflowY,
					paneBottom: r(pane), paneScroll: pane.scrollHeight, paneClient: pane.clientHeight, paneOverflowY: getComputedStyle(pane).overflowY,
					footerBottom: r(footer), footerTop: footer && Math.round(footer.getBoundingClientRect().top), mainBottom: r(main), shell: r(document.querySelector(".ju-shell")),
					lastEl: (() => { let max = 0; document.querySelectorAll(".tab-pane.active *").forEach((e) => { const b = e.getBoundingClientRect().bottom; if (b > max) max = b; }); return Math.round(max); })() };
			});
			console.log(`M ${w}x${h} ${tab} ` + JSON.stringify(m));
		}
		await context.close();
	}
});
