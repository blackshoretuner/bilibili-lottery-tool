#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""B 站抽奖一键参与 —— 自动找到转发抽奖动态，帮你完成「关注 + 转发 + 评论」。

用法：
    python main.py --test    先试跑，只看会参与哪些，不动手
    python main.py           正式跑（会问你一句要不要继续）
    python main.py --yes     正式跑，不问，适合挂定时任务

整个脚本从上往下分成 9 块，想改哪块直接搜标题。

判定方式说明：B 站原来那个能准确识别官方互动抽奖的接口已经下线，现在只能靠
正文关键词判断（见第 6 块），所以会有误判。请务必先用 --test 看一眼。
"""

from __future__ import annotations

import argparse
import json
import logging
import random
import re
import sys
import time
import urllib.parse
from dataclasses import dataclass, field
from datetime import date, timedelta
from hashlib import md5
from pathlib import Path
from typing import Any, Iterator

import requests

ROOT = Path(__file__).resolve().parent
log = logging.getLogger("bili")


# ============================================================
# 1. 配置：config.json 里没填的项，就用这里的默认值
# ============================================================

DEFAULTS: dict[str, Any] = {
    "cookie": "",                              # 必填，你的 B 站登录凭证
    "topic_ids": [],                           # 去这些话题下面找抽奖（填数字 id）
    "learn_topics": True,                      # 自动从扫到的动态里学习抽奖话题 id
    "topic_keyword": "抽奖",                    # 话题名里含这个词就记下来
    "up_mids": [],                             # 额外盯这几个 UP 主（填 uid 数字）
    "dynamic_ids": [],                         # 手动指定的动态，可填链接或 id
    "scan_following": True,                    # 是否顺便扫自己的关注动态流
    # 各来源翻几页。话题页翻一页就到底了（接口封顶 20 来条），所以没有这一项；
    # 关注流和 UP 空间能一直翻，翻得越多找到的抽奖越多，代价是每页一次请求。
    "feed_pages": 10,                          # 关注流翻几页（每页约 25 条）
    "up_pages": 3,                             # 每个 UP 空间翻几页
    # 开跑前先读一遍自己的转发历史，避免重复参与。record.json 可能被删或记漏，
    # 而且「转发别人的转发」会被 B 站折叠到根动态上，光靠 record 对不上。
    "check_my_reposts": True,
    "my_repost_pages": 5,                      # 自己的动态翻几页（每页约 12 条）
    "do_follow": True,                         # 参与时是否关注 UP
    # 联名抽奖常写「关注 @甲 和 @乙」，只关注发动态的那个等于没参与。
    # 打开后会把正文 @ 到的号一起关注（都会进「抽奖」分组，方便以后批量取关）。
    "follow_at_mentions": True,
    "do_repost": True,                         # 参与时是否转发
    "do_comment": True,                        # 参与时是否评论
    "follow_group": "抽奖",                     # 关注的 UP 丢进这个分组，方便以后批量取关
    "repost_texts": ["转发抽奖，冲！", "许愿中奖~", "来了来了，接好运", "参与一下，祝我好运"],
    "comment_texts": ["参与一下，谢谢UP！", "许愿中奖~", "蹲一个好运", "支持一下"],
    # 判定关键词：正文里要同时命中「抽奖词」和「动作词」，且不含「排除词」。
    # 官方判定接口已下线，只能靠正文猜，所以这三组词直接决定准不准。
    # 默认这套偏保守——宁可漏掉，也别把不是抽奖的转到你主页上。
    "lottery_words": ["抽奖", "抽獎", "包邮送", "免费送", "送出", "抽一位", "抽三位"],
    # 只留「转发」：转发抽奖就是靠转发参与的。写上关注/评论会把评论区抽奖也算进来，
    # 而评论区抽奖你转发一百遍也没用。
    "action_words": ["转发", "轉發"],
    "skip_words": [
        # 已经开完奖的，转了没用
        "已开奖", "开奖结果", "中奖名单", "名单公布", "已结束",
        "恭喜以下", "中奖用户", "获奖名单",
        # 评论区抽奖，参与方式不是转发
        "评论区抽", "评论抽", "评论区留言",
        # 抽奖聚合号的每日汇总帖和说明帖，本身不是抽奖
        "抽奖合集", "传送门", "每日更新", "特别关注", "更新通知",
        "工具人", "建议是", "教程", "怎么参与",
    ],
    # 兜底用的：正文里**没写**开奖时间时，才按「发布多久了」判断还开不开着。
    # 正文写了开奖时间的会优先按那个算，不受这个值影响（0 = 不按发布时间过滤）。
    "max_age_days": 21,
    "max_per_run": 20,                         # 跑一次最多参与几个
    "max_per_day": 40,                         # 一天最多参与几个
    "min_delay": 20,                           # 两个动作之间最少等几秒
    "max_delay": 45,                           # 最多等几秒
}

# 下面这些基本不用改，所以没放进配置文件
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")
TIMEOUT = 15            # 单个请求超时（秒）
PAGES = 2               # 每个来源翻几页
PAGE_SIZE = 20          # 每页取几条
RISK_COOLDOWN = 600     # 撞风控后歇多久（秒）
MAX_RISK_HITS = 2       # 连续撞几次风控就收工


def load_config(path: str | Path) -> dict:
    path = Path(path)
    if not path.exists():
        raise SystemExit(
            f"× 找不到配置文件 {path}\n"
            f"  请把 config.example.json 复制一份、改名成 config.json，再填进 Cookie。"
        )
    try:
        user = json.loads(path.read_text(encoding="utf-8"))
    except ValueError as exc:
        raise SystemExit(
            f"× {path} 不是合法的 JSON：{exc}\n"
            f"  常见原因：少了逗号、多了逗号、或者引号打成了中文引号。"
        )

    cfg = {**DEFAULTS, **user}
    # 用真正会被拿去请求的那份解析结果来校验，而不是看原始字符串里有没有这几个词，
    # 否则「字符串里有、但解析后丢了」这种问题要等到发请求才暴露
    parsed = parse_cookie(cfg["cookie"])
    missing = [k for k in ("SESSDATA", "bili_jct") if not parsed.get(k)]
    if missing:
        raise SystemExit(
            f"× config.json 里的 cookie 少了这些字段：{'、'.join(missing)}\n"
            "  它必须是从浏览器复制的一整条 Cookie，里面要能看到 SESSDATA 和 bili_jct。\n"
            "  具体怎么拿，看 README 的「第 3 步」。"
        )
    return cfg


def parse_cookie(raw: str) -> dict[str, str]:
    """把一整条 Cookie 切成字典。

    这里特意不用标准库的 http.cookies.SimpleCookie：B 站 Cookie 里有
    bmg_af_sc={"none":{...}} 这种带大括号和引号的值，SimpleCookie 认为它非法，
    然后会把它后面的所有字段一起静默丢掉——包括 SESSDATA。而这个字段出现的
    位置并不固定，所以只能自己按分号切。
    """
    out: dict[str, str] = {}
    for part in raw.strip().strip(";").split(";"):
        key, sep, value = part.partition("=")
        key = key.strip()
        if sep and key:
            out[key] = value.strip()
    return out


# ============================================================
# 2. WBI 签名：B 站部分接口要求带 w_rid / wts，不带就报 -403
# ============================================================

# 官方前端 JS 里硬编码的乱序表
_MIXIN_TAB = [46, 47, 18, 2, 53, 8, 23, 32, 15, 50, 10, 31, 58, 3, 45, 35,
              27, 43, 5, 49, 33, 9, 42, 19, 29, 28, 14, 39, 12, 38, 41, 13,
              37, 48, 7, 16, 24, 55, 40, 61, 26, 17, 0, 1, 60, 51, 30, 4,
              22, 25, 54, 21, 56, 59, 6, 63, 57, 62, 11, 36, 20, 34, 44, 52]

# 官方前端签名前会先把 value 里的这几个字符删掉
_DROP_CHARS = "!'()*"


def make_mixin_key(img_url: str, sub_url: str) -> str:
    """把 nav 接口给的两张图片名拼起来打乱，取前 32 位当签名密钥。"""
    raw = "".join(u.rsplit("/", 1)[-1].split(".")[0] for u in (img_url, sub_url))
    return "".join(raw[i] for i in _MIXIN_TAB if i < len(raw))[:32]


def wbi_sign(params: dict, mixin_key: str) -> dict:
    signed = {**params, "wts": int(time.time())}
    clean = {k: "".join(c for c in str(signed[k]) if c not in _DROP_CHARS) for k in sorted(signed)}
    query = urllib.parse.urlencode(clean)
    signed["w_rid"] = md5((query + mixin_key).encode()).hexdigest()
    return signed


# ============================================================
# 3. 请求客户端：带上 Cookie 发请求，顺便管好限速和报错
# ============================================================

RISK_CODES = {-352, -509, -799, -412}          # 风控 / 限流，该歇了
AUTH_CODES = {-101, -111, 4100000}             # Cookie 失效
DONE_CODES = {22014, 22015, 65006, 4100013}    # 「已关注」「已转发」，算成功
FOLLOW_FULL = 22009    # 关注数到顶了。B 站上限：普通 1000 / 大会员 2000 / 硬核会员 5000


class ApiError(RuntimeError):
    def __init__(self, code: int, message: str, url: str = ""):
        super().__init__(f"[{code}] {message}")
        self.code, self.message, self.url = code, message, url

    @property
    def is_risk(self) -> bool:
        return self.code in RISK_CODES

    @property
    def is_auth(self) -> bool:
        return self.code in AUTH_CODES


class Bili:
    def __init__(self, cookie: str, min_delay: float = 0, max_delay: float = 0):
        cookies = parse_cookie(cookie)
        self.csrf = cookies.get("bili_jct", "")
        self.mid = cookies.get("DedeUserID", "")
        self.uname = ""
        self.min_delay, self.max_delay = min_delay, max(min_delay, max_delay)
        self._last_action = 0.0
        self._mixin_key = ""

        self.http = requests.Session()
        self.http.headers.update({
            "User-Agent": UA,
            "Referer": "https://www.bilibili.com/",
            "Origin": "https://www.bilibili.com",
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "zh-CN,zh;q=0.9",
        })
        self.http.cookies.update(cookies)

    # ---- 发请求 ----

    def _req(self, method: str, url: str, **kw) -> Any:
        kw.setdefault("timeout", TIMEOUT)
        try:
            resp = self.http.request(method, url, **kw)
        except requests.RequestException as exc:
            raise ApiError(-1, f"网络错误: {exc}", url) from exc

        if resp.status_code == 412:
            raise ApiError(-412, "被 B 站拦截（412），歇一会儿再跑", url)
        if resp.status_code >= 400:
            # 接口下线或改路径会返回 404。统一转成 ApiError，好让上层
            # 「跳过这个来源、继续往下跑」，而不是整个脚本崩掉。
            raise ApiError(-resp.status_code, f"HTTP {resp.status_code}，接口可能已下线", url)
        try:
            body = resp.json()
        except ValueError:
            raise ApiError(-2, f"返回的不是 JSON: {resp.text[:120]}", url) from None

        if body.get("code", 0) != 0:
            raise ApiError(body["code"], body.get("message") or body.get("msg") or "", url)
        return body.get("data")

    def get(self, url: str, params: dict | None = None, wbi: bool = False) -> Any:
        params = dict(params or {})
        if wbi:
            params = wbi_sign(params, self._get_mixin_key())
        return self._req("GET", url, params=params)

    def post(self, url: str, data: dict) -> Any:
        """表单式 POST，B 站老接口都用这种。"""
        return self._req("POST", url, data={**data, "csrf": self.csrf, "csrf_token": self.csrf})

    def post_json(self, url: str, payload: dict) -> Any:
        """JSON 式 POST，新版发动态接口用这种。"""
        return self._req("POST", url, params={"platform": "web", "csrf": self.csrf}, json=payload)

    # ---- 准备工作 ----

    def _get_mixin_key(self) -> str:
        if not self._mixin_key:
            img = self._req("GET", "https://api.bilibili.com/x/web-interface/nav")["wbi_img"]
            self._mixin_key = make_mixin_key(img["img_url"], img["sub_url"])
        return self._mixin_key

    def login_check(self) -> str:
        """验证 Cookie 有没有失效，同时把昵称记下来。"""
        data = self._req("GET", "https://api.bilibili.com/x/web-interface/nav")
        if not data.get("isLogin"):
            raise ApiError(-101, "Cookie 已失效，请重新复制一份")
        self.uname = data.get("uname", "")
        self.mid = str(data.get("mid") or self.mid)
        return self.uname

    def ensure_buvid(self) -> None:
        """补一个 buvid3，缺它有些接口会直接判风控。"""
        if self.http.cookies.get("buvid3"):
            return
        try:
            data = self._req("GET", "https://api.bilibili.com/x/frontend/finger/spi")
            for src, name in (("b_3", "buvid3"), ("b_4", "buvid4")):
                if data.get(src):
                    self.http.cookies.set(name, data[src], domain=".bilibili.com")
        except ApiError as exc:
            log.debug("拿 buvid 失败，忽略: %s", exc)

    # ---- 限速 ----

    def pause(self) -> None:
        """两个动作之间随机等一会儿，别把 B 站惹了。"""
        if self._last_action:
            wait = random.uniform(self.min_delay, self.max_delay) - (time.time() - self._last_action)
            if wait > 0:
                log.debug("等待 %.1fs", wait)
                time.sleep(wait)
        self._last_action = time.time()

    def cooldown(self) -> None:
        log.warning("撞到风控了，歇 %d 秒", RISK_COOLDOWN)
        time.sleep(RISK_COOLDOWN)
        self._last_action = time.time()


# ============================================================
# 4. 参与记录：记住参与过哪些，避免重复；顺便管每日限额
# ============================================================

class Record:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.data: dict[str, Any] = {"joined": {}, "daily": {}, "group_id": None,
                                     "topics": {}}
        if self.path.exists():
            try:
                self.data.update(json.loads(self.path.read_text(encoding="utf-8")))
            except (ValueError, OSError):
                log.warning("%s 读不出来，当成空的重新开始", self.path)

    @property
    def _today(self) -> str:
        return date.today().isoformat()

    @property
    def topics(self) -> dict[str, str]:
        """历次学到的抽奖话题 {topic_id: 话题名}。"""
        return self.data.setdefault("topics", {})

    def done(self, dyn_id: str) -> bool:
        return str(dyn_id) in self.data["joined"]

    def add(self, dyn_id: str, detail: dict) -> None:
        entry = {"ts": int(time.time()), **detail}
        self.data["joined"][str(dyn_id)] = entry
        # 转发「转发动态」时 B 站会把我们的转发挂到根动态上。把根动态 id 也记一条，
        # 否则下次扫到那条根动态时对不上，会重复转发同一个抽奖。
        root = str(detail.get("root_id") or "")
        if root and root != str(dyn_id):
            self.data["joined"][root] = {**entry, "alias_of": str(dyn_id)}
        self.data["daily"][self._today] = self.today_count() + 1

    def today_count(self) -> int:
        return int(self.data["daily"].get(self._today, 0))

    def save(self) -> None:
        # 每日计数只留最近 30 天，别让文件无限长
        self.data["daily"] = {k: self.data["daily"][k] for k in sorted(self.data["daily"])[-30:]}
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.data, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(self.path)


# ============================================================
# 5. 找抽奖动态：从话题页 / UP 空间 / 关注流 / 手填 id 四个入口捞
# ============================================================

WEB_DYN = "https://api.bilibili.com/x/polymer/web-dynamic/v1"
FEATURES = "itemOpusStyle,listOnlyfans,opusBigCover,onlyfansVote"


@dataclass
class Target:
    """一条待参与的抽奖动态。前半段来自动态卡片，后半段来自抽奖接口。"""

    dyn_id: str
    uid: int = 0
    uname: str = ""
    text: str = ""
    source: str = ""
    comment_id: str = ""
    comment_type: int = 0
    topic_id: int = 0       # 动态带的话题标记，用来学习新的抽奖话题
    topic_name: str = ""
    pub_ts: int = 0         # 发布时间戳
    # 这条动态本身如果是转发，根动态的 id。转发「转发动态」时 B 站会把你的
    # 转发挂到根动态上，所以去重必须连根一起比，否则会重复转发同一个抽奖。
    root_id: str = ""
    # 正文里 @ 到的号（uid -> 名字）。联名抽奖常要求「关注 @甲 和 @乙」，
    # 只关注发动态的那个等于没参与。
    at_uids: dict[str, str] = field(default_factory=dict)
    draw_ts: int = 0        # 从正文里解析出的开奖时间，0 = 正文里没写
    reason: str = ""        # 命中了哪些关键词，试跑时给你看，好判断准不准

    @property
    def url(self) -> str:
        return f"https://t.bilibili.com/{self.dyn_id}"

    @property
    def age_days(self) -> float:
        """发布至今多少天。拿不到发布时间就返回 0（当成新的，不误杀）。"""
        return (time.time() - self.pub_ts) / 86400 if self.pub_ts else 0.0

    @property
    def pub_at(self) -> str:
        if not self.pub_ts:
            return "时间未知"
        return time.strftime("%m-%d", time.localtime(self.pub_ts))

    @property
    def draw_at(self) -> str:
        if not self.draw_ts:
            return "开奖时间不明"
        return time.strftime("%m-%d开奖", time.localtime(self.draw_ts))

    @property
    def brief(self) -> str:
        """正文摘要，报告里用来让你一眼看出这条是什么。"""
        text = " ".join(self.text.split())
        return text[:40] + ("…" if len(text) > 40 else "")


def _walk_cards(node: Any) -> Iterator[dict]:
    """递归找出返回数据里所有长得像动态卡片的部分。

    B 站改接口很勤，写死 data.xxx.items[0] 这种路径迟早失效，
    所以这里只认「有 id_str 和 modules」这个特征，外层怎么包都能捞到。
    """
    if isinstance(node, dict):
        if "id_str" in node and isinstance(node.get("modules"), (dict, list)):
            yield node
        for value in node.values():
            yield from _walk_cards(value)
    elif isinstance(node, list):
        for value in node:
            yield from _walk_cards(value)


def _to_target(card: dict, source: str) -> Target | None:
    dyn_id = str(card.get("id_str") or "")
    if not dyn_id.isdigit():
        return None

    # modules 有时是字典，有时是列表，统一成字典
    mods = card.get("modules")
    if not isinstance(mods, dict):
        mods = {k: v for m in (mods or []) if isinstance(m, dict) for k, v in m.items()}

    author = mods.get("module_author") or {}
    dynamic = mods.get("module_dynamic") or {}
    major = dynamic.get("major") or {}
    orig = card.get("orig") or {}
    root_id = str(orig.get("id_str") or "")
    # 原动态被作者删掉时，B 站返回的是字符串 "0" 而不是省略字段。而 "0" 是真值，
    # 会被当成一个真实的根动态 id——于是所有「转发了已删动态」的抽奖都被归到
    # 同一个根 "0" 下面：collapse_by_root 把它们折叠成一条，去重集合也会在参与
    # 完第一条后把 "0" 记下，后面每一条都当成「已经参与过」跳过。实测自己 89 条
    # 转发里有 7 条是这种，占比不低。归一成空串，下游全靠真值判断，改这一处就够。
    if root_id == "0":
        root_id = ""

    # 正文里 @ 到的号：富文本节点里带 uid，比从文字里抠名字靠谱
    at_uids: dict[str, str] = {}

    def _walk_at(node: Any) -> None:
        if isinstance(node, dict):
            if node.get("type") == "RICH_TEXT_NODE_TYPE_AT" and node.get("rid"):
                at_uids[str(node["rid"])] = str(node.get("text") or "").lstrip("@").strip()
            for value in node.values():
                _walk_at(value)
        elif isinstance(node, list):
            for value in node:
                _walk_at(value)

    _walk_at(dynamic)
    basic = card.get("basic") or {}

    # 正文位置有好几种：纯文字动态、专栏式动态、视频动态
    opus = major.get("opus") or {}
    text = ((dynamic.get("desc") or {}).get("text")
            or (opus.get("summary") or {}).get("text")
            or opus.get("title") or "")
    if major.get("archive"):
        text = f"{text} {major['archive'].get('title', '')}".strip()

    topic = dynamic.get("topic") or _find_topic(card) or {}

    return Target(
        dyn_id=dyn_id,
        uid=int(author.get("mid") or 0),
        uname=author.get("name") or "",
        text=text,
        source=source,
        comment_id=str(basic.get("comment_id_str") or ""),
        comment_type=int(basic.get("comment_type") or 0),
        pub_ts=int(author.get("pub_ts") or 0),
        root_id=root_id,
        at_uids=at_uids,
        topic_id=int(topic.get("id") or 0),
        topic_name=topic.get("name") or "",
    )


def _find_topic(node: Any) -> dict | None:
    """在卡片里翻出话题标记（#xxx# 那个）。位置偶尔会变，所以递归找。"""
    if isinstance(node, dict):
        if isinstance(node.get("topic"), dict) and node["topic"].get("id"):
            return node["topic"]
        for value in node.values():
            found = _find_topic(value)
            if found:
                return found
    elif isinstance(node, list):
        for value in node:
            found = _find_topic(value)
            if found:
                return found
    return None


