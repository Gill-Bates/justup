//
// app/static/js/utils/settings-tabs.js
// Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
//

/**
 * Persists the active settings tab in the URL hash (replaceState, no history
 * entries) with a localStorage fallback, and restores it synchronously while
 * the page is parsed so the default tab never flashes.
 *
 * Classic script (not a module) so it can run before first paint.
 */
(function (root) {
	'use strict';

	var STORAGE_KEY = 'ju-settings-tab';
	var FALLBACK_ID = 'general';

	/** Normalizes '#Network' / 'network' to 'network'. */
	function parseHash(hash) {
		return String(hash || '').replace(/^#/, '').trim().toLowerCase();
	}

	/** Picks hash, then stored value, then the fallback; only ids in `available` qualify. */
	function resolveTabId(hash, stored, available) {
		var candidates = [parseHash(hash), parseHash(stored)];
		for (var i = 0; i < candidates.length; i++) {
			if (candidates[i] && available.indexOf(candidates[i]) !== -1) return candidates[i];
		}
		return available.indexOf(FALLBACK_ID) !== -1 ? FALLBACK_ID : (available[0] || null);
	}

	function init(win) {
		var doc = win.document;
		var strip = doc.getElementById('settingsTabs');
		if (!strip) return;
		var buttons = Array.prototype.slice.call(strip.querySelectorAll('[role="tab"][data-bs-target]'));
		var available = buttons.map(function (b) { return b.id.replace(/-tab$/, ''); });
		if (!available.length) return;

		function readStored() {
			try { return win.localStorage.getItem(STORAGE_KEY); } catch (e) { return null; }
		}

		function persist(id) {
			try { win.localStorage.setItem(STORAGE_KEY, id); } catch (e) { /* storage unavailable */ }
			if (parseHash(win.location.hash) !== id) {
				win.history.replaceState(win.history.state, '', win.location.pathname + win.location.search + '#' + id);
			}
		}

		// Direct class switch without Bootstrap's fade, for the pre-paint restore.
		function applyInstantly(id) {
			buttons.forEach(function (btn) {
				var on = btn.id === id + '-tab';
				btn.classList.toggle('active', on);
				btn.setAttribute('aria-selected', on ? 'true' : 'false');
				btn.setAttribute('tabindex', on ? '0' : '-1');
				var pane = doc.querySelector(btn.getAttribute('data-bs-target'));
				if (pane) {
					pane.classList.toggle('active', on);
					pane.classList.toggle('show', on);
				}
			});
		}

		function activeId() {
			var btn = strip.querySelector('[role="tab"].active');
			return btn ? btn.id.replace(/-tab$/, '') : null;
		}

		function show(id) {
			if (id === activeId()) return;
			var btn = doc.getElementById(id + '-tab');
			if (btn && win.bootstrap && win.bootstrap.Tab) win.bootstrap.Tab.getOrCreateInstance(btn).show();
			else applyInstantly(id);
		}

		var initial = resolveTabId(win.location.hash, readStored(), available);
		applyInstantly(initial);
		persist(initial);

		// htmx 1.x 'revealed' also fires for display:none panes, so panes lazy-load on this event.
		function announce(id) {
			var pane = doc.getElementById(id + '-pane');
			if (pane) pane.dispatchEvent(new win.CustomEvent('settings-tab-shown'));
		}

		strip.addEventListener('shown.bs.tab', function (e) {
			var id = e.target.id.replace(/-tab$/, '');
			persist(id);
			announce(id);
		});
		// htmx binds its triggers on DOMContentLoaded, which has run its handler first.
		doc.addEventListener('DOMContentLoaded', function () { announce(initial); });
		win.addEventListener('hashchange', function () {
			var id = parseHash(win.location.hash);
			if (available.indexOf(id) !== -1) show(id);
			else persist(activeId() || initial); // unknown hash: restore the active tab's hash
		});
	}

	var api = { STORAGE_KEY: STORAGE_KEY, parseHash: parseHash, resolveTabId: resolveTabId, init: init };
	root.SettingsTabs = api;
	if (root.document) init(root);
})(typeof window !== 'undefined' ? window : globalThis);
