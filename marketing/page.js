/**
 * The marketing page's behaviour: the hero's rooms, one for each kind of business, the sections rising in once, and the one
 * live thing: five demo businesses, each opening the plugin's real booking form on a starting booking ("Table for 8, Friday
 * 8pm"), answering live against the demo site's engine when the Showcase plugin serves this page, and otherwise from times the
 * real engine worked out (demo/*.json, recorded by tests/record-demo.php).
 */
import { DemoSource } from './widget/demo-source.js';
import { RestSource } from './widget/jsb-booking.js';

document.documentElement.classList.add( 'js' );
const still = matchMedia( '(prefers-reduced-motion: reduce)' ).matches;

// ------------------------------------------------------------------ the slot: a table, a chair, a bay, a visit, a treatment

// The hero is the room being booked, and the slot in the headline holds the same picture. When the line under the current word
// has run its length, the next room fades in, in the slot and behind the whole hero together. The words choose a room
// directly; without motion nothing turns on its own.
const hero = document.querySelector( '.hero' );
const slot = document.querySelector( '.slot' );
const frames = [ ...slot.querySelectorAll( 'img' ) ];
const rooms = [ ...document.querySelectorAll( '.room' ) ];
const picks = [ ...document.querySelectorAll( '.doors-pick button' ) ];
const tryLine = document.querySelector( '.doors-try' );
const DOORS = [
	[ 'Salons: stylists, chairs and add-ons.', 'salon', 'Mei Hair Studio' ],
	[ 'Restaurants: tables joined for a large party.', 'restaurant', 'Rumah Makan Seri' ],
	[ 'Workshops: bays, mechanics and parts.', 'workshop', 'Bengkel Ah Kow' ],
	[ 'Home services: travel time between visits.', 'field', 'CoolAir Services' ],
	[ 'Clinics and spas: rooms and practitioners.', 'staff', 'Klinik Dr Wong' ],
];
let room = 0;

function enter( n ) {
	if ( n === room ) {
		return;
	}
	const from = rooms[ room ];
	const to = rooms[ n ];
	rooms.forEach( ( r ) => r.classList.remove( 'under' ) );
	from.classList.add( 'under' );
	from.classList.remove( 'on' );
	to.classList.add( 'on' );
	room = n;
	frames.forEach( ( f, i ) => f.classList.toggle( 'on', i === n ) );
	picks.forEach( ( p, i ) => p.setAttribute( 'aria-pressed', String( i === n ) ) );
	const [ what, kind, name ] = DOORS[ n ];
	tryLine.firstElementChild.textContent = what;
	tryLine.lastElementChild.textContent = `Book at ${ name }`;
	tryLine.lastElementChild.dataset.try = kind;
}
picks.forEach( ( p, i ) => {
	p.addEventListener( 'click', () => enter( i ) );
	p.addEventListener( 'animationend', () => enter( ( i + 1 ) % rooms.length ) );
} );
tryLine.lastElementChild.addEventListener( 'click', ( e ) => {
	document.querySelector( `.biz [data-kind="${ e.currentTarget.dataset.try }"]` ).click();
} );
// the line under the word pauses while someone reads the headline, works the words, or has scrolled past
const hold = ( on ) => hero.classList.toggle( 'held', on );
let pointer = false;
let away = false;
const update = () => hold( pointer || away || hero.contains( document.activeElement ) );
document.querySelector( '.hero-title' ).addEventListener( 'pointerenter', () => ( ( pointer = true ), update() ) );
document.querySelector( '.hero-title' ).addEventListener( 'pointerleave', () => ( ( pointer = false ), update() ) );
hero.addEventListener( 'focusin', update );
hero.addEventListener( 'focusout', () => setTimeout( update ) );
new IntersectionObserver( ( [ e ] ) => ( ( away = ! e.isIntersecting ), update() ), { threshold: 0.25 } ).observe( hero );

// ------------------------------------------------------------------ sections rise in once

// Only what starts below the first screen waits to rise in; what is on screen at load is simply there, and nothing stays hidden
// if the observer never fires (a thumbnail, a print, a skim).
const io = new IntersectionObserver( ( entries ) => {
	for ( const e of entries ) {
		if ( e.isIntersecting ) {
			e.target.classList.add( 'in' );
			io.unobserve( e.target );
		}
	}
}, { rootMargin: '0px 0px -12% 0px' } );
document.querySelectorAll( '[data-reveal]' ).forEach( ( s ) => {
	if ( s.getBoundingClientRect().top < innerHeight ) {
		s.classList.add( 'in' );
		return;
	}
	s.classList.add( 'wait' );
	io.observe( s );
} );
setTimeout( () => document.querySelectorAll( '[data-reveal].wait:not(.in)' ).forEach( ( s ) => s.classList.add( 'in' ) ), 4000 );

