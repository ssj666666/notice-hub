"""自检脚本：不开微信、不连真邮箱，用本地假网页验证全链路。

跑法:
    py -3.12 tools/selftest.py

它会验证：规则预筛 / 截止日期加权 / 网页变化检测 / 去重 /
         未配微信时不误标已推送 / 消息格式化
"""
from __future__ import annotations

import asyncio
import http.server
import socket
import sys
import tempfile
import threading
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.analyze import prescreen                     # noqa: E402
from app.collectors import RawItem                    # noqa: E402
from app.config import DEFAULTS, _deep_merge          # noqa: E402
from app.db import CST, Database                      # noqa: E402
from app.notify import build_message                  # noqa: E402
from app.pipeline import Pipeline                     # noqa: E402

PASS, FAIL = [], []


def check(name: str, condition: bool, detail: str = "") -> None:
    (PASS if condition else FAIL).append(name)
    mark = "PASS" if condition else "FAIL"
    print(f"  [{mark}] {name}" + (f"  -> {detail}" if detail and not condition else ""))


# ---------------- 本地假网页 ----------------
PAGE = {"html": "<html><body></body></html>"}


class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        body = PAGE["html"].encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


def start_server() -> tuple[http.server.ThreadingHTTPServer, str]:
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, f"http://127.0.0.1:{server.server_address[1]}/"


def page(*links: tuple[str, str]) -> str:
    body = "".join(f'<a href="{href}">{text}</a>\n' for href, text in links)
    return f"<html><body>{body}</body></html>"


# ---------------- 各段测试 ----------------
def test_prescreen() -> None:
    print("\n[1] 规则预筛")
    cfg = _deep_merge(DEFAULTS, {
        "filter": {
            "l3_keywords": ["补退选", "选课", "报到"],
            "l2_keywords": ["奖学金", "考试"],
            "drop_keywords": ["活动回顾", "圆满结束"],
            "deadline_urgent_days": 3,
        }
    })

    def item(title: str, content: str = "") -> RawItem:
        return RawItem(source="t", source_type="test", title=title, content=content)

    r = prescreen(item("关于开展补退选工作的通知"), cfg)
    check("L3 关键词 -> 5 分", r["importance"] == 5, str(r))

    r = prescreen(item("奖学金评定结果公示"), cfg)
    check("L2 关键词 -> 4 分", r["importance"] == 4, str(r))

    r = prescreen(item("秋季运动会活动回顾"), cfg)
    check("drop 关键词 -> 丢弃", r["dropped"] is True, str(r))

    r = prescreen(item("校园随手拍"), cfg)
    check("无关键词 -> 2 分", r["importance"] == 2, str(r))

    # 截止日期加权：L2 话题 + 2 天后截止 -> 升到 5
    soon = datetime.now(CST).date() + timedelta(days=2)
    content = f"考试安排已发布，办理时间截止 {soon.month}月{soon.day}日，请及时确认。"
    r = prescreen(item("本学期考试安排", content), cfg)
    check("L2 + 近截止 -> 升到 5 分", r["importance"] == 5, str(r))

    # 远期日期不该加权
    far = datetime.now(CST).date() + timedelta(days=40)
    content = f"考试安排已发布，截止 {far.month}月{far.day}日。"
    r = prescreen(item("本学期考试安排", content), cfg)
    check("L2 + 远期截止 -> 保持 4 分", r["importance"] == 4, str(r))

    # drop 词在正文里、但标题命中了 L3 -> 不该被丢
    r = prescreen(item("补退选工作安排", "本次活动回顾见附件"), cfg)
    check("L3 优先于 drop", r["dropped"] is False and r["importance"] == 5, str(r))


def test_message() -> None:
    print("\n[2] 消息格式化")
    title, content, url = build_message({
        "title": "补退选通知", "summary": "9月20日前完成补退选",
        "deadline": "2026-09-20", "audience": "2026级本科",
        "action": "登录教务网操作", "source": "教务部", "url": "https://x.test/1",
    })
    check("标题正确", title == "补退选通知")
    check("含截止日期", "2026-09-20" in content, content)
    check("含来源", "教务部" in content, content)
    check("链接正确", url == "https://x.test/1")


