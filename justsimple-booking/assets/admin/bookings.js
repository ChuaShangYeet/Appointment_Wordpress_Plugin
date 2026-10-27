/**
 * Bookings: "Find a booking and act on it."
 *
 * The table is the screen: tabs for where bookings stand, a search that finds a code, a name or a phone number however it is
 * spaced, a date range, and the filters this kind of business has. Each row carries its one main next step; the rest is in the
 * detail sheet. j and k move through the rows, Enter opens one, Esc closes it.
 */
import { api, esc, hm, ymdOf, dayLabel, plural, pill, STATUS, say, loading, errorHtml, pressing, loadModules, currentModule, rememberModule, moduleChooser, noModulesHtml } from './app.js';
import { openSheet } from './detail.js';

const root = document.getElementById( 'jsb-admin' );
const LIMIT = 500;
const ARRIVES = [ 'salon', 'workshop', 'field' ];
const TABS = [
	[ 'upcoming', 'Coming up', [ 'pending', 'confirmed' ] ],
	[ 'pending', 'Waiting to confirm', [ 'pending' ] ],
	[ 'working', 'In progress', [ 'in-progress', 'waiting-for-parts' ] ],
	[ 'done', 'Done', [ 'completed' ] ],
	[ 'off', 'Cancelled and missed', [ 'cancelled', 'rejected', 'no-show' ] ],
	[ 'all', 'All', null ],
];
const st = { modules: [], m: null, rows: [], tab: 'upcoming', q: '', from: '', to: '', vip: '', clash: false, sel: -1, error: null, loaded: false };

start();

async function start() {
	try {
		st.modules = await loadModules();
	} catch ( e ) {
		root.innerHTML = errorHtml( e );
		return;
	}
	st.m = currentModule( st.modules );
	if ( ! st.m ) {
		root.innerHTML = noModulesHtml();
		return;
	}
	rememberModule( st.m );
	root.addEventListener( 'click', onClick );
	root.addEventListener( 'change', onChange );
	root.addEventListener( 'input', onInput );
	document.addEventListener( 'keydown', onKey );
	drawShell();
	await load();
}

async function load( flash ) {
	const list = root.querySelector( '.jsb-list' );
	const req = api( `/admin/modules/${ st.m.id }/bookings`, { query: { q: st.q, from: st.from, to: st.to, table_type: st.vip, schedule_conflict: st.clash ? 1 : '' } } );
	const ticket = ( st.ticket = ( st.ticket || 0 ) + 1 );
	try {
		const rows = await loading( list, req, 'Finding bookings…' );
		if ( ticket !== st.ticket ) {
			return;
		}
		st.rows = rows;
		st.error = null;
	} catch ( e ) {
		if ( ticket !== st.ticket ) {
			return;
		}
		st.error = e;
	}
	st.loaded = true;
	drawList( flash );
}

// ------------------------------------------------------------------ drawing

function drawShell() {
	const kind = st.m.type;
	root.innerHTML = moduleChooser( st.modules, st.m ) +
		`<div class="jsb-title" style="margin-top:20px"><div><h2>Bookings</h2><p class="jsb-finding" id="jsb-range"></p></div></div>` +
		`<div class="jsb-states" role="tablist" aria-label="Where bookings stand"></div>` +
		`<div class="jsb-toolbar"><label class="jsb-vh" for="jsb-q">Search</label><input id="jsb-q" type="search" placeholder="Code, name or phone" value="${ esc( st.q ) }" autocomplete="off">` +
		`<label for="jsb-from">From</label><input id="jsb-from" type="date" value="${ esc( st.from ) }">` +
		`<label for="jsb-to">to</label><input id="jsb-to" type="date" value="${ esc( st.to ) }">` +
		( kind === 'restaurant' ? `<label class="jsb-vh" for="jsb-vip">Room</label><select id="jsb-vip"><option value="">All rooms</option><option value="normal"${ st.vip === 'normal' ? ' selected' : '' }>Main dining room</option><option value="vip"${ st.vip === 'vip' ? ' selected' : '' }>VIP room</option></select>` : '' ) +
		( kind === 'field' || kind === 'salon' ? `<label class="jsb-inline"><input id="jsb-clash" type="checkbox"${ st.clash ? ' checked' : '' }> Runs into the next</label>` : '' ) +
		`<span class="spacer"></span><span class="total" id="jsb-total" role="status"></span></div>` +
		`<div class="jsb-list"></div>`;
}

