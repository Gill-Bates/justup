//
// app/static/js/charts/combined-chart.js
// Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
//

/**
 * Combined chart: latency lines (left axis), HTTP status markers (lower band, shared x-axis)
 * and downtime background bands. Legend toggles are real buttons for keyboard use.
 * Depends on: chart-config.js, chart-builder.js (_registerChart, _destroyExisting),
 *   combined-chart-data.js (CombinedChartData), appState.
 * @module charts/combined-chart
 */

/** Series colors with >= 3:1 contrast on the card background in each theme. */
var COMBINED_PALETTES = {
	light: {
		http: '#0f766e', ping: '#7c3aed', tcp: '#b45309',
		'2xx': '#15803d', '3xx': '#475569', '4xx': '#c2410c', '5xx': '#b91c1c', err: '#7f1d1d',
		down: '#dc3545', sla: '#a16207', slaText: '#ffffff',
	},
	dark: {
		http: '#2dd4bf', ping: '#a78bfa', tcp: '#fbbf24',
		'2xx': '#4ade80', '3xx': '#94a3b8', '4xx': '#fb923c', '5xx': '#f87171', err: '#fca5a5',
		down: '#f87171', sla: '#facc15', slaText: '#000000',
	},
};

function _combinedPalette(theme) {
	return COMBINED_PALETTES[theme && theme.mode === 'dark' ? 'dark' : 'light'];
}

function _prefersReducedMotion() {
	return !!(window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches);
}

/** Name of the chart-local interaction mode that makes the whole status segment hoverable. */
var COMBINED_INTERACTION_MODE = 'combinedStatus';

/**
 * Register (once) a custom interaction mode: the ms/peak lines keep plain point+intersect hover,
 * and the status strip additionally resolves the bucket under the pointer's x across its full
 * [x, end] width — the colored segments span that range while the dataset points sit only at x.
 */
function _registerCombinedInteraction() {
	var modes = window.Chart && Chart.Interaction && Chart.Interaction.modes;
	if (!modes || modes[COMBINED_INTERACTION_MODE]) return;
	modes[COMBINED_INTERACTION_MODE] = function (chart, e, options, useFinalPosition) {
		var items = modes.point(chart, e, options, useFinalPosition);
		if (items.some(function (it) { return chart.data.datasets[it.datasetIndex]._kind === 'status'; })) return items;
		var di = chart.data.datasets.findIndex(function (d) { return d._kind === 'status'; });
		if (di < 0 || !chart.isDatasetVisible(di)) return items;
		var helpers = window.Chart && Chart.helpers;
		var isLeave = !e || e.type === 'mouseout';
		var pos = (isLeave || !helpers) ? null : helpers.getRelativePosition(e, chart);
		var meta = chart.getDatasetMeta(di);
		if (!pos || !meta || !chart.scales.x) return items;
		var xVal = chart.scales.x.getValueForPixel(pos.x);
		var idx = chart.data.datasets[di].data.findIndex(function (p) { return xVal >= p.x && xVal <= p.end; });
		if (idx < 0 || !meta.data[idx]) return items;
		return items.concat([{ element: meta.data[idx], datasetIndex: di, index: idx }]);
	};
}

function _el(tag, className, text) {
	var node = document.createElement(tag);
	if (className) node.className = className;
	if (text !== undefined) node.textContent = text;
	return node;
}

/** Draw a small CSS swatch whose shape mirrors the chart marker (not color-only). */
function _swatch(shape, color, isLine) {
	var s = _el('span', 'combined-swatch combined-swatch--' + (isLine ? 'line' : shape));
	s.style.setProperty('--swatch-color', color);
	s.setAttribute('aria-hidden', 'true');
	return s;
}

/** Style downtime boxes ("down*"), SLA lines ("sla*") and status strip ("status*") per theme. */
function _styleAnnotations(ann, palette) {
	Object.keys(ann || {}).forEach(function (k) {
		if (k.indexOf('down') === 0) {
			ann[k].backgroundColor = withAlpha(palette.down, 0.14);
			ann[k].borderColor = palette.down;
		} else if (k.indexOf('sla') === 0) {
			ann[k].borderColor = palette.sla;
			if (ann[k].label) {
				ann[k].label.backgroundColor = palette.sla;
				ann[k].label.color = palette.slaText;
			}
		} else if (k.indexOf('status') === 0 && ann[k]._cls) {
			ann[k].backgroundColor = palette[ann[k]._cls];
		}
	});
}