def parse_dyn_id(text: str) -> str | None:
    """从「链接」或「一串数字」里取出动态 id。"""
    text = str(text).strip()
    if text.isdigit():
        return text
    m = re.search(r"(?:t\.bilibili\.com/|dynamic/|opus/)(\d{10,})", text)
    return m.group(1) if m else None


def fetch_my_reposts(bili: Bili, pages: int = 5) -> set[str]:
    """翻自己的动态，把「我转发过哪些原动态」读出来。

    为什么不能只信 record.json：

    · 它可能被删（README 里还教你删它来重置），也可能记漏
    · **你手动转发过的抽奖它根本不知道**
    · 更麻烦的是「转发别人的转发」：B 站会把你的转发挂到**根动态**上。
      实测转 ASUS 那条（它本身是转发西昊的），我的转发记录里原动态变成了
      西昊那条。record 里记的 id 和实际转到的 id 对不上，下次扫到西昊的
      原动态就会再转一遍。

    所以以 B 站上的实际转发历史为准，record.json 只当补充。
    """
    reposted: set[str] = set()
    offset = ""
    for page in range(1, max(1, pages) + 1):
        try:
            data = bili.get(f"{WEB_DYN}/feed/space",
                            {"host_mid": bili.mid, "offset": offset,
                             "timezone_offset": -480, "platform": "web",
                             "features": FEATURES}, wbi=True)
        except ApiError as exc:
            log.warning("读自己的转发历史失败（第 %d 页），去重只能靠 record.json: %s",
                        page, exc)
            break
        items = (data or {}).get("items") or []
        for item in items:
            orig = item.get("orig")
            if isinstance(orig, dict) and orig.get("id_str"):
                reposted.add(str(orig["id_str"]))
        offset = str((data or {}).get("offset") or "")
        if not items or not offset:
            break
    log.info("从自己的动态里读到 %d 条转发记录", len(reposted))
    return reposted