function shown() {
	const tab = TABS.find( ( t ) => t[ 0 ] === st.tab );
	return tab[ 2 ] ? st.rows.filter( ( r ) => tab[ 2 ].includes( r.status ) ) : st.rows;
}

function drawList( flash ) {
	const focusRow = document.activeElement && document.activeElement.closest && document.activeElement.closest( 'tr[data-booking]' );
	const rows = shown();
	root.querySelector( '#jsb-range' ).textContent = st.from || st.to
		? `${ st.from ? dayLabel( st.from, { day: 'numeric', month: 'short', year: 'numeric' } ) : 'Today' } to ${ st.to ? dayLabel( st.to, { day: 'numeric', month: 'short', year: 'numeric' } ) : '30 days on' }`
		: 'From today, the next 30 days.';
	root.querySelector( '.jsb-states' ).innerHTML = TABS.map( ( [ key, label, sts ] ) => {
		const n = sts ? st.rows.filter( ( r ) => sts.includes( r.status ) ).length : st.rows.length;
		return `<button type="button" role="tab" data-tab="${ key }" aria-selected="${ st.tab === key }" tabindex="${ st.tab === key ? 0 : -1 }">${ label }<span class="n">${ st.loaded ? n : '' }</span></button>`;
	} ).join( '' );
	root.querySelector( '#jsb-total' ).textContent = st.loaded ? plural( rows.length, 'booking', 'bookings' ) : '';
	const list = root.querySelector( '.jsb-list' );
	if ( st.error ) {
		list.innerHTML = errorHtml( st.error );
		return;
	}
	if ( ! rows.length ) {
		let head;
		let text;
		if ( st.rows.length ) {
			// found, just not in this tab: say where
			const where = TABS.filter( ( t ) => t[ 2 ] && st.rows.some( ( r ) => t[ 2 ].includes( r.status ) ) ).map( ( t ) => `“${ t[ 1 ] }”` );
			head = st.q ? `“${ esc( st.q ) }” is not in this tab` : 'Nothing in this tab';
			text = `${ plural( st.rows.length, 'booking', 'bookings' ) } ${ st.q ? 'match' + ( st.rows.length === 1 ? 'es' : '' ) : 'in these dates' } under ${ where.join( ' and ' ) }.`;
		} else if ( st.q ) {
			head = `Nothing matches “${ esc( st.q ) }”`;
			text = 'Try part of the name, the last digits of the phone number, or the first letters of the code.';
		} else {
			head = 'No bookings here';
			text = 'Nothing booked in these dates yet. New bookings from the booking page show here.';
		}
		list.innerHTML = `<div class="jsb-empty"><b>${ head }</b>${ text }</div>`;
		return;
	}
	const kind = st.m.type;
	list.innerHTML = `<table class="jsb-table"><thead><tr><th scope="col">When</th><th scope="col">Customer</th><th scope="col">${ kind === 'restaurant' ? 'Party' : 'What' }</th><th scope="col">${ kind === 'restaurant' ? 'Tables' : 'With' }</th><th scope="col">Status</th><th scope="col"><span class="jsb-vh">Next step</span></th></tr></thead><tbody>` +
		rows.map( ( b, i ) => row( b, i, kind ) ).join( '' ) +
		`</tbody></table>` + ( st.rows.length >= LIMIT ? `<p class="jsb-more">Showing the first ${ LIMIT }. Narrow the dates to see the rest.</p>` : '' );
	if ( flash ) {
		list.querySelector( `tr[data-booking="${ flash }"]` )?.classList.add( 'flash' );
	}
	if ( focusRow ) {
		list.querySelector( `tr[data-booking="${ focusRow.dataset.booking }"]` )?.focus();
	}
}