/** True while the "HTTP status" dataset is visible, so the strip follows its legend toggle. */
function _statusDatasetVisible(chart) {
	var i = chart.data.datasets.findIndex(function (d) { return d._kind === 'status'; });
	return i < 0 || chart.isDatasetVisible(i);
}

/**
 * Box annotations that fill the yStatus lane with one adjacent colored segment per bucket,
 * producing a gap-free categorical status strip (no interpolation across codes). Each box's
 * display is bound to the status dataset's visibility so the legend toggle hides the strip too.
 * @param {{x:number,end:number,cls:string}[]} buckets
 * @param {object} palette
 * @returns {object} keyed statusN annotations
 */
function _statusStripAnnotations(buckets, palette) {
	var ann = {};
	buckets.forEach(function (b, i) {
		ann['status' + i] = {
			type: 'box', xScaleID: 'x', yScaleID: 'yStatus',
			xMin: b.x, xMax: b.end, yMin: 0, yMax: 1,
			drawTime: 'beforeDatasetsDraw', backgroundColor: palette[b.cls], borderWidth: 0,
			display: function (ctx) { return _statusDatasetVisible(ctx.chart); },
			_cls: b.cls,
		};
	});
	return ann;
}

/** Status classes actually present in the given buckets, in severity order, for the legend key. */
function _presentStatusClasses(buckets) {
	return ['2xx', '3xx', '4xx', '5xx', 'err'].filter(function (k) {
		return buckets.some(function (b) { return b.cls === k; });
	});
}

var COMBINED_DASHES = [[], [6, 3], [2, 3], [8, 3, 2, 3]];

/** Re-apply theme colors to datasets, bands and legend swatches (called from applyChartTheme). */
function _themeCombined(chart, theme) {
	var palette = _combinedPalette(theme);
	chart._palette = palette;
	chart.data.datasets.forEach(function (ds) {
		if (ds._srcId) {
			ds.borderColor = palette[ds._srcId];
			ds.backgroundColor = createAreaGradient(palette[ds._srcId], 0.18);
		}
	});
	_styleAnnotations(chart.options.plugins.annotation && chart.options.plugins.annotation.annotations, palette);
	['y', 'yStatus'].forEach(function (id) {
		var sc = chart.options.scales[id];
		if (!sc) return;
		if (sc.title) sc.title.color = theme.muted;
		if (sc.border && id === 'yStatus') sc.border.color = theme.grid;
	});
	if (chart._renderLegend) chart._renderLegend();
}

/** Local date/time string, or UTC when the page is set to UTC display. */
function _fmtDateTime(ms) {
	var utc = typeof pageConfig !== 'undefined' && pageConfig.useUtc === true;
	return new Date(ms).toLocaleString(undefined, utc ? { timeZone: 'UTC' } : undefined);
}

function _statusTooltip(item) {
	var raw = item.raw;
	var cls = CombinedChartData.STATUS_CLASSES[raw.cls];
	if (raw.end) {
		var period = _fmtDateTime(raw.x) + ' – ' + _fmtDateTime(raw.end);
		var codes = Object.keys(raw.codeCounts || {}).sort().map(function (code) {
			return code + ' × ' + raw.codeCounts[code];
		}).join(', ');
		return [period, 'Worst: HTTP ' + Math.round(raw.code) + ' (' + cls.label + ')',
			raw.count + ' checks' + (codes ? ': ' + codes : '')];
	}
	if (raw.rollup) {
		var med = Math.round(raw.code * 10) / 10;
		return ['Median status code ' + med + ' (' + cls.label + ')', 'Rollup of an interval; individual codes are not kept'];
	}
	var base = raw.cls === 'err' ? 'No valid status code (' + Math.round(raw.code) + ')'
		: 'HTTP ' + Math.round(raw.code) + ' (' + cls.label + ')';
	return raw.count > 1 ? [base, 'Worst of ' + raw.count + ' checks in this interval'] : base;
}

/**
 * Render legend toggle buttons + status-class key.
 * Buttons use aria-pressed so state is exposed without relying on color.
 */