async def test_pipeline(base_url: str) -> None:
    print("\n[3] 全链路：网页变化检测 + 去重")
    tmp = Path(tempfile.mkdtemp(prefix="noticehub-test-"))
    db = Database(tmp / "test.db")
    cfg = _deep_merge(DEFAULTS, {
        "sources": {"web": [{"name": "假通知页", "enabled": True,
                             "url": base_url, "weight": 1.5}]},
        "filter": {
            "l3_keywords": ["补退选", "选课", "停电"],
            "l2_keywords": ["奖学金"],
            "drop_keywords": ["活动回顾"],
            "deadline_urgent_days": 3,
        },
        "notify": {"min_importance": 4, "wechat": {"enabled": False}},
    })
    pipeline = Pipeline(cfg, db)

    # 第一次：只建基线，不应产出条目
    PAGE["html"] = page(("/n1", "关于2026年秋季学期选课工作的通知"),
                        ("/n2", "关于举办秋季运动会的通知"))
    r1 = await pipeline.run_once(force=True)
    check("首轮只建基线，不产出条目", r1.get("new") == 0, str(r1))

    # 第二次：新增两行 -> 应产出 2 条，且分级正确
    PAGE["html"] = page(("/n1", "关于2026年秋季学期选课工作的通知"),
                        ("/n2", "关于举办秋季运动会的通知"),
                        ("/n3", "关于开展补退选工作的通知"),
                        ("/n4", "研究生国家奖学金评审通知"))
    r2 = await pipeline.run_once(force=True)
    check("次轮发现 2 条新增", r2.get("new") == 2, str(r2))

    rows = {i["title"]: i for i in db.list_items(include_dropped=True)}
    check("抓到补退选条目", "关于开展补退选工作的通知" in rows, str(list(rows)))
    if "关于开展补退选工作的通知" in rows:
        check("补退选被判 5 分", rows["关于开展补退选工作的通知"]["importance"] == 5)
    if "研究生国家奖学金评审通知" in rows:
        check("奖学金被判 4 分", rows["研究生国家奖学金评审通知"]["importance"] == 4)

    # 第三次：页面没变 -> 不应有新增（去重）
    r3 = await pipeline.run_once(force=True)
    check("页面未变时 0 新增（去重生效）", r3.get("new") == 0, str(r3))

    # 第四次：加入一条 drop 关键词的行 -> 应被丢弃
    PAGE["html"] = page(("/n1", "关于2026年秋季学期选课工作的通知"),
                        ("/n2", "关于举办秋季运动会的通知"),
                        ("/n3", "关于开展补退选工作的通知"),
                        ("/n4", "研究生国家奖学金评审通知"),
                        ("/n5", "运动会活动回顾"))
    await pipeline.run_once(force=True)
    dropped = [i for i in db.list_items(include_dropped=True) if i["dropped"]]
    check("活动回顾被丢弃", len(dropped) == 1, str([d["title"] for d in dropped]))

    # 关键：微信没配，不能把条目标成已推送
    high = [i for i in db.list_items(min_importance=4) if not i["dropped"]]
    check("有达到推送门槛的条目", len(high) >= 2, str(len(high)))
    check("未配微信时未误标已推送", all(i["pushed"] == 0 for i in high),
          str([(i["title"], i["pushed"]) for i in high]))
    check("未配微信时推送数为 0", r3.get("pushed") == 0)

    stats = db.stats()
    check("统计接口可用", stats["total"] > 0 and "last_run" in stats, str(stats))
    db.close()
    print(f"\n  测试数据目录（可删）: {tmp}")


