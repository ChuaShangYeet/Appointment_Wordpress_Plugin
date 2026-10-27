/**
 * <jsb-booking module="3" api="https://site/wp-json/justsimple/v1" css=".../jsb-booking.css"> — the booking widget.
 *
 * A web component with its own shadow root, so it works inside any theme. Plain ES module, no build step.
 * The widget answers one question, "When can I come?": the free times are always the biggest thing. Before them it asks only
 * what decides those times for this kind of business (a service, add-ons, a party size, a car, a place), and after them only
 * who is booking. Steps: choose → when → your details → done. Answered steps collapse to one line with "Change".
 *
 * Motion is feedback, never performance: no entrances; a step opens with 200 ms of opacity and 6 px; reduced motion keeps opacity only.
 * Data comes through a source object (RestSource here; the marketing page swaps in demo data with the same methods).
 */

const DAYS_SHOWN = 14;
const SLOW_MS = 300; // under this a load shows nothing at all

// ------------------------------------------------------------------ small helpers

const esc = ( s ) => String( s ?? '' ).replace( /[&<>"']/g, ( c ) => ( { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' } )[ c ] );
const hm = ( iso ) => iso.slice( 11, 16 ); // the API speaks the business's local time with its offset
const ymdOf = ( iso ) => iso.slice( 0, 10 );
const noon = ( ymd ) => new Date( ymd + 'T12:00:00Z' );
const addDays = ( ymd, n ) => {
	const d = noon( ymd );
	d.setUTCDate( d.getUTCDate() + n );
	return d.toISOString().slice( 0, 10 );
};
const todayIn = ( tz ) => {
	try {
		return new Intl.DateTimeFormat( 'en-CA', { timeZone: tz, year: 'numeric', month: '2-digit', day: '2-digit' } ).format( new Date() );
	} catch ( e ) {
		return new Date().toISOString().slice( 0, 10 );
	}
};
const fmtDay = ( ymd, opts ) => new Intl.DateTimeFormat( undefined, Object.assign( { timeZone: 'UTC' }, opts ) ).format( noon( ymd ) );
const longDate = ( ymd ) => fmtDay( ymd, { weekday: 'long', day: 'numeric', month: 'long' } );
const minutes = ( m ) => ( m >= 60 ? Math.floor( m / 60 ) + ' h' + ( m % 60 ? ' ' + ( m % 60 ) + ' min' : '' ) : m + ' min' );
const newKey = () => ( window.crypto && crypto.randomUUID ? crypto.randomUUID() : 'k' + Date.now() + Math.random().toString( 36 ).slice( 2 ) );

/** White text on the owner's colour only when it reads (4.5:1); otherwise ink. A pale brand colour never makes an unreadable button. */
function onAccent( hex ) {
	const ch = ( i ) => {
		const c = parseInt( hex.slice( i, i + 2 ), 16 ) / 255;
		return c <= 0.03928 ? c / 12.92 : ( ( c + 0.055 ) / 1.055 ) ** 2.4;
	};
	if ( ! /^#[0-9a-f]{6}$/i.test( hex ) ) {
		return '#ffffff';
	}
	const l = 0.2126 * ch( 1 ) + 0.7152 * ch( 3 ) + 0.0722 * ch( 5 );
	return 1.05 / ( l + 0.05 ) >= 4.5 ? '#ffffff' : '#1c211b';
}

export class ApiError extends Error {
	constructor( status, body ) {
		super( ( body && body.message ) || 'Request failed' );
		this.status = status;
		this.code = ( body && body.code ) || 'network';
		this.data = ( body && body.data ) || {};
	}
}

/** The plugin's public REST routes. */
export class RestSource {
	constructor( base ) {
		this.base = String( base || '' ).replace( /\/$/, '' );
	}
	async call( method, path, query, body ) {
		const qs = query ? '?' + new URLSearchParams( Object.entries( query ).filter( ( [ , v ] ) => v !== null && v !== undefined && v !== '' ) ).toString() : '';
		let res;
		try {
			res = await fetch( this.base + path + qs, {
				method,
				headers: body ? { 'Content-Type': 'application/json' } : {},
				body: body ? JSON.stringify( body ) : undefined,
				credentials: 'omit', // public routes: no cookie, so no nonce trouble behind page caches
			} );
		} catch ( e ) {
			throw new ApiError( 0, { code: 'network', message: 'No connection. Check your internet and try again.' } );
		}
		let json = null;
		try {
			json = await res.json();
		} catch ( e ) {}
		if ( ! res.ok ) {
			throw new ApiError( res.status, json );
		}
		return json;
	}
	module( id ) { return this.call( 'GET', `/modules/${ id }` ); }
	vehicles( id ) { return this.call( 'GET', `/modules/${ id }/supported-vehicles` ); }
	availability( id, q ) { return this.call( 'GET', `/modules/${ id }/availability`, q ); }
	month( id, q ) { return this.call( 'GET', `/modules/${ id }/availability/month`, q ); }
	book( id, body ) { return this.call( 'POST', `/modules/${ id }/bookings`, null, body ); }
	show( code, token ) { return this.call( 'GET', `/bookings/${ encodeURIComponent( code ) }`, { token } ); }
	cancel( code, token ) { return this.call( 'POST', `/bookings/${ encodeURIComponent( code ) }/cancel`, null, { token } ); }
}

// ------------------------------------------------------------------ the element

class JsbBooking extends HTMLElement {
	connectedCallback() {
		if ( this._root ) {
			return;
		}
		this.moduleId = parseInt( this.getAttribute( 'module' ), 10 ) || 0;
		this.src = this.source || new RestSource( this.getAttribute( 'api' ) );
		const shadow = this.attachShadow( { mode: 'open' } );
		const css = this.getAttribute( 'css' ) || new URL( './jsb-booking.css', import.meta.url ).href;
		shadow.innerHTML = `<link rel="stylesheet" href="${ esc( css ) }"><div class="jsb" part="widget"><div id="root"><p class="summary">Loading…</p></div><div id="live" class="vh" aria-live="polite"></div></div>`;
		this._root = shadow.getElementById( 'root' );
		this._live = shadow.getElementById( 'live' );
		this._root.addEventListener( 'click', ( e ) => this.onClick( e ) );
		this._root.addEventListener( 'change', ( e ) => this.onChange( e ) );
		this._root.addEventListener( 'input', ( e ) => this.onInput( e ) );
		this._root.addEventListener( 'keydown', ( e ) => this.onKey( e ) );
		this._root.addEventListener( 'submit', ( e ) => {
			e.preventDefault();
			this.submit();
		} );
		this.start();
	}

	// ---------------------------------------------------------------- loading

	async start() {
		const p = new URLSearchParams( location.search );
		if ( p.get( 'jsb_booking' ) && p.get( 'jsb_token' ) ) {
			try {
				const b = await this.src.show( p.get( 'jsb_booking' ), p.get( 'jsb_token' ) );
				if ( b.module && b.module.id === this.moduleId ) {
					this.st = { mode: 'manage', booking: b, code: p.get( 'jsb_booking' ), token: p.get( 'jsb_token' ) };
					await this.loadModule( true );
					return;
				}
			} catch ( e ) {
				if ( e.status === 404 ) {
					this.st = { mode: 'manage-missing' };
					await this.loadModule( true );
					return;
				}
			}
		}
		await this.loadModule( false );
	}

	async loadModule( manageOnly ) {
		try {
			this.m = await this.src.module( this.moduleId );
		} catch ( e ) {
			this._root.innerHTML = `<p class="msg error">${ esc( e.status === 404 ? 'This booking page is not available right now.' : e.message ) }</p>`;
			return;
		}
		const look = this.m.look || {};
		this.style.setProperty( '--jsb-accent', look.accent || '#4f6238' );
		this.style.setProperty( '--jsb-on-accent', onAccent( look.accent || '#4f6238' ) );
		if ( look.font === 'dm-sans' ) {
			this.style.setProperty( '--jsb-font', '"DM Sans", system-ui, sans-serif' );
		}
		this.cur = look.currency || 'RM';
		this.tz = this.m.timezone || 'UTC';
		if ( manageOnly ) {
			this.render();
			return;
		}
		this.vehicles = this.m.type === 'workshop' ? await this.src.vehicles( this.moduleId ).catch( () => [] ) : [];
		const base = this.m.services.filter( ( s ) => ! s.is_addon );
		this.st = {
			mode: 'book',
			step: 'choose',
			opening: null,
			choice: {
				service: base.length === 1 ? base[ 0 ].id : null, // single-service kinds (staff, salon base, field)
				services: base.length === 1 && this.m.type === 'workshop' ? [ base[ 0 ].id ] : [], // workshop: several, stacked
				addons: [],
				staff: '',
				party: 2,
				tableType: 'normal',
				separate: false,
				table: '',
				car: { model: '', year: '', plate: '' },
				loc: null, // { lat, lon, how: 'device' | 'map' }
			},
			day: null,
			monthDates: null,
			slots: [],
			slotsFor: null,
			loading: false,
			slow: false,
			slot: null,
			taken: null,
			lead: [],
			details: { name: '', phone: '', email: '', request: '', line1: '', city: '', postcode: '' },
			ack: false,
			accepted: null,
			errors: {},
			msg: null,
			busy: false,
			key: null,
			result: null,
		};
		this.render();
	}

	// ---------------------------------------------------------------- what the module needs

	get kind() {
		return this.m.type;
	}
	svc( id ) {
		return this.m.services.find( ( s ) => s.id === id );
	}
	chosenServices() {
		const c = this.st.choice;
		if ( this.kind === 'workshop' ) {
			return c.services.map( ( id ) => this.svc( id ) ).filter( Boolean );
		}
		if ( this.kind === 'restaurant' ) {
			return [];
		}
		const list = c.service ? [ this.svc( c.service ) ] : [];
		return this.kind === 'salon' ? list.concat( c.addons.map( ( id ) => this.svc( id ) ) ).filter( Boolean ) : list.filter( Boolean );
	}
	onSite() {
		const s = this.kind === 'field' && this.st.choice.service ? this.svc( this.st.choice.service ) : null;
		return !! s && s.location_type === 'on_site';
	}
	/** Why "See free times" is not ready yet, or null. */
	chooseProblem() {
		const c = this.st.choice;
		switch ( this.kind ) {
			case 'restaurant': {
				const info = this.m.restaurant[ c.tableType ] || {};
				const max = c.separate ? info.largest_separate_party : info.largest_table;
				if ( ! ( c.party >= 1 ) ) {
					return 'Choose how many people.';
				}
				if ( max && c.party > max ) {
					return c.separate || ! info.largest_separate_party || info.largest_separate_party <= info.largest_table
						? `Online booking takes up to ${ max } people. For a bigger group, please call.`
						: `No single table seats ${ c.party }. Tick "separate tables" to book more than one.`;
				}
				return null;
			}
			case 'workshop': {
				if ( ! c.services.length ) {
					return 'Choose at least one service.';
				}
				const v = this.vehicles.find( ( x ) => String( x.id ) === String( c.car.model ) );
				if ( ! v ) {
					return 'Choose your car.';
				}
				if ( ! c.car.plate.trim() ) {
					return 'Enter the number plate.';
				}
				if ( ( v.year_from || v.year_to ) ) {
					const y = parseInt( c.car.year, 10 );
					if ( ! y ) {
						return `Enter the model year: this workshop services the ${ v.make } ${ v.model } from ${ v.year_from || '…' } to ${ v.year_to || '…' }.`;
					}
					if ( ( v.year_from && y < v.year_from ) || ( v.year_to && y > v.year_to ) ) {
						return `This workshop services the ${ v.make } ${ v.model } only for ${ v.year_from || '…' }–${ v.year_to || '…' } models.`;
					}
				}
				return null;
			}
			default:
				if ( ! c.service ) {
					return 'Choose a service.';
				}
				if ( this.onSite() && ! c.loc ) {
					return 'Tell us where to come: use your location' + ( this.m.map && this.m.map.enabled ? ' or pick it on the map.' : '.' );
				}
				return null;
		}
	}
	/** The query every availability call carries. */
	query() {
		const c = this.st.choice;
		const q = {};
		if ( this.kind === 'restaurant' ) {
			Object.assign( q, { party_size: c.party, table_type: c.tableType, separate: c.separate ? 1 : '', table: c.table } );
		} else {
			q.services = this.chosenServices().map( ( s ) => s.id ).join( ',' );
			q.staff = c.staff;
		}
		if ( c.loc && this.onSite() ) {
			q.latitude = c.loc.lat.toFixed( 6 );
			q.longitude = c.loc.lon.toFixed( 6 );
		}
		return q;
	}

	// ---------------------------------------------------------------- availability

	stripDays() {
		const first = todayIn( this.tz );
		const window = ( this.m.policy && this.m.policy.booking_window_days ) || 60;
		const n = Math.min( DAYS_SHOWN, window + 1 );
		return Array.from( { length: n }, ( _, i ) => addDays( first, i ) );
	}

	async loadMonths() {
		const days = this.stripDays();
		const months = [ ...new Set( days.map( ( d ) => d.slice( 0, 7 ) ) ) ];
		const q = this.query();
		const sig = JSON.stringify( q );
		this._monthSig = sig;
		try {
			const res = await Promise.all( months.map( ( month ) => this.src.month( this.moduleId, Object.assign( { month }, q ) ) ) );
			if ( this._monthSig !== sig ) {
				return;
			}
			this.st.monthDates = new Set( res.flatMap( ( r ) => r.dates ) );
		} catch ( e ) {
			this.st.monthDates = null; // days stay choosable; the day's own call will say
		}
		this.render();
	}

	async loadDay( day, { keepTaken = false } = {} ) {
		const st = this.st;
		st.day = day;
		st.loading = true;
		st.slow = false;
		st.slot = null;
		if ( ! keepTaken ) {
			st.taken = null;
		}
		const ticket = ( this._dayTicket = ( this._dayTicket || 0 ) + 1 );
		const slowTimer = setTimeout( () => {
			if ( this._dayTicket === ticket && st.loading ) {
				st.slow = true;
				this.render();
			}
		}, SLOW_MS );
		this.render();
		try {
			const r = await this.src.availability( this.moduleId, Object.assign( { date: day }, this.query() ) );
			if ( this._dayTicket !== ticket ) {
				return;
			}
			st.slots = r.slots || [];
			st.lead = r.lead_time_notice || [];
			st.slotsFor = day;
			st.msg = null;
		} catch ( e ) {
			if ( this._dayTicket !== ticket ) {
				return;
			}
			st.slots = [];
			st.msg = { type: 'error', text: this.problemText( e ) };
		} finally {
			clearTimeout( slowTimer );
			if ( this._dayTicket === ticket ) {
				st.loading = false;
				st.slow = false;
				this.render();
				this.say( st.slots.length ? `${ st.slots.length } free times on ${ longDate( day ) }.` : `No free times on ${ longDate( day ) }.` );
			}
		}
	}

	nextFreeDay( after ) {
		if ( ! this.st.monthDates ) {
			return null;
		}
		return this.stripDays().find( ( d ) => d > after && this.st.monthDates.has( d ) ) || null;
	}

	// ---------------------------------------------------------------- events

	onClick( e ) {
		const t = e.target.closest( '[data-act]' );
		if ( ! t ) {
			return;
		}
		const act = t.dataset.act;
		const st = this.st;
		if ( act === 'see-times' ) {
			const p = this.chooseProblem();
			if ( p ) {
				st.msg = { type: 'error', text: p, where: 'choose' };
				this.render( '[data-act="see-times"]' );
				return;
			}
			st.msg = null;
			st.day = st.day && this.stripDays().includes( st.day ) ? st.day : this.stripDays()[ 0 ]; // the step renders with its day
			this.open( 'when' );
			this.loadMonths();
			this.loadDay( st.day );
		} else if ( act === 'edit' ) {
			st.msg = null;
			this.open( t.dataset.step );
		} else if ( act === 'day' ) {
			this.loadDay( t.dataset.day );
		} else if ( act === 'next-day' ) {
			this.loadDay( t.dataset.day );
			this.focusLater( `[data-act="day"][data-day="${ t.dataset.day }"]` );
		} else if ( act === 'party' ) {
			st.choice.party = Math.max( 1, Math.min( 200, st.choice.party + parseInt( t.dataset.d, 10 ) ) );
			this.choiceChanged();
		} else if ( act === 'locate' ) {
			this.locate();
		} else if ( act === 'map' ) {
			this.openMap();
		} else if ( act === 'again' ) {
			this.loadModule( false );
		} else if ( act === 'cancel-booking' ) {
			this.cancelBooking();
		} else if ( act === 'confirm-cancel' ) {
			st.confirmCancel = true;
			this.render( '[data-act="cancel-booking"]' );
		}
	}

	onChange( e ) {
		const t = e.target;
		const k = t.dataset.k;
		const st = this.st;
		if ( ! k ) {
			return;
		}
		const c = st.choice;
		switch ( k ) {
			case 'service':
				c.service = parseInt( t.value, 10 );
				if ( this.kind === 'field' && ! this.onSite() ) {
					c.loc = null;
				}
				break;
			case 'addon':
				c.addons = [ ...this._root.querySelectorAll( '[data-k="addon"]:checked' ) ].map( ( x ) => parseInt( x.value, 10 ) );
				break;
			case 'services':
				c.services = [ ...this._root.querySelectorAll( '[data-k="services"]:checked' ) ].map( ( x ) => parseInt( x.value, 10 ) );
				break;
			case 'staff':
				c.staff = t.value;
				break;
			case 'tableType':
				c.tableType = t.value;
				c.table = '';
				break;
			case 'separate':
				c.separate = t.checked;
				if ( c.separate ) {
					c.table = '';
				}
				break;
			case 'table':
				c.table = t.value;
				break;
			case 'party':
				c.party = Math.max( 1, Math.min( 200, parseInt( t.value, 10 ) || 1 ) );
				// a typed field changes on its way out: render after the focus has moved, so Tab lands where it was going
				setTimeout( () => this.choiceChanged(), 0 );
				return;
			case 'car-model':
				c.car.model = t.value;
				break;
			case 'time': {
				st.slot = st.slots.find( ( s ) => s.start === t.value ) || null;
				st.accepted = null;
				st.msg = null;
				if ( this._arrowTime ) {
					// arrows walk the radio group: choose, keep focus here; Tab goes on to the form below
					this._arrowTime = false;
					st.step = 'details';
					this.render();
				} else {
					this.open( 'details' );
				}
				return;
			}
			case 'ack':
				st.ack = t.checked;
				return;
			default:
				return;
		}
		this.choiceChanged( `[data-k="${ k }"]${ t.type === 'radio' ? `[value="${ t.value }"]` : '' }` );
	}

	onInput( e ) {
		const t = e.target;
		const k = t.dataset.k;
		if ( ! k ) {
			return;
		}
		if ( k.startsWith( 'd-' ) ) {
			this.st.details[ k.slice( 2 ) ] = t.value; // typing never re-renders
			if ( this.st.errors[ k.slice( 2 ) ] ) {
				delete this.st.errors[ k.slice( 2 ) ];
				t.removeAttribute( 'aria-invalid' );
			}
		} else if ( k === 'car-year' || k === 'car-plate' ) {
			this.st.choice.car[ k.slice( 4 ) ] = t.value;
		}
	}

	/** The day strip is a listbox: arrows move, Home/End jump, Enter or Space choose. */
	onKey( e ) {
		const t = e.target;
		if ( t.dataset.k === 'time' ) {
			this._arrowTime = e.key.startsWith( 'Arrow' );
			return;
		}
		if ( t.dataset.act !== 'day' ) {
			return;
		}
		const all = [ ...this._root.querySelectorAll( '[data-act="day"]' ) ];
		const i = all.indexOf( t );
		let j = null;
		if ( e.key === 'ArrowRight' || e.key === 'ArrowDown' ) {
			j = Math.min( all.length - 1, i + 1 );
		} else if ( e.key === 'ArrowLeft' || e.key === 'ArrowUp' ) {
			j = Math.max( 0, i - 1 );
		} else if ( e.key === 'Home' ) {
			j = 0;
		} else if ( e.key === 'End' ) {
			j = all.length - 1;
		} else if ( e.key === 'Enter' || e.key === ' ' ) {
			e.preventDefault();
			this.loadDay( t.dataset.day );
			this.focusLater( `[data-act="day"][data-day="${ t.dataset.day }"]` );
			return;
		}
		if ( j !== null ) {
			e.preventDefault();
			all.forEach( ( el, n ) => el.setAttribute( 'tabindex', n === j ? '0' : '-1' ) );
			all[ j ].focus();
		}
	}

	/** Anything that changes the times sends the customer back to the choose step's state, and drops a chosen time. */
	choiceChanged( focusSel ) {
		const st = this.st;
		st.msg = null;
		st.key = null;
		st.monthDates = null;
		st.slot = null;
		st.slots = [];
		st.slotsFor = null;
		st.mapOpen = false; // a re-render empties the map's holder
		if ( st.step !== 'choose' ) {
			st.step = 'choose';
		}
		this.render( focusSel );
	}

	open( step ) {
		this.st.step = step;
		this.st.opening = step;
		this.st.openedAt = performance.now();
		this.render( `#step-${ step } h3`, true );
	}

	// ---------------------------------------------------------------- location (field service)

	locate() {
		const st = this.st;
		if ( ! window.isSecureContext || ! navigator.geolocation ) {
			st.msg = { type: 'error', where: 'choose', text: 'This browser cannot share your location here.' + ( this.m.map && this.m.map.enabled ? ' Pick the place on the map instead.' : this.phoneText() ) };
			this.render( '[data-act="locate"]' );
			return;
		}
		st.locating = true;
		this.render( '[data-act="locate"]' );
		navigator.geolocation.getCurrentPosition(
			( p ) => {
				st.locating = false;
				this.setLocation( p.coords.latitude, p.coords.longitude, 'device', Math.round( p.coords.accuracy ) );
			},
			( err ) => {
				st.locating = false;
				st.msg = { type: 'error', where: 'choose', text: ( err.code === 1 ? 'Location was not shared.' : 'Your location could not be found.' ) + ( this.m.map && this.m.map.enabled ? ' You can pick the place on the map instead.' : this.phoneText() ) };
				this.render( '[data-act="locate"]' );
			},
			{ enableHighAccuracy: true, timeout: 12000, maximumAge: 60000 }
		);
	}

	setLocation( lat, lon, how, accuracy ) {
		this.st.choice.loc = { lat, lon, how, accuracy };
		this.st.mapOpen = false;
		this.choiceChanged( '[data-act="see-times"]' );
		this.say( 'Location set.' );
	}

	async openMap() {
		const st = this.st;
		st.mapOpen = true;
		this.render();
		const holder = this._root.querySelector( '#map' );
		try {
			const { openMapPicker } = await import( new URL( './map-picker.js', import.meta.url ).href );
			const c = st.choice.loc || ( this.m.map.center ? { lat: this.m.map.center[ 0 ], lon: this.m.map.center[ 1 ] } : { lat: 3.139, lon: 101.6869 } );
			openMapPicker( holder, {
				lat: c.lat,
				lon: c.lon,
				onPick: ( lat, lon ) => this.setLocation( lat, lon, 'map' ),
				onCancel: () => {
					st.mapOpen = false;
					this.render( '[data-act="map"]' );
				},
			} );
		} catch ( e ) {
			st.mapOpen = false;
			st.msg = { type: 'error', where: 'choose', text: 'The map could not be loaded.' + this.phoneText() };
			this.render();
		}
	}

	phoneText() {
		const p = this.m.look && this.m.look.phone;
		return p ? ` You can also call ${ p } to book.` : '';
	}

	// ---------------------------------------------------------------- booking

	async submit() {
		const st = this.st;
		if ( st.busy || ! st.slot ) {
			return;
		}
		const d = st.details;
		const errors = {};
		if ( ! d.name.trim() ) {
			errors.name = 'Enter your name.';
		}
		if ( ( d.phone.match( /\d/g ) || [] ).length < 7 ) {
			errors.phone = 'Enter a phone number with at least 7 digits.';
		}
		if ( d.email && ! /^[^\s@]+@[^\s@]+\.[^\s@]+$/.test( d.email ) ) {
			errors.email = 'Enter a valid email, or leave it empty.';
		}
		if ( this.onSite() && ! d.line1.trim() ) {
			errors.line1 = 'Enter the street address.';
		}
		if ( st.lead.length && ! st.ack ) {
			errors.ack = 'Tick to confirm you know about the waiting time for parts.';
		}
		st.errors = errors;
		if ( Object.keys( errors ).length ) {
			this.render( `[data-k="${ errors.ack ? 'ack' : 'd-' + Object.keys( errors )[ 0 ] }"]` );
			this.say( Object.values( errors )[ 0 ] );
			return;
		}
		const c = st.choice;
		const body = {
			start: st.slot.start,
			customer: { name: d.name.trim(), phone: d.phone.trim(), email: d.email.trim() },
			special_request: d.request.trim(),
			idempotency_key: ( st.key = st.key || newKey() ),
			website: this._root.querySelector( '[name="website"]' ).value, // the honeypot
		};
		if ( this.kind === 'restaurant' ) {
			Object.assign( body, { party_size: c.party, table_type: c.tableType, allow_separate_seating: c.separate, table_id: c.table || null } );
			const spend = st.accepted || st.slot.minimum_spend;
			if ( spend ) {
				body.accepted_minimum_spend = spend;
			}
		} else {
			body.service_ids = this.chosenServices().map( ( s ) => s.id );
			body.staff_id = c.staff || null;
		}
		if ( this.kind === 'workshop' ) {
			const v = this.vehicles.find( ( x ) => String( x.id ) === String( c.car.model ) );
			body.vehicle = { plate_no: c.car.plate, supported_vehicle_id: v.id, year: c.car.year || null };
			body.acknowledge_lead_time = st.ack;
		}
		if ( this.onSite() ) {
			body.address = { line1: d.line1, city: d.city, postcode: d.postcode, latitude: c.loc.lat, longitude: c.loc.lon };
		}
		st.busy = true;
		this.render( '[data-act="book"]' );
		try {
			const r = await this.src.book( this.moduleId, body );
			st.result = r;
			st.busy = false;
			this.open( 'done' );
			this.scrollIntoView( { block: 'nearest', behavior: matchMedia( '(prefers-reduced-motion: reduce)' ).matches ? 'auto' : 'smooth' } );
			this.say( ( r.booking.status === 'pending' ? 'Request sent. ' : 'Booked. ' ) + 'Your code is ' + r.booking.code.split( '' ).join( ' ' ) + '.' );
		} catch ( e ) {
			st.busy = false;
			if ( e.status ) {
				st.key = null; // the server answered: a new attempt is a new request
			}
			this.bookingFailed( e );
		}
	}

	bookingFailed( e ) {
		const st = this.st;
		if ( e.code === 'slot_unavailable' && e.data.slots ) {
			st.taken = st.slot && st.slot.start;
			st.slots = e.data.slots;
			st.slot = null;
			st.step = 'when';
			st.msg = { type: 'warning', where: 'when', text: `Sorry, ${ hm( st.taken ) } was just taken. Here are the times still free.` };
			const near = st.slots.map( ( s ) => s.start ).sort( ( a, b ) => Math.abs( Date.parse( a ) - Date.parse( st.taken ) ) - Math.abs( Date.parse( b ) - Date.parse( st.taken ) ) )[ 0 ];
			this.render( near ? `[data-k="time"][value="${ near }"]` : '#step-when h3' );
			this.say( st.msg.text );
			return;
		}
		if ( e.code === 'minimum_spend_not_accepted' ) {
			st.accepted = e.data.minimum_spend;
			st.msg = { type: 'warning', where: 'details', text: `The tables free now have a minimum spend of ${ this.cur } ${ e.data.minimum_spend }. Nothing has been booked.` };
			this.render( '[data-act="book"]' );
			this.say( st.msg.text );
			return;
		}
		if ( e.code === 'lead_time_not_acknowledged' ) {
			st.lead = e.data.parts || st.lead;
			st.errors = { ack: 'Tick to confirm you know about the waiting time for parts.' };
			this.render( '[data-k="ack"]' );
			return;
		}
		const field = e.data && e.data.field;
		const map = { 'customer.name': 'name', 'customer.phone': 'phone', 'customer.email': 'email', 'address.line1': 'line1' };
		if ( field && map[ field ] ) {
			st.errors = { [ map[ field ] ]: e.message };
			this.render( `[data-k="d-${ map[ field ] }"]` );
			this.say( e.message );
			return;
		}
		st.msg = { type: 'error', where: 'details', text: this.problemText( e ) };
		this.render( '[data-act="book"]' );
		this.say( st.msg.text );
	}

	problemText( e ) {
		if ( e.status === 429 ) {
			return 'Too many tries in a short time. Please wait a few minutes and try again.';
		}
		if ( e.status === 503 ) {
			return 'Many people are booking right now. Please try again in a moment.';
		}
		if ( e.code === 'outside_service_area' ) {
			return e.message + this.phoneText();
		}
		return e.message || 'Something went wrong. Please try again.';
	}

	async cancelBooking() {
		const st = this.st;
		st.busy = true;
		this.render( '[data-act="cancel-booking"]' );
		try {
			st.booking = await this.src.cancel( st.code, st.token );
			st.msg = { type: 'accent', text: 'Your booking is cancelled.' };
		} catch ( e ) {
			st.msg = { type: 'error', text: this.problemText( e ) };
		}
		st.busy = false;
		st.confirmCancel = false;
		this.render( '#manage h3', true );
		this.say( st.msg.text );
	}

	// ---------------------------------------------------------------- rendering

	say( text ) {
		this._live.textContent = '';
		setTimeout( () => ( this._live.textContent = text ), 30 );
	}

	focusLater( sel ) {
		this._focusAfter = sel;
	}

	/** Re-render the whole widget, keeping (or moving) focus. Typing never re-renders, so text fields keep their caret. */
	render( focusSel, isStepHeading ) {
		const active = this.shadowRoot.activeElement;
		const heading = active && active.matches( 'h3' ) && active.closest( '.step[id]' );
		if ( heading && ! focusSel ) {
			focusSel = `#${ heading.id } h3`; // a step heading that was just focused stays focused through the loads that follow
			isStepHeading = true;
		}
		const keep = active && ( active.id ||( active.dataset.k && `[data-k="${ active.dataset.k }"]${ active.value && active.type === 'radio' ? `[value="${ active.value }"]` : '' }` ) || ( active.dataset.act && `[data-act="${ active.dataset.act }"]${ active.dataset.day ? `[data-day="${ active.dataset.day }"]` : '' }` ) );
		this._root.innerHTML = this.html();
		const sel = this._focusAfter || focusSel || ( keep && ( active.id ? '#' + active.id : keep ) );
		this._focusAfter = null;
		const el = sel && this._root.querySelector( sel );
		if ( el ) {
			if ( isStepHeading ) {
				el.setAttribute( 'tabindex', '-1' );
			}
			el.focus( { preventScroll: ! isStepHeading } );
		}
	}

	html() {
		const st = this.st;
		if ( ! st ) {
			return '';
		}
		if ( st.mode === 'manage' || st.mode === 'manage-missing' ) {
			return this.manageHtml();
		}
		const approval = this.m.policy && this.m.policy.needs_approval;
		const head = `<header class="jsb-head"><h2>${ esc( this.m.title ) }</h2>${ approval ? '<p>The business confirms each request; you will hear back soon.</p>' : '' }</header>`;
		if ( st.step === 'done' ) {
			return head + this.doneHtml();
		}
		const order = [ 'choose', 'when', 'details' ];
		const at = order.indexOf( st.step );
		return (
			head +
			this.chooseHtml( at === 0 ) +
			( at >= 1 ? this.whenHtml() : '' ) + // the times stay open under the form, so another time is one click away
			( at >= 2 ? this.detailsHtml() : '' )
		);
	}

	stepWrap( id, title, open, body, summary ) {
		// the loads right after opening re-render the step; a negative delay carries the 200 ms on instead of restarting it
		const since = Math.round( performance.now() - ( this.st.openedAt || 0 ) );
		const opening = this.st.opening === id && since < 200 ? ` opening" style="--jsb-since:-${ since }ms` : '';
		if ( ! open ) {
			return `<section class="step" id="step-${ id }"><div class="step-head"><h3>${ title }</h3><button type="button" class="link" data-act="edit" data-step="${ id }">Change<span class="vh"> ${ title.toLowerCase() }</span></button></div><p class="summary">${ summary }</p></section>`;
		}
		return `<section class="step${ opening }" id="step-${ id }"><div class="step-head"><h3>${ title }</h3></div><div class="step-body">${ body }</div></section>`;
	}

	msgHtml( where ) {
		const m = this.st.msg;
		return m && ( ! m.where || m.where === where ) ? `<p class="msg ${ m.type }" role="${ m.type === 'error' ? 'alert' : 'status' }">${ esc( m.text ) }</p>` : '';
	}

	price( s ) {
		if ( s.price_after_inspection ) {
			return Number( s.price ) > 0 ? `from ${ esc( this.cur ) } ${ esc( s.price ) }` : 'price after inspection';
		}
		return Number( s.price ) > 0 ? `${ esc( this.cur ) } ${ esc( s.price ) }` : '';
	}

	serviceChoice( s, type, name, checked, extra ) {
		const where = this.kind === 'field' ? { on_site: 'At your place', virtual: 'Online', in_office: 'At our office' }[ s.location_type ] + ' · ' : '';
		return `<label class="choice"><input type="${ type }" name="${ name }" value="${ s.id }" data-k="${ name }"${ checked ? ' checked' : '' }><span class="name">${ esc( s.name ) }</span><span class="price">${ this.price( s ) }</span><span class="meta">${ where }${ extra || minutes( s.duration_minutes ) }${ s.description ? ' · ' + esc( s.description ) : '' }</span></label>`;
	}

	chooseSummary() {
		const c = this.st.choice;
		if ( this.kind === 'restaurant' ) {
			const t = c.table ? ' · ' + esc( ( this.m.restaurant.tables || [] ).find( ( x ) => String( x.id ) === String( c.table ) )?.name || '' ) : '';
			return `<strong>${ c.party } ${ c.party === 1 ? 'person' : 'people' }</strong>${ c.tableType === 'vip' ? ' · VIP' : '' }${ c.separate ? ' · separate tables are fine' : '' }${ t }`;
		}
		const list = this.chosenServices();
		const total = list.reduce( ( n, s ) => n + s.duration_minutes, 0 );
		let s = `<strong>${ list.map( ( x ) => esc( x.name ) ).join( ' + ' ) }</strong> · ${ minutes( total ) }`;
		const who = c.staff && this.m.staff.find( ( x ) => String( x.id ) === String( c.staff ) );
		if ( who ) {
			s += ' · ' + esc( who.name );
		}
		if ( this.kind === 'workshop' ) {
			const v = this.vehicles.find( ( x ) => String( x.id ) === String( c.car.model ) );
			s += ` · ${ esc( c.car.plate.toUpperCase() ) }, ${ esc( v ? v.make + ' ' + v.model : '' ) }${ c.car.year ? ' ' + esc( c.car.year ) : '' }`;
		}
		if ( this.onSite() && c.loc ) {
			s += c.loc.how === 'map' ? ' · place picked on the map' : ' · at your current location';
		}
		return s;
	}

	chooseHtml( open ) {
		const k = this.kind;
		const c = this.st.choice;
		const title = { restaurant: 'Your table', workshop: 'Your car and the work', field: 'What you need' }[ k ] || 'What would you like?';
		if ( ! open ) {
			return this.stepWrap( 'choose', title, false, '', this.chooseSummary() );
		}
		let b = '';
		const base = this.m.services.filter( ( s ) => ! s.is_addon );
		if ( k === 'restaurant' ) {
			const r = this.m.restaurant;
			b += `<div class="group"><label class="label" for="party">How many people?</label><div class="stepper"><button type="button" id="party-less" data-act="party" data-d="-1" aria-label="One fewer">−</button><input id="party" type="number" min="1" max="200" inputmode="numeric" value="${ c.party }" data-k="party"><button type="button" id="party-more" data-act="party" data-d="1" aria-label="One more">+</button></div></div>`;
			if ( r.vip && r.vip.largest_table ) {
				b += `<fieldset class="group fs"><legend class="label">Where would you like to sit?</legend><div class="choices">` +
					[ [ 'normal', 'Main dining room', '' ], [ 'vip', 'VIP room', 'A minimum spend applies; you will see it before you book.' ] ].map( ( [ v, n, m ] ) => `<label class="choice"><input type="radio" name="tableType" value="${ v }" data-k="tableType"${ c.tableType === v ? ' checked' : '' }><span class="name">${ n }</span><span></span>${ m ? `<span class="meta">${ m }</span>` : '' }</label>` ).join( '' ) +
					`</div></fieldset>`;
			}
			const info = r[ c.tableType ] || {};
			if ( info.largest_separate_party > info.largest_table && ( c.party > info.largest_table || c.separate ) ) {
				b += `<label class="check group"><input type="checkbox" data-k="separate"${ c.separate ? ' checked' : '' }><span>We are happy to sit at separate tables<span class="hint">No single table here seats more than ${ info.largest_table }.</span></span></label>`;
			}
			if ( r.table_selection && ! c.separate ) {
				const tables = ( r.tables || [] ).filter( ( t ) => t.table_type === c.tableType && t.capacity >= c.party );
				b += `<div class="group"><label class="label" for="table">Table</label><select id="table" data-k="table"><option value="">Any table that fits</option>${ tables.map( ( t ) => `<option value="${ t.id }"${ String( c.table ) === String( t.id ) ? ' selected' : '' }>${ esc( t.name ) } · seats ${ t.capacity }${ t.minimum_spend ? ' · minimum spend ' + esc( this.cur ) + ' ' + esc( t.minimum_spend ) : '' }</option>` ).join( '' ) }</select></div>`;
			}
		} else if ( k === 'workshop' ) {
			b += `<fieldset class="group fs"><legend class="label">Services <span class="hint" style="display:inline;margin:0">(done one after another)</span></legend><div class="choices">${ base.map( ( s ) => this.serviceChoice( s, 'checkbox', 'services', c.services.includes( s.id ) ) ).join( '' ) }</div></fieldset>`;
			const v = this.vehicles.find( ( x ) => String( x.id ) === String( c.car.model ) );
			b += `<div class="group row"><div><label class="label" for="car-model">Car</label><select id="car-model" data-k="car-model"><option value="">Choose…</option>${ this.vehicles.map( ( x ) => `<option value="${ x.id }"${ String( c.car.model ) === String( x.id ) ? ' selected' : '' }>${ esc( x.make + ' ' + x.model ) }${ x.year_from || x.year_to ? ` (${ x.year_from || '…' }–${ x.year_to || '…' })` : '' }</option>` ).join( '' ) }</select></div>` +
				`<div><label class="label" for="car-year">Model year${ v && ( v.year_from || v.year_to ) ? '' : ' <span class="hint" style="display:inline">(optional)</span>' }</label><input id="car-year" type="text" inputmode="numeric" maxlength="4" autocomplete="off" value="${ esc( c.car.year ) }" data-k="car-year"></div></div>` +
				`<div class="group"><label class="label" for="car-plate">Number plate</label><input id="car-plate" type="text" autocomplete="off" autocapitalize="characters" maxlength="20" value="${ esc( c.car.plate ) }" data-k="car-plate"></div>`;
		} else {
			const pick = k === 'salon' ? 'Service' : k === 'field' ? 'Service' : 'Service';
			b += `<fieldset class="group fs"><legend class="label">${ pick }</legend><div class="choices">${ base.map( ( s ) => this.serviceChoice( s, 'radio', 'service', c.service === s.id ) ).join( '' ) }</div></fieldset>`;
			if ( k === 'salon' ) {
				const addons = this.m.services.filter( ( s ) => s.is_addon );
				if ( addons.length ) {
					b += `<fieldset class="group fs"><legend class="label">Add-ons <span class="hint" style="display:inline;margin:0">(each adds its time to the appointment)</span></legend><div class="choices">${ addons.map( ( s ) => this.serviceChoice( s, 'checkbox', 'addon', c.addons.includes( s.id ), '+ ' + minutes( s.duration_minutes ) ) ).join( '' ) }</div></fieldset>`;
				}
			}
			if ( this.onSite() ) {
				const loc = c.loc;
				b += `<div class="group"><span class="label">Where should we come?</span><div class="where"><button type="button" class="btn secondary" data-act="locate"${ this.st.locating ? ' disabled' : '' }>${ this.st.locating ? 'Finding you…' : 'Use my location' }</button>${ this.m.map && this.m.map.enabled ? `<button type="button" class="link" data-act="map">Pick on a map</button>` : '' }</div>` +
					( loc ? `<p class="msg accent" role="status">${ loc.how === 'map' ? 'Place picked on the map.' : 'Using your current location' + ( loc.accuracy ? ` (within about ${ loc.accuracy } m)` : '' ) + '.' } You will add the street address in the last step.</p>` : '' ) +
					`<div class="map" id="map"${ this.st.mapOpen ? '' : ' hidden' }></div></div>`;
			}
		}
		if ( k !== 'restaurant' && this.m.staff.length > 1 ) {
			b += `<div class="group"><label class="label" for="staff">${ k === 'workshop' ? 'Mechanic' : 'With' }</label><select id="staff" data-k="staff"><option value="">Anyone who is free</option>${ this.m.staff.map( ( s ) => `<option value="${ s.id }"${ String( c.staff ) === String( s.id ) ? ' selected' : '' }>${ esc( s.name ) }</option>` ).join( '' ) }</select></div>`;
		}
		b += this.msgHtml( 'choose' ) + `<div class="actions"><button type="button" class="btn" data-act="see-times">See free times</button></div>`;
		return this.stepWrap( 'choose', title, true, b );
	}

	slotExtra( s ) {
		if ( this.kind === 'restaurant' ) {
			const parts = [];
			if ( s.combined ) {
				parts.push( s.table_ids.length + ' tables' );
			}
			if ( s.minimum_spend ) {
				parts.push( 'min. ' + this.cur + ' ' + s.minimum_spend );
			}
			return parts.join( ' · ' );
		}
		if ( this.kind === 'salon' || this.kind === 'workshop' || this.chosenServices().length > 1 ) {
			return 'until ' + hm( s.end );
		}
		return '';
	}

	whenHtml() {
		const st = this.st;
		const days = this.stripDays();
		const md = st.monthDates;
		const sel = st.day;
		let b = `<div class="days" role="listbox" aria-label="Day" aria-orientation="horizontal">` +
			days.map( ( d ) => {
				const none = md && ! md.has( d );
				return `<button type="button" class="day" role="option" data-act="day" data-day="${ d }" aria-selected="${ d === sel }" tabindex="${ d === sel ? 0 : -1 }"${ none ? ' aria-disabled="true"' : '' } aria-label="${ esc( longDate( d ) ) }${ none ? ', no free times' : '' }"><span class="dow">${ esc( fmtDay( d, { weekday: 'short' } ) ) }</span><span class="dom">${ esc( fmtDay( d, { day: 'numeric' } ) ) }</span><span class="mon">${ esc( fmtDay( d, { month: 'short' } ) ) }</span></button>`;
			} ).join( '' ) +
			`</div>`;
		if ( st.lead.length ) {
			b += `<p class="msg warning">Parts for this work must be ordered: ${ st.lead.map( ( p ) => esc( p.name ) + ( p.estimated_lead_time_days ? ` (about ${ p.estimated_lead_time_days } days)` : '' ) ).join( ', ' ) }. The job may wait for them.</p>`;
		}
		b += this.msgHtml( 'when' );
		if ( st.slow ) {
			b += `<p class="summary" role="status">Finding free times…</p>`;
		}
		const shown = st.slots.slice();
		if ( st.taken && ! shown.find( ( s ) => s.start === st.taken ) ) {
			shown.push( { start: st.taken, end: st.taken, taken: true } );
			shown.sort( ( a, b2 ) => ( a.start < b2.start ? -1 : 1 ) );
		}
		if ( ! st.loading && st.slotsFor && ! shown.length ) {
			const next = this.nextFreeDay( sel );
			b += `<div class="times-empty"><p style="margin:0">No free times on ${ esc( longDate( sel ) ) }.</p>${ next ? `<p style="margin:8px 0 0"><button type="button" class="link" data-act="next-day" data-day="${ next }">See ${ esc( longDate( next ) ) }</button></p>` : '' }</div>`;
		} else {
			b += `<fieldset class="fs"><legend class="vh">Time on ${ esc( longDate( sel || '' ) ) }</legend><div class="times${ st.loading ? ' loading' : '' }"${ st.loading ? ' aria-busy="true"' : '' }>` +
				shown.map( ( s ) => {
					if ( s.taken ) {
						return `<span class="time taken"><span><b>${ hm( s.start ) }</b><small>just taken</small></span></span>`;
					}
					const extra = this.slotExtra( s );
					return `<label class="time"><input type="radio" name="time" value="${ esc( s.start ) }" data-k="time"${ st.slot && st.slot.start === s.start ? ' checked' : '' }><span><b>${ hm( s.start ) }</b>${ extra ? `<small>${ esc( extra ) }</small>` : '' }</span></label>`;
				} ).join( '' ) +
				`</div></fieldset>`;
		}
		return this.stepWrap( 'when', 'When', true, b );
	}

	field( key, label, type, opts = {} ) {
		const st = this.st;
		const err = st.errors[ key ];
		const auto = { name: 'name', phone: 'tel', email: 'email', line1: 'address-line1', city: 'address-level2', postcode: 'postal-code' }[ key ] || 'off';
		const input = type === 'textarea'
			? `<textarea id="d-${ key }" data-k="d-${ key }"${ err ? ' aria-invalid="true"' : '' }>${ esc( st.details[ key ] ) }</textarea>`
			: `<input id="d-${ key }" type="${ type }" data-k="d-${ key }" autocomplete="${ auto }" value="${ esc( st.details[ key ] ) }"${ opts.required ? ' required aria-required="true"' : '' }${ err ? ` aria-invalid="true" aria-describedby="e-${ key }"` : '' }>`;
		return `<div class="group"><label class="label" for="d-${ key }">${ label }${ opts.optional ? ' <span class="hint" style="display:inline">(optional)</span>' : '' }</label>${ input }${ err ? `<span class="field-error" id="e-${ key }">${ esc( err ) }</span>` : '' }</div>`;
	}

	detailsHtml() {
		const st = this.st;
		let b = `<form novalidate>` +
			`<div class="row">${ this.field( 'name', 'Your name', 'text', { required: true } ) }${ this.field( 'phone', 'Phone', 'tel', { required: true } ) }</div>` +
			this.field( 'email', 'Email', 'email', { optional: true } ) +
			`<span class="hint">We send your booking code and a link to change or cancel it.</span>`;
		if ( this.onSite() ) {
			b += this.field( 'line1', 'Street address', 'text', { required: true } ) + `<div class="row">${ this.field( 'city', 'City', 'text', { optional: true } ) }${ this.field( 'postcode', 'Postcode', 'text', { optional: true } ) }</div>`;
		}
		b += this.field( 'request', this.kind === 'restaurant' ? 'Anything we should know?' : 'Notes for us', 'textarea', { optional: true } );
		if ( st.lead.length ) {
			b += `<label class="check"><input type="checkbox" data-k="ack"${ st.ack ? ' checked' : '' }${ st.errors.ack ? ' aria-invalid="true" aria-describedby="e-ack"' : '' }><span>I understand some parts must be ordered first, so the work may take longer.${ st.errors.ack ? `<span class="field-error" id="e-ack">${ esc( st.errors.ack ) }</span>` : '' }</span></label>`;
		}
		const p = this.m.policy;
		if ( p && p.min_notice_cancel_minutes ) {
			b += `<p class="hint">You can cancel online until ${ minutes( p.min_notice_cancel_minutes ) } before.</p>`;
		}
		const spend = st.accepted || ( st.slot && st.slot.minimum_spend );
		b += `<input type="text" name="website" class="hp" tabindex="-1" autocomplete="off" aria-hidden="true">` + this.msgHtml( 'details' );
		const label = st.busy ? 'Booking…' : spend ? `Accept ${ esc( this.cur ) } ${ esc( spend ) } minimum spend and book` : this.m.policy && this.m.policy.needs_approval ? 'Send request' : 'Book';
		b += `<div class="actions"><button type="submit" class="btn${ st.busy ? ' busy' : '' }" data-act="book"${ st.busy ? ' aria-disabled="true"' : '' }>${ label }</button></div></form>`;
		return this.stepWrap( 'details', 'Your details', true, b );
	}

	factsHtml( bk ) {
		const rows = [];
		rows.push( [ 'When', `${ esc( longDate( ymdOf( bk.start ) ) ) }, ${ hm( bk.start ) }–${ hm( bk.end ) }` ] );
		if ( bk.party_size ) {
			rows.push( [ 'Table', `${ bk.party_size } ${ bk.party_size === 1 ? 'person' : 'people' }${ bk.tables && bk.tables.length ? ' · ' + bk.tables.map( ( t ) => esc( t.name ) ).join( ', ' ) : '' }` ] );
		}
		if ( bk.services && bk.services.length ) {
			rows.push( [ 'What', bk.services.map( ( s ) => esc( s.name ) ).join( ' + ' ) ] );
		}
		if ( bk.staff && bk.staff.length ) {
			rows.push( [ 'With', bk.staff.map( ( s ) => esc( s.name ) ).join( ', ' ) ] );
		}
		if ( bk.vehicle ) {
			rows.push( [ 'Car', `${ esc( bk.vehicle.plate_no ) } · ${ esc( bk.vehicle.make + ' ' + bk.vehicle.model ) }` ] );
		}
		if ( bk.address ) {
			rows.push( [ 'Where', esc( [ bk.address.line1, bk.address.postcode, bk.address.city ].filter( Boolean ).join( ', ' ) ) ] );
		}
		if ( bk.meeting_link ) {
			rows.push( [ 'Join', `<a href="${ esc( bk.meeting_link ) }" rel="noopener" target="_blank">${ esc( bk.meeting_link ) }</a>` ] );
		}
		if ( bk.minimum_spend_agreed ) {
			rows.push( [ 'Minimum spend', `${ esc( this.cur ) } ${ esc( bk.minimum_spend_agreed ) }` ] );
		}
		if ( bk.cancel_deadline && bk.can_cancel ) {
			rows.push( [ 'Cancel by', `${ esc( longDate( ymdOf( bk.cancel_deadline ) ) ) }, ${ hm( bk.cancel_deadline ) }` ] );
		}
		return `<dl class="facts">${ rows.map( ( [ k, v ] ) => `<dt>${ k }</dt><dd>${ v }</dd>` ).join( '' ) }</dl>`;
	}

	manageLink( code, token ) {
		const u = new URL( location.href );
		u.searchParams.set( 'jsb_booking', code );
		u.searchParams.set( 'jsb_token', token );
		return u.href;
	}

	doneHtml() {
		const r = this.st.result;
		const bk = r.booking;
		const pending = bk.status === 'pending';
		const body = `<p class="summary" style="margin-top:0">${ pending ? 'Request sent. The business will confirm it' + ( bk.hold_expires ? ' by ' + esc( hm( bk.hold_expires ) ) : '' ) + '.' : 'You are booked.' } Your booking code:</p>` +
			`<p class="done-code" aria-label="Booking code ${ esc( bk.code.split( '' ).join( ' ' ) ) }">${ esc( bk.code ) }</p>` +
			`<p class="hint" style="margin-top:0">Quote it if you call.${ this.st.details.email ? ' We have emailed it to you.' : '' }</p>` +
			this.factsHtml( bk ) +
			`<div class="actions"><a class="btn secondary" href="${ esc( this.manageLink( bk.code, r.manage_token ) ) }">Change or cancel</a><button type="button" class="link" data-act="again">Make another booking</button></div>`;
		return `<section class="step opening" id="step-done"><div class="step-head"><h3>${ pending ? 'Request sent' : 'Booked' }</h3></div><div class="step-body">${ body }</div></section>`;
	}

	manageHtml() {
		const st = this.st;
		const head = this.m ? `<header class="jsb-head"><h2>${ esc( this.m.title ) }</h2></header>` : '';
		if ( st.mode === 'manage-missing' ) {
			return head + `<section class="step" id="manage"><h3>Booking not found</h3><p class="msg error">This link is not valid any more. Check the code in your email${ this.phoneText() ? ',' + this.phoneText().replace( ' You can also', ' or' ) : '.' }</p></section>`;
		}
		const bk = st.booking;
		const status = { pending: 'Waiting for the business to confirm', confirmed: 'Confirmed', cancelled: 'Cancelled', rejected: 'Not accepted', completed: 'Completed', 'no-show': 'Missed', 'in-progress': 'In progress', 'waiting-for-parts': 'Waiting for parts' }[ bk.status ] || bk.status;
		let b = `<p class="summary" style="margin-top:0">Booking code</p><p class="done-code">${ esc( bk.code ) }</p><p class="summary"><strong>${ esc( status ) }</strong></p>` + this.factsHtml( bk ) + this.msgHtml();
		if ( bk.can_cancel ) {
			b += st.confirmCancel
				? `<p class="msg warning" role="status">Cancel this booking? This cannot be undone.</p><div class="actions"><button type="button" class="btn" data-act="cancel-booking"${ st.busy ? ' aria-disabled="true"' : '' }>${ st.busy ? 'Cancelling…' : 'Yes, cancel it' }</button><a class="link" href="${ esc( location.pathname ) }">Keep it</a></div>`
				: `<div class="actions"><button type="button" class="btn secondary" data-act="confirm-cancel">Cancel booking</button></div>`;
		} else if ( [ 'pending', 'confirmed' ].includes( bk.status ) ) {
			b += `<p class="hint">It is too late to cancel online.${ this.phoneText().replace( ' You can also call', ' Please call' ).replace( ' to book.', '.' ) }</p>`;
		}
		b += `<div class="actions"><a class="link" href="${ esc( location.pathname ) }">Make a new booking</a></div>`;
		return head + `<section class="step" id="manage"><h3>Your booking</h3><div class="step-body">${ b }</div></section>`;
	}
}

if ( ! customElements.get( 'jsb-booking' ) ) {
	customElements.define( 'jsb-booking', JsbBooking );
}
