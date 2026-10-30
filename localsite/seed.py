"""Fills an empty local database with the five sample businesses (the same ones as the prototype's prototype/data.js) through the real API.

Everything goes through the API as an operator, an owner and customers would, so every rule applies. The only shortcut is for history:
bookings cannot be made in the past, so "past" bookings are made two weeks ahead and then moved back fourteen days in the database
(:func:`shift_back`), after which their outcome (completed, cancelled, no-show) is set through the admin API like a member of staff would.
"""
from __future__ import annotations

import datetime as dt
import random
from typing import Any
from zoneinfo import ZoneInfo

import asyncpg
import httpx

KL = ZoneInfo("Asia/Kuala_Lumpur")
SHIFT_DAYS = 14

SAMPLE_CUSTOMER = {"name": "Aisha Rahman", "phone": "+60123456789", "email": "aisha.rahman@gmail.com"}
NAMES = ["Tan Wei Ming", "Priya Nair", "Muhammad Faris", "Lim Mei Ling", "Siti Aminah", "Kumar Rajan", "Chloe Wong", "Nur Iman", "Daniel Lee",
         "Farah Hassan", "Arjun Pillai", "Ong Jia Hui", "Hakim Yusof", "Grace Teoh", "Zulkifli Omar", "Mei Xin Chua", "Ravi Shankar",
         "Liyana Azmi", "Jonathan Goh", "Aina Sofea"]


def every_day(open_: str, close: str, closed: tuple[int, ...] = ()) -> dict[int, tuple[str, str]]:
    return {d: (open_, close) for d in range(7) if d not in closed}


def _minutes(clock: str) -> int:
    h, m = clock.split(":")
    return int(h) * 60 + int(m)


def hm(minutes: int) -> str:
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


