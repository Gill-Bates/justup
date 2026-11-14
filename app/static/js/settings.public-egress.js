//
// app/static/js/settings.public-egress.js
// Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
//

/**
 * settings.public-egress.js - "Public outbound IPs" card (Settings > Network)
 *
 * One request when the Network tab fragment first appears, one more per Refresh click.
 * No polling, no storage, no settings write. Every value reaches the DOM through textContent.
 * The describe* helpers are pure (no DOM) so they can be tested in Node.
 */

const REQUEST_TIMEOUT_MS = 15000;
const COPY_FEEDBACK_MS = 2000;

// Visible reason: one short line. The long text goes to the title attribute (REASON_TITLES).
const REASONS = {
    no_route: 'No route for this family.',
    dns_failed: 'Name lookup failed.',
    timeout: 'No answer in time.',
    connect_failed: 'Connection failed.',
    tls_failed: 'TLS check failed.',
    redirect: 'Unusable answer.',
    http_status: 'Unusable answer.',
    invalid_response: 'Unusable answer.',
    response_too_large: 'Unusable answer.',
    wrong_family: 'Wrong address family.',
    non_public_address: 'Non-public address returned.',
    blocked_target: 'Resolved to private address.',
    providers_disagree: 'Services disagree.',
    busy: 'Busy, try again shortly.',
};

const REASON_TITLES = {
    no_route: 'This server has no route for this address family.',
    dns_failed: 'The echo services could not be resolved.',
    timeout: 'The echo services did not answer in time.',
    connect_failed: 'The connection to the echo services failed.',
    tls_failed: 'The echo services failed TLS verification.',
    redirect: 'An echo service sent an unusable answer.',
    http_status: 'An echo service sent an unusable answer.',
    invalid_response: 'An echo service sent an unusable answer.',
    response_too_large: 'An echo service sent an unusable answer.',
    wrong_family: 'An echo service answered with the wrong address family.',
    non_public_address: 'An echo service returned a non-public address; it was discarded.',
    blocked_target: 'An echo service resolved to a non-public address and was not contacted.',
    providers_disagree: 'Independent echo services report different addresses.',
    busy: 'Earlier lookups are still running; try again shortly.',
};

const BADGES = {
    loading: ['Checking\u2026', 'neutral'],
    available: ['Available', 'success'],
    unavailable: ['Unavailable', 'neutral'],
    failed: ['Lookup failed', 'danger'],
    inconsistent: ['Inconsistent', 'warning'],
};

/** Format an ISO timestamp for display; an unparsable value yields an empty string. */
export function formatObserved(iso) {
    const date = new Date(iso);
    if (!iso || Number.isNaN(date.getTime())) return '';
    return date.toLocaleString(undefined, { dateStyle: 'medium', timeStyle: 'short' });
}

function hasText(value) {
    return typeof value === 'string' && value !== '';
}

function stateView(status, extra) {
    const [badgeText, badgeTone] = BADGES[status];
    return { status, badgeText, badgeTone, address: '', detail: '', title: '', geo: null, canCopy: false, ...extra };
}

/**
 * Flag, ASN and place of an address from the API's `geo` object. Every part is optional; without any
 * usable part the result is null (no line, no placeholder). Only strings of a sane shape are accepted.
 */
export function describeGeo(geo) {
    if (!geo || typeof geo !== 'object') return null;
    const text = (value) => (typeof value === 'string' ? value.trim().slice(0, 120) : '');
    const code = /^[A-Za-z]{2}$/.test(geo.country_code) ? geo.country_code.toUpperCase() : '';
    const country = text(geo.country) || code;
    const city = text(geo.city);
    const asn = [text(geo.asn), text(geo.as_org)].filter(Boolean).join(' ');
    const place = [city, country].filter((part, index, all) => part && all.indexOf(part) === index).join(', ');
    const label = [asn, place].filter(Boolean).join(' \u00b7 ');
    if (!label) return null;
    return { code, country, label };
}

/**
 * Turn one family entry of the API response into display state. `family` may be missing or malformed;
 * that is a failed lookup, never an address. The server answers with the cached address when a refresh
 * fails, so an address is always `available` and can be copied.
 */
