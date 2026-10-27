/**
 * A data source for <jsb-booking> that replays answers recorded from the real engine (tests/record-demo.php), so the widget can
 * run on a page with no server: the marketing page's demo. Same methods as RestSource.
 *
 * The recording covers one reference week. Asked about a date, the source takes the recorded answer for the same question on
 * the same weekday and moves it to that date. Times already past are dropped, as the engine drops them. Choosing a person keeps
 * the times the engine offered with that person among them. A booking is not made: the done screen says so.
 */

const DROP = [ 'date', 'month', 'latitude', 'longitude', 'staff', 'table' ];
const CODE = 'ABCDEFGHJKMNPQRSTUVWXYZ23456789';

/** The recorder's key: the question without its date, pin or chosen person, sorted (PHP http_build_query encodes the same). */
function keyOf( q ) {
	return new URLSearchParams(
		Object.entries( q || {} )
			.filter( ( [ k, v ] ) => v !== null && v !== undefined && String( v ) !== '' && ! DROP.includes( k ) )
			.map( ( [ k, v ] ) => [ k, String( v ) ] )
			.sort( ( a, b ) => ( a[ 0 ] < b[ 0 ] ? -1 : 1 ) )
	).toString();
}

const weekday = ( ymd ) => new Date( ymd + 'T12:00:00Z' ).getUTCDay();

export class DemoSource {
	/** @param {object} data one recorded file: { kind, ref: [7 dates, Monday first], module, vehicles, answers } */
	constructor( data, { delay = 220 } = {} ) {
		this.data = data;
		this.demo = true;
		this.delay = delay;
		this.last = { slots: [] };
	}

	wait( value ) {
		return new Promise( ( ok ) => setTimeout( () => ok( value ), this.delay ) );
	}

	module() {
		return this.wait( JSON.parse( JSON.stringify( this.data.module ) ) );
	}

	vehicles() {
		return this.wait( this.data.vehicles || [] );
	}

	/** The recorded day with this weekday, moved to `ymd`. */
	day( q, ymd ) {
		const w = weekday( ymd );
		const entry = ( this.data.answers[ keyOf( q ) ] || {} )[ w ];
		if ( ! entry ) {
			return { slots: [] };
		}
		const from = this.data.ref[ ( w + 6 ) % 7 ];
		const out = JSON.parse( JSON.stringify( entry ).split( from ).join( ymd ) );
		const now = Date.now();
		out.slots = out.slots.filter( ( s ) => Date.parse( s.start ) > now );
		if ( q.staff ) {
			out.slots = out.slots.filter( ( s ) => ( s.staff_ids || [] ).includes( Number( q.staff ) ) );
		}
		return out;
	}

	availability( id, q ) {
		if ( ! this.data.answers[ keyOf( q ) ] ) {
			// not "no free times": the demo was simply not asked this
			return Promise.reject( Object.assign( new Error( 'The demo covers parties of up to 12 and the choices shown here. On your own site, every choice gets its own free times.' ), { status: 422, code: 'demo_not_recorded', data: {} } ) );
		}
		const out = this.day( q, q.date );
		this.last = out;
		return this.wait( Object.assign( { date: q.date }, out ) );
	}

	month( id, q ) {
		const [ y, m ] = q.month.split( '-' ).map( Number );
		const days = new Date( Date.UTC( y, m, 0 ) ).getUTCDate();
		const dates = [];
		for ( let d = 1; d <= days; d++ ) {
			const ymd = `${ q.month }-${ String( d ).padStart( 2, '0' ) }`;
			if ( this.day( q, ymd ).slots.length ) {
				dates.push( ymd );
			}
		}
		return this.wait( { month: q.month, dates } );
	}

	/** Nothing is booked: the answer is what the real confirmation would show, built from the time the engine offered. */
	book( id, body ) {
		const m = this.data.module;
		const slot = ( this.last.slots || [] ).find( ( s ) => s.start === body.start );
		if ( ! slot ) {
			return Promise.reject( Object.assign( new Error( 'That time is not available any more.' ), { status: 409, code: 'slot_unavailable', data: { slots: this.last.slots || [] } } ) );
		}
		const svc = ( ids ) => ( ids || [] ).map( ( i ) => m.services.find( ( s ) => s.id === i ) ).filter( Boolean ).map( ( s ) => ( { id: s.id, name: s.name } ) );
		const who = body.staff_id || ( slot.staff_ids || [] )[ 0 ];
		const person = ( m.staff || [] ).find( ( s ) => s.id === Number( who ) );
		const car = body.vehicle && ( this.data.vehicles || [] ).find( ( v ) => v.id === body.vehicle.supported_vehicle_id );
		let code = 'DEMO';
		for ( let i = 0; i < 8; i++ ) {
			code += CODE[ Math.floor( Math.random() * CODE.length ) ];
		}
		return this.wait( {
			booking: {
				code,
				status: m.policy && m.policy.needs_approval ? 'pending' : 'confirmed',
				start: slot.start,
				end: slot.end,
				services: svc( body.service_ids ),
				staff: person ? [ { id: person.id, name: person.name } ] : [],
				party_size: body.party_size || null,
				vehicle: car ? { plate_no: String( body.vehicle.plate_no || '' ).toUpperCase(), make: car.make, model: car.model } : null,
				address: body.address ? { line1: body.address.line1, city: body.address.city, postcode: body.address.postcode } : null,
				minimum_spend_agreed: body.accepted_minimum_spend || null,
				can_cancel: false,
				cancel_deadline: null,
			},
			manage_token: null,
		} );
	}

	show() {
		return Promise.reject( Object.assign( new Error( 'Not found' ), { status: 404, code: 'not_found', data: {} } ) );
	}

	cancel() {
		return this.show();
	}
}
