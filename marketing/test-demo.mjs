// Keyboard-only check of the marketing page's demo: for each demo business, reach its button with Tab, press Enter, then book a
// time in the real widget using Tab, arrow keys, typing and Enter, and read what the widget announces.
//   php marketing/build.php && node marketing/test-demo.mjs                       the built page, recorded answers
//   node marketing/test-demo.mjs http://localhost/jsb-test/                      a site whose Showcase serves the page: live
//   node marketing/test-demo.mjs "http://localhost/jsb-test/?demo=recorded"      the same site, forced onto the recorded answers
import { createServer } from 'node:http';
import { createReadStream, statSync } from 'node:fs';
import { extname, join, resolve, sep } from 'node:path';
import { fileURLToPath } from 'node:url';
import { Browser } from 'file:///C:/Users/DespairSoul/.claude/skills/design-taste-frontend/scripts/lib/cdp.mjs';

const root = resolve( fileURLToPath( new URL( './dist', import.meta.url ) ) );
const TYPES = { '.html': 'text/html', '.css': 'text/css', '.js': 'text/javascript', '.json': 'application/json', '.webp': 'image/webp', '.woff2': 'font/woff2' };
const server = createServer( ( req, res ) => {
	try {
		let f = resolve( join( root, decodeURIComponent( new URL( req.url, 'http://x' ).pathname ) ) );
		if ( f !== root && ! f.startsWith( root + sep ) ) {
			throw 0;
		}
		if ( statSync( f ).isDirectory() ) {
			f = join( f, 'index.html' );
		}
		res.writeHead( 200, { 'content-type': TYPES[ extname( f ) ] || 'application/octet-stream' } );
		createReadStream( f ).pipe( res );
	} catch {
		res.writeHead( 404 );
		res.end();
	}
} );
const site = process.argv[ 2 ] || null;
if ( ! site ) {
	await new Promise( ( ok ) => server.listen( 0, '127.0.0.1', ok ) );
}
const url = site || `http://127.0.0.1:${ server.address().port }/`;

const KEYS = { Tab: 9, Enter: 13, ' ': 32, ArrowRight: 39 };
const sleep = ( ms ) => new Promise( ( ok ) => setTimeout( ok, ms ) );
// the element that really has focus, inside the widget's shadow root too
const DEEP = `(() => { let a = document.activeElement; while (a && a.shadowRoot && a.shadowRoot.activeElement) a = a.shadowRoot.activeElement; return a; })()`;

