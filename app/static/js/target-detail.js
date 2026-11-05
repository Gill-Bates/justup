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
 *   5. charts/chart-reset.js
 *   6. charts/chart-builder.js
 *   7. charts/combined-chart-data.js
 *   8. charts/combined-chart.js
 *   9. charts/chart-theme.js
 *  10. target-detail/date-range.js
 *  11. target-detail/metrics.js
 *  12. target-detail/pdf-export.js
 *  13. target-detail/test-alert.js
 *  14. target-detail/auto-close.js
 *  (base.html, before app.js: target-detail/traceroute-cache.js -> window.TracerouteCache)
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
	rawRetentionDays: 30,
	tcpPorts: [],
	slaConfig: { enabled: false, availabilityPct: 99.9, responseTimeMs: 500, pingLatencyMs: 100 },
	// Feature flags (set by Jinja2 data-attributes)
	enableHttp: false,
	enablePing: false,
	enableTcp: false,
	enableCert: false,
};
let activeDowntimeQuery = '';
let downtimeHistorySwapGuard = null;

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
	pageConfig.rawRetentionDays = parseInt(d.rawRetentionDays, 10) || pageConfig.rawRetentionDays;
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
	let diagnosticsActive = false;
	let baseline = { kind: 'none' }; // persistent line: last scan time or the outdated notice
	let transient = null; // short-lived line: running / error
	const formatScanTime = (ms) => {
		const options = { day: 'numeric', month: 'short', year: 'numeric', hour: '2-digit', minute: '2-digit', hour12: false };
		if (pageConfig.useUtc) options.timeZone = 'UTC';
		return new Date(ms).toLocaleString(navigator.language || 'de-DE', options);
	};
	const paintStatus = () => {
		if (!refreshStatus) return;
		let text = '';
		let state = '';
		if (diagnosticsActive && transient) {
			({ text, state } = transient);
		} else if (diagnosticsActive && baseline.kind === 'scanned') {
			text = `Last scanned: ${formatScanTime(baseline.measuredAt)}`;
		} else if (diagnosticsActive && baseline.kind === 'outdated') {
			text = 'Outdated – new scan failed';
			state = 'error';
		}
		refreshStatus.textContent = text;
		refreshStatus.dataset.state = state;
		refreshStatus.hidden = !text;
	};
	const setStatus = (status) => {
		if (status.kind === 'error') {
			transient = { text: status.message, state: 'error' };
		} else {
			transient = null;
			baseline = status;
		}
		paintStatus();
	};
	const setBusy = (busy) => {
		if (runAgain) {
			runAgain.setAttribute('aria-busy', busy ? 'true' : 'false');
			runAgain.setAttribute('aria-disabled', busy ? 'true' : 'false');
		}
		runAgainSpinner?.classList.toggle('d-none', !busy);
		runAgainIcon?.classList.toggle('d-none', busy);
		container.toggleAttribute('aria-busy', busy);
		if (busy) {
			transient = { text: 'Running a new measurement…', state: '' };
			paintStatus();
		}
	};
	const showLoading = () => {
		const status = document.createElement('div');
		status.className = 'text-center py-4';
		status.setAttribute('role', 'status');
		status.setAttribute('aria-live', 'polite');
		status.textContent = 'Loading network diagnostics…';
		container.replaceChildren(status);
	};
	const showLoadError = (text) => {
		const message = document.createElement('div');
		message.className = 'alert alert-warning mb-0';
		message.setAttribute('role', 'alert');
		message.textContent = text || 'Network diagnostics could not be loaded.';
		container.replaceChildren(message);
	};
	// Server-rendered, autoescaped fragment (live or from the browser cache): parse inertly,
	// drop any script element, then replace the old maps and rows.
	const swapFragment = (html) => {
		window.TracerouteMap?.cleanupAll?.(container);
		const doc = new DOMParser().parseFromString(html, 'text/html');
		doc.querySelectorAll('script').forEach(script => script.remove());
		container.replaceChildren(...doc.body.childNodes);
		container.dataset.loaded = 'true';
		if (window.TracerouteMap?.initAllMaps) {
			window.TracerouteMap.initAllMaps(container);
			requestAnimationFrame(() => container.querySelectorAll('.traceroute-map-container').forEach(map => {
				if (map._leaflet_map) map._leaflet_map.invalidateSize();
			}));
		}
	};
	const cacheApi = window.TracerouteCache;
	const store = cacheApi.createStore({ storage: cacheApi.resolveStorage(window) });
	store.purgeOtherUsers(container.dataset.cacheUser);
	store.prune();
	const loader = cacheApi.createLoader({
		url: container.dataset.url,
		scope: { user: container.dataset.cacheUser, target: container.dataset.cacheTarget, build: container.dataset.cacheBuild, geo: container.dataset.cacheGeo },
		store,
		fetchFn: (url, init) => fetch(url, init),
		locks: navigator.locks || null,
		isHidden: () => document.hidden,
		isActive: () => diagnosticsActive,
		render: ({ html }) => swapFragment(html),
		onRedirect: (url) => window.location.assign(url),
		setBusy,
		setStatus,
		setLoading: showLoading,
		setLoadError: showLoadError,
	});
	const showRunAgain = (visible) => {
		diagnosticsActive = visible;
		if (runAgain) runAgain.hidden = !visible;
		if (!visible) transient = null;
		paintStatus();
	};
	runAgain?.addEventListener('click', () => loader.refresh());
	// A result stored by another tab replaces waiting or outdated state without a request.
	window.addEventListener('storage', () => {
		if (diagnosticsActive && !document.hidden) loader.recheck();
	});
	document.addEventListener('visibilitychange', () => {
		if (!document.hidden && diagnosticsActive) loader.checkExpiry();
	});
	diagnosticsTab.addEventListener('shown.bs.tab', () => {
		updateTargetViewHash('network-diagnostics');
		showRunAgain(true);
		loader.show();
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
		loader.show();
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
	for (let i = 0; i < 2; i++) {
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

/** Replace the downtime panel body with a compact error state. */
function _showDowntimeError() {
	const bodyEl = document.getElementById('downtime-history-body');
	if (!bodyEl) return;
	const box = document.createElement('p');
	box.className = 'downtime-empty downtime-empty--error';
	box.setAttribute('role', 'alert');
	const title = document.createElement('strong');
	title.textContent = 'Downtimes could not be loaded';
	box.append(title, 'Try another range or reload the page.');
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

	// Token-based guard against overlapping loads
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
		btn.innerHTML = '<span class="spinner-border spinner-border-sm" aria-hidden="true"></span><span class="visually-hidden">Loading…</span>';
	}

	const isStale = () => appState.currentLoadToken !== loadToken;
	if (!silent) _setPanelsBusy(true);

	// Guarded fetch shorthand (returns null when stale)
	const fetch = (metric) => fetchMetricGuarded(tid, metric, loadToken, signal);

	try {
		// Kick off all fetches in parallel
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

		// Await results and build charts

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
			const rawDays = certData?.points?.at(-1)?.value;
			const days = rawDays == null ? NaN : Number(rawDays);
			// Missing data must not keep an older value alive for the HTMX KPI refresh
			appState.certDays = Number.isFinite(days) ? Math.floor(days) : null;
			if (appState.certDays !== null) {
				const latest = appState.certDays;
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
				btn.innerHTML = '<i class="material-icons" aria-hidden="true">refresh</i> Load';
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
		const requestUrl = `/ui/fragments/downtime-history/${pageConfig.targetId}?since=${since}&until=${until}`;
		activeDowntimeQuery = new URL(requestUrl, window.location.href).search;
		if (!downtimeHistorySwapGuard) {
			downtimeHistorySwapGuard = function (evt) {
				if (evt.detail?.target?.id !== 'downtime-history-body') return;
				const responseUrl = evt.detail.xhr?.responseURL;
				if (!responseUrl) return;
				if (new URL(responseUrl, window.location.href).search !== activeDowntimeQuery) {
					evt.detail.shouldSwap = false;
				}
			};
			document.body.addEventListener('htmx:beforeSwap', downtimeHistorySwapGuard);
		}
		htmx.ajax('GET',
			requestUrl,
			{ source: '#downtime-history-body', target: '#downtime-history-body', swap: 'innerHTML' }
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
		if (!ch) return;
		window.JuChartReset.resetChartView(ch);
		setTimeout(() => updateChartSampling(ch), 0);
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
	cleanupChartThemeObserver();
	if (_pdfCleanup) {
		_pdfCleanup.cleanup();
		_pdfCleanup = null;
	}
}

// ═══════════════════════════════════════════════════════════════════════════════
// INITIALIZATION
// ═══════════════════════════════════════════════════════════════════════════════

document.addEventListener('DOMContentLoaded', () => {
	// Config first: the cached diagnostics render reads pageConfig.useUtc.
	_readPageConfig();
	_setupNetworkDiagnostics();

	if (!pageConfig.targetId) {
		console.error('target-detail.js: no targetId configured');
		return;
	}

	appState.theme = getChartTheme();
	if (window.Chart) {
		Chart.defaults.color = appState.theme.text;
		Chart.defaults.borderColor = appState.theme.grid;
		Chart.defaults.font.family = "'IBM Plex Sans', sans-serif";
	}

	// Chart sampling: resize listener and adaptive decimation
	window._chartSamplingInit?.();

	// Date range defaults to the last 24h
	initDateInputs();

	loadCharts();
	_startLiveRefresh();

	// The KPI cards are swapped by HTMX on live events; keep the certificate value the charts computed.
	const infoCards = document.getElementById('target-info-cards');
	// WebKit has no scroll anchoring: replacing the cards above the charts jumped the scrolled
	// main area (about 280 px at 1024x768). Restore the offset synchronously, before the next paint.
	let scrollHost = null;
	let scrollTopBeforeSwap = 0;
	infoCards?.addEventListener('htmx:beforeSwap', () => {
		scrollHost = document.getElementById('main-content');
		scrollTopBeforeSwap = scrollHost ? scrollHost.scrollTop : 0;
	});
	infoCards?.addEventListener('htmx:afterSwap', () => {
		if (scrollHost && scrollHost.scrollTop !== scrollTopBeforeSwap) scrollHost.scrollTop = scrollTopBeforeSwap;
		if (appState.certDays === null) return;
		const certEl = document.getElementById('cert-days-value');
		if (certEl) certEl.textContent = appState.certDays + ' days';
		const certHint = document.getElementById('cert-days-hint');
		if (certHint) certHint.hidden = true;
	});

	// Theme observer: re-theme charts on a dark/light switch
	initChartThemeObserver(200);

	_pdfCleanup = initPdfJobListener(pageConfig.targetId);
	checkPendingPdfJob(pageConfig.targetId);


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
		if (downtimeHistorySwapGuard) {
			document.body.removeEventListener('htmx:beforeSwap', downtimeHistorySwapGuard);
			downtimeHistorySwapGuard = null;
		}
	});
});
