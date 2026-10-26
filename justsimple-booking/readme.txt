=== JustSimple Booking ===
Contributors: justsimple
Tags: booking, appointments, reservations, salon, restaurant
Requires at least: 6.2
Tested up to: 6.8
Requires PHP: 7.4
Stable tag: 0.1.0
License: GPLv2 or later
License URI: https://www.gnu.org/licenses/gpl-2.0.html

Appointments, table reservations, workshop bays and on-site visits, booked straight from your own WordPress site.

== Description ==

Every booking is worked out on your own site, so no outside service and no account are needed. Set up one or more booking modules:

* Staff appointments for spas, clinics and consultants
* Salon and barber appointments with add-ons
* Restaurant tables
* Car workshops, where both a mechanic and a bay must be free
* On-site and online visits within your service area

This is release 0.1.0. It installs the database and the status page; creating modules arrives in the next release.

== Installation ==

1. Upload the `justsimple-booking` folder to `/wp-content/plugins/`, or upload the zip under Plugins > Add New.
2. Activate the plugin.
3. Open Booking > System status and check that every line is green.

== Frequently Asked Questions ==

= Does it work on WordPress.com's free plan? =

No. WordPress.com's free plan does not allow plugins. Any self-hosted WordPress works, including free hosting.

= What happens to my bookings if I delete the plugin? =

They stay in your database unless you tick "Also delete every module, booking and customer" in Booking > Settings first.

== Changelog ==

= 0.1.0 =
* Database tables for all five booking types, a system status page and clean uninstall.
