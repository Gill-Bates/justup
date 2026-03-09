//
// app/static/js/charts/chart-config.js
// Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
//

/**
 * Shared Chart.js configuration: line chart factory, zoom options, SLA annotations.
 * Depends on: withAlpha(), getCssVar() from utils/with-alpha.js
 * @module charts/chart-config
 */

// SLA annotation styling
const SLA_LINE_COLOR = '#f0ad4e';
const SLA_LINE_WIDTH = 2;

// Default color for response-time / latency lines
const COLOR_RESPONSE_TIME = '#198754';

// Color palette for multi-port TCP charts
const TCP_PORT_COLORS = [
	'#0d6efd', '#198754', '#dc3545', '#fd7e14', '#6f42c1',
	'#20c997', '#d63384', '#ffc107', '#6c757d', '#0dcaf0',
];

/**
 * Zoom/pan plugin options (shared by all line charts).
 * Requires chartjs-plugin-zoom.
 * @param {Function} onComplete - Callback after zoom/pan (e.g. updateChartSampling)
 */
function createZoomOptions(onComplete) {
	return {
		zoom: {
			drag: {
				enabled: true,
				backgroundColor: 'rgba(13, 110, 253, 0.2)',
				borderColor: 'rgba(13, 110, 253, 0.8)',
				borderWidth: 1,
			},
			mode: 'x',
			onZoomComplete: onComplete ? function(context) {
				if (context && context.chart) onComplete(context.chart);
			} : undefined,
		},
		pan: {
			enabled: true,
			mode: 'x',
			onPanComplete: onComplete ? function(context) {
				if (context && context.chart) onComplete(context.chart);
			} : undefined,
		},
	};
}

/**
 * Create SLA annotation line for charts.
 * @param {object} slaConfig - { enabled, availabilityPct, responseTimeMs, pingLatencyMs }
 * @param {number|null} value - SLA target value
 * @param {string} label - Label text (e.g. "200 ms")
 * @returns {object|null} Chart.js annotation config, or null if disabled
 */
function createSlaAnnotation(slaConfig, value, label) {
	if (!slaConfig || !slaConfig.enabled || value === null || value === undefined) return null;
	return {
		type: 'line',
		yMin: value,
		yMax: value,
		borderColor: SLA_LINE_COLOR,
		borderWidth: SLA_LINE_WIDTH,
		borderDash: [6, 4],
		label: {
			display: true,
			content: 'SLA: ' + String(label || '').substring(0, 50),
			position: 'end',
			backgroundColor: SLA_LINE_COLOR,
			color: '#000',
			font: { size: 10, weight: 'bold' },
			padding: { top: 2, bottom: 2, left: 4, right: 4 },
		},
	};
}

/**
 * Build a standard Chart.js line-chart config object.
 * Does NOT create a Chart instance — returns the config for `new Chart(ctx, config)`.
 * @param {object} theme - Chart theme colors
 * @param {string} theme.text - Primary text color
 * @param {string} theme.muted - Secondary/muted text color (axis ticks)
 * @param {string} theme.grid - Grid line color
 * @param {object} [zoomOpts] - Zoom plugin options (from createZoomOptions)
 * @param {Function} [getVisibleRangeMs] - Function returning visible x-range in ms
 * @returns {object} Chart.js config
 */
function createLineConfig(theme, zoomOpts, getVisibleRangeMs) {
	// Locale-aware axis formatters
	const userLocale = navigator.language || 'de-DE';

	function formatAxisTime(date) {
		return date.toLocaleTimeString(userLocale, {
			hour: '2-digit',
			minute: '2-digit',
			second: undefined,
			hour12: false  // consistent width across locales
		});
	}
	function formatAxisDate(date) {
		return date.toLocaleDateString(userLocale, { month: 'short', day: 'numeric' });
	}
	function formatAxisDateTime(date) {
		return formatAxisDate(date) + ' ' + formatAxisTime(date);
	}

	return {
		type: 'line',
		options: {
			responsive: true,
			maintainAspectRatio: false,
			layout: { padding: { top: 10, right: 10, left: 5, bottom: 5 } },
			clip: false,
			scales: {
				x: {
					type: 'time',
					display: true,
					ticks: {
						color: theme.muted,
						callback: function (value) {
							const date = new Date(value);
							const rangeMs = typeof getVisibleRangeMs === 'function'
								? getVisibleRangeMs(this.chart)
								: 0;
							const rangeDays = rangeMs / (1000 * 60 * 60 * 24);
							if (rangeDays <= 1) return formatAxisTime(date);
							if (rangeDays <= 7) return formatAxisDateTime(date);
							return formatAxisDate(date);
						},
					},
					grid: { color: theme.grid },
					title: { display: false },
				},
				y: {
					display: true,
					beginAtZero: true,
					ticks: {
						color: theme.muted,
						callback: function (value) {
							if (Number.isInteger(value)) return value;
							// Show one decimal for fractional SLA values (e.g., 99.9)
							// Suppress Chart.js auto-generated intermediate sub-ticks
							if (value === Math.round(value * 10) / 10) {
								return value.toFixed(1);
							}
							return '';
						},
					},
					grid: { color: theme.grid },
				},
			},
			plugins: {
				legend: { display: false, labels: { color: theme.text } },
				zoom: zoomOpts || {},
				tooltip: {
					callbacks: {
						title: function (tooltipItems) {
							if (!tooltipItems.length) return '';
							return formatAxisDateTime(new Date(tooltipItems[0].parsed.x));
						},
						label: function (ctx) {
							// Default: unit-agnostic. Chart builders override for "ms" or "%"
							return ' ' + Math.round(ctx.parsed.y);
						},
					},
				},
			},
			elements: {
				point: { radius: 2, hoverRadius: 6, borderWidth: 1, hitRadius: 10 },
				line: { tension: 0.2, borderWidth: 1.5 },
			},
		},
	};
}

// Explicit exports for global access
window.createLineConfig = createLineConfig;
window.createZoomOptions = createZoomOptions;
window.createSlaAnnotation = createSlaAnnotation;
window.TCP_PORT_COLORS = TCP_PORT_COLORS;
window.COLOR_RESPONSE_TIME = COLOR_RESPONSE_TIME;
