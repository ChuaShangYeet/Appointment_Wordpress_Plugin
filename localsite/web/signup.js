/* Self-service sign-up on the free plan (backend: GET /v1/countries, /v1/signup/config, POST /v1/signup, POST /v1/signup/verify).
   Nothing exists until the owner confirms the emailed link; then the business, its website and its keys are created, and the keys are shown once. */
(function () {
  const J = window.JS;
  const api = J.api;

  const TYPES = [
    ['spa', 'Sp', 'Spa or clinic', 'Massage, beauty, physio: one staff member per appointment.'],
    ['salon', 'Sa', 'Salon or barber', 'Cuts and colour, with extras added to an appointment.'],
    ['restaurant', 'Re', 'Restaurant', 'Table reservations by party size, with private rooms.'],
    ['automotive', 'Au', 'Car workshop', 'Job orders that need a mechanic and a service bay.'],
    ['professional', 'Ho', 'Home services', 'Visits to the customer, within an area you choose.'],
  ];
  /** A quick check while typing; the backend decides (app/services/maps_links.py). */
  const MAPS_LINK = /^https:\/\/(maps\.app\.goo\.gl\/|goo\.gl\/maps|(www\.|maps\.)?google\.[a-z.]+\/maps|maps\.google\.)/i;
  const FIELDS = { type: ['business_type'], business: ['name', 'address_line1', 'city', 'state', 'postcode'], website: ['domain', 'owner_email', 'accepted_terms'] };

  function init() {
    return {
      step: 'type', type: null, name: '', country: 'MY', timezone: 'Asia/Kuala_Lumpur', line1: '', line2: '', city: '', state: '', postcode: '',
      mapsUrl: '', place: null, domain: '', email: '', terms: false, human: 'no', errors: {}, busy: false, err: null, created: null,
    };
  }
  const st = () => J.state.signup;
  const e = (k) => st().errors[k];

  function preview() {
    try { const h = api.hostOf(st().domain); return `<div class="notice info"><span>Your booking widget will work on <span class="mono">https://${J.esc(h)}</span> and <span class="mono">https://www.${J.esc(h)}</span>.</span></div>`; } catch (x) { return ''; }
  }

  function typeStep() {
    const s = st();
    return `<div class="stack-lg"><div class="stack"><h2>What kind of business is it?</h2><p class="muted">This decides how bookings work, and can't be changed later.</p></div>
      <div class="types">${TYPES.map(([id, g, name, desc]) => `<button type="button" class="choice" data-act="su.type" data-id="${id}" aria-pressed="${s.type === id}"><span class="glyph" aria-hidden="true">${g}</span><span class="name">${name}</span><span class="desc">${desc}</span></button>`).join('')}</div>
      <div><button type="button" class="btn" data-act="su.next" ${s.type ? '' : 'disabled'}>Continue</button></div></div>`;
  }

  function businessStep() {
    const s = st(), c = J.COUNTRIES[s.country];
    return `<div class="stack-lg"><h2>About your business</h2>
      ${J.field({ id: 'su-name', label: 'Business name', model: 'signup.name', err: e('name'), placeholder: 'Serene Lotus Spa', hint: 'As customers know it. You can change it later.' })}
      <div class="stack"><span class="label">Country</span><div class="seg" role="group" aria-label="Country">${Object.entries(J.COUNTRIES).map(([k, x]) => `<button type="button" data-act="su.country" data-c="${k}" aria-pressed="${s.country === k}">${x.name}</button>`).join('')}</div>
        <span class="hint">Sets your time zone choices, phone code (+${c.calling}), currency (${c.currency}) and address format. It can't be changed later.</span></div>
      ${c.zones.length > 1 ? J.field({ id: 'su-tz', label: 'Time zone', model: 'signup.timezone', options: c.zones.map(([z, l]) => [z, `${l} (${z.split('/')[1].replace('_', ' ')})`]), hint: 'Opening hours and booking times follow this clock, including daylight saving.' }) : ''}
      <div class="stack"><span class="label">Address</span>
        ${J.field({ id: 'su-l1', label: 'Street', model: 'signup.line1', err: e('address_line1'), attrs: 'autocomplete="address-line1"' })}
        ${J.field({ id: 'su-l2', label: 'Unit, floor or building', model: 'signup.line2', optional: true, attrs: 'autocomplete="address-line2"' })}
        <div class="grid2">
          ${c.states.length ? J.field({ id: 'su-city', label: s.country === 'AU' ? 'Suburb' : 'City', model: 'signup.city', err: e('city'), attrs: 'autocomplete="address-level2"' }) : ''}
          ${c.states.length ? J.field({ id: 'su-state', label: 'State', model: 'signup.state', err: e('state'), options: [['', 'Choose']].concat(c.states.map(([k, n]) => [k, n])) }) : ''}
          ${J.field({ id: 'su-pc', label: 'Postcode', model: 'signup.postcode', err: e('postcode'), placeholder: c.postcodeExample, attrs: `inputmode="numeric" maxlength="${c.postcode + 2}" autocomplete="postal-code"` })}
        </div></div>
      ${J.field({ id: 'su-maps', label: 'Google Maps link', model: 'signup.mapsUrl', optional: true, err: e('maps_url'), placeholder: 'https://maps.app.goo.gl/…', hint: 'In Google Maps, open your business, tap <b>Share</b> and copy the link. Customers get directions from it. You can also add it later in Set-up.', attrs: 'inputmode="url" autocomplete="off"' })}
      <div class="row"><button type="button" class="btn ghost" data-act="su.back">Back</button><button type="button" class="btn" data-act="su.next">Continue</button></div></div>`;
  }

  function websiteStep() {
    const s = st(), p = J.FREE_PLAN;
    return `<div class="stack-lg"><h2>Your website and your sign-in</h2>
      ${J.field({ id: 'su-domain', label: 'Website address', model: 'signup.domain', err: e('domain'), placeholder: 'serenelotus.my', hint: 'Where your booking widget will go. On the free plan you have one website, and its address can change once every 30 days.', live: true, attrs: 'autocomplete="url" inputmode="url"' })}
      ${preview()}
      ${J.field({ id: 'su-email', label: 'Your email', model: 'signup.email', type: 'email', err: e('owner_email'), hint: 'You sign in with this: we email you a link, no password. One business per email address.', attrs: 'autocomplete="email"' })}
      <div class="card flat stack"><span class="label">The free plan includes</span><ul class="hist">
        <li><time class="mono">${p.bookings_per_month}</time><span>bookings a month</span></li><li><time class="mono">${p.resources}</time><span>staff, tables or bays</span></li>
        <li><time class="mono">${p.services}</time><span>services</span></li><li><time class="mono">${p.whatsapp_per_month} + ${p.sms_per_month}</time><span>WhatsApp and SMS messages a month (email is unlimited)</span></li></ul></div>
      <label class="check ${e('accepted_terms') ? 'invalid' : ''}" for="su-terms"><input id="su-terms" type="checkbox" data-model="signup.terms" ${s.terms ? 'checked' : ''}><span>I accept the <span class="btn link" style="display:inline">terms of service</span> (version 1) and the privacy notice.${e('accepted_terms') ? `<br><span class="err">${J.esc(e('accepted_terms'))}</span>` : ''}</span></label>
      <div class="human ${s.human === 'ok' ? 'ok' : ''}"><button type="button" data-act="su.human" aria-label="Confirm you are a person" ${s.human !== 'no' ? 'disabled' : ''}>${s.human === 'ok' ? '✓' : s.human === 'checking' ? '<span class="spinner"></span>' : ''}</button><span>${s.human === 'ok' ? 'Done' : "I'm a person, not a bot"}</span><span class="who">human check</span></div>
      ${s.err ? `<div class="notice bad"><span>${J.esc(s.err)}</span></div>` : ''}
      <div class="row"><button type="button" class="btn ghost" data-act="su.back">Back</button><button type="button" class="btn" data-act="su.submit" ${s.human === 'ok' && !s.busy ? '' : 'disabled'}>${s.busy ? J.busyLabel('Sending…') : 'Create my business'}</button></div></div>`;
  }

  function sentStep() {
    const s = st();
    return `<div class="stack-lg"><div class="stack"><h2>Check your email</h2><p>We sent a link to <b>${J.esc(s.email.trim().toLowerCase())}</b>. Open it to finish: nothing is created until you do. It works once and expires in 24 hours.</p></div>
      ${s.err ? `<div class="notice bad"><span>${J.esc(s.err)}</span></div>` : ''}
      <div class="card flat stack"><span class="label">Test site</span><p>The email is in Messages. This button opens its link.</p><div><button type="button" class="btn" data-act="su.confirm" ${s.busy ? 'disabled' : ''}>${s.busy ? J.busyLabel('Setting up…') : 'Open the confirmation link'}</button></div></div>
      <p class="small muted">Wrong address? <button type="button" class="btn link" data-act="su.edit">Go back and fix it</button>.</p></div>`;
  }

  function placeNote() {
    const p = st().place;
    if (!p) return '';
    if (p.busy) return `<div class="row muted">${J.busyLabel('Reading your Google Maps link…')}</div>`;
    if (p.err) return `<div class="notice warn"><strong>We couldn't read your Google Maps link</strong><span>${J.esc(p.err)} You can add it later in Set-up, under Location.</span></div>`;
    return `<div class="notice ok"><span>Location saved from your Google Maps link${p.saved.formatted_address ? ': ' + J.esc(p.saved.formatted_address) : ''}.</span></div>`;
  }

  function doneStep() {
    const c = st().created;
    return `<div class="stack-lg"><div class="ticket"><div class="top"><span class="label" style="color:inherit;opacity:.8">Free plan</span><h2>${J.esc(st().name)} is set up</h2><span class="ref">${J.esc(c.domain)}</span></div>
      <div class="body stack"><div class="notice warn stack"><strong>Copy your keys now: they are shown only once</strong>
        <span class="label">Website key (goes into your web page)</span><div class="secret" id="su-pk">${J.esc(c.publishable_key)}</div><div><button type="button" class="btn ghost small" data-act="copy" data-target="su-pk">Copy website key</button></div>
        <span class="label">Secret key (goes into the WordPress plugin; never share it)</span><div class="secret" id="su-sk">${J.esc(c.secret_key)}</div><div><button type="button" class="btn ghost small" data-act="copy" data-target="su-sk">Copy secret key</button></div>
        <span class="small">Lost them later? Sign in with your email and get a new pair from <b>Plan &amp; website</b>.</span></div>
      ${placeNote()}
      <p>Next: add your services, your team and your opening hours. Then your booking page is ready.</p>
      <div><button type="button" class="btn" data-act="su.toAdmin">Set up my business</button></div></div></div></div>`;
  }

  function render() {
    const s = st();
    const steps = ['type', 'business', 'website'];
    const i = steps.indexOf(s.step);
    const body = { type: typeStep, business: businessStep, website: websiteStep, sent: sentStep, done: doneStep }[s.step]();
    return `<div class="page"><div class="narrow">
      <div class="stack" style="margin-bottom:20px;gap:6px"><span class="label">JustSimple for businesses</span><h1>Take bookings on your own website</h1><p class="muted">Free for small businesses. No card needed.</p></div>
      ${i >= 0 ? `<div class="stepper">${['Kind of business', 'Details', 'Website and sign-in'].map((t, n) => `<span class="${n < i ? 'done' : n === i ? 'now' : ''}">${t}</span>`).join('')}</div>` : ''}
      <div class="card">${body}</div></div></div>`;
  }

  function body() {
    const s = st(), c = J.COUNTRIES[s.country];
    return {
      name: s.name, business_type: s.type, country: s.country, timezone: s.timezone, address_line1: s.line1, address_line2: s.line2 || null,
      city: c.states.length ? s.city : 'Singapore', state: s.state || null, postcode: s.postcode, domain: s.domain, owner_email: s.email, accepted_terms: s.terms, accepted_terms_version: '1',
    };
  }
  function localCheck(step) {
    const s = st(), c = J.COUNTRIES[s.country], err = {};
    if (step === 'business') {
      if (!s.name.trim()) err.name = "Enter your business's name";
      if (!s.line1.trim()) err.address_line1 = 'The street is needed';
      if (c.states.length && !s.city.trim()) err.city = 'The city or suburb is needed';
      if (c.states.length && !s.state) err.state = 'Choose the state';
      if (s.mapsUrl.trim() && !MAPS_LINK.test(s.mapsUrl.trim())) err.maps_url = 'Paste the link Google Maps gives you: it starts with https://maps.app.goo.gl/ or https://www.google.com/maps';
      if (!new RegExp(`^\\d{${c.postcode}}$`).test(s.postcode.replace(/\s/g, ''))) err.postcode = `A ${c.name} postcode has ${c.postcode} digits (for example ${c.postcodeExample})`;
    }
    s.errors = err;
    return !Object.keys(err).length;
  }

  Object.assign(J.actions, {
    'su.type'(ds) { st().type = ds.id; J.render(); },
    'su.country'(ds) { const s = st(); s.country = ds.c; s.timezone = J.COUNTRIES[ds.c].zones[0][0]; s.state = ''; s.errors = {}; J.render(); },
    'su.back'() { const s = st(); s.step = s.step === 'website' ? 'business' : 'type'; s.err = null; J.render(); J.top(); },
    'su.next'() { const s = st(); if (s.step === 'business' && !localCheck('business')) { J.render(); return; } s.step = s.step === 'type' ? 'business' : 'website'; J.render(); J.top(); },
    'su.human'() { const s = st(); s.human = 'checking'; J.render(); setTimeout(() => { s.human = 'ok'; J.render(); }, 900); },
    async 'su.submit'() {
      const s = st();
      s.busy = true; s.err = null; J.render();
      const [, err] = await J.call(() => api.signupStart(body()));
      s.busy = false;
      if (err) {
        if (err.extra && err.extra.fields) {
          s.errors = err.extra.fields;
          const first = Object.keys(FIELDS).find((k) => FIELDS[k].some((f) => s.errors[f]));
          if (first && first !== s.step) { s.step = first; J.toast('Some details need fixing'); }
        } else s.err = err.status === 503 ? "We couldn't send the confirmation email just now. Try again in a moment." : err.detail;
        s.human = 'no';
        J.render(); return;
      }
      s.step = 'sent'; s.errors = {}; J.render(); J.top();
    },
    'su.edit'() { const s = st(); s.step = 'website'; s.human = 'no'; J.render(); },
    async 'su.confirm'() {
      const s = st();
      s.busy = true; J.render();
      const [created, err] = await J.call(() => api.signupVerify());
      s.busy = false;
      if (err) { s.err = 'This link was already used or has expired. Sign up again to get a new one.'; J.render(); return; }
      s.created = created; s.step = 'done'; J.render(); J.top();
      if (s.mapsUrl.trim()) { // the business exists now: its location comes from the link, as in Set-up > Location
        s.place = { busy: true }; J.render();
        const [res, perr] = await J.call(() => api.setLocationFromLink(created.tenant_id, s.mapsUrl.trim(), 25));
        s.place = perr ? { err: perr.detail } : { saved: res };
        J.render();
      }
    },
    'su.toAdmin'() {
      const id = st().created.tenant_id;
      J.state.biz = id; J.state.role = 'owner'; J.state.view = 'admin';
      J.state.book = J.views.book.init(); J.state.account = J.views.account.init(); J.state.admin = J.views.admin.init(); J.state.admin.tab = 'setup';
      J.state.signup = init();
      J.render(); J.top();
    },
  });

  /** Back and Forward: the step; only back to a step already passed, and not once the request is sent. */
  const ORDER = ['type', 'business', 'website'];
  function place() { const s = st(); s.far = Math.max(s.far || 0, ORDER.indexOf(s.step)); return s.step; }
  function goPlace(step) {
    const s = st(), i = ORDER.indexOf(step);
    if (i < 0 || !ORDER.includes(s.step) || i > (s.far || 0)) return false;
    s.step = step; s.err = null;
    return true;
  }
  J.views.signup = { init, render, place, goPlace };
})();
