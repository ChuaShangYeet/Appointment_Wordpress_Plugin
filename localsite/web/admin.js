/* The business's own admin (backend: /v1/admin/... and /v1/auth/api-keys). The role comes from the prototype strip:
   owner and manager see everything; a staff member tied to a resource sees only their own appointments and no health notes. */
(function () {
  const J = window.JS;
  const T = J.T;
  const api = J.api;

  function init() {
    return {
      tab: 'bookings', date: T.today(), filter: 'all', list: null, listKey: null, loading: false, attn: null, version: 0,
      open: null, detail: null, err: null, busy: null, move: null, extra: { addonId: '', result: null, busy: false },
      plan: null, site: null, keys: null, newKeys: null, siteForm: { domain: '', err: null, busy: false, open: false },
      svcForm: { name: '', duration: '60', price: '', afterInspection: false, where: 'in_office', description: '', travel: '30', extra: false, err: null },
      prodForm: { name: '', price: '', stock: '10', err: null }, carForm: { make: '', model: '', from: '', to: '', err: null }, resForm: { name: '', type: 'staff', capacity: '4', err: null },
      hours: null, hoursErr: null,
    };
  }
  const st = () => J.state.admin;
  function viewer(b) {
    const role = String(J.state.role || 'owner');
    // 'staff:<id>' is one member of staff; one of another business (after switching business) falls back to the owner
    const staff = role.startsWith('staff:') && b.resources.find((r) => r.type === 'staff' && r.id === role.slice(6));
    if (staff) return { role: 'staff', staffId: staff.id, label: staff.name };
    return { role: role === 'manager' ? 'manager' : 'owner', staffId: null, label: role === 'manager' ? 'Manager' : 'Owner' };
  }
  const canManage = (v) => v.role === 'owner' || v.role === 'manager';
  J.adminViewer = () => viewer(J.biz());
  const MOVE_LABEL = { confirmed: 'Accept', rejected: 'Decline', cancelled: 'Cancel booking', 'in-progress': 'Customer has arrived', completed: 'Mark completed', 'no-show': "Didn't turn up", 'waiting-for-parts': 'Waiting for parts' };

  /* ------------------------------------------------------------------ loading */
  // which list is on screen: a different member of staff is a different list
  const listKey = (b, s, v) => [b.id, s.date, v.role, v.staffId || '', s.filter, s.version].join('|');
  async function load() {
    const b = J.biz(), s = st(), v = viewer(b);
    const key = listKey(b, s, v);
    s.loading = true;
    const [attn] = await J.call(() => api.attention(b.id, v));
    s.attn = attn;
    let list;
    if (s.filter === 'all') [list] = await J.call(() => api.adminBookings(b.id, s.date, v));
    else {
      const items = (attn && attn[s.filter]) || [];
      const days = [...new Set(items.map((x) => x.date))].sort();
      list = [];
      for (const d of days) { const [rows] = await J.call(() => api.adminBookings(b.id, d, v)); list.push(...(rows || []).filter((r) => items.some((x) => x.id === r.id))); }
    }
    s.list = list || []; s.listKey = key; s.loading = false;
    if (s.open) { const [d] = await J.call(() => api.adminBooking(b.id, s.open, v)); s.detail = d; }
    J.render();
  }
  function reload() { st().version += 1; st().listKey = null; J.render(); }

  /* ------------------------------------------------------------------ the list and the board */
  function holdLeft(x) {
    const m = Math.max(0, Math.round((Date.parse(x.hold_expires_at) - Date.now()) / 60000));
    return m <= 0 ? 'Hold ended' : `Hold ends in ${m} min`;
  }
  function resNames(b, x) {
    return [...new Set(x.allocations.filter((a) => a.e > a.s).map((a) => a.rid))].map((id) => api.staffName(b.id, id)).join(', ');
  }
  function listView(b) {
    const s = st();
    if (!s.list) return `<div class="card row muted">${J.busyLabel('Loading bookings…')}</div>`;
    if (!s.list.length) return `<div class="card empty"><h3>${s.filter === 'all' ? 'No bookings on this day' : 'Nothing waiting'}</h3><p>${s.filter === 'all' ? 'Bookings made online and by your team appear here.' : 'All caught up.'}</p></div>`;
    const multiDay = s.filter !== 'all';
    return `<div class="blist">${s.list.map((x) => `<button type="button" class="brow" data-act="adm.open" data-id="${x.id}" aria-current="${s.open === x.id}">
        <span class="time">${multiDay ? `<small>${T.dateLabel(x.start_at)}</small>` : ''}${T.time24(x.start_at)}<small>${T.time24(x.end_at)}</small></span>
        <span class="stack" style="gap:2px;min-width:0"><span class="who">${J.esc(x.customer.name)}${b.type === 'restaurant' ? ` <span class="muted">· ${x.party_size} ${x.party_size === 1 ? 'guest' : 'guests'}</span>` : ''}</span>
          <span class="what">${b.type === 'restaurant' ? (x.table_type === 'vip' ? 'Private room · ' : '') : J.esc(x.service_names.join(' + ')) + ' · '}${J.esc(resNames(b, x))}</span></span>
        <span class="tags">${J.chip(x.status)}${x.flags.length ? '<span class="chip flag">Needs review</span>' : ''}${x.schedule_conflict ? '<span class="chip flag">Clash</span>' : ''}${x.status === 'pending' ? `<span class="small muted">${holdLeft(x)}</span>` : ''}</span></button>`).join('')}</div>`;
  }

  function boardView(b, v) {
    const s = st();
    if (!s.list) return `<div class="card row muted">${J.busyLabel('Loading…')}</div>`;
    const hours = (b.hours || [])[T.weekday(s.date)];
    const lo = Math.floor(((hours && hours[0]) || 540) / 60) * 60 - 60, hi = Math.ceil(((hours && hours[1]) || 1080) / 60) * 60 + 60;
    const span = hi - lo;
    const pct = (ms) => ((T.minutes(ms) + (T.key(ms) !== s.date ? 1440 : 0) - lo) / span) * 100;
    const rows = b.resources.filter((r) => !v.staffId || r.id === v.staffId);
    const ticks = [];
    for (let m = lo; m <= hi; m += 60) ticks.push(`<span style="left:${((m - lo) / span) * 100}%">${String(Math.floor(m / 60) % 24).padStart(2, '0')}:00</span>`);
    const nowLine = s.date === T.today() ? `<i class="now-line" style="left:${pct(Date.now())}%" aria-hidden="true"></i>` : '';
    const blocks = (rid) => s.list.filter((x) => !['cancelled', 'rejected', 'no-show'].includes(x.status)).flatMap((x) => x.allocations.filter((a) => a.rid === rid && a.e > a.s).flatMap((a) => {
      const bs = Date.parse(x.start_at), be = Date.parse(x.end_at);
      const out = [];
      const core = [Math.max(a.s, bs), b.type === 'restaurant' || b.type === 'automotive' ? a.e : Math.min(a.e, be)];
      if (a.s < bs) out.push(`<span class="blk travel" style="left:${pct(a.s)}%;width:${pct(bs) - pct(a.s)}%" title="Travel">travel</span>`);
      out.push(`<button type="button" class="blk ${x.status} ${x.flags.length || x.schedule_conflict ? 'flagged' : ''}" style="left:${pct(core[0])}%;width:${Math.max(0.8, pct(core[1]) - pct(core[0]))}%" data-act="adm.open" data-id="${x.id}" title="${J.esc(x.customer.name)}, ${T.time24(x.start_at)}">${J.esc(J.first(x.customer.name))}${b.type === 'restaurant' ? ' · ' + x.party_size : ''}</button>`);
      if (a.e > be && b.type === 'professional') out.push(`<span class="blk travel" style="left:${pct(be)}%;width:${pct(a.e) - pct(be)}%" title="Travel">travel</span>`);
      return out;
    })).join('');
    return `<div class="board"><div class="board-inner">
      <div class="board-hours"><span></span><div class="ticks">${ticks.join('')}</div></div>
      ${rows.map((r) => `<div class="board-row"><span class="rname">${J.esc(r.name)}<small>${r.type === 'table' ? r.capacity + ' seats' + ((r.attributes || {}).is_vip ? ' · private' : '') : r.type === 'bay' ? 'Bay' : J.esc(r.note || '')}${r.status === 'inactive' ? ' · off' : ''}</small></span><div class="track">${blocks(r.id)}${nowLine}</div></div>`).join('')}
    </div></div><p class="hint" style="margin-top:8px">Dashed blocks are requests waiting for an answer; a mark on the left edge means the booking needs a look.</p>`;
  }

  /* ------------------------------------------------------------------ the booking drawer */
  function moveBox(b, label, act) {
    const m = st().move;
    let slots = '';
    if (m.loading) slots = `<div class="row muted">${J.busyLabel('Finding free times…')}</div>`;
    else if (m.slots) slots = m.slots.length ? J.views.book.slotGrid(b, m.slots, m.slot, 'adm.moveSlot') : '<p class="muted small">Nothing free that day.</p>';
    return `<div class="card flat stack" style="background:var(--surface-2)"><h3>${label}</h3>
      ${J.views.book.dayStrip({ date: m.date }, m.month, 'adm.moveDate')}${slots}
      ${m.err ? `<p class="err">${J.esc(m.err)}</p>` : ''}
      <div class="row"><button type="button" class="btn small" data-act="${act}" ${m.slot && !m.busy ? '' : 'disabled'}>${m.busy ? J.busyLabel('Saving…') : m.slot ? T.dateLabel(m.slot) + ', ' + T.time(m.slot) : 'Pick a time'}</button><button type="button" class="btn ghost small" data-act="adm.moveClose">Close</button></div>
      ${act === 'adm.moveConfirm' ? '<p class="hint">The customer gets a "your booking was moved" message.</p>' : ''}</div>`;
  }

  function drawer(b, v) {
    const s = st(), x = s.detail;
    if (!x) return `<aside class="drawer"><div class="card row muted">${J.busyLabel('Loading…')}</div></aside>`;
    const moves = api.allowedMoves(b.id, x);
    const sees = canManage(v);
    const rows = [
      ['When', `${T.day(T.key(x.start_at), { weekday: 'short', day: 'numeric', month: 'short' })}, ${T.time(x.start_at)} <span class="muted">· about ${J.mins(Math.round((Date.parse(x.end_at) - Date.parse(x.start_at)) / 60000))}, kept free until ${T.time(x.end_at)}</span>`],
      b.type === 'restaurant' ? ['Party', `${x.party_size} ${x.party_size === 1 ? 'guest' : 'guests'} · ${x.table_type === 'vip' ? 'Private room' : 'Dining room'}${x.minimum_spend_agreed ? ` · agreed min. spend ${J.money(x.minimum_spend_agreed)}` : ''}`] : ['Service', J.esc(x.service_names.join(' + '))],
      [b.type === 'restaurant' ? 'Table' : 'With', J.esc(resNames(b, x)) || '—'],
      ['Customer', `${J.esc(x.customer.name)}<br><span class="mono small">${J.esc(J.fmt.prettyPhone(x.customer.phone))}</span>${x.contact_email ? `<br><span class="small">${J.esc(x.contact_email)}</span>` : ''}`],
    ];
    if (x.vehicle) rows.push(['Car', `<span class="mono">${J.esc(x.vehicle.plate)}</span> · ${J.esc(x.vehicle.model)}${x.vehicle.year ? ', ' + x.vehicle.year : ''}`]);
    if (x.address) {
      const a = x.address, place = [a.postcode, a.city].filter(Boolean).join(' ');
      const map = a.latitude != null ? `<br><a class="small" href="https://www.google.com/maps/search/?api=1&amp;query=${a.latitude},${a.longitude}" target="_blank" rel="noopener noreferrer">Open in Google Maps</a>` : '';
      rows.push(['Visit at', `${J.esc(a.line1)}${place ? `<br><span class="small">${J.esc(place)}</span>` : ''}${a.distance_km != null ? ` <span class="muted small">(${a.distance_km} km)</span>` : ''}${map}`]);
    }
    if (x.special_request) rows.push(['Note', J.esc(x.special_request)]);
    rows.push(['Made', `${T.when(x.created_at)} · ${x.source === 'wordpress' ? 'website' : x.source === 'admin' ? 'by staff' : 'booking page'}`]);
    const adds = b.type === 'salon' ? b.services.filter((y) => y.is_addon) : [];
    const canMove = sees && ['pending', 'confirmed'].includes(x.status);
    return `<aside class="drawer" aria-label="Booking details"><div class="card">
      <div class="spread"><div class="stack" style="gap:4px"><h2>${J.esc(x.customer.name)}</h2><span class="mono small muted">REF ${J.esc(x.ref)}</span></div><button type="button" class="closex" data-act="adm.close" aria-label="Close">✕</button></div>
      <div class="row">${J.chip(x.status)}${x.status === 'pending' ? `<span class="small muted">${holdLeft(x)}</span>` : ''}</div>
      ${s.err ? `<div class="notice bad"><span>${J.esc(s.err)}</span></div>` : ''}
      ${x.status === 'pending' ? `<div class="notice warn"><strong>Waiting for your answer</strong><span>The ${b.type === 'restaurant' ? 'table' : 'time'} is held until ${T.time(x.hold_expires_at)}. If nobody answers by then the request is declined automatically and the customer is told.</span></div>` : ''}
      ${x.flags.length ? `<div class="notice warn"><strong>Needs a look</strong>${x.flags.map((f) => `<span>${J.esc(J.FLAGS[f] || f)}.</span>`).join('')}<span class="small">Nothing was cancelled. Move the booking, or keep it as it is.</span>
        ${sees ? `<div class="row" style="margin-top:6px"><button type="button" class="btn small ghost" data-act="adm.clearFlags">Keep as it is</button>${canMove ? '<button type="button" class="btn small ghost" data-act="adm.moveOpen" data-kind="move">Move it</button>' : ''}</div>` : ''}</div>` : ''}
      ${x.schedule_conflict ? `<div class="notice bad"><strong>Runs into another appointment</strong><span>A running appointment needs more time than planned. Nothing was moved; talk to the customers involved.</span>${sees ? '<div style="margin-top:6px"><button type="button" class="btn small ghost" data-act="adm.resolve">Mark as sorted</button></div>' : ''}</div>` : ''}
      <dl class="summary">${rows.map(([k, val]) => `<dt>${k}</dt><dd>${val}</dd>`).join('')}</dl>
      ${x.health_notes ? (sees ? `<div class="private stack" style="gap:4px"><span class="label">Health notes · private</span><span>${J.esc(x.health_notes)}</span><span class="hint">Only owners and managers can read this. Each time it is opened is recorded.</span></div>`
        : '<div class="private small muted">This booking has health notes. Only owners and managers can read them.</div>') : ''}
      ${x.parts && x.parts.length ? `<div class="stack" style="gap:6px"><span class="label">Parts on the job order</span><div class="scrollx"><table class="table"><thead><tr><th>Part</th><th>Qty</th><th>On shelf</th></tr></thead><tbody>${x.parts.map((p) => `<tr><td>${J.esc(p.name)}</td><td class="mono">${p.quantity}</td><td>${p.needs_ordering ? '<span class="chip pending">Order in</span>' : `<span class="mono">${p.in_stock}</span>`}</td></tr>`).join('')}</tbody></table></div></div>` : ''}
      ${moves.length ? `<div class="stack" style="gap:8px"><span class="label">Update status</span><div class="moves">${moves.map((m) => `<button type="button" class="btn small ${m.to === 'cancelled' || m.to === 'rejected' || m.to === 'no-show' ? 'danger' : m.to === 'confirmed' || m.to === 'completed' || m.to === 'in-progress' ? '' : 'ghost'}" data-act="adm.status" data-to="${m.to}" ${m.blocked || s.busy ? 'disabled' : ''}>${s.busy === m.to ? J.busyLabel('Saving…') : MOVE_LABEL[m.to] || m.to}</button>`).join('')}</div>
        ${moves.filter((m) => m.blocked).map((m) => `<span class="hint">${MOVE_LABEL[m.to]}: ${J.esc(m.blocked)}.</span>`).join('')}</div>` : ''}
      ${x.status === 'waiting-for-parts' ? (s.move ? moveBox(b, 'Book the rest of the job', 'adm.resumeConfirm') : '<div><button type="button" class="btn small" data-act="adm.moveOpen" data-kind="resume">Parts are in: book the rest</button></div>') : ''}
      ${canMove && !(s.move && s.move.kind === 'move') ? '<div><button type="button" class="btn ghost small" data-act="adm.moveOpen" data-kind="move">Move to another time</button></div>' : ''}
      ${canMove && s.move && s.move.kind === 'move' ? moveBox(b, 'Move to another time', 'adm.moveConfirm') : ''}
      ${!sees && ['pending', 'confirmed'].includes(x.status) ? '<p class="hint">Only a manager or the owner can move bookings.</p>' : ''}
      ${b.type === 'salon' && x.status === 'in-progress' ? `<div class="stack" style="gap:8px"><span class="label">Add to this appointment</span>
        <div class="row"><select id="adm-extra" data-model="admin.extra.addonId" aria-label="Extra service" style="flex:1;min-width:160px"><option value="">Choose an extra</option>${adds.map((a) => `<option value="${a.id}" ${s.extra.addonId === a.id ? 'selected' : ''}>${J.esc(a.name)} (+${a.duration_minutes} min)</option>`).join('')}</select><button type="button" class="btn small" data-act="adm.addExtra" ${s.extra.addonId && !s.extra.busy ? '' : 'disabled'}>Add</button></div>
        ${s.extra.result ? (s.extra.result.extended ? `<div class="notice ok"><span>Added. The appointment now ends at ${T.time(s.extra.result.booking.end_at)}.</span></div>`
          : `<div class="notice bad"><strong>That runs into the next appointment</strong>${s.extra.result.conflicts.map((c) => `<span>${T.time(c.start_at)}: ${J.esc(J.first(c.customer_name))}, ${J.esc(c.service_names.join(' + '))}</span>`).join('')}<span class="small">The extra is on the ticket but the time was not stretched. Both appointments are marked; the stylist has been messaged.</span></div>`) : ''}</div>` : ''}
      <div class="stack" style="gap:6px"><span class="label">History</span><ul class="hist">${x.history.map((h) => `<li><time>${T.dateLabel(h.at)} ${T.time24(h.at)}</time><span>${h.moved ? `Moved from ${T.when(h.from_time)} to ${T.when(h.to_time)}` : h.from ? `${J.STATUS[h.from] || h.from} → <b>${J.STATUS[h.to] || h.to}</b>` : `Booked as <b>${J.STATUS[h.to] || h.to}</b>`} <span class="muted">· ${h.actor === 'customer' ? 'customer' : h.actor === 'system' ? 'automatically' : J.esc(h.who || 'staff')}</span></span></li>`).join('')}</ul></div>
    </div></aside>`;
  }

  /* ------------------------------------------------------------------ set-up, plan and website */
  function setupView(b, v) {
    const s = st();
    if (!canManage(v)) return '<div class="card empty"><p>Only managers and the owner can change the set-up.</p></div>';
    const who = b.type === 'restaurant' ? 'Tables' : b.type === 'automotive' ? 'Mechanics and bays' : 'Team';
    const hours = s.hours || (b.hours ? b.hours.map((h) => (h ? { open: T.clock(h[0]), o: h[0], c: h[1] } : null)) : [0, 1, 2, 3, 4, 5, 6].map(() => null));
    if (!s.hours) s.hours = hours.map((h) => (h ? { o: h.o, c: h.c } : null));
    const toHHMM = (m) => `${String(Math.floor(m / 60)).padStart(2, '0')}:${String(m % 60).padStart(2, '0')}`;
    const checklist = b.isNew ? `<section class="card stack"><h2>Get ready for your first booking</h2><ul class="hist">
        ${[[b.services.length || b.type === 'restaurant', b.type === 'restaurant' ? 'Seating times are set (6 pm to 9 pm, every 30 minutes)' : 'Add at least one service'], [b.resources.length, `Add ${b.type === 'restaurant' ? 'your tables' : b.type === 'automotive' ? 'a mechanic and a bay' : 'yourself or your staff'}`], [b.hours, 'Set your opening hours'], [!b.policy.missing, b.policy.missing ? 'Set your booking rules (notice, how far ahead, cancelling), under Booking rules below' : 'Booking rules are set']]
          .map(([done, text]) => `<li><time>${done ? '<span class="chip confirmed">Done</span>' : '<span class="chip pending">To do</span>'}</time><span>${text}</span></li>`).join('')}</ul>
        ${(b.services.length || b.type === 'restaurant') && b.resources.length && b.hours && !b.policy.missing ? '<div class="notice ok"><span>Your booking page is ready. Open <b>Book</b> in the strip above to try it.</span></div>' : ''}</section>` : '';
    return `<div class="stack-lg">${checklist}
      ${b.type !== 'restaurant' ? `<section class="card stack"><h2>Services</h2>
        ${b.services.length ? `<div class="scrollx"><table class="table"><thead><tr><th>Service</th><th>Time</th>${b.type === 'professional' ? '<th>Where</th>' : ''}<th>Price</th></tr></thead><tbody>${b.services.map((x) => `<tr><td><b>${J.esc(x.name)}</b>${x.is_addon ? ' <span class="chip">Extra</span>' : ''}${x.service_category === 'inspection' ? ' <span class="chip">Inspection</span>' : ''}${x.description ? `<br><span class="small muted">${J.esc(x.description)}</span>` : ''}</td>
          <td class="mono">about ${J.mins(x.duration_minutes)}${x.buffer_minutes ? `<br><span class="small muted">+ ${x.buffer_minutes} min to tidy up</span>` : ''}</td>
          ${b.type === 'professional' ? `<td>${x.location_type === 'on_site' ? `At the customer's<br><span class="small muted">${x.travel_buffer_minutes ? `${x.travel_buffer_minutes} min travel each way` : 'No travel time kept'}</span>` : 'At our office'}</td>` : ''}
          <td class="mono">${J.price(x)}</td></tr>`).join('')}</tbody></table></div>` : '<p class="muted">No services yet.</p>'}
        <form class="stack" data-submit="adm.addService"><span class="label">Add a service</span><div class="grid2">
          ${J.field({ id: 'sv-name', label: 'Name', model: 'admin.svcForm.name', placeholder: 'Deep tissue massage' })}
          ${J.field({ id: 'sv-dur', label: 'Usual length (minutes)', model: 'admin.svcForm.duration', type: 'number', attrs: 'min="5" max="1440" inputmode="numeric"', hint: 'Customers see it as "about". Your calendar keeps this much time; when a job ends early or runs late, move the next booking.' })}
          ${J.field({ id: 'sv-price', label: s.svcForm.afterInspection ? 'Starting price (RM)' : 'Price (RM)', model: 'admin.svcForm.price', type: 'number', attrs: 'min="0" inputmode="decimal"', placeholder: '0', hint: s.svcForm.afterInspection ? 'What the customer pays at least, like an inspection fee. Leave 0 when you can\'t say yet.' : '0 means free.' })}
          ${b.type === 'professional' ? J.field({ id: 'sv-where', label: 'Where', model: 'admin.svcForm.where', options: [['in_office', 'At our office'], ['on_site', "At the customer's address"]] }) : ''}
          ${b.type === 'professional' && s.svcForm.where === 'on_site' ? J.field({ id: 'sv-travel', label: 'Travel time each way (minutes)', model: 'admin.svcForm.travel', type: 'number', attrs: 'min="0" max="240" inputmode="numeric"', hint: 'Kept free before and after each visit, so the technician can get there and back. Put 0 to keep none, and move the next visit yourself when a job runs over.' }) : ''}</div>
          <label class="check" for="sv-inspect"><input id="sv-inspect" type="checkbox" data-model="admin.svcForm.afterInspection" data-rerender ${s.svcForm.afterInspection ? 'checked' : ''}><span>We tell the price after we have looked at the job (like a car with an unknown problem, or a house's wiring)</span></label>
          ${J.field({ id: 'sv-desc', label: 'Description', model: 'admin.svcForm.description', area: true, optional: true, placeholder: 'What the customer gets, in a sentence or two', hint: 'Shown under the service on your booking page.' })}
          ${b.type === 'salon' ? `<label class="check" for="sv-extra"><input id="sv-extra" type="checkbox" data-model="admin.svcForm.extra" ${s.svcForm.extra ? 'checked' : ''}><span>This is an extra (added to a main service, like a scalp massage)</span></label>` : ''}
          ${s.svcForm.err ? `<div class="notice bad"><span>${J.esc(s.svcForm.err)}</span></div>` : ''}<div><button class="btn small" type="submit">Add service</button></div></form></section>` : `<section class="card stack"><h2>Seating times</h2><p>Reservations start every ${b.slotSettings.interval} minutes from ${T.clock(b.slotSettings.first)} to ${T.clock(b.slotSettings.last)}. A table is kept for ${b.slotSettings.duration} minutes plus ${b.slotSettings.buffer} to reset it.</p></section>`}
      ${b.type === 'salon' ? productsSection(b) : ''}${b.type === 'automotive' ? carsSection(b) : ''}
      <section class="card stack"><h2>${who}</h2>
        ${b.resources.length ? `<div class="scrollx"><table class="table"><thead><tr><th>Name</th><th>${b.type === 'restaurant' ? 'Seats' : 'Role'}</th><th>Status</th><th></th></tr></thead><tbody>${b.resources.map((r) => `<tr><td>${J.esc(r.name)}</td><td>${r.type === 'table' ? r.capacity + ((r.attributes || {}).is_vip ? ' · private, min. ' + J.money(r.attributes.minimum_spend) : '') : r.type === 'bay' ? 'Bay' : J.esc(r.note || 'Staff')}</td><td>${r.status === 'inactive' ? '<span class="chip">Off</span>' : '<span class="chip confirmed">Taking bookings</span>'}</td>
          <td><button type="button" class="btn ghost small" data-act="adm.toggleRes" data-id="${r.id}" data-to="${r.status === 'inactive' ? 'active' : 'inactive'}">${r.status === 'inactive' ? 'Switch on' : 'Switch off'}</button></td></tr>`).join('')}</tbody></table></div>` : '<p class="muted">Nobody yet.</p>'}
        <form class="stack" data-submit="adm.addRes"><span class="label">Add ${b.type === 'restaurant' ? 'a table' : b.type === 'automotive' ? 'a mechanic or bay' : 'a staff member'}</span><div class="grid2">
          ${J.field({ id: 'rs-name', label: 'Name', model: 'admin.resForm.name', placeholder: b.type === 'restaurant' ? 'Table 8' : 'Name' })}
          ${b.type === 'restaurant' ? J.field({ id: 'rs-cap', label: 'Seats', model: 'admin.resForm.capacity', type: 'number', attrs: 'min="1" inputmode="numeric"' }) : ''}
          ${b.type === 'automotive' ? J.field({ id: 'rs-type', label: 'Kind', model: 'admin.resForm.type', options: [['staff', 'Mechanic'], ['bay', 'Bay']] }) : ''}</div>
          ${s.resForm.err ? `<div class="notice bad"><strong>${s.resForm.err.plan ? "Your plan doesn't allow more" : "Couldn't add it"}</strong><span>${J.esc(s.resForm.err.text)}</span></div>` : ''}<div><button class="btn small" type="submit">Add</button></div></form></section>
      <section class="card stack"><h2>Opening hours</h2>
        <div class="scrollx"><table class="table"><thead><tr><th>Day</th><th>Open</th><th>Close</th><th>Closed</th></tr></thead><tbody>
        ${['Sunday', 'Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday'].map((d, i) => { const h = s.hours[i]; return `<tr><td>${d}</td>
          <td><input id="h-o${i}" type="time" aria-label="${d} opens" value="${h ? toHHMM(h.o) : ''}" data-change="adm.hour" data-i="${i}" data-k="o" ${h ? '' : 'disabled'}></td>
          <td><input id="h-c${i}" type="time" aria-label="${d} closes" value="${h ? toHHMM(h.c) : ''}" data-change="adm.hour" data-i="${i}" data-k="c" ${h ? '' : 'disabled'}></td>
          <td><input id="h-x${i}" type="checkbox" aria-label="Closed on ${d}" ${h ? '' : 'checked'} data-change="adm.closedDay" data-i="${i}" style="width:20px;height:20px;accent-color:var(--accent)"></td></tr>`; }).join('')}</tbody></table></div>
        ${s.hoursErr ? `<p class="err">${J.esc(s.hoursErr)}</p>` : ''}<div><button type="button" class="btn small" data-act="adm.saveHours">Save opening hours</button></div>
        <p class="hint">A close time earlier than the open time means you close after midnight.</p></section>
      ${locationSection(b)}
      ${rulesSection(b)}</div>`;
  }

  /** A salon's products for sale (shown on the booking page under the extras, and added to a running appointment's ticket). */
  function productsSection(b) {
    const s = st(), f = s.prodForm;
    const label = { in_stock: 'In stock', out_of_stock: 'Out of stock', available_on_order: 'On order' };
    return `<section class="card stack"><h2>Products for sale</h2>
      ${b.products.length ? `<div class="scrollx"><table class="table"><thead><tr><th>Product</th><th>Price</th><th>On the shelf</th></tr></thead><tbody>${b.products.map((p) => `<tr><td>${J.esc(p.name)}</td><td class="mono">${J.money(p.price)}</td><td>${p.stock_qty != null ? `<span class="mono">${p.stock_qty}</span> · ` : ''}${label[p.stock_status] || p.stock_status}</td></tr>`).join('')}</tbody></table></div>` : '<p class="muted">No products yet.</p>'}
      <form class="stack" data-submit="adm.addProduct"><span class="label">Add a product</span><div class="grid2">
        ${J.field({ id: 'pr-name', label: 'Name', model: 'admin.prodForm.name', placeholder: 'Argan oil serum' })}
        ${J.field({ id: 'pr-price', label: 'Price (RM)', model: 'admin.prodForm.price', type: 'number', attrs: 'min="0" inputmode="decimal"' })}
        ${J.field({ id: 'pr-stock', label: 'How many on the shelf', model: 'admin.prodForm.stock', type: 'number', attrs: 'min="0" inputmode="numeric"' })}</div>
        ${f.err ? `<div class="notice bad"><span>${J.esc(f.err)}</span></div>` : ''}<div><button class="btn small" type="submit">Add product</button></div></form></section>`;
  }

  /** The car makes and models a workshop services, with their model years: customers can only book for these. */
  function carsSection(b) {
    const s = st(), f = s.carForm;
    return `<section class="card stack"><h2>Car models we service</h2>
      ${b.supportedVehicles.length ? `<div class="scrollx"><table class="table"><thead><tr><th>Make</th><th>Model</th><th>Model years</th><th></th></tr></thead><tbody>${b.supportedVehicles.map((v) => `<tr><td>${J.esc(v.make)}</td><td>${J.esc(v.model)}</td><td class="mono">${v.year_from || '…'}–${v.year_to || 'now'}</td>
        <td><button type="button" class="btn ghost small" data-act="adm.removeCar" data-id="${v.id}">Remove</button></td></tr>`).join('')}</tbody></table></div>` : '<p class="muted">No car models yet: customers cannot book until you add the ones you service.</p>'}
      <form class="stack" data-submit="adm.addCar"><span class="label">Add a car model</span><div class="grid2">
        ${J.field({ id: 'car-make', label: 'Make', model: 'admin.carForm.make', placeholder: 'Perodua' })}
        ${J.field({ id: 'car-model', label: 'Model', model: 'admin.carForm.model', placeholder: 'Bezza' })}
        ${J.field({ id: 'car-from', label: 'From model year', model: 'admin.carForm.from', type: 'number', optional: true, attrs: 'min="1950" max="2100" inputmode="numeric"' })}
        ${J.field({ id: 'car-to', label: 'Up to model year', model: 'admin.carForm.to', type: 'number', optional: true, attrs: 'min="1950" max="2100" inputmode="numeric"', hint: 'Leave empty if you service every newer one.' })}</div>
        ${f.err ? `<div class="notice bad"><span>${J.esc(f.err)}</span></div>` : ''}<div><button class="btn small" type="submit">Add car model</button></div></form></section>`;
  }

  /** Where the business is: pasted as a Google Maps link (a share link from the app works). The point is read from it; the link becomes the directions link. */
  function locationSection(b) {
    const s = st(), f = s.place || { url: '', radius: String((b.place && b.place.radius_km) || 25), err: null, busy: false, saved: null };
    s.place = f;
    const p = b.place || {};
    const pro = b.type === 'professional';
    return `<section class="card stack"><h2>Location</h2>
      ${p.latitude != null ? `<dl class="summary"><dt>Map point</dt><dd class="mono">${p.latitude.toFixed(5)}, ${p.longitude.toFixed(5)}</dd>${pro ? `<dt>Visits up to</dt><dd>${p.radius_km} km away</dd>` : ''}<dt>Directions link</dt><dd>${p.maps_url ? `<a href="${J.esc(p.maps_url)}" target="_blank" rel="noopener noreferrer" class="mono small">${J.esc(p.maps_url)}</a>` : '<span class="muted">None yet</span>'}</dd></dl>`
        : '<div class="notice info"><span>Not set yet. Customers get directions from it' + (pro ? ', and it is where your service area is measured from' : '') + '.</span></div>'}
      <form class="stack" data-submit="adm.placeSave">
        ${J.field({ id: 'pl-url', label: 'Google Maps link', model: 'admin.place.url', placeholder: 'https://maps.app.goo.gl/…', hint: 'In Google Maps, open your business, tap <b>Share</b> and copy the link. A link from the browser address bar works too.', attrs: 'inputmode="url" autocomplete="off"' })}
        ${pro ? J.field({ id: 'pl-radius', label: 'Visit customers up to (km)', model: 'admin.place.radius', type: 'number', attrs: 'min="1" max="500" inputmode="decimal"' }) : ''}
        ${f.err ? `<div class="notice bad"><span>${J.esc(f.err)}</span></div>` : ''}
        ${f.saved ? `<div class="notice ok"><span>Saved: ${f.saved}</span></div>` : ''}
        <div><button class="btn small" type="submit" ${f.busy ? 'disabled' : ''}>${f.busy ? J.busyLabel('Reading the link…') : 'Save location'}</button></div></form></section>`;
  }

  /** The booking rules (a policy): a change applies to bookings made from now on; existing bookings keep the rules they were made under. */
  function rulesSection(b) {
    const s = st(), p = b.policy, f = s.rules;
    if (!f && p.missing) return `<section class="card stack"><h2>Booking rules</h2><div class="notice warn"><strong>Not set yet</strong><span>Customers can't book until you set how much notice you need, how far ahead they can book and until when they can cancel.</span></div><div><button type="button" class="btn small" data-act="adm.rulesEdit">Set booking rules</button></div></section>`;
    if (!f) return `<section class="card stack"><div class="spread"><h2>Booking rules</h2><button type="button" class="btn ghost small" data-act="adm.rulesEdit">Change</button></div><dl class="summary">
        <dt>Notice</dt><dd>At least ${J.mins(p.min_notice_min)} before the start</dd><dt>How far ahead</dt><dd>Up to ${p.window_days} days</dd>
        <dt>Cancelling</dt><dd>Free until ${p.cancel_notice_h} hours before</dd><dt>Approval</dt><dd>${p.default_status === 'pending' ? `You confirm every booking; requests wait ${p.pending_timeout_min} minutes` : 'Bookings are confirmed straight away'}</dd></dl></section>`;
    return `<section class="card stack"><h2>Booking rules</h2>
      <form class="stack" data-submit="adm.rulesSave"><div class="grid2">
        ${J.field({ id: 'ru-notice', label: 'Notice needed (hours)', model: 'admin.rules.notice_h', type: 'number', attrs: 'min="0" max="720" step="0.5" inputmode="decimal"', hint: 'How long before the start a booking can still be made online.' })}
        ${J.field({ id: 'ru-window', label: 'How far ahead (days)', model: 'admin.rules.window_days', type: 'number', attrs: 'min="1" max="730" inputmode="numeric"' })}
        ${J.field({ id: 'ru-cancel', label: 'Free cancelling until (hours before)', model: 'admin.rules.cancel_notice_h', type: 'number', attrs: 'min="0" max="720" inputmode="numeric"', hint: 'After that, customers have to call you.' })}
        ${J.field({ id: 'ru-status', label: 'New bookings', model: 'admin.rules.default_status', options: [['confirmed', 'Confirmed straight away'], ['pending', 'Wait for me to accept them']] })}
        ${f.default_status === 'pending' ? J.field({ id: 'ru-hold', label: 'Hold a request for (minutes)', model: 'admin.rules.pending_timeout_min', type: 'number', attrs: 'min="5" max="1440" inputmode="numeric"', hint: 'Not answered by then, it is declined and the customer is told.' }) : ''}</div>
        <p class="hint">New rules apply to bookings made from now on. Bookings already made keep the rules they were made under.</p>
        ${f.err ? `<div class="notice bad"><span>${J.esc(f.err)}</span></div>` : ''}
        <div class="row"><button class="btn small" type="submit" ${f.busy ? 'disabled' : ''}>${f.busy ? J.busyLabel('Saving…') : 'Save rules'}</button><button type="button" class="btn ghost small" data-act="adm.rulesCancel">Cancel</button></div></form></section>`;
  }

  function meter(label, used, allowed) {
    if (allowed == null) return `<div class="meter"><div class="spread"><span>${label}</span><span class="mono">${used}</span></div><span class="hint">No limit</span></div>`;
    const p = Math.min(100, Math.round((used / allowed) * 100));
    const cls = used >= allowed ? 'full' : p >= 80 ? 'near' : '';
    return `<div class="meter ${cls}"><div class="spread"><span>${label}</span><span class="mono">${used} / ${allowed}</span></div><div class="bar" role="img" aria-label="${used} of ${allowed}"><i style="width:${p}%"></i></div>${cls === 'full' ? '<span class="hint">Limit reached</span>' : cls === 'near' ? '<span class="hint">Close to the limit</span>' : ''}</div>`;
  }
  function planView(b, v) {
    const s = st();
    if (!s.plan) { if (!s.planLoading) { s.planLoading = true; setTimeout(loadPlan); } return `<div class="card row muted">${J.busyLabel('Loading…')}</div>`; }
    if (!canManage(v)) return '<div class="card empty"><p>Only managers and the owner can see the plan.</p></div>';
    const p = s.plan, L = p.limits || {}, u = p.usage, site = s.site, f = s.siteForm;
    const owner = v.role === 'owner';
    return `<div class="stack-lg">
      <section class="card stack"><div class="spread"><h2>${p.plan === 'free' ? 'Free plan' : 'Paid plan'}</h2>${p.plan === 'free' ? '<span class="chip">RM 0 a month</span>' : '<span class="chip confirmed">No limits</span>'}</div>
        ${p.plan === 'free' && (u.bookings_per_month / L.bookings_per_month >= 0.8 || u.resources >= L.resources) ? '<div class="notice warn"><span>You are close to some limits. When one is reached, nothing more of that kind can be added until next month (or until you remove something). We also email you about it.</span></div>' : ''}
        <div class="meters">${meter('Bookings this month', u.bookings_per_month, L.bookings_per_month)}${meter('Staff, tables and bays', u.resources, L.resources)}${meter('Services', u.services, L.services)}${meter('WhatsApp messages this month', u.whatsapp_per_month, L.whatsapp_per_month)}${meter('SMS this month', u.sms_per_month, L.sms_per_month)}</div>
        ${p.plan === 'free' ? '<p class="small">One website on the Free plan, with up to two working keys of each kind.</p>' : ''}
        ${p.plan === 'free' ? '<p class="hint">Emails to customers are never limited. To lift the limits, ask us to move you to a paid plan.</p>' : ''}</section>
      <section class="card stack"><h2>Your website</h2>
        ${site ? `<dl class="summary"><dt>Address</dt><dd class="mono">${J.esc(site.domain)}</dd><dt>Booking widget works on</dt><dd class="mono small">${site.allowed_origins.map(J.esc).join('<br>')}</dd>${site.every_days ? `<dt>Changes</dt><dd>Once every ${site.every_days} days${site.next_allowed_at ? ` · next change from <b>${T.dateLabel(site.next_allowed_at)}</b>` : ' · you can change it now'}</dd>` : ''}</dl>
        ${owner ? (f.open ? `<form class="stack" data-submit="adm.siteSave">${J.field({ id: 'st-domain', label: 'New website address', model: 'admin.siteForm.domain', placeholder: 'mynewshop.my', hint: 'www and non-www count as the same site.' })}
            ${f.err ? `<div class="notice bad"><span>${J.esc(f.err)}</span></div>` : ''}<div class="row"><button class="btn small" type="submit" ${f.busy ? 'disabled' : ''}>${f.busy ? J.busyLabel('Saving…') : 'Change address'}</button><button type="button" class="btn ghost small" data-act="adm.siteToggle">Cancel</button></div></form>`
          : '<div><button type="button" class="btn ghost small" data-act="adm.siteToggle">Change website address</button></div>') : '<p class="hint">Only the owner can change the website address.</p>'}` : J.busyLabel('Loading…')}</section>
      ${owner ? `<section class="card stack"><h2>Keys for your website</h2><p class="small muted">The WordPress plugin or your developer uses these to connect your website. Only the first characters are shown.</p>
        ${s.keys ? `<div class="scrollx"><table class="table"><thead><tr><th>Key</th><th>Kind</th><th>Created</th><th>State</th></tr></thead><tbody>${s.keys.map((k) => `<tr><td class="mono">${J.esc(k.prefix)}…</td><td>${k.kind === 'publishable' ? 'Website key' : 'Secret key'}</td><td>${T.dateLabel(k.created_at)}</td><td>${k.revoked_at ? '<span class="chip">Revoked</span>' : '<span class="chip confirmed">Working</span>'}</td></tr>`).join('')}</tbody></table></div>` : ''}
        ${s.newKeys ? `<div class="notice warn stack"><strong>Copy these now: they are shown only once</strong>
            <span class="label">Website key (goes into your web page)</span><div class="secret" id="key-pk">${J.esc(s.newKeys.publishable_key)}</div><div><button type="button" class="btn ghost small" data-act="copy" data-target="key-pk">Copy website key</button></div>
            <span class="label">Secret key (goes into the WordPress plugin; never share it)</span><div class="secret" id="key-sk">${J.esc(s.newKeys.secret_key)}</div><div><button type="button" class="btn ghost small" data-act="copy" data-target="key-sk">Copy secret key</button></div>
            ${s.newKeys.revoked ? `<span class="small">Your plan allows two working keys of each kind, so the ${s.newKeys.revoked} oldest stopped working. Paste the new secret key into the plugin now.</span>` : ''}</div>`
          : '<div><button type="button" class="btn ghost small" data-act="adm.newKeys">Get a new pair of keys</button></div>'}</section>` : ''}</div>`;
  }
  async function loadPlan() {
    const s = st(), id = J.state.biz;
    const [plan] = await J.call(() => api.planUsage(id));
    const [site] = await J.call(() => api.site(id));
    const [keys] = await J.call(() => api.keys(id));
    s.plan = plan; s.site = site; s.keys = keys; s.planLoading = false; J.render();
  }

  /* ------------------------------------------------------------------ the frame */
  function render() {
    const b = J.biz(), s = st(), v = viewer(b);
    const key = listKey(b, s, v);
    if ((s.tab === 'bookings' || s.tab === 'schedule') && s.listKey !== key && !s.loading) setTimeout(load);
    const tabs = [['bookings', 'Bookings'], ['schedule', 'Schedule']].concat(canManage(v) ? [['setup', 'Set-up'], ['plan', 'Plan & website']] : []);
    if (!tabs.some(([k]) => k === s.tab)) s.tab = 'bookings';
    const a = s.attn || { pending: [], flagged: [], conflicts: [] };
    const isToday = s.date === T.today();
    let main;
    if (s.tab === 'setup') main = setupView(b, v);
    else if (s.tab === 'plan') main = planView(b, v);
    else {
      const open = !!s.open;
      main = `${s.filter === 'all' ? `<div class="spread" style="margin-bottom:12px"><div class="datenav"><button type="button" class="back" data-act="adm.day" data-d="-1" aria-label="Previous day">←</button><span class="d">${isToday ? 'Today' : T.day(s.date, { weekday: 'long' })}<br><span class="muted small" style="font-family:var(--font-body);font-weight:500">${T.day(s.date, { day: 'numeric', month: 'long', year: 'numeric' })}</span></span><button type="button" class="back" data-act="adm.day" data-d="1" aria-label="Next day">→</button></div>
          ${isToday ? '' : '<button type="button" class="btn ghost small" data-act="adm.today">Back to today</button>'}</div>`
        : `<div class="spread" style="margin-bottom:12px"><h3>${s.filter === 'pending' ? 'Requests waiting for an answer' : s.filter === 'flagged' ? 'Bookings to review' : 'Appointments that clash'}</h3><button type="button" class="btn ghost small" data-act="adm.filter" data-f="all">Show the day view</button></div>`}
        <div class="adminbody ${open ? 'open' : ''}"><div style="min-width:0">${s.tab === 'schedule' && s.filter === 'all' ? boardView(b, v) : listView(b)}</div>${open ? drawer(b, v) : ''}</div>`;
    }
    return `<div class="page"><div class="wide">
      <div class="adminhead"><div class="stack" style="gap:4px"><span class="label">${J.esc(b.kind)} · admin</span><h1>${J.esc(b.name)}</h1></div>
        <span class="small muted">Signed in as <b>${J.esc(v.label)}</b>${v.staffId ? ' (sees own appointments only)' : ''}</span></div>
      <div class="tabs" role="tablist">${tabs.map(([k, t]) => `<button type="button" role="tab" aria-current="${s.tab === k}" data-act="adm.tab" data-tab="${k}">${t}</button>`).join('')}</div>
      ${s.tab === 'bookings' || s.tab === 'schedule' ? `<div class="attn">
        <button type="button" data-act="adm.filter" data-f="pending" aria-pressed="${s.filter === 'pending'}" ${a.pending.length ? '' : 'disabled'}><span class="n">${a.pending.length}</span> request${a.pending.length === 1 ? '' : 's'} to answer</button>
        <button type="button" data-act="adm.filter" data-f="flagged" aria-pressed="${s.filter === 'flagged'}" ${a.flagged.length ? '' : 'disabled'}><span class="n">${a.flagged.length}</span> booking${a.flagged.length === 1 ? '' : 's'} to review</button>
        ${a.conflicts.length ? `<button type="button" data-act="adm.filter" data-f="conflicts" aria-pressed="${s.filter === 'conflicts'}"><span class="n">${a.conflicts.length}</span> clash${a.conflicts.length === 1 ? '' : 'es'}</button>` : ''}</div>` : ''}
      ${main}</div></div>`;
  }

  /* ------------------------------------------------------------------ actions */
  async function openDetail(id) {
    const s = st();
    s.open = id; s.detail = null; s.err = null; s.move = null; s.extra = { addonId: '', result: null, busy: false }; J.render();
    const [d] = await J.call(() => api.adminBooking(J.state.biz, id, viewer(J.biz())));
    s.detail = d; J.render();
  }
  async function moveMonth() {
    const s = st(), b = J.biz(), x = s.detail, m = s.move;
    m.month = null; J.render();
    const today = T.today();
    const days = [];
    for (let i = 0; i < 14; i++) {
      const date = T.addDays(today, i);
      const [slots] = await J.call(() => api.moveOptions(b.id, x.id, date, m.kind === 'move' ? false : true, true));
      days.push({ date, available: !!(slots && slots.length), slot_count: slots ? slots.length : 0, closed: null, slots });
    }
    m.month = days;
    const first = days.find((d) => d.available);
    m.date = first ? first.date : null; m.slots = first ? first.slots : []; J.render();
  }

  Object.assign(J.actions, {
    'adm.tab'(ds) { const s = st(); s.tab = ds.tab; if (ds.tab === 'plan') s.plan = null; if (ds.tab === 'setup') s.hours = null; J.render(); },
    'adm.day'(ds) { const s = st(); s.date = T.addDays(s.date, Number(ds.d)); s.open = null; s.list = null; J.render(); },
    'adm.today'() { const s = st(); s.date = T.today(); s.open = null; s.list = null; J.render(); },
    'adm.filter'(ds) { const s = st(); s.filter = s.filter === ds.f ? 'all' : ds.f; s.list = null; s.open = null; J.render(); },
    'adm.open'(ds) { openDetail(ds.id); },
    'adm.close'() { const s = st(); s.open = null; s.detail = null; s.move = null; J.render(); },
    async 'adm.status'(ds) {
      const s = st(), b = J.biz();
      s.busy = ds.to; s.err = null; J.render();
      const [d, err] = await J.call(() => api.transition(b.id, s.open, ds.to, viewer(b)));
      s.busy = null;
      if (err) { s.err = err.detail; J.render(); return; }
      s.detail = d; J.toast(`${d.customer.name}: ${J.STATUS[d.status]}`); reload();
    },
    async 'adm.clearFlags'() { const s = st(); const [d] = await J.call(() => api.clearFlags(J.state.biz, s.open)); s.detail = d; J.toast('Kept as it is'); reload(); },
    async 'adm.resolve'() { const s = st(); const [d] = await J.call(() => api.resolveConflict(J.state.biz, s.open)); s.detail = d; J.toast('Marked as sorted'); reload(); },
    'adm.moveOpen'(ds) { const s = st(); s.move = { kind: ds.kind, date: null, month: null, slots: null, slot: null, err: null }; moveMonth(); },
    'adm.moveClose'() { st().move = null; J.render(); },
    'adm.moveDate'(ds, el) { if (el.getAttribute('aria-disabled') === 'true') return; const m = st().move; m.date = ds.date; m.slot = null; m.slots = (m.month.find((d) => d.date === ds.date) || {}).slots || []; J.render(); },
    'adm.moveSlot'(ds) { st().move.slot = ds.start; J.render(); },
    async 'adm.moveConfirm'() {
      const s = st(), m = s.move;
      m.busy = true; J.render();
      const [, err] = await J.call(() => api.reschedule(J.state.biz, s.open, { start_at: m.slot, any_available: false }, null));
      m.busy = false;
      if (err) { m.err = err.detail; J.render(); return; }
      s.move = null; J.toast('Moved. The customer has been told.'); s.open = null; reload();
    },
    async 'adm.resumeConfirm'() {
      const s = st(), m = s.move;
      m.busy = true; J.render();
      const [d, err] = await J.call(() => api.resume(J.state.biz, s.open, m.slot));
      m.busy = false;
      if (err) { m.err = err.detail; J.render(); return; }
      s.move = null; s.detail = d; J.toast('Job booked in again'); reload();
    },
    async 'adm.addExtra'() {
      const s = st();
      s.extra.busy = true; J.render();
      const [res, err] = await J.call(() => api.addToTicket(J.state.biz, s.open, s.extra.addonId));
      s.extra.busy = false;
      if (err) { s.err = err.detail; J.render(); return; }
      s.extra.result = res; s.extra.addonId = ''; s.detail = res.booking; reload();
    },
    async 'adm.addService'() {
      const s = st(), f = s.svcForm;
      const [, err] = await J.call(() => api.addService(J.state.biz, {
        name: f.name, duration_minutes: Number(f.duration), price: Number(f.price || 0), price_after_inspection: !!f.afterInspection, location_type: f.where, description: f.description.trim() || null,
        travel_buffer_minutes: f.where === 'on_site' ? Number(f.travel || 0) : 0, is_addon: !!f.extra,
      }));
      if (err) { f.err = err.detail; J.render(); return; }
      s.svcForm = { name: '', duration: '60', price: '', afterInspection: false, where: 'in_office', description: '', travel: '30', extra: false, err: null }; J.toast('Service added'); J.render();
    },
    async 'adm.addProduct'() {
      const s = st(), f = s.prodForm;
      const [, err] = await J.call(() => api.addProduct(J.state.biz, { name: f.name, price: Number(f.price || 0), stock_qty: Number(f.stock || 0) }));
      if (err) { f.err = err.detail; J.render(); return; }
      s.prodForm = { name: '', price: '', stock: '10', err: null }; J.toast('Product added'); J.render();
    },
    async 'adm.addCar'() {
      const s = st(), f = s.carForm;
      const [, err] = await J.call(() => api.addVehicle(J.state.biz, { make: f.make, model: f.model, year_from: f.from ? Number(f.from) : null, year_to: f.to ? Number(f.to) : null }));
      if (err) { f.err = err.detail; J.render(); return; }
      s.carForm = { make: '', model: '', from: '', to: '', err: null }; J.toast('Car model added'); J.render();
    },
    async 'adm.removeCar'(ds) {
      const [, err] = await J.call(() => api.removeVehicle(J.state.biz, ds.id));
      J.toast(err ? err.detail : 'Car model removed'); J.render();
    },
    async 'adm.addRes'() {
      const s = st(), f = s.resForm, b = J.biz();
      const type = b.type === 'restaurant' ? 'table' : b.type === 'automotive' ? f.type : 'staff';
      const [, err] = await J.call(() => api.addResource(b.id, { name: f.name, type, capacity: Number(f.capacity) }));
      if (err) { f.err = { plan: err.status === 402, text: err.detail }; J.render(); return; }
      s.resForm = { name: '', type: 'staff', capacity: '4', err: null }; J.toast('Added'); J.render();
    },
    async 'adm.toggleRes'(ds) {
      const s = st();
      const [res, err] = await J.call(() => api.setResourceStatus(J.state.biz, ds.id, ds.to));
      if (err) { s.resForm.err = { plan: err.status === 402, text: err.detail }; J.render(); return; }
      J.toast(ds.to === 'inactive' ? (res.flagged ? `Switched off. ${res.flagged} upcoming booking${res.flagged === 1 ? ' is' : 's are'} marked for review.` : 'Switched off') : 'Switched on');
      reload();
    },
    'adm.hour'(ds, el) { const s = st(); const [h, m] = el.value.split(':').map(Number); if (!isNaN(h)) s.hours[ds.i][ds.k] = h * 60 + m; },
    'adm.closedDay'(ds, el) { const s = st(); s.hours[ds.i] = el.checked ? null : { o: 540, c: 1080 }; J.render(); },
    async 'adm.saveHours'() {
      const s = st();
      if (s.hours.some((h) => h && h.o === h.c)) { s.hoursErr = 'Opening and closing times must differ.'; J.render(); return; }
      s.hoursErr = null;
      await J.call(() => api.setHours(J.state.biz, s.hours.map((h) => (h ? [h.o, h.c] : null))));
      J.toast('Opening hours saved'); reload();
    },
    async 'adm.placeSave'() {
      const b = J.biz(), f = st().place;
      const radius = Number(f.radius) || (b.place && b.place.radius_km) || 25;
      f.busy = true; f.err = null; f.saved = null; J.render();
      const [res, err] = await J.call(() => api.setLocationFromLink(b.id, f.url.trim(), radius));
      f.busy = false;
      if (err) { f.err = err.detail; J.render(); return; }
      f.url = ''; f.saved = `${res.latitude.toFixed(5)}, ${res.longitude.toFixed(5)}${res.formatted_address ? ' (' + J.esc(res.formatted_address) + ')' : ''}`;
      J.toast('Location saved'); J.render();
    },
    'adm.rulesEdit'() {
      const p = J.biz().policy;
      st().rules = { notice_h: String(p.min_notice_min / 60), window_days: String(p.window_days), cancel_notice_h: String(p.cancel_notice_h), default_status: p.default_status, pending_timeout_min: String(p.pending_timeout_min), err: null, busy: false };
      J.render();
    },
    'adm.rulesCancel'() { st().rules = null; J.render(); },
    async 'adm.rulesSave'() {
      const f = st().rules;
      const n = { notice: Number(f.notice_h), win: Number(f.window_days), cancel: Number(f.cancel_notice_h), hold: Number(f.pending_timeout_min) };
      if (![n.notice, n.win, n.cancel, n.hold].every((x) => isFinite(x) && x >= 0) || n.win < 1) { f.err = 'Use numbers: notice and cancelling in hours, how far ahead in days (at least 1).'; J.render(); return; }
      f.busy = true; f.err = null; J.render();
      const [, err] = await J.call(() => api.setPolicy(J.state.biz, { min_notice_min: Math.round(n.notice * 60), window_days: Math.round(n.win), cancel_notice_h: Math.round(n.cancel), default_status: f.default_status, pending_timeout_min: Math.round(n.hold) || 30 }));
      f.busy = false;
      if (err) { f.err = err.detail; J.render(); return; }
      st().rules = null; J.toast('Booking rules saved'); J.render();
    },
    'adm.siteToggle'() { const f = st().siteForm; f.open = !f.open; f.err = null; J.render(); },
    async 'adm.siteSave'() {
      const s = st(), f = s.siteForm;
      f.busy = true; f.err = null; J.render();
      const [site, err] = await J.call(() => api.changeSite(J.state.biz, f.domain));
      f.busy = false;
      if (err) { f.err = err.detail; J.render(); return; }
      s.site = site; f.open = false; f.domain = ''; J.toast('Website address changed'); J.render();
    },
    async 'adm.newKeys'() {
      const s = st();
      const [keys] = await J.call(() => api.recoverKeys(J.state.biz));
      s.newKeys = keys; const [list] = await J.call(() => api.keys(J.state.biz)); s.keys = list; J.render();
    },
    copy(ds) {
      const el = document.getElementById(ds.target);
      const text = el.textContent;
      const select = () => { const r = document.createRange(); r.selectNodeContents(el); const sel = window.getSelection(); sel.removeAllRanges(); sel.addRange(r); J.toast('Selected: press Ctrl+C (or Cmd+C) to copy'); };
      try { navigator.clipboard.writeText(text).then(() => J.toast('Copied'), select); } catch (e) { select(); }
    },
  });

  J.views.admin = { init, render };
})();
