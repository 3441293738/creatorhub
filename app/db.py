"""数据库初始化与会话。含 SQLite 轻量自动迁移(为已有表补缺失列)。"""
from __future__ import annotations

from sqlalchemy import event, func, inspect, select, text
from sqlmodel import SQLModel, Session, create_engine

from .models import AccountIdReservation, DouyinAccount

_engine = None


@event.listens_for(DouyinAccount, "before_insert")
def _reserve_account_id(_mapper, connection, account):
    """Allocate inside the account transaction, never from a deleted row ID."""
    table = AccountIdReservation.__table__
    if account.id is None:
        result = connection.execute(table.insert().values())
        account.id = int(result.inserted_primary_key[0])
    else:
        # Explicit IDs are used by imports/fixtures. Keep subsequent automatic
        # allocations above them as well. SQLite serializes these writes.
        connection.execute(table.insert().prefix_with("OR IGNORE").values(id=account.id))


def _seed_account_id_watermark(engine):
    """Additive migration: also reserve IDs left in orphaned historical rows."""
    highest = 0
    with engine.begin() as connection:
        if connection.execute(select(AccountIdReservation.id).limit(1)).first() is not None:
            # Once seeded, every ORM insert reserves its ID transactionally.
            # Avoid scanning large historical tables on every service restart.
            return
        for table in SQLModel.metadata.tables.values():
            column = (table.c.id if table.name == DouyinAccount.__tablename__
                      else table.c.get("account_id"))
            if column is not None:
                highest = max(highest, int(connection.execute(
                    select(func.max(column))).scalar() or 0))
        if highest:
            connection.execute(AccountIdReservation.__table__.insert()
                               .prefix_with("OR IGNORE").values(id=highest))


def _auto_migrate(engine):
    """为已存在的表补上模型里新增的列(SQLite 友好,仅 ADD COLUMN)。"""
    insp = inspect(engine)
    for table in SQLModel.metadata.tables.values():
        if not insp.has_table(table.name):
            continue
        existing = {c["name"] for c in insp.get_columns(table.name)}
        for col in table.columns:
            if col.name in existing:
                continue
            coltype = col.type.compile(engine.dialect)
            ddl = f'ALTER TABLE "{table.name}" ADD COLUMN "{col.name}" {coltype}'
            # 模型若给了标量默认值,作为 SQL DEFAULT 写入 —— SQLite 会用它回填已有行,
            # 避免新列在旧数据上为 NULL(例如 platform 列需回填为 'douyin')。
            scalar = (getattr(col.default, "arg", None)
                      if col.default is not None and getattr(col.default, "is_scalar", False)
                      else None)
            if table.name == "publishtask" and col.name == "scheduled_at_is_utc":
                # A crash between ADD COLUMN and data migration must not label
                # old local-time values as UTC. New ORM rows explicitly use True.
                ddl += " DEFAULT 0"
            elif isinstance(scalar, bool):
                ddl += f" DEFAULT {1 if scalar else 0}"
            elif isinstance(scalar, str):
                ddl += " DEFAULT '" + scalar.replace("'", "''") + "'"
            elif isinstance(scalar, (int, float)):
                ddl += f" DEFAULT {scalar}"
            elif not col.nullable:
                ddl += " DEFAULT ''"
            with engine.begin() as conn:
                conn.execute(text(ddl))


def init_db(db_path: str):
    global _engine
    _engine = create_engine(
        f"sqlite:///{db_path}",
        connect_args={"check_same_thread": False},
    )
    inspector = inspect(_engine)
    legacy_schedules = inspector.has_table("publishtask") and (
        "scheduled_at_is_utc" not in {
            column["name"] for column in inspector.get_columns("publishtask")})
    SQLModel.metadata.create_all(_engine)
    _auto_migrate(_engine)
    with _engine.begin() as connection:
        connection.execute(text("""
            UPDATE publishtask SET scheduled_at_is_utc = 1
            WHERE scheduled_at IS NULL AND COALESCE(scheduled_at_is_utc, 0) = 0
        """))
        # Existing user-entered dates had no zone; risk deferrals were already
        # UTC. Preserve the raw date and request confirmation rather than
        # shifting a pending publication into the past during an upgrade.
        if legacy_schedules:
            connection.execute(text("""
                UPDATE publishtask SET scheduled_at_is_utc = 1
                WHERE next_allowed_at IS NOT NULL
                   OR COALESCE(blocked_reason, '') != ''
                   OR COALESCE(error, '') LIKE '服务重启%'
            """))
        # Repeat the conservative hold on startup so a previously interrupted
        # migration completes before any scheduler can consume legacy dates.
        connection.execute(text("""
            UPDATE publishtask SET status = 'draft',
                error = '旧预约未记录时区，请编辑并确认发布时间后再入队'
            WHERE COALESCE(scheduled_at_is_utc, 0) = 0
              AND scheduled_at IS NOT NULL AND status = 'pending'
        """))
    _seed_account_id_watermark(_engine)
    return _engine


def get_session() -> Session:
    assert _engine is not None, "init_db() 未调用"
    return Session(_engine)
