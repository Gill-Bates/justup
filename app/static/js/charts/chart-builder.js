//
// app/static/js/charts/chart-builder.js
// Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
//

/**
 * Chart construction functions for target-detail page.
 * Depends on:
 *   - utils/with-alpha.js      → withAlpha()
 *   - charts/chart-config.js   → createLineConfig(), createZoomOptions(),
 *                                 createSlaAnnotation(), TCP_PORT_COLORS, COLOR_RESPONSE_TIME
 *   - charts/chart-sampling.js → updateChartSampling(), _getVisibleRangeMs()
 *   - appState                 → appState.charts, appState.theme, appState.range (from target-detail.js)
 * @module charts/chart-builder
 */

/** Mobile breakpoint for responsive legend placement. */
var _MOBILE_BREAKPOINT = 768;

/**
 * Return 'bottom' on narrow screens, 'right' on wider ones.
 * @returns {'bottom'|'right'}
 */
function _legendPosition() {
	return window.innerWidth < _MOBILE_BREAKPOINT ? 'bottom' : 'right';
}

/**
 * @typedef {Object} AppState
 * @property {Map<string, Chart>} charts - Active chart instances by canvas ID
 * @property {Object} theme - Current theme colors { text, grid, muted, ... }
 * @property {Object} range - Selected date range { from: Date, to: Date }
 * @property {Symbol|null} currentLoadToken - Race condition guard for async loads
 * @property {Map} metricsCache - Cached metric data
 */

/**
 * Tooltip formatter for millisecond values.
 * @param {Object} ctx - Chart.js tooltip context
 * @returns {string} Formatted label
 */
function _msTooltipLabel(ctx) {
	return ' ' + Math.round(ctx.parsed.y) + ' ms';
}

/**
 * Tooltip formatter for port-specific millisecond values.
 * @param {Object} ctx - Chart.js tooltip context
 * @returns {string} Formatted label with port number
 */
function _portMsTooltipLabel(ctx) {
	const port = ctx.dataset.label.replace('Port ', '');
	return ' Port ' + port + ': ' + Math.round(ctx.parsed.y) + ' ms';
}

/**
 * Destroy an existing chart by canvas ID if it exists in appState.charts.
 * Removes event listener flags to prevent memory leaks on canvas replacement.
 * @param {string} canvasId
 */
function _destroyExisting(canvasId) {
	if (typeof appState !== 'undefined' && appState.charts.has(canvasId)) {
		const chart = appState.charts.get(canvasId);
		// Remove dblclick flag so new chart can rebind if canvas is replaced
		if (chart.canvas && chart.canvas.dataset) {
			delete chart.canvas.dataset.chartDblClickBound;
		}
		chart.destroy();
		appState.charts.delete(canvasId);
	}
}

/**
 * Register a chart in appState and set up double-click reset-zoom.
 * @param {string} canvasId
 * @param {Chart} chart
 * @param {HTMLCanvasElement} ctx
 * @param {boolean} [skipSampling=false] - Skip decimation updates (for non-line charts)
 */
function _registerChart(canvasId, chart, ctx, skipSampling) {
	if (typeof appState === 'undefined') return;
	
	appState.charts.set(canvasId, chart);

	// Responsive legend: update position on resize (right ↔ bottom)
	if (chart.options && chart.options.plugins && chart.options.plugins.legend) {
		var origOnResize = chart.options.onResize;
		chart.options.onResize = function(ch, size) {
			var newPos = _legendPosition();
			if (ch.options.plugins.legend.position !== newPos) {
				ch.options.plugins.legend.position = newPos;
				ch.update('none');
			}
			if (typeof origOnResize === 'function') origOnResize(ch, size);
		};
	}

	if (!skipSampling) {
		// Defer sampling update to avoid Proxy recursion during Chart construction
		setTimeout(() => {
			// Verify this chart is still the active one for this canvas (race condition guard)
			if (appState.charts.get(canvasId) === chart &&
				chart.canvas && chart.canvas.isConnected) {
				updateChartSampling(chart);
			}
		}, 0);
	}

	// Use addEventListener to avoid overwriting existing handlers
	const dblClickHandler = function() {
		const ch = appState.charts.get(canvasId);
		if (ch && ch.resetZoom) {
			ch.resetZoom();
			if (!skipSampling) {
				setTimeout(() => {
					// Verify chart is still active before updating
					if (appState.charts.get(canvasId) === ch &&
						ch.canvas && ch.canvas.isConnected) {
						updateChartSampling(ch);
					}
				}, 0);
			}
		}
	};
	
	// Only bind once per canvas
	if (!ctx.dataset.chartDblClickBound) {
		ctx.addEventListener('dblclick', dblClickHandler);
		ctx.dataset.chartDblClickBound = 'true';
	}
}

