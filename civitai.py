"""Civitai 公开 API 客户端:模型/LoRA 搜索与详情,描述 HTML 白名单消毒。

- 本机到 civitai.com 通常不可直连,代理在「设置」页配置(civitai_proxy,可选)。
- API Token 可选(civitai_token),部分下载与更高速率限制需要。
- 搜索/详情响应做进程内 TTL 缓存。
"""
import json
import re
import time
from html.parser import HTMLParser
from urllib.parse import urlsplit

import requests

import db

BASE = "https://civitai.com/api/v1"

_cache = {}
_cache_lock = __import__("threading").Lock()


class CivitaiError(Exception):
    pass


def cached(key, ttl, fn):
    now = time.time()
    with _cache_lock:
        hit = _cache.get(key)
        if hit and now - hit[0] < ttl:
            return hit[1]
        if len(_cache) > 100:  # 防长驻进程无界增长:挤掉最旧的过期项
            for k in sorted(_cache, key=lambda k: _cache[k][0])[: len(_cache) - 100]:
                _cache.pop(k, None)
    val = fn()
    with _cache_lock:
        _cache[key] = (now, val)
    return val


def clear_cache():
    with _cache_lock:
        _cache.clear()


def _proxies():
    proxy = (db.get_setting("civitai_proxy") or "").strip()
    if not proxy:
        return None
    if not re.match(r"^https?://[A-Za-z0-9.\-]+(:\d{1,5})?$", proxy):
        raise CivitaiError("Civitai 代理地址无效,应为 http://IP:端口 形式")
    return {"http": proxy, "https": proxy}


_sess_obj = None


def _session():
    """带代理与凭据的会话(模块级复用,省去每请求 TLS 握手);
    代理/Token 变更后由 reset_session() 重建。"""
    global _sess_obj
    if _sess_obj is None:
        s = requests.Session()
        s.proxies = _proxies() or {}
        token = (db.get_setting("civitai_token") or "").strip()
        if token:
            s.headers["Authorization"] = f"Bearer {token}"
        _sess_obj = s
    return _sess_obj


def reset_session():
    """设置页改了代理/Token 后调用,丢弃旧会话。"""
    global _sess_obj
    _sess_obj = None


def _api_url(path):
    """构造 Civitai API 地址;路径必须以 / 开头且不含上跳。"""
    if not isinstance(path, str) or not path.startswith("/") or ".." in path:
        raise CivitaiError(f"无效的 Civitai API 路径: {path!r}")
    return BASE + path


def get_json(path, params=None, timeout=40):
    """经代理拉 Civitai 偶发慢(实测 1~40s 波动,多为陈旧连接停顿):
    失败后重建会话(丢掉旧连接)自动重试一次,仍失败才报错。"""
    r, last = None, None
    for _ in range(2):
        try:
            r = _session().get(_api_url(path), params=params, timeout=timeout)
            break
        except requests.RequestException as e:
            last = e
            reset_session()
    else:
        raise CivitaiError(
            f"访问 Civitai 失败: {last.__class__.__name__},请检查设置页的代理配置") from last
    if r.status_code != 200:
        raise CivitaiError(f"Civitai 返回 {r.status_code}")
    return r.json()


def fetch_image(url):
    """拉取 civitai CDN 图片,返回 requests 流式响应。

    仅允许 https + civitai.com(含子域),重建后的 URL 只含 scheme/host/path。
    """
    u = urlsplit(url if isinstance(url, str) else "")
    if (u.scheme != "https" or not u.hostname
            or not (u.hostname == "civitai.com" or u.hostname.endswith(".civitai.com"))):
        raise CivitaiError("仅支持 civitai.com 的图片地址")
    clean = "https://" + u.hostname + u.path
    if u.query:
        clean += "?" + u.query
    try:
        r = _session().get(clean, timeout=30, stream=True)
    except requests.RequestException as e:
        raise CivitaiError(f"取图失败: {e.__class__.__name__}") from e
    if r.status_code != 200:
        r.close()
        raise CivitaiError(f"取图失败: Civitai 返回 {r.status_code}")
    return r


def search(q=None, types=("Checkpoint",), base=None, sort="Most Downloaded",
           cursor=None, nsfw=False, force=False):
    """cursor 分页:首页 cursor=None,后续传上一页响应的 nextCursor。

    实测 Civitai 的 /models 对部分排序(如 Most Downloaded)忽略 page 参数,
    只有 cursor 分页在所有排序下行为一致。
    """
    params = {"limit": 20, "sort": sort, "types": ",".join(types),
              "nsfw": "true" if nsfw else "false"}
    if q:
        params["query"] = q
    if cursor:
        params["cursor"] = cursor
    if base:
        params["baseModels"] = base
    key = "civsearch:" + repr(sorted(params.items()))
    if force:
        data = get_json("/models", params)
    else:
        data = cached(key, 300, lambda: get_json("/models", params))
    items = [_card(it) for it in data.get("items") or [] if nsfw or not it.get("nsfw")]
    return {"items": items, "nextCursor": (data.get("metadata") or {}).get("nextCursor")}


