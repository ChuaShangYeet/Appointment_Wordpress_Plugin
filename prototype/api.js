/* A mock of the booking backend, in the browser. Each function answers what the matching endpoint of the real API would
   (see the README's endpoint table), after a short delay, and fails the way it would: an ApiError with the HTTP status, the
   error code and a message (the backend's {"error", "detail", ...} shape). Nothing leaves the page; Reset starts over. */
window.JS = window.JS || {};
(function () {
  const OFFSET = '+08:00';
  const TZ = 'Asia/Kuala_Lumpur';
  const MIN = 60000, HOUR = 3600000, DAY = 86400000;
  const pad = (n) => String(n).padStart(2, '0');
  const partsFmt = new Intl.DateTimeFormat('en-GB', { timeZone: TZ, year: 'numeric', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', hourCycle: 'h23' });
  function kl(d) { const o = {}; for (const p of partsFmt.formatToParts(d)) o[p.type] = p.value; return o; }

  /* ------------------------------------------------------------------ time, always on the business's clock */
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

  function rng(seed) {
    let h = 1779033703 ^ seed.length;
    for (let i = 0; i < seed.length; i++) { h = Math.imul(h ^ seed.charCodeAt(i), 3432918353); h = (h << 13) | (h >>> 19); }
    let a = h >>> 0;
    return () => { a |= 0; a = (a + 0x6d2b79f5) | 0; let t = Math.imul(a ^ (a >>> 15), 1 | a); t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t; return ((t ^ (t >>> 14)) >>> 0) / 4294967296; };
  }
  const clone = (x) => (x === undefined ? undefined : JSON.parse(JSON.stringify(x)));
  const wait = (ms) => new Promise((r) => setTimeout(r, ms));
  const iso = (ms) => new Date(ms).toISOString();

  /* ------------------------------------------------------------------ phone numbers and emails (backend: app/core/phone.py) */
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
  const emailOk = (e) => /^[^\s@]+@[^\s@]+\.[^\s@]{2,}$/.test(String(e || '').trim());
  const normEmail = (e) => String(e || '').trim().toLowerCase();
  JS.fmt = { prettyPhone, maskPhone, normPhone };

  /* ------------------------------------------------------------------ state */
  let DB;
  const LIVE = ['confirmed', 'in-progress', 'waiting-for-parts'];

  function biz(id) {
    const b = DB.businesses.find((x) => x.id === id);
    if (!b) throw new ApiError(404, 'not_found', 'Business not found');
    return b;
  }
  const res = (b, id) => b.resources.find((r) => r.id === id);
  const svc = (b, id) => b.services.find((s) => s.id === id);
  const staffOf = (b) => b.resources.filter((r) => r.type === 'staff' && r.status !== 'inactive');
  const qualifiedStaff = (b, s) => (s.staff && s.staff.length ? s.staff : staffOf(b).map((r) => r.id));
  const qualifiedBays = (b, s) => (s.bays && s.bays.length ? s.bays : b.resources.filter((r) => r.type === 'bay').map((r) => r.id));

  function dayInfo(b, key) {
    const c = (b.closedDates || []).find((x) => x.date === key);
    if (c) return { closed: c.name };
    const h = b.hours && b.hours[T.weekday(key)];
    if (!h) return { closed: 'Closed' };
    return { open: h[0], close: h[1] };
  }
  function windowsFor(b, r, key, info) {
    const o = T.at(key, info.open).getTime(), c = T.at(key, info.close).getTime();
    if (r.type !== 'staff') return [[o, c]];
    if ((r.off || []).includes(T.weekday(key))) return [];
    if (r.breakAt == null) return [[o, c]];
    const bs = T.at(key, r.breakAt).getTime();
    return [[o, bs], [bs + HOUR, c]];
  }
  function isLive(bk, now) {
    if (bk.status === 'pending') return !bk.hold_expires_at || Date.parse(bk.hold_expires_at) > now;
    return LIVE.includes(bk.status);
  }
  function busyMap(b, rids, excludeId, now) {
    const m = {};
    for (const id of rids) m[id] = [];
    for (const bk of DB.bookings) {
      if (bk.biz !== b.id || bk.id === excludeId || !isLive(bk, now)) continue;
      for (const a of bk.allocations) if (m[a.rid]) m[a.rid].push([a.s, a.e]);
    }
    return m;
  }
  const fits = (ws, s, e) => ws.some((w) => w[0] <= s && e <= w[1]);
  const freeIn = (busy, s, e) => !busy.some((x) => x[0] < e && x[1] > s);
  const load = (busy) => busy.reduce((t, x) => t + (x[1] - x[0]), 0);
  function inBounds(b, s, o) {
    if (o.any) return true;
    if (o.ignoreNotice) return s > o.now;
    return s >= o.now + b.policy.min_notice_min * MIN && s <= o.now + b.policy.window_days * DAY;
  }

  /* ------------------------------------------------------------------ the allocators (backend: app/services/allocators) */
  function planStaff(b, key, info, q, o) {
    const svcs = (q.service_ids || []).map((id) => svc(b, id));
    if (!svcs.length || svcs.some((x) => !x)) return [];
    const base = svcs.filter((s) => !s.is_addon);
    let qual = null;
    for (const s of base) { const ids = qualifiedStaff(b, s); qual = qual ? qual.filter((x) => ids.includes(x)) : ids.slice(); }
    qual = (qual || []).filter((id) => res(b, id) && res(b, id).status !== 'inactive');
    if (q.resource_id) qual = qual.filter((x) => x === q.resource_id);
    const dur = svcs.reduce((t, s) => t + s.duration_minutes, 0);
    const block = svcs.reduce((t, s) => t + s.duration_minutes + s.buffer_minutes, 0);
    const travel = base[0] && base[0].location_type === 'on_site' ? base[0].travel_buffer_minutes || 0 : 0;
    const busy = busyMap(b, qual, o.excludeId, o.now);
    const wins = Object.fromEntries(qual.map((id) => [id, windowsFor(b, res(b, id), key, info)]));
    const out = [];
    for (let m = info.open; m + block <= info.close; m += b.step || 30) {
      const s = T.at(key, m).getTime(), e = s + block * MIN, ps = s - travel * MIN, pe = e + travel * MIN;
      if (!inBounds(b, s, o)) continue;
      const free = qual.filter((id) => fits(wins[id], ps, pe) && freeIn(busy[id], ps, pe));
      if (!free.length) continue;
      const pick = q.resource_id || free.slice().sort((x, y) => load(busy[x]) - load(busy[y]) || res(b, x).name.localeCompare(res(b, y).name))[0];
      out.push({ start: s, end: s + dur * MIN, resource_ids: free, allocations: [{ rid: pick, s: ps, e: pe }], travel_minutes: travel });
    }
    return out;
  }

  function seat(tables, party, separate) {
    const sorted = tables.slice().sort((a, c) => a.capacity - c.capacity || a.name.localeCompare(c.name));
    const one = sorted.find((t) => t.capacity >= party);
    if (one) return [one];
    if (!separate) return null;
    let best = null, bestTotal = 0;
    (function rec(i, pick, total) {
      if (total >= party) {
        if (!best || pick.length < best.length || (pick.length === best.length && total < bestTotal)) { best = pick.slice(); bestTotal = total; }
        return;
      }
      if (i >= sorted.length || pick.length >= 6 || (best && pick.length >= best.length)) return;
      pick.push(sorted[i]); rec(i + 1, pick, total + sorted[i].capacity); pick.pop(); rec(i + 1, pick, total);
    })(0, [], 0);
    return best;
  }
  function planRestaurant(b, key, info, q, o) {
    const st = b.slotSettings;
    const vip = q.table_type === 'vip';
    const tables = b.resources.filter((r) => r.type === 'table' && r.status !== 'inactive' && !!(r.attributes || {}).is_vip === vip);
    const busy = busyMap(b, tables.map((t) => t.id), o.excludeId, o.now);
    const out = [];
    for (let m = st.first; m <= st.last; m += st.interval) {
      if (m < info.open || m + st.duration > info.close) continue;
      const s = T.at(key, m).getTime(), e = s + (st.duration + st.buffer) * MIN;
      if (!inBounds(b, s, o)) continue;
      const free = tables.filter((t) => freeIn(busy[t.id], s, e) && (!q.resource_id || t.id === q.resource_id));
      const chosen = seat(free, q.party_size || 2, !!q.allow_separate_seating);
      if (!chosen) continue;
      const spend = chosen.reduce((t, x) => t + ((x.attributes || {}).minimum_spend || 0), 0);
      out.push({ start: s, end: s + st.duration * MIN, resource_ids: chosen.map((t) => t.id), combined: chosen.length > 1, minimum_spend: spend || null, allocations: chosen.map((t) => ({ rid: t.id, s, e })) });
    }
    return out;
  }

  function planWorkshop(b, key, info, q, o) {
    const svcs = (q.service_ids || []).map((id) => svc(b, id)).filter(Boolean);
    if (!svcs.length) return [];
    const mechs = [...new Set(svcs.flatMap((s) => qualifiedStaff(b, s)))].filter((id) => res(b, id) && (!q.resource_id || id === q.resource_id));
    const bays = [...new Set(svcs.flatMap((s) => qualifiedBays(b, s)))];
    const all = [...mechs, ...bays];
    const busy = busyMap(b, all, o.excludeId, o.now);
    const wins = Object.fromEntries(all.map((id) => [id, windowsFor(b, res(b, id), key, info)]));
    const block = svcs.reduce((t, s) => t + s.duration_minutes + s.buffer_minutes, 0);
    const out = [];
    for (let m = info.open; m + block <= info.close; m += b.step || 30) {
      const s0 = T.at(key, m).getTime();
      if (!inBounds(b, s0, o)) continue;
      let cur = s0, prevM = null, prevB = null, ok = true;
      const allocs = [];
      for (const sv of svcs) {
        const e = cur + (sv.duration_minutes + sv.buffer_minutes) * MIN;
        const ms = qualifiedStaff(b, sv).filter((id) => mechs.includes(id) && fits(wins[id], cur, e) && freeIn(busy[id], cur, e));
        const bs = qualifiedBays(b, sv).filter((id) => wins[id] && fits(wins[id], cur, e) && freeIn(busy[id], cur, e));
        if (!ms.length || !bs.length) { ok = false; break; }
        const mk = ms.includes(prevM) ? prevM : ms.sort((x, y) => load(busy[x]) - load(busy[y]))[0];
        const bk = bs.includes(prevB) ? prevB : bs[0];
        allocs.push({ rid: mk, s: cur, e, service_id: sv.id }, { rid: bk, s: cur, e, service_id: sv.id });
        prevM = mk; prevB = bk; cur = e;
      }
      if (!ok) continue;
      out.push({ start: s0, end: cur, resource_ids: [...new Set(allocs.map((a) => a.rid))], allocations: allocs });
    }
    return out;
  }

  function plan(b, key, q, o) {
    const info = dayInfo(b, key);
    if (info.closed) return [];
    if (b.type === 'restaurant') return b.slotSettings ? planRestaurant(b, key, info, q, o) : [];
    if (b.type === 'automotive') return planWorkshop(b, key, info, q, o);
    return planStaff(b, key, info, q, o);
  }

  const slotOut = (p) => ({ start_at: iso(p.start), end_at: iso(p.end), resource_ids: p.resource_ids, combined: !!p.combined, minimum_spend: p.minimum_spend || null, travel_minutes: p.travel_minutes || 0 });

  function leadTimeNotice(b, serviceIds) {
    const out = [];
    for (const id of serviceIds || []) for (const p of (svc(b, id) || {}).parts || []) out.push(clone(p));
    return out;
  }

  /* ------------------------------------------------------------------ bookings */
  function newRef(r) {
    const abc = 'ABCDEFGHJKLMNPQRSTUVWXYZ23456789';
    let s = '';
    for (let i = 0; i < 6; i++) s += abc[Math.floor((r ? r() : Math.random()) * abc.length)];
    return s;
  }
  function makeBooking(b, p, fields) {
    DB.seq += 1;
    const bk = {
      id: 'bk-' + DB.seq, ref: newRef(fields.r), biz: b.id, status: fields.status || 'confirmed',
      start_at: iso(p.start), end_at: iso(p.end), allocations: p.allocations.map((a) => ({ ...a })),
      service_ids: fields.service_ids || [], party_size: fields.party_size || null, table_type: fields.table_type || null,
      allow_separate_seating: fields.allow_separate_seating || null, minimum_spend_agreed: p.minimum_spend || null,
      customer: fields.customer, contact_email: fields.contact_email || null, special_request: fields.special_request || null,
      health_notes: fields.health_notes || null, vehicle: fields.vehicle || null, address: fields.address || null,
      lead_time_acknowledged_at: fields.lead_time_acknowledged_at || null, hold_expires_at: fields.hold_expires_at || null,
      flags: fields.flags || [], schedule_conflict: false, source: fields.source || 'headless',
      created_at: fields.created_at || iso(Date.now()), review: fields.review || null, items: fields.items || null,
      history: [{ from: null, to: fields.status || 'confirmed', actor: fields.actor || 'customer', at: fields.created_at || iso(Date.now()) }],
    };
    DB.bookings.push(bk);
    return bk;
  }
  const deadline = (b, bk) => (bk.status === 'confirmed' ? Date.parse(bk.start_at) - b.policy.cancel_notice_h * HOUR : null);
  function customerView(b, bk, now) {
    const dl = deadline(b, bk);
    const can = bk.status === 'pending' || (dl != null && now <= dl);
    return {
      ...clone(bk), service_names: bk.service_ids.map((id) => (svc(b, id) || {}).name).filter(Boolean),
      can_cancel: can, can_reschedule: can, cancel_deadline: dl ? iso(dl) : null,
    };
  }

  /* ------------------------------------------------------------------ the sample data */
  const ADDRESSES = [
    ['15 Jalan SS 21/37, Damansara Utama, 47400 Petaling Jaya', 4.2], ['8 Jalan Bangsar Utama 1, 59000 Kuala Lumpur', 9.6],
    ['22 Jalan USJ 9/5Q, 47620 Subang Jaya', 10.4], ['3 Jalan Puchong Prima 4/2, 47150 Puchong', 12.9], ['41 Jalan 14/22, 46100 Petaling Jaya', 2.8],
  ];
  const PARTS = {
    'aut-s1': [['Engine oil 5W-30 (4 L)', 1, 120, 18], ['Oil filter', 1, 25, 40]],
    'aut-s2': [['Front brake pads (OEM)', 1, 140, 0]],
    'aut-s3': [['Cabin air filter', 1, 35, 6]],
  };

  function randomQuery(b, r) {
    if (b.type === 'restaurant') return { party_size: 1 + Math.floor(r() * 6), table_type: r() < 0.12 ? 'vip' : 'normal' };
    const bases = b.services.filter((s) => !s.is_addon);
    if (!bases.length) return null;
    if (b.type === 'automotive') {
      const first = bases[Math.floor(r() * bases.length)];
      const ids = [first.id];
      if (r() < 0.3) { const second = bases[Math.floor(r() * bases.length)]; if (second.id !== first.id) ids.push(second.id); }
      return { service_ids: ids };
    }
    const ids = [bases[Math.floor(r() * bases.length)].id];
    if (b.type === 'salon' && r() < 0.4) { const adds = b.services.filter((s) => s.is_addon); ids.push(adds[Math.floor(r() * adds.length)].id); }
    return { service_ids: ids };
  }

  function seedBusiness(b) {
    const today = T.today();
    const now = Date.now();
    const r = rng(b.id + today);
    b.closedDates = (b.closed || []).map((c) => ({ date: T.addDays(today, c.offset), name: c.name }));
    for (const x of b.resources) x.status = x.status || 'active';
    for (let d = -12; d <= 13; d++) {
      const key = T.addDays(today, d);
      if (dayInfo(b, key).closed) continue;
      const n = b.type === 'restaurant' ? 6 + Math.floor(r() * 6) : b.type === 'professional' ? 2 + Math.floor(r() * 2) : 3 + Math.floor(r() * 4);
      for (let i = 0; i < n; i++) {
        const q = randomQuery(b, r);
        if (!q) continue;
        const slots = plan(b, key, q, { now, any: true });
        if (!slots.length) continue;
        const p = slots[Math.floor(r() * slots.length)];
        const name = JS.FIRST_NAMES[Math.floor(r() * JS.FIRST_NAMES.length)];
        let status = 'confirmed';
        let hold = null;
        if (p.end < now) { const x = r(); status = x < 0.84 ? 'completed' : x < 0.92 ? 'cancelled' : 'no-show'; }
        else if (p.start <= now) status = ['salon', 'automotive', 'professional'].includes(b.type) ? 'in-progress' : 'confirmed';
        else if (b.policy.default_status === 'pending' && d <= 2 && r() < 0.15) { status = 'pending'; hold = iso(now + (6 + Math.floor(r() * 22)) * MIN); }
        const fields = {
          r, status, hold_expires_at: hold, service_ids: q.service_ids, party_size: q.party_size, table_type: q.table_type,
          customer: { name, phone: '+601' + (2 + Math.floor(r() * 8)) + String(Math.floor(r() * 1e7)).padStart(7, '0'), email: null, gender: null },
          created_at: iso(Math.min(p.start - (2 + Math.floor(r() * 9)) * DAY, now - (status === 'pending' ? 10 : 600) * MIN)), source: r() < 0.7 ? 'wordpress' : 'headless',
        };
        if (status === 'pending') fields.created_at = iso(now - 8 * MIN);
        if (b.type === 'automotive') fields.vehicle = { plate: 'W' + 'ABCDEFGHJKLMNPQRSTUVWXY'[Math.floor(r() * 23)] + 'X ' + (1000 + Math.floor(r() * 8999)), model: ['Perodua Myvi', 'Proton Saga', 'Honda City', 'Toyota Vios'][Math.floor(r() * 4)], year: 2014 + Math.floor(r() * 11) };
        if (b.type === 'professional' && svc(b, q.service_ids[0]).location_type === 'on_site') { const a = ADDRESSES[Math.floor(r() * ADDRESSES.length)]; fields.address = { line1: a[0], distance_km: a[1] }; }
        if (b.type === 'spa' && r() < 0.25) fields.health_notes = ['Lower back pain since August.', 'Pregnant, 20 weeks. Please avoid pressure points on the ankles.', 'Allergic to nut oils.'][Math.floor(r() * 3)];
        if (b.type === 'restaurant' && r() < 0.3) fields.special_request = ['Birthday dinner, a candle on the dessert please.', 'One guest is vegetarian.', 'High chair for a toddler.'][Math.floor(r() * 3)];
        const bk = makeBooking(b, p, fields);
        if (status === 'in-progress' || status === 'completed' || status === 'cancelled' || status === 'no-show') bk.history.push({ from: 'confirmed', to: status, actor: 'staff', at: iso(Math.min(now, p.start + (status === 'completed' ? (p.end - p.start) : 0))) });
      }
    }
    // an appointment running right now (during opening hours), so the "add an extra" and "waiting for parts" flows can be tried; for the salon the
    // stylist's next client is booked straight after it, so an extra runs into them
    const info = dayInfo(b, today);
    const nowMin = T.minutes(now);
    if (['salon', 'automotive', 'professional'].includes(b.type) && !info.closed && nowMin >= info.open + 20 && nowMin <= info.close - 90) {
      const startMin = Math.floor((nowMin - 15) / 30) * 30;
      const first = b.services.find((s) => !s.is_addon && s.location_type !== 'in_office');
      const q = { service_ids: b.type === 'automotive' ? ['aut-s2', 'aut-s1'] : [first.id] };
      const running = plan(b, today, q, { now, any: true }).filter((x) => x.start <= now && x.allocations[0].e > now + 10 * MIN);
      // the salon wants one whose stylist is free to take a client straight afterwards (no break in between), so an extra can run into that client
      const nextFree = (x) => plan(b, today, { service_ids: ['sal-s2'], resource_id: x.allocations[0].rid }, { now, any: true, excludeId: null }).some((y) => y.start === x.allocations[0].e);
      const p = (b.type === 'salon' ? running.find((x) => x.allocations.length && nextFree(x)) : null) || running.find((x) => T.minutes(x.start) === startMin) || running[0];
      if (p) {
        const run = makeBooking(b, p, { r, status: 'in-progress', service_ids: q.service_ids, customer: { name: 'Chloe Wong', phone: '+60162233445' }, created_at: iso(now - 3 * DAY), vehicle: b.type === 'automotive' ? { plate: 'BKT 8821', model: 'Perodua Myvi', year: 2017 } : null, address: b.type === 'professional' ? { line1: ADDRESSES[0][0], distance_km: ADDRESSES[0][1] } : null });
        run.history.push({ from: 'confirmed', to: 'in-progress', actor: 'staff', at: iso(p.start + 5 * MIN) });
        if (b.type === 'salon') {
          const stylist = p.allocations[0].rid;
          const next = plan(b, today, { service_ids: ['sal-s2'], resource_id: stylist }, { now, any: true }).find((x) => x.start === p.allocations[0].e);
          if (next) makeBooking(b, next, { r, status: 'confirmed', service_ids: ['sal-s2'], customer: { name: 'Daniel Lee', phone: '+60123009876' }, created_at: iso(now - 2 * DAY) });
        }
      }
    }
    // a few upcoming bookings a settings change no longer fits (backend: review flags, app/services/flagging.py)
    const future = DB.bookings.filter((x) => x.biz === b.id && x.status === 'confirmed' && Date.parse(x.start_at) > now + DAY);
    const flag = { spa: 'outside_shift', salon: 'day_off', restaurant: 'table_cap_exceeded', automotive: 'staff_not_qualified', professional: 'outside_service_area' }[b.type];
    if (future[2] && flag) future[2].flags = [flag];
    if (future[7] && b.type === 'spa') future[7].flags = ['resource_inactive'];
    seedSampleCustomer(b, now, r);
  }

  function seedSampleCustomer(b, now, r) {
    const me = JS.SAMPLE_CUSTOMER;
    const cust = { name: me.name, phone: me.phone, email: me.email, emailConfirmed: false, gender: null, addresses: [], vehicles: [], requests: [] };
    if (b.type === 'professional') cust.addresses.push({ id: 'ad-1', line1: '15 Jalan SS 21/37, Damansara Utama', city: 'Petaling Jaya', postcode: '47400', distance_km: 4.2 });
    if (b.type === 'automotive') cust.vehicles.push({ id: 've-1', plate_no: 'WVX 2213', supported_vehicle_id: 'sv1', make: 'Perodua', model: 'Myvi', year: 2019 });
    DB.customers[b.id] = { [me.phone]: cust };
    const today = T.today();
    const q = b.type === 'restaurant' ? { party_size: 2, table_type: 'normal' } : { service_ids: [b.services.find((s) => !s.is_addon).id] };
    const extra = (when) => {
      const f = { r, service_ids: q.service_ids, party_size: q.party_size, table_type: q.table_type, customer: { name: me.name, phone: me.phone, email: me.email, gender: null }, contact_email: me.email, created_at: iso(when) };
      if (b.type === 'automotive') f.vehicle = { plate: 'WVX 2213', model: 'Perodua Myvi', year: 2019 };
      if (b.type === 'professional') f.address = { line1: cust.addresses[0].line1 + ', 47400 Petaling Jaya', distance_km: 4.2 };
      return f;
    };
    const pickSlot = (fromDay, toDay, test) => {
      for (let d = fromDay; d <= toDay; d++) {
        const p = plan(b, T.addDays(today, d), q, { now, any: true }).find(test);
        if (p) return p;
      }
      return null;
    };
    const cancelH = b.policy.cancel_notice_h;
    const soon = pickSlot(0, 2, (p) => p.start > now + 3 * HOUR && p.start < now + (cancelH - 1) * HOUR);
    if (soon) makeBooking(b, soon, { ...extra(now - 3 * DAY), status: 'confirmed' });
    const later = pickSlot(4, 9, (p) => T.minutes(p.start) >= 660);
    if (later) makeBooking(b, later, { ...extra(now - DAY), status: 'confirmed' });
    const past1 = pickSlot(-9, -6, (p) => p.end < now);
    if (past1) { const x = makeBooking(b, past1, { ...extra(Date.parse(iso(past1.start)) - 5 * DAY), status: 'completed' }); x.history.push({ from: 'confirmed', to: 'completed', actor: 'staff', at: iso(past1.end) }); }
    const past2 = pickSlot(-12, -10, (p) => p.end < now);
    if (past2) { const x = makeBooking(b, past2, { ...extra(Date.parse(iso(past2.start)) - 5 * DAY), status: 'completed', review: { rating: 5, comment: 'Lovely as always.' } }); x.history.push({ from: 'confirmed', to: 'completed', actor: 'staff', at: iso(past2.end) }); }
  }

  function reset() {
    DB = {
      businesses: clone(JS.BUSINESSES), bookings: [], customers: {}, seq: 1000, otp: {}, links: {}, confirmLinks: {}, takenDone: false,
      usage: { spa: { bookings: 41, whatsapp: 12, sms: 3 }, salon: { bookings: 23, whatsapp: 6, sms: 0 }, professional: { bookings: 47, whatsapp: 17, sms: 4 } },
      sites: {}, keys: {}, signup: null, created: 0,
    };
    const now = Date.now();
    for (const b of DB.businesses) {
      seedBusiness(b);
      DB.sites[b.id] = { domain: b.domain, changed_at: b.id === 'spa' ? iso(now - 12 * DAY) : null };
      DB.keys[b.id] = [
        { id: b.id + '-k1', kind: 'publishable', prefix: 'pk_' + newRef().slice(0, 5).toLowerCase(), created_at: iso(now - 60 * DAY), revoked_at: null },
        { id: b.id + '-k2', kind: 'secret', prefix: 'sk_' + newRef().slice(0, 5).toLowerCase(), created_at: iso(now - 60 * DAY), revoked_at: null },
      ];
    }
  }

  /* ------------------------------------------------------------------ checks shared by booking and moving */
  function slotFor(b, key, q, start, o) {
    const plans = plan(b, key, q, o);
    return { found: plans.find((p) => p.start === start) || null, plans };
  }
  function bookingQuery(bk) {
    return { service_ids: bk.service_ids, party_size: bk.party_size, table_type: bk.table_type || 'normal', allow_separate_seating: bk.allow_separate_seating };
  }
  function staffOfBooking(b, bk) {
    const ids = bk.allocations.map((a) => a.rid).filter((id) => (res(b, id) || {}).type === 'staff');
    return ids[0] || null;
  }

  function geocodeText(b, text) {
    const t = String(text || '').trim();
    if (t.length < 3 || /asdf|qwerty|xyz|nowhere|test/i.test(t)) throw new ApiError(422, 'address_not_found', "We could not find that address; check it, or drop a pin on the map");
    const places = [[/seremban|melaka|ipoh|johor|penang|kuantan|genting/i, 62.4], [/klang/i, 26.8], [/putrajaya|cyberjaya/i, 28.9], [/shah alam/i, 17.6], [/cheras/i, 15.4],
      [/puchong/i, 12.9], [/subang|usj/i, 10.4], [/bangsar|mont kiara|kl sentral|kuala lumpur|\bkl\b/i, 9.6], [/damansara|ttdi|kota damansara/i, 5.1], [/petaling jaya|\bpj\b|ss ?\d/i, 2.9]];
    const hit = places.find(([re]) => re.test(t));
    const km = hit ? hit[1] : 8.6;
    const exact = /\d/.test(t) && t.split(/[\s,]+/).length >= 3;
    const formatted = t.replace(/\s+/g, ' ').replace(/,?\s*malaysia$/i, '') + ', Malaysia';
    const radius = b.location ? b.location.radius_km : 25;
    return { latitude: 3.1, longitude: 101.6, formatted_address: formatted, exact, within_service_area: km <= radius, distance_km: km, service_radius_km: radius };
  }

  function validateBooking(b, body, now) {
    const errors = {};
    if (!String(body.customer?.name || '').trim()) errors.name = 'Enter your name';
    try { body.customer.phone = normPhone(body.customer?.phone); } catch (e) { errors.phone = 'Enter a phone number we can reach you on, such as 012-345 6789'; }
    if (body.customer?.email && !emailOk(body.customer.email)) errors.email = 'That email address looks incomplete';
    if (Object.keys(errors).length) throw new ApiError(422, 'validation_failed', 'Some details need fixing', { fields: errors });
    if (b.type === 'automotive') {
      const v = body.vehicle;
      if (!v) throw new ApiError(422, 'validation_failed', 'Tell us which car this is for');
      const sv = b.supportedVehicles.find((x) => x.id === v.supported_vehicle_id);
      if (!sv) throw new ApiError(422, 'incompatible_vehicle', 'This workshop does not service that vehicle');
      if (v.year && ((sv.year_from && v.year < sv.year_from) || (sv.year_to && v.year > sv.year_to))) throw new ApiError(422, 'incompatible_vehicle', `This workshop services ${sv.make} ${sv.model} only for model years ${sv.year_from || '…'}-${sv.year_to || '…'}`);
      const notice = leadTimeNotice(b, body.service_ids);
      if (notice.length && !body.acknowledge_lead_time) throw new ApiError(422, 'lead_time_not_acknowledged', 'Some parts must be ordered; please acknowledge the estimated lead time', { parts: notice });
    }
    if (b.type === 'professional') {
      const s = svc(b, body.service_ids[0]);
      if (s && s.location_type === 'on_site') {
        if (!body.address) throw new ApiError(422, 'validation_failed', 'An on-site visit needs the address');
        if (!body.address.within_service_area) throw new ApiError(422, 'outside_service_area', `That address is ${body.address.distance_km} km away; this business serves up to ${b.location.radius_km} km`);
      }
    }
  }

  /* ------------------------------------------------------------------ the API */
  const api = {
    reset,
    /** The "someone takes the slot first" scenario fires on the next booking attempt after it is chosen. */
    armScenario() { DB.takenDone = false; },
    T,
    businesses() { return DB.businesses.map((b) => ({ id: b.id, name: b.name, type: b.type, kind: b.kind, plan: b.plan })); },
    business(id) { return clone(biz(id)); },
    staffName(id, rid) { return (res(biz(id), rid) || {}).name || 'Unknown'; },

    async month(id, q, days = 21) {
      await wait(250);
      const b = biz(id), now = Date.now(), today = T.today();
      return Array.from({ length: days }, (_, i) => {
        const key = T.addDays(today, i);
        const info = dayInfo(b, key);
        if (info.closed) return { date: key, available: false, slot_count: 0, closed: info.closed };
        const n = plan(b, key, q, { now }).length;
        return { date: key, available: n > 0, slot_count: n, closed: null };
      });
    },
    async availability(id, date, q) {
      await wait(300);
      const b = biz(id);
      const slots = plan(b, date, q, { now: Date.now() }).map(slotOut);
      return { date, timezone: TZ, slots, lead_time_notice: b.type === 'automotive' ? leadTimeNotice(b, q.service_ids) : [] };
    },
    async geocode(id, text) {
      await wait(600);
      return geocodeText(biz(id), text);
    },

    /** POST /public/bookings */
    async createBooking(id, body, scenario) {
      await wait(700);
      const b = biz(id), now = Date.now();
      validateBooking(b, body, now);
      if (scenario === 'monthly_limit') throw new ApiError(402, 'plan_limit_reached', `${b.name} cannot take more online bookings this month`, { limit: 'max_bookings_per_month' });
      const start = Date.parse(body.start_at);
      const key = T.key(start);
      const q = { service_ids: body.service_ids, resource_id: body.resource_id || null, party_size: body.party_size, table_type: body.table_type || 'normal', allow_separate_seating: body.allow_separate_seating };
      if (start < now + b.policy.min_notice_min * MIN) throw new ApiError(422, 'outside_booking_window', 'Too close to the start time to book');
      if (scenario === 'slot_taken' && !DB.takenDone) {
        // someone else books the very same time a moment earlier: once per time the scenario is chosen, other guests take every table or
        // person that could serve this start, so the visitor has to pick another time
        DB.takenDone = true;
        for (let i = 0; i < 6; i++) {
          const p = slotFor(b, key, q, start, { now }).found;
          if (!p) break;
          makeBooking(b, p, { status: 'confirmed', service_ids: body.service_ids, party_size: body.party_size, table_type: body.table_type, customer: { name: ['Lim Mei Ling', 'Arjun Pillai', 'Grace Teoh'][i % 3], phone: '+6017228814' + i }, created_at: iso(now) });
        }
      }
      const { found, plans } = slotFor(b, key, q, start, { now });
      if (!found) throw new ApiError(409, 'slot_conflict', 'Slot just taken, please pick another time', { slots: plans.map(slotOut) });
      if (found.minimum_spend && !body.accept_minimum_spend) throw new ApiError(422, 'minimum_spend_not_accepted', 'This table has a minimum spend that must be accepted', { minimum_spend: found.minimum_spend });
      const pending = scenario === 'needs_approval' || b.policy.default_status === 'pending';
      const phone = body.customer.phone;
      const book = DB.customers[b.id] || (DB.customers[b.id] = {});
      if (!book[phone]) book[phone] = { name: body.customer.name, phone, email: null, emailConfirmed: false, gender: body.customer.gender || null, addresses: [], vehicles: [], requests: [] };
      const cust = book[phone];
      cust.name = body.customer.name; // (the name follows the latest booking; an email already on file is never replaced)
      if (!cust.gender && body.customer.gender) cust.gender = body.customer.gender;
      const bk = makeBooking(b, found, {
        status: pending ? 'pending' : 'confirmed', hold_expires_at: pending ? iso(now + b.policy.pending_timeout_min * MIN) : null,
        service_ids: body.service_ids, party_size: body.party_size, table_type: b.type === 'restaurant' ? body.table_type : null, allow_separate_seating: body.allow_separate_seating,
        customer: { name: body.customer.name, phone, email: body.customer.email ? normEmail(body.customer.email) : null, gender: body.customer.gender || null },
        contact_email: body.customer.email ? normEmail(body.customer.email) : null, special_request: body.special_request, health_notes: body.health_notes,
        vehicle: body.vehicle ? { plate: body.vehicle.plate_no, model: (() => { const sv = b.supportedVehicles.find((x) => x.id === body.vehicle.supported_vehicle_id); return sv ? `${sv.make} ${sv.model}` : ''; })(), year: body.vehicle.year } : null,
        address: body.address ? { line1: body.address.formatted_address, distance_km: body.address.distance_km } : null,
        lead_time_acknowledged_at: body.acknowledge_lead_time ? iso(now) : null, source: 'headless', created_at: iso(now),
      });
      if (DB.usage[b.id]) DB.usage[b.id].bookings += 1;
      if (body.vehicle && body.vehicle.save !== false && !cust.vehicles.some((v) => v.plate_no === body.vehicle.plate_no)) {
        const sv = b.supportedVehicles.find((x) => x.id === body.vehicle.supported_vehicle_id);
        cust.vehicles.push({ id: 've-' + DB.seq, plate_no: body.vehicle.plate_no, supported_vehicle_id: sv.id, make: sv.make, model: sv.model, year: body.vehicle.year });
      }
      return customerView(b, bk, now);
    },

    /* ---------------- customer sign-in (POST /public/auth/otp, /otp/verify, /magic-link, /magic-link/verify, /email, /email/confirm) */
    async requestOtp(id, rawPhone, scenario) {
      await wait(500);
      const phone = normPhone(rawPhone);
      if (scenario === 'send_fails') throw new ApiError(503, 'delivery_failed', 'We could not send the message; please try again shortly');
      DB.otp[id + phone] = { tries: 0 };
      return { status: 'sent', to: maskPhone(phone), channel: 'WhatsApp' };
    },
    async verifyOtp(id, rawPhone, code) {
      await wait(400);
      const phone = normPhone(rawPhone);
      const rec = DB.otp[id + phone];
      if (!rec) throw new ApiError(401, 'unauthorized', 'Invalid or expired code');
      rec.tries += 1;
      if (rec.tries > 5) throw new ApiError(403, 'forbidden', 'Too many attempts; request a new code');
      if (!/^\d{6}$/.test(code) || code === '000000') throw new ApiError(401, 'unauthorized', 'Invalid or expired code');
      delete DB.otp[id + phone];
      const book = DB.customers[id] || (DB.customers[id] = {});
      if (!book[phone]) book[phone] = { name: '', phone, email: null, emailConfirmed: false, gender: null, addresses: [], vehicles: [], requests: [] };
      return { method: 'phone', phone, limited: false };
    },
    async requestMagicLink(id, rawEmail, scenario) {
      await wait(500);
      const email = normEmail(rawEmail);
      if (!emailOk(email)) throw new ApiError(422, 'validation_failed', 'Enter a complete email address');
      const book = DB.customers[id] || {};
      const confirmed = Object.values(book).find((c) => c.emailConfirmed && c.email === email);
      if (scenario === 'send_fails') { delete DB.links[id + email]; return { status: 'sent' }; } // (a failed send still answers the same)
      if (confirmed) DB.links[id + email] = { phone: confirmed.phone, limited: false };
      else {
        const made = DB.bookings.find((x) => x.biz === id && x.contact_email === email);
        if (made) DB.links[id + email] = { phone: made.customer.phone, limited: true };
        else delete DB.links[id + email];
      }
      return { status: 'sent' };
    },
    async openMagicLink(id, rawEmail) {
      await wait(400);
      const email = normEmail(rawEmail);
      const link = DB.links[id + email];
      if (!link) throw new ApiError(401, 'unauthorized', 'Invalid or expired link');
      delete DB.links[id + email];
      return { method: 'email', phone: link.phone, email, limited: link.limited };
    },
    linkWasSent(id, rawEmail) { return !!DB.links[id + normEmail(rawEmail)]; },
    async requestEmailConfirm(id, session, rawEmail) {
      await wait(500);
      if (session.limited) throw new ApiError(403, 'forbidden', 'Sign in with your phone number to change your email');
      const email = normEmail(rawEmail);
      if (!emailOk(email)) throw new ApiError(422, 'validation_failed', 'Enter a complete email address');
      const cust = DB.customers[id][session.phone];
      if (cust.emailConfirmed && cust.email === email) throw new ApiError(409, 'conflict', 'This email address is already confirmed');
      DB.confirmLinks[id + session.phone] = email;
      return { status: 'sent' };
    },
    async openEmailConfirm(id, session) {
      await wait(400);
      const email = DB.confirmLinks[id + session.phone];
      if (!email) throw new ApiError(401, 'unauthorized', 'Invalid or expired link');
      delete DB.confirmLinks[id + session.phone];
      const cust = DB.customers[id][session.phone];
      cust.email = email; cust.emailConfirmed = true;
      return { status: 'confirmed' };
    },

    /* ---------------- the signed-in customer (GET /public/me/..., cancel, reschedule, review) */
    async me(id, session) {
      await wait(250);
      const cust = (DB.customers[id] || {})[session.phone];
      if (!cust) throw new ApiError(404, 'not_found', 'Customer not found');
      return clone(cust);
    },
    async myBookings(id, session) {
      await wait(300);
      const b = biz(id), now = Date.now();
      return DB.bookings
        .filter((x) => x.biz === id && x.customer.phone === session.phone && (!session.limited || x.contact_email === session.email))
        .sort((x, y) => Date.parse(x.start_at) - Date.parse(y.start_at))
        .map((x) => customerView(b, x, now));
    },
    async cancel(id, bookingId, session) {
      await wait(500);
      const b = biz(id), now = Date.now();
      const bk = DB.bookings.find((x) => x.id === bookingId && x.biz === id && x.customer.phone === session.phone);
      if (!bk) throw new ApiError(404, 'not_found', 'Booking not found');
      if (!['pending', 'confirmed'].includes(bk.status)) throw new ApiError(409, 'invalid_transition', `A booking that is '${bk.status}' can no longer be cancelled online; please contact the business`);
      const dl = deadline(b, bk);
      if (dl != null && now > dl) throw new ApiError(422, 'too_late_to_cancel', "Too late to cancel under this booking's cancellation policy", { cancel_deadline: iso(dl) });
      bk.history.push({ from: bk.status, to: 'cancelled', actor: 'customer', at: iso(now) });
      bk.status = 'cancelled'; bk.hold_expires_at = null; bk.flags = [];
      return customerView(b, bk, now);
    },
    async moveOptions(id, bookingId, date, anyStaff, admin) {
      await wait(300);
      const b = biz(id), now = Date.now();
      const bk = DB.bookings.find((x) => x.id === bookingId);
      const q = { ...bookingQuery(bk), resource_id: anyStaff || b.type === 'restaurant' || b.type === 'automotive' ? null : staffOfBooking(b, bk) };
      return plan(b, date, q, { now, excludeId: bk.id, ignoreNotice: !!admin }).filter((p) => p.start !== Date.parse(bk.start_at)).map(slotOut);
    },
    async reschedule(id, bookingId, body, session) {
      await wait(600);
      const b = biz(id), now = Date.now();
      const bk = DB.bookings.find((x) => x.id === bookingId && x.biz === id);
      if (!bk || (session && bk.customer.phone !== session.phone)) throw new ApiError(404, 'not_found', 'Booking not found');
      if (bk.status === 'waiting-for-parts') throw new ApiError(409, 'invalid_transition', 'A job waiting for parts is rescheduled with Resume');
      if (!['pending', 'confirmed'].includes(bk.status)) throw new ApiError(409, 'invalid_transition', `A booking that is '${bk.status}' can no longer be rescheduled`);
      const dl = deadline(b, bk);
      if (session && dl != null && now > dl) throw new ApiError(422, 'too_late_to_reschedule', "Too late to reschedule under this booking's cancellation policy", { reschedule_deadline: iso(dl) });
      const start = Date.parse(body.start_at);
      const q = { ...bookingQuery(bk), resource_id: body.any_available || b.type === 'restaurant' || b.type === 'automotive' ? null : body.resource_id || staffOfBooking(b, bk) };
      const { found, plans } = slotFor(b, T.key(start), q, start, { now, excludeId: bk.id, ignoreNotice: !session });
      if (!found) throw new ApiError(409, 'slot_unavailable', 'That time is not available', { slots: plans.map(slotOut) });
      const before = bk.start_at;
      bk.start_at = iso(found.start); bk.end_at = iso(found.end); bk.allocations = found.allocations.map((a) => ({ ...a })); bk.flags = [];
      bk.history.push({ moved: true, from_time: before, to_time: bk.start_at, actor: session ? 'customer' : 'staff', at: iso(now) });
      return customerView(b, bk, now);
    },
    async review(id, bookingId, body, session) {
      await wait(500);
      const bk = DB.bookings.find((x) => x.id === bookingId && x.biz === id && x.customer.phone === session.phone);
      if (!bk) throw new ApiError(404, 'not_found', 'Booking not found');
      if (bk.status !== 'completed') throw new ApiError(409, 'conflict', 'Only a completed appointment can be reviewed');
      if (bk.review) throw new ApiError(409, 'conflict', 'You have already reviewed this appointment');
      if (!(body.rating >= 1 && body.rating <= 5)) throw new ApiError(422, 'validation_failed', 'Choose a rating from 1 to 5 stars');
      bk.review = { rating: body.rating, comment: (body.comment || '').trim() || null };
      return clone(bk.review);
    },
    async updateGender(id, session, gender) {
      await wait(300);
      DB.customers[id][session.phone].gender = gender || null;
      return { gender: gender || null };
    },
    async addAddress(id, session, text) {
      await wait(600);
      const b = biz(id), cust = DB.customers[id][session.phone];
      const found = geocodeText(b, text);
      if (!found.exact) throw new ApiError(422, 'address_not_precise', 'That address is not specific enough to place on the map; add the street and number', { formatted_address: found.formatted_address });
      if (cust.addresses.length >= 20) throw new ApiError(409, 'conflict', 'At most 20 addresses can be saved; remove one first');
      cust.addresses.push({ id: 'ad-' + (++DB.seq), line1: found.formatted_address.replace(/, Malaysia$/, ''), city: null, postcode: null, distance_km: found.distance_km });
      return clone(cust.addresses);
    },
    async removeSaved(id, session, kind, rowId) {
      await wait(400);
      const cust = DB.customers[id][session.phone];
      const list = kind === 'address' ? cust.addresses : cust.vehicles;
      const row = list.find((x) => x.id === rowId);
      const used = DB.bookings.some((x) => x.biz === id && ['pending', 'confirmed', 'in-progress', 'waiting-for-parts'].includes(x.status) && x.customer.phone === session.phone &&
        (kind === 'address' ? x.address && row && x.address.line1.startsWith(row.line1) : x.vehicle && row && x.vehicle.plate === row.plate_no));
      if (used) throw new ApiError(409, 'conflict', `This ${kind} is used by an upcoming booking; cancel or finish it first, or save a new ${kind} instead`);
      list.splice(list.indexOf(row), 1);
      return clone(list);
    },
    async dataRequest(id, session, kind) {
      await wait(500);
      const cust = DB.customers[id][session.phone];
      if (cust.requests.some((r) => r.kind === kind && r.status === 'pending')) throw new ApiError(409, 'conflict', `You already have an open ${kind} request`);
      cust.requests.push({ kind, status: 'pending', requested_at: iso(Date.now()) });
      return clone(cust.requests);
    },

    /* ---------------- the business admin (GET /admin/bookings..., PATCH .../status, POST .../reschedule, .../items, .../resume, .../clear-flags) */
    async adminBookings(id, date, viewer) {
      await wait(250);
      const b = biz(id), now = Date.now();
      return DB.bookings
        .filter((x) => x.biz === id && T.key(x.start_at) === date && (!viewer.staffId || x.allocations.some((a) => a.rid === viewer.staffId)))
        .sort((x, y) => Date.parse(x.start_at) - Date.parse(y.start_at))
        .map((x) => ({ ...clone(x), hold_live: x.status === 'pending' && Date.parse(x.hold_expires_at) > now, service_names: x.service_ids.map((s) => (svc(b, s) || {}).name).filter(Boolean) }));
    },
    async attention(id, viewer) {
      const now = Date.now();
      const mine = DB.bookings.filter((x) => x.biz === id && (!viewer.staffId || x.allocations.some((a) => a.rid === viewer.staffId)));
      return {
        pending: mine.filter((x) => x.status === 'pending' && Date.parse(x.hold_expires_at) > now).map((x) => ({ id: x.id, date: T.key(x.start_at) })),
        flagged: mine.filter((x) => x.flags.length && ['pending', 'confirmed'].includes(x.status) && Date.parse(x.end_at) > now).map((x) => ({ id: x.id, date: T.key(x.start_at) })),
        conflicts: mine.filter((x) => x.schedule_conflict).map((x) => ({ id: x.id, date: T.key(x.start_at) })),
      };
    },
    async adminBooking(id, bookingId) {
      await wait(150);
      const b = biz(id);
      const x = DB.bookings.find((y) => y.id === bookingId && y.biz === id);
      if (!x) throw new ApiError(404, 'not_found', 'Booking not found');
      const parts = b.type === 'automotive' ? x.service_ids.flatMap((s) => (PARTS[s] || []).map(([name, quantity, unit_price, in_stock]) => ({ name, quantity, unit_price, in_stock, needs_ordering: quantity > in_stock }))) : [];
      return { ...clone(x), service_names: x.service_ids.map((s) => (svc(b, s) || {}).name).filter(Boolean), parts };
    },
    async transition(id, bookingId, to, viewer) {
      await wait(450);
      const b = biz(id), now = Date.now();
      const x = DB.bookings.find((y) => y.id === bookingId && y.biz === id);
      const allowed = api.allowedMoves(id, x, now);
      const move = allowed.find((m) => m.to === to);
      if (!move) throw new ApiError(409, 'invalid_transition', `Cannot move booking from '${x.status}' to '${to}'`);
      if (move.blocked) throw new ApiError(409, 'invalid_transition', move.blocked);
      x.history.push({ from: x.status, to, actor: 'staff', who: viewer.label, at: iso(now) });
      if (to === 'waiting-for-parts') for (const a of x.allocations) { if (a.s >= now) a.e = a.s; else if (a.e > now) a.e = now; }
      x.status = to;
      if (to !== 'pending') x.hold_expires_at = null;
      if (!['pending', 'confirmed'].includes(to)) x.flags = [];
      return api.adminBooking(id, bookingId);
    },
    /** What the status buttons offer for a booking, and why one is not open yet (backend: TRANSITIONS and ensure_status_time in app/services/booking.py). */
    allowedMoves(id, x, now = Date.now()) {
      const b = biz(id);
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
    },
    async clearFlags(id, bookingId) { await wait(300); const x = DB.bookings.find((y) => y.id === bookingId); x.flags = []; return api.adminBooking(id, bookingId); },
    async resolveConflict(id, bookingId) { await wait(300); const x = DB.bookings.find((y) => y.id === bookingId); x.schedule_conflict = false; return api.adminBooking(id, bookingId); },
    async resume(id, bookingId, startAt) {
      await wait(600);
      const b = biz(id), now = Date.now();
      const x = DB.bookings.find((y) => y.id === bookingId);
      if (x.status !== 'waiting-for-parts') throw new ApiError(409, 'invalid_transition', 'Only a job waiting for parts can be resumed');
      const start = Date.parse(startAt);
      const { found, plans } = slotFor(b, T.key(start), bookingQuery(x), start, { now, excludeId: x.id, ignoreNotice: true });
      if (!found) throw new ApiError(409, 'slot_unavailable', 'That time is not available', { slots: plans.map(slotOut) });
      x.allocations = x.allocations.filter((a) => a.e > a.s).concat(found.allocations.map((a) => ({ ...a })));
      x.end_at = iso(found.end);
      x.history.push({ from: 'waiting-for-parts', to: 'confirmed', actor: 'staff', at: iso(now) });
      x.status = 'confirmed';
      return api.adminBooking(id, bookingId);
    },
    async addToTicket(id, bookingId, addonId) {
      await wait(500);
      const b = biz(id), now = Date.now();
      const x = DB.bookings.find((y) => y.id === bookingId);
      if (x.status !== 'in-progress') throw new ApiError(409, 'invalid_transition', 'Items can be added once the appointment is in progress');
      const add = svc(b, addonId);
      const delta = (add.duration_minutes + add.buffer_minutes) * MIN;
      const a = x.allocations.find((y) => (res(b, y.rid) || {}).type === 'staff');
      const clashes = DB.bookings.filter((y) => y.id !== x.id && y.biz === id && isLive(y, now) && y.allocations.some((z) => z.rid === a.rid && z.s < a.e + delta && z.e > a.e));
      x.service_ids.push(addonId);
      if (!clashes.length) { a.e += delta; x.end_at = iso(Date.parse(x.end_at) + delta); }
      else { x.schedule_conflict = true; for (const c of clashes) c.schedule_conflict = true; }
      return {
        booking: await api.adminBooking(id, bookingId), extended: !clashes.length, added_minutes: add.duration_minutes,
        conflicts: clashes.map((c) => ({ booking_id: c.id, customer_name: c.customer.name, start_at: c.start_at, end_at: c.end_at, service_names: c.service_ids.map((s) => (svc(b, s) || {}).name) })),
      };
    },

    /* ---------------- set-up, plan and website (/admin/services, /resources, /operating-hours, /plan, /site, /auth/api-keys) */
    async planUsage(id) {
      await wait(200);
      const b = biz(id), u = DB.usage[id], today = T.today();
      const month = today.slice(0, 7);
      const counted = DB.bookings.filter((x) => x.biz === id && x.start_at.slice(0, 7) === month && !['cancelled', 'rejected'].includes(x.status)).length;
      const usage = {
        resources: b.resources.filter((r) => r.status !== 'inactive').length, services: b.services.filter((s) => !s.inactive).length,
        bookings_per_month: u ? u.bookings : counted, whatsapp_per_month: u ? u.whatsapp : 64, sms_per_month: u ? u.sms : 9, sites: 1, domains: 1,
        live_keys_per_kind: Math.max(...['publishable', 'secret'].map((k) => DB.keys[id].filter((x) => x.kind === k && !x.revoked_at).length)),
      };
      return { plan: b.plan, limits: b.plan === 'free' ? clone(JS.FREE_PLAN) : null, usage };
    },
    async addService(id, body) {
      await wait(400);
      const b = biz(id);
      if (!String(body.name || '').trim()) throw new ApiError(422, 'validation_failed', 'Give the service a name');
      if (!(body.duration_minutes > 0 && body.duration_minutes <= 1440)) throw new ApiError(422, 'validation_failed', 'Duration must be between 1 and 1,440 minutes');
      if (b.plan === 'free' && b.services.length >= JS.FREE_PLAN.services) throw new ApiError(402, 'plan_limit_reached', `The Free plan allows ${JS.FREE_PLAN.services} services and ${b.services.length} are in use; remove one or move to a bigger plan`);
      b.services.push({ id: 'svc-' + (++DB.seq), name: body.name.trim(), duration_minutes: +body.duration_minutes, buffer_minutes: 0, price: +body.price || 0, description: '', location_type: body.location_type || 'in_office', travel_buffer_minutes: body.location_type === 'on_site' ? 30 : 0 });
      return clone(b.services);
    },
    async addResource(id, body) {
      await wait(400);
      const b = biz(id);
      if (!String(body.name || '').trim()) throw new ApiError(422, 'validation_failed', 'Give it a name');
      if (body.type === 'table' && !(body.capacity >= 1)) throw new ApiError(422, 'validation_failed', 'tables require a capacity of at least 1');
      const active = b.resources.filter((r) => r.status !== 'inactive').length;
      if (b.plan === 'free' && active >= JS.FREE_PLAN.resources) throw new ApiError(402, 'plan_limit_reached', `The Free plan allows ${JS.FREE_PLAN.resources} staff, tables and bays and ${active} are in use; remove one or move to a bigger plan`, { limit: 'max_resources', allowed: JS.FREE_PLAN.resources, used: active });
      b.resources.push({ id: 'res-' + (++DB.seq), type: body.type, name: body.name.trim(), capacity: body.capacity || null, attributes: {}, breakAt: body.type === 'staff' ? 780 : null, off: [], status: 'active', note: body.type === 'staff' ? 'New' : '' });
      return clone(b.resources);
    },
    async setResourceStatus(id, rid, status) {
      await wait(350);
      const b = biz(id), r = res(b, rid);
      if (status === 'active' && b.plan === 'free' && b.resources.filter((x) => x.status !== 'inactive').length >= JS.FREE_PLAN.resources) throw new ApiError(402, 'plan_limit_reached', `The Free plan allows ${JS.FREE_PLAN.resources} staff, tables and bays and that many are active; switch another off first`);
      r.status = status;
      let flagged = 0;
      for (const x of DB.bookings) {
        if (x.biz !== id || !['pending', 'confirmed'].includes(x.status) || Date.parse(x.end_at) < Date.now() || !x.allocations.some((a) => a.rid === rid)) continue;
        if (status === 'inactive' && !x.flags.includes('resource_inactive')) { x.flags = [...x.flags, 'resource_inactive']; flagged++; }
        if (status === 'active') x.flags = x.flags.filter((f) => f !== 'resource_inactive');
      }
      return { resources: clone(b.resources), flagged };
    },
    async setHours(id, hours) {
      await wait(400);
      biz(id).hours = clone(hours);
      return clone(hours);
    },
    async site(id) {
      await wait(200);
      const b = biz(id), s = DB.sites[id];
      const next = b.plan === 'free' && s.changed_at ? Date.parse(s.changed_at) + 30 * DAY : null;
      return { domain: s.domain, allowed_origins: [`https://${s.domain}`, `https://www.${s.domain}`], origins_follow_domain: b.plan === 'free', domain_changed_at: s.changed_at, next_allowed_at: next && next > Date.now() ? iso(next) : null, every_days: b.plan === 'free' ? 30 : null };
    },
    async changeSite(id, raw) {
      await wait(500);
      const b = biz(id), s = DB.sites[id];
      const host = api.hostOf(raw);
      if (host === s.domain) return api.site(id);
      const info = await api.site(id);
      if (info.next_allowed_at) throw new ApiError(402, 'plan_limit_reached', `The Free plan allows the website address to change once every 30 days; the next change is possible from ${T.dateLabel(info.next_allowed_at)}`, { next_allowed_at: info.next_allowed_at });
      s.domain = host; s.changed_at = iso(Date.now());
      return api.site(id);
    },
    hostOf(raw) {
      let t = String(raw || '').trim().toLowerCase();
      if (!t) throw new ApiError(422, 'validation_failed', 'The website address is empty');
      t = t.replace(/^https?:\/\//, '').split(/[/?#]/)[0].replace(/\.$/, '');
      if (t.includes(':')) throw new ApiError(422, 'validation_failed', 'Give the website address without a port');
      if (/^\d+(\.\d+){3}$/.test(t)) throw new ApiError(422, 'validation_failed', 'Give the website\'s name (shop.example), not an IP address');
      if (!t.includes('.') || !t.split('.').every((l) => /^(?!-)[a-z0-9-]{1,63}(?<!-)$/.test(l)) || /^\d+$/.test(t.split('.').pop())) throw new ApiError(422, 'validation_failed', 'Give the website\'s public name, such as shop.example');
      return t.startsWith('www.') && t.split('.').length > 2 ? t.slice(4) : t;
    },
    async keys(id) { await wait(200); return clone(DB.keys[id]); },
    async recoverKeys(id) {
      await wait(600);
      const b = biz(id), now = iso(Date.now());
      const made = { publishable: 'pk_' + Array.from({ length: 43 }, () => 'abcdefghijkmnopqrstuvwxyzABCDEFGHJKLMNPQRSTUVWXYZ23456789_-'[Math.floor(Math.random() * 60)]).join(''), secret: 'sk_' + Array.from({ length: 43 }, () => 'abcdefghijkmnopqrstuvwxyzABCDEFGHJKLMNPQRSTUVWXYZ23456789_-'[Math.floor(Math.random() * 60)]).join('') };
      let revoked = 0;
      for (const kind of ['publishable', 'secret']) {
        DB.keys[id].push({ id: 'k' + (++DB.seq), kind, prefix: made[kind].slice(0, 8), created_at: now, revoked_at: null, fresh: true });
        if (b.plan === 'free') {
          const live = DB.keys[id].filter((k) => k.kind === kind && !k.revoked_at);
          for (const k of live.slice(0, Math.max(0, live.length - JS.FREE_PLAN.live_keys_per_kind))) { k.revoked_at = now; revoked++; }
        }
      }
      return { publishable_key: made.publishable, secret_key: made.secret, revoked };
    },

    /* ---------------- self-service sign-up (POST /signup, /signup/verify) */
    async signupStart(body, scenario) {
      await wait(700);
      const errors = {};
      const c = JS.COUNTRIES[body.country];
      if (!String(body.name || '').trim()) errors.name = "Enter your business's name";
      if (!body.business_type) errors.business_type = 'Choose what kind of business it is';
      if (!String(body.address_line1 || '').trim()) errors.address_line1 = 'The street is needed';
      if (c.states.length && !String(body.city || '').trim()) errors.city = 'The city or suburb is needed';
      if (c.states.length && !body.state) errors.state = 'Choose the state';
      const pc = String(body.postcode || '').replace(/\s/g, '');
      if (!new RegExp(`^\\d{${c.postcode}}$`).test(pc)) errors.postcode = `A ${c.name} postcode has ${c.postcode} digits (for example ${c.postcodeExample})`;
      try { api.hostOf(body.domain); } catch (e) { errors.domain = e.detail; }
      const email = normEmail(body.owner_email);
      if (!emailOk(email)) errors.owner_email = 'Enter a complete email address';
      else if (JS.DISPOSABLE.some((d) => email.endsWith('@' + d) || email.endsWith('.' + d))) errors.owner_email = 'Please use an email address you keep: addresses from temporary-mail services are not accepted';
      if (!body.accepted_terms) errors.accepted_terms = 'Tick the box to accept the terms';
      if (Object.keys(errors).length) throw new ApiError(422, 'validation_failed', 'Some details need fixing', { fields: errors });
      if (scenario === 'send_fails') throw new ApiError(503, 'delivery_failed', 'We could not send the confirmation email; please try again shortly');
      DB.signup = clone(body);
      return { status: 'sent' };
    },
    async signupVerify() {
      await wait(700);
      const s = DB.signup;
      if (!s) throw new ApiError(401, 'unauthorized', 'Invalid or expired confirmation link');
      DB.signup = null;
      const id = 'new-' + (++DB.created);
      const host = api.hostOf(s.domain);
      const b = {
        id, type: s.business_type, kind: { spa: 'Spa & wellness', salon: 'Hair salon', restaurant: 'Restaurant', automotive: 'Car service workshop', professional: 'Home services' }[s.business_type],
        name: s.name.trim(), address: [s.address_line1, s.address_line2, s.postcode + ' ' + (s.city || ''), s.state].filter(Boolean).join(', '),
        phone: '', email: normEmail(s.owner_email), domain: host, plan: 'free', rating: [0, 0], description: '', hours: null, closedDates: [],
        policy: { default_status: 'confirmed', min_notice_min: 120, cancel_notice_h: 24, window_days: 60, pending_timeout_min: 30 }, who: { restaurant: 'table', automotive: 'mechanic', professional: 'technician', salon: 'stylist' }[s.business_type] || 'therapist',
        step: 30, services: [], resources: [], isNew: true, supportedVehicles: s.business_type === 'automotive' ? clone(JS.BUSINESSES[3].supportedVehicles) : undefined,
        slotSettings: s.business_type === 'restaurant' ? { booking_mode: 'fixed', first: 1080, last: 1260, interval: 30, duration: 90, buffer: 15 } : undefined,
        location: s.business_type === 'professional' ? { radius_km: 20, base: s.city } : undefined, collectsGender: ['spa', 'salon'].includes(s.business_type),
      };
      DB.businesses.push(b);
      DB.customers[id] = {};
      DB.usage[id] = { bookings: 0, whatsapp: 0, sms: 0 };
      DB.sites[id] = { domain: host, changed_at: null };
      DB.keys[id] = [];
      const keys = await api.recoverKeys(id);
      return { tenant_id: id, publishable_key: keys.publishable_key, secret_key: keys.secret_key, webhook_secret: 'whsec_' + newRef() + newRef() + newRef(), domain: host, plan: 'free' };
    },
  };

  reset();
  JS.api = api;
})();
