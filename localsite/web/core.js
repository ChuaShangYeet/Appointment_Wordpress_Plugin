/* The app shell: state, rendering, event wiring, the prototype strip and helpers shared by the four areas.
   Each area (booking.js, account.js, admin.js, signup.js) registers JS.views[name] = { init, render } and its actions in JS.actions. */
window.JS = window.JS || {};
(function () {
  const J = window.JS;
  const T = J.T;
  const STORE = 'justsimple-local-v1';

  J.state = { view: 'book', biz: 'spa', role: 'owner' };
  try {
    const saved = JSON.parse(localStorage.getItem(STORE) || '{}');
    if (['book', 'account', 'admin', 'signup'].includes(saved.view)) J.state.view = saved.view;
    if (saved.biz && J.api.businesses().some((b) => b.id === saved.biz)) J.state.biz = saved.biz;
    if (saved.role) J.state.role = saved.role;
  } catch (e) { /* storage blocked: start from the defaults */ }
  function save() {
    try { localStorage.setItem(STORE, JSON.stringify({ view: J.state.view, biz: J.state.biz, role: J.state.role })); } catch (e) { /* not kept */ }
  }

  J.views = {};
  J.actions = {};

  /* ------------------------------------------------------------------ helpers */
  J.esc = (s) => String(s ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  J.biz = () => J.api.business(J.state.biz);
  /** A price in the shown business's currency, as people there write it: RM 150 in Malaysia, $150 in Singapore or Australia, £150, ¥15,000. */
  J.money = (n) => {
    const v = Number(n);
    if (v === 0) return 'Free';
    let b = {};
    try { b = J.biz(); } catch (e) { /* no business shown: ringgit */ }
    const cur = b.currency || 'MYR';
    if (cur === 'MYR') return 'RM ' + v.toLocaleString('en-MY', { maximumFractionDigits: 2 });
    const whole = Number.isInteger(v) ? { minimumFractionDigits: 0 } : {};
    try { return new Intl.NumberFormat('en-' + (b.country || 'US'), { style: 'currency', currency: cur, ...whole }).format(v); } catch (e) { return cur + ' ' + v.toLocaleString('en'); }
  };
  /** A service's price as the customer reads it: 0 is free, unless the price is told after an inspection (then a price above 0 is where it starts). */
  J.price = (x) => (x.price_after_inspection ? (Number(x.price) > 0 ? 'From ' + J.money(x.price) : 'Price after inspection') : J.money(x.price));
  /** The total of several services, read the same way. */
  J.priceTotal = (list) => {
    const sum = list.reduce((t, x) => t + Number(x.price), 0);
    if (!list.some((x) => x.price_after_inspection)) return J.money(sum);
    return sum > 0 ? 'From ' + J.money(sum) : 'Price after inspection';
  };
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

  /** One outline icon set (paths from Lucide, ISC licence), drawn at the text size in the text colour. */
  const ICONS = {
    'arrow-left': '<path d="m12 19-7-7 7-7"/><path d="M19 12H5"/>',
    'chevron-left': '<path d="m15 18-6-6 6-6"/>',
    'chevron-right': '<path d="m9 18 6-6-6-6"/>',
    'chevron-down': '<path d="m6 9 6 6 6-6"/>',
    x: '<path d="M18 6 6 18"/><path d="m6 6 12 12"/>',
    check: '<path d="M20 6 9 17l-5-5"/>',
    star: '<path d="M11.525 2.295a.53.53 0 0 1 .95 0l2.31 4.679a2.123 2.123 0 0 0 1.595 1.16l5.166.756a.53.53 0 0 1 .294.904l-3.736 3.638a2.123 2.123 0 0 0-.611 1.878l.882 5.14a.53.53 0 0 1-.771.56l-4.618-2.428a2.122 2.122 0 0 0-1.973 0L6.396 21.01a.53.53 0 0 1-.77-.56l.881-5.139a2.122 2.122 0 0 0-.611-1.879L2.16 9.795a.53.53 0 0 1 .294-.906l5.165-.755a2.122 2.122 0 0 0 1.597-1.16z"/>',
    pin: '<path d="M20 10c0 4.993-5.539 10.193-7.399 11.799a1 1 0 0 1-1.202 0C9.539 20.193 4 14.993 4 10a8 8 0 0 1 16 0"/><circle cx="12" cy="10" r="3"/>',
    clock: '<circle cx="12" cy="12" r="10"/><path d="M12 6v6l4 2"/>',
    home: '<path d="M15 21v-8a1 1 0 0 0-1-1h-4a1 1 0 0 0-1 1v8"/><path d="M3 10a2 2 0 0 1 .709-1.528l7-5.999a2 2 0 0 1 2.582 0l7 5.999A2 2 0 0 1 21 10v9a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z"/>',
    mail: '<rect width="20" height="16" x="2" y="4" rx="2"/><path d="m22 7-8.97 5.7a1.94 1.94 0 0 1-2.06 0L2 7"/>',
    'log-out': '<path d="M9 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h4"/><path d="m16 17 5-5-5-5"/><path d="M21 12H9"/>',
    reset: '<path d="M3 12a9 9 0 1 0 9-9 9.75 9.75 0 0 0-6.74 2.74L3 8"/><path d="M3 3v5h5"/>',
    menu: '<path d="M4 12h16"/><path d="M4 6h16"/><path d="M4 18h16"/>',
    minus: '<path d="M5 12h14"/>',
    plus: '<path d="M5 12h14"/><path d="M12 5v14"/>',
  };
  J.icon = (name) => `<svg class="icon" viewBox="0 0 24 24" aria-hidden="true" focusable="false">${ICONS[name]}</svg>`;
  /** The JustSimple mark: a booked day. */
  J.MARK = '<svg class="mark" viewBox="0 0 28 28" aria-hidden="true" focusable="false"><rect width="28" height="28" rx="7" fill="#157150"/><path d="M10 6.5v3M18 6.5v3" stroke="#fff" stroke-width="2" stroke-linecap="round"/><rect x="6.5" y="8" width="15" height="13.5" rx="3" fill="none" stroke="#fff" stroke-width="1.8"/><path d="m10.6 14.9 2.3 2.3 4.5-4.5" fill="none" stroke="#fff" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/></svg>';

  /** Entrances: the page re-renders on every change, so an element animates in only on the render right after J.arrive(key, cls) was called. */
  let arriving = {};
  J.arrive = (key, cls) => { arriving[key] = cls; };
  J.arrival = (key) => (arriving[key] ? ' ' + arriving[key] : '');

  /** The business's header as its customers see it. */
  const COVERS = { spa: 'spa', salon: 'salon', restaurant: 'restaurant', automotive: 'workshop', professional: 'home' };  // (img/: free Unsplash photos)
  J.storefront = function (b) {
    const [avg, n] = b.rating || [0, 0];
    const cover = COVERS[b.type] ? `<img class="cover" src="img/${COVERS[b.type]}.webp" alt="" width="1200" height="600" decoding="async">` : '';
    return `<div class="storefront-wrap">${cover}<div class="storefront"><div class="monogram" aria-hidden="true">${J.esc(J.initials(b.name))}</div><div class="stack" style="gap:2px;min-width:0"><h1>${J.esc(b.name)}</h1>
      <div class="meta"><span>${J.esc(b.kind)}</span>${n ? `<span><span class="stars">${J.icon('star')}</span>${avg} <span class="muted">(${n} reviews)</span></span>` : ''}<span>${J.icon('pin')}${J.esc(b.address)}</span>${b.place && b.place.maps_url ? `<a href="${J.esc(b.place.maps_url)}" target="_blank" rel="noopener noreferrer">Directions</a>` : ''}</div></div></div></div>`;
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
  const AREAS = [['book', 'Book'], ['account', 'My bookings'], ['admin', 'Business admin'], ['signup', 'Sign up a business']];
  function harness() {
    const list = J.api.businesses();
    const b = J.biz();
    const staff = b.resources.filter((r) => r.type === 'staff');
    const roles = [['owner', 'Owner'], ['manager', 'Manager']].concat(staff.map((r) => [`staff:${r.id}`, `Staff: ${r.name}`]));
    return `<div class="harness-inner">
      <div class="brand">${J.MARK}<b>JustSimple</b><span class="tag">${J.api.hosted() ? 'Online demo · shared, resets nightly' : 'Local test site'}</span></div>
      <button type="button" class="hbtn hmenu" data-act="menu" aria-expanded="${!!J.state.menu}" aria-controls="hpanel">${J.state.menu ? J.icon('x') + 'Close' : J.icon('menu') + AREAS.find(([v]) => v === J.state.view)[1]}</button>
      <div class="hpanel ${J.state.menu ? 'open' : ''}" id="hpanel">
      <nav aria-label="Demo areas">${AREAS.map(([v, t]) => `<button type="button" data-act="nav" data-view="${v}" ${J.state.view === v ? 'aria-current="page"' : ''}>${t}</button>`).join('')}</nav>
      <div class="controls">
        <button type="button" class="hbtn" data-act="inbox" aria-expanded="${!!J.state.inbox}">${J.icon('mail')}Messages<span class="count">${J.inbox.length}</span></button>
        ${J.api.hosted() ? `<button type="button" class="hbtn" data-act="signOutDemo">${J.icon('log-out')}Sign out</button>` : `<button type="button" class="hbtn" data-act="resetDemo">${J.icon('reset')}Reset data</button>`}
      </div>
      ${J.state.view === 'signup' ? '' : `<div class="hctx">
        <label for="h-biz">Business <select id="h-biz" data-change="setBiz">${list.map((x) => `<option value="${x.id}" ${x.id === J.state.biz ? 'selected' : ''}>${J.esc(x.name)} (${J.esc(x.kind)})</option>`).join('')}</select></label>
        ${J.state.view === 'admin' ? `<label for="h-role">Signed in as <select id="h-role" data-change="setRole">${roles.map(([v, t]) => `<option value="${v}" ${J.state.role === v ? 'selected' : ''}>${J.esc(t)}</option>`).join('')}</select></label>` : ''}
      </div>`}</div></div>`;
  }

  function freshViews() {
    for (const name of Object.keys(J.views)) J.state[name] = J.views[name].init();
  }

  Object.assign(J.actions, {
    nav(ds) { J.state.view = ds.view; J.state.menu = false; save(); J.render(); window.scrollTo(0, 0); },
    menu() { J.state.menu = !J.state.menu; J.render(); },
    setBiz(ds, el) { J.state.biz = el.value; save(); freshViews(); J.render(); },
    setRole(ds, el) { J.state.role = el.value; save(); J.state.admin.open = null; J.render(); },
    inbox() { J.state.inbox = !J.state.inbox; if (J.state.inbox) J.arrive('inbox', 'enter'); J.render(); },
    signOutDemo() { J.api.signOut(); },
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
    arriving = {};
    remember();
  };
  J.top = () => window.scrollTo({ top: 0 });

  /* ------------------------------------------------------------------ the browser's Back and Forward
     The site is one page, so each place (an area, plus a booking step, an admin tab or open booking, an account tab) is written to the
     address as #area/place and kept in the browser's history. Back and Forward hand the place to its area (views[name].goPlace),
     which goes there only if it still can (a later booking step needs the earlier ones filled in); otherwise the address is corrected. */
  const AREA_IDS = ['book', 'account', 'admin', 'signup'];
  let shown = null, restoring = false;
  function placeNow() {
    const view = J.views[J.state.view];
    const sub = view.place ? view.place() : '';
    return J.state.view + (sub ? '/' + sub : '');
  }
  function remember() {
    const now = placeNow();
    if (now === shown && location.hash === '#' + now) return;
    try { // (the same place with another address: Back or Forward asked for a place that could not be reached, so the address is corrected)
      if (shown === null || restoring || now === shown) history.replaceState(null, '', '#' + now);
      else history.pushState(null, '', '#' + now);
    } catch (e) { /* history not available: Back leaves the site, as before */ }
    shown = now;
  }
  function goTo(hash) {
    const [area, ...rest] = decodeURIComponent(hash || '').split('/');
    if (!AREA_IDS.includes(area)) return false;
    for (const name of Object.keys(J.views)) if (!J.state[name]) J.state[name] = J.views[name].init();
    J.state.view = area; J.state.menu = false; save();
    const view = J.views[area];
    if (view.goPlace) view.goPlace(rest.join('/'));
    return true;
  }
  window.addEventListener('popstate', () => {
    restoring = true;
    try { goTo(location.hash.slice(1)); J.render(); } finally { restoring = false; }
    J.top();
  });

  /** What the backend "sent": nothing leaves this computer, so the codes and links people would get by WhatsApp, SMS or email show up here. */
  function inboxPanel() {
    if (!J.state.inbox) return '';
    const list = J.inbox.slice().reverse().slice(0, 40);
    return `<aside class="inbox${J.arrival('inbox')}" aria-label="Messages sent"><div class="spread"><h2 style="font-size:1rem">Messages sent</h2><button type="button" class="closex" data-act="inbox" aria-label="Close">${J.icon('x')}</button></div>
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
    goTo(location.hash.slice(1)); // (a refreshed page or a shared link opens where it points, as far as it still can)
    J.render();
  };
})();
