/**
 * Set up a booking page: "Is this page ready to take bookings?"
 *
 * The readiness list comes first (from the server, the same questions the engine asks when it looks for free times); each item
 * links to the section that settles it. Then the sections this kind of business has, two columns each: what it is and what is
 * set now on the left, the panel on the right. Every section saves on its own and redraws only itself, so typing in one is never
 * lost to another. The engine's refusal comes back on the field it names.
 */
import { cfg, api, esc, plural, KIND, say, errorHtml, pressing } from './app.js';

const DAYS = [ [ 1, 'Monday' ], [ 2, 'Tuesday' ], [ 3, 'Wednesday' ], [ 4, 'Thursday' ], [ 5, 'Friday' ], [ 6, 'Saturday' ], [ 0, 'Sunday' ] ];
const PEOPLE = { staff: [ 'Staff', 'person' ], salon: [ 'Stylists', 'stylist' ], workshop: [ 'Mechanics', 'mechanic' ], field: [ 'Professionals', 'professional' ] };
const S = { root: null, id: 0, m: null, data: {}, open: {}, ask: {} };

export async function openSetup( root, id ) {
	S.root = root;
	S.id = id;
	root.innerHTML = '<p class="jsb-slow">Loading…</p>';
	try {
		S.m = await api( `/admin/modules/${ id }` );
	} catch ( e ) {
		root.innerHTML = backLink() + errorHtml( e, false );
		return;
	}
	const k = S.m.type;
	const lists = [ 'policies', 'days-off', ...( k === 'restaurant' ? [ 'tables' ] : [ 'services', 'staff' ] ), ...( k === 'workshop' ? [ 'bays', 'supported-vehicles', 'products' ] : [] ), ...( k === 'salon' ? [ 'products' ] : [] ) ];
	const got = await Promise.all( [
		...lists.map( ( p ) => api( `/admin/modules/${ id }/${ p }` ) ),
		api( `/admin/modules/${ id }/hours` ),
		k === 'restaurant' ? api( `/admin/modules/${ id }/seating` ) : Promise.resolve( null ),
	] ).catch( ( e ) => e );
	if ( got instanceof Error ) {
		root.innerHTML = backLink() + errorHtml( got, false );
		return;
	}
	lists.forEach( ( p, i ) => ( S.data[ p ] = got[ i ] ) );
	S.data.hours = got[ lists.length ];
	S.data.seating = got[ lists.length + 1 ];
	if ( S.data.staff ) {
		// each person's hours and what they can do, for the list's one-line summaries
		const extra = await Promise.all( S.data.staff.map( ( p ) => Promise.all( [ api( `/admin/staff/${ p.id }/shifts` ), api( `/admin/staff/${ p.id }/services` ) ] ) ) );
		S.data.staff.forEach( ( p, i ) => {
			p.shifts = extra[ i ][ 0 ];
			p.service_ids = extra[ i ][ 1 ].service_ids;
		} );
	}
	draw();
	root.addEventListener( 'click', onClick );
	root.addEventListener( 'submit', onSubmit );
	root.addEventListener( 'input', onInput );
	root.addEventListener( 'change', onInput );
	if ( location.hash ) {
		root.querySelector( location.hash + ' h3' )?.focus();
	}
}

const backLink = () => `<p class="jsb-back"><a href="${ esc( cfg.urls.modules ) }">‹ Booking pages</a></p>`;
const money = ( v ) => `${ esc( S.m.settings.currency || 'RM' ) } ${ esc( v ) }`;
const mins = ( n ) => ( n >= 60 && n % 60 === 0 ? plural( n / 60, 'hour', 'hours' ) : n >= 60 ? `${ Math.floor( n / 60 ) } h ${ n % 60 } min` : `${ n } min` );
const policy = () => ( S.data.policies || [] ).find( ( p ) => p.id === S.m.default_policy_id ) || ( S.data.policies || [] )[ 0 ];

// ------------------------------------------------------------------ the sections, by kind

function sections() {
	const k = S.m.type;
	const out = [ 'basics' ];
	if ( k === 'restaurant' ) {
		out.push( 'tables', 'seating' );
	} else {
		out.push( 'services', 'people' );
	}
	if ( k === 'workshop' ) {
		out.push( 'bays', 'vehicles', 'parts' );
	}
	if ( k === 'salon' ) {
		out.push( 'retail' );
	}
	if ( k === 'field' ) {
		out.push( 'location' );
	}
	out.push( 'hours', 'rules', 'daysoff', 'look' );
	return out;
}

function draw() {
	const m = S.m;
	S.root.innerHTML = backLink() +
		`<div class="jsb-setup"><div class="jsb-title"><div><h2>${ esc( m.title ) }</h2><p class="jsb-finding">${ esc( KIND[ m.type ]?.what || m.type ) } · ${ m.status === 'active' ? 'taking bookings' : 'switched off' }</p></div></div>` +
		`<div id="jsb-ready"></div>` +
		sections().map( ( s ) => `<section class="jsb-set" id="sec-${ s }" aria-labelledby="h-${ s }"></section>` ).join( '' ) + `</div>`;
	drawReady();
	sections().forEach( drawSection );
}

function drawReady() {
	const r = S.m.readiness || [];
	const done = r.filter( ( x ) => x.done ).length;
	const el = S.root.querySelector( '#jsb-ready' );
	el.innerHTML = `<div class="jsb-card jsb-ready"><h3>${ done === r.length ? 'Ready to take bookings' : `${ done } of ${ r.length } done` }</h3>` +
		`<div class="jsb-ready-track" aria-hidden="true"><i style="width:${ r.length ? ( done / r.length ) * 100 : 0 }%"></i></div>` +
		`<ol>${ r.map( ( x ) => `<li class="${ x.done ? 'done' : '' }"><span class="mark" aria-hidden="true"></span>${ x.done ? `<span>${ esc( x.label ) }</span><span class="jsb-vh"> (done)</span>` : `<a href="#sec-${ esc( x.section ) }" data-act="goto" data-section="${ esc( x.section ) }">${ esc( x.label ) }</a>` }</li>` ).join( '' ) }</ol></div>`;
}

async function refreshModule() {
	try {
		S.m = await api( `/admin/modules/${ S.id }` );
		drawReady();
		const f = S.root.querySelector( '.jsb-setup > .jsb-title .jsb-finding' );
		f.textContent = `${ KIND[ S.m.type ]?.what || S.m.type } · ${ S.m.status === 'active' ? 'taking bookings' : 'switched off' }`;
	} catch ( e ) {}
}

