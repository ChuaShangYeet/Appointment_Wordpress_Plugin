/**
 * The optional "Pick on a map" for on-site visits: OpenStreetMap tiles under a fixed crosshair. The customer moves the map
 * (drag, or arrow keys) until the crosshair sits on their door, then presses "Use this spot". No library, loaded only when
 * opened; the owner can switch it off (Settings › map_picker). Tiles come from tile.openstreetmap.org with its attribution.
 */

const TILE = 256;
const MIN_Z = 12;
const MAX_Z = 18;
const TILE_URL = ( z, x, y ) => `https://tile.openstreetmap.org/${ z }/${ x }/${ y }.png`;

// Web Mercator, in world pixels at zoom z
const toPx = ( lat, lon, z ) => {
	const n = TILE * 2 ** z;
	const s = Math.sin( ( lat * Math.PI ) / 180 );
	return { x: ( ( lon + 180 ) / 360 ) * n, y: ( 0.5 - Math.log( ( 1 + s ) / ( 1 - s ) ) / ( 4 * Math.PI ) ) * n };
};
const toLatLon = ( x, y, z ) => {
	const n = TILE * 2 ** z;
	const lon = ( x / n ) * 360 - 180;
	const lat = ( Math.atan( Math.sinh( Math.PI * ( 1 - ( 2 * y ) / n ) ) ) * 180 ) / Math.PI;
	return { lat, lon };
};

const CSS = `
.mp { position: relative; height: 320px; overflow: hidden; border: 1px solid var(--jsb-control); border-radius: var(--jsb-radius-control);
	background: var(--jsb-raised); touch-action: none; cursor: grab; outline: none; }
.mp:focus-visible { outline: 2px solid var(--jsb-accent); outline-offset: 2px; }
.mp.dragging { cursor: grabbing; }
.mp-tiles { position: absolute; inset: 0; }
.mp-tiles img { position: absolute; width: ${ TILE }px; height: ${ TILE }px; user-select: none; -webkit-user-drag: none; pointer-events: none; }
.mp-cross { position: absolute; left: 50%; top: 50%; width: 28px; height: 28px; margin: -14px 0 0 -14px; pointer-events: none; }
.mp-cross::before, .mp-cross::after { content: ""; position: absolute; background: var(--jsb-ink); box-shadow: 0 0 0 2px #fff; }
.mp-cross::before { left: 13px; top: 0; width: 2px; height: 28px; }
.mp-cross::after { top: 13px; left: 0; height: 2px; width: 28px; }
.mp-zoom { position: absolute; right: 8px; top: 8px; display: grid; gap: 4px; }
.mp-zoom button { width: 44px; height: 44px; border: 1px solid var(--jsb-control); border-radius: var(--jsb-radius-control); background: #fff; font: inherit; font-size: 20px; cursor: pointer; color: var(--jsb-ink); }
.mp-credit { position: absolute; right: 0; bottom: 0; padding: 2px 6px; background: rgba(255,255,255,.85); font-size: 12px; }
.mp-credit a { color: var(--jsb-ink); }
`;

