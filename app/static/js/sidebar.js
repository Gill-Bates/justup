//
// app/static/js/sidebar.js
// Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
//

/**
 * Sidebar behavior: desktop rail mode, mobile off-canvas drawer and disclosure groups.
 * The toggle is a button whose aria-expanded always mirrors the state of the current viewport.
 */
(function () {
	'use strict';

	var root = document.documentElement;
	var toggle = document.getElementById('sidebar-toggle');
	var sidebar = document.getElementById('app-sidebar');
	if (!toggle || !sidebar) return;

	var backdrop = document.getElementById('sidebar-backdrop');
	// Keep in sync with the 991.98px breakpoint in gentelella-layout.css and dashboard-grid.js.
	var mobile = window.matchMedia('(max-width: 991.98px)');
	var STORAGE_KEY = 'ju-sidebar';

	function isOpen() {
		return mobile.matches ? root.classList.contains('ju-nav-open') : !root.classList.contains('ju-nav-sm');
	}

	function sync() {
		toggle.setAttribute('aria-expanded', isOpen() ? 'true' : 'false');
	}

	function closeDrawer(returnFocus) {
		root.classList.remove('ju-nav-open');
		sync();
		if (returnFocus) toggle.focus();
	}

	toggle.addEventListener('click', function () {
		if (mobile.matches) {
			var open = !root.classList.contains('ju-nav-open');
			root.classList.toggle('ju-nav-open', open);
			sync();
			if (open) {
				var first = sidebar.querySelector('a[href], button');
				if (first) first.focus();
			}
			return;
		}
		var collapsed = root.classList.toggle('ju-nav-sm');
		try { localStorage.setItem(STORAGE_KEY, collapsed ? 'collapsed' : 'expanded'); } catch (e) { /* storage blocked */ }
		sync();
	});

	if (backdrop) backdrop.addEventListener('click', function () { closeDrawer(true); });

	var closeBtn = document.getElementById('sidebar-close');
	if (closeBtn) closeBtn.addEventListener('click', function () { closeDrawer(true); });

	document.addEventListener('keydown', function (evt) {
		if (evt.key === 'Escape' && mobile.matches && root.classList.contains('ju-nav-open')) {
			closeDrawer(true);
		}
	});

	// The drawer has no focus trap (disclosure pattern); tabbing out of it closes it.
	sidebar.addEventListener('focusout', function (evt) {
		if (!mobile.matches || !root.classList.contains('ju-nav-open')) return;
		var next = evt.relatedTarget;
		if (next && !sidebar.contains(next) && next !== toggle) closeDrawer(false);
	});

	// Leaving the mobile layout must not leave the drawer state behind.
	mobile.addEventListener('change', function () {
		root.classList.remove('ju-nav-open');
		sync();
	});

	sidebar.addEventListener('click', function (evt) {
		var link = evt.target.closest('a[href]');
		if (link && mobile.matches) root.classList.remove('ju-nav-open');
	});

	// Disclosure groups: a button with aria-expanded shows or hides the controlled list.
	sidebar.querySelectorAll('.ju-nav-group-toggle').forEach(function (btn) {
		var panel = document.getElementById(btn.getAttribute('aria-controls'));
		if (!panel) return;
		btn.addEventListener('click', function () {
			var expand = btn.getAttribute('aria-expanded') !== 'true';
			btn.setAttribute('aria-expanded', expand ? 'true' : 'false');
			panel.hidden = !expand;
		});
	});

	sync();
})();
