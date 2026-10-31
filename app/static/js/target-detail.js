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
 *  11. target-detail/auto-close.js
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
	loadInFlight: false,
	loadAbort: null,
	certDays: null,
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

function _setupNetworkDiagnostics() {
	const diagnosticsTab = document.getElementById('target-network-diagnostics-tab');
	const dashboardTab = document.getElementById('target-dashboard-tab');
	const container = document.getElementById('traceroute-container');
	if (!diagnosticsTab || !dashboardTab || !container || diagnosticsTab.dataset.listenerAttached) return;
	diagnosticsTab.dataset.listenerAttached = 'true';

	const runAgain = document.getElementById('traceroute-run-again');
	const runAgainIcon = document.getElementById('traceroute-run-again-icon');
	const runAgainSpinner = document.getElementById('traceroute-run-again-spinner');
	const refreshStatus = document.getElementById('traceroute-refresh-status');
	let statusTimer = null;

	const showLoading = () => {
		const status = document.createElement('div');
		status.className = 'text-center py-4';
		status.setAttribute('role', 'status');
		status.setAttribute('aria-live', 'polite');
		status.textContent = 'Loading network diagnostics…';
		container.replaceChildren(status);
	};

	const setRefreshStatus = (text, state, autoClear) => {
		if (!refreshStatus) return;
		clearTimeout(statusTimer);
		refreshStatus.textContent = text || '';
		refreshStatus.dataset.state = state || '';
		refreshStatus.hidden = !text;
		if (text && autoClear) statusTimer = setTimeout(() => setRefreshStatus('', ''), 10000);
	};

	const setRunAgainBusy = (busy) => {
		if (!runAgain) return;
		runAgain.setAttribute('aria-busy', busy ? 'true' : 'false');
		runAgain.setAttribute('aria-disabled', busy ? 'true' : 'false');
		runAgainSpinner?.classList.toggle('d-none', !busy);
		runAgainIcon?.classList.toggle('d-none', busy);
	};

	// Server-rendered, autoescaped fragment: parse inertly and replace the old maps and rows.
	const swapFragment = (html) => {
		window.TracerouteMap?.cleanupAll?.(container);
		const doc = new DOMParser().parseFromString(html, 'text/html');
		container.replaceChildren(...doc.body.childNodes);
		container.dataset.loaded = 'true';
		if (window.TracerouteMap?.initAllMaps) {
			window.TracerouteMap.initAllMaps(container);
			requestAnimationFrame(() => container.querySelectorAll('.traceroute-map-container').forEach(map => {
				if (map._leaflet_map) map._leaflet_map.invalidateSize();
			}));
		}
	};

	// Returns the fragment HTML, or null when the session expired and a redirect was started.
	const fetchFragment = async (refresh) => {
		const url = refresh ? `${container.dataset.url}?refresh=1` : container.dataset.url;
		const response = await fetch(url, { headers: { 'HX-Request': 'true' } });
		const redirect = response.headers.get('HX-Redirect');
		if (redirect && redirect.startsWith('/') && !redirect.startsWith('//')) {
			window.location.assign(redirect);
			return null;
		}
		if (response.status === 429) {
			throw new Error('A measurement was just started. Please wait a few seconds and try again.');
		}
		if (!response.ok) throw new Error(`Network diagnostics could not be loaded (${response.status}).`);
		return response.text();
	};

	const loadDiagnostics = async () => {
		if (container.dataset.loaded === 'true' || container.dataset.loading === 'true') return;
		container.dataset.loading = 'true';
		showLoading();
		try {
			const html = await fetchFragment(false);
			if (html !== null) swapFragment(html);
		} catch (error) {
			const message = document.createElement('div');
			message.className = 'alert alert-warning mb-0';
			message.setAttribute('role', 'alert');
			message.textContent = error instanceof Error ? error.message : 'Network diagnostics could not be loaded.';
			container.replaceChildren(message);
		} finally {
			delete container.dataset.loading;
		}
	};

	// Explicit "Run again": a forced measurement; the previous result stays visible on failure.
	const runMeasurementAgain = async () => {
		if (!runAgain || runAgain.getAttribute('aria-busy') === 'true' || container.dataset.loading === 'true') return;
		container.dataset.loading = 'true';
		setRunAgainBusy(true);
		container.setAttribute('aria-busy', 'true');
		setRefreshStatus('Running a new measurement…', '');
		try {
			const html = await fetchFragment(true);
			if (html === null) return;
			swapFragment(html);
			setRefreshStatus(`Measurement updated at ${new Date().toLocaleTimeString()}`, 'ok', true);
		} catch (error) {
			setRefreshStatus(error instanceof Error ? error.message : 'The measurement failed.', 'error');
		} finally {
			delete container.dataset.loading;
			container.removeAttribute('aria-busy');
			setRunAgainBusy(false);
		}
	};

	const showRunAgain = (visible) => {
		if (runAgain) runAgain.hidden = !visible;
		if (!visible) setRefreshStatus('', '');
	};

	runAgain?.addEventListener('click', runMeasurementAgain);
	diagnosticsTab.addEventListener('shown.bs.tab', () => {
		updateTargetViewHash('network-diagnostics');
		showRunAgain(true);
		loadDiagnostics();
	});
	dashboardTab.addEventListener('shown.bs.tab', () => {
		updateTargetViewHash('');
		showRunAgain(false);
		requestAnimationFrame(() => appState.charts.forEach(chart => chart.resize()));
	});

	if (window.location.hash === '#network-diagnostics' && window.bootstrap?.Tab) {
		window.bootstrap.Tab.getOrCreateInstance(diagnosticsTab).show();
	}

	if (diagnosticsTab.getAttribute('aria-selected') === 'true') {
		showRunAgain(true);
		loadDiagnostics();
	}
}

