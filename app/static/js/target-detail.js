//
// app/static/js/target-detail.js
// Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
//

/**
 * Main orchestrator for the target-detail page.
 *
 * Owns appState and wires together all sub-modules.
 * This is the ONLY file that reads from the #target-config data-attributes.
 *
 * Depends on (load order):
 *   1. utils/throttle.js
 *   2. utils/with-alpha.js
 *   3. charts/chart-config.js
 *   4. charts/chart-sampling.js
 *   5. charts/chart-builder.js
 *   6. charts/chart-theme.js
 *   7. target-detail/date-range.js
 *   8. target-detail/metrics.js
 *   9. target-detail/pdf-export.js
 *  10. target-detail/test-alert.js
 *
 * @module target-detail
 */

// ═══════════════════════════════════════════════════════════════════════════════
// APPLICATION STATE
// ═══════════════════════════════════════════════════════════════════════════════

const appState = {
	range: { from: null, to: null },
	charts: new Map(),
	theme: null,
	metricsCache: new Map(),
	currentLoadToken: null,
	themeObserver: null,
};

// ═══════════════════════════════════════════════════════════════════════════════
// PAGE CONFIGURATION (read once from #target-config data-attributes)
// ═══════════════════════════════════════════════════════════════════════════════

let pageConfig = {
	targetId: null,
	useUtc: false,
	pdfPageSize: 'a4',
	tcpPorts: [],
	slaConfig: { enabled: false, availabilityPct: 99.9, responseTimeMs: 500, pingLatencyMs: 100 },
	// Feature flags (set by Jinja2 data-attributes)
	enableHttp: false,
	enablePing: false,
	enableTcp: false,
	enableCert: false,
};

/**
 * Read page configuration from the #target-config element's data-attributes.
 * Must be called once during DOMContentLoaded.
 */
function _readPageConfig() {
	const el = document.getElementById('target-config');
	if (!el) {
		console.error('target-detail.js: #target-config element not found');
		return;
	}

	const d = el.dataset;

	pageConfig.targetId = parseInt(d.targetId) || null;
	pageConfig.useUtc = d.useUtc === 'true';
	pageConfig.pdfPageSize = d.pdfPageSize || 'a4';
	pageConfig.tcpPorts = (d.tcpPorts || '80').split(',').map(p => p.trim()).filter(Boolean);

	pageConfig.slaConfig = {
		enabled: d.slaEnabled === 'true',
		availabilityPct: parseFloat(d.slaAvailability) || 99.9,
		responseTimeMs: parseFloat(d.slaResponseTime) || 500,
		pingLatencyMs: parseFloat(d.slaPingLatency) || 100,
	};

	pageConfig.enableHttp = d.enableHttp === 'true';
	pageConfig.enablePing = d.enablePing === 'true';
	pageConfig.enableTcp = d.enableTcp === 'true';
	pageConfig.enableCert = d.enableCert === 'true';
}

// ═══════════════════════════════════════════════════════════════════════════════
// CHART LIFECYCLE
// ═══════════════════════════════════════════════════════════════════════════════

/**
 * Destroy all Chart.js instances and clear the chart registry.
 */
function destroyAllCharts() {
	appState.charts.forEach((chart, key) => {
		try {
			chart.destroy();
		} catch (e) {
			console.warn(`Failed to destroy chart "${key}":`, e);
		}
	});
	appState.charts.clear();
}

/**
 * Load all charts for the current date range.
 *
 * Uses token-based race-condition guard: if the user triggers a new load
 * while the previous one is still in flight, the stale responses are discarded.
 */