BUSINESSES: list[dict[str, Any]] = [
    {
        "id": "spa", "type": "spa", "name": "Serene Lotus Spa", "plan": "free", "domain": "serenelotus.my", "owner_email": "owner@serenelotus.my",
        "kind": "Spa & wellness", "who": "therapist",
        "profile": {"address": "23 Jalan Telawi 3, Bangsar Baru, 59100 Kuala Lumpur", "contact_no": "+60 3-2282 4410", "contact_email": "hello@serenelotus.my",
                    "description": "Massages, facials and reflexology in a quiet shophouse off Jalan Telawi."},
        "hours": every_day("10:00", "21:00"),
        "policy": {"default_status": "confirmed", "min_notice_booking": 2 * 3600, "min_notice_cancel": 24 * 3600, "booking_window": 60 * 86400},
        "staff": [("Mei Lin", "8 years", 780, (1,)), ("Aisyah", "5 years", 840, (3,)), ("Priya", "11 years", 780, (0,))],
        "services": [
            ("Balinese massage", 60, 15, 150, "Long strokes and acupressure to ease tight muscles.", [0, 1, 2], {}),
            ("Hot stone massage", 90, 15, 220, "Warm basalt stones on the back, legs and shoulders.", [0, 2], {}),
            ("Signature facial", 60, 15, 180, "Cleanse, exfoliation, mask and a hydrating finish.", [1], {}),
            ("Foot reflexology", 45, 15, 90, "Pressure-point work on the feet and lower legs.", [0, 1, 2], {}),
        ],
        "days_off": [(6, "Staff training day")],
        "past": 14, "future": 16,
    },
    {
        "id": "salon", "type": "salon", "name": "Studio Gunting", "plan": "free", "domain": "studiogunting.my", "owner_email": "owner@studiogunting.my",
        "kind": "Hair salon", "who": "stylist",
        "profile": {"address": "Lot 1-12, SS15 Courtyard, 47500 Subang Jaya, Selangor", "contact_no": "+60 3-5611 2090", "contact_email": "book@studiogunting.my",
                    "description": "Cuts, colour and treatments. Walk-ins welcome when a chair is free."},
        "hours": every_day("10:00", "20:00", closed=(2,)),
        "policy": {"default_status": "confirmed", "min_notice_booking": 3600, "min_notice_cancel": 12 * 3600, "booking_window": 45 * 86400},
        "staff": [("Jason", "Cuts", 780, ()), ("Nurul", "Colour specialist", 840, (4,)), ("Kenji", "Senior stylist", 780, (1,))],
        "services": [
            ("Women's cut & blow-dry", 60, 0, 85, "Wash, cut and a finished blow-dry.", [0, 1, 2], {}),
            ("Men's cut", 30, 0, 45, "Clipper or scissor cut, hot towel finish.", [0, 1, 2], {}),
            ("Full colour", 120, 15, 260, "One colour, roots to ends, with toner.", [1, 2], {}),
            ("Keratin smoothing", 150, 15, 380, "Smooths frizz for up to three months.", [2], {}),
            ("Scalp massage", 15, 0, 20, "Ten minutes of pressure-point massage at the basin.", [0, 1, 2], {"is_addon": True}),
            ("Deep conditioning", 30, 0, 60, "Repair mask under steam.", [0, 1, 2], {"is_addon": True}),
            ("Fringe trim", 15, 0, 15, "A tidy-up between cuts.", [0, 1, 2], {"is_addon": True}),
        ],
        "products": [("Argan oil serum", 69, 12, "in_stock", None), ("Colour-safe shampoo", 48, 20, "in_stock", None), ("Clay styling paste", 55, 0, "out_of_stock", None)],
        "past": 14, "future": 16,
    },
    {
        "id": "restaurant", "type": "restaurant", "name": "Rempah Kitchen", "plan": "paid", "domain": "rempahkitchen.my", "owner_email": "owner@rempahkitchen.my",
        "kind": "Malaysian restaurant", "who": "table",
        "profile": {"address": "8 Jalan Kia Peng, 50450 Kuala Lumpur", "contact_no": "+60 3-2141 7788", "contact_email": "tables@rempahkitchen.my",
                    "description": "Nyonya and Malay dishes, cooked to order. Dinner only."},
        "hours": every_day("18:00", "23:00", closed=(1,)),
        # every reservation is confirmed by hand: a booking starts as a request that holds its table for 30 minutes
        "policy": {"default_status": "pending", "min_notice_booking": 2 * 3600, "min_notice_cancel": 6 * 3600, "booking_window": 30 * 86400},
        "tables": [("Table 1", 2, {"location": "indoor"}), ("Table 2", 2, {"location": "outdoor"}), ("Table 3", 4, {"location": "indoor"}),
                   ("Table 4", 4, {"location": "indoor"}), ("Table 5", 4, {"location": "outdoor"}), ("Table 6", 6, {"location": "indoor"}),
                   ("Table 7", 8, {"location": "indoor"}),
                   ("Peranakan Room", 6, {"location": "indoor", "is_vip": True, "minimum_spend": 500}),
                   ("Courtyard Room", 10, {"location": "indoor", "is_vip": True, "minimum_spend": 1200})],
        "slots": {"booking_mode": "fixed", "slot_duration_minutes": 90, "slot_interval_minutes": 30, "first_slot_time": "18:00",
                  "last_slot_time": "21:30", "buffer_minutes": 15},
        "past": 40, "future": 45,
    },
    {
        "id": "automotive", "type": "automotive", "name": "Jaya Auto Workshop", "plan": "paid", "domain": "jayaauto.my", "owner_email": "owner@jayaauto.my",
        "kind": "Car service workshop", "who": "mechanic",
        "profile": {"address": "12 Jalan Industri USJ 1/6, 47600 Subang Jaya, Selangor", "contact_no": "+60 3-8024 5512", "contact_email": "service@jayaauto.my",
                    "description": "Servicing for Perodua, Proton, Honda and Toyota. Free car wash with every service."},
        "hours": every_day("08:30", "18:00", closed=(0,)),
        "policy": {"default_status": "confirmed", "min_notice_booking": 3 * 3600, "min_notice_cancel": 24 * 3600, "booking_window": 30 * 86400},
        "staff": [("Ah Seng", "Brakes & engine", 750, ()), ("Ravi", "Aircond", 780, ()), ("Faiz", "General service", 750, (6,))],
        "bays": ["Bay 1", "Bay 2", "Alignment bay"],
        "vehicles": [("Perodua", "Myvi", 2011, None), ("Perodua", "Axia", 2014, None), ("Proton", "Saga", 2016, None), ("Proton", "X50", 2020, None),
                     ("Honda", "City", 2014, 2024), ("Toyota", "Vios", 2013, None)],
        "products": [("Engine oil 5W-30 (4 L)", 120, 18, "in_stock", None), ("Oil filter", 25, 40, "in_stock", None),
                     ("Front brake pads (OEM)", 140, 0, "available_on_order", 5), ("Cabin air filter", 35, 6, "in_stock", None)],
        # (name, minutes, buffer, price, description, staff indexes, extra) - the bays are given by "bays" below, the parts by "parts"
        "services": [
            ("Oil & filter change", 45, 15, 180, "Fully synthetic oil, new oil filter, 20-point check.", [0, 1, 2], {"bays": [0, 1], "parts": [0, 1]}),
            ("Brake pad replacement", 90, 0, 320, "Front pads replaced, discs checked.", [0, 2], {"bays": [0, 1], "parts": [2]}),
            ("Aircond service", 60, 0, 150, "Gas top-up, cabin filter and vent cleaning.", [1], {"bays": [0, 1], "parts": [3]}),
            ("Wheel alignment", 30, 0, 60, "Computerised four-wheel alignment.", [0, 1, 2], {"bays": [2]}),
            ("Check-up for an unknown problem", 45, 0, 50, "A noise, a warning light, it pulls to one side? A mechanic finds the cause first. RM 50 to look; "
             "we tell you the repair price before doing any work.", [0, 1, 2], {"bays": [0, 1], "price_after_inspection": True}),
        ],
        "past": 26, "future": 30,
    },
    {
        "id": "professional", "type": "professional", "name": "Kemas Home Services", "plan": "free", "domain": "kemashome.my", "owner_email": "owner@kemashome.my",
        "kind": "Aircond, plumbing & wiring", "who": "technician",
        "profile": {"address": "27 Jalan SS 2/24, 47300 Petaling Jaya, Selangor", "contact_no": "+60 3-7865 0321", "contact_email": "jobs@kemashome.my",
                    "description": "Aircond servicing, plumbing and wiring repairs at your home, anywhere within 25 km of PJ."},
        "hours": every_day("09:00", "18:00", closed=(0,)),
        "location": {"latitude": 3.1186, "longitude": 101.6216, "service_radius_km": 25},
        "policy": {"default_status": "confirmed", "min_notice_booking": 4 * 3600, "min_notice_cancel": 24 * 3600, "booking_window": 30 * 86400},
        "staff": [("Hafiz", "Aircond", 780, ()), ("Daniel", "Plumbing", 780, (6,)), ("Siti", "Aircond, plumbing & wiring", 810, ())],
        "services": [
            ("Aircond check-up", 45, 0, 60, "We find out what is wrong and quote the repair.", [0, 2], {"location_type": "on_site", "service_category": "inspection", "travel_buffer_minutes": 30,
                                                                                                        "price_after_inspection": True}),
            ("Aircond chemical wash", 90, 0, 180, "Indoor unit taken apart and washed. Price per unit.", [0, 2], {"location_type": "on_site", "service_category": "standard", "travel_buffer_minutes": 30}),
            ("Leak inspection", 45, 0, 80, "Pipes, taps and water heater checked for leaks.", [1, 2], {"location_type": "on_site", "service_category": "inspection", "travel_buffer_minutes": 30}),
            ("Renovation consultation", 45, 0, 0, "Bring your floor plan to our PJ office. No charge.", [2], {"location_type": "in_office", "service_category": "standard"}),
            ("Wiring inspection", 60, 0, 0, "Tripping power, a burning smell, sockets that stopped working? Our electrician checks the wiring and tells you what "
             "needs fixing and what it costs.", [2], {"location_type": "on_site", "service_category": "inspection", "travel_buffer_minutes": 30, "price_after_inspection": True}),
        ],
        "past": 10, "future": 12,
    },
]