def test_dates() -> None:
    print("\n[0] 日期抽取（之前漏了，这是「抓到 7 月旧通知」的根因）")
    from datetime import datetime

    from app.autodetect import extract_date
    from app.db import CST

    year = datetime.now(CST).year
    cases = [
        ("2026-09-10 关于选课的通知", "2026-09-10"),
        ("2026/09/10 通知", "2026-09-10"),
        ("2026.09.10", "2026-09-10"),
        ("2026年9月10日 通知", "2026-09-10"),
        ("17 Nov 2025 2025年度评审结果", "2025-11-17"),
        ("Nov 17, 2025", "2025-11-17"),
        ("14 Aug 关于餐厅排风改造的通知", f"{year}-08-14"),
        ("9月10日 讲座", f"{year}-09-10"),
        ("没有任何日期", ""),
    ]
    for text, want in cases:
        got = extract_date(text)
        check(f"抽日期 {text[:22]!r} -> {want or '(空)'}", got == want,
              f"实际 {got!r}")

    # 回归：无年份时，未来日期一定是去年（发布日不可能在未来）
    from datetime import date
    today = date.today()
    ahead = today + timedelta(days=1)
    if ahead.year == today.year:
        got = extract_date(f"{ahead.month}月{ahead.day}日")
        check(f"明天 {ahead.month}月{ahead.day}日 应归到去年",
              got.startswith(str(today.year - 1)), f"实际 {got}")
    got_today = extract_date(f"{today.month}月{today.day}日")
    check("当天日期归到今年", got_today == today.isoformat(), f"实际 {got_today}")

    # 回归：医学部页面那种「22 Sep」，在今天 9/18 时必须是去年
    from datetime import datetime as _dt
    from app.db import CST as _CST
    _today = _dt.now(_CST).date()
    if _today.month == 9 and _today.day < 22:
        got = extract_date("22 Sep 关于供暖系统上水试压的通知")
        check("页面上不带年份的未来月份归到去年",
              got == f"{_today.year - 1}-09-22", f"实际 {got}")


def test_stale_guard() -> None:
    print("\n[0c] 日期解析工具")
    from app.pipeline import _parse_date

    # 回归：采集层给的是纯日期字符串，不能和带时区的 now() 直接相减
    got = _parse_date("2026-09-10")
    check("纯日期可解析", got is not None and got.isoformat() == "2026-09-10", str(got))
    check("空值返回 None", _parse_date("") is None)
    check("RFC822 可解析",
          _parse_date("Tue, 10 Sep 2026 12:00:00 +0800") is not None)
    check("乱码返回 None", _parse_date("不是日期") is None)


def test_categories() -> None:
    print("\n[0b] 分类体系")
    from app.categories import CATEGORY_KEYS, DEFAULT_CATEGORY, classify

    cases = [
        ("2026-2027学年第一学期本科生选课通知", "教务学业"),
        ("2026年下半年全国大学英语四、六级考试报名通知", "教务学业"),
        ("关于举办量子力学前沿学术讲座的通知", "讲座讲坛"),
        ("北京大学百家讲坛：科学与人文之间", "讲座讲坛"),
        ("某科技公司校园招聘宣讲会", "就业实习"),
        ("2026年国家奖学金评审工作通知", "奖助学金"),
        ("本科生科研训练项目立项通知", "科研竞赛"),
        ("关于宿舍区停电检修的通知", "校园生活"),
        ("本学期体质测试工作安排", "体育活动"),
        ("2027年春季学期海外交换项目报名", "国际交流"),
        ("校学生会招新公告", "社团活动"),
        ("关于国庆节放假安排的通知", "行政通知"),
        # 回归：招生宣讲会曾被「宣讲会」抢到就业实习
        ("国际关系学院本科专业二次招生宣讲会通知", "教务学业"),
        ("今天天气不错", DEFAULT_CATEGORY),
    ]
    for text, want in cases:
        got, conf = classify(text)
        check(f"{text[:22]} -> {want}", got == want, f"实际 {got} (conf={conf})")

    # 所有类目名必须稳定，因为它们会写进数据库
    check("类目数量为 10+1", len(CATEGORY_KEYS) == 10, str(len(CATEGORY_KEYS)))
    check("有「其他」兜底", DEFAULT_CATEGORY == "其他")


