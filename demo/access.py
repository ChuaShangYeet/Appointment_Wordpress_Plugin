"""Who may use the demo website: visitors **register** once (work email, organisation, optional mobile) and **log in** with their email after that.
Both are confirmed by a link that is really emailed; the website opens only after following it. Every step is written to an access log, which the
owner downloads as CSV at ``/demo/visitors`` (with the platform admin token), next to the list of visitors.

Only the website is behind the sign-in: ``/`` (the pages) and ``/dev/*`` (what the pages call). The API (``/v1``, ``/docs``, ``/api``) stays open
for developers, as before.

* **Email.** Links are sent through the demo's own SMTP account (``DEMO_SMTP_*``), separate from the backend's: the booking system's messages stay
  in the demo inbox and are never sent. Without an SMTP account nobody can sign in, unless ``DEMO_ACCESS_SHOW_LINK=true`` (for trying the demo on
  one's own computer), which shows the link on the page instead of emailing it.
* **The link** works once, for 30 minutes, and is kept only as a hash. Following it sets a signed cookie (``demo_access``, 30 days, HttpOnly).
* **Log in** for an address nobody registered sends nothing and answers the same as for a registered one (so the page does not tell who uses the
  demo); the attempt is still logged. **Register** with an address already registered sends a log-in link instead.
* **Abuse.** At most 3 links per email address and 10 per client address in 10 minutes (in memory: the demo runs as one instance).

Log events: ``register_requested``, ``register_existing`` (already registered: a log-in link was sent), ``registration_confirmed``,
``login_requested``, ``login_unknown_email`` (nothing sent), ``logged_in``, ``link_invalid`` (expired or used), ``logged_out``.
"""
from __future__ import annotations

import asyncio
import base64
import csv
import hashlib
import hmac
import html
import io
import logging
import os
import secrets
import time
from collections import defaultdict, deque
from collections.abc import Awaitable, Callable
from typing import Any
from urllib.parse import parse_qs
from zoneinfo import ZoneInfo

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

log = logging.getLogger("demo")

COOKIE = "demo_access"
COOKIE_DAYS = 30
LINK_MINUTES = 30
KL = ZoneInfo("Asia/Kuala_Lumpur")  # (the owner reads the lists in Malaysia time; the database keeps UTC)
PHONE_COUNTRY = "MY"  # the country the mobile number on Register starts on


def _phone_countries() -> list[tuple[str, str, str]]:
    """``(country code, name, calling code)`` of every country, by name, for the country picked in front of the mobile number on Register
    (``app.core.countries``: the same list a business signs up from). A number typed with ``+`` keeps its own code."""
    from app.core.countries import IN_NAME_ORDER

    return [(c.code, c.name, c.calling_code) for c in IN_NAME_ORDER]
SCHEMA = [
    """CREATE TABLE IF NOT EXISTS demo_visitors (
        id bigserial PRIMARY KEY,
        email text NOT NULL UNIQUE,
        phone text,
        organisation text NOT NULL,
        created_at timestamptz NOT NULL DEFAULT now(),
        verified_at timestamptz,
        last_seen_at timestamptz,
        link_hash text,
        link_expires_at timestamptz,
        client_address text
    )""",
    "ALTER TABLE demo_visitors ADD COLUMN IF NOT EXISTS link_purpose text",
    "ALTER TABLE demo_visitors ADD COLUMN IF NOT EXISTS login_count integer NOT NULL DEFAULT 0",
    """CREATE TABLE IF NOT EXISTS demo_access_log (
        id bigserial PRIMARY KEY,
        at timestamptz NOT NULL DEFAULT now(),
        event text NOT NULL,
        email text,
        organisation text,
        phone text,
        client_address text
    )""",
]
EVENT_LABELS = {
    "register_requested": "Registered (link sent)", "register_existing": "Registered again (log-in link sent)",
    "registration_confirmed": "Registration confirmed", "login_requested": "Log in (link sent)", "login_unknown_email": "Log in, not registered (nothing sent)",
    "logged_in": "Logged in", "link_invalid": "Link expired or used", "logged_out": "Logged out",
}


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _client(request: Request) -> str:
    return request.client.host if request.client else "unknown"