function _renderCombinedLegend(chart, legendEl, keyEl) {
	var palette = chart._palette;
	legendEl.textContent = '';
	chart.data.datasets.forEach(function (ds, i) {
		if (ds._kind === 'peak' && !ds.data.some(function (point) { return point.y !== null; })) return;
		var btn = _el('button', 'btn btn-sm combined-legend-btn');
		btn.type = 'button';
		btn.setAttribute('aria-pressed', chart.isDatasetVisible(i) ? 'true' : 'false');
		var color = ds._srcId ? palette[ds._srcId] : palette['2xx'];
		btn.appendChild(_swatch(ds._kind === 'status' ? 'circle' : 'line', color, ds._kind !== 'status'));
		btn.appendChild(_el('span', '', ds.label));
		btn.addEventListener('click', function () {
			chart.setDatasetVisibility(i, !chart.isDatasetVisible(i));
			chart.update('none');
			_renderCombinedLegend(chart, legendEl, keyEl);
		});
		legendEl.appendChild(btn);
	});
	if (chart._bandCount > 0) {
		var bandBtn = _el('button', 'btn btn-sm combined-legend-btn');
		bandBtn.type = 'button';
		bandBtn.setAttribute('aria-pressed', chart._bandsVisible ? 'true' : 'false');
		var sw = _swatch('band', palette.down, false);
		bandBtn.appendChild(sw);
		bandBtn.appendChild(_el('span', '', 'Downtime (' + chart._bandSourceLabel + ')'));
		bandBtn.addEventListener('click', function () {
			chart._bandsVisible = !chart._bandsVisible;
			var ann = chart.options.plugins.annotation.annotations;
			Object.keys(ann).forEach(function (k) {
				if (k.indexOf('down') === 0) ann[k].display = chart._bandsVisible;
			});
			chart.update('none');
			_renderCombinedLegend(chart, legendEl, keyEl);
		});
		legendEl.appendChild(bandBtn);
	}

	keyEl.textContent = '';
	(chart._presentClasses || []).forEach(function (key) {
		var cls = CombinedChartData.STATUS_CLASSES[key];
		var item = _el('span', 'combined-key-item');
		item.appendChild(_swatch(cls.shape, palette[key], false));
		item.appendChild(_el('span', '', cls.label));
		keyEl.appendChild(item);
	});
}

function _renderCombinedTable(tbody, sources, summaries, statusBuckets, bands) {
	tbody.textContent = '';
	function row(cells) {
		var tr = _el('tr');
		cells.forEach(function (c, i) {
			var cell = _el(i === 0 ? 'th' : 'td', '', c);
			if (i === 0) cell.scope = 'row';
			tr.appendChild(cell);
		});
		tbody.appendChild(tr);
	}
	sources.forEach(function (src) {
		var s = summaries[src.key];
		row([src.label, s.median === null ? 'no data' : 'min ' + Math.round(s.min) + ' / median '
			+ Math.round(s.median) + ' / max ' + Math.round(s.max) + ' ms']);
	});
	var counts = {};
	statusBuckets.forEach(function (b) { counts[b.cls] = (counts[b.cls] || 0) + b.count; });
	Object.keys(counts).sort().forEach(function (k) {
		row(['HTTP status ' + CombinedChartData.STATUS_CLASSES[k].label, counts[k] + ' samples']);
	});
	bands.slice(0, 20).forEach(function (b, i) {
		row(['Outage ' + (i + 1), _fmtDateTime(b.start) + ' to ' + _fmtDateTime(b.end)]);
	});
	if (bands.length > 20) row(['More outages', String(bands.length - 20) + ' not listed']);
}

function _summarizeSource(series, points) {
	var summary = CombinedChartData.summarize(series, [], []);
	var rawMax = Math.max.apply(null, points.map(function (p) { return p.max == null ? -Infinity : p.max; }));
	if (isFinite(rawMax)) summary.max = rawMax;
	return summary;
}