def test_recency() -> None:
    print("\n[0d] 日期衰减与过期闸门")
    from app.analyze import prescreen

    cfg = _deep_merge(DEFAULTS, {"filter": {
        "l3_keywords": ["停电", "停水", "选课"],
        "l2_keywords": [], "drop_keywords": [],
        "importance_decay_days": 7,
        "visible_days": 30,
        "deadline_urgent_days": 3,
    }})
    today = datetime.now(CST).date()

    def raw(title: str, days_ago: int | None) -> RawItem:
        published = (today - timedelta(days=days_ago)).isoformat() if days_ago is not None else ""
        return RawItem(source="t", source_type="test", title=title,
                       published_at=published)

    # 就是用户投诉的那一条：4 月的停电停水通知被判了 5 分
    r = prescreen(raw("关于医学部分区分时段停电停水的通知", 168), cfg)
    check("5 个月前的停电通知降到 2 分", r["importance"] == 2, str(r["importance"]))
    check("降级原因写进 analysis", "过期" in r["analysis"], r["analysis"])
    check("age_days 算对", r["age_days"] == 168, str(r["age_days"]))

    r = prescreen(raw("关于医学部分区分时段停电停水的通知", 1), cfg)
    check("昨天的停电通知仍是 5 分", r["importance"] == 5, str(r["importance"]))

    r = prescreen(raw("本科生选课通知", 10), cfg)
    check("10 天前的选课通知降到 2 分", r["importance"] == 2, str(r["importance"]))

    r = prescreen(raw("本科生选课通知", 3), cfg)
    check("3 天前的选课通知不降级", r["importance"] == 5, str(r["importance"]))

    r = prescreen(raw("停电通知", None), cfg)
    check("无日期不降级（无从判断）", r["importance"] == 5, str(r["importance"]))
    check("无日期 age_days 为 None", r["age_days"] is None)


def test_expiry_field() -> None:
    """讲座类：发布很久但还没开讲的，不该被判过期。"""
    print("\n[0e] 有效期字段（expires_at）")
    from app.analyze import _is_future, prescreen

    today = datetime.now(CST).date()
    past = (today - timedelta(days=3)).isoformat()
    long_ago = (today - timedelta(days=40)).isoformat()
    upcoming = (today + timedelta(days=10)).isoformat()

    check("未来日期判为有效", _is_future(upcoming) is True)
    check("今天算有效", _is_future(today.isoformat()) is True)
    check("过去日期判为无效", _is_future(past) is False)
    check("空值判为无效", _is_future("") is False)

    cfg = _deep_merge(DEFAULTS, {"filter": {
        "l3_keywords": ["讲座", "停水"], "l2_keywords": [], "drop_keywords": [],
        "importance_decay_days": 7, "visible_days": 30,
        "deadline_urgent_days": 3,
    }})

    # 40 天前发布、下周才开讲的讲座 —— 不该降级
    r = prescreen(RawItem(source="t", source_type="test",
                          title="前沿讲座：量子计算", published_at=long_ago,
                          expires_at=upcoming), cfg)
    check("未开讲的讲座不降级", r["importance"] == 5, str(r["importance"]))

    # 40 天前发布、已经开完的讲座 —— 该降级
    r = prescreen(RawItem(source="t", source_type="test",
                          title="前沿讲座：量子计算", published_at=long_ago,
                          expires_at=past), cfg)
    check("已开完的讲座降级", r["importance"] == 2, str(r["importance"]))

    # 40 天前的停水通知（没有有效期）—— 该降级
    r = prescreen(RawItem(source="t", source_type="test",
                          title="关于停水的通知", published_at=long_ago), cfg)
    check("普通旧通知降级", r["importance"] == 2, str(r["importance"]))


