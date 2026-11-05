//
// app/static/js/target-wizard.js
// Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
//

(() => {
  'use strict';

  const form = document.getElementById('target-wizard-form');
  if (!form) return;

  const steps = [...form.querySelectorAll('.target-wizard-step')];
  const markers = [...document.querySelectorAll('#wizard-progress li')];
  const errorBox = document.getElementById('wizard-error');
  const nextButton = document.getElementById('wizard-next');
  const backButton = document.getElementById('wizard-back');
  const saveButton = document.getElementById('wizard-save');
  const cancelLink = document.getElementById('wizard-cancel');
  const fields = {
    address: document.getElementById('wizard-address'),
    ports: document.getElementById('wizard-ports'),
    udpPorts: document.getElementById('wizard-udp-ports'),
    manualPorts: document.getElementById('wizard-manual-ports'),
    manualPortsError: document.getElementById('wizard-manual-ports-error'),
    name: document.getElementById('wizard-name'),
    group: document.getElementById('wizard-group'),
    http: document.getElementById('wizard-http'),
    ping: document.getElementById('wizard-ping'),
    tcp: document.getElementById('wizard-tcp'),
    udp: document.getElementById('wizard-udp'),
    cert: document.getElementById('wizard-cert'),
    url: document.getElementById('wizard-http-url'),
    method: document.getElementById('wizard-method'),
    codes: document.getElementById('wizard-codes'),
    interval: document.getElementById('wizard-interval'),
    retries: document.getElementById('wizard-retries'),
    retention: document.getElementById('wizard-retention'),
    autoClose: document.getElementById('wizard-auto-close'),
    ignoreCert: document.getElementById('wizard-ignore-cert')
  };
  const overrideFields = {
    enable_http_check: fields.http,
    enable_ping: fields.ping,
    enable_tcp_connect: fields.tcp,
    enable_udp_scan: fields.udp,
    enable_cert_expiration: fields.cert,
    http_url: fields.url,
    http_prefer_head: fields.method,
    http_success_codes: fields.codes,
    interval_seconds: fields.interval,
    max_retries: fields.retries,
    retention_days: fields.retention,
    auto_close_alert: fields.autoClose,
    ignore_cert_errors: fields.ignoreCert
  };
  // Overrides that belong to a field group are only sent while the check that owns the group is active.
  const overrideGroups = {
    http_url: 'http',
    http_prefer_head: 'http',
    http_success_codes: 'http',
    ignore_cert_errors: 'tls'
  };
  // Ports are entered once (step 2) in the picker; wizard-ports and wizard-udp-ports are hidden backing fields.
  // The checks step only shows them and links back to the visible manual input.
  const portSummaries = {
    tcp: { input: fields.ports, toggle: fields.tcp, label: 'TCP', summary: document.getElementById('wizard-tcp-summary'), value: document.getElementById('wizard-tcp-summary-value'), error: document.getElementById('wizard-tcp-summary-error') },
    udp: { input: fields.udpPorts, toggle: fields.udp, label: 'UDP', summary: document.getElementById('wizard-udp-summary'), value: document.getElementById('wizard-udp-summary-value'), error: document.getElementById('wizard-udp-summary-error') }
  };
  const groupLabels = { http: 'HTTP settings', tls: 'TLS certificate option', tcp: 'TCP port field', udp: 'UDP port field' };
  const dirty = new Set();
  let shownGroups = {};
  let step = 0;
  let reviewedValues = null;
  let conflicts = [];
  let saving = false;
  let recipientsLoaded = false;

  function profile() {
    return form.querySelector('input[name="profile"]:checked')?.value || 'website';
  }

  function errorDetailMessage(detail, fallback) {
    return window.JuApiError.message(detail, fallback);
  }
  function clearError() {
    errorBox.classList.add('d-none');
    errorBox.textContent = '';
    for (const field of form.querySelectorAll('.is-invalid')) {
      field.classList.remove('is-invalid');
      field.removeAttribute('aria-invalid');
      const message = document.getElementById(`${field.id}-error`);
      if (message) message.textContent = '';
    }
    if (fields.manualPortsError) {
      fields.manualPortsError.textContent = '';
      fields.manualPortsError.hidden = true;
    }
  }

  function showError(message, field) {
    errorBox.textContent = message;
    errorBox.classList.remove('d-none');
    if (field) {
      field.classList.add('is-invalid');
      field.setAttribute('aria-invalid', 'true');
      let detail = document.getElementById(`${field.id}-error`);
      if (!detail) {
        detail = document.createElement('div');
        detail.id = `${field.id}-error`;
        detail.className = 'invalid-feedback';
        detail.setAttribute('role', 'alert');
        field.after(detail);
        // Keep the existing help-text reference; the error is added to it, not swapped in.
        const describedBy = (field.getAttribute('aria-describedby') || '').split(/\s+/).filter(Boolean);
        if (!describedBy.includes(detail.id)) field.setAttribute('aria-describedby', [...describedBy, detail.id].join(' '));
      }
      detail.textContent = message;
      field.focus();
    } else {
      errorBox.focus();
    }
  }

  function setStep(index, { scroll = true } = {}) {
    step = index;
    steps.forEach((section, position) => { section.hidden = position !== step; });
    markers.forEach((marker, position) => {
      marker.classList.toggle('is-current', position === step);
      marker.classList.toggle('is-complete', position < step);
      if (position === step) marker.setAttribute('aria-current', 'step');
      else marker.removeAttribute('aria-current');
    });
    backButton.hidden = step === 0;
    cancelLink.hidden = step !== 0;
    nextButton.hidden = step === steps.length - 1;
    saveButton.hidden = step !== steps.length - 1;
    clearError();
    updatePortSummaries();
    const heading = steps[step].querySelector('h2');
    heading?.focus({ preventScroll: true });
    if (scroll) heading?.scrollIntoView({ block: 'nearest' });
  }

  function updateKindFields() {
    const kind = profile();
    // An active port check on another profile still needs the port fields in step 2.
    const portsNeeded = fields.tcp.checked || fields.udp.checked;
    for (const element of form.querySelectorAll('.wizard-kind')) {
      const onDemand = 'portsOnDemand' in element.dataset && portsNeeded;
      element.hidden = !onDemand && !element.dataset.kinds.split(' ').includes(kind);
    }
    document.getElementById('wizard-address-label').textContent =
      ['website', 'api'].includes(kind) ? 'Full URL or domain *' : 'Hostname or IP address *';
    fields.address.autocomplete = ['website', 'api'].includes(kind) ? 'url' : 'off';
  }

  function activeChecks() {
    return { http: fields.http.checked, ping: fields.ping.checked, tcp: fields.tcp.checked, udp: fields.udp.checked, cert: fields.cert.checked };
  }

  // Shows the field groups of the active checks; a user toggle is announced through the status region.
  function updateCheckGroups({ announce = false } = {}) {
    const visible = window.JuCheckGroups.apply(form, activeChecks());
    if (announce) {
      const changes = Object.keys(groupLabels)
        .filter(group => visible[group] !== Boolean(shownGroups[group]))
        .map(group => `${groupLabels[group]} ${visible[group] ? 'shown' : 'hidden'}`);
      document.getElementById('wizard-checks-status').textContent = changes.join('. ');
    }
    shownGroups = visible;
  }

  function updatePortSummaries() {
    for (const { input, value } of Object.values(portSummaries)) {
      value.textContent = input.value.trim().replace(/\s*,\s*/g, ', ') || 'None chosen yet';
    }
  }

  function editPorts(protocol) {
    updateKindFields();
    setStep(1);
    const input = fields.manualPorts;
    input.focus();
    input.scrollIntoView?.({ block: 'center' });
  }

  // A port problem on the address step is shown at the visible manual input, never at the hidden backing fields.
  function showAddressPortsError(message) {
    errorBox.textContent = message;
    errorBox.classList.remove('d-none');
    const input = fields.manualPorts;
    input.classList.add('is-invalid');
    input.setAttribute('aria-invalid', 'true');
    fields.manualPortsError.textContent = message;
    fields.manualPortsError.hidden = false;
    input.focus();
  }

  // Text typed into the manual input but not yet added counts as chosen ports; invalid text blocks Continue.
  function commitPendingPorts() {
    if (!fields.manualPorts.value.trim()) return true;
    form.querySelector('[data-scan-action="manual-add"]')?.click();
    if (fields.manualPortsError.hidden) return true;
    errorBox.textContent = fields.manualPortsError.textContent;
    errorBox.classList.remove('d-none');
    return false;
  }

  // Reports a port problem at the summary of the checks step and focuses its Edit ports action.
  function showPortsError(protocol, message) {
    const { summary, error } = portSummaries[protocol];
    errorBox.textContent = message;
    errorBox.classList.remove('d-none');
    summary.classList.add('is-invalid');
    error.textContent = message;
    form.querySelector(`[data-edit-ports="${protocol}"]`)?.focus();
  }

  function suggestName() {
    if (fields.name.dataset.userEdited === 'true') return;
    const address = fields.address.value.trim();
    if (!address) return;
    try {
      fields.name.value = new URL(address.includes('://') ? address : `https://${address}`).hostname;
    } catch (_) {
      fields.name.value = address.split('/')[0];
    }
  }

  function readOverride(key, field) {
    if (field.type === 'checkbox') return field.checked;
    if (key === 'http_prefer_head') return field.value === 'HEAD';
    if (['interval_seconds', 'max_retries', 'retention_days'].includes(key)) return Number(field.value);
    return field.value.trim() || null;
  }

  function requestBody() {
    const overrides = {};
    const visible = window.JuCheckGroups.visibleGroups(activeChecks());
    for (const [key, field] of Object.entries(overrideFields)) {
      if (!dirty.has(key)) continue;
      if (key in overrideGroups && !visible[overrideGroups[key]]) continue;
      overrides[key] = readOverride(key, field);
    }
    return {
      profile: profile(),
      address: fields.address.value.trim(),
      name: fields.name.value.trim() || null,
      group: fields.group.value || null,
      tcp_ports: fields.ports.value.trim() || null,
      udp_ports: fields.udpPorts.value.trim() || null,
      overrides
    };
  }

  function showProfileValues(values) {
    for (const [key, field] of Object.entries(overrideFields)) {
      if (dirty.has(key)) continue;
      const value = values[key];
      if (field.type === 'checkbox') field.checked = Boolean(value);
      else if (key === 'http_prefer_head') field.value = value ? 'HEAD' : 'GET';
      else field.value = value ?? '';
    }
    if (values.name && !fields.name.value.trim()) fields.name.value = values.name;
    updateCheckGroups();
  }

  async function preview() {
    const response = await window.apiFetch('/targets/preview', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(requestBody())
    });
    if (!response.ok) {
      let detail;
      try { detail = (await response.json()).detail; } catch (_) { /* use status below */ }
      throw new Error(errorDetailMessage(detail, `Unable to preview target (${response.status}).`));
    }
    return response.json();
  }

  function validateAddress() {
    if (!fields.address.value.trim()) {
      showError('Enter a URL, hostname, or IP address.', fields.address);
      return false;
    }
    if (!commitPendingPorts()) return false;
    if (profile() === 'tcp' && !fields.ports.value.trim() && !fields.udpPorts.value.trim()) {
      showAddressPortsError('Choose at least one TCP or UDP port.');
      return false;
    }
    if (fields.ports.value.trim() && !validPorts(fields.ports.value)) {
      showAddressPortsError('Enter TCP port numbers from 1 to 65535, separated by commas.');
      return false;
    }
    if (fields.udpPorts.value.trim() && !validPorts(fields.udpPorts.value)) {
      showAddressPortsError('Enter UDP port numbers from 1 to 65535, separated by commas.');
      return false;
    }
    return true;
  }

  // Same rule as the port picker, so "22,,80" or a trailing comma passes in both places.
  function validPorts(value) {
    const parsed = window.JuPortScan.parsePortList(value);
    return parsed.invalid.length === 0 && parsed.ports.length > 0;
  }

  function validateChecks() {
    for (const field of [fields.interval, fields.retries, fields.retention]) {
      if (!field.checkValidity()) {
        showError(`Enter a valid value for ${field.previousElementSibling?.textContent || 'this setting'}.`, field);
        return false;
      }
    }
    if (!fields.http.checked && !fields.ping.checked && !fields.tcp.checked && !fields.udp.checked) {
      showError('Enable at least one monitoring check.');
      return false;
    }
    for (const [protocol, { input, toggle, label }] of Object.entries(portSummaries)) {
      if (!toggle.checked) continue;
      if (!input.value.trim()) {
        showPortsError(protocol, `Choose at least one ${label} port.`);
        return false;
      }
      if (!validPorts(input.value)) {
        showPortsError(protocol, `Enter ${label} port numbers from 1 to 65535, separated by commas.`);
        return false;
      }
    }
    return true;
  }

  function addSummaryRow(list, label, value) {
    const item = document.createElement('div');
    item.className = 'row border-bottom py-2';
    const heading = document.createElement('dt');
    heading.className = 'col-sm-4 text-body-secondary';
    heading.textContent = label;
    const description = document.createElement('dd');
    description.className = 'col-sm-8 mb-0';
    description.textContent = value == null || value === '' ? '—' : String(value);
    item.append(heading, description);
    list.append(item);
  }

  function renderReview() {
    const values = reviewedValues;
    const summary = document.getElementById('wizard-summary');
    summary.replaceChildren();
    const list = document.createElement('dl');
    list.className = 'mb-0';
    addSummaryRow(list, 'Name / group', `${values.name} / ${values.group || 'Ungrouped'}`);
    addSummaryRow(list, 'Host', values.host);
    // Rows of an inactive check are left out instead of showing a placeholder.
    if (values.enable_http_check) addSummaryRow(list, 'Full URL', values.http_url);
    if (values.enable_tcp_connect) addSummaryRow(list, 'TCP ports', values.tcp_ports);
    if (values.enable_udp_scan) addSummaryRow(list, 'UDP ports', values.udp_ports);
    const checks = [values.enable_http_check && 'HTTP', values.enable_ping && 'ICMP ping', values.enable_tcp_connect && 'TCP', values.enable_udp_scan && 'UDP', values.enable_cert_expiration && 'Certificate expiry'].filter(Boolean);
    addSummaryRow(list, 'Checks', checks.join(', ') || 'None');
    addSummaryRow(list, 'Interval / retries / retention', `${values.interval_seconds} s / ${values.max_retries} / ${values.retention_days} days`);
    if (values.enable_http_check) addSummaryRow(list, 'HTTP method / success codes', `${values.http_prefer_head ? 'HEAD' : 'GET'} / ${values.http_success_codes}`);
    addSummaryRow(list, 'Certificate monitoring', values.enable_cert_expiration ? 'On' : 'Off');
    if (values.enable_http_check || values.enable_cert_expiration) addSummaryRow(list, 'Ignore certificate errors', values.ignore_cert_errors ? 'Yes' : 'No');
    addSummaryRow(list, 'Auto-close alerts', values.auto_close_alert ? 'On' : 'Off');
    addSummaryRow(list, 'Status', values.is_enabled ? 'Active' : 'Inactive');
    const selected = [...document.querySelectorAll('#wizard-recipients input:checked')].map(input => input.dataset.name);
    addSummaryRow(list, 'Alert recipients', selected.join(', ') || 'None');
    summary.append(list);

    const conflictBox = document.getElementById('wizard-conflicts');
    conflictBox.replaceChildren();
    saveButton.disabled = conflicts.some(conflict => conflict.classification === 'exact');
    if (!conflicts.length) return;
    const warning = document.createElement('div');
    warning.className = saveButton.disabled ? 'alert alert-danger' : 'alert alert-warning';
    warning.textContent = saveButton.disabled ? 'An identical target already exists.' : 'Related targets already exist. Review them before starting another monitor.';
    const listElement = document.createElement('ul');
    listElement.className = 'mb-0';
    for (const conflict of conflicts) {
      const item = document.createElement('li');
      const link = document.createElement('a');
      link.href = `/ui/targets/${encodeURIComponent(conflict.id)}`;
      link.textContent = `${conflict.name} (${conflict.check_type}, ${conflict.classification})`;
      item.append(link);
      listElement.append(item);
    }
    warning.append(listElement);
    conflictBox.append(warning);
  }

  async function loadRecipients() {
    if (recipientsLoaded) return;
    const container = document.getElementById('wizard-recipients');
    try {
      const response = await window.apiFetch('/recipients');
      if (!response.ok) throw new Error('Unable to load alert recipients.');
      const recipients = await response.json();
      container.replaceChildren();
      if (!recipients.length) {
        container.textContent = 'No alert recipients configured.';
      } else {
        for (const recipient of recipients) {
          const wrap = document.createElement('div');
          wrap.className = 'col-12 col-sm-6';
          const label = document.createElement('label');
          label.className = 'form-check';
          const input = document.createElement('input');
          input.type = 'checkbox';
          input.className = 'form-check-input';
          input.value = String(recipient.id);
          input.dataset.name = recipient.name;
          const text = document.createElement('span');
          text.className = 'form-check-label';
          text.textContent = recipient.name;
          label.append(input, text);
          wrap.append(label);
          container.append(wrap);
        }
      }
      recipientsLoaded = true;
    } catch (error) {
      container.textContent = error.message;
      throw error;
    }
  }

  async function advance() {
    if (step === 0) {
      updateKindFields();
      setStep(1);
      return;
    }
    if (step === 1 && !validateAddress()) return;
    if (step === 2 && !validateChecks()) return;
    if (step === 3) {
      renderReview();
      setStep(4);
      return;
    }
    nextButton.disabled = true;
    const startStep = step;
    const startSignature = JSON.stringify(requestBody());
    try {
      if (step === 1 || step === 2) {
        const result = await preview();
        // Discard a stale response if the user navigated or edited meanwhile
        if (step !== startStep || JSON.stringify(requestBody()) !== startSignature) return;
        showProfileValues(result.values);
        conflicts = result.conflicts || [];
        if (step === 2) {
          // Load right before the recipients step so a failure surfaces on the preceding step.
          await loadRecipients();
          if (step !== startStep) return;
          reviewedValues = result.values;
        }
        setStep(step + 1);
      }
    } catch (error) {
      if (step === 1 && /port/i.test(error.message)) showAddressPortsError(error.message);
      else showError(error.message);
    } finally {
      nextButton.disabled = false;
    }
  }

  for (const [key, field] of Object.entries(overrideFields)) {
    field.addEventListener(field.type === 'checkbox' || field.tagName === 'SELECT' ? 'change' : 'input', () => dirty.add(key));
  }
  for (const toggle of [fields.http, fields.ping, fields.tcp, fields.udp, fields.cert]) {
    toggle.addEventListener('change', () => updateCheckGroups({ announce: true }));
  }
  // The picker writes the hidden backing fields and reports "change" for them.
  for (const input of [fields.ports, fields.udpPorts]) {
    input.addEventListener('input', updatePortSummaries);
    input.addEventListener('change', updatePortSummaries);
  }
  for (const button of form.querySelectorAll('[data-edit-ports]')) {
    button.addEventListener('click', () => editPorts(button.dataset.editPorts));
  }
  fields.name.addEventListener('input', () => { fields.name.dataset.userEdited = 'true'; });
  fields.address.addEventListener('input', suggestName);
  form.querySelectorAll('input[name="profile"]').forEach(input => input.addEventListener('change', updateKindFields));
  nextButton.addEventListener('click', advance);
  backButton.addEventListener('click', () => setStep(step - 1));
  form.addEventListener('submit', async event => {
    event.preventDefault();
    if (step !== steps.length - 1) {
      if (!nextButton.disabled) await advance();
      return;
    }
    if (saving || !reviewedValues || saveButton.disabled) return;
    saving = true;
    saveButton.disabled = true;
    saveButton.textContent = 'Saving…';
    try {
      const recipientIds = [...document.querySelectorAll('#wizard-recipients input:checked')].map(input => Number(input.value));
      const response = await window.apiFetch('/targets', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ ...reviewedValues, alert_recipient_ids: recipientIds,
          ...(form.elements.network_scan_id.value ? { network_scan_id: form.elements.network_scan_id.value } : {}) })
      });
      if (!response.ok) {
        let detail;
        try { detail = (await response.json()).detail; } catch (_) { /* use status below */ }
        if (response.status === 409 && Array.isArray(detail?.conflicts)) {
          conflicts = detail.conflicts;
          renderReview();
        }
        throw new Error(errorDetailMessage(detail, `Unable to create target (${response.status}).`));
      }
      const target = await response.json();
      window.location.assign(`/ui/targets/${encodeURIComponent(target.id)}`);
    } catch (error) {
      showError(error.message);
      saveButton.disabled = conflicts.some(conflict => conflict.classification === 'exact');
    } finally {
      saving = false;
      saveButton.textContent = 'Start monitoring';
    }
  });
  updateKindFields();
  updateCheckGroups();
  setStep(0, { scroll: false });
})();
