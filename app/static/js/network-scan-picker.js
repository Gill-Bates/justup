//
// app/static/js/network-scan-picker.js
// Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
//

(() => {
  'use strict';

  const terminal = new Set(['completed', 'failed', 'cancelled', 'expired']);
  const portPattern = /^[0-9]+$/;

  function parsePorts(value) {
    if (!value.trim()) return [];
    const ports = value.split(',').map(part => part.trim());
    if (ports.some(part => !portPattern.test(part) || Number(part) < 1 || Number(part) > 65535)) {
      throw new Error('Correct the existing ports before adding scan results. Use numbers from 1 to 65535.');
    }
    return [...new Set(ports.map(Number))];
  }

  function scanHost(input) {
    const value = input.value.trim();
    if (!value) return '';
    if (!value.includes('://')) return value;
    // URL.hostname wraps IPv6 in brackets, which the backend host validator rejects.
    try { return new URL(value).hostname.replace(/^\[(.*)\]$/, '$1'); } catch (_) { return value; }
  }

  function errorMessage(detail, status) {
    if (typeof detail === 'string') return detail;
    if (typeof detail?.message === 'string') return detail.message;
    return `Scan request failed (${status}).`;
  }

  async function responseJson(response) {
    let data;
    try { data = await response.json(); } catch (_) { data = null; }
    if (!response.ok) {
      const error = new Error(errorMessage(data?.detail, response.status));
      error.code = data?.detail?.code;
      throw error;
    }
    return data;
  }

  function init(root) {
    const form = root.closest('form');
    const byId = id => document.getElementById(id);
    const host = byId(root.dataset.hostId);
    const tcp = byId(root.dataset.tcpId);
    const udp = byId(root.dataset.udpId);
    const tcpToggle = byId(root.dataset.tcpToggleId);
    const udpToggle = byId(root.dataset.udpToggleId);
    const scanId = form?.querySelector('[name="network_scan_id"]');
    const start = root.querySelector('[data-scan-action="start"]');
    const cancel = root.querySelector('[data-scan-action="cancel"]');
    const selectOpen = root.querySelector('[data-scan-action="select-open"]');
    const apply = root.querySelector('[data-scan-action="apply"]');
    const status = root.querySelector('[data-scan-status]');
    const results = root.querySelector('[data-scan-results]');
    const rows = root.querySelector('[data-scan-rows]');
    const notice = root.querySelector('[data-scan-unavailable]');
    const noticeText = root.querySelector('[data-scan-unavailable-text]');
    const recheck = root.querySelector('[data-scan-action="recheck"]');
    if (![host, tcp, udp, tcpToggle, udpToggle, scanId, start, cancel, selectOpen, apply, status, results, rows, notice, noticeText, recheck].every(Boolean)) return;

    let job = null;
    let checkedHost = '';
    let generation = 0;
    let timer = null;
    let controller = null;
    let unavailable = false;
    let checking = null;
    let lastCheck = 0;
    let recheckTimer = null;

    function message(value, error = false) {
      status.textContent = value;
      status.setAttribute('role', error ? 'alert' : 'status');
      status.classList.toggle('text-danger', error);
    }

    function stopPolling() {
      generation += 1;
      if (timer !== null) clearTimeout(timer);
      timer = null;
      controller?.abort();
      controller = null;
    }

    function ready() {
      start.disabled = unavailable;
      cancel.hidden = true;
    }

    // Scanning needs the Nmap worker. While it is down the button stays disabled and the visible reason is its description.
    function markUnavailable(reason) {
      unavailable = true;
      const text = String(reason || 'Nmap scan worker is unavailable').trim();
      noticeText.textContent = `${text}${/[.!?]$/.test(text) ? '' : '.'} Port scanning is disabled; you can still enter ports manually.`;
      notice.hidden = false;
      start.setAttribute('aria-describedby', noticeText.id);
      if (cancel.hidden) start.disabled = true;
      scheduleRecheck();
    }

    function markAvailable() {
      const wasUnavailable = unavailable;
      unavailable = false;
      notice.hidden = true;
      noticeText.textContent = '';
      start.removeAttribute('aria-describedby');
      if (cancel.hidden) start.disabled = false;
      if (wasUnavailable && !job) message('Nmap scan worker is available again.');
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

    function render(data) {
      rows.replaceChildren();
      const found = new Set();
      const ports = [];
      for (const result of Array.isArray(data?.hosts) ? data.hosts : []) {
        for (const port of Array.isArray(result?.ports) ? result.ports : []) {
          if (!['tcp', 'udp'].includes(port?.protocol) || !Number.isInteger(port.port) || port.port < 1 || port.port > 65535) continue;
          const key = `${port.protocol}:${port.port}`;
          if (found.has(key)) continue;
          found.add(key);
          ports.push(port);
        }
      }
      ports.sort((a, b) => a.protocol.localeCompare(b.protocol) || a.port - b.port);
      for (const port of ports) {
        const row = document.createElement('tr');
        const choice = document.createElement('input');
        choice.type = 'checkbox';
        choice.className = 'form-check-input';
        choice.dataset.protocol = port.protocol;
        choice.dataset.port = String(port.port);
        choice.dataset.state = String(port.state || 'unknown');
        choice.setAttribute('aria-label', `Select ${port.protocol.toUpperCase()} port ${port.port} (${choice.dataset.state})`);
        const cell = document.createElement('td');
        cell.append(choice);
        row.append(cell);
        for (const value of [port.protocol.toUpperCase(), port.port, port.state || 'unknown']) {
          const item = document.createElement('td');
          item.textContent = String(value);
          row.append(item);
        }
        const service = document.createElement('td');
        const info = port.service;
        const name = info?.name || 'Unknown';
        service.textContent = info?.method === 'probed'
          ? [name, info.product, info.version].filter(Boolean).join(' · ')
          : info?.method === 'table' ? `${name} (table hint)` : name;
        row.append(service);
        rows.append(row);
      }
      results.hidden = !ports.length;
      message(ports.length ? `${ports.length} port${ports.length === 1 ? '' : 's'} found. Choose ports to monitor.` : 'No ports found. You can still enter ports manually.');
    }

    async function poll(id, sequence) {
      if (sequence !== generation) return;
      controller = new AbortController();
      try {
        const response = await window.apiFetch(`/network-scans/${encodeURIComponent(id)}`, { signal: controller.signal });
        const data = await responseJson(response);
        if (sequence !== generation || scanHost(host) !== checkedHost) return;
        if (!['queued', 'running', ...terminal].includes(data?.status)) throw new Error('Invalid scan status.');
        job = data;
        if (data.status === 'completed') {
          render(data.result);
          ready();
        } else if (terminal.has(data.status)) {
          results.hidden = true;
          if (data.error?.code === 'unavailable') markUnavailable(data.error.message);
          ready();
          message(data.error?.code === 'unavailable' ? '' : data.error?.message || `Scan ${data.status}. Enter ports manually or try again.`, data.status === 'failed' && data.error?.code !== 'unavailable');
        } else {
          message(data.status === 'queued' ? 'Scan queued…' : 'Scanning ports…');
          timer = window.setTimeout(() => poll(id, sequence), 2000);
        }
      } catch (error) {
        if (sequence !== generation || error.name === 'AbortError') return;
        if (error.code === 'unavailable') markUnavailable(error.message);
        ready();
        message(error.code === 'unavailable' ? '' : error.message || 'Scan failed. Enter ports manually or try again.', error.code !== 'unavailable');
      } finally {
        controller = null;
      }
    }

    start.addEventListener('click', async () => {
      if (unavailable) return;
      const target = scanHost(host);
      const protocols = ['tcp', 'udp'].filter(protocol => root.querySelector(`[data-scan-protocol="${protocol}"]`)?.checked);
      if (!target || !protocols.length) {
        message(!target ? 'Enter a host before scanning.' : 'Choose TCP or UDP before scanning.', true);
        return;
      }
      stopPolling();
      scanId.value = '';
      results.hidden = true;
      checkedHost = target;
      job = null;
      start.disabled = true;
      cancel.hidden = false;
      message('Starting scan…');
      const sequence = generation;
      controller = new AbortController();
      try {
        const response = await window.apiFetch('/network-scans', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ host: target, protocols, profile: root.querySelector('[data-scan-profile]').value }),
          signal: controller.signal
        });
        const data = await responseJson(response);
        if (sequence !== generation || scanHost(host) !== checkedHost) return;
        if (typeof data?.id !== 'string') throw new Error('Scan response is missing a job ID.');
        job = data;
        await poll(data.id, sequence);
      } catch (error) {
        if (sequence !== generation || error.name === 'AbortError') return;
        if (error.code === 'unavailable') markUnavailable(error.message);
        ready();
        message(error.code === 'unavailable' ? '' : error.message || 'Unable to start the scan. Enter ports manually or try again.', error.code !== 'unavailable');
      } finally {
        controller = null;
      }
    });

    cancel.addEventListener('click', async () => {
      const id = job?.id;
      stopPolling();
      ready();
      results.hidden = true;
      scanId.value = '';
      job = null;
      message('Scan cancelled. You can still enter ports manually.');
      if (id) {
        try {
          const response = await window.apiFetch(`/network-scans/${encodeURIComponent(id)}`, { method: 'DELETE' });
          if (!response.ok) throw new Error('Scan cancellation failed');
        }
        catch (_) { message('Could not confirm scan cancellation.', true); }
      }
    });

    host.addEventListener('input', () => {
      if (!job && !scanId.value && cancel.hidden) return;
      if (scanHost(host) === checkedHost) return;
      const id = job?.id;
      const wasRunning = job && !terminal.has(job.status);
      stopPolling();
      ready();
      results.hidden = true;
      job = null;
      scanId.value = '';
      message('Host changed. Scan again to use discovered service details.');
      if (id && wasRunning) window.apiFetch(`/network-scans/${encodeURIComponent(id)}`, { method: 'DELETE' }).catch(() => {});
    });

    selectOpen.addEventListener('click', () => {
      for (const choice of rows.querySelectorAll('input[type="checkbox"]')) choice.checked = choice.dataset.state === 'open';
    });

    apply.addEventListener('click', () => {
      if (job?.status !== 'completed' || scanHost(host) !== checkedHost) return;
      const selected = [...rows.querySelectorAll('input[type="checkbox"]:checked')];
      if (!selected.length) { message('Select at least one port to add.', true); return; }
      try {
        const values = [];
        for (const [protocol, input, toggle] of [['tcp', tcp, tcpToggle], ['udp', udp, udpToggle]]) {
          const additions = selected.filter(choice => choice.dataset.protocol === protocol).map(choice => Number(choice.dataset.port));
          if (!additions.length) continue;
          values.push([input, toggle, [...new Set([...parsePorts(input.value), ...additions])].sort((a, b) => a - b).join(',')]);
        }
        for (const [input, toggle, value] of values) {
          input.value = value;
          input.dispatchEvent(new Event(input.type === 'hidden' ? 'change' : 'input', { bubbles: true }));
          toggle.checked = true;
          toggle.dispatchEvent(new Event('change', { bubbles: true }));
        }
        scanId.value = job.id;
        message('Selected ports added. Save the target to start monitoring them.');
      } catch (error) { message(error.message, true); }
    });

    recheck.addEventListener('click', () => refreshAvailability(true));
    document.addEventListener('visibilitychange', () => { if (document.visibilityState === 'visible') refreshAvailability(); });
    window.addEventListener('focus', () => refreshAvailability());
    window.addEventListener('pagehide', () => {
      stopPolling();
      if (recheckTimer !== null) clearTimeout(recheckTimer);
    }, { once: true });
    refreshAvailability(true);
  }

  document.querySelectorAll('[data-network-scan-picker]').forEach(init);
})();
