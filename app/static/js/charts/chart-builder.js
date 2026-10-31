//
// app/static/js/charts/chart-builder.js
// Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
//

/**
 * Chart construction functions for target-detail page.
 * Depends on:
 *   - utils/with-alpha.js      → withAlpha()
 *   - charts/chart-config.js   → createLineConfig(), createZoomOptions()
 *   - charts/combined-chart.js → _combinedPalette(), _prefersReducedMotion() (runtime use only)
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
 * Destroy an existing chart by canvas ID if it exists in appState.charts.
 * Removes event listener flags to prevent memory leaks on canvas replacement.
 * @param {string} canvasId
 */
function _unbindDblClick(canvas) {
	if (canvas._chartDblClickHandler) {
		canvas.removeEventListener('dblclick', canvas._chartDblClickHandler);
		canvas._chartDblClickHandler = null;
	}
	if (canvas.dataset) delete canvas.dataset.chartDblClickBound;
}

function _destroyExisting(canvasId) {
	if (typeof appState !== 'undefined' && appState.charts.has(canvasId)) {
		const chart = appState.charts.get(canvasId);
		// Detach the old dblclick handler so rebuilds do not stack listeners
		if (chart.canvas) {
			_unbindDblClick(chart.canvas);
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
	if (chart.options && chart.options.plugins && chart.options.plugins.legend && !chart._fixedLegend) {
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
			if (ch._reloadVisible) ch._reloadVisible();
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
	
	// Exactly one handler per canvas: replace any previous one
	_unbindDblClick(ctx);
	ctx.addEventListener('dblclick', dblClickHandler);
	ctx._chartDblClickHandler = dblClickHandler;
	ctx.dataset.chartDblClickBound = 'true';
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
			observed: !!p.observed,
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
			backgroundColor: withAlpha(color, 0.12),
			borderWidth: 2,
			pointRadius: c => c.raw?.observed ? 4 : 0,
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

	const plotEl = document.getElementById('availability-plot');
	const emptyPanel = document.getElementById('availability-empty');
	if (!datasets.length) {
		if (plotEl) plotEl.hidden = true;
		if (emptyPanel) {
			emptyPanel.hidden = false;
			const msg = emptyPanel.querySelector('[data-role="message"]');
			if (msg) msg.textContent = 'No availability data in the selected range. Pick a longer range or wait for the next check.';
		}
		ctx.setAttribute('aria-label', 'Availability per check over time. No data to display.');
		return;
	}
	if (plotEl) plotEl.hidden = false;
	if (emptyPanel) emptyPanel.hidden = true;
	// A single series gets a soft area so the up/down state reads as a band, not a hairline.
	if (datasets.length === 1) datasets[0].fill = 'origin';
	ctx.setAttribute('aria-label', 'Availability per check over time: ' + datasets.map(d => d.label).join(', ') + '.');

	// Y-axis: 0/1 with Up/Down labels
	config.options.scales.y.min = 0;
	config.options.scales.y.max = 1;
	config.options.scales.y.ticks.stepSize = 1;
	config.options.scales.y.ticks.callback = v => v === 1 ? 'Up' : 'Down';

	// Tooltip: show service name + status
	config.options.plugins.tooltip.callbacks.label = ctx => {
		const status = ctx.raw.observed ? 'Down observed in rollup interval'
			: (ctx.raw.y === 1 ? 'Up' : 'Down');
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
 * Build the HTTP status-code doughnut shown next to the combined chart.
 * Colors follow the status classes of the combined chart markers; labels carry the code as text.
 * @param {string} canvasId - Canvas element ID
 * @param {object} data - { points: [{ ts, value }, ...], error?: boolean }  (value: HTTP status code)
 */
function buildStatusCodeChart(canvasId, data) {
	const ctx = document.getElementById(canvasId);
	if (!ctx) return;

	_destroyExisting(canvasId);

	const wrap = document.getElementById('status-donut-plot');
	const emptyEl = document.getElementById('status-donut-empty');
	const totalEl = document.getElementById('status-donut-total');
	const points = (data && data.points) || [];
	const exact = !!(data && data.status_counts);
	const statusCounts = exact ? data.status_counts : {};
	if (!exact) points.forEach(p => {
		const code = Math.round(Number(p.value));
		const key = code >= 100 && code <= 599 ? String(code) : 'Error';
		statusCounts[key] = (statusCounts[key] || 0) + 1;
	});
	const unit = exact ? (data.raw_status_only ? 'raw checks in the last 30 days' : 'checks') : 'samples';
	const numericCodes = Object.keys(statusCounts).filter(k => k !== 'Error').map(Number).sort((a, b) => a - b);
	const keys = numericCodes.map(String);
	if (statusCounts.Error) keys.push('Error');

	if (!keys.length) {
		if (wrap) wrap.hidden = true;
		if (totalEl) totalEl.hidden = true;
		if (emptyEl) {
			emptyEl.hidden = false;
			emptyEl.textContent = data && data.error
				? 'Status codes could not be loaded.'
				: 'No status codes in the selected range.';
		}
		ctx.setAttribute('aria-label', 'HTTP status code distribution: no data.');
		return;
	}
	if (wrap) wrap.hidden = false;
	if (emptyEl) emptyEl.hidden = true;
	if (totalEl) {
		const total = keys.reduce((sum, k) => sum + statusCounts[k], 0);
		totalEl.textContent = total + ' ' + unit;
		totalEl.hidden = false;
	}

	const clsOf = k => (k === 'Error' ? 'err' : CombinedChartData.classifyStatus(Number(k)).key);
	const colorsFor = theme => keys.map(k => _combinedPalette(theme)[clsOf(k)]);
	const labels = keys.map(k => (k === 'Error' ? 'Error' : 'HTTP ' + k));
	const values = keys.map(k => statusCounts[k]);
	const theme = appState.theme;
	const chart = new Chart(ctx, {
		type: 'doughnut',
		options: {
			responsive: true,
			maintainAspectRatio: false,
			cutout: '68%',
			layout: { padding: 4 },
			animation: _prefersReducedMotion() ? false : { duration: 250 },
			plugins: {
				legend: { display: true, position: 'bottom', labels: { color: theme.text, usePointStyle: true, pointStyle: 'rectRounded', boxWidth: 10, boxHeight: 10, padding: 12, font: { size: 12 } } },
				tooltip: { callbacks: { label: c => ` ${c.label}: ${c.raw} ${unit}` } },
				zoom: {
					zoom: { drag: { enabled: false }, wheel: { enabled: false }, pinch: { enabled: false } },
					pan: { enabled: false },
				},
			},
		},
		data: {
			labels: labels,
			datasets: [{ data: values, backgroundColor: colorsFor(theme), borderColor: theme.bg, borderWidth: 2 }],
		},
	});
	chart._fixedLegend = true;
	chart._themeHook = function (t) {
		chart.data.datasets[0].backgroundColor = colorsFor(t);
		chart.data.datasets[0].borderColor = t.bg;
	};
	ctx.setAttribute('role', 'img');
	ctx.setAttribute('aria-label', 'HTTP status code distribution: '
		+ labels.map((l, i) => l + ' ' + values[i] + ' ' + unit).join(', ') + '.');
	_registerChart(canvasId, chart, ctx, true);
}