ADDRESSES = [
    ("15 Jalan SS 21/37, Damansara Utama", "Petaling Jaya", "47400", 3.1365, 101.6230),
    ("8 Jalan Bangsar Utama 1", "Kuala Lumpur", "59000", 3.1300, 101.6710),
    ("22 Jalan USJ 9/5Q", "Subang Jaya", "47620", 3.0438, 101.5806),
    ("3 Jalan Puchong Prima 4/2", "Puchong", "47150", 3.0280, 101.6170),
    ("41 Jalan 14/22", "Petaling Jaya", "46100", 3.1073, 101.6067),
]
NOTES = ["Lower back pain since August.", "Pregnant, 20 weeks. Please avoid pressure points on the ankles.", "Allergic to nut oils."]
REQUESTS = ["Birthday dinner, a candle on the dessert please.", "One guest is vegetarian.", "High chair for a toddler."]
PLATES = ["WXA 1123", "BKT 8821", "VCD 4410", "PJS 2290", "WTR 7781", "BMM 3305", "JQA 6612", "VBK 9034"]


class Seeder:
    def __init__(self, client: httpx.AsyncClient, platform_token: str, dsn: str, read_inbox: Any) -> None:
        """``dsn``: a direct database connection for the two history shortcuts. It need not be a superuser: each change is made as the business it belongs to
        (``app.tenant_id``), so row-level security lets it through."""
        self.c = client
        self.platform = platform_token
        self.dsn = dsn
        self.inbox = read_inbox
        self.rng = random.Random(20261016)

    async def call(self, method: str, url: str, *, ok: tuple[int, ...] = (200, 201, 202, 204), **kw: Any) -> Any:
        r = await self.c.request(method, url, **kw)
        if r.status_code not in ok:
            raise RuntimeError(f"{method} {url} -> {r.status_code} {r.text[:400]}")
        return r.json() if r.content else None

    # ------------------------------------------------------------------ set-up
    async def make_business(self, spec: dict[str, Any]) -> dict[str, Any]:
        made = await self.call("POST", "/v1/auth/tenants", headers={"X-Platform-Token": self.platform}, json={
            "name": spec["name"], "business_type": spec["type"], "country": "MY", "timezone": "Asia/Kuala_Lumpur", "domain": spec["domain"],
            "plan": "paid", "owner_email": spec["owner_email"],  # (put on its real plan at the end, so the sample history does not count against it)
        })
        tok = await self.call("POST", "/v1/auth/token", headers={"X-API-Key": made["secret_key"]},
                              json={"user_id": "owner-1", "role": "owner", "display_name": "Owner"})
        admin = {"Authorization": f"Bearer {tok['access_token']}"}
        site = {"X-Site-Key": made["publishable_key"]}
        b: dict[str, Any] = {"spec": spec, "tenant_id": made["tenant_id"], "pk": made["publishable_key"], "sk": made["secret_key"], "admin": admin, "site": site}

        await self.call("PUT", "/v1/admin/business-profile", headers=admin, json={"business_name": spec["name"], **spec["profile"]})
        pol = await self.call("POST", "/v1/admin/policies", headers=admin, json={"name": "Standard", "make_default": True, "pending_timeout": 1800, **spec["policy"]})
        b["policy_id"] = pol["id"]
        await self.call("PUT", "/v1/admin/operating-hours", headers=admin,
                        json=[{"weekday": d, "open_time": o, "close_time": c} for d, (o, c) in spec["hours"].items()])

        b["staff"] = []
        for name, note, break_at, off in spec.get("staff", []):
            r = await self.call("POST", "/v1/admin/resources", headers=admin, json={"type": "staff", "name": name, "attributes": {"note": note}})
            shifts = [{"weekday": d, "start_time": o, "end_time": c, "break_start": hm(break_at), "break_end": hm(break_at + 60)}
                      for d, (o, c) in spec["hours"].items() if d not in off]
            await self.call("PUT", f"/v1/admin/resources/{r['id']}/shifts", headers=admin, json=shifts)
            b["staff"].append(r["id"])
        b["bays"] = []
        for name in spec.get("bays", []):
            r = await self.call("POST", "/v1/admin/resources", headers=admin, json={"type": "bay", "name": name})
            b["bays"].append(r["id"])
        b["tables"] = []
        for name, seats, attrs in spec.get("tables", []):
            r = await self.call("POST", "/v1/admin/resources", headers=admin, json={"type": "table", "name": name, "capacity": seats, "attributes": attrs})
            b["tables"].append(r["id"])
        if "slots" in spec:
            await self.call("PUT", "/v1/admin/time-slot-settings", headers=admin, json=spec["slots"])
        if "location" in spec:
            await self.call("PUT", "/v1/admin/business-location", headers=admin, json=spec["location"])

        b["vehicles"] = []
        for make, model, y_from, y_to in spec.get("vehicles", []):
            r = await self.call("POST", "/v1/admin/supported-vehicles", headers=admin, json={"make": make, "model": model, "year_from": y_from, "year_to": y_to})
            b["vehicles"].append(r)
        b["products"] = []
        for name, price, stock, status, lead in spec.get("products", []):
            r = await self.call("POST", "/v1/admin/products", headers=admin, json={
                "name": name, "price": price, "stock_qty": stock, "stock_status": status, "estimated_lead_time_days": lead})
            b["products"].append(r["id"])

        b["services"] = []
        for name, minutes, buffer, price, desc, staff_ix, extra in spec.get("services", []):
            extra = dict(extra)
            bays = [b["bays"][i] for i in extra.pop("bays", [])]
            parts = [b["products"][i] for i in extra.pop("parts", [])]
            r = await self.call("POST", "/v1/admin/services", headers=admin, json={
                "name": name, "duration_minutes": minutes, "buffer_minutes": buffer, "price": price, "description": desc,
                "resource_ids": [b["staff"][i] for i in staff_ix] + bays, **extra})
            if parts:
                await self.call("PUT", f"/v1/admin/services/{r['id']}/parts", headers=admin, json={"product_ids": parts})
            b["services"].append(r)
        return b

    # ------------------------------------------------------------------ bookings
    def query(self, b: dict[str, Any]) -> dict[str, Any]:
        t = b["spec"]["type"]
        r = self.rng
        if t == "restaurant":
            return {"party_size": 1 + int(r.random() * 6), "table_type": "vip" if r.random() < 0.1 else "normal"}
        bases = [s for s in b["services"] if not s["is_addon"]]
        first = r.choice(bases)
        ids = [first["id"]]
        if t == "automotive" and r.random() < 0.3:
            second = r.choice(bases)
            if second["id"] != first["id"]:
                ids.append(second["id"])
        if t == "salon" and r.random() < 0.35:
            ids.append(r.choice([s for s in b["services"] if s["is_addon"]])["id"])
        return {"service_ids": ids}

    async def slots(self, b: dict[str, Any], day: dt.date, q: dict[str, Any]) -> list[dict[str, Any]]:
        if b["spec"]["type"] == "professional" and any(x["id"] in q.get("service_ids", []) and x["location_type"] == "on_site" for x in b["services"]):
            q.setdefault("_addr", self.rng.choice(ADDRESSES))  # (an on-site visit's free times depend on where it is)
            q["latitude"], q["longitude"] = q["_addr"][3], q["_addr"][4]
        params: list[tuple[str, Any]] = [("date", day.isoformat())]
        params += [("service_ids", s) for s in q.get("service_ids", [])]
        for k in ("party_size", "table_type", "resource_id", "latitude", "longitude"):
            if q.get(k) is not None:
                params.append((k, q[k]))
        out = await self.call("GET", "/v1/public/availability", headers=b["site"], params=params)
        return out["slots"]

    async def book(self, b: dict[str, Any], slot: dict[str, Any], q: dict[str, Any], customer: dict[str, Any], **extra: Any) -> dict[str, Any] | None:
        t = b["spec"]["type"]
        body: dict[str, Any] = {"start_at": slot["start_at"], "customer": customer, "source": self.rng.choice(["wordpress", "wordpress", "headless"]), **extra}
        body.update({k: v for k, v in q.items() if k in ("service_ids", "party_size", "table_type", "resource_id")})
        if slot.get("minimum_spend"):
            body["accepted_minimum_spend"] = slot["minimum_spend"]
        if t == "automotive":
            body.setdefault("vehicle", {"plate_no": self.rng.choice(PLATES), "supported_vehicle_id": self.rng.choice(b["vehicles"])["id"], "year": 2015 + self.rng.randrange(9)})
            body["acknowledge_lead_time"] = True
        if t == "professional" and "address" not in body and any(s["id"] in q.get("service_ids", []) and s["location_type"] == "on_site" for s in b["services"]):
            line1, city, pc, lat, lon = q.get("_addr") or self.rng.choice(ADDRESSES)
            body["address"] = {"line1": line1, "city": city, "postcode": pc, "latitude": lat, "longitude": lon}
        if t == "spa" and self.rng.random() < 0.25:
            body["health_notes"] = self.rng.choice(NOTES)
        if t == "restaurant" and self.rng.random() < 0.3:
            body["special_request"] = self.rng.choice(REQUESTS)
        r = await self.c.post("/v1/public/bookings", headers=b["site"], json=body)
        if r.status_code in (409, 422):  # (taken by an earlier pick of this run, or the address/vehicle rule) - just skip it
            return None
        if r.status_code != 201:
            raise RuntimeError(f"booking -> {r.status_code} {r.text[:300]}")
        made = r.json()
        if made["status"] == "pending":
            await self.call("PATCH", f"/v1/admin/bookings/{made['id']}/status", headers=b["admin"], json={"status": "confirmed"})
        return made

    def stranger(self) -> dict[str, Any]:
        return {"name": self.rng.choice(NAMES), "phone": "+601" + self.rng.choice("269") + f"{self.rng.randrange(10**7):07d}"}  # (012, 016, 019: every block is a mobile range)

    async def fill(self, b: dict[str, Any], days: range, count: int) -> list[str]:
        made: list[str] = []
        today = dt.datetime.now(KL).date()
        open_days = [today + dt.timedelta(days=d) for d in days if (today + dt.timedelta(days=d)).isoweekday() % 7 in b["spec"]["hours"]]
        for i in range(count * 3):
            if len(made) >= count or not open_days:
                break
            day = open_days[i % len(open_days)]
            q = self.query(b)
            free = await self.slots(b, day, q)
            if not free:
                continue
            got = await self.book(b, self.rng.choice(free), q, self.stranger())
            if got:
                made.append(got["id"])
        return made

    async def shift_back(self, tenant_id: str) -> None:
        """Move every booking of the business 14 days back (they were all made 1-14 days ahead), with their allocations and history."""
        conn = await asyncpg.connect(self.dsn)
        try:
            async with conn.transaction():
                await conn.execute("SELECT set_config('app.tenant_id', $1, true)", tenant_id)
                await conn.execute(
                    "UPDATE bookings SET start_at = start_at - make_interval(days => $2), end_at = end_at - make_interval(days => $2), "
                    "created_at = created_at - make_interval(days => $2 + 3) WHERE tenant_id = $1", tenant_id, SHIFT_DAYS)
                await conn.execute(
                    "UPDATE booking_allocations SET period = tstzrange(lower(period) - make_interval(days => $2), upper(period) - make_interval(days => $2), '[)') "
                    "WHERE tenant_id = $1", tenant_id, SHIFT_DAYS)
                await conn.execute(
                    "UPDATE booking_status_history SET changed_at = changed_at - make_interval(days => $2 + 3) WHERE tenant_id = $1", tenant_id, SHIFT_DAYS)
        finally:
            await conn.close()

    async def status(self, b: dict[str, Any], booking_id: str, to: str) -> None:
        await self.call("PATCH", f"/v1/admin/bookings/{booking_id}/status", headers=b["admin"], json={"status": to})

    async def sample_customer_bookings(self, b: dict[str, Any], window: range) -> list[dict[str, Any]]:
        """Bookings of the sample customer (Aisha) in the given days ahead, at most two."""
        t = b["spec"]["type"]
        q = {"party_size": 2, "table_type": "normal"} if t == "restaurant" else {"service_ids": [next(s for s in b["services"] if not s["is_addon"])["id"]]}
        extra: dict[str, Any] = {}
        if t == "automotive":
            extra["vehicle"] = {"plate_no": "WVX 2213", "supported_vehicle_id": b["vehicles"][0]["id"], "year": 2019}
        if t == "professional":
            q["_addr"] = ADDRESSES[0]
            line1, city, pc, lat, lon = ADDRESSES[0]
            extra["address"] = {"line1": line1, "city": city, "postcode": pc, "latitude": lat, "longitude": lon}
        today = dt.datetime.now(KL).date()
        out = []
        for d in window:
            day = today + dt.timedelta(days=d)
            free = [s for s in await self.slots(b, day, q) if dt.datetime.fromisoformat(s["start_at"]).astimezone(KL).hour >= 11]
            if free:
                got = await self.book(b, free[len(free) // 2], q, dict(SAMPLE_CUSTOMER), **extra)
                if got:
                    out.append(got)
            if len(out) >= 2:
                break
        return out

    async def running_now(self, b: dict[str, Any]) -> str | None:
        """For salon, workshop and home services: an appointment that started a little while ago, so 'add an extra' and 'waiting for parts' can be tried.
        Made 14 days ahead at the time it will be after the shift back (same weekday, so the same hours apply)."""
        t = b["spec"]["type"]
        if t not in ("salon", "automotive", "professional"):
            return None
        now = dt.datetime.now(KL)
        bases = [s for s in b["services"] if not s["is_addon"]]
        q = {"service_ids": [(bases if t != "professional" else [s for s in bases if s["location_type"] == "on_site"])[0]["id"]]}
        if t == "automotive":
            q = {"service_ids": [b["services"][1]["id"], b["services"][0]["id"]]}
        day = (now + dt.timedelta(days=SHIFT_DAYS)).date()
        for s in await self.slots(b, day, q):
            start = dt.datetime.fromisoformat(s["start_at"]) - dt.timedelta(days=SHIFT_DAYS)
            if start <= now - dt.timedelta(minutes=5) and dt.datetime.fromisoformat(s["end_at"]) - dt.timedelta(days=SHIFT_DAYS) > now + dt.timedelta(minutes=15):
                got = await self.book(b, s, q, {"name": "Chloe Wong", "phone": "+60162233445"})
                return got["id"] if got else None
        return None

    # ------------------------------------------------------------------ one business
    async def seed_business(self, spec: dict[str, Any]) -> dict[str, Any]:
        b = await self.make_business(spec)
        # history: made 1-14 days ahead, then moved back two weeks
        past = await self.fill(b, range(1, SHIFT_DAYS), spec["past"])
        aisha_past = await self.sample_customer_bookings(b, range(2, SHIFT_DAYS - 2))
        running = await self.running_now(b)
        await self.shift_back(b["tenant_id"])
        now = dt.datetime.now(dt.UTC)
        for i, bid in enumerate(past):
            x = self.rng.random()
            await self.status(b, bid, "completed" if x < 0.84 else "cancelled" if x < 0.92 else "no-show")
        for x in aisha_past:
            await self.status(b, x["id"], "completed")
        if running:
            await self.status(b, running, "in-progress")
        # what is coming up
        await self.fill(b, range(0, SHIFT_DAYS), spec["future"])
        aisha_next = await self.sample_customer_bookings(b, range(1, 10))
        if spec["policy"]["default_status"] == "pending":  # two fresh requests waiting for the restaurant to answer
            for _ in range(2):
                day = dt.datetime.now(KL).date() + dt.timedelta(days=2)
                free = await self.slots(b, day, {"party_size": 4, "table_type": "normal"})
                if free:
                    body = {"start_at": free[0]["start_at"], "party_size": 4, "table_type": "normal", "customer": self.stranger()}
                    await self.c.post("/v1/public/bookings", headers=b["site"], json=body)
        # a staff day off that now clashes with upcoming appointments: they are flagged for review
        if spec["type"] == "salon":
            day = dt.datetime.now(KL).date() + dt.timedelta(days=4)
            await self.call("POST", "/v1/admin/days-off", headers=b["admin"], json={
                "resource_id": b["staff"][2], "name": "Kenji on leave", "start_date": day.isoformat(), "end_date": day.isoformat()})
        for offset, name in spec.get("days_off", []):
            day = dt.datetime.now(KL).date() + dt.timedelta(days=offset)
            await self.call("POST", "/v1/admin/days-off", headers=b["admin"], json={"name": name, "start_date": day.isoformat(), "end_date": day.isoformat()})
        # the sample customer rated one past visit
        if aisha_past:
            await self.review(b, aisha_past[0]["id"])
        if spec["plan"] != "paid":
            await self.call("PUT", f"/v1/platform/tenants/{b['tenant_id']}/plan", headers={"X-Platform-Token": self.platform}, json={"plan": spec["plan"]})
        _ = now, aisha_next
        return {
            "id": spec["id"], "tenant_id": b["tenant_id"], "name": spec["name"], "type": spec["type"], "kind": spec["kind"], "who": spec["who"],
            "plan": spec["plan"], "domain": spec["domain"], "owner_email": spec["owner_email"],
            "publishable_key": b["pk"], "secret_key": b["sk"], "default_policy_id": b["policy_id"],
            # minutes after midnight per weekday (0 = Sunday), null = closed: the API has no way to read opening hours back
            "hours": [[_minutes(spec["hours"][d][0]), _minutes(spec["hours"][d][1])] if d in spec["hours"] else None for d in range(7)],
        }

    async def review(self, b: dict[str, Any], booking_id: str) -> None:
        """Signs in as the sample customer with a phone code (read from the local inbox) and rates the visit."""
        before = max([m["id"] for m in self.inbox()] or [0])
        await self.call("POST", "/v1/public/auth/otp", headers=b["site"], json={"phone": SAMPLE_CUSTOMER["phone"]})
        msg = next(m for m in self.inbox() if m["id"] > before)
        import re
        code = re.search(r"\b(\d{6})\b", msg["body"]).group(1)  # type: ignore[union-attr]
        tok = await self.call("POST", "/v1/public/auth/otp/verify", headers=b["site"], json={"phone": SAMPLE_CUSTOMER["phone"], "code": code})
        await self.call("POST", f"/v1/public/bookings/{booking_id}/review", headers={**b["site"], "Authorization": f"Bearer {tok['access_token']}"},
                        json={"rating": 5, "comment": "Lovely as always."})

    async def run(self) -> dict[str, Any]:
        out = [await self.seed_business(spec) for spec in BUSINESSES]
        # the sample history is not news: nothing of it is sent when the worker starts
        conn = await asyncpg.connect(self.dsn)
        try:
            for b in out:
                async with conn.transaction():
                    await conn.execute("SELECT set_config('app.tenant_id', $1, true)", b["tenant_id"])
                    await conn.execute("UPDATE outbox_events SET published_at = now() WHERE published_at IS NULL AND tenant_id = $1", b["tenant_id"])
        finally:
            await conn.close()
        return {"businesses": out, "sample_customer": SAMPLE_CUSTOMER}
