/**
 * The marketing page's behaviour: the hero's rooms, one for each kind of business, the sections rising in once, and the one
 * live thing: five demo businesses, each opening the plugin's real booking form on a starting booking ("Table for 8, Friday
 * 8pm"), answering live against the demo site's engine when the Showcase plugin serves this page, and otherwise from times the
 * real engine worked out (demo/*.json, recorded by tests/record-demo.php).
 */
import { DemoSource } from './widget/demo-source.js?v={{v}}'; // the build puts its fingerprint here, so a new build is never an old cached file
import { RestSource } from './widget/jsb-booking.js?v={{v}}';
const VER = new URL( import.meta.url ).search; // this page's ?v=, passed on to the files it loads itself

document.documentElement.classList.add( 'js' );
const still = matchMedia( '(prefers-reduced-motion: reduce)' ).matches;

// ------------------------------------------------------------------ the visit log

// On the live demo the Showcase logs what a visit does, without cookies: the id lives only in this tab's memory, so nothing stays
// in the browser and a reload is a new visit. Steps go out every 10 seconds at most, and when the tab is hidden or closed.
const LOG = window.jsbShowcase && window.jsbShowcase.track;
const VISIT = LOG && Array.from( crypto.getRandomValues( new Uint8Array( 16 ) ), ( b ) => ( b % 36 ).toString( 36 ) ).join( '' );
const OPENED = Date.now();
const CAMPAIGN = new URLSearchParams( location.search ).get( 'utm_source' );
let cameFrom = CAMPAIGN ? 'utm:' + CAMPAIGN : document.referrer;
let steps = [];

function step( event, detail = '' ) {
	if ( LOG ) {
		steps.push( [ Math.round( ( Date.now() - OPENED ) / 1000 ), event, detail ] );
	}
}

function sendSteps() {
	if ( ! steps.length ) {
		return;
	}
	const body = JSON.stringify( { visit: VISIT, from: cameFrom, steps } );
	if ( navigator.sendBeacon( LOG, new Blob( [ body ], { type: 'application/json' } ) ) ) {
		steps = [];
		cameFrom = '';
	}
}

if ( LOG ) {
	step( 'open' );
	setInterval( sendSteps, 10000 );
	document.addEventListener( 'visibilitychange', () => {
		if ( document.visibilityState === 'hidden' ) {
			step( 'leave' );
			sendSteps();
		} else {
			step( 'back' );
		}
	} );
	document.addEventListener( 'click', ( e ) => {
		const a = e.target.closest( 'a' );
		if ( a && a.hostname.endsWith( 'onrender.com' ) ) {
			step( 'hosted' );
		}
	} );
	const places = { '#try': 'try', '#kinds': 'kinds', '.how': 'how', '#questions': 'questions', '#get': 'close', '.foot': 'foot' };
	const reached = new IntersectionObserver( ( entries ) => {
		for ( const e of entries ) {
			if ( e.isIntersecting ) {
				step( 'section', e.target.dataset.place );
				reached.unobserve( e.target );
			}
		}
	}, { rootMargin: '0px 0px -30% 0px' } );
	Object.entries( places ).forEach( ( [ selector, place ] ) => {
		const el = document.querySelector( selector );
		el.dataset.place = place;
		reached.observe( el );
	} );
}

// ------------------------------------------------------------------ the download, by emailed link

// On the live demo the Download buttons open a box asking for an email; the Showcase sends a private link. The page on its own
// keeps plain download links.
const GET = window.jsbShowcase && window.jsbShowcase.get;
if ( GET ) {
	const box = document.getElementById( 'get-box' );
	const form = document.getElementById( 'get-form' );
	const said = document.getElementById( 'get-msg' );
	const send = form.querySelector( '[type="submit"]' );
	document.querySelectorAll( '[data-zip]' ).forEach( ( a ) => a.addEventListener( 'click', ( e ) => {
		e.preventDefault();
		box.showModal();
	} ) );
	box.querySelector( '.get-close' ).addEventListener( 'click', () => box.close() );
	box.addEventListener( 'click', ( e ) => e.target === box && box.close() ); // a click on the backdrop
	form.addEventListener( 'submit', async ( e ) => {
		e.preventDefault();
		send.disabled = true;
		said.textContent = 'Sending…';
		const email = form.email.value.trim();
		try {
			const r = await fetch( GET, {
				method: 'POST',
				credentials: 'omit',
				headers: { 'Content-Type': 'application/json' },
				body: JSON.stringify( { email, website: form.website.value } ),
			} );
			const answer = await r.json();
			if ( ! r.ok ) {
				throw new Error( answer.message );
			}
			said.textContent = `Sent to ${ email }. Check your inbox: it can take a minute, and may land in spam.`;
			step( 'download' );
		} catch ( err ) {
			said.textContent = err.message && ! ( err instanceof SyntaxError ) && ! ( err instanceof TypeError ) ? err.message : 'We could not send the email just now. Please try again later.';
		}
		send.disabled = false;
	} );
}

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