function row( b, i, kind ) {
	const when = `<b>${ esc( dayLabel( ymdOf( b.start ), { weekday: 'short', day: 'numeric', month: 'short' } ) ) }</b> ${ hm( b.start ) }–${ hm( b.end ) }`;
	const who = `<b>${ esc( b.customer.name ) }</b>${ b.mine === false ? '<small>A colleague\'s booking</small>' : b.customer.phone ? `<small>${ esc( b.customer.phone ) }</small>` : '' }`;
	const what = kind === 'restaurant'
		? `${ plural( b.party_size || 0, 'person', 'people' ) }${ b.table_type === 'vip' ? ' · VIP' : '' }`
		: ( b.services || [] ).map( ( s ) => esc( s.name ) ).join( ' + ' ) + ( b.vehicle ? `<small>${ esc( b.vehicle.plate_no ) } · ${ esc( b.vehicle.make + ' ' + b.vehicle.model ) }</small>` : '' );
	const res = kind === 'restaurant' ? b.tables || [] : [ ...( b.staff || [] ), ...( b.bays || [] ) ];
	let next = '';
	if ( b.mine !== false ) {
		if ( b.status === 'pending' ) {
			next = [ 'confirmed', 'Confirm' ];
		} else if ( b.status === 'confirmed' && ARRIVES.includes( kind ) ) {
			next = [ 'in-progress', kind === 'field' ? 'Started' : 'Arrived' ];
		} else if ( b.status === 'confirmed' || b.status === 'in-progress' ) {
			next = [ 'completed', 'Done' ];
		}
	}
	return `<tr data-booking="${ b.id }" tabindex="-1"${ i === st.sel ? ' aria-selected="true"' : '' }>` +
		`<td class="when">${ when }</td><td class="who">${ who }</td><td class="what">${ what }</td><td class="with">${ res.map( ( r ) => esc( r.name ) ).join( ', ' ) || '–' }</td>` +
		`<td>${ pill( b.status ) }${ b.schedule_conflict ? ' <span class="jsb-pill clash">Runs late</span>' : '' }</td>` +
		`<td class="act">${ next ? `<button type="button" class="jsb-btn${ next[ 0 ] === 'confirmed' ? ' primary' : '' }" data-act="step" data-to="${ next[ 0 ] }" data-busy="…" aria-label="${ esc( next[ 1 ] + ': ' + b.customer.name + ', ' + hm( b.start ) ) }">${ next[ 1 ] }</button>` : '' }<button type="button" class="jsb-btn" data-act="open" aria-label="${ esc( 'Open ' + b.customer.name + ', ' + hm( b.start ) ) }">Open</button></td></tr>`;
}

// ------------------------------------------------------------------ events

function open( tr ) {
	const id = parseInt( tr.dataset.booking, 10 );
	st.sel = [ ...root.querySelectorAll( 'tbody tr' ) ].indexOf( tr );
	root.querySelectorAll( 'tbody tr' ).forEach( ( r, i ) => r.setAttribute( 'aria-selected', String( i === st.sel ) ) );
	openSheet( id, { kind: st.m.type, host: root, returnFocus: tr.querySelector( '[data-act="open"]' ), onChanged: ( b ) => load( b.id ) } );
}

