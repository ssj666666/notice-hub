"""采集层：RSS / IMAP 邮箱 / 网页监控。三种采集器输出统一的 RawItem。"""
from __future__ import annotations

import email
import hashlib
import imaplib
import json
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from email.header import decode_header, make_header
from email.utils import parsedate_to_datetime

import feedparser
import httpx
from bs4 import BeautifulSoup

from .autodetect import detect_selectors, extract_date

log = logging.getLogger("notice-hub.collector")

CST = timezone(timedelta(hours=8))
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36 NoticeHub/1.0")


@dataclass
class RawItem:
    source: str
    source_type: str
    title: str
    url: str = ""
    content: str = ""
    published_at: str = ""
    weight: float = 1.0
    matched: list[str] = field(default_factory=list)
    # 有效期：活动/截止的实际发生日。有它且还没过期时，
    # 即使发布日期很老也不算"过期"（讲座就是这样：发布一个月前，但下周才开讲）
    expires_at: str = ""

    @property
    def uid(self) -> str:
        basis = (self.url or "").strip() or f"{self.source}|{self.title}"
        return hashlib.sha1(basis.encode("utf-8", "ignore")).hexdigest()


def html_to_text(html: str) -> str:
    if not html:
        return ""
    try:
        soup = BeautifulSoup(html, "html.parser")
        for tag in soup(["script", "style", "noscript"]):
            tag.decompose()
        text = soup.get_text("\n")
    except Exception:
        text = re.sub(r"<[^>]+>", " ", html)
    text = re.sub(r"[ \t\xa0]+", " ", text)
    text = re.sub(r"\n{2,}", "\n", text)
    return text.strip()


def _iso(dt: datetime | None) -> str:
    if dt is None:
        return ""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=CST)
    return dt.astimezone(CST).isoformat(timespec="seconds")


# 标题开头的日期/时间前缀，很多学校站把日期塞在链接文字里
_TITLE_NOISE = re.compile(
    r"^\s*[\[\(（]?\s*20\d{2}\s*[-/年.]\s*\d{1,2}\s*[-/月.]\s*\d{1,2}\s*[日号]?"
    r"(\s*\d{1,2}:\d{2}(:\d{2})?)?\s*[\]\)）]?\s*[-—–·|]?\s*"
)


def clean_title(title: str) -> str:
    """把标题里混进来的日期前缀剥掉，让标题干净可读。"""
    if not title:
        return ""
    cleaned = _TITLE_NOISE.sub("", title).strip()
    return cleaned or title.strip()


# ============================================================
#  RSS
# ============================================================
class RSSCollector:
    def __init__(self, src: dict, client: httpx.AsyncClient):
        self.src = src
        self.client = client

    async def collect(self) -> list[RawItem]:
        name = self.src.get("name") or self.src.get("url", "rss")
        url = self.src.get("url")
        if not url:
            return []
        try:
            resp = await self.client.get(url, headers={"User-Agent": UA}, timeout=30)
            resp.raise_for_status()
            feed = feedparser.parse(resp.content)
        except Exception as exc:
            log.warning("RSS 采集失败 [%s]: %s", name, exc)
            return []

        items: list[RawItem] = []
        for entry in feed.entries[:40]:
            title = (entry.get("title") or "").strip()
            link = (entry.get("link") or "").strip()
            if not title:
                continue

            raw_html = ""
            if entry.get("content"):
                raw_html = entry["content"][0].get("value", "")
            if not raw_html:
                raw_html = entry.get("summary", "")
            content = html_to_text(raw_html)

            published = ""
            for key in ("published_parsed", "updated_parsed"):
                parsed = entry.get(key)
                if parsed:
                    published = _iso(datetime(*parsed[:6], tzinfo=timezone.utc))
                    break
            if not published:
                for key in ("published", "updated"):
                    if entry.get(key):
                        try:
                            published = _iso(parsedate_to_datetime(entry[key]))
                            break
                        except Exception:
                            pass

            items.append(RawItem(
                source=name, source_type="rss", title=title, url=link,
                content=content, published_at=published,
                weight=float(self.src.get("weight", 1.0)),
            ))
        log.info("RSS [%s] 抓到 %d 条", name, len(items))
        return items


