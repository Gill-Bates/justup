//
// app/static/js/charts/chart-sampling.js
// Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
//

/**
 * Adaptive decimation / point-visibility for Chart.js line charts.
 * Automatically reduces data points on mobile devices for better touch interaction.
 * Depends on: appState.range (from target-detail.js)
 * @module charts/chart-sampling
 */

// Mobile breakpoint (Bootstrap sm)
const MOBILE_BREAKPOINT = 768;

// Decimation configuration (extracted for maintainability)
var DECIMATION_CONFIG = {
	mobile: {
		widthMultiplier: 0.35,
		minSamples: 80,
		maxSamples: 600,
		longRangeExtraFactor: 0.6,  // additional reduction for >1 day
	},
	desktop: {
		widthMultiplier: 0.9,
		minSamples: 200,
		maxSamples: 3000,  // reduced from 10000 to prevent browser lag
	},
	// rangeDays threshold -> factor (ordered, first match wins)
	rangeBuckets: [
		{ above: 180, factor: 0.35 },
		{ above: 30,  factor: 0.45 },
		{ above: 7,   factor: 0.55 },
		{ above: 1,   factor: 0.70 },
		{ above: 0.25, factor: 0.80 },
		{ above: 1/24, factor: 1.05 },
		{ above: 0,    factor: 1.35 },
	]
};

/**
 * Check if current viewport is mobile-sized.
 * @returns {boolean}
 */
function _isMobile() {
	return window.innerWidth < MOBILE_BREAKPOINT;
}

/**
 * Get the visible x-axis range in milliseconds.
 * Falls back to appState.range if chart scales are unavailable.
 * @param {Chart} chart - Chart.js instance
 * @returns {number} Range in ms (0 if unknown)
 */
function _getVisibleRangeMs(chart) {
	try {
		const sx = chart?.scales?.x;
		if (sx && typeof sx.min === 'number' && typeof sx.max === 'number' && sx.max > sx.min) {
			return sx.max - sx.min;
		}
	} catch (e) { /* ignore */ }

	// Fallback to global appState (set by target-detail.js)
	if (typeof appState !== 'undefined' && appState?.range) {
		const from = appState.range.from;
		const to = appState.range.to;
		// appState.range.from/to are Date objects
		if (from instanceof Date && to instanceof Date && to > from) {
			return to.getTime() - from.getTime();
		}
		// Fallback for numeric timestamps (legacy compatibility)
		if (typeof from === 'number' && typeof to === 'number' && to > from) {
			return to - from;
		}
	}
	return 0;
}

/**
 * Compute optimal number of decimation samples based on visible range and chart width.
 * Mobile devices get significantly fewer samples for better touch interaction.
 * @param {Chart} chart - Chart.js instance
 * @returns {number} Target sample count (clamped per DECIMATION_CONFIG)
 */
function _computeDecimationSamples(chart) {
	var rangeMs = _getVisibleRangeMs(chart);
	var rangeDays = rangeMs ? (rangeMs / (1000 * 60 * 60 * 24)) : 0;
	var width = Math.max(300, Math.floor(chart?.chartArea?.width || chart?.width || 800));
	var mobile = _isMobile();
	var cfg = mobile ? DECIMATION_CONFIG.mobile : DECIMATION_CONFIG.desktop;

	// Base samples from chart width
	var base = Math.round(width * cfg.widthMultiplier);

	// Time-range factor: wider range = more aggressive decimation
	var factor = 1.0;
	for (var i = 0; i < DECIMATION_CONFIG.rangeBuckets.length; i++) {
		var bucket = DECIMATION_CONFIG.rangeBuckets[i];
		if (rangeDays > bucket.above) {
			factor = bucket.factor;
			break;
		}
	}

	// Extra reduction on mobile for longer time ranges
	if (mobile && rangeDays > 1) {
		factor *= cfg.longRangeExtraFactor;
	}

	var samples = Math.round(base * factor);
	return Math.max(cfg.minSamples, Math.min(cfg.maxSamples, samples));
}

/**
 * Check if chart supports Chart.js decimation plugin.
 * Requires {x, y} data format on ALL datasets and time/linear x-axis.
 * @param {Chart} chart - Chart.js instance
 * @returns {boolean}
 */
function _chartSupportsDecimation(chart) {
	// Chart.js decimation requires:
	// 1. Line chart (already checked by caller)
	// 2. All datasets use {x, y} format
	// 3. x-axis is linear or time scale
	var datasets = chart.data?.datasets;
	if (!datasets || datasets.length === 0) return false;

	var scaleType = chart.options?.scales?.x?.type;
	if (scaleType !== 'time' && scaleType !== 'linear' && scaleType !== 'timeseries') {
		return false;
	}

	// Verify ALL datasets are compatible with decimation
	for (var i = 0; i < datasets.length; i++) {
		var ds = datasets[i];
		if (!ds.data || ds.data.length === 0) continue;
		var firstPoint = ds.data[0];
		var hasXY = firstPoint !== null && typeof firstPoint === 'object'
			&& 'x' in firstPoint && 'y' in firstPoint;
		if (!hasXY) return false;
	}
	return true;
}

