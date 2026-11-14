//
// app/static/js/utils/check-groups.js
// Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
//

// The one rule that maps the check toggles to the field groups they own (progressive disclosure).
(function (root) {
	'use strict';

	// Which toggle shows a group. ignore_cert_errors acts on the HTTP request and on the certificate check.
	const GROUP_RULES = {
		http: (c) => c.http,
		tls: (c) => c.http || c.cert,
		tcp: (c) => c.tcp,
		udp: (c) => c.udp,
		ping: (c) => c.ping,
		cert: (c) => c.cert
	};

	function visibleGroups(checks) {
		const state = {
			http: Boolean(checks && checks.http),
			ping: Boolean(checks && checks.ping),
			tcp: Boolean(checks && checks.tcp),
			udp: Boolean(checks && checks.udp),
			cert: Boolean(checks && checks.cert)
		};
		const result = {};
		for (const [group, rule] of Object.entries(GROUP_RULES)) result[group] = rule(state);
		return result;
	}

	// Elements carry data-check-group; the shared optional-groups mechanic hides them (utils/optional-groups.js).
	function apply(container, checks) {
		return root.JuOptionalGroups.apply(container, 'check-group', visibleGroups(checks));
	}

	root.JuCheckGroups = { visibleGroups: visibleGroups, apply: apply };
})(window);
