"""Facebook Comet 动态 GraphQL Query Registry（零硬编码过期 doc_id）。

设计原则：
1. 绝不硬编码伪造或过期的私有 `doc_id`；
2. 通过两条实时官方来源自动发现当前版本的 `doc_id` 与非敏感静态变量模板：
   - **纯 HTTP JS 模块发现**：从 Facebook 页面 `rsrcMap` 引用的 `fbcdn.net/rsrc.php/*.js` 模块中
     正则提取 Relay `$Parameters` 定义的 `{id, name}`；
   - **浏览器被动学习**：在账号隔离浏览器正常访问搜索、主页、群组或帖子时，被动记录
     Facebook 前端自身发出的 `POST /api/graphql/` 中的公开字段（`fb_api_req_friendly_name`、
     `doc_id` 与剥离个人/动态值后的静态特性开关模板）；
3. 持久化文件（`data/fb_gql_registry.json`）仅存公开的 Relay 查询名、数字 `doc_id` 与布尔/枚举
   特性开关，严禁写入任何 Cookie、`fb_dtsg`、`lsd`、账号 ID、搜索词或用户正文；
4. 若某条缓存的 `doc_id` 收到服务端版本失效错误，立即自动失效并由 `hybrid` 模式回退浏览器重新学习。
"""
from __future__ import annotations

import json
from pathlib import Path
import re
import threading
import time
from typing import Any, Dict, Iterable, List, Mapping, Optional, Tuple
from urllib.parse import parse_qs

REGISTRY_TTL_SECONDS = 7 * 86400
_REGISTRY_PATH = Path("./data/fb_gql_registry.json")
_LOCK = threading.RLock()
_MEMORY_CACHE: Optional[Dict[str, Any]] = None

# 逻辑业务槽位 -> Facebook Comet 官方 Relay Friendly Name 匹配模式（按优先级排序）
QUERY_SLOT_PATTERNS: Dict[str, Tuple[str, ...]] = {
    "search_posts": (
        "SearchCometResultsPaginatedResultsQuery",
        "SearchCometResultsInitialResultsQuery",
        "SearchCometGlobalSearchDefaultTabQuery",
        "SearchCometResults",
    ),
    "profile_posts": (
        "ProfileCometTimelineFeedRefetchQuery",
        "ProfileCometTimelineFeedQuery",
        "CometProfileTimelineRefetchQuery",
        "CometModernPageFeedPaginationQuery",
        "ProfileCometTimeline",
        "CometPageTimeline",
    ),
    "group_posts": (
        "GroupsCometFeedRegularStoriesPaginationQuery",
        "GroupsCometDiscussionFeedStoriesPaginationQuery",
        "GroupsCometFeedRegularStories",
        "GroupsCometFeed",
    ),
    "post_comments": (
        "CommentsListComponentsPaginationQuery",
        "Depth1CommentsListPaginationQuery",
        "CommentListComponentsRootQuery",
        "CometFocusedStoryViewUFIQuery",
        "CommentsListComponents",
        "UFI2CommentsProvider",
    ),
    "reel_comments": (
        "FBUnifiedVideoFeedbackRightRailWithCommentPreloadingQuery",
        "FBUnifiedVideoFeedbackRightRail",
        "CometVideoFeedbackRightRail",
        "VideoFeedbackRightRail",
    ),
}

# 绝不持久化到模板中的动态/敏感变量名（小写集合）
_STRIP_VAR_KEYS = frozenset({
    "cursor",
    "end_cursor",
    "after",
    "before",
    "commentsaftercursor",
    "commentsbeforecursor",
    "focuscommentid",
    "initial_node_id",
    "query",
    "q",
    "search_query",
    "id",
    "userid",
    "user_id",
    "profileid",
    "profile_id",
    "groupid",
    "group_id",
    "nodeid",
    "node_id",
    "feedbackid",
    "feedback_id",
    "storyid",
    "story_id",
    "post_id",
    "actor_id",
    "target_id",
    "targetid",
    "bsid",
    "session_id",
    "sessionid",
    "encrypted_tracking",
    "tracking",
    "client_mutation_id",
    "input",
    "message",
    "text",
    "comment_text",
})

_DOC_ID_RE = re.compile(r"^\d{10,24}$")
_JS_RELAY_PAIR_RE_1 = re.compile(
    r'(?:id|documentId)\s*:\s*"(\d{10,24})"[^{}]{0,220}?name\s*:\s*"([A-Za-z0-9_]+Query)"'
)
_JS_RELAY_PAIR_RE_2 = re.compile(
    r'name\s*:\s*"([A-Za-z0-9_]+Query)"[^{}]{0,220}?(?:id|documentId)\s*:\s*"(\d{10,24})"'
)


