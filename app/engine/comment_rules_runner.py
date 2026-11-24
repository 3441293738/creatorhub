"""自动评论规则时段/配额门禁、候选目标发现、试跑预览与评论任务生成。"""
from __future__ import annotations

from datetime import datetime, timedelta
import json
import random
import re
import time
from typing import Any

from sqlalchemy import or_
from sqlmodel import select

from app.ai_copywriting import (
    describe_effective_route,
    get_copywriting_config,
    resolve_runtime_ai,
)
from app.browser import fetch_comments, fetch_creator_comments, fetch_facebook_comments, fetch_facebook_search, fetch_facebook_self_profile, fetch_facebook_user_posts, fetch_ig_comments, fetch_ig_posts, fetch_ig_search, fetch_ks_comments, fetch_ks_videos, fetch_twitter_self_profile, fetch_videos
from app.db import get_session
from app.engine import compose
from app.engine.cadence import bounded_ratio
from app.engine.monitor_helpers import log
from app.models import CommentRule, CommentTask, DouyinAccount
from app.platforms.bilibili import BilibiliClient, parse_bili_aweme
from app.platforms.douyin import parse_comment, parse_creator_comment
from app.platforms.instagram import InstagramClient, parse_ig_aweme
from app.platforms.kuaishou import flatten_ks_comments, parse_ks_comment, parse_ks_feed
from app.platforms.twitter import parse_twitter_aweme
from app.platforms.facebook import parse_facebook_aweme, post_url as fb_post_url
from app.platforms.xhs import XhsApiError, flatten_comments as flatten_xhs_comments, parse_note_brief, parse_comment as parse_xhs_comment
from app.risk import OperationKind, RiskCategory, classify_platform_error

VALID_COMPOSE_MODES = ("template_only", "ai_with_fallback", "ai_only_draft", "ai_only_skip")
VALID_MATCH_MODES = ("all", "any", "all_terms", "regex")
VALID_COMMENT_SCOPES = ("all", "top_only", "reply_only")
_KW_SPLIT = re.compile(r"[,，、;；\r\n]+")
_ACTIVE_QUOTA_STATUSES = ("draft", "pending", "doing", "done", "uncertain")


def parse_keyword_list(raw: Any, fallback_text: str = "") -> list[str]:
    """解析 JSON 数组或中英文逗号/分号/换行分隔的关键词，并按大小写折叠去重。"""
    items: list[str] = []
    if isinstance(raw, list):
        items.extend(str(x) for x in raw)
    elif isinstance(raw, str) and raw.strip():
        s = raw.strip()
        parsed = None
        if s.startswith("["):
            try:
                v = json.loads(s)
                if isinstance(v, list):
                    parsed = [str(x) for x in v]
            except Exception:
                parsed = None
        if parsed is not None:
            items.extend(parsed)
        else:
            items.extend(_KW_SPLIT.split(s))
    if not items and fallback_text and str(fallback_text).strip():
        items.extend(_KW_SPLIT.split(str(fallback_text).strip()))
    cleaned: list[str] = []
    seen: set[str] = set()
    for val in items:
        text = str(val or "").strip()
        if not text:
            continue
        key = text.casefold()
        if key not in seen:
            seen.add(key)
            cleaned.append(text[:80])
    return cleaned[:50]


def effective_compose_mode(rule_or_dict: Any) -> str:
    if isinstance(rule_or_dict, dict):
        cm = str(rule_or_dict.get("compose_mode") or "").strip().lower()
        use_ai = bool(rule_or_dict.get("use_ai"))
    else:
        cm = str(getattr(rule_or_dict, "compose_mode", "") or "").strip().lower()
        use_ai = bool(getattr(rule_or_dict, "use_ai", False))
    if cm in VALID_COMPOSE_MODES:
        return cm
    return "ai_with_fallback" if use_ai else "template_only"


def rule_matches_keywords(
    text: str,
    match_mode: str,
    include_words: list[str],
    exclude_words: list[str],
) -> tuple[bool, str]:
    """返回 (是否通过, 跳过原因码 "" | "skip_kw" | "filter")。"""
    raw = str(text or "")
    folded = raw.casefold()
    if exclude_words and any(w.casefold() in folded for w in exclude_words):
        return False, "skip_kw"
    mode = (match_mode or "any").strip().lower()
    if mode == "all" or not include_words:
        return True, ""
    if mode == "all_terms":
        ok = all(w.casefold() in folded for w in include_words)
        return (True, "") if ok else (False, "filter")
    if mode == "regex":
        for pat in include_words:
            try:
                if re.search(pat, raw, re.IGNORECASE):
                    return True, ""
            except re.error:
                if pat.casefold() in folded:
                    return True, ""
        return False, "filter"
    ok = any(w.casefold() in folded for w in include_words)
    return (True, "") if ok else (False, "filter")