def learn_topics(rec: Record, targets: list[Target], keyword: str) -> int:
    """从扫到的动态里挑出「名字带抽奖字样」的话题，记进 record.json。

    B 站按名字搜话题的接口已经下线了，没法用「互动抽奖」这种词直接查到 topic_id。
    但动态卡片自己会带话题标记，所以改成边扫边学：今天从关注流里认识了
    #互动抽奖#，明天就能把整个话题都扫一遍，越跑覆盖面越大。
    """
    learned = 0
    for target in targets:
        if not target.topic_id or keyword not in target.topic_name:
            continue
        if str(target.topic_id) not in rec.topics:
            rec.topics[str(target.topic_id)] = target.topic_name
            log.info("学到新的抽奖话题：#%s#  (topic_id=%s)", target.topic_name, target.topic_id)
            learned += 1
    return learned


# 读接口之间的间隔。写操作用 config 里的 min_delay/max_delay（20~45 秒），
# 那是给关注、转发、评论准备的；读请求没必要那么慢，但也不能一个不等。
# 真实翻车：up_mids 从 5 个扩到 26 个之后，26×3 页的读请求连发出去，B 站
# 当场风控，26 个 UP 的空间动态一条都没拉到——全程只花了 17 秒。更糟的是
# 它是静默的：候选从 1562 掉到 836，报告只说「扫了 836 条」，不说少的那
# 726 条是被风控吃了，看上去就像今天抽奖比较少。
READ_MIN_DELAY = 0.4
READ_MAX_DELAY = 1.0

# 本轮被风控打掉的来源。扫完要报出来——少扫了一半候选却不说，
# 会被当成「今天抽奖少」，而不是「扫描没跑完」。
_RISK_HIT: list[str] = []


