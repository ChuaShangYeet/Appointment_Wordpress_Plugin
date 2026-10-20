"""The demo's own routes: a start page with the sample businesses and their keys, the same as JSON, the inbox, and the emailed links."""
from __future__ import annotations

import html
from collections.abc import Callable
from typing import Any

from fastapi import APIRouter
from fastapi.responses import HTMLResponse

BRAND = "JustSimple"  # placeholder name until the real one is chosen
CSP = "default-src 'none'; style-src 'unsafe-inline' https://fonts.googleapis.com; font-src https://fonts.gstatic.com; img-src 'self' data:; frame-ancestors 'none'; base-uri 'none'"
TYPES = {"spa": "Spa & wellness", "salon": "Hair salon", "restaurant": "Restaurant", "automotive": "Car workshop", "professional": "Home & professional services"}


def _page(title: str, body: str) -> HTMLResponse:
    doc = f"""<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>{html.escape(title)}</title>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&family=JetBrains+Mono:wght@400;500&display=swap">
<style>
:root {{ --bg:#f7f7fb; --surface:#fff; --line:#e4e4ee; --fg:#14142b; --muted:#5f6178; --accent:#4f46e5; --accent-soft:#eef0ff; --warn:#8a5a00; --warn-soft:#fff5dc; color-scheme: light; }}
* {{ box-sizing: border-box; }}
body {{ margin:0; background:var(--bg); color:var(--fg); font:15px/1.55 Inter, system-ui, sans-serif; }}
main {{ max-width: 1040px; margin: 0 auto; padding: 40px 16px 72px; display:flex; flex-direction:column; gap:28px; }}
h1 {{ font-size: clamp(1.8rem, 4vw, 2.6rem); line-height:1.1; margin:0; letter-spacing:-0.02em; }}
h2 {{ font-size:1.2rem; margin:0 0 12px; }}
p {{ margin:0; }}
a {{ color: var(--accent); }}
.lead {{ color: var(--muted); font-size:1.05rem; max-width: 720px; }}
.brand {{ display:flex; gap:10px; align-items:center; font-weight:700; font-size:1.05rem; }}
.logo {{ width:34px; height:34px; border-radius:10px; background:linear-gradient(135deg, var(--accent), #a855f7); color:#fff; display:grid; place-items:center; font-weight:700; }}
.pill {{ display:inline-block; border-radius:999px; padding:2px 10px; font-size:.78rem; font-weight:600; background:var(--accent-soft); color:var(--accent); }}
.grid {{ display:grid; grid-template-columns: repeat(auto-fit, minmax(300px, 1fr)); gap:16px; }}
.card {{ background:var(--surface); border:1px solid var(--line); border-radius:14px; padding:18px; box-shadow: 0 1px 2px rgb(20 20 43 / 5%), 0 8px 24px rgb(20 20 43 / 5%); min-width:0; display:flex; flex-direction:column; gap:10px; }}
.k {{ font: 12.5px/1.4 'JetBrains Mono', ui-monospace, monospace; background:var(--bg); border:1px solid var(--line); border-radius:8px; padding:6px 8px; overflow-wrap:anywhere; }}
.label {{ font-size:.72rem; text-transform:uppercase; letter-spacing:.08em; color:var(--muted); font-weight:600; }}
.warn {{ background:var(--warn-soft); color:var(--fg); border:1px solid color-mix(in srgb, var(--warn) 35%, transparent); border-radius:12px; padding:12px 14px; }}
pre {{ font: 12.5px/1.5 'JetBrains Mono', ui-monospace, monospace; background:var(--surface); border:1px solid var(--line); border-radius:12px; padding:14px; overflow-x:auto; margin:0; }}
.links {{ display:flex; flex-wrap:wrap; gap:10px; }}
.btn {{ display:inline-flex; align-items:center; border-radius:10px; padding:10px 16px; font-weight:600; text-decoration:none; background:var(--accent); color:#fff; }}
.btn.ghost {{ background:transparent; color:var(--fg); border:1px solid var(--line); }}
.msg {{ border-top:1px solid var(--line); padding:10px 0; white-space:pre-wrap; overflow-wrap:anywhere; font-size:.9rem; }}
</style></head><body><main>{body}</main></body></html>"""
    return HTMLResponse(doc, headers={"Content-Security-Policy": CSP, "Cache-Control": "no-store"})


