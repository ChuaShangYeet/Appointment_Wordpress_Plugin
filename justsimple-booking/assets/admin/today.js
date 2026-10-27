/**
 * Today: "What's happening today, and what needs me?"
 *
 * The biggest block is the day board: the module's things being booked down the side (stylists, tables, mechanics and bays,
 * professionals), the hours across, and each booking drawn as what it is: confirmed a moss tint, a request a dashed outline with
 * the time it lapses, in progress full moss, waiting for parts hatched, a visit's travel a paler run either side, a booking that
 * runs into the next outlined in red. Time someone is not working is one tone darker. The rail lists what waits on the reader.
 * The sentence under the title says the one thing to know, with its numbers.
 */
import { cfg, api, esc, hm, ymdOf, minutesOf, addDays, dayLabel, plural, STATUS, KIND, say, loading, errorHtml, pressing, loadModules, currentModule, rememberModule, moduleChooser, noModulesHtml } from './app.js';
import { openSheet } from './detail.js';

const root = document.getElementById( 'jsb-admin' );
const NARROW = 640;
// a day in the address (?date=2026-10-31) opens on that day, so a day can be bookmarked or sent to someone
const askedDate = ( new URLSearchParams( location.search ).get( 'date' ) || '' ).match( /^\d{4}-\d{2}-\d{2}$/ )?.[ 0 ] || null;
const st = { modules: [], m: null, date: askedDate, today: null, day: null, error: null, lane: 0, narrow: false };

const LEGEND = {
	confirmed: 'Confirmed',
	pending: 'Waiting to confirm',
	'in-progress': 'In progress',
	'waiting-for-parts': 'Waiting for parts',
	completed: 'Done',
	'no-show': 'Did not come',
};

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
	new ResizeObserver( () => {
		const narrow = root.clientWidth < NARROW;
		if ( narrow !== st.narrow ) {
			st.narrow = narrow;
			if ( st.day ) {
				draw();
			}
		}
	} ).observe( root );
	st.narrow = root.clientWidth < NARROW;
	await load();
	// the board keeps up on its own while it is on screen and nothing is open over it
	setInterval( () => {
		if ( document.visibilityState === 'visible' && ! document.querySelector( '.jsb-sheet' ) ) {
			load( { quiet: true } );
		}
	}, 120000 );
}

async function load( { quiet = false, flash = null } = {} ) {
	const board = root.querySelector( '.jsb-board-card' );
	const req = api( `/admin/modules/${ st.m.id }/day`, { query: { date: st.date } } );
	try {
		st.day = board && ! quiet ? await loading( board, req, 'Loading the day…' ) : await req;
		st.today = st.today || ymdOf( st.day.now ); // the server's today, in the business's clock
		st.date = st.day.date;
		const u = new URL( location.href );
		if ( st.date === st.today ) {
			u.searchParams.delete( 'date' );
		} else {
			u.searchParams.set( 'date', st.date );
		}
		history.replaceState( null, '', u );
		st.error = null;
	} catch ( e ) {
		st.error = e;
		if ( st.day ) {
			st.date = st.day.date; // the title stays with the day whose bookings are on screen
		}
		if ( quiet ) {
			return;
		}
	}
	draw( flash );
}

// ------------------------------------------------------------------ drawing

function draw( flash ) {
	const focusKey = document.activeElement && root.contains( document.activeElement ) ? keyOf( document.activeElement ) : null;
	const d = st.day;
	const isToday = st.date === st.today;
	let h = moduleChooser( st.modules, st.m );
	h += `<div class="jsb-title" style="margin-top:20px"><div><h2>${ isToday ? 'Today, ' : '' }${ esc( dayLabel( st.date ) ) }</h2><p class="jsb-finding">${ d ? finding( d, isToday ) : '' }</p></div>` +
		`<div class="jsb-tools"><button type="button" class="jsb-btn icon" data-act="day" data-d="-1" aria-label="Day before">‹</button>` +
		`<button type="button" class="jsb-btn" data-act="today"${ isToday ? ' disabled' : '' }>Today</button>` +
		`<button type="button" class="jsb-btn icon" data-act="day" data-d="1" aria-label="Day after">›</button>` +
		`<label class="jsb-vh" for="jsb-date">Go to date</label><input id="jsb-date" type="date" data-act="date" value="${ esc( st.date ) }"></div></div>`;
	if ( st.error && ! d ) {
		root.innerHTML = h + errorHtml( st.error );
		return;
	}
	h += `<div class="jsb-grid"><div class="jsb-card jsb-board-card">${ boardHead( d ) }${ st.error ? errorHtml( st.error ) : '' }${ st.narrow ? agenda( d, isToday ) : board( d, isToday ) }</div>${ rail( d ) }</div>`;
	root.innerHTML = h;
	if ( flash ) {
		root.querySelectorAll( `[data-booking="${ flash }"]` ).forEach( ( el ) => el.classList.add( 'flash' ) );
	}
	if ( focusKey ) {
		root.querySelector( focusKey )?.focus();
	}
	if ( isToday && ! st.scrolled ) {
		st.scrolled = true; // once: the board starts at "now"
		const b = root.querySelector( '.jsb-board' );
		const now = root.querySelector( '.jsb-now' );
		if ( b && now ) {
			// "now" a third of the way into the visible hours (the names column stays pinned on the left)
			const names = root.querySelector( '.jsb-lane .who' )?.offsetWidth || 0;
			b.scrollLeft = Math.max( 0, now.getBoundingClientRect().left - b.getBoundingClientRect().left + b.scrollLeft - names - ( b.clientWidth - names ) / 3 );
		}
	}
}