function updateTargetViewHash(view) {
	const url = new URL(window.location.href);
	url.hash = view ? `#${view}` : '';
	window.history.replaceState(null, '', url);
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

const LOADING_PANEL_IDS = ['combined-card', 'availability-card', 'downtime-history-card'];

/** Toggle the loading overlay on the chart panels (styled via [aria-busy] in target-detail.css). */
function _setPanelsBusy(busy) {
	LOADING_PANEL_IDS.forEach((id) => {
		const panel = document.getElementById(id);
		if (!panel) return;
		if (busy) panel.setAttribute('aria-busy', 'true');
		else panel.removeAttribute('aria-busy');
	});
}

/** Show a designed empty/error message instead of the availability chart. */
function _showAvailabilityMessage(message) {
	const plot = document.getElementById('availability-plot');
	const empty = document.getElementById('availability-empty');
	if (!plot || !empty) return;
	plot.hidden = true;
	empty.hidden = false;
	const msg = empty.querySelector('[data-role="message"]');
	if (msg) msg.textContent = message;
}

/** Skeleton markup shared by the initial and reloaded downtime panel. */
function _downtimeSkeleton() {
	const wrap = document.createElement('div');
	wrap.className = 'target-skeleton';
	wrap.setAttribute('role', 'status');
	for (let i = 0; i < 3; i++) {
		const line = document.createElement('span');
		line.className = 'target-skeleton-line';
		wrap.appendChild(line);
	}
	const hidden = document.createElement('span');
	hidden.className = 'visually-hidden';
	hidden.textContent = 'Loading downtimes…';
	wrap.appendChild(hidden);
	return wrap;
}

/** Replace the downtime panel body with a designed error state. */
function _showDowntimeError() {
	const bodyEl = document.getElementById('downtime-history-body');
	if (!bodyEl) return;
	const box = document.createElement('div');
	box.className = 'target-empty-state';
	box.setAttribute('role', 'alert');
	const icon = document.createElement('span');
	icon.className = 'target-empty-icon target-empty-icon--error';
	icon.innerHTML = '<i class="material-icons" aria-hidden="true">error_outline</i>';
	const title = document.createElement('p');
	title.className = 'target-empty-title';
	title.textContent = 'Downtimes could not be loaded';
	const text = document.createElement('p');
	text.className = 'target-empty-text';
	text.textContent = 'Try another range or reload the page.';
	box.append(icon, title, text);
	bodyEl.replaceChildren(box);
}

/**
 * Load all charts for the current date range.
 *
 * Uses token-based race-condition guard: if the user triggers a new load
 * while the previous one is still in flight, the stale responses are discarded.
 * `silent` (live refresh) skips the loading indicators and the downtime skeleton.
 */
async function loadCharts({ silent = false } = {}) {
	const tid = pageConfig.targetId;
	if (!tid) return;

	// Token-based guard (replaces isLoading flag)
	const loadToken = Symbol('loadCharts');
	appState.currentLoadToken = loadToken;
	// One controller per cycle: cancel what the previous cycle still has in flight.
	appState.loadAbort?.abort();
	const abortController = new AbortController();
	appState.loadAbort = abortController;
	const { signal } = abortController;
	appState.metricsCache.clear();
	appState.loadInFlight = true;

	const btn = silent ? null : document.getElementById('load-range-btn');
	if (btn) {
		btn.disabled = true;
		btn.innerHTML = '<span class="spinner-border spinner-border-sm"></span>';
	}

	const isStale = () => appState.currentLoadToken !== loadToken;
	if (!silent) _setPanelsBusy(true);

	// Guarded fetch shorthand (returns null when stale)
	const fetch = (metric) => fetchMetricGuarded(tid, metric, loadToken, signal);

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
			if (pageConfig.tcpPorts.length > 1) {
				fetches.tcpPorts = pageConfig.tcpPorts.map((port) => fetch(`tcp_ms_${port}`));
			} else {
				fetches.tcpMs = fetch('tcp_ms');
			}
		}

		if (pageConfig.enableCert) {
			fetches.certDays = fetch('cert_days_left');
		}

		// ── Await results and build charts ────────────────────────────

		// Combined response time + status code + downtime chart (reuses the in-flight fetches)
		const enabledLatency = CombinedChartData.enabledSources(pageConfig);
		if (enabledLatency.length) {
			const primary = enabledLatency[0];
			const upFetches = { http: fetches.httpUp, ping: fetches.pingUp, tcp: fetches.tcpUp };
			const [httpMs, pingMs, tcpMs, tcpPorts, statusData, upData] = await Promise.all([
				fetches.httpMs,
				fetches.pingMs,
				fetches.tcpMs,
				fetches.tcpPorts ? Promise.all(fetches.tcpPorts) : null,
				fetches.httpStatus || Promise.resolve({ points: [] }),
				upFetches[primary.id],
			]);
			if (isStale()) return;
			let aggregate = tcpMs;
			// Per-port series without any data fall back to the aggregate tcp_ms metric.
			if (tcpPorts && !tcpPorts.some((r) => r?.points?.length)) {
				aggregate = await fetch('tcp_ms');
				if (isStale()) return;
			}
			buildCombinedChart('chart-combined', {
				sources: CombinedChartData.buildLatencySources(
					pageConfig,
					pageConfig.tcpPorts,
					{ http: httpMs, ping: pingMs, tcp: aggregate, tcpPorts: tcpPorts },
					pageConfig.slaConfig
				),
				statusPoints: statusData?.points || [],
				statusBucketSeconds: statusData?.bucket_seconds,
				statusRawOnly: !!statusData?.raw_status_only,
				upPoints: upData?.points || [],
				upBucketSeconds: upData?.bucket_seconds,
				upSourceLabel: primary.id.toUpperCase(),
				statusEnabled: pageConfig.enableHttp,
				statusError: !!statusData?.error,
				upError: !!upData?.error,
				range: appState.range,
			});
			if (pageConfig.enableHttp) buildStatusCodeChart('chart-status-code', statusData || { points: [] });
		}

		// Availability chart (per-service, not aggregated)
		const availabilityData = await fetchAvailabilitySeries(tid, loadToken, signal);
		if (isStale()) return;
		if (availabilityData) {
			buildAvailabilitySeriesChart('chart-uptime', availabilityData);
		} else {
			_showAvailabilityMessage('Availability data could not be loaded. Try another range or reload the page.');
		}

		// Certificate days-left (KPI update, no chart)
		if (pageConfig.enableCert && fetches.certDays) {
			const certData = await fetches.certDays;
			if (isStale()) return;
			if (certData?.points?.length > 0) {
				const latest = Math.floor(certData.points[certData.points.length - 1].value);
				appState.certDays = latest;
				const certEl = document.getElementById('cert-days-value');
				if (certEl) certEl.textContent = latest + ' days';
				const certHint = document.getElementById('cert-days-hint');
				if (certHint) certHint.hidden = true;
			}
		}

		// Downtime history (HTMX fragment)
		if (!isStale()) {
			_reloadDowntimeHistory({ skeleton: !silent });
		}

	} finally {
		// Only reset button if we are still the active load
		if (!isStale()) {
			appState.loadInFlight = false;
			_setPanelsBusy(false);
			if (_livePending) _scheduleLiveRefresh();
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

function _reloadDowntimeHistory({ skeleton = true } = {}) {
	const bodyEl = document.getElementById('downtime-history-body');
	if (!bodyEl || !pageConfig.targetId) return;

	const since = appState.range.from?.toISOString() || '';
	const until = appState.range.to?.toISOString() || '';

	if (skeleton) bodyEl.replaceChildren(_downtimeSkeleton());

	if (typeof htmx !== 'undefined') {
		htmx.ajax('GET',
			`/ui/fragments/downtime-history/${pageConfig.targetId}?since=${since}&until=${until}`,
			{ target: '#downtime-history-body', swap: 'innerHTML' }
		);
	}
}

// ═══════════════════════════════════════════════════════════════════════════════
// LIVE REFRESH (server-sent invalidation events, see live-events.js)
// ═══════════════════════════════════════════════════════════════════════════════

const LIVE_MIN_INTERVAL_MS = 10000;
let _liveUnsubscribe = null;
let _liveTimer = null;
let _liveLastRun = 0;
let _livePending = false;

/** True while the user zooms/pans a chart or edits the date inputs; a rebuild would destroy that work. */
function _userIsInteracting() {
	const zoomed = Array.from(appState.charts.values()).some((ch) => ch?.isZoomedOrPanned?.());
	const active = document.activeElement;
	return zoomed || !!(active && (active.id === 'range-from' || active.id === 'range-to'));
}

/**
 * Reload the charts for the selected range. Rolling and calendar presets are recomputed so they
 * advance with time; a manually chosen range (empty preset) is left exactly as the user set it.
 */
function _runLiveRefresh() {
	_liveTimer = null;
	if (document.hidden) return;
	const preset = document.getElementById('quick-range')?.value || '';
	if (!preset) return;
	if (appState.loadInFlight || _userIsInteracting()) {
		_livePending = true;
		return;
	}
	_livePending = false;
	_liveLastRun = Date.now();
	if (applyQuickRange(preset)) loadCharts({ silent: true });
}

/** Coalesce bursts: at most one chart reload per LIVE_MIN_INTERVAL_MS, never a parallel one. */
function _scheduleLiveRefresh() {
	if (_liveTimer !== null) return;
	const wait = Math.max(0, LIVE_MIN_INTERVAL_MS - (Date.now() - _liveLastRun));
	_liveTimer = setTimeout(_runLiveRefresh, wait);
}

function _startLiveRefresh() {
	const live = window.JustUpLive?.client;
	if (!live || _liveUnsubscribe) return;
	const mine = (events) => events.some((e) => Number(e.data?.target_id) === Number(pageConfig.targetId));
	_liveUnsubscribe = live.subscribe(
		['target.measurement'],
		(events) => { if (mine(events)) _scheduleLiveRefresh(); },
		{ resync: _scheduleLiveRefresh, coalesceMs: 1000 }
	);
}

function _stopLiveRefresh() {
	_liveUnsubscribe?.();
	_liveUnsubscribe = null;
	if (_liveTimer !== null) clearTimeout(_liveTimer);
	_liveTimer = null;
}

// ═══════════════════════════════════════════════════════════════════════════════
// ZOOM RESET
// ═══════════════════════════════════════════════════════════════════════════════

function resetAllZoom() {
	appState.charts.forEach(ch => {
		if (ch && ch.resetZoom) {
			ch.resetZoom();
			if (ch._reloadVisible) ch._reloadVisible();
			setTimeout(() => updateChartSampling(ch), 0);
		}
	});
	if (_livePending) _scheduleLiveRefresh();
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
	_stopLiveRefresh();
	appState.loadAbort?.abort();

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
	_setupNetworkDiagnostics();

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
		Chart.defaults.font.family = "'IBM Plex Sans', sans-serif";
	}

	// 3. Initialize chart sampling (resize listener, adaptive decimation)
	window._chartSamplingInit?.();

	// 4. Initialize date range (last 24h)
	initDateInputs();

	// 5. Load charts
	loadCharts();
	_startLiveRefresh();

	// The KPI cards are swapped by HTMX on live events; keep the certificate value the charts computed.
	document.getElementById('target-info-cards')?.addEventListener('htmx:afterSwap', () => {
		if (appState.certDays === null) return;
		const certEl = document.getElementById('cert-days-value');
		if (certEl) certEl.textContent = appState.certDays + ' days';
		const certHint = document.getElementById('cert-days-hint');
		if (certHint) certHint.hidden = true;
	});

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
			const customDates = document.getElementById('target-custom-range');
			if (customDates) customDates.open = this.value === '';
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

	// Downtime fragment failures get a designed error state instead of a stuck skeleton.
	document.body.addEventListener('htmx:responseError', (evt) => {
		if (evt.detail?.target?.id === 'downtime-history-body') _showDowntimeError();
	});
	document.body.addEventListener('htmx:sendError', (evt) => {
		if (evt.detail?.target?.id === 'downtime-history-body') _showDowntimeError();
	});

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
		autoCloseToggle.addEventListener('change', () => saveAutoCloseAlert(autoCloseToggle, pageConfig.targetId));
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