/**
 * Update decimation and point-visibility settings on a line chart.
 * Mobile devices get larger hit radii for easier touch interaction.
 * Skips non-line charts and guards against recursive calls.
 * @param {Chart} chart - Chart.js instance
 */
function updateChartSampling(chart) {
	if (!chart) return;
	if (chart.config?.type !== 'line') return;
	if (chart._isUpdatingSampling) return;
	// Skip sampling updates during theme changes (theme only affects colors, not data density)
	if (chart._isThemeUpdating) return;
	// Guard: Canvas may be detached from DOM (page navigation, HTMX swap)
	// Check canvas, parentNode, and document connection to prevent null reference errors
	if (!chart.canvas || !chart.canvas.parentNode || !chart.canvas.isConnected) return;

	var mobile = _isMobile();
	var samples = _computeDecimationSamples(chart);
	
	// Use maximum point count across all datasets
	var points = 0;
	var datasets = chart.data?.datasets || [];
	for (var d = 0; d < datasets.length; d++) {
		var len = datasets[d]?.data?.length || 0;
		if (len > points) points = len;
	}
	
	var shouldDecimate = points > samples && _chartSupportsDecimation(chart);

	// More aggressive point hiding on mobile
	var hideThreshold = mobile
		? Math.max(60, Math.round(samples * 0.5))
		: Math.max(250, Math.round(samples * 0.8));
	var hidePoints = points > hideThreshold;

	// Larger hit radius on mobile for easier touch selection
	var targetRadius = hidePoints ? 0 : (mobile ? 3 : 2);
	var targetHoverRadius = mobile ? 8 : 6;
	var targetHitRadius = mobile ? 20 : 10;

	// Only update if settings actually changed (all five properties)
	var dec = chart.options.plugins?.decimation;
	var currentPoint = chart.options.elements?.point;

	if (dec &&
		dec.enabled === shouldDecimate &&
		dec.samples === samples &&
		currentPoint?.radius === targetRadius &&
		currentPoint?.hoverRadius === targetHoverRadius &&
		currentPoint?.hitRadius === targetHitRadius) {
		return;
	}

	chart._isUpdatingSampling = true;
	try {
		if (!chart.options.plugins) chart.options.plugins = {};
		chart.options.plugins.decimation = {
			enabled: shouldDecimate,
			algorithm: 'lttb',
			samples: samples,
		};

		if (!chart.options.elements) chart.options.elements = {};
		if (!chart.options.elements.point) chart.options.elements.point = {};
		chart.options.elements.point.radius = targetRadius;
		chart.options.elements.point.hoverRadius = targetHoverRadius;
		chart.options.elements.point.hitRadius = targetHitRadius;

		// 'none' mode: synchronous update without animation.
		// The _isUpdatingSampling flag guards against re-entry if
		// Chart.js triggers plugin hooks synchronously during update().
		chart.update('none');
	} finally {
		chart._isUpdatingSampling = false;
	}
}

/**
 * Debounced resize handler to update all active charts when viewport changes.
 * Particularly important for mobile device rotation (portrait ↔ landscape).
 */
var _resizeDebounce = null;
var _resizeHandler = null;

function _handleViewportResize() {
	if (_resizeDebounce) clearTimeout(_resizeDebounce);
	_resizeDebounce = setTimeout(function() {
		// Iterate over appState.charts (Map), if available
		if (typeof appState !== 'undefined' && appState.charts && typeof appState.charts.forEach === 'function') {
			appState.charts.forEach(function(chart) {
				// Guard: Check canvas is still connected before updating
				if (chart && chart.config?.type === 'line' && chart.canvas?.isConnected) {
					updateChartSampling(chart);
				}
			});
		}
	}, 200);
}

function registerResizeListener() {
	if (_resizeHandler) return;
	_resizeHandler = _handleViewportResize;
	window.addEventListener('resize', _resizeHandler, { passive: true });
}

function unregisterResizeListener() {
	if (_resizeHandler) {
		window.removeEventListener('resize', _resizeHandler);
		_resizeHandler = null;
	}
	if (_resizeDebounce) {
		clearTimeout(_resizeDebounce);
		_resizeDebounce = null;
	}
}

function fullCleanup() {
	unregisterResizeListener();
	delete window.updateChartSampling;
	delete window._chartSamplingCleanup;
	delete window._chartSamplingInit;
}

// Automatic cleanup on HTMX navigation to prevent resize handler leaks
if (typeof document !== 'undefined' && document.body) {
	document.body.addEventListener('htmx:beforeSwap', function(evt) {
		// Only cleanup if sampling is initialized
		if (typeof window._chartSamplingCleanup === 'function') {
			window._chartSamplingCleanup();
		}
	});
}

// Exports for external use
// NOTE: Auto-registration removed. Chart pages should call:
//   window._chartSamplingInit() during initialization
//   window._chartSamplingCleanup() on page cleanup
window.updateChartSampling = updateChartSampling;
window._chartSamplingInit = registerResizeListener;
window._chartSamplingCleanup = fullCleanup;