# ---------- 本地库(SQLite):拉取过的搜索页与模型数据落库,加载直接读本地 ----------

def _search_key(q, types, base, sort, cursor, nsfw):
    return "|".join([",".join(types), q or "", base or "", sort,
                     cursor or "", "nsfw" if nsfw else "sfw"])


def _store_cards(items, ctype):
    now = time.time()
    for card in items:
        db.execute(
            "INSERT INTO civ_models(civ_id,type,card_json,fetched_at) VALUES(?,?,?,?) "
            "ON CONFLICT(civ_id) DO UPDATE SET card_json=excluded.card_json,"
            "type=excluded.type,fetched_at=excluded.fetched_at",
            (card["id"], ctype, json.dumps(card, ensure_ascii=False), now))


def search_local(q=None, types=("Checkpoint",), base=None, sort="Most Downloaded",
                 cursor=None, nsfw=False, refresh=False):
    """本地库优先:命中搜索页直接回本地卡片;未命中(或 refresh)拉 Civitai 并入库。"""
    ctype = ",".join(types)
    key = _search_key(q, types, base, sort, cursor, nsfw)
    if not refresh:
        page = db.query_one("SELECT ids, next_cursor FROM civ_searches WHERE key=?", (key,))
        if page:
            return {"items": _cards_by_ids(json.loads(page["ids"])),
                    "nextCursor": page["next_cursor"], "cached": True}
    res = search(q=q, types=types, base=base, sort=sort, cursor=cursor,
                 nsfw=nsfw, force=refresh)
    _store_cards(res["items"], ctype)
    db.execute(
        "INSERT INTO civ_searches(key,ids,next_cursor,fetched_at) VALUES(?,?,?,?) "
        "ON CONFLICT(key) DO UPDATE SET ids=excluded.ids,"
        "next_cursor=excluded.next_cursor,fetched_at=excluded.fetched_at",
        (key, json.dumps([c["id"] for c in res["items"]]),
         res.get("nextCursor") or "", time.time()))
    res["cached"] = False
    return res


def _cards_by_ids(ids):
    if not ids:
        return []
    marks = ",".join("?" * len(ids))
    rows = db.query(f"SELECT civ_id, card_json FROM civ_models WHERE civ_id IN ({marks})",
                    [int(i) for i in ids])
    by_id = {r["civ_id"]: r["card_json"] for r in rows}
    return [json.loads(by_id[i]) for i in ids if i in by_id]


def get_model_local(model_id, refresh=False):
    """详情本地库优先:看过一次即落库(含描述/用法/版本);没有或 refresh 才在线拉。"""
    model_id = int(model_id)
    row = db.query_one("SELECT card_json, detail_json FROM civ_models WHERE civ_id=?",
                       (model_id,))
    if not refresh and row and row["detail_json"]:
        return json.loads(row["detail_json"])
    d = get_model(model_id, force=refresh)
    db.execute(
        "INSERT INTO civ_models(civ_id,type,card_json,detail_json,fetched_at) "
        "VALUES(?,?,?,?,?) ON CONFLICT(civ_id) DO UPDATE SET "
        "detail_json=excluded.detail_json,fetched_at=excluded.fetched_at",
        (model_id, d.get("type") or "",
         row["card_json"] if row else json.dumps(_card_from_detail(d), ensure_ascii=False),
         json.dumps(d, ensure_ascii=False), time.time()))
    return d


def _card_from_detail(d):
    """只看过详情、没出现在搜索里的模型,补一张最简卡片。"""
    v = (d.get("versions") or [{}])[0]
    return {"id": d.get("id"), "name": d.get("name"), "type": d.get("type"),
            "creator": d.get("creator"), "base": v.get("baseModel"),
            "cover": v.get("cover") or "", "downloads": d.get("downloads") or 0,
            "likes": d.get("likes") or 0}


def match_by_filename(stem, ctype):
    """按文件名在 Civitai 匹配模型:精确同名(去分隔符)>互相包含。

    返回 {civ_id, civ_name, base_model, trained_words, cover} 或 None。
    """
    def norm(s):
        return re.sub(r"[^a-z0-9\u4e00-\u9fff]+", "", (s or "").lower())

    target = norm(stem)
    if not target:
        return None
    data = get_json("/models", {"query": stem, "types": ctype, "limit": 5, "nsfw": "true"})
    best = None
    for it in data.get("items") or []:
        for v in it.get("modelVersions") or []:
            for f in v.get("files") or []:
                n = norm(f.get("name", "").rsplit(".", 1)[0])
                if not n:
                    continue
                if n == target:
                    score = 100
                elif target in n or n in target:
                    score = 60
                else:
                    continue
                if best is None or score > best["_score"]:
                    best = {"_score": score, "civ_id": it.get("id"),
                            "civ_name": it.get("name") or "",
                            "base_model": v.get("baseModel") or "",
                            "trained_words": v.get("trainedWords") or [],
                            "cover": ((v.get("images") or [{}])[0].get("url") or "")}
    if best:
        best.pop("_score")
    return best


