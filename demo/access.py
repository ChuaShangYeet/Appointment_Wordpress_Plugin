"""Who may use the demo website: a visitor signs in with their email (confirmed by a link that is really emailed), an optional mobile number and
their organisation, before the website opens. The owner reads the list at ``/demo/visitors`` with the platform admin token.

Only the website is behind the sign-in: ``/`` (the pages) and ``/dev/*`` (what the pages call). The API (``/v1``, ``/docs``, ``/api``) stays open
for developers, as before.

* **Email.** The confirmation link is sent through the demo's own SMTP account (``DEMO_SMTP_*``), separate from the backend's: the booking system's
  messages stay in the demo inbox and are never sent. Without an SMTP account the sign-in answers that email is not set up, unless
  ``DEMO_ACCESS_SHOW_LINK=true`` (for trying the demo on one's own computer), which shows the link on the page instead of emailing it.
* **The link** works once, for 30 minutes, and is kept only as a hash. Following it sets a signed cookie (``demo_access``, 30 days, HttpOnly).
* **Abuse.** At most 3 links per email address and 10 per client address in 10 minutes (in memory: the demo runs as one instance).
"""
from __future__ import annotations

import base64
import csv
import hashlib
import hmac
import html
import io
import os
import secrets
import time
from collections import defaultdict, deque
from collections.abc import Awaitable, Callable
from typing import Any
from urllib.parse import parse_qs

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

COOKIE = "demo_access"
COOKIE_DAYS = 30
LINK_MINUTES = 30
TABLE = """CREATE TABLE IF NOT EXISTS demo_visitors (
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
)"""


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


