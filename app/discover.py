"""网址自动探测：给一个网址，判断该怎么抓，并生成可用的源配置。

支持三种情况：
  1. RSS/Atom 订阅源        → 直接生成 rss 源
  2. 静态列表页             → 自动识别 item_selector，生成 web 源
  3. JS 渲染页              → 尝试从页面里挖 JSON 接口；挖不到就退回变化检测模式
另外还会扫描页面上的「通知/公告/新闻」链接，供用户挑选更合适的列表页。
"""
from __future__ import annotations

import logging
import re
from urllib.parse import urljoin, urlparse

import httpx
from bs4 import BeautifulSoup

from .autodetect import detect_selectors

log = logging.getLogger("notice-hub.discover")

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")

JS_HINTS = ("__NUXT__", "window.__INITIAL_STATE__", 'id="app"', "id='app'",
            "v-app", "ng-app", "layui.use", "artTemplate")

# 页面上像"列表入口"的链接文字
LIST_WORDS = ("通知", "公告", "新闻", "动态", "讲座", "招聘", "招新", "招募",
              "信息", "活动", "要闻", "综合", "more", "更多")

# 内联脚本里像接口的字符串
API_PAT = re.compile(
    r"""["'`]([^"'`\s]{6,180}?(?:/v1/|/api/|/ajax/|ajax_|getList|listData|/json/|\.json)[^"'`\s]{0,90})["'`]""",
    re.I)


def _guess_name(soup: BeautifulSoup, url: str) -> str:
    if soup.title:
        title = re.sub(r"\s+", " ", soup.title.get_text(" ", strip=True)).strip()
        # 去掉常见的后缀，例如 "通知公告 - 北京大学教务部"
        title = re.split(r"[|\-—_·]", title)[0].strip()
        if 2 <= len(title) <= 24:
            return title
    return urlparse(url).netloc


async def discover(client: httpx.AsyncClient, url: str,
                   timeout: float = 30) -> dict:
    url = (url or "").strip()
    if not url:
        return {"ok": False, "message": "请填写网址"}
    if not url.startswith(("http://", "https://")):
        url = "https://" + url

    try:
        resp = await client.get(url, headers={"User-Agent": UA}, timeout=timeout)
        resp.raise_for_status()
    except Exception as exc:
        return {"ok": False, "url": url,
                "message": f"打不开这个网址：{type(exc).__name__}。"
                           f"检查一下地址，或者这个站可能需要登录。"}

    resp.encoding = resp.encoding or "utf-8"
    text = resp.text
    head = text.lstrip()[:400].lower()

    # ---------- 1. RSS / Atom ----------
    if head.startswith("<?xml") and ("<rss" in head or "<feed" in head) \
            or "<rss" in head[:200] or "<feed" in head[:200]:
        name = _guess_name(BeautifulSoup(text, "html.parser"), url)
        return {
            "ok": True, "url": url, "kind": "rss", "name": name,
            "message": "这是一个 RSS/Atom 订阅源，可以直接接入。",
            "suggested": {"name": name, "enabled": True, "url": url,
                          "type": "rss", "weight": 1.2},
            "samples": [], "stats": {},
        }

    soup = BeautifulSoup(text, "html.parser")
    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()
    visible = soup.get_text(" ", strip=True)
    name = _guess_name(soup, url)

    # ---------- 2. 静态列表页 ----------
    detected = detect_selectors(text, top=3)
    if detected:
        best = detected[0]
        suggested = {
            "name": name, "enabled": True, "url": url, "type": "web",
            "item_selector": best["selector"],
            "title_selector": "a",
            "link_selector": "a",
            "weight": 1.3,
        }
        return {
            "ok": True, "url": url, "kind": "list", "name": name,
            "message": f"识别到列表结构，命中 {best['count']} 项"
                       f"（{'含日期' if best['date_ratio'] > 0.5 else '未发现日期'}）。",
            "suggested": suggested,
            "samples": [d["sample_title"] for d in detected if d["sample_title"]],
            "stats": {"items": best["count"],
                      "dated": round(best["date_ratio"] * best["count"])},
            "candidates": _candidate_pages(soup, url),
        }

    # ---------- 3. JS 渲染页 ----------
    inline = "\n".join(s.get_text() for s in
                       BeautifulSoup(text, "html.parser").find_all("script")
                       if not s.get("src"))
    apis: list[str] = []
    for m in API_PAT.finditer(inline):
        candidate = m.group(1)
        if candidate.startswith("//"):
            candidate = "https:" + candidate
        if candidate.startswith("http") or candidate.startswith("/"):
            apis.append(candidate)
    apis = list(dict.fromkeys(apis))[:8]

    is_js = any(h in text for h in JS_HINTS) or len(visible) < 400
    note = ("这个页面看起来是 JS 动态加载的，没找到静态列表结构。"
            "如果浏览器里能看到列表，常见原因是同一个网址换个请求方式就返回 JSON"
            "（试试加 X-Requested-With: XMLHttpRequest 头，很多学校站都这样）。")
    if apis:
        note += " 我从页面脚本里挖到几个疑似接口，可以用 sources.api 手工配置。"
    return {
        "ok": True, "url": url, "kind": "js" if is_js else "unknown",
        "name": name,
        "message": note,
        "suggested": {
            "name": name, "enabled": True, "url": url, "type": "web",
            "weight": 1.2,
        },
        "suggested_note": "也可以先用变化检测模式接入（不填选择器）："
                          "首轮只建基线，页面出现新内容才会产出条目。",
        "api_candidates": apis,
        "samples": [],
        "stats": {"visible_text": len(visible)},
        "candidates": _candidate_pages(soup, url),
    }


def _candidate_pages(soup: BeautifulSoup, base: str) -> list[dict]:
    """扫描页面上的列表入口链接，帮用户找到更合适的「列表页」。"""
    host = urlparse(base).netloc
    out, seen = [], {base}
    for a in soup.find_all("a"):
        label = re.sub(r"\s+", "", a.get_text(" ", strip=True))
        if not label or len(label) > 16:
            continue
        if not any(w in label for w in LIST_WORDS):
            continue
        href = a.get("href") or ""
        if not href or href.startswith(("#", "javascript", "mailto")):
            continue
        full = urljoin(base, href)
        if full in seen or urlparse(full).netloc != host:
            continue
        seen.add(full)
        out.append({"label": label, "url": full})
        if len(out) >= 12:
            break
    return out
