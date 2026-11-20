"""Rednote / XiaoHongShu shared publish form normalizer and formatter (`ePc`).

Implements the exact form normalization, validation, and payload assembly rules:
  - ``weighted_truncate`` (``exV``): iterates Unicode code points; ``U+0000..U+00FF``
    counts as ``0.5``, all other code points count as ``1.0``, stopping before
    exceeding budget (title budget ``20`` -> up to 40 ASCII chars or 20 CJK chars;
    description budget ``1000``).
  - Image-mode description check: UTF-16 code units ``<= 900``.
  - Topics (``CAT_tags``): up to 10 items; single string topic ``<= 50`` chars;
    resolved topic entities populate ``hash_tag`` while all named topics append
    ``#name[话题]# `` to ``formData["desc"]``.
  - Mentions (``CAT_mention``): ``common["ats"]`` entries with ``denotation_char="@"``
    and ``index`` sequence ``"0", "2", "3", ...`` (preserving exact client behavior
    by default, with ``legacy_at_index=False`` opt-in for sequential indices).
  - Description assembly: joins ``[CAT_desc, topic_text, at_text]`` (or
    ``[at_text, CAT_desc, topic_text]`` when ``mention_append_position == "begin"``)
    with ``"  "`` while keeping outer ``desc`` as original ``CAT_desc``.
  - Scheduled time (``CAT_timing.type == 2``): ``postTime`` in Unix milliseconds.
  - Rednote capability guard: domestic-only commerce/group extensions can be
    rejected or stripped when targeting overseas Rednote.
"""
from __future__ import annotations

import copy
import json
from datetime import datetime, timezone
from typing import Any, Callable, Mapping, Sequence

MAX_TITLE_WEIGHT = 20.0
MAX_DESC_WEIGHT = 1000.0
MAX_IMAGE_DESC_UTF16 = 900
MAX_TOPICS = 10
MAX_TOPIC_LENGTH = 50
MAX_GOODS = 18
MAX_IMAGES = 18

UNVERIFIED_REDNOTE_FIELDS = (
    "CAT_goods",
    "CAT_groupChat",
    "CAT_declare",
    "CAT_collection",
    "CAT_refNote",
)


def char_weight(ch: str) -> float:
    return 0.5 if ord(ch) <= 0xFF else 1.0


def weighted_length(text: str) -> float:
    return sum(char_weight(ch) for ch in str(text or ""))


def weighted_truncate(text: str, budget: float) -> str:
    """Truncate ``text`` by code-point weight without exceeding ``budget`` (``exV``)."""
    raw = str(text or "")
    if budget <= 0 or not raw:
        return ""
    used = 0.0
    out: list[str] = []
    for ch in raw:
        w = char_weight(ch)
        if used + w > budget + 1e-9:
            break
        out.append(ch)
        used += w
    return "".join(out)


def utf16_code_units(text: str) -> int:
    """Return JavaScript ``String.prototype.length`` (UTF-16 code unit count)."""
    return len(str(text or "").encode("utf-16-le")) // 2


def strip_literal_undefined(mapping: Mapping[str, Any] | None) -> dict[str, Any]:
    """Delete properties whose value is literally the string ``"undefined"`` (``exX``)."""
    if not isinstance(mapping, Mapping):
        return {}
    return {k: copy.deepcopy(v) for k, v in mapping.items() if v != "undefined"}


def has_comment(value: Any) -> bool:
    """Return True when follow-up comment config carries non-empty content (``exU``)."""
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


