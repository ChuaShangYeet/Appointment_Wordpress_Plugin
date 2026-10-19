"""customer identity: normalised phone numbers, email lookup, ordered booking items

Revision ID: 0014
Revises: 0013

* ``tenants.phone_country_code`` completes national numbers typed without a country code.
* Existing customer phone numbers are rewritten to the normalised ``+<digits>`` spelling (see ``app.core.phone``; the
  rule is copied here so this migration never changes meaning). A number whose normal form is already another
  customer's of the same business is left as it is: merging two people is not something a migration may decide.
* ``customers`` gets a case-insensitive email index (magic-link login).
* ``booking_items.seq`` records the order the services were chosen in (existing rows get 0, i.e. "unknown order").
"""
import re

import sqlalchemy as sa
from alembic import op

revision = "0014"
down_revision = "0013"
branch_labels = None
depends_on = None

_SEPARATORS = re.compile(r"[\s\-().]")


def _normalise(raw: str, country_code: str) -> str | None:
    """The rule of ``app.core.phone.normalise_phone`` as it stood when this migration was written; ``None`` = unusable."""
    text = _SEPARATORS.sub("", raw.strip())
    international = text.startswith("+")
    digits = text[1:] if international else text
    if not digits.isascii() or not digits.isdigit():
        return None
    if international:
        pass
    elif digits.startswith("00"):
        digits = digits[2:]
    elif digits.startswith("0"):
        digits = country_code + digits[1:]
    elif not digits.startswith(country_code):
        digits = country_code + digits
    if digits.startswith("0") or not 7 <= len(digits) <= 15:
        return None
    return "+" + digits


def upgrade() -> None:
    op.add_column("tenants", sa.Column("phone_country_code", sa.String(3), server_default="60", nullable=False))
    op.add_column("booking_items", sa.Column("seq", sa.Integer(), server_default="0", nullable=False))
    op.create_index("ix_customers_tenant_lower_email", "customers", ["tenant_id", sa.text("lower(email)")], unique=False)

    conn = op.get_bind()
    for tenant_id, country_code in conn.execute(sa.text("SELECT id, phone_country_code FROM tenants")).all():
        # customers is row-level secured (and the table owner is subject to it): look at one business at a time
        conn.execute(sa.text("SELECT set_config('app.tenant_id', :t, true)"), {"t": str(tenant_id)})
        rows = conn.execute(sa.text("SELECT id, phone FROM customers")).all()
        taken = {phone for _, phone in rows}
        for customer_id, phone in rows:
            if phone.startswith("erased-"):
                continue
            normal = _normalise(phone, country_code)
            if normal is None or normal == phone or normal in taken:
                continue
            conn.execute(sa.text("UPDATE customers SET phone = :p WHERE id = :i"), {"p": normal, "i": customer_id})
            taken.discard(phone)
            taken.add(normal)
    conn.execute(sa.text("SELECT set_config('app.tenant_id', '', true)"))


def downgrade() -> None:
    # phone numbers stay in their normalised spelling: the old spelling is not recoverable
    op.drop_index("ix_customers_tenant_lower_email", table_name="customers")
    op.drop_column("booking_items", "seq")
    op.drop_column("tenants", "phone_country_code")