function keyOf( el ) {
	if ( el.id ) {
		return '#' + el.id;
	}
	if ( el.dataset.booking && el.dataset.act ) {
		return `[data-act="${ el.dataset.act }"][data-booking="${ el.dataset.booking }"]`;
	}
	if ( el.dataset.act ) {
		return `[data-act="${ el.dataset.act }"]${ el.dataset.d ? `[data-d="${ el.dataset.d }"]` : '' }`;
	}
	return null;
}

/** The one thing to know, with its numbers. */
function finding( d, isToday ) {
	const c = d.counts;
	const on = isToday ? 'today' : 'that day';
	const tally = c.total ? `${ plural( c.total, 'booking', 'bookings' ) } ${ on }, ${ c.completed ? c.completed + ' done' : 'none done yet' }.` : `No bookings ${ on }.`;
	const req = d.needs_you.filter( ( n ) => n.why === 'request' );
	const clash = d.needs_you.filter( ( n ) => n.why === 'clash' );
	const parts = d.needs_you.filter( ( n ) => n.why === 'parts' );
	if ( d.closed ) {
		return `<strong>Closed ${ on }.</strong> ${ req.length ? plural( req.length, 'request', 'requests' ) + ' still waiting for an answer.' : '' }`;
	}
	if ( req.length ) {
		return `<strong>${ plural( req.length, 'request', 'requests' ) } waiting for you;</strong> the first lapses at ${ hm( req[ 0 ].hold_expires ) }${ ymdOf( req[ 0 ].hold_expires ) !== st.today ? ' on ' + esc( dayLabel( ymdOf( req[ 0 ].hold_expires ), { weekday: 'short', day: 'numeric', month: 'short' } ) ) : '' }. ${ tally }`;
	}
	if ( clash.length ) {
		return `<strong>${ esc( clash[ 0 ].customer ) }'s booking runs into the next one.</strong> ${ tally }`;
	}
	if ( parts.length ) {
		return `<strong>${ plural( parts.length, 'job is', 'jobs are' ) } waiting for parts.</strong> ${ tally }`;
	}
	return `Nothing waiting for you. ${ tally }`;
}

function boardHead( d ) {
	const rows = KIND[ st.m.type ]?.rows || 'staff';
	return `<div class="jsb-card-head"><h3>The day</h3><span class="count">${ d.resources.length ? plural( d.resources.length, rows.replace( /s$/, '' ), rows ) : '' }</span></div>`;
}

/** The hours the board spans: everything working or booked, whole hours, at least 08:00-18:00 when the day is empty. */
function span( d ) {
	let lo = Infinity;
	let hi = -Infinity;
	const take = ( a, b ) => {
		const s = ymdOf( a ) < d.date ? 0 : minutesOf( a );
		const e = ymdOf( b ) > d.date ? 24 * 60 : minutesOf( b ) || 24 * 60;
		lo = Math.min( lo, s );
		hi = Math.max( hi, e );
	};
	d.resources.forEach( ( r ) => r.working.forEach( ( w ) => take( w[ 0 ], w[ 1 ] ) ) );
	d.bars.forEach( ( b ) => take( b.start, b.end ) );
	if ( lo === Infinity ) {
		lo = 8 * 60;
		hi = 18 * 60;
	}
	if ( lo === 0 && hi === 24 * 60 && d.bars.length ) {
		// open round the clock: frame the bookings, not midnight to midnight
		lo = Math.min( ...d.bars.map( ( b ) => ( ymdOf( b.start ) < d.date ? 0 : minutesOf( b.start ) ) ) );
		hi = Math.max( ...d.bars.map( ( b ) => ( ymdOf( b.end ) > d.date ? 24 * 60 : minutesOf( b.end ) ) ) );
		lo = Math.max( 0, lo - 60 );
		hi = Math.min( 24 * 60, hi + 60 );
	}
	lo = Math.floor( lo / 60 ) * 60;
	hi = Math.max( lo + 4 * 60, Math.ceil( hi / 60 ) * 60 );
	return [ lo, hi ];
}

