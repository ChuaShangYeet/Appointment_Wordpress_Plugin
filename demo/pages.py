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
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Hanken+Grotesk:wght@400;500;600;700&family=JetBrains+Mono:wght@400;500&display=swap">
<style>
/* the same look as the demo website (localsite/web/index.html): one face, near-black actions, the brand green on the mark and links */
:root {{ --bg:#f6f7f6; --surface:#fff; --raised:#f2f4f3; --line:#e3e6e4; --control:#cfd4d1; --fg:#121614; --muted:#59625e; --primary:#151917; --accent:#157150; --accent-ink:#0f5a3f; --warn-ink:#7a4a07; --warn-soft:#fdf3dc; --warn-line:#f2d28b; color-scheme: light; }}
* {{ box-sizing: border-box; }}
body {{ margin:0; background:var(--bg); color:var(--fg); font:15px/1.55 'Hanken Grotesk', system-ui, sans-serif; -webkit-font-smoothing:antialiased; }}
::selection {{ background:#e3f2eb; color:var(--accent-ink); }}
main {{ max-width: 1080px; margin: 0 auto; padding: 32px 20px 72px; display:flex; flex-direction:column; gap:28px; }}
h1 {{ font-size: clamp(1.7rem, 3.4vw, 2.3rem); line-height:1.15; margin:0; letter-spacing:-0.02em; font-weight:600; text-wrap:balance; }}
h2 {{ font-size:1.15rem; margin:0 0 12px; font-weight:600; letter-spacing:-0.01em; }}
p {{ margin:0; }}
a {{ color: var(--accent-ink); text-underline-offset:3px; }}
:focus-visible {{ outline:2px solid var(--accent); outline-offset:2px; }}
.lead {{ color: var(--muted); font-size:1.05rem; max-width: 680px; }}
.brand {{ display:flex; gap:10px; align-items:center; font-weight:700; font-size:1.05rem; letter-spacing:-0.02em; }}
.logo {{ width:28px; height:28px; flex:none; font-size:0; background:url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 28 28'%3E%3Crect width='28' height='28' rx='7' fill='%23157150'/%3E%3Cpath d='M10 6.5v3M18 6.5v3' stroke='%23fff' stroke-width='2' stroke-linecap='round'/%3E%3Crect x='6.5' y='8' width='15' height='13.5' rx='3' fill='none' stroke='%23fff' stroke-width='1.8'/%3E%3Cpath d='m10.6 14.9 2.3 2.3 4.5-4.5' fill='none' stroke='%23fff' stroke-width='2' stroke-linecap='round' stroke-linejoin='round'/%3E%3C/svg%3E") no-repeat center / contain; }}
.pill {{ font-size:.85rem; font-weight:500; color:var(--muted); letter-spacing:0; }}
.grid {{ display:grid; grid-template-columns: repeat(auto-fit, minmax(300px, 1fr)); gap:16px; }}
.card {{ background:var(--surface); border:1px solid var(--line); border-radius:10px; padding:20px; min-width:0; display:flex; flex-direction:column; gap:10px; }}
.k {{ font: 12.5px/1.45 'JetBrains Mono', ui-monospace, monospace; background:var(--raised); border-radius:6px; padding:7px 9px; overflow-wrap:anywhere; }}
.label {{ font-size:.87rem; color:var(--muted); font-weight:500; }}
.warn {{ background:var(--warn-soft); color:var(--warn-ink); border:1px solid var(--warn-line); border-radius:8px; padding:12px 14px; }}
pre {{ font: 12.5px/1.55 'JetBrains Mono', ui-monospace, monospace; background:var(--surface); border:1px solid var(--line); border-radius:8px; padding:16px; overflow-x:auto; margin:0; }}
.links {{ display:flex; flex-wrap:wrap; gap:10px; }}
.btn {{ display:inline-flex; align-items:center; justify-content:center; border-radius:6px; padding:0 16px; min-height:42px; font:inherit; font-weight:600; text-decoration:none; background:var(--primary); color:#fff; transition: background-color 150ms ease, transform 160ms cubic-bezier(0.23, 1, 0.32, 1); }}
.btn:active {{ transform: scale(0.97); }}
.btn.ghost {{ background:var(--surface); color:var(--fg); border:1px solid var(--control); }}
@media (hover: hover) and (pointer: fine) {{ .btn:hover {{ background:#2b302d; }} .btn.ghost:hover {{ background:var(--raised); }} }}
input {{ font-size:16px !important; border-color:var(--control) !important; border-radius:6px !important; min-height:42px; }}
input:focus {{ outline:none; border-color:var(--primary) !important; box-shadow:0 0 0 3px rgb(18 22 20 / 10%); }}
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