export function openMapPicker( holder, { lat, lon, onPick, onCancel } ) {
	let z = 15;
	let c = toPx( lat, lon, z ); // the world pixel under the crosshair
	holder.hidden = false;
	holder.innerHTML = `<style>${ CSS }</style>
		<p class="hint" id="mp-help" style="margin:0 0 8px">Move the map until the cross is on the place. Drag it, or focus it and use the arrow keys.</p>
		<div class="mp" tabindex="0" role="application" aria-roledescription="map" aria-label="Map" aria-describedby="mp-help">
			<div class="mp-tiles"></div><div class="mp-cross" aria-hidden="true"></div>
			<div class="mp-zoom"><button type="button" data-z="1" aria-label="Zoom in">+</button><button type="button" data-z="-1" aria-label="Zoom out">−</button></div>
			<div class="mp-credit">© <a href="https://www.openstreetmap.org/copyright" target="_blank" rel="noopener">OpenStreetMap</a> contributors</div>
		</div>
		<div class="actions" style="margin-top:12px"><button type="button" class="btn" data-mp="use">Use this spot</button><button type="button" class="link" data-mp="cancel">Cancel</button></div>`;
	const box = holder.querySelector( '.mp' );
	const layer = holder.querySelector( '.mp-tiles' );
	const tiles = new Map();

	function draw() {
		const w = box.clientWidth;
		const h = box.clientHeight;
		const left = c.x - w / 2;
		const top = c.y - h / 2;
		const max = 2 ** z;
		const want = new Set();
		for ( let tx = Math.floor( left / TILE ); tx <= Math.floor( ( left + w ) / TILE ); tx++ ) {
			for ( let ty = Math.max( 0, Math.floor( top / TILE ) ); ty <= Math.min( max - 1, Math.floor( ( top + h ) / TILE ) ); ty++ ) {
				const key = `${ z }/${ tx }/${ ty }`;
				want.add( key );
				let img = tiles.get( key );
				if ( ! img ) {
					img = document.createElement( 'img' );
					img.alt = '';
					img.src = TILE_URL( z, ( ( tx % max ) + max ) % max, ty );
					tiles.set( key, img );
					layer.appendChild( img );
				}
				img.style.transform = `translate(${ Math.round( tx * TILE - left ) }px, ${ Math.round( ty * TILE - top ) }px)`;
			}
		}
		for ( const [ key, img ] of tiles ) {
			if ( ! want.has( key ) ) {
				img.remove();
				tiles.delete( key );
			}
		}
	}

	function zoom( d ) {
		const nz = Math.max( MIN_Z, Math.min( MAX_Z, z + d ) );
		if ( nz === z ) {
			return;
		}
		const ll = toLatLon( c.x, c.y, z );
		z = nz;
		c = toPx( ll.lat, ll.lon, z );
		draw();
	}

	let drag = null;
	box.addEventListener( 'pointerdown', ( e ) => {
		if ( e.target.closest( 'button, a' ) ) {
			return;
		}
		drag = { x: e.clientX, y: e.clientY, cx: c.x, cy: c.y };
		box.setPointerCapture( e.pointerId );
		box.classList.add( 'dragging' );
	} );
	box.addEventListener( 'pointermove', ( e ) => {
		if ( drag ) {
			c = { x: drag.cx - ( e.clientX - drag.x ), y: drag.cy - ( e.clientY - drag.y ) };
			draw();
		}
	} );
	const end = () => {
		drag = null;
		box.classList.remove( 'dragging' );
	};
	box.addEventListener( 'pointerup', end );
	box.addEventListener( 'pointercancel', end );
	box.addEventListener( 'wheel', ( e ) => {
		e.preventDefault();
		zoom( e.deltaY < 0 ? 1 : -1 );
	}, { passive: false } );
	box.addEventListener( 'keydown', ( e ) => {
		const step = e.shiftKey ? 160 : 40;
		const moves = { ArrowLeft: [ -step, 0 ], ArrowRight: [ step, 0 ], ArrowUp: [ 0, -step ], ArrowDown: [ 0, step ] };
		if ( moves[ e.key ] ) {
			e.preventDefault();
			c = { x: c.x + moves[ e.key ][ 0 ], y: c.y + moves[ e.key ][ 1 ] };
			draw();
		} else if ( e.key === '+' || e.key === '=' ) {
			zoom( 1 );
		} else if ( e.key === '-' ) {
			zoom( -1 );
		}
	} );
	holder.addEventListener( 'click', ( e ) => {
		const b = e.target.closest( 'button' );
		if ( ! b ) {
			return;
		}
		if ( b.dataset.z ) {
			zoom( parseInt( b.dataset.z, 10 ) );
		} else if ( b.dataset.mp === 'use' ) {
			const ll = toLatLon( c.x, c.y, z );
			onPick( Math.round( ll.lat * 1e6 ) / 1e6, Math.round( ll.lon * 1e6 ) / 1e6 );
		} else if ( b.dataset.mp === 'cancel' ) {
			onCancel();
		}
	} );
	draw();
	box.focus();
}