// Keep summary, aria-label and data table in sync with the data the chart currently shows.
function _renderCombinedText(canvas, input, view) {
	var D = CombinedChartData;
	var visibleSrc = input.sources.filter(function (s) { return view.summaries[s.key].median !== null; })[0];
	var visibleSummary = D.summarize(visibleSrc ? view.series[visibleSrc.key] : [], view.statusBuckets, view.bands);
	if (visibleSrc && view.summaries[visibleSrc.key]) {
		var sourceSummary = view.summaries[visibleSrc.key];
		visibleSummary.min = sourceSummary.min;
		visibleSummary.median = sourceSummary.median;
		visibleSummary.max = sourceSummary.max;
	}
	var text = D.describeSummary(visibleSrc ? visibleSrc.label : 'Response time', visibleSummary);
	var rollupNote = input.range.from.getTime() < Date.now() - 30 * 86400000
		? ' Data older than 30 days uses stored rollups; raw latency extremes and individual status codes are unavailable there.'
		: '';
	if (input.statusRawOnly) rollupNote += ' Status markers cover retained raw checks only.';
	if (view.zoomed) rollupNote += ' Figures describe the zoomed range.';
	canvas.setAttribute('role', 'img');
	canvas.setAttribute('aria-label', 'Response time and HTTP status over time. ' + text + '.');
	var summaryEl = document.getElementById('combined-summary');
	if (summaryEl) summaryEl.textContent = text + '.' + rollupNote;
	var tbody = document.getElementById('combined-table-body');
	if (tbody) _renderCombinedTable(tbody, input.sources, view.summaries, view.statusBuckets, view.bands);
}

function _createCombinedZoomLoader(chart, canvasId, input, datasets, fromMs, toMs, onView) {
	var original = datasets.map(function (d) { return d.data; });
	var originalAnnotations = Object.assign({}, chart.options.plugins.annotation.annotations);
	var originalBandCount = chart._bandCount;
	var originalPresentClasses = chart._presentClasses;
	var serial = 0;
	var controller = null;
	return function () {
		clearTimeout(chart._zoomTimer);
		serial += 1;
		if (controller) controller.abort();
		var current = serial;
		var start = Math.max(fromMs, chart.scales.x.min);
		var end = Math.min(toMs, chart.scales.x.max);
		if (end <= start || end - start >= (toMs - fromMs) * 0.95) {
			chart.data.datasets.forEach(function (d, i) { d.data = original[i]; });
			chart.options.plugins.annotation.annotations = Object.assign({}, originalAnnotations);
			chart._bandCount = originalBandCount;
			chart._presentClasses = originalPresentClasses;
			if (chart.options.scales.yPeak) {
				chart.options.scales.yPeak.display = original.some(function (data, i) {
					return datasets[i]._kind === 'peak' && data.some(function (point) { return point.y !== null; });
				});
			}
			chart.update('none');
			chart._renderLegend();
			if (onView) onView(null);
			return;
		}
		chart._zoomTimer = setTimeout(async function () {
			if (appState.charts.get(canvasId) !== chart) return;
			controller = new AbortController();
			var width = chart.chartArea?.width || chart.canvas.clientWidth || 900;
			var requests = input.sources.map(function (src) {
				var metric = src.key.indexOf('tcp:') === 0 ? 'tcp_ms_' + src.key.split(':')[1] : src.id + '_ms';
				return fetchDisplayMetricRange(pageConfig.targetId, metric, start, end, width, controller.signal);
			});
			if (input.statusEnabled) requests.push(fetchDisplayMetricRange(pageConfig.targetId, 'http_status', start, end, width, controller.signal));
			requests.push(fetchDisplayMetricRange(pageConfig.targetId, input.upSourceLabel.toLowerCase() + '_up', start, end, width, controller.signal));
			try {
				var results = await Promise.all(requests);
				if (current !== serial || appState.charts.get(canvasId) !== chart) return;
				var view = { summaries: {}, series: {}, statusBuckets: [], bands: [], zoomed: true };
				input.sources.forEach(function (src, i) {
					var result = results[i];
					var series = CombinedChartData.toGappedSeries(result.points, result.bucket_seconds * 3000);
					view.series[src.key] = series;
					view.summaries[src.key] = _summarizeSource(series, result.points);
					chart.data.datasets.forEach(function (d) {
						if (d._srcKey !== src.key) return;
						if (d._kind === 'ms') d.data = series;
						if (d._kind === 'peak') d.data = CombinedChartData.toPeakSeries(result.points);
					});
				});
				var ann = chart.options.plugins.annotation.annotations;
				var statusIndex = input.sources.length;
				if (input.statusEnabled) {
					var status = results[statusIndex];
					var buckets = CombinedChartData.bucketStatus(status.points, start, end, 1);
					view.statusBuckets = buckets;
					chart._presentClasses = _presentStatusClasses(buckets);
					var statusDataset = chart.data.datasets.find(function (d) { return d._kind === 'status'; });
					if (statusDataset) statusDataset.data = buckets.map(function (b) {
						return { x: b.x, y: 0.5, end: b.end, code: b.code, cls: b.cls,
							count: b.count, codeCounts: b.codeCounts, rollup: false };
					});
					Object.keys(ann).forEach(function (key) { if (key.indexOf('status') === 0) delete ann[key]; });
					Object.assign(ann, _statusStripAnnotations(buckets, chart._palette));
					statusIndex += 1;
				}
				var upResult = results[statusIndex];
				var bands = CombinedChartData.buildDownBands(upResult.points, upResult.bucket_seconds * 1000);
				Object.keys(ann).forEach(function (key) { if (key.indexOf('down') === 0) delete ann[key]; });
				bands.forEach(function (band, i) {
					ann['down' + i] = { type: 'box', xMin: band.start, xMax: band.end,
						drawTime: 'beforeDatasetsDraw', backgroundColor: withAlpha(chart._palette.down, 0.14),
						borderColor: chart._palette.down, borderWidth: 1, borderDash: [4, 3] };
				});
				chart._bandCount = bands.length;
				view.bands = bands;
				if (chart.options.scales.yPeak) {
					chart.options.scales.yPeak.display = chart.data.datasets.some(function (d) {
						return d._kind === 'peak' && d.data.some(function (point) { return point.y !== null; });
					});
				}
				chart.update('none');
				chart._renderLegend();
				if (onView) onView(view);
			} catch (error) {
				if (error.name !== 'AbortError') console.warn('Could not load zoom detail:', error);
			}
		}, 300);
	};
}