def common_to_rednote_form(material: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Convert common publish inputs into shared XHS/Rednote form fields (``ePi``)."""
    src = material or {}
    out: dict[str, Any] = {}
    if "title" in src and src.get("title") is not None:
        out["title"] = weighted_truncate(str(src.get("title") or ""), MAX_TITLE_WEIGHT)
    if "cover" in src:
        out["CAT_cover"] = src.get("cover")
    if "desc" in src:
        out["CAT_desc"] = weighted_truncate(str(src.get("desc") or ""), MAX_DESC_WEIGHT)
    if "tags" in src:
        raw_tags = src.get("tags")
        if isinstance(raw_tags, str):
            parsed_tags = [t.strip().lstrip("#") for t in raw_tags.split(",") if t.strip()]
            out["CAT_tags"] = parsed_tags[:MAX_TOPICS]
        elif isinstance(raw_tags, Sequence):
            out["CAT_tags"] = list(raw_tags)[:MAX_TOPICS]
    if "timing" in src:
        out["CAT_timing"] = src.get("timing")
    if "comment" in src:
        out["CAT_comment"] = src.get("comment")
    return out


def default_rednote_form(
    common_converted: Mapping[str, Any] | None = None,
    overrides: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Build default form state merged with converted common values & overrides (``ePl``)."""
    base: dict[str, Any] = {
        "title": "",
        "CAT_desc": "",
        "CAT_tags": [],
        "privacy_type": 0,
        "CAT_timing": {"type": 1},
    }
    for layer in (strip_literal_undefined(common_converted), strip_literal_undefined(overrides)):
        for key, val in layer.items():
            if isinstance(val, Mapping) and isinstance(base.get(key), dict):
                base[key] = {**base[key], **dict(val)}
            else:
                base[key] = val
    return base


def validate_rednote_form(
    form: Mapping[str, Any],
    *,
    media_type: str = "images",
    allow_unverified_extensions: bool = False,
) -> None:
    """Validate form constraints for Rednote publishing."""
    title = str(form.get("title") or "").strip()
    if weighted_length(title) > MAX_TITLE_WEIGHT + 1e-9:
        raise ValueError("海外小红书标题超出长度上限（最多 20 个汉字或 40 个英文字符）")

    desc = str(form.get("CAT_desc", form.get("desc", "")) or "")
    normalized_media = str(media_type or "images").strip().lower()
    if normalized_media in {"image", "images"} and utf16_code_units(desc) > MAX_IMAGE_DESC_UTF16:
        raise ValueError(f"海外小红书图文正文最多 {MAX_IMAGE_DESC_UTF16} 字符")
    if weighted_length(desc) > MAX_DESC_WEIGHT + 1e-9:
        raise ValueError("海外小红书正文超出长度上限")

    tags = form.get("CAT_tags") or []
    if not isinstance(tags, Sequence) or isinstance(tags, (str, bytes)):
        raise ValueError("话题列表格式无效")
    if len(tags) > MAX_TOPICS:
        raise ValueError(f"最多添加 {MAX_TOPICS} 个话题")
    for item in tags:
        if isinstance(item, str) and len(item.strip().lstrip("#")) > MAX_TOPIC_LENGTH:
            raise ValueError(f"单个话题最多 {MAX_TOPIC_LENGTH} 字符")

    goods = form.get("CAT_goods") or []
    if isinstance(goods, Sequence) and not isinstance(goods, (str, bytes)):
        if len(goods) > MAX_GOODS:
            raise ValueError(f"商品挂载最多 {MAX_GOODS} 项")

    privacy = form.get("privacy_type", 0)
    if privacy not in (0, 1):
        raise ValueError("海外小红书可见范围仅支持公开(0)或仅自己可见(1)")

    if not allow_unverified_extensions:
        unsupported = [key for key in UNVERIFIED_REDNOTE_FIELDS if form.get(key)]
        if unsupported:
            raise ValueError(
                f"海外小红书暂不支持未验证的国内专属扩展字段: {', '.join(unsupported)}"
            )


def _to_epoch_ms(value: Any) -> int:
    if isinstance(value, (int, float)):
        num = float(value)
        return int(num) if num > 1e11 else int(num * 1000)
    if isinstance(value, datetime):
        dt = value if value.tzinfo else value.replace(tzinfo=timezone.utc)
        return int(dt.timestamp() * 1000)
    text = str(value or "").strip()
    if not text:
        raise ValueError("定时发布时间不能为空")
    if text.isdigit():
        num = int(text)
        return num if num > 10**11 else num * 1000
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    dt = datetime.fromisoformat(text)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)