/** One section: its label column (title, what it is for, what is set now) and its panel. */
function drawSection( key ) {
	const el = S.root.querySelector( `#sec-${ key }` );
	const [ title, desc, now, panel ] = SECTION[ key ]();
	el.innerHTML = `<div class="jsb-set-label"><h3 id="h-${ key }" tabindex="-1">${ title }</h3><p>${ desc }</p>${ now ? `<p class="now">${ now }</p>` : '' }</div><div class="jsb-set-panel">${ panel }</div>`;
}

const SECTION = {
	basics() {
		const m = S.m;
		const s = m.settings;
		const pages = cfg.pages || [];
		const known = pages.some( ( p ) => p.url === s.page_url );
		return [
			'The booking page',
			'Its name, whether it takes bookings, and where on your site it is.',
			s.page_url ? `Shown on <a href="${ esc( s.page_url ) }" target="_blank" rel="noopener">${ esc( s.page_url.replace( /^https?:\/\/[^/]+/, '' ) || '/' ) }</a>` : 'Not on a page yet.',
			`<form data-form="basics" novalidate>` +
				fld( { k: 'title', label: 'Name', type: 'text', required: true }, m.title ) +
				fld( { k: 'status', label: 'Taking bookings', type: 'check', hint: 'Switched off, the booking form shows nothing to visitors.' }, m.status === 'active' ) +
				`<div class="jsb-field"><span class="lab">Shortcode</span><div class="jsb-inline"><code class="jsb-code">${ esc( m.shortcode ) }</code><button type="button" class="jsb-link" data-act="copy" data-code="${ esc( m.shortcode ) }">Copy</button></div><span class="hint">Paste it into any page, or add the “Booking” block in the editor. <a href="${ esc( cfg.urls.newPage ) }">Make a new page</a></span></div>` +
				fld( { k: 'page_url', label: 'The page it is on', type: 'select', options: [ [ '', known || ! s.page_url ? 'Not chosen yet' : s.page_url ], ...pages.map( ( p ) => [ p.url, p.title || p.url ] ) ], hint: 'The link in booking emails opens this page, to change or cancel. Left unchosen, the first page that shows the form is used.' }, s.page_url ) +
				fld( { k: 'notify_email', label: 'Tell me about new bookings at', type: 'email', hint: 'Empty: the site\'s admin email.' }, s.notify_email ) +
				saveRow( 'basics' ) + `</form>` +
				`<div class="jsb-danger">${ S.ask.remove ? `<div class="jsb-ask" role="group" aria-labelledby="ask-remove"><p id="ask-remove">Delete “${ esc( m.title ) }” and every booking it holds? This cannot be undone.</p><div class="jsb-actions"><button type="button" class="jsb-btn primary" data-act="remove-module" data-busy="Deleting…">Yes, delete it</button><button type="button" class="jsb-btn" data-act="keep-module">Keep it</button></div><div class="jsb-err" role="alert"></div></div>` : `<button type="button" class="jsb-link danger" data-act="ask-remove">Delete this booking page</button>` }</div>`,
		];
	},
	services() {
		const k = S.m.type;
		const rows = S.data.services;
		const f = [
			{ k: 'name', label: 'Name', type: 'text', required: true },
			{ k: 'duration_minutes', label: 'Takes (minutes)', type: 'number', min: 5, max: 1440, required: true, half: true },
			{ k: 'price', label: `Price (${ esc( S.m.settings.currency || 'RM' ) })`, type: 'money', half: true },
			...( k === 'salon' ? [ { k: 'is_addon', label: 'An add-on', type: 'check', hint: 'Chosen on top of a service (gel finish, scalp treatment); its time is added on.' } ] : [] ),
			...( k === 'field' ? [
				{ k: 'location_type', label: 'Where it happens', type: 'select', options: [ [ 'in_office', 'At your office' ], [ 'on_site', 'At the customer\'s place' ], [ 'virtual', 'Online' ] ] },
				{ k: 'travel_buffer_minutes', label: 'Travel time each way (minutes)', type: 'number', min: 0, max: 240, hint: 'Held before and after an on-site visit.', half: true },
				{ k: 'service_category', label: 'Kind of visit', type: 'select', nullable: true, options: [ [ '', 'Standard' ], [ 'inspection', 'Inspection (can lead to a follow-up)' ] ], half: true },
			] : [] ),
			...( k === 'field' || k === 'workshop' ? [ { k: 'price_after_inspection', label: 'Price is set after an inspection', type: 'check' } ] : [] ),
			...( ( S.data.policies || [] ).length > 1 ? [ { k: 'policy_id', label: 'Booking rule', type: 'select', int: true, options: [ [ '', 'The page\'s rule' ], ...S.data.policies.map( ( p ) => [ String( p.id ), p.name ] ) ] } ] : [] ),
			{ k: 'description', label: 'Description', type: 'textarea', nullable: true },
			{ k: 'active', label: 'Bookable', type: 'check' },
		];
		return [
			'Services',
			'What people book, how long it takes and what it costs.',
			rows.length ? `${ plural( rows.filter( ( s ) => s.active ).length, 'service', 'services' ) } bookable.` : 'None yet.',
			collection( 'services', rows, f, ( s ) => [
				s.name + ( s.active ? '' : ' (not bookable)' ),
				[ mins( s.duration_minutes ), Number( s.price ) > 0 ? money( s.price ) : '', s.is_addon ? 'add-on' : '', k === 'field' ? { in_office: 'at your office', on_site: 'at the customer\'s', virtual: 'online' }[ s.location_type ] : '' ].filter( Boolean ).join( ' · ' ),
			], { active: true, location_type: 'in_office', duration_minutes: 30 }, 'Add a service' ),
		];
	},
	people() {
		const [ title, one ] = PEOPLE[ S.m.type ] || PEOPLE.staff;
		const rows = S.data.staff;
		const f = [
			{ k: 'name', label: 'Name', type: 'text', required: true },
			{ k: 'contact_email', label: 'Email', type: 'email', nullable: true, half: true },
			{ k: 'contact_phone', label: 'Phone', type: 'text', nullable: true, half: true },
			...( S.m.type === 'field' ? [ { k: 'meeting_link', label: 'Their online meeting room', type: 'url', nullable: true, hint: 'Sent to customers who book an online service with them.' } ] : [] ),
			{ k: 'wp_user_id', label: 'Their login on this site', type: 'select', int: true, nullable: true, options: [ [ '', 'None' ], ...( cfg.users || [] ).map( ( u ) => [ String( u.id ), u.name ] ) ], hint: 'Linked, they see the whole day but work only their own bookings.' },
			{ k: 'status', label: 'Status', type: 'select', options: [ [ 'active', 'Working here' ], [ 'inactive', 'Not taking bookings' ] ] },
		];
		const svc = ( S.data.services || [] ).filter( ( s ) => ! s.is_addon );
		return [
			title,
			`Who does the work, the hours they work, and what they can do.`,
			rows.length ? `${ plural( rows.filter( ( p ) => p.status === 'active' ).length, one, title.toLowerCase() ) } working.` : 'Nobody yet.',
			collection( 'staff', rows, f, ( p ) => [
				p.name + ( p.status === 'active' ? '' : ' (not taking bookings)' ),
				[ weekSummary( p.shifts || [], 'start_time', 'end_time' ), p.service_ids && p.service_ids.length ? `does ${ p.service_ids.length } of ${ svc.length } services` : '' ].filter( Boolean ).join( ' · ' ),
			], { status: 'active' }, `Add a ${ one }`, ( p ) =>
				`<fieldset class="jsb-field"><legend>Working hours</legend>${ week( 'shifts', p ? p.shifts || [] : [], true ) }</fieldset>` +
				( svc.length > 1 ? `<fieldset class="jsb-field"><legend>Can do</legend><div class="jsb-checks">${ svc.map( ( s ) => `<label><input type="checkbox" name="can" value="${ s.id }"${ p && p.service_ids && p.service_ids.includes( s.id ) ? ' checked' : '' }> ${ esc( s.name ) }</label>` ).join( '' ) }</div><span class="hint">None ticked: every service.</span></fieldset>` : '' ) ),
		];
	},
	tables() {
		const rows = S.data.tables;
		const f = [
			{ k: 'name', label: 'Name or number', type: 'text', required: true, half: true },
			{ k: 'capacity', label: 'Seats', type: 'number', min: 1, max: 50, required: true, half: true },
			{ k: 'location', label: 'Where it is', type: 'text', nullable: true, hint: 'Window, terrace, upstairs: for you.' },
			{ k: 'is_vip', label: 'In the VIP room', type: 'check' },
			{ k: 'minimum_spend', label: `Minimum spend (${ esc( S.m.settings.currency || 'RM' ) })`, type: 'money', hint: 'Shown to the customer, who accepts it before booking.' },
			{ k: 'status', label: 'Status', type: 'select', options: [ [ 'active', 'In use' ], [ 'inactive', 'Not in use' ] ] },
		];
		const seats = rows.filter( ( t ) => t.status === 'active' ).reduce( ( n, t ) => n + t.capacity, 0 );
		return [
			'Tables',
			'Each table and how many it seats. Bigger groups are seated across tables that are free together.',
			rows.length ? `${ plural( rows.length, 'table', 'tables' ) }, ${ seats } seats in use.` : 'None yet.',
			collection( 'tables', rows, f, ( t ) => [ `${ t.name }${ t.status === 'active' ? '' : ' (not in use)' }`, [ plural( t.capacity, 'seat', 'seats' ), t.is_vip ? 'VIP' : '', Number( t.minimum_spend ) > 0 ? 'minimum ' + money( t.minimum_spend ) : '', t.location || '' ].filter( Boolean ).join( ' · ' ) ], { status: 'active', capacity: 2 }, 'Add a table' ),
		];
	},
	seating() {
		const s = S.data.seating || { booking_mode: 'flexible', slot_duration_minutes: 90, slot_interval_minutes: 30, first_slot_time: '12:00', last_slot_time: '21:00', buffer_minutes: 15, allow_table_selection: false };
		return [
			'Seating times',
			'When people can book a table and how long they keep it.',
			S.data.seating ? `${ s.first_slot_time }–${ s.last_slot_time }, every ${ mins( s.slot_interval_minutes ) }, ${ mins( s.slot_duration_minutes ) } a table.` : 'Not set yet.',
			`<form data-form="seating" novalidate>` +
				fld( { k: 'booking_mode', label: 'Times offered', type: 'select', options: [ [ 'flexible', 'Any time on the step' ], [ 'fixed', 'Fixed sittings only' ] ] }, s.booking_mode ) +
				`<div class="jsb-pair">${ fld( { k: 'first_slot_time', label: 'First booking at', type: 'time' }, s.first_slot_time ) }${ fld( { k: 'last_slot_time', label: 'Last booking at', type: 'time' }, s.last_slot_time ) }</div>` +
				`<div class="jsb-pair">${ fld( { k: 'slot_interval_minutes', label: 'A time every (minutes)', type: 'number', min: 5, max: 240 }, s.slot_interval_minutes ) }${ fld( { k: 'slot_duration_minutes', label: 'A table is kept for (minutes)', type: 'number', min: 15, max: 600 }, s.slot_duration_minutes ) }</div>` +
				fld( { k: 'buffer_minutes', label: 'Clearing time after (minutes)', type: 'number', min: 0, max: 240 }, s.buffer_minutes ) +
				fld( { k: 'allow_table_selection', label: 'Let customers choose their table', type: 'check' }, s.allow_table_selection ) +
				saveRow( 'seating' ) + `</form>`,
		];
	},
	bays() {
		const rows = S.data.bays;
		return [ 'Bays', 'Each job needs a free bay as well as a free mechanic.', rows.length ? `${ plural( rows.length, 'bay', 'bays' ) }.` : 'None yet.',
			collection( 'bays', rows, [ { k: 'name', label: 'Name', type: 'text', required: true }, { k: 'status', label: 'Status', type: 'select', options: [ [ 'active', 'In use' ], [ 'inactive', 'Not in use' ] ] } ], ( b ) => [ b.name, b.status === 'active' ? '' : 'not in use' ], { status: 'active' }, 'Add a bay' ) ];
	},
	vehicles() {
		const rows = S.data[ 'supported-vehicles' ];
		const f = [
			{ k: 'make', label: 'Make', type: 'text', required: true, half: true },
			{ k: 'model', label: 'Model', type: 'text', required: true, half: true },
			{ k: 'year_from', label: 'From model year', type: 'number', min: 1900, max: 2100, nullable: true, half: true },
			{ k: 'year_to', label: 'To model year', type: 'number', min: 1900, max: 2100, nullable: true, half: true },
		];
		return [ 'Cars you service', 'Customers choose their car from this list; others are told before they book.', rows.length ? `${ plural( rows.length, 'model', 'models' ) }.` : 'None yet: nobody can book.',
			collection( 'supported-vehicles', rows, f, ( v ) => [ `${ v.make } ${ v.model }`, v.year_from || v.year_to ? `${ v.year_from || '…' }–${ v.year_to || '…' }` : 'any year' ], {}, 'Add a car model' ) ];
	},
	parts() {
		return productSection( 'Parts', 'Parts a job can use. One on order shows its waiting time to the customer.', 'Add a part', true );
	},
	retail() {
		return productSection( 'Products for sale', 'Sold during an appointment; the stock goes down when it is done.', 'Add a product', false );
	},
	location() {
		const s = S.m.settings;
		return [
			'Where you are',
			'On-site visits are offered only within this distance, with the travel time held either side.',
			null === s.latitude ? 'Not set yet: on-site services cannot be booked.' : `${ s.service_radius_km ?? '–' } km around ${ Number( s.latitude ).toFixed( 4 ) }, ${ Number( s.longitude ).toFixed( 4 ) }.`,
			`<form data-form="location" novalidate><div class="jsb-pair">` +
				fld( { k: 'latitude', label: 'Latitude', type: 'number', step: 'any', min: -90, max: 90, nullable: true }, s.latitude ) +
				fld( { k: 'longitude', label: 'Longitude', type: 'number', step: 'any', min: -180, max: 180, nullable: true }, s.longitude ) + `</div>` +
				`<p class="hint">On <a href="https://www.openstreetmap.org" target="_blank" rel="noopener">openstreetmap.org</a>, right-click your place and choose “Show address”: the two numbers are these.</p>` +
				fld( { k: 'service_radius_km', label: 'You travel up to (km)', type: 'number', step: 'any', min: 0, max: 500, nullable: true }, s.service_radius_km ) +
				saveRow( 'location' ) + `</form>`,
		];
	},
	hours() {
		const rows = S.data.hours;
		return [
			'Opening hours',
			S.m.type === 'restaurant' ? 'Days you are open; your seating times apply within them.' : 'Bookings fall inside these hours and inside each person\'s own.',
			rows.length ? weekSummary( rows, 'open_time', 'close_time' ) : 'Not limited: each person\'s working hours decide.',
			`<form data-form="hours" novalidate>` + fld( { k: 'limit', label: 'Limit bookings to opening hours', type: 'check' }, rows.length > 0 ) +
				`<div class="jsb-hours"${ rows.length ? '' : ' hidden' }>${ week( 'hours', rows, false ) }</div>` + saveRow( 'hours' ) + `</form>`,
		];
	},
	rules() {
		const p = policy();
		if ( ! p ) {
			return [ 'Booking rules', '', '', '<p class="hint">This page has no rule yet.</p>' ];
		}
		return [
			'Booking rules',
			'Whether you confirm each booking, how much notice you need, and until when customers can cancel online.',
			`${ p.default_status === 'pending' ? 'You confirm each request' : 'Booked straight away' }; cancel online until ${ mins( p.min_notice_cancel_minutes ) } before.`,
			`<form data-form="rules" novalidate>` +
				fld( { k: 'default_status', label: 'New bookings', type: 'select', options: [ [ 'confirmed', 'Are confirmed straight away' ], [ 'pending', 'Wait for me to confirm' ] ] }, p.default_status ) +
				fld( { k: 'pending_timeout_minutes', label: 'A request lapses if not answered within (minutes)', type: 'number', min: 5, max: 10080 }, p.pending_timeout_minutes ) +
				`<div class="jsb-pair">${ fld( { k: 'min_notice_booking_minutes', label: 'Notice needed to book (minutes)', type: 'number', min: 0, max: 525600 }, p.min_notice_booking_minutes ) }${ fld( { k: 'booking_window_days', label: 'Bookable up to (days ahead)', type: 'number', min: 1, max: 730 }, p.booking_window_days ) }</div>` +
				fld( { k: 'min_notice_cancel_minutes', label: 'Customers can cancel online until (minutes before)', type: 'number', min: 0, max: 525600, hint: '1440 is a day.' }, p.min_notice_cancel_minutes ) +
				saveRow( 'rules' ) + `</form>`,
		];
	},
	daysoff() {
		const rows = S.data[ 'days-off' ];
		const who = [ [ '', 'The whole business (closed)' ], ...( S.data.staff || S.data.tables || [] ).filter( () => S.m.type !== 'restaurant' ).map( ( p ) => [ String( p.id ), p.name ] ), ...( S.m.type === 'restaurant' ? ( S.data.tables || [] ).map( ( t ) => [ String( t.id ), 'Table ' + t.name ] ) : [] ) ];
		const name = ( id ) => ( who.find( ( w ) => w[ 0 ] === String( id ?? '' ) ) || [ '', '' ] )[ 1 ];
		const f = [
			{ k: 'name', label: 'What it is', type: 'text', required: true, hint: 'Public holiday, annual leave, repairs.' },
			{ k: 'resource_id', label: 'Who', type: 'select', int: true, nullable: true, options: who },
			{ k: 'start_date', label: 'From', type: 'date', required: true, half: true },
			{ k: 'end_date', label: 'To', type: 'date', required: true, half: true },
			{ k: 'repeat_yearly', label: 'Every year', type: 'check' },
		];
		return [ 'Days off', 'Holidays close the business; leave takes one person off.', rows.length ? `${ plural( rows.length, 'entry', 'entries' ) }.` : 'None.',
			collection( 'days-off', rows, f, ( d ) => [ d.name, `${ d.start_date === d.end_date ? d.start_date : d.start_date + ' to ' + d.end_date }${ d.repeat_yearly ? ', every year' : '' } · ${ name( d.resource_id ) }` ], {}, 'Add days off' ) ];
	},
	look() {
		const s = S.m.settings;
		return [
			'How it looks',
			'The booking form takes your site\'s font by default. Choose its one colour; everything else follows from it.',
			`<span class="jsb-swatch" style="background:${ esc( s.accent ) }"></span> ${ esc( s.accent ) } · ${ s.font === 'dm-sans' ? 'DM Sans' : 'your site\'s font' }`,
			`<form data-form="look" novalidate><div class="jsb-pair">` +
				`<div class="jsb-field"><label for="f-accent">Colour</label><div class="jsb-inline"><input id="f-accent" name="accent" type="color" value="${ esc( s.accent ) }"><input name="accent_hex" type="text" value="${ esc( s.accent ) }" maxlength="7" style="width:110px" aria-label="Colour as a hex code"></div></div>` +
				fld( { k: 'font', label: 'Font', type: 'select', options: [ [ 'theme', 'Your site\'s font' ], [ 'dm-sans', 'DM Sans (included)' ] ] }, s.font ) + `</div><div class="jsb-pair">` +
				fld( { k: 'currency', label: 'Currency shown', type: 'text', max: 8 }, s.currency ) +
				fld( { k: 'phone', label: 'Phone for questions', type: 'text', max: 32, hint: 'Shown when a booking cannot be made online.' }, s.phone ) + `</div>` +
				( S.m.type === 'field' ? fld( { k: 'map_picker', label: 'Offer a map to pick the visit address', type: 'check', hint: 'Uses OpenStreetMap; the customer\'s browser loads its map tiles.' }, s.map_picker ) : '' ) +
				`<p class="jsb-contrast hint" aria-live="polite"></p>` + saveRow( 'look' ) + `</form>` +
				`<div class="jsb-preview"><p class="lab">Preview: the real booking form</p>${ S.m.status === 'active' ? `<jsb-booking module="${ S.id }" api="${ esc( cfg.urls.rest ) }" css="${ esc( cfg.urls.assets + 'front/jsb-booking.css?ver=' + cfg.version ) }"></jsb-booking>` : '<p class="hint">Switch the page on to see its booking form here.</p>' }</div>`,
		];
	},
};