def classify_query_slot(friendly_name: str) -> str:
    name = str(friendly_name or "").strip()
    if not name:
        return ""
    for slot, patterns in QUERY_SLOT_PATTERNS.items():
        for pat in patterns:
            if name == pat or pat in name:
                return slot
    return ""


def _sanitize_variables_template(raw_vars: Any, depth: int = 0) -> Dict[str, Any]:
    """仅保留布尔、短枚举/数字静态特性开关，彻底剥离所有用户/目标/分页动态值。"""
    if depth > 3 or not isinstance(raw_vars, Mapping):
        return {}
    out: Dict[str, Any] = {}
    for k, v in raw_vars.items():
        ks = str(k or "").strip()
        if not ks or ks.lower() in _STRIP_VAR_KEYS:
            continue
        if isinstance(v, bool):
            out[ks] = v
        elif isinstance(v, (int, float)):
            if abs(v) <= 10000:
                out[ks] = v
        elif isinstance(v, str):
            # 仅保留简短枚举值（如 TOP_LEVEL / FEED / PROFILE 等），丢弃任意文本或长 Token
            if len(v) <= 48 and re.fullmatch(r"[A-Za-z0-9_.:/-]+", v) and not v.isdigit():
                out[ks] = v
        elif isinstance(v, Mapping):
            sub = _sanitize_variables_template(v, depth + 1)
            if sub:
                out[ks] = sub
    return out


def _empty_store() -> Dict[str, Any]:
    return {"version": 1, "operations": {}, "slots": {}}


def _load_store() -> Dict[str, Any]:
    global _MEMORY_CACHE
    with _LOCK:
        if _MEMORY_CACHE is not None:
            return _MEMORY_CACHE
        data = _empty_store()
        try:
            if _REGISTRY_PATH.is_file():
                parsed = json.loads(_REGISTRY_PATH.read_text(encoding="utf-8"))
                if isinstance(parsed, dict):
                    data["operations"] = dict(parsed.get("operations") or {})
                    data["slots"] = dict(parsed.get("slots") or {})
        except Exception:
            data = _empty_store()
        _MEMORY_CACHE = data
        return data


