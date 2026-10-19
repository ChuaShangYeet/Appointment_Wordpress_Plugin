"""a notification log row can be ``sending``

Revision ID: 0027
Revises: 0026

A sign-in code is now written to ``notification_log`` *before* it is handed to the gateway, with the new status ``sending`` (and a ``sent_at``, so it counts against the plan's
message allowance at once). It is settled to ``sent`` or ``failed`` afterwards; a worker killed in between leaves it ``sending`` and it keeps its place, because the message may
have gone out. This only widens the check on ``status``.

Downgrading turns any ``sending`` row into ``failed`` (the old constraint has no room for it) before restoring the old check.
"""
from alembic import op

revision = "0027"
down_revision = "0026"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_constraint(op.f("ck_notification_log_status"), "notification_log", type_="check")
    op.create_check_constraint(op.f("ck_notification_log_status"), "notification_log", "status IN ('sending','sent','failed','skipped')")


def downgrade() -> None:
    # (the policies are FORCEd, so even the owner sees no rows without a tenant: lift the force for this one statement)
    op.execute("ALTER TABLE notification_log NO FORCE ROW LEVEL SECURITY")
    op.execute("UPDATE notification_log SET status = 'failed', error = coalesce(error, 'interrupted before the result was known') WHERE status = 'sending'")
    op.execute("ALTER TABLE notification_log FORCE ROW LEVEL SECURITY")
    op.drop_constraint(op.f("ck_notification_log_status"), "notification_log", type_="check")
    op.create_check_constraint(op.f("ck_notification_log_status"), "notification_log", "status IN ('sent','failed','skipped')")
