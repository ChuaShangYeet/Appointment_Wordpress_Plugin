=== JustSimple Booking ===
Contributors: justsimple
Tags: booking, appointments, reservations, salon, restaurant
Requires at least: 6.2
Tested up to: 7.0
Requires PHP: 7.4
Stable tag: 0.1.6
License: GPLv2 or later
License URI: https://www.gnu.org/licenses/gpl-2.0.html

Appointments, table reservations, workshop bays and on-site visits, booked straight from your own WordPress site.

== Description ==

Every booking is worked out on your own site, so no outside service and no account are needed. Make one booking page for each thing you take bookings for; each is one of five kinds:

* Appointments with one person, for spas, clinics and consultants: times show only where that person, or anyone, is free.
* Salons and barbers: add-ons make a booking longer, and starts that no longer fit drop out.
* Restaurants: a party is seated across tables that are free together; VIP tables can have a minimum spend the guest accepts before booking.
* Car workshops: a mechanic and a bay must both be free for every service, and a part on order is shown before booking.
* Home services and online visits: travel time is held either side of each visit, only inside the area you cover.

What you get:

* A booking form for any page, as a shortcode or a block. It shows the free times first and asks only what decides them. It takes your colour and works on phones and by keyboard.
* Customers book with a name and a mobile number, checked for the country they choose; no customer account. They get a booking code, and by email a link to change or cancel.
* In the admin: Today (the day's board), Bookings (search, confirm, change, cancel), Booking pages (setup for each kind, with a check of what is still missing) and System status.
* Two people booking the last free time at once: one gets it, the other is shown the times still free.

The plugin sends email only, through WordPress's own mail. It does not take payments.

== Installation ==

1. Upload the zip under Plugins > Add New Plugin > Upload Plugin, or upload the `justsimple-booking` folder to `/wp-content/plugins/`.
2. Activate the plugin.
3. Open Booking > System status and check that every line is green.
4. Open Booking > Booking pages, make a booking page and choose its kind. Fill in what its setup asks for (services, people, tables or bays, hours, booking rules) until the page says it is ready.
5. Put its shortcode, or the Booking block, on any page of your site.

== Frequently Asked Questions ==

= Does it work on WordPress.com's free plan? =

No. WordPress.com's free plan does not allow plugins. Any self-hosted WordPress works, including free hosting.

= Where are the bookings kept? =

In your own site's database, in tables the plugin makes. Nothing is sent to us or to anyone else.

= Why must the customer give a mobile number? =

So you can call or message them about their booking. The number is checked against Google's numbering plans for the country the customer chooses (Malaysia by default when the site's time zone is in Malaysia; you can change it for each booking page).

= Customers do not get the emails. =

The plugin sends through WordPress's own mail, which many hosts deliver poorly. An SMTP plugin usually fixes it.

= What happens to my bookings if I delete the plugin? =

They stay in your database unless you tick "Also delete every module, booking and customer" in Booking > Settings first.

== Changelog ==

= 0.1.6 =
* Free times and booking details are never kept by the browser. Some hosts tell browsers to keep every JSON answer for a year, which could show a returning visitor times that were booked since.

= 0.1.5 =
* The booking form opens faster: the page carries the booking page's details, so the form asks the server only for its free times (one round trip less), and it still checks the details in the background in case a page cache kept old ones.
* While it loads, the form shows its shape in grey (the days and the times) instead of a line of text.

= 0.1.4 =
* A refused landline number now says the business needs a mobile to reach the customer (it used to mention WhatsApp and SMS, which the plugin does not send).
* This readme describes what the plugin does today.

= 0.1.3 =
* Customers choose their country code in front of the phone number, and the number must be a mobile of that country of the right length (Malaysia: 9 or 10 digits after +60).
* Numbers are kept one way (+60123456789), so 012-345 6789 and +60 12-345 6789 are one customer; numbers already on file are rewritten on update and keep working.
* Each booking page chooses the country of its customers' numbers.

= 0.1.2 =
* The booking form can say whether a booking on a demo site is live or recorded.

= 0.1.1 =
* Every table is created as InnoDB, and tables a host made as MyISAM are converted, so the guard against double booking holds on every host.

= 0.1.0 =
* First release: the five kinds of booking page, the booking form (shortcode and block), Today, Bookings, Booking pages, emails, System status and clean uninstall.
