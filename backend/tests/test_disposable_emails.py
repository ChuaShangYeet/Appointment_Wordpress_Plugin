"""Throw-away email addresses are refused at sign-up (app.core.disposable_emails)."""
from __future__ import annotations

from typing import Any

import httpx
import pytest

from app.core import disposable_emails as de
from app.core.config import Settings, get_settings
from tests.conftest import Recorder
from tests.test_ratelimit import redis  # noqa: F401  (fixture)
from tests.test_signup import ask, open_signup  # noqa: F401  (fixtures and helpers)


@pytest.mark.parametrize("address", [
    "x@mailinator.com", "x@MAILINATOR.COM", "someone@Guerrillamail.COM", "a.b+tag@yopmail.com", "me@10minutemail.com", "me@sharklasers.com",
    "me@temp-mail.org", "x@trashmail.net", "x@sub.domain.mailinator.com", "x@mailinator.com.", " x@mailinator.com ", "x@armyspy.com", "x@dispostable.com",
])
def test_known_throwaway_services_are_recognised(address: str) -> None:
    assert de.is_disposable(address)


@pytest.mark.parametrize("address", [
    "owner@gmail.com", "owner@outlook.com", "owner@example.com", "owner@mycompany.com.my", "x@examplemailinator.com", "x@mailinator.com.au", "x@mailinator.co",
    "x@xmailinator.com", "mailinator.com@gmail.com", "x@com", "x@", "x", "", "@", "x@.", "x@mail.mailinator", "x@yopmail",
])
def test_ordinary_addresses_are_not(address: str) -> None:
    assert not de.is_disposable(address)


def test_only_the_part_after_the_last_at_sign_counts() -> None:
    assert de.is_disposable('"odd@name"@mailinator.com') and not de.is_disposable("mailinator.com@gmail.com")
    assert de.domain_of("A@B@Example.COM.") == "example.com" and de.domain_of("plain") == "plain"


def test_a_domain_matches_itself_and_what_is_under_it_never_a_lookalike() -> None:
    assert de._suffixes("a.b.mailinator.com") == ["a.b.mailinator.com", "b.mailinator.com", "mailinator.com"]
    assert de._suffixes("com") == [] and de._suffixes("") == [] and de._suffixes("a..b.com") == ["a.b.com", "b.com"]
    assert de.is_disposable("x@deep.sub.mailinator.com") and not de.is_disposable("x@mailinator.com.evil.test")


def test_the_operator_can_add_and_remove_domains() -> None:
    assert not de.is_disposable("x@tempmail.example")
    assert de.is_disposable("x@tempmail.example", extra_blocked=["tempmail.example"])
    assert de.is_disposable("x@a.tempmail.example", extra_blocked=[" TempMail.Example. "])  # (spelling is forgiven)
    assert not de.is_disposable("x@mailinator.com", allowed=["mailinator.com"])
    assert not de.is_disposable("x@a.mailinator.com", allowed=["mailinator.com"])  # taking a domain off takes its subdomains too
    assert not de.is_disposable("x@mailinator.com", extra_blocked=["mailinator.com"], allowed=["MAILINATOR.com"])  # allowed wins
    assert de.is_disposable("x@guerrillamail.com", allowed=["mailinator.com"])  # and only what was named


def test_the_built_in_list_is_clean() -> None:
    domains = de.BUILTIN_DISPOSABLE_DOMAINS
    assert len(domains) > 100 and all(d == d.lower() and d == d.strip(".") and "." in d and " " not in d and "@" not in d for d in domains)
    assert not {"gmail.com", "outlook.com", "yahoo.com", "hotmail.com", "icloud.com", "proton.me", "example.com"} & domains  # no real mail provider on it


def test_the_domain_lists_are_settings() -> None:
    s = Settings(_env_file=None)  # type: ignore[call-arg]
    assert s.blocked_email_domains == [] and s.allowed_email_domains == []
    s = Settings(_env_file=None, blocked_email_domains=["a.test"], allowed_email_domains=["b.test"])  # type: ignore[call-arg]
    assert s.blocked_email_domains == ["a.test"] and s.allowed_email_domains == ["b.test"]


# --------------------------------------------------------------------- at sign-up
async def test_sign_up_refuses_a_throwaway_address_and_sends_nothing(client: httpx.AsyncClient, open_signup: Recorder, redis: Any, monkeypatch: pytest.MonkeyPatch) -> None:  # noqa: F811
    monkeypatch.setattr(get_settings(), "signup_resend_cooldown_seconds", 60)
    r = await ask(client, owner_email="owner@mailinator.com")
    assert r.status_code == 422 and r.json()["error"] == "disposable_email" and "temporary-mail" in r.json()["detail"]
    assert open_signup.sent == [] and await redis.keys("signup:*") == []  # no email, no pending request, no cool-down or daily count spent


async def test_a_refused_address_is_refused_however_it_is_written(client: httpx.AsyncClient, open_signup: Recorder) -> None:
    for address in ("Owner@MAILINATOR.COM", "owner+one@a.mailinator.com", "  owner@Yopmail.com "):
        assert (await ask(client, owner_email=address.strip())).json().get("error") == "disposable_email", address
    assert open_signup.sent == []


async def test_a_real_address_still_signs_up(client: httpx.AsyncClient, open_signup: Recorder) -> None:
    assert (await ask(client, owner_email="owner@gmail.com")).status_code == 202 and len(open_signup.sent) == 1


async def test_old_terms_are_put_right_before_the_address_is_judged(client: httpx.AsyncClient, open_signup: Recorder) -> None:
    r = await ask(client, owner_email="owner@mailinator.com", accepted_terms_version="old")
    assert r.status_code == 422 and r.json()["error"] == "validation_failed"  # (the old terms are the first thing to put right)


async def test_the_operators_lists_apply_at_sign_up(client: httpx.AsyncClient, open_signup: Recorder, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(get_settings(), "blocked_email_domains", ["bad.example"])
    assert (await ask(client, owner_email="owner@bad.example")).json()["error"] == "disposable_email"
    monkeypatch.setattr(get_settings(), "allowed_email_domains", ["mailinator.com"])
    assert (await ask(client, owner_email="owner@mailinator.com")).status_code == 202


async def test_a_refusal_does_not_reveal_who_is_registered(client: httpx.AsyncClient, open_signup: Recorder) -> None:
    first = await ask(client, owner_email="owner@mailinator.com")
    second = await ask(client, owner_email="someone.else@mailinator.com")
    assert first.status_code == second.status_code == 422 and first.json() == second.json()


async def test_customers_may_use_any_address(seeded: Any, mail: Recorder) -> None:
    from tests.test_customer_identity import booked_with_email

    await booked_with_email(seeded, email="customer@mailinator.com")  # (a customer's address is not checked)