# ============================================================
#  IMAP 邮箱
# ============================================================
class IMAPCollector:
    def __init__(self, src: dict):
        self.src = src

    def _decode(self, value: str | None) -> str:
        if not value:
            return ""
        try:
            return str(make_header(decode_header(value)))
        except Exception:
            return value

    @staticmethod
    def _body_text(msg: email.message.Message) -> str:
        parts: list[str] = []
        if msg.is_multipart():
            for part in msg.walk():
                ctype = part.get_content_type()
                disp = str(part.get("Content-Disposition") or "")
                if "attachment" in disp:
                    continue
                if ctype in ("text/plain", "text/html"):
                    try:
                        payload = part.get_payload(decode=True) or b""
                        charset = part.get_content_charset() or "utf-8"
                        parts.append(payload.decode(charset, "ignore"))
                    except Exception:
                        continue
        else:
            try:
                payload = msg.get_payload(decode=True) or b""
                charset = msg.get_content_charset() or "utf-8"
                parts.append(payload.decode(charset, "ignore"))
            except Exception:
                pass
        joined = "\n".join(parts)
        return html_to_text(joined) if "<" in joined else joined.strip()

    def collect(self) -> list[RawItem]:
        """imaplib 是同步的，调用方用 to_thread 包一层。"""
        src = self.src
        name = src.get("name", "邮箱")
        host = src.get("host")
        if not host:
            return []
        since_days = int(src.get("since_days", 3))
        since = (datetime.now(CST) - timedelta(days=since_days)).strftime("%d-%b-%Y")

        items: list[RawItem] = []
        conn = None
        try:
            if src.get("ssl", True):
                conn = imaplib.IMAP4_SSL(host, int(src.get("port", 993)), timeout=30)
            else:
                conn = imaplib.IMAP4(host, int(src.get("port", 143)), timeout=30)
            conn.login(src.get("username", ""), src.get("password", ""))
            conn.select(src.get("mailbox", "INBOX"), readonly=True)

            typ, data = conn.search(None, f'(UNSEEN SINCE "{since}")')
            if typ != "OK":
                typ, data = conn.search(None, f'(SINCE "{since}")')
            ids = (data[0].split() if data and data[0] else [])[-40:]

            for num in ids:
                typ, fetched = conn.fetch(num, "(BODY.PEEK[])")
                if typ != "OK" or not fetched or not fetched[0]:
                    continue
                raw = fetched[0][1]
                if not isinstance(raw, (bytes, bytearray)):
                    continue
                msg = email.message_from_bytes(raw)
                subject = self._decode(msg.get("Subject")) or "(无主题)"
                sender = self._decode(msg.get("From"))
                published = ""
                if msg.get("Date"):
                    try:
                        published = _iso(parsedate_to_datetime(msg["Date"]))
                    except Exception:
                        pass
                body = self._body_text(msg)[:8000]
                items.append(RawItem(
                    source=name, source_type="imap",
                    title=subject if not sender else f"{subject}",
                    url="", content=f"发件人: {sender}\n{body}",
                    published_at=published,
                    weight=float(src.get("weight", 1.5)),
                ))
            log.info("邮箱 [%s] 抓到 %d 封", name, len(items))
        except Exception as exc:
            log.warning("邮箱采集失败 [%s]: %s", name, exc)
        finally:
            if conn is not None:
                try:
                    conn.logout()
                except Exception:
                    pass
        return items