function _configureCombinedAxes(options, width) {
	var compact = width < 500;
	options.layout.padding.right = compact ? 4 : 12;
	options.layout.padding.left = compact ? 2 : 6;
	options.scales.x.ticks.maxTicksLimit = compact ? 3 : undefined;
	options.scales.x.ticks.font.size = compact ? 10 : 12;
	options.scales.y.ticks.maxTicksLimit = compact ? 4 : undefined;
	options.scales.y.ticks.font.size = compact ? 10 : 12;
	options.scales.y.ticks.padding = compact ? 3 : 8;
	options.scales.y.title.display = !compact;
	if (options.scales.yPeak) {
		options.scales.yPeak.ticks.maxTicksLimit = compact ? 4 : undefined;
		options.scales.yPeak.ticks.font = { size: compact ? 10 : 12 };
		options.scales.yPeak.ticks.padding = compact ? 3 : 8;
		options.scales.yPeak.title.display = !compact;
	}
	if (options.scales.yStatus) options.scales.yStatus.title.display = !compact;
}

/**
 * Build or rebuild the combined chart.
 * @param {string} canvasId
 * @param {object} input - { sources:[{key,id,label,points,error,sla,dash}], statusPoints, statusError, upPoints,
 *   upSourceLabel, statusEnabled, range:{from:Date,to:Date} }
 */