def test_api_collector() -> None:
    print("\n[0f] API 采集器辅助函数")
    from app.collectors import _dig, _value_to_iso, clean_title

    payload = {"data": {"list": [{"a": 1}, {"a": 2}]}, "x": 9}
    check("_dig 嵌套字典", _dig(payload, "data.list.1.a") == 2, str(_dig(payload, "data.list.1.a")))
    check("_dig 取不到返回 None", _dig(payload, "nope.deep.path") is None)
    check("_dig 单层", _dig(payload, "x") == 9)

    check("unix 秒 -> 日期", _value_to_iso(1786812171).startswith("2026-08-"),
          _value_to_iso(1786812171))
    check("unix 毫秒 -> 日期", _value_to_iso(1786812171000).startswith("2026-08-"),
          _value_to_iso(1786812171000))
    check("日期时间串 -> 日期", _value_to_iso("2026-08-19 14:30:00") == "2026-08-19",
          _value_to_iso("2026-08-19 14:30:00"))
    check("空值 -> 空串", _value_to_iso(None) == "")

    check("剥掉标题里的日期前缀",
          clean_title("2026-08-31 关于新学期医学部学生社团注册的通知")
          == "关于新学期医学部学生社团注册的通知")
    check("没有前缀时原样返回", clean_title("选课通知") == "选课通知")
    check("不会把标题吃空",
          clean_title("2026-08-31") == "2026-08-31")


def test_detect_selectors() -> None:
    """选择器自动识别：三种典型写法都要能认出来（离线，不联网）。"""
    print("\n[0g] 选择器自动识别")
    from app.autodetect import detect_selectors

    # ① class 写法
    html_class = """<html><body><div class="notice_list">
      <div class="notice_item"><a href="/a">2026年秋季学期本科生选课通知</a><span>2026-09-10</span></div>
      <div class="notice_item"><a href="/b">关于补退选工作的通知</a><span>2026-09-11</span></div>
      <div class="notice_item"><a href="/c">四六级报名通知</a><span>2026-09-12</span></div>
      <div class="notice_item"><a href="/d">推免资格申请通知</a><span>2026-09-13</span></div>
    </div></body></html>"""
    res = detect_selectors(html_class)
    check("class 写法能识别", bool(res) and "notice_item" in res[0]["selector"],
          str(res[:1]))

    # ② id 前缀写法（无 class）
    html_id = """<html><body><ul>
      <li id="line_u11_0"><a href="/a">2027年接收推荐免试研究生复试通知</a><span>[2026-09-14]</span></li>
      <li id="line_u11_1"><a href="/b">后勤场馆岗位招聘启事</a><span>[2026-09-02]</span></li>
      <li id="line_u11_2"><a href="/c">关于接收推免生的说明</a><span>[2026-09-01]</span></li>
      <li id="line_u11_3"><a href="/d">暑假带值班表</a><span>[2026-07-01]</span></li>
    </ul></body></html>"""
    res = detect_selectors(html_id)
    check("id 前缀写法能识别",
          bool(res) and "id^=" in res[0]["selector"], str(res[:1]))

    # ③ 孙节点写法（li 无 class 无 id，靠最近的有 class 的祖先）
    html_deep = """<html><body><div class="list30"><ul>
      <li><a href="/a">关于加强期末考试纪律的通知</a><span>2025-12-23</span></li>
      <li><a href="/b">关于学籍异动办理的通知</a><span>2025-09-17</span></li>
      <li><a href="/c">关于成绩复核的通知</a><span>2025-09-04</span></li>
      <li><a href="/d">关于转专业手续的通知</a><span>2025-04-15</span></li>
    </ul></div></body></html>"""
    res = detect_selectors(html_deep)
    check("孙节点写法能识别",
          bool(res) and "list30" in res[0]["selector"], str(res[:1]))

    # 导航菜单不该被误选成列表
    html_nav = """<html><body><ul class="nav">
      <li><a href="/1">首页</a></li><li><a href="/2">概况</a></li>
      <li><a href="/3">师资</a></li><li><a href="/4">联系</a></li>
      <li><a href="/5">更多</a></li>
    </ul></body></html>"""
    res = detect_selectors(html_nav)
    check("纯导航菜单不会被当成列表", not res, str(res[:1]))

    check("空页面返回空", detect_selectors("") == [])