class Access:
    def __init__(self, url: str, secret: str, public_url: str, admin_token: str, page: Callable[[str, str], HTMLResponse],
                 busy: Callable[[], bool] = lambda: False) -> None:
        self.url, self.secret, self.public_url, self.admin_token, self.page = url, secret.encode(), public_url.rstrip("/"), admin_token, page
        self.busy = busy  # (true while the sample data is being reset: reset.py)
        self.recent: dict[str, deque[float]] = defaultdict(deque)
        self.secure = self.public_url.startswith("https://")

    # ------------------------------------------------------------------ the database
    async def _conn(self) -> Any:
        import asyncpg

        return await asyncpg.connect(self.url)

    async def prepare(self) -> None:
        conn = await self._conn()
        try:
            for statement in SCHEMA:
                await conn.execute(statement)
        finally:
            await conn.close()

    async def _log(self, conn: Any, event: str, email: str | None, client: str, organisation: str | None = None, phone: str | None = None) -> None:
        await conn.execute("INSERT INTO demo_access_log (event, email, organisation, phone, client_address) VALUES ($1, $2, $3, $4, $5)",
                           event, email, organisation, phone, client)

    # ------------------------------------------------------------------ the cookie
    def _sign(self, text: str) -> str:
        return base64.urlsafe_b64encode(hmac.new(self.secret, b"demo-access:" + text.encode(), hashlib.sha256).digest()).decode().rstrip("=")

    def cookie_for(self, visitor_id: int) -> str:
        text = f"{visitor_id}.{int(time.time()) + COOKIE_DAYS * 86400}"
        return f"{text}.{self._sign(text)}"

    def visitor_of(self, request: Request) -> int | None:
        raw = request.cookies.get(COOKIE, "")
        parts = raw.split(".")
        if len(parts) != 3 or not parts[0].isdigit() or not parts[1].isdigit():
            return None
        text = f"{parts[0]}.{parts[1]}"
        if not hmac.compare_digest(parts[2], self._sign(text)) or int(parts[1]) < time.time():
            return None
        return int(parts[0])

    async def require(self, request: Request) -> None:
        """For ``/dev/*``: the pages work only for a signed-in visitor."""
        if self.visitor_of(request) is None:
            raise HTTPException(401, "Sign in to the demo first")
        if self.busy():
            raise HTTPException(503, "The demo's sample data is being reset; try again in a couple of minutes")

    def resetting_page(self) -> HTMLResponse:
        return self.page("Back in a moment", """<div class="brand"><span class="logo">J</span>JustSimple<span class="pill">Demo</span></div>
<div class="card" style="max-width:560px;display:flex;flex-direction:column;gap:10px"><h1 style="font-size:1.6rem">The demo is being reset</h1>
<p>The sample businesses and their bookings are being put back to fresh. This takes a minute or two.</p>
<p style="color:var(--muted)">Reload this page in a moment. You stay logged in.</p></div>""")

    # ------------------------------------------------------------------ limits
    def _allowed(self, key: str, most: int, window: float = 600) -> bool:
        now, seen = time.monotonic(), self.recent[key]
        while seen and seen[0] < now - window:
            seen.popleft()
        if len(seen) >= most:
            return False
        seen.append(now)
        return True

    # ------------------------------------------------------------------ email
    def _smtp(self) -> Any:
        host = os.environ.get("DEMO_SMTP_HOST", "")
        if not host:
            return None
        from app.services.delivery import SmtpEmailProvider

        return SmtpEmailProvider(
            host, int(os.environ.get("DEMO_SMTP_PORT", "587")), os.environ.get("DEMO_SMTP_FROM", "") or os.environ.get("DEMO_SMTP_USER", ""),
            user=os.environ.get("DEMO_SMTP_USER") or None, password=os.environ.get("DEMO_SMTP_PASSWORD") or None,
            starttls=os.environ.get("DEMO_SMTP_STARTTLS", "true").lower() != "false",
        )

    @staticmethod
    def _show_link() -> bool:
        return os.environ.get("DEMO_ACCESS_SHOW_LINK", "").lower() == "true"

    def _can_send(self) -> bool:
        return self._smtp() is not None or self._show_link()

    async def _send_link(self, email: str, token: str, purpose: str) -> str | None:
        """Email the link (or, with DEMO_ACCESS_SHOW_LINK, return it to show). ``None``: sent. Raises on a failed send."""
        link = f"{self.public_url}/demo/access/verify?token={token}"
        smtp = self._smtp()
        if smtp is None:
            return link
        from app.services.delivery import Message

        what = "confirm your registration for" if purpose == "register" else "log in to"
        body = (f"Hello,\n\nOpen this link to {what} the JustSimple demo:\n{link}\n\nIt works once, for {LINK_MINUTES} minutes. "
                "If you did not ask for it, ignore this email.\n\nJustSimple")
        subject = "Confirm your JustSimple demo registration" if purpose == "register" else "Your log-in link for the JustSimple demo"
        await smtp.send(Message(channel="email", recipient=email, subject=subject, body=body))
        return None

    # ------------------------------------------------------------------ pages
    def _shell(self, title: str, inner: str, note: str = "", tab: str = "login") -> HTMLResponse:
        tabs = (f'<nav class="tabs"><a href="/" {"aria-current=page" if tab == "login" else ""}>Log in</a>'
                f'<a href="/demo/access/register" {"aria-current=page" if tab == "register" else ""}>Register</a></nav>')
        body = f"""<div class="brand"><span class="logo">J</span>JustSimple<span class="pill">Demo</span></div>
<div class="hero"><img class="photo" src="/img/welcome.webp" alt="" width="1200" height="600">
<div style="display:flex;flex-direction:column;gap:20px;min-width:0">
<div style="display:flex;flex-direction:column;gap:10px"><h1>Try the booking system</h1>
<p class="lead">Online booking for salons, spas, restaurants, car workshops and home services: the booking page your customers use, their own account, and your admin. Register once; after that, log in with your email. We email you a link each time. The demo is shared, and its sample data goes back to fresh every night.</p></div>
{f'<div class="warn">{note}</div>' if note else ''}
<div class="card" style="display:flex;flex-direction:column;gap:14px">{tabs}{inner}</div></div></div>
<style>.hero{{display:grid;gap:24px;align-items:start}}.hero>div{{max-width:560px}}
.photo{{display:block;width:100%;height:auto;aspect-ratio:2/1;object-fit:cover;border-radius:8px;background:var(--raised)}}
@media (min-width:880px){{.hero{{grid-template-columns:minmax(0,560px) minmax(0,1fr);gap:40px}}.photo{{grid-column:2;grid-row:1;aspect-ratio:4/3;object-position:35% 50%;position:sticky;top:24px}}}}.f{{display:flex;flex-direction:column;gap:4px}}.f span{{font-weight:500}}.f i{{font-weight:400;color:var(--muted)}}.f input{{font:inherit;padding:9px 12px;border:1px solid var(--control);border-radius:6px;background:var(--surface);color:var(--fg)}}.f small{{color:var(--muted)}}
.f select{{font:inherit;font-size:16px;min-height:42px;padding:9px 8px;border:1px solid var(--control);border-radius:6px;background:var(--surface);color:var(--fg);flex:0 1 auto;max-width:45%;min-width:0}}
.f select:focus{{outline:none;border-color:var(--primary);box-shadow:0 0 0 3px rgb(18 22 20 / 10%)}}
.tel{{display:flex;gap:8px}}.tel input{{flex:1;min-width:0}}.err{{color:#8a1c20;font-size:.9rem;font-weight:500}}
.tabs{{display:flex;gap:4px;border-bottom:1px solid var(--line)}}.tabs a{{text-decoration:none;color:var(--muted);font-weight:500;padding:8px 12px 10px;margin-bottom:-1px;border-bottom:2px solid transparent}}.tabs a[aria-current]{{color:var(--fg);font-weight:600;border-bottom-color:var(--primary)}}</style>"""
        return self.page(title, body)

    @staticmethod
    def _field(v: dict[str, str], e: dict[str, str], name: str, label: str, kind: str, hint: str, required: bool, auto: str) -> str:
        err = f'<span class="err">{html.escape(e[name])}</span>' if name in e else ""
        return (f'<label class="f"><span>{label}{"" if required else " <i>(optional)</i>"}</span>'
                f'<input name="{name}" type="{kind}" value="{html.escape(v.get(name, ""))}" autocomplete="{auto}" {"required" if required else ""} maxlength="150">'
                f'<small>{hint}</small>{err}</label>')

    @staticmethod
    def _phone_field(v: dict[str, str], e: dict[str, str]) -> str:
        err = f'<span class="err">{html.escape(e["phone"])}</span>' if "phone" in e else ""
        chosen = v.get("phone_country") or PHONE_COUNTRY
        options = "".join(f'<option value="{code}" {"selected" if code == chosen else ""}>{html.escape(name)} (+{calling})</option>'
                          for code, name, calling in _phone_countries())
        return (f'<div class="f"><label for="phone"><span>Mobile number <i>(optional)</i></span></label>'
                f'<div class="tel"><select name="phone_country" aria-label="Country code">{options}</select>'
                f'<input id="phone" name="phone" type="tel" value="{html.escape(v.get("phone", ""))}" autocomplete="tel-national" maxlength="150"></div>'
                f'<small>So we can follow up, if you like.</small>{err}</div>')

    def login_form(self, values: dict[str, str] | None = None, errors: dict[str, str] | None = None, note: str = "") -> HTMLResponse:
        v, e = values or {}, errors or {}
        inner = f"""<form method="post" action="/demo/access/login" style="display:flex;flex-direction:column;gap:14px">
{self._field(v, e, "email", "Email", "email", "The address you registered with. We email you a log-in link: if it is not in your inbox, check your Spam or Junk folder.", True, "email")}
<button class="btn" type="submit" style="border:0;cursor:pointer;justify-content:center">Email me a log-in link</button></form>
<small style="color:var(--muted)">New here? <a href="/demo/access/register">Register</a> first: it takes a minute.</small>"""
        return self._shell("Log in to the demo", inner, note, "login")

    def register_form(self, values: dict[str, str] | None = None, errors: dict[str, str] | None = None, note: str = "") -> HTMLResponse:
        v, e = values or {}, errors or {}
        inner = f"""<form method="post" action="/demo/access/register" style="display:flex;flex-direction:column;gap:14px">
{self._field(v, e, "email", "Work email", "email", "We send a link here to confirm it is yours: if it is not in your inbox, check your Spam or Junk folder.", True, "email")}
{self._field(v, e, "organisation", "Organisation", "text", "Your business or company.", True, "organization")}
{self._phone_field(v, e)}
<button class="btn" type="submit" style="border:0;cursor:pointer;justify-content:center">Register</button></form>
<small style="color:var(--muted)">The demo is shared with other visitors and uses sample data: please don't enter real customer details in it. Already registered? <a href="/">Log in</a>.</small>"""
        return self._shell("Register for the demo", inner, note, "register")

    def _check_your_email(self, email: str, shown_link: str | None, *, maybe: bool) -> HTMLResponse:
        said = (f"If <b>{html.escape(email)}</b> is registered, we sent it a log-in link." if maybe
                else f"We sent a link to <b>{html.escape(email)}</b>.")
        shown = (f'<p><b>Email is not set up on this server</b> (DEMO_ACCESS_SHOW_LINK is on), so here is the link: <a href="{html.escape(shown_link)}">open it</a>.</p>'
                 if shown_link else "")
        return self.page("Check your email", f"""<div class="brand"><span class="logo">J</span>JustSimple<span class="pill">Demo</span></div>
<div class="card" style="max-width:560px;display:flex;flex-direction:column;gap:10px"><h1 style="font-size:1.6rem">Check your email</h1>
<p>{said} Open it on this device to go into the demo. It works once, for {LINK_MINUTES} minutes.</p>
{self._spam_reminder()}
<p style="color:var(--muted)">Still nothing after a few minutes?{" Check you registered with this address (<a href='/demo/access/register'>register</a>), or" if maybe else ""} <a href="/">ask for a new link</a>.</p>{shown}</div>""")

    @staticmethod
    def _spam_reminder() -> str:
        """The demo's emails often land in Spam (they come from a free email address through a sending service), so say so plainly."""
        sender = os.environ.get("DEMO_SMTP_FROM", "") or os.environ.get("DEMO_SMTP_USER", "")
        who = f" It comes from <b>{html.escape(sender)}</b>." if sender else ""
        return (f'<div class="warn"><b>Not in your inbox? Check your Spam or Junk folder</b> (in Gmail, also the Promotions tab).{who} '
                'If you find it there, mark it as <b>Not spam</b> so the next link arrives in your inbox.</div>')

    # ------------------------------------------------------------------ the two requests
    @staticmethod
    def _email(raw: str) -> tuple[str, str | None]:
        from email_validator import EmailNotValidError, validate_email

        try:
            return validate_email(raw, check_deliverability=False).normalized.lower(), None
        except EmailNotValidError:
            return "", "Enter a complete email address."

    async def register(self, request: Request) -> HTMLResponse:
        from app.core.disposable_emails import is_disposable
        from app.core.exceptions import ValidationFailed
        from app.core.phone import normalise_phone

        raw = parse_qs((await request.body()).decode("utf8", "replace"))
        from app.core.countries import COUNTRIES

        values = {k: (raw.get(k, [""])[0]).strip()[:150] for k in ("email", "organisation", "phone", "phone_country")}
        if values["phone_country"] not in COUNTRIES:
            values["phone_country"] = PHONE_COUNTRY
        errors: dict[str, str] = {}
        email, problem = self._email(values["email"])
        if problem:
            errors["email"] = problem
        elif is_disposable(email):
            errors["email"] = "Please use an email address you keep, not a temporary one."
        if len(values["organisation"]) < 2:
            errors["organisation"] = "Enter your organisation's name."
        phone = None
        if values["phone"]:
            try:
                phone = normalise_phone(values["phone"], COUNTRIES[values["phone_country"]].calling_code)
            except ValidationFailed:
                errors["phone"] = "That doesn't look like a phone number; leave it empty if you prefer."
        if errors:
            return self.register_form(values, errors)
        client = _client(request)
        if not self._allowed("email:" + email, 3) or not self._allowed("ip:" + client, 10):
            return self.register_form(values, note="Too many links were asked for just now. Wait a few minutes and try again, or use the link already sent.")
        if not self._can_send():
            return self.register_form(values, note="Sending email is not set up on this demo yet, so nobody can sign in. If you run it, see demo/README.md (DEMO_SMTP_*).")
        token = secrets.token_urlsafe(32)
        conn = await self._conn()
        try:
            existing = await conn.fetchrow("SELECT verified_at, organisation, phone FROM demo_visitors WHERE email = $1", email)
            if existing is not None and existing["verified_at"] is not None:  # already registered: a log-in link; the details on file stay
                purpose, event = "login", "register_existing"
                await conn.execute("UPDATE demo_visitors SET link_hash = $2, link_expires_at = now() + make_interval(mins => $3), link_purpose = 'login', "
                                   "client_address = $4 WHERE email = $1", email, _hash(token), LINK_MINUTES, client)
            else:
                purpose, event = "register", "register_requested"
                await conn.execute(
                    "INSERT INTO demo_visitors (email, phone, organisation, link_hash, link_expires_at, link_purpose, client_address) "
                    "VALUES ($1, $2, $3, $4, now() + make_interval(mins => $5), 'register', $6) "
                    "ON CONFLICT (email) DO UPDATE SET phone = EXCLUDED.phone, organisation = EXCLUDED.organisation, link_hash = EXCLUDED.link_hash, "
                    "link_expires_at = EXCLUDED.link_expires_at, link_purpose = 'register', client_address = EXCLUDED.client_address",
                    email, phone, values["organisation"], _hash(token), LINK_MINUTES, client,
                )
            on_file = existing if event == "register_existing" else {"organisation": values["organisation"], "phone": phone}
            await self._log(conn, event, email, client, on_file["organisation"], on_file["phone"])
        finally:
            await conn.close()
        try:
            shown = await self._send_link(email, token, purpose)
        except Exception:  # noqa: BLE001 - say so; the reason goes to the log
            log.exception("demo email to %s failed", email.split("@")[-1])
            return self.register_form(values, note="We couldn't send the email just now. Check the address and try again in a minute.")
        return self._check_your_email(email, shown, maybe=False)

    async def login(self, request: Request) -> HTMLResponse:
        raw = parse_qs((await request.body()).decode("utf8", "replace"))
        values = {"email": (raw.get("email", [""])[0]).strip()[:150]}
        email, problem = self._email(values["email"])
        if problem:
            return self.login_form(values, {"email": problem})
        client = _client(request)
        if not self._allowed("email:" + email, 3) or not self._allowed("ip:" + client, 10):
            return self.login_form(values, note="Too many links were asked for just now. Wait a few minutes and try again, or use the link already sent.")
        if not self._can_send():
            return self.login_form(values, note="Sending email is not set up on this demo yet, so nobody can sign in. If you run it, see demo/README.md (DEMO_SMTP_*).")
        token = secrets.token_urlsafe(32)
        conn = await self._conn()
        try:
            row = await conn.fetchrow("SELECT organisation, phone, verified_at FROM demo_visitors WHERE email = $1", email)
            if row is None:
                await self._log(conn, "login_unknown_email", email, client)
                purpose = None
            else:
                purpose = "login" if row["verified_at"] is not None else "register"  # (never confirmed: this link confirms the registration too)
                await conn.execute("UPDATE demo_visitors SET link_hash = $2, link_expires_at = now() + make_interval(mins => $3), link_purpose = $4, "
                                   "client_address = $5 WHERE email = $1", email, _hash(token), LINK_MINUTES, purpose, client)
                await self._log(conn, "login_requested", email, client, row["organisation"], row["phone"])
        finally:
            await conn.close()
        shown = None
        if purpose is not None:
            try:
                shown = await self._send_link(email, token, purpose)
            except Exception:  # noqa: BLE001
                log.exception("demo email to %s failed", email.split("@")[-1])
                return self.login_form(values, note="We couldn't send the email just now. Try again in a minute.")
        return self._check_your_email(email, shown, maybe=True)

    async def verify(self, token: str, request: Request) -> Response:
        client = _client(request)
        conn = await self._conn()
        try:
            row = await conn.fetchrow(
                "UPDATE demo_visitors SET verified_at = COALESCE(verified_at, now()), last_seen_at = now(), login_count = login_count + 1, "
                "link_hash = NULL, link_expires_at = NULL WHERE link_hash = $1 AND link_expires_at > now() "
                "RETURNING id, email, organisation, phone, link_purpose", _hash(token))
            if row is None:
                await self._log(conn, "link_invalid", None, client)
            else:
                await self._log(conn, "registration_confirmed" if row["link_purpose"] == "register" else "logged_in", row["email"], client, row["organisation"], row["phone"])
        finally:
            await conn.close()
        if row is None:
            return self.login_form(note="That link has expired or was already used. Ask for a new one below.")
        response = RedirectResponse("/", status_code=303)
        response.set_cookie(COOKIE, self.cookie_for(row["id"]), max_age=COOKIE_DAYS * 86400, httponly=True, samesite="lax", secure=self.secure)
        return response

    async def sign_out(self, request: Request) -> Response:
        visitor = self.visitor_of(request)
        if visitor is not None:
            conn = await self._conn()
            try:
                row = await conn.fetchrow("SELECT email, organisation, phone FROM demo_visitors WHERE id = $1", visitor)
                if row is not None:
                    await self._log(conn, "logged_out", row["email"], _client(request), row["organisation"], row["phone"])
            finally:
                await conn.close()
        response = RedirectResponse("/", status_code=303)
        response.delete_cookie(COOKIE)
        return response

    # ------------------------------------------------------------------ the owner's lists
    def _token_ok(self, given: str) -> bool:
        return bool(given) and hmac.compare_digest(given.encode(), self.admin_token.encode())

    async def visitors(self) -> list[dict[str, Any]]:
        conn = await self._conn()
        try:
            rows = await conn.fetch("SELECT email, phone, organisation, created_at, verified_at, last_seen_at, login_count FROM demo_visitors ORDER BY created_at DESC")
        finally:
            await conn.close()
        return [dict(r) for r in rows]

    async def access_log(self, limit: int | None = None) -> list[dict[str, Any]]:
        conn = await self._conn()
        try:
            rows = await conn.fetch("SELECT at, event, email, organisation, phone, client_address FROM demo_access_log ORDER BY at DESC, id DESC"
                                    + (f" LIMIT {int(limit)}" if limit else ""))
        finally:
            await conn.close()
        return [dict(r) for r in rows]