/** Rooms 2 to 5 (and their pictures in the headline) wait until the booking form is up, so they never compete with it. */
function roomPhotos( n ) {
	[ rooms[ n ] && rooms[ n ].querySelector( 'img' ), frames[ n ] ].forEach( ( img ) => {
		if ( img && img.dataset.src ) {
			img.src = img.dataset.src;
			delete img.dataset.src;
		}
	} );
}

function enter( n ) {
	if ( n === room ) {
		return;
	}
	roomPhotos( n );
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
	p.addEventListener( 'click', () => {
		step( 'room', rooms[ i ].dataset.kind );
		enter( i );
	} );
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
const head = document.getElementById( 'biz-head' );
const headImg = head.querySelector( 'img' );
const ASSETS = new URL( './img/', import.meta.url );
/** Each demo business: its room in the hero, its photo, and the rule its bookings keep (the five-kinds table's words). */
const BUSINESS = {
	salon: { room: 0, img: 'salon.webp', w: 1920, h: 1333, alt: 'A salon with three styling chairs facing round mirrors on a dark wall', rule: 'An add-on makes the booking longer, and starts that no longer fit drop out.' },
	restaurant: { room: 1, img: 'restaurant.webp', w: 1600, h: 2400, alt: 'Dishes on blue and white plates, laid on a dark cloth among chillies and shallots', rule: 'A party of 8 is seated across tables that are free together.' },
	workshop: { room: 2, img: 'workshop.webp', w: 1600, h: 2400, alt: 'A mechanic leaning over an open engine bay', rule: 'A mechanic and a bay must both be free; a part on order is shown before booking.' },
	field: { room: 3, img: 'home.webp', w: 1920, h: 1080, alt: 'Air conditioning units on the white wall of a building', rule: 'Travel time is held either side of each visit, only inside the area covered.' },
	staff: { room: 4, img: 'clinic.webp', w: 1920, h: 1282, alt: 'A clinic\'s examination room with a couch, an ultrasound machine and a plant by the window', rule: 'Times show only where the doctor is free, or any doctor if you do not mind who.' },
};

/** The header above the booking form becomes the chosen business; its colour comes from the business's own settings, once loaded. */
function showBusiness( kind, name ) {
	const b = BUSINESS[ kind ];
	head.querySelector( 'b' ).textContent = name;
	head.querySelector( 'span' ).textContent = b.rule;
	if ( ! headImg.src.endsWith( '/' + b.img ) ) {
		Object.assign( headImg, { src: new URL( b.img, ASSETS ).href, alt: b.alt, width: b.w, height: b.h } );
		headImg.classList.remove( 'fresh' );
		void headImg.offsetWidth; // (restart the fade)
		headImg.classList.add( 'fresh' );
	}
	enter( b.room ); // the hero shows the same room, so the top of the page and the demo agree
}

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
	const known = id && LIVE.modules && LIVE.modules[ kind ];
	if ( known ) {
		// the Showcase put each business's details in the page: no round trip before the free times are asked for
		const source = new RestSource( LIVE.api );
		source.demo = 'live';
		source.module = async ( m ) => ( m === id ? known.module : RestSource.prototype.module.call( source, m ) );
		source.vehicles = async ( m ) => ( m === id ? known.vehicles : RestSource.prototype.vehicles.call( source, m ) );
		return { source, id, data: known, how: 'live' };
	}
	if ( id ) {
		try {
			const get = async ( path ) => {
				const r = await fetch( `${ LIVE.api }${ path }`, { credentials: 'omit' } );
				if ( ! r.ok || ! ( r.headers.get( 'content-type' ) || '' ).includes( 'json' ) ) {
					throw new Error( 'not JSON' );
				}
				return r.json();
			};
			const [ module, vehicles ] = await Promise.all( [ get( `/modules/${ id }` ), kind === 'workshop' ? get( `/modules/${ id }/supported-vehicles` ) : [] ] );
			const source = new RestSource( LIVE.api );
			source.demo = 'live';
			return { source, id, data: { module, vehicles }, how: 'live' };
		} catch ( e ) {
			// falls through to the recorded answers
		}
	}
	const data = await fetch( new URL( `./demo/${ kind }.json${ VER }`, import.meta.url ) ).then( ( r ) => r.json() );
	return { source: new DemoSource( data ), id: 1, data, how: id ? 'fallback' : 'recorded' };
}

