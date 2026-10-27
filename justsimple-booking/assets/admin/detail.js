/**
 * The booking detail: a sheet from the right over the list or the board, which stay usable behind it. It shows what was booked,
 * by kind, and only the next steps the booking's status and kind allow (the engine's StatusFlow). Destructive steps ask in
 * place. Esc closes it and focus goes back where it came from.
 */
import { api, esc, hm, ymdOf, dayLabel, pill, pressing, say, errorHtml, plural } from './app.js';

const ARRIVES = [ 'salon', 'workshop', 'field' ]; // kinds with the "in progress" step

/** status => [ [to, label, style, needs asking] ] */
function steps( b, kind ) {
	switch ( b.status ) {
		case 'pending':
			return [ [ 'confirmed', 'Confirm', 'primary' ], [ 'rejected', 'Turn down', 'danger', 'Turn this request down? The customer is told.' ] ];
		case 'confirmed':
			return [
				...( ARRIVES.includes( kind ) ? [ [ 'in-progress', kind === 'field' ? 'Started' : 'Arrived', 'primary' ] ] : [] ),
				[ 'completed', 'Done', ARRIVES.includes( kind ) ? '' : 'primary' ],
				[ 'no-show', 'Did not come', '', 'Mark as not come? The time is freed.' ],
				[ 'cancelled', 'Cancel', 'danger', 'Cancel this booking? The customer is told.' ],
			];
		case 'in-progress':
			return [ [ 'completed', 'Done', 'primary' ], ...( kind === 'workshop' ? [ [ 'waiting-for-parts', 'Waiting for parts', '' ] ] : [] ) ];
		case 'waiting-for-parts':
			return [ [ 'cancelled', 'Cancel the job', 'danger', 'Cancel this job? The customer is told.' ] ];
		default:
			return [];
	}
}

let sheet = null;

export function closeSheet() {
	if ( ! sheet ) {
		return;
	}
	const back = sheet.returnFocus;
	sheet.el.remove();
	document.removeEventListener( 'keydown', sheet.onKey );
	sheet = null;
	if ( back && back.isConnected ) {
		back.focus();
	}
}

/**
 * @param {number} id
 * @param {{ kind: string, host: HTMLElement, onChanged?: (b:object) => void, returnFocus?: HTMLElement }} o
 */
export async function openSheet( id, o ) {
	closeSheet();
	const el = document.createElement( 'aside' );
	el.className = 'jsb-sheet';
	el.setAttribute( 'role', 'dialog' );
	el.setAttribute( 'aria-modal', 'false' );
	el.setAttribute( 'aria-labelledby', 'jsb-sheet-title' );
	el.innerHTML = `<div class="jsb-sheet-head"><h2 id="jsb-sheet-title" tabindex="-1">Booking</h2><button type="button" class="jsb-btn icon" data-act="close" aria-label="Close">×</button></div><div class="jsb-sheet-body"><p class="jsb-slow">Loading…</p></div>`;
	o.host.appendChild( el );
	const onKey = ( e ) => {
		if ( e.key === 'Escape' ) {
			closeSheet();
		}
	};
	document.addEventListener( 'keydown', onKey );
	sheet = { el, onKey, returnFocus: o.returnFocus || document.activeElement, id, o, asking: null };
	el.addEventListener( 'click', ( e ) => onClick( e ) );
	el.querySelector( 'h2' ).focus();
	await load();
}

async function load( flash ) {
	const s = sheet;
	try {
		const b = await api( `/admin/bookings/${ s.id }` );
		s.b = b;
		s.history = b.mine === false ? [] : await api( `/admin/bookings/${ s.id }/history` ).catch( () => [] ); // a colleague's history is not theirs to read
		if ( sheet === s ) {
			draw( flash );
		}
	} catch ( e ) {
		if ( sheet === s ) {
			s.el.querySelector( '.jsb-sheet-body' ).innerHTML = errorHtml( e );
		}
	}
}