async function loadCharts() {
	const tid = pageConfig.targetId;
	if (!tid) return;

	// Token-based guard (replaces isLoading flag)
	const loadToken = Symbol('loadCharts');
	appState.currentLoadToken = loadToken;
	appState.metricsCache.clear();

	const btn = document.getElementById('load-range-btn');
	if (btn) {
		btn.disabled = true;
		btn.innerHTML = '<span class="spinner-border spinner-border-sm"></span>';
	}

	const isStale = () => appState.currentLoadToken !== loadToken;

	// Guarded fetch shorthand (returns null when stale)
	const fetch = (metric) => fetchMetricGuarded(tid, metric, loadToken);

	try {
		// ── Kick off all fetches in parallel ──────────────────────────
		const fetches = {};

		if (pageConfig.enableHttp) {
			fetches.httpUp = fetch('http_up');
			fetches.httpMs = fetch('http_ms');
			fetches.httpStatus = fetch('http_status');
		}

		if (pageConfig.enablePing) {
			fetches.pingUp = fetch('ping_up');
			fetches.pingMs = fetch('ping_ms');
		}

		if (pageConfig.enableTcp) {
			fetches.tcpUp = fetch('tcp_up');
		}

		if (pageConfig.enableCert) {
			fetches.certDays = fetch('cert_days_left');
		}

		// ── Await results and build charts ────────────────────────────

		// Availability chart (per-service, not aggregated)
		const availabilityData = await fetchAvailabilitySeries(tid, loadToken);
		if (isStale()) return;
		if (availabilityData) {
			buildAvailabilitySeriesChart('chart-uptime', availabilityData);
		}

		// HTTP: Response time + Status codes
		if (pageConfig.enableHttp) {
			if (fetches.httpMs) {
				const httpData = await fetches.httpMs;
				if (isStale()) return;
				if (httpData) {
					const sla = createSlaAnnotation(
						pageConfig.slaConfig,
						pageConfig.slaConfig.responseTimeMs,
						`${pageConfig.slaConfig.responseTimeMs} ms`
					);
					buildChart('chart-response-time', httpData, COLOR_RESPONSE_TIME, sla);
				}
			}

			if (fetches.httpStatus) {
				const statusData = await fetches.httpStatus;
				if (isStale()) return;
				if (statusData) buildStatusCodeChart('chart-status-code', statusData);
			}
		}

		// Ping: Latency chart
		if (pageConfig.enablePing && fetches.pingMs) {
			const pingData = await fetches.pingMs;
			if (isStale()) return;
			if (pingData) {
				const sla = createSlaAnnotation(
					pageConfig.slaConfig,
					pageConfig.slaConfig.pingLatencyMs,
					`${pageConfig.slaConfig.pingLatencyMs} ms`
				);
				buildChart('chart-ping', pingData, COLOR_RESPONSE_TIME, sla);
			}
		}

		// TCP: Multi-port or single
		if (pageConfig.enableTcp) {
			if (pageConfig.tcpPorts.length > 1) {
				await buildMultiPortTcpChart(
					'chart-tcp',
					pageConfig.tcpPorts,
					(metric) => fetchMetricGuarded(tid, metric, loadToken),
					loadToken
				);
				if (isStale()) return;
			} else {
				const tcpData = await fetch('tcp_ms');
				if (isStale()) return;
				if (tcpData) buildChart('chart-tcp', tcpData, '#6c757d');
			}
		}

		// Certificate days-left (KPI update, no chart)
		if (pageConfig.enableCert && fetches.certDays) {
			const certData = await fetches.certDays;
			if (isStale()) return;
			if (certData?.points?.length > 0) {
				const latest = Math.floor(certData.points[certData.points.length - 1].value);
				const certEl = document.getElementById('cert-days-value');
				if (certEl) certEl.textContent = latest + ' days';
			}
		}

		// Downtime history (HTMX fragment)
		if (!isStale()) {
			_reloadDowntimeHistory();
		}

	} finally {
		// Only reset button if we are still the active load
		if (!isStale()) {
			if (btn) {
				btn.disabled = false;
				btn.innerHTML = '<span class="material-icons">refresh</span> Load';
			}
		}
	}
}

// ═══════════════════════════════════════════════════════════════════════════════
// DOWNTIME HISTORY (HTMX FRAGMENT)
// ═══════════════════════════════════════════════════════════════════════════════

function _reloadDowntimeHistory() {
	const bodyEl = document.getElementById('downtime-history-body');
	if (!bodyEl || !pageConfig.targetId) return;

	const since = appState.range.from?.toISOString() || '';
	const until = appState.range.to?.toISOString() || '';

	bodyEl.innerHTML =
		'<div class="text-center py-4">' +
		'<div class="spinner-border spinner-border-sm text-primary"></div>' +
		'</div>';

	if (typeof htmx !== 'undefined') {
		htmx.ajax('GET',
			`/ui/fragments/downtime-history/${pageConfig.targetId}?since=${since}&until=${until}`,
			{ target: '#downtime-history-body', swap: 'innerHTML' }
		);
	}
}

// ═══════════════════════════════════════════════════════════════════════════════
// ZOOM RESET
// ═══════════════════════════════════════════════════════════════════════════════

function resetAllZoom() {
	appState.charts.forEach(ch => {
		if (ch && ch.resetZoom) {
			ch.resetZoom();
			setTimeout(() => updateChartSampling(ch), 0);
		}
	});
}

// ═══════════════════════════════════════════════════════════════════════════════
// CLEANUP
// ═══════════════════════════════════════════════════════════════════════════════

/** @type {{cleanup: Function, setSafetyTimeout: Function}|null} PDF listener and timeout manager */
let _pdfCleanup = null;

/**
 * Full page cleanup: destroy charts, disconnect observers, remove listeners.
 * Safe to call multiple times.
 */
function _cleanup() {
	// Clean up chart sampling listeners first
	window._chartSamplingCleanup?.();
	
	destroyAllCharts();
	appState.themeObserver?.disconnect();
	appState.themeObserver = null;
	if (_pdfCleanup) {
		_pdfCleanup.cleanup();
		_pdfCleanup = null;
	}
}