async function onClick( e ) {
	const tab = e.target.closest( 'a[data-module]' );
	if ( tab ) {
		e.preventDefault();
		pick( parseInt( tab.dataset.module, 10 ) );
		return;
	}
	const t = e.target.closest( '[data-tab], [data-act], tbody tr' );
	if ( ! t ) {
		return;
	}
	if ( t.dataset.tab ) {
		st.tab = t.dataset.tab;
		st.sel = -1;
		drawList();
		root.querySelector( `[data-tab="${ st.tab }"]` ).focus();
		return;
	}
	if ( t.dataset.act === 'retry' ) {
		load();
		return;
	}
	const tr = t.closest( 'tr[data-booking]' );
	if ( t.dataset.act === 'step' && tr ) {
		const id = parseInt( tr.dataset.booking, 10 );
		try {
			await pressing( t, () => api( `/admin/bookings/${ id }/status`, { method: 'PATCH', data: { status: t.dataset.to } } ) );
			say( STATUS[ t.dataset.to ] + '.' );
			await load( id );
			root.querySelector( `tr[data-booking="${ id }"] [data-act="open"]` )?.focus();
		} catch ( err ) {
			tr.insertAdjacentHTML( 'afterend', `<tr><td colspan="6"><p class="jsb-msg error" role="alert">${ esc( err.message ) }</p></td></tr>` );
		}
		return;
	}
	if ( tr ) {
		open( tr );
	}
}

let typing;
function onInput( e ) {
	if ( e.target.id === 'jsb-q' ) {
		clearTimeout( typing );
		typing = setTimeout( () => {
			st.q = e.target.value.trim();
			load();
		}, 250 );
	}
}

function onChange( e ) {
	const t = e.target;
	if ( t.dataset.act === 'module' ) {
		pick( parseInt( t.value, 10 ) );
		return;
	}
	const map = { 'jsb-from': 'from', 'jsb-to': 'to', 'jsb-vip': 'vip' };
	if ( map[ t.id ] ) {
		st[ map[ t.id ] ] = t.value;
		load();
	} else if ( t.id === 'jsb-clash' ) {
		st.clash = t.checked;
		load();
	}
}

/** j / k through the rows, Enter opens; the status tabs move with the arrow keys. */
function onKey( e ) {
	const tag = ( e.target.tagName || '' ).toLowerCase();
	if ( e.target.closest && e.target.closest( '.jsb-states' ) && ( e.key === 'ArrowRight' || e.key === 'ArrowLeft' ) ) {
		const i = TABS.findIndex( ( t ) => t[ 0 ] === st.tab );
		st.tab = TABS[ ( i + ( e.key === 'ArrowRight' ? 1 : TABS.length - 1 ) ) % TABS.length ][ 0 ];
		drawList();
		root.querySelector( `[data-tab="${ st.tab }"]` ).focus();
		return;
	}
	if ( [ 'input', 'select', 'textarea' ].includes( tag ) || e.metaKey || e.ctrlKey || e.altKey || document.querySelector( '.jsb-sheet' ) && e.key !== 'j' && e.key !== 'k' ) {
		return;
	}
	const rows = [ ...root.querySelectorAll( 'tbody tr[data-booking]' ) ];
	if ( ! rows.length ) {
		return;
	}
	if ( e.key === 'j' || e.key === 'k' ) {
		e.preventDefault();
		st.sel = Math.max( 0, Math.min( rows.length - 1, st.sel + ( e.key === 'j' ? 1 : -1 ) ) );
		rows.forEach( ( r, i ) => r.setAttribute( 'aria-selected', String( i === st.sel ) ) );
		rows[ st.sel ].focus();
		if ( document.querySelector( '.jsb-sheet' ) ) {
			open( rows[ st.sel ] ); // with the sheet open, j/k walk the bookings in it
		}
	} else if ( e.key === 'Enter' && e.target.matches && e.target.matches( 'tr[data-booking]' ) ) {
		e.preventDefault();
		open( e.target );
	}
}

function pick( id ) {
	st.m = st.modules.find( ( m ) => m.id === id ) || st.m;
	rememberModule( st.m );
	st.sel = -1;
	drawShell();
	load();
}
