"""a notification log row can be ``refused``

Revision ID: 0030
Revises: 0029

When a provider answers that it will never take a message (a number that is not on WhatsApp, a mailbox that does not exist, a recipient who opted out), the log now says
``refused`` and the outbox stops retrying that channel. Before, the message was tried six times over about a quarter of an hour, and every later message about the same booking
waited behind it. This only widens the check on ``status``.

Downgrading turns any ``refused`` row into ``failed`` (the old constraint has no room for it) before restoring the old check.
"""
from alembic import op

revision = "0030"
down_revision = "0029"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_constraint(op.f("ck_notification_log_status"), "notification_log", type_="check")
    op.create_check_constraint(op.f("ck_notification_log_status"), "notification_log", "status IN ('sending','sent','failed','skipped','refused')")


def downgrade() -> None:
    # (the policies are FORCEd, so even the owner sees no rows without a tenant: lift the force for this one statement)
    op.execute("ALTER TABLE notification_log NO FORCE ROW LEVEL SECURITY")
    op.execute("UPDATE notification_log SET status = 'failed' WHERE status = 'refused'")
    op.execute("ALTER TABLE notification_log FORCE ROW LEVEL SECURITY")
    op.drop_constraint(op.f("ck_notification_log_status"), "notification_log", type_="check")
    op.create_check_constraint(op.f("ck_notification_log_status"), "notification_log", "status IN ('sending','sent','failed','skipped')")
