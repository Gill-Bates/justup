//
// app/static/js/utils/user-storage.js
// Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
//

/**
 * User-scoped localStorage keys. The server puts an opaque, non-secret user key on <html
 * data-user-key>; data that names internal targets must live under it, so a second account in the
 * same browser never sees it. Keys of any other user are dropped on load (session change without
 * a logout), all of them on logout. Preferences such as theme or sidebar state stay global.
 */
(function () {
	'use strict';

	var PREFIX = 'justup.u.';

	function currentUserId() {
		var root = document.documentElement;
		return (root && root.getAttribute('data-user-key')) || '';
	}

	/** Storage key for `name` of the current user, or null when nobody is signed in. */
	function scopedKey(name) {
		var user = currentUserId();
		return user ? PREFIX + user + '.' + name : null;
	}

	function keysWithPrefix(store) {
		var keys = [];
		for (var i = 0; i < store.length; i++) {
			var key = store.key(i);
			if (key && key.indexOf(PREFIX) === 0) keys.push(key);
		}
		return keys;
	}

	function removeWhere(keep) {
		[function () { return localStorage; }, function () { return sessionStorage; }].forEach(function (get) {
			try {
				var store = get();
				keysWithPrefix(store).forEach(function (key) { if (!keep(key)) store.removeItem(key); });
			} catch (e) { /* storage unavailable: nothing persisted */ }
		});
	}

	/** Logout: remove every user-scoped entry. */
	function clearUserScoped() {
		removeWhere(function () { return false; });
	}

	/** Page load: keep only the current user's entries and delete unscoped legacy keys. */
	function dropOtherUsers(legacyKeys) {
		var own = scopedKey('');
		removeWhere(function (key) { return own !== null && key.indexOf(own) === 0; });
		try {
			(legacyKeys || []).forEach(function (key) { localStorage.removeItem(key); });
		} catch (e) { /* storage unavailable */ }
	}

	window.JustUpUserStorage = {
		currentUserId: currentUserId,
		scopedKey: scopedKey,
		clearUserScoped: clearUserScoped,
		dropOtherUsers: dropOtherUsers
	};
})();
