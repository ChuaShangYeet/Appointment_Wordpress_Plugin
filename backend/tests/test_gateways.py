"""WhatsApp Cloud API and Twilio SMS providers (blueprint §15), against a fake HTTP transport - no live message is ever sent."""
from __future__ import annotations

import json
import logging
from typing import Any
from urllib.parse import parse_qs

import httpx
import pytest
from sqlalchemy import select

from app.core.config import Settings, get_settings
from app.core.database import tenant_session
from app.models import NotificationLog
from app.services import delivery
from app.services.delivery import DeliveryError, Message, TwilioSmsProvider, WhatsAppCloudProvider, template_param
from tests.conftest import Recorder, Seed
from tests.test_customer_cancel import CODE, PHONE, otp, request_code  # noqa: F401
from tests.test_customer_identity import booked_with_email
from tests.test_notifications import put_template, run_outbox

TOKEN = "EAAG-secret-token-0123456789"
SID = "AC00000000000000000000000000000000"
AUTH = "twilio-auth-token-abcdef0123456789"


class Gateway:
    """A fake HTTP server: remembers every request, answers with whatever the test queued (default: success)."""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.answers: list[Any] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        answer = self.answers.pop(0) if self.answers else None
        if isinstance(answer, Exception):
            raise answer
        return answer if answer is not None else httpx.Response(200, json={"messages": [{"id": "wamid.ABC"}], "sid": "SM123"})

    @property
    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handler)

    def json(self, index: int = -1) -> dict[str, Any]:
        return json.loads(self.requests[index].content)  # type: ignore[no-any-return]

    def form(self, index: int = -1) -> dict[str, str]:
        return {k: v[0] for k, v in parse_qs(self.requests[index].content.decode()).items()}


def whatsapp(gw: Gateway, **kw: Any) -> WhatsAppCloudProvider:
    return WhatsAppCloudProvider(TOKEN, "10987654321", transport=gw.transport, **kw)


def twilio(gw: Gateway, **kw: Any) -> TwilioSmsProvider:
    kw.setdefault("from_number", "+15005550006")
    return TwilioSmsProvider(SID, AUTH, transport=gw.transport, **kw)


# --------------------------------------------------------------------- WhatsApp Cloud API
async def test_a_template_message_goes_to_the_graph_api_with_its_variables_and_button() -> None:
    gw = Gateway()
    sent = await whatsapp(gw).send(Message("whatsapp", "+60123450001", "text", external_template="auth_code", params=("123456",), button_param="123456"))
    assert sent == "wamid.ABC"
    (request,) = gw.requests
    assert request.method == "POST" and str(request.url) == "https://graph.facebook.com/v21.0/10987654321/messages"
    assert request.headers["Authorization"] == f"Bearer {TOKEN}" and request.headers["Content-Type"] == "application/json"
    assert gw.json() == {
        "messaging_product": "whatsapp", "to": "60123450001", "type": "template",
        "template": {"name": "auth_code", "language": {"code": "en"}, "components": [
            {"type": "body", "parameters": [{"type": "text", "text": "123456"}]},
            {"type": "button", "sub_type": "url", "index": "0", "parameters": [{"type": "text", "text": "123456"}]},
        ]},
    }


async def test_variables_keep_their_order_and_the_button_is_optional() -> None:
    gw = Gateway()
    await whatsapp(gw, language="ms").send(Message("whatsapp", "+60123450001", "x", external_template="booking_ok", params=("Aisha", "Massage", "Mon 05 Oct", "10:00")))
    template = gw.json()["template"]
    assert template["language"] == {"code": "ms"}
    assert [c["type"] for c in template["components"]] == ["body"]
    assert [p["text"] for p in template["components"][0]["parameters"]] == ["Aisha", "Massage", "Mon 05 Oct", "10:00"]


async def test_a_template_without_variables_has_no_components() -> None:
    gw = Gateway()
    await whatsapp(gw).send(Message("whatsapp", "+60123450001", "x", external_template="hello_world"))
    assert gw.json()["template"] == {"name": "hello_world", "language": {"code": "en"}}


