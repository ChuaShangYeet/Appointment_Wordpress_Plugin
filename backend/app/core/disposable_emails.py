"""Throw-away email addresses: services that hand out a mailbox for ten minutes, no sign-up, anyone can read it.

A business that signs itself up with one of these can never be reached again (its owner login and any lost-key recovery go to that address), and it is the cheapest way to
make many free businesses. So sign-up refuses them (``app.services.signup``). Customers' addresses are *not* checked: a customer who prefers a throw-away address is still a customer.

**This list is a starting point, not a guarantee.** It holds well-known services; new ones appear every week. ``BLOCKED_EMAIL_DOMAINS`` adds domains (JSON list in the environment),
``ALLOWED_EMAIL_DOMAINS`` removes some from the block (a domain on the built-in list that a real business uses, say). A domain matches itself and everything under it
(``a.mailinator.com``), never a domain that only ends with the same letters (``examplemailinator.com``).
"""
from __future__ import annotations

from collections.abc import Iterable

BUILTIN_DISPOSABLE_DOMAINS = frozenset("""
mailinator.com mailinator.net mailinator2.com notmailinator.com reallymymail.com safetymail.info sogetthis.com spamherelots.com suremail.info thisisnotmyrealemail.com
guerrillamail.com guerrillamail.net guerrillamail.org guerrillamail.biz guerrillamail.de guerrillamail.info guerrillamailblock.com sharklasers.com grr.la spam4.me pokemail.net
10minutemail.com 10minutemail.net 10minutemail.org 20minutemail.com 30minutemail.com minuteinbox.com tempinbox.com tempail.com tempmail.com tempmail.net tempmail.org
temp-mail.org temp-mail.io temp-mail.ru tempmailo.com tempmailaddress.com tempm.com tmpmail.org tmpmail.net tmpeml.com
yopmail.com yopmail.fr yopmail.net cool.fr.nf jetable.org nospam.ze.tc nomail.xl.cx mega.zik.dj speed.1s.fr courriel.fr.nf moncourrier.fr.nf monemail.fr.nf monmail.fr.nf
trashmail.com trashmail.net trashmail.de trashmail.me trash-mail.com wegwerfmail.de wegwerfmail.net wegwerfemail.de einrot.com
mailnesia.com maildrop.cc mailcatch.com mailnull.com mailforspam.com mailsac.org mailtothis.com mintemail.com mytemp.email mytrashmail.com
dispostable.com fakeinbox.com fakemail.net fakemailgenerator.com emailfake.com email-fake.com fake-mail.net getnada.com nada.email nada.ltd
mohmal.com moakt.com moakt.cc discard.email discardmail.com discardmail.de spamgourmet.com spamgourmet.net spambox.us spambog.com spamex.com spamfree24.org
getairmail.com emailondeck.com burnermail.io anonbox.net anonymbox.com instantemailaddress.com inboxkitten.com harakirimail.com dropmail.me throwawaymail.com
tempr.email trashmail.io mail-temp.com mailpoof.com luxusmail.org guerrillamail.ch
armyspy.com cuvox.de dayrep.com einrot.de fleckens.hu gustr.com jourrapide.com rhyta.com superrito.com teleworm.us
""".split())


def domain_of(address: str) -> str:
    """The part after the last ``@``, lower-cased, without surrounding dots or spaces."""
    return address.rpartition("@")[2].strip().strip(".").lower()


def _suffixes(domain: str) -> list[str]:
    """``a.b.mailinator.com`` -> ``a.b.mailinator.com``, ``b.mailinator.com``, ``mailinator.com`` (a bare top-level domain is never a match)."""
    labels = [label for label in domain.split(".") if label]
    return [".".join(labels[i:]) for i in range(len(labels) - 1)]


def is_disposable(address: str, *, extra_blocked: Iterable[str] = (), allowed: Iterable[str] = ()) -> bool:
    """Is this address on a throw-away service? ``extra_blocked`` adds domains to the list; ``allowed`` takes domains (and their subdomains) off it."""
    suffixes = _suffixes(domain_of(address))
    allowed_set = {d.strip().strip(".").lower() for d in allowed}
    if any(s in allowed_set for s in suffixes):
        return False
    blocked = BUILTIN_DISPOSABLE_DOMAINS | {d.strip().strip(".").lower() for d in extra_blocked}
    return any(s in blocked for s in suffixes)