def _save_store(store: Dict[str, Any]) -> None:
    try:
        _REGISTRY_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = _REGISTRY_PATH.with_suffix(".tmp")
        tmp.write_text(json.dumps(store, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(_REGISTRY_PATH)
    except Exception:
        pass


def record_operation(
    friendly_name: str,
    doc_id: str,
    *,
    variables: Optional[Mapping[str, Any]] = None,
    slot_hint: str = "",
    source: str = "browser",
) -> bool:
    """写入一条实时发现的 Facebook GraphQL Operation 规格（不含任何隐私字段）。"""
    fname = str(friendly_name or "").strip()
    did = str(doc_id or "").strip()
    if not fname or not _DOC_ID_RE.match(did):
        return False
    slot = slot_hint or classify_query_slot(fname)
    if not slot:
        return False

    template = _sanitize_variables_template(variables or {})
    now = int(time.time())
    with _LOCK:
        store = _load_store()
        prev = store["operations"].get(fname)
        merged_template = dict((prev or {}).get("variables_template") or {})
        merged_template.update(template)
        changed = (
            not prev
            or prev.get("doc_id") != did
            or merged_template != (prev or {}).get("variables_template")
        )
        store["operations"][fname] = {
            "friendly_name": fname,
            "doc_id": did,
            "slot": slot,
            "variables_template": merged_template,
            "source": source,
            "updated_at": now,
        }
        # 同槽位按优先模式排名，优先保留分页查询（Paginated / Refetch / Pagination）
        cur_slot_name = store["slots"].get(slot)
        patterns = QUERY_SLOT_PATTERNS.get(slot, ())

        def _rank(op_name: str) -> int:
            for idx, p in enumerate(patterns):
                if op_name == p:
                    return idx
            for idx, p in enumerate(patterns):
                if p in op_name:
                    return idx + 10
            return 99

        if not cur_slot_name or cur_slot_name not in store["operations"] or _rank(fname) <= _rank(cur_slot_name):
            store["slots"][slot] = fname
            changed = True
        if changed:
            _save_store(store)
    return True


def record_observed_gql_request(
    post_body: str,
    *,
    friendly_name_header: str = "",
    slot_hint: str = "",
) -> bool:
    """从浏览器实际发出的 `POST /api/graphql/` 表单体中被动学习 live `doc_id` 与静态特性模板。"""
    if not post_body:
        return False
    try:
        qs = parse_qs(post_body, keep_blank_values=False)
    except Exception:
        return False
    did = (qs.get("doc_id") or [""])[0].strip()
    fname = (qs.get("fb_api_req_friendly_name") or [friendly_name_header or ""])[0].strip()
    if not did or not fname:
        return False
    slot = slot_hint or classify_query_slot(fname)
    if not slot:
        return False
    raw_vars: Dict[str, Any] = {}
    v_str = (qs.get("variables") or [""])[0]
    if v_str:
        try:
            parsed_v = json.loads(v_str)
            if isinstance(parsed_v, Mapping):
                raw_vars = dict(parsed_v)
        except Exception:
            raw_vars = {}
    return record_operation(fname, did, variables=raw_vars, slot_hint=slot, source="browser_observed")


def get_slot_operation(slot: str) -> Optional[Dict[str, Any]]:
    """获取指定逻辑槽位（`search_posts` / `profile_posts` / `group_posts` / `post_comments`）的 live 规格。"""
    now = int(time.time())
    with _LOCK:
        store = _load_store()
        fname = store["slots"].get(slot)
        if fname:
            entry = store["operations"].get(fname)
            if entry and now - int(entry.get("updated_at") or 0) <= REGISTRY_TTL_SECONDS:
                return dict(entry)
        # 回退查找同槽位最新的有效记录
        best: Optional[Dict[str, Any]] = None
        for entry in store["operations"].values():
            if entry.get("slot") != slot:
                continue
            if now - int(entry.get("updated_at") or 0) > REGISTRY_TTL_SECONDS:
                continue
            if best is None or int(entry.get("updated_at") or 0) > int(best.get("updated_at") or 0):
                best = entry
        return dict(best) if best else None


def invalidate_operation(friendly_name: str) -> None:
    """当服务端提示某 `doc_id` 失效/不兼容时立即清除缓存。"""
    fname = str(friendly_name or "").strip()
    if not fname:
        return
    with _LOCK:
        store = _load_store()
        if fname in store["operations"]:
            slot = store["operations"][fname].get("slot")
            del store["operations"][fname]
            if slot and store["slots"].get(slot) == fname:
                store["slots"].pop(slot, None)
            _save_store(store)


def extract_doc_ids_from_js(js_text: str) -> List[Tuple[str, str]]:
    """从 Facebook 官方 `rsrc.php/*.js` 模块源码中提取 Relay `(friendly_name, doc_id)` 对。"""
    found: Dict[str, str] = {}
    if not js_text or "Query" not in js_text:
        return []
    for m in _JS_RELAY_PAIR_RE_1.finditer(js_text):
        did, fname = m.group(1), m.group(2)
        if classify_query_slot(fname):
            found[fname] = did
    for m in _JS_RELAY_PAIR_RE_2.finditer(js_text):
        fname, did = m.group(1), m.group(2)
        if classify_query_slot(fname):
            found[fname] = did
    return list(found.items())


def extract_rsrc_js_urls_from_html(html_text: str, *, target_slots: Iterable[str] = ()) -> List[str]:
    """从 SSR HTML 提取可能包含目标 Relay Query 参数定义的 `fbcdn.net/rsrc.php/*.js` 链接。"""
    if not html_text:
        return []
    # 1) 优先定位 HTML 中直接提及目标查询名附近的 rsrc URL，其次取前若干主包
    raw_urls = re.findall(r'https:(?:\\/|/){2}[A-Za-z0-9.-]*fbcdn\.net(?:\\/|/)rsrc\.php[^"\'\s<>]+?\.js[^"\'\s<>]*', html_text)
    seen: set[str] = set()
    urls: List[str] = []
    for raw in raw_urls:
        u = raw.replace("\\/", "/").strip()
        if u and u not in seen:
            seen.add(u)
            urls.append(u)
    if not target_slots:
        return urls[:18]
    # 尝试从 rsrcMap 键映射中优先挑选与目标槽位关键字相关的 JS 资源
    want_tokens: List[str] = []
    for sl in target_slots:
        want_tokens.extend(QUERY_SLOT_PATTERNS.get(sl, ()))
    prioritized: List[str] = []
    for tok in want_tokens:
        idx = html_text.find(tok)
        if idx >= 0:
            window = html_text[max(0, idx - 600): idx + 2000]
            for raw in re.findall(r'https:(?:\\/|/){2}[A-Za-z0-9.-]*fbcdn\.net(?:\\/|/)rsrc\.php[^"\'\s<>]+?\.js[^"\'\s<>]*', window):
                u = raw.replace("\\/", "/").strip()
                if u and u not in prioritized:
                    prioritized.append(u)
    for u in urls:
        if u not in prioritized:
            prioritized.append(u)
        if len(prioritized) >= 18:
            break
    return prioritized[:18]


def registry_summary() -> Dict[str, Any]:
    """返回脱敏后的 Registry 诊断摘要（供状态排查使用）。"""
    with _LOCK:
        store = _load_store()
        return {
            "slots": {
                slot: {
                    "friendly_name": fname,
                    "doc_id": (store["operations"].get(fname) or {}).get("doc_id", ""),
                    "source": (store["operations"].get(fname) or {}).get("source", ""),
                }
                for slot, fname in store["slots"].items()
            },
            "operation_count": len(store["operations"]),
        }
