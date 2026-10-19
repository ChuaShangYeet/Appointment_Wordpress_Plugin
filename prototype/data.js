/* Sample businesses for the prototype. The field names follow the backend's API schemas (backend/app/schemas) where the
   prototype shows the same thing, so a later version can swap the mock API for the real one. Times are minutes after
   midnight in the business's time zone (Asia/Kuala_Lumpur, UTC+8, no daylight saving). Weekdays: 0 = Sunday. */
window.JS = window.JS || {};
(function () {
  const all = (open, close) => [0, 1, 2, 3, 4, 5, 6].map(() => [open, close]);
  const without = (hours, ...days) => hours.map((h, i) => (days.includes(i) ? null : h));

  JS.BUSINESSES = [
    {
      id: 'spa', type: 'spa', kind: 'Spa & wellness', name: 'Serene Lotus Spa',
      address: '23 Jalan Telawi 3, Bangsar Baru, 59100 Kuala Lumpur', phone: '+60 3-2282 4410', email: 'hello@serenelotus.my',
      domain: 'serenelotus.my', plan: 'free', rating: [4.8, 126], description: 'Massages, facials and reflexology in a quiet shophouse off Jalan Telawi.',
      hours: all(600, 1260),
      policy: { default_status: 'confirmed', min_notice_min: 120, cancel_notice_h: 24, window_days: 60, pending_timeout_min: 30 },
      who: 'therapist', step: 30, collectsGender: true, healthNotes: true,
      services: [
        { id: 'spa-s1', name: 'Balinese massage', duration_minutes: 60, buffer_minutes: 15, price: 150, description: 'Long strokes and acupressure to ease tight muscles.', staff: ['spa-r1', 'spa-r2', 'spa-r3'] },
        { id: 'spa-s2', name: 'Hot stone massage', duration_minutes: 90, buffer_minutes: 15, price: 220, description: 'Warm basalt stones on the back, legs and shoulders.', staff: ['spa-r1', 'spa-r3'] },
        { id: 'spa-s3', name: 'Signature facial', duration_minutes: 60, buffer_minutes: 15, price: 180, description: 'Cleanse, exfoliation, mask and a hydrating finish.', staff: ['spa-r2'] },
        { id: 'spa-s4', name: 'Foot reflexology', duration_minutes: 45, buffer_minutes: 15, price: 90, description: 'Pressure-point work on the feet and lower legs.', staff: ['spa-r1', 'spa-r2', 'spa-r3'] },
      ],
      resources: [
        { id: 'spa-r1', type: 'staff', name: 'Mei Lin', note: '8 years', breakAt: 780, off: [1] },
        { id: 'spa-r2', type: 'staff', name: 'Aisyah', note: '5 years', breakAt: 840, off: [3] },
        { id: 'spa-r3', type: 'staff', name: 'Priya', note: '11 years', breakAt: 780, off: [0] },
      ],
      closed: [{ offset: 6, name: 'Staff training day' }],
    },
    {
      id: 'salon', type: 'salon', kind: 'Hair salon', name: 'Studio Gunting',
      address: 'Lot 1-12, SS15 Courtyard, 47500 Subang Jaya, Selangor', phone: '+60 3-5611 2090', email: 'book@studiogunting.my',
      domain: 'studiogunting.my', plan: 'free', rating: [4.6, 88], description: 'Cuts, colour and treatments. Walk-ins welcome when a chair is free.',
      hours: without(all(600, 1200), 2),
      policy: { default_status: 'confirmed', min_notice_min: 60, cancel_notice_h: 12, window_days: 45, pending_timeout_min: 30 },
      who: 'stylist', step: 30, collectsGender: true,
      services: [
        { id: 'sal-s1', name: "Women's cut & blow-dry", duration_minutes: 60, buffer_minutes: 0, price: 85, description: 'Wash, cut and a finished blow-dry.', staff: ['sal-r1', 'sal-r2', 'sal-r3'] },
        { id: 'sal-s2', name: "Men's cut", duration_minutes: 30, buffer_minutes: 0, price: 45, description: 'Clipper or scissor cut, hot towel finish.', staff: ['sal-r1', 'sal-r2', 'sal-r3'] },
        { id: 'sal-s3', name: 'Full colour', duration_minutes: 120, buffer_minutes: 15, price: 260, description: 'One colour, roots to ends, with toner.', staff: ['sal-r2', 'sal-r3'] },
        { id: 'sal-s4', name: 'Keratin smoothing', duration_minutes: 150, buffer_minutes: 15, price: 380, description: 'Smooths frizz for up to three months.', staff: ['sal-r3'] },
        { id: 'sal-a1', name: 'Scalp massage', duration_minutes: 15, buffer_minutes: 0, price: 20, is_addon: true, description: 'Ten minutes of pressure-point massage at the basin.' },
        { id: 'sal-a2', name: 'Deep conditioning', duration_minutes: 30, buffer_minutes: 0, price: 60, is_addon: true, description: 'Repair mask under steam.' },
        { id: 'sal-a3', name: 'Fringe trim', duration_minutes: 15, buffer_minutes: 0, price: 15, is_addon: true, description: 'A tidy-up between cuts.' },
      ],
      products: [
        { name: 'Argan oil serum', price: 69, stock_status: 'in_stock' },
        { name: 'Colour-safe shampoo', price: 48, stock_status: 'in_stock' },
        { name: 'Clay styling paste', price: 55, stock_status: 'out_of_stock' },
      ],
      resources: [
        { id: 'sal-r1', type: 'staff', name: 'Jason', note: 'Cuts', breakAt: 780, off: [] },
        { id: 'sal-r2', type: 'staff', name: 'Nurul', note: 'Colour specialist', breakAt: 840, off: [4] },
        { id: 'sal-r3', type: 'staff', name: 'Kenji', note: 'Senior stylist', breakAt: 780, off: [1] },
      ],
      closed: [],
    },
    {
      id: 'restaurant', type: 'restaurant', kind: 'Malaysian restaurant', name: 'Rempah Kitchen',
      address: '8 Jalan Kia Peng, 50450 Kuala Lumpur', phone: '+60 3-2141 7788', email: 'tables@rempahkitchen.my',
      domain: 'rempahkitchen.my', plan: 'paid', rating: [4.7, 342], description: 'Nyonya and Malay dishes, cooked to order. Dinner only.',
      hours: without(all(1080, 1380), 1),
      // every reservation is confirmed by hand: a booking starts as a pending request that holds its table for 30 minutes
      policy: { default_status: 'pending', min_notice_min: 120, cancel_notice_h: 6, window_days: 30, pending_timeout_min: 30 },
      who: 'table', step: 30,
      slotSettings: { booking_mode: 'fixed', first: 1080, last: 1290, interval: 30, duration: 90, buffer: 15 },
      services: [],
      resources: [
        { id: 'rst-t1', type: 'table', name: 'Table 1', capacity: 2, attributes: { location: 'indoor' } },
        { id: 'rst-t2', type: 'table', name: 'Table 2', capacity: 2, attributes: { location: 'outdoor' } },
        { id: 'rst-t3', type: 'table', name: 'Table 3', capacity: 4, attributes: { location: 'indoor' } },
        { id: 'rst-t4', type: 'table', name: 'Table 4', capacity: 4, attributes: { location: 'indoor' } },
        { id: 'rst-t5', type: 'table', name: 'Table 5', capacity: 4, attributes: { location: 'outdoor' } },
        { id: 'rst-t6', type: 'table', name: 'Table 6', capacity: 6, attributes: { location: 'indoor' } },
        { id: 'rst-t7', type: 'table', name: 'Table 7', capacity: 8, attributes: { location: 'indoor' } },
        { id: 'rst-v1', type: 'table', name: 'Peranakan Room', capacity: 6, attributes: { location: 'indoor', is_vip: true, minimum_spend: 500 } },
        { id: 'rst-v2', type: 'table', name: 'Courtyard Room', capacity: 10, attributes: { location: 'indoor', is_vip: true, minimum_spend: 1200 } },
      ],
      closed: [],
    },
    {
      id: 'automotive', type: 'automotive', kind: 'Car service workshop', name: 'Jaya Auto Workshop',
      address: '12 Jalan Industri USJ 1/6, 47600 Subang Jaya, Selangor', phone: '+60 3-8024 5512', email: 'service@jayaauto.my',
      domain: 'jayaauto.my', plan: 'paid', rating: [4.5, 211], description: 'Servicing for Perodua, Proton, Honda and Toyota. Free car wash with every service.',
      hours: without(all(510, 1080), 0),
      policy: { default_status: 'confirmed', min_notice_min: 180, cancel_notice_h: 24, window_days: 30, pending_timeout_min: 30 },
      who: 'mechanic', step: 30,
      supportedVehicles: [
        { id: 'sv1', make: 'Perodua', model: 'Myvi', year_from: 2011, year_to: null },
        { id: 'sv2', make: 'Perodua', model: 'Axia', year_from: 2014, year_to: null },
        { id: 'sv3', make: 'Proton', model: 'Saga', year_from: 2016, year_to: null },
        { id: 'sv4', make: 'Proton', model: 'X50', year_from: 2020, year_to: null },
        { id: 'sv5', make: 'Honda', model: 'City', year_from: 2014, year_to: 2024 },
        { id: 'sv6', make: 'Toyota', model: 'Vios', year_from: 2013, year_to: null },
      ],
      services: [
        { id: 'aut-s1', name: 'Oil & filter change', duration_minutes: 45, buffer_minutes: 15, price: 180, description: 'Fully synthetic oil, new oil filter, 20-point check.', staff: ['aut-m1', 'aut-m2', 'aut-m3'], bays: ['aut-b1', 'aut-b2'] },
        { id: 'aut-s2', name: 'Brake pad replacement', duration_minutes: 90, buffer_minutes: 0, price: 320, description: 'Front pads replaced, discs checked.', staff: ['aut-m1', 'aut-m3'], bays: ['aut-b1', 'aut-b2'], parts: [{ name: 'Front brake pads (OEM)', estimated_lead_time_days: 5 }] },
        { id: 'aut-s3', name: 'Aircond service', duration_minutes: 60, buffer_minutes: 0, price: 150, description: 'Gas top-up, cabin filter and vent cleaning.', staff: ['aut-m2'], bays: ['aut-b1', 'aut-b2'] },
        { id: 'aut-s4', name: 'Wheel alignment', duration_minutes: 30, buffer_minutes: 0, price: 60, description: 'Computerised four-wheel alignment.', staff: ['aut-m1', 'aut-m2', 'aut-m3'], bays: ['aut-b3'] },
      ],
      resources: [
        { id: 'aut-m1', type: 'staff', name: 'Ah Seng', note: 'Brakes & engine', breakAt: 750, off: [] },
        { id: 'aut-m2', type: 'staff', name: 'Ravi', note: 'Aircond', breakAt: 780, off: [] },
        { id: 'aut-m3', type: 'staff', name: 'Faiz', note: 'General service', breakAt: 750, off: [6] },
        { id: 'aut-b1', type: 'bay', name: 'Bay 1' },
        { id: 'aut-b2', type: 'bay', name: 'Bay 2' },
        { id: 'aut-b3', type: 'bay', name: 'Alignment bay' },
      ],
      closed: [],
    },
    {
      id: 'professional', type: 'professional', kind: 'Aircond & plumbing', name: 'Kemas Home Services',
      address: '27 Jalan SS 2/24, 47300 Petaling Jaya, Selangor', phone: '+60 3-7865 0321', email: 'jobs@kemashome.my',
      domain: 'kemashome.my', plan: 'free', rating: [4.7, 64], description: 'Aircond servicing and plumbing repairs at your home, anywhere within 25 km of PJ.',
      hours: without(all(540, 1080), 0),
      location: { radius_km: 25, base: 'SS 2, Petaling Jaya' },
      policy: { default_status: 'confirmed', min_notice_min: 240, cancel_notice_h: 24, window_days: 30, pending_timeout_min: 30 },
      who: 'technician', step: 30,
      services: [
        { id: 'pro-s1', name: 'Aircond check-up', duration_minutes: 45, buffer_minutes: 0, price: 60, location_type: 'on_site', service_category: 'inspection', travel_buffer_minutes: 30, description: 'We find out what is wrong and quote the repair.', staff: ['pro-p1', 'pro-p3'] },
        { id: 'pro-s2', name: 'Aircond chemical wash', duration_minutes: 90, buffer_minutes: 0, price: 180, location_type: 'on_site', service_category: 'standard', travel_buffer_minutes: 30, description: 'Indoor unit taken apart and washed. Price per unit.', staff: ['pro-p1', 'pro-p3'] },
        { id: 'pro-s3', name: 'Leak inspection', duration_minutes: 45, buffer_minutes: 0, price: 80, location_type: 'on_site', service_category: 'inspection', travel_buffer_minutes: 30, description: 'Pipes, taps and water heater checked for leaks.', staff: ['pro-p2', 'pro-p3'] },
        { id: 'pro-s4', name: 'Renovation consultation', duration_minutes: 45, buffer_minutes: 0, price: 0, location_type: 'in_office', service_category: 'standard', travel_buffer_minutes: 0, description: 'Bring your floor plan to our PJ office. No charge.', staff: ['pro-p3'] },
      ],
      resources: [
        { id: 'pro-p1', type: 'staff', name: 'Hafiz', note: 'Aircond', breakAt: 780, off: [] },
        { id: 'pro-p2', type: 'staff', name: 'Daniel', note: 'Plumbing', breakAt: 780, off: [6] },
        { id: 'pro-p3', type: 'staff', name: 'Siti', note: 'Aircond & plumbing', breakAt: 810, off: [] },
      ],
      closed: [],
    },
  ];

  /** The free plan's numbers (backend: plans table, migration 0023 and 0028). */
  JS.FREE_PLAN = { resources: 3, services: 10, bookings_per_month: 50, whatsapp_per_month: 20, sms_per_month: 20, sites: 1, domains: 1, live_keys_per_kind: 2, domain_change_days: 30 };

  /** The countries a business can sign up in (backend: app/core/countries.py). */
  JS.COUNTRIES = {
    MY: {
      name: 'Malaysia', calling: '60', currency: 'MYR', postcode: 5, postcodeExample: '50450',
      zones: [['Asia/Kuala_Lumpur', 'Malaysia (all states)']],
      states: [['JHR', 'Johor'], ['KDH', 'Kedah'], ['KTN', 'Kelantan'], ['MLK', 'Melaka'], ['NSN', 'Negeri Sembilan'], ['PHG', 'Pahang'], ['PNG', 'Pulau Pinang'], ['PRK', 'Perak'], ['PLS', 'Perlis'], ['SGR', 'Selangor'], ['TRG', 'Terengganu'], ['SBH', 'Sabah'], ['SWK', 'Sarawak'], ['KUL', 'Kuala Lumpur'], ['LBN', 'Labuan'], ['PJY', 'Putrajaya']],
    },
    SG: { name: 'Singapore', calling: '65', currency: 'SGD', postcode: 6, postcodeExample: '018956', zones: [['Asia/Singapore', 'Singapore']], states: [] },
    AU: {
      name: 'Australia', calling: '61', currency: 'AUD', postcode: 4, postcodeExample: '2000',
      zones: [['Australia/Sydney', 'New South Wales and ACT'], ['Australia/Melbourne', 'Victoria'], ['Australia/Brisbane', 'Queensland'], ['Australia/Adelaide', 'South Australia'], ['Australia/Perth', 'Western Australia'], ['Australia/Darwin', 'Northern Territory'], ['Australia/Hobart', 'Tasmania'], ['Australia/Broken_Hill', 'Broken Hill'], ['Australia/Eucla', 'Eucla'], ['Australia/Lord_Howe', 'Lord Howe Island']],
      states: [['NSW', 'New South Wales'], ['VIC', 'Victoria'], ['QLD', 'Queensland'], ['SA', 'South Australia'], ['WA', 'Western Australia'], ['TAS', 'Tasmania'], ['NT', 'Northern Territory'], ['ACT', 'Australian Capital Territory']],
    },
  };

  /** A few throw-away mail services (the backend's list has about a hundred: app/core/disposable_emails.py). */
  JS.DISPOSABLE = ['mailinator.com', 'yopmail.com', '10minutemail.com', 'guerrillamail.com', 'tempmail.com', 'temp-mail.org', 'trashmail.com', 'maildrop.cc', 'getnada.com'];

  /** The sample customer whose phone number unlocks the sample bookings in "My account". */
  JS.SAMPLE_CUSTOMER = { name: 'Aisha Rahman', phone: '+60123456789', email: 'aisha.rahman@gmail.com' };

  JS.FIRST_NAMES = ['Tan Wei Ming', 'Priya Nair', 'Muhammad Faris', 'Lim Mei Ling', 'Siti Aminah', 'Kumar Rajan', 'Chloe Wong', 'Nur Iman', 'Daniel Lee', 'Farah Hassan', 'Arjun Pillai', 'Ong Jia Hui', 'Hakim Yusof', 'Grace Teoh', 'Zulkifli Omar', 'Mei Xin Chua', 'Ravi Shankar', 'Liyana Azmi', 'Jonathan Goh', 'Aina Sofea'];
})();