export function describeFamily(label, family) {
    let status = 'failed';
    if (family && typeof family.status === 'string' && Object.hasOwn(BADGES, family.status)) status = family.status;
    if (status === 'available' && !hasText(family.address)) status = 'failed';
    const known = family && typeof family.reason === 'string' && Object.hasOwn(REASONS, family.reason);
    const reason = known ? REASONS[family.reason] : '';
    const reasonTitle = known ? REASON_TITLES[family.reason] : '';
    const view = stateView(status);
    if (status === 'available') {
        view.address = family.address;
        view.canCopy = true;
        view.geo = describeGeo(family.geo);
    } else if (status === 'inconsistent') {
        const seen = Array.isArray(family.observed) ? family.observed.filter((a) => typeof a === 'string') : [];
        view.detail = reason;
        view.title = (reasonTitle || reason) + (seen.length ? ' Observed: ' + seen.join(', ') + '.' : '');
    } else if (status === 'unavailable') {
        view.detail = 'No ' + label + ' connectivity.';
    } else if (status === 'failed') {
        view.detail = reason; // empty: the badge already says "Lookup failed", the dash stays
        view.title = reasonTitle;
    }
    return view;
}

/** While a refresh runs, keep the last known address visible but not copyable. */
export function describeLoading(previous) {
    if (previous && hasText(previous.address)) {
        return stateView('loading', { address: previous.address, geo: previous.geo });
    }
    return stateView('loading');
}

/** The request itself failed: keep showing the last known address (cached), else a failed lookup. */
export function describeRequestFailure(previous, reason) {
    if (previous && hasText(previous.address)) {
        return stateView('available', { address: previous.address, canCopy: true, geo: previous.geo });
    }
    return stateView('failed', { detail: reason, title: reason });
}

function familyLabel(key) {
    return key === 'ipv6' ? 'IPv6' : 'IPv4';
}

// Same pinned flag-icons URL as app.js and traceroute_map.html; CSP img-src covers this CDN.
const FLAG_BASE = 'https://cdn.jsdelivr.net/npm/flag-icons@7.3.2/flags/4x3/';

function renderGeo(line, geo) {
    if (!line) return;
    line.hidden = !geo;
    line.title = geo ? geo.label : '';
    if (!geo) {
        line.replaceChildren();
        return;
    }
    const nodes = [];
    if (geo.code) {
        // Decorative: the country name is part of the text next to it. A flag that fails to load shows its code.
        const flag = document.createElement('span');
        flag.className = 'egress-flag';
        flag.setAttribute('aria-hidden', 'true');
        const image = document.createElement('img');
        image.src = FLAG_BASE + geo.code.toLowerCase() + '.svg';
        image.alt = '';
        image.width = 16;
        image.height = 12;
        image.decoding = 'async';
        const fallback = document.createElement('span');
        fallback.className = 'egress-flag-code';
        fallback.hidden = true;
        fallback.textContent = geo.code;
        image.addEventListener('error', () => { image.hidden = true; fallback.hidden = false; }, { once: true });
        flag.append(image, fallback);
        nodes.push(flag);
    }
    const text = document.createElement('span');
    text.className = 'egress-geo-text';
    text.textContent = geo.label;
    nodes.push(text);
    line.replaceChildren(...nodes);
}

function renderFamily(row, view) {
    const key = row.dataset.egressFamily;
    row.dataset.egressState = view.status;
    const address = row.querySelector('[data-egress-address]');
    const badge = row.querySelector('[data-egress-badge]');
    const message = row.querySelector('[data-egress-message]');
    const detail = row.querySelector('[data-egress-detail]');
    const copy = row.querySelector('[data-egress-copy]');
    // Without an address the detail text takes the dash's place.
    const note = view.address ? '' : view.detail;
    address.hidden = note !== '';
    address.textContent = view.address || '\u2014';
    address.classList.toggle('text-body-secondary', !view.address);
    message.hidden = note === '';
    message.textContent = note;
    message.title = (!view.address && view.title) || note;
    // Dot classes come from JuStatusDot (utils/status-badge.js); the word stays the visible label.
    badge.textContent = view.badgeText;
    const dot = row.querySelector('[data-egress-dot]');
    if (dot) {
        dot.className = window.JuStatusDot.classes(view.badgeTone);
        // Loading is S6 (word only, no dot; DC-STATE-1).
        dot.hidden = view.status === 'loading';
    }
    detail.textContent = view.address ? view.detail : '';
    detail.title = view.address ? view.title || view.detail : '';
    renderGeo(row.querySelector('[data-egress-geo]'), view.address ? view.geo : null);
    copy.disabled = !view.canCopy;
    copy.dataset.egressValue = view.canCopy ? view.address : '';
    copy.setAttribute('aria-label', 'Copy ' + familyLabel(key) + ' address');
}

