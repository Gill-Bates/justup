//
// app/static/js/notifications.js
// Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
//

/**
 * Topbar bell: unread badge, short shake on new events, preview popover and polling.
 * The pure helpers and the poller are exposed as window.JustUpNotifications for tests.
 */
(function (root) {
	'use strict';

	var POLL_MS = 15000;
	var MAX_BACKOFF_MS = 120000;

	var CATEGORY_ICONS = {
		monitoring: 'monitor_heart', reports: 'picture_as_pdf', delivery: 'forward_to_inbox',
		security: 'shield', activity: 'history'
	};

	function categoryIcon(category) {
		return CATEGORY_ICONS[category] || 'notifications';
	}

	// Second line: what the event says about its object/actor, then when. Never prints null/undefined.
	function detailLine(n, when) {
		var context = (n && (n.body || n.actor)) || '';
		return [String(context).trim(), when || ''].filter(Boolean).join(' · ');
	}

	function formatBadge(count) {
		var n = Math.max(0, parseInt(count, 10) || 0);
		if (n === 0) return '';
		return n > 99 ? '99+' : String(n);
	}

	function ariaLabel(count) {
		var n = Math.max(0, parseInt(count, 10) || 0);
		return n === 0 ? 'Notifications' : 'Notifications, ' + n + ' unread';
	}

	function bindSwipeRows(container) {
		if (!container || container.dataset.swipeBound) return;
		container.dataset.swipeBound = '1';
		var start = null;
		var openItem = null;
		var swallowClick = false;

		function closeOpenItem() {
			if (openItem) openItem.classList.remove('is-dismiss-revealed');
			openItem = null;
		}

		function reveal(item) {
			if (openItem && openItem !== item) closeOpenItem();
			item.classList.add('is-dismiss-revealed');
			openItem = item;
		}

		container.addEventListener('pointerdown', function (event) {
			var target = event.target;
			var item = target && target.closest && target.closest('.ju-notif-swipe-item');
			if (!item || !container.contains(item) || target.closest('.ju-notif-dismiss-button')) {
				start = null;
				return;
			}
			start = { x: event.clientX, y: event.clientY, id: event.pointerId, item: item };
		}, { passive: true });

		container.addEventListener('pointerup', function (event) {
			if (!start || start.id !== event.pointerId) return;
			var dx = event.clientX - start.x;
			var dy = event.clientY - start.y;
			if (Math.abs(dx) >= 48 && Math.abs(dx) > Math.abs(dy) * 1.2) {
				if (dx < 0) reveal(start.item);
				else if (openItem === start.item) closeOpenItem();
				swallowClick = true;
				root.setTimeout(function () { swallowClick = false; }, 500);
			}
			start = null;
		}, { passive: true });

		container.addEventListener('pointercancel', function () { start = null; }, { passive: true });
		container.addEventListener('focusin', function (event) {
			var dismiss = event.target.closest('.ju-notif-dismiss-button');
			var item = dismiss && dismiss.closest('.ju-notif-swipe-item');
			if (item) reveal(item);
		});
		container.addEventListener('focusout', function (event) {
			var item = event.target.closest('.ju-notif-swipe-item');
			if (item && openItem === item && !item.contains(event.relatedTarget)) closeOpenItem();
		});
		var clickRoot = root.document && root.document.addEventListener ? root.document : container;
		clickRoot.addEventListener('click', function (event) {
			var target = event.target;
			// An outside click only closes the open row; it is not cancelled so the user's real target still acts
			if (openItem && !openItem.contains(target)) closeOpenItem();
			if (!swallowClick || !target || !target.closest || !target.closest('.ju-notif-swipe-content')) return;
			event.preventDefault();
			event.stopPropagation();
			swallowClick = false;
		}, true);
	}

	// unread_event_seq rises for a new notification and for a regrouped delivery re-surfacing on its
	// existing id, which latest_unread_id (max id) misses; fall back to the id when seq is absent.
	function shouldShake(prev, next) {
		if (!prev || !next || !(next.unread > 0)) return false;
		if (typeof next.unread_event_seq === 'number' && typeof prev.unread_event_seq === 'number') {
			return next.unread_event_seq > prev.unread_event_seq;
		}
		return next.latest_unread_id > prev.latest_unread_id;
	}

	/**
	 * Sequential poller: no overlapping requests, stale responses are dropped, paused while hidden,
	 * exponential backoff on errors and a permanent stop when authentication is gone.
	 * With opts.isSuppressed() true (healthy live channel) it only fetches on explicit refresh().
	 */
	function createPoller(opts) {
		var timer = null;
		var inFlight = false;
		var stopped = false;
		var failures = 0;
		var generation = 0;
		var queued = false;

		function delay() {
			return failures === 0 ? opts.intervalMs : Math.min(opts.intervalMs * Math.pow(2, failures), MAX_BACKOFF_MS);
		}
		function schedule() {
			if (stopped || timer !== null || opts.isHidden() || (opts.isSuppressed && opts.isSuppressed())) return;
			timer = opts.setTimer(function () { timer = null; tick(); }, delay());
		}
		function clear() {
			if (timer !== null) { opts.clearTimer(timer); timer = null; }
		}
		function tick() {
			if (stopped || inFlight) return Promise.resolve();
			clear();
			inFlight = true;
			var mine = ++generation;
			return Promise.resolve().then(function () { return opts.fetchSummary(); }).then(function (res) {
				if (stopped || mine !== generation) return;
				if (res && (res.status === 401 || res.status === 403)) { stopped = true; clear(); opts.onAuthLost(); return; }
				if (!res || !res.ok) { failures++; return; }
				failures = 0;
				return res.json().then(function (data) { if (!stopped && mine === generation) opts.onData(data); });
			}).catch(function () { failures++; }).then(function () {
				inFlight = false;
				if (queued) { queued = false; return tick(); }
				schedule();
			});
		}
		return {
			start: function () { return tick(); },
			// Immediate refresh after a user action or when the tab becomes visible again.
			refresh: function () {
				clear();
				// Never overlap: a running request becomes stale and one fresh request follows it.
				if (inFlight) { generation++; queued = true; return Promise.resolve(); }
				return tick();
			},
			onVisibility: function () { if (opts.isHidden()) clear(); else this.refresh(); },
			// The live channel took over: drop the pending poll timer.
			suspend: function () { clear(); },
			stop: function () { stopped = true; clear(); },
			state: function () { return { failures: failures, inFlight: inFlight, stopped: stopped, scheduled: timer !== null }; }
		};
	}

	root.JustUpNotifications = {
		POLL_MS: POLL_MS,
		categoryIcon: categoryIcon,
		detailLine: detailLine,
		formatBadge: formatBadge,
		ariaLabel: ariaLabel,
		shouldShake: shouldShake,
		createPoller: createPoller,
		renderPreview: renderPreview,
		bindSwipeRows: bindSwipeRows,
	};

	if (!root.document || !root.document.getElementById) return;
	var doc = root.document;
	var bell = doc.getElementById('notif-bell');
	if (!bell) return;

	var badge = doc.getElementById('notif-badge');
	var pop = doc.getElementById('notif-popover');
	var list = doc.getElementById('notif-pop-list');
	var count = doc.getElementById('notif-pop-count');
	var live = doc.getElementById('notif-live');
	var readAll = doc.getElementById('notif-read-all');
	var last = null;
	var poller = null;
	var renderingPreview = false;
	var liveClient = root.JustUpLive && root.JustUpLive.client;

	function reducedMotion() {
		return !!(root.matchMedia && root.matchMedia('(prefers-reduced-motion: reduce)').matches);
	}

	function shake() {
		if (reducedMotion()) return;
		bell.classList.remove('is-shaking');
		void bell.offsetWidth; // restart the animation
		bell.classList.add('is-shaking');
		root.setTimeout(function () { bell.classList.remove('is-shaking'); }, 800);
	}

	function render(data) {
		var text = formatBadge(data.unread);
		badge.textContent = text;
		badge.hidden = text === '';
		bell.setAttribute('aria-label', ariaLabel(data.unread));
		count.textContent = data.unread ? data.unread + ' unread' : 'All caught up';
		readAll.disabled = data.unread === 0;
		renderPreview(list, data, doc, bell);
		bindSwipeRows(list);
	}

	function renderPreview(previewList, data, documentRef, fallbackFocus) {
		var items = data.items.slice(0, 5);
		var existing = Object.create(null);
		Array.prototype.slice.call(previewList.children).forEach(function (li) {
			var button = li.querySelector('.ju-notif-pop-btn');
			if (button) existing[button.dataset.id] = li;
		});

		var desired = [];
		items.forEach(function (n) {
			var id = String(n.id);
			var li = existing[id] || documentRef.createElement('li');
			li.className = 'ju-notif-pop-item ju-notif-swipe-item ju-notif-' + n.severity + (n.is_read ? '' : ' is-unread');
			var button = li.querySelector('.ju-notif-pop-btn');
			if (!button) {
				button = documentRef.createElement('button');
				button.type = 'button';
				button.className = 'ju-notif-pop-btn ju-notif-swipe-content';
				var icon = documentRef.createElement('span');
				icon.className = 'material-icons ju-notif-pop-icon';
				icon.setAttribute('aria-hidden', 'true');
				var main = documentRef.createElement('span');
				main.className = 'ju-notif-pop-main';
				var title = documentRef.createElement('span');
				title.className = 'ju-notif-pop-title';
				var hidden = documentRef.createElement('span');
				hidden.className = 'visually-hidden';
				hidden.textContent = 'Unread: ';
				var titleText = documentRef.createElement('span');
				titleText.className = 'ju-notif-pop-text';
				title.appendChild(hidden);
				title.appendChild(titleText);
				var detail = documentRef.createElement('small');
				detail.className = 'ju-notif-pop-detail text-body-secondary';
				main.appendChild(title);
				main.appendChild(detail);
				button.appendChild(icon);
				button.appendChild(main);
				li.appendChild(button);
				var dismissAction = documentRef.createElement('div');
				dismissAction.className = 'ju-notif-dismiss-action';
				var dismissButton = documentRef.createElement('button');
				dismissButton.type = 'button';
				dismissButton.className = 'ju-notif-dismiss-button';
				var dismissIcon = documentRef.createElement('span');
				dismissIcon.className = 'material-icons';
				dismissIcon.setAttribute('aria-hidden', 'true');
				dismissIcon.textContent = 'delete';
				var dismissLabel = documentRef.createElement('span');
				dismissLabel.textContent = 'Delete';
				dismissButton.appendChild(dismissIcon);
				dismissButton.appendChild(dismissLabel);
				dismissAction.appendChild(dismissButton);
				li.appendChild(dismissAction);
			}
			button.dataset.id = id;
			dismissButton = li.querySelector('.ju-notif-dismiss-button');
			dismissButton.dataset.notificationId = id;
			dismissButton.setAttribute('aria-label', 'Delete notification: ' + (n.title || ''));
			dismissButton.title = 'Delete notification';
			button.querySelector('.material-icons').textContent = categoryIcon(n.category);
			button.querySelector('.visually-hidden').hidden = !!n.is_read;
			var text = button.querySelector('.ju-notif-pop-text');
			text.textContent = n.title || '';
			text.title = n.title || '';
			button.querySelector('.ju-notif-pop-detail').textContent = detailLine(n, relTime(n.created_at));
			desired.push(li);
		});

		if (!items.length) {
			var empty = documentRef.createElement('li');
			empty.className = 'ju-notif-pop-empty text-body-secondary';
			empty.textContent = 'No notifications yet.';
			desired.push(empty);
		}

		var active = documentRef.activeElement;
		var activeControl = active && active.classList && previewList.contains(active)
			&& (active.classList.contains('ju-notif-pop-btn') || active.classList.contains('ju-notif-dismiss-button'))
			? active : null;
		var activeItem = activeControl && activeControl.closest('li');
		var activeWillRemain = !!activeItem && desired.indexOf(activeItem) !== -1;
		var replacementFocus = null;
		renderingPreview = true;
		try {
			if (activeItem && !activeWillRemain) {
				// The focused item is gone: move to the next remaining one, then the previous one, then any new one, else the bell.
				var before = Array.prototype.slice.call(previewList.children);
				var at = before.indexOf(activeItem);
				var focusable = function (li) {
					return desired.indexOf(li) !== -1 && li.querySelector('.ju-notif-pop-btn');
				};
				var neighbour = before.slice(at + 1).filter(focusable)[0] || before.slice(0, at).filter(focusable).pop()
					|| desired.filter(function (li) { return li.querySelector('.ju-notif-pop-btn'); })[0];
				replacementFocus = (neighbour && neighbour.querySelector('.ju-notif-pop-btn')) || fallbackFocus;
			}

			Array.prototype.slice.call(previewList.children).forEach(function (li) {
				if (desired.indexOf(li) === -1) li.remove();
			});
			desired.forEach(function (li, index) {
				var current = previewList.children[index] || null;
				if (current !== li) previewList.insertBefore(li, current);
			});
			if (replacementFocus) replacementFocus.focus();
			else if (activeWillRemain && documentRef.activeElement !== activeControl) activeControl.focus();
		} finally {
			renderingPreview = false;
		}
	}

	function relTime(iso) {
		var s = Math.max(0, Math.round((Date.now() - Date.parse(iso)) / 1000));
		if (isNaN(s)) return '';
		if (s < 60) return 'just now';
		if (s < 3600) return Math.floor(s / 60) + ' min ago';
		if (s < 86400) return Math.floor(s / 3600) + ' h ago';
		return Math.floor(s / 86400) + ' d ago';
	}

	function onData(data) {
		var shakeNow = shouldShake(last, data);
		last = { unread: data.unread, latest_unread_id: data.latest_unread_id, unread_event_seq: data.unread_event_seq };
		render(data);
		if (shakeNow) { shake(); live.textContent = 'New notification'; }
	}

	function post(url) {
		return root.apiFetch(url, { method: 'POST' }).then(function (r) {
			if (r.status === 401 || r.status === 403) { poller.stop(); return r; }
			return r;
		});
	}

	function setOpen(open) {
		pop.hidden = !open;
		bell.setAttribute('aria-expanded', open ? 'true' : 'false');
		if (open) { var first = pop.querySelector('button, a'); if (first) first.focus(); }
	}

	bell.addEventListener('click', function () { setOpen(pop.hidden); });
	doc.addEventListener('keydown', function (e) {
		if (e.key === 'Escape' && !pop.hidden) { setOpen(false); bell.focus(); }
	});
	doc.addEventListener('click', function (e) {
		if (!pop.hidden && !pop.contains(e.target) && !bell.contains(e.target)) setOpen(false);
	});
	// Non-modal popover: tabbing past its last control closes it instead of leaving it open behind the focus.
	pop.addEventListener('focusout', function (e) {
		if (renderingPreview) return;
		var next = e.relatedTarget;
		if (!next || (!pop.contains(next) && next !== bell)) setOpen(false);
	});
	readAll.addEventListener('click', function () {
		post('/ui/notifications/read-all').then(function () { poller.refresh(); });
	});
	list.addEventListener('click', function (e) {
		var dismiss = e.target.closest('.ju-notif-dismiss-button');
		if (dismiss) {
			e.preventDefault();
			e.stopPropagation();
			if (dismiss.dataset.pending) return;
			// Not `disabled`: that would drop keyboard focus and close the popover before the row is replaced.
			dismiss.dataset.pending = '1';
			dismiss.setAttribute('aria-disabled', 'true');
			var release = function () {
				delete dismiss.dataset.pending;
				dismiss.removeAttribute('aria-disabled');
			};
			post('/ui/notifications/' + encodeURIComponent(dismiss.dataset.notificationId) + '/dismiss')
				.then(function (response) {
					if (response.ok) poller.refresh();
					else release();
				})
				.catch(release);
			return;
		}
		var btn = e.target.closest('.ju-notif-pop-btn');
		if (!btn) return;
		post('/ui/notifications/' + encodeURIComponent(btn.dataset.id) + '/read').then(function () { poller.refresh(); });
	});

	poller = createPoller({
		intervalMs: POLL_MS,
		fetchSummary: function () { return root.apiFetch('/ui/notifications/summary', { cache: 'no-store' }); },
		onData: onData,
		onAuthLost: function () { badge.hidden = true; },
		isHidden: function () { return doc.hidden; },
		isSuppressed: function () { return !!liveClient && liveClient.state() === 'connected'; },
		setTimer: function (fn, ms) { return root.setTimeout(fn, ms); },
		clearTimer: function (t) { root.clearTimeout(t); }
	});
	doc.addEventListener('visibilitychange', function () { poller.onVisibility(); });
	root.addEventListener('justup:notifications-changed', function () { poller.refresh(); });
	if (liveClient) {
		// Push replaces the 15 s poll while the stream is healthy; the poller resumes as the fallback.
		liveClient.subscribe(['notification.changed'], function () { poller.refresh(); }, { resync: function () { poller.refresh(); } });
		liveClient.onState(function (next) { if (next === 'connected') poller.suspend(); else poller.refresh(); });
	}
	poller.start();
})(typeof window !== 'undefined' ? window : globalThis);