function board( d, isToday ) {
	if ( ! d.resources.length ) {
		return `<div class="jsb-empty"><b>Nobody to book yet</b>${ cfg.manage ? `Add ${ esc( KIND[ st.m.type ]?.rows || 'staff' ) } to this booking page under <a href="${ esc( cfg.urls.modules ) }">Modules</a>; the day draws them here.` : 'The owner has not added anyone to this booking page yet.' }</div>`;
	}
	const [ lo, hi ] = span( d );
	const len = hi - lo;
	const pct = ( iso, clampEnd ) => {
		let m = ymdOf( iso ) < d.date ? 0 : ymdOf( iso ) > d.date ? 24 * 60 : minutesOf( iso );
		if ( clampEnd && m === 0 && ymdOf( iso ) > d.date ) {
			m = 24 * 60;
		}
		return ( ( Math.min( Math.max( m, lo ), hi ) - lo ) / len ) * 100;
	};
	const step = len > 14 * 60 ? 120 : 60;
	const hours = [];
	for ( let t = lo; t <= hi; t += step ) {
		hours.push( t );
	}
	const label = ( t ) => String( Math.floor( t / 60 ) % 24 ).padStart( 2, '0' ) + ':00';
	const holds = Object.fromEntries( d.needs_you.filter( ( n ) => n.hold_expires ).map( ( n ) => [ n.booking_id, n.hold_expires ] ) );
	// the whole day fills the width when it fits; below 64 px an hour a short booking loses its name, so the hours scroll instead
	let h = `<div class="jsb-board" role="region" aria-label="The day board" tabindex="-1"><div class="jsb-board-inner" style="min-width:calc(var(--names) + 20px + ${ ( len / 60 ) * 64 }px)">`;
	// the first and last labels sit inside the track, not centred on its edges (the pinned names would cover half of the first)
	const tick = ( t ) => ( t === lo ? 'translateX(0)' : t === hi ? 'translateX(-100%)' : 'translateX(-50%)' );
	const nowAt = isToday ? minutesOf( d.now ) : -999; // a tick too close to "now" gives way to its label
	h += `<div class="jsb-axis" aria-hidden="true"><span></span><div class="ticks">${ hours.filter( ( t ) => Math.abs( t - nowAt ) >= 25 ).map( ( t ) => `<span style="left:${ ( ( t - lo ) / len ) * 100 }%;transform:${ tick( t ) }">${ label( t ) }</span>` ).join( '' ) }</div></div>`;
	for ( const r of d.resources ) {
		const bars = d.bars.filter( ( b ) => b.resource_id === r.id );
		// the overall hours; a break shows in the row itself
		const sub = r.off === 'closed' ? 'Closed' : r.off === 'leave' ? 'Day off' : r.working.length ? `${ hm( r.working[ 0 ][ 0 ] ) }–${ hm( r.working[ r.working.length - 1 ][ 1 ] ) }` : 'Not working';
		h += `<div class="jsb-lane${ r.mine ? ' mine' : '' }" role="group" aria-label="${ esc( r.name ) }: ${ esc( sub ) }"><div class="who"><b>${ esc( r.name ) }</b><small>${ st.m.type === 'workshop' ? ( r.kind === 'bay' ? 'Bay · ' : 'Mechanic · ' ) : '' }${ esc( sub ) }</small></div><div class="track">`;
		h += hours.slice( 1, -1 ).map( ( t ) => `<i class="hour" style="left:${ ( ( t - lo ) / len ) * 100 }%"></i>` ).join( '' );
		h += r.working.map( ( w ) => `<i class="work" style="left:${ pct( w[ 0 ] ) }%;width:${ pct( w[ 1 ], true ) - pct( w[ 0 ] ) }%"></i>` ).join( '' );
		for ( const b of bars ) {
			const left = pct( b.start );
			const width = Math.max( 0.4, pct( b.end, true ) - left );
			const whole = minutesOf( b.end ) - minutesOf( b.start ) || 1;
			const before = ( ( minutesOf( b.service_start ) - minutesOf( b.start ) ) / whole ) * 100;
			const after = ( ( minutesOf( b.end ) - minutesOf( b.service_end ) ) / whole ) * 100;
			const hold = b.status === 'pending' && holds[ b.booking_id ] ? ` · answer by ${ hm( holds[ b.booking_id ] ) }` : '';
			const aria = `${ hm( b.service_start ) } to ${ hm( b.service_end ) }, ${ b.customer }${ b.what ? ', ' + b.what : '' }, ${ STATUS[ b.status ] }${ b.clash ? ', runs into the next booking' : '' }${ hold }${ before > 0 ? ', with travel time' : '' }`;
			h += `<button type="button" class="jsb-bar ${ esc( b.status ) }${ b.clash ? ' clash' : '' }" data-act="open" data-booking="${ b.booking_id }" style="left:${ left }%;width:${ width }%" aria-label="${ esc( aria ) }">` +
				( before > 0 ? `<i class="pad" style="width:${ before }%"></i>` : '' ) +
				// the bar is short: the first name; the full one is in its label and in the sheet
				`<span class="body"><b>${ esc( b.customer.split( /\s+/ )[ 0 ] ) }</b><span>${ esc( b.what ) }${ esc( hold ) }</span></span>` +
				( after > 0 ? `<i class="pad" style="width:${ after }%"></i>` : '' ) +
				`</button>`;
		}
		h += `</div></div>`;
	}
	if ( isToday ) {
		const now = minutesOf( d.now );
		if ( now >= lo && now <= hi ) {
			h += `<div class="jsb-lane" style="position:absolute;inset:0;border:0;pointer-events:none" aria-hidden="true"><span></span><div style="position:relative;margin-right:20px"><i class="jsb-now" style="left:${ ( ( now - lo ) / len ) * 100 }%" data-label="${ hm( d.now ) }"></i></div></div>`;
		}
	}
	h += `</div></div>` + legend( d );
	return h;
}

