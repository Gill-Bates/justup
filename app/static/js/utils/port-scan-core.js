//
// app/static/js/utils/port-scan-core.js
// Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
//

// Pure rules of the port picker (no DOM): port parsing, protocol-typed input, merging, the meaning of
// Nmap states, and the user-facing outcome of a scan job. Loaded as a classic script and by tests/js.
(function (root) {
	'use strict';
	const PORT_PATTERN = /^[0-9]+$/;
	const TERMINAL = ['completed', 'failed', 'cancelled', 'expired'];
	const PROTOCOLS = ['tcp', 'udp'];

	/** One port number from 1 to 65535, or null. */
	function parsePort(text) {
		const value = String(text ?? '').trim();
		if (!PORT_PATTERN.test(value)) return null;
		const port = Number(value);
		return port >= 1 && port <= 65535 ? port : null;
	}

	/** Comma-separated port list of a form field. Invalid parts are reported, never dropped silently. */
	function parsePortList(text) {
		const ports = [];
		const invalid = [];
		for (const part of String(text ?? '').split(',').map(item => item.trim()).filter(Boolean)) {
			const port = parsePort(part);
			if (port === null) invalid.push(part);
			else if (!ports.includes(port)) ports.push(port);
		}
		return { ports: ports.sort((a, b) => a - b), invalid };
	}

	/**
	 * Manual, protocol-typed input: `22`, `tcp:22`, `22/tcp`, `udp:53`, `53/udp`, separated by commas,
	 * spaces or line breaks. A port without a protocol is TCP.
	 */
	function parseTypedPorts(text) {
		const result = { tcp: [], udp: [], invalid: [] };
		for (const token of String(text ?? '').split(/[\s,;]+/).filter(Boolean)) {
			const match = /^(?:(tcp|udp)[:/])?([0-9]+)(?:[:/](tcp|udp))?$/i.exec(token);
			const port = match ? parsePort(match[2]) : null;
			const typed = match && (match[1] || match[3]);
			if (port === null || (match[1] && match[3] && match[1].toLowerCase() !== match[3].toLowerCase())) {
				result.invalid.push(token);
				continue;
			}
			const protocol = (typed || 'tcp').toLowerCase();
			if (!result[protocol].includes(port)) result[protocol].push(port);
		}
		for (const protocol of PROTOCOLS) result[protocol].sort((a, b) => a - b);
		return result;
	}

	/** Existing ports plus additions: sorted, without duplicates. Existing manual entries always stay. */
	function mergePorts(existing, additions) {
		return [...new Set([...existing, ...additions])].sort((a, b) => a - b);
	}

	/** Field value of a port list. */
	function formatPorts(ports) {
		return ports.join(',');
	}

	// Nmap states. Only "open" is a confirmed result; "closed" is confirmed closed, every other state is unclear.
	const STATES = {
		open: { label: 'Open', confirmed: true, tone: 'open', icon: 'check_circle', note: 'confirmed open' },
		closed: { label: 'Closed', confirmed: false, tone: 'closed', icon: 'cancel', note: 'confirmed closed' },
		filtered: { label: 'Filtered', confirmed: false, tone: 'uncertain', icon: 'help', note: 'uncertain, not confirmed' },
		'open|filtered': { label: 'Open or filtered', confirmed: false, tone: 'uncertain', icon: 'help', note: 'uncertain, not confirmed open' },
		'closed|filtered': { label: 'Closed or filtered', confirmed: false, tone: 'uncertain', icon: 'help', note: 'uncertain' },
		unfiltered: { label: 'Unfiltered', confirmed: false, tone: 'uncertain', icon: 'help', note: 'uncertain' }
	};
	const UNKNOWN_STATE = { label: 'Unknown', confirmed: false, tone: 'uncertain', icon: 'help', note: 'no usable result' };

	function describeState(state) {
		return STATES[String(state ?? '').toLowerCase()] || UNKNOWN_STATE;
	}

	/** Service column text. A table hint is the port number's usual service, not a detection. */
	function serviceLabel(service) {
		const name = service && typeof service.name === 'string' && service.name ? service.name : '';
		if (!name) return 'Unknown';
		if (service.method === 'table') return `${name} (table hint)`;
		return [name, service.product, service.version].filter(item => typeof item === 'string' && item).join(' · ');
	}

	/** Distinct, sorted rows of a scan result; malformed entries are ignored. */
	function collectPorts(result) {
		const seen = new Set();
		const rows = [];
		for (const host of Array.isArray(result?.hosts) ? result.hosts : []) {
			for (const port of Array.isArray(host?.ports) ? host.ports : []) {
				if (!PROTOCOLS.includes(port?.protocol) || !Number.isInteger(port.port) || port.port < 1 || port.port > 65535) continue;
				const key = `${port.protocol}:${port.port}`;
				if (seen.has(key)) continue;
				seen.add(key);
				const state = typeof port.state === 'string' ? port.state : 'unknown';
				rows.push({ protocol: port.protocol, port: port.port, state, info: describeState(state), service: serviceLabel(port.service), hint: port.service?.method === 'table' && Boolean(port.service?.name) });
			}
		}
		return rows.sort((a, b) => a.protocol.localeCompare(b.protocol) || a.port - b.port);
	}

	/** Rows that are confirmed open. */
	function confirmedOpen(rows) {
		return rows.filter(row => row.info.confirmed);
	}

	/** Counts of confirmed open, confirmed closed and unclear rows; the three never overlap. */
	function summarizeRows(rows) {
		const open = rows.filter(row => row.info.confirmed).length;
		const closed = rows.filter(row => row.info.tone === 'closed').length;
		return { total: rows.length, open, closed, unclear: rows.length - open - closed };
	}

	/** Confirmed open rows first-class, every other state apart. Order inside each group is kept. */
	function splitResults(rows) {
		return { confirmed: confirmedOpen(rows), other: rows.filter(row => !row.info.confirmed) };
	}

	/** "3 closed, 12 unclear" style detail of the rows that are not confirmed open. */
	function otherDetail(counts) {
		return [counts.closed && `${counts.closed} closed`, counts.unclear && `${counts.unclear} unclear`].filter(Boolean).join(', ');
	}

	const ERROR_TEXT = {
		unavailable: 'The Nmap scan worker is unavailable.',
		busy: 'A scan for this host is already running, or too many scans are active. Try again in a moment.',
		timeout: 'The scan timed out before it finished.',
		permission_denied: 'Nmap lacks the permission for this scan. UDP scans need the raw-socket capability of the scan worker.',
		invalid_host: 'This host cannot be scanned. Check the host name or address.',
		invalid_ports: 'The scan was refused because of invalid ports.',
		output_limit: 'The scan produced more output than allowed and was stopped.',
		process_failed: 'The scan process failed.',
		invalid_xml: 'The scan result could not be read.',
		forbidden: 'Only administrators can scan ports. Sign in again or ask an administrator.'
	};

	/**
	 * What the user sees for a job in a given state.
	 * `kind`: running | results | empty | uncertain-only | failed | cancelled | expired.
	 * `tone`: info | warning | error. `unavailable` marks a missing worker (disables the scan action).
	 */
	function describeJob(job, protocols) {
		const requested = Array.isArray(protocols) ? protocols : [];
		if (!job || typeof job !== 'object') return { kind: 'failed', tone: 'error', message: 'The scan response was not understood.' };
		if (job.status === 'queued') return { kind: 'running', tone: 'info', message: 'Scan queued…' };
		if (job.status === 'running') return { kind: 'running', tone: 'info', message: 'Scanning ports…' };
		if (job.status === 'cancelled') return { kind: 'cancelled', tone: 'info', message: 'Scan cancelled. You can still enter ports manually.' };
		if (job.status === 'expired') return { kind: 'expired', tone: 'warning', message: 'The scan result expired. Scan again or enter ports manually.' };
		if (job.status === 'failed') {
			const code = job.error?.code;
			const fallback = typeof job.error?.message === 'string' && job.error.message ? job.error.message : 'The scan failed.';
			return {
				kind: 'failed', tone: 'error', unavailable: code === 'unavailable', reason: fallback,
				message: `${ERROR_TEXT[code] || fallback} You can still enter ports manually.`
			};
		}
		if (job.status !== 'completed') return { kind: 'failed', tone: 'error', message: 'The scan returned an unknown status.' };
		const rows = collectPorts(job.result);
		const confirmed = confirmedOpen(rows);
		if (!rows.length) {
			return { kind: 'empty', tone: 'info', rows, message: 'No open ports found. You can still enter ports manually.' };
		}
		const udpNote = requested.includes('udp') && !confirmed.some(row => row.protocol === 'udp')
			? ' No UDP port could be confirmed open: UDP results are uncertain, select them manually if you want to monitor them.'
			: '';
		const counts = summarizeRows(rows);
		const detail = otherDetail(counts);
		const found = `${rows.length} port${rows.length === 1 ? '' : 's'} found`;
		if (!confirmed.length) {
			return {
				kind: 'uncertain-only', tone: 'warning', rows, counts,
				message: `${found}, none confirmed open${detail ? ` (${detail})` : ''}.${udpNote}`
			};
		}
		return {
			kind: 'results', tone: udpNote ? 'warning' : 'info', rows, counts,
			message: `${found}: ${confirmed.length} confirmed open${detail ? `, ${detail}` : ''}.${udpNote}`
		};
	}

	/** Error of a failed HTTP call (start or poll) as the same shape `describeJob` returns. */
	function describeRequestError(error) {
		const code = error && error.code;
		const message = ERROR_TEXT[code] || (error && error.message) || 'The scan request failed.';
		return {
			kind: 'failed', tone: 'error', unavailable: code === 'unavailable', reason: error && error.message,
			message: `${message} You can still enter ports manually.`
		};
	}

	/** Protocols to scan: the active checks, or TCP when neither is on yet. */
	function defaultProtocols(tcpActive, udpActive) {
		const chosen = PROTOCOLS.filter(protocol => (protocol === 'tcp' ? tcpActive : udpActive));
		return chosen.length ? chosen : ['tcp'];
	}

	root.JuPortScan = {
		TERMINAL, PROTOCOLS, parsePort, parsePortList, parseTypedPorts, mergePorts, formatPorts,
		describeState, serviceLabel, collectPorts, confirmedOpen, summarizeRows, splitResults, describeJob, describeRequestError, defaultProtocols
	};
})(window);
