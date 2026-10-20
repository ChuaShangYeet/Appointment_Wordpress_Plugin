/* The app shell: state, rendering, event wiring, the prototype strip and helpers shared by the four areas.
   Each area (booking.js, account.js, admin.js, signup.js) registers JS.views[name] = { init, render } and its actions in JS.actions. */
window.JS = window.JS || {};
(function () {
  const J = window.JS;
  const T = J.T;
  const STORE = 'justsimple-local-v1';

  J.state = { view: 'book', biz: 'spa', scenario: 'normal', role: 'owner' };
  try {
    const saved = JSON.parse(localStorage.getItem(STORE) || '{}');
    if (['book', 'account', 'admin', 'signup'].includes(saved.view)) J.state.view = saved.view;
    if (saved.biz && J.api.businesses().some((b) => b.id === saved.biz)) J.state.biz = saved.biz;
    if (saved.scenario) J.state.scenario = saved.scenario;
    if (saved.role) J.state.role = saved.role;
  } catch (e) { /* storage blocked: start from the defaults */ }
  function save() {
    try { localStorage.setItem(STORE, JSON.stringify({ view: J.state.view, biz: J.state.biz, scenario: J.state.scenario, role: J.state.role })); } catch (e) { /* not kept */ }
  }

  J.views = {};
  J.actions = {};

  /* ------------------------------------------------------------------ helpers */
  J.esc = (s) => String(s ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  J.money = (n) => (Number(n) === 0 ? 'Free' : 'RM ' + Number(n).toLocaleString('en-MY', { maximumFractionDigits: 2 }));
  J.biz = () => J.api.business(J.state.biz);
  J.mins = (n) => (n >= 60 ? `${Math.floor(n / 60)} h${n % 60 ? ' ' + (n % 60) + ' min' : ''}` : `${n} min`);
  J.initials = (name) => name.split(/\s+/).filter((w) => /^[A-Za-z]/.test(w)).slice(0, 2).map((w) => w[0]).join('').toUpperCase() || 'JS';
  J.first = (name) => (String(name || '').split(/\s+/)[0] || '');
  J.STATUS = { pending: 'Waiting for approval', confirmed: 'Confirmed', 'in-progress': 'In progress', 'waiting-for-parts': 'Waiting for parts', completed: 'Completed', cancelled: 'Cancelled', rejected: 'Declined', 'no-show': 'No-show' };
  J.chip = (status) => `<span class="chip ${J.esc(status)}">${J.esc(J.STATUS[status] || status)}</span>`;
  J.WHO = { therapist: 'therapist', stylist: 'stylist', table: 'table', mechanic: 'mechanic', technician: 'technician' };
  /** What each review flag tells the business (backend: FLAG_REASONS in app/services/flagging.py). */
  J.FLAGS = {
    outside_shift: "The staff member's shift no longer covers this booking",
    day_off: "A holiday or the staff member's leave now covers this day",
    outside_operating_hours: 'The booking is no longer inside your opening hours',
    outside_slot_times: 'The start time is outside the reservation times you now accept',
    staff_not_qualified: 'The assigned person or bay is no longer set up for this service',
    table_cap_exceeded: 'More tables are reserved at once than your open-table rule allows',
    outside_service_area: 'The address is now outside your service area',
    resource_inactive: 'An assigned staff member, table or bay has been switched off',
    table_too_small: 'The table no longer seats the whole party',
    table_class_changed: 'The table moved between normal and private room',
  };

  J.get = function (path) { return path.split('.').reduce((o, k) => (o == null ? undefined : o[k]), J.state); };
  J.set = function (path, value) {
    const keys = path.split('.');
    let o = J.state;
    for (const k of keys.slice(0, -1)) o = o[k];
    o[keys[keys.length - 1]] = value;
  };

  /** A labelled form field bound to a state path (data-model); `live` re-renders on every keystroke. */
  J.field = function ({ id, label, model, type = 'text', value, hint, err, optional, placeholder, attrs = '', live, area, options }) {
    const v = value !== undefined ? value : J.get(model);
    let control;
    if (options) control = `<select id="${id}" data-model="${model}" ${live ? 'data-rerender' : ''} ${attrs}>${options.map(([val, text]) => `<option value="${J.esc(val)}" ${String(val) === String(v ?? '') ? 'selected' : ''}>${J.esc(text)}</option>`).join('')}</select>`;
    else if (area) control = `<textarea id="${id}" data-model="${model}" placeholder="${J.esc(placeholder || '')}" ${attrs}>${J.esc(v)}</textarea>`;
    else control = `<input id="${id}" type="${type}" data-model="${model}" value="${J.esc(v)}" placeholder="${J.esc(placeholder || '')}" ${live ? 'data-live' : ''} ${attrs}>`;
    return `<label class="field ${err ? 'invalid' : ''}" for="${id}"><span>${J.esc(label)}${optional ? ' <span class="opt">(optional)</span>' : ''}</span>${control}${hint ? `<span class="hint">${hint}</span>` : ''}${err ? `<span class="err">${J.esc(err)}</span>` : ''}</label>`;
  };

  /** The business's header as its customers see it. */
  J.storefront = function (b) {
    const [avg, n] = b.rating || [0, 0];
    return `<div class="storefront"><div class="monogram" aria-hidden="true">${J.esc(J.initials(b.name))}</div><div class="stack" style="gap:4px"><h1>${J.esc(b.name)}</h1>
      <div class="meta"><span>${J.esc(b.kind)}</span>${n ? `<span><span class="stars" aria-hidden="true">★</span> ${avg} (${n} reviews)</span>` : ''}<span>${J.esc(b.address)}</span>${b.place && b.place.maps_url ? `<a href="${J.esc(b.place.maps_url)}" target="_blank" rel="noopener noreferrer">Directions</a>` : ''}</div></div></div>`;
  };

  J.busyLabel = (text) => `<span class="spinner" aria-hidden="true"></span>${J.esc(text)}`;

  /* ------------------------------------------------------------------ toast */
  let toastTimer;
  J.toast = function (text) {
    const el = document.getElementById('toast');
    el.textContent = text;
    el.hidden = false;
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => { el.hidden = true; }, 3200);
  };

  /** Run an async API call; returns [result, error]. Unexpected (non-API) errors are shown and logged. */
  J.call = async function (fn) {
    try { return [await fn(), null]; } catch (e) {
      if (e instanceof J.ApiError) return [null, e];
      console.error(e);
      J.toast('Something went wrong: ' + e.message);
      return [null, e];
    }
  };

  /* ------------------------------------------------------------------ the prototype strip */
  const SCENARIOS = [
    ['normal', 'Everything works'],
    ['slot_taken', 'Someone takes the slot first'],
  ];
  const AREAS = [['book', 'Book'], ['account', 'My bookings'], ['admin', 'Business admin'], ['signup', 'Sign up a business']];
  function harness() {
    const list = J.api.businesses();
    const b = J.biz();
    const staff = b.resources.filter((r) => r.type === 'staff');
    const roles = [['owner', 'Owner'], ['manager', 'Manager']].concat(staff.length ? [['staff', `Staff: ${staff[0].name}`]] : []);
    return `<div class="harness-inner">
      <div class="brand"><b>JustSimple</b><span class="tag">${J.api.hosted() ? 'Online demo · shared by everyone' : 'Local test site · real backend'}</span></div>
      <button type="button" class="hbtn hmenu" data-act="menu" aria-expanded="${!!J.state.menu}" aria-controls="hpanel">${J.state.menu ? 'Close' : AREAS.find(([v]) => v === J.state.view)[1] + ' ▾'}</button>
      <div class="hpanel ${J.state.menu ? 'open' : ''}" id="hpanel">
      <nav aria-label="Prototype areas">${AREAS.map(([v, t]) => `<button type="button" data-act="nav" data-view="${v}" ${J.state.view === v ? 'aria-current="page"' : ''}>${t}</button>`).join('')}</nav>
      <div class="controls">
        ${J.state.view === 'signup' ? '' : `<label for="h-biz">Business <select id="h-biz" data-change="setBiz">${list.map((x) => `<option value="${x.id}" ${x.id === J.state.biz ? 'selected' : ''}>${J.esc(x.name)} (${J.esc(x.kind)})</option>`).join('')}</select></label>`}
        ${J.state.view === 'admin' ? `<label for="h-role">Signed in as <select id="h-role" data-change="setRole">${roles.map(([v, t]) => `<option value="${v}" ${J.state.role === v ? 'selected' : ''}>${J.esc(t)}</option>`).join('')}</select></label>` : ''}
        <label for="h-scn">Scenario <select id="h-scn" data-change="setScenario">${SCENARIOS.map(([v, t]) => `<option value="${v}" ${J.state.scenario === v ? 'selected' : ''}>${t}</option>`).join('')}</select></label>
        <button type="button" class="hbtn" data-act="inbox" aria-expanded="${!!J.state.inbox}">Messages<span class="count">${J.inbox.length}</span></button>
        <button type="button" class="hbtn" data-act="resetDemo">Reset data</button>
      </div></div></div>`;
  }

  function freshViews() {
    for (const name of Object.keys(J.views)) J.state[name] = J.views[name].init();
  }

  Object.assign(J.actions, {
    nav(ds) { J.state.view = ds.view; J.state.menu = false; save(); J.render(); window.scrollTo(0, 0); },
    menu() { J.state.menu = !J.state.menu; J.render(); },
    setBiz(ds, el) { J.state.biz = el.value; save(); freshViews(); J.render(); },
    setRole(ds, el) { J.state.role = el.value; save(); J.state.admin.open = null; J.render(); },
    setScenario(ds, el) { J.state.scenario = el.value; J.api.armScenario(); save(); J.toast('Scenario: ' + SCENARIOS.find(([v]) => v === el.value)[1]); },
    inbox() { J.state.inbox = !J.state.inbox; J.render(); },
    resetDemo() { J.api.reset(); if (!J.api.businesses().some((b) => b.id === J.state.biz)) J.state.biz = 'spa'; freshViews(); J.render(); J.toast('Sample data reset'); },
  });

  /* ------------------------------------------------------------------ rendering */
  J.render = function () {
    const active = document.activeElement;
    const focusId = active && active.id;
    const selection = focusId && typeof active.selectionStart === 'number' ? [active.selectionStart, active.selectionEnd] : null;
    const scrolls = [...document.querySelectorAll('[data-keep-scroll]')].map((el) => [el.dataset.keepScroll, el.scrollLeft]);
    for (const name of Object.keys(J.views)) if (!J.state[name]) J.state[name] = J.views[name].init();
    document.getElementById('harness').innerHTML = harness();
    document.getElementById('app').innerHTML = J.views[J.state.view].render() + inboxPanel();
    for (const [key, left] of scrolls) { const el = document.querySelector(`[data-keep-scroll="${key}"]`); if (el) el.scrollLeft = left; }
    if (focusId) {
      const el = document.getElementById(focusId);
      if (el && !el.disabled) { el.focus({ preventScroll: true }); if (selection && el.setSelectionRange) try { el.setSelectionRange(...selection); } catch (e) { /* not a text field */ } }
    }
    const view = J.views[J.state.view];
    if (view.after) view.after();
  };
  J.top = () => window.scrollTo({ top: 0 });

  /** What the backend "sent": nothing leaves this computer, so the codes and links people would get by WhatsApp, SMS or email show up here. */
  function inboxPanel() {
    if (!J.state.inbox) return '';
    const list = J.inbox.slice().reverse().slice(0, 40);
    return `<aside class="inbox" aria-label="Messages sent"><div class="spread"><b>Messages sent</b><button type="button" class="closex" data-act="inbox" aria-label="Close">✕</button></div>
      <p class="hint">Nothing is really sent: WhatsApp, SMS and email messages appear here instead. Sign-in codes are in the WhatsApp messages; the buttons on the page open the emailed links for you.</p>
      ${list.length ? list.map((m) => `<div class="msg"><span class="label">${J.esc(m.channel)} to ${J.esc(m.to)} · ${T.time(m.sent_at)}</span>${m.subject ? `<b>${J.esc(m.subject)}</b><br>` : ''}${J.esc(m.body)}</div>`).join('') : '<p class="muted">Nothing yet.</p>'}</aside>`;
  }
  J.onMessages = () => { if (!typing()) J.render(); };

  function typing() {
    const a = document.activeElement;
    return a && /INPUT|TEXTAREA|SELECT/.test(a.tagName);
  }

  J.start = function () {
    document.addEventListener('click', (e) => {
      const el = e.target.closest('[data-act]');
      if (!el || el.disabled || el.getAttribute('aria-disabled') === 'true') return;
      const fn = J.actions[el.dataset.act];
      if (!fn) { console.warn('no action', el.dataset.act); return; }
      e.preventDefault();
      fn(el.dataset, el, e);
    });
    document.addEventListener('input', (e) => {
      const el = e.target;
      if (!el.dataset || !el.dataset.model || el.tagName === 'SELECT' || el.type === 'checkbox' || el.type === 'radio') return;
      J.set(el.dataset.model, el.value);
      if (el.dataset.live !== undefined) J.render();
    });
    document.addEventListener('change', (e) => {
      const el = e.target;
      if (el.dataset && el.dataset.model) {
        J.set(el.dataset.model, el.type === 'checkbox' ? el.checked : el.value);
        if (el.dataset.rerender !== undefined || el.type === 'checkbox' || el.tagName === 'SELECT') J.render();
      }
      if (el.dataset && el.dataset.change) J.actions[el.dataset.change](el.dataset, el, e);
    });
    document.addEventListener('submit', (e) => {
      e.preventDefault();
      const f = e.target;
      if (f.dataset.submit) J.actions[f.dataset.submit](f.dataset, f, e);
    });
    // hold countdowns and "now" lines move with the clock
    setInterval(() => { if (J.state.view === 'admin' && !typing()) J.render(); }, 30000);
    J.render();
  };
})();