def _collect(bili: Bili, url: str, params: dict, label: str, use_wbi: bool,
             pages: int = PAGES) -> list[Target]:
    """翻 pages 页，把每页里的动态卡片都收进来。

    各来源的产量上限差很多，所以翻几页要分开配：
      · 话题页封顶 20~22 条，page_size 调大无效、offset 直接为空，翻一页就到底
      · 关注流和 UP 空间能一直往下翻，翻多少页取决于你愿意等多久
    """
    found: list[Target] = []
    offset = ""
    for page in range(1, max(1, pages) + 1):
        time.sleep(random.uniform(READ_MIN_DELAY, READ_MAX_DELAY))
        try:
            data = bili.get(url, {**params, "offset": offset, "page": page}, wbi=use_wbi)
        except ApiError as exc:
            log.warning("%s 第 %d 页拉取失败: %s", label, page, exc)
            if exc.is_risk:
                _RISK_HIT.append(label)
            break
        cards = list(_walk_cards(data))
        found += [t for t in (_to_target(c, label) for c in cards) if t]
        offset = str((data or {}).get("offset") or "")
        if not cards or not offset:
            break
    log.info("%s: %d 条动态", label, len(found))
    return found


def _fetch_one(bili: Bili, dyn_id: str) -> Target | None:
    try:
        data = bili.get(f"{WEB_DYN}/detail",
                        {"id": dyn_id, "features": FEATURES,
                         "timezone_offset": -480, "platform": "web"}, wbi=True)
    except ApiError as exc:
        log.warning("动态 %s 详情拉取失败: %s", dyn_id, exc)
        return None
    for card in _walk_cards(data):
        target = _to_target(card, "手填")
        if target and target.dyn_id == str(dyn_id):
            return target
    return None


def find_candidates(bili: Bili, cfg: dict, rec: Record) -> list[Target]:
    """把各来源的动态汇总起来，按动态 id 去重。

    顺序有讲究：先扫不需要 topic_id 的来源（关注流、UP 空间、手填），
    从里面学到抽奖话题 id，再去扫话题——这样第一次跑就能滚起来。
    """
    found: dict[str, Target] = {}

    def take(items: list[Target]) -> None:
        for item in items:
            found.setdefault(item.dyn_id, item)

    for mid in cfg["up_mids"]:
        take(_collect(bili, f"{WEB_DYN}/feed/space",
                      {"host_mid": mid, "timezone_offset": -480,
                       "platform": "web", "features": FEATURES},
                      f"UP {mid}", True, cfg["up_pages"]))

    if cfg["scan_following"]:
        take(_collect(bili, f"{WEB_DYN}/feed/all",
                      {"type": "all", "timezone_offset": -480,
                       "platform": "web", "features": FEATURES},
                      "关注流", True, cfg["feed_pages"]))

    for raw in cfg["dynamic_ids"]:
        dyn_id = parse_dyn_id(raw)
        if not dyn_id:
            log.warning("看不懂的动态 id: %s", raw)
            continue
        # 手填的优先，覆盖掉别处扫来的同一条
        found[dyn_id] = _fetch_one(bili, dyn_id) or Target(dyn_id=dyn_id, source="手填")

    if cfg["learn_topics"]:
        learn_topics(rec, list(found.values()), cfg["topic_keyword"])

    # 配置里写死的 + 历次学到的，一起扫
    topic_ids = list(dict.fromkeys([*cfg["topic_ids"], *(int(t) for t in rec.topics)]))
    if not topic_ids:
        log.info("还没有任何抽奖话题 id。扫到带 #%s# 标记的动态后会自动记住，"
                 "也可以自己填进 config.json 的 topic_ids（怎么找见 README）",
                 cfg["topic_keyword"])
    for tid in topic_ids:
        label = rec.topics.get(str(tid)) or str(tid)
        # 话题页有「热门」和「最新」两个 tab，对应 sort_by 2 和 3，返回的动态
        # 几乎不重叠：热门那边是大额长周期抽奖，最新那边是刚发的。都要扫。
        for sort_by, tab in ((2, "热门"), (3, "最新")):
            take(_collect(bili, "https://api.bilibili.com/x/topic/web/details/cards",
                          {"topic_id": tid, "sort_by": sort_by, "page_size": PAGE_SIZE},
                          f"话题「{label}」{tab}", False, pages=1))

    # 话题里的动态又可能带上别的抽奖话题，再学一轮给下次用
    if cfg["learn_topics"]:
        learn_topics(rec, list(found.values()), cfg["topic_keyword"])

    return list(found.values())


# ============================================================
# 6. 判断：这条动态像不像抽奖
# ============================================================
#
# B 站原来有个接口（lottery_svr/lottery_notice）能准确回答「这条是不是互动抽奖、
# 什么时候开奖、奖品是什么」。这个接口已经下线了——对真实动态一律返回 -9999，
# 有时甚至直接连接超时。所以现在只能退一步靠正文关键词判断，代价是：
#   · 会有误判，可能转发到不是抽奖的动态
#   · 拿不到开奖时间和奖品，也没法自动跳过已经开奖的
# 因此每次改完关键词都建议先 --test 看一眼命中了什么。


_YMD = re.compile(r"(20\d{2})\s*[年./\-]\s*(\d{1,2})\s*[月./\-]\s*(\d{1,2})")
_MD = re.compile(r"(?<!\d)(\d{1,2})\s*月\s*(\d{1,2})\s*[日号]")


MAX_DRAW_AHEAD_DAYS = 730       # 开奖日期离发布超过两年，多半是解析错了
# 只写月日、且日期早于发布时，最多往后滚这么久。跨年合理（12月发、1月5日开奖），
# 跨大半年就不合理了——那多半是在提往期的事，不是这次的开奖时间。
MAX_ROLL_FORWARD_DAYS = 150


def parse_draw_date(text: str, pub_ts: int = 0) -> int:
    """从正文里把开奖日期抠出来，返回**开奖当天的最后一刻**；找不到返回 0。

    官方接口没了之后，正文里手写的「开奖时间：X月X日」就是唯一线索了。
    取找到的最晚那个日期——抽奖正文里常同时出现活动开始日和开奖日，
    开奖总是更晚的那个。

    两个容易出错的地方，都踩过：

    1. **返回当天最后一刻，不是零点。** 一条「今天 20:00 开奖」的抽奖，
       如果按当天 00:00 算，下午跑脚本时就成了「已开奖」被跳过——可它明明
       还能参与。只有整天过完了才算结束。

    2. **只写月日时，按动态发布时间补年份，不是按今天。** 一条 2 月发布、
       写着「3月1日开奖」的老抽奖，若按今天补年份，会因为距今超过半年而被
       补成明年的 3 月 1 日，凭空变成「还开着」，去转一个早就结束的抽奖。
       以发布时间为基准就不会错：开奖不可能早于发布。
    """
    known_pub = bool(pub_ts)
    base = date.fromtimestamp(pub_ts) if known_pub else date.today()
    found = []

    # 写全了年月日的，原样收下——哪怕是过去的日期也要留着，那正是
    # 「这个抽奖已经开完了」的证据
    for y, m, d in _YMD.findall(text):
        try:
            found.append(date(int(y), int(m), int(d)))
        except ValueError:
            pass            # 2026年13月45日 这种就算了

    # 先把带年份的日期从文本里抹掉，再找只写了月日的。不然「2026年1月1日」里的
    # 「1月1日」会被当成没写年份，然后补成明年，直接把最晚日期算歪。
    rest = _YMD.sub(" ", text)
    for m, d in _MD.findall(rest):
        for year in (base.year, base.year + 1):
            try:
                cand = date(year, int(m), int(d))
            except ValueError:
                continue
            # 开奖不可能早于发布，但也不能为了满足这条就无脑滚到明年：
            # 一条 8-17 发的中奖公布帖写着「8月16号抽1位」，滚成明年 8-16
            # 就成了「还开着」，于是去转一个两周前就开完的抽奖（真发生过）。
            # 跨年是合理的（12月发、1月5日开奖），跨大半年就不合理了。
            if base <= cand <= base + timedelta(days=MAX_ROLL_FORWARD_DAYS):
                found.append(cand)
                break

    if known_pub:
        # 早于发布日的必然不是开奖日（多半是活动开始日或往期回顾），扔掉。
        # 发布时间未知时不能这么滤——那会把「已开奖」这个信号一起丢掉。
        found = [d for d in found if d >= base]
    # 离基准日太远的多半是解析错了（把发货承诺、版权年份当成了开奖日）
    found = [d for d in found if (d - base).days <= MAX_DRAW_AHEAD_DAYS]
    if not found:
        return 0

    # +86399 = 当天 23:59:59
    return int(time.mktime(max(found).timetuple())) + 86399