function productSection( title, desc, add, parts ) {
	const rows = S.data.products;
	const f = [
		{ k: 'name', label: 'Name', type: 'text', required: true },
		{ k: 'price', label: `Price (${ esc( S.m.settings.currency || 'RM' ) })`, type: 'money', half: true },
		{ k: 'stock_qty', label: 'In stock', type: 'number', min: 0, half: true },
		...( parts ? [ { k: 'stock_status', label: 'Availability', type: 'select', options: [ [ 'in_stock', 'In stock' ], [ 'available_on_order', 'Ordered when needed' ], [ 'out_of_stock', 'Out of stock' ] ] }, { k: 'estimated_lead_time_days', label: 'Days to arrive when ordered', type: 'number', min: 0, max: 365, nullable: true } ] : [] ),
		{ k: 'sku', label: 'Code (SKU)', type: 'text', nullable: true },
	];
	return [ title, desc, rows.length ? `${ plural( rows.length, 'item', 'items' ) }.` : 'None yet.',
		collection( 'products', rows, f, ( p ) => [ p.name, [ Number( p.price ) > 0 ? money( p.price ) : '', `${ p.stock_qty } in stock`, p.stock_status === 'available_on_order' ? `ordered, ${ p.estimated_lead_time_days ?? '?' } days` : '' ].filter( Boolean ).join( ' · ' ) ], { stock_status: 'in_stock', stock_qty: 0 }, add ) ];
}

