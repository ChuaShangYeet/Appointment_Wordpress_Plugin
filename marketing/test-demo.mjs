// Keyboard-only check of the marketing page's demo: for each example line, reach its button with Tab, press Enter, then book a
// time in the real widget using Tab, arrow keys, typing and Enter, and read what the widget announces.
//   php marketing/build.php && node marketing/test-demo.mjs
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
await new Promise( ( ok ) => server.listen( 0, '127.0.0.1', ok ) );
const url = `http://127.0.0.1:${ server.address().port }/`;

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
			if ( line.startsWith( 'Table' ) ) {
				await step( 'the page opens on this line', ( await p.eval( `document.getElementById('ask').value` ) ) === line );
			} else {
				await step( 'Tab reaches the example', await tabTo( `[data-ask="${ line }"]` ) );
				await press( 'Enter' );
			}
			await sleep( 1800 );
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
			await step( `booked: "${ done.live }"`, /DEMO/.test( done.live.replace( / /g, '' ) ) );
			await step( 'the done screen says it is a demo', done.demo );
			await step( 'focus moved to the done heading', done.focus === 'H3' );
			steps.push( '   ' + done.facts.replace( /\n+/g, ' | ' ) );
		} catch ( e ) {
			failures++;
		}
		console.log( `== ${ line }\n  ${ steps.join( '\n  ' ) }` );
	}
	if ( p.consoleErrors.length ) {
		console.log( 'page errors: ' + p.consoleErrors.join( ' / ' ) );
		failures++;
	}
} finally {
	await b.close();
	server.close();
}
console.log( failures ? `${ failures } FAILED` : 'all passed' );
process.exit( failures ? 1 : 0 );