def looks_like_lottery(target: Target, cfg: dict) -> str:
    """像抽奖就返回命中原因（给人看的一串关键词），不像就返回空字符串。

    规则：正文里要同时出现「抽奖词」和「动作词」，且不能出现「排除词」
    （已开奖、中奖名单之类——那种转了也没用）。
    """
    text = target.text
    if not text:
        return ""

    for word in cfg["skip_words"]:
        if word in text:
            return ""

    hit_lottery = [w for w in cfg["lottery_words"] if w in text]
    hit_action = [w for w in cfg["action_words"] if w in text]
    if not hit_lottery or not hit_action:
        return ""

    return "+".join(hit_lottery[:2] + hit_action[:2])


# ============================================================
# 7. 三件套动作：关注、转发、评论
# ============================================================

def pick_text(texts: list[str], fallback: str) -> str:
    """随机选一句再加个随机尾巴，避免每条都一模一样被判重复内容。"""
    tail = random.choice(["", " ", "~", "！", "。", " 🎁", " ✨"])
    return (random.choice(texts) if texts else fallback) + tail


def do_follow(bili: Bili, uid: int) -> bool:
    try:
        bili.post("https://api.bilibili.com/x/relation/modify",
                  {"fid": uid, "act": 1, "re_src": 11})
        return True
    except ApiError as exc:
        if exc.code in DONE_CODES or "已关注" in exc.message:
            return True     # 早就关注了，也算成功
        raise


def get_group_id(bili: Bili, name: str) -> int | None:
    """拿到（必要时创建）关注分组的 id。"""
    if not name:
        return None
    try:
        for group in bili.get("https://api.bilibili.com/x/relation/tags") or []:
            if group.get("name") == name:
                return int(group["tagid"])
    except ApiError as exc:
        log.warning("读关注分组失败: %s", exc)
    try:
        data = bili.post("https://api.bilibili.com/x/relation/tag/create", {"tag": name})
        return int((data or {}).get("tag_id") or 0) or None
    except ApiError as exc:
        log.warning("建关注分组「%s」失败: %s", name, exc)
        return None


def move_to_group(bili: Bili, uid: int, group_id: int) -> None:
    try:
        bili.post("https://api.bilibili.com/x/relation/tags/addUsers",
                  {"fids": uid, "tagids": group_id})
    except ApiError as exc:
        log.debug("把 %s 移进分组失败: %s", uid, exc)


def do_repost(bili: Bili, dyn_id: str, text: str) -> bool:
    """先用新版发动态接口，不行再退回老的转发接口。"""
    try:
        bili.post_json("https://api.bilibili.com/x/dynamic/feed/create/dyn", {
            "dyn_req": {
                "content": {"contents": [{"raw_text": text, "type": 1, "biz_id": ""}]},
                "scene": 4,
                "attach_card": None,
                "upload_id": f"{bili.mid}_{int(time.time())}_{random.randint(1000, 9999)}",
                "meta": {"app_meta": {"from": "create.dynamic.web", "mobi_app": "web"}},
            },
            "web_repost_src": {"dyn_id_str": str(dyn_id)},
        })
        return True
    except ApiError as exc:
        if exc.is_risk or exc.is_auth:
            raise       # 风控不能靠换接口绕，直接停
        log.debug("新版转发接口失败(%s)，试试老接口", exc)

    try:
        bili.post("https://api.vc.bilibili.com/dynamic_repost/v1/dynamic_repost/repost",
                  {"dynamic_id": dyn_id, "content": text, "at_uids": "", "ctrl": "[]"})
        return True
    except ApiError as exc:
        if exc.code in DONE_CODES:
            return True
        raise


def resolve_comment_target(bili: Bili, target: Target) -> bool:
    """补齐评论区的 oid / type，补不上返回 False。

    评论区并不挂在动态自己身上：图文动态（DYNAMIC_TYPE_DRAW）的评论区是
    oid=379837513 type=11 这种，跟 dyn_id 毫无关系。正确的值在动态的
    basic.comment_id_str / comment_type 里，但**话题卡片和关注流这些列表接口
    经常不返回 basic**，只有单条详情接口才有——所以缺了就回头查一次详情。

    以前这里是「缺了就按 type=17 兜底」，结果对图文动态一律 -404，评论根本发不出去。
    宁可跳过评论，也不要往错的地方发。
    """
    if target.comment_id and target.comment_type:
        return True

    detail = _fetch_one(bili, target.dyn_id)
    if detail and detail.comment_id and detail.comment_type:
        target.comment_id = detail.comment_id
        target.comment_type = detail.comment_type
        log.debug("%s 评论区定位到 oid=%s type=%s",
                  target.dyn_id, target.comment_id, target.comment_type)
        return True

    log.warning("%s 查不到评论区位置，跳过评论", target.url)
    return False


def do_comment(bili: Bili, target: Target, text: str) -> int:
    """发评论，返回评论 id（rpid）；失败返回 0。

    rpid 要留着——后面靠它回读评论列表，确认评论真的挂上去了。
    """
    try:
        data = bili.post("https://api.bilibili.com/x/v2/reply/add", {
            "oid": target.comment_id,
            "type": target.comment_type,
            "message": text,
            "plat": 1,
        })
    except ApiError as exc:
        if exc.is_risk or exc.is_auth:
            raise
        log.warning("评论失败: %s", exc)
        return 0

    rpid = int(((data or {}).get("rpid")) or ((data or {}).get("reply") or {}).get("rpid") or 0)
    if not rpid:
        log.warning("%s 评论接口返回成功但没给 rpid，无法验证", target.dyn_id)
    return rpid


NO_SUCH_REPLY = 12006       # 「没有该评论」
VERIFY_ATTEMPTS = 4         # 复查评论最多试几次（B 站索引有延迟）
VERIFY_WAIT = 4.0           # 每次重试前等几秒，逐次加长


def verify_comment(bili: Bili, target: Target, rpid: int) -> str:
    """按 rpid 查这条评论到底存不存在，返回一句给人看的结论。

    只看 reply/add 返回 code 0 是不够的：B 站可能返回成功，但评论进了审核、
    被删、或者压根发到了别的地方。

    这里用 reply/reply?root=<rpid> 做**确定性**检查——评论不存在会明确返回
    12006「没有该评论」。之前是翻评论列表找 rpid，那样只在「刚发完」那一刻
    有效：热门抽奖评论区一天几千条（实测有 18677 条的），隔一天回头复查就
    一律「没找到」，全是假警报。按 rpid 查则隔多久都能复查。

    另外要重试：刚发出去的评论 B 站要过几秒才索引得到，发完立刻查会误报。
    """
    if not rpid:
        return "无法验证：没拿到 rpid"

    data = None
    for attempt in range(VERIFY_ATTEMPTS):
        try:
            data = bili.get("https://api.bilibili.com/x/v2/reply/reply",
                            {"oid": target.comment_id, "type": target.comment_type,
                             "root": rpid, "ps": 1, "pn": 1})
            break
        except ApiError as exc:
            if exc.code != NO_SUCH_REPLY:
                return f"无法验证：{exc}"
            # 刚发出去的评论，B 站这边要过几秒才查得到。实测发完立刻查有一半
            # 报「没找到」，隔两分钟再查全都在——那是索引延迟，不是没发上。
            # 这种假警报比不验证还糟，会让人以为评论真的丢了。
            if attempt == VERIFY_ATTEMPTS - 1:
                return "没找到（这条评论不存在，可能被删或没过审）"
            time.sleep(VERIFY_WAIT * (attempt + 1))

    root = (data or {}).get("root") or {}
    if not root.get("rpid"):
        return "无法验证：接口没返回评论内容"
    if str((root.get("member") or {}).get("mid")) != str(bili.mid):
        return "存疑（rpid 查到了，但作者不是本账号）"
    return "已确认（按 rpid 查到，作者是本账号）"


# ============================================================
# 8. 主流程：扫 -> 判 -> 参与 -> 记账
# ============================================================

@dataclass
class Joined:
    target: Target
    followed: bool = False
    followed_uids: list[str] = field(default_factory=list)   # 实际关注成功的号
    reposted: bool = False
    commented: bool = False
    rpid: int = 0                # 评论 id，留着验证用
    comment_check: str = ""      # 按 rpid 复查的验证结论
    error: str = ""

    @property
    def ok(self) -> bool:
        return not self.error and (self.followed or self.reposted or self.commented)


