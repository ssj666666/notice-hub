"""微信公众号文章搜索（基于搜狗微信「文章搜索」）。

能做什么：
  - 按公众号名或关键词搜文章，列出标题 / 公众号 / 日期 / 链接
  - 抓取文章正文（用于总结）
不能做什么：
  - 拿不到「永久链接」（搜狗给的是带签名的临时链接，几小时后可能失效）
  - 不是实时流（搜狗只索引了一部分历史，新文章可能搜不到）
  - 公众号的完整历史消息（微信要求客户端 session，拿不到）

用法:
    py -3.12 tools/wechat_search.py 北大体育
    py -3.12 tools/wechat_search.py 北大体育 --pages 3
    py -3.12 tools/wechat_search.py 北大体育 --account-only --read 3
    py -3.12 tools/wechat_search.py "北大 体测" --pages 2
"""
from __future__ import annotations

import argparse
import asyncio
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
from bs4 import BeautifulSoup

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
CST = timezone(timedelta(hours=8))
BASE = "https://weixin.sogou.com"
DELAY = 2.0          # 每次请求间隔，别把搜狗惹毛


def _fmt_date(stamp: str) -> str:
    try:
        return datetime.fromtimestamp(int(stamp), CST).strftime("%Y-%m-%d")
    except Exception:
        return "?"


async def search(c: httpx.AsyncClient, query: str, page: int = 1) -> list[dict]:
    params = {"type": "2", "query": query}
    if page > 1:
        params["page"] = str(page)
    r = await c.get(f"{BASE}/weixin", params=params,
                    headers={"User-Agent": UA, "Referer": f"{BASE}/"})
    r.encoding = r.encoding or "utf-8"
    soup = BeautifulSoup(r.text, "html.parser")

    out = []
    for box in soup.select("div.txt-box"):
        h3 = box.find("h3")
        a = h3.find("a") if h3 else None
        if not a:
            continue
        # 账号名在 div.s-p > span.all-time-y2（不是 a.account）
        sp = box.select_one("div.s-p")
        acc = sp.select_one("span.all-time-y2") if sp else None
        if acc is None and sp is not None:
            acc = sp.find("span")
        summary = box.find("p", class_="txt-info")
        stamp = re.search(r"timeConvert\('(\d+)'\)", str(box.parent))
        out.append({
            "title": a.get_text(" ", strip=True),
            "account": acc.get_text(" ", strip=True) if acc else "",
            "date": _fmt_date(stamp.group(1)) if stamp else "?",
            "_ts": int(stamp.group(1)) if stamp else 0,
            "summary": summary.get_text(" ", strip=True) if summary else "",
            "sogou": a.get("href", ""),
        })
    return out


async def resolve(c: httpx.AsyncClient, sogou_link: str) -> str:
    """搜狗的 /link?url=... 用 JS 分段拼接，需要逐段取出再拼。"""
    url = sogou_link if sogou_link.startswith("http") else BASE + sogou_link
    r = await c.get(url, headers={"User-Agent": UA, "Referer": f"{BASE}/weixin"})
    parts = re.findall(r"url\s*\+=\s*'([^']*)'", r.text)
    if parts:
        return "".join(parts).replace("@", "")
    m = re.search(r"https?://mp\.weixin\.qq\.com/s[/?][^\s\"'<>\\]{6,200}", r.text)
    return m.group(0) if m else ""


async def read_article(c: httpx.AsyncClient, url: str) -> dict:
    r = await c.get(url, headers={"User-Agent": UA})
    r.encoding = r.encoding or "utf-8"
    soup = BeautifulSoup(r.text, "html.parser")
    node = soup.select_one("#activity-name") or soup.title
    title = node.get_text(" ", strip=True) if node else ""
    body = soup.select_one("#js_content")
    text = re.sub(r"\s+", " ", body.get_text(" ", strip=True)) if body else ""
    biz = re.search(r'var\s+biz\s*=\s*"([^"]+)"', r.text)
    return {"title": title, "text": text, "length": len(text),
            "biz": biz.group(1) if biz else ""}


async def main() -> int:
    ap = argparse.ArgumentParser(description="微信公众号文章搜索（搜狗微信）")
    ap.add_argument("query", help="公众号名或关键词")
    ap.add_argument("--pages", type=int, default=1, help="翻几页（每页约 10 条）")
    ap.add_argument("--account-only", action="store_true",
                    help="只保留公众号名等于关键词的结果")
    ap.add_argument("--read", type=int, default=0, help="读取前 N 篇正文")
    ap.add_argument("--max-chars", type=int, default=600, help="每篇正文最多打印多少字")
    args = ap.parse_args()

    async with httpx.AsyncClient(follow_redirects=True, verify=False, timeout=30) as c:
        await c.get(BASE + "/", headers={"User-Agent": UA})

        rows: list[dict] = []
        for p in range(1, args.pages + 1):
            got = await search(c, args.query, p)
            rows.extend(got)
            print(f"  第 {p} 页: {len(got)} 条", file=sys.stderr)
            if len(got) < 10:
                break
            await asyncio.sleep(DELAY)

        if args.account_only:
            rows = [r for r in rows if args.query in r["account"]]
        # 按时间倒序
        rows.sort(key=lambda r: r["_ts"], reverse=True)

        print()
        print("=" * 78)
        print(f"  「{args.query}」共 {len(rows)} 条"
              + (f"（最早 {min(r['date'] for r in rows)}，"
                 f"最新 {max(r['date'] for r in rows)}）" if rows else ""))
        print("=" * 78)
        for i, r in enumerate(rows, 1):
            print(f"\n  {i:2}. [{r['date']}] {r['title'][:60]}")
            print(f"      公众号: {r['account']}")
            if r["summary"]:
                print(f"      摘要  : {r['summary'][:80]}")

        if args.read and rows:
            print()
            print("=" * 78)
            print(f"  读取前 {min(args.read, len(rows))} 篇正文")
            print("=" * 78)
            for i, r in enumerate(rows[:args.read], 1):
                link = await resolve(c, r["sogou"])
                await asyncio.sleep(DELAY)
                if not link:
                    print(f"\n  {i}. {r['title'][:50]}  —— 链接解析失败")
                    continue
                art = await read_article(c, link)
                await asyncio.sleep(DELAY)
                print(f"\n  {i}. {art['title'][:60] or r['title'][:60]}")
                print(f"      公众号: {r['account']}   日期: {r['date']}"
                      f"   正文 {art['length']} 字")
                print(f"      链接(临时，可能几小时后失效): {link[:100]}")
                if art["text"]:
                    shown = art["text"][:args.max_chars]
                    print(f"      正文: {shown}"
                          + ("…" if len(art['text']) > args.max_chars else ""))
        return 0


if __name__ == "__main__":
    import warnings
    warnings.filterwarnings("ignore")
    sys.stdout.reconfigure(encoding="utf-8")
    raise SystemExit(asyncio.run(main()))
