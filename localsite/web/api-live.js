/* The screens' API, answered by the real backend running on this computer (run_local.py). It has the same functions as the prototype's
   in-browser mock (prototype/api.js), so the screens hardly change; each function calls the real endpoints and turns the answer into the
   shape the screens expect. Where the real API does not give the screens something they need, the gap is named in a "BACKEND GAP" comment
   and the local test site fills it (from the admin API, or from data/local.json); none of those fills exist in the product. */
window.JS = window.JS || {};
(function () {
  const OFFSET = '+08:00';
  const TZ = 'Asia/Kuala_Lumpur';
  const MIN = 60000, HOUR = 3600000, DAY = 86400000;
  const pad = (n) => String(n).padStart(2, '0');
  const partsFmt = new Intl.DateTimeFormat('en-GB', { timeZone: TZ, year: 'numeric', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', hourCycle: 'h23' });
  function kl(d) { const o = {}; for (const p of partsFmt.formatToParts(d)) o[p.type] = p.value; return o; }

  /* ------------------------------------------------------------------ time, always on the business's clock (the sample businesses are all in Malaysia) */
  const T = {
    key(d) { const p = kl(new Date(d)); return `${p.year}-${p.month}-${p.day}`; },
    minutes(d) { const p = kl(new Date(d)); return (+p.hour) * 60 + (+p.minute); },
    at(key, min) { return new Date(Date.parse(`${key}T00:00:00${OFFSET}`) + min * MIN); },
    addDays(key, n) { const d = new Date(key + 'T00:00:00Z'); d.setUTCDate(d.getUTCDate() + n); return d.toISOString().slice(0, 10); },
    weekday(key) { return new Date(key + 'T00:00:00Z').getUTCDay(); },
    today() { return T.key(Date.now()); },
    day(key, opts) { return new Intl.DateTimeFormat('en-GB', { timeZone: 'UTC', ...opts }).format(new Date(key + 'T00:00:00Z')); },
    clock(min) { const h = Math.floor(min / 60) % 24, m = min % 60; return `${((h + 11) % 12) + 1}${m ? ':' + pad(m) : ''} ${h < 12 ? 'am' : 'pm'}`; },
    time(d) { return T.clock(T.minutes(d)); },
    time24(d) { const m = T.minutes(d); return `${pad(Math.floor(m / 60))}:${pad(m % 60)}`; },
    dateLabel(d) { return T.day(T.key(d), { weekday: 'short', day: 'numeric', month: 'short' }); },
    when(d) { return `${T.dateLabel(d)}, ${T.time(d)}`; },
  };
  JS.T = T;

  class ApiError extends Error {
    constructor(status, error, detail, extra = {}) { super(detail); this.status = status; this.error = error; this.detail = detail; this.extra = extra; }
  }
  JS.ApiError = ApiError;

  /* ------------------------------------------------------------------ phone numbers (the same rules as backend app/core/phone.py, for showing them) */
  function normPhone(raw, cc = '60') {
    const text = String(raw || '').trim().replace(/[\s\-().]/g, '');
    const intl = text.startsWith('+');
    let d = intl ? text.slice(1) : text;
    if (!/^\d+$/.test(d)) throw new ApiError(422, 'validation_failed', 'That is not a valid phone number');
    if (!intl) {
      if (d.startsWith('00')) d = d.slice(2);
      else if (d.startsWith('0')) d = cc + d.slice(1);
      else if (!d.startsWith(cc)) d = cc + d;
    }
    if (d.startsWith('0') || d.length < 7 || d.length > 15) throw new ApiError(422, 'validation_failed', 'That is not a valid phone number');
    return '+' + d;
  }
  function prettyPhone(p) {
    if (!p) return '';
    if (p.startsWith('+60') && p.length >= 11) { const r = p.slice(3); return r.length >= 10 ? `+60 ${r.slice(0, 2)}-${r.slice(2, 6)} ${r.slice(6)}` : `+60 ${r.slice(0, 2)}-${r.slice(2, 5)} ${r.slice(5)}`; }
    return p;
  }
  function maskPhone(p) { return p ? `${p.slice(0, 3)}****${p.slice(-4)}` : ''; }
  const normEmail = (e) => String(e || '').trim().toLowerCase();
  JS.fmt = { prettyPhone, maskPhone, normPhone };

  /* ------------------------------------------------------------------ talking to the backend */
  const FIELD_NAMES = { address_line1: 'address_line1', owner_email: 'owner_email' };
  async function http(method, path, { site, token, body, query, headers = {} } = {}) {
    let url = path;
    if (query) {
      const u = new URLSearchParams();
      for (const [k, v] of Object.entries(query)) {
        if (v == null || v === '') continue;
        if (Array.isArray(v)) v.forEach((x) => u.append(k, x)); else u.append(k, v);
      }
      if ([...u].length) url += '?' + u;
    }
    const h = { ...headers };
    if (site) h['X-Site-Key'] = site;
    if (token) h.Authorization = 'Bearer ' + token;
    if (body !== undefined) h['Content-Type'] = 'application/json';
    let r;
    try { r = await fetch(url, { method, headers: h, body: body === undefined ? undefined : JSON.stringify(body) }); } catch (e) { throw new ApiError(0, 'offline', 'The local server is not answering. Is run_local still running?'); }
    const text = await r.text();
    let data = null;
    try { data = text ? JSON.parse(text) : null; } catch (e) { data = text; }
    if (r.status === 401 && path.startsWith('/dev/') && CFG.hosted) { window.location.href = '/'; } // (the demo sign-in ran out)
    if (r.ok) { if (data && typeof data === 'object') Object.defineProperty(data, '_headers', { value: r.headers, enumerable: false }); return data; }
    if (data && Array.isArray(data.detail)) { // FastAPI's own validation answer: one message per field
      const fields = {};
      for (const d of data.detail) { const loc = (d.loc || []).filter((x) => x !== 'body' && x !== 'query'); const k = FIELD_NAMES[loc[loc.length - 1]] || loc[loc.length - 1] || '_'; fields[k] = String(d.msg || '').replace(/^Value error, /, ''); }
      throw new ApiError(422, 'validation_failed', Object.values(fields)[0] || 'Some details need fixing', { fields });
    }
    const { error, detail, ...extra } = data && typeof data === 'object' ? data : {};
    throw new ApiError(r.status, error || 'http_' + r.status, typeof detail === 'string' ? detail : String(text || r.statusText), extra);
  }

  /* ------------------------------------------------------------------ the businesses on this computer */
  let CFG = { businesses: [], sample_customer: null };
  const SNAP = {};
  const TOKENS = {};
  const cfg = (id) => { const c = CFG.businesses.find((x) => x.id === id); if (!c) throw new ApiError(404, 'not_found', 'Business not found'); return c; };
  const site = (id) => cfg(id).publishable_key;

  /** An admin token for a role (the local stand-in for the WordPress plugin's token exchange; staff tokens are pinned to their own resource). */
  async function adminToken(id, role = 'owner', resourceId = null) {
    const key = [id, role, resourceId || ''].join('|');
    const t = TOKENS[key];
    if (t && t.until > Date.now()) return t.token;
    const got = await http('POST', '/dev/admin-token', { body: { business: id, role, resource_id: resourceId, name: role === 'owner' ? 'Owner' : role === 'manager' ? 'Manager' : null } });
    TOKENS[key] = { token: got.access_token, until: Date.now() + (got.expires_in - 60) * 1000 };
    return got.access_token;
  }
  const owner = (id) => adminToken(id, 'owner');
  async function viewerToken(id, viewer) {
    if (viewer && viewer.staffId) return adminToken(id, 'staff', viewer.staffId);
    return adminToken(id, viewer && viewer.role === 'manager' ? 'manager' : 'owner');
  }

  const secs = (td) => { // pydantic sends a timedelta as ISO 8601 ("PT2H", "P60D")
    if (typeof td === 'number') return td;
    const m = /^P(?:(\d+)D)?(?:T(?:(\d+)H)?(?:(\d+)M)?(?:([\d.]+)S)?)?$/.exec(td || '');
    return m ? (+(m[1] || 0)) * 86400 + (+(m[2] || 0)) * 3600 + (+(m[3] || 0)) * 60 + (+(m[4] || 0)) : 0;
  };
  const clockMin = (t) => { const [h, m] = String(t).split(':').map(Number); return h * 60 + m; };

  async function loadBusiness(id) {
    const c = cfg(id), pk = c.publishable_key;
    const tok = await owner(id);
    const pub = (p) => http('GET', '/v1/public' + p, { site: pk });
    const adm = (p) => http('GET', '/v1/admin' + p, { token: tok });
    const [profile, rating, services, resources, products, vehicles, policies, plan, daysOff] = await Promise.all([
      pub('/business'), pub('/reviews/summary'), adm('/services'), adm('/resources'),
      c.type === 'salon' ? adm('/products') : [], c.type === 'automotive' ? pub('/supported-vehicles') : [],
      adm('/policies'), adm('/plan'), adm('/days-off'),
    ]);
    const slots = c.type === 'restaurant' ? await adm('/time-slot-settings') : null;
    const place = await adm('/business-location');
    const loc = c.type === 'professional' ? place : null;
    // BACKEND GAP: nothing says which rule set is the business's default; the local site remembers the one it made (data/local.json)
    const pol = policies.find((p) => p.id === c.default_policy_id) || policies[policies.length - 1];
    const today = T.today();
    const closedDates = [];
    for (const d of daysOff.filter((x) => !x.resource_id)) for (let k = d.start_date; k <= d.end_date && k <= T.addDays(today, 60); k = T.addDays(k, 1)) closedDates.push({ date: k, name: d.name });
    SNAP[id] = {
      id, tenant_id: c.tenant_id, type: c.type, kind: c.kind || { spa: 'Spa & wellness', salon: 'Hair salon', restaurant: 'Restaurant', automotive: 'Car service workshop', professional: 'Home services' }[c.type],
      who: c.who || { restaurant: 'table', automotive: 'mechanic', professional: 'technician', salon: 'stylist' }[c.type] || 'therapist',
      name: profile.business_name || c.name, address: profile.address || [profile.address_line1, profile.postcode, profile.city].filter(Boolean).join(', '),
      phone: profile.contact_no || '', email: profile.contact_email || '', description: profile.description || '', domain: c.domain,
      plan: plan.plan.code, rating: [rating.average ? Math.round(rating.average * 10) / 10 : 0, rating.count || 0],
      // BACKEND GAP: opening hours can be written (PUT /admin/operating-hours) but not read back; the local site keeps a copy
      hours: c.hours || null, closedDates,
      policy: pol ? { id: pol.id, name: pol.name, default_status: pol.default_status, min_notice_min: Math.round(secs(pol.min_notice_booking) / 60), cancel_notice_h: Math.round(secs(pol.min_notice_cancel) / 3600), window_days: Math.round(secs(pol.booking_window) / 86400), pending_timeout_min: Math.round(secs(pol.pending_timeout) / 60) }
        // BACKEND GAP: a business that signed itself up has no rules at all, and its booking page answers 422 until some are made
        : { missing: true, default_status: 'confirmed', min_notice_min: 120, cancel_notice_h: 24, window_days: 60, pending_timeout_min: 30 },
      step: 15, collectsGender: ['spa', 'salon'].includes(c.type), healthNotes: c.type === 'spa', isNew: !!c.is_new,
      // BACKEND GAP: which staff can do which service is not given out (not even to the admin), so every staff member is offered and the free times decide
      services: services.filter((s) => s.active).map((s) => ({ ...s, price: Number(s.price), description: s.description || '' })),
      resources: resources.map((r) => ({ ...r, note: (r.attributes || {}).note || '' })),
      products: products.map((p) => ({ ...p, price: Number(p.price) })), supportedVehicles: vehicles,
      slotSettings: slots ? { booking_mode: slots.booking_mode, first: clockMin(slots.first_slot_time), last: clockMin(slots.last_slot_time), interval: slots.slot_interval_minutes, duration: slots.slot_duration_minutes, buffer: slots.buffer_minutes } : undefined,
      place: { latitude: place.latitude, longitude: place.longitude, radius_km: place.service_radius_km, maps_url: profile.google_maps_url || null },
      location: loc ? { radius_km: loc.service_radius_km, base: (profile.address || '').split(',').slice(0, 2).join(','), latitude: loc.latitude, longitude: loc.longitude } : undefined,
    };
    return SNAP[id];
  }
  const snap = (id) => { const b = SNAP[id]; if (!b) throw new ApiError(404, 'not_found', 'Business not loaded'); return b; };
  const svcName = (b, sid) => (b.services.find((s) => s.id === sid) || {}).name;
  const ref = (id) => String(id).replace(/-/g, '').slice(0, 8).toUpperCase();

  /* ------------------------------------------------------------------ the inbox: what the backend "sent" (WhatsApp, SMS, email) */
  const INBOX = [];
  let lastMsg = 0;
  async function pollInbox() {
    try {
      const fresh = await http('GET', '/dev/messages', { query: { after: lastMsg } });
      if (fresh.length) { INBOX.push(...fresh); lastMsg = fresh[fresh.length - 1].id; if (JS.onMessages) JS.onMessages(fresh); }
    } catch (e) { /* the server may be restarting */ }
  }
  /** The token in the newest link of the given kind sent to this address (waits a moment: the message goes out as the request finishes). */
  async function tokenFromInbox(to, kind, since = 0) {
    for (let i = 0; i < 20; i++) {
      await pollInbox();
      // (the local site's links end in #link/<kind>/<token>, the hosted demo's in /demo/link/<kind>/<token>)
      const m = INBOX.slice().reverse().find((x) => x.id > since && x.to === to && linkToken(x.body, kind));
      if (m) return linkToken(m.body, kind);
      await new Promise((r) => setTimeout(r, 250));
    }
    return null;
  }
  function linkToken(body, kind) {
    const found = /(?:#|\/demo\/)link\/([a-z-]+)\/([^\s"'<>)?&]+)/g;
    for (const m of body.matchAll(found)) if (m[1] === kind) return decodeURIComponent(m[2]);
    return null;
  }
  JS.inbox = INBOX;

  function jwtClaims(token) { try { return JSON.parse(atob(token.split('.')[1].replace(/-/g, '+').replace(/_/g, '/'))); } catch (e) { return {}; } }

  function km(lat1, lon1, lat2, lon2) {
    const r = Math.PI / 180, a = Math.sin(((lat2 - lat1) * r) / 2) ** 2 + Math.cos(lat1 * r) * Math.cos(lat2 * r) * Math.sin(((lon2 - lon1) * r) / 2) ** 2;
    return Math.round(12742 * Math.asin(Math.sqrt(a)) * 10) / 10;
  }

  function availabilityQuery(q, date) {
    return { date, service_ids: q.service_ids || [], resource_id: q.resource_id, party_size: q.party_size, table_type: q.table_type || 'normal', allow_separate_seating: q.allow_separate_seating || undefined, latitude: q.latitude, longitude: q.longitude };
  }
  const slotOut = (x) => ({ ...x, minimum_spend: x.minimum_spend == null ? null : Number(x.minimum_spend), travel_minutes: x.travel_minutes || 0 });

  /* ------------------------------------------------------------------ admin bookings in the screens' shape */
  function adminRow(b, x) {
    return {
      ...x, ref: ref(x.id), customer: { name: x.customer_name || 'Unknown', phone: x.customer_phone || '' },
      service_names: (x.service_ids || []).map((s) => svcName(b, s)).filter(Boolean),
      // BACKEND GAP: the admin booking answer has the resources but not their own times (travel padding, a workshop's segments); the whole booking's time is used
      allocations: (x.resource_ids || []).map((rid) => ({ rid, s: Date.parse(x.start_at), e: Date.parse(x.end_at) })),
      minimum_spend_agreed: x.minimum_spend_agreed == null ? null : Number(x.minimum_spend_agreed),
      // BACKEND GAP: the admin answer has vehicle_id only, not the car, so the drawer cannot show it. The visit's address comes with the booking's detail.
      vehicle: null, address: x.visit_address || null, flags: x.flags || [],
    };
  }
  /** Which status buttons apply and why one is not open yet (the backend's TRANSITIONS and its timed moves, STATUS_EARLY_MINUTES = 60). */
  function allowedMoves(id, x, now = Date.now()) {
    const b = snap(id);
    const map = { pending: ['confirmed', 'rejected', 'cancelled'], confirmed: ['in-progress', 'completed', 'no-show', 'cancelled'], 'in-progress': ['completed', 'waiting-for-parts'], 'waiting-for-parts': ['cancelled'] };
    const start = Date.parse(x.start_at);
    return (map[x.status] || [])
      .filter((to) => !(to === 'in-progress' && !['salon', 'automotive', 'professional'].includes(b.type)) && !(to === 'waiting-for-parts' && b.type !== 'automotive'))
      .map((to) => {
        let blocked = null;
        if (to === 'no-show' && now < start) blocked = `Can be marked a no-show from ${T.time(start)} on ${T.dateLabel(start)}, when it starts`;
        if ((to === 'in-progress' || to === 'completed') && x.status !== 'in-progress' && now < start - HOUR) blocked = `Opens at ${T.time(start - HOUR)} on ${T.dateLabel(start)}, an hour before the start`;
        return { to, blocked };
      });
  }

  /* ------------------------------------------------------------------ the API the screens use */
  let scenarioArmed = true;
  const api = {
    T,
    allowedMoves,
    async boot() {
      CFG = await http('GET', '/dev/config');
      await Promise.all(CFG.businesses.map((c) => loadBusiness(c.id).catch((e) => console.error('could not load', c.id, e))));
      await pollInbox();
      setInterval(pollInbox, 2500);
      JS.SAMPLE_CUSTOMER = CFG.sample_customer;
    },
    reload: loadBusiness,
    hosted() { return !!CFG.hosted; },
    async signOut() { await fetch('/demo/access/sign-out', { method: 'POST' }); window.location.href = '/'; },
    reset() { JS.toast(CFG.hosted ? 'This online demo is shared by everyone, so it cannot be reset from here.' : 'To start again from the sample data, close the local site and run reset.bat (or python run_local.py --reset).'); },
    armScenario() { scenarioArmed = true; },
    businesses() { return CFG.businesses.filter((c) => SNAP[c.id]).map((c) => ({ id: c.id, name: SNAP[c.id].name, type: c.type, kind: SNAP[c.id].kind, plan: SNAP[c.id].plan })); },
    business(id) { return snap(id); },
    staffName(id, rid) { return ((snap(id).resources.find((r) => r.id === rid)) || {}).name || 'Unknown'; },

    /* ---------------- free times */
    async month(id, q, days = 21) {
      const b = snap(id), today = T.today();
      const months = [...new Set([today.slice(0, 7), T.addDays(today, days - 1).slice(0, 7)])];
      const all = {};
      for (const m of months) {
        const res = await http('GET', '/v1/public/availability/month', { site: site(id), query: { ...availabilityQuery(q, undefined), month: m } });
        for (const d of res.days) all[d.date] = d;
      }
      return Array.from({ length: days }, (_, i) => {
        const key = T.addDays(today, i);
        const d = all[key] || { available: false, slot_count: 0 };
        const holiday = b.closedDates.find((x) => x.date === key);
        const closed = holiday ? holiday.name : b.hours && !b.hours[T.weekday(key)] ? 'Closed' : null;
        return { date: key, available: d.available, slot_count: d.slot_count, closed: d.available ? null : closed };
      });
    },
    async availability(id, date, q) {
      const res = await http('GET', '/v1/public/availability', { site: site(id), query: availabilityQuery(q, date) });
      return { ...res, slots: res.slots.map(slotOut) };
    },
    async geocode(id, text) {
      const b = snap(id);
      const g = await http('POST', '/v1/public/geocode', { site: site(id), body: { address: text } });
      return { ...g, service_radius_km: b.location ? b.location.radius_km : null };
    },

    /** POST /public/bookings. The scenario "someone takes the slot first" really books that time for another guest a moment before. */
    async createBooking(id, body, scenario) {
      const b = snap(id);
      const out = {
        service_ids: body.service_ids, start_at: body.start_at, resource_id: body.resource_id || undefined,
        customer: { name: body.customer.name, phone: body.customer.phone, email: body.customer.email || undefined, gender: body.customer.gender || undefined },
        party_size: body.party_size || undefined, table_type: body.table_type || 'normal', allow_separate_seating: !!body.allow_separate_seating,
        accept_minimum_spend: !!body.accept_minimum_spend, accepted_minimum_spend: body.accepted_minimum_spend ?? undefined,
        acknowledge_lead_time: !!body.acknowledge_lead_time, special_request: body.special_request || undefined, health_notes: body.health_notes || undefined,
        vehicle: body.vehicle || undefined, source: 'headless',
        address: body.address ? { line1: body.address.formatted_address || body.address.line1, latitude: body.address.latitude, longitude: body.address.longitude } : undefined,
      };
      if (scenario === 'slot_taken' && scenarioArmed) {
        scenarioArmed = false;
        const rival = { ...out, customer: { name: 'Lim Mei Ling', phone: '+60172288140' }, health_notes: undefined, special_request: undefined, address: out.address, vehicle: out.vehicle && { ...out.vehicle, plate_no: 'VBK 9034' } };
        for (let i = 0; i < 6; i++) { try { await http('POST', '/v1/public/bookings', { site: site(id), body: rival }); } catch (e) { break; } }
      }
      try {
        const made = await http('POST', '/v1/public/bookings', { site: site(id), body: out, headers: { 'Idempotency-Key': 'web-' + Date.now() + '-' + Math.random().toString(36).slice(2, 10) } });
        return {
          ...made, ref: ref(made.id), customer: { ...out.customer, phone: normPhone(out.customer.phone) }, contact_email: out.customer.email || null,
          minimum_spend_agreed: made.minimum_spend_agreed == null ? null : Number(made.minimum_spend_agreed),
          allocations: made.resource_ids.map((rid) => ({ rid, s: Date.parse(made.start_at), e: Date.parse(made.end_at) })),
        };
      } catch (e) {
        if (e.status === 409) { // the screens offer the other free times of that day
          const day = await api.availability(id, T.key(body.start_at), { ...body, latitude: out.address && out.address.latitude, longitude: out.address && out.address.longitude }).catch(() => ({ slots: [] }));
          e.extra = { ...e.extra, slots: day.slots };
        }
        throw e;
      }
    },

    /* ---------------- customer sign-in */
    async requestOtp(id, rawPhone) {
      const phone = normPhone(rawPhone);
      await http('POST', '/v1/public/auth/otp', { site: site(id), body: { phone } });
      return { status: 'sent', to: maskPhone(phone), channel: 'WhatsApp' };
    },
    async verifyOtp(id, rawPhone, code) {
      const phone = normPhone(rawPhone);
      const t = await http('POST', '/v1/public/auth/otp/verify', { site: site(id), body: { phone, code } });
      return { method: 'phone', phone, limited: false, token: t.access_token };
    },
    async requestMagicLink(id, rawEmail) {
      const email = normEmail(rawEmail);
      api._linkAsked = { email, after: lastMsg };
      await http('POST', '/v1/public/auth/magic-link', { site: site(id), body: { email } });
      await new Promise((r) => setTimeout(r, 600));
      await pollInbox();
      return { status: 'sent' };
    },
    linkWasSent(id, rawEmail) {
      const email = normEmail(rawEmail), after = (api._linkAsked && api._linkAsked.after) || 0;
      return INBOX.some((m) => m.id > after && m.to === email && linkToken(m.body, 'sign-in'));
    },
    async openMagicLink(id, rawEmail) {
      const email = normEmail(rawEmail);
      const token = await tokenFromInbox(email, 'sign-in', (api._linkAsked && api._linkAsked.after) || 0);
      if (!token) throw new ApiError(401, 'unauthorized', 'Invalid or expired link');
      const t = await http('POST', '/v1/public/auth/magic-link/verify', { site: site(id), body: { token } });
      const limited = !!jwtClaims(t.access_token).em;
      let phone = '';
      if (!limited) { try { phone = (await http('GET', '/v1/public/me/profile', { site: site(id), token: t.access_token })).phone; } catch (e) { /* shown without it */ } }
      return { method: 'email', phone, email, limited, token: t.access_token };
    },
    async requestEmailConfirm(id, session, rawEmail) {
      const email = normEmail(rawEmail);
      api._confirmAsked = { email, after: lastMsg };
      await http('POST', '/v1/public/auth/email', { site: site(id), token: session.token, body: { email } });
      return { status: 'sent' };
    },
    async openEmailConfirm(id, session) {
      const asked = api._confirmAsked || {};
      const token = await tokenFromInbox(asked.email, 'confirm-email', asked.after || 0);
      if (!token) throw new ApiError(401, 'unauthorized', 'Invalid or expired link');
      await http('POST', '/v1/public/auth/email/confirm', { site: site(id), token: session.token, body: { token } });
      return { status: 'confirmed' };
    },

    /* ---------------- the signed-in customer */
    async me(id, session) {
      if (session.limited) return { name: session.email, phone: '', email: session.email, emailConfirmed: false, gender: null, addresses: [], vehicles: [], requests: [] };
      const b = snap(id), o = { site: site(id), token: session.token };
      const [p, addresses, vehicles, requests] = await Promise.all([
        http('GET', '/v1/public/me/profile', o), b.type === 'professional' ? http('GET', '/v1/public/me/addresses', o) : [],
        b.type === 'automotive' ? http('GET', '/v1/public/me/vehicles', o) : [], http('GET', '/v1/public/me/data-requests', o),
      ]);
      const loc = b.location;
      return {
        name: p.name, phone: p.phone, email: p.email, emailConfirmed: !!p.email_verified_at, gender: p.gender,
        addresses: addresses.map((a) => ({ ...a, distance_km: loc && a.latitude != null ? km(loc.latitude, loc.longitude, a.latitude, a.longitude) : null })),
        vehicles, requests,
      };
    },
    async myBookings(id, session) {
      const b = snap(id);
      const list = await http('GET', '/v1/public/me/bookings', { site: site(id), token: session.token, query: { include_past: true, limit: 100 } });
      // BACKEND GAP: a customer's booking says neither who serves it nor which services (by id), the table type or the review already given, which
      // the screens need to name the staff member, offer a move with the same person, and show the rating. The local site reads them from the admin API.
      const tok = await owner(id);
      const [details, reviews] = await Promise.all([
        Promise.all(list.map((x) => http('GET', `/v1/admin/bookings/${x.id}`, { token: tok }).catch(() => null))),
        http('GET', '/v1/admin/reviews', { token: tok, query: { limit: 200 } }).catch(() => []),
      ]);
      return list.map((x, i) => {
        const d = details[i] || {};
        const r = reviews.find((y) => y.booking_id === x.id);
        return {
          ...x, ref: ref(x.id), service_ids: d.service_ids || [], table_type: d.table_type || 'normal',
          allocations: (d.resource_ids || []).map((rid) => ({ rid, s: Date.parse(x.start_at), e: Date.parse(x.end_at) })),
          review: r ? { rating: r.rating, comment: r.comment } : null, vehicle: null, address: null, hold_expires_at: d.hold_expires_at || null,
          service_names: x.service_names && x.service_names.length ? x.service_names : (d.service_ids || []).map((s) => svcName(b, s)).filter(Boolean),
        };
      }).sort((p, q) => Date.parse(p.start_at) - Date.parse(q.start_at));
    },
    async cancel(id, bookingId, session) { return http('POST', `/v1/public/bookings/${bookingId}/cancel`, { site: site(id), token: session.token }); },
    /** Free times to move a booking to (the booking's own time still counts as taken, so it is left out). */
    async moveOptions(id, bookingId, date, anyStaff) {
      const b = snap(id);
      const d = await http('GET', `/v1/admin/bookings/${bookingId}`, { token: await owner(id) });
      const staff = (d.resource_ids || []).find((rid) => (b.resources.find((r) => r.id === rid) || {}).type === 'staff');
      const q = { service_ids: d.service_ids, party_size: d.party_size, table_type: d.table_type || 'normal', allow_separate_seating: d.allow_separate_seating, resource_id: anyStaff || b.type === 'restaurant' || b.type === 'automotive' ? null : staff || null };
      const pin = d.visit_address; // (an on-site visit: its free times are looked up where it goes)
      if (pin && pin.latitude != null) Object.assign(q, { latitude: pin.latitude, longitude: pin.longitude });
      else if (b.type === 'professional' && d.address_id) return []; // (an address without a map pin: no free times can be worked out)
      const res = await api.availability(id, date, q);
      return res.slots.filter((x) => x.start_at.slice(0, 16) !== new Date(d.start_at).toISOString().slice(0, 16));
    },
    async reschedule(id, bookingId, body, session) {
      const out = { start_at: body.start_at, any_available: !!body.any_available, resource_id: body.resource_id || undefined, accept_minimum_spend: true };
      if (session) return http('POST', `/v1/public/bookings/${bookingId}/reschedule`, { site: site(id), token: session.token, body: out });
      return http('POST', `/v1/admin/bookings/${bookingId}/reschedule`, { token: await owner(id), body: out });
    },
    async review(id, bookingId, body, session) {
      return http('POST', `/v1/public/bookings/${bookingId}/review`, { site: site(id), token: session.token, body: { rating: body.rating, comment: (body.comment || '').trim() || undefined } });
    },
    async updateGender(id, session, gender) { return http('PATCH', '/v1/public/me/profile', { site: site(id), token: session.token, body: { gender: gender || null } }); },
    async addAddress(id, session, text) { return http('POST', '/v1/public/me/addresses', { site: site(id), token: session.token, body: { line1: text } }); },
    async removeSaved(id, session, kind, rowId) { return http('DELETE', `/v1/public/me/${kind === 'address' ? 'addresses' : 'vehicles'}/${rowId}`, { site: site(id), token: session.token }); },
    async dataRequest(id, session, kind) { return http('POST', '/v1/public/me/data-requests', { site: site(id), token: session.token, body: { kind } }); },

    /* ---------------- the business admin */
    async adminBookings(id, date, viewer) {
      const b = snap(id);
      const list = await http('GET', '/v1/admin/bookings', { token: await viewerToken(id, viewer), query: { date, limit: 500 } });
      return list.map((x) => adminRow(b, x)).sort((p, q) => Date.parse(p.start_at) - Date.parse(q.start_at));
    },
    async attention(id, viewer) {
      const tok = await viewerToken(id, viewer), now = Date.now();
      const [pending, flagged, conflicts] = await Promise.all([
        http('GET', '/v1/admin/bookings', { token: tok, query: { status: 'pending', limit: 500 } }),
        http('GET', '/v1/admin/bookings', { token: tok, query: { flagged: true, limit: 500 } }),
        http('GET', '/v1/admin/bookings', { token: tok, query: { schedule_conflict: true, limit: 500 } }),
      ]);
      const pick = (x) => ({ id: x.id, date: T.key(x.start_at) });
      return {
        pending: pending.filter((x) => x.hold_expires_at && Date.parse(x.hold_expires_at) > now).map(pick),
        flagged: flagged.filter((x) => ['pending', 'confirmed'].includes(x.status) && Date.parse(x.end_at) > now).map(pick),
        conflicts: conflicts.map(pick),
      };
    },
    async adminBooking(id, bookingId, viewer) {
      const b = snap(id), v = viewer || JS.adminViewer && JS.adminViewer();
      const tok = await viewerToken(id, v);
      const [d, hist] = await Promise.all([http('GET', `/v1/admin/bookings/${bookingId}`, { token: tok }), http('GET', `/v1/admin/bookings/${bookingId}/history`, { token: tok })]);
      const parts = b.type === 'automotive' ? (await http('GET', `/v1/admin/bookings/${bookingId}/parts`, { token: tok }).catch(() => ({ parts: [] }))).parts : [];
      const history = hist.map((h) => ({ from: h.from_status, to: h.to_status, actor: h.actor_type, who: h.actor_type === 'staff' ? (h.actor_id || '').replace(/-1$/, '').replace(/^staff-.*/, 'staff') : null, at: h.changed_at }));
      return { ...adminRow(b, d), health_notes: d.health_notes, history, created_at: hist.length ? hist[0].changed_at : d.start_at, parts: parts.map((p) => ({ ...p, unit_price: Number(p.unit_price) })) };
    },
    async transition(id, bookingId, to, viewer) {
      await http('PATCH', `/v1/admin/bookings/${bookingId}/status`, { token: await viewerToken(id, viewer), body: { status: to } });
      return api.adminBooking(id, bookingId, viewer);
    },
    async clearFlags(id, bookingId) { await http('POST', `/v1/admin/bookings/${bookingId}/clear-flags`, { token: await owner(id) }); return api.adminBooking(id, bookingId); },
    async resolveConflict(id, bookingId) { await http('POST', `/v1/admin/bookings/${bookingId}/resolve-conflict`, { token: await owner(id) }); return api.adminBooking(id, bookingId); },
    async resume(id, bookingId, startAt) { await http('POST', `/v1/admin/bookings/${bookingId}/resume`, { token: await owner(id), body: { start_at: startAt } }); return api.adminBooking(id, bookingId); },
    async addToTicket(id, bookingId, addonId) {
      const r = await http('POST', `/v1/admin/bookings/${bookingId}/items`, { token: await owner(id), body: { service_ids: [addonId] } });
      return { booking: await api.adminBooking(id, bookingId), extended: r.extended, added_minutes: r.added_minutes, conflicts: r.conflicts.map((c) => ({ ...c, booking_id: c.booking_id })) };
    },

    /* ---------------- set-up, plan and website */
    async planUsage(id) {
      const p = await http('GET', '/v1/admin/plan', { token: await owner(id) });
      return { plan: p.plan.code, limits: p.plan.code === 'free' ? p.plan.limits : null, usage: p.usage };
    },
    async addService(id, body) {
      const b = snap(id);
      const staff = b.resources.filter((r) => r.type === 'staff').map((r) => r.id);
      const bays = b.type === 'automotive' ? b.resources.filter((r) => r.type === 'bay').map((r) => r.id) : [];
      await http('POST', '/v1/admin/services', { token: await owner(id), body: {
        name: body.name, duration_minutes: body.duration_minutes, price: body.price || 0, price_after_inspection: !!body.price_after_inspection, location_type: body.location_type || 'in_office',
        travel_buffer_minutes: body.location_type === 'on_site' ? body.travel_buffer_minutes ?? 30 : 0, description: body.description || undefined,
        is_addon: !!body.is_addon, resource_ids: [...staff, ...bays],
      } });
      return (await loadBusiness(id)).services;
    },
    async addProduct(id, body) {
      await http('POST', '/v1/admin/products', { token: await owner(id), body: { name: body.name, price: body.price, stock_qty: body.stock_qty, stock_status: body.stock_qty > 0 ? 'in_stock' : 'out_of_stock' } });
      return (await loadBusiness(id)).products;
    },
    async addVehicle(id, body) {
      await http('POST', '/v1/admin/supported-vehicles', { token: await owner(id), body });
      return (await loadBusiness(id)).supportedVehicles;
    },
    async removeVehicle(id, vehicleId) {
      await http('DELETE', `/v1/admin/supported-vehicles/${vehicleId}`, { token: await owner(id) });
      return (await loadBusiness(id)).supportedVehicles;
    },
    async addResource(id, body) {
      const b = snap(id), tok = await owner(id);
      const made = await http('POST', '/v1/admin/resources', { token: tok, body: { name: body.name, type: body.type, capacity: body.type === 'table' ? body.capacity : undefined } });
      if (body.type === 'staff' && b.hours) await workTheOpeningHours(tok, b, made.id);
      if (body.type !== 'table') { // the screens have no "who does what" yet: a new person or bay can do every service
        for (const s of b.services) {
          const ids = b.resources.filter((r) => r.type === 'staff' || r.type === 'bay').map((r) => r.id).concat(made.id);
          await http('PATCH', `/v1/admin/services/${s.id}`, { token: tok, body: { resource_ids: ids } });
        }
      }
      return (await loadBusiness(id)).resources;
    },
    async setResourceStatus(id, rid, status) {
      const r = await http('PATCH', `/v1/admin/resources/${rid}`, { token: await owner(id), body: { status } });
      const flagged = Number((r._headers && r._headers.get('X-Bookings-Flagged')) || 0);
      return { resources: (await loadBusiness(id)).resources, flagged };
    },
    async setHours(id, hours) {
      const body = hours.map((h, d) => (h ? { weekday: d, open_time: hhmm(h[0]), close_time: hhmm(h[1] % 1440) } : null)).filter(Boolean);
      const tok = await owner(id), b = snap(id);
      await http('PUT', '/v1/admin/operating-hours', { token: tok, body });
      await http('PUT', `/dev/businesses/${id}`, { body: { hours } });
      cfg(id).hours = hours; b.hours = hours;
      for (const r of b.resources.filter((x) => x.type === 'staff')) { // staff added before there were hours have no shifts yet
        const shifts = await http('GET', `/v1/admin/resources/${r.id}/shifts`, { token: tok });
        if (!shifts.length) await workTheOpeningHours(tok, b, r.id);
      }
      await loadBusiness(id);
      return hours;
    },
    /** PUT /admin/business-location with a Google Maps link: the backend reads the point (following a share link to Google) and keeps the link for directions. */
    async setLocationFromLink(id, url, radiusKm) {
      const saved = await http('PUT', '/v1/admin/business-location', { token: await owner(id), body: { maps_url: url, service_radius_km: radiusKm } });
      await loadBusiness(id);
      return saved;
    },
    async setPolicy(id, p) {
      const made = await http('POST', '/v1/admin/policies', { token: await owner(id), body: {
        name: 'Rules from ' + new Date().toISOString().slice(0, 16).replace('T', ' '), make_default: true, default_status: p.default_status,
        min_notice_booking: p.min_notice_min * 60, min_notice_cancel: p.cancel_notice_h * 3600, booking_window: p.window_days * 86400, pending_timeout: p.pending_timeout_min * 60,
      } });
      await http('PUT', `/dev/businesses/${id}`, { body: { default_policy_id: made.id } });
      cfg(id).default_policy_id = made.id;
      return (await loadBusiness(id)).policy;
    },
    async site(id) {
      const s = await http('GET', '/v1/admin/site', { token: await owner(id) });
      const origins = s.allowed_origins.length ? s.allowed_origins : s.domain ? [`https://${s.domain}`, `https://www.${s.domain}`] : [];
      return { domain: s.domain, allowed_origins: origins, origins_follow_domain: s.origins_follow_domain, domain_changed_at: s.domain_changed_at, next_allowed_at: s.domain_change.next_allowed_at, every_days: s.domain_change.every_days };
    },
    async changeSite(id, raw) {
      await http('PUT', '/v1/admin/site', { token: await owner(id), body: { domain: String(raw || '').trim() } });
      return api.site(id);
    },
    hostOf(raw) { // (only to preview the address on the sign-up page; the backend decides)
      let t = String(raw || '').trim().toLowerCase();
      if (!t) throw new ApiError(422, 'validation_failed', 'The website address is empty');
      t = t.replace(/^https?:\/\//, '').split(/[/?#]/)[0].replace(/\.$/, '');
      if (t.includes(':') || /^\d+(\.\d+){3}$/.test(t) || !t.includes('.') || !t.split('.').every((l) => /^(?!-)[a-z0-9-]{1,63}(?<!-)$/.test(l))) throw new ApiError(422, 'validation_failed', "Give the website's public name, such as shop.example");
      return t.startsWith('www.') && t.split('.').length > 2 ? t.slice(4) : t;
    },
    async keys(id) { return http('GET', '/v1/auth/api-keys', { token: await owner(id) }); },
    async recoverKeys(id) {
      const made = await http('POST', '/v1/auth/api-keys/recover', { token: await owner(id), body: {} });
      // the free plan may switch the oldest keys off, so the local stand-in for the WordPress plugin keeps the new ones
      await http('PUT', `/dev/businesses/${id}`, { body: { publishable_key: made.publishable_key, secret_key: made.secret_key } });
      cfg(id).publishable_key = made.publishable_key;
      for (const k of Object.keys(TOKENS)) if (k.startsWith(id + '|')) delete TOKENS[k];
      return { publishable_key: made.publishable_key, secret_key: made.secret_key, revoked: made.revoked };
    },

    /* ---------------- self-service sign-up */
    async signupStart(body) {
      if (!body.accepted_terms) throw new ApiError(422, 'validation_failed', 'Some details need fixing', { fields: { accepted_terms: 'Tick the box to accept the terms' } });
      const email = normEmail(body.owner_email);
      api._signupAsked = { email, after: lastMsg };
      const terms = (await http('GET', '/v1/signup/config')).terms_version; // (the page sends the version of the terms it showed)
      const out = { ...body, owner_email: body.owner_email.trim(), accepted_terms_version: terms };
      delete out.accepted_terms;
      for (const k of Object.keys(out)) if (out[k] === null || out[k] === '') delete out[k];
      try { await http('POST', '/v1/signup', { body: out }); } catch (e) {
        if (e.error === 'disposable_email') throw new ApiError(422, 'validation_failed', e.detail, { fields: { owner_email: e.detail } });
        throw e;
      }
      return { status: 'sent' };
    },
    async signupVerify() {
      const asked = api._signupAsked || {};
      const token = await tokenFromInbox(asked.email, 'sign-up', asked.after || 0);
      if (!token) throw new ApiError(401, 'unauthorized', 'Invalid or expired confirmation link');
      const made = await http('POST', '/v1/signup/verify', { body: { token } });
      const conf = await http('GET', '/dev/config');
      const s = JS.state.signup;
      await http('POST', '/dev/businesses', { body: { tenant_id: made.tenant_id, name: s.name, business_type: s.type, publishable_key: made.publishable_key, secret_key: made.secret_key } });
      CFG = await http('GET', '/dev/config');
      const mine = CFG.businesses.find((x) => x.tenant_id === made.tenant_id);
      mine.domain = api.hostOf(s.domain);
      // a new business has the backend's default rules until its owner sets them
      await loadBusiness(mine.id);
      void conf;
      return { tenant_id: mine.id, publishable_key: made.publishable_key, secret_key: made.secret_key, domain: mine.domain, plan: 'free' };
    },
  };
  /** A staff member works the opening hours (no break) until their own shifts are set; without shifts nobody is ever free. */
  async function workTheOpeningHours(tok, b, rid) {
    const shifts = b.hours.map((h, d) => (h ? { weekday: d, start_time: hhmm(h[0]), end_time: hhmm(h[1]) } : null)).filter(Boolean);
    await http('PUT', `/v1/admin/resources/${rid}/shifts`, { token: tok, body: shifts });
  }
  function hhmm(m) { return `${pad(Math.floor(m / 60) % 24)}:${pad(m % 60)}`; }

  JS.api = api;
})();
