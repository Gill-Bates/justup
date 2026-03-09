//
// app/static/js/target-detail/metrics.js
// Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
//

/**
 * Metric data fetching: API calls, caching, race-condition guards.
 * Depends on:
 *   - appState.range, appState.metricsCache, appState.currentLoadToken (from target-detail.js)
 *   - apiFetch (global, from base.html)
 *   - targetId (global, from target-detail.html inline config)
 * @module target-detail/metrics
 */

/**
 * Determine optimal point-count limit for API queries.
 * Adapts to screen width so mobile devices request fewer points.
 * @private
 * @param {string} metric - Metric name
 * @param {number} rangeDays - Visible range in days
 * @returns {number} Point limit for API query
 */
function _chooseLimitForMetric(metric, rangeDays) {
	const screenWidth = window.innerWidth || 800;
	const isMobile = screenWidth < 768;
	// Scale base by screen width: mobile ~40% of desktop values
	const widthFactor = isMobile ? 0.4 : Math.min(1.0, screenWidth / 1400);

	let base;
	if (rangeDays <= 1) base = 280;
	else if (rangeDays <= 7) base = 600;
	else if (rangeDays <= 30) base = 900;
	else base = 1200;

	base = Math.round(base * widthFactor);

	// Uptime series: fewer points needed (binary 0/1)
	if (metric === 'http_up' || metric === 'ping_up' || metric === 'tcp_up') {
		base = Math.round(base * 0.8);
	}

	// Status distribution: more samples for accuracy
	if (metric === 'http_status') {
		return Math.min(isMobile ? 3000 : 10000, Math.max(800, base * 5));
	}

	const minLimit = isMobile ? 30 : 50;
	const maxLimit = isMobile ? 500 : 1500;
	return Math.max(minLimit, Math.min(maxLimit, base));
}

/**
 * Helper to check if appState has a valid range.
 * @private
 * @returns {boolean}
 */
function _hasValidRange() {
	return appState?.range?.from && appState?.range?.to && appState.range.from < appState.range.to;
}

/**
 * Fetch per-service availability series (new endpoint).
 * Returns separate timelines for PING, HTTP, TCP instead of aggregated status.
 * @param {number} targetId - Target ID
 * @param {Symbol} loadToken - Current load token
 * @returns {Promise<object|null>} { series: { "PING": [{ts, up}], ... } }
 */
async function fetchAvailabilitySeries(targetId, loadToken) {
	if (appState.currentLoadToken !== loadToken) return null;

	const { from, to } = appState.range;
	if (!from || !to) return null;

	try {
		const days = Math.ceil((to - from) / (24 * 60 * 60 * 1000));
		const screenWidth = window.innerWidth || 800;
		const isMobile = screenWidth < 768;
		// Mobile: limit points to ~40% of desktop
		const limit = isMobile ? Math.min(2000, Math.round(screenWidth * 3)) : 10000;
		const url = `/ui/fragments/availability-series/${targetId}?days=${days}&limit=${limit}`;

		const resp = await fetch(url);
		if (!resp.ok) return null;

		// Stale check after network
		if (appState.currentLoadToken !== loadToken) return null;

		const data = await resp.json();
		return data;

	} catch (err) {
		console.warn(`Failed to fetch availability series for target ${targetId}:`, err);
		return null;
	}
}

/**
 * Fetch a single metric from the backend with automatic resolution and caching.
 * @param {number} targetId - Target ID
 * @param {string} metric - Metric name (e.g. 'http_ms', 'ping_up')
 * @param {boolean} [useCache=true] - Whether to use cached data
 * @returns {Promise<object>} { points: [{ ts, value }, ...] }
 */