def tag_corner(soup, tag):
    return soup


async def test_baseline_suppression(base_url: str) -> None:
    """结构化模式首次接入：历史条目必须存下来、但绝不推送。"""
    print("\n[4] 首次接入基线抑制（结构化模式）")
    tmp = Path(tempfile.mkdtemp(prefix="noticehub-base-"))
    db = Database(tmp / "base.db")
    cfg = _deep_merge(DEFAULTS, {
        "sources": {"web": [{
            "name": "结构化通知页", "enabled": True, "url": base_url,
            "item_selector": "div.notice_item",
            "title_selector": "a", "link_selector": "a", "weight": 1.5,
        }]},
        "filter": {"l3_keywords": ["选课", "报名", "补退选"], "l2_keywords": [],
                   "drop_keywords": [], "deadline_urgent_days": 3},
        "notify": {"min_importance": 4, "wechat": {"enabled": False}},
    })
    pipeline = Pipeline(cfg, db)

    PAGE["html"] = (
        '<div class="notice_list">'
        '<div class="notice_item"><div class="notice_box"><a href="/a">'
        '2026-2027学年第一学期本科生选课通知</a></div></div>'
        '<div class="notice_item"><div class="notice_box"><a href="/b">'
        '北京大学2026年跨学科项目报名通知</a></div></div>'
        '</div>')
    r = await pipeline.run_once(force=True)
    check("首轮抓到 2 条历史通知", r.get("new") == 2, str(r))

    rows = db.list_items(include_dropped=True)
    check("历史条目标记为基线", all(i["is_baseline"] == 1 for i in rows),
          str([(i["title"][:12], i["is_baseline"]) for i in rows]))
    check("历史条目被判 5 分（说明确实够重要）",
          all(i["importance"] == 5 for i in rows), str([i["importance"] for i in rows]))
    check("基线条目不在推送候选里",
          len(db.candidates_for_push(4)) == 0, str(len(db.candidates_for_push(4))))
    check("基线条目仍能在看板看到", len(rows) == 2)

    # 之后出现的新通知要正常成为推送候选
    PAGE["html"] = (
        '<div class="notice_list">'
        '<div class="notice_item"><div class="notice_box"><a href="/a">'
        '2026-2027学年第一学期本科生选课通知</a></div></div>'
        '<div class="notice_item"><div class="notice_box"><a href="/b">'
        '北京大学2026年跨学科项目报名通知</a></div></div>'
        '<div class="notice_item"><div class="notice_box"><a href="/c">'
        '关于补退选工作安排的通知</a></div></div>'
        '</div>')
    await pipeline.run_once(force=True)
    candidates = db.candidates_for_push(4)
    check("后续新通知进入推送候选", len(candidates) == 1,
          str([c["title"] for c in candidates]))
    check("候选里没有历史条目",
          all(c["is_baseline"] == 0 for c in candidates))
    db.close()


async def main() -> int:
    print("=" * 60)
    print("  通知中枢 自检")
    print("=" * 60)

    test_dates()
    test_stale_guard()
    test_recency()
    test_expiry_field()
    test_api_collector()
    test_detect_selectors()
    test_categories()
    test_prescreen()
    test_message()

    server, base_url = start_server()
    try:
        await test_pipeline(base_url)
        await test_baseline_suppression(base_url)
    finally:
        server.shutdown()

    print("\n" + "=" * 60)
    print(f"  通过 {len(PASS)} 项，失败 {len(FAIL)} 项")
    if FAIL:
        print("  失败项:")
        for name in FAIL:
            print(f"    - {name}")
    print("=" * 60)
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