// ------------------------------------------------------------------ the demo businesses

const holder = document.getElementById( 'answer-widget' );
const note = document.getElementById( 'answer-note' );
const NOTE = note.textContent;
const businesses = [ ...document.querySelectorAll( '.biz button' ) ];

const WEEKDAYS = [ 'sunday', 'monday', 'tuesday', 'wednesday', 'thursday', 'friday', 'saturday' ];
const has = ( text, words ) => words.some( ( w ) => text.includes( w ) );

/** Today in the demo businesses' own clock (Kuala Lumpur), and the next date for a weekday word. */
function dayFrom( text ) {
	const today = new Intl.DateTimeFormat( 'en-CA', { timeZone: 'Asia/Kuala_Lumpur' } ).format( new Date() );
	const at = ( n ) => {
		const d = new Date( today + 'T12:00:00Z' );
		d.setUTCDate( d.getUTCDate() + n );
		return d.toISOString().slice( 0, 10 );
	};
	if ( text.includes( 'tomorrow' ) ) {
		return at( 1 );
	}
	if ( text.includes( 'today' ) || text.includes( 'tonight' ) ) {
		return at( 0 );
	}
	const i = WEEKDAYS.findIndex( ( w ) => text.includes( w ) || text.includes( w.slice( 0, 3 ) + ' ' ) );
	if ( i < 0 ) {
		return null;
	}
	// "Tuesday", said on a Tuesday, means the next one ("today" is its own word)
	const now = new Date( today + 'T12:00:00Z' ).getUTCDay();
	return at( ( i - now + 7 ) % 7 || 7 );
}

/** What a business's starting line asks for: which demo business, and the choices to fill in. */
function read( raw ) {
	const t = ' ' + raw.toLowerCase().replace( /[,.!?]/g, ' ' ) + ' ';
	const day = dayFrom( t );
	if ( has( t, [ 'table', 'party', 'people', 'pax', 'dinner', 'lunch', 'vip' ] ) ) {
		const n = ( t.match( /(?:for|party of|table of)?\s(\d{1,3})\s(?!pm|am)/ ) || [] )[ 1 ];
		return { kind: 'restaurant', day, party: Math.max( 1, Math.min( 200, parseInt( n || '2', 10 ) ) ), vip: t.includes( 'vip' ) };
	}
	if ( has( t, [ 'brake', 'oil', 'car', 'myvi', 'axia', 'saga', ' city ', 'vios', 'tyre', 'workshop', 'gas' ] ) ) {
		return { kind: 'workshop', day, t };
	}
	if ( has( t, [ 'aircon', 'air-con', 'air con', 'at my place', 'at home', 'visit', 'video', 'call' ] ) ) {
		return { kind: 'field', day, t };
	}
	if ( has( t, [ 'hair', 'cut', 'colour', 'color', 'gel', 'wash', 'blow', 'salon', 'scalp', 'treatment' ] ) ) {
		return { kind: 'salon', day, t };
	}
	if ( has( t, [ 'doctor', 'clinic', 'consult', 'vaccin', 'follow', 'check-up', 'checkup', 'gp ' ] ) ) {
		return { kind: 'staff', day, t };
	}
	return null;
}