// One booking form per business, each made once and kept: switching shows another that has already loaded its times.
const made = {}; // kind -> Promise<{ el, data, how }>
let shown = null; // the kind on view

function make( kind, line ) {
	made[ kind ] = made[ kind ] || ( async () => {
		const { source, id, data, how } = await sourceFor( kind );
		if ( LOG && source.book ) {
			const book = source.book.bind( source );
			source.book = async ( ...args ) => {
				try {
					const done = await book( ...args );
					step( 'booked', kind ); // that it happened and where: nothing the visitor typed
					return done;
				} catch ( e ) {
					step( 'refused', kind );
					throw e;
				}
			};
		}
		const el = document.createElement( 'jsb-booking' );
		el.setAttribute( 'module', String( id ) );
		el.setAttribute( 'css', new URL( './widget/jsb-booking.css' + VER, import.meta.url ).href );
		el.source = source;
		el.hidden = true;
		holder.append( el );
		el.preset( choices( read( line ), data ) );
		return { el, data, how };
	} )();
	return made[ kind ];
}

/** The form has its first times (or says there are none), or a few seconds have passed. */
async function settled( el ) {
	for ( let waited = 0; waited < 5000; waited += 150 ) {
		if ( el.shadowRoot && el.shadowRoot.querySelector( '[data-k="time"], .times-empty, .msg.error' ) ) {
			return;
		}
		await new Promise( ( ok ) => setTimeout( ok, 150 ) );
	}
}

async function answer( line, { again = false } = {} ) {
	const kind = read( line ).kind;
	try {
		const w = await make( kind, line );
		if ( again ) {
			w.el.preset( choices( read( line ), w.data ) ); // the same business chosen again: back to its starting booking
		}
		note.textContent = NOTES[ w.how ];
		document.getElementById( 'try' ).style.setProperty( '--biz', ( w.data.module.look && w.data.module.look.accent ) || '' ); // the header's name takes its colour
		holder.querySelectorAll( ':scope > :not(jsb-booking)' ).forEach( ( x ) => x.remove() );
		holder.querySelectorAll( 'jsb-booking' ).forEach( ( x ) => ( x.hidden = x !== w.el ) );
		shown = kind;
		return w;
	} catch ( e ) {
		holder.innerHTML = '<p class="hint">The demo could not load. The page needs to be opened from a web address, not as a file.</p>';
		return null;
	}
}

/** Once the first form has its times: the hero's other photos, then the other businesses, one at a time, out of sight. */
async function afterFirst( first ) {
	if ( first ) {
		await settled( first.el );
	}
	rooms.forEach( ( r, i ) => roomPhotos( i ) );
	for ( const b of businesses ) {
		const w = await make( b.dataset.kind, b.dataset.ask ).catch( () => null );
		if ( w ) {
			await settled( w.el );
		}
	}
}

// each business opens on its own starting line, already filled in; the visitor carries on from there
businesses.forEach( ( b ) => b.addEventListener( 'click', () => {
	const again = shown === b.dataset.kind;
	step( 'business', b.dataset.kind );
	businesses.forEach( ( x ) => x.setAttribute( 'aria-pressed', String( x === b ) ) );
	showBusiness( b.dataset.kind, b.querySelector( 'span' ).textContent.split( ' · ' )[ 0 ] );
	answer( b.dataset.ask, { again } );
} ) );
answer( ( businesses.find( ( b ) => b.getAttribute( 'aria-pressed' ) === 'true' ) || businesses[ 0 ] ).dataset.ask ).then( afterFirst );
