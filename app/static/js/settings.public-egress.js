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

const REASONS = {
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
};

const BADGES = {
    loading: ['Checking\u2026', 'ju-status-badge--neutral'],
    available: ['Available', 'ju-status-badge--success'],
    unavailable: ['Unavailable', 'ju-status-badge--neutral'],
    failed: ['Lookup failed', 'ju-status-badge--danger'],
    stale: ['Stale', 'ju-status-badge--warning'],
    inconsistent: ['Inconsistent', 'ju-status-badge--warning'],
};

/** Format an ISO timestamp for display; an unparsable value yields an empty string. */
export function formatObserved(iso) {
    const date = new Date(iso);
    if (!iso || Number.isNaN(date.getTime())) return '';
    return date.toLocaleString(undefined, { dateStyle: 'medium', timeStyle: 'medium' });
}

/**
 * Turn one family entry of the API response into display state. `family` may be missing or malformed;
 * that is a failed lookup, never an address.
 */
export function describeFamily(label, family) {
    const status = family && typeof family.status === 'string' && BADGES[family.status] ? family.status : 'failed';
    const reason = (family && REASONS[family.reason]) || '';
    const [badgeText, badgeClass] = BADGES[status];
    const view = { status, badgeText, badgeClass, address: '', detail: '', canCopy: false };
    if (status === 'available') {
        view.address = family.address;
        view.canCopy = typeof family.address === 'string' && family.address !== '';
        const sources = Array.isArray(family.sources) ? family.sources.length : 0;
        view.detail = sources > 1 ? 'Confirmed by ' + sources + ' independent services.' : 'Reported by one service.';
    } else if (status === 'stale') {
        view.address = family.address;
        view.canCopy = typeof family.address === 'string' && family.address !== '';
        const seen = formatObserved(family.observed_at);
        view.detail = 'Last confirmed' + (seen ? ' ' + seen : '') + '; not confirmed now. ' + reason;
    } else if (status === 'inconsistent') {
        const seen = Array.isArray(family.observed) ? family.observed.filter((a) => typeof a === 'string') : [];
        view.detail = reason + (seen.length ? ' Observed: ' + seen.join(', ') + '.' : '');
    } else if (status === 'unavailable') {
        view.detail = label + ' is not available from this server. ' + reason;
    } else if (status === 'failed') {
        view.detail = reason || 'The lookup failed.';
    }
    return view;
}

function familyLabel(key) {
    return key === 'ipv6' ? 'IPv6' : 'IPv4';
}

function renderFamily(row, view) {
    const key = row.dataset.egressFamily;
    row.dataset.egressState = view.status;
    const address = row.querySelector('[data-egress-address]');
    const badge = row.querySelector('[data-egress-badge]');
    const detail = row.querySelector('[data-egress-detail]');
    const copy = row.querySelector('[data-egress-copy]');
    address.textContent = view.address || '\u2014';
    address.classList.toggle('text-body-secondary', !view.address);
    badge.textContent = view.badgeText;
    badge.className = 'badge ' + view.badgeClass;
    detail.textContent = view.detail;
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
    const refreshBtn = card.querySelector('[data-egress-refresh]');
    const checked = card.querySelector('[data-egress-checked]');
    const proxyNote = card.querySelector('[data-egress-proxy]');
    const live = card.querySelector('[data-egress-live]');
    const apiFetch = window.apiFetch || ((url, options) => fetch(url, { credentials: 'same-origin', ...options }));
    let busy = false;

    const showAll = (status) => {
        rows.forEach((row) => renderFamily(row, describeFamily(familyLabel(row.dataset.egressFamily), { status })));
    };

    async function load(refresh) {
        if (busy) return;
        busy = true;
        refreshBtn.setAttribute('aria-disabled', 'true');
        refreshBtn.setAttribute('aria-busy', 'true');
        showAll('loading');
        const controller = new AbortController();
        const timer = setTimeout(() => controller.abort(), REQUEST_TIMEOUT_MS);
        let failureReason = '';
        try {
            const resp = await apiFetch(card.dataset.endpoint + (refresh ? '?refresh=true' : ''), {
                signal: controller.signal,
                headers: { Accept: 'application/json' },
            });
            if (resp.status === 429) failureReason = 'Too many requests. Try again in a moment.';
            else if (!resp.ok) failureReason = 'The server could not run the lookup (HTTP ' + resp.status + ').';
            else {
                const data = await resp.json();
                rows.forEach((row) => {
                    const key = row.dataset.egressFamily;
                    renderFamily(row, describeFamily(familyLabel(key), data && data[key]));
                });
                const when = formatObserved(data && data.checked_at);
                checked.textContent = when || 'unknown';
                if (data && data.checked_at) checked.setAttribute('datetime', data.checked_at);
                if (proxyNote) proxyNote.hidden = !(data && data.proxy_env);
            }
        } catch (_error) {
            failureReason = 'justUp! could not be reached for the lookup.';
        } finally {
            clearTimeout(timer);
            busy = false;
            refreshBtn.removeAttribute('aria-disabled');
            refreshBtn.removeAttribute('aria-busy');
        }
        if (failureReason) {
            showAll('failed');
            rows.forEach((row) => {
                row.querySelector('[data-egress-detail]').textContent = failureReason;
            });
            checked.textContent = 'not checked';
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

    showAll('loading');
    load(false);
}