/** The choices for the widget, named from the demo business's own services and cars. */
function choices( ask, data ) {
	const m = data.module;
	const svc = ( word ) => ( m.services.find( ( s ) => s.name.toLowerCase().includes( word ) ) || {} ).id;
	const t = ask.t || '';
	switch ( ask.kind ) {
		case 'restaurant': {
			const type = ask.vip && m.restaurant.vip.largest_table ? 'vip' : 'normal';
			return { day: ask.day, party: ask.party, tableType: type, separate: ask.party > m.restaurant[ type ].largest_table };
		}
		case 'salon': {
			const base = t.includes( 'colour' ) || t.includes( 'color' ) ? svc( 'colour' ) : t.includes( 'wash' ) || t.includes( 'blow' ) ? svc( 'wash' ) : svc( 'haircut' );
			const addons = [ t.includes( 'gel' ) && svc( 'gel' ), ( t.includes( 'scalp' ) || t.includes( 'treatment' ) ) && svc( 'scalp' ) ].filter( Boolean );
			return { day: ask.day, service: base, addons };
		}
		case 'workshop': {
			const services = [ t.includes( 'oil' ) && svc( 'oil' ), t.includes( 'brake' ) && svc( 'brake' ), ( t.includes( 'aircon' ) || t.includes( 'gas' ) ) && svc( 'aircon' ) ].filter( Boolean );
			const car = data.vehicles.find( ( v ) => t.includes( v.model.toLowerCase() ) ) || data.vehicles[ 0 ];
			const year = ( t.match( /\b(19|20)\d\d\b/ ) || [] )[ 0 ] || '';
			return { day: ask.day, services: services.length ? services : [ svc( 'oil' ) ], car: { model: String( car.id ), year, plate: 'WXY 1234' } };
		}
		case 'field': {
			const id = t.includes( 'video' ) || t.includes( 'call' ) ? svc( 'video' ) : t.includes( 'servic' ) ? svc( 'servicing' ) : svc( 'check' );
			return { day: ask.day, service: id, loc: { lat: 3.1478, lon: 101.6953 } }; // a street in Kuala Lumpur, as if picked on the map
		}
		default: {
			const id = t.includes( 'vaccin' ) ? svc( 'vaccin' ) : t.includes( 'follow' ) ? svc( 'follow' ) : svc( 'consult' );
			return { day: ask.day, service: id };
		}
	}
}

// On the live demo site (the JustSimple Showcase plugin serves this page) window.jsbShowcase names the site's REST address and
// its five demo booking pages: the widget then books for real against the engine. Anywhere else, or when the live site does
// not answer with JSON (a host's browser check, an outage), it replays the recorded answers. ?demo=recorded forces those.
const LIVE = window.jsbShowcase && new URLSearchParams( location.search ).get( 'demo' ) !== 'recorded' ? window.jsbShowcase : null;
const NOTES = {
	live: 'The plugin\'s real booking form, booking live on this demo site. Everything here is wiped at 3 am each night, and no email is sent.',
	recorded: NOTE,
	fallback: 'The live demo is not answering just now, so this replays times the engine worked out earlier. Nothing you book here is real.',
};

/** { source, id, data: { module, vehicles }, how: 'live' | 'recorded' | 'fallback' } for one kind of business. */
async function sourceFor( kind ) {
	const id = LIVE && LIVE.pages && LIVE.pages[ kind ];
	if ( id ) {
		try {
			const get = async ( path ) => {
				const r = await fetch( `${ LIVE.api }${ path }`, { credentials: 'omit' } );
				if ( ! r.ok || ! ( r.headers.get( 'content-type' ) || '' ).includes( 'json' ) ) {
					throw new Error( 'not JSON' );
				}
				return r.json();
			};
			const module = await get( `/modules/${ id }` );
			const vehicles = kind === 'workshop' ? await get( `/modules/${ id }/supported-vehicles` ) : [];
			const source = new RestSource( LIVE.api );
			source.demo = 'live';
			return { source, id, data: { module, vehicles }, how: 'live' };
		} catch ( e ) {
			// falls through to the recorded answers
		}
	}
	const data = await fetch( new URL( `./demo/${ kind }.json`, import.meta.url ) ).then( ( r ) => r.json() );
	return { source: new DemoSource( data ), id: 1, data, how: id ? 'fallback' : 'recorded' };
}

const cache = {};
let shown = null; // { kind, el, how }

async function answer( line ) {
	const ask = read( line );
	try {
		cache[ ask.kind ] = cache[ ask.kind ] || sourceFor( ask.kind );
		const { source, id, data, how } = await cache[ ask.kind ];
		note.textContent = NOTES[ how ];
		if ( ! shown || shown.kind !== ask.kind ) {
			const el = document.createElement( 'jsb-booking' );
			el.setAttribute( 'module', String( id ) );
			el.setAttribute( 'css', new URL( './widget/jsb-booking.css', import.meta.url ).href );
			el.source = source;
			holder.replaceChildren( el );
			shown = { kind: ask.kind, el, how };
		}
		shown.el.preset( choices( ask, data ) );
	} catch ( e ) {
		holder.innerHTML = '<p class="hint">The demo could not load. The page needs to be opened from a web address, not as a file.</p>';
	}
}

// each business opens on its own starting line, already filled in; the visitor carries on from there
businesses.forEach( ( b ) => b.addEventListener( 'click', () => {
	businesses.forEach( ( x ) => x.setAttribute( 'aria-pressed', String( x === b ) ) );
	answer( b.dataset.ask );
} ) );
answer( ( businesses.find( ( b ) => b.getAttribute( 'aria-pressed' ) === 'true' ) || businesses[ 0 ] ).dataset.ask );
