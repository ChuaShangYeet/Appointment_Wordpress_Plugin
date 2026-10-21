/* The booking widget a visitor uses on the business's website (backend: GET /public/services, /resources, /availability,
   /availability/month, POST /public/geocode and POST /public/bookings). The steps depend on the kind of business. */
(function () {
  const J = window.JS;
  const T = J.T;
  const api = J.api;

  const STEPS = {
    spa: ['service', 'staff', 'time', 'details', 'review'],
    salon: ['service', 'addons', 'staff', 'time', 'details', 'review'],
    restaurant: ['party', 'time', 'details', 'review'],
    automotive: ['vehicle', 'service', 'staff', 'time', 'details', 'review'],
    professional: ['service', 'address', 'staff', 'time', 'details', 'review'],
  };
  const LABEL = { service: 'Service', addons: 'Extras', staff: 'Who', time: 'Time', details: 'Your details', review: 'Confirm', party: 'Table', vehicle: 'Car', address: 'Address' };

  function init() {
    return {
      step: null, serviceId: null, serviceIds: [], addonIds: [], resourceId: 'any',
      month: null, monthKey: null, loadingMonth: false, date: null, slots: null, slotsKey: null, loadingSlots: false, notice: [], slot: null,
      party: 2, tableType: 'normal', separate: false,
      vehicle: { mode: null, savedId: null, plate: '', modelId: '', year: '' }, vehicleErr: null,
      address: { text: '', result: null, error: null, checking: false, savedId: null },
      details: { name: '', phone: '', email: '', gender: '', notes: '', request: '' }, fieldErrors: {},
      acceptSpend: false, ackLead: false, human: 'no', busy: false, error: null, taken: null, result: null,
    };
  }
  const st = () => J.state.book;
  function stepsFor(b) {
    let s = STEPS[b.type].slice();
    if (b.type === 'professional') {
      const sv = b.services.find((x) => x.id === st().serviceId);
      if (!sv || sv.location_type !== 'on_site') s = s.filter((x) => x !== 'address');
    }
    return s;
  }
  function serviceIds(b) {
    const s = st();
    if (b.type === 'restaurant') return [];
    if (b.type === 'automotive') return s.serviceIds;
    return s.serviceId ? [s.serviceId, ...(b.type === 'salon' ? s.addonIds : [])] : [];
  }
  function query(b) {
    const s = st();
    const at = stepsFor(b).includes('address') && s.address.result ? s.address.result : {};
    return { service_ids: serviceIds(b), resource_id: s.resourceId === 'any' ? null : s.resourceId, party_size: s.party, table_type: s.tableType, allow_separate_seating: s.separate, latitude: at.latitude, longitude: at.longitude };
  }
  const services = (b) => serviceIds(b).map((id) => b.services.find((x) => x.id === id)).filter(Boolean);
  const total = (b) => services(b).reduce((t, x) => t + x.price, 0);
  const duration = (b) => services(b).reduce((t, x) => t + x.duration_minutes, 0);
  const session = () => J.state.account && J.state.account.session;
  const me = () => J.state.account && J.state.account.me;

  /* ------------------------------------------------------------------ loading availability */
  async function loadMonth() {
    const b = J.biz(), s = st();
    const key = JSON.stringify(query(b));
    s.loadingMonth = true; J.render();
    const [month, err] = await J.call(() => api.month(b.id, query(b)));
    s.loadingMonth = false;
    if (!month) { s.monthKey = key; s.month = null; s.monthErr = err ? err.detail : 'Could not load the free days'; J.render(); return; } // (asked once: no retry loop)
    s.monthErr = null;
    s.month = month; s.monthKey = key;
    const still = month.find((d) => d.date === s.date && d.available);
    if (!still) { s.date = (month.find((d) => d.available) || {}).date || null; s.slot = null; }
    await loadSlots();
  }
  async function loadSlots() {
    const b = J.biz(), s = st();
    if (!s.date) { J.render(); return; }
    s.loadingSlots = true; J.render();
    const [res] = await J.call(() => api.availability(b.id, s.date, query(b)));
    s.loadingSlots = false;
    if (res) { s.slots = res.slots; s.notice = res.lead_time_notice; s.slotsKey = JSON.stringify(query(b)) + s.date; if (s.slot && !res.slots.some((x) => x.start_at === s.slot.start_at)) s.slot = null; }
    J.render();
  }

  /* ------------------------------------------------------------------ steps */
  function serviceStep(b) {
    const s = st();
    if (b.type === 'automotive') {
      const lead = s.serviceIds.flatMap((id) => (b.services.find((x) => x.id === id) || {}).parts || []);
      return `<div class="stack"><h2>What does the car need?</h2><p class="muted">Pick one or more. We do them one after another, in the order you pick them.</p>
        <div class="choices">${b.services.map((x) => {
          const n = s.serviceIds.indexOf(x.id);
          return `<button type="button" class="choice" data-act="book.toggleService" data-id="${x.id}" aria-pressed="${n >= 0}">
            <span class="name">${n >= 0 ? `<span class="mono">${n + 1}.</span> ` : ''}${J.esc(x.name)}</span><span class="price">${J.price(x)}</span>
            <span class="desc">${J.esc(x.description)}</span><span class="facts"><span>About ${J.mins(x.duration_minutes)}</span>${x.parts ? '<span>Parts ordered in</span>' : ''}</span></button>`;
        }).join('')}</div>
        ${lead.length ? `<div class="notice warn"><strong>Some parts have to be ordered</strong><span>${lead.map((p) => `${J.esc(p.name)}: about ${p.estimated_lead_time_days} days`).join('; ')}. You can still book now; we will ask you to agree to the wait before you confirm.</span></div>` : ''}</div>`;
    }
    const list = b.services.filter((x) => !x.is_addon);
    if (!list.length || (b.type === 'restaurant' && !b.resources.length)) return `<div class="empty"><h2>Not taking online bookings yet</h2><p>${J.esc(b.name)} is still setting up. Check back soon${b.phone ? ', or call ' + J.esc(b.phone) : ''}.</p></div>`;
    return `<div class="stack"><h2>${b.type === 'salon' ? 'Choose your main service' : 'Choose a service'}</h2>
      <div class="choices">${list.map((x) => `<button type="button" class="choice" data-act="book.pickService" data-id="${x.id}" aria-pressed="${s.serviceId === x.id}">
        <span class="name">${J.esc(x.name)}</span><span class="price">${J.price(x)}</span>
        ${x.description ? `<span class="desc">${J.esc(x.description)}</span>` : ''}
        <span class="facts"><span>About ${J.mins(x.duration_minutes)}</span>${x.location_type === 'on_site' ? '<span>At your home</span>' : x.location_type === 'in_office' && b.type === 'professional' ? '<span>At our office</span>' : ''}${x.service_category === 'inspection' ? '<span>Inspection and quote</span>' : ''}</span></button>`).join('')}</div></div>`;
  }

  function addonsStep(b) {
    const s = st();
    const adds = b.services.filter((x) => x.is_addon);
    const base = b.services.find((x) => x.id === s.serviceId);
    return `<div class="stack"><h2>Add anything?</h2><p class="muted">Extras are done in the same visit, straight after your ${J.esc(base ? base.name.toLowerCase() : 'service')}. They add time to your appointment.</p>
      <div class="choices">${adds.map((x) => `<button type="button" class="choice" data-act="book.toggleAddon" data-id="${x.id}" aria-pressed="${s.addonIds.includes(x.id)}">
        <span class="name">${J.esc(x.name)}</span><span class="price">+ ${J.price(x)}</span><span class="desc">${J.esc(x.description)}</span><span class="facts"><span>+ ${J.mins(x.duration_minutes)}</span></span></button>`).join('')}</div>
      ${(b.products || []).length ? `<div class="card flat stack"><span class="label">On our shelf (buy at the salon)</span>${b.products.map((p) => `<div class="spread small"><span>${J.esc(p.name)}</span><span class="muted">${J.money(p.price)} · ${p.stock_status === 'in_stock' ? 'In stock' : 'Out of stock'}</span></div>`).join('')}</div>` : ''}</div>`;
  }

  function staffStep(b) {
    const s = st();
    const chosen = services(b).filter((x) => !x.is_addon);
    const staff = b.resources.filter((r) => r.type === 'staff' && r.status !== 'inactive' && chosen.every((x) => !x.staff || x.staff.includes(r.id)));
    const who = J.WHO[b.who] || 'staff member';
    return `<div class="stack"><h2>Who would you like?</h2>
      <div class="choices">
        <button type="button" class="choice" data-act="book.pickStaff" data-id="any" aria-pressed="${s.resourceId === 'any'}"><span class="name">First available ${who}</span><span></span><span class="desc">We pick whoever is free, so you see the most times.</span></button>
        ${staff.map((r) => `<button type="button" class="choice" data-act="book.pickStaff" data-id="${r.id}" aria-pressed="${s.resourceId === r.id}"><span class="name">${J.esc(r.name)}</span><span></span>${r.note ? `<span class="desc">${J.esc(r.note)}</span>` : ''}</button>`).join('')}
      </div></div>`;
  }

  function partyStep(b) {
    const s = st();
    const vipFrom = Math.min(...b.resources.filter((r) => (r.attributes || {}).is_vip).map((r) => r.attributes.minimum_spend || 0));
    return `<div class="stack-lg"><h2>Book a table</h2>
      <div class="stack"><span class="label">How many people?</span>
        <div class="row"><button type="button" class="btn ghost" data-act="book.party" data-d="-1" ${s.party <= 1 ? 'disabled' : ''} aria-label="One fewer">−</button>
        <span class="mono" style="font-size:1.6rem;min-width:3ch;text-align:center" aria-live="polite">${s.party}</span>
        <button type="button" class="btn ghost" data-act="book.party" data-d="1" ${s.party >= 20 ? 'disabled' : ''} aria-label="One more">+</button>
        <span class="muted">${s.party === 1 ? 'person' : 'people'}</span></div></div>
      <div class="stack"><span class="label">Where would you like to sit?</span>
        <div class="seg" role="group" aria-label="Seating"><button type="button" data-act="book.table" data-t="normal" aria-pressed="${s.tableType === 'normal'}">Dining room</button><button type="button" data-act="book.table" data-t="vip" aria-pressed="${s.tableType === 'vip'}">Private room</button></div>
        ${s.tableType === 'vip' && isFinite(vipFrom) ? `<p class="hint">Private rooms have a minimum spend, from ${J.money(vipFrom)}. You will see the amount before you confirm.</p>` : ''}</div>
      <label class="check" for="b-separate"><input id="b-separate" type="checkbox" data-model="book.separate" ${s.separate ? 'checked' : ''}><span><b>We don't mind sitting at separate tables</b><br><span class="hint">If no single table is big enough, we can seat your group at tables next to each other.</span></span></label></div>`;
  }

  function vehicleStep(b) {
    const s = st(), v = s.vehicle;
    const saved = (me() && !session().limited && me().vehicles) || [];
    if (v.mode === null) v.mode = saved.length ? 'saved' : 'new';
    const models = b.supportedVehicles.map((m) => [m.id, `${m.make} ${m.model} (${m.year_from}${m.year_to ? '–' + m.year_to : ' onwards'})`]);
    return `<div class="stack-lg"><h2>Which car?</h2>
      ${saved.length ? `<div class="choices">${saved.map((x) => `<button type="button" class="choice" data-act="book.pickVehicle" data-id="${x.id}" aria-pressed="${v.mode === 'saved' && v.savedId === x.id}"><span class="name mono">${J.esc(x.plate_no)}</span><span></span><span class="desc">${J.esc(x.make)} ${J.esc(x.model)}, ${x.year}</span></button>`).join('')}
        <button type="button" class="choice" data-act="book.pickVehicle" data-id="new" aria-pressed="${v.mode === 'new'}"><span class="name">A different car</span><span></span></button></div>` : ''}
      ${v.mode === 'new' ? `<div class="stack">
        ${J.field({ id: 'b-plate', label: 'Number plate', model: 'book.vehicle.plate', placeholder: 'WVX 2213', attrs: 'autocomplete="off" style="text-transform:uppercase"' })}
        ${J.field({ id: 'b-model', label: 'Make and model', model: 'book.vehicle.modelId', options: [['', 'Choose your car']].concat(models), hint: 'We only service the models listed.' })}
        ${J.field({ id: 'b-year', label: 'Year', model: 'book.vehicle.year', type: 'number', placeholder: '2019', attrs: 'inputmode="numeric" min="1990" max="2026"' })}
        ${s.vehicleErr ? `<div class="notice bad"><strong>We can't take this car</strong><span>${J.esc(s.vehicleErr)}</span></div>` : ''}</div>` : ''}</div>`;
  }

  function addressStep(b) {
    const s = st(), a = s.address;
    const saved = (me() && !session().limited && me().addresses) || [];
    const r = a.result;
    return `<div class="stack-lg"><h2>Where should we come?</h2><p class="muted">${J.esc(b.name)} visits homes within ${b.location.radius_km} km of ${J.esc(b.location.base)}.</p>
      ${saved.length ? `<div class="choices">${saved.map((x) => `<button type="button" class="choice" data-act="book.pickAddress" data-id="${x.id}" aria-pressed="${a.savedId === x.id}"><span class="name">${J.esc(x.line1)}</span><span></span><span class="desc">${x.distance_km} km away · saved address</span></button>`).join('')}</div><span class="label">Or type another address</span>` : ''}
      <form class="stack" data-submit="book.checkAddress">
        ${J.field({ id: 'b-address', label: 'Address', model: 'book.address.text', placeholder: 'House number, street, area', hint: 'Include the house or unit number, so we find the right door.', attrs: 'autocomplete="street-address"' })}
        <div><button class="btn ghost" type="submit" ${a.checking ? 'disabled' : ''}>${a.checking ? J.busyLabel('Checking…') : 'Check this address'}</button></div>
      </form>
      ${a.error ? `<div class="notice bad"><strong>We couldn't find that address</strong><span>${J.esc(a.error)}</span></div>` : ''}
      ${r && !a.savedId ? (!r.exact ? `<div class="notice warn"><strong>That's an area, not an exact address</strong><span>We found “${J.esc(r.formatted_address)}”. Add the house number and street so the technician can find you.</span></div>`
        : r.within_service_area ? `<div class="notice ok"><strong>We come to you</strong><span>${J.esc(r.formatted_address)} · ${r.distance_km} km from us</span></div>`
        : `<div class="notice bad"><strong>That's outside our service area</strong><span>${J.esc(r.formatted_address)} is ${r.distance_km} km away; we go up to ${r.service_radius_km} km. Call us on ${J.esc(b.phone)} and we may still help.</span></div>`) : ''}</div>`;
  }

  function dayStrip(s, month, act = 'book.pickDate') {
    if (!month) return `<div class="row muted">${J.busyLabel('Finding free days…')}</div>`;
    return `<div class="daystrip" data-keep-scroll="${act}" role="group" aria-label="Dates">${month.map((d) => {
      const cls = d.closed ? 'closed' : d.available ? '' : 'full';
      const note = d.closed ? 'Closed' : d.available ? `${d.slot_count} free` : 'No times';
      return `<button type="button" class="day ${cls}" data-act="${act}" data-date="${d.date}" aria-pressed="${s.date === d.date}" ${d.available ? '' : 'aria-disabled="true"'} title="${J.esc(d.closed || '')}">
        <span class="dw">${T.day(d.date, { weekday: 'short' })}</span><span class="dn">${T.day(d.date, { day: 'numeric' })}</span><span class="dc">${note}</span></button>`;
    }).join('')}</div>`;
  }

  function slotSub(b, x) {
    if (b.type === 'restaurant') {
      const names = x.resource_ids.map((id) => api.staffName(b.id, id));
      return `${x.combined ? names.length + ' tables together' : J.esc(names[0])}${x.minimum_spend ? ' · min. ' + J.money(x.minimum_spend) : ''}`;
    }
    if (x.travel_minutes) return 'Arrival time';
    return '';
  }
  function slotGrid(b, slots, current, act = 'book.pickSlot') {
    const groups = [['Morning', 0, 720], ['Afternoon', 720, 1020], ['Evening', 1020, 1440]];
    return groups.map(([name, lo, hi]) => {
      const list = slots.filter((x) => T.minutes(x.start_at) >= lo && T.minutes(x.start_at) < hi);
      if (!list.length) return '';
      return `<div class="slotgroup"><span class="label">${name}</span><div class="slots">${list.map((x) => `<button type="button" class="slot" data-act="${act}" data-start="${x.start_at}" aria-pressed="${current === x.start_at}"><span class="t">${T.time(x.start_at)}</span>${slotSub(b, x) ? `<span class="s">${slotSub(b, x)}</span>` : ''}</button>`).join('')}</div></div>`;
    }).join('');
  }

  function timeStep(b) {
    const s = st();
    const key = JSON.stringify(query(b));
    if (s.monthKey !== key && !s.loadingMonth) setTimeout(loadMonth);
    const lead = s.notice && s.notice.length ? `<div class="notice warn"><strong>Parts have to be ordered</strong><span>${s.notice.map((p) => `${J.esc(p.name)}: about ${p.estimated_lead_time_days} days`).join('; ')}. If they are not in by your appointment we will call you to move it.</span></div>` : '';
    let body = '';
    if (s.monthErr && !s.loadingMonth) return `<div class="stack-lg"><h2>When?</h2><div class="notice bad"><strong>We couldn't load the free times</strong><span>${J.esc(s.monthErr)}</span></div><div><button type="button" class="btn ghost" data-act="book.retry">Try again</button></div></div>`;
    if (s.loadingSlots || s.loadingMonth) body = `<div class="row muted">${J.busyLabel('Finding free times…')}</div>`;
    else if (s.month && !s.month.some((d) => d.available)) body = `<div class="empty"><h3>No free times in the next three weeks</h3><p>Try ${s.resourceId !== 'any' ? 'another ' + (J.WHO[b.who] || 'staff member') + ', or ' : ''}call ${J.esc(b.phone)}.</p></div>`;
    else if (s.slots) body = s.slots.length ? slotGrid(b, s.slots, s.slot && s.slot.start_at) : `<div class="empty"><p>No free times on ${T.day(s.date, { weekday: 'long', day: 'numeric', month: 'long' })}. Pick another day.</p></div>`;
    return `<div class="stack-lg"><h2>When?</h2>${lead}${dayStrip(s, s.month)}${body}
      <p class="hint">Times are in Malaysia time. ${b.type === 'professional' && services(b)[0] && services(b)[0].location_type === 'on_site' ? 'The time shown is when the technician arrives.' : ''}</p></div>`;
  }

  function detailsStep(b) {
    const s = st(), e = s.fieldErrors;
    const signed = session();
    return `<div class="stack-lg"><h2>Your details</h2>
      ${signed ? `<div class="notice info"><span>Booking as <b>${J.esc(s.details.name || 'you')}</b>, signed in with ${J.esc(J.fmt.prettyPhone(signed.phone))}.</span></div>` : `<p class="muted">Already booked with us? <button type="button" class="btn link" data-act="nav" data-view="account">Sign in</button> to use your saved details.</p>`}
      <div class="stack">
        ${J.field({ id: 'b-name', label: 'Name', model: 'book.details.name', err: e.name, attrs: 'autocomplete="name"' })}
        ${J.field({ id: 'b-phone', label: 'Mobile number', model: 'book.details.phone', type: 'tel', err: e.phone, placeholder: '012-345 6789', hint: 'We send updates to this number. Use the same number to manage your booking later.', attrs: 'autocomplete="tel" inputmode="tel"' })}
        ${J.field({ id: 'b-email', label: 'Email', model: 'book.details.email', type: 'email', optional: true, err: e.email, hint: 'For the confirmation and reminders.', attrs: 'autocomplete="email"' })}
        ${b.collectsGender ? J.field({ id: 'b-gender', label: 'Gender', model: 'book.details.gender', optional: true, options: [['', 'Prefer not to say'], ['female', 'Female'], ['male', 'Male'], ['other', 'Other']], hint: 'Some guests prefer a therapist of the same gender. Only staff see this.' }) : ''}
        ${b.healthNotes ? J.field({ id: 'b-notes', label: 'Health notes', model: 'book.details.notes', area: true, optional: true, placeholder: 'Injuries, pregnancy, allergies to oils…', hint: 'Kept private: only the owner and manager can read these.' }) : ''}
        ${J.field({ id: 'b-request', label: b.type === 'restaurant' ? 'Special requests' : b.type === 'automotive' ? 'Anything we should know about the car?' : 'Anything we should know?', model: 'book.details.request', area: true, optional: true, placeholder: b.type === 'restaurant' ? 'Birthday, high chair, dietary needs…' : b.type === 'automotive' ? 'Noises, warning lights…' : '' })}
      </div></div>`;
  }

  const PAY_AT = { spa: 'pay at the spa', salon: 'pay at the salon', automotive: 'pay when you collect the car', professional: 'pay after the visit' };
  function summaryRows(b, booked) {
    const s = st();
    const rows = [];
    if (b.type === 'restaurant') rows.push(['Table', `${s.party} ${s.party === 1 ? 'person' : 'people'} · ${s.tableType === 'vip' ? 'Private room' : 'Dining room'}`]);
    else rows.push(['Service', services(b).map((x) => J.esc(x.name)).join(' + ') + ` <span class="muted">(about ${J.mins(duration(b))})</span>`]);
    if (b.type !== 'restaurant') {
      const people = booked ? [...new Set(booked.allocations.map((a) => a.rid))].filter((id) => (b.resources.find((r) => r.id === id) || {}).type === 'staff').map((id) => api.staffName(b.id, id)) : [];
      rows.push(['With', people.length ? J.esc(people.join(', ')) : s.resourceId === 'any' ? `First available ${J.WHO[b.who] || ''}` : J.esc(api.staffName(b.id, s.resourceId))]);
    }
    if (s.slot) rows.push(['When', `${T.day(T.key(s.slot.start_at), { weekday: 'long', day: 'numeric', month: 'long' })}, ${T.time(s.slot.start_at)}`]);
    if (b.type === 'restaurant' && booked) rows.push(['Seating', J.esc(booked.allocations.map((a) => api.staffName(b.id, a.rid)).join(' + ')) + (booked.minimum_spend_agreed ? ` · min. ${J.money(booked.minimum_spend_agreed)}` : '')]);
    else if (b.type === 'restaurant' && s.slot) rows.push(['Seating', slotSub(b, s.slot)]);
    if (b.type === 'automotive') rows.push(['Car', J.esc(vehicleText(b))]);
    if (b.type === 'professional' && s.address.result && stepsFor(b).includes('address')) rows.push(['Address', J.esc(s.address.result.formatted_address)]);
    rows.push(['Contact', `${J.esc(s.details.name)}<br><span class="muted">${J.esc(s.details.phone)}${s.details.email ? ' · ' + J.esc(s.details.email) : ''}</span>`]);
    if (s.details.request) rows.push(['Note', J.esc(s.details.request)]);
    if (s.details.notes && b.healthNotes) rows.push(['Health notes', 'Added <span class="muted">· private to the owner and manager</span>']);
    if (b.type !== 'restaurant') rows.push(['Price', `${J.priceTotal(services(b))} <span class="muted">· ${services(b).some((x) => x.price_after_inspection) ? 'we tell you the final price once we have looked, before any work starts; ' : ''}${PAY_AT[b.type] || 'pay on the day'}</span>`]);
    return `<dl class="summary">${rows.map(([k, v]) => `<dt>${k}</dt><dd>${v}</dd>`).join('')}</dl>`;
  }
  function vehicleText(b) {
    const v = st().vehicle;
    if (v.mode === 'saved') { const x = (me().vehicles || []).find((y) => y.id === v.savedId); return x ? `${x.plate_no} · ${x.make} ${x.model}, ${x.year}` : ''; }
    const m = b.supportedVehicles.find((x) => x.id === v.modelId);
    return `${v.plate.toUpperCase()} · ${m ? m.make + ' ' + m.model : ''}${v.year ? ', ' + v.year : ''}`;
  }

  function reviewStep(b) {
    const s = st();
    const pending = J.state.scenario === 'needs_approval' || b.policy.default_status === 'pending';
    const start = s.slot && Date.parse(s.slot.start_at);
    const cancelBy = start && start - b.policy.cancel_notice_h * 3600000;
    const lead = s.notice && s.notice.length;
    const spend = s.slot && s.slot.minimum_spend;
    return `<div class="stack-lg"><h2>Check and confirm</h2>
      ${s.taken ? `<div class="notice bad"><strong>Someone just booked ${T.time(s.taken.time)}</strong><span>Pick another time on ${T.dateLabel(s.taken.time)}:</span></div>
        <div class="card flat">${s.taken.slots.length ? slotGrid(b, s.taken.slots, null, 'book.pickTakenSlot') : '<p class="muted">Nothing else is free that day. Go back and choose another day.</p>'}</div>` : ''}
      ${s.error ? `<div class="notice bad"><strong>${J.esc(s.error.title)}</strong><span>${J.esc(s.error.text)}</span></div>` : ''}
      <div class="card">${summaryRows(b)}</div>
      <div class="notice info"><span>${pending ? `<b>${J.esc(b.name)} confirms every booking by hand.</b> We hold your ${b.type === 'restaurant' ? 'table' : 'time'} for ${b.policy.pending_timeout_min} minutes while they look at it, and message you when they answer.<br>` : ''}
        ${!cancelBy ? '' : pending ? 'You can withdraw the request any time before it is confirmed.' : cancelBy > Date.now() ? `Free to cancel or move until <b>${T.when(cancelBy)}</b> (${b.policy.cancel_notice_h} hours before). After that, please call ${J.esc(b.phone)}.`
          : `This is less than ${b.policy.cancel_notice_h} hours away, so once booked it can't be cancelled or moved online. If your plans change, call ${J.esc(b.phone)}.`}</span></div>
      ${spend ? `<label class="check" for="b-spend"><input id="b-spend" type="checkbox" data-model="book.acceptSpend" ${s.acceptSpend ? 'checked' : ''}><span>I agree to a <b>minimum spend of ${J.money(spend)}</b> for the private room.</span></label>` : ''}
      ${lead ? `<label class="check" for="b-lead"><input id="b-lead" type="checkbox" data-model="book.ackLead" ${s.ackLead ? 'checked' : ''}><span>I understand some parts have to be ordered (about ${Math.max(...s.notice.map((p) => p.estimated_lead_time_days))} days) and my appointment may move if they arrive late.</span></label>` : ''}
      <div class="human ${s.human === 'ok' ? 'ok' : ''}"><button type="button" data-act="book.human" aria-label="Confirm you are a person" ${s.human !== 'no' ? 'disabled' : ''}>${s.human === 'ok' ? '✓' : s.human === 'checking' ? '<span class="spinner"></span>' : ''}</button><span>${s.human === 'ok' ? 'Done' : "I'm a person, not a bot"}</span><span class="who">human check</span></div></div>`;
  }

  function doneStep(b) {
    const x = st().result;
    const pending = x.status === 'pending';
    return `<div class="stack-lg"><div class="ticket"><div class="top ${pending ? 'pending' : ''}">
        <span class="label" style="color:inherit;opacity:.8">${pending ? 'Request sent' : 'Booked'}</span>
        <h2>${pending ? 'Waiting for ' + J.esc(b.name) + ' to confirm' : "You're booked in"}</h2>
        <span class="ref">REF ${J.esc(x.ref)}</span></div>
      <div class="body stack">${summaryRows(b, x)}
        ${pending ? `<div class="notice warn"><span>Your ${b.type === 'restaurant' ? 'table' : 'time'} is held until <b>${T.time(x.hold_expires_at)}</b>. If ${J.esc(b.name)} doesn't answer by then, the request lapses and you can book another time.</span></div>`
          : `<div class="notice ok"><span>${x.contact_email ? `We emailed the details to ${J.esc(x.contact_email)}.` : 'Your booking is confirmed.'} We'll remind you before your appointment.</span></div>`}
        <p class="muted small">To cancel or move it, open <b>My bookings</b> and sign in with ${J.esc(J.fmt.prettyPhone(x.customer.phone))}.</p></div></div>
      <div class="row"><button type="button" class="btn" data-act="nav" data-view="account">My bookings</button><button type="button" class="btn ghost" data-act="book.again">Book something else</button></div></div>`;
  }

  /* ------------------------------------------------------------------ the frame */
  function canContinue(b, step) {
    const s = st();
    switch (step) {
      case 'service': return b.type === 'automotive' ? s.serviceIds.length > 0 : !!s.serviceId;
      case 'addons': case 'staff': case 'party': case 'details': return true;
      case 'vehicle': return s.vehicle.mode === 'saved' ? !!s.vehicle.savedId : !!(s.vehicle.plate.trim() && s.vehicle.modelId);
      case 'address': return !!(s.address.result && s.address.result.exact && s.address.result.within_service_area);
      case 'time': return !!s.slot;
      case 'review': return s.human === 'ok' && !s.taken && (!s.slot.minimum_spend || s.acceptSpend) && (!(s.notice && s.notice.length) || s.ackLead) && !s.busy;
      default: return false;
    }
  }

  function render() {
    const b = J.biz(), s = st();
    const steps = stepsFor(b);
    if (!s.step || (s.step !== 'done' && !steps.includes(s.step))) s.step = steps[0];
    if ((b.type === 'restaurant' ? !b.resources.length : !b.services.length) || b.policy.missing) {
      return `<div class="page"><div class="narrow">${J.storefront(b)}<div class="card empty"><h2>Not taking online bookings yet</h2><p>${J.esc(b.name)} is still setting up. Check back soon${b.phone ? ', or call ' + J.esc(b.phone) : ''}.</p></div></div></div>`;
    }
    if (s.step === 'done') return `<div class="page"><div class="narrow">${J.storefront(b)}${doneStep(b)}</div></div>`;
    const i = steps.indexOf(s.step);
    const body = { service: serviceStep, addons: addonsStep, staff: staffStep, party: partyStep, vehicle: vehicleStep, address: addressStep, time: timeStep, details: detailsStep, review: reviewStep }[s.step](b);
    const isReview = s.step === 'review';
    const pending = J.state.scenario === 'needs_approval' || b.policy.default_status === 'pending';
    const price = b.type === 'restaurant' ? '' : total(b) || services(b).length ? `<span class="total">Total <b>${J.priceTotal(services(b))}</b>${duration(b) ? ` · about ${J.mins(duration(b))}` : ''}</span>` : '<span></span>';
    return `<div class="page"><div class="narrow">
      ${J.storefront(b)}
      <div class="stepper" aria-label="Steps">${steps.map((x, n) => `<span class="${n < i ? 'done' : n === i ? 'now' : ''}">${LABEL[x]}</span>`).join('')}</div>
      <div class="flowhead">${i > 0 ? '<button type="button" class="back" data-act="book.back" aria-label="Back">←</button>' : ''}<span class="label">Step ${i + 1} of ${steps.length}</span></div>
      ${body}
      <div class="footer-bar">${price || '<span></span>'}
        <button type="button" class="btn" data-act="${isReview ? 'book.confirm' : 'book.next'}" ${canContinue(b, s.step) ? '' : 'disabled'}>${isReview ? (s.busy ? J.busyLabel('Booking…') : pending ? 'Send request' : 'Confirm booking') : 'Continue'}</button></div>
    </div></div>`;
  }

  /* ------------------------------------------------------------------ actions */
  function go(step) { st().step = step; J.render(); J.top(); }
  function resetTime() { const s = st(); s.monthKey = null; s.slot = null; s.slots = null; s.taken = null; s.error = null; }

  Object.assign(J.actions, {
    'book.pickService'(ds) { const s = st(); if (s.serviceId !== ds.id) { s.serviceId = ds.id; s.addonIds = []; s.resourceId = 'any'; s.address.result = null; resetTime(); } J.render(); },
    'book.toggleService'(ds) { const s = st(); const n = s.serviceIds.indexOf(ds.id); if (n >= 0) s.serviceIds.splice(n, 1); else s.serviceIds.push(ds.id); s.resourceId = 'any'; resetTime(); J.render(); },
    'book.toggleAddon'(ds) { const s = st(); const n = s.addonIds.indexOf(ds.id); if (n >= 0) s.addonIds.splice(n, 1); else s.addonIds.push(ds.id); resetTime(); J.render(); },
    'book.pickStaff'(ds) { const s = st(); if (s.resourceId !== ds.id) { s.resourceId = ds.id; resetTime(); } J.render(); },
    'book.party'(ds) { const s = st(); s.party = Math.max(1, Math.min(20, s.party + Number(ds.d))); resetTime(); J.render(); },
    'book.table'(ds) { const s = st(); s.tableType = ds.t; resetTime(); J.render(); },
    'book.pickVehicle'(ds) { const v = st().vehicle; if (ds.id === 'new') { v.mode = 'new'; v.savedId = null; } else { v.mode = 'saved'; v.savedId = ds.id; } st().vehicleErr = null; J.render(); },
    'book.pickAddress'(ds) {
      const s = st(), x = me().addresses.find((y) => y.id === ds.id);
      s.address.savedId = ds.id; s.address.error = null;
      s.address.result = { formatted_address: x.line1, exact: true, within_service_area: true, distance_km: x.distance_km, service_radius_km: J.biz().location.radius_km, latitude: x.latitude, longitude: x.longitude };
      resetTime(); J.render();
    },
    async 'book.checkAddress'() {
      const s = st(), a = s.address;
      a.checking = true; a.error = null; a.result = null; a.savedId = null; J.render();
      const [res, err] = await J.call(() => api.geocode(J.state.biz, a.text));
      a.checking = false;
      if (err) a.error = err.detail; else a.result = res;
      resetTime(); J.render();
    },
    'book.pickDate'(ds, el) { if (el.getAttribute('aria-disabled') === 'true') return; const s = st(); s.date = ds.date; s.slot = null; loadSlots(); },
    'book.pickSlot'(ds) { const s = st(); s.slot = s.slots.find((x) => x.start_at === ds.start); J.render(); },
    'book.pickTakenSlot'(ds) { const s = st(); s.slot = s.taken.slots.find((x) => x.start_at === ds.start); s.taken = null; s.slots = null; s.monthKey = null; s.date = T.key(s.slot.start_at); J.toast(`Changed to ${T.time(s.slot.start_at)}`); J.render(); },
    'book.human'() { const s = st(); s.human = 'checking'; J.render(); setTimeout(() => { s.human = 'ok'; J.render(); }, 900); },
    'book.back'() { const s = st(); const steps = stepsFor(J.biz()); const i = steps.indexOf(s.step); s.error = null; go(steps[Math.max(0, i - 1)]); },
    'book.retry'() { const s = st(); s.monthErr = null; s.monthKey = null; J.render(); },
    'book.again'() { J.state.book = init(); J.render(); J.top(); },
    'book.next'() {
      const b = J.biz(), s = st();
      const steps = stepsFor(b);
      if (s.step === 'vehicle' && s.vehicle.mode === 'new') {
        const m = b.supportedVehicles.find((x) => x.id === s.vehicle.modelId);
        const y = Number(s.vehicle.year);
        if (!s.vehicle.year || !(y >= 1950 && y <= 2027)) { s.vehicleErr = 'Enter the year the car was made, such as 2019.'; J.render(); return; }
        if ((m.year_from && y < m.year_from) || (m.year_to && y > m.year_to)) { s.vehicleErr = `This workshop services the ${m.make} ${m.model} only for model years ${m.year_from || '…'}–${m.year_to || 'now'}.`; J.render(); return; }
        s.vehicleErr = null;
      }
      if (s.step === 'details') {
        const e = {};
        if (!s.details.name.trim()) e.name = 'Enter your name';
        try { J.fmt.normPhone(s.details.phone); } catch (x) { e.phone = 'Enter a mobile number we can reach you on, such as 012-345 6789'; }
        if (s.details.email && !/^[^\s@]+@[^\s@]+\.[^\s@]{2,}$/.test(s.details.email.trim())) e.email = 'That email address looks incomplete';
        s.fieldErrors = e;
        if (Object.keys(e).length) { J.render(); return; }
      }
      if (s.step === 'time' && !s.details.phone && session()) {
        s.details.phone = J.fmt.prettyPhone(session().phone);
        if (me()) { s.details.name = s.details.name || me().name; s.details.email = s.details.email || me().email || ''; s.details.gender = s.details.gender || me().gender || ''; }
      }
      go(steps[steps.indexOf(s.step) + 1]);
    },
    async 'book.confirm'() {
      const b = J.biz(), s = st();
      s.busy = true; s.error = null; J.render();
      const v = s.vehicle;
      const savedCar = v.mode === 'saved' && me() ? me().vehicles.find((x) => x.id === v.savedId) : null;
      const body = {
        service_ids: serviceIds(b), start_at: s.slot.start_at, resource_id: s.resourceId === 'any' ? null : s.resourceId,
        customer: { name: s.details.name.trim(), phone: s.details.phone, email: s.details.email.trim() || null, gender: s.details.gender || null },
        party_size: b.type === 'restaurant' ? s.party : null, table_type: s.tableType, allow_separate_seating: s.separate, accept_minimum_spend: s.acceptSpend, accepted_minimum_spend: s.acceptSpend && s.slot.minimum_spend ? s.slot.minimum_spend : null,
        acknowledge_lead_time: s.ackLead, special_request: s.details.request.trim() || null, health_notes: s.details.notes.trim() || null,
        vehicle: b.type !== 'automotive' ? null : savedCar ? { plate_no: savedCar.plate_no, supported_vehicle_id: savedCar.supported_vehicle_id, year: savedCar.year } : { plate_no: v.plate.trim().toUpperCase(), supported_vehicle_id: v.modelId, year: Number(v.year) },
        address: stepsFor(b).includes('address') ? s.address.result : null,
      };
      const [res, err] = await J.call(() => api.createBooking(b.id, body, J.state.scenario));
      s.busy = false;
      if (res) { s.result = res; s.human = 'no'; go('done'); return; }
      if (!err || !err.status) { J.render(); return; }
      if (err.status === 409) { s.taken = { time: s.slot.start_at, slots: err.extra.slots || [] }; s.slot = null; s.human = 'no'; }
      else if (err.status === 402) s.error = { title: 'Online booking is full for this month', text: `${b.name} can't take more online bookings until next month. Call ${b.phone} and they will fit you in.` };
      else if (err.error === 'validation_failed' && err.extra.fields) { s.fieldErrors = err.extra.fields; go('details'); return; }
      else if (err.status === 503) s.error = { title: "We couldn't book that just now", text: 'Please try again in a moment.' };
      else s.error = { title: "We couldn't book that", text: err.detail };
      J.render();
    },
  });

  J.views.book = { init, render, slotGrid, dayStrip };
})();