def get_model(model_id, force=False):
    if force:
        data = get_json(f"/models/{int(model_id)}")
    else:
        data = cached("civmodel:" + str(model_id), 600,
                      lambda: get_json(f"/models/{int(model_id)}"))
    return {
        "id": data.get("id"),
        "name": data.get("name"),
        "type": data.get("type"),
        "creator": (data.get("creator") or {}).get("username"),
        "tags": data.get("tags") or [],
        "downloads": (data.get("stats") or {}).get("downloadCount", 0),
        "likes": (data.get("stats") or {}).get("thumbsUpCount", 0),
        "description": sanitize_html(data.get("description") or ""),
        "versions": [_card_version(v) for v in data.get("modelVersions") or []],
    }


def _card(it):
    v = (it.get("modelVersions") or [{}])[0]
    stats = it.get("stats") or {}
    card = _card_version(v)
    card.update({
        "id": it.get("id"), "name": it.get("name"), "type": it.get("type"),
        "creator": (it.get("creator") or {}).get("username"),
        "nsfw": bool(it.get("nsfw")),
        "base": v.get("baseModel"),
        "downloads": stats.get("downloadCount", 0),
        "likes": stats.get("thumbsUpCount", 0),
        "cover": ((v.get("images") or [{}])[0].get("url") or ""),
    })
    return card


def _card_version(v):
    files = (v.get("files") or [{}])[0]
    return {
        "id": v.get("id"), "name": v.get("name"), "baseModel": v.get("baseModel"),
        "trainedWords": v.get("trainedWords") or [],
        "downloadUrl": v.get("downloadUrl"),
        "file": files.get("name"),
        "sizeKB": files.get("sizeKB"),
        "cover": ((v.get("images") or [{}])[0].get("url") or ""),
        "publishedAt": (v.get("publishedAt") or "")[:10],
    }


# ---------- 描述 HTML 消毒 ----------

ALLOWED_TAGS = {"p", "br", "b", "i", "em", "strong", "u", "s", "ul", "ol", "li",
                "h1", "h2", "h3", "h4", "h5", "h6", "code", "pre", "blockquote",
                "a", "img", "table", "thead", "tbody", "tr", "td", "th", "hr"}
VOID_TAGS = {"br", "img", "hr"}


class _Sanitizer(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.out = []
        self.open_tags = []
        self.skip_depth = 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style", "iframe", "noscript"):
            self.skip_depth += 1
            return
        if tag not in ALLOWED_TAGS:
            return
        keep = []
        if tag == "a":
            href = dict(attrs).get("href") or ""
            if href.startswith("/"):
                href = "https://civitai.com" + href
            if href.startswith(("http://", "https://")):
                keep.append(('target="_blank"', None))
                keep.append(('rel="noopener noreferrer"', None))
                keep.append((f'href="{_esc_attr(href)}"', None))
        elif tag == "img":
            src = dict(attrs).get("src") or ""
            if src.startswith(("http://", "https://")):
                keep.append((f'src="{_esc_attr(src)}"', None))
                keep.append(('loading="lazy"', None))
                keep.append(('style="max-width:100%"', None))
        elif tag == "td":
            span = dict(attrs).get("colspan")
            if span:
                keep.append((f'colspan="{_esc_attr(span)}"', None))
        attrs_str = (" " + " ".join(k for k, _ in keep)) if keep else ""
        if tag in VOID_TAGS:
            self.out.append(f"<{tag}{attrs_str}>")
        else:
            self.out.append(f"<{tag}{attrs_str}>")
            self.open_tags.append(tag)

    def handle_startendtag(self, tag, attrs):
        if tag in VOID_TAGS:
            self.handle_starttag(tag, attrs)
        elif tag in ALLOWED_TAGS:
            self.handle_starttag(tag, attrs)
            self.handle_endtag(tag)

    def handle_endtag(self, tag):
        if tag in ("script", "style", "iframe", "noscript"):
            self.skip_depth = max(0, self.skip_depth - 1)
            return
        if tag in ALLOWED_TAGS and tag not in VOID_TAGS and tag in self.open_tags:
            # 关闭中间未闭合的允许标签,保持输出平衡
            while self.open_tags:
                t = self.open_tags.pop()
                self.out.append(f"</{t}>")
                if t == tag:
                    break

    def handle_data(self, data):
        if self.skip_depth:
            return
        self.out.append(data.replace("<", "&lt;").replace(">", "&gt;"))


def _esc_attr(v):
    return (v.replace("&", "&amp;").replace('"', "&quot;")
             .replace("<", "&lt;").replace(">", "&gt;"))


def sanitize_html(html):
    p = _Sanitizer()
    try:
        p.feed(html or "")
        p.close()
    except Exception:
        return ""
    while p.open_tags:
        p.out.append(f"</{p.open_tags.pop()}>")
    return "".join(p.out)
