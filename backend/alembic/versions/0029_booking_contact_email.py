"""a booking keeps its own contact email; a customer's email can be confirmed

Revision ID: 0029
Revises: 0028

Anyone could book with another person's phone number and their own email, and the customer record (keyed by phone) took the new email; the emailed sign-in link then
signed the attacker in as that customer. So an email typed into a public booking no longer belongs to the customer record:

* ``bookings.contact_email``: the email given with *that* booking (lower-cased). Messages about the booking go there, and an emailed sign-in link shows the bookings made with
  that address and nothing else. Existing bookings get their customer's current email, so nobody's messages stop.
* ``customers.email_verified_at``: set when the person who signed in with the phone confirmed the email through a link sent to it. Only a confirmed email signs in as the
  whole customer. Nothing is backfilled: every email held today is unconfirmed (it may have been typed by anyone), so those customers use their phone for the full view.

The index serves the lookup behind the emailed sign-in link. Downgrading drops both columns and the index.
"""
import sqlalchemy as sa
from alembic import op

revision = "0029"
down_revision = "0028"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("bookings", sa.Column("contact_email", sa.String(length=254), nullable=True))
    op.add_column("customers", sa.Column("email_verified_at", sa.DateTime(timezone=True), nullable=True))
    # (the policies are FORCEd, so even the owner sees no rows without a tenant: lift the force for this one statement)
    op.execute("ALTER TABLE bookings NO FORCE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE customers NO FORCE ROW LEVEL SECURITY")
    op.execute(
        "UPDATE bookings SET contact_email = lower(trim(customers.email)) FROM customers "
        "WHERE customers.id = bookings.customer_id AND customers.email IS NOT NULL AND customers.erased_at IS NULL"
    )
    op.execute("ALTER TABLE customers FORCE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE bookings FORCE ROW LEVEL SECURITY")
    op.create_index("ix_bookings_tenant_lower_contact_email", "bookings", ["tenant_id", sa.text("lower(contact_email)")])


def downgrade() -> None:
    op.drop_index("ix_bookings_tenant_lower_contact_email", table_name="bookings")
    op.drop_column("customers", "email_verified_at")
    op.drop_column("bookings", "contact_email")