class Access:
    def __init__(self, url: str, secret: str, public_url: str, admin_token: str, page: Callable[[str, str], HTMLResponse]) -> None:
        self.url, self.secret, self.public_url, self.admin_token, self.page = url, secret.encode(), public_url.rstrip("/"), admin_token, page
        self.recent: dict[str, deque[float]] = defaultdict(deque)
        self.secure = self.public_url.startswith("https://")

    # ------------------------------------------------------------------ the database
    async def _conn(self) -> Any:
        import asyncpg

        return await asyncpg.connect(self.url)

    async def prepare(self) -> None:
        conn = await self._conn()
        try:
            await conn.execute(TABLE)
        finally:
            await conn.close()

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

    # ------------------------------------------------------------------ pages
    def form(self, values: dict[str, str] | None = None, errors: dict[str, str] | None = None, note: str = "") -> HTMLResponse:
        v, e = values or {}, errors or {}

        def field(name: str, label: str, kind: str, hint: str, required: bool, auto: str) -> str:
            err = f'<span class="err">{html.escape(e[name])}</span>' if name in e else ""
            return (f'<label class="f"><span>{label}{"" if required else " <i>(optional)</i>"}</span>'
                    f'<input name="{name}" type="{kind}" value="{html.escape(v.get(name, ""))}" autocomplete="{auto}" {"required" if required else ""} maxlength="150">'
                    f'<small>{hint}</small>{err}</label>')

        body = f"""<div class="brand"><span class="logo">J</span>JustSimple<span class="pill">Demo</span></div>
<div style="display:flex;flex-direction:column;gap:10px;max-width:560px"><h1>Try the booking system</h1>
<p class="lead">Online booking for salons, spas, restaurants, car workshops and home services: the booking page your customers use, their own account, and your admin. Sign in to open the demo; we email you a link.</p></div>
{f'<div class="warn">{note}</div>' if note else ''}
<form method="post" action="/demo/access/request" class="card" style="max-width:560px;display:flex;flex-direction:column;gap:14px">
{field("email", "Work email", "email", "We send the sign-in link here.", True, "email")}
{field("organisation", "Organisation", "text", "Your business or company.", True, "organization")}
{field("phone", "Mobile number", "tel", "So we can follow up, if you like.", False, "tel")}
<button class="btn" type="submit" style="border:0;cursor:pointer;justify-content:center">Email me a sign-in link</button>
<small style="color:var(--muted)">The demo is shared with other visitors and uses sample data: please don't enter real customer details in it.</small></form>
<style>.f{{display:flex;flex-direction:column;gap:4px}}.f span{{font-weight:600}}.f i{{font-weight:400;color:var(--muted)}}.f input{{font:inherit;padding:10px 12px;border:1px solid var(--line);border-radius:10px;background:var(--surface);color:var(--fg)}}.f small{{color:var(--muted)}}.err{{color:#c0362c;font-size:.9rem}}</style>"""
        return self.page("Sign in to the demo", body)

    async def request_link(self, request: Request) -> HTMLResponse:
        from email_validator import EmailNotValidError, validate_email

        from app.core.disposable_emails import is_disposable
        from app.core.exceptions import ValidationFailed
        from app.core.phone import normalise_phone

        raw = parse_qs((await request.body()).decode("utf8", "replace"))
        values = {k: (raw.get(k, [""])[0]).strip()[:150] for k in ("email", "organisation", "phone")}
        errors: dict[str, str] = {}
        try:
            email = validate_email(values["email"], check_deliverability=False).normalized.lower()
            if is_disposable(email):
                errors["email"] = "Please use an email address you keep, not a temporary one."
        except EmailNotValidError:
            errors["email"] = "Enter a complete email address."
            email = ""
        if len(values["organisation"]) < 2:
            errors["organisation"] = "Enter your organisation's name."
        phone = None
        if values["phone"]:
            try:
                phone = normalise_phone(values["phone"], "60")
            except ValidationFailed:
                errors["phone"] = "That doesn't look like a phone number; leave it empty if you prefer."
        if errors:
            return self.form(values, errors)
        client = request.client.host if request.client else "unknown"
        if not self._allowed("email:" + email, 3) or not self._allowed("ip:" + client, 10):
            return self.form(values, note="Too many sign-in links were asked for just now. Wait a few minutes and try again, or use the link already sent.")
        smtp = self._smtp()
        if smtp is None and not self._show_link():
            return self.form(values, note="Sending email is not set up on this demo yet, so nobody can sign in. If you run it, see demo/README.md (DEMO_SMTP_*).")
        token = secrets.token_urlsafe(32)
        conn = await self._conn()
        try:
            await conn.execute(
                "INSERT INTO demo_visitors (email, phone, organisation, link_hash, link_expires_at, client_address) VALUES ($1, $2, $3, $4, now() + make_interval(mins => $5), $6) "
                "ON CONFLICT (email) DO UPDATE SET phone = COALESCE(EXCLUDED.phone, demo_visitors.phone), organisation = EXCLUDED.organisation, "
                "link_hash = EXCLUDED.link_hash, link_expires_at = EXCLUDED.link_expires_at, client_address = EXCLUDED.client_address",
                email, phone, values["organisation"], _hash(token), LINK_MINUTES, client,
            )
        finally:
            await conn.close()
        link = f"{self.public_url}/demo/access/verify?token={token}"
        shown = ""
        if smtp is not None:
            from app.services.delivery import Message

            body = (f"Hello,\n\nOpen this link to sign in to the JustSimple demo:\n{link}\n\nIt works once, for {LINK_MINUTES} minutes. "
                    "If you did not ask for it, ignore this email.\n\nJustSimple")
            try:
                await smtp.send(Message(channel="email", recipient=email, subject="Your sign-in link for the JustSimple demo", body=body))
            except Exception:  # noqa: BLE001 - say so; the reason goes to the log
                import logging

                logging.getLogger("demo").exception("demo sign-in email to %s failed", email.split("@")[-1])
                return self.form(values, note="We couldn't send the email just now. Check the address and try again in a minute.")
        else:
            shown = f'<p><b>Email is not set up on this server</b> (DEMO_ACCESS_SHOW_LINK is on), so here is the link: <a href="{html.escape(link)}">sign in</a>.</p>'
        return self.page("Check your email", f"""<div class="brand"><span class="logo">J</span>JustSimple<span class="pill">Demo</span></div>
<div class="card" style="max-width:560px;display:flex;flex-direction:column;gap:10px"><h1 style="font-size:1.6rem">Check your email</h1>
<p>We sent a sign-in link to <b>{html.escape(email)}</b>. Open it on this device to go into the demo. It works once, for {LINK_MINUTES} minutes.</p>
<p style="color:var(--muted)">Nothing there? Look in the spam folder, or <a href="/">ask again</a>.</p>{shown}</div>""")

    async def verify(self, token: str) -> Response:
        conn = await self._conn()
        try:
            row = await conn.fetchrow(
                "UPDATE demo_visitors SET verified_at = COALESCE(verified_at, now()), last_seen_at = now(), link_hash = NULL, link_expires_at = NULL "
                "WHERE link_hash = $1 AND link_expires_at > now() RETURNING id", _hash(token))
        finally:
            await conn.close()
        if row is None:
            return self.form(note="That sign-in link has expired or was already used. Ask for a new one below.")
        response = RedirectResponse("/", status_code=303)
        response.set_cookie(COOKIE, self.cookie_for(row["id"]), max_age=COOKIE_DAYS * 86400, httponly=True, samesite="lax", secure=self.secure)
        return response

    # ------------------------------------------------------------------ the owner's list
    def _token_ok(self, given: str) -> bool:
        return bool(given) and hmac.compare_digest(given.encode(), self.admin_token.encode())

    async def visitors(self) -> list[dict[str, Any]]:
        conn = await self._conn()
        try:
            rows = await conn.fetch("SELECT email, phone, organisation, created_at, verified_at, last_seen_at FROM demo_visitors ORDER BY created_at DESC")
        finally:
            await conn.close()
        return [dict(r) for r in rows]