/**
 * Build a standard time-series line chart (response time, ping latency, etc.).
 * @param {string} canvasId - Canvas element ID
 * @param {object} data - { points: [{ ts, value }, ...] }
 * @param {string} color - Line/fill color
 * @param {object|null} [slaAnnotation] - SLA annotation from createSlaAnnotation()
 */
function buildChart(canvasId, data, color, slaAnnotation) {
	const ctx = document.getElementById(canvasId);
	if (!ctx) return;

	_destroyExisting(canvasId);

	const theme = appState.theme;
	const zoomOpts = createZoomOptions(updateChartSampling);
	const config = createLineConfig(theme, zoomOpts, _getVisibleRangeMs);

	config.data = {
		datasets: [{
			data: (data.points || []).map(p => ({ x: new Date(p.ts), y: p.value })),
			borderColor: color,
			backgroundColor: withAlpha(color, 0.2),
			fill: true,
		}],
	};

	if (slaAnnotation) {
		config.options.plugins.annotation = {
			annotations: { slaLine: slaAnnotation },
		};
	}

	// Override tooltip to show milliseconds unit
	config.options.plugins.tooltip.callbacks.label = _msTooltipLabel;

	const chart = new Chart(ctx, config);
	_registerChart(canvasId, chart, ctx);
}

/**
 * Build a per-service availability chart (PING, HTTP, TCP as separate lines).
 * This replaces the old aggregated uptime chart to avoid phantom downtimes.
 * @param {string} canvasId - Canvas element ID
 * @param {object} data - { series: { "PING": [{ts, up}], "HTTP": [{ts, up}], ... } }
 */
