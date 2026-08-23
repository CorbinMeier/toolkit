// Mobile control UI for bin/dashd-control (#30). Plain vanilla JS, no
// framework -- this is a small form, not worth a build step.
const STORE_KEY = 'dashd-control-token';
const params = new URLSearchParams(location.search);
if (params.get('t')) localStorage.setItem(STORE_KEY, params.get('t'));
const token = localStorage.getItem(STORE_KEY) || '';

const app = document.getElementById('app');
const statusEl = document.getElementById('status');

function authedFetch(path, opts = {}) {
  const headers = { ...(opts.headers || {}), Authorization: `Bearer ${token}` };
  return fetch(path, { ...opts, headers });
}

// section/key must match bin/dashd-control's EDITABLE whitelist -- any
// field added here that isn't also whitelisted server-side gets silently
// reported back in the save response's "rejected" list, not applied.
const FIELDS = [
  { section: 'units', key: 'temperature', label: 'Temperature unit', type: 'select', options: ['fahrenheit', 'celsius'] },
  { section: 'units', key: 'wind', label: 'Wind unit', type: 'select', options: ['mph', 'kmh', 'ms', 'kn'] },
  { section: 'units', key: 'clock24', label: '24-hour clock', type: 'checkbox' },
  { section: 'display', key: 'transparent', label: 'Transparent background', type: 'checkbox' },
  { section: 'display', key: 'safe_area_top', label: 'Safe area top (px)', type: 'number' },
  { section: 'display', key: 'safe_area_bottom', label: 'Safe area bottom (px)', type: 'number' },
  { section: 'logs', key: 'source_type', label: 'Log source type', type: 'select', options: ['journalctl', 'file'] },
  { section: 'logs', key: 'journalctl_unit', label: 'journalctl unit', type: 'text' },
  { section: 'logs', key: 'file_path', label: 'Log file path', type: 'text' },
];

function fieldEl(f, value) {
  const wrap = document.createElement('div');
  const label = document.createElement('label');
  label.textContent = f.label;
  wrap.appendChild(label);

  let input;
  if (f.type === 'select') {
    input = document.createElement('select');
    f.options.forEach((o) => {
      const opt = document.createElement('option');
      opt.value = o;
      opt.textContent = o;
      opt.selected = o === value;
      input.appendChild(opt);
    });
  } else {
    input = document.createElement('input');
    input.type = f.type;
    if (f.type === 'checkbox') input.checked = !!value;
    else input.value = value ?? '';
  }
  input.dataset.section = f.section;
  input.dataset.key = f.key;
  input.dataset.type = f.type;
  wrap.appendChild(input);
  return wrap;
}

function groupBySection(fields) {
  const groups = {};
  fields.forEach((f) => {
    (groups[f.section] ||= []).push(f);
  });
  return groups;
}

async function load() {
  if (!token) {
    app.textContent = 'No pairing token -- scan the QR code shown at the desktop again.';
    return;
  }
  let res;
  try {
    res = await authedFetch('/api/control/config');
  } catch {
    app.textContent = 'Could not reach the control server.';
    return;
  }
  if (!res.ok) {
    app.textContent = `Unauthorized (${res.status}) -- rescan the QR code.`;
    return;
  }
  const cfg = await res.json();

  app.replaceChildren();
  const inputs = [];
  Object.entries(groupBySection(FIELDS)).forEach(([section, fields]) => {
    const fs = document.createElement('fieldset');
    const legend = document.createElement('legend');
    legend.textContent = section;
    fs.appendChild(legend);
    fields.forEach((f) => {
      const wrap = fieldEl(f, (cfg[section] || {})[f.key]);
      fs.appendChild(wrap);
      inputs.push(wrap.querySelector('input, select'));
    });
    app.appendChild(fs);
  });

  const btn = document.createElement('button');
  btn.textContent = 'Save';
  btn.addEventListener('click', () => save(inputs));
  app.appendChild(btn);
}

async function save(inputs) {
  const patch = {};
  inputs.forEach((input) => {
    const { section, key, type } = input.dataset;
    patch[section] ||= {};
    if (type === 'checkbox') patch[section][key] = input.checked;
    else if (type === 'number') patch[section][key] = Number(input.value);
    else patch[section][key] = input.value;
  });

  statusEl.textContent = 'Saving…';
  try {
    const res = await authedFetch('/api/control/config', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(patch),
    });
    const body = await res.json();
    if (!res.ok) {
      statusEl.textContent = `Error: ${body.error || res.status}`;
    } else if (body.rejected && body.rejected.length) {
      statusEl.textContent = `Saved (rejected: ${body.rejected.join(', ')})`;
    } else {
      statusEl.textContent = 'Saved.';
    }
  } catch (e) {
    statusEl.textContent = `Error: ${e}`;
  }
}

load();
