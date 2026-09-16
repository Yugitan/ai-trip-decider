"""add day to trip_route_stops

多日行程的站点需要知道自己属于第几天。`seq` 仍是整条方案内递增的序号，
所以旧行不需要重新编号 —— server_default 让它们自动落在第 1 天。

Revision ID: 7c1d9a4e5b02
Revises: 217165a7a13b
Create Date: 2026-09-16 13:05:12.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = "7c1d9a4e5b02"
down_revision: str | None = "217165a7a13b"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "trip_route_stops",
        sa.Column("day", sa.Integer(), server_default=sa.text("1"), nullable=False),
    )
    # 约束与 app/db/models.py 保持一致：day 从 1 开始。
    # 不加它的话，一个 0 或负数的 day 会在前端把站点掉到"第 0 天"那一组里，
    # 而查询/断言都看不出问题。
    op.create_check_constraint("ck_stops_day_positive", "trip_route_stops", "day >= 1")


def downgrade() -> None:
    op.drop_constraint("ck_stops_day_positive", "trip_route_stops", type_="check")
    op.drop_column("trip_route_stops", "day")
