//
// app/static/js/utils/tabs-overflow.js
// Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
//
// Overflow behaviour of every shared tab strip (.ju-tabs, DESIGN.md "Tabs"): marks which edges can still be
// scrolled (CSS fades them) and keeps the active tab inside the visible part. It never changes the look.
(function () {
	'use strict';
	const EDGE_SLOP_PX = 1;
	const reduceMotion = window.matchMedia('(prefers-reduced-motion: reduce)');

	function bind(strip) {
		if (strip.dataset.tabsBound === '1') return;
		strip.dataset.tabsBound = '1';
		const update = () => {
			const max = strip.scrollWidth - strip.clientWidth;
			strip.classList.toggle('has-overflow', max > EDGE_SLOP_PX);
			strip.classList.toggle('is-at-start', strip.scrollLeft <= EDGE_SLOP_PX);
			strip.classList.toggle('is-at-end', strip.scrollLeft >= max - EDGE_SLOP_PX);
		};
		// Scroll only the strip (never the page) so the tab is fully visible and centred.
		const reveal = (tab, smooth) => {
			if (!tab) return;
			const left = tab.offsetLeft - (strip.clientWidth - tab.offsetWidth) / 2;
			strip.scrollTo({ left: Math.max(0, left), behavior: smooth && !reduceMotion.matches ? 'smooth' : 'auto' });
		};
		strip.addEventListener('scroll', update, { passive: true });
		window.addEventListener('resize', update);
		strip.addEventListener('shown.bs.tab', (e) => reveal(e.target, true));
		// Keyboard focus only: scrolling on a mouse press moves the tab away before mouseup and the click is lost.
		strip.addEventListener('focusin', (e) => {
			if (e.target.matches?.('.nav-link:focus-visible')) reveal(e.target, true);
		});
		reveal(strip.querySelector('.nav-link.active'), false);
		update();
	}

	function init() {
		document.querySelectorAll('.ju-tabs').forEach(bind);
	}

	if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init);
	else init();
})();