function buildAvailabilitySeriesChart(canvasId, data) {
	const ctx = document.getElementById(canvasId);
	if (!ctx) return;

	_destroyExisting(canvasId);

	const seriesData = data.series || {};
	const theme = appState.theme;
	const zoomOpts = createZoomOptions(updateChartSampling);
	const config = createLineConfig(theme, zoomOpts, _getVisibleRangeMs);

	// Service-specific colors (semantic)
	const serviceColors = {
		'PING': '#0d6efd',   // Primary blue
		'HTTP': '#198754',   // Success green
		'TCP': '#fd7e14',    // Orange
		'CERT': '#6f42c1',   // Purple
	};

	// Generate color for TCP ports (variations of orange)
	function getTcpPortColor(serviceName) {
		if (!serviceName.startsWith('TCP:')) return null;
		var port = parseInt(serviceName.split(':')[1], 10);
		if (isNaN(port)) return null;
		// Golden angle provides good perceptual separation
		var hue = (port * 137.508) % 360;
		// Theme-aware lightness: lighter in dark mode for better contrast
		var lightness = theme.mode === 'dark' ? 65 : 45;
		return 'hsl(' + Math.round(hue) + ', 70%, ' + lightness + '%)';
	}

	const datasets = [];
	for (const [serviceName, points] of Object.entries(seriesData)) {
		if (!points || points.length === 0) continue;

		const chartData = points.map(p => ({
			x: new Date(p.ts),
			y: p.up,
		}));

		const color = serviceColors[serviceName] || getTcpPortColor(serviceName) || '#6c757d';
		// Store TCP port hue for theme-aware color updates
		var tcpPortHue = null;
		if (serviceName.startsWith('TCP:')) {
			var port = parseInt(serviceName.split(':')[1], 10);
			if (!isNaN(port)) {
				tcpPortHue = (port * 137.508) % 360;
			}
		}

		var dataset = {
			label: serviceName,
			data: chartData,
			borderColor: color,
			backgroundColor: withAlpha(color, 0.1),
			borderWidth: 2,
			pointRadius: 0,
			tension: 0,
			stepped: 'before',
			fill: false,
			segment: {
				// Red segments when service is down
				borderColor: function(segCtx) {
					return segCtx.p0.parsed.y === 0 ? '#dc3545' : color;
				},
			},
		};
		// Store hue metadata for theme updates (if TCP port)
		if (tcpPortHue !== null) {
			dataset._tcpPortHue = tcpPortHue;
		}
		datasets.push(dataset);
	}

	// Y-axis: 0/1 with Up/Down labels
	config.options.scales.y.min = 0;
	config.options.scales.y.max = 1;
	config.options.scales.y.ticks.stepSize = 1;
	config.options.scales.y.ticks.callback = v => v === 1 ? 'Up' : 'Down';

	// Tooltip: show service name + status
	config.options.plugins.tooltip.callbacks.label = ctx => {
		const status = ctx.raw.y === 1 ? 'Up' : 'Down';
		return `${ctx.dataset.label}: ${status}`;
	};

	// Legend: right on desktop, bottom on mobile
	config.options.plugins.legend = {
		display: true,
		position: _legendPosition(),
		labels: {
			color: theme.text,
			padding: 15,
			usePointStyle: false,  // Use box instead of point style
			boxWidth: 12,
			boxHeight: 12,
			// Custom label generator for outlined squares
			generateLabels: function(chart) {
				const datasets = chart.data.datasets;
				// Chart.js legend drawing uses `legendItem.fontColor` (not `legend.labels.color`) for text.
				// Pull the resolved color from legend options so theme toggles are reflected.
				let labelColor = (chart.legend && chart.legend.options && chart.legend.options.labels)
					? chart.legend.options.labels.color
					: (chart.options.plugins && chart.options.plugins.legend && chart.options.plugins.legend.labels
						? chart.options.plugins.legend.labels.color
						: undefined);
				// Handle scriptable color option (function) by resolving it to a string.
				if (typeof labelColor === 'function') {
					try {
						labelColor = labelColor({ chart: chart });
					} catch (e) {
						// ignore
					}
				}
				return datasets.map(function(dataset, i) {
					const meta = chart.getDatasetMeta(i);
					return {
						text: dataset.label,
						fillStyle: 'transparent',  // No fill - outlined only
						strokeStyle: dataset.borderColor,
						lineWidth: 2,
						hidden: meta.hidden,
						datasetIndex: i,
						fontColor: labelColor,
					};
				});
			},
		},
		// Safe onClick handler to prevent null reference errors
		onClick: function(e, legendItem, legend) {
			const index = legendItem.datasetIndex;
			const chart = legend.chart;
			if (index == null || !chart) return;  // Guard against undefined
			chart.setDatasetVisibility(index, !chart.isDatasetVisible(index));
			chart.update();
		},
	};

	config.data = { datasets };

	const chart = new Chart(ctx, config);
	_registerChart(canvasId, chart, ctx);
}

/**
 * Build an HTTP status-code doughnut chart.
 * Zoom/pan are explicitly disabled (nonsensical on doughnut).
 * @param {string} canvasId - Canvas element ID
 * @param {object} data - { points: [{ ts, value }, ...] }  (value: HTTP status code)
 */
