"""尝试读取「北大体育」微信公众号：逐条验证各条可行路径。

路径：
  1. 搜狗微信·公众号搜索 (type=1)
  2. 搜狗微信·文章搜索 (type=2)
  3. 搜狗移动版
  4. Bing / 搜索引擎找 mp.weixin.qq.com 链接
  5. 拿到文章链接后能否直接抓正文
"""
from __future__ import annotations

import asyncio
import re
import sys
from pathlib import Path

import httpx
from bs4 import BeautifulSoup

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

NAME = "北大体育"
UA_DESKTOP = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
UA_MOBILE = ("Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
             "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1")


def show(label: str, r: httpx.Response | None, err: str = "") -> None:
    if r is None:
        print(f"  [{label}] ERR {err}")
        return
    text = r.text
    ct = r.headers.get("content-type", "")[:34]
    block = ("验证码" in text or "antispider" in text.lower()
             or "请输入验证码" in text or "访问过于频繁" in text)
    print(f"  [{label}] HTTP {r.status_code} {len(r.content)}B ct={ct} 拦截={block}")
    # 找 mp.weixin 链接
    links = set(re.findall(r"https?://mp\.weixin\.qq\.com/s[/?][^\s\"'<>]{6,120}", text))
    if links:
        print(f"      发现 {len(links)} 个公众号文章链接")
        for u in list(links)[:5]:
            print(f"        {u}")
    # 找公众号搜索结果块
    for pat, desc in ((r'uigs="account_name_\d+"', "账号结果块"),
                      (r'<div class="txt-box"', "文章结果块")):
        n = len(re.findall(pat, text))
        if n:
            print(f"      {desc}: {n} 个")
    if not links and not block:
        body = BeautifulSoup(text, "html.parser").get_text(" ", strip=True)
        print(f"      纯文本前 150 字: {body[:150]!r}")


async def main() -> None:
    async with httpx.AsyncClient(follow_redirects=True, verify=False, timeout=30) as c:
        print("=" * 76)
        print(f"  1-3. 搜狗微信（公众号搜索 / 文章搜索 / 移动版）")
        print("=" * 76)
        trials = [
            ("公众号搜索", "https://weixin.sogou.com/weixin",
             {"type": "1", "query": NAME}, UA_DESKTOP),
            ("文章搜索", "https://weixin.sogou.com/weixin",
             {"type": "2", "query": NAME}, UA_DESKTOP),
            ("文章搜索+时间", "https://weixin.sogou.com/weixin",
             {"type": "2", "query": NAME, "tsn": "1", "ft": "", "et": "",
              "interation": "", "wxid": ""}, UA_DESKTOP),
            ("移动版", "https://weixin.sogou.com/weixin",
             {"type": "2", "query": NAME}, UA_MOBILE),
        ]
        for label, url, params, ua in trials:
            try:
                # 先访问首页拿 cookie
                await c.get("https://weixin.sogou.com/", headers={"User-Agent": ua})
                r = await c.get(url, params=params, headers={"User-Agent": ua})
                show(label, r)
            except Exception as exc:
                show(label, None, type(exc).__name__)
            print()

        print("=" * 76)
        print("  4. 搜索引擎找 mp.weixin.qq.com 链接")
        print("=" * 76)
        engines = [
            ("Bing", "https://www.bing.com/search",
             {"q": f'site:mp.weixin.qq.com 北大体育'}),
            ("Bing2", "https://www.bing.com/search",
             {"q": f'北大体育 公众号 mp.weixin.qq.com'}),
            ("搜狗网页", "https://www.sogou.com/web", {"query": f"北大体育 公众号"}),
            ("360", "https://www.so.com/s", {"q": f"北大体育 微信公众号"}),
        ]
        found: set[str] = set()
        for label, url, params in engines:
            try:
                r = await c.get(url, params=params,
                                headers={"User-Agent": UA_DESKTOP})
                links = set(re.findall(
                    r"https?://mp\.weixin\.qq\.com/s[/?][^\s\"'<>&]{6,120}", r.text))
                print(f"  [{label}] HTTP {r.status_code} {len(r.content)}B，"
                      f"命中 mp.weixin 链接 {len(links)} 个")
                for u in list(links)[:6]:
                    print(f"        {u}")
                found |= links
            except Exception as exc:
                print(f"  [{label}] ERR {type(exc).__name__}")
            print()

        print("=" * 76)
        print(f"  合计找到 {len(found)} 个公众号文章链接")
        print("=" * 76)

        print()
        print("=" * 76)
        print("  5. 任取一个 mp.weixin 文章，看能否直接抓正文")
        print("=" * 76)
        sample = next(iter(found), None)
        if not sample:
            print("  没有样本链接可测")
            return
        print(f"  样本: {sample}")
        try:
            r = await c.get(sample, headers={"User-Agent": UA_MOBILE})
            r.encoding = r.encoding or "utf-8"
            soup = BeautifulSoup(r.text, "html.parser")
            title = soup.select_one("#activity-name") or soup.title
            content = soup.select_one("#js_content")
            print(f"  HTTP {r.status_code} {len(r.content)}B")
            print(f"  标题: {title.get_text(strip=True)[:80] if title else '(无)'}")
            if content:
                txt = re.sub(r"\s+", " ", content.get_text(" ", strip=True))
                print(f"  正文长度: {len(txt)} 字")
                print(f"  正文前 200 字: {txt[:200]}")
            else:
                body = soup.get_text(" ", strip=True)
                print(f"  未找到 #js_content，纯文本前 200 字: {body[:200]!r}")
        except Exception as exc:
            print(f"  ERR {type(exc).__name__}: {exc}")


if __name__ == "__main__":
    import warnings
    warnings.filterwarnings("ignore")
    sys.stdout.reconfigure(encoding="utf-8")
    asyncio.run(main())