# ============================================================
#  网页监控
# ============================================================
class WebCollector:
    """模式A：按选择器结构化提取。模式B：变化检测，只挑新出现的行。"""

    def __init__(self, src: dict, client: httpx.AsyncClient, db):
        self.src = src
        self.client = client
        self.db = db

    async def collect(self) -> list[RawItem]:
        src = self.src
        name = src.get("name", "网页")
        url = src.get("url")
        if not url:
            return []
        try:
            resp = await self.client.get(url, headers={"User-Agent": UA}, timeout=30)
            resp.raise_for_status()
            resp.encoding = resp.encoding or "utf-8"
            html = resp.text
        except Exception as exc:
            log.warning("网页采集失败 [%s]: %s", name, exc)
            return []

        item_selector = src.get("item_selector")
        if not item_selector and src.get("auto_detect"):
            found = detect_selectors(html, top=1)
            if found:
                item_selector = found[0]["selector"]
                log.info("网页 [%s] 自动识别到列表选择器 %s（命中 %d 项，样例: %s）",
                         name, item_selector, found[0]["count"], found[0]["sample_title"])
            else:
                log.warning("网页 [%s] 自动识别失败，退回变化检测模式", name)

        soup = BeautifulSoup(html, "html.parser")
        if item_selector:
            return self._structured(soup, url, name, item_selector)
        return self._changed_lines(soup, url, name)

    def _structured(self, soup: BeautifulSoup, base_url: str, name: str,
                    item_selector: str) -> list[RawItem]:
        src = self.src
        title_sel = src.get("title_selector") or "a"
        link_sel = src.get("link_selector") or "a"
        date_sel = src.get("date_selector")
        from urllib.parse import urljoin

        out: list[RawItem] = []
        nodes = soup.select(item_selector)
        if not nodes:
            log.warning("网页 [%s] 选择器 %s 没匹配到任何元素", name, item_selector)
            return []

        for node in nodes[:60]:
            tnode = node.select_one(title_sel) or node
            lnode = node.select_one(link_sel) or tnode
            title = clean_title(tnode.get_text(" ", strip=True))
            if not title or len(title) < 4:
                continue
            href = lnode.get("href", "") if hasattr(lnode, "get") else ""
            node_text = node.get_text(" ", strip=True)

            # 日期：优先用指定的 date_selector，否则在整条记录里找
            published = ""
            if date_sel:
                dnode = node.select_one(date_sel)
                if dnode is not None:
                    published = extract_date(dnode.get_text(" ", strip=True))
            if not published:
                published = extract_date(node_text)

            out.append(RawItem(
                source=name, source_type="web", title=title,
                url=urljoin(base_url, href) if href else base_url,
                content=node_text[:4000],
                published_at=published,
                weight=float(src.get("weight", 1.5)),
            ))
        dated = sum(1 for i in out if i.published_at)
        log.info("网页 [%s] 结构化提取 %d 条（其中 %d 条抽出日期）",
                 name, len(out), dated)
        return out

    def _changed_lines(self, soup: BeautifulSoup, url: str, name: str) -> list[RawItem]:
        """把页面正文拆成行，跟上次快照比对，只产出新出现的行。"""
        src = self.src
        from urllib.parse import urljoin

        for tag in soup(["script", "style", "noscript", "nav", "footer"]):
            tag.decompose()

        lines: list[str] = []
        for a in soup.find_all("a"):
            text = a.get_text(" ", strip=True)
            if 6 <= len(text) <= 200:
                lines.append(f"{text}\t{urljoin(url, a.get('href', ''))}")
        if not lines:
            for raw in soup.get_text("\n").splitlines():
                text = raw.strip()
                if 6 <= len(text) <= 200:
                    lines.append(text)

        # 去重、保序
        seen, ordered = set(), []
        for line in lines:
            if line not in seen:
                seen.add(line)
                ordered.append(line)

        key = f"web::{url}"
        previous_raw = self.db.get_snapshot(key)
        previous = set(previous_raw.split("\n")) if previous_raw else set()
        first_time = previous_raw is None
        self.db.set_snapshot(key, "\n".join(ordered))

        if first_time:
            log.info("网页 [%s] 首次抓取，建立基线 %d 行（本次不产出条目）", name, len(ordered))
            return []

        fresh = [line for line in ordered if line not in previous]
        out: list[RawItem] = []
        for line in fresh[:40]:
            if "\t" in line:
                title, href = line.split("\t", 1)
            else:
                title, href = line, url
            out.append(RawItem(
                source=name, source_type="web", title=title, url=href,
                content=f"来源页面: {url}\n新增内容: {title}",
                weight=float(src.get("weight", 1.5)),
            ))
        log.info("网页 [%s] 发现 %d 条新增", name, len(out))
        return out


# ============================================================
#  JSON API（给那些列表由 JS 异步加载、静态 HTML 抓不到的站用）
# ============================================================
def _dig(obj, path: str):
    """按 "a.b.c" 取嵌套值；支持列表下标 a.0.b。取不到返回 None。"""
    cur = obj
    for part in str(path).split("."):
        if cur is None:
            return None
        if isinstance(cur, list):
            try:
                cur = cur[int(part)]
            except (ValueError, IndexError):
                return None
        elif isinstance(cur, dict):
            cur = cur.get(part)
        else:
            return None
    return cur


