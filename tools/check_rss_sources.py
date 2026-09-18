"""临时探查：这 12 个公众号在不同服务上有没有可用的 RSS。"""
import re
import sys
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/124.0"}

ACCOUNTS = [
    "北京大学百家讲坛",
    "北大讲坛online",
    "北京大学",
    "北京大学医学部",
    "北京大学教务处",
    "北京大学医学部教务处",
    "北大体育",
    "北京大学学生会",
    "北京大学医学部学生会",
    "北医团委",
    "北京大学就业指导中心",
    "北京大学医学部就业指导中心",
]

# 泛化关键词，用来在一大片列表里捞相关号
KEYWORDS = ["北大", "北京大学", "北医", "医学部", "讲坛", "学生会", "就业",
            "团委", "教务", "体育", "百家讲坛"]


def fetch(url: str) -> str | None:
    try:
        r = httpx.get(url, headers=UA, timeout=30, follow_redirects=True)
        if r.status_code == 200:
            r.encoding = r.encoding or "utf-8"
            return r.text
        print(f"    HTTP {r.status_code}  {url}")
    except Exception as exc:
        print(f"    ERR {type(exc).__name__}  {url}")
    return None


def check_wechat2rss() -> None:
    print("\n" + "=" * 70)
    print("  Wechat2RSS 免费列表")
    print("=" * 70)
    html = fetch("https://wechat2rss.xlab.app/list/all.html")
    if not html:
        return

    # 列表形如 <a href="https://wechat2rss.xlab.app/feed/<hash>.xml">号名</a>
    entries = re.findall(
        r'<a[^>]+href="(https://wechat2rss\.xlab\.app/feed/[0-9a-f]+\.xml)"[^>]*>(.*?)</a>',
        html, re.S)
    entries = [(re.sub(r"<[^>]+>", "", name).strip(), url) for url, name in entries]
    print(f"  列表总收录数: {len(entries)}")
    if entries:
        print(f"  样例: {entries[0][0]}  ->  {entries[0][1]}")

    exact = {name: None for name in ACCOUNTS}
    for name, url in entries:
        if name.strip() in exact:
            exact[name.strip()] = url

    print("\n  --- 精确匹配结果 ---")
    for name, url in exact.items():
        print(f"    {'✓' if url else '✗'} {name}" + (f"  ->  {url}" if url else ""))

    print("\n  --- 泛化关键词命中（可能有相关号）---")
    hits = [(n, u) for n, u in entries
            if any(k in n for k in KEYWORDS)]
    if not hits:
        print("    （无）")
    for n, u in hits:
        print(f"    · {n}")

    # 顺带看看有没有高校类目
    print("\n  --- 列表里的类目 ---")
    cats = re.findall(r"<h2[^>]*>(.*?)</h2>", html)
    print("   ", [c.strip() for c in cats])


def check_other_providers() -> None:
    print("\n" + "=" * 70)
    print("  其他服务")
    print("=" * 70)
    for label, url in (
        ("feeddd 列表", "https://feeddd.org/feeds"),
        ("今天看啥", "https://www.jintiankansha.me/"),
        ("WeRSS", "https://werss.app/"),
        ("RSSHub 文档·微信公众号", "https://docs.rsshub.app/routes/new-media"),
    ):
        print(f"\n  [{label}] {url}")
        html = fetch(url)
        if html:
            title = re.search(r"<title[^>]*>(.*?)</title>", html, re.S)
            print(f"    可达，title={title.group(1).strip()[:60] if title else '(无)'}")
            for acc in ("北京大学", "北医", "医学部"):
                if acc in html:
                    print(f"    页面内容里出现了「{acc}」")


if __name__ == "__main__":
    check_wechat2rss()
    check_other_providers()
