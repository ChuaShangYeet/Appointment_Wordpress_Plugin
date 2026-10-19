/* The customer's own area (backend: POST /public/auth/otp(/verify), /magic-link(/verify), /email(/confirm), GET /public/me/bookings,
   POST /public/bookings/{id}/cancel, /reschedule, /review, /public/me/addresses, /me/vehicles, /me/profile, /me/data-requests). */
(function () {
  const J = window.JS;
  const T = J.T;
  const api = J.api;

  function init() {
    return {
      session: null, me: null, mode: 'phone', phone: '', code: '', codeSent: null, email: '', linkSent: false, busy: false, err: null,
      tab: 'upcoming', bookings: null, loading: false, cancelAsk: null, cancelBusy: false,
      move: null, rate: null, confirm: { email: '', sent: false, err: null, busy: false }, addr: { text: '', err: null, busy: false }, reqErr: null,
    };
  }
  const st = () => J.state.account;
  const LIVE = ['pending', 'confirmed', 'in-progress', 'waiting-for-parts'];

  async function refresh() {
    const s = st(), id = J.state.biz;
    s.loading = true; J.render();
    const [list] = await J.call(() => api.myBookings(id, s.session));
    const [me] = await J.call(() => api.me(id, s.session));
    s.bookings = list || []; s.me = me; s.loading = false;
    J.render();
  }

  /* ------------------------------------------------------------------ signing in */
  function signIn(b) {
    const s = st();
    const phone = s.mode === 'phone';
    let body;
    if (phone && !s.codeSent) {
      body = `<form class="stack" data-submit="acc.sendCode">
        ${J.field({ id: 'a-phone', label: 'Mobile number', model: 'account.phone', type: 'tel', placeholder: '012-345 6789', hint: 'The number you booked with. We send a 6-digit code to it by WhatsApp.', attrs: 'autocomplete="tel" inputmode="tel"' })}
        ${s.err ? `<p class="err">${J.esc(s.err)}</p>` : ''}
        <button class="btn" type="submit" ${s.busy ? 'disabled' : ''}>${s.busy ? J.busyLabel('Sending…') : 'Send me a code'}</button>
        <p class="hint">Prototype: <b class="mono">012-345 6789</b> has sample bookings at every business.</p></form>`;
    } else if (phone) {
      body = `<form class="stack" data-submit="acc.verify">
        <p>We sent a code by ${J.esc(s.codeSent.channel)} to <b class="mono">${J.esc(s.codeSent.to)}</b>. It works for 5 minutes.</p>
        <label class="field" for="a-code"><span>6-digit code</span><input id="a-code" class="codebox" type="text" inputmode="numeric" autocomplete="one-time-code" maxlength="6" data-model="account.code" value="${J.esc(s.code)}"></label>
        ${s.err ? `<p class="err">${J.esc(s.err)}</p>` : ''}
        <button class="btn" type="submit" ${s.busy ? 'disabled' : ''}>${s.busy ? J.busyLabel('Checking…') : 'Sign in'}</button>
        <div class="row small"><button type="button" class="btn link" data-act="acc.otherNumber">Use a different number</button></div>
        <p class="hint">Prototype: any 6 digits sign you in, except 000000 (a wrong code).</p></form>`;
    } else if (!s.linkSent) {
      body = `<form class="stack" data-submit="acc.sendLink">
        ${J.field({ id: 'a-email', label: 'Email', model: 'account.email', type: 'email', placeholder: 'you@example.com', hint: 'The address you gave when you booked. We email you a sign-in link.', attrs: 'autocomplete="email"' })}
        ${s.err ? `<p class="err">${J.esc(s.err)}</p>` : ''}
        <button class="btn" type="submit" ${s.busy ? 'disabled' : ''}>${s.busy ? J.busyLabel('Sending…') : 'Email me a link'}</button>
        <p class="hint">Prototype: <b class="mono">aisha.rahman@gmail.com</b> has sample bookings.</p></form>`;
    } else {
      const sent = api.linkWasSent(b.id, s.email);
      body = `<div class="stack"><div class="notice info"><strong>Check your inbox</strong><span>If ${J.esc(s.email)} has bookings at ${J.esc(b.name)}, a sign-in link is on its way. It works once, for 15 minutes.</span></div>
        <div class="card flat stack"><span class="label">Prototype</span>${sent ? `<p>Pretend you opened the email and tapped the link.</p><div><button type="button" class="btn" data-act="acc.openLink" ${s.busy ? 'disabled' : ''}>${s.busy ? J.busyLabel('Opening…') : 'Open the link from the email'}</button></div>` : '<p class="muted">No email was sent: no booking here was made with that address. The customer sees the same message either way, so nobody can find out who books here.</p>'}</div>
        ${s.err ? `<p class="err">${J.esc(s.err)}</p>` : ''}
        <div><button type="button" class="btn link" data-act="acc.otherEmail">Use a different email</button></div></div>`;
    }
    return `<div class="card stack-lg"><div class="stack"><h2>Your bookings</h2><p class="muted">Sign in to see, move or cancel your bookings at ${J.esc(b.name)}. No password needed.</p></div>
      <div class="seg" role="group" aria-label="Sign-in method"><button type="button" data-act="acc.mode" data-m="phone" aria-pressed="${phone}">Code to my phone</button><button type="button" data-act="acc.mode" data-m="email" aria-pressed="${!phone}">Link to my email</button></div>
      ${body}</div>`;
  }

  /* ------------------------------------------------------------------ bookings */
  function bookingLine(b, x) {
    const staff = x.allocations.map((a) => a.rid).filter((id, i, all) => all.indexOf(id) === i).map((id) => J.api.staffName(b.id, id));
    if (b.type === 'restaurant') return `${x.party_size} ${x.party_size === 1 ? 'person' : 'people'} · ${staff.join(' + ')}`;
    const people = b.resources.filter((r) => r.type === 'staff').map((r) => r.name);
    return `${x.service_names.map(J.esc).join(' + ')} · with ${J.esc(staff.filter((n) => people.includes(n)).join(', ') || 'our team')}`;
  }

  function moveBox(b, x) {
    const m = st().move;
    let slots = '';
    if (m.loading) slots = `<div class="row muted">${J.busyLabel('Finding free times…')}</div>`;
    else if (m.slots) slots = m.slots.length ? J.views.book.slotGrid(b, m.slots, m.slot, 'acc.moveSlot') : '<p class="muted">Nothing free that day.</p>';
    const named = b.type !== 'restaurant' && b.type !== 'automotive';
    return `<div class="card flat stack" style="background:var(--surface-2)"><h3>Move to another time</h3>
      ${named ? `<label class="check" for="a-anystaff"><input id="a-anystaff" type="checkbox" data-change="acc.moveAny" ${m.any ? 'checked' : ''}><span>Any ${J.WHO[b.who] || 'staff member'} is fine (shows more times)</span></label>` : ''}
      ${J.views.book.dayStrip({ date: m.date }, m.month, 'acc.moveDate')}${slots}
      ${m.slot && Date.parse(m.slot) - Date.now() < b.policy.cancel_notice_h * 3600000 ? `<div class="notice warn"><span>This time is less than ${b.policy.cancel_notice_h} hours away: once moved, you can't cancel or move it again online.</span></div>` : ''}
      ${m.err ? `<div class="notice bad"><span>${J.esc(m.err)}</span></div>` : ''}
      <div class="row"><button type="button" class="btn" data-act="acc.moveConfirm" ${m.slot && !m.busy ? '' : 'disabled'}>${m.busy ? J.busyLabel('Moving…') : m.slot ? 'Move to ' + T.dateLabel(m.slot) + ', ' + T.time(m.slot) : 'Pick a new time'}</button><button type="button" class="btn ghost" data-act="acc.moveCancel">Keep the current time</button></div></div>`;
  }

  function upcomingCard(b, x) {
    const s = st();
    const start = new Date(x.start_at);
    let rule = '';
    if (x.status === 'pending') rule = `<span class="muted small">${J.esc(b.name)} hasn't answered yet. Your time is held until ${T.time(x.hold_expires_at)}; you can withdraw the request any time.</span>`;
    else if (x.can_cancel) rule = `<span class="muted small">Free to cancel or move until ${T.when(x.cancel_deadline)}.</span>`;
    else if (x.status === 'confirmed') rule = `<span class="small">Too late to change online: the limit was ${T.when(x.cancel_deadline)}. Call <span class="mono">${J.esc(b.phone)}</span> if your plans change.</span>`;
    const asking = s.cancelAsk === x.id;
    return `<article class="card bk">
      <div class="spread"><span class="when">${T.day(T.key(start), { weekday: 'short', day: 'numeric', month: 'short' })} · ${T.time(start)}</span>${J.chip(x.status)}</div>
      <span>${bookingLine(b, x)}</span>
      ${x.vehicle ? `<span class="muted small mono">${J.esc(x.vehicle.plate)} · ${J.esc(x.vehicle.model)}</span>` : ''}${x.address ? `<span class="muted small">${J.esc(x.address.line1)}</span>` : ''}
      ${rule}<span class="muted small mono">REF ${J.esc(x.ref)}</span>
      ${asking ? `<div class="notice warn"><strong>${x.status === 'pending' ? 'Withdraw this request?' : 'Cancel this booking?'}</strong><span>The time goes back to ${J.esc(b.name)} for someone else.</span>
          <div class="row" style="margin-top:6px"><button type="button" class="btn danger" data-act="acc.cancelYes" data-id="${x.id}" ${s.cancelBusy ? 'disabled' : ''}>${s.cancelBusy ? J.busyLabel('Cancelling…') : 'Yes, cancel it'}</button><button type="button" class="btn ghost" data-act="acc.cancelNo">Keep it</button></div></div>`
        : x.can_cancel ? `<div class="actions"><button type="button" class="btn ghost small" data-act="acc.moveOpen" data-id="${x.id}">Move</button><button type="button" class="btn danger small" data-act="acc.cancelAsk" data-id="${x.id}">${x.status === 'pending' ? 'Withdraw request' : 'Cancel'}</button></div>` : ''}
      ${s.move && s.move.id === x.id ? moveBox(b, x) : ''}</article>`;
  }

  function pastCard(b, x) {
    const s = st();
    const start = new Date(x.start_at);
    let review = '';
    if (x.status === 'completed') {
      if (x.review) review = `<span class="small">You rated this <span class="stars">${'★'.repeat(x.review.rating)}</span>${x.review.comment ? ': “' + J.esc(x.review.comment) + '”' : ''}</span>`;
      else if (s.rate && s.rate.id === x.id) {
        const r = s.rate;
        review = `<div class="card flat stack" style="background:var(--surface-2)"><span class="label">How was it?</span>
          <div class="rate" role="radiogroup" aria-label="Rating">${[1, 2, 3, 4, 5].map((n) => `<button type="button" role="radio" aria-checked="${r.rating === n}" aria-label="${n} star${n > 1 ? 's' : ''}" class="${n <= r.rating ? 'on' : ''}" data-act="acc.star" data-n="${n}">★</button>`).join('')}</div>
          ${J.field({ id: 'a-comment', label: 'Anything to add?', model: 'account.rate.comment', area: true, optional: true, hint: 'Shown on the booking page with your first name only.' })}
          ${r.err ? `<p class="err">${J.esc(r.err)}</p>` : ''}
          <div class="row"><button type="button" class="btn" data-act="acc.rateSend" ${r.rating && !r.busy ? '' : 'disabled'}>${r.busy ? J.busyLabel('Sending…') : 'Send review'}</button><button type="button" class="btn ghost" data-act="acc.rateClose">Not now</button></div></div>`;
      } else review = `<div><button type="button" class="btn ghost small" data-act="acc.rateOpen" data-id="${x.id}">Rate this visit</button></div>`;
    }
    return `<article class="card bk"><div class="spread"><span class="when" style="font-size:1rem">${T.day(T.key(start), { weekday: 'short', day: 'numeric', month: 'short', year: 'numeric' })} · ${T.time(start)}</span>${J.chip(x.status)}</div>
      <span class="muted">${bookingLine(b, x)}</span>${review}</article>`;
  }

  function details(b) {
    const s = st(), me = s.me, c = s.confirm;
    if (!me) return `<div class="row muted">${J.busyLabel('Loading…')}</div>`;
    const emailState = me.email ? (me.emailConfirmed ? '<span class="chip confirmed">Confirmed</span>' : '<span class="chip pending">Not confirmed</span>') : '';
    return `<div class="stack-lg">
      <section class="card stack"><h3>You</h3>
        <dl class="summary"><dt>Name</dt><dd>${J.esc(me.name || '—')}</dd><dt>Mobile</dt><dd class="mono">${J.esc(J.fmt.prettyPhone(me.phone))}</dd><dt>Email</dt><dd>${J.esc(me.email || 'None yet')} ${emailState}</dd></dl>
        ${me.emailConfirmed ? '' : `<div class="stack"><p class="small muted">Confirm an email to sign in with a link, and to get booking emails there.${me.email ? ' Until you do, the email on file might have been typed in by someone else.' : ''}</p>
          ${c.sent ? `<div class="notice info"><span>We sent a confirmation link to <b>${J.esc(c.email)}</b>.</span></div><div class="card flat stack"><span class="label">Prototype</span><div><button type="button" class="btn small" data-act="acc.confirmOpen">Open the confirmation link</button></div></div>`
            : `<form class="row" data-submit="acc.confirmSend"><input id="a-confirm" type="email" data-model="account.confirm.email" value="${J.esc(c.email || me.email || '')}" placeholder="you@example.com" style="flex:1;min-width:200px" aria-label="Email to confirm"><button class="btn small" type="submit" ${c.busy ? 'disabled' : ''}>${c.busy ? J.busyLabel('Sending…') : 'Send confirmation link'}</button></form>`}
          ${c.err ? `<p class="err">${J.esc(c.err)}</p>` : ''}</div>`}
        ${b.collectsGender ? J.field({ id: 'a-gender', label: 'Gender', model: 'account.me.gender', options: [['', 'Prefer not to say'], ['female', 'Female'], ['male', 'Male'], ['other', 'Other']], attrs: 'data-change="acc.gender"', hint: 'Only staff see this.' }) : ''}
      </section>
      ${b.type === 'professional' ? `<section class="card stack"><h3>Saved addresses</h3>${me.addresses.length ? me.addresses.map((a) => `<div class="spread"><span>${J.esc(a.line1)} <span class="muted small">· ${a.distance_km} km away</span></span><button type="button" class="btn danger small" data-act="acc.remove" data-kind="address" data-id="${a.id}">Remove</button></div>`).join('') : '<p class="muted">None yet.</p>'}
        <form class="stack" data-submit="acc.addAddress">${J.field({ id: 'a-addr', label: 'Add an address', model: 'account.addr.text', placeholder: 'House number, street, area', err: s.addr.err })}<div><button class="btn ghost small" type="submit" ${s.addr.busy ? 'disabled' : ''}>${s.addr.busy ? J.busyLabel('Checking…') : 'Save address'}</button></div></form></section>` : ''}
      ${b.type === 'automotive' ? `<section class="card stack"><h3>Your cars</h3>${me.vehicles.length ? me.vehicles.map((v) => `<div class="spread"><span><b class="mono">${J.esc(v.plate_no)}</b> <span class="muted">${J.esc(v.make)} ${J.esc(v.model)}, ${v.year}</span></span><button type="button" class="btn danger small" data-act="acc.remove" data-kind="vehicle" data-id="${v.id}">Remove</button></div>`).join('') : '<p class="muted">None yet. A car you book with is saved here.</p>'}</section>` : ''}
      <section class="card stack"><h3>Your data</h3><p class="small muted">Under the Personal Data Protection Act you can ask ${J.esc(b.name)} for a copy of what they hold about you, or ask them to delete it. The owner handles requests.</p>
        <div class="row"><button type="button" class="btn ghost small" data-act="acc.request" data-kind="access">Ask for a copy of my data</button><button type="button" class="btn danger small" data-act="acc.request" data-kind="erasure">Ask to delete my data</button></div>
        ${s.reqErr ? `<p class="err">${J.esc(s.reqErr)}</p>` : ''}
        ${me.requests.length ? `<ul class="hist">${me.requests.map((r) => `<li><time>${T.dateLabel(r.requested_at)}</time><span>${r.kind === 'access' ? 'Copy of my data' : 'Delete my data'} · <b>${r.status === 'pending' ? 'waiting for the owner' : r.status}</b></span></li>`).join('')}</ul>` : ''}</section></div>`;
  }

  function signedIn(b) {
    const s = st(), x = s.session;
    const now = Date.now();
    const list = s.bookings || [];
    const upcoming = list.filter((y) => LIVE.includes(y.status) && Date.parse(y.end_at) >= now);
    const past = list.filter((y) => !upcoming.includes(y)).reverse();
    const tabs = [['upcoming', `Upcoming (${upcoming.length})`], ['past', 'Past']].concat(x.limited ? [] : [['details', 'Your details']]);
    if (x.limited && s.tab === 'details') s.tab = 'upcoming';
    let body;
    if (s.loading && !s.bookings) body = `<div class="row muted">${J.busyLabel('Loading your bookings…')}</div>`;
    else if (s.tab === 'details') body = details(b);
    else {
      const rows = s.tab === 'upcoming' ? upcoming : past;
      body = rows.length ? `<div class="stack">${rows.map((y) => (s.tab === 'upcoming' ? upcomingCard(b, y) : pastCard(b, y))).join('')}</div>`
        : `<div class="empty"><h3>${s.tab === 'upcoming' ? 'Nothing coming up' : 'No past visits yet'}</h3>${s.tab === 'upcoming' ? '<button type="button" class="btn" data-act="nav" data-view="book">Book now</button>' : ''}</div>`;
    }
    return `<div class="stack-lg">
      <div class="spread"><div class="stack" style="gap:2px"><span class="label">Signed in</span><b>${J.esc((s.me && s.me.name) || J.fmt.prettyPhone(x.phone))}</b></div><button type="button" class="btn ghost small" data-act="acc.signOut">Sign out</button></div>
      ${x.limited ? `<div class="notice warn"><strong>You signed in with an email link</strong><span>You can see and change the bookings made with ${J.esc(x.email)}. To see all your bookings and your saved details, sign in with your phone number.</span><div style="margin-top:6px"><button type="button" class="btn small ghost" data-act="acc.toPhone">Sign in with my phone</button></div></div>` : ''}
      <div class="tabs" role="tablist">${tabs.map(([k, t]) => `<button type="button" role="tab" aria-current="${s.tab === k}" data-act="acc.tab" data-tab="${k}">${t}</button>`).join('')}</div>
      ${body}</div>`;
  }

  function render() {
    const b = J.biz();
    const s = st();
    return `<div class="page"><div class="narrow">${J.storefront(b)}${s.session ? signedIn(b) : signIn(b)}</div></div>`;
  }

  /* ------------------------------------------------------------------ actions */
  async function loadMoveDay() {
    const s = st(), m = s.move;
    m.loading = true; m.slot = null; J.render();
    const [slots] = await J.call(() => api.moveOptions(J.state.biz, m.id, m.date, m.any, false));
    m.loading = false; m.slots = slots || []; J.render();
  }
  async function loadMoveMonth() {
    const s = st(), m = s.move, b = J.biz();
    const x = s.bookings.find((y) => y.id === m.id);
    const staff = x.allocations.map((a) => a.rid).find((rid) => (b.resources.find((r) => r.id === rid) || {}).type === 'staff');
    const q = { service_ids: x.service_ids, party_size: x.party_size, table_type: x.table_type || 'normal', resource_id: m.any || b.type === 'automotive' ? null : staff || null };
    m.month = null; J.render();
    const [month] = await J.call(() => api.month(J.state.biz, q));
    m.month = month || [];
    const first = m.month.find((d) => d.available);
    m.date = first ? first.date : null;
    if (m.date) await loadMoveDay(); else J.render();
  }

  Object.assign(J.actions, {
    'acc.mode'(ds) { const s = st(); s.mode = ds.m; s.err = null; J.render(); },
    async 'acc.sendCode'() {
      const s = st();
      s.busy = true; s.err = null; J.render();
      const [res, err] = await J.call(() => api.requestOtp(J.state.biz, s.phone, J.state.scenario));
      s.busy = false;
      if (err) s.err = err.status === 503 ? "We couldn't send the code just now. Try again in a moment, or sign in with an email link instead." : err.detail;
      else { s.codeSent = res; s.code = ''; }
      J.render();
    },
    async 'acc.verify'() {
      const s = st();
      s.busy = true; s.err = null; J.render();
      const [session, err] = await J.call(() => api.verifyOtp(J.state.biz, s.phone, s.code.trim()));
      s.busy = false;
      if (err) { s.err = err.status === 403 ? 'Too many tries. Ask for a new code.' : 'That code is wrong or has expired. Check the message and try again.'; J.render(); return; }
      s.session = session; s.tab = 'upcoming'; s.codeSent = null; s.code = '';
      refresh();
    },
    'acc.otherNumber'() { const s = st(); s.codeSent = null; s.err = null; J.render(); },
    async 'acc.sendLink'() {
      const s = st();
      s.busy = true; s.err = null; J.render();
      const [, err] = await J.call(() => api.requestMagicLink(J.state.biz, s.email, J.state.scenario));
      s.busy = false;
      if (err) s.err = err.detail; else s.linkSent = true;
      J.render();
    },
    async 'acc.openLink'() {
      const s = st();
      s.busy = true; J.render();
      const [session, err] = await J.call(() => api.openMagicLink(J.state.biz, s.email));
      s.busy = false;
      if (err) { s.err = 'This link has expired or was already used. Ask for a new one.'; J.render(); return; }
      s.session = session; s.linkSent = false; s.tab = 'upcoming';
      refresh();
    },
    'acc.otherEmail'() { const s = st(); s.linkSent = false; s.err = null; J.render(); },
    'acc.signOut'() { J.state.account = init(); J.render(); },
    'acc.toPhone'() { const keep = st().session.phone; J.state.account = init(); st().mode = 'phone'; st().phone = J.fmt.prettyPhone(keep); J.render(); },
    'acc.tab'(ds) { const s = st(); s.tab = ds.tab; s.move = null; s.cancelAsk = null; J.render(); },
    'acc.cancelAsk'(ds) { const s = st(); s.cancelAsk = ds.id; s.move = null; J.render(); },
    'acc.cancelNo'() { st().cancelAsk = null; J.render(); },
    async 'acc.cancelYes'(ds) {
      const s = st();
      s.cancelBusy = true; J.render();
      const [, err] = await J.call(() => api.cancel(J.state.biz, ds.id, s.session));
      s.cancelBusy = false; s.cancelAsk = null;
      if (err) J.toast(err.status === 422 ? 'Too late to cancel online. Please call the business.' : err.detail); else J.toast('Booking cancelled');
      refresh();
    },
    'acc.moveOpen'(ds) { const s = st(); s.cancelAsk = null; s.move = { id: ds.id, any: false, date: null, month: null, slots: null, slot: null, err: null }; loadMoveMonth(); },
    'acc.moveCancel'() { st().move = null; J.render(); },
    'acc.moveAny'(ds, el) { const m = st().move; m.any = el.checked; loadMoveMonth(); },
    'acc.moveDate'(ds, el) { if (el.getAttribute('aria-disabled') === 'true') return; const m = st().move; m.date = ds.date; loadMoveDay(); },
    'acc.moveSlot'(ds) { st().move.slot = ds.start; J.render(); },
    async 'acc.moveConfirm'() {
      const s = st(), m = s.move;
      m.busy = true; m.err = null; J.render();
      const [res, err] = await J.call(() => api.reschedule(J.state.biz, m.id, { start_at: m.slot, any_available: m.any }, s.session));
      m.busy = false;
      if (err) { m.err = err.status === 409 ? 'That time was just taken. Pick another.' : err.status === 422 ? 'Too late to move this booking online. Please call the business.' : err.detail; if (err.status === 409) { m.slot = null; await loadMoveDay(); } else J.render(); return; }
      s.move = null; J.toast(`Moved to ${T.when(res.start_at)}`);
      refresh();
    },
    'acc.rateOpen'(ds) { st().rate = { id: ds.id, rating: 0, comment: '', err: null, busy: false }; J.render(); },
    'acc.rateClose'() { st().rate = null; J.render(); },
    'acc.star'(ds) { st().rate.rating = Number(ds.n); J.render(); },
    async 'acc.rateSend'() {
      const s = st(), r = s.rate;
      r.busy = true; J.render();
      const [, err] = await J.call(() => api.review(J.state.biz, r.id, { rating: r.rating, comment: r.comment }, s.session));
      r.busy = false;
      if (err) { r.err = err.detail; J.render(); return; }
      s.rate = null; J.toast('Thanks for your review');
      refresh();
    },
    async 'acc.confirmSend'() {
      const s = st(), c = s.confirm;
      if (!c.email) c.email = s.me.email || '';
      c.busy = true; c.err = null; J.render();
      const [, err] = await J.call(() => api.requestEmailConfirm(J.state.biz, s.session, c.email));
      c.busy = false;
      if (err) c.err = err.detail; else c.sent = true;
      J.render();
    },
    async 'acc.confirmOpen'() {
      const s = st();
      const [, err] = await J.call(() => api.openEmailConfirm(J.state.biz, s.session));
      if (err) { s.confirm.err = err.detail; J.render(); return; }
      s.confirm = { email: '', sent: false, err: null, busy: false };
      J.toast('Email confirmed');
      refresh();
    },
    async 'acc.gender'(ds, el) { await J.call(() => api.updateGender(J.state.biz, st().session, el.value)); J.toast('Saved'); },
    async 'acc.addAddress'() {
      const s = st();
      s.addr.busy = true; s.addr.err = null; J.render();
      const [, err] = await J.call(() => api.addAddress(J.state.biz, s.session, s.addr.text));
      s.addr.busy = false;
      if (err) { s.addr.err = err.detail; J.render(); return; }
      s.addr.text = ''; J.toast('Address saved'); refresh();
    },
    async 'acc.remove'(ds) {
      const s = st();
      const [, err] = await J.call(() => api.removeSaved(J.state.biz, s.session, ds.kind, ds.id));
      J.toast(err ? err.detail : ds.kind === 'address' ? 'Address removed' : 'Car removed');
      refresh();
    },
    async 'acc.request'(ds) {
      const s = st();
      s.reqErr = null;
      const [, err] = await J.call(() => api.dataRequest(J.state.biz, s.session, ds.kind));
      if (err) s.reqErr = err.detail; else J.toast(ds.kind === 'access' ? 'Request sent: the owner will send you a copy' : 'Request sent: the owner will delete your data once your bookings are over');
      refresh();
    },
  });

  J.views.account = { init, render };
})();
