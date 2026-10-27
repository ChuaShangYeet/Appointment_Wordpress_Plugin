/**
 * Booking pages: "Which booking pages do I have, and where are they?"
 *
 * One row per module: what kind it is, whether it is on, today's bookings, how ready it is, and its shortcode to copy.
 * A new one is made in place (a name and a kind) and opens straight into its setup. ?page=jsb-modules&edit=ID is the setup.
 */
import { api, esc, plural, KIND, say, errorHtml, pressing } from './app.js';
import { openSetup } from './setup.js';

const root = document.getElementById( 'jsb-admin' );
const KINDS = [
	[ 'staff', 'Appointments', 'A clinic, a tutor, a consultant: one person, one booking at a time.' ],
	[ 'salon', 'Salon', 'Services with add-ons, done by a stylist.' ],
	[ 'restaurant', 'Restaurant', 'Tables by party size, combined for big groups, VIP rooms.' ],
	[ 'workshop', 'Workshop', 'A mechanic and a bay for each job, the customer\'s car, parts on order.' ],
	[ 'field', 'Field service', 'Visits at the customer\'s place, online meetings, or at your office.' ],
];
const st = { modules: [], filter: 'on', q: '', adding: false };

const edit = parseInt( new URLSearchParams( location.search ).get( 'edit' ), 10 );
if ( edit ) {
	openSetup( root, edit );
} else {
	start();
}

async function start() {
	root.addEventListener( 'click', onClick );
	root.addEventListener( 'input', ( e ) => {
		if ( e.target.id === 'jsb-mq' ) {
			st.q = e.target.value.trim().toLowerCase();
			drawRows();
		}
	} );
	root.addEventListener( 'submit', onSubmit );
	await load();
}

async function load() {
	try {
		st.modules = await api( '/admin/modules' );
		st.error = null;
	} catch ( e ) {
		st.error = e;
	}
	draw();
}

const ready = ( m ) => ( m.readiness || [] ).filter( ( r ) => r.done ).length;

function draw() {
	const on = st.modules.filter( ( m ) => m.status === 'active' ).length;
	let h = `<div class="jsb-title"><div><h2>Booking pages</h2><p class="jsb-finding">${ st.modules.length ? `${ plural( on, 'page is', 'pages are' ) } taking bookings${ st.modules.length > on ? `, ${ st.modules.length - on } switched off` : '' }.` : 'Each booking page is one thing people book: haircuts, dinner tables, car services or home visits.' }</p></div>` +
		`<div class="jsb-tools"><button type="button" class="jsb-btn primary" data-act="new"${ st.adding ? ' hidden' : '' }>New booking page</button></div></div>`;
	if ( st.adding ) {
		h += newForm();
	}
	if ( st.error ) {
		h += errorHtml( st.error );
	} else if ( ! st.modules.length && ! st.adding ) {
		h += `<div class="jsb-card jsb-empty" style="margin-top:24px"><b>No booking pages yet</b>Make the first one: give it a name and choose what people book.</div>`;
	} else if ( st.modules.length ) {
		h += `<div class="jsb-states" role="tablist" aria-label="Which pages">${ [ [ 'on', 'On', on ], [ 'off', 'Switched off', st.modules.length - on ], [ 'all', 'All', st.modules.length ] ].map( ( [ k, l, n ] ) => `<button type="button" role="tab" data-filter="${ k }" aria-selected="${ st.filter === k }">${ l }<span class="n">${ n }</span></button>` ).join( '' ) }</div>` +
			`<div class="jsb-toolbar"><label class="jsb-vh" for="jsb-mq">Find a booking page</label><input id="jsb-mq" type="search" placeholder="Find by name" value="${ esc( st.q ) }"><span class="spacer"></span><span class="total" id="jsb-mtotal" role="status"></span></div><div class="jsb-mlist"></div>`;
	}
	root.innerHTML = h;
	if ( st.modules.length ) {
		drawRows();
	}
	if ( st.adding ) {
		root.querySelector( '#jsb-new-title' ).focus();
	}
}