// ------------------------------------------------------------------ building blocks

let uid = 0;
/** One field. `half` sits two to a row in a .jsb-pair; values go back to the server by `k`. */
function fld( f, v ) {
	const id = 'f-' + f.k + '-' + ( ++uid );
	const hint = f.hint ? `<span class="hint" id="${ id }-h">${ f.hint }</span>` : '';
	const desc = f.hint ? ` aria-describedby="${ id }-h"` : '';
	const req = f.required ? ' required aria-required="true"' : '';
	if ( f.type === 'check' ) {
		return `<div class="jsb-field check"><label><input type="checkbox" id="${ id }" name="${ f.k }"${ v ? ' checked' : '' }${ desc }> ${ f.label }</label>${ hint }<span class="jsb-ferr" hidden></span></div>`;
	}
	let input;
	if ( f.type === 'select' ) {
		input = `<select id="${ id }" name="${ f.k }"${ desc }${ f.int ? ' data-int="1"' : '' }${ f.nullable ? ' data-null="1"' : '' }>${ f.options.map( ( [ val, lab ] ) => `<option value="${ esc( val ) }"${ String( v ?? '' ) === String( val ) ? ' selected' : '' }>${ esc( lab ) }</option>` ).join( '' ) }</select>`;
	} else if ( f.type === 'textarea' ) {
		input = `<textarea id="${ id }" name="${ f.k }" rows="3"${ desc }${ f.nullable ? ' data-null="1"' : '' }>${ esc( v ?? '' ) }</textarea>`;
	} else {
		const type = f.type === 'money' ? 'text' : f.type;
		const extra = ( f.min !== undefined ? ` min="${ f.min }"` : '' ) + ( f.max !== undefined && f.type === 'number' ? ` max="${ f.max }"` : '' ) + ( f.max !== undefined && f.type === 'text' ? ` maxlength="${ f.max }"` : '' ) + ( f.step ? ` step="${ f.step }"` : '' ) + ( f.type === 'money' ? ' inputmode="decimal" data-money="1"' : '' );
		input = `<input id="${ id }" name="${ f.k }" type="${ type }" value="${ esc( v ?? '' ) }"${ extra }${ req }${ desc }${ f.nullable ? ' data-null="1"' : '' }${ f.type === 'number' ? ' data-num="1"' : '' }>`;
	}
	return `<div class="jsb-field${ f.half ? ' half' : '' }"><label for="${ id }">${ f.label }</label>${ input }${ hint }<span class="jsb-ferr" hidden></span></div>`;
}

