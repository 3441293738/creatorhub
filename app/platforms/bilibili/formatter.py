"""哔哩哔哩投稿表单转换、参数校验、播放来源聚合与 UPOS 分片请求构造。

规则约定：
  - 标题 <=80 字、简介 <=250 字、标签最多 10 项（自制稿件附带活动话题时标签最多 9 项，单标签 <=20 字）；
  - `copyright=1`（自制）可将活动话题置于 `tag` 首位并写入 `topic_detail`/`mission_id`/`topic_id`；
  - `copyright=2`（转载）必填 `source`（<=200 字），忽略原创活动话题；
  - `recreate`：允许二创为 `1`，默认不开启为 `-1`；
  - `up_close_reply` 排斥 `up_selection_reply`；
  - 定时发布写入 `dtime`（Unix 秒）；
  - 播放来源五类百分比聚合（PC / 移动端 / H5及小程序 / 云视听小电视 / 其它）。
"""
from __future__ import annotations

import copy
import math
from datetime import datetime, timezone
from typing import Any, Callable, Mapping

CREATION_STATEMENT_OPTIONS: tuple[dict[str, Any], ...] = (
    {"value": -1, "label": "内容无需标注"},
    {"value": 1, "label": "含AI生成内容"},
    {"value": 2, "label": "含虚构演绎内容"},
    {"value": 3, "label": "内容含营销信息"},
    {"value": 4, "label": "个人观点，仅供参考"},
    {"value": -2, "label": "内容为转载"},
)

DEFAULT_BILI_FORM: dict[str, Any] = {
    "copyright": 1,
    "bili_recreate": False,
    "bili_no_reprint": True,
    "bili_open_elec": True,
    "dynamic": "",
    "bili_timing": {"type": 1},
}

FALLBACK_PARTITIONS: tuple[dict[str, Any], ...] = (
    {"id": 21, "name": "日常", "parent_name": "生活"},
    {"id": 138, "name": "搞笑", "parent_name": "生活"},
    {"id": 161, "name": "手工", "parent_name": "生活"},
    {"id": 76, "name": "美食制作", "parent_name": "美食"},
    {"id": 231, "name": "计算机技术", "parent_name": "科技"},
    {"id": 95, "name": "数码", "parent_name": "科技"},
    {"id": 201, "name": "科学科普", "parent_name": "知识"},
    {"id": 208, "name": "校园学习", "parent_name": "知识"},
    {"id": 122, "name": "野生技能协会", "parent_name": "知识"},
    {"id": 17, "name": "单机游戏", "parent_name": "游戏"},
    {"id": 172, "name": "手机游戏", "parent_name": "游戏"},
    {"id": 182, "name": "影视杂谈", "parent_name": "影视"},
    {"id": 183, "name": "影视剪辑", "parent_name": "影视"},
    {"id": 28, "name": "原创音乐", "parent_name": "音乐"},
    {"id": 31, "name": "翻唱", "parent_name": "音乐"},
    {"id": 27, "name": "综合", "parent_name": "动画"},
    {"id": 157, "name": "美妆护肤", "parent_name": "时尚"},
    {"id": 164, "name": "健身", "parent_name": "运动"},
    {"id": 250, "name": "出行", "parent_name": "生活"},
)


