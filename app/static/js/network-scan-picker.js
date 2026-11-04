//
// app/static/js/network-scan-picker.js
// Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
//

// Port picker of the target wizard and the target form: manual, protocol-typed entry and Nmap discovery.
// The rules live in utils/port-scan-core.js; this file owns the DOM and the requests.
(() => {
  'use strict';
  const core = window.JuPortScan;
  if (!core) return;
  const MAX_FIELD_LENGTH = 200;
  const POLL_MS = 2000;

  // The scan API takes a bare host; URL.hostname keeps the brackets of an IPv6 literal.
  function scanHost(input) {
    const value = input.value.trim();
    if (!value) return '';
    if (!value.includes('://')) return value;
    try { return new URL(value).hostname.replace(/^\[(.*)\]$/, '$1'); } catch (_) { return value; }
  }
  // Error body of the scan API: a string, {code, message} or a 422 list of {msg}.
  function errorMessage(detail, status) {
    if (typeof detail === 'string') return detail;
    if (typeof detail?.message === 'string') return detail.message;
    if (Array.isArray(detail)) return detail.map(item => item?.msg).filter(Boolean).join('; ');
    return `Scan request failed (${status}).`;
  }
  async function responseJson(response) {
    let data;
    try { data = await response.json(); } catch (_) { data = null; }
    if (!response.ok) {
      const error = new Error(errorMessage(data?.detail, response.status));
      error.code = data?.detail?.code
        || (response.status === 422 ? 'invalid_host' : response.status === 429 ? 'busy' : [401, 403].includes(response.status) ? 'forbidden' : undefined);
      throw error;
    }
    return data;
  }
  function el(tag, className, text) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined) node.textContent = text;
    return node;
  }
  // Status badge of a scan row: icon and words, so an uncertain state is not told apart by colour alone.
  function stateBadge(info) {
    const variant = info.confirmed ? 'success' : 'neutral';
    const badge = el('span', `badge ju-status-badge--${variant} ju-state-badge${info.tone === 'uncertain' ? ' ju-state-badge--unknown' : ''}`);
    const icon = el('span', 'material-icons', info.icon);
    icon.setAttribute('aria-hidden', 'true');
    badge.append(icon, info.confirmed ? 'Open' : info.label);
    const hint = el('span', info.confirmed ? 'visually-hidden' : 'small text-body-secondary ms-1', info.confirmed ? ' (confirmed)' : `(${info.note})`);
    const cell = el('td');
    cell.append(badge, hint);
    return cell;
  }

  function init(root) {
    const form = root.closest('form');
    const byId = id => document.getElementById(id);
    const q = selector => root.querySelector(selector);
    const host = byId(root.dataset.hostId);
    const fields = { tcp: byId(root.dataset.tcpId), udp: byId(root.dataset.udpId) };
    const toggles = { tcp: byId(root.dataset.tcpToggleId), udp: byId(root.dataset.udpToggleId) };
    const scanId = form?.querySelector('[name="network_scan_id"]');
    const el_ = {
      start: q('[data-scan-action="start"]'), cancel: q('[data-scan-action="cancel"]'),
      selectOpen: q('[data-scan-action="select-open"]'), apply: q('[data-scan-action="apply"]'),
      recheck: q('[data-scan-action="recheck"]'), manualAdd: q('[data-scan-action="manual-add"]'),
      manualInput: q('[data-manual-input]'), manualError: q('[data-manual-error]'),
      chips: q('[data-port-chips]'), chipsEmpty: q('[data-port-chips-empty]'),
      status: q('[data-scan-status-text]'), spinner: q('[data-scan-spinner]'), error: q('[data-scan-error]'),
      results: q('[data-scan-results]'), rows: q('[data-scan-rows]'),
      notice: q('[data-scan-unavailable]'), noticeText: q('[data-scan-unavailable-text]'),
      profile: q('[data-scan-profile]')
    };
    const protocolBoxes = { tcp: q('[data-scan-protocol="tcp"]'), udp: q('[data-scan-protocol="udp"]') };
    const required = [host, fields.tcp, fields.udp, toggles.tcp, toggles.udp, scanId, protocolBoxes.tcp, protocolBoxes.udp, ...Object.values(el_)];
    if (!required.every(Boolean)) return;

    let job = null;
    let checkedHost = '';
    let checkedProtocols = [];
    let generation = 0;
    let timer = null;
    let controller = null;
    let unavailable = false;
    let checking = null;
    let lastCheck = 0;
    let recheckTimer = null;

    // ----- messages: one polite status line, one assertive error line -----
    function setStatus(text, busy = false) {
      el_.error.hidden = true;
      el_.error.textContent = '';
      el_.status.textContent = text;
      el_.spinner.hidden = !busy;
    }
    function setError(text) {
      el_.status.textContent = '';
      el_.spinner.hidden = true;
      el_.error.textContent = text;
      el_.error.hidden = !text;
    }
    function show(outcome) {
      if (outcome.tone === 'error') setError(outcome.message);
      else setStatus(outcome.message, outcome.kind === 'running');
    }

    // ----- manual entry and the selected-ports list -----
    function readPorts(protocol) {
      const parsed = core.parsePortList(fields[protocol].value);
      return parsed;
    }
    function writePorts(protocol, ports, { enable = true } = {}) {
      const input = fields[protocol];
      input.value = core.formatPorts(ports);
      // The TCP tag input listens for "change" on its hidden field; text fields report "input".
      input.dispatchEvent(new Event(input.type === 'hidden' ? 'change' : 'input', { bubbles: true }));
      if (enable && ports.length && !toggles[protocol].checked) {
        toggles[protocol].checked = true;
        toggles[protocol].dispatchEvent(new Event('change', { bubbles: true }));
      }
    }
    // Merges additions into both fields, or changes nothing: invalid existing text and an overlong value are refused.
    function addPorts(additions) {
      const plan = [];
      for (const protocol of core.PROTOCOLS) {
        if (!additions[protocol].length) continue;
        const existing = readPorts(protocol);
        if (existing.invalid.length) {
          return { error: `Correct the existing ${protocol.toUpperCase()} ports first (${existing.invalid.join(', ')}). Use numbers from 1 to 65535.` };
        }
        const merged = core.mergePorts(existing.ports, additions[protocol]);
        if (core.formatPorts(merged).length > MAX_FIELD_LENGTH) {
          return { error: `Too many ${protocol.toUpperCase()} ports: the list is limited to ${MAX_FIELD_LENGTH} characters.` };
        }
        plan.push({ protocol, merged, added: merged.length - existing.ports.length });
      }
      for (const { protocol, merged } of plan) writePorts(protocol, merged);
      return { added: Object.fromEntries(plan.map(item => [item.protocol, item.added])) };
    }
    function renderChips(focusAfter) {
      el_.chips.replaceChildren();
      let count = 0;
      for (const protocol of core.PROTOCOLS) {
        const parsed = readPorts(protocol);
        const values = [...parsed.ports, ...parsed.invalid];
        for (const value of values) {
          const invalid = parsed.invalid.includes(value);
          const label = `${protocol.toUpperCase()} ${value}`;
          const item = el('li', `tag-input__item${invalid ? ' is-invalid' : ''}`);
          item.append(el('span', 'tag-input__text', label));
          if (invalid) item.append(el('span', 'visually-hidden', ' (invalid)'));
          const remove = el('button', 'tag-input__remove');
          remove.type = 'button';
          remove.dataset.protocol = protocol;
          remove.dataset.value = String(value);
          remove.setAttribute('aria-label', `Remove ${protocol.toUpperCase()} port ${value}`);
          const mark = el('span', undefined, '×');
          mark.setAttribute('aria-hidden', 'true');
          remove.append(mark);
          item.append(remove);
          el_.chips.append(item);
          count += 1;
        }
      }
      el_.chipsEmpty.hidden = count > 0;
      if (focusAfter) {
        const next = el_.chips.querySelector('.tag-input__remove');
        (next || el_.manualInput).focus();
      }
    }
    function manualMessage(text) {
      el_.manualError.textContent = text;
      el_.manualError.hidden = !text;
      el_.manualInput.setAttribute('aria-invalid', text ? 'true' : 'false');
    }
    function addManual() {
      const parsed = core.parseTypedPorts(el_.manualInput.value);
      if (!parsed.tcp.length && !parsed.udp.length && !parsed.invalid.length) {
        manualMessage('Enter at least one port, for example 22, 443 or udp:53.');
        el_.manualInput.focus();
        return;
      }
      if (parsed.invalid.length) {
        manualMessage(`Not a valid port: ${parsed.invalid.join(', ')}. Use numbers from 1 to 65535, optionally as udp:53 or 53/udp.`);
        el_.manualInput.focus();
        return;
      }
      const outcome = addPorts(parsed);
      if (outcome.error) {
        manualMessage(outcome.error);
        el_.manualInput.focus();
        return;
      }
      manualMessage('');
      el_.manualInput.value = '';
      renderChips();
      const tcpAdded = outcome.added.tcp || 0;
      const udpAdded = outcome.added.udp || 0;
      setStatus(tcpAdded + udpAdded
        ? `Added ${tcpAdded} TCP and ${udpAdded} UDP port${tcpAdded + udpAdded === 1 ? '' : 's'}. Save the target to start monitoring them.`
        : 'Those ports are already in the list.');
      el_.manualInput.focus();
    }
    el_.manualAdd.addEventListener('click', addManual);
    el_.manualInput.addEventListener('keydown', event => {
      if (event.key !== 'Enter') return;
      event.preventDefault();
      addManual();
    });
    el_.manualInput.addEventListener('input', () => { if (!el_.manualError.hidden) manualMessage(''); });
    el_.chips.addEventListener('click', event => {
      const remove = event.target instanceof Element ? event.target.closest('.tag-input__remove') : null;
      if (!remove) return;
      const protocol = remove.dataset.protocol;
      const parsed = readPorts(protocol);
      const rest = [...parsed.ports.map(String), ...parsed.invalid].filter(value => value !== remove.dataset.value);
      // Invalid text stays as typed so the form validation still reports it; valid ports are re-normalised.
      writePorts(protocol, rest.map(value => (core.parsePort(value) === null ? value : Number(value))), { enable: false });
      renderChips(true);
      setStatus(`Removed ${protocol.toUpperCase()} port ${remove.dataset.value}. Save the target to apply the change.`);
    });
    for (const input of Object.values(fields)) {
      input.addEventListener('input', () => renderChips());
      input.addEventListener('change', () => renderChips());
    }

    // ----- protocols follow the active checks -----
    function syncProtocols() {
      const chosen = core.defaultProtocols(toggles.tcp.checked, toggles.udp.checked);
      for (const protocol of core.PROTOCOLS) protocolBoxes[protocol].checked = chosen.includes(protocol);
    }
    // Switching a check on or off follows with its own protocol only; the other box keeps the user's choice.
    for (const protocol of core.PROTOCOLS) {
      toggles[protocol].addEventListener('change', () => { protocolBoxes[protocol].checked = toggles[protocol].checked; });
    }

    // ----- scan state -----
    function stopPolling() {
      generation += 1;
      if (timer !== null) clearTimeout(timer);
      timer = null;
      controller?.abort();
      controller = null;
    }
    function ready() {
      el_.start.disabled = unavailable;
      el_.start.removeAttribute('aria-busy');
      el_.cancel.hidden = true;
    }
    // Scanning needs the Nmap worker. While it is down the button stays disabled and the visible reason is its description.
    function markUnavailable(reason) {
      unavailable = true;
      el_.noticeText.textContent = `${String(reason || 'Nmap scan worker is unavailable').replace(/[.\s]+$/, '')}. Port scanning is disabled; you can still enter ports manually.`;
      el_.notice.hidden = false;
      el_.start.setAttribute('aria-describedby', el_.noticeText.id);
      if (el_.cancel.hidden) el_.start.disabled = true;
      scheduleRecheck();
    }
    function markAvailable() {
      const wasUnavailable = unavailable;
      unavailable = false;
      el_.notice.hidden = true;
      el_.noticeText.textContent = '';
      el_.start.removeAttribute('aria-describedby');
      if (el_.cancel.hidden) el_.start.disabled = false;
      if (wasUnavailable && !job) setStatus('Nmap scan worker is available again.');
    }
    // The supervisor restarts the worker with backoff, so a down worker is re-checked slowly and on focus.
    function scheduleRecheck() {
      if (recheckTimer !== null) clearTimeout(recheckTimer);
      recheckTimer = unavailable ? window.setTimeout(() => refreshAvailability(), 30000) : null;
    }
    function refreshAvailability(force = false) {
      if (checking) return checking;
      if (!force && document.visibilityState === 'hidden') return Promise.resolve();
      if (!force && Date.now() - lastCheck < 5000) return Promise.resolve();
      checking = (async () => {
        try {
          const response = await window.apiFetch('/network-scans/status', { cache: 'no-store' });
          if (!response.ok) return;
          const data = await response.json();
          if (typeof data?.available !== 'boolean') return;
          if (data.available) markAvailable(); else markUnavailable(data.message);
        } catch (_) {
          // Unknown stays unchanged; starting a scan reports the real reason.
        } finally {
          checking = null;
          lastCheck = Date.now();
          scheduleRecheck();
        }
      })();
      return checking;
    }

    // ----- results -----
    function renderRows(rows) {
      el_.rows.replaceChildren();
      for (const row of rows) {
        const tr = el('tr');
        const choice = el('input', 'form-check-input');
        choice.type = 'checkbox';
        choice.dataset.protocol = row.protocol;
        choice.dataset.port = String(row.port);
        choice.dataset.state = row.state;
        choice.dataset.confirmed = row.info.confirmed ? 'true' : 'false';
        choice.setAttribute('aria-label', `Select ${row.protocol.toUpperCase()} port ${row.port}, ${row.info.confirmed ? 'confirmed open' : `${row.info.label.toLowerCase()}, ${row.info.note}`}`);
        const select = el('td');
        select.append(choice);
        // Scanner text is data: every cell is set through textContent.
        tr.append(select, el('td', undefined, row.protocol.toUpperCase()), el('td', undefined, String(row.port)), el('td', undefined, row.service), stateBadge(row.info));
        el_.rows.append(tr);
      }
      el_.results.hidden = !rows.length;
    }
    function finish(outcome) {
      ready();
      if (outcome.unavailable) markUnavailable(outcome.reason);
      if (outcome.kind === 'results' || outcome.kind === 'uncertain-only') renderRows(outcome.rows);
      else el_.results.hidden = true;
      show(outcome.unavailable ? { ...outcome, message: '' } : outcome);
    }
    async function poll(id, sequence) {
      if (sequence !== generation) return;
      controller = new AbortController();
      try {
        const response = await window.apiFetch(`/network-scans/${encodeURIComponent(id)}`, { signal: controller.signal });
        const data = await responseJson(response);
        // A newer scan, a cancel or a changed host makes this answer stale.
        if (sequence !== generation || scanHost(host) !== checkedHost) return;
        job = data;
        const outcome = core.describeJob(data, checkedProtocols);
        if (outcome.kind === 'running') {
          show(outcome);
          timer = window.setTimeout(() => poll(id, sequence), POLL_MS);
        } else {
          finish(outcome);
        }
      } catch (error) {
        if (sequence !== generation || error.name === 'AbortError') return;
        finish(core.describeRequestError(error));
      } finally {
        controller = null;
      }
    }
    el_.start.addEventListener('click', async () => {
      if (unavailable) return;
      const target = scanHost(host);
      const protocols = core.PROTOCOLS.filter(protocol => protocolBoxes[protocol].checked);
      if (!target) { setError('Enter a host before scanning.'); host.focus(); return; }
      if (!protocols.length) { setError('Choose TCP or UDP before scanning.'); return; }
      stopPolling();
      scanId.value = '';
      el_.results.hidden = true;
      checkedHost = target;
      checkedProtocols = protocols;
      job = null;
      el_.start.disabled = true;
      el_.start.setAttribute('aria-busy', 'true');
      el_.cancel.hidden = false;
      setStatus('Starting scan…', true);
      const sequence = generation;
      controller = new AbortController();
      try {
        const response = await window.apiFetch('/network-scans', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ host: target, protocols, profile: el_.profile.value }),
          signal: controller.signal
        });
        const data = await responseJson(response);
        if (sequence !== generation || scanHost(host) !== checkedHost) return;
        if (typeof data?.id !== 'string') throw new Error('The scan response is missing a job ID.');
        job = data;
        await poll(data.id, sequence);
      } catch (error) {
        if (sequence !== generation || error.name === 'AbortError') return;
        finish(core.describeRequestError(error));
      } finally {
        controller = null;
      }
    });
    el_.cancel.addEventListener('click', async () => {
      const id = job?.id;
      stopPolling();
      ready();
      el_.results.hidden = true;
      scanId.value = '';
      job = null;
      setStatus('Scan cancelled. You can still enter ports manually.');
      el_.start.focus();
      if (id) {
        try {
          const response = await window.apiFetch(`/network-scans/${encodeURIComponent(id)}`, { method: 'DELETE' });
          if (!response.ok) throw new Error('Scan cancellation failed');
        } catch (_) { setError('Could not confirm that the scan was cancelled. It stops by itself when it times out.'); }
      }
    });
    host.addEventListener('input', () => {
      if (!job && !scanId.value && el_.cancel.hidden) return;
      if (scanHost(host) === checkedHost) return;
      const id = job?.id;
      const wasRunning = job && !core.TERMINAL.includes(job.status);
      stopPolling();
      ready();
      el_.results.hidden = true;
      job = null;
      scanId.value = '';
      setStatus('Host changed. Scan again to discover ports for the new host.');
      if (id && wasRunning) window.apiFetch(`/network-scans/${encodeURIComponent(id)}`, { method: 'DELETE' }).catch(() => {});
    });
    // Additive: confirmed rows are ticked, nothing the user ticked before is removed.
    el_.selectOpen.addEventListener('click', () => {
      const confirmed = [...el_.rows.querySelectorAll('input[type="checkbox"][data-confirmed="true"]')];
      for (const choice of confirmed) choice.checked = true;
      setStatus(confirmed.length
        ? `${confirmed.length} confirmed open port${confirmed.length === 1 ? '' : 's'} selected.`
        : 'No confirmed open ports to select. Tick uncertain ports yourself if you want to monitor them.');
    });
    el_.apply.addEventListener('click', () => {
      if (job?.status !== 'completed' || scanHost(host) !== checkedHost) return;
      const selected = [...el_.rows.querySelectorAll('input[type="checkbox"]:checked')];
      if (!selected.length) { setError('Select at least one port to add.'); return; }
      const additions = { tcp: [], udp: [] };
      for (const choice of selected) additions[choice.dataset.protocol].push(Number(choice.dataset.port));
      const outcome = addPorts(additions);
      if (outcome.error) { setError(outcome.error); return; }
      scanId.value = job.id;
      renderChips();
      const tcpAdded = outcome.added.tcp || 0;
      const udpAdded = outcome.added.udp || 0;
      setStatus(tcpAdded + udpAdded
        ? `Added ${tcpAdded} TCP and ${udpAdded} UDP port${tcpAdded + udpAdded === 1 ? '' : 's'}. Save the target to start monitoring them.`
        : 'The selected ports were already in the list. Nothing changed.');
    });
    el_.recheck.addEventListener('click', () => refreshAvailability(true));
    document.addEventListener('visibilitychange', () => { if (document.visibilityState === 'visible') refreshAvailability(); });
    window.addEventListener('focus', () => refreshAvailability());
    window.addEventListener('pagehide', () => {
      stopPolling();
      if (recheckTimer !== null) clearTimeout(recheckTimer);
    }, { once: true });
    syncProtocols();
    renderChips();
    refreshAvailability(true);
  }
  document.querySelectorAll('[data-network-scan-picker]').forEach(init);
})();
