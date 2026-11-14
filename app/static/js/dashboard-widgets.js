//
// app/static/js/dashboard-widgets.js
// Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
//

// Frozen registry of the dashboard widgets: the single client-side source of ids, labels and the
// factory layout. The server allowlist (DASHBOARD_WIDGETS in app/api/dashboard_layout.py) must list
// exactly these ids; tests/test_dashboard_layout.py keeps both in sync.
(function () {
	'use strict';

	const def = (id, label, x, y, w, h, minW, minH, defaultVisible = true, adminOnly = false) => Object.freeze({
		id, label, defaultX: x, defaultY: y, defaultSize: Object.freeze({ w, h }),
		minW, minH, defaultVisible, adminOnly,
	});

	window.JuDashboardRegistry = Object.freeze({
		registry: Object.freeze([
			def('status-online', 'Online', 0, 0, 3, 2, 2, 2),
			def('status-offline', 'Offline', 3, 0, 3, 2, 2, 2),
			def('status-disabled', 'Disabled', 6, 0, 3, 2, 2, 2),
			def('status-total', 'Total', 9, 0, 3, 2, 2, 2),
			def('quality', 'Monitor quality', 0, 2, 6, 3, 2, 3),
			def('downtime', 'Downtime overview (24h)', 0, 5, 12, 4, 3, 3),
			def('alerts-open', 'Alerts', 6, 2, 6, 3, 3, 3),
			def('delivery-queue', 'Delivery queue', 4, 2, 4, 3, 4, 3, true, true),
			def('uptime-day', 'Uptime (24h)', 0, 10, 3, 2, 3, 2, false),
			def('cert-expiry', 'Certificate expiry', 3, 10, 3, 4, 3, 3, false),
			def('slowest-monitors', 'Slowest monitors (latest)', 0, 14, 6, 4, 4, 3, false),
			def('quality-trend', 'Quality trend (24h)', 6, 14, 6, 4, 4, 3, false),
			def('stale-checks', 'Stale checks', 6, 10, 3, 4, 3, 3, false),
		]),
		// Factory arrangement: with the admin-only delivery queue the middle row holds three equal cards
		// (quality, queue, alerts); without it quality and alerts share the row, the downtime table spans the page.
		forRole(isAdmin) {
			const adminRow = {
				quality: { defaultX: 0, defaultY: 2, defaultSize: { w: 4, h: 3 } },
				'alerts-open': { defaultX: 8, defaultY: 2, defaultSize: { w: 4, h: 3 } },
			};
			return this.registry.filter((entry) => !entry.adminOnly || isAdmin).map((entry) => (
				isAdmin && adminRow[entry.id] ? { ...entry, ...adminRow[entry.id] } : entry
			));
		},
	});
})();