function saveRow( key, extra = '' ) {
	return `<div class="jsb-err" role="alert"></div><div class="jsb-actions"><button type="submit" class="jsb-btn primary" data-busy="Saving…">Save</button>${ extra }<span class="jsb-saved" role="status" data-saved="${ key }"></span></div>`;
}

/** What a form holds, typed for the API: numbers as numbers, empty optional fields as null, ticks as booleans. */
function values( form ) {
	const out = {};
	form.querySelectorAll( '[name]' ).forEach( ( el ) => {
		if ( el.closest( '[data-week]' ) || el.name === 'can' ) {
			return;
		}
		if ( el.type === 'checkbox' ) {
			out[ el.name ] = el.checked;
			return;
		}
		const v = el.value.trim();
		if ( v === '' && el.dataset.null ) {
			out[ el.name ] = null;
		} else if ( el.dataset.num || el.dataset.int ) {
			out[ el.name ] = v === '' ? null : Number( v );
		} else {
			out[ el.name ] = v;
		}
	} );
	return out;
}

/** The week, Monday first: a tick for each day that has hours, and the hours (a person's with an optional break). */
function week( name, rows, withBreak ) {
	const by = Object.fromEntries( rows.map( ( r ) => [ r.weekday, r ] ) );
	const s = withBreak ? [ 'start_time', 'end_time' ] : [ 'open_time', 'close_time' ];
	return `<div class="jsb-week" data-week="${ name }"><div class="head" aria-hidden="true"><span></span><span>${ withBreak ? 'Starts' : 'Opens' }</span><span>${ withBreak ? 'Ends' : 'Closes' }</span>${ withBreak ? '<span>Break</span>' : '' }</div>` +
		DAYS.map( ( [ d, label ] ) => {
			const r = by[ d ];
			const t = ( k, def ) => esc( r ? ( r[ k ] || '' ).slice( 0, 5 ) : def );
			return `<div class="day" data-day="${ d }"><label><input type="checkbox" data-k="on"${ r ? ' checked' : '' }> ${ label }</label>` +
				`<input type="time" data-k="${ s[ 0 ] }" value="${ t( s[ 0 ], withBreak ? '09:00' : '10:00' ) }" aria-label="${ label }: ${ withBreak ? 'starts' : 'opens' }"${ r ? '' : ' disabled' }>` +
				`<input type="time" data-k="${ s[ 1 ] }" value="${ t( s[ 1 ], withBreak ? '18:00' : '20:00' ) }" aria-label="${ label }: ${ withBreak ? 'ends' : 'closes' }"${ r ? '' : ' disabled' }>` +
				( withBreak ? `<span class="brk"><input type="time" data-k="break_start" value="${ t( 'break_start', '' ) }" aria-label="${ label }: break from"${ r ? '' : ' disabled' }><span aria-hidden="true">–</span><input type="time" data-k="break_end" value="${ t( 'break_end', '' ) }" aria-label="${ label }: break until"${ r ? '' : ' disabled' }></span>` : '' ) +
				`</div>`;
		} ).join( '' ) + `<p class="hint">An end before the start runs past midnight.</p></div>`;
}

