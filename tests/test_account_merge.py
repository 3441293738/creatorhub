from __future__ import annotations

from datetime import datetime, timedelta

from sqlmodel import SQLModel, Session, create_engine, select

from app.account_merge import (
    duplicate_xhs_account_ids,
    reconcile_xhs_accounts,
)
from app.models import (
    AccountRiskState,
    DmMonitorState,
    DouyinAccount,
    PublishTask,
)


def _session(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'accounts.db'}",
        connect_args={"check_same_thread": False},
    )
    SQLModel.metadata.create_all(engine)
    return Session(engine)


def test_xhs_read_and_creator_logins_merge_by_user_id(tmp_path):
    with _session(tmp_path) as session:
        read = DouyinAccount(
            platform="xhs",
            nickname="同一账号",
            sec_uid="user-73",
            douyin_id="red-73",
            storage_state='{"cookies":[{"name":"web_session"}]}',
            profile_dir=str(tmp_path / "profiles" / "read"),
            created_at=datetime.utcnow() - timedelta(days=1),
        )
        creator = DouyinAccount(
            platform="xhs",
            nickname="同一账号",
            sec_uid="user-73",
            douyin_id="red-73",
            creator_storage_state='{"cookies":[{"name":"creator_session"}]}',
            profile_dir=str(tmp_path / "profiles" / "creator"),
        )
        session.add(read)
        session.add(creator)
        session.commit()
        session.refresh(read)
        session.refresh(creator)
        session.add(PublishTask(account_id=creator.id, platform="xhs"))
        session.add(AccountRiskState(account_id=read.id, risk_level=1))
        session.add(AccountRiskState(account_id=creator.id, risk_level=3))
        session.add(DmMonitorState(
            account_id=read.id, platform="xhs", baseline_initialized=True))
        session.add(DmMonitorState(
            account_id=creator.id, platform="xhs",
            last_poll_at=datetime.utcnow()))
        session.commit()

        assert duplicate_xhs_account_ids(session, creator.id) == [read.id, creator.id]
        merges = reconcile_xhs_accounts(session, account_id=creator.id)

        assert len(merges) == 1
        assert merges[0].kept_id == read.id
        assert merges[0].removed_id == creator.id
        merged = session.get(DouyinAccount, read.id)
        assert merged.storage_state == read.storage_state
        assert merged.creator_storage_state == creator.creator_storage_state
        assert session.get(DouyinAccount, creator.id) is None
        task = session.exec(select(PublishTask)).one()
        assert task.account_id == read.id
        risk = session.get(AccountRiskState, read.id)
        assert risk.risk_level == 3
        states = session.exec(select(DmMonitorState)).all()
        assert len(states) == 1
        assert states[0].account_id == read.id
        assert states[0].baseline_initialized is True
        assert states[0].last_poll_at is not None


def test_xhs_accounts_are_not_merged_by_display_name_only(tmp_path):
    with _session(tmp_path) as session:
        session.add(DouyinAccount(
            platform="xhs", nickname="重名", sec_uid="user-a"))
        session.add(DouyinAccount(
            platform="xhs", nickname="重名", sec_uid="user-b"))
        session.commit()

        assert reconcile_xhs_accounts(session) == []
        assert len(session.exec(select(DouyinAccount)).all()) == 2


def test_xhs_red_id_can_bridge_missing_user_id(tmp_path):
    with _session(tmp_path) as session:
        first = DouyinAccount(
            platform="xhs", sec_uid="user-a", douyin_id="Shared-Red")
        second = DouyinAccount(
            platform="xhs", sec_uid="", douyin_id="shared-red",
            creator_storage_state="creator-state")
        session.add(first)
        session.add(second)
        session.commit()
        session.refresh(first)

        reconcile_xhs_accounts(session)

        rows = session.exec(select(DouyinAccount)).all()
        assert len(rows) == 1
        assert rows[0].id == first.id
        assert rows[0].creator_storage_state == "creator-state"