function draw( msg ) {
	const { b, o, el } = sheet;
	const hadFocus = el.contains( document.activeElement );
	const kind = o.kind;
	const title = b.customer.name || 'Booking';
	const facts = [];
	facts.push( [ 'When', `${ esc( dayLabel( ymdOf( b.start ) ) ) }, ${ hm( b.start ) }–${ hm( b.end ) }` ] );
	if ( b.party_size ) {
		facts.push( [ 'Party', `${ plural( b.party_size, 'person', 'people' ) }${ b.table_type === 'vip' ? ' · VIP' : '' }` ] );
	}
	if ( b.services && b.services.length ) {
		facts.push( [ 'What', b.services.map( ( x ) => esc( x.name ) + ( x.is_upsell ? ' <small>(added on the day)</small>' : '' ) ).join( ' + ' ) ] );
	}
	const res = [ ...( b.staff || [] ), ...( b.bays || [] ) ];
	if ( res.length ) {
		facts.push( [ kind === 'workshop' ? 'Mechanic, bay' : 'With', res.map( ( x ) => esc( x.name ) ).join( ', ' ) ] );
	}
	if ( b.tables && b.tables.length ) {
		facts.push( [ b.tables.length > 1 ? 'Tables' : 'Table', b.tables.map( ( x ) => esc( x.name ) ).join( ', ' ) ] );
	}
	if ( b.mine !== false ) {
		const c = b.customer;
		facts.push( [ 'Phone', c.phone ? `<a href="tel:${ esc( c.phone.replace( /[^\d+]/g, '' ) ) }">${ esc( c.phone ) }</a>` : '–' ] );
		if ( c.email ) {
			facts.push( [ 'Email', `<a href="mailto:${ esc( c.email ) }">${ esc( c.email ) }</a>` ] );
		}
		if ( b.vehicle ) {
			facts.push( [ 'Car', `${ esc( b.vehicle.plate_no ) } · ${ esc( b.vehicle.make + ' ' + b.vehicle.model ) }${ b.vehicle.year ? ' ' + esc( b.vehicle.year ) : '' }` ] );
		}
		if ( b.address ) {
			facts.push( [ 'Where', esc( [ b.address.line1, b.address.postcode, b.address.city ].filter( Boolean ).join( ', ' ) ) ] );
		}
		if ( b.meeting_link ) {
			facts.push( [ 'Meeting', `<a href="${ esc( b.meeting_link ) }" target="_blank" rel="noopener">${ esc( b.meeting_link ) }</a>` ] );
		}
		if ( b.minimum_spend_agreed ) {
			facts.push( [ 'Minimum spend', esc( b.minimum_spend_agreed ) ] );
		}
		if ( b.special_request ) {
			facts.push( [ 'Request', esc( b.special_request ) ] );
		}
		if ( b.hold_expires && b.status === 'pending' ) {
			facts.push( [ 'Answer by', `${ hm( b.hold_expires ) }${ ymdOf( b.hold_expires ) !== ymdOf( b.start ) ? ', ' + esc( dayLabel( ymdOf( b.hold_expires ), { day: 'numeric', month: 'short' } ) ) : '' } <small>(then it lapses)</small>` ] );
		}
		if ( b.actual_duration_minutes ) {
			facts.push( [ 'Took', `${ b.actual_duration_minutes } min` ] );
		}
		if ( b.parts && b.parts.length ) {
			facts.push( [ 'Parts', b.parts.map( ( p ) => `${ esc( p.name ) } × ${ p.quantity }` ).join( ', ' ) ] );
		}
		if ( b.products && b.products.length ) {
			facts.push( [ 'Sold', b.products.map( ( p ) => `${ esc( p.name ) } × ${ p.quantity }` ).join( ', ' ) ] );
		}
	}

	let body = `<dl class="jsb-facts">${ facts.map( ( [ k, v ] ) => `<dt>${ k }</dt><dd>${ v }</dd>` ).join( '' ) }</dl>`;
	if ( msg ) {
		body += `<div class="jsb-msg ${ msg.type }" role="${ msg.type === 'error' ? 'alert' : 'status' }">${ esc( msg.text ) }</div>`;
	}
	if ( b.mine === false ) {
		body += `<section><p class="jsb-msg">This is a colleague's booking: you see when and what. Its details and next steps stay with them.</p></section>`;
	} else {
		const st = steps( b, kind );
		const asking = sheet.asking;
		let acts = '';
		if ( asking ) {
			acts = `<div class="jsb-ask" role="group" aria-labelledby="jsb-ask-q"><p id="jsb-ask-q">${ esc( asking[ 3 ] ) }</p><div class="jsb-actions"><button type="button" class="jsb-btn primary" data-act="status" data-to="${ asking[ 0 ] }" data-sure="1" data-busy="Working…">Yes, ${ esc( asking[ 1 ].toLowerCase() ) }</button><button type="button" class="jsb-btn" data-act="not-now">Keep it</button></div></div>`;
		} else if ( st.length ) {
			acts = `<div class="jsb-actions">${ st.map( ( [ to, label, style ] ) => `<button type="button" class="jsb-btn ${ style }" data-act="status" data-to="${ to }" data-busy="Working…">${ esc( label ) }</button>` ).join( '' ) }</div>`;
		}
		if ( b.schedule_conflict ) {
			acts += `<p class="jsb-msg error" style="margin-top:12px">This booking runs into the next one. <button type="button" class="jsb-link" data-act="sorted" data-busy="Saving…">Mark as sorted</button></p>`;
		}
		if ( b.status === 'waiting-for-parts' ) {
			acts += `<div style="margin-top:16px"><label class="jsb-inline">Parts are in: book the rest for <input type="date" data-k="resume-day" value="${ esc( ymdOf( b.start ) ) }"> at <input type="time" data-k="resume-time" step="900" value="09:00"></label><div class="jsb-actions" style="margin-top:8px"><button type="button" class="jsb-btn primary" data-act="resume" data-busy="Booking…">Book the rest</button></div></div>`;
		}
		if ( kind === 'field' && b.status === 'in-progress' ) {
			acts += `<div style="margin-top:16px" class="jsb-inline"><label for="jsb-took">It took</label><input id="jsb-took" type="number" min="1" max="1440" style="width:90px" value="${ b.actual_duration_minutes || '' }"> minutes <button type="button" class="jsb-btn" data-act="took" data-busy="Saving…">Save</button></div>`;
		}
		if ( kind === 'field' && ( b.services || [] ).some( ( x ) => x.location_type === 'virtual' ) && [ 'pending', 'confirmed' ].includes( b.status ) ) {
			acts += `<div style="margin-top:16px" class="jsb-inline"><label for="jsb-room">Meeting link</label><input id="jsb-room" type="url" style="flex:1;min-width:200px" value="${ esc( b.meeting_link || '' ) }" placeholder="https://"> <button type="button" class="jsb-btn" data-act="room" data-busy="Saving…">Save</button></div>`;
		}
		if ( acts ) {
			body += `<section><h3>Next</h3>${ acts }</section>`;
		}
		if ( sheet.history.length ) {
			body += `<section><h3>History</h3><ol class="jsb-history">${ sheet.history.map( ( h ) => `<li><span>${ esc( dayLabel( ymdOf( h.at ), { day: 'numeric', month: 'short' } ) ) }, ${ hm( h.at ) }</span><span><b>${ esc( STATUS_SHORT[ h.to ] || h.to ) }</b>${ h.by ? ' by ' + esc( h.by ) : '' }${ h.note ? ' · ' + esc( h.note ) : '' }</span></li>` ).join( '' ) }</ol></section>`;
		}
	}
	el.querySelector( '.jsb-sheet-head' ).innerHTML =
		`<h2 id="jsb-sheet-title" tabindex="-1">${ esc( title ) }</h2><button type="button" class="jsb-btn icon" data-act="close" aria-label="Close">×</button>` +
		`<div class="meta">${ pill( b.status ) }${ b.schedule_conflict ? '<span class="jsb-pill clash">Runs into the next</span>' : '' }${ b.code ? `<span>Code <b>${ esc( b.code ) }</b></span>` : '' }</div>`;
	el.querySelector( '.jsb-sheet-body' ).innerHTML = body;
	if ( hadFocus && ! el.contains( document.activeElement ) ) {
		el.querySelector( 'h2' ).focus(); // the redraw replaced what had focus
	}
}