def router(access: Access, site: Callable[[Request], Awaitable[Response]]) -> APIRouter:
    """``site`` answers ``/`` for a signed-in visitor (the website itself)."""
    r = APIRouter(tags=["demo sign-in"], include_in_schema=False)

    @r.get("/")
    async def home(request: Request) -> Response:
        if access.visitor_of(request) is None:
            return access.form()
        return await site(request)

    @r.post("/demo/access/request")
    async def request_link(request: Request) -> Response:
        return await access.request_link(request)

    @r.get("/demo/access/verify")
    async def verify(token: str = "") -> Response:
        return await access.verify(token)

    @r.post("/demo/access/sign-out")
    async def sign_out() -> Response:
        response = RedirectResponse("/", status_code=303)
        response.delete_cookie(COOKIE)
        return response

    def token_form(note: str = "") -> HTMLResponse:
        return access.page("Demo visitors", f"""<h1>Demo visitors</h1>{f'<div class="warn">{note}</div>' if note else ''}
<form method="post" action="/demo/visitors" class="card" style="max-width:480px;display:flex;flex-direction:column;gap:10px">
<label style="display:flex;flex-direction:column;gap:4px"><b>Platform admin token</b><input name="token" type="password" autocomplete="off" required style="font:inherit;padding:10px;border:1px solid var(--line);border-radius:10px"></label>
<small style="color:var(--muted)">PLATFORM_ADMIN_TOKEN in the service's Environment settings on Render.</small>
<button class="btn" type="submit" style="border:0;cursor:pointer;justify-content:center">Show the list</button></form>""")

    @r.get("/demo/visitors")
    async def visitors_form() -> Response:
        return token_form()

    @r.post("/demo/visitors")
    async def visitors_list(request: Request) -> Response:
        raw = parse_qs((await request.body()).decode("utf8", "replace"))
        token = raw.get("token", [""])[0]
        if not access._token_ok(token):
            import asyncio

            await asyncio.sleep(1)  # (slows guessing)
            return token_form("That token is not right.")
        rows = await access.visitors()
        if raw.get("format", [""])[0] == "csv":
            out = io.StringIO()
            w = csv.writer(out)
            w.writerow(["email", "mobile", "organisation", "asked_at", "confirmed_at", "last_signed_in_at"])
            for v in rows:
                w.writerow([v["email"], v["phone"] or "", v["organisation"], v["created_at"].isoformat(), v["verified_at"].isoformat() if v["verified_at"] else "",
                            v["last_seen_at"].isoformat() if v["last_seen_at"] else ""])
            return Response(out.getvalue(), media_type="text/csv", headers={"Content-Disposition": 'attachment; filename="demo-visitors.csv"', "Cache-Control": "no-store"})
        fmt = lambda d: d.strftime("%d %b %Y, %H:%M UTC") if d else "—"  # noqa: E731
        trs = "".join(f"<tr><td>{html.escape(v['email'])}</td><td>{html.escape(v['phone'] or '')}</td><td>{html.escape(v['organisation'])}</td><td>{fmt(v['created_at'])}</td>"
                      f"<td>{'Yes, ' + fmt(v['verified_at']) if v['verified_at'] else 'Not yet'}</td><td>{fmt(v['last_seen_at'])}</td></tr>" for v in rows)
        return access.page("Demo visitors", f"""<h1>Demo visitors ({len(rows)})</h1>
<form method="post" action="/demo/visitors"><input type="hidden" name="token" value="{html.escape(token)}"><input type="hidden" name="format" value="csv"><button class="btn" type="submit" style="border:0;cursor:pointer">Download CSV</button></form>
<div class="card" style="overflow-x:auto"><table style="border-collapse:collapse;width:100%;font-size:.92rem" cellpadding="8"><thead><tr style="text-align:left"><th>Email</th><th>Mobile</th><th>Organisation</th><th>Asked</th><th>Confirmed</th><th>Last sign-in</th></tr></thead>
<tbody>{trs or '<tr><td colspan="6">Nobody yet.</td></tr>'}</tbody></table></div>""")

    return r
