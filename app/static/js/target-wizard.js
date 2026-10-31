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
    extraPorts: document.getElementById('wizard-extra-ports'),
    name: document.getElementById('wizard-name'),
    group: document.getElementById('wizard-group'),
    http: document.getElementById('wizard-http'),
    ping: document.getElementById('wizard-ping'),
    tcp: document.getElementById('wizard-tcp'),
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
  const dirty = new Set();
  let step = 0;
  let reviewedValues = null;
  let conflicts = [];
  let suggestedHttps = false;
  let saving = false;
  let recipientsLoaded = false;

  function profile() {
    return form.querySelector('input[name="profile"]:checked')?.value || 'website';
  }

  function clearError() {
    errorBox.classList.add('d-none');
    errorBox.textContent = '';
    for (const field of form.querySelectorAll('.is-invalid')) {
      field.classList.remove('is-invalid');
      const message = document.getElementById(`${field.id}-error`);
      if (message) message.textContent = '';
    }
  }

  function showError(message, field) {
    errorBox.textContent = message;
    errorBox.classList.remove('d-none');
    if (field) {
      field.classList.add('is-invalid');
      let detail = document.getElementById(`${field.id}-error`);
      if (!detail) {
        detail = document.createElement('div');
        detail.id = `${field.id}-error`;
        detail.className = 'invalid-feedback';
        detail.setAttribute('role', 'alert');
        field.after(detail);
        field.setAttribute('aria-describedby', detail.id);
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
    const heading = steps[step].querySelector('h2');
    heading?.focus({ preventScroll: true });
    if (scroll) heading?.scrollIntoView({ block: 'nearest' });
  }

  function updateKindFields() {
    const kind = profile();
    for (const element of form.querySelectorAll('.wizard-kind')) {
      element.hidden = !element.dataset.kinds.split(' ').includes(kind);
    }
    document.getElementById('wizard-address-label').textContent =
      ['website', 'api'].includes(kind) ? 'Full URL or domain *' : 'Hostname or IP address *';
    fields.address.autocomplete = ['website', 'api'].includes(kind) ? 'url' : 'off';
  }

  function updateTcpPortField() {
    document.getElementById('wizard-extra-ports-wrap').hidden = !fields.tcp.checked;
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
    for (const [key, field] of Object.entries(overrideFields)) {
      if (dirty.has(key)) overrides[key] = readOverride(key, field);
    }
    return {
      profile: profile(),
      address: fields.address.value.trim(),
      name: fields.name.value.trim() || null,
      group: fields.group.value || null,
      tcp_ports: fields.ports.value.trim() || null,
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
      const message = typeof detail === 'string' ? detail
        : detail?.message || (Array.isArray(detail) ? detail.map(item => item.msg).join('; ') : null)
          || `Unable to preview target (${response.status}).`;
      throw new Error(message);
    }
    return response.json();
  }

  function validateAddress() {
    if (!fields.address.value.trim()) {
      showError('Enter a URL, hostname, or IP address.', fields.address);
      return false;
    }
    if (profile() === 'tcp' && !fields.ports.value.trim()) {
      showError('Choose at least one TCP port.', fields.ports);
      return false;
    }
    return true;
  }

  function validateChecks() {
    for (const field of [fields.interval, fields.retries, fields.retention]) {
      if (!field.checkValidity()) {
        showError(`Enter a valid value for ${field.previousElementSibling?.textContent || 'this setting'}.`, field);
        return false;
      }
    }
    if (!fields.http.checked && !fields.ping.checked && !fields.tcp.checked) {
      showError('Enable at least one monitoring check.');
      return false;
    }
    if (fields.tcp.checked && !fields.ports.value.trim()) {
      showError('Choose at least one TCP port.', fields.extraPorts);
      return false;
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
    if (suggestedHttps) {
      const notice = document.createElement('div');
      notice.className = 'alert alert-info';
      notice.textContent = 'HTTPS was suggested because the address had no scheme. Use Change checks or recipients to edit the full URL before saving.';
      summary.append(notice);
    }
    const list = document.createElement('dl');
    list.className = 'mb-0';
    addSummaryRow(list, 'Name / group', `${values.name} / ${values.group || 'Ungrouped'}`);
    addSummaryRow(list, 'Host', values.host);
    addSummaryRow(list, 'Full URL', values.enable_http_check ? values.http_url : null);
    addSummaryRow(list, 'TCP ports', values.enable_tcp_connect ? values.tcp_ports : null);
    const checks = [values.enable_http_check && 'HTTP', values.enable_ping && 'ICMP ping', values.enable_tcp_connect && 'TCP', values.enable_cert_expiration && 'Certificate expiry'].filter(Boolean);
    addSummaryRow(list, 'Checks', checks.join(', ') || 'None');
    addSummaryRow(list, 'Interval / retries / retention', `${values.interval_seconds} s / ${values.max_retries} / ${values.retention_days} days`);
    addSummaryRow(list, 'HTTP method / success codes', values.enable_http_check ? `${values.http_prefer_head ? 'HEAD' : 'GET'} / ${values.http_success_codes}` : null);
    addSummaryRow(list, 'Certificate monitoring', values.enable_cert_expiration ? 'On' : 'Off');
    addSummaryRow(list, 'Ignore certificate errors', values.ignore_cert_errors ? 'Yes' : 'No');
    addSummaryRow(list, 'Auto-close alerts', values.auto_close_alert ? 'On' : 'Off');
    addSummaryRow(list, 'Status', values.is_enabled ? 'Active' : 'Inactive');
    const selected = [...document.querySelectorAll('#wizard-recipients input:checked')].map(input => input.dataset.name);
    addSummaryRow(list, 'Alert recipients', selected.join(', ') || 'None');
    summary.append(list);
    const actions = document.createElement('div');
    actions.className = 'd-flex flex-wrap gap-2 mt-3';
    for (const [label, index] of [['Change type', 0], ['Change address', 1], ['Change checks or recipients', 2]]) {
      const button = document.createElement('button');
      button.type = 'button';
      button.className = 'btn btn-outline-secondary btn-sm';
      button.textContent = label;
      button.addEventListener('click', () => setStep(index));
      actions.append(button);
    }
    summary.append(actions);

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
    nextButton.disabled = true;
    try {
      if (step === 1 || step === 2) {
        const result = await preview();
        showProfileValues(result.values);
        conflicts = result.conflicts || [];
        suggestedHttps = Boolean(result.suggested_https);
        if (step === 1) await loadRecipients();
        else {
          reviewedValues = result.values;
          renderReview();
        }
        setStep(step + 1);
      }
    } catch (error) {
      const field = step === 1 && /port/i.test(error.message) ? fields.ports : null;
      showError(error.message, field);
    } finally {
      nextButton.disabled = false;
    }
  }

  for (const [key, field] of Object.entries(overrideFields)) {
    field.addEventListener(field.type === 'checkbox' || field.tagName === 'SELECT' ? 'change' : 'input', () => dirty.add(key));
  }
  fields.tcp.addEventListener('change', updateTcpPortField);
  fields.ports.addEventListener('input', () => { fields.extraPorts.value = fields.ports.value; });
  fields.extraPorts.addEventListener('input', () => { fields.ports.value = fields.extraPorts.value; });
  fields.name.addEventListener('input', () => { fields.name.dataset.userEdited = 'true'; });
  fields.address.addEventListener('input', suggestName);
  form.querySelectorAll('input[name="profile"]').forEach(input => input.addEventListener('change', updateKindFields));
  nextButton.addEventListener('click', advance);
  backButton.addEventListener('click', () => setStep(step - 1));
  form.addEventListener('submit', async event => {
    event.preventDefault();
    if (step !== 3) {
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
        body: JSON.stringify({ ...reviewedValues, alert_recipient_ids: recipientIds })
      });
      if (!response.ok) {
        let detail;
        try { detail = (await response.json()).detail; } catch (_) { /* use status below */ }
        if (response.status === 409 && Array.isArray(detail?.conflicts)) {
          conflicts = detail.conflicts;
          renderReview();
        }
        throw new Error(detail?.message || (typeof detail === 'string' ? detail : `Unable to create target (${response.status}).`));
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
  updateTcpPortField();
  setStep(0, { scroll: false });
})();
