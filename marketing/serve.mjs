// Serves marketing/dist on http://localhost:4173 for looking at the built page (php marketing/build.php first). Read-only, no caching.
import { createServer } from 'node:http';
import { createReadStream, statSync } from 'node:fs';
import { extname, join, resolve, sep } from 'node:path';
import { fileURLToPath } from 'node:url';

const root = resolve( fileURLToPath( new URL( './dist', import.meta.url ) ) );
const port = Number( process.env.PORT || 4173 );
const TYPES = { '.html': 'text/html; charset=utf-8', '.css': 'text/css; charset=utf-8', '.js': 'text/javascript; charset=utf-8', '.json': 'application/json', '.webp': 'image/webp', '.woff2': 'font/woff2', '.zip': 'application/zip', '.txt': 'text/plain; charset=utf-8' };

createServer( ( req, res ) => {
	try {
		let file = resolve( join( root, decodeURIComponent( new URL( req.url, 'http://x' ).pathname ) ) );
		if ( file !== root && ! file.startsWith( root + sep ) ) {
			res.writeHead( 403 );
			return res.end();
		}
		if ( statSync( file ).isDirectory() ) {
			file = join( file, 'index.html' );
		}
		res.writeHead( 200, { 'content-type': TYPES[ extname( file ) ] || 'application/octet-stream', 'cache-control': 'no-store' } );
		createReadStream( file ).pipe( res );
	} catch {
		res.writeHead( 404 );
		res.end( 'not found' );
	}
} ).listen( port, '127.0.0.1', () => console.log( `marketing page on http://localhost:${ port }` ) );
