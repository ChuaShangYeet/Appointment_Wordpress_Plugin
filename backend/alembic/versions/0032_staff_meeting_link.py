"""staff keep their own meeting room link

Revision ID: 0032
Revises: 0031

``resources.meeting_link``: a staff member's own video room (Google Meet, Zoom, Teams...), given to their online appointments when they are confirmed
(the business brings its own meeting service; nothing here makes rooms). Existing staff have none. Downgrading drops the column; links already copied onto bookings stay.
"""
import sqlalchemy as sa
from alembic import op

revision = "0032"
down_revision = "0031"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("resources", sa.Column("meeting_link", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("resources", "meeting_link")
