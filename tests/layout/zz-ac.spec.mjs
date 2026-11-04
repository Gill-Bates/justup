import fs from "node:fs";
import { test } from "./fixtures.mjs";
test("dump", async ({ browser, app, adminState }) => {
	const context = await browser.newContext({ viewport: { width: 1280, height: 900 }, storageState: adminState });
	const page = await context.newPage();
	await page.goto(app.base + "/ui/targets/4", { waitUntil: "load" });
	await page.waitForFunction(() => { const c = document.getElementById("chart-uptime"); const ch = c && window.Chart?.getChart(c); return ch && ch.data.datasets.some((d) => d.data.length) && !/Loading/i.test(c.getAttribute("aria-label")); }, null, { timeout: 30000 });
	await page.waitForTimeout(1500);
	const url = await page.evaluate(() => { const c = document.getElementById("chart-uptime"); const ch = window.Chart.getChart(c); ch.options.plugins.legend.display = false; ch.update("none"); return [c.toDataURL("image/png"), c.width, c.height, JSON.stringify(ch.chartArea)]; });
	fs.writeFileSync("/tmp/claude-0/-opt-justup/20ea6160-4f71-461b-8bd1-5466bb813680/scratchpad/avail.png", Buffer.from(url[0].split(",")[1], "base64"));
	console.log("DIM " + url[1] + "x" + url[2] + " " + url[3]);
	await context.close();
});