function weekRows( el ) {
	return [ ...el.querySelectorAll( '.day' ) ].filter( ( d ) => d.querySelector( '[data-k="on"]' ).checked ).map( ( d ) => {
		const row = { weekday: Number( d.dataset.day ) };
		d.querySelectorAll( 'input[type="time"]' ).forEach( ( i ) => ( row[ i.dataset.k ] = i.value || null ) );
		return row;
	} );
}

/** "Mon–Fri 10:00–19:00, Sat 10:00–16:00" */
function weekSummary( rows, a, b ) {
	if ( ! rows.length ) {
		return 'no hours yet';
	}
	const short = { 0: 'Sun', 1: 'Mon', 2: 'Tue', 3: 'Wed', 4: 'Thu', 5: 'Fri', 6: 'Sat' };
	const order = [ 1, 2, 3, 4, 5, 6, 0 ];
	const span = ( r ) => `${ r[ a ].slice( 0, 5 ) }–${ r[ b ].slice( 0, 5 ) }`;
	const by = Object.fromEntries( rows.map( ( r ) => [ r.weekday, r ] ) );
	const runs = [];
	for ( const d of order ) {
		const r = by[ d ];
		const last = runs[ runs.length - 1 ];
		if ( r && last && last.span === span( r ) && order.indexOf( last.to ) === order.indexOf( d ) - 1 ) {
			last.to = d;
		} else if ( r ) {
			runs.push( { from: d, to: d, span: span( r ) } );
		}
	}
	return runs.map( ( x ) => `${ short[ x.from ] }${ x.to !== x.from ? '–' + short[ x.to ] : '' } ${ x.span }` ).join( ', ' );
}

/** A list that edits in place: one line per row, Edit opens its form under it, Add opens an empty one at the end. */
function collection( path, rows, fields, line, blank, addLabel, extra ) {
	const open = S.open[ path ];
	const form = ( row ) => `<form data-form="row" data-path="${ path }" data-id="${ row ? row.id : '' }" class="jsb-rowform" novalidate>` +
		`<div class="jsb-grid2">${ fields.map( ( f ) => fld( f, row ? row[ f.k ] : blank[ f.k ] ) ).join( '' ) }</div>${ extra ? extra( row ) : '' }` +
		`<div class="jsb-err" role="alert"></div><div class="jsb-actions"><button type="submit" class="jsb-btn primary" data-busy="Saving…">${ row ? 'Save' : 'Add' }</button><button type="button" class="jsb-btn" data-act="close-row" data-path="${ path }">Cancel</button>` +
		( row ? ( S.ask[ path + row.id ] ? `<span class="jsb-ask-inline">Remove it?<button type="button" class="jsb-btn danger" data-act="remove-row" data-path="${ path }" data-id="${ row.id }" data-busy="Removing…">Yes, remove</button><button type="button" class="jsb-link" data-act="keep-row" data-path="${ path }" data-id="${ row.id }">No</button></span>` : `<button type="button" class="jsb-link danger" data-act="ask-row" data-path="${ path }" data-id="${ row.id }">Remove</button>` ) : '' ) +
		`</div></form>`;
	let h = rows.length ? `<ul class="jsb-rows">` : '';
	for ( const r of rows ) {
		const [ main, meta ] = line( r );
		h += `<li data-row="${ r.id }">${ open === r.id ? form( r ) : `<div class="line"><span><b>${ esc( main ) }</b>${ meta ? `<small>${ esc( meta ) }</small>` : '' }</span><button type="button" class="jsb-btn" data-act="edit-row" data-path="${ path }" data-id="${ r.id }" aria-label="Edit ${ esc( main ) }">Edit</button></div>` }</li>`;
	}
	h += rows.length ? `</ul>` : '';
	h += open === 'new' ? form( null ) : `<button type="button" class="jsb-btn" data-act="add-row" data-path="${ path }">${ esc( addLabel ) }</button>`;
	return h;
}