async def test_a_message_without_a_template_is_sent_as_plain_text() -> None:
    gw = Gateway()
    await whatsapp(gw).send(Message("whatsapp", "+60123450001", "See you at ten"))
    assert gw.json() == {"messaging_product": "whatsapp", "to": "60123450001", "type": "text", "text": {"preview_url": False, "body": "See you at ten"}}


def test_template_variables_are_made_acceptable_to_meta() -> None:
    assert template_param("Massage\n+ Facial\tcombo") == "Massage + Facial combo"
    assert template_param("a    b") == "a b" and template_param("a   b") == "a   b"  # only runs of four or more spaces are refused
    assert template_param("  padded  ") == "padded"
    assert template_param("") == "-" and template_param(" \n ") == "-"  # an empty variable is refused too


async def test_the_api_address_follows_the_version_and_base_url() -> None:
    gw = Gateway()
    await whatsapp(gw, api_version="v19.0", base_url="https://graph.example.test/").send(Message("whatsapp", "+60123450001", "x"))
    assert str(gw.requests[0].url) == "https://graph.example.test/v19.0/10987654321/messages"


async def test_an_answer_without_an_id_still_counts_as_accepted() -> None:
    for body in ({"messages": []}, {}, {"messages": [{}]}):
        gw = Gateway()
        gw.answers = [httpx.Response(200, json=body)]
        assert await whatsapp(gw).send(Message("whatsapp", "+60123450001", "x")) is None
    gw = Gateway()
    gw.answers = [httpx.Response(200, text="not json")]
    assert await whatsapp(gw).send(Message("whatsapp", "+60123450001", "x")) is None


async def test_a_refusal_carries_the_apis_code_and_text_but_not_the_request() -> None:
    gw = Gateway()
    gw.answers = [httpx.Response(400, json={"error": {"message": "(#131030) Recipient phone number not in allowed list", "code": 131030}})]
    with pytest.raises(DeliveryError) as caught:
        await whatsapp(gw).send(Message("whatsapp", "+60123450001", "x", external_template="auth_code", params=("123456",)))
    text = str(caught.value)
    assert text.startswith("WhatsApp API 400:") and "131030" in text and "not in allowed list" in text
    assert TOKEN not in text and "60123450001" not in text and "123456" not in text


@pytest.mark.parametrize("answer", [httpx.Response(500, text="<html>oops</html>"), httpx.Response(429, json={"error": "slow down"}), httpx.Response(401, json=[1])])
async def test_other_failures_are_delivery_errors_too(answer: httpx.Response) -> None:
    gw = Gateway()
    gw.answers = [answer]
    with pytest.raises(DeliveryError, match=f"WhatsApp API {answer.status_code}"):
        await whatsapp(gw).send(Message("whatsapp", "+60123450001", "x"))


@pytest.mark.parametrize("failure", [httpx.ConnectError("could not reach https://graph.facebook.com Bearer " + TOKEN), httpx.ReadTimeout("slow")])
async def test_a_network_failure_reports_only_its_type(failure: Exception) -> None:
    gw = Gateway()
    gw.answers = [failure]
    with pytest.raises(DeliveryError) as caught:
        await whatsapp(gw).send(Message("whatsapp", "+60123450001", "x"))
    assert str(caught.value) == f"WhatsApp request failed: {type(failure).__name__}"  # the exception's own text could hold the token


# --------------------------------------------------------------------- Twilio SMS
async def test_an_sms_is_a_form_post_with_the_account_as_basic_auth() -> None:
    gw = Gateway()
    sent = await twilio(gw).send(Message("sms", "+60123450001", "Your code is 123456"))
    assert sent == "SM123"
    (request,) = gw.requests
    assert request.method == "POST" and str(request.url) == f"https://api.twilio.com/2010-04-01/Accounts/{SID}/Messages.json"
    import base64

    assert request.headers["Authorization"] == "Basic " + base64.b64encode(f"{SID}:{AUTH}".encode()).decode()
    assert request.headers["Content-Type"] == "application/x-www-form-urlencoded"
    assert gw.form() == {"To": "+60123450001", "Body": "Your code is 123456", "From": "+15005550006"}