const b = await Browser.launch();
let failures = 0;
try {
	const p = await b.newPage( { width: 1280, height: 900 } );
	const press = async ( key ) => {
		const code = KEYS[ key ];
		const base = { key, code: key === ' ' ? 'Space' : key, windowsVirtualKeyCode: code, nativeVirtualKeyCode: code };
		await p.send( 'Input.dispatchKeyEvent', { type: 'keyDown', ...base, text: key === 'Enter' ? '\r' : key === ' ' ? ' ' : undefined } );
		await p.send( 'Input.dispatchKeyEvent', { type: 'keyUp', ...base } );
		await sleep( 40 );
	};
	const type = async ( text ) => p.send( 'Input.insertText', { text } );
	const focusedIs = ( sel ) => p.eval( `(() => { const a = ${ DEEP }; return !!a && a.matches(${ JSON.stringify( sel ) }); })()` );
	/** Tab forward until the focused element matches, as a keyboard user would. */
	const tabTo = async ( sel, max = 120 ) => {
		for ( let i = 0; i < max; i++ ) {
			if ( await focusedIs( sel ) ) {
				return true;
			}
			await press( 'Tab' );
		}
		return false;
	};
	const has = ( sel ) => p.eval( `!!document.querySelector('jsb-booking')?.shadowRoot.querySelector(${ JSON.stringify( sel ) })` );

	for ( const line of [ 'Haircut and gel, Saturday', 'Brake pads for a 2019 Myvi, Tuesday', 'Aircon check at my place, Monday', 'See the doctor, Thursday', 'Table for 8, Friday 8pm' ] ) {
		await p.goto( url, { settleMs: 1200 } );
		const steps = [];
		const step = async ( what, ok ) => {
			steps.push( `${ ok ? 'ok' : 'FAIL' } ${ what }` );
			if ( ! ok ) {
				throw new Error( what );
			}
		};
		try {
			await step( 'Tab reaches the business', await tabTo( `.biz [data-ask="${ line }"]` ) );
			await press( 'Enter' );
			await step( 'it is the chosen one', ( await p.eval( `document.querySelector('.biz [data-ask="${ line }"]').getAttribute('aria-pressed')` ) ) === 'true' );
			// wait for the times, as a person would: the live engine takes a second or two a step, the recorded answers almost none
			// (and until they stop changing: a new business's form loads its first day, then the day asked for)
			const times = `[...document.querySelector('jsb-booking').shadowRoot.querySelectorAll('[data-k="time"]')].map((t) => t.textContent).join()`;
			for ( let waited = 0, last = '', same = 0; waited < 20000 && same < 3; waited += 250 ) {
				await sleep( 250 );
				const now = ( await has( '[data-k="time"]' ) ) ? await p.eval( times ) : '';
				same = now && now === last ? same + 1 : 0;
				last = now;
			}
			const shown = await has( '[data-k="time"]' );
			if ( ! shown ) {
				steps.push( '   the widget says: ' + ( await p.eval( `document.querySelector('jsb-booking').shadowRoot.querySelector('#root').innerText.replace(/\\n+/g, ' | ').slice(0, 400)` ) ) );
			}
			await step( 'times are shown', shown );
			await step( 'Tab reaches the times', await tabTo( '[data-k="time"]' ) );
			await press( 'ArrowRight' );
			await sleep( 500 );
			await step( 'Tab reaches the name', await tabTo( '[data-k="d-name"]' ) );
			await type( 'Keyboard Demo' );
			await press( 'Tab' );
			await step( 'Tab reaches the country code, Malaysia', await p.eval( `(() => { const a = ${ DEEP }; return !! a && a.matches('[data-k="d-phone_country"]') && a.value === 'MY'; })()` ) );
			await press( 'Tab' );
			await step( 'then the mobile number', await focusedIs( '[data-k="d-phone"]' ) );
			await type( '0123456789' );
			if ( await has( '[data-k="ack"]' ) ) {
				await step( 'Tab reaches the parts notice', await tabTo( '[data-k="ack"]' ) );
				await press( ' ' );
			}
			if ( await has( '[data-k="d-line1"]' ) ) {
				await step( 'Tab reaches the street address', await tabTo( '[data-k="d-line1"]' ) );
				await type( '12 Jalan Demo' );
			}
			await step( 'Tab reaches Book', await tabTo( '[data-act="book"]' ) );
			await press( 'Enter' );
			await sleep( 1200 );
			const done = await p.eval( `(() => { const r = document.querySelector('jsb-booking').shadowRoot; return { live: r.querySelector('#live').textContent, demo: !!r.querySelector('#step-done .msg.accent'), facts: (r.querySelector('.facts') || {}).innerText || '', focus: (${ DEEP } || {}).tagName }; })()` );
			// live bookings get real codes; recorded ones start DEMO. The note above the form says which this is.
			const expectLive = !! site && ! /demo=recorded/.test( site );
			const noteText = await p.eval( `document.getElementById('answer-note').textContent` );
			await step( `booked: "${ done.live }"`, expectLive ? /Your code is/.test( done.live ) && ! /DEMO/.test( done.live.replace( / /g, '' ) ) : /DEMO/.test( done.live.replace( / /g, '' ) ) );
			await step( `the page says it is ${ expectLive ? 'live' : 'recorded' }`, expectLive ? /booking live/.test( noteText ) : ! /booking live/.test( noteText ) );
			await step( 'the done screen says it is a demo', done.demo );
			await step( 'focus moved to the done heading', done.focus === 'H3' );
			steps.push( '   ' + done.facts.replace( /\n+/g, ' | ' ) );
		} catch ( e ) {
			failures++;
			steps.push( '   the widget says: ' + ( await p.eval( `document.querySelector('jsb-booking').shadowRoot.querySelector('#root').innerText.replace(/\\n+/g, ' | ').slice(0, 400)` ) ) );
			steps.push( '   focus is on: ' + ( await p.eval( `(() => { const a = ${ DEEP }; return a ? a.tagName + ' ' + ( a.getAttribute('data-k') || a.className || '' ) : 'nothing'; })()` ) ) );
		}
		console.log( `== ${ line }\n  ${ steps.join( '\n  ' ) }` );
	}
	if ( p.consoleErrors.length ) {
		console.log( 'page errors: ' + p.consoleErrors.join( ' / ' ) );
		failures++;
	}
} finally {
	await b.close();
	if ( ! site ) {
		server.close();
	}
}
console.log( failures ? `${ failures } FAILED` : 'all passed' );
process.exit( failures ? 1 : 0 );
