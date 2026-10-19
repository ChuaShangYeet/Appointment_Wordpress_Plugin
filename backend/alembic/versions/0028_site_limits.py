"""free plan: one website - site, domain and key limits, the domain-change cooldown, and the plugin's reported address

Revision ID: 0028
Revises: 0027

``plans`` gains ``max_sites``, ``max_domains``, ``max_live_keys_per_kind`` (limits: ``NULL`` = none, ``0`` = none at all) and ``domain_change_days`` (days that must pass
between two changes of a site's address; ``NULL`` = no cooldown). ``free`` gets 1, 1, 2 and 30; ``paid`` and every other plan are left at ``NULL`` for all four, so no business
that exists now is limited by this.

``sites`` gains ``domain_changed_at`` (when the address was last changed, from which the cooldown counts), and ``last_site_url`` / ``last_site_url_at`` (the address the
WordPress plugin reported at token exchange: a signal for the operator). Nothing is revoked or rewritten: a business that already has more than a plan allows keeps it.
"""
import sqlalchemy as sa
from alembic import op

revision = "0028"
down_revision = "0027"
branch_labels = None
depends_on = None

COLUMNS = ("max_sites", "max_domains", "max_live_keys_per_kind", "domain_change_days")


def upgrade() -> None:
    for column in COLUMNS:
        op.add_column("plans", sa.Column(column, sa.Integer(), nullable=True))
        op.create_check_constraint(op.f(f"ck_plans_{column}"), "plans", f"{column} IS NULL OR {column} >= 0")
    op.execute("UPDATE plans SET max_sites = 1, max_domains = 1, max_live_keys_per_kind = 2, domain_change_days = 30 WHERE code = 'free'")
    op.add_column("sites", sa.Column("domain_changed_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("sites", sa.Column("last_site_url", sa.String(length=300), nullable=True))
    op.add_column("sites", sa.Column("last_site_url_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    for column in ("last_site_url_at", "last_site_url", "domain_changed_at"):
        op.drop_column("sites", column)
    for column in reversed(COLUMNS):
        op.drop_constraint(op.f(f"ck_plans_{column}"), "plans", type_="check")
        op.drop_column("plans", column)