function drawRows() {
	const rows = st.modules.filter( ( m ) => ( st.filter === 'all' || ( st.filter === 'on' ) === ( m.status === 'active' ) ) && ( ! st.q || m.title.toLowerCase().includes( st.q ) ) );
	root.querySelector( '#jsb-mtotal' ).textContent = plural( rows.length, 'page', 'pages' );
	const list = root.querySelector( '.jsb-mlist' );
	if ( ! rows.length ) {
		list.innerHTML = `<div class="jsb-empty"><b>${ st.q ? `No booking page called “${ esc( st.q ) }”` : st.filter === 'off' ? 'Every booking page is switched on' : 'No booking page is switched on' }</b>${ st.filter === 'on' && ! st.q ? 'Switch one on in its setup when it is ready.' : '' }</div>`;
		return;
	}
	list.innerHTML = `<table class="jsb-table"><thead><tr><th scope="col">Booking page</th><th scope="col">Status</th><th scope="col" class="num">Today</th><th scope="col">Ready</th><th scope="col">Shortcode</th><th scope="col"><span class="jsb-vh">Set up</span></th></tr></thead><tbody>` +
		rows.map( ( m ) => {
			const total = ( m.readiness || [] ).length;
			const done = ready( m );
			return `<tr data-module="${ m.id }">` +
				`<td class="who"><b>${ esc( m.title ) }</b><small>${ esc( KIND[ m.type ]?.what || m.type ) }</small></td>` +
				`<td>${ m.status === 'active' ? '<span class="jsb-pill confirmed">On</span>' : '<span class="jsb-pill cancelled">Off</span>' }</td>` +
				`<td class="num">${ m.today }</td>` +
				`<td>${ total ? ( done === total ? 'Ready' : `<span class="jsb-ready-n">${ done } of ${ total }</span>` ) : '–' }</td>` +
				`<td><code class="jsb-code">${ esc( m.shortcode ) }</code> <button type="button" class="jsb-link" data-act="copy" data-code="${ esc( m.shortcode ) }" aria-label="${ esc( 'Copy the shortcode for ' + m.title ) }">Copy</button></td>` +
				`<td class="act"><a class="jsb-btn" href="?page=jsb-modules&edit=${ m.id }">Set up</a></td></tr>`;
		} ).join( '' ) + `</tbody></table>`;
}

function newForm() {
	return `<form class="jsb-card jsb-new" style="margin-top:24px" novalidate><h3>New booking page</h3>` +
		`<div class="jsb-field"><label for="jsb-new-title">Name</label><input id="jsb-new-title" type="text" maxlength="150" placeholder="For example: Mei Hair Studio" required><span class="hint">Customers see it at the top of the booking form.</span></div>` +
		`<fieldset class="jsb-field"><legend>What do people book?</legend><div class="jsb-kinds">${ KINDS.map( ( [ k, l, d ], i ) => `<label class="jsb-kind"><input type="radio" name="kind" value="${ k }"${ i === 0 ? ' checked' : '' }><span><b>${ l }</b><small>${ d }</small></span></label>` ).join( '' ) }</div><span class="hint">This cannot be changed later: each kind books differently.</span></fieldset>` +
		`<div class="jsb-err" role="alert"></div><div class="jsb-actions"><button type="submit" class="jsb-btn primary" data-busy="Making it…">Make it and set it up</button><button type="button" class="jsb-btn" data-act="cancel-new">Cancel</button></div></form>`;
}

async function onSubmit( e ) {
	if ( ! e.target.classList.contains( 'jsb-new' ) ) {
		return;
	}
	e.preventDefault();
	const f = e.target;
	const title = f.querySelector( '#jsb-new-title' ).value.trim();
	const err = f.querySelector( '.jsb-err' );
	if ( ! title ) {
		err.innerHTML = '<p class="jsb-msg error">Give the booking page a name.</p>';
		f.querySelector( '#jsb-new-title' ).focus();
		return;
	}
	try {
		const m = await pressing( f.querySelector( '[type="submit"]' ), () => api( '/admin/modules', { method: 'POST', data: { title, type: f.querySelector( '[name="kind"]:checked' ).value } } ) );
		location.href = `?page=jsb-modules&edit=${ m.id }`;
	} catch ( e2 ) {
		err.innerHTML = `<p class="jsb-msg error">${ esc( e2.message ) }</p>`;
	}
}

async function onClick( e ) {
	const t = e.target.closest( '[data-act], [data-filter]' );
	if ( ! t ) {
		return;
	}
	if ( t.dataset.filter ) {
		st.filter = t.dataset.filter;
		root.querySelectorAll( '[data-filter]' ).forEach( ( b ) => b.setAttribute( 'aria-selected', String( b === t ) ) );
		drawRows();
	} else if ( t.dataset.act === 'new' ) {
		st.adding = true;
		draw();
	} else if ( t.dataset.act === 'cancel-new' ) {
		st.adding = false;
		draw();
		root.querySelector( '[data-act="new"]' ).focus();
	} else if ( t.dataset.act === 'copy' ) {
		try {
			await navigator.clipboard.writeText( t.dataset.code );
			t.textContent = 'Copied';
			say( 'Shortcode copied.' );
			setTimeout( () => ( t.textContent = 'Copy' ), 2000 );
		} catch ( err ) {
			t.closest( 'td' ).querySelector( 'code' ).focus();
			say( 'Select the shortcode and copy it.' );
		}
	} else if ( t.dataset.act === 'retry' ) {
		load();
	}
}

