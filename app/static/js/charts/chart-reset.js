//
// app/static/js/charts/chart-reset.js
// Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
//

// One reset for every chart: double-click and the "Reset zoom" button both call resetChartView().
// It restores the scale limits and data captured at build time instead of trusting the zoom
// plugin's own bookkeeping, which does not know about the data swapped in by zoom detail loads.
(function (root) {
	'use strict';

	// Records the limits each scale was built with (undefined means automatic).
	function captureLoadView(chart) {
		if (!chart || !chart.options || !chart.options.scales) return;
		const limits = {};
		Object.keys(chart.options.scales).forEach(function (id) {
			const scale = chart.options.scales[id];
			limits[id] = { min: scale.min, max: scale.max };
		});
		chart._loadScaleLimits = limits;
	}

	function resetChartView(chart) {
		if (!chart || !chart.options || !chart.options.scales || !chart._loadScaleLimits) return;
		// A detail request still in flight would otherwise repaint the zoomed range after the reset.
		if (typeof chart._cancelZoomLoader === 'function') chart._cancelZoomLoader();
		if (typeof chart.resetZoom === 'function') chart.resetZoom('none');
		Object.keys(chart._loadScaleLimits).forEach(function (id) {
			const scale = chart.options.scales[id];
			const limit = chart._loadScaleLimits[id];
			if (!scale) return;
			['min', 'max'].forEach(function (key) {
				if (limit[key] === undefined) delete scale[key];
				else scale[key] = limit[key];
			});
		});
		// Only the load data may end up on screen; the combined chart also restores bands and peaks.
		if (typeof chart._restoreLoadView === 'function') chart._restoreLoadView();
		else if (typeof chart._reloadVisible === 'function') chart._reloadVisible();
		chart.update('none');
	}

	root.JuChartReset = { captureLoadView: captureLoadView, resetChartView: resetChartView };
})(typeof window !== 'undefined' ? window : globalThis);