function buildCombinedChart(canvasId, input) {
	var canvas = document.getElementById(canvasId);
	if (!canvas) return;
	_destroyExisting(canvasId);

	var D = window.CombinedChartData;
	var theme = appState.theme;
	var palette = _combinedPalette(theme);
	var emptyEl = document.getElementById('combined-empty');
	var plotEl = document.getElementById('combined-plot');
	var legendEl = document.getElementById('combined-legend');
	var keyEl = document.getElementById('combined-status-key');
	var summaryEl = document.getElementById('combined-summary');
	var detailsEl = document.getElementById('combined-data-details');

	var fromMs = input.range.from.getTime();
	var toMs = input.range.to.getTime();
	var statusBuckets = D.bucketStatus(input.statusPoints, fromMs, toMs,
		Math.max(1, Math.ceil((toMs - fromMs) / (1000 * (input.statusBucketSeconds || 1800)))));
	var bands = D.buildDownBands(input.upPoints, input.upBucketSeconds ? input.upBucketSeconds * 1000 : undefined);

	var datasets = [];
	var summaries = {};
	var fullSeries = {};
	var firstWithData = true;
	var slaLines = [];
	input.sources.forEach(function (src) {
		var series = D.toGappedSeries(src.points, src.bucketSeconds ? src.bucketSeconds * 3000 : undefined);
		summaries[src.key] = _summarizeSource(series, src.points);
		fullSeries[src.key] = series;
		if (!series.length) return;
		if (src.sla) slaLines.push({ index: datasets.length, sla: src.sla });
		datasets.push({
			label: src.label, data: series, _kind: 'ms', _srcId: src.id, _srcKey: src.key, yAxisID: 'y', borderDash: COMBINED_DASHES[(src.dash || 0) % 4],
			borderColor: palette[src.id], backgroundColor: createAreaGradient(palette[src.id], 0.18),
			fill: true, spanGaps: false, tension: 0, borderWidth: 2, pointRadius: 0, pointHitRadius: 3,
			hidden: !firstWithData,
		});
		var peaks = D.toPeakSeries(src.points);
		datasets.push({
				label: src.label + ' peak response time', data: peaks, _kind: 'peak', _srcId: src.id, _srcKey: src.key,
				yAxisID: 'yPeak', type: 'line', showLine: true, spanGaps: false, tension: 0,
				borderColor: palette[src.id], borderDash: [4, 3], borderWidth: 1.5, fill: false,
				pointRadius: function (context) { return context.raw && context.raw.outlier ? 4 : 0; },
				pointHoverRadius: 6, pointHitRadius: 4,
				pointBackgroundColor: palette[src.id], pointBorderColor: palette[src.id], hidden: !firstWithData,
			});
		firstWithData = false;
	});
	if (statusBuckets.length) {
		// The colored strip is drawn as box annotations (_statusStripAnnotations); this dataset
		// keeps invisible centered points so _statusTooltip still fires on direct hover.
		datasets.push({
			label: 'HTTP status', _kind: 'status', yAxisID: 'yStatus', type: 'line', showLine: false,
			data: statusBuckets.map(function (b) { return { x: b.x, y: 0.5, end: b.end, code: b.code, cls: b.cls, count: b.count, codeCounts: b.codeCounts, rollup: b.rollup }; }),
			pointRadius: 0, pointHoverRadius: 0, pointHitRadius: 8,
		});
	}

	var hasMs = datasets.some(function (d) { return d._kind === 'ms'; });
	var hasAny = hasMs || statusBuckets.length > 0;
	var allFailed = input.sources.length > 0 && input.sources.every(function (s) { return s.error; });

	// Visible empty / error state instead of an empty canvas.
	if (!hasAny) {
		if (plotEl) plotEl.hidden = true;
		if (legendEl) legendEl.textContent = '';
		if (keyEl) keyEl.textContent = '';
		if (detailsEl) detailsEl.hidden = true;
		if (emptyEl) {
			emptyEl.hidden = false;
			emptyEl.querySelector('[data-role="message"]').textContent = allFailed
				? 'The measurements could not be loaded. Reload the page or try again later.'
				: 'No measurements in the selected range. Pick a longer range or wait for the next check.';
		}
		if (summaryEl) summaryEl.textContent = 'No data to display.';
		var staleNote = document.getElementById('combined-status-note');
		if (staleNote) staleNote.textContent = '';
		canvas.setAttribute('aria-label', 'Response time and HTTP status over time. No data to display.');
		return;
	}
	if (plotEl) plotEl.hidden = false;
	if (emptyEl) emptyEl.hidden = true;
	if (detailsEl) detailsEl.hidden = false;

	var zoomOpts = createZoomOptions(function (ch) { if (ch._reloadVisible) ch._reloadVisible(); });
	var config = createLineConfig(theme, zoomOpts, _getVisibleRangeMs);
	config.data = { datasets: datasets };
	var o = config.options;
	o.animation = _prefersReducedMotion() ? false : { duration: 250 };
	// ms/peak points keep direct point+intersect hover; the custom mode adds whole-width
	// reach for the status strip, whose colored segments span [x, end] but have points only at x.
	_registerCombinedInteraction();
	o.interaction = { mode: COMBINED_INTERACTION_MODE, intersect: true };
	o.scales.x.min = fromMs;
	o.scales.x.max = toMs;
	o.scales.y.stack = 'combined';
	o.scales.y.stackWeight = 11;
	o.scales.y.title = { display: true, text: 'Response time (ms)', color: theme.muted };
	o.scales.y.ticks.callback = function (v) { return Number.isInteger(v) ? formatCompactAxisValue(v) : ''; };
	// Keep a visible SLA line in view even when every measurement is far below it.
	o.scales.y.afterDataLimits = function (scale) {
		slaLines.forEach(function (l) {
			if (!scale.chart.isDatasetVisible(l.index)) return;
			var top = l.sla.value * 1.1;
			if (top > scale.max) scale.max = top;
		});
	};
	if (datasets.some(function (d) { return d._kind === 'peak'; })) {
		o.scales.yPeak = { type: 'linear', position: 'right',
			display: datasets.some(function (d) { return d._kind === 'peak' && d.data.some(function (point) { return point.y !== null; }); }), beginAtZero: true,
			grid: { drawOnChartArea: false }, ticks: { color: theme.muted, callback: formatCompactAxisValue },
			title: { display: true, text: 'Max response time (ms)', color: theme.muted } };
	}
	if (statusBuckets.length) {
		o.scales.yStatus = {
			type: 'linear', position: 'left', stack: 'combined', stackWeight: 1, min: 0, max: 1,
			grid: { display: false }, border: { display: true, color: theme.grid },
			ticks: { display: false }, title: { display: true, text: 'Status', color: theme.muted },
		};
	}
	_configureCombinedAxes(o, canvas.clientWidth || 900);
	o.onResize = function (chart, size) { _configureCombinedAxes(chart.options, size.width); };
	o.plugins.legend = { display: false };
	o.plugins.tooltip.callbacks.label = function (item) {
		if (item.dataset._kind === 'status') return _statusTooltip(item);
		if (item.dataset._kind === 'peak') {
			var peakLabel = ' Peak response time: ' + Math.round(item.parsed.y) + ' ms';
			if (item.raw.median !== null) peakLabel += ' (median ' + Math.round(item.raw.median) + ' ms)';
			return peakLabel + ' across ' + item.raw.count + ' stored value' + (item.raw.count === 1 ? '' : 's');
		}
		return ' ' + item.dataset.label + ': ' + Math.round(item.parsed.y) + ' ms';
	};

	var annotations = {};
	bands.forEach(function (b, i) {
		annotations['down' + i] = {
			type: 'box', xMin: b.start, xMax: b.end, drawTime: 'beforeDatasetsDraw',
			backgroundColor: withAlpha(palette.down, 0.14), borderColor: palette.down,
			borderWidth: 1, borderDash: [4, 3],
		};
	});
	slaLines.forEach(function (l) {
		var a = createSlaAnnotation({ enabled: true }, l.sla.value, l.sla.label);
		if (!a) return;
		a.display = function (c) { return c.chart.isDatasetVisible(l.index); };
		annotations['sla' + l.index] = a;
	});
	if (statusBuckets.length) Object.assign(annotations, _statusStripAnnotations(statusBuckets, palette));
	_styleAnnotations(annotations, palette);
	o.plugins.annotation = { annotations: annotations };

	var chart = new Chart(canvas, config);
	chart._palette = palette;
	chart._skipSampling = true;
	chart._bandCount = bands.length;
	chart._bandsVisible = true;
	chart._bandSourceLabel = input.upSourceLabel || 'primary check';
	chart._presentClasses = _presentStatusClasses(statusBuckets);
	chart._renderLegend = function () { _renderCombinedLegend(chart, legendEl, keyEl); };
	chart._themeHook = function (t) { _themeCombined(chart, t); };
	var fullView = { summaries: summaries, series: fullSeries, statusBuckets: statusBuckets, bands: bands, zoomed: false };
	chart._reloadVisible = _createCombinedZoomLoader(chart, canvasId, input, datasets, fromMs, toMs,
		function (view) { _renderCombinedText(canvas, input, view || fullView); });
	_registerChart(canvasId, chart, canvas, true);
	chart._renderLegend();

	_renderCombinedText(canvas, input, fullView);
	var noteEl = document.getElementById('combined-status-note');
	if (noteEl) {
		noteEl.textContent = !input.statusEnabled ? 'No HTTP status codes: the HTTP check is not enabled.'
			: (input.statusError ? 'HTTP status codes could not be loaded.'
				: (!statusBuckets.length ? 'No HTTP status codes recorded in this range.' : ''));
		if (input.upError) noteEl.textContent += ' Downtime bands could not be loaded.';
	}
}

window.buildCombinedChart = buildCombinedChart;