async function writeClipboard(text) {
    if (navigator.clipboard && window.isSecureContext) {
        await navigator.clipboard.writeText(text);
        return;
    }
    // Plain-HTTP deployments have no async clipboard API.
    const field = document.createElement('textarea');
    field.value = text;
    field.setAttribute('readonly', '');
    field.style.position = 'fixed';
    field.style.opacity = '0';
    document.body.appendChild(field);
    field.select();
    try {
        if (!document.execCommand('copy')) throw new Error('copy rejected');
    } finally {
        field.remove();
    }
}

export function initPublicEgress(root = document) {
    const card = root.querySelector('[data-public-egress]');
    if (!card || card.dataset.egressBound === '1') return;
    card.dataset.egressBound = '1';

    const rows = Array.from(card.querySelectorAll('[data-egress-family]'));
    const cachedNote = card.querySelector('[data-egress-cached]');
    const known = {}; // last view that carried an address, per family
    const refreshBtn = card.querySelector('[data-egress-refresh]');
    const checked = card.querySelector('[data-egress-checked]');
    const proxyNote = card.querySelector('[data-egress-proxy]');
    const live = card.querySelector('[data-egress-live]');
    const apiFetch = window.apiFetch || ((url, options) => fetch(url, { credentials: 'same-origin', ...options }));
    let busy = false;

    const show = (row, view) => {
        if (view.address && view.status === 'available') known[row.dataset.egressFamily] = view;
        renderFamily(row, view);
    };

    async function load(refresh) {
        if (busy) return;
        busy = true;
        refreshBtn.setAttribute('aria-disabled', 'true');
        refreshBtn.setAttribute('aria-busy', 'true');
        card.setAttribute('aria-busy', 'true');
        rows.forEach((row) => renderFamily(row, describeLoading(known[row.dataset.egressFamily])));
        const controller = new AbortController();
        const timer = setTimeout(() => controller.abort(), REQUEST_TIMEOUT_MS);
        let failed = false;
        let failureReason = '';
        try {
            const resp = await apiFetch(card.dataset.endpoint + (refresh ? '?refresh=true' : ''), {
                signal: controller.signal,
                headers: { Accept: 'application/json' },
            });
            if (resp.status === 429) { failed = true; failureReason = 'Too many requests.'; }
            else if (!resp.ok) { failed = true; failureReason = 'Server error (HTTP ' + resp.status + ').'; }
            else {
                const data = await resp.json();
                rows.forEach((row) => {
                    const key = row.dataset.egressFamily;
                    show(row, describeFamily(familyLabel(key), data && data[key]));
                });
                const when = formatObserved(data && data.checked_at);
                checked.textContent = when || 'unknown';
                if (data && data.checked_at) checked.setAttribute('datetime', data.checked_at);
                else checked.removeAttribute('datetime');
                if (cachedNote) cachedNote.hidden = !(data && data.cached === true);
                if (proxyNote) proxyNote.hidden = !(data && data.proxy_env);
            }
        } catch (_error) {
            // The "Lookup failed" badge already says it; no extra detail line.
            failed = true;
        } finally {
            clearTimeout(timer);
            busy = false;
            refreshBtn.removeAttribute('aria-disabled');
            refreshBtn.removeAttribute('aria-busy');
            card.removeAttribute('aria-busy');
        }
        if (failed) {
            rows.forEach((row) => show(row, describeRequestFailure(known[row.dataset.egressFamily], failureReason)));
            live.textContent = 'Public address lookup failed.';
        } else {
            live.textContent = 'Public address lookup finished.';
        }
    }

    refreshBtn.addEventListener('click', (event) => {
        event.preventDefault();
        if (refreshBtn.getAttribute('aria-disabled') === 'true') return;
        load(true);
    });

    card.addEventListener('click', async (event) => {
        const button = event.target.closest('[data-egress-copy]');
        if (!button || button.disabled) return;
        event.preventDefault();
        const label = familyLabel(button.closest('[data-egress-family]').dataset.egressFamily);
        const icon = button.querySelector('.material-icons');
        try {
            await writeClipboard(button.dataset.egressValue);
            live.textContent = label + ' address copied.';
            if (icon) {
                icon.textContent = 'check';
                setTimeout(() => { icon.textContent = 'content_copy'; }, COPY_FEEDBACK_MS);
            }
        } catch (_error) {
            live.textContent = 'Copying the ' + label + ' address failed.';
        }
    });

    load(false);
}
