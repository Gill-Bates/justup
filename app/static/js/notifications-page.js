//
// app/static/js/notifications-page.js
// Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
//

/** Notifications page: per-user dismissal, read-all action and authorized PDF download. */
(function () {
	'use strict';
	var page = document.getElementById('notifications-page');
	if (!page) return;
	window.JustUpNotifications?.bindSwipeRows(page);

	function markRead(id) {
		return window.apiFetch('/ui/notifications/' + encodeURIComponent(id) + '/read', { method: 'POST' });
	}
	function changed() { window.dispatchEvent(new Event('justup:notifications-changed')); }

	var all = document.getElementById('np-read-all');
	if (all) all.addEventListener('click', function () {
		window.apiFetch('/ui/notifications/read-all', { method: 'POST' }).then(function (r) {
			if (r.ok) window.location.reload();
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
				if (window.justUpToast && window.justUpToast.error) {
					window.justUpToast.error('Notification could not be deleted');
				}
			});
			return;
		}
		var mark = e.target.closest('.np-mark');
		if (mark) {
			mark.disabled = true;
			markRead(mark.dataset.id).then(function (r) {
				if (!r.ok) throw new Error('Notification could not be marked as read');
				var item = mark.closest('.ju-notif-item');
				if (item) {
					item.classList.remove('is-unread');
					var dot = item.querySelector('.ju-unread-dot');
					if (dot) dot.remove();
					var hidden = item.querySelector('.ju-notif-title .visually-hidden');
					if (hidden) hidden.remove();
				}
				mark.remove();
				changed();
			}).catch(function () {
				mark.disabled = false;
				if (window.justUpToast && window.justUpToast.error) {
					window.justUpToast.error('Notification could not be marked as read');
				}
			});
			return;
		}
		var open = e.target.closest('.np-open');
		if (open) {
			var modified = e.metaKey || e.ctrlKey || e.shiftKey || e.altKey || (typeof e.button === 'number' && e.button > 0);
			if (modified) {
				markRead(open.dataset.id).then(function (r) { if (r.ok) changed(); }).catch(function () {});
				return;
			}
			e.preventDefault();
			var href = open.href;
			Promise.resolve().then(function () { return markRead(open.dataset.id); }).then(function (r) {
				if (r.ok) changed();
			}).catch(function () {}).then(function () {
				window.location.assign(href);
			});
			return;
		}
		var dl = e.target.closest('.np-download');
		if (dl) {
			window.justUpPdfJobs.startDownload(dl.dataset.url, 'report.pdf').then(function () {
				// Read state changes only after a successful, authorized download.
				return markRead(dl.dataset.id).then(function (r) { if (r.ok) changed(); }, function () {});
			}).catch(function () {
				if (window.justUpToast && window.justUpToast.error) window.justUpToast.error('Report is no longer available');
			});
		}
	});
}());