async def test_a_messaging_service_replaces_the_sender_number() -> None:
    gw = Gateway()
    await twilio(gw, from_number=None, messaging_service_sid="MG123").send(Message("sms", "+60123450001", "x"))
    assert gw.form() == {"To": "+60123450001", "Body": "x", "MessagingServiceSid": "MG123"}
    gw2 = Gateway()
    await twilio(gw2, messaging_service_sid="MG123").send(Message("sms", "+60123450001", "x"))  # a service wins when both are given
    assert "From" not in gw2.form() and gw2.form()["MessagingServiceSid"] == "MG123"


def test_twilio_needs_a_sender() -> None:
    with pytest.raises(ValueError):
        TwilioSmsProvider(SID, AUTH)


async def test_a_twilio_refusal_carries_its_code_and_text_only() -> None:
    gw = Gateway()
    gw.answers = [httpx.Response(400, json={"code": 21211, "message": "The 'To' number is not a valid phone number.", "status": 400})]
    with pytest.raises(DeliveryError) as caught:
        await twilio(gw).send(Message("sms", "+60123450001", "Your code is 123456"))
    text = str(caught.value)
    assert text.startswith("Twilio 400:") and "21211" in text and "not a valid phone number" in text
    assert AUTH not in text and SID not in text and "123456" not in text


async def test_twilio_without_an_id_or_with_a_network_failure() -> None:
    gw = Gateway()
    gw.answers = [httpx.Response(201, json={})]
    assert await twilio(gw).send(Message("sms", "+60123450001", "x")) is None
    gw.answers = [httpx.Response(201, text="?")]
    assert await twilio(gw).send(Message("sms", "+60123450001", "x")) is None
    gw.answers = [httpx.ConnectError("boom " + AUTH)]
    with pytest.raises(DeliveryError) as caught:
        await twilio(gw).send(Message("sms", "+60123450001", "x"))
    assert str(caught.value) == "SMS request failed: ConnectError"


# --------------------------------------------------------------------- configuration
def settings(**kw: Any) -> Settings:
    return Settings(_env_file=None, **kw)  # type: ignore[call-arg]


#: A bot check that is set up, so that its start-up warning does not mix into what these tests count.
BOT_CHECK = {"captcha_provider": "turnstile", "turnstile_site_key": "site-key", "turnstile_secret_key": "secret-key"}


