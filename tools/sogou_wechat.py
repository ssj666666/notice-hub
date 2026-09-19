"""解析搜狗跳转链接 -> 真实 mp.weixin 链接 -> 抓正文；并测试按时间排序能否拿到近期文章。"""
from __future__ import annotations

import asyncio
import re
import sys
import time
from datetime import datetime, timezone, timedelta

import httpx

sys.path.insert(0, str(Path := __import__("pathlib").Path(__file__).resolve().parent.parent))

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
CST = timezone(timedelta(hours=8))
NAME = "北大体育"


def ts(v: str) -> str:
    try:
        return datetime.fromtimestamp(int(v), CST).strftime("%Y-%m-%d")
    except Exception:
        return "?"


async def resolve(c: httpx.AsyncClient, sogou_link: str) -> tuple[str, str]:
    """把 /link?url=... 解析成真实的 mp.weixin 链接。搜狗用 JS 拼接，需要逐段取。"""
    url = sogou_link if sogou_link.startswith("http") else "https://weixin.sogou.com" + sogou_link
    try:
        r = await c.get(url, headers={"Referer": "https://weixin.sogou.com/weixin"})
    except Exception as exc:
        return "", f"{type(exc).__name__}"
    text = r.text
    # 常见形式：url += 'xxx'; ...
    parts = re.findall(r"url\s*\+=\s*'([^']*)'", text)
    if parts:
        return "".join(parts).replace("@", ""), "js-concat"
    m = re.search(r"https?://mp\.weixin\.qq\.com/s[/?][^\s\"'<>\\]{6,200}", text)
    if m:
        return m.group(0), "direct"
    if len(text) < 300:
        return "", f"短页面 {len(text)}B: {text[:80]!r}"
    return "", "未匹配"


async def fetch_article(c: httpx.AsyncClient, url: str) -> dict:
    try:
        r = await c.get(url, headers={"User-Agent": UA})
    except Exception as exc:
        return {"ok": False, "err": type(exc).__name__}
    r.encoding = r.encoding or "utf-8"
    html = r.text
    title = re.search(r'<h1[^>]*id="activity-name"[^>]*>(.*?)</h1>', html, re.S)
    if not title:
        title = re.search(r"<title>(.*?)</title>", html, re.S)
    body = re.search(r'<div[^>]*id="js_content"[^>]*>(.*?)</div>\s*<script', html, re.S) \
        or re.search(r'<div[^>]*id="js_content"[^>]*>(.*)</div>', html, re.S)
    import html as _h
    from bs4 import BeautifulSoup
    t = _h.unescape(re.sub(r"<[^>]+>", "", title.group(1))).strip() if title else ""
    text = ""
    if body:
        soup = BeautifulSoup(body.group(1), "html.parser")
        text = re.sub(r"\s+", " ", soup.get_text(" ", strip=True))
    return {"ok": bool(text), "title": t, "text": text,
            "len": len(text), "bytes": len(r.content)}


async def main() -> None:
    async with httpx.AsyncClient(follow_redirects=True, verify=False, timeout=30,
                                 headers={"User-Agent": UA}) as c:
        await c.get("https://weixin.sogou.com/")

        # ---------- A. 解析跳转 + 抓正文 ----------
        print("=" * 76)
        print("  A. 解析搜狗跳转链接，并尝试抓正文")
        print("=" * 76)
        r = await c.get("https://weixin.sogou.com/weixin",
                        params={"type": "2", "query": NAME})
        r.encoding = r.encoding or "utf-8"
        from bs4 import BeautifulSoup
        soup = BeautifulSoup(r.text, "html.parser")
        links = [a["href"] for a in soup.select("div.txt-box h3 a") if a.get("href")]
        titles = [a.get_text(" ", strip=True) for a in soup.select("div.txt-box h3 a")]
        print(f"  本轮拿到 {len(links)} 条结果\n")
        ok_resolve = 0
        for i, (t, l) in enumerate(zip(titles[:4], links[:4]), 1):
            real, how = await resolve(c, l)
            print(f"  [{i}] {t[:44]}")
            if real:
                ok_resolve += 1
                print(f"      真实链接({how}): {real[:110]}")
                art = await fetch_article(c, real)
                if art.get("ok"):
                    print(f"      ✓ 正文 {art['len']} 字 | {art['title'][:52]}")
                    print(f"        开头: {art['text'][:140]}")
                else:
                    print(f"      ✗ 正文抓取失败: {art}")
            else:
                print(f"      ✗ 解析失败: {how}")
            await asyncio.sleep(1.5)
        print(f"\n  跳转解析成功 {ok_resolve}/{min(4, len(links))}")

        # ---------- B. 能否拿到近期文章 ----------
        print()
        print("=" * 76)
        print("  B. 能否按时间拿到近期文章")
        print("=" * 76)
        for label, params in [
            ("按时间排序 tsn=1", {"type": "2", "query": NAME, "tsn": "1"}),
            ("按时间排序 tsn=2", {"type": "2", "query": NAME, "tsn": "2"}),
            ("按时间 tsn=1 + 近一年", {"type": "2", "query": NAME, "tsn": "1",
                                       "ft": datetime.now().strftime("%Y-%m-%d"),
                                       "et": "", "interation": "", "wxid": ""}),
            ("第 2 页", {"type": "2", "query": NAME, "page": "2"}),
            ("关键词：北大体育 2026", {"type": "2", "query": "北大体育 2026"}),
            ("关键词：北大体育 体测", {"type": "2", "query": "北大体育 体测"}),
        ]:
            try:
                rr = await c.get("https://weixin.sogou.com/weixin", params=params)
                rr.encoding = rr.encoding or "utf-8"
                s2 = BeautifulSoup(rr.text, "html.parser")
                boxes = s2.select("div.txt-box")
                stamps = re.findall(r"timeConvert\('(\d+)'\)", rr.text)
                dates = [ts(x) for x in stamps]
                print(f"\n  [{label}] {len(boxes)} 条结果")
                if dates:
                    print(f"      日期范围: {min(dates)} ~ {max(dates)}")
                for b in boxes[:3]:
                    a = b.select_one("h3 a")
                    tsx = re.search(r"timeConvert\('(\d+)'\)", str(b.parent))
                    print(f"        {ts(tsx.group(1)) if tsx else '?'}  "
                          f"{a.get_text(' ', strip=True)[:44] if a else '?'}")
            except Exception as exc:
                print(f"  [{label}] ERR {type(exc).__name__}")
            await asyncio.sleep(2)


if __name__ == "__main__":
    import warnings
    warnings.filterwarnings("ignore")
    sys.stdout.reconfigure(encoding="utf-8")
    asyncio.run(main())