function buildStatusCodeChart(canvasId, data) {
	const ctx = document.getElementById(canvasId);
	if (!ctx) return;

	_destroyExisting(canvasId);

	const points = data.points || [];
	const statusCounts = {};
	points.forEach(p => {
		const code = Math.floor(p.value);
		if (code >= 100 && code <= 599) {
			statusCounts[code] = (statusCounts[code] || 0) + 1;
		} else {
			// Track invalid codes (timeouts, DNS errors, etc.) as 'Error'
			statusCounts['Error'] = (statusCounts['Error'] || 0) + 1;
		}
	});

	// Separate numeric codes and 'Error' category
	const allKeys = Object.keys(statusCounts);
	const numericCodes = allKeys.filter(k => k !== 'Error').map(Number).sort((a, b) => a - b);
	const hasErrors = allKeys.includes('Error');
	
	const labels = numericCodes.map(code => `HTTP ${code}`);
	const values = numericCodes.map(code => statusCounts[code]);
	const colors = numericCodes.map(code => {
		if (code >= 200 && code < 300) return '#198754';
		if (code >= 300 && code < 400) return '#ffc107';
		if (code >= 400) return '#dc3545';
		return '#6c757d';
	});
	
	// Append 'Error' category at the end
	if (hasErrors) {
		labels.push('Error');
		values.push(statusCounts['Error']);
		colors.push('#6c757d');
	}

	const theme = appState.theme;
	const chart = new Chart(ctx, {
		type: 'doughnut',
		options: {
			responsive: true,
			maintainAspectRatio: false,
			cutout: '70%',
			plugins: {
				legend: {
					display: true,
					position: _legendPosition(),
					labels: { color: theme.text },
				},
				tooltip: {
					callbacks: {
						label: ctx => ` ${ctx.label}: ${ctx.raw} requests`,
					},
				},
				// Explicitly disable zoom/pan on doughnut
				zoom: {
					zoom: { drag: { enabled: false }, wheel: { enabled: false }, pinch: { enabled: false } },
					pan: { enabled: false },
				},
			},
		},
		data: {
			labels: labels,
			datasets: [{
				data: values,
				backgroundColor: colors,
				borderColor: 'transparent',
				borderWidth: 2,
			}],
		},
	});

	_registerChart(canvasId, chart, ctx, true);
}

/**
 * Build combined TCP chart with all ports as separate datasets.
 * Falls back to aggregate tcp_ms if no per-port data is available.
 *
 * @param {string} canvasId - Fallback canvas element ID
 * @param {string[]} ports - Array of port numbers (as strings)
 * @param {Function} fetchMetricFn - Metric fetch function: (metric) => Promise<{points}>
 * @param {Symbol|null} [loadToken] - Optional race-condition guard token
 */