def _value_to_iso(value) -> str:
    """把接口返回的日期值转成 YYYY-MM-DD。支持 unix 秒/毫秒时间戳和字符串。"""
    if value is None or value == "":
        return ""
    # 数字时间戳
    try:
        num = float(value)
        if num > 1e11:      # 毫秒
            num /= 1000.0
        if 1e9 < num < 4e9:  # 合理的 unix 秒区间（2001~2096）
            return datetime.fromtimestamp(num, CST).strftime("%Y-%m-%d")
    except (TypeError, ValueError):
        pass
    # 字符串日期
    text = str(value)
    iso = extract_date(text)
    if iso:
        return iso
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(text[:19], fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    return ""


def _parse_json(resp: httpx.Response):
    """容错解析 JSON。

    有些站（比如北医就业）返回头写的是 text/html，body 却是 JSON，
    所以不能信 content-type，得看内容。
    """
    try:
        return resp.json()
    except Exception:
        pass
    text = resp.text.lstrip("\ufeff \t\r\n")
    for opener in ("{", "["):
        idx = text.find(opener)
        if idx > 0:
            text = text[idx:]
            break
    return json.loads(text)


class APICollector:
    """配置化的 JSON 接口采集器。

    配置项见 config.example.yaml 的 sources.api 段。
    """

    def __init__(self, src: dict, client: httpx.AsyncClient):
        self.src = src
        self.client = client

    async def collect(self) -> list[RawItem]:
        src = self.src
        name = src.get("name", "API")
        url = src.get("url")
        if not url:
            return []

        headers = {"User-Agent": UA}
        headers.update(src.get("headers") or {})
        params = src.get("params") or {}
        form = src.get("form")
        timeout = float(src.get("timeout_seconds", 30))
        method = str(src.get("method", "GET")).upper()

        try:
            if method == "POST":
                kwargs = {"params": params, "headers": headers, "timeout": timeout}
                if form:
                    kwargs["data"] = form
                elif src.get("json"):
                    kwargs["json"] = src["json"]
                resp = await self.client.post(url, **kwargs)
            else:
                resp = await self.client.get(url, params=params, headers=headers,
                                             timeout=timeout)
            resp.raise_for_status()
            payload = _parse_json(resp)
        except Exception as exc:
            log.warning("API 采集失败 [%s]: %s", name, exc)
            return []

        rows = _dig(payload, src.get("items_path", "data"))
        if not isinstance(rows, list):
            log.warning("API [%s] items_path=%r 没取到数组（顶层键: %s）",
                        name, src.get("items_path"), list(payload)[:8])
            return []

        title_field = src.get("title_field", "title")
        url_field = src.get("url_field", "url")
        url_template = src.get("url_template", "")
        date_field = src.get("date_field", "")
        content_fields = src.get("content_fields") or []
        labels = src.get("field_labels") or {}
        limit = int(src.get("limit", 60))

        out: list[RawItem] = []
        for row in rows[:limit]:
            if not isinstance(row, dict):
                continue
            title = str(_dig(row, title_field) or "").strip()
            if not title:
                continue

            if url_template:
                try:
                    link = url_template.format(**row)
                except (KeyError, IndexError):
                    link = url_template
            else:
                link = str(_dig(row, url_field) or "")

            # 接口常返回相对路径，用 url_base 补全
            base = src.get("url_base")
            if base and link.startswith("/"):
                link = base.rstrip("/") + link

            published = _value_to_iso(_dig(row, date_field)) if date_field else ""
            expires = ""
            expire_field = src.get("expire_field")
            if expire_field:
                expires = _value_to_iso(_dig(row, expire_field))

            lines = []
            for field in content_fields:
                value = _dig(row, field)
                if value in (None, "", [], {}):
                    continue
                text = html_to_text(str(value)) if "<" in str(value) else str(value)
                text = text.strip()
                if not text:
                    continue
                label = labels.get(field, field)
                lines.append(f"{label}：{text[:600]}")

            out.append(RawItem(
                source=name, source_type="api", title=title,
                url=link, content="\n".join(lines)[:8000],
                published_at=published, expires_at=expires,
                weight=float(src.get("weight", 1.3)),
            ))
        dated = sum(1 for i in out if i.published_at)
        log.info("API [%s] 取到 %d 条（其中 %d 条有日期）", name, len(out), dated)
        return out


# ============================================================
#  汇总
# ============================================================
async def collect_all(cfg: dict, db, clients,
                      sources: list[dict] | None = None) -> list[RawItem]:
    """采集所有到期的源。

    clients 可以是单个 AsyncClient，也可以是 {True: 校验的, False: 不校验的}
    两个客户端——有些学校站（比如北大资助中心 www.sfao.pku.edu.cn）证书链
    不完整，必须跳过校验才连得上，但又不想全局关掉校验。
    """
    import asyncio

    from .config import enabled_sources

    def pick(src: dict):
        if not isinstance(clients, dict):
            return clients
        return clients[bool(src.get("verify_ssl", True))]

    srcs = sources if sources is not None else enabled_sources(cfg)
    tasks = []
    for src in srcs:
        client = pick(src)
        stype = src.get("_type") or src.get("type")
        if stype == "rss":
            tasks.append(RSSCollector(src, client).collect())
        elif stype == "web":
            tasks.append(WebCollector(src, client, db).collect())
        elif stype == "api":
            tasks.append(APICollector(src, client).collect())
        elif stype == "imap":
            tasks.append(asyncio.to_thread(IMAPCollector(src).collect))

    results = await asyncio.gather(*tasks, return_exceptions=True)
    out: list[RawItem] = []
    for res in results:
        if isinstance(res, Exception):
            log.warning("采集任务异常: %s", res)
            continue
        out.extend(res)
    log.info("本轮共采集 %d 条原始条目", len(out))
    return out