def _kl(d: Any) -> str:
    """A time for the CSV files, in Malaysia time: 2026-10-21 14:05:09 (a form spreadsheets read as a date and time)."""
    return d.astimezone(KL).strftime("%Y-%m-%d %H:%M:%S") if d else ""


def router(access: Access, site: Callable[[Request], Awaitable[Response]], resetter: Any = None) -> APIRouter:
    """``site`` answers ``/`` for a signed-in visitor (the website itself); ``resetter`` (reset.py) is pressed from the visitors page."""
    r = APIRouter(tags=["demo sign-in"], include_in_schema=False)

    @r.get("/")
    async def home(request: Request) -> Response:
        if access.visitor_of(request) is None:
            return access.login_form()
        if access.busy():
            return access.resetting_page()
        return await site(request)

    @r.get("/demo/access/register")
    async def register_page() -> Response:
        return access.register_form()

    @r.post("/demo/access/register")
    async def register(request: Request) -> Response:
        return await access.register(request)

    @r.post("/demo/access/login")
    async def login(request: Request) -> Response:
        return await access.login(request)

    @r.get("/demo/access/verify")
    async def verify(request: Request, token: str = "") -> Response:
        return await access.verify(token, request)

    @r.post("/demo/access/sign-out")
    async def sign_out(request: Request) -> Response:
        return await access.sign_out(request)

    def token_form(note: str = "") -> HTMLResponse:
        return access.page("Demo visitors", f"""<h1>Demo visitors</h1>{f'<div class="warn">{note}</div>' if note else ''}
<form method="post" action="/demo/visitors" class="card" style="max-width:480px;display:flex;flex-direction:column;gap:10px">
<label style="display:flex;flex-direction:column;gap:4px"><b>Platform admin token</b><input name="token" type="password" autocomplete="off" required style="font:inherit;padding:9px 12px;border:1px solid var(--control);border-radius:6px"></label>
<small style="color:var(--muted)">PLATFORM_ADMIN_TOKEN in the service's Environment settings on Render.</small>
<button class="btn" type="submit" style="border:0;cursor:pointer;justify-content:center">Show the lists</button></form>""")

    @r.get("/demo/visitors")
    async def visitors_form() -> Response:
        return token_form()

    def csv_response(name: str, header: list[str], rows: list[list[Any]]) -> Response:
        out = io.StringIO()
        w = csv.writer(out)
        w.writerow(header)
        w.writerows(rows)
        return Response("﻿" + out.getvalue(), media_type="text/csv; charset=utf-8",  # (the BOM makes Excel read it as UTF-8)
                        headers={"Content-Disposition": f'attachment; filename="{name}"', "Cache-Control": "no-store"})

    @r.post("/demo/visitors")
    async def visitors_list(request: Request) -> Response:
        raw = parse_qs((await request.body()).decode("utf8", "replace"))
        token = raw.get("token", [""])[0]
        if not access._token_ok(token):
            await asyncio.sleep(1)  # (slows guessing)
            return token_form("That token is not right.")
        what = raw.get("format", [""])[0]
        if what == "reset" and resetter is not None:
            started = not resetter.running
            if started:
                asyncio.get_running_loop().create_task(resetter.run("reset by the owner"))
            note = ("The reset has started: the sample data is fresh again in a minute or two. The visitors and the log are kept." if started
                    else "A reset is already running.")
            return access.page("Demo reset", f"""<h1>Demo reset</h1><div class="warn">{note}</div>
<form method="post" action="/demo/visitors"><input type="hidden" name="token" value="{html.escape(token)}"><button class="btn" type="submit" style="border:0;cursor:pointer">Back to the visitors</button></form>""")
        if what == "log.csv":
            return csv_response("demo-access-log.csv", ["time_malaysia", "event", "what_happened", "email", "organisation", "mobile", "client_address"],
                                [[_kl(x["at"]), x["event"], EVENT_LABELS.get(x["event"], x["event"]), x["email"] or "", x["organisation"] or "", x["phone"] or "",
                                  x["client_address"] or ""] for x in await access.access_log()])
        visitors = await access.visitors()
        if what == "visitors.csv":
            return csv_response("demo-visitors.csv", ["email", "mobile", "organisation", "registered_malaysia", "confirmed_malaysia", "last_login_malaysia", "logins"],
                                [[v["email"], v["phone"] or "", v["organisation"], _kl(v["created_at"]), _kl(v["verified_at"]), _kl(v["last_seen_at"]), v["login_count"]]
                                 for v in visitors])
        fmt = lambda d: d.astimezone(KL).strftime("%d %b %Y, %H:%M") if d else "—"  # noqa: E731
        vrows = "".join(f"<tr><td>{html.escape(v['email'])}</td><td>{html.escape(v['phone'] or '')}</td><td>{html.escape(v['organisation'])}</td><td>{fmt(v['created_at'])}</td>"
                        f"<td>{'Yes, ' + fmt(v['verified_at']) if v['verified_at'] else 'Not yet'}</td><td>{fmt(v['last_seen_at'])}</td><td>{v['login_count']}</td></tr>" for v in visitors)
        lrows = "".join(f"<tr><td>{fmt(x['at'])}</td><td>{html.escape(EVENT_LABELS.get(x['event'], x['event']))}</td><td>{html.escape(x['email'] or '—')}</td>"
                        f"<td>{html.escape(x['organisation'] or '')}</td></tr>" for x in await access.access_log(100))
        button = lambda fmt_, label: (f'<form method="post" action="/demo/visitors" style="display:inline"><input type="hidden" name="token" value="{html.escape(token)}">'  # noqa: E731
                                      f'<input type="hidden" name="format" value="{fmt_}"><button class="btn" type="submit" style="border:0;cursor:pointer">{label}</button></form>')
        table = 'style="border-collapse:collapse;width:100%;font-size:.92rem" cellpadding="8"'
        reset_box = ""
        if resetter is not None:
            from reset import reset_hour

            last, hour = await resetter.last_reset(), reset_hour()
            when = last.astimezone(KL).strftime("%d %b %Y, %H:%M") + " Malaysia time" if last else "never"
            plan = f"Every night at {hour:02d}:00 Malaysia time" if hour is not None else "The nightly reset is switched off (DEMO_RESET_HOUR)"
            reset_box = f"""<section><h2>Sample data</h2><div class="card" style="display:flex;flex-direction:column;gap:10px">
<p>{plan}, the five sample businesses and their bookings go back to fresh; businesses visitors signed up and everything they changed are removed. The visitors and the log below are kept.</p>
<p style="color:var(--muted)">Last reset: {html.escape(when)}{" (running now)" if resetter.running else ""}.</p>
<div>{button("reset", "Reset the demo data now")}</div></div></section>"""
        return access.page("Demo visitors", f"""<h1>Demo visitors ({len(visitors)})</h1>
<div class="links">{button("log.csv", "Download the register and log-in log (CSV)")}{button("visitors.csv", "Download the visitors (CSV)")}</div>
<p style="color:var(--muted)">All times are Malaysia time (UTC+8), here and in the CSV files.</p>
{reset_box}
<section><h2>Visitors</h2><div class="card" style="overflow-x:auto"><table {table}><thead><tr style="text-align:left"><th>Email</th><th>Mobile</th><th>Organisation</th><th>Registered</th><th>Confirmed</th><th>Last log-in</th><th>Log-ins</th></tr></thead>
<tbody>{vrows or '<tr><td colspan="7">Nobody yet.</td></tr>'}</tbody></table></div></section>
<section><h2>Register and log-in log (latest 100; the CSV has all)</h2><div class="card" style="overflow-x:auto"><table {table}><thead><tr style="text-align:left"><th>Time</th><th>What happened</th><th>Email</th><th>Organisation</th></tr></thead>
<tbody>{lrows or '<tr><td colspan="4">Nothing yet.</td></tr>'}</tbody></table></div></section>""")

    return r