async function buildMultiPortTcpChart(canvasId, ports, fetchMetricFn, loadToken) {
	// Early stale-request guard BEFORE cleanup
	if (loadToken && typeof appState !== 'undefined' && appState.currentLoadToken !== loadToken) return;
	
	const fallbackCanvas = document.getElementById(canvasId);
	if (!fallbackCanvas) return;
	if (typeof appState === 'undefined') return;

	const container = document.getElementById('tcp-small-multiples-container');
	const fallbackContainer = document.getElementById('tcp-fallback-chart');

	// Clean up existing TCP charts (collect keys first to avoid concurrent modification)
	const tcpKeys = [];
	appState.charts.forEach(function(chart, key) {
		if (key.startsWith('tcp-port-')) {
			tcpKeys.push(key);
		}
	});
	tcpKeys.forEach(function(key) {
		const chart = appState.charts.get(key);
		if (chart) chart.destroy();
		appState.charts.delete(key);
	});
	
	_destroyExisting(canvasId);

	if (container) container.style.display = 'none';
	if (fallbackContainer) fallbackContainer.style.display = 'block';

	const theme = appState.theme;

	// Fetch data for each port in parallel
	const fetchPromises = ports.map((port, index) => {
		// Generate theme-aware color based on port number
		const portNum = parseInt(port);
		const hue = (portNum * 137.508) % 360;  // Golden angle for good separation
		const lightness = theme.mode === 'dark' ? 65 : 45;
		const color = 'hsl(' + Math.round(hue) + ', 70%, ' + lightness + '%)';
		
		return fetchMetricFn(`tcp_ms_${port}`).then(data => ({
			port: portNum,
			data,
			color,
		}));
	});

	const settledResults = await Promise.allSettled(fetchPromises);
	const results = settledResults
		.filter(function(r) { return r.status === 'fulfilled'; })
		.map(function(r) { return r.value; });
	
	// Log any failures for debugging
	settledResults
		.filter(function(r) { return r.status === 'rejected'; })
		.forEach(function(r) { console.warn('TCP port fetch failed:', r.reason); });

	// Stale-request guard
	if (loadToken && typeof appState !== 'undefined' && appState.currentLoadToken !== loadToken) return;

	const validResults = results.filter(r => r.data?.points?.length > 0);

	// Fallback to aggregate tcp_ms
	if (validResults.length === 0) {
		const fallbackData = await fetchMetricFn('tcp_ms');
		if (loadToken && appState.currentLoadToken !== loadToken) return;

		const zoomOpts = createZoomOptions(updateChartSampling);
		const config = createLineConfig(theme, zoomOpts, _getVisibleRangeMs);
		config.data = {
			datasets: [{
				label: 'TCP',
				data: (fallbackData?.points || []).map(p => ({ x: new Date(p.ts), y: p.value })),
				borderColor: '#6c757d',
				backgroundColor: withAlpha('#6c757d', 0.2),
				fill: true,
			}],
		};

		// Override tooltip to show milliseconds unit
		config.options.plugins.tooltip.callbacks.label = _msTooltipLabel;

		const chart = new Chart(fallbackCanvas, config);
		_registerChart(canvasId, chart, fallbackCanvas);
		return;
	}

	// Build datasets for all ports
	const datasets = validResults.map(({ port, data, color }) => {
		// Calculate hue for theme-aware updates
		var hue = (port * 137.508) % 360;
		return {
			label: `Port ${port}`,
			data: (data.points || []).map(p => ({ x: new Date(p.ts), y: p.value })),
			borderColor: color,
			backgroundColor: withAlpha(color, 0.1),
			fill: false,
			tension: 0.1,
			borderWidth: 2,
			pointRadius: 0,
			pointHitRadius: 8,
			// Store hue for theme updates
			_tcpPortHue: hue,
		};
	});

	const zoomOpts = createZoomOptions(updateChartSampling);
	const config = createLineConfig(theme, zoomOpts, _getVisibleRangeMs);
	config.data = { datasets };

	// Legend: right on desktop, bottom on mobile (same style as Availability chart)
	config.options.plugins.legend = {
		display: true,
		position: _legendPosition(),
		labels: {
			color: theme.text,
			padding: 15,
			usePointStyle: false,
			boxWidth: 12,
			boxHeight: 12,
			generateLabels: function(chart) {
				const datasets = chart.data.datasets;
				let labelColor = (chart.legend && chart.legend.options && chart.legend.options.labels)
					? chart.legend.options.labels.color
					: (chart.options.plugins && chart.options.plugins.legend && chart.options.plugins.legend.labels
						? chart.options.plugins.legend.labels.color
						: undefined);
				if (typeof labelColor === 'function') {
					try {
						labelColor = labelColor({ chart: chart });
					} catch (e) {
						// ignore
					}
				}
				return datasets.map(function(dataset, i) {
					const meta = chart.getDatasetMeta(i);
					return {
						text: dataset.label,
						fillStyle: 'transparent',
						strokeStyle: dataset.borderColor,
						lineWidth: 2,
						hidden: meta.hidden,
						datasetIndex: i,
						fontColor: labelColor,
					};
				});
			},
		},
		onClick: function (e, legendItem, legend) {
			const index = legendItem.datasetIndex;
			const chart = legend.chart;
			if (index == null || !chart) return;
			chart.setDatasetVisibility(index, !chart.isDatasetVisible(index));
			chart.update();
		},
	};

	config.options.maintainAspectRatio = false;

	// Override tooltip to show port-specific milliseconds
	config.options.plugins.tooltip.callbacks.label = _portMsTooltipLabel;

	const chart = new Chart(fallbackCanvas, config);
	_registerChart(canvasId, chart, fallbackCanvas);
}
