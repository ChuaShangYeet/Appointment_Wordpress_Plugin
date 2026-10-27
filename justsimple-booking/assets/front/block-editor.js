/**
 * The "Booking" block in the editor. No build step: plain wp.element calls. The editor shows which booking page the block
 * holds and lets the author pick it; the site renders the real widget on the server (render_callback in Front.php).
 */
( function ( wp ) {
	const el = wp.element.createElement;
	const { useState, useEffect } = wp.element;
	const { Placeholder, SelectControl, Spinner, PanelBody } = wp.components;
	const { InspectorControls, useBlockProps } = wp.blockEditor;
	const { __ } = wp.i18n;

	const TYPES = { staff: __( 'Appointments', 'justsimple-booking' ), salon: __( 'Salon', 'justsimple-booking' ), restaurant: __( 'Restaurant', 'justsimple-booking' ), workshop: __( 'Workshop', 'justsimple-booking' ), field: __( 'Field service', 'justsimple-booking' ) };

	function useModules() {
		const [ state, set ] = useState( { loading: true, modules: [], error: null } );
		useEffect( () => {
			wp.apiFetch( { path: '/justsimple/v1/admin/modules' } )
				.then( ( modules ) => set( { loading: false, modules, error: null } ) )
				.catch( ( e ) => set( { loading: false, modules: [], error: e.message || String( e ) } ) );
		}, [] );
		return state;
	}

	function Picker( { value, onChange, modules } ) {
		return el( SelectControl, {
			label: __( 'Booking page', 'justsimple-booking' ),
			value: String( value || 0 ),
			options: [ { value: '0', label: __( 'Choose…', 'justsimple-booking' ) } ].concat(
				modules.map( ( m ) => ( { value: String( m.id ), label: m.title + ' · ' + ( TYPES[ m.type ] || m.type ) + ( 'active' === m.status ? '' : ' · ' + __( 'off', 'justsimple-booking' ) ) } ) )
			),
			onChange: ( v ) => onChange( parseInt( v, 10 ) || 0 ),
		} );
	}

	wp.blocks.registerBlockType( 'justsimple/booking', {
		edit( { attributes, setAttributes } ) {
			const { loading, modules, error } = useModules();
			const chosen = modules.find( ( m ) => m.id === attributes.module );
			const pick = ( v ) => setAttributes( { module: v } );
			let body;
			if ( loading ) {
				body = el( Spinner );
			} else if ( error ) {
				body = el( 'p', null, error );
			} else if ( ! modules.length ) {
				body = el( 'p', null, __( 'Make a booking page first, under Booking › Modules.', 'justsimple-booking' ) );
			} else {
				body = el( Picker, { value: attributes.module, onChange: pick, modules } );
			}
			return el(
				'div',
				useBlockProps(),
				el( InspectorControls, null, el( PanelBody, { title: __( 'Booking page', 'justsimple-booking' ) }, modules.length ? el( Picker, { value: attributes.module, onChange: pick, modules } ) : null ) ),
				el(
					Placeholder,
					{
						icon: 'calendar-alt',
						label: chosen ? chosen.title : __( 'Booking', 'justsimple-booking' ),
						instructions: chosen
							? ( 'active' === chosen.status ? __( 'Visitors book here. The live form shows on the site.', 'justsimple-booking' ) : __( 'This booking page is switched off, so visitors will see nothing yet.', 'justsimple-booking' ) )
							: __( 'Choose which booking page to show.', 'justsimple-booking' ),
					},
					body
				)
			);
		},
		save: () => null, // rendered on the server
	} );
} )( window.wp );