@dataclass
class Summary:
    scanned: int = 0
    lotteries: int = 0
    skip_done: int = 0
    skip_dup: int = 0            # 同一个抽奖有多个转发入口，折叠掉的条数
    skip_self: int = 0
    over_limit: int = 0          # 可参与但超出本轮上限、没排上的
    skip_drawn: int = 0
    skip_old: int = 0
    stopped: str = ""
    risk_sources: list[str] = field(default_factory=list)   # 被风控打掉的来源


class Stop(RuntimeError):
    """出大事了（连续风控 / Cookie 失效），本轮不再继续。"""

    def __init__(self, message: str, joined: "Joined | None" = None):
        super().__init__(message)
        self.joined = joined


def scan(bili: Bili, cfg: dict, rec: Record) -> tuple[list[Target], Summary]:
    summary = Summary()
    _RISK_HIT.clear()
    candidates = find_candidates(bili, cfg, rec)
    summary.scanned = len(candidates)
    summary.risk_sources = list(_RISK_HIT)

    # 以 B 站上的实际转发历史为准来去重，record.json 只当补充
    done_ids = fetch_my_reposts(bili, cfg["my_repost_pages"]) if cfg["check_my_reposts"] else set()
    log.info("共 %d 条候选动态，开始逐条判断像不像抽奖", len(candidates))

    todo: list[Target] = []
    for target in candidates:
        # 四条线一起比：这条动态本身、它的根动态，各自去 record 和实际转发历史里查。
        # 少比一条就可能重复转发同一个抽奖。
        ids = {target.dyn_id} | ({target.root_id} if target.root_id else set())
        if (ids & done_ids) or any(rec.done(i) for i in ids):
            summary.skip_done += 1
            continue

        # 跳过自己发的。我们的转发文案本身就含「转发抽奖」，不然会把自己
        # 上一轮的转发又当成抽奖，转发自己的转发
        if target.uid and str(target.uid) == str(bili.mid):
            summary.skip_self += 1
            continue

        # 判定只看正文，不发请求，所以这一步很快
        target.reason = looks_like_lottery(target, cfg)
        if not target.reason:
            continue

        summary.lotteries += 1

        # 判断还开不开着，优先信正文里写的开奖时间，那是硬信息；
        # 正文没写才退回「发布多久了」这个粗略近似
        target.draw_ts = parse_draw_date(target.text, target.pub_ts)
        if target.draw_ts:
            if target.draw_ts < time.time():
                summary.skip_drawn += 1
                log.debug("%s 正文写着 %s，已经开完了", target.dyn_id, target.draw_at)
                continue
        elif cfg["max_age_days"] and target.age_days > cfg["max_age_days"]:
            summary.skip_old += 1
            log.debug("%s 发布于 %s 且正文没写开奖时间，太老了", target.dyn_id, target.pub_at)
            continue

        # 凑够本轮上限后不再往名单里加，但**继续判定剩下的**。
        # 判定是纯本地匹配、不发请求，继续跑不花代价；提前 break 的话，
        # 报告里「扫了 482 条动态，其中抽奖 4 个」就成了假话——实际上只判了
        # 一小部分就收工了，你也看不出到底还有多少抽奖在等着。
        if len(todo) < cfg["max_per_run"]:
            todo.append(target)
        else:
            summary.over_limit += 1

    todo, summary.skip_dup = collapse_by_root(todo)
    if summary.skip_dup:
        log.info("折叠掉 %d 条重复入口（同一个抽奖有多个转发入口）", summary.skip_dup)
    if summary.over_limit:
        log.info("本轮上限 %d 个，另有 %d 个可参与的没排上",
                 cfg["max_per_run"], summary.over_limit)
    return todo, summary


def collapse_by_root(targets: list[Target]) -> tuple[list[Target], int]:
    """同一个抽奖的多个入口只留一条。返回（留下的, 折叠掉几条）。

    真实翻车：同一轮里既扫到了 ASUS 转发川崎的那条，又扫到了川崎的原动态。
    两条都参与，B 站把两次转发都挂到川崎那个根动态上，等于同一个抽奖转了两次。

    留哪一条：优先留「本身就是根」的那条，那才是抽奖的正主。被折叠掉的那条
    如果 @ 了联名方，把它的 @ 名单并过来，否则会漏关合作方。
    """
    by_root: dict[str, list[Target]] = {}
    for t in targets:
        by_root.setdefault(t.root_id or t.dyn_id, []).append(t)

    kept: list[Target] = []
    folded = 0
    for root, group in by_root.items():
        if len(group) == 1:
            kept.append(group[0])
            continue
        pick = next((t for t in group if t.dyn_id == root), group[0])
        for other in group:
            if other is pick:
                continue
            pick.at_uids.update(other.at_uids)      # 合作方名单别丢
            folded += 1
            log.info("  %s 和 %s 是同一个抽奖（根 %s），只参与前者",
                     pick.dyn_id, other.dyn_id, root)
        kept.append(pick)
    return kept, folded


def join_one(bili: Bili, cfg: dict, target: Target,
             group_id: int | None, risk_hits: list[int]) -> Joined:
    result = Joined(target)
    try:
        if cfg["do_follow"] and target.uid:
            # 要关注的不止发动态的人：联名抽奖写「关注 @甲 和 @乙」，
            # 少关注一个就等于没参与。正文 @ 到的号一起关注。
            wanted = {str(target.uid): target.uname}
            if cfg["follow_at_mentions"]:
                wanted.update(target.at_uids)

            for uid, name in wanted.items():
                bili.pause()
                try:
                    if do_follow(bili, int(uid)):
                        result.followed_uids.append(uid)
                        if group_id:
                            move_to_group(bili, int(uid), group_id)
                except ApiError as exc:
                    if exc.is_risk or exc.is_auth:
                        raise
                    if exc.code == FOLLOW_FULL:
                        # 关注满了就别接着跑了。抽奖基本都要求关注，关不上就等于
                        # 白转白评论——还不如停下来先腾位置。
                        raise Stop(
                            "关注数已达上限，关不了新号了。抽奖基本都要求关注，"
                            "再跑下去只是白转。\n"
                            "  先腾位置：python follow.py --unfollow 已注销\n"
                            "            python follow.py --unfollow 长期不更新\n"
                            "  （先用 --dry-run 看看会取关谁）", result) from exc
                    log.warning("关注 %s(%s) 失败: %s", name or "?", uid, exc)
            # 发动态的人关注上了才算数——@ 的合作方失败只是少一层保险
            result.followed = str(target.uid) in result.followed_uids
            if len(wanted) > 1:
                log.info("    关注了 %d 个（正文要求关注 %s）",
                         len(result.followed_uids),
                         "、".join(n or u for u, n in wanted.items()))

        if cfg["do_repost"]:
            bili.pause()
            result.reposted = do_repost(bili, target.dyn_id,
                                        pick_text(cfg["repost_texts"], "转发抽奖"))

        if cfg["do_comment"] and resolve_comment_target(bili, target):
            bili.pause()
            result.rpid = do_comment(bili, target,
                                     pick_text(cfg["comment_texts"], "参与一下"))
            result.commented = bool(result.rpid)
            if result.rpid:
                result.comment_check = verify_comment(bili, target, result.rpid)
                log.info("    评论验证：%s", result.comment_check)
    except ApiError as exc:
        result.error = str(exc)
        if exc.is_auth:
            raise Stop(f"Cookie 失效: {exc}", result) from exc
        if exc.is_risk:
            risk_hits[0] += 1
            if risk_hits[0] >= MAX_RISK_HITS:
                raise Stop(f"连续 {risk_hits[0]} 次风控，本轮收工", result) from exc
            bili.cooldown()
        else:
            log.warning("参与 %s 失败: %s", target.url, exc)
    return result