class CommentRuleRunnerMixin:
    # ── 活跃时段(夜间静默)──
    def _in_active_window(self, account_id=None) -> bool:
        """Check active hours in the bound account's persisted timezone."""
        if not self.cfg.risk_control.enabled or not self.cfg.engine.quiet_hours_enabled:
            return True
        account = self._load_account(account_id)
        if account is not None:
            return self.risk._in_active_window(account, datetime.utcnow())
        start = self.cfg.engine.active_hours_start
        end = self.cfg.engine.active_hours_end
        if end <= start:
            return True
        h = (datetime.utcnow() + timedelta(hours=8)).hour   # 东八区(账号默认时区)
        if end <= 24:
            return start <= h < end
        return h >= start or h < (end - 24)                 # 跨零点

    # ── 自动评论:规则生成任务 + 任务执行 ──
    def _today_start(self, account_id=None) -> datetime:
        n = datetime.utcnow()
        account = self._load_account(account_id)
        if account is not None:
            return self.risk._local_day_start_utc(account, n)
        return datetime(n.year, n.month, n.day)

    @staticmethod
    def _hour_ago() -> datetime:
        return datetime.utcnow() - timedelta(hours=1)

    def _acct_today_count(self, s, account_id) -> int:
        """该账号今日已成功发出的评论数(跨所有规则,用于执行前硬性上限)。"""
        if not account_id:
            return 0
        return len(s.exec(select(CommentTask.id)
                          .where(CommentTask.account_id == account_id)
                          .where(CommentTask.status == "done")
                          .where(CommentTask.done_at >= self._today_start(account_id))).all())

    def _acct_today_reserved_count(self, s, account_id) -> int:
        """该账号今日已占用配额（含草稿/排队/执行中/已发送/待确认），防止多轮重复灌队。"""
        if not account_id:
            return 0
        day_start = self._today_start(account_id)
        return len(s.exec(
            select(CommentTask.id)
            .where(CommentTask.account_id == account_id)
            .where(CommentTask.status.in_(_ACTIVE_QUOTA_STATUSES))
            .where(or_(CommentTask.created_at >= day_start, CommentTask.done_at >= day_start))
        ).all())

    def _acct_hour_comment_count(self, account_id) -> int:
        """该账号近一小时已成功发出的评论数(每小时配额,比日上限更贴人类节律)。"""
        if not account_id:
            return 0
        with get_session() as s:
            return len(s.exec(select(CommentTask.id)
                              .where(CommentTask.account_id == account_id)
                              .where(CommentTask.status == "done")
                              .where(CommentTask.done_at >= self._hour_ago())).all())

    def _rule_today_count(self, s, rule_id) -> int:
        rule = s.get(CommentRule, rule_id)
        account_id = rule.account_id if rule else None
        return len(s.exec(select(CommentTask.id)
                          .where(CommentTask.rule_id == rule_id)
                          .where(CommentTask.status == "done")
                          .where(CommentTask.done_at >= self._today_start(account_id))).all())

    def _rule_today_reserved_count(self, s, rule_id, account_id=None) -> int:
        if account_id is None:
            rule = s.get(CommentRule, rule_id)
            account_id = rule.account_id if rule else None
        day_start = self._today_start(account_id)
        return len(s.exec(
            select(CommentTask.id)
            .where(CommentTask.rule_id == rule_id)
            .where(CommentTask.status.in_(_ACTIVE_QUOTA_STATUSES))
            .where(or_(CommentTask.created_at >= day_start, CommentTask.done_at >= day_start))
        ).all())

    def _active_risk_comment_limits(self) -> tuple[int, int, int]:
        """返回风控中心当前生效的评论 (min_gap, hourly_cap, daily_cap)，未开启时均为 0。"""
        rc = self.cfg.risk_control
        if not (rc.enabled and getattr(rc, "write_limits_enabled", True)):
            return 0, 0, 0
        return self.risk._limits(OperationKind.COMMENT)

    def effective_comment_policy(
        self,
        *,
        platform: str = "douyin",
        rule_daily_cap: int = 0,
        rule_min_gap: int = 0,
        rule_require_review: bool = False,
        rule_channel_id: str = "",
        rule_model: str = "",
    ) -> dict[str, Any]:
        """汇总规则、旧版引擎限制、风控中心、小红书全局审核及 AI 配置的最终生效约束。"""
        rc_gap, rc_hourly, rc_daily = self._active_risk_comment_limits()
        legacy_gap = max(0, int(self.cfg.engine.comment_min_gap_seconds or 0))
        legacy_hourly = max(0, int(self.cfg.engine.comment_hourly_cap_per_account or 0))
        legacy_daily = max(0, int(self.cfg.engine.comment_daily_cap_per_account or 0))

        eff_gap = max(1, int(rule_min_gap or 0), legacy_gap, rc_gap)
        acct_daily_candidates = [v for v in (legacy_daily, rc_daily) if v > 0]
        eff_acct_daily = min(acct_daily_candidates) if acct_daily_candidates else 0
        acct_hourly_candidates = [v for v in (legacy_hourly, rc_hourly) if v > 0]
        eff_acct_hourly = min(acct_hourly_candidates) if acct_hourly_candidates else 0

        xhs_manual_only = (platform == "xhs" and self._xhs_comment_write_mode() == "manual")
        xhs_review_forced = (
            platform == "xhs"
            and bool(getattr(self.cfg.engine, "xhs_comment_review_before_publish", True))
        )
        eff_review = bool(rule_require_review) or xhs_review_forced or xhs_manual_only

        ai_cfg = get_copywriting_config()
        ai_route = describe_effective_route(
            ai_cfg, rule_channel_id=rule_channel_id, rule_model=rule_model
        )
        jitter_on = bool(
            self.cfg.risk_control.enabled
            and getattr(self.cfg.risk_control, "jitter_enabled", True)
        )
        quiet_on = bool(
            self.cfg.risk_control.enabled and self.cfg.engine.quiet_hours_enabled
        )
        return {
            "platform": platform,
            "rule_daily_cap": int(rule_daily_cap or 0),
            "account_daily_cap": eff_acct_daily,
            "account_hourly_cap": eff_acct_hourly,
            "effective_min_gap_seconds": eff_gap,
            "risk_write_limits_active": bool(
                self.cfg.risk_control.enabled and getattr(self.cfg.risk_control, "write_limits_enabled", True)
            ),
            "risk_comment_min_gap_seconds": rc_gap,
            "risk_comment_daily_cap": rc_daily,
            "risk_comment_hourly_cap": rc_hourly,
            "legacy_comment_min_gap_seconds": legacy_gap,
            "legacy_comment_daily_cap": legacy_daily,
            "jitter_enabled": jitter_on,
            "comment_jitter": bounded_ratio(self.cfg.engine.comment_jitter) if jitter_on else 0.0,
            "quiet_hours_enabled": quiet_on,
            "active_hours": [self.cfg.engine.active_hours_start, self.cfg.engine.active_hours_end],
            "xhs_manual_only": xhs_manual_only,
            "xhs_review_forced": xhs_review_forced,
            "effective_review_required": eff_review,
            "global_recent_works": int(self.cfg.engine.comment_recent_works or 5),
            "global_recent_days": int(self.cfg.engine.comment_recent_days or 7),
            "ai_enabled": bool(ai_cfg.enabled),
            "ai_route": ai_route,
        }

    def _acct_gap_ok(self, account_id) -> bool:
        """距该账号上一条成功评论是否已超过全局最小间隔(防同账号连发)。"""
        if not account_id:
            return True
        gap = self.cfg.engine.comment_min_gap_seconds
        if gap <= 0:
            return True
        with get_session() as s:
            rows = s.exec(select(CommentTask.done_at)
                          .where(CommentTask.account_id == account_id)
                          .where(CommentTask.status == "done")).all()
        last = max([d for d in rows if d] or [None])
        return last is None or (datetime.utcnow() - last).total_seconds() >= gap

    def _comment_gate_error(self, account_id) -> str:
        """Return the reason a comment write must remain queued."""
        pause_error = self._write_pause_error(account_id)
        if pause_error:
            return pause_error
        if not self._in_active_window(account_id):
            return "当前处于非活跃时段，评论任务已保留在队列"
        hcap = self.cfg.engine.comment_hourly_cap_per_account
        if hcap > 0 and self._acct_hour_comment_count(account_id) >= hcap:
            return "已达到账号每小时评论上限"
        if not self._acct_gap_ok(account_id):
            return "尚未达到账号评论最小间隔"
        decision = self.risk.preflight(account_id, OperationKind.COMMENT)
        if not decision.allowed:
            return decision.reason
        return ""

    async def _process_comment_rules(self):
        due = []
        with get_session() as s:
            rules = s.exec(select(CommentRule).where(CommentRule.enabled == True)).all()  # noqa: E712
            for r in rules:
                if self._periodic_due(r, "comment_rule"):
                    due.append(r.id)
        for rid in due:
            try:
                await self.run_comment_rule(rid)
            except Exception as e:
                log.warning("自动评论规则 %s 生成失败: %s", rid, e)
                self._mark_rule(rid, f"生成失败: {e!r}")

    def _resolve_rule_ai(self, rf: dict[str, Any]) -> tuple[dict[str, Any] | None, str]:
        """解析规则生效的 AI 配置。返回 (ai_dict_or_None, unavailable_reason)。"""
        try:
            ai = resolve_runtime_ai(
                rule_override={
                    "ai_channel_id": rf.get("ai_channel_id", ""),
                    "ai_model": rf.get("ai_model", ""),
                    "ai_preset_id": rf.get("ai_preset_id", ""),
                    "ai_prompt": rf.get("ai_prompt", ""),
                    "ai_max_chars": rf.get("ai_max_chars", 0),
                },
                require_enabled=True,
            )
        except Exception as exc:
            return None, str(exc)
        if ai is None:
            return None, "全局 AI 文案总开关未开启（可在「设置 → AI 文案」开启）"
        if not (ai.get("base_url") and ai.get("model")):
            return None, "所选 AI 模型渠道未填写接口地址或模型 ID"
        return ai, ""

    def _mark_rule(self, rule_id, error: str):
        with get_session() as s:
            r = s.get(CommentRule, rule_id)
            if r:
                r.last_run_at = datetime.utcnow()
                r.last_error = error
                s.add(r); s.commit()

    async def preview_comment_rule(self, rule_id: int, *, limit: int = 8) -> dict:
        """真试跑（Dry-Run 预览）：不落库 CommentTask，也不改写规则运行时间。"""
        return await self._execute_comment_rule(
            rule_id, dry_run=True, preview_limit=max(1, min(20, int(limit or 8)))
        )

    async def run_comment_rule(self, rule_id: int, *, force_draft: bool = False) -> dict:
        """跑一轮规则:发现目标 -> 去重/过滤 -> 生成 CommentTask(错峰排期)。"""
        return await self._execute_comment_rule(rule_id, dry_run=False, force_draft=force_draft)

    async def regenerate_comment_task(self, task_id: int) -> dict:
        """对单条草稿/待发/失败评论任务使用 AI 重新生成一条不同表述的文案。"""
        with get_session() as s:
            t = s.get(CommentTask, task_id)
            if not t:
                return {"ok": False, "error": "任务不存在"}
            if t.status not in ("draft", "pending", "failed"):
                return {"ok": False, "error": f"任务状态为 {t.status}，不可重写"}
            rule = s.get(CommentRule, t.rule_id) if t.rule_id else None
            rf = {
                "platform": t.platform,
                "mode": rule.mode if rule else ("auto_reply" if t.target_comment_id else "auto_comment"),
                "keyword": rule.keyword if rule else "",
                "ai_channel_id": getattr(rule, "ai_channel_id", "") if rule else "",
                "ai_model": getattr(rule, "ai_model", "") if rule else "",
                "ai_preset_id": getattr(rule, "ai_preset_id", "") if rule else "",
                "ai_prompt": getattr(rule, "ai_prompt", "") if rule else "",
                "ai_max_chars": getattr(rule, "ai_max_chars", 0) if rule else 0,
            }
            prev_content = (t.content or "").strip()
            ctx = {
                "source_text": t.target_text or "",
                "work_desc": getattr(t, "work_desc", "") or "",
                "nick": t.target_nick or "",
                "kw": rf["keyword"],
                "platform": t.platform,
                "mode": rf["mode"],
                "recent_replies": [prev_content] if prev_content else [],
            }
        ai, reason = self._resolve_rule_ai(rf)
        if not ai:
            return {"ok": False, "error": reason or "AI 文案尚未启用或配置不完整"}
        try:
            new_text = await compose.generate(ctx, ai)
        except Exception as exc:
            return {"ok": False, "error": str(exc) or exc.__class__.__name__}
        with get_session() as s:
            t = s.get(CommentTask, task_id)
            if not t:
                return {"ok": False, "error": "任务不存在"}
            if t.status not in ("draft", "pending", "failed"):
                return {"ok": False, "error": f"任务状态已变更为 {t.status}"}
            t.content = new_text[:300]
            t.content_source = "ai"
            t.ai_error = ""
            s.add(t); s.commit(); s.refresh(t)
            from app.services import metadata as metadata_service
            return {"ok": True, "task": metadata_service._task_dict(t)}

    async def _execute_comment_rule(
        self,
        rule_id: int,
        *,
        dry_run: bool = False,
        force_draft: bool = False,
        preview_limit: int | None = None,
    ) -> dict:
        with get_session() as s:
            r = s.get(CommentRule, rule_id)
            if not r:
                return {"ok": False, "error": "规则不存在"}
            cm = effective_compose_mode(r)
            include_words = parse_keyword_list(
                getattr(r, "include_keywords", "[]"),
                fallback_text=getattr(r, "reply_filter", ""),
            )
            exclude_words = parse_keyword_list(
                getattr(r, "exclude_keywords", "[]"),
                fallback_text=getattr(r, "skip_keywords", ""),
            )
            match_mode = str(getattr(r, "match_mode", "any") or "any").strip().lower()
            if match_mode not in VALID_MATCH_MODES:
                match_mode = "any"
            comment_scope = str(getattr(r, "comment_scope", "all") or "all").strip().lower()
            if comment_scope not in VALID_COMMENT_SCOPES:
                comment_scope = "all"
            rf = dict(
                platform=r.platform,
                mode=r.mode,
                target_kind=r.target_kind,
                keyword=r.keyword,
                sec_uid=r.sec_uid,
                aweme_id=r.aweme_id,
                xsec_token=r.xsec_token,
                daily_cap=max(0, int(r.daily_cap or 0)),
                min_gap=max(1, int(r.min_gap_seconds or 1)),
                max_per_run=max(1, int(r.max_per_run or 1)),
                account_id=r.account_id,
                compose_mode=cm,
                include_keywords=include_words,
                exclude_keywords=exclude_words,
                match_mode=match_mode,
                comment_scope=comment_scope,
                min_source_length=max(0, int(getattr(r, "min_source_length", 0) or 0)),
                max_source_age_hours=max(0, int(getattr(r, "max_source_age_hours", 0) or 0)),
                recent_works=max(0, int(getattr(r, "recent_works", 0) or 0)),
                recent_days=max(0, int(getattr(r, "recent_days", 0) or 0)),
                require_review=bool(r.require_review),
                ai_channel_id=str(getattr(r, "ai_channel_id", "") or "").strip(),
                ai_model=str(getattr(r, "ai_model", "") or "").strip(),
                ai_preset_id=str(getattr(r, "ai_preset_id", "") or "").strip(),
                ai_prompt=str(getattr(r, "ai_prompt", "") or "").strip(),
                ai_max_chars=max(0, int(getattr(r, "ai_max_chars", 0) or 0)),
            )
            templates = compose.parse_templates(r.templates)
            acc = s.get(DouyinAccount, r.account_id) if r.account_id else None
            rf["account_uid"] = acc.uid if acc else ""
            rf["has_creator"] = bool(acc and acc.creator_storage_state)
            if acc and acc.status == "invalid":
                if not dry_run:
                    self._mark_rule(rule_id, "账号登录态已失效")
                return {"ok": False, "error": "账号登录态已失效"}
            if acc and self._proxy_bad(acc):
                if not dry_run:
                    self._mark_rule(rule_id, "账号代理标记为不可用(proxy bad),已跳过")
                return {"ok": False, "error": "账号代理不可用(proxy bad)"}
            acc_state = acc.storage_state if acc else ""
            acc_proxy = acc.proxy if acc else ""
            acc_sec_uid = acc.sec_uid if acc else ""
            acc_nick = acc.nickname if acc else ""
            if acc:
                identity = self.browser.identity_for(acc)
            else:
                identity, acc_proxy = self._anon_identity_proxy(rf["platform"], session=s)

        if not rf["account_id"] or not acc:
            if not dry_run:
                self._mark_rule(rule_id, "未绑定发评论账号")
            return {"ok": False, "error": "未绑定发评论账号"}
        if rf["compose_mode"] in ("template_only", "ai_with_fallback") and not templates:
            if not dry_run:
                self._mark_rule(rule_id, "未配置文案模板")
            return {"ok": False, "error": "当前文案模式需要至少配置一条本地模板"}
        if not dry_run:
            pause_error = self._write_pause_error(rf["account_id"])
            if pause_error:
                self._mark_rule(rule_id, pause_error)
                return {"ok": False, "error": pause_error}

        policy = self.effective_comment_policy(
            platform=rf["platform"],
            rule_daily_cap=rf["daily_cap"],
            rule_min_gap=rf["min_gap"],
            rule_require_review=rf["require_review"],
            rule_channel_id=rf["ai_channel_id"],
            rule_model=rf["ai_model"],
        )
        xhs_manual_only = bool(policy["xhs_manual_only"])
        review_required = bool(policy["effective_review_required"]) or bool(force_draft)

        wants_ai = rf["compose_mode"] in ("ai_with_fallback", "ai_only_draft", "ai_only_skip")
        ai_cfg_runtime: dict[str, Any] | None = None
        ai_circuit_error = ""
        if wants_ai:
            ai_cfg_runtime, ai_circuit_error = self._resolve_rule_ai(rf)

        read_decision = self.risk.preflight(
            rf["account_id"], OperationKind.READ_HEAVY)
        if not read_decision.allowed:
            return {"ok": False, "error": read_decision.reason,
                    "skipped": True,
                    "next_allowed_at": (read_decision.next_allowed_at.isoformat()
                                        if read_decision.next_allowed_at else None)}
        async with self._operation_guard(
                rf["account_id"], OperationKind.READ_HEAVY,
                fallback_key=f"rule:{rule_id}"):
            read_decision = self.risk.preflight(
                rf["account_id"], OperationKind.READ_HEAVY)
            if not read_decision.allowed:
                return {"ok": False, "error": read_decision.reason,
                        "skipped": True,
                        "next_allowed_at": (
                            read_decision.next_allowed_at.isoformat()
                            if read_decision.next_allowed_at else None)}
            try:
                cands, error = await self._discover_targets(
                    rf, acc_state, acc_proxy, acc_sec_uid, acc_nick, identity)
            except Exception as e:
                self.risk.record_failure(
                    rf["account_id"], OperationKind.READ_HEAVY, e)
                if not dry_run:
                    self._mark_rule(rule_id, f"发现目标失败: {e!r}")
                return {"ok": False, "error": repr(e)}
            if error:
                self.risk.record_failure(
                    rf["account_id"], OperationKind.READ_HEAVY, error)
                category, _signal = classify_platform_error(error)
                error = str(error)
                if category in {
                        RiskCategory.RISK, RiskCategory.AUTH,
                        RiskCategory.NETWORK}:
                    if not dry_run:
                        self._mark_rule(rule_id, f"发现目标失败: {error}")
                    return {
                        "ok": False, "created": 0, "candidates": 0,
                        "error": error, "dry_run": dry_run,
                    }
            else:
                self.risk.record_success(
                    rf["account_id"], OperationKind.READ_HEAVY)

        # ── 过滤 + 去重 + 合成 ──
        created = 0
        preview_items: list[dict[str, Any]] = []
        now_ts = int(time.time())
        age_cutoff = (
            now_ts - rf["max_source_age_hours"] * 3600
            if rf["max_source_age_hours"] > 0 else 0
        )

        with get_session() as s:
            existing = set()
            for row in s.exec(
                select(CommentTask.aweme_id, CommentTask.target_comment_id)
                .where(CommentTask.rule_id == rule_id)
            ).all():
                existing.add((row[0], row[1]))

            # auto_comment 仅按顶层评论去重，避免与 auto_reply 或已取消任务混淆
            acct_commented = set(s.exec(
                select(CommentTask.aweme_id)
                .where(CommentTask.account_id == rf["account_id"])
                .where(CommentTask.target_comment_id == "")
                .where(CommentTask.status.in_(_ACTIVE_QUOTA_STATUSES))
            ).all())

            # 读取近期已生成文案用于 AI 避重
            recent_rows = s.exec(
                select(CommentTask.content)
                .where(CommentTask.rule_id == rule_id)
                .where(CommentTask.content != "")
                .order_by(CommentTask.id.desc())
                .limit(5)
            ).all()
            recent_replies: list[str] = [str(x).strip() for x in reversed(recent_rows) if str(x or "").strip()]

            # 计算本轮可生成上限（0 = 规则级不单独限制；同时计入今日已排队任务防止重复灌队）
            if dry_run and preview_limit is not None:
                remain = min(preview_limit, rf["max_per_run"])
            else:
                remain = rf["max_per_run"]
                if rf["daily_cap"] > 0:
                    remain = min(remain, max(0, rf["daily_cap"] - self._rule_today_reserved_count(s, rule_id, rf["account_id"])))
                acct_cap = int(policy["account_daily_cap"] or 0)
                if acct_cap > 0:
                    remain = min(remain, max(0, acct_cap - self._acct_today_reserved_count(s, rf["account_id"])))

            base = datetime.utcnow()
            gap = max(1, int(policy["effective_min_gap_seconds"] or rf["min_gap"]))
            jitter_on = bool(policy["jitter_enabled"])
            jitter = float(policy["comment_jitter"] or 0.0) if jitter_on else 0.0
            offset = 0.0
            to_rest = random.randint(3, 6)
            skip = {
                "dup": 0, "skip_kw": 0, "filter": 0, "scope": 0,
                "short": 0, "age": 0, "ai_fail": 0, "empty": 0, "cap": 0,
            }
            ai_consecutive_fail = 0

            for c in cands:
                if remain <= 0:
                    skip["cap"] += 1
                    continue
                key = (c["aweme_id"], c.get("target_comment_id", ""))
                if key in existing:
                    skip["dup"] += 1
                    continue
                if rf["mode"] == "auto_comment" and c["aweme_id"] in acct_commented:
                    skip["dup"] += 1
                    continue

                if rf["mode"] == "auto_reply" and rf["comment_scope"] != "all":
                    is_reply = bool(c.get("is_reply"))
                    if rf["comment_scope"] == "top_only" and is_reply:
                        skip["scope"] += 1
                        continue
                    if rf["comment_scope"] == "reply_only" and not is_reply:
                        skip["scope"] += 1
                        continue

                text_blob = (c.get("source_text", "") or "").strip()
                if rf["min_source_length"] > 0 and len(text_blob) < rf["min_source_length"]:
                    skip["short"] += 1
                    continue

                ct = int(c.get("create_time") or 0)
                if ct > 100_000_000_000:
                    ct //= 1000
                if age_cutoff and ct and ct < age_cutoff:
                    skip["age"] += 1
                    continue

                kw_ok, kw_reason = rule_matches_keywords(
                    text_blob, rf["match_mode"], rf["include_keywords"], rf["exclude_keywords"]
                )
                if not kw_ok:
                    skip[kw_reason] += 1
                    continue

                gctx = dict(c.get("ctx", {}))
                gctx.setdefault("work_title", (c.get("work_desc") or "")[:60])
                gctx.update(
                    source_text=text_blob,
                    work_desc=c.get("work_desc") or "",
                    platform=rf["platform"],
                    mode=rf["mode"],
                    recent_replies=list(recent_replies[-5:]),
                )

                content = ""
                content_source = "template"
                ai_err_msg = ""
                force_item_draft = False

                if wants_ai:
                    if ai_cfg_runtime and not ai_circuit_error:
                        try:
                            content = await compose.generate(gctx, ai_cfg_runtime)
                            content_source = "ai"
                            ai_consecutive_fail = 0
                        except compose.AiGenerateError as e:
                            ai_err_msg = str(e)
                            log.info("AI 文案生成失败(rule=%s, cat=%s): %s", rule_id, e.category, ai_err_msg)
                            if e.fatal:
                                ai_circuit_error = ai_err_msg
                            else:
                                ai_consecutive_fail += 1
                                if ai_consecutive_fail >= 2:
                                    ai_circuit_error = f"连续失败已熔断：{ai_err_msg}"
                        except Exception as e:
                            ai_err_msg = str(e) or e.__class__.__name__
                            log.info("AI 文案生成异常(rule=%s): %s", rule_id, ai_err_msg)
                            ai_consecutive_fail += 1
                            if ai_consecutive_fail >= 2:
                                ai_circuit_error = f"连续异常已熔断：{ai_err_msg}"
                    else:
                        ai_err_msg = ai_circuit_error or "AI 文案未就绪"

                    if not content:
                        if rf["compose_mode"] == "ai_with_fallback":
                            content = compose.render(templates, gctx)
                            content_source = "ai_fallback" if content else ""
                        elif rf["compose_mode"] == "ai_only_draft":
                            content = ""
                            content_source = "ai_empty_draft"
                            force_item_draft = True
                        elif rf["compose_mode"] == "ai_only_skip":
                            skip["ai_fail"] += 1
                            continue
                else:
                    content = compose.render(templates, gctx)
                    content_source = "template"

                if not content and not force_item_draft:
                    skip["empty"] += 1
                    continue

                if content:
                    recent_replies.append(content)

                if jitter_on and jitter > 0:
                    step = gap * random.uniform(1.0, 1.0 + jitter)
                    to_rest -= 1
                    if to_rest <= 0:
                        step += gap * random.uniform(1.5, 3.0)
                        to_rest = random.randint(3, 6)
                else:
                    step = float(gap)
                offset += step
                sched = base + timedelta(seconds=offset)

                status = "draft" if (review_required or xhs_manual_only or force_item_draft) else "pending"
                if dry_run:
                    preview_items.append({
                        "aweme_id": c["aweme_id"],
                        "work_desc": (c.get("work_desc") or "")[:200],
                        "target_comment_id": c.get("target_comment_id", ""),
                        "target_nick": c.get("target_nick", ""),
                        "source_text": text_blob[:200],
                        "content": content,
                        "content_source": content_source,
                        "ai_error": ai_err_msg[:240],
                        "would_status": status,
                    })
                else:
                    s.add(CommentTask(
                        platform=rf["platform"],
                        rule_id=rule_id,
                        account_id=rf["account_id"],
                        aweme_id=c["aweme_id"],
                        xsec_token=c.get("xsec_token", ""),
                        target_comment_id=c.get("target_comment_id", ""),
                        target_nick=c.get("target_nick", ""),
                        target_text=text_blob[:200],
                        work_desc=(c.get("work_desc") or "")[:200],
                        content=content,
                        content_source=content_source,
                        ai_error=ai_err_msg[:240],
                        method="manual" if xhs_manual_only else "",
                        scheduled_at=sched,
                        status=status,
                    ))
                existing.add(key)
                if rf["mode"] == "auto_comment":
                    acct_commented.add(c["aweme_id"])
                created += 1
                remain -= 1

            # 跳过原因汇总
            parts = []
            if skip["filter"]:
                parts.append(f'{skip["filter"]}条未命中包含词')
            if skip["skip_kw"]:
                parts.append(f'{skip["skip_kw"]}条命中排除词')
            if skip["scope"]:
                parts.append(f'{skip["scope"]}条不符合评论层级')
            if skip["short"]:
                parts.append(f'{skip["short"]}条字数过短')
            if skip["age"]:
                parts.append(f'{skip["age"]}条超出时效范围')
            if skip["dup"]:
                parts.append(f'{skip["dup"]}条已生成过/已评论过')
            if skip["ai_fail"]:
                parts.append(f'{skip["ai_fail"]}条 AI 生成失败跳过')
            if skip["empty"]:
                parts.append(f'{skip["empty"]}条文案渲染为空')
            if skip["cap"]:
                parts.append(f'{skip["cap"]}条超出本轮上限/每日上限')
            note = "；".join(parts)

            if not dry_run:
                r = s.get(CommentRule, rule_id)
                if r:
                    r.last_run_at = datetime.utcnow()
                    if error:
                        r.last_error = error
                    elif not cands:
                        r.last_error = "本轮未发现可评论目标"
                    elif created == 0:
                        r.last_error = f"发现{len(cands)}个目标但生成0条:{note or '全部被排除'}"
                    elif ai_circuit_error:
                        r.last_error = f"AI 降级提示：{ai_circuit_error}"[:240]
                    else:
                        r.last_error = ""
                    s.add(r)
                s.commit()

        log.info("自动评论规则 %s(%s):发现 %s 候选,命中 %s 条 (skip=%s)",
                 rule_id, "dry_run" if dry_run else "run", len(cands), created, skip)
        result = {
            "ok": True,
            "dry_run": dry_run,
            "created": created,
            "candidates": len(cands),
            "skipped": skip,
            "note": note,
            "error": error,
            "ai_warning": ai_circuit_error,
            "review": review_required or xhs_manual_only,
            "manual_only": xhs_manual_only,
            "effective_policy": policy,
        }
        if dry_run:
            result["items"] = preview_items
        return result

    @staticmethod
    def _is_self_comment(raw: dict, acc_nick: str, acc_sec_uid: str = "",
                         acc_uid: str = "") -> bool:
        """Prefer stable account ids over nickname-only self-comment filtering."""
        if not isinstance(raw, dict):
            return False
        user = (raw.get("user") or raw.get("commenter")
                or raw.get("user_info") or {})
        if not isinstance(user, dict):
            user = {}
        mine = {str(v).strip().lower() for v in (acc_uid, acc_sec_uid) if str(v or "").strip()}
        seen = set()
        for obj in (raw, user):
            for key in (
                "uid", "user_id", "userId", "sec_uid", "secUid",
                "user_sec_uid", "user_uid", "author_id", "authorId",
            ):
                value = obj.get(key)
                if value not in (None, ""):
                    seen.add(str(value).strip().lower())
        if mine and mine.intersection(seen):
            return True
        nick = (raw.get("user_nickname") or raw.get("nickname")
                or raw.get("author_name") or raw.get("authorName")
                or user.get("nickname") or user.get("nick_name")
                or user.get("name") or "")
        return bool(acc_nick and nick == acc_nick)

    async def _discover_targets(self, rf, state, proxy, acc_sec_uid, acc_nick, identity):
        """按规则模式发现可评论目标。返回 (candidates, error)。
        candidate: {aweme_id, xsec_token, target_comment_id, target_nick, ctx, source_text, work_desc, is_reply, create_time}"""
        platform, mode, kind = rf["platform"], rf["mode"], rf["target_kind"]
        recent_works = max(1, int(rf.get("recent_works") or self.cfg.engine.comment_recent_works or 5))
        recent_days = max(0, int(rf.get("recent_days") or self.cfg.engine.comment_recent_days or 7))
        acc_uid = str(rf.get("account_uid") or "")
        cands: list = []
        # ── 小红书:签名直连 ──
        if platform == "xhs":
            client = self._xhs_client(identity, state, proxy)
            if client is None:
                return [], "账号登录态缺少 a1,请重新扫码登录"
            if mode == "auto_comment":
                if kind == "keyword":
                    raw = await client.search_notes(rf["keyword"])
                else:   # creator
                    d = await client.notes_by_creator(rf["sec_uid"], xsec_token=rf["xsec_token"])
                    raw = d.get("notes") or []
                for it in raw[:recent_works]:
                    b = parse_note_brief(it)
                    if not b:
                        continue
                    title = b.get("title", "") or ""
                    author = b.get("author", "") or b.get("user_nickname", "") or ""
                    cands.append({"aweme_id": b["note_id"],
                                  "xsec_token": b.get("xsec_token", ""),
                                  "target_comment_id": "", "target_nick": author,
                                  "ctx": {"kw": rf["keyword"], "author": author, "work_title": title[:60]},
                                  "source_text": title, "work_desc": title,
                                  "is_reply": False, "create_time": int(b.get("create_time") or 0)})
            else:   # auto_reply:回复自己作品的评论
                notes = []
                if kind == "work" and rf["aweme_id"]:
                    notes = [{"note_id": rf["aweme_id"], "xsec_token": rf["xsec_token"], "title": ""}]
                else:
                    d = await client.notes_by_creator(acc_sec_uid, xsec_token=rf["xsec_token"])
                    for it in (d.get("notes") or [])[:recent_works]:
                        b = parse_note_brief(it)
                        if b:
                            notes.append({"note_id": b["note_id"],
                                          "xsec_token": b.get("xsec_token", ""),
                                          "title": b.get("title", "") or ""})
                for nt in notes:
                    try:
                        d = await client.note_comments(nt["note_id"], xsec_token=nt["xsec_token"])
                        rawc = d.get("comments") or []
                    except XhsApiError as e:
                        return [], e
                    except Exception as exc:
                        category, _signal = classify_platform_error(exc)
                        if category in {
                                RiskCategory.RISK, RiskCategory.AUTH,
                                RiskCategory.NETWORK}:
                            return [], exc
                        continue
                    for rc in flatten_xhs_comments(rawc):
                        c = parse_xhs_comment(rc)
                        if not c or not c.get("comment_id"):
                            continue
                        if self._is_self_comment(rc, acc_nick, acc_sec_uid, acc_uid):
                            continue
                        cands.append({"aweme_id": nt["note_id"],
                                      "xsec_token": nt["xsec_token"],
                                      "target_comment_id": c["comment_id"],
                                      "target_nick": c.get("user_nickname", ""),
                                      "ctx": {"nick": c.get("user_nickname", ""),
                                              "work_title": (nt.get("title") or "")[:60]},
                                      "source_text": c.get("text", ""),
                                      "work_desc": nt.get("title", ""),
                                      "is_reply": bool(c.get("reply_to")),
                                      "create_time": int(c.get("create_time") or 0)})
            return cands, ""
        # ── 快手:浏览器自动化(拦截 GraphQL,与抖音同范式)──
        if platform == "kuaishou":
            if mode == "auto_comment":
                if kind == "keyword":
                    return [], "快手暂不支持关键词发现,请用「创作者」模式指定博主"
                items, author_info, err = await fetch_ks_videos(
                    self.browser, identity, rf["sec_uid"], set(), max_scrolls=4,
                    block_media=self.cfg.engine.block_media_resources)
                author_name = (author_info.get("nickname", "") if isinstance(author_info, dict) else "")
                for feed in items[:recent_works]:
                    aw = parse_ks_feed(feed)
                    if aw:
                        cands.append({"aweme_id": aw.aweme_id, "xsec_token": "",
                                      "target_comment_id": "", "target_nick": author_name,
                                      "ctx": {"author": author_name, "work_title": (aw.desc or "")[:60]},
                                      "source_text": aw.desc, "work_desc": aw.desc,
                                      "is_reply": False, "create_time": int(aw.create_time or 0)})
                return cands, err
            # auto_reply 快手:回复自己作品评论
            works = []
            if rf["target_kind"] == "work" and rf["aweme_id"]:
                works = [(rf["aweme_id"], "")]
            else:
                items, _a, err = await fetch_ks_videos(
                    self.browser, identity, acc_sec_uid, set(), max_scrolls=4,
                    block_media=self.cfg.engine.block_media_resources)
                if err and classify_platform_error(err)[0] in {
                        RiskCategory.RISK, RiskCategory.AUTH, RiskCategory.NETWORK}:
                    return [], err
                cutoff = int(time.time()) - recent_days * 86400 if recent_days else 0
                for feed in items[:recent_works]:
                    aw = parse_ks_feed(feed)
                    if aw and (not cutoff or not aw.create_time or aw.create_time >= cutoff):
                        works.append((aw.aweme_id, aw.desc))
            for pid, wdesc in works:
                raw, comment_error = await fetch_ks_comments(
                    self.browser, identity, pid, set(),
                    max_scrolls=self.cfg.engine.comment_max_scrolls,
                    block_media=self.cfg.engine.block_media_resources)
                if comment_error and classify_platform_error(comment_error)[0] in {
                        RiskCategory.RISK, RiskCategory.AUTH, RiskCategory.NETWORK}:
                    return [], comment_error
                for rc in flatten_ks_comments(raw):
                    c = parse_ks_comment(rc)
                    if not c or not c.get("comment_id"):
                        continue
                    if self._is_self_comment(rc, acc_nick, acc_sec_uid, acc_uid):
                        continue
                    cands.append({"aweme_id": pid, "xsec_token": "",
                                  "target_comment_id": c["comment_id"],
                                  "target_nick": c.get("user_nickname", ""),
                                  "ctx": {"nick": c.get("user_nickname", ""),
                                          "work_title": (wdesc or "")[:60]},
                                  "source_text": c.get("text", ""),
                                  "work_desc": wdesc or "",
                                  "is_reply": bool(c.get("reply_to")),
                                  "create_time": int(c.get("create_time") or 0)})
            return cands, ""
        # ── 哔哩哔哩: WBI 搜索/空间 + 创作中心评论管理直连 ──
        if platform == "bilibili":
            bcli = BilibiliClient(
                state,
                getattr(identity, "ua", "") or self.cfg.engine.user_agent,
                proxy=proxy,
                timeout=self.cfg.engine.request_timeout_seconds,
            )
            async with bcli.session_scope():
                if mode == "auto_comment":
                    if kind == "keyword":
                        rows, _, err = await bcli.search_videos(
                            rf["keyword"], page=1, page_size=20, order="pubdate"
                        )
                    else:
                        rows, _, err = await bcli.fetch_user_videos(
                            rf["sec_uid"], pn=1, ps=20, order="pubdate"
                        )
                    for r in rows[:recent_works]:
                        aw = parse_bili_aweme(r, require_media=False)
                        if aw:
                            author = str(r.get("author") or "")
                            cands.append({
                                "aweme_id": aw.aweme_id,
                                "xsec_token": "",
                                "target_comment_id": "",
                                "target_nick": author,
                                "ctx": {"kw": rf["keyword"] if kind == "keyword" else "",
                                        "author": author, "work_title": (aw.desc or "")[:60]},
                                "source_text": aw.desc,
                                "work_desc": aw.desc,
                                "is_reply": False,
                                "create_time": int(aw.create_time or 0),
                            })
                    return cands, err

                # auto_reply: 回复自己稿件收到的评论
                if rf["target_kind"] == "work" and rf["aweme_id"]:
                    rows, err = await bcli.fetch_comments(
                        rf["aweme_id"], max_count=40, include_replies=True
                    )
                    for c in rows:
                        if not c.get("comment_id"):
                            continue
                        if self._is_self_comment(c, acc_nick, acc_sec_uid, acc_uid):
                            continue
                        cands.append({
                            "aweme_id": rf["aweme_id"],
                            "xsec_token": "",
                            "target_comment_id": c["comment_id"],
                            "target_nick": c.get("user_nickname", ""),
                            "ctx": {"nick": c.get("user_nickname", "")},
                            "source_text": c.get("text", ""),
                            "work_desc": str(c.get("work_title") or ""),
                            "is_reply": bool(c.get("reply_to")),
                            "create_time": int(c.get("create_time") or 0),
                        })
                    return cands, err

                up_rows, _, up_err = await bcli.fetch_up_fulllist_comments(pn=1, ps=30)
                comment_cutoff = int(time.time()) - recent_days * 86400 if recent_days else 0
                for c in up_rows:
                    bv = str(c.get("aweme_id") or "")
                    cid = str(c.get("comment_id") or "")
                    if not bv or not cid:
                        continue
                    ct = int(c.get("create_time") or 0)
                    if comment_cutoff and ct and ct < comment_cutoff:
                        continue
                    if self._is_self_comment(c, acc_nick, acc_sec_uid, acc_uid):
                        continue
                    wtitle = str(c.get("work_title") or c.get("title") or "")
                    cands.append({
                        "aweme_id": bv,
                        "xsec_token": "",
                        "target_comment_id": cid,
                        "target_nick": c.get("user_nickname", ""),
                        "ctx": {"nick": c.get("user_nickname", ""), "work_title": wtitle[:60]},
                        "source_text": c.get("text", ""),
                        "work_desc": wtitle,
                        "is_reply": bool(c.get("reply_to")),
                        "create_time": ct,
                    })
                return cands, up_err
        # ── Instagram: Mobile/Web API 搜索/时间线 + stream_comments 直连 + 浏览器回退 ──
        if platform == "instagram":
            imode = str(getattr(self.cfg.engine, "instagram_read_mode", "hybrid") or "hybrid").lower()
            icli = InstagramClient(
                state,
                getattr(identity, "ua", "") or self.cfg.engine.user_agent,
                proxy=proxy,
                timeout=self.cfg.engine.request_timeout_seconds,
                account_seed=(
                    (getattr(identity, "fp_seed", "") or getattr(identity, "profile_dir", ""))
                    if identity is not None
                    else ""
                ),
                timezone_id=(
                    getattr(identity, "timezone_id", "Asia/Shanghai")
                    if identity is not None
                    else "Asia/Shanghai"
                ),
            )
            async with icli.session_scope():
                if mode == "auto_comment":
                    rows: list[dict] = []
                    err = ""
                    if imode in {"api", "hybrid"}:
                        if kind == "keyword":
                            rows, err = await icli.search_posts(rf["keyword"], max_pages=2)
                        else:
                            rows, _next, _more, err = await icli.fetch_user_feed(rf["sec_uid"])
                    if (
                        imode == "browser"
                        or (imode == "hybrid" and not rows and err)
                    ) and identity is not None:
                        if kind == "keyword":
                            b_rows, b_err = await fetch_ig_search(
                                self.browser, identity, rf["keyword"], max_scrolls=2
                            )
                        else:
                            b_rows, _a, b_err = await fetch_ig_posts(
                                self.browser, identity, rf["sec_uid"], set(), max_scrolls=2
                            )
                        if b_rows:
                            rows, err = b_rows, ""
                        elif imode == "browser":
                            err = b_err
                    for r in rows[:recent_works]:
                        aw = parse_ig_aweme(r, require_media=False)
                        if aw:
                            author = getattr(aw, "author_name", "") or getattr(aw, "author_unique_id", "") or ""
                            cands.append({
                                "aweme_id": aw.aweme_id,
                                "xsec_token": "",
                                "target_comment_id": "",
                                "target_nick": author,
                                "ctx": {"kw": rf["keyword"] if kind == "keyword" else "",
                                        "author": author, "work_title": (aw.desc or "")[:60]},
                                "source_text": aw.desc,
                                "work_desc": aw.desc,
                                "is_reply": False,
                                "create_time": int(aw.create_time or 0),
                            })
                    return cands, err

                # auto_reply: 回复自己帖子/Reels 收到的评论
                self_uid_str = str(icli.self_uid or acc_sec_uid or "")
                works_to_check: list[tuple[str, str]] = []
                err = ""
                if rf["target_kind"] == "work" and rf["aweme_id"]:
                    works_to_check = [(rf["aweme_id"], "")]
                else:
                    target_self = acc_sec_uid or icli.self_uid
                    feed_rows: list[dict] = []
                    if imode in {"api", "hybrid"} and target_self:
                        feed_rows, _next, _more, err = await icli.fetch_user_feed(target_self)
                    if (
                        imode == "browser"
                        or (imode == "hybrid" and not feed_rows and err)
                    ) and identity is not None and target_self:
                        b_rows, _a, b_err = await fetch_ig_posts(
                            self.browser, identity, target_self, set(), max_scrolls=2
                        )
                        if b_rows:
                            feed_rows, err = b_rows, ""
                        elif imode == "browser":
                            err = b_err
                    cutoff = int(time.time()) - recent_days * 86400 if recent_days else 0
                    for r in feed_rows:
                        aw = parse_ig_aweme(r, require_media=False)
                        if aw and (not cutoff or not aw.create_time or aw.create_time >= cutoff):
                            works_to_check.append((aw.aweme_id, aw.desc or ""))
                        if len(works_to_check) >= recent_works:
                            break

                comment_cutoff = int(time.time()) - recent_days * 86400 if recent_days else 0
                for work_code, wdesc in works_to_check:
                    c_rows: list[dict] = []
                    c_err = ""
                    if imode in {"api", "hybrid"}:
                        c_rows, c_err = await icli.fetch_comments(
                            work_code, max_count=40, include_replies=True
                        )
                    if (
                        imode == "browser"
                        or (imode == "hybrid" and not c_rows and c_err)
                    ) and identity is not None:
                        b_c_rows, b_c_err = await fetch_ig_comments(
                            self.browser, identity, work_code, max_count=40
                        )
                        if b_c_rows:
                            c_rows, c_err = b_c_rows, ""
                        elif imode == "browser":
                            c_err = b_c_err
                    if c_err and not c_rows:
                        err = err or c_err
                        continue
                    for c in c_rows:
                        cid = str(c.get("comment_id") or "")
                        if not cid:
                            continue
                        ct = int(c.get("create_time") or 0)
                        if comment_cutoff and ct and ct < comment_cutoff:
                            continue
                        if self._is_self_comment(c, acc_nick, acc_sec_uid, self_uid_str or acc_uid):
                            continue
                        cands.append({
                            "aweme_id": work_code,
                            "xsec_token": "",
                            "target_comment_id": cid,
                            "target_nick": c.get("user_nickname", ""),
                            "ctx": {"nick": c.get("user_nickname", ""), "work_title": wdesc[:60]},
                            "source_text": c.get("text", ""),
                            "work_desc": wdesc,
                            "is_reply": bool(c.get("reply_to")),
                            "create_time": ct,
                        })
                return cands, ("" if cands else err)
        # ── Twitter: 隔离浏览器拦截 SearchTimeline / UserTweets / TweetDetail 发现待评推文与回复 ──
        if platform == "twitter":
            if identity is None:
                return [], "缺少 Twitter 账号浏览器隔离身份"
            if mode == "auto_comment":
                if kind == "keyword":
                    rows, err = await self._tw_read_search(
                        identity,
                        rf["keyword"],
                        latest=True,
                        max_pages=2,
                    )
                else:
                    rows, _a, err = await self._tw_read_user_tweets(
                        identity,
                        rf["sec_uid"],
                        set(),
                        max_pages=2,
                    )
                if err == "empty":
                    err = ""
                for r in rows[:recent_works]:
                    aw = parse_twitter_aweme(r, skip_retweets=(kind != "keyword"))
                    if aw:
                        author = aw.author_name or aw.author_unique_id
                        cands.append({
                            "aweme_id": aw.aweme_id,
                            "xsec_token": "",
                            "target_comment_id": "",
                            "target_nick": author,
                            "ctx": {"kw": rf["keyword"] if kind == "keyword" else "",
                                    "author": author, "work_title": (aw.desc or "")[:60]},
                            "source_text": aw.desc,
                            "work_desc": aw.desc,
                            "is_reply": False,
                            "create_time": int(aw.create_time or 0),
                        })
                return cands, err

            # auto_reply: 回复自己推文下的新评论
            works_to_check_tw: list[tuple[str, str]] = []
            err = ""
            if rf["target_kind"] == "work" and rf["aweme_id"]:
                works_to_check_tw = [(rf["aweme_id"], "")]
            else:
                target_self = acc_sec_uid or acc_uid
                if not target_self:
                    prof, p_err = await fetch_twitter_self_profile(self.browser, identity)
                    if p_err and not prof:
                        return [], p_err
                    target_self = str(prof.get("unique_id") or prof.get("sec_uid") or "")
                if target_self:
                    feed_rows, _a, err = await self._tw_read_user_tweets(
                        identity,
                        target_self,
                        set(),
                        max_pages=2,
                    )
                    if err == "empty":
                        err = ""
                    cutoff = int(time.time()) - recent_days * 86400 if recent_days else 0
                    for r in feed_rows:
                        aw = parse_twitter_aweme(r, skip_retweets=True)
                        if aw and (not cutoff or not aw.create_time or aw.create_time >= cutoff):
                            works_to_check_tw.append((aw.aweme_id, aw.desc or ""))
                        if len(works_to_check_tw) >= recent_works:
                            break

            comment_cutoff = int(time.time()) - recent_days * 86400 if recent_days else 0
            for tid, wdesc in works_to_check_tw:
                c_rows, c_err, _ = await self._tw_read_comments(
                    identity,
                    tid,
                    max_count=40,
                    include_replies=True,
                )
                if c_err and c_err != "empty" and not c_rows:
                    err = err or c_err
                    continue
                for c in c_rows:
                    cid = str(c.get("comment_id") or "")
                    if not cid:
                        continue
                    ct = int(c.get("create_time") or 0)
                    if comment_cutoff and ct and ct < comment_cutoff:
                        continue
                    if self._is_self_comment(c, acc_nick, acc_sec_uid, acc_uid):
                        continue
                    cands.append({
                        "aweme_id": tid,
                        "xsec_token": "",
                        "target_comment_id": cid,
                        "target_nick": c.get("user_nickname", ""),
                        "ctx": {"nick": c.get("user_nickname", ""), "work_title": wdesc[:60]},
                        "source_text": c.get("text", ""),
                        "work_desc": wdesc,
                        "is_reply": bool(c.get("reply_to")),
                        "create_time": ct,
                    })
            return cands, ("" if cands else err)
        # ── Facebook: 协议优先（SSR + 动态 GraphQL）+ 隔离浏览器回退 ──
        if platform == "facebook":
            fb_mode = str(getattr(self.cfg.engine, "facebook_read_mode", "hybrid") or "hybrid").lower()
            if identity is None and (fb_mode == "browser" or not state):
                return [], "缺少 Facebook 账号浏览器隔离身份"
            if mode == "auto_comment":
                if kind == "keyword":
                    rows, err = await self._fb_read_search(
                        identity,
                        rf["keyword"],
                        recent=True,
                        max_pages=3,
                        max_items=max(20, recent_works * 2),
                        state=state,
                        proxy=proxy,
                    )
                else:
                    rows, _a, err = await self._fb_read_user_posts(
                        identity,
                        rf["sec_uid"],
                        set(),
                        max_pages=3,
                        max_items=max(20, recent_works * 2),
                        state=state,
                        proxy=proxy,
                    )
                if err in {"empty", "api_empty_search", "api_empty_timeline"}:
                    err = ""
                for r in rows[:recent_works]:
                    aw = parse_facebook_aweme(r)
                    if aw:
                        author = aw.author_name or aw.author_unique_id
                        cands.append({
                            "aweme_id": aw.source_url or aw.aweme_id,
                            "xsec_token": "",
                            "target_comment_id": "",
                            "target_nick": author,
                            "ctx": {"kw": rf["keyword"] if kind == "keyword" else "",
                                    "author": author, "work_title": (aw.desc or "")[:60]},
                            "source_text": aw.desc,
                            "work_desc": aw.desc,
                            "is_reply": False,
                            "create_time": int(aw.create_time or 0),
                        })
                return cands, err

            # auto_reply: 回复自己 Facebook 帖子下的新评论
            works_to_check_fb: list[tuple[str, str, str]] = []
            err = ""
            if rf["target_kind"] == "work" and rf["aweme_id"]:
                wid = rf["aweme_id"]
                works_to_check_fb = [(wid, wid if str(wid).startswith("http") else fb_post_url(wid), "")]
            else:
                target_self = acc_sec_uid or acc_uid
                if not target_self and state and fb_mode in {"api", "hybrid"}:
                    fcli_self = self._make_facebook_client(identity, state=state, proxy=proxy)
                    if fcli_self.has_login_cookies:
                        async with fcli_self.session_scope():
                            prof, p_err = await fcli_self.fetch_self_profile()
                        if p_err.startswith(("logged_out:", "verification_required:")):
                            return [], p_err
                        if prof:
                            target_self = str(prof.get("unique_id") or prof.get("sec_uid") or prof.get("uid") or "")
                if not target_self and identity is not None:
                    prof, p_err = await fetch_facebook_self_profile(self.browser, identity)
                    if p_err and not prof:
                        return [], p_err
                    target_self = str(prof.get("unique_id") or prof.get("sec_uid") or prof.get("uid") or "")
                if target_self:
                    feed_rows, _a, err = await self._fb_read_user_posts(
                        identity,
                        target_self,
                        set(),
                        max_pages=3,
                        max_items=max(20, recent_works * 2),
                        state=state,
                        proxy=proxy,
                    )
                    if err in {"empty", "api_empty_timeline"}:
                        err = ""
                    cutoff = int(time.time()) - recent_days * 86400 if recent_days else 0
                    for r in feed_rows:
                        aw = parse_facebook_aweme(r)
                        if aw and (not cutoff or not aw.create_time or aw.create_time >= cutoff):
                            works_to_check_fb.append((aw.aweme_id, aw.source_url or fb_post_url(aw.aweme_id), aw.desc or ""))
                        if len(works_to_check_fb) >= recent_works:
                            break
            comment_cutoff = int(time.time()) - recent_days * 86400 if recent_days else 0
            for pid, purl, wdesc in works_to_check_fb:
                c_rows, c_err, _ = await self._fb_read_comments(
                    identity,
                    purl,
                    max_pages=4,
                    max_count=40,
                    include_replies=True,
                    state=state,
                    proxy=proxy,
                )
                if c_err and c_err not in {"empty", "api_empty_comments"} and not c_rows:
                    err = err or c_err
                    continue
                for c in c_rows:
                    cid = str(c.get("comment_id") or "")
                    if not cid:
                        continue
                    ct = int(c.get("create_time") or 0)
                    if comment_cutoff and ct and ct < comment_cutoff:
                        continue
                    if self._is_self_comment(c, acc_nick, acc_sec_uid, acc_uid):
                        continue
                    cands.append({
                        "aweme_id": purl,
                        "xsec_token": "",
                        "target_comment_id": cid,
                        "target_nick": c.get("user_nickname", ""),
                        "ctx": {"nick": c.get("user_nickname", ""), "work_title": wdesc[:60]},
                        "source_text": c.get("text", ""),
                        "work_desc": wdesc,
                        "is_reply": bool(c.get("reply_to")),
                        "create_time": ct,
                    })
            return cands, ("" if cands else err)
        # ── 抖音:浏览器自动化(发现仍用拦截抓取)──
        if mode == "auto_comment":
            if kind == "keyword":
                return [], "抖音暂不支持关键词发现,请用「创作者」模式指定博主"
            items, author_info, err = await fetch_videos(
                self.browser, identity, rf["sec_uid"], set(), max_scrolls=4,
                block_media=self.cfg.engine.block_media_resources)
            author_name = (author_info.get("nickname", "") if isinstance(author_info, dict) else "")
            for it in items[:recent_works]:
                aid = str(it.get("aweme_id") or "")
                desc = it.get("desc", "") or ""
                if aid:
                    cands.append({"aweme_id": aid, "xsec_token": "",
                                  "target_comment_id": "", "target_nick": author_name,
                                  "ctx": {"author": author_name, "work_title": desc[:60]},
                                  "source_text": desc, "work_desc": desc,
                                  "is_reply": False, "create_time": int(it.get("create_time") or 0)})
            return cands, err
        # auto_reply 抖音:回复自己作品评论
        if rf.get("has_creator"):
            raw, creator_error = await fetch_creator_comments(
                self.browser, identity, set(),
                page_url=self.cfg.engine.creator_comment_url,
                max_scrolls=max(1, min(self.cfg.engine.comment_max_scrolls, 4)),
                block_media=self.cfg.engine.block_media_resources)
            selected_works = set()
            comment_cutoff = int(time.time()) - recent_days * 86400 if recent_days else 0
            for rc in raw:
                c = parse_creator_comment(rc)
                if not c or not c.get("comment_id") or not c.get("aweme_id"):
                    continue
                created_at = int(c.get("create_time") or 0)
                if created_at > 100_000_000_000:
                    created_at //= 1000
                if comment_cutoff and created_at and created_at < comment_cutoff:
                    continue
                if kind == "work" and c["aweme_id"] != rf["aweme_id"]:
                    continue
                if self._is_self_comment(rc, acc_nick, acc_sec_uid, acc_uid):
                    continue
                if kind != "work" and c["aweme_id"] not in selected_works:
                    if len(selected_works) >= recent_works:
                        continue
                    selected_works.add(c["aweme_id"])
                wdesc = str(rc.get("item_title") or rc.get("aweme_desc") or rc.get("title") or "")
                cands.append({"aweme_id": c["aweme_id"], "xsec_token": "",
                              "target_comment_id": c["comment_id"],
                              "target_nick": c.get("user_nickname", ""),
                              "ctx": {"nick": c.get("user_nickname", ""), "work_title": wdesc[:60]},
                              "source_text": c.get("text", ""),
                              "work_desc": wdesc,
                              "is_reply": bool(c.get("reply_to")),
                              "create_time": created_at})
            return cands, creator_error
        works = []
        if rf["target_kind"] == "work" and rf["aweme_id"]:
            works = [(rf["aweme_id"], "")]
        else:
            items, _a, err = await fetch_videos(
                self.browser, identity, acc_sec_uid, set(), max_scrolls=4,
                block_media=self.cfg.engine.block_media_resources)
            if err and classify_platform_error(err)[0] in {
                    RiskCategory.RISK, RiskCategory.AUTH, RiskCategory.NETWORK}:
                return [], err
            cutoff = int(time.time()) - recent_days * 86400 if recent_days else 0
            for it in items[:recent_works]:
                aid = str(it.get("aweme_id") or "")
                create_time = int(it.get("create_time") or 0)
                if aid and (not cutoff or not create_time or create_time >= cutoff):
                    works.append((aid, it.get("desc", "")))
        for aid, wdesc in works:
            raw, comment_error = await fetch_comments(
                self.browser, identity, aid, set(),
                max_scrolls=self.cfg.engine.comment_max_scrolls,
                block_media=self.cfg.engine.block_media_resources)
            if comment_error and classify_platform_error(comment_error)[0] in {
                    RiskCategory.RISK, RiskCategory.AUTH, RiskCategory.NETWORK}:
                return [], comment_error
            for rc in raw:
                c = parse_comment(rc)
                if not c or not c.get("comment_id"):
                    continue
                if self._is_self_comment(rc, acc_nick, acc_sec_uid, acc_uid):
                    continue
                cands.append({"aweme_id": aid, "xsec_token": "",
                              "target_comment_id": c["comment_id"],
                              "target_nick": c.get("user_nickname", ""),
                              "ctx": {"nick": c.get("user_nickname", ""), "work_title": (wdesc or "")[:60]},
                              "source_text": c.get("text", ""),
                              "work_desc": wdesc or "",
                              "is_reply": bool(c.get("reply_to")),
                              "create_time": int(c.get("create_time") or 0)})
        return cands, ""

    async def _process_comment_tasks(self):
        now = datetime.utcnow()
        due = []
        with get_session() as s:
            tasks = s.exec(select(CommentTask).where(CommentTask.status == "pending")).all()
            for t in tasks:
                if self._task_due(t, now):
                    due.append((t.id, t.account_id))
        seen_acct = set()
        for tid, aid in due:
            # 同一轮每账号最多执行一条,且尊重全局最小间隔 + 每小时配额(其余下轮再发)
            if aid in seen_acct or self._comment_gate_error(aid):
                continue
            seen_acct.add(aid)
            try:
                await self.execute_comment_task(tid)
            except Exception as e:
                log.warning("评论任务 %s 执行异常: %s", tid, e)
