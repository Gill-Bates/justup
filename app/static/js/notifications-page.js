//
// app/static/js/notifications-page.js
// Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
//

/** Notifications page: per-user dismissal, read-all action and authorized PDF download. */
(function () {
	'use strict';
	var page = document.getElementById('notifications-page');
	if (!page) return;
	function markRead(id, options) {
		return window.apiFetch('/ui/notifications/' + encodeURIComponent(id) + '/read', Object.assign({ method: 'POST' }, options));
	}
	function changed() { window.dispatchEvent(new Event('justup:notifications-changed')); }
	function toastError(message) {
		if (window.justUpToast && window.justUpToast.error) window.justUpToast.error(message);
	}

	// Clears the unread markers of a row and drops its "read" action; link/download rows stay interactive.
	function showRead(item) {
		item.classList.remove('is-unread');
		var dot = item.querySelector('.ju-unread-dot');
		if (dot) dot.remove();
		var hidden = item.querySelector('.ju-notif-title .visually-hidden');
		if (hidden) hidden.remove();
		var content = item.querySelector('.ju-notif-swipe-content');
		if (!content) return;
		content.removeAttribute('aria-keyshortcuts');
		if (content.dataset.action === 'read') {
			['role', 'tabindex', 'aria-label', 'data-action'].forEach(function (a) { content.removeAttribute(a); });
			content.classList.remove('is-actionable');
		}
	}

	function markItemRead(item) {
		if (!item || !item.classList.contains('is-unread') || item.dataset.pending) return Promise.resolve();
		item.dataset.pending = '1';
		return markRead(item.dataset.id).then(function (r) {
			if (!r.ok) throw new Error('Notification could not be marked as read');
			showRead(item);
			changed();
		}).catch(function () {
			toastError('Notification could not be marked as read');
		}).then(function () { delete item.dataset.pending; });
	}

	// Primary action of a row: link and download also mark it read, a plain unread row only marks it read.
	// A modified or middle click on a link is left to the browser (new tab); it only records the read state.
	function activate(content, modified) {
		var item = content.closest('.ju-notif-item');
		var action = content.dataset.action;
		if (action === 'link') {
			var href = content.getAttribute('href');
			if (modified) {
				markRead(content.dataset.id).then(function (r) {
					if (r.ok) { if (item) showRead(item); changed(); }
				}).catch(function () {});
				return;
			}
			markRead(content.dataset.id, { keepalive: true }).then(function (r) {
				if (r.ok) changed();
			}).catch(function () {});
			window.location.assign(href);
		} else if (action === 'download') {
			// The anchor itself starts the attachment download; only the read state is recorded here.
			markRead(content.dataset.id).then(function (r) {
				if (r.ok) { if (item) showRead(item); changed(); }
			}).catch(function () {});
		} else if (action === 'read') {
			markItemRead(item);
		}
	}

	window.JustUpNotifications?.bindSwipeRows(page, { onSwipeRead: markItemRead });

	var all = document.getElementById('np-read-all');
	if (all) all.addEventListener('click', function () {
		window.apiFetch('/ui/notifications/read-all', { method: 'POST' }).then(function (r) {
			if (!r.ok) throw new Error('Notifications could not be marked as read');
			window.location.reload();
		}).catch(function () {
			toastError('Notifications could not be marked as read');
		});
	});

	page.addEventListener('click', function (e) {
		var dismiss = e.target.closest('.ju-notif-dismiss-button');
		if (dismiss) {
			e.preventDefault();
			dismiss.disabled = true;
			window.apiFetch('/ui/notifications/' + encodeURIComponent(dismiss.dataset.notificationId) + '/dismiss', {
				method: 'POST'
			}).then(function (response) {
				if (!response.ok) throw new Error('Notification could not be deleted');
				changed();
				window.location.reload();
			}).catch(function () {
				dismiss.disabled = false;
				toastError('Notification could not be deleted');
			});
			return;
		}
		var content = e.target.closest('.ju-notif-swipe-content.is-actionable');
		if (!content) return;
		var modified = e.metaKey || e.ctrlKey || e.shiftKey || e.altKey;
		// Unmodified link clicks mark read before navigating; modified ones keep native behavior.
		if (content.dataset.action === 'link' && !modified) e.preventDefault();
		// A download anchor is never cancelled: the browser performs the attachment navigation natively.
		activate(content, modified);
	});
	page.addEventListener('auxclick', function (e) {
		var content = e.target.closest('.ju-notif-swipe-content.is-actionable');
		if (content && content.dataset.action === 'link' && e.button === 1) activate(content, true);
	});
	page.addEventListener('keydown', function (e) {
		var content = e.target.closest && e.target.closest('.ju-notif-swipe-content.is-actionable');
		if (!content || e.target !== content || e.isComposing || e.metaKey || e.ctrlKey || e.altKey) return;
		// A real link already turns Enter into a click and keeps Space for scrolling.
		var isLink = content.tagName === 'A';
		if (!isLink && (e.key === 'Enter' || e.key === ' ')) {
			e.preventDefault();
			activate(content, false);
		} else if (e.key === 'ArrowRight') {
			// Keyboard counterpart of the right swipe.
			e.preventDefault();
			markItemRead(content.closest('.ju-notif-item'));
		}
	});
}());