def join_all(bili: Bili, cfg: dict, rec: Record,
             todo: list[Target], summary: Summary) -> list[Joined]:
    left_today = cfg["max_per_day"] - rec.today_count()
    if left_today <= 0:
        summary.stopped = f"今天已经参与 {rec.today_count()} 个，到上限了"
        return []

    group_id = None
    if cfg["do_follow"] and cfg["follow_group"]:
        group_id = rec.data["group_id"] or get_group_id(bili, cfg["follow_group"])
        rec.data["group_id"] = group_id

    results: list[Joined] = []
    risk_hits = [0]     # 用列表装，好让 join_one 改得动
    # 这一轮里已经参与到的动态（含它们折叠到的根动态）。扫描那一刻的快照不够用：
    # 参与完 A 之后，B 站把转发挂到了 A 的根动态上，如果名单里还有那个根动态，
    # 不当场记下来就会再转一遍。真发生过——川崎那个抽奖被转了两次。
    done_now: set[str] = set()
    for i, target in enumerate(todo, 1):
        if left_today <= 0:
            summary.stopped = "到今日上限了，剩下的下次再来"
            break

        # 动手前再确认一次：这一轮里是不是已经参与过同一个抽奖了
        ids = {target.dyn_id} | ({target.root_id} if target.root_id else set())
        if ids & done_now:
            summary.skip_dup += 1
            log.info("[%d/%d] 跳过 %s —— 本轮已经参与过同一个抽奖了",
                     i, len(todo), target.url)
            continue

        log.info("[%d/%d] %s  UP=%s  命中=%s  %s", i, len(todo), target.url,
                 target.uname or target.uid, target.reason, target.brief)
        try:
            result = join_one(bili, cfg, target, group_id, risk_hits)
        except Stop as exc:
            if exc.joined:
                results.append(exc.joined)
            summary.stopped = str(exc)
            break

        results.append(result)
        if result.ok:
            left_today -= 1
            done_now |= ids          # 当场记下，后面同根的就不会再转一遍
            rec.add(target.dyn_id, {
                "uid": target.uid, "uname": target.uname, "reason": target.reason,
                "root_id": target.root_id,
                "followed_uids": result.followed_uids,
                "need_follow": list(target.at_uids) or [str(target.uid)],
                "draw_ts": target.draw_ts, "rpid": result.rpid,
                "comment_check": result.comment_check,
                "followed": result.followed, "reposted": result.reposted,
                "commented": result.commented,
            })
            rec.save()
    return results


# ============================================================
# 9. 命令行入口：解析参数、跑流程、打印报告
# ============================================================

def fix_console_encoding() -> None:
    """让输出遇到编不出的字符时别崩。

    Windows 控制台默认是 GBK，而抽奖动态的正文里经常有 emoji（📢🎉🎁），
    直接 print 会抛 UnicodeEncodeError 把整个脚本弄崩。

    这里只改错误处理、不动编码：保留控制台原生编码，中文照常显示，
    只有真编不出来的字符变成问号。如果改成强行 UTF-8，在 GBK 控制台下
    连中文都会变乱码，反而更糟。
    """
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(errors="replace")
            except (ValueError, OSError):
                pass        # 输出被重定向到不支持的地方，忽略


def setup_logging(verbose: bool) -> None:
    ROOT.joinpath("logs").mkdir(exist_ok=True)
    console = logging.StreamHandler(sys.stdout)
    console.setLevel(logging.DEBUG if verbose else logging.INFO)
    to_file = logging.FileHandler(ROOT / "logs" / "run.log", encoding="utf-8")
    to_file.setLevel(logging.DEBUG)
    logging.basicConfig(level=logging.DEBUG, handlers=[console, to_file],
                        format="%(asctime)s %(levelname)-7s %(message)s", datefmt="%H:%M:%S")
    logging.getLogger("urllib3").setLevel(logging.WARNING)


def ask(cfg: dict, todo: list[Target]) -> bool:
    acts = "+".join(name for name, on in (("关注", cfg["do_follow"]),
                                          ("转发", cfg["do_repost"]),
                                          ("评论", cfg["do_comment"])) if on)
    print(f"\n找到 {len(todo)} 个能参与的抽奖，准备执行 [{acts}]：")
    for target in todo[:10]:
        print(f"  · {target.url}  {target.uname or target.uid}"
              f"  {target.pub_at}发布 / {target.draw_at}  [{target.reason}]")
        print(f"      {target.brief}")
    if len(todo) > 10:
        print(f"  ... 还有 {len(todo) - 10} 个")
    return input("\n确认继续？(输 y 回车继续，其他任意键取消) ").strip().lower() in ("y", "yes")


def report(summary: Summary, results: list[Joined], test_mode: bool) -> None:
    print("\n" + "=" * 60)
    print(f"扫了 {summary.scanned} 条动态，其中官方抽奖 {summary.lotteries} 个")
    print(f"跳过：参与过的 {summary.skip_done} 个，已开奖的 {summary.skip_drawn} 个，"
          f"太老的 {summary.skip_old} 个，自己发的 {summary.skip_self} 个"
          + (f"，同一抽奖的重复入口 {summary.skip_dup} 个" if summary.skip_dup else ""))
    if summary.over_limit:
        print(f"另有 {summary.over_limit} 个能参与，但超出本轮上限没排上"
              f"（改 config.json 的 max_per_run 或加 --max N 可以放开）")
    if summary.risk_sources:
        n = len(summary.risk_sources)
        head = "、".join(summary.risk_sources[:4]) + ("…" if n > 4 else "")
        print(f"\n⚠ 有 {n} 个来源被风控挡掉了，这轮**没扫全**：{head}")
        print("  上面的数字是打了折的，不代表今天抽奖就这么少。"
              "歇十几分钟再跑一次，或把 up_mids 拆成两批轮流扫。")

    if test_mode:
        print(f"\n【试跑】以下 {len(results)} 个抽奖会被参与（现在什么都没做）：")
        for r in results:
            print(f"  · {r.target.url}  {r.target.uname or r.target.uid}"
                  f"  {r.target.pub_at}发布 / {r.target.draw_at}  [命中 {r.target.reason}]")
            print(f"      {r.target.brief}")
        print("\n没问题的话，去掉 --test 再跑一次就会真的参与。")
    else:
        good = [r for r in results if r.ok]
        bad = [r for r in results if not r.ok]
        print(f"\n成功 {len(good)} 个：")
        for r in good:
            tags = "".join(t for t, on in (("关", r.followed), ("转", r.reposted),
                                           ("评", r.commented)) if on)
            print(f"  ✓ [{tags}] {r.target.url}  {r.target.uname or r.target.uid}"
                  f"  {r.target.draw_at}  [{r.target.reason}]")
            if r.comment_check:
                print(f"        评论验证: {r.comment_check}  rpid={r.rpid}")
        if bad:
            print(f"\n失败 {len(bad)} 个：")
            for r in bad:
                print(f"  ✗ {r.target.url}  {r.error or '什么都没做成'}")

    if summary.stopped:
        print(f"\n提前结束：{summary.stopped}")
    print("=" * 60)


SELF_CHECK_DYN = "1240492189851582487"      # 拿一条公开动态当探针


def _probe_comment(bili: Bili) -> None:
    """按正式流程走一遍：先定位评论区，再读评论。定位不到就算这一项失败。"""
    target = _fetch_one(bili, SELF_CHECK_DYN)
    if not target:
        raise ApiError(-1, "动态详情拉不到，评论区无从定位")
    if not resolve_comment_target(bili, target):
        raise ApiError(-1, "定位不到评论区的 oid/type")
    bili.get("https://api.bilibili.com/x/v2/reply",
             {"oid": target.comment_id, "type": target.comment_type,
              "sort": 0, "ps": 1, "pn": 1})