def common_to_bili(material: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """将通用素材字段映射为 B 站表单字段。"""
    src = material or {}
    out: dict[str, Any] = {}
    if "title" in src:
        out["title"] = str(src.get("title") or "")[:80]
    if "cover" in src:
        out["bili_cover"] = src.get("cover")
    if "desc" in src:
        out["desc"] = str(src.get("desc") or "")[:250]
    if "tags" in src:
        raw_tags = src.get("tags")
        out["bili_tags"] = list(raw_tags)[:10] if isinstance(raw_tags, (list, tuple)) else raw_tags
    if "timing" in src:
        out["bili_timing"] = src.get("timing")
    if "comment" in src:
        out["bili_comment"] = src.get("comment")
    return out


def has_comment(value: Any) -> bool:
    if isinstance(value, str):
        return bool(value.strip())
    if not isinstance(value, Mapping):
        return False
    content = value.get("content")
    if isinstance(content, str) and content.strip():
        return True
    content_list = value.get("contentList")
    if isinstance(content_list, (list, tuple)):
        return any(isinstance(item, str) and bool(item.strip()) for item in content_list)
    return False


def validate_bili_form(form: Mapping[str, Any]) -> None:
    """按 B 站表单规则校验投稿参数（区分自制与转载路径）。"""
    title = str(form.get("title") or "").strip()
    if not title:
        raise ValueError("必须设置标题")
    if len(title) > 80:
        raise ValueError("哔哩哔哩标题最多 80 字")
    desc = str(form.get("desc") or "")
    if len(desc) > 250:
        raise ValueError("哔哩哔哩视频简介最多 250 字")
    copyright_type = int(form.get("copyright") or 1)
    tags = [str(t).strip() for t in (form.get("bili_tags") or []) if str(t).strip()]
    topic = form.get("bili_topic")
    has_topic = isinstance(topic, Mapping) and bool(topic.get("topic_name"))

    if copyright_type == 2:
        source = str(form.get("source") or "").strip()
        if not source:
            raise ValueError("转载稿件必须填写转载来源")
        if len(source) > 200:
            raise ValueError("转载来源最多 200 字")
        if len(tags) < 1:
            raise ValueError("请至少填写一个标签")
        if len(tags) > 10:
            raise ValueError("标签数量不能超过10个")
    else:
        if not has_topic and len(tags) < 1:
            raise ValueError("请至少填写一个标签或在下方选择一个话题")
        limit = 9 if has_topic else 10
        if len(tags) > limit:
            raise ValueError("标签话题合计数量不能超过10个")
        if any(len(t) > 20 for t in tags):
            raise ValueError("单个标签最多20字")

    dynamic = str(form.get("dynamic") or "")
    if len(dynamic) > 233:
        raise ValueError("粉丝动态最多 233 字")


def _to_unix_seconds(value: Any) -> int:
    if isinstance(value, (int, float)):
        # 兼容秒与毫秒输入
        val = float(value)
        return int(val // 1000) if val > 1e11 else int(val)
    if isinstance(value, datetime):
        dt = value if value.tzinfo else value.replace(tzinfo=timezone.utc)
        return int(dt.timestamp())
    text = str(value or "").strip()
    if not text:
        raise ValueError("定时发布时间不能为空")
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    dt = datetime.fromisoformat(text)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp())


def format_bili_form(
    form: Mapping[str, Any],
    *,
    offset_time: Callable[[Any], Any] | None = None,
) -> dict[str, Any]:
    """将内部投稿配置转换为标准化的 B 站投稿表单参数。"""
    if "bili_timing" not in form or not isinstance(form.get("bili_timing"), Mapping):
        raise ValueError("bili_timing object required")

    formatted: dict[str, Any] = copy.deepcopy(dict(form))
    copyright_type = formatted.get("copyright")
    section = formatted.get("bili_section")
    topic = formatted.get("bili_topic")
    raw_tags = formatted.get("bili_tags")
    sound_setting = formatted.get("bili_sound_setting") or []
    interact = formatted.get("bili_interact") or []
    timing = dict(formatted["bili_timing"])
    formatted["bili_timing"] = timing
    comment = formatted.get("bili_comment")

    if isinstance(section, Mapping) and section.get("id"):
        formatted["human_type2"] = int(section["id"])

    tags: list[str] = [str(item) for item in (raw_tags or []) if str(item)]
    if copyright_type == 1 and isinstance(topic, Mapping) and topic.get("topic_name"):
        tags = [str(topic["topic_name"])] + tags
    if tags:
        # 空列表时不覆盖已存在的 tag 字段
        formatted["tag"] = ",".join(tags)

    formatted["no_reprint"] = 1 if bool(formatted.get("bili_no_reprint")) else 0
    formatted["recreate"] = 1 if bool(formatted.get("bili_recreate")) else -1
    formatted["open_elec"] = 1 if bool(formatted.get("bili_open_elec")) else 0
    formatted["dolby"] = 1 if ("dolby" in sound_setting) else 0
    formatted["lossless_music"] = 1 if ("lossless_music" in sound_setting) else 0
    formatted["up_close_danmu"] = bool("up_close_danmu" in interact)
    formatted["up_close_reply"] = bool("up_close_reply" in interact)
    formatted["up_selection_reply"] = (not formatted["up_close_reply"]) and bool("up_selection_reply" in interact)

    if copyright_type == 1 and isinstance(topic, Mapping) and topic.get("topic_name"):
        formatted["topic_detail"] = {
            "from_source": topic.get("source"),
            "from_topic_id": topic.get("topic_id"),
        }
        formatted["mission_id"] = topic.get("mission_id")
        formatted["topic_id"] = topic.get("topic_id")

    if timing.get("type") == 2 and timing.get("time") is not None:
        if offset_time is not None:
            timing["time"] = offset_time(timing["time"])
        formatted["dtime"] = _to_unix_seconds(timing["time"])

    valid_comment = has_comment(comment)
    return {
        "title": formatted.get("title"),
        "desc": formatted.get("desc"),
        "cover": formatted.get("bili_cover"),
        "timing": timing.get("time"),
        "comment": comment if valid_comment else None,
        "commentStatus": 0 if valid_comment else None,
        "formData": formatted,
    }


def build_bili_form_from_task(
    *,
    title: str,
    desc: str = "",
    tags: list[str] | None = None,
    schedule_at: datetime | None = None,
    bili_options: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """将 CreatorHub 的 PublishTask 字段与 bili_options 合并为 B 站表单输入。"""
    opts = dict(bili_options or {})
    clean_tags = [str(t).strip().lstrip("#")[:20] for t in (tags or []) if str(t).strip().lstrip("#")]
    clean_title = (str(title or "").strip() or str(desc or "").strip().splitlines()[0] if str(desc or "").strip() else str(title or "").strip())[:80]
    clean_desc = str(desc or "").strip()[:250]

    copyright_type = 2 if int(opts.get("copyright") or 1) == 2 else 1
    tid = int(opts.get("tid") or opts.get("human_type2") or 0)
    topic = opts.get("topic") if isinstance(opts.get("topic"), Mapping) else None

    # B 站视频投稿强制至少 1 个标签；未显式填写且无活动话题时从标题派生安全标签兜底
    if not clean_tags and not (copyright_type == 1 and topic and topic.get("topic_name")):
        fallback_tag = (clean_title or "生活记录")[:20].strip() or "视频"
        clean_tags = [fallback_tag]

    max_tags = 9 if (copyright_type == 1 and topic and topic.get("topic_name")) else 10
    clean_tags = clean_tags[:max_tags]

    interact: list[str] = []
    if opts.get("up_close_danmu"):
        interact.append("up_close_danmu")
    if opts.get("up_close_reply"):
        interact.append("up_close_reply")
    elif opts.get("up_selection_reply"):
        interact.append("up_selection_reply")

    sound_setting: list[str] = []
    if opts.get("dolby"):
        sound_setting.append("dolby")
    if opts.get("lossless_music"):
        sound_setting.append("lossless_music")

    timing: dict[str, Any] = {"type": 1}
    if schedule_at is not None:
        timing = {"type": 2, "time": schedule_at}

    raw_recreate = opts.get("recreate", False)
    if isinstance(raw_recreate, bool):
        allow_recreate = raw_recreate
    elif isinstance(raw_recreate, (int, float)):
        allow_recreate = int(raw_recreate) > 0
    else:
        allow_recreate = str(raw_recreate or "").strip().lower() in {"1", "true", "yes"}

    form: dict[str, Any] = {
        **copy.deepcopy(DEFAULT_BILI_FORM),
        "title": clean_title,
        "desc": clean_desc,
        "copyright": copyright_type,
        "bili_tags": clean_tags,
        "bili_recreate": allow_recreate,
        "bili_no_reprint": bool(opts.get("no_reprint", True)),
        "bili_open_elec": bool(opts.get("open_elec", True)),
        "bili_interact": interact,
        "bili_sound_setting": sound_setting,
        "bili_timing": timing,
        "dynamic": str(opts.get("dynamic") or "")[:233],
    }
    if copyright_type == 2:
        form["source"] = str(opts.get("source") or "").strip()[:200]
    if tid > 0:
        form["bili_section"] = {"id": str(tid), "name": str(opts.get("tid_name") or "")}
    if copyright_type == 1 and topic and topic.get("topic_name"):
        form["bili_topic"] = dict(topic)
    statement_id = opts.get("creation_statement_id")
    if statement_id is not None and str(statement_id).strip() != "":
        try:
            form["creation_statement_id"] = int(statement_id)
        except (TypeError, ValueError):
            pass
    if opts.get("watermark"):
        form["watermark"] = {"state": 1}
    if opts.get("cover"):
        form["bili_cover"] = str(opts["cover"])
    return form


def play_source_percent(raw: Mapping[str, Any] | None) -> list[dict[str, Any]]:
    """五类播放来源百分比聚合（PC / 移动端 / H5及小程序 / 云视听小电视 / 其它）。"""
    if not isinstance(raw, Mapping) or not raw:
        return []

    def _num(key: str) -> float:
        try:
            return float(raw.get(key) or 0)
        except (TypeError, ValueError):
            return 0.0

    groups = (
        ("PC", _num("new_pc") + _num("pc")),
        ("移动端", _num("android") + _num("ios") + _num("new_mobile")),
        ("H5及小程序", _num("h5") + _num("new_h5")),
        ("云视听小电视", _num("tv") + _num("new_ott")),
        ("其它", _num("new_others") + _num("other") + _num("out")),
    )
    total = sum(val for _, val in groups)
    if total <= 0:
        return []
    return [
        {"name": name, "value": round(val / total * 100.0, 2)}
        for name, val in groups
    ]


# ── UPOS 分片上传与投稿接口请求描述辅助函数 ──


def bili_upload_url(preupload: Mapping[str, Any]) -> str:
    endpoint = str(preupload["endpoint"])
    uri = str(preupload["upos_uri"]).replace("upos://", "", 1)
    return f"https:{endpoint}/{uri}"


def bili_req_upload_id_request(
    node: Mapping[str, Any],
    extra_params: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    params = {"uploads": "", "output": "json", **(dict(extra_params or {}))}
    return {
        "url": node["uploadUrl"],
        "options": {
            "params": params,
            "method": "post",
            "headers": {"x-upos-auth": node["auth"]},
        },
    }


def bili_part_request(
    *,
    node: Mapping[str, Any],
    part_number: int,
    data: bytes,
    total: int,
) -> dict[str, Any]:
    chunk_size = int(node["chunk_size"])
    part = int(part_number)
    return {
        "url": node["uploadUrl"],
        "options": {
            "params": {
                "partNumber": part,
                "chunk": part - 1,
                "chunks": math.ceil(total / chunk_size) if chunk_size > 0 else 1,
                "size": len(data),
                "start": chunk_size * (part - 1),
                "end": min(chunk_size * part, total),
                "total": total,
                "uploadId": node["upload_id"],
            },
            "data": data,
            "method": "PUT",
            "headers": {
                "Content-Type": "application/octet-stream",
                "x-upos-auth": node["auth"],
            },
            "responseType": "response",
            "timeout": 1800000,
        },
    }


def bili_part_receipt(part_number: int) -> dict[str, Any]:
    """构造 UPOS 分片上传完成回执项（一基分片序号与固定 eTag 标识）。"""
    return {"eTag": "etag", "partNumber": int(part_number)}


def bili_commit_request(
    node: Mapping[str, Any],
    params: Mapping[str, Any],
    data: Any,
) -> dict[str, Any]:
    return {
        "url": node["uploadUrl"],
        "options": {
            "method": "POST",
            "params": {
                "output": "json",
                "uploadId": node["upload_id"],
                **(dict(params or {})),
            },
            "data": data,
            "headers": {"x-upos-auth": node["auth"]},
        },
    }


def bili_post_create_request(
    create_data: Mapping[str, Any],
    *,
    csrf: str,
    now_ms: int,
) -> dict[str, Any]:
    return {
        "url": "https://member.bilibili.com/x/vu/web/add/v3",
        "options": {
            "params": {"t": int(now_ms), "csrf": str(csrf)},
            "data": dict(create_data),
            "method": "POST",
        },
    }