/** Only the marks on screen, each drawn as its mark. */
function legend( d ) {
	const seen = new Set( d.bars.map( ( b ) => b.status ) );
	const items = Object.keys( LEGEND ).filter( ( k ) => seen.has( k ) ).map( ( k ) => `<span><i class="sw ${ k }"></i>${ LEGEND[ k ] }</span>` );
	if ( d.bars.some( ( b ) => b.start !== b.service_start || b.end !== b.service_end ) ) {
		items.push( `<span><i class="sw travel"></i>Travel</span>` );
	}
	if ( d.bars.some( ( b ) => b.clash ) ) {
		items.push( `<span><i class="sw clash"></i>Runs into the next</span>` );
	}
	if ( d.resources.some( ( r ) => r.working.length ) ) {
		items.push( `<span><i class="sw off"></i>Not working</span>` );
	}
	return items.length ? `<div class="jsb-legend" aria-hidden="true">${ items.join( '' ) }</div>` : '';
}

/** The phone: one person (or table, or bay) at a time, their day as a list. */
function agenda( d ) {
	if ( ! d.resources.length ) {
		return board( d );
	}
	st.lane = Math.min( st.lane, d.resources.length - 1 );
	const r = d.resources[ st.lane ];
	const bars = d.bars.filter( ( b ) => b.resource_id === r.id );
	let h = `<div class="jsb-agenda"><div class="jsb-agenda-nav"><button type="button" class="jsb-btn icon" data-act="lane" data-d="-1" aria-label="Previous"${ st.lane === 0 ? ' disabled' : '' }>‹</button>` +
		`<b>${ esc( r.name ) }${ r.mine ? ' (you)' : '' } <small style="color:var(--muted);font-weight:400">${ st.lane + 1 } of ${ d.resources.length }</small></b>` +
		`<button type="button" class="jsb-btn icon" data-act="lane" data-d="1" aria-label="Next"${ st.lane === d.resources.length - 1 ? ' disabled' : '' }>›</button></div>`;
	if ( r.off ) {
		h += `<p class="off">${ r.off === 'closed' ? 'Closed' : 'Day off' }.</p>`;
	} else if ( ! bars.length ) {
		h += `<p class="off">${ r.working.length ? 'Working ' + r.working.map( ( w ) => `${ hm( w[ 0 ] ) }–${ hm( w[ 1 ] ) }` ).join( ', ' ) + '; nothing booked.' : 'Not working.' }</p>`;
	} else {
		h += `<ol>${ bars.map( ( b ) => `<li><button type="button" data-act="open" data-booking="${ b.booking_id }"><span class="when">${ hm( b.service_start ) }–${ hm( b.service_end ) }</span><span>${ esc( b.customer ) }<br><small style="color:var(--muted)">${ esc( b.what ) }</small></span><span class="jsb-pill ${ esc( b.status ) }">${ esc( STATUS[ b.status ] ) }</span></button></li>` ).join( '' ) }</ol>`;
	}
	return h + `</div>`;
}