def router(store: Any, inbox: Callable[[], list[dict[str, Any]]]) -> APIRouter:
    """``store`` holds the businesses and their keys (``DbStore``): read on every request, as keys can be replaced from the website."""
    r = APIRouter(tags=["demo"])

    def info() -> dict[str, Any]:
        businesses = [b for b in store.data["businesses"] if not b.get("is_new")]  # (the five sample businesses, not ones visitors sign up)
        customer = store.data["sample_customer"]
        return {
            "note": "Public demo: every key here is shared with everyone and the data is shared. Never use real customers or businesses.",
            "docs": "/docs",
            "businesses": [{k: b[k] for k in ("id", "tenant_id", "name", "type", "plan", "publishable_key", "secret_key")} for b in businesses],
            "sample_customer": customer,
            "messages": "/demo/messages",
        }

    @r.get("/demo/info")
    async def demo_info() -> dict[str, Any]:
        """The sample businesses with their keys (shared on purpose: this is a public demo), and the sample customer."""
        return info()

    @r.get("/demo/messages")
    async def demo_messages(after: int = 0, to: str | None = None) -> list[dict[str, Any]]:
        """What the backend 'sent' (WhatsApp, SMS, email), newest last. Nothing is really sent: sign-in codes are read here. ``to`` filters by recipient."""
        return [m for m in inbox() if m["id"] > after and (to is None or m["to"] == to)][-100:]

    @r.get("/demo/link/{kind}/{token}", response_class=HTMLResponse)
    async def demo_link(kind: str, token: str) -> HTMLResponse:
        """Where the emailed links lead: the token, ready to send to the matching verify call."""
        calls = {"sign-in": "POST /v1/public/auth/magic-link/verify", "confirm-email": "POST /v1/public/auth/email/confirm",
                 "sign-up": "POST /v1/signup/verify", "owner": "POST /v1/owner/login/verify", "review": "POST /v1/public/review-invitation"}
        call = calls.get(kind, "the matching verify call")
        return _page("Link", f"""<h1>Emailed link</h1><p class="lead">In a real product this page finishes the step. In the demo, send the token to <b>{html.escape(call)}</b>.</p>
<div class="card"><span class="label">Token</span><div class="k">{html.escape(token)}</div></div>""")

    @r.get("/api", response_class=HTMLResponse, include_in_schema=False)
    async def home() -> HTMLResponse:
        businesses = [b for b in store.data["businesses"] if not b.get("is_new")]
        customer = store.data["sample_customer"]
        cards = "".join(f"""<div class="card"><div style="display:flex;justify-content:space-between;gap:8px;align-items:center"><b>{html.escape(b["name"])}</b><span class="pill">{html.escape(TYPES.get(b["type"], b["type"]))}</span></div>
<span class="label">Publishable key (X-Site-Key)</span><div class="k">{html.escape(b["publishable_key"])}</div>
<span class="label">Secret key (X-API-Key)</span><div class="k">{html.escape(b["secret_key"])}</div></div>""" for b in businesses)
        first = businesses[0]
        sample = f"""# 1. free times tomorrow (customer side, publishable key)
curl "$BASE/v1/public/services" -H "X-Site-Key: {first["publishable_key"]}"
curl "$BASE/v1/public/availability?date=YYYY-MM-DD&service_ids=SERVICE_ID" -H "X-Site-Key: {first["publishable_key"]}"

# 2. book one of them
curl -X POST "$BASE/v1/public/bookings" -H "X-Site-Key: {first["publishable_key"]}" -H "Content-Type: application/json" \\
  -d '{{"service_ids": ["SERVICE_ID"], "start_at": "START_AT_FROM_A_SLOT", "customer": {{"name": "Test", "phone": "+60123000000"}}}}'

# 3. business side: swap the secret key for an admin token, then read the day
curl -X POST "$BASE/v1/auth/token" -H "X-API-Key: {first["secret_key"]}" -H "Content-Type: application/json" -d '{{"user_id": "bot", "role": "manager"}}'
curl "$BASE/v1/admin/bookings?date=YYYY-MM-DD" -H "Authorization: Bearer ACCESS_TOKEN\""""
        body = f"""<div class="brand"><span class="logo">{BRAND[0]}</span>{BRAND}<span class="pill">Demo API</span></div>
<div style="display:flex;flex-direction:column;gap:12px"><h1>Bookings for salons, spas, restaurants, workshops and home services.</h1>
<p class="lead">This is the real booking engine with five sample businesses on the free plan. Call it from a chatbot, an app or a website: every rule (opening hours, staff shifts, double-booking protection, notice periods, plan limits) applies.</p></div>
<div class="links"><a class="btn" href="/">Open the demo website</a><a class="btn ghost" href="/docs">API docs</a><a class="btn ghost" href="/demo/info">Keys as JSON</a><a class="btn ghost" href="/demo/messages">Messages sent</a></div>
<div class="warn"><b>Public demo.</b> These keys are shared with everyone, and so is the data. Nothing is really sent: WhatsApp, SMS and email messages (sign-in codes too) appear under <a href="/demo/messages">Messages sent</a>. Never use real customer details.</div>
<section><h2>The sample businesses</h2><div class="grid">{cards}</div></section>
<section><h2>Try it</h2><p class="lead" style="margin-bottom:12px">Sample customer with bookings at every business: {html.escape(customer["name"])}, {html.escape(customer["phone"])}, {html.escape(customer["email"])}.</p><pre>{html.escape(sample)}</pre></section>"""
        return _page(f"{BRAND} demo API", body)

    return r
