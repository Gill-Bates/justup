//
// app/static/js/dashboard-widgets.js
// Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
//

// Frozen registry of the dashboard widgets: the single client-side source of ids, labels and the
// factory layout. The server allowlist (DASHBOARD_WIDGETS in app/api/dashboard_layout.py) must list
// exactly these ids; tests/test_dashboard_layout.py keeps both in sync.
(function () {
	'use strict';

	const def = (id, label, x, y, w, h, minW, minH) => Object.freeze({
		id, label, defaultX: x, defaultY: y, defaultSize: Object.freeze({ w, h }),
		minW, minH, defaultVisible: true,
	});

	window.JuDashboardRegistry = Object.freeze({
		registry: Object.freeze([
			def('status-online', 'Online', 0, 0, 3, 2, 1, 2),
			def('status-offline', 'Offline', 3, 0, 3, 2, 1, 2),
			def('status-disabled', 'Disabled', 6, 0, 3, 2, 1, 2),
			def('status-total', 'Total', 9, 0, 3, 2, 1, 2),
			def('quality', 'Monitor quality', 0, 2, 3, 4, 2, 2),
			def('downtime', 'Downtime overview (24h)', 3, 2, 6, 4, 3, 3),
		]),
	});
})();
