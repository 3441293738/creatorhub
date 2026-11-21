"""自动评论规则时段/配额门禁、候选目标发现与评论任务生成。"""
from __future__ import annotations

from datetime import datetime, timedelta
import random
import time

from sqlmodel import select

from app.browser import fetch_comments, fetch_creator_comments, fetch_ig_comments, fetch_ig_posts, fetch_ig_search, fetch_ks_comments, fetch_ks_videos, fetch_twitter_self_profile, fetch_videos
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
from app.platforms.xhs import XhsApiError, flatten_comments as flatten_xhs_comments, parse_note_brief, parse_comment as parse_xhs_comment
from app.risk import OperationKind, RiskCategory, classify_platform_error
from app.settings import get_setting


class CommentRuleRunnerMixin:
    # ── 活跃时段(夜间静默)──
    def _in_active_window(self, account_id=None) -> bool:
        """Check active hours in the bound account's persisted timezone."""
        if not self.cfg.engine.quiet_hours_enabled:
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
        """该账号今日已成功发出的评论数(跨所有规则,用于全局每日上限)。"""
        if not account_id:
            return 0
        return len(s.exec(select(CommentTask.id)
                          .where(CommentTask.account_id == account_id)
                          .where(CommentTask.status == "done")
                          .where(CommentTask.done_at >= self._today_start(account_id))).all())

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
        """Return the reason a comment write must remain queued.

        This is deliberately checked again inside the account lock.  The
        scheduler check is only an optimization; API-triggered ``run-now``
        and concurrent callers must go through the same gate.
        """
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

    def _ai_settings(self):
        """读全局 AI 文案设置;未启用返回 None(引擎据此决定是否调大模型)。"""
        if get_setting("ai_enabled", "0") != "1":
            return None
        return {
            "base_url": get_setting("ai_base_url", ""),
            "api_key": get_setting("ai_api_key", ""),
            "model": get_setting("ai_model", ""),
            "prompt": get_setting("ai_prompt", ""),
            "temperature": get_setting("ai_temperature", "0.9"),
        }

    def _mark_rule(self, rule_id, error: str):
        with get_session() as s:
            r = s.get(CommentRule, rule_id)
            if r:
                r.last_run_at = datetime.utcnow()
                r.last_error = error
                s.add(r); s.commit()

    async def run_comment_rule(self, rule_id: int) -> dict:
        """跑一轮规则:发现目标 -> 去重/过滤 -> 生成 CommentTask(错峰排期)。"""
        with get_session() as s:
            r = s.get(CommentRule, rule_id)
            if not r:
                return {"ok": False, "error": "规则不存在"}
            rf = dict(platform=r.platform, mode=r.mode, target_kind=r.target_kind,
                       keyword=r.keyword, sec_uid=r.sec_uid, aweme_id=r.aweme_id,
                       xsec_token=r.xsec_token, daily_cap=r.daily_cap,
                       min_gap=r.min_gap_seconds, max_per_run=r.max_per_run,
                       account_id=r.account_id, reply_filter=(r.reply_filter or "").strip(),
                       skip_keywords=r.skip_keywords or "",
                       require_review=bool(r.require_review))
            templates = compose.parse_templates(r.templates)
            use_ai = bool(r.use_ai)
            acc = s.get(DouyinAccount, r.account_id) if r.account_id else None
            rf["account_uid"] = acc.uid if acc else ""
            rf["has_creator"] = bool(acc and acc.creator_storage_state)
            if acc and acc.status == "invalid":
                self._mark_rule(rule_id, "账号登录态已失效")
                return {"ok": False, "error": "account_invalid"}
            if acc and self._proxy_bad(acc):
                self._mark_rule(rule_id, "账号代理标记为不可用(proxy bad),已跳过")
                return {"ok": False, "error": "proxy bad"}
            acc_state = acc.storage_state if acc else ""
            acc_proxy = acc.proxy if acc else ""
            acc_sec_uid = acc.sec_uid if acc else ""
            acc_nick = acc.nickname if acc else ""
            identity = self.browser.identity_for(acc) if acc else self.browser.anon_identity()

        if not rf["account_id"] or not acc:
            self._mark_rule(rule_id, "未绑定发评论账号")
            return {"ok": False, "error": "未绑定账号"}
        if not templates:
            self._mark_rule(rule_id, "未配置文案模板")
            return {"ok": False, "error": "未配置文案模板"}
        pause_error = self._write_pause_error(rf["account_id"])
        if pause_error:
            self._mark_rule(rule_id, pause_error)
            return {"ok": False, "error": pause_error}

        xhs_manual_only = (rf["platform"] == "xhs"
                           and self._xhs_comment_write_mode() == "manual")
        xhs_review_required = (rf["platform"] == "xhs"
                               and bool(getattr(
                                   self.cfg.engine,
                                   "xhs_comment_review_before_publish",
                                   True)))
        review_required = rf["require_review"] or xhs_review_required

        skip_words = [w.strip() for w in rf["skip_keywords"].split(",") if w.strip()]
        ai = self._ai_settings() if use_ai else None

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
                    self._mark_rule(rule_id, f"发现目标失败: {error}")
                    return {
                        "ok": False, "created": 0, "candidates": 0,
                        "error": error,
                    }
            else:
                self.risk.record_success(
                    rf["account_id"], OperationKind.READ_HEAVY)

        # 过滤 + 去重 + 生成
        created = 0
        with get_session() as s:
            existing = set()
            for row in s.exec(select(CommentTask.aweme_id, CommentTask.target_comment_id)
                              .where(CommentTask.rule_id == rule_id)).all():
                existing.add((row[0], row[1]))
            # 单列 select:exec().all() 直接返回标量(同 known= 查询的写法),勿用 (a,) 解包
            acct_commented = set(s.exec(
                select(CommentTask.aweme_id)
                .where(CommentTask.account_id == rf["account_id"])).all())
            remain = min(rf["max_per_run"],
                         max(0, rf["daily_cap"] - self._rule_today_count(s, rule_id)))
            cap = self.cfg.engine.comment_daily_cap_per_account
            if cap > 0:
                remain = min(remain, max(0, cap - self._acct_today_count(s, rf["account_id"])))

            base = datetime.utcnow()
            gap = max(1, rf["min_gap"], self.cfg.engine.comment_min_gap_seconds)
            jitter = bounded_ratio(self.cfg.engine.comment_jitter)
            offset = 0.0
            to_rest = random.randint(3, 6)   # 突发+休息:连发几条后插一段长歇,别匀速排队
            skip = {"dup": 0, "skip_kw": 0, "filter": 0, "empty": 0, "cap": 0}
            for c in cands:
                if remain <= 0:
                    skip["cap"] += 1
                    continue
                key = (c["aweme_id"], c.get("target_comment_id", ""))
                if key in existing:
                    skip["dup"] += 1
                    continue
                # auto_comment:同账号不在同一作品下重复评论
                if rf["mode"] == "auto_comment" and c["aweme_id"] in acct_commented:
                    skip["dup"] += 1
                    continue
                text_blob = (c.get("source_text", "") or "")
                if skip_words and any(w in text_blob for w in skip_words):
                    skip["skip_kw"] += 1
                    continue
                if rf["mode"] == "auto_reply" and rf["reply_filter"] \
                        and rf["reply_filter"] not in text_blob:
                    skip["filter"] += 1
                    continue
                content = ""
                if ai:   # 优先大模型生成,失败回退模板库
                    try:
                        gctx = dict(c.get("ctx", {}))
                        gctx.update(source_text=c.get("source_text", ""),
                                    platform=rf["platform"], mode=rf["mode"])
                        content = await compose.generate(gctx, ai)
                    except Exception as e:
                        log.info("AI 文案生成失败,回退模板: %s", e)
                        content = ""
                if not content:
                    content = compose.render(templates, c.get("ctx", {}))
                if not content:
                    skip["empty"] += 1
                    continue
                step = gap * random.uniform(1.0, 1.0 + jitter) if jitter else gap
                to_rest -= 1
                if to_rest <= 0:                 # 一簇发完,插一段 3~8 倍 gap 的长歇再继续
                    step += gap * random.uniform(3, 8)
                    to_rest = random.randint(3, 6)
                offset += step
                sched = base + timedelta(seconds=offset)
                # 小红书默认先生成草稿,人工通过后由队列自动发布;
                # manual 模式则始终只保留草稿,不调用签名直连评论接口。
                status = "draft" if (review_required or xhs_manual_only) else "pending"
                s.add(CommentTask(
                    platform=rf["platform"], rule_id=rule_id, account_id=rf["account_id"],
                    aweme_id=c["aweme_id"], xsec_token=c.get("xsec_token", ""),
                    target_comment_id=c.get("target_comment_id", ""),
                    target_nick=c.get("target_nick", ""),
                    target_text=(c.get("source_text", "") or "")[:200],
                    content=content,
                    method="manual" if xhs_manual_only else "",
                    scheduled_at=sched, status=status))
                existing.add(key)
                acct_commented.add(c["aweme_id"])
                created += 1

            # 跳过原因汇总(让"发现N个却生成0条"能解释清楚)
            parts = []
            if skip["filter"]:
                parts.append(f'{skip["filter"]}条不含回复过滤词「{rf["reply_filter"]}」')
            if skip["skip_kw"]:
                parts.append(f'{skip["skip_kw"]}条命中跳过词')
            if skip["dup"]:
                parts.append(f'{skip["dup"]}条已生成过/已评论过')
            if skip["empty"]:
                parts.append(f'{skip["empty"]}条文案渲染为空')
            if skip["cap"]:
                parts.append(f'{skip["cap"]}条超出本轮上限/每日上限')
            note = ";".join(parts)

            r = s.get(CommentRule, rule_id)
            if r:
                r.last_run_at = datetime.utcnow()
                if error:
                    r.last_error = error
                elif not cands:
                    r.last_error = "本轮未发现可评论目标"
                elif created == 0:
                    r.last_error = f"发现{len(cands)}个目标但生成0条:{note or '全部被排除'}"
                else:
                    r.last_error = ""
                s.add(r)
            s.commit()
        log.info("自动评论规则 %s:发现 %s 候选,生成 %s 条任务 (skip=%s)",
                 rule_id, len(cands), created, skip)
        return {"ok": True, "created": created, "candidates": len(cands),
                "skipped": skip, "note": note, "error": error,
                "review": review_required or xhs_manual_only,
                "manual_only": xhs_manual_only}

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
        mine = {str(v).strip() for v in (acc_uid, acc_sec_uid) if str(v or "").strip()}
        seen = set()
        for obj in (raw, user):
            for key in ("uid", "user_id", "userId", "sec_uid", "secUid"):
                value = obj.get(key)
                if value not in (None, ""):
                    seen.add(str(value).strip())
        if mine and mine.intersection(seen):
            return True
        nick = (raw.get("user_nickname") or raw.get("nickname")
                or user.get("nickname") or user.get("name") or "")
        return bool(acc_nick and nick == acc_nick)

    async def _discover_targets(self, rf, state, proxy, acc_sec_uid, acc_nick, identity):
        """按规则模式发现可评论目标。返回 (candidates, error)。
        candidate: {aweme_id, xsec_token, target_comment_id, target_nick, ctx, source_text}"""
        platform, mode, kind = rf["platform"], rf["mode"], rf["target_kind"]
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
                for it in raw:
                    b = parse_note_brief(it)
                    if not b:
                        continue
                    cands.append({"aweme_id": b["note_id"],
                                  "xsec_token": b.get("xsec_token", ""),
                                  "target_comment_id": "", "target_nick": "",
                                  "ctx": {"kw": rf["keyword"]},
                                  "source_text": b.get("title", "")})
            else:   # auto_reply:回复自己作品的评论
                notes = []
                if kind == "work" and rf["aweme_id"]:
                    notes = [{"note_id": rf["aweme_id"], "xsec_token": rf["xsec_token"]}]
                else:
                    d = await client.notes_by_creator(acc_sec_uid, xsec_token=rf["xsec_token"])
                    for it in (d.get("notes") or [])[:self.cfg.engine.comment_recent_works]:
                        b = parse_note_brief(it)
                        if b:
                            notes.append({"note_id": b["note_id"],
                                          "xsec_token": b.get("xsec_token", "")})
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
                        if c.get("user_nickname") and c["user_nickname"] == acc_nick:
                            continue   # 不回复自己
                        cands.append({"aweme_id": nt["note_id"],
                                      "xsec_token": nt["xsec_token"],
                                      "target_comment_id": c["comment_id"],
                                      "target_nick": c.get("user_nickname", ""),
                                      "ctx": {"nick": c.get("user_nickname", "")},
                                      "source_text": c.get("text", "")})
            return cands, ""
        # ── 快手:浏览器自动化(拦截 GraphQL,与抖音同范式)──
        if platform == "kuaishou":
            if mode == "auto_comment":
                if kind == "keyword":
                    return [], "快手暂不支持关键词发现,请用「创作者」模式指定博主"
                items, _author, err = await fetch_ks_videos(
                    self.browser, identity, rf["sec_uid"], set(), max_scrolls=4,
                    block_media=self.cfg.engine.block_media_resources)
                for feed in items[:self.cfg.engine.comment_recent_works]:
                    aw = parse_ks_feed(feed)
                    if aw:
                        cands.append({"aweme_id": aw.aweme_id, "xsec_token": "",
                                      "target_comment_id": "", "target_nick": "",
                                      "ctx": {}, "source_text": aw.desc})
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
                cutoff = int(time.time()) - max(0, self.cfg.engine.comment_recent_days) * 86400
                for feed in items[:self.cfg.engine.comment_recent_works]:
                    aw = parse_ks_feed(feed)
                    if aw and (not cutoff or not aw.create_time or aw.create_time >= cutoff):
                        works.append((aw.aweme_id, aw.desc))
            for pid, _desc in works:
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
                    if c.get("user_nickname") and c["user_nickname"] == acc_nick:
                        continue
                    cands.append({"aweme_id": pid, "xsec_token": "",
                                  "target_comment_id": c["comment_id"],
                                  "target_nick": c.get("user_nickname", ""),
                                  "ctx": {"nick": c.get("user_nickname", "")},
                                  "source_text": c.get("text", "")})
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
                    for r in rows[: self.cfg.engine.comment_recent_works]:
                        aw = parse_bili_aweme(r, require_media=False)
                        if aw:
                            cands.append({
                                "aweme_id": aw.aweme_id,
                                "xsec_token": "",
                                "target_comment_id": "",
                                "target_nick": "",
                                "ctx": {},
                                "source_text": aw.desc,
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
                        if (c.get("user_sec_uid") and c["user_sec_uid"] == acc_sec_uid) or (
                            c.get("user_nickname") and c["user_nickname"] == acc_nick
                        ):
                            continue
                        cands.append({
                            "aweme_id": rf["aweme_id"],
                            "xsec_token": "",
                            "target_comment_id": c["comment_id"],
                            "target_nick": c.get("user_nickname", ""),
                            "ctx": {"nick": c.get("user_nickname", "")},
                            "source_text": c.get("text", ""),
                        })
                    return cands, err

                up_rows, _, up_err = await bcli.fetch_up_fulllist_comments(pn=1, ps=30)
                comment_cutoff = int(time.time()) - max(0, self.cfg.engine.comment_recent_days) * 86400
                for c in up_rows:
                    bv = str(c.get("aweme_id") or "")
                    cid = str(c.get("comment_id") or "")
                    if not bv or not cid:
                        continue
                    ct = int(c.get("create_time") or 0)
                    if comment_cutoff and ct and ct < comment_cutoff:
                        continue
                    if (c.get("user_sec_uid") and c["user_sec_uid"] == acc_sec_uid) or (
                        c.get("user_nickname") and c["user_nickname"] == acc_nick
                    ):
                        continue
                    cands.append({
                        "aweme_id": bv,
                        "xsec_token": "",
                        "target_comment_id": cid,
                        "target_nick": c.get("user_nickname", ""),
                        "ctx": {"nick": c.get("user_nickname", "")},
                        "source_text": c.get("text", ""),
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
                    for r in rows[: self.cfg.engine.comment_recent_works]:
                        aw = parse_ig_aweme(r, require_media=False)
                        if aw:
                            cands.append({
                                "aweme_id": aw.aweme_id,
                                "xsec_token": "",
                                "target_comment_id": "",
                                "target_nick": "",
                                "ctx": {"kw": rf["keyword"]} if kind == "keyword" else {},
                                "source_text": aw.desc,
                            })
                    return cands, err

                # auto_reply: 回复自己帖子/Reels 收到的评论
                self_uid_str = str(icli.self_uid or acc_sec_uid or "")
                works_to_check: list[str] = []
                err = ""
                if rf["target_kind"] == "work" and rf["aweme_id"]:
                    works_to_check = [rf["aweme_id"]]
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
                    cutoff = int(time.time()) - max(0, self.cfg.engine.comment_recent_days) * 86400
                    for r in feed_rows:
                        aw = parse_ig_aweme(r, require_media=False)
                        if aw and (not cutoff or not aw.create_time or aw.create_time >= cutoff):
                            works_to_check.append(aw.aweme_id)
                        if len(works_to_check) >= self.cfg.engine.comment_recent_works:
                            break

                comment_cutoff = int(time.time()) - max(0, self.cfg.engine.comment_recent_days) * 86400
                for work_code in works_to_check:
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
                        if (
                            (self_uid_str and str(c.get("user_uid") or "") == self_uid_str)
                            or (acc_sec_uid and str(c.get("user_sec_uid") or "") == acc_sec_uid)
                            or (acc_nick and c.get("user_nickname") == acc_nick)
                        ):
                            continue
                        cands.append({
                            "aweme_id": work_code,
                            "xsec_token": "",
                            "target_comment_id": cid,
                            "target_nick": c.get("user_nickname", ""),
                            "ctx": {"nick": c.get("user_nickname", "")},
                            "source_text": c.get("text", ""),
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
                for r in rows[: self.cfg.engine.comment_recent_works]:
                    aw = parse_twitter_aweme(r, skip_retweets=(kind != "keyword"))
                    if aw:
                        cands.append({
                            "aweme_id": aw.aweme_id,
                            "xsec_token": "",
                            "target_comment_id": "",
                            "target_nick": aw.author_name or aw.author_unique_id,
                            "ctx": {"kw": rf["keyword"]} if kind == "keyword" else {},
                            "source_text": aw.desc,
                        })
                return cands, err

            # auto_reply: 回复自己推文下的新评论
            works_to_check: list[str] = []
            err = ""
            if rf["target_kind"] == "work" and rf["aweme_id"]:
                works_to_check = [rf["aweme_id"]]
            else:
                target_self = acc_sec_uid or rf.get("account_uid", "")
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
                    cutoff = int(time.time()) - max(0, self.cfg.engine.comment_recent_days) * 86400
                    for r in feed_rows:
                        aw = parse_twitter_aweme(r, skip_retweets=True)
                        if aw and (not cutoff or not aw.create_time or aw.create_time >= cutoff):
                            works_to_check.append(aw.aweme_id)
                        if len(works_to_check) >= self.cfg.engine.comment_recent_works:
                            break

            comment_cutoff = int(time.time()) - max(0, self.cfg.engine.comment_recent_days) * 86400
            for tid in works_to_check:
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
                    c_sec = str(c.get("user_sec_uid") or "").lower()
                    if (
                        (acc_sec_uid and c_sec == str(acc_sec_uid).lower())
                        or (acc_nick and c.get("user_nickname") == acc_nick)
                    ):
                        continue
                    cands.append({
                        "aweme_id": tid,
                        "xsec_token": "",
                        "target_comment_id": cid,
                        "target_nick": c.get("user_nickname", ""),
                        "ctx": {"nick": c.get("user_nickname", "")},
                        "source_text": c.get("text", ""),
                    })
            return cands, ("" if cands else err)
        # ── 抖音:浏览器自动化(发现仍用拦截抓取)──
        if mode == "auto_comment":
            if kind == "keyword":
                return [], "抖音暂不支持关键词发现,请用「创作者」模式指定博主"
            items, _author, err = await fetch_videos(
                self.browser, identity, rf["sec_uid"], set(), max_scrolls=4,
                block_media=self.cfg.engine.block_media_resources)
            for it in items[:self.cfg.engine.comment_recent_works]:
                aid = str(it.get("aweme_id") or "")
                if aid:
                    cands.append({"aweme_id": aid, "xsec_token": "",
                                  "target_comment_id": "", "target_nick": "",
                                  "ctx": {}, "source_text": it.get("desc", "")})
            return cands, err
        # auto_reply 抖音:回复自己作品评论
        if rf.get("has_creator"):
            raw, creator_error = await fetch_creator_comments(
                self.browser, identity, set(),
                page_url=self.cfg.engine.creator_comment_url,
                max_scrolls=max(1, min(self.cfg.engine.comment_max_scrolls, 4)),
                block_media=self.cfg.engine.block_media_resources)
            selected_works = set()
            comment_cutoff = int(time.time()) - max(0, self.cfg.engine.comment_recent_days) * 86400
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
                if self._is_self_comment(rc, acc_nick, acc_sec_uid,
                                         rf.get("account_uid", "")):
                    continue
                if kind != "work" and c["aweme_id"] not in selected_works:
                    if len(selected_works) >= self.cfg.engine.comment_recent_works:
                        continue
                    selected_works.add(c["aweme_id"])
                cands.append({"aweme_id": c["aweme_id"], "xsec_token": "",
                              "target_comment_id": c["comment_id"],
                              "target_nick": c.get("user_nickname", ""),
                              "ctx": {"nick": c.get("user_nickname", "")},
                              "source_text": c.get("text", "")})
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
            cutoff = int(time.time()) - max(0, self.cfg.engine.comment_recent_days) * 86400
            for it in items[:self.cfg.engine.comment_recent_works]:
                aid = str(it.get("aweme_id") or "")
                create_time = int(it.get("create_time") or 0)
                if aid and (not cutoff or not create_time or create_time >= cutoff):
                    works.append((aid, it.get("desc", "")))
        for aid, _desc in works:
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
                if self._is_self_comment(rc, acc_nick, acc_sec_uid,
                                         rf.get("account_uid", "")):
                    continue
                cands.append({"aweme_id": aid, "xsec_token": "",
                              "target_comment_id": c["comment_id"],
                              "target_nick": c.get("user_nickname", ""),
                              "ctx": {"nick": c.get("user_nickname", "")},
                              "source_text": c.get("text", "")})
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