@pytest.fixture
def registry(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    fresh: dict[str, Any] = {}
    monkeypatch.setattr(delivery, "_providers", fresh)
    return fresh


def test_whatsapp_is_registered_only_with_both_a_token_and_a_phone_number_id(registry: dict[str, Any]) -> None:
    delivery.configure_providers(settings(environment="production", whatsapp_access_token=TOKEN))
    delivery.configure_providers(settings(environment="production", whatsapp_phone_number_id="123"))
    assert registry == {}
    delivery.configure_providers(settings(environment="production", whatsapp_access_token=TOKEN, whatsapp_phone_number_id="123",
                                          whatsapp_api_version="v20.0", whatsapp_template_language="ms", whatsapp_base_url="https://g.example.test", delivery_timeout_seconds=3))
    provider = registry["whatsapp"]
    assert isinstance(provider, WhatsAppCloudProvider) and provider.url == "https://g.example.test/v20.0/123/messages"
    assert provider.language == "ms" and provider.timeout == 3 and provider._token == TOKEN


def test_twilio_is_registered_only_when_chosen_and_complete(registry: dict[str, Any], caplog: pytest.LogCaptureFixture) -> None:
    delivery.configure_providers(settings(environment="production", twilio_account_sid=SID, twilio_auth_token=AUTH, twilio_from="+1500"))
    assert registry == {}  # SMS_PROVIDER not chosen
    delivery.configure_providers(settings(environment="production", sms_provider="twilio", twilio_account_sid=SID))
    assert registry == {}  # no auth token
    with caplog.at_level(logging.ERROR):
        delivery.configure_providers(settings(environment="production", sms_provider="twilio", twilio_account_sid=SID, twilio_auth_token=AUTH))
    assert registry == {} and "TWILIO_FROM" in caplog.text and AUTH not in caplog.text
    delivery.configure_providers(settings(environment="production", sms_provider="twilio", twilio_account_sid=SID, twilio_auth_token=AUTH,
                                          twilio_messaging_service_sid="MG1", twilio_base_url="https://t.example.test", delivery_timeout_seconds=4))
    provider = registry["sms"]
    assert isinstance(provider, TwilioSmsProvider) and provider.messaging_service_sid == "MG1" and provider.from_number is None
    assert provider.base_url == "https://t.example.test" and provider.timeout == 4


def test_development_fills_the_gaps_with_the_logging_provider_but_keeps_real_ones(registry: dict[str, Any]) -> None:
    delivery.configure_providers(settings(environment="development", whatsapp_access_token=TOKEN, whatsapp_phone_number_id="123"))
    assert isinstance(registry["whatsapp"], WhatsAppCloudProvider)
    assert isinstance(registry["sms"], delivery.LoggingProvider) and isinstance(registry["email"], delivery.LoggingProvider)


def test_the_secrets_do_not_show_up_when_the_settings_are_printed() -> None:
    shown = repr(settings(whatsapp_access_token=TOKEN, twilio_auth_token=AUTH))
    assert TOKEN not in shown and AUTH not in shown


def test_missing_delivery_configuration_is_listed(registry: dict[str, Any]) -> None:
    problems = delivery.configuration_problems(settings(environment="production", **BOT_CHECK))
    assert len(problems) == 3
    assert any("SMTP_HOST" in p for p in problems) and any("OTP_CHANNEL=whatsapp" in p for p in problems) and any("MAGIC_LINK_URL" in p for p in problems)
    assert any("OTP_CHANNEL=sms" in p for p in delivery.configuration_problems(settings(environment="production", otp_channel="sms", **BOT_CHECK)))
    ready = settings(environment="production", **BOT_CHECK, smtp_host="smtp.example.test", magic_link_url="https://shop.example.test/login?t={token}",
                     whatsapp_access_token=TOKEN, whatsapp_phone_number_id="1", whatsapp_otp_template="auth_code")
    delivery.configure_providers(ready)
    assert delivery.configuration_problems(ready) == []


def test_a_whatsapp_provider_without_an_otp_template_is_reported(registry: dict[str, Any]) -> None:
    config = settings(environment="production", **BOT_CHECK, smtp_host="smtp.example.test", magic_link_url="https://x.test/{token}", whatsapp_access_token=TOKEN, whatsapp_phone_number_id="1")
    delivery.configure_providers(config)
    (problem,) = delivery.configuration_problems(config)
    assert "WHATSAPP_OTP_TEMPLATE" in problem
    sms = settings(environment="production", **BOT_CHECK, otp_channel="sms", smtp_host="smtp.example.test", magic_link_url="https://x.test/{token}", sms_provider="twilio",
                   twilio_account_sid=SID, twilio_auth_token=AUTH, twilio_from="+1500", whatsapp_access_token=TOKEN, whatsapp_phone_number_id="1")
    delivery.configure_providers(sms)
    assert delivery.configuration_problems(sms) == []  # the template is needed only when the code goes by WhatsApp


def test_the_report_is_logged_outside_development_and_silent_inside(registry: dict[str, Any], caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.ERROR):
        assert delivery.report_configuration(settings(environment="development")) == [] and caplog.records == []
        assert delivery.report_configuration(settings(environment="test")) == [] and caplog.records == []
        problems = delivery.report_configuration(settings(environment="production", **BOT_CHECK))
    assert len(problems) == 3 and len(caplog.records) == 3
    assert all(r.levelno == logging.ERROR and "delivery configuration" in r.getMessage() for r in caplog.records)


# --------------------------------------------------------------------- sign-in codes and booking messages through the gateways
async def log_of(s: Seed, event: str) -> list[NotificationLog]:
    async with tenant_session(s.tenant_id) as db:
        return list((await db.scalars(select(NotificationLog).where(NotificationLog.event == event))).all())


async def test_the_sign_in_code_goes_out_as_the_approved_whatsapp_template(seeded: Seed, otp: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    gw = Gateway()
    delivery.set_provider("whatsapp", whatsapp(gw))
    monkeypatch.setattr(get_settings(), "whatsapp_otp_template", "sign_in_code")
    monkeypatch.setattr(get_settings(), "otp_channel", "whatsapp")
    assert (await request_code(seeded)).status_code == 202
    body = gw.json()
    assert body["to"] == "60123450001" and body["type"] == "template" and body["template"]["name"] == "sign_in_code"
    assert [c["type"] for c in body["template"]["components"]] == ["body", "button"]
    assert body["template"]["components"][0]["parameters"] == [{"type": "text", "text": CODE}]
    assert body["template"]["components"][1]["parameters"] == [{"type": "text", "text": CODE}]
    (row,) = await log_of(seeded, "auth.otp")
    assert (row.status, row.provider, row.provider_message_id, row.channel) == ("sent", "whatsapp-cloud", "wamid.ABC", "whatsapp")
    assert row.recipient == "+60****0001" and CODE not in (row.error or "")


async def test_the_copy_code_button_can_be_switched_off(seeded: Seed, otp: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    gw = Gateway()
    delivery.set_provider("whatsapp", whatsapp(gw))
    monkeypatch.setattr(get_settings(), "whatsapp_otp_template", "sign_in_code")
    monkeypatch.setattr(get_settings(), "whatsapp_otp_button", False)
    assert (await request_code(seeded)).status_code == 202
    assert [c["type"] for c in gw.json()["template"]["components"]] == ["body"]


async def test_without_a_template_the_code_is_plain_text(seeded: Seed, otp: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    gw = Gateway()
    delivery.set_provider("whatsapp", whatsapp(gw))
    monkeypatch.setattr(get_settings(), "whatsapp_otp_template", "")
    assert (await request_code(seeded)).status_code == 202
    assert gw.json()["type"] == "text" and CODE in gw.json()["text"]["body"]


async def test_the_sign_in_code_can_go_by_sms(seeded: Seed, otp: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    gw = Gateway()
    delivery.set_provider("sms", twilio(gw))
    monkeypatch.setattr(get_settings(), "otp_channel", "sms")
    monkeypatch.setattr(get_settings(), "whatsapp_otp_template", "sign_in_code")  # irrelevant for SMS
    assert (await request_code(seeded)).status_code == 202
    assert gw.form()["To"] == PHONE and CODE in gw.form()["Body"] and "expires in 5 minutes" in gw.form()["Body"]
    (row,) = await log_of(seeded, "auth.otp")
    assert (row.status, row.provider, row.provider_message_id, row.channel) == ("sent", "twilio", "SM123", "sms")


async def test_a_gateway_refusal_stops_the_login_and_lets_the_customer_ask_again(seeded: Seed, otp: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    gw = Gateway()
    gw.answers = [httpx.Response(401, json={"error": {"message": "Invalid OAuth access token", "code": 190}})]
    delivery.set_provider("whatsapp", whatsapp(gw))
    monkeypatch.setattr(get_settings(), "whatsapp_otp_template", "sign_in_code")
    monkeypatch.setattr(get_settings(), "otp_resend_cooldown_seconds", 60)
    refused = await request_code(seeded)
    assert refused.status_code == 503 and refused.json()["error"] == "delivery_failed"
    assert TOKEN not in refused.text and "190" not in refused.text  # the customer is told nothing about the gateway
    (row,) = await log_of(seeded, "auth.otp")
    assert row.status == "failed" and "190" in (row.error or "") and "Invalid OAuth access token" in (row.error or "") and TOKEN not in (row.error or "")
    assert (await request_code(seeded)).status_code == 202  # no cool-down was left behind


async def test_a_booking_message_with_a_whatsapp_template_reaches_the_gateway(seeded: Seed, mail: Recorder) -> None:
    gw = Gateway()
    delivery.set_provider("whatsapp", whatsapp(gw))
    assert (await put_template(seeded, "booking.confirmed", "whatsapp", body="x", external_template="booking_confirmed_v2")).status_code == 200
    await booked_with_email(seeded, hour=10)
    await run_outbox(seeded)
    template = gw.json()["template"]
    assert template["name"] == "booking_confirmed_v2" and gw.json()["to"] == PHONE.lstrip("+")
    assert [p["text"] for p in template["components"][0]["parameters"]][:2] == ["Aisha", "Massage"]
    assert {m.channel for m in mail.sent} == {"email"}  # email still goes through the (recording) email provider


# --------------------------------------------------------------------- start-up
async def test_the_api_and_the_worker_report_missing_delivery_configuration_when_they_start(monkeypatch: pytest.MonkeyPatch) -> None:
    from app import main, worker
    from app.services import outbox

    seen: list[str] = []
    monkeypatch.setattr(delivery, "configure_providers", lambda s: seen.append("configure"))
    monkeypatch.setattr(delivery, "report_configuration", lambda s: seen.append("report") or [])
    async with main.lifespan(main.app):
        assert seen == ["configure", "report"]  # providers first, then the report about them
    seen.clear()

    async def no_sweep(*a: Any, **k: Any) -> None:
        seen.append("run")

    monkeypatch.setattr(worker, "run", no_sweep)
    monkeypatch.setattr(outbox, "set_event_publisher", lambda p: seen.append("publisher"))
    await worker.amain(["--once"])
    assert seen[:2] == ["configure", "report"] and "run" in seen


# --------------------------------------------------------------------- details that are easy to get wrong
async def test_the_timeout_is_passed_on_to_the_http_client() -> None:
    gw = Gateway()
    await whatsapp(gw, timeout=3).send(Message("whatsapp", "+60123450001", "x"))
    await twilio(gw, timeout=4).send(Message("sms", "+60123450001", "x"))
    await whatsapp(gw).send(Message("whatsapp", "+60123450001", "x"))
    assert [r.extensions["timeout"]["read"] for r in gw.requests] == [3, 4, 10.0]


async def test_a_long_error_text_is_cut_and_an_empty_one_says_so() -> None:
    gw = Gateway()
    gw.answers = [httpx.Response(400, json={"error": {"code": 1, "message": "x" * 500}}), httpx.Response(500, text="<html>oops</html>")]
    with pytest.raises(DeliveryError) as long_one:
        await whatsapp(gw).send(Message("whatsapp", "+60123450001", "x"))
    assert len(str(long_one.value)) == len("WhatsApp API 400: ") + 200
    with pytest.raises(DeliveryError) as empty:
        await whatsapp(gw).send(Message("whatsapp", "+60123450001", "x"))
    assert str(empty.value) == "WhatsApp API 500: no details"


async def test_a_base_url_may_end_with_a_slash() -> None:
    gw = Gateway()
    await twilio(gw, base_url="https://sms.example.test/").send(Message("sms", "+60123450001", "x"))
    assert str(gw.requests[0].url) == f"https://sms.example.test/2010-04-01/Accounts/{SID}/Messages.json"


def test_twilio_needs_its_token_even_when_a_sender_is_given(registry: dict[str, Any]) -> None:
    delivery.configure_providers(settings(environment="production", sms_provider="twilio", twilio_account_sid=SID, twilio_from="+1500"))
    assert registry == {}


async def test_without_a_template_the_code_message_carries_no_template_fields(seeded: Seed, otp: Any, mail: Recorder, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(get_settings(), "otp_channel", "whatsapp")
    monkeypatch.setattr(get_settings(), "whatsapp_otp_template", "")
    assert (await request_code(seeded)).status_code == 202
    sent = mail.sent[-1]
    assert (sent.channel, sent.external_template, sent.params, sent.button_param) == ("whatsapp", None, (), None)
    monkeypatch.setattr(get_settings(), "whatsapp_otp_template", "sign_in_code")
    await request_code(seeded, "+60123450002")
    sent = mail.sent[-1]
    assert (sent.external_template, sent.params, sent.button_param) == ("sign_in_code", (CODE,), CODE)