// ═══════════════════════════════════════════════════════════════════════════════
// INITIALIZATION
// ═══════════════════════════════════════════════════════════════════════════════

document.addEventListener('DOMContentLoaded', () => {

	// 1. Read config from data-attributes
	_readPageConfig();

	if (!pageConfig.targetId) {
		console.error('target-detail.js: no targetId configured');
		return;
	}

	// 2. Initialize theme
	appState.theme = getChartTheme();
	if (window.Chart) {
		Chart.defaults.color = appState.theme.text;
		Chart.defaults.borderColor = appState.theme.grid;
		Chart.defaults.font.family = "'Roboto', 'Helvetica Neue', Arial, sans-serif";
	}

	// 3. Initialize chart sampling (resize listener, adaptive decimation)
	window._chartSamplingInit?.();

	// 4. Initialize date range (last 24h)
	initDateInputs();

	// 5. Load charts
	loadCharts();

	// 6. Theme observer (auto-update on dark/light switch)
	initChartThemeObserver(200);

	// 7. PDF export
	_pdfCleanup = initPdfJobListener(pageConfig.targetId);
	checkPendingPdfJob(pageConfig.targetId);

	// ── Event listeners ──────────────────────────────────────────────

	// Quick-range dropdown
	const quickRange = document.getElementById('quick-range');
	if (quickRange) {
		quickRange.addEventListener('change', function () {
			const range = applyQuickRange(this.value);
			if (range) loadCharts();
		});
	}

	// Manual "Load" button
	const loadBtn = document.getElementById('load-range-btn');
	if (loadBtn) {
		loadBtn.addEventListener('click', () => {
			const range = readAndValidateDateInputs();
			if (!range) return;
			appState.range.from = range.from;
			appState.range.to = range.to;
			const qr = document.getElementById('quick-range');
			if (qr) qr.value = '';
			loadCharts();
		});
	}

	// Reset zoom
	const resetBtn = document.getElementById('reset-zoom-btn');
	if (resetBtn) {
		resetBtn.addEventListener('click', resetAllZoom);
	}

	// Clear quick-range selection when manual inputs change
	const fromInput = document.getElementById('range-from');
	const toInput = document.getElementById('range-to');
	if (fromInput) {
		fromInput.addEventListener('change', () => {
			const qr = document.getElementById('quick-range');
			if (qr) qr.value = '';
		});
	}
	if (toInput) {
		toInput.addEventListener('change', () => {
			const qr = document.getElementById('quick-range');
			if (qr) qr.value = '';
		});
	}

	// PDF export button
	const pdfBtn = document.getElementById('export-pdf-btn');
	if (pdfBtn) {
		pdfBtn.addEventListener('click', async () => {
			const safetyTimeout = await downloadPdfReport(pageConfig.targetId, pageConfig.pdfPageSize);
			// Integrate safety timeout with PDF job listener cleanup
			if (safetyTimeout && _pdfCleanup) {
				_pdfCleanup.setSafetyTimeout(safetyTimeout);
			}
		});
	}

	// Test alert button
	const testBtn = document.getElementById('test-alert-btn');
	if (testBtn) {
		testBtn.addEventListener('click', () => {
			sendTestAlert(pageConfig.targetId);
		});
	}

	// Auto-close alert toggle
	const autoCloseToggle = document.getElementById('auto-close-alert-toggle');
	if (autoCloseToggle) {
		autoCloseToggle.addEventListener('change', async () => {
			try {
				const res = await fetch(`/targets/${pageConfig.targetId}`, {
					method: 'PATCH',
					headers: { 'Content-Type': 'application/json' },
					body: JSON.stringify({ auto_close_alert: autoCloseToggle.checked }),
				});
				if (!res.ok) {
					autoCloseToggle.checked = !autoCloseToggle.checked;
					console.error('Failed to update auto_close_alert:', res.status);
				}
			} catch (err) {
				autoCloseToggle.checked = !autoCloseToggle.checked;
				console.error('Failed to update auto_close_alert:', err);
			}
		});
	}

	// ── Cleanup ──────────────────────────────────────────────────────

	window.addEventListener('beforeunload', _cleanup);

	document.body.addEventListener('htmx:beforeSwap', function cleanupOnSwap(evt) {
		// Only clean up on full-page HTMX navigation swaps (target = body),
		// NOT on inner fragment swaps (e.g. downtime-history, traceroute).
		// Inner swaps must not destroy charts built by loadCharts().
		const swapTarget = evt.detail.target;
		if (swapTarget !== document.body && swapTarget !== document.documentElement) {
			return;
		}
		_cleanup();
		document.body.removeEventListener('htmx:beforeSwap', cleanupOnSwap);
	});
});