def _normalize_topic_item(item: Any) -> dict[str, Any] | None:
    if isinstance(item, str):
        clean = item.strip().lstrip("#").strip()
        if not clean:
            return None
        return {"name": clean, "type": "topic"}
    if isinstance(item, Mapping):
        name = str(item.get("name") or "").strip().lstrip("#").strip()
        if not name:
            return None
        entity: dict[str, Any] = {"name": name, "type": "topic"}
        if item.get("id"):
            entity["id"] = str(item["id"])
        if item.get("link"):
            entity["link"] = str(item["link"])
        return entity
    return None


def format_rednote_form(
    form: Mapping[str, Any],
    *,
    mention_append_position: str = "end",
    legacy_at_index: bool = True,
    strip_domestic_extensions: bool = False,
    offset_time: Callable[[Any], Any] | None = None,
) -> dict[str, Any]:
    """Format form values into the internal publish task envelope (``ePc.formatFormValues``)."""
    t: dict[str, Any] = copy.deepcopy(dict(form))
    if strip_domestic_extensions:
        for key in UNVERIFIED_REDNOTE_FIELDS:
            t.pop(key, None)

    original_desc = str(t.get("CAT_desc", t.get("desc", "")) or "")
    t["CAT_desc"] = original_desc

    timing = dict(t.get("CAT_timing") or {"type": 1})
    t["CAT_timing"] = timing
    if timing.get("type") == 2 and timing.get("time") is not None:
        if offset_time is not None:
            timing["time"] = offset_time(timing["time"])
        t["postTime"] = _to_epoch_ms(timing["time"])

    raw_tags = t.get("CAT_tags") or []
    normalized_tags: list[dict[str, Any]] = []
    if isinstance(raw_tags, Sequence) and not isinstance(raw_tags, (str, bytes)):
        for raw_tag in list(raw_tags)[:MAX_TOPICS]:
            norm = _normalize_topic_item(raw_tag)
            if norm is not None:
                normalized_tags.append(norm)
    t["CAT_tags"] = normalized_tags

    hash_tags: list[dict[str, Any]] = []
    topic_tokens: list[str] = []
    for tag in normalized_tags:
        if tag.get("id"):
            hash_tags.append({
                "id": tag["id"],
                "name": tag["name"],
                "link": tag.get("link", ""),
                "type": "topic",
            })
        if tag.get("name"):
            topic_tokens.append(f"#{tag['name']}[话题]# ")
    if normalized_tags:
        t["hash_tag"] = hash_tags

    common = dict(t.get("common") or {})
    common["goods_info"] = {}
    goods = t.get("CAT_goods") or []
    if isinstance(goods, Sequence) and not isinstance(goods, (str, bytes)) and len(goods) > 0:
        common["goods_info"] = {"extension": {"live_preheat": "0"}}
        biz_relations = []
        for idx, item in enumerate(list(goods)[:MAX_GOODS]):
            if not isinstance(item, Mapping):
                continue
            seller_type = str(item.get("goods_seller_type") or "").lower()
            extra_info = json.dumps(
                {
                    "bind_order": idx,
                    "goods_id": item.get("goods_id", ""),
                    "goods_name": item.get("name", ""),
                    "goods_type": seller_type,
                    "image_type": "spec",
                    "left_bottom_type": "BUY_GOODS",
                    "tab_id": 1,
                },
                ensure_ascii=False,
                separators=(",", ":"),
            )
            biz_relations.append({
                "biz_type": "GOODS_SELLER_V2",
                "biz_id": item.get("goods_id", ""),
                "extra_info": extra_info,
            })
        common["biz_relations"] = biz_relations

    declare = t.get("CAT_declare")
    if isinstance(declare, Mapping) and declare.get("userDeclarationBind") is not None:
        t["userDeclarationBind"] = declare["userDeclarationBind"]

    collection = t.get("CAT_collection")
    if isinstance(collection, Mapping) and collection.get("id"):
        t["noteCollectionBind"] = {"id": collection["id"]}

    option_relations: list[dict[str, Any]] = []
    if t.get("CAT_ORIGINAL_STATEMENT"):
        option_relations.append({
            "type": "ORIGINAL_STATEMENT",
            "relationList": [{
                "bizType": "ORIGINAL_STATEMENT",
                "bizId": "",
                "extraInfo": "{}",
            }],
        })
    ref_note = t.get("CAT_refNote")
    if isinstance(ref_note, Mapping) and ref_note.get("id"):
        note_id = str(ref_note["id"])
        option_relations.append({
            "type": "REF_POST",
            "relationList": [{
                "bizType": "REF_POST",
                "bizId": note_id,
                "extraInfo": json.dumps(
                    {"ref_id": note_id, "ref_type": "note", "note_id": note_id},
                    ensure_ascii=False,
                    separators=(",", ":"),
                ),
            }],
        })
    t["optionRelationList"] = option_relations

    mentions = t.get("CAT_mention") or []
    at_tokens: list[str] = []
    if isinstance(mentions, Sequence) and not isinstance(mentions, (str, bytes)) and len(mentions) > 0:
        ats: list[dict[str, Any]] = []
        for idx, mention in enumerate(mentions):
            if not isinstance(mention, Mapping):
                continue
            origin = mention.get("originData") if isinstance(mention.get("originData"), Mapping) else mention
            nickname = str(
                origin.get("user_nickname")
                or origin.get("nickname")
                or mention.get("name")
                or ""
            ).strip()
            user_id = str(origin.get("user_id") or mention.get("userId") or "").strip()
            if not nickname:
                continue
            # Preserve exact client index sequence "0", "2", "3", ... when legacy_at_index=True
            at_index = str(idx + 1 if (legacy_at_index and idx > 0) else idx)
            ats.append({
                "denotation_char": "@",
                "image": origin.get("image", ""),
                "index": at_index,
                "name": nickname,
                "nickname": nickname,
                "user_id": user_id,
                "user_nickname": nickname,
                "value": nickname,
                "_type": "user",
            })
            at_tokens.append(f"@{nickname}")
        if ats:
            common["ats"] = ats
    t["common"] = common

    group_chat = t.get("CAT_groupChat")
    if isinstance(group_chat, Mapping) and group_chat.get("group_id"):
        t["groupBind"] = {
            "groupId": group_chat.get("group_id"),
            "groupName": group_chat.get("group_name", ""),
            "desc": group_chat.get("desc", ""),
            "avatar": group_chat.get("avatar", ""),
        }

    topic_text = " ".join(topic_tokens).strip()
    at_text = " ".join(at_tokens).strip()
    if str(mention_append_position or "end").strip().lower() == "begin":
        parts = [at_text, original_desc, topic_text]
    else:
        parts = [original_desc, topic_text, at_text]
    t["desc"] = "  ".join(p for p in parts if p).strip()

    comment_val = t.get("CAT_comment")
    comment_present = has_comment(comment_val)
    return {
        "title": t.get("title", ""),
        "cover": t.get("CAT_cover"),
        "desc": original_desc,
        "timing": timing.get("time"),
        "formData": t,
        "comment": comment_val if comment_present else None,
        "commentStatus": 0 if comment_present else None,
    }


def clip_rednote_images(images: Sequence[str] | None) -> list[str]:
    """Enforce the 18-image selection cap on note tasks."""
    if not images:
        return []
    return [str(img) for img in list(images)[:MAX_IMAGES] if str(img)]
