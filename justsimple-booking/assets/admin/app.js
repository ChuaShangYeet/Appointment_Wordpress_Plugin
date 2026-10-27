/**
 * Shared by the admin screens: the REST client (wp.apiFetch, which sends the nonce), formatting in the business's own clock,
 * the module chooser, the states every block has (loading, empty, error) and the one live region.
 *
 * The rule the screens keep: nothing on screen changes before the server answers. A button shows it is working; the engine's
 * answer (or its refusal, in its own words) is what the screen then shows.
 */

export const cfg = window.jsbAdmin || {};
const SLOW_MS = 300;

export const esc = ( s ) => String( s ?? '' ).replace( /[&<>"']/g, ( c ) => ( { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' } )[ c ] );

/** The API speaks the business's local time with its offset, so the wall clock is in the string itself. */
export const hm = ( iso ) => ( iso ? iso.slice( 11, 16 ) : '–' );
export const ymdOf = ( iso ) => iso.slice( 0, 10 );
export const minutesOf = ( iso ) => parseInt( iso.slice( 11, 13 ), 10 ) * 60 + parseInt( iso.slice( 14, 16 ), 10 );
const noon = ( ymd ) => new Date( ymd + 'T12:00:00Z' );
export const addDays = ( ymd, n ) => {
	const d = noon( ymd );
	d.setUTCDate( d.getUTCDate() + n );
	return d.toISOString().slice( 0, 10 );
};
export const dayLabel = ( ymd, opts = { weekday: 'long', day: 'numeric', month: 'long' } ) => new Intl.DateTimeFormat( undefined, Object.assign( { timeZone: 'UTC' }, opts ) ).format( noon( ymd ) );
export const plural = ( n, one, many ) => `${ n } ${ n === 1 ? one : many }`;

export const STATUS = {
	pending: 'Waiting to confirm',
	confirmed: 'Confirmed',
	'in-progress': 'In progress',
	'waiting-for-parts': 'Waiting for parts',
	completed: 'Done',
	cancelled: 'Cancelled',
	rejected: 'Turned down',
	'no-show': 'Did not come',
};
export const pill = ( status ) => `<span class="jsb-pill ${ esc( status ) }">${ esc( STATUS[ status ] || status ) }</span>`;

/** The things a module books, by kind: what the board's rows are. */
export const KIND = {
	staff: { rows: 'staff', what: 'Appointments' },
	salon: { rows: 'stylists', what: 'Salon' },
	restaurant: { rows: 'tables', what: 'Restaurant' },
	workshop: { rows: 'mechanics and bays', what: 'Workshop' },
	field: { rows: 'professionals', what: 'Field service' },
};

export class ApiError extends Error {
	constructor( e ) {
		super( ( e && e.message ) || 'The server did not answer. Check your connection and try again.' );
		this.code = ( e && e.code ) || 'network';
		this.status = ( e && e.data && e.data.status ) || 0;
		this.data = ( e && e.data ) || {};
	}
}

export async function api( path, { method = 'GET', data, query } = {} ) {
	const qs = query ? '?' + new URLSearchParams( Object.entries( query ).filter( ( [ , v ] ) => v !== null && v !== undefined && v !== '' ) ).toString() : '';
	try {
		return await window.wp.apiFetch( { path: '/justsimple/v1' + path + ( qs === '?' ? '' : qs ), method, data } );
	} catch ( e ) {
		throw new ApiError( e );
	}
}

// ------------------------------------------------------------------ live region

let live;
export function say( text ) {
	if ( ! live ) {
		live = document.createElement( 'div' );
		live.className = 'jsb-vh';
		live.setAttribute( 'aria-live', 'polite' );
		document.body.appendChild( live );
	}
	live.textContent = '';
	setTimeout( () => ( live.textContent = text ), 30 );
}

// ------------------------------------------------------------------ states

/** Runs `promise`; while it is slow (over 300 ms) the old content dims and one line says what is happening. */
export async function loading( el, promise, text = 'Loading…' ) {
	let note = null;
	const timer = setTimeout( () => {
		el.classList.add( 'jsb-loading' );
		el.setAttribute( 'aria-busy', 'true' );
		note = document.createElement( 'p' );
		note.className = 'jsb-slow';
		note.setAttribute( 'role', 'status' );
		note.textContent = text;
		el.prepend( note );
	}, SLOW_MS );
	try {
		return await promise;
	} finally {
		clearTimeout( timer );
		el.classList.remove( 'jsb-loading' );
		el.removeAttribute( 'aria-busy' );
		if ( note ) {
			note.remove();
		}
	}
}

export const errorHtml = ( e, again = true ) => `<div class="jsb-msg error" role="alert">${ esc( e.message ) }${ again ? ' <button type="button" class="jsb-link" data-act="retry">Try again</button>' : '' }</div>`;

/** A button that says it is working until the server answers. */
export async function pressing( btn, fn ) {
	if ( btn.getAttribute( 'aria-busy' ) === 'true' ) {
		return null;
	}
	const label = btn.textContent;
	btn.setAttribute( 'aria-busy', 'true' );
	btn.dataset.label = label;
	btn.textContent = btn.dataset.busy || 'Saving…';
	try {
		return await fn();
	} finally {
		if ( btn.isConnected ) {
			btn.removeAttribute( 'aria-busy' );
			btn.textContent = label;
		}
	}
}

// ------------------------------------------------------------------ modules

const LAST = 'jsb-admin-module';

export async function loadModules() {
	const all = await api( '/admin/modules' );
	return all.filter( ( m ) => m.status === 'active' ).concat( all.filter( ( m ) => m.status !== 'active' ) );
}

/** The module in the URL, else the last one used here, else the first one switched on. */
export function currentModule( modules ) {
	const want = parseInt( new URLSearchParams( location.search ).get( 'module' ), 10 ) || parseInt( storage( LAST ), 10 );
	return modules.find( ( m ) => m.id === want ) || modules[ 0 ] || null;
}

export function rememberModule( m ) {
	storage( LAST, String( m.id ) );
	const u = new URL( location.href );
	u.searchParams.set( 'module', m.id );
	history.replaceState( null, '', u );
}

function storage( key, value ) {
	try {
		if ( value === undefined ) {
			return localStorage.getItem( key );
		}
		localStorage.setItem( key, value );
	} catch ( e ) {}
	return null;
}

/** Underline tabs for a handful of booking pages; a select once there are more than fit on one line. */
export function moduleChooser( modules, current ) {
	if ( modules.length <= 1 ) {
		return '';
	}
	const label = ( m ) => esc( m.title ) + ( m.status === 'active' ? '' : ' (off)' );
	if ( modules.length <= 5 ) {
		return `<nav class="jsb-tabs" aria-label="Booking pages">${ modules.map( ( m ) => `<a href="?page=${ esc( new URLSearchParams( location.search ).get( 'page' ) ) }&module=${ m.id }" data-module="${ m.id }"${ m.id === current.id ? ' aria-current="page"' : '' }>${ label( m ) }</a>` ).join( '' ) }</nav>`;
	}
	return `<div class="jsb-modpick"><label for="jsb-module">Booking page</label><select id="jsb-module" data-act="module">${ modules.map( ( m ) => `<option value="${ m.id }"${ m.id === current.id ? ' selected' : '' }>${ label( m ) } · ${ esc( KIND[ m.type ]?.what || m.type ) }</option>` ).join( '' ) }</select></div>`;
}

export const noModulesHtml = () =>
	`<div class="jsb-card jsb-empty"><b>No booking pages yet</b>${ cfg.manage ? `Make one under <a href="${ esc( cfg.urls.modules ) }">Booking pages</a>: a salon, a restaurant, a workshop or home visits. Its bookings show here.` : 'The owner has not made a booking page yet. Its bookings will show here.' }</div>`;