const STATUS_SHORT = { pending: 'Requested', confirmed: 'Confirmed', 'in-progress': 'Started', 'waiting-for-parts': 'Waiting for parts', completed: 'Done', cancelled: 'Cancelled', rejected: 'Turned down', 'no-show': 'Did not come' };

async function onClick( e ) {
	const t = e.target.closest( '[data-act]' );
	if ( ! t || ! sheet ) {
		return;
	}
	const s = sheet;
	const act = t.dataset.act;
	if ( act === 'close' ) {
		closeSheet();
		return;
	}
	if ( act === 'retry' ) {
		load();
		return;
	}
	if ( act === 'not-now' ) {
		s.asking = null;
		draw();
		s.el.querySelector( '[data-act="status"]' )?.focus();
		return;
	}
	if ( act === 'status' ) {
		const step = steps( s.b, s.o.kind ).find( ( x ) => x[ 0 ] === t.dataset.to );
		if ( step && step[ 3 ] && ! t.dataset.sure ) {
			s.asking = step;
			draw();
			s.el.querySelector( '.jsb-ask [data-act="status"]' ).focus();
			return;
		}
		return run( t, () => api( `/admin/bookings/${ s.id }/status`, { method: 'PATCH', data: { status: t.dataset.to } } ), `${ STATUS_SHORT[ t.dataset.to ] }.` );
	}
	if ( act === 'sorted' ) {
		return run( t, () => api( `/admin/bookings/${ s.id }`, { method: 'PATCH', data: { schedule_conflict: false } } ), 'Marked as sorted.' );
	}
	if ( act === 'took' ) {
		const v = parseInt( s.el.querySelector( '#jsb-took' ).value, 10 );
		return run( t, () => api( `/admin/bookings/${ s.id }/actual-duration`, { method: 'PATCH', data: { minutes: v } } ), 'Saved.' );
	}
	if ( act === 'room' ) {
		const v = s.el.querySelector( '#jsb-room' ).value.trim();
		return run( t, () => api( `/admin/bookings/${ s.id }/meeting-link`, { method: 'PATCH', data: { url: v || null } } ), 'Meeting link saved.' );
	}
	if ( act === 'resume' ) {
		const day = s.el.querySelector( '[data-k="resume-day"]' ).value;
		const time = s.el.querySelector( '[data-k="resume-time"]' ).value;
		return run( t, () => api( `/admin/bookings/${ s.id }/resume`, { method: 'POST', data: { start: `${ day }T${ time }:00${ s.b.start.slice( 19 ) }` } } ), 'The rest of the job is booked.' );
	}
}

/** One step: the button says it is working; then the server's answer is drawn, or its refusal in its own words. */
async function run( btn, fn, done ) {
	const s = sheet;
	try {
		await pressing( btn, fn );
		s.asking = null;
		await load( { type: 'ok', text: done } );
		say( done );
		if ( s.o.onChanged ) {
			s.o.onChanged( s.b );
		}
		s.el.querySelector( 'h2' ).focus();
	} catch ( e ) {
		if ( sheet !== s ) {
			return;
		}
		s.asking = null;
		draw( { type: 'error', text: e.message } );
		say( e.message );
	}
}
