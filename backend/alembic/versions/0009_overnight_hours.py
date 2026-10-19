"""phase 5d: overnight opening hours and shifts (an end before the start means the next day)

Revision ID: 0009
Revises: 0008
"""
from alembic import op

revision = "0009"
down_revision = "0008"
branch_labels = None
depends_on = None


def _secs_after_start(col: str) -> str:
    return f"((EXTRACT(EPOCH FROM ({col} - start_time))::int % 86400) + 86400) % 86400"


BREAK_WITHIN_SHIFT = (
    "(break_start IS NULL AND break_end IS NULL) OR (break_start IS NOT NULL AND break_end IS NOT NULL AND "
    f"{_secs_after_start('break_start')} < {_secs_after_start('break_end')} AND "
    f"{_secs_after_start('break_end')} <= {_secs_after_start('end_time')})"
)


def upgrade() -> None:
    # shifts: end_time <> start_time (an end before the start runs past midnight); the break is measured from the start
    op.drop_constraint(op.f("ck_resource_shifts_span"), "resource_shifts", type_="check")
    op.drop_constraint(op.f("ck_resource_shifts_break_within_shift"), "resource_shifts", type_="check")
    op.create_check_constraint(op.f("ck_resource_shifts_span"), "resource_shifts", "end_time <> start_time")
    op.create_check_constraint(op.f("ck_resource_shifts_break_within_shift"), "resource_shifts", BREAK_WITHIN_SHIFT)
    # fixed-mode slots may end after midnight (last_slot_time before first_slot_time)
    op.drop_constraint(op.f("ck_time_slot_settings_slot_order"), "time_slot_settings", type_="check")


def downgrade() -> None:
    # Only valid while no overnight row exists; the constraints below reject them.
    op.create_check_constraint(op.f("ck_time_slot_settings_slot_order"), "time_slot_settings", "first_slot_time <= last_slot_time")
    op.drop_constraint(op.f("ck_resource_shifts_break_within_shift"), "resource_shifts", type_="check")
    op.drop_constraint(op.f("ck_resource_shifts_span"), "resource_shifts", type_="check")
    op.create_check_constraint(op.f("ck_resource_shifts_span"), "resource_shifts", "end_time > start_time")
    op.create_check_constraint(
        op.f("ck_resource_shifts_break_within_shift"), "resource_shifts",
        "(break_start IS NULL AND break_end IS NULL) OR (break_start IS NOT NULL AND break_end IS NOT NULL AND "
        "break_start < break_end AND break_start >= start_time AND break_end <= end_time)",
    )