def self_check(bili: Bili, cfg: dict, rec: Record) -> int:
    """自检：把工具依赖的每个接口都戳一遍，看还活不活。

    B 站下线接口的频率很高——官方抽奖判定接口、话题名搜索、分区树接口都是
    跑着跑着就没了的。与其等某次正式跑时莫名其妙一个都扫不到，不如有个地方
    一眼看出「哪个接口死了」。

    只发 GET，不会关注/转发/评论任何东西。
    """
    print("\n" + "=" * 62)
    print("自检")
    print("=" * 62)
    bad = 0

    # ---- 账号 ----
    print("\n【账号】")
    try:
        uname = bili.login_check()
        print(f"  ok    登录正常：{uname} (uid={bili.mid})")
    except ApiError as exc:
        print(f"  失败  Cookie 用不了：{exc}")
        print("        去 README「第 3 步」重新复制一份 Cookie。")
        return 1        # 登录都不行，后面没必要测了

    # ---- 接口 ----
    # (名字, 请求, 这个接口挂了会怎样)
    probes = [
        ("关注流",
         lambda: bili.get(f"{WEB_DYN}/feed/all",
                          {"type": "all", "page": 1, "offset": "",
                           "timezone_offset": -480, "platform": "web",
                           "features": FEATURES}, wbi=True),
         "找不到关注的人发的抽奖（这是产量最大的来源）"),
        ("UP 空间动态",
         lambda: bili.get(f"{WEB_DYN}/feed/space",
                          {"host_mid": bili.mid, "offset": "",
                           "timezone_offset": -480, "platform": "web",
                           "features": FEATURES}, wbi=True),
         "盯不了指定 UP，也读不到自己的转发历史（去重会退化）"),
        ("动态详情",
         lambda: bili.get(f"{WEB_DYN}/detail",
                          {"id": SELF_CHECK_DYN, "features": FEATURES,
                           "timezone_offset": -480, "platform": "web"}, wbi=True),
         "定位不到评论区，需要评论的抽奖会跳过评论"),
        ("话题卡片",
         lambda: bili.get("https://api.bilibili.com/x/topic/web/details/cards",
                          {"topic_id": (cfg["topic_ids"] or [1261901])[0],
                           "offset": "", "page_size": 20, "sort_by": 3}),
         "扫不到抽奖话题板块"),
        # 注意：评论区的 oid/type 跟动态 id 无关，必须先定位再查。
        # 一开始我图省事直接拿 dyn_id + type=17 去探，对图文动态一律 -404，
        # 自检自己报了个假故障。
        ("评论区定位 + 读评论", lambda: _probe_comment(bili), "评论发出去后没法验证"),
        ("关注分组",
         lambda: bili.get("https://api.bilibili.com/x/relation/tags"),
         "关注的 UP 归不了「抽奖」分组，以后不好批量取关"),
        ("关注列表",
         lambda: bili.get("https://api.bilibili.com/x/relation/followings",
                          {"vmid": bili.mid, "pn": 1, "ps": 1, "order": "desc"}),
         "follow.py 的分类和取关都用不了"),
        ("投稿列表",
         lambda: bili.get("https://api.bilibili.com/x/space/wbi/arc/search",
                          {"mid": bili.mid, "ps": 1, "pn": 1, "order": "pubdate"},
                          wbi=True),
         "follow.py 认不出 UP 的内容分区"),
    ]
    print("\n【接口】B 站下线接口很勤，这里逐个戳一遍")
    for name, call, hurt in probes:
        try:
            call()
            print(f"  ok    {name}")
        except ApiError as exc:
            bad += 1
            print(f"  失败  {name}：{exc}")
            print(f"        影响：{hurt}")

    # ---- 配置 ----
    print("\n【配置】")
    warns = []
    if not cfg["lottery_words"] or not cfg["action_words"]:
        warns.append("抽奖词或动作词是空的，那就一个抽奖都判不出来")
    if cfg["min_delay"] < 10:
        warns.append(f"动作间隔 {cfg['min_delay']} 秒偏短，容易撞风控（建议 20 以上）")
    if cfg["max_delay"] < cfg["min_delay"]:
        warns.append("max_delay 比 min_delay 还小，间隔会退化成固定值")
    if not cfg["do_repost"]:
        warns.append("do_repost 是关的——转发抽奖不转发等于没参与")
    if not (cfg["topic_ids"] or cfg["up_mids"] or cfg["scan_following"]
            or cfg["dynamic_ids"]):
        warns.append("四个来源全关了，扫不到任何动态")
    if cfg["max_per_run"] > cfg["max_per_day"]:
        warns.append("max_per_run 比 max_per_day 还大，单轮上限实际不起作用")
    for w in warns:
        print(f"  注意  {w}")
    if not warns:
        print(f"  ok    没发现问题"
              f"（单轮上限 {cfg['max_per_run']}，每日上限 {cfg['max_per_day']}，"
              f"间隔 {cfg['min_delay']}~{cfg['max_delay']} 秒）")

    # ---- 关注余量 ----
    # 抽奖基本都要求关注，关注满了整个工具就废了一半，值得单独看一眼
    print("\n【关注余量】")
    try:
        data = bili.get("https://api.bilibili.com/x/relation/followings",
                        {"vmid": bili.mid, "pn": 1, "ps": 1, "order": "desc"})
        total = int((data or {}).get("total") or 0)
        nav = bili.get("https://api.bilibili.com/x/web-interface/nav")
        # 上限取决于账号类型，这里按最宽松的算，够用了
        cap = 5000 if nav.get("vipStatus") or int(nav.get("level_info", {})
                                                  .get("current_level", 0)) >= 6 else 1000
        left = cap - total
        if left <= 0:
            bad += 1
            print(f"  已满  {total}/{cap}，关不了新号了")
            print("        抽奖基本都要求关注，这种状态下参与等于白转。")
            print("        腾位置：python follow.py --unfollow 已注销")
        elif left < 50:
            print(f"  注意  {total}/{cap}，只剩 {left} 个位置了")
            print("        快满了，建议先用 follow.py 清一批注销号和僵尸号。")
        else:
            print(f"  ok    {total}/{cap}，还能关注 {left} 个")
    except ApiError as exc:
        print(f"  查不到关注数：{exc}")

    # ---- 记录 ----
    print("\n【记录】")
    joined = rec.data.get("joined", {})
    alias = sum(1 for v in joined.values() if v.get("alias_of"))
    print(f"  参与过 {len(joined) - alias} 个抽奖"
          f"{f'（另有 {alias} 条根动态别名）' if alias else ''}")
    print(f"  今天已用 {rec.today_count()} / {cfg['max_per_day']} 个名额")
    print(f"  学到的抽奖话题 {len(rec.topics)} 个："
          f"{'、'.join(rec.topics.values()) if rec.topics else '（还没学到）'}")
    verified = [v.get("comment_check", "") for v in joined.values()
                if v.get("rpid") and not v.get("alias_of")]
    if verified:
        good = sum(1 for v in verified if v.startswith("已确认"))
        print(f"  评论验证：{good}/{len(verified)} 条已确认在评论区")

    print("\n" + "=" * 62)
    if bad:
        print(f"有 {bad} 个接口不通。B 站可能又改接口了——看上面「影响」那行，"
              f"判断还能不能凑合用。")
    else:
        print("全部正常。")
    print("离线逻辑自检另跑：python tests.py")
    print("=" * 62)
    return 1 if bad else 0


def main() -> int:
    parser = argparse.ArgumentParser(description="一键参与 B 站抽奖")
    parser.add_argument("--check", action="store_true",
                        help="自检：看 Cookie 还灵不灵、各个接口有没有下线")
    parser.add_argument("--test", action="store_true", help="试跑：只看会参与哪些，不动手")
    parser.add_argument("--max", type=int, metavar="N", default=0,
                        help="本轮最多参与几个，覆盖 config.json 的 max_per_run")
    parser.add_argument("-y", "--yes", action="store_true", help="不问我，直接跑（挂定时任务用）")
    parser.add_argument("-v", "--verbose", action="store_true", help="打印详细日志，排查问题用")
    parser.add_argument("-c", "--config", default=str(ROOT / "config.json"), help="配置文件路径")
    parser.add_argument("-s", "--record", default=str(ROOT / "record.json"), help="参与记录路径")
    args = parser.parse_args()

    fix_console_encoding()      # 必须在任何输出之前
    setup_logging(args.verbose)
    cfg = load_config(args.config)      # 配置有问题会直接退出并说明原因
    if args.max:
        # 池子大的时候临时放开，省得为跑一次去改配置再改回来
        cfg["max_per_run"] = args.max
    rec = Record(args.record)
    bili = Bili(cfg["cookie"], cfg["min_delay"], cfg["max_delay"])

    try:
        bili.ensure_buvid()
        if args.check:
            return self_check(bili, cfg, rec)

        print(f"当前账号：{bili.login_check()} (uid={bili.mid})"
              + ("   ← 试跑模式" if args.test else ""))

        todo, summary = scan(bili, cfg, rec)
        log.info("判断完毕：抽奖 %d 个，其中能参与 %d 个", summary.lotteries, len(todo))

        results: list[Joined] = []
        if not todo:
            print("\n这轮没找到新的可参与抽奖。")
        elif args.test:
            results = [Joined(t) for t in todo]
        elif args.yes or ask(cfg, todo):
            results = join_all(bili, cfg, rec, todo, summary)
        else:
            summary.stopped = "你取消了"

        report(summary, results, args.test)
        return 0
    except ApiError as exc:
        log.error("接口报错，已停下：%s", exc)
        return 1
    except KeyboardInterrupt:
        print("\n已中断，进度已保存。")
        return 130
    finally:
        rec.save()


if __name__ == "__main__":
    sys.exit(main())
