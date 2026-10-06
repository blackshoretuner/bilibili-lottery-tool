#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""自测：跑 python tests.py，不碰网络、不动你的账号。

重点盯已经踩过的坑，防止改代码时又踩回去。每条断言上面都写了
「当初错在哪」，别随手删。
"""

from __future__ import annotations

import datetime
import io
import os
import sys
import tempfile
import time
import types

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import main as M

D = tempfile.mkdtemp()
DAY = 86400
COOKIE = "SESSDATA=aa%2Cbb; bili_jct=deadbeef; DedeUserID=999"
today = datetime.date.today()
td = datetime.timedelta
ymd = lambda d: f"{d.year}年{d.month}月{d.day}日"
ts_of = lambda d: int(time.mktime(d.timetuple()))

passed = []


def ok(name: str, detail: str = "") -> None:
    passed.append(name)
    print(f"  ok  {name}{'  ' + detail if detail else ''}")


# ============================================================
# 1. 开奖日期：转发前必须把「今天几号」和「哪天开奖」算对
# ============================================================

def draw(text: str, pub: datetime.date | None = None) -> int:
    return M.parse_draw_date(text, ts_of(pub) if pub else 0)


def still_open(text: str, pub: datetime.date | None = None) -> bool | None:
    """None = 没解析出日期。"""
    ts = draw(text, pub)
    return None if not ts else ts >= time.time()


# 坑 1：开奖日返回的是当天零点，导致「今天 20:00 开奖」在下午跑脚本时
# 被判成已开奖跳过——可它明明还能参与。必须按当天最后一刻算。
assert still_open(f"转发抽奖 开奖时间{ymd(today)} 20:00", today - td(days=5)) is True, \
    "今天开奖的抽奖被误判成已开奖了"
assert still_open(f"转发抽奖 开奖时间{ymd(today)}", today - td(days=5)) is True
assert still_open(f"转发抽奖 {today.month}月{today.day}日开奖", today - td(days=5)) is True
ok("今天开奖仍算「还开着」", "（当天 23:59:59 之前都能参与）")

assert still_open(f"转发抽奖 {ymd(today - td(days=1))}开奖", today - td(days=10)) is False
assert still_open(f"转发抽奖 {ymd(today + td(days=1))}开奖", today - td(days=5)) is True
ok("昨天开奖跳过 / 明天开奖参与")

# 坑 2：只写月日不写年份时，按「今天」补年份。一条 2 月发布、写着「3月1日开奖」
# 的老抽奖，会因为距今超过半年而被补成明年 3 月 1 日，凭空变成「还开着」。
# 必须按动态发布时间补年份——开奖不可能早于发布。
feb = datetime.date(today.year, 2, 10)
assert still_open("转发抽奖 3月1日开奖", feb) is False, \
    "老抽奖的月日被补成了明年，会去转一个早就结束的抽奖"
assert datetime.date.fromtimestamp(draw("转发抽奖 3月1日开奖", feb)) == \
    datetime.date(today.year, 3, 1)
ok("只写月日时按发布时间补年份", "（2月发布+3月1日开奖 = 今年，已结束）")

# 跨年要正确：12 月发布、写「1月5日开奖」，是明年的 1 月 5 日
dec = datetime.date(today.year, 12, 20)
assert datetime.date.fromtimestamp(draw("转发抽奖 1月5日开奖", dec)) == \
    datetime.date(today.year + 1, 1, 5)
ok("跨年补年份正确", "（12月发布 + 1月5日 = 明年）")

# 早于发布日的日期不是开奖日，是活动开始日或往期回顾，要忽略
pub = today - td(days=30)
got = datetime.date.fromtimestamp(
    draw(f"活动 {ymd(today - td(days=60))} 开始，{ymd(today + td(days=5))} 开奖", pub))
assert got == today + td(days=5), got
ok("早于发布日的日期被忽略", "（不会把活动开始日当成开奖日）")

# 同时出现多个日期取最晚的：开奖总比活动开始晚
both = draw(f"{ymd(today + td(days=2))}开始 {ymd(today + td(days=9))}开奖", today)
assert datetime.date.fromtimestamp(both) == today + td(days=9)
ok("多个日期取最晚那个")

# 离谱的远期日期多半是解析错了（把发货承诺、版权年份当成开奖日），不采信
assert draw(f"转发抽奖 {ymd(today + td(days=1100))}开奖", today - td(days=2)) == 0
ok("三年后的日期不采信", "（退回按发布时间判断）")

# 坑 10：只写月日、且日期早于发布时，不能为了满足「开奖晚于发布」就无脑滚到明年。
# 真实翻车：一条 8-17 发的中奖公布帖写「8月16号抽1位」，被滚成明年 8-16，
# 于是判成「还开着」，去转了一个两周前就开完的抽奖。
late = today - td(days=1)
assert draw(f"转发抽奖 {late.month}月{late.day}号抽1位幸运鹅", today) == 0, \
    "早于发布日的月日被滚到明年了，会去转已经开完的抽奖"
ok("月日早于发布时不滚到明年", "（跨大半年的滚动不合理，直接不采信）")

# 但跨年是合理的：12 月发、1 月 5 日开奖
assert datetime.date.fromtimestamp(draw("转发抽奖 1月5日开奖", dec)) == \
    datetime.date(today.year + 1, 1, 5)
ok("合理的跨年滚动仍然保留", f"（12月20日发 + 1月5日 = 明年，滚 {16} 天）")

# 发布时间未知时（详情拉取失败的兜底），不能滤掉过去的日期，
# 否则「已开奖」这个信号就丢了
assert still_open(f"转发抽奖 {ymd(today - td(days=30))}开奖") is False, \
    "没有发布时间时，过去的开奖日期被丢掉了"
assert still_open(f"转发抽奖 {ymd(today + td(days=30))}开奖") is True
ok("没有发布时间时仍能识别已开奖")

# 各种写法都要认
for text, want in [
    ("【开奖时间】：2026 年 10 月 5 日凌晨", datetime.date(2026, 10, 5)),
    ("直播开奖日期：2026年8月8日18:00", datetime.date(2026, 8, 8)),
    ("本次抽奖将会在2026.4.30号抽出", datetime.date(2026, 4, 30)),
    ("活动时间：2026.01.31-2026.02.28", datetime.date(2026, 2, 28)),
]:
    ts = draw(text)
    assert ts and datetime.date.fromtimestamp(ts) == want, (text, ts)
assert draw("2026年13月45日 开奖") == 0          # 非法日期
assert draw("转发抽奖送手办，没写时间") == 0
ok("四种日期写法都能解析", "（非法日期和无日期返回 0）")

# 「2026年1月1日」里的「1月1日」不能被当成没写年份再补一次
x = draw("活动 2026年1月1日 开始，2026年3月1日 开奖")
assert datetime.date.fromtimestamp(x) == datetime.date(2026, 3, 1)
ok("带年份的日期不会被二次解析")


# ============================================================
# 2. Cookie 解析
# ============================================================

# 坑 3：标准库 SimpleCookie 遇到 bmg_af_sc={"none":...} 这种带大括号的值会
# 判定非法，然后把它「后面的所有字段」静默丢掉——包括 SESSDATA。
# 而这个字段在真实 Cookie 里的位置不固定，换个账号就炸。
# 字段值都是编的，但形状跟真 Cookie 一致（SESSDATA 带 %2C、rpdid 带管道符和
# 引号、bmg_af_sc 带大括号）——复现这个 bug 靠的是形状，不需要真凭证。
NASTY = ('buvid3=abc; bmg_af_sc={"none":{"on":1,"def":"i1.hdslb.com"}}; '
         "rpdid=|(u)YklYYku~0J'u~)JJlu|uY; "
         'SESSDATA=1234abcd%2C1900000000%2C0a1b2%2A11cd; '
         'bili_jct=0123abcd; DedeUserID=1000001; b_lsid=201448CC_1A03')
got = M.parse_cookie(NASTY)
assert got.get("SESSDATA", "").startswith("1234abcd%2C"), f"SESSDATA 丢了: {got}"
assert got.get("bili_jct") == "0123abcd" and got.get("DedeUserID") == "1000001"
assert got.get("bmg_af_sc") == '{"none":{"on":1,"def":"i1.hdslb.com"}}'
assert len(got) == 7, f"应解析出 7 个字段，实际 {len(got)}"
c = M.Bili(NASTY)
assert c.csrf == "0123abcd" and c.mid == "1000001"
ok("含大括号的字段不会吃掉后面的 SESSDATA", f"（{len(got)} 个字段全解析）")


# ============================================================
# 3. HTTP 错误要转成 ApiError
# ============================================================

class FakeResp:
    def __init__(self, code):
        self.status_code, self.text = code, "<html>err</html>"

    def json(self):
        raise ValueError("not json")


def client_returning(resp):
    b = M.Bili(COOKIE)
    b.http = types.SimpleNamespace(headers={}, cookies={},
                                   request=lambda m, u, **k: resp)
    return b


# 坑 4：raise_for_status() 抛的是 requests.HTTPError，躲过了所有 except ApiError，
# 结果一个可选来源下线就把整个脚本带走了。
import requests as _rq
for code, want in ((404, -404), (500, -500), (412, -412)):
    try:
        client_returning(FakeResp(code))._req("GET", "https://api.bilibili.com/x/dead")
        raise AssertionError(f"HTTP {code} 应该抛 ApiError")
    except _rq.exceptions.HTTPError:
        raise AssertionError(f"回归：HTTP {code} 仍抛 HTTPError")
    except M.ApiError as e:
        assert e.code == want, (code, e)
assert M.ApiError(-412, "").is_risk
assert M._collect(client_returning(FakeResp(404)),
                  "https://api.bilibili.com/x/dead", {}, "挂了的来源", False) == []
ok("HTTP 404/500/412 转成 ApiError", "（单个来源挂掉不影响其他来源）")


# ============================================================
# 4. 关键词判定
# ============================================================

C = M.DEFAULTS
cases = [
    ("转发本条动态抽奖送手办一个", True, "正常转发抽奖"),
    ("转发本条动态，包邮送键盘一把", True, "包邮送"),
    ("抽奖啦，关注我就有机会", False, "关注型：转发帮不上"),
    ("一键三连，评论区抽一位兄弟送出礼物", False, "评论区抽奖：转发无效"),
    ("今天来玩个抽卡游戏", False, "只有抽字没动作词"),
    ("转发这条视频给朋友看", False, "只有动作词没抽奖词"),
    ("转发抽奖已开奖，恭喜以下用户", False, "已开奖"),
    ("今日抽奖合集，转发传送门在评论区", False, "聚合号汇总帖"),
    ("建议是转发七八个抽奖后休息一下", False, "聚合号说明帖"),
    ("", False, "空正文"),
]
for text, want, why in cases:
    hit = bool(M.looks_like_lottery(M.Target(dyn_id="1", text=text), C))
    assert hit == want, f"{why}: {text!r} -> {hit}，期望 {want}"
assert M.looks_like_lottery(M.Target(dyn_id="1", text="转发抽奖送手办"), C) == "抽奖+转发"
ok("关键词判定", f"{len(cases)} 个用例，其中 {sum(1 for _, w, _ in cases if not w)} 个必须排除")


# ============================================================
# 5. 评论：定位评论区 + 按 rpid 复查
# ============================================================

def stub(handler):
    M.Bili._req = handler
    return M.Bili(COOKIE)


NAV = {"isLogin": True, "uname": "我", "mid": 999, "wbi_img": {
    "img_url": "https://x/bfs/wbi/7cd084941338484aae1ad9425b84077c.png",
    "sub_url": "https://x/bfs/wbi/4932caff0ff746eab6f01bf08b70ac45.png"}}

# 坑 5：评论区并不挂在动态自己身上。图文动态的评论区是 oid=379837513 type=11
# 这种，跟 dyn_id 毫无关系。列表接口经常不返回 basic，缺了就得回查详情；
# 以前是缺了就按 type=17 兜底，结果一律 -404，评论根本发不出去。
def h_detail(self, method, url, **kw):
    if "web-interface/nav" in url:
        return NAV
    if "/detail" in url:
        return {"item": {"id_str": "1152590577429119072",
                         "basic": {"comment_id_str": "379837513", "comment_type": 11},
                         "modules": {"module_author": {"mid": 7, "name": "图文UP"},
                                     "module_dynamic": {"desc": {"text": "转发抽奖"}}}}}
    raise AssertionError(url)


t = M.Target(dyn_id="1152590577429119072")
assert M.resolve_comment_target(stub(h_detail), t) is True
assert (t.comment_id, t.comment_type) == ("379837513", 11)
# 查不到就返回 False——宁可跳过评论，也不往错地方发
assert M.resolve_comment_target(stub(
    lambda s, m, u, **k: NAV if "nav" in u else {"item": {"id_str": "9", "basic": {},
                                                          "modules": {}}}),
    M.Target(dyn_id="9")) is False
ok("评论区定位", f"列表缺 basic 时回查详情，拿到 oid={t.comment_id} type={t.comment_type}")


def h_add(self, method, url, **kw):
    if "reply/add" in url:
        d = kw.get("data") or {}
        assert d["oid"] == "379837513" and d["type"] == 11, d
        return {"rpid": 88886666}
    return h_detail(self, method, url, **kw)


assert M.do_comment(stub(h_add), t, "参与一下") == 88886666
# 接口说成功但没给 rpid，不能谎报
assert M.do_comment(stub(lambda s, m, u, **k:
                         {} if "reply/add" in u else h_detail(s, m, u, **k)), t, "x") == 0
ok("发评论返回 rpid", "（没给 rpid 时返回 0，不谎报成功）")


# 坑 6：一开始靠翻评论列表第一页找 rpid，只在「刚发完」那一刻有效。
# 热门抽奖评论区一天几千条（实测有 18677 条的），隔天复查一律「没找到」，
# 全是假警报。改成按 rpid 直查，评论不存在会明确返回 12006。
def h_reply(payload):
    def f(self, method, url, **kw):
        if "/x/v2/reply/reply" in url:
            if isinstance(payload, Exception):
                raise payload
            return payload
        return h_detail(self, method, url, **kw)
    return f


assert M.verify_comment(stub(h_reply({"root": {"rpid": 1, "member": {"mid": 999}}})),
                        t, 1).startswith("已确认")
assert M.verify_comment(stub(h_reply(M.ApiError(M.NO_SUCH_REPLY, "没有该评论"))),
                        t, 1).startswith("没找到")
assert M.verify_comment(stub(h_reply({"root": {"rpid": 1, "member": {"mid": 12345}}})),
                        t, 1).startswith("存疑")
# 读不到要说「无法验证」，跟「没找到」区分开——前者不代表评论没发出去
assert M.verify_comment(stub(h_reply(M.ApiError(-404, "啥都木有"))), t, 1) \
    .startswith("无法验证")
assert M.verify_comment(stub(h_reply({"root": {}})), t, 1).startswith("无法验证")
assert M.verify_comment(stub(h_reply({})), t, 0).startswith("无法验证")
ok("按 rpid 复查评论", "已确认 / 没找到 / 存疑 / 无法验证 四种结论都对")

# 坑 8：刚发出去的评论，B 站要过几秒才索引得到。发完立刻查会报「没找到」，
# 隔两分钟再查全都在——实测三条里误报了两条。这种假警报比不验证还糟。
M.VERIFY_WAIT = 0                       # 测试里别真等
tries = {"n": 0}


def h_lag(self, method, url, **kw):
    if "/x/v2/reply/reply" in url:
        tries["n"] += 1
        if tries["n"] < 3:              # 前两次装作还没索引到
            raise M.ApiError(M.NO_SUCH_REPLY, "没有该评论")
        return {"root": {"rpid": 1, "member": {"mid": 999}}}
    return h_detail(self, method, url, **kw)


assert M.verify_comment(stub(h_lag), t, 1).startswith("已确认"), "索引延迟时应该重试"
assert tries["n"] == 3, tries
ok("索引延迟会重试", f"前 2 次没找到，第 {tries['n']} 次查到")

# 但一直查不到就得如实说没找到，不能无限重试假装成功
tries["n"] = 0


def h_gone(self, method, url, **kw):
    if "/x/v2/reply/reply" in url:
        tries["n"] += 1
        raise M.ApiError(M.NO_SUCH_REPLY, "没有该评论")
    return h_detail(self, method, url, **kw)


assert M.verify_comment(stub(h_gone), t, 1).startswith("没找到")
assert tries["n"] == M.VERIFY_ATTEMPTS, tries
ok("重试到头仍如实报「没找到」", f"试了 {tries['n']} 次")

# 非「没有该评论」的错误不该重试——那是别的问题，重试没意义
tries["n"] = 0


def h_err(self, method, url, **kw):
    if "/x/v2/reply/reply" in url:
        tries["n"] += 1
        raise M.ApiError(-404, "啥都木有")
    return h_detail(self, method, url, **kw)


assert M.verify_comment(stub(h_err), t, 1).startswith("无法验证")
assert tries["n"] == 1, f"其他错误不该重试，实际试了 {tries['n']} 次"
ok("其他错误不重试", "直接报「无法验证」")


# ============================================================
# 6. 扫描编排
# ============================================================

NOW = int(time.time())


def card(dyn, mid, name, text, age_days=1):
    return {"id_str": str(dyn),
            "basic": {"comment_id_str": str(dyn), "comment_type": 17},
            "modules": {"module_author": {"mid": mid, "name": name,
                                          "pub_ts": NOW - int(age_days * DAY)},
                        "module_dynamic": {"desc": {"text": text}}}}


FUTURE = ymd(today + td(days=40))
PAST = ymd(today - td(days=5))
TOPIC = {"topic_card_list": {"items": [
    {"dynamic_card_item": card(1001, 11, "有效UP", "转发本条动态抽奖送手办")},
    {"dynamic_card_item": card(1002, 12, "已开奖UP", "转发抽奖已开奖，恭喜以下用户")},
    {"dynamic_card_item": card(1003, 13, "路人UP", "今天天气不错")},
    # 发布 60 天前、没写开奖时间 -> 按 max_age_days 挡掉
    {"dynamic_card_item": card(1004, 14, "陈年UP", "转发本条动态抽奖送键盘", 60)},
    # 发布 60 天前、但写着开奖时间还没到 -> 必须留下（这是修复前被误杀的情况）
    {"dynamic_card_item": card(1005, 15, "长周期UP",
                               f"转发本条动态抽奖送显卡，开奖时间：{FUTURE}", 60)},
    # 10 天前发布、开奖时间是 5 天前（在发布之后、现在之前）-> 按已开奖挡掉。
    # 注意开奖日必须晚于发布日，否则那日期根本不可能是这条抽奖的开奖时间，
    # 会被当成活动开始日之类忽略掉，进而退回按发布时间判断。
    {"dynamic_card_item": card(1006, 16, "过期UP",
                               f"转发本条动态抽奖送鼠标，开奖时间：{PAST}", 10)},
    # 自己发的转发，文案本身含「转发抽奖」，不排除就会转发自己的转发
    {"dynamic_card_item": card(1007, 999, "我自己", "转发抽奖，冲！")},
]}, "offset": ""}


def h_scan(self, method, url, **kw):
    if "web-interface/nav" in url:
        return NAV
    if "finger/spi" in url:
        return {"b_3": "x", "b_4": "y"}
    if "topic/web/details/cards" in url:
        return TOPIC
    if "feed/all" in url:
        return {"items": [], "offset": ""}
    if "feed/space" in url:         # scan 会先读自己的转发历史来去重
        return {"items": [], "offset": ""}
    raise AssertionError(url)


cfg = {**M.DEFAULTS, "cookie": COOKIE, "topic_ids": [4444],
       "min_delay": 0, "max_delay": 0}
b = stub(h_scan)
b.login_check()
rec = M.Record(os.path.join(D, "rec.json"))
todo, sm = M.scan(b, cfg, rec)

ids = sorted(t.dyn_id for t in todo)
assert ids == ["1001", "1005"], ids
assert sm.skip_old == 1, f"陈年那条该按发布时间挡掉，skip_old={sm.skip_old}"
assert sm.skip_drawn == 1, f"过期那条该按开奖时间挡掉，skip_drawn={sm.skip_drawn}"
assert sm.skip_self == 1, f"自己发的该跳过，skip_self={sm.skip_self}"
ok("扫描编排", f"7 条筛出 2 条（太老 {sm.skip_old} / 已开奖 {sm.skip_drawn} / "
               f"自己发的 {sm.skip_self}）")


# ============================================================
# 坑 9：已参与去重只认 record.json，有两个洞
#   a) record 可能被删（README 还教你删它重置）、记漏，或者你手动转发过
#   b) 转发「转发动态」时 B 站会把你的转发挂到**根动态**上，record 里记的 id
#      和实际转到的 id 对不上，下次扫到那条根动态就会再转一遍
# 实测 B 站上有 12 条转发、record 只记了 8 条，改完后跳过数从 4 涨到 12。
# ============================================================

MY_REPOSTS = {"items": [
    {"id_str": "9001", "orig": {"id_str": "7001"}},      # 我转发过 7001
    {"id_str": "9002", "orig": {"id_str": "7002"}},
    {"id_str": "9003"},                                  # 原创动态，没有 orig
], "offset": ""}


def h_dedup(self, method, url, **kw):
    if "web-interface/nav" in url:
        return NAV
    if "finger/spi" in url:
        return {"b_3": "x", "b_4": "y"}
    if "feed/space" in url and str((kw.get("params") or {}).get("host_mid")) == "999":
        return MY_REPOSTS
    if "topic/web/details/cards" in url:
        return DEDUP_TOPIC
    if "feed/all" in url:
        return {"items": [], "offset": ""}
    raise AssertionError(url)


def fwd(dyn, root, mid, text):
    """一条「转发动态」：它自己有 id，被转的根动态 id 放在 orig 里。"""
    c = card(dyn, mid, f"UP{mid}", text)
    c["orig"] = {"id_str": str(root)}
    return c


DEDUP_TOPIC = {"topic_card_list": {"items": [
    # 我转发过 7001，扫到它本身要跳过
    {"dynamic_card_item": card(7001, 21, "转过的", "转发本条动态抽奖送手办")},
    # 我转发过 7002。这条是「别人转发 7002」，我转它会被折叠到 7002，
    # 所以必须靠 root_id 认出来——这正是修复前会重复转发的情况
    {"dynamic_card_item": fwd(8002, 7002, 22, "转发本条动态抽奖送键盘")},
    # 没转过，应该留下
    {"dynamic_card_item": card(7003, 23, "没转过的", "转发本条动态抽奖送鼠标")},
]}, "offset": ""}

b2 = stub(h_dedup)
b2.login_check()
mine = M.fetch_my_reposts(b2, pages=1)
assert mine == {"7001", "7002"}, mine
ok("读自己的转发历史", f"拿到 {sorted(mine)}（没有 orig 的原创动态不算）")

cfg2 = {**M.DEFAULTS, "cookie": COOKIE, "topic_ids": [4444],
        "min_delay": 0, "max_delay": 0, "scan_following": False}
todo2, sm2 = M.scan(b2, cfg2, M.Record(os.path.join(D, "rec2.json")))
ids2 = sorted(t.dyn_id for t in todo2)
assert ids2 == ["7003"], f"应该只剩没转过的那条，实际 {ids2}"
assert sm2.skip_done == 2, sm2.skip_done
ok("按转发历史去重", "本身转过的 + 根动态转过的，都挡住了")

# 关掉开关就退回只信 record.json
cfg3 = {**cfg2, "check_my_reposts": False}
todo3, _ = M.scan(b2, cfg3, M.Record(os.path.join(D, "rec3.json")))
assert sorted(t.dyn_id for t in todo3) == ["7001", "7003", "8002"]
ok("check_my_reposts 开关有效", "关掉后不读转发历史")

# 参与后要连根动态一起记，否则下次扫到根动态还是会重复
r4 = M.Record(os.path.join(D, "rec4.json"))
r4.add("8002", {"uid": 22, "root_id": "7002"})
assert r4.done("8002") and r4.done("7002"), "根动态没被记进去"
assert r4.data["joined"]["7002"].get("alias_of") == "8002"
assert r4.today_count() == 1, "别名不该重复计入每日额度"
ok("参与后连根动态一起记", "（别名条目不占每日额度）")


# ============================================================
# 坑 11：联名抽奖写「关注 @甲 和 @乙」，只关注发动态的那个等于没参与。
# 真实翻车：三条 ASUS 联名抽奖分别要求关注西昊/川崎/特步，一个都没关注。
# ============================================================

AT_CARD = {"id_str": "5001",
           "basic": {"comment_id_str": "5001", "comment_type": 17},
           "modules": {"module_author": {"mid": 31, "name": "主办方",
                                         "pub_ts": NOW - DAY},
                       "module_dynamic": {"desc": {
                           "text": "关注@主办方 和@合作方 ，转发本动态抽奖送手办",
                           "rich_text_nodes": [
                               {"type": "RICH_TEXT_NODE_TYPE_AT", "rid": "31",
                                "text": "@主办方"},
                               {"type": "RICH_TEXT_NODE_TYPE_AT", "rid": "32",
                                "text": "@合作方"},
                               {"type": "RICH_TEXT_NODE_TYPE_TEXT",
                                "text": "转发本动态抽奖送手办"},
                           ]}}}}
at_target = M._to_target(AT_CARD, "T")
assert at_target.at_uids == {"31": "主办方", "32": "合作方"}, at_target.at_uids
ok("认出正文 @ 的号", f"{at_target.at_uids}（从富文本节点取 uid，比抠文字靠谱）")

followed, grouped = [], []


def h_follow(self, method, url, **kw):
    d = kw.get("data") or {}
    if "web-interface/nav" in url:
        return NAV
    if "relation/modify" in url:
        assert int(d["act"]) == 1, d
        followed.append(str(d["fid"]))
        return {}
    if "tags/addUsers" in url:
        grouped.append(str(d["fids"]))
        return {}
    if "create/dyn" in url:
        return {"dyn_id_str": "9"}
    if "reply/add" in url:
        return {"rpid": 1}
    if "/x/v2/reply/reply" in url:
        return {"root": {"rpid": 1, "member": {"mid": 999}}}
    raise AssertionError(url)


cfg_at = {**M.DEFAULTS, "cookie": COOKIE, "min_delay": 0, "max_delay": 0}
r = M.join_one(stub(h_follow), cfg_at, at_target, group_id=66, risk_hits=[0])
assert sorted(followed) == ["31", "32"], f"合作方没关注：{followed}"
assert sorted(grouped) == ["31", "32"], f"没都归进抽奖分组：{grouped}"
assert sorted(r.followed_uids) == ["31", "32"]
assert r.followed is True and r.reposted and r.commented
ok("联名抽奖会关注全部 @ 的号", f"关注了 {sorted(followed)}，且都进了分组")

# 关掉开关就只关注发动态的人
followed.clear(); grouped.clear()
M.join_one(stub(h_follow), {**cfg_at, "follow_at_mentions": False},
           M._to_target(AT_CARD, "T"), group_id=66, risk_hits=[0])
assert followed == ["31"], followed
ok("follow_at_mentions 开关有效", "关掉后只关注发动态的人")

# 合作方关注失败不该让整条参与算失败——发动态的人关上了就还算数
followed.clear()


def h_partial(self, method, url, **kw):
    d = kw.get("data") or {}
    if "relation/modify" in url and str(d.get("fid")) == "32":
        raise M.ApiError(22001, "不能关注该用户", url)
    return h_follow(self, method, url, **kw)


r2 = M.join_one(stub(h_partial), cfg_at, M._to_target(AT_CARD, "T"),
                group_id=66, risk_hits=[0])
assert r2.followed is True and r2.followed_uids == ["31"], (r2.followed, r2.followed_uids)
ok("合作方关注失败不拖垮整条", "（发动态的人关上了就算数）")


# ============================================================
# 坑 12：同一个抽奖有多个入口。同一轮里既扫到「ASUS 转发川崎的那条」，
# 又扫到「川崎的原动态」，两条都参与——B 站把两次转发都挂到川崎那个根动态上，
# 等于同一个抽奖转了两次。真发生过。
# ============================================================

def t_at(dyn, root="", ats=None):
    t = M.Target(dyn_id=str(dyn), root_id=str(root), uid=1, uname=f"UP{dyn}",
                 text="转发抽奖", reason="抽奖+转发", pub_ts=NOW - DAY)
    t.at_uids = dict(ats or {})
    return t


# 三条候选：A 是转发 R 的，R 是根，B 无关
kept, folded = M.collapse_by_root([
    t_at("A", root="R", ats={"88": "联名方"}),
    t_at("R"),
    t_at("B"),
])
ids = sorted(t.dyn_id for t in kept)
assert ids == ["B", "R"], f"同根的没折叠：{ids}"
assert folded == 1, folded
# 留下的必须是根动态本身，且要继承被折叠那条的 @ 名单，否则漏关联名方
root_kept = [t for t in kept if t.dyn_id == "R"][0]
assert root_kept.at_uids == {"88": "联名方"}, root_kept.at_uids
ok("同根入口只留一条", "留根动态，并继承被折叠那条的 @ 名单")

# 顺序反过来结果要一样
kept2, _ = M.collapse_by_root([t_at("R"), t_at("A", root="R")])
assert [t.dyn_id for t in kept2] == ["R"]
ok("折叠不受扫描顺序影响")

# 坑 14：原动态被作者删掉时，B 站的 orig.id_str 是字符串 "0"，不是缺字段。
# "0" 是真值，会被当成真实根 id——几个互不相干的抽奖全被归到根 "0" 底下，
# 折叠成一条，剩下的全漏掉。_to_target 必须把它归一成空串。
gone = {"id_str": "0", "modules": {}}
card_gone = {"id_str": "777", "type": "DYNAMIC_TYPE_FORWARD", "orig": gone,
             "modules": {"module_author": {"mid": 1, "name": "UP", "pub_ts": NOW - DAY},
                         "module_dynamic": {"desc": {"text": "转发抽奖 关注+转发"}}}}
t_gone = M._to_target(card_gone, "x")
assert t_gone is not None and t_gone.root_id == "", \
    f'原动态已删时 root_id 应归一成空串，实际 {t_gone.root_id!r}'

# 两条都转了「已被删除的原动态」，它们是不同的抽奖，不能折叠
kept3, folded3 = M.collapse_by_root([t_at("X", root=""), t_at("Y", root="")])
assert sorted(t.dyn_id for t in kept3) == ["X", "Y"], \
    f"转发已删动态的不同抽奖被错误折叠：{[t.dyn_id for t in kept3]}"
assert folded3 == 0, folded3
ok("原动态已删（root=\"0\"）不会把无关抽奖折叠成一条")

# 动手前的当场确认：参与完 A 之后，名单里的 R 必须被跳过
acted2 = []


def h_join(self, method, url, **kw):
    d = kw.get("data") or {}
    if "web-interface/nav" in url:
        return NAV
    if "relation/modify" in url:
        return {}
    if "tags/addUsers" in url:
        return {}
    if "create/dyn" in url:
        acted2.append(str((kw.get("json") or {})
                          .get("web_repost_src", {}).get("dyn_id_str")))
        return {"dyn_id_str": "9"}
    if "reply/add" in url:
        return {"rpid": 1}
    if "/x/v2/reply/reply" in url:
        return {"root": {"rpid": 1, "member": {"mid": 999}}}
    if "relation/tags" in url:
        return [{"tagid": 66, "name": "抽奖"}]
    raise AssertionError(url)


# 故意绕过 collapse，直接把同根的两条塞进 join_all，模拟折叠没拦住的情况
sm3 = M.Summary()
cfg_j = {**M.DEFAULTS, "cookie": COOKIE, "min_delay": 0, "max_delay": 0,
         "do_comment": False}
res3 = M.join_all(stub(h_join), cfg_j, M.Record(os.path.join(D, "rec5.json")),
                  [t_at("A", root="R"), t_at("R")], sm3)
assert acted2 == ["A"], f"同一个抽奖转了两次：{acted2}"
assert sm3.skip_dup == 1, sm3.skip_dup
ok("动手前当场再确认一次", "参与完 A 后，同根的 R 被跳过（不只靠扫描时的快照）")


# ============================================================
# 坑 13：关注数有上限（普通 1000 / 大会员 2000 / 硬核会员 5000）。
# 真实撞上：4994 个关注跑到 5000 满格，之后每个新号都关不上（22009）。
# 抽奖基本都要求关注，关不上就等于白转白评论——必须停下来喊人，
# 不能只 log 一行 warning 然后继续刷。
# ============================================================

def h_full(self, method, url, **kw):
    if "web-interface/nav" in url:
        return NAV
    if "relation/modify" in url:
        raise M.ApiError(M.FOLLOW_FULL, "关注失败，已达关注上限", url)
    if "relation/tags" in url:
        return [{"tagid": 66, "name": "抽奖"}]
    raise AssertionError(f"关注满了还继续发请求：{url}")


try:
    M.join_one(stub(h_full), {**M.DEFAULTS, "cookie": COOKIE,
                              "min_delay": 0, "max_delay": 0},
               t_at("X"), group_id=66, risk_hits=[0])
    raise AssertionError("关注满了应该抛 Stop，而不是继续转发评论")
except M.Stop as e:
    assert "上限" in str(e) and "follow.py" in str(e), e
ok("关注满了立刻停", "并提示用 follow.py 腾位置，不会继续白转")

# 其他关注失败（比如对方拉黑）不该停，只跳过这一个
def h_one_bad(self, method, url, **kw):
    d = kw.get("data") or {}
    if "relation/modify" in url and str(d.get("fid")) == "1":
        raise M.ApiError(22001, "不能关注自己", url)
    return h_join(self, method, url, **kw)


r5 = M.join_one(stub(h_one_bad), {**M.DEFAULTS, "cookie": COOKIE,
                                  "min_delay": 0, "max_delay": 0,
                                  "do_comment": False},
                t_at("Y"), group_id=66, risk_hits=[0])
assert r5.followed is False and r5.reposted is True
ok("普通关注失败不停机", "只是这条的关注没成，转发照做")

# 开奖时间优先于发布时间：1005 发布 60 天前，但开奖时间没到，必须在名单里
assert any(t.dyn_id == "1005" for t in todo), "开奖时间没到的长周期抽奖被误杀了"
ok("开奖时间优先于发布时间", "（60天前发布但未开奖的仍会参与）")


# ============================================================
# 7. 记录与控制台编码
# ============================================================

rp = os.path.join(D, "r.json")
r = M.Record(rp)
assert not r.done("123")
r.add("123", {"uid": 1})
r.data["group_id"] = 77
r.save()
r2 = M.Record(rp)
assert r2.done("123") and r2.today_count() == 1 and r2.data["group_id"] == 77
open(rp, "w", encoding="utf-8").write("{ 这不是 json")
assert M.Record(rp).today_count() == 0          # 坏文件要降级不是崩
ok("参与记录", "去重 / 每日计数 / 落盘 / 坏文件降级")

# 坑 15：转发历史只翻 my_repost_pages 页（默认 5 页≈40 条），而转发一直累积。
# 8 月 30 日转过的根动态到 10 月已经排在第 215 条，翻 5 页看不到；那条 record
# 又是老 schema 没存 root_id，于是两道去重同时失效，同一个根被转了第二次。
# 根动态要永久记住，不能依赖翻页深度。
rr = os.path.join(D, "roots.json")
r6 = M.Record(rr)
assert r6.repost_roots == set()
assert r6.remember_roots({"R1", "R2"}) == 2
assert r6.remember_roots({"R2", "R3"}) == 1            # 只增不重
assert r6.remember_roots({"", "0", None}) == 0         # 空值和 "0" 不收
r6.add("D9", {"uid": 1, "reposted": True, "root_id": "R9"})
assert "R9" in r6.repost_roots, "参与时转发的根动态没被记住"
r6.add("D8", {"uid": 1, "reposted": True})             # 没根就记自己
assert "D8" in r6.repost_roots
r6.add("D7", {"uid": 1, "reposted": False})            # 没转发就不记
assert "D7" not in r6.repost_roots
r6.save()
assert M.Record(rr).repost_roots == {"R1", "R2", "R3", "R9", "D8"}, "落盘后记忆丢了"
ok("转过的根动态永久记住", "不靠翻页深度，隔几个月也不会重复转")

# 坑 7：抽奖正文里常有 emoji，GBK 控制台下 print 会直接抛 UnicodeEncodeError。
# 只改 errors 不改 encoding——强行 UTF-8 会让 GBK 控制台下连中文都变乱码。
g = io.TextIOWrapper(io.BytesIO(), encoding="gbk")
try:
    g.write("抽奖🎁")
    g.flush()
    raise AssertionError("gbk 严格模式竟然没报错，测试前提不成立")
except UnicodeEncodeError:
    pass
g2 = io.TextIOWrapper(io.BytesIO(), encoding="gbk")
g2.reconfigure(errors="replace")
g2.write("抽奖🎁转发")
g2.flush()
assert g2.encoding == "gbk" and g2.errors == "replace"
_o, _e = sys.stdout, sys.stderr
try:
    sys.stdout = sys.stderr = io.StringIO()     # 没有 reconfigure 的对象不能崩
    M.fix_console_encoding()
finally:
    sys.stdout, sys.stderr = _o, _e
ok("控制台编码兜底", "emoji 不再崩，且保留控制台原生编码")


print(f"\n全部 {len(passed)} 项通过　（{time.strftime('%Y-%m-%d %H:%M')}）")