const PATH_SECTION = { services: 'services', staff: 'people', tables: 'tables', bays: 'bays', 'supported-vehicles': 'vehicles', products: S => ( S.m.type === 'workshop' ? 'parts' : 'retail' ), 'days-off': 'daysoff' };
const sectionOf = ( path ) => ( typeof PATH_SECTION[ path ] === 'function' ? PATH_SECTION[ path ]( S ) : PATH_SECTION[ path ] );

// ------------------------------------------------------------------ saving

/** The engine names the field it refused ("duration_minutes", "settings.accent", "shifts.2.end_time"): show it there. */
function showError( form, e ) {
	form.querySelectorAll( '.jsb-ferr' ).forEach( ( x ) => {
		x.hidden = true;
		x.textContent = '';
	} );
	form.querySelectorAll( '[aria-invalid]' ).forEach( ( x ) => x.removeAttribute( 'aria-invalid' ) );
	const name = String( ( e.data && e.data.field ) || '' ).split( '.' ).filter( ( p ) => ! /^\d+$/.test( p ) ).pop();
	const input = name && form.querySelector( `[name="${ name }"]` );
	if ( input ) {
		const err = input.closest( '.jsb-field' ).querySelector( '.jsb-ferr' );
		// it sits under the field's own label: "duration_minutes must be …" reads as "Must be …"
		const text = e.message.startsWith( name + ' ' ) ? e.message.slice( name.length + 1 ) : e.message;
		err.textContent = text.charAt( 0 ).toUpperCase() + text.slice( 1 );
		err.hidden = false;
		input.setAttribute( 'aria-invalid', 'true' );
		input.focus();
	} else {
		form.querySelector( '.jsb-err' ).innerHTML = `<p class="jsb-msg error">${ esc( e.message ) }</p>`;
	}
	say( e.message );
}

function saved( key, text = 'Saved' ) {
	const el = S.root.querySelector( `[data-saved="${ key }"]` );
	if ( el ) {
		el.textContent = text;
		setTimeout( () => el.isConnected && ( el.textContent = '' ), 2000 );
	}
	say( text + '.' );
}

const FORMS = {
	async basics( v ) {
		await api( `/admin/modules/${ S.id }`, { method: 'PATCH', data: { title: v.title, status: v.status ? 'active' : 'draft', settings: { page_url: v.page_url, notify_email: v.notify_email } } } );
		return 'basics';
	},
	async seating( v ) {
		S.data.seating = await api( `/admin/modules/${ S.id }/seating`, { method: 'PUT', data: v } );
		return 'seating';
	},
	async location( v ) {
		await api( `/admin/modules/${ S.id }`, { method: 'PATCH', data: { settings: v } } );
		return 'location';
	},
	async hours( v, form ) {
		const rows = v.limit ? weekRows( form.querySelector( '[data-week]' ) ) : [];
		S.data.hours = await api( `/admin/modules/${ S.id }/hours`, { method: 'PUT', data: { hours: rows } } );
		return 'hours';
	},
	async rules( v ) {
		const p = policy();
		const out = await api( `/admin/policies/${ p.id }`, { method: 'PATCH', data: v } );
		Object.assign( p, out );
		return 'rules';
	},
	async look( v ) {
		const settings = { accent: v.accent_hex || v.accent, font: v.font, currency: v.currency, phone: v.phone };
		if ( 'map_picker' in v ) {
			settings.map_picker = v.map_picker;
		}
		await api( `/admin/modules/${ S.id }`, { method: 'PATCH', data: { settings } } );
		return 'look';
	},
	/** A row of a list; a person also saves their hours and what they can do. */
	async row( v, form ) {
		const path = form.dataset.path;
		const id = form.dataset.id;
		const body = { ...v };
		const row = id
			? await api( `/admin/${ path }/${ id }`, { method: 'PATCH', data: body } )
			: await api( `/admin/modules/${ S.id }/${ path }`, { method: 'POST', data: body } );
		if ( path === 'staff' ) {
			row.shifts = await api( `/admin/staff/${ row.id }/shifts`, { method: 'PUT', data: { shifts: weekRows( form.querySelector( '[data-week]' ) ) } } );
			const can = [ ...form.querySelectorAll( '[name="can"]:checked' ) ].map( ( c ) => Number( c.value ) );
			row.service_ids = ( await api( `/admin/staff/${ row.id }/services`, { method: 'PUT', data: { service_ids: can } } ) ).service_ids || can;
		}
		const list = S.data[ path ];
		const i = list.findIndex( ( r ) => String( r.id ) === String( row.id ) );
		if ( i >= 0 ) {
			list[ i ] = Object.assign( list[ i ], row );
		} else {
			list.push( row );
		}
		S.open[ path ] = null;
		return sectionOf( path );
	},
};

async function onSubmit( e ) {
	const form = e.target.closest( 'form[data-form]' );
	if ( ! form ) {
		return;
	}
	e.preventDefault();
	const kind = form.dataset.form;
	const btn = form.querySelector( '[type="submit"]' );
	try {
		const section = await pressing( btn, () => FORMS[ kind ]( values( form ), form ) );
		await refreshModule();
		drawSection( section );
		if ( kind === 'row' ) {
			// the list redrew: the row just saved takes focus
			const path = form.dataset.path;
			const item = S.root.querySelector( `#sec-${ section } [data-act="edit-row"][data-path="${ path }"]` + ( form.dataset.id ? `[data-id="${ form.dataset.id }"]` : ':last-of-type' ) );
			( item || S.root.querySelector( `#h-${ section }` ) ).focus();
			say( form.dataset.id ? 'Saved.' : 'Added.' );
			if ( path === 'services' && S.data.staff ) {
				drawSection( 'people' ); // "can do" lists the services
			}
			if ( path === 'staff' || path === 'tables' ) {
				drawSection( 'daysoff' ); // days off name the people and tables
			}
		} else {
			saved( section );
			S.root.querySelector( `#sec-${ section } [type="submit"]` )?.focus();
			if ( kind === 'look' || kind === 'basics' ) {
				drawSection( 'look' ); // the preview shows what was saved
			}
			if ( kind === 'basics' ) {
				S.root.querySelector( '.jsb-setup h2' ).textContent = S.m.title;
			}
		}
	} catch ( err ) {
		showError( form, err );
	}
}