function rail( d ) {
	const items = d.needs_you;
	const why = { request: 'Wants to book', clash: 'Runs into the next booking', parts: 'Waiting for parts' };
	let h = `<div class="jsb-rail"><h3 tabindex="-1">Needs you${ items.length ? ` <span style="color:var(--muted);font-weight:400">${ items.length }</span>` : '' }</h3><div class="jsb-card">`;
	if ( ! items.length ) {
		h += `<p class="jsb-needs-empty">Nothing waiting. Requests to confirm, bookings that run late and jobs waiting for parts show here.</p>`;
	} else {
		h += `<ul class="jsb-needs">${ items.map( ( n ) => `<li><div class="line"><b>${ esc( n.customer ) }</b><span>${ ymdOf( n.start ) === st.date ? hm( n.start ) : esc( dayLabel( ymdOf( n.start ), { weekday: 'short', day: 'numeric', month: 'short' } ) ) + ', ' + hm( n.start ) }</span></div>` +
			`<p class="sub">${ why[ n.why ] }${ n.hold_expires ? ` · lapses at ${ hm( n.hold_expires ) }` : '' }</p>` +
			`<div class="acts">${ n.why === 'request' ? `<button type="button" class="jsb-btn primary" data-act="confirm" data-booking="${ n.booking_id }" data-busy="Confirming…">Confirm</button>` : '' }<button type="button" class="jsb-btn" data-act="open" data-booking="${ n.booking_id }">Open</button></div></li>` ).join( '' ) }</ul>`;
	}
	return h + `</div></div>`;
}

// ------------------------------------------------------------------ events

async function onClick( e ) {
	const tab = e.target.closest( 'a[data-module]' );
	if ( tab ) {
		e.preventDefault();
		pick( parseInt( tab.dataset.module, 10 ) );
		return;
	}
	const t = e.target.closest( '[data-act]' );
	if ( ! t ) {
		return;
	}
	const act = t.dataset.act;
	if ( act === 'day' ) {
		st.date = addDays( st.date, parseInt( t.dataset.d, 10 ) );
		load();
	} else if ( act === 'today' ) {
		st.date = st.today;
		load();
	} else if ( act === 'lane' ) {
		st.lane += parseInt( t.dataset.d, 10 );
		draw();
	} else if ( act === 'retry' ) {
		load();
	} else if ( act === 'open' ) {
		openSheet( parseInt( t.dataset.booking, 10 ), { kind: st.m.type, host: root, returnFocus: t, onChanged: ( b ) => load( { quiet: true, flash: b.id } ) } );
	} else if ( act === 'confirm' ) {
		const id = parseInt( t.dataset.booking, 10 );
		try {
			await pressing( t, () => api( `/admin/bookings/${ id }/status`, { method: 'PATCH', data: { status: 'confirmed' } } ) );
			say( 'Confirmed.' );
			await load( { quiet: true, flash: id } );
			// the item has left the rail: carry on with the next one, or the rail itself when it is empty
			( root.querySelector( '.jsb-needs [data-act]' ) || root.querySelector( '.jsb-rail h3' ) )?.focus();
		} catch ( err ) {
			t.closest( 'li' ).insertAdjacentHTML( 'beforeend', `<p class="jsb-msg error" role="alert">${ esc( err.message ) }</p>` );
		}
	}
}

function onChange( e ) {
	const t = e.target;
	if ( t.dataset.act === 'module' ) {
		pick( parseInt( t.value, 10 ) );
	} else if ( t.dataset.act === 'date' && t.value ) {
		st.date = t.value;
		load();
	}
}

function pick( id ) {
	st.m = st.modules.find( ( m ) => m.id === id ) || st.m;
	rememberModule( st.m );
	st.lane = 0;
	st.scrolled = false;
	load();
}