async function fetchMetric(targetId, metric, useCache = true) {
	// Validate range
	if (!_hasValidRange()) {
		console.warn('appState.range not available or invalid');
		return { points: [] };
	}

	const { from, to } = appState.range;

	// Check cache
	const cacheKey = `${metric}_${from.toISOString()}_${to.toISOString()}`;
	if (useCache && appState.metricsCache && appState.metricsCache.has(cacheKey)) {
		return appState.metricsCache.get(cacheKey);
	}

	try {
		const rangeDays = (to - from) / (1000 * 60 * 60 * 24);
		const limit = _chooseLimitForMetric(metric, rangeDays);

		// Build API URL with URLSearchParams (consistent with pdf-export.js)
		const params = new URLSearchParams({
			auto_resolution: 'true',
			limit: String(limit),
			since: from.toISOString(),
			until: to.toISOString(),
		});

		const url = `/targets/${encodeURIComponent(targetId)}/metrics/${encodeURIComponent(metric)}?${params}`;

		const resp = await apiFetch(url);

		if (!resp.ok) {
			showFetchError(`Failed to load ${metric}`);
			return { points: [] };
		}

		const data = await resp.json();

		// Cache the result (with size limit to prevent memory leak)
		const MAX_CACHE_ENTRIES = 50;
		if (appState.metricsCache) {
			// Remove oldest entry if cache is full
			if (appState.metricsCache.size >= MAX_CACHE_ENTRIES) {
				const firstKey = appState.metricsCache.keys().next().value;
				appState.metricsCache.delete(firstKey);
			}
			appState.metricsCache.set(cacheKey, data);
		}

		return data;
	} catch (e) {
		console.error('Fetch error:', e);
		showFetchError(`Network error loading ${metric}`);
		return { points: [] };
	}
}

/**
 * Guarded fetch wrapper to respect current load token and avoid race conditions.
 * Returns null if the load token became stale during the fetch.
 * @param {number} targetId - Target ID
 * @param {string} metric - Metric name
 * @param {Symbol} loadToken - Current load token
 * @returns {Promise<object|null>} { points } or null if stale
 */
async function fetchMetricGuarded(targetId, metric, loadToken) {
	if (!appState || appState.currentLoadToken !== loadToken) return null;

	try {
		const data = await fetchMetric(targetId, metric, true);
		if (appState.currentLoadToken !== loadToken) return null;
		return data;
	} catch (e) {
		// Stale requests don't need error handling
		if (appState.currentLoadToken !== loadToken) return null;
		throw e;
	}
}

/**
 * Show user-visible error feedback when a fetch fails.
 * Debounced to prevent toast spam when multiple metrics fail simultaneously.
 * @param {string} message - Error message
 */
let _lastFetchErrorTime = 0;
const FETCH_ERROR_DEBOUNCE_MS = 2000;

function showFetchError(message) {
	console.warn('Fetch error:', message);
	const now = Date.now();
	if (now - _lastFetchErrorTime > FETCH_ERROR_DEBOUNCE_MS) {
		_lastFetchErrorTime = now;
		window.justUpToast?.error?.(message);
	}
}

/**
 * Format a timestamp for display in downtime tables, etc.
 * Uses browser locale and respects UTC/local time setting.
 * @param {number|string} ts - Timestamp (ms or ISO string)
 * @param {boolean} [utc] - Whether to display in UTC (falls back to pageConfig.useUtc or global useUtc)
 * @returns {string} Formatted date/time
 */
function formatTimestamp(ts, utc) {
	const d = new Date(ts);
	const locale = navigator.language || 'de-DE';

	// Determine UTC mode: explicit param > pageConfig > global useUtc > false
	const isUtc = typeof utc === 'boolean'
		? utc
		: (typeof pageConfig !== 'undefined' && pageConfig.useUtc)
			? pageConfig.useUtc
			: (typeof useUtc !== 'undefined' ? useUtc : false);

	const timeOptions = {
		hour: '2-digit',
		minute: '2-digit',
		hour12: false,
	};
	if (isUtc) timeOptions.timeZone = 'UTC';

	const dateOptions = {};
	if (isUtc) dateOptions.timeZone = 'UTC';

	// Show date+time for ranges > 24h
	if (appState?.range) {
		const { from, to } = appState.range;
		if (from && to && (to - from) > 24 * 60 * 60 * 1000) {
			return d.toLocaleDateString(locale, dateOptions) + ' '
				+ d.toLocaleTimeString(locale, timeOptions);
		}
	}

	return d.toLocaleTimeString(locale, timeOptions);
}