async function onClick( e ) {
	const t = e.target.closest( '[data-act]' );
	if ( ! t ) {
		return;
	}
	const { act, path, id } = t.dataset;
	if ( act === 'goto' ) {
		e.preventDefault();
		const h = S.root.querySelector( `#h-${ t.dataset.section }` );
		h.scrollIntoView( { block: 'start', behavior: matchMedia( '(prefers-reduced-motion: reduce)' ).matches ? 'auto' : 'smooth' } );
		h.focus( { preventScroll: true } );
		history.replaceState( null, '', '#sec-' + t.dataset.section );
	} else if ( act === 'edit-row' || act === 'add-row' ) {
		S.open[ path ] = act === 'add-row' ? 'new' : Number( id );
		drawSection( sectionOf( path ) );
		S.root.querySelector( `#sec-${ sectionOf( path ) } form[data-path="${ path }"] input, #sec-${ sectionOf( path ) } form[data-path="${ path }"] select` )?.focus();
	} else if ( act === 'close-row' ) {
		const was = S.open[ path ];
		S.open[ path ] = null;
		drawSection( sectionOf( path ) );
		S.root.querySelector( `#sec-${ sectionOf( path ) } [data-act="${ was === 'new' ? 'add-row' : 'edit-row' }"][data-path="${ path }"]${ was === 'new' ? '' : `[data-id="${ was }"]` }` )?.focus();
	} else if ( act === 'ask-row' || act === 'keep-row' ) {
		S.ask[ path + id ] = act === 'ask-row';
		const form = t.closest( 'form' );
		const keep = values( form ); // asking must not lose what was typed
		drawSection( sectionOf( path ) );
		const again = S.root.querySelector( `form[data-path="${ path }"][data-id="${ id }"]` );
		Object.entries( keep ).forEach( ( [ k, v ] ) => {
			const el = again.querySelector( `[name="${ k }"]` );
			if ( el && el.type === 'checkbox' ) {
				el.checked = !! v;
			} else if ( el ) {
				el.value = v ?? '';
			}
		} );
		again.querySelector( act === 'ask-row' ? '[data-act="remove-row"]' : '[data-act="ask-row"]' ).focus();
	} else if ( act === 'remove-row' ) {
		try {
			await pressing( t, () => api( `/admin/${ path }/${ id }`, { method: 'DELETE' } ) );
			S.data[ path ] = S.data[ path ].filter( ( r ) => String( r.id ) !== String( id ) );
			S.open[ path ] = null;
			await refreshModule();
			drawSection( sectionOf( path ) );
			S.root.querySelector( `#h-${ sectionOf( path ) }` ).focus();
			say( 'Removed.' );
		} catch ( err ) {
			showError( t.closest( 'form' ), err );
		}
	} else if ( act === 'copy' ) {
		try {
			await navigator.clipboard.writeText( t.dataset.code );
			t.textContent = 'Copied';
			say( 'Shortcode copied.' );
			setTimeout( () => ( t.textContent = 'Copy' ), 2000 );
		} catch ( err ) {
			say( 'Select the shortcode and copy it.' );
		}
	} else if ( act === 'ask-remove' || act === 'keep-module' ) {
		S.ask.remove = act === 'ask-remove';
		drawSection( 'basics' );
		S.root.querySelector( act === 'ask-remove' ? '[data-act="remove-module"]' : '[data-act="ask-remove"]' ).focus();
	} else if ( act === 'remove-module' ) {
		try {
			await pressing( t, () => api( `/admin/modules/${ S.id }`, { method: 'DELETE' } ) );
			location.href = cfg.urls.modules;
		} catch ( err ) {
			t.closest( '.jsb-ask' ).querySelector( '.jsb-err' ).innerHTML = `<p class="jsb-msg error">${ esc( err.message ) }</p>`;
			say( err.message );
		}
	}
}

/** Live parts of the forms: the hours rows switch on and off, the colour previews before it is saved. */
function onInput( e ) {
	const t = e.target;
	if ( t.dataset.k === 'on' ) {
		t.closest( '.day' ).querySelectorAll( 'input[type="time"]' ).forEach( ( i ) => ( i.disabled = ! t.checked ) );
	} else if ( t.name === 'limit' ) {
		t.closest( 'form' ).querySelector( '.jsb-hours' ).hidden = ! t.checked;
	} else if ( t.name === 'accent' || t.name === 'accent_hex' ) {
		const form = t.closest( 'form' );
		const hex = t.value.trim();
		if ( /^#[0-9a-f]{6}$/i.test( hex ) ) {
			form.querySelector( t.name === 'accent' ? '[name="accent_hex"]' : '[name="accent"]' ).value = hex.toLowerCase();
			preview( { accent: hex } );
		}
	} else if ( t.name === 'font' && t.closest( '[data-form="look"]' ) ) {
		preview( { font: t.value } );
	}
}

/** The preview is the real widget: these are the same two properties it sets from the saved look. */
function preview( { accent, font } ) {
	const w = S.root.querySelector( '.jsb-preview jsb-booking' );
	const note = S.root.querySelector( '.jsb-contrast' );
	if ( accent ) {
		const ratio = contrast( accent );
		w?.style.setProperty( '--jsb-accent', accent );
		w?.style.setProperty( '--jsb-on-accent', ratio >= 4.5 ? '#ffffff' : '#1c211b' );
		if ( note ) {
			note.textContent = ratio >= 4.5 ? `White text on this colour reads well (${ ratio.toFixed( 1 ) }:1).` : `This colour is light, so its buttons use dark text (white would be ${ ratio.toFixed( 1 ) }:1).`;
		}
	}
	if ( font ) {
		w?.style.setProperty( '--jsb-font', font === 'dm-sans' ? '"DM Sans", system-ui, sans-serif' : 'inherit' );
	}
}

function contrast( hex ) {
	const ch = ( i ) => {
		const c = parseInt( hex.slice( i, i + 2 ), 16 ) / 255;
		return c <= 0.03928 ? c / 12.92 : ( ( c + 0.055 ) / 1.055 ) ** 2.4;
	};
	return 1.05 / ( 0.2126 * ch( 1 ) + 0.7152 * ch( 3 ) + 0.0722 * ch( 5 ) + 0.05 );
}

// the preview needs the widget itself
if ( ! customElements.get( 'jsb-booking' ) && cfg.urls ) {
	import( cfg.urls.assets + 'front/jsb-booking.js?ver=' + cfg.version );
}
