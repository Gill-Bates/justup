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
 * Helper to check if appState has a valid range.
 * @private
 * @returns {boolean}
 */
function _hasValidRange() {
	return appState?.range?.from && appState?.range?.to && appState.range.from < appState.range.to;
}

function displayPlotWidth(width) {
	const pixels = Math.max(120, Math.min(2400, Math.round(width || 900)));
	return pixels < 500 ? Math.max(120, Math.round(pixels / 3)) : pixels;
}

/** True for the AbortError raised by a deliberately cancelled load cycle. */
function isAbortError(err) {
	return err?.name === 'AbortError';
}

/**
 * Fetch per-service availability series (new endpoint).
 * Returns separate timelines for PING, HTTP, TCP instead of aggregated status.
 * @param {number} targetId - Target ID
 * @param {Symbol} loadToken - Current load token
 * @param {AbortSignal} [signal] - Load-cycle signal; an abort resolves to null without a warning
 * @returns {Promise<object|null>} { series: { "PING": [{ts, up}], ... } }
 */
async function fetchAvailabilitySeries(targetId, loadToken, signal) {
	if (appState.currentLoadToken !== loadToken) return null;

	const { from, to } = appState.range;
	if (!from || !to) return null;

	try {
		const params = new URLSearchParams({ since: from.toISOString(), until: to.toISOString(), limit: '50000' });
		const url = `/ui/fragments/availability-series/${targetId}?${params}`;

		const resp = await apiFetch(url, { signal });
		if (!resp.ok) return null;

		// Stale check after network
		if (appState.currentLoadToken !== loadToken) return null;

		const data = await resp.json();
		return data;

	} catch (err) {
		if (isAbortError(err)) return null;
		console.warn(`Failed to fetch availability series for target ${targetId}:`, err);
		return null;
	}
}

/**
 * Fetch a single metric from the backend with automatic resolution and caching.
 * @param {number} targetId - Target ID
 * @param {string} metric - Metric name (e.g. 'http_ms', 'ping_up')
 * @param {boolean} [useCache=true] - Whether to use cached data
 * @param {AbortSignal} [signal] - Load-cycle signal; an abort rejects with an AbortError
 * @returns {Promise<object>} { points: [{ ts, value }, ...] }
 */
async function fetchMetric(targetId, metric, useCache = true, signal) {
	// Validate range
	if (!_hasValidRange()) {
		console.warn('appState.range not available or invalid');
		return { points: [] };
	}

	const { from, to } = appState.range;

	// Check cache
	const plotWidth = displayPlotWidth(document.getElementById('chart-combined')?.clientWidth || window.innerWidth || 900);
	const cacheKey = `${metric}_${from.toISOString()}_${to.toISOString()}_${plotWidth}`;
	if (useCache && appState.metricsCache && appState.metricsCache.has(cacheKey)) {
		return appState.metricsCache.get(cacheKey);
	}

	try {
		// Build API URL with URLSearchParams (consistent with pdf-export.js)
		const params = new URLSearchParams({
			display: 'true',
			plot_width: String(plotWidth),
			since: from.toISOString(),
			until: to.toISOString(),
		});

		const url = `/targets/${encodeURIComponent(targetId)}/metrics/${encodeURIComponent(metric)}?${params}`;

		const resp = await apiFetch(url, { signal });

		if (!resp.ok) {
			showFetchError(`Failed to load ${metric}`);
			return { points: [], error: true };
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
		if (isAbortError(e)) throw e;
		console.error('Fetch error:', e);
		showFetchError(`Network error loading ${metric}`);
		return { points: [], error: true };
	}
}

async function fetchDisplayMetricRange(targetId, metric, fromMs, toMs, plotWidth, signal) {
	const params = new URLSearchParams({
		display: 'true', plot_width: String(displayPlotWidth(plotWidth)),
		since: new Date(fromMs).toISOString(), until: new Date(toMs).toISOString(),
	});
	const response = await apiFetch(
		`/targets/${encodeURIComponent(targetId)}/metrics/${encodeURIComponent(metric)}?${params}`,
		{ signal },
	);
	if (!response.ok) throw new Error(`Metric request failed: ${response.status}`);
	return response.json();
}

/**
 * Guarded fetch wrapper to respect current load token and avoid race conditions.
 * Returns null if the load token became stale during the fetch.
 * @param {number} targetId - Target ID
 * @param {string} metric - Metric name
 * @param {Symbol} loadToken - Current load token
 * @param {AbortSignal} [signal] - Load-cycle signal; aborted requests resolve to null
 * @returns {Promise<object|null>} { points } or null if stale or aborted
 */
async function fetchMetricGuarded(targetId, metric, loadToken, signal) {
	if (!appState || appState.currentLoadToken !== loadToken) return null;

	try {
		const data = await fetchMetric(targetId, metric, true, signal);
		if (appState.currentLoadToken !== loadToken) return null;
		return data;
	} catch (e) {
		// Cancelled or stale requests are expected and need no error handling
		if (isAbortError(e) || appState.currentLoadToken !== loadToken) return null;
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
