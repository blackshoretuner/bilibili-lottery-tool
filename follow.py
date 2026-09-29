#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""B 站关注管理 —— 把关注按群体分类，然后整组取关；顺带清理注销号和僵尸号。

用法：
    python follow.py                     看分类报告（第一次会抓一遍关注列表）
    python follow.py --refresh           重新抓关注列表（默认用缓存）
    python follow.py --deep 200          深度检查 200 个还没查过的（注销/僵尸）
    python follow.py --unfollow 抽奖      取关某一类（会先列出来让你确认）
    python follow.py --unfollow 已注销 --limit 50

Cookie、限速、风控那套逻辑都是从 main.py 复用的，配置也共用 config.json。

从上往下分成 5 块，想改哪块直接搜标题。
"""

from __future__ import annotations

import argparse
import json
import logging
import random
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

# 直接复用抽奖脚本里那套客户端：Cookie 解析、WBI 签名、错误码分类、限速、风控冷却
from main import (
    FEATURES, ROOT, WEB_DYN, ApiError, Bili, Record,
    _to_target, _walk_cards, fix_console_encoding, load_config, setup_logging,
)

log = logging.getLogger("follow")

# 读接口用的轻量间隔。main.py 里那个 20~45 秒是给「关注/转发/评论」这类
# 写操作用的，拿来跑几百个只读请求会跑到天亮。
READ_MIN_DELAY = 0.5
READ_MAX_DELAY = 1.4

# 深度检查要跑几千个人、上万次请求，中途几乎必然被风控挡。挡了不能直接退，
# 歇一会儿接着查同一个人就行。歇的时间逐次翻倍：5 分钟、10 分钟、15 分钟…
RISK_COOLDOWN = 300     # 第一次撞风控歇多久（秒）
RISK_MAX_RETRY = 6      # 连着歇这么多次还被挡就真收工

# 取关也是写操作，但比发动态、发评论轻得多——没有内容进公共区域。
# config.json 里那套 20~45 秒是给抽奖三件套用的，拿来取关 492 个要跑
# 四个多小时。这里单独给一档快的，撞风控会自动退避，节奏是自己会调的。
UNFOLLOW_MIN_DELAY = 3.0
UNFOLLOW_MAX_DELAY = 7.0

PAGE_SIZE = 50          # 关注列表每页人数，50 是接口上限
MAX_PAGES = 200         # 兜底，防止接口异常时无限翻页
ZOMBIE_DAYS = 365       # 最后一条动态超过这么久，算长期不更新
DEEP_CACHE_DAYS = 30    # 深度检查结果缓存多久
SAMPLE_TITLES = 8       # 每个 UP 取几个最近标题，用来认具体在做什么


# ============================================================
# 0. 分区对照表
# ============================================================
#
# B 站的投稿分区分两级。一级分区名能从 arc/search 的 tlist 里直接拿到（权威），
# 但二级分区只给 typeid 不给名字——view 接口的 tname 现在返回空字符串，
# 官方也没有分区树接口可查。所以二级只能内置对照表。
#
# 这张表用真实数据校验过：每个 UP 的 tlist 给了一级分区名，如果表里某个
# typeid 的父分区跟 tlist 的主分区长期对不上，就说明那条写错了。
# 遇到表里没有的 typeid，会显示成「游戏区/未知(121)」而不是瞎猜。

SUB_AREAS: dict[str, dict[int, str]] = {
    "动画": {24: "MAD·AMV", 25: "MMD·3D", 47: "短片·手书·配音", 210: "手办·模玩",
             86: "特摄", 253: "动漫杂谈", 27: "综合"},
    "番剧": {51: "资讯", 152: "官方延伸", 32: "完结动画", 33: "连载动画"},
    "国创": {153: "国产动画", 168: "国产原创相关", 169: "布袋戏", 170: "资讯",
             195: "动态漫·广播剧"},
    "音乐": {28: "原创音乐", 31: "翻唱", 30: "VOCALOID·UTAU", 194: "电音", 59: "演奏",
             193: "MV", 29: "音乐现场", 130: "音乐综合", 243: "乐评盘点", 244: "音乐教学"},
    "舞蹈": {20: "宅舞", 198: "街舞", 199: "明星舞蹈", 200: "中国舞", 154: "舞蹈综合",
             156: "舞蹈教程"},
    "游戏": {17: "单机游戏", 171: "电子竞技", 172: "手机游戏", 65: "网络游戏",
             173: "桌游棋牌", 121: "GMV", 136: "音游", 19: "Mugen"},
    "知识": {201: "科学科普", 124: "社科·法律·心理", 228: "人文历史", 207: "财经商业",
             208: "校园学习", 209: "职业职场", 229: "设计·创意", 122: "野生技能协会"},
    "科技": {95: "数码", 230: "软件应用", 231: "计算机技术", 232: "科工机械",
             233: "极客DIY"},
    "运动": {235: "篮球", 249: "足球", 164: "健身", 236: "竞技体育", 237: "运动文化",
             238: "运动综合"},
    "汽车": {245: "赛车", 246: "改装玩车", 247: "新能源车", 248: "房车", 240: "摩托车",
             227: "购车攻略", 176: "汽车生活"},
    "生活": {138: "搞笑", 250: "出行", 251: "三农", 239: "家居房产", 161: "手工",
             162: "绘画", 21: "日常", 254: "亲子"},
    "美食": {76: "美食制作", 212: "美食侦探", 213: "美食测评", 214: "田园美食",
             215: "美食记录"},
    "动物圈": {218: "喵星人", 219: "汪星人", 222: "大熊猫", 221: "野生动物",
               220: "爬宠", 75: "动物综合"},
    "鬼畜": {22: "鬼畜调教", 26: "音MAD", 126: "人力VOCALOID", 216: "鬼畜剧场",
             127: "教程演示"},
    "时尚": {157: "美妆护肤", 252: "仿妆cos", 158: "穿搭", 159: "时尚潮流"},
    "资讯": {203: "热点", 204: "环球", 205: "社会", 206: "综合"},
    "娱乐": {71: "综艺", 241: "娱乐杂谈", 242: "粉丝创作", 137: "明星综合"},
    "影视": {182: "影视杂谈", 183: "影视剪辑", 85: "短片", 184: "预告·资讯"},
    "纪录片": {37: "人文·历史", 178: "科学·探索·自然", 179: "军事",
               180: "社会·美食·旅行"},
    "电影": {147: "华语电影", 145: "欧美电影", 146: "日本电影", 83: "其他国家"},
    "电视剧": {185: "国产剧", 187: "海外剧"},
}

# typeid -> (一级分区, 二级分区)，从上面那张表压平出来，查起来快
TYPEID_MAP: dict[int, tuple[str, str]] = {
    tid: (parent, name) for parent, subs in SUB_AREAS.items() for tid, name in subs.items()
}

# 「二创」不是 B 站的一级分区，是散在几个二级分区里的。这里按创作性质归一类。
RECREATION_TYPEIDS = {24, 25, 47, 22, 26, 126, 216, 242, 183}

# 认「具体在做什么」用的词库：从最近的投稿标题里匹配。
# B 站没有「这个 UP 在做哪个游戏」的接口，标题匹配是唯一可行的办法。
# 想加词就在 config.json 里写 "content_words": ["xxx", "yyy"]，会并进来。
CONTENT_WORDS = [
    # 热门游戏
    "原神", "星穹铁道", "崩坏", "绝区零", "鸣潮", "王者荣耀", "英雄联盟", "无畏契约",
    "CS2", "DOTA", "永劫无间", "三角洲", "使命召唤", "艾尔登法环", "黑神话", "塞尔达",
    "宝可梦", "我的世界", "Minecraft", "泰拉瑞亚", "戴森球", "群星", "文明", "全战",
    "三国杀", "炉石", "金铲铲", "蛋仔", "第五人格", "明日方舟", "碧蓝航线", "少女前线",
    "阴阳师", "FGO", "命运冠位", "赛马娘", "舰娘", "东方", "俄罗斯方块", "怪物猎人",
    "只狼", "血源", "魂系", "生化危机", "GTA", "红色警戒", "星际", "魔兽",
    # 硬件数码
    "显卡", "CPU", "主板", "机械键盘", "耳机", "手机", "笔记本", "路由器", "NAS",
    # 动漫影视
    "番剧", "动画", "剧场版", "漫画", "轻小说", "特摄", "奥特曼", "高达",
]


# ============================================================
# 1. 数据结构与缓存
# ============================================================

@dataclass
class Follow:
    mid: int
    uname: str = ""
    sign: str = ""
    attribute: int = 2          # 2=我单向关注, 6=互相关注
    follow_ts: int = 0          # 关注时间
    groups: list[int] = field(default_factory=list)     # 所在的关注分组 id
    special: int = 0            # 是否特别关注
    official: int = -1          # -1=无认证, 0=个人认证, 1=机构认证
    # 下面这些要逐个查才知道，存在缓存里
    alive: bool | None = None   # None=还没查过, False=注销/封禁
    last_active: int = 0        # 最后一条动态的时间戳，0=没查过或查不到
    area: str = ""              # 一级分区（投稿最多的那个），来自 tlist，权威
    sub_area: str = ""          # 二级分区，来自 typeid + 内置对照表
    recreation: bool = False    # 主要投稿是不是二创性质
    hits: list[str] = field(default_factory=list)      # 标题里命中的内容词
    checked_ts: int = 0

    @property
    def url(self) -> str:
        return f"https://space.bilibili.com/{self.mid}"

    @property
    def follow_days(self) -> float:
        return (time.time() - self.follow_ts) / 86400 if self.follow_ts else 0.0

    @property
    def silent_days(self) -> float:
        return (time.time() - self.last_active) / 86400 if self.last_active else 0.0

    @property
    def checked(self) -> bool:
        return bool(self.checked_ts) and \
            (time.time() - self.checked_ts) / 86400 < DEEP_CACHE_DAYS


class FollowCache:
    """关注列表快照 + 深度检查结果，都存在一个 json 里。

    4994 个关注、每个要两次请求才能判断死活，一次跑完必被限流，
    所以深度检查是增量的：每次查一批，查过的记下来，下次跳过。
    """

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.data: dict[str, Any] = {"fetched_ts": 0, "follows": {}, "tag_names": {}}
        if self.path.exists():
            try:
                self.data.update(json.loads(self.path.read_text(encoding="utf-8")))
            except (ValueError, OSError):
                log.warning("%s 读不出来，当成空的重新开始", self.path)

    @property
    def fetched_at(self) -> str:
        ts = self.data.get("fetched_ts") or 0
        return time.strftime("%Y-%m-%d %H:%M", time.localtime(ts)) if ts else "从未"

    def load(self) -> list[Follow]:
        out = []
        for raw in self.data["follows"].values():
            known = {k: v for k, v in raw.items() if k in Follow.__annotations__}
            out.append(Follow(**known))
        return out

    def save_follows(self, follows: list[Follow], tag_names: dict[int, str]) -> None:
        """存关注列表，但保留已有的深度检查结果。"""
        old = self.data["follows"]
        merged = {}
        for f in follows:
            prev = old.get(str(f.mid)) or {}
            f.alive = f.alive if f.alive is not None else prev.get("alive")
            f.last_active = f.last_active or prev.get("last_active", 0)
            f.checked_ts = f.checked_ts or prev.get("checked_ts", 0)
            f.area = f.area or prev.get("area", "")
            f.sub_area = f.sub_area or prev.get("sub_area", "")
            f.recreation = f.recreation or prev.get("recreation", False)
            f.hits = f.hits or prev.get("hits", [])
            merged[str(f.mid)] = {k: getattr(f, k) for k in Follow.__annotations__}
        self.data["follows"] = merged
        self.data["tag_names"] = {str(k): v for k, v in tag_names.items()}
        self.data["fetched_ts"] = int(time.time())
        self.save()

    def update_one(self, f: Follow) -> None:
        self.data["follows"][str(f.mid)] = {k: getattr(f, k) for k in Follow.__annotations__}

    def drop(self, mid: int) -> None:
        self.data["follows"].pop(str(mid), None)

    def tag_name(self, tid: int) -> str:
        return self.data.get("tag_names", {}).get(str(tid), str(tid))

    def save(self) -> None:
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.data, ensure_ascii=False, indent=1), encoding="utf-8")
        tmp.replace(self.path)


# ============================================================
# 2. 抓取关注列表
# ============================================================

def fetch_tags(bili: Bili) -> dict[int, str]:
    try:
        return {int(t["tagid"]): t.get("name") or str(t["tagid"])
                for t in bili.get("https://api.bilibili.com/x/relation/tags") or []}
    except ApiError as exc:
        log.warning("读分组失败: %s", exc)
        return {}


def fetch_all(bili: Bili) -> list[Follow]:
    """一页 50 个往下翻，直到翻空。4994 个关注约 100 页。"""
    follows: dict[int, Follow] = {}
    for pn in range(1, MAX_PAGES + 1):
        try:
            data = bili.get("https://api.bilibili.com/x/relation/followings",
                            {"vmid": bili.mid, "pn": pn, "ps": PAGE_SIZE, "order": "desc"})
        except ApiError as exc:
            log.warning("第 %d 页拉取失败，先用已拿到的 %d 个: %s", pn, len(follows), exc)
            break

        page = (data or {}).get("list") or []
        for u in page:
            mid = int(u.get("mid") or 0)
            if not mid:
                continue
            follows[mid] = Follow(
                mid=mid,
                uname=u.get("uname") or "",
                sign=u.get("sign") or "",
                attribute=int(u.get("attribute") or 2),
                follow_ts=int(u.get("mtime") or 0),
                groups=[int(t) for t in (u.get("tag") or [])],
                special=int(u.get("special") or 0),
                official=int((u.get("official_verify") or {}).get("type", -1)),
            )
        total = (data or {}).get("total")
        if pn == 1:
            log.info("关注总数 %s，开始翻页", total)
        if len(page) < PAGE_SIZE:
            break
        if pn % 20 == 0:
            log.info("  已拉 %d 个…", len(follows))
        time.sleep(random.uniform(READ_MIN_DELAY, READ_MAX_DELAY))

    log.info("共拉到 %d 个关注", len(follows))
    return list(follows.values())


# ============================================================
# 3. 深度检查：谁注销了、谁长期不更新
# ============================================================

def fetch_area(bili: Bili, f: Follow, words: list[str]) -> None:
    """查这个 UP 主要在哪个分区做内容。一次请求。

    一级分区取 tlist 里投稿最多的那个——名字是接口给的，权威。
    二级分区取最近投稿里出现最多的 typeid，再查内置对照表拿名字。
    """
    try:
        data = bili.get("https://api.bilibili.com/x/space/wbi/arc/search",
                        {"mid": f.mid, "ps": 30, "pn": 1, "order": "pubdate"}, wbi=True)
    except ApiError as exc:
        if exc.is_risk or exc.is_auth:
            raise
        log.debug("%s(%s) 投稿列表读不到: %s", f.uname, f.mid, exc)
        return

    lst = (data or {}).get("list") or {}
    tlist = lst.get("tlist") or {}
    # tlist 是「历史全部投稿」的分区统计，先拿它兜底
    if tlist:
        top = max(tlist.values(), key=lambda v: v.get("count") or 0)
        f.area = top.get("name") or ""

    vlist = lst.get("vlist") or []
    if not vlist:
        return

    # 二级分区：最近投稿里出现最多的 typeid
    counts: dict[int, int] = {}
    for v in vlist:
        tid = int(v.get("typeid") or 0)
        if tid:
            counts[tid] = counts.get(tid, 0) + 1
    if counts:
        tid = max(counts, key=lambda k: counts[k])
        parent, name = TYPEID_MAP.get(tid, ("", ""))
        if parent:
            # 一级也跟着 typeid 走，保证两级自洽。
            # 不这么做会出现「鬼畜区/电子竞技」这种自相矛盾的标签：
            # 历史投稿以鬼畜为主，但最近改做电竞了。以最近的为准更有参考价值。
            f.area, f.sub_area = parent, name
        else:
            # 表里没有这个 typeid，一级还用 tlist 的（那个是权威的），
            # 二级老实显示 id，别瞎猜
            f.sub_area = f"未知({tid})"
        f.recreation = tid in RECREATION_TYPEIDS

    # 标题里命中的内容词，用来认「具体在做什么」
    titles = " ".join((v.get("title") or "") for v in vlist[:SAMPLE_TITLES])
    low = titles.lower()
    f.hits = [w for w in words if (w.lower() in low)][:4]


def check_one(bili: Bili, f: Follow, words: list[str] = ()) -> None:
    """查一个 UP 的死活、最后活跃时间、内容分区，结果写回 f。三次请求。"""
    f.checked_ts = int(time.time())
    try:
        info = bili.get("https://api.bilibili.com/x/space/wbi/acc/info",
                        {"mid": f.mid}, wbi=True)
    except ApiError as exc:
        if exc.is_risk or exc.is_auth:
            raise
        # 账号注销/不存在会直接报错
        f.alive = False
        log.debug("%s(%s) 查不到，按注销算: %s", f.uname, f.mid, exc)
        return

    f.alive = not (info or {}).get("silence")      # silence=1 是被封
    if info.get("name"):
        f.uname = info["name"]
    if not f.alive:
        return

    try:
        data = bili.get(f"{WEB_DYN}/feed/space",
                        {"host_mid": f.mid, "offset": "", "timezone_offset": -480,
                         "platform": "web", "features": FEATURES}, wbi=True)
    except ApiError as exc:
        if exc.is_risk or exc.is_auth:
            raise
        log.debug("%s(%s) 动态读不到: %s", f.uname, f.mid, exc)
        return

    stamps = [t.pub_ts for t in
              (_to_target(c, "x") for c in _walk_cards(data)) if t and t.pub_ts]
    f.last_active = max(stamps) if stamps else 0

    fetch_area(bili, f, list(words) or CONTENT_WORDS)


def deep_check(bili: Bili, follows: list[Follow], cache: FollowCache,
               limit: int, words: list[str] = ()) -> int:
    """挑还没查过的查一批。返回这轮查了多少个。

    撞风控不再直接收工——check_one 只在风控/掉登录时才往外抛，别的错
    （注销、私密、没投稿）它自己咽了。所以这里抓到的基本都是风控，
    歇一会儿接着查同一个人就行。第一版一挡就 break，结果 4993 个人
    跑到 415 就停了，怎么跑都扫不完。
    """
    todo = [f for f in follows if not f.checked][:limit]
    if not todo:
        print("所有关注都查过了（缓存 %d 天内有效）。想重查就删掉 %s"
              % (DEEP_CACHE_DAYS, cache.path.name))
        return 0

    print(f"要查 {len(todo)} 个（每个三次请求，大约 {len(todo) * 3.0 / 60:.0f} 分钟；"
          f"撞风控会自动歇一会儿再接着跑，不用管它）…", flush=True)
    done = 0
    naps = 0                       # 连着歇了几次，查成功一个就清零
    i = 0
    while i < len(todo):
        f = todo[i]
        try:
            check_one(bili, f, words)
        except ApiError as exc:
            cache.save()           # 先把已经查好的存住，再决定歇还是走
            if exc.is_auth:
                print(f"\nCookie 失效了，已查 {done} 个并存好：{exc}", flush=True)
                return done
            naps += 1
            if naps > RISK_MAX_RETRY:
                print(f"\n歇了 {RISK_MAX_RETRY} 次还是被挡，今天先到这（已查 {done} 个，"
                      f"进度存好了，过几小时再跑 --deep 会从这儿接着查）：{exc}", flush=True)
                return done
            wait = RISK_COOLDOWN * naps
            print(f"\n撞到风控（{exc}），歇 {wait // 60} 分钟再接着查 —— "
                  f"已查 {done}/{len(todo)}，第 {naps}/{RISK_MAX_RETRY} 次", flush=True)
            time.sleep(wait)
            continue               # 同一个人重来，别跳过
        naps = 0
        cache.update_one(f)
        done += 1
        i += 1
        if done % 25 == 0:
            cache.save()
            print(f"  {done}/{len(todo)}  已存盘", flush=True)
        time.sleep(random.uniform(READ_MIN_DELAY, READ_MAX_DELAY))

    cache.save()
    return done


# ============================================================
# 4. 分类
# ============================================================

def build_groups(follows: list[Follow], rec: Record,
                 cache: FollowCache) -> dict[str, list[Follow]]:
    """按群体分类。一个人可以同时属于多个类别。

    分类依据都来自关注列表本身（不额外请求），只有「已注销」「长期不更新」
    要靠深度检查的结果。
    """
    lottery_uids = {str(v.get("uid")) for v in rec.data.get("joined", {}).values()}
    lottery_tags = {tid for tid, name in
                    ((t, cache.tag_name(t)) for f in follows for t in f.groups)
                    if "抽奖" in name}

    def is_lottery(f: Follow) -> bool:
        # 抽奖号靠两条线认：参与记录里的 uid，和「抽奖」分组。
        # 实测名字/签名带「抽奖」的一个都没有，所以关键词那条路没用。
        return str(f.mid) in lottery_uids or bool(set(f.groups) & lottery_tags)

    rules: dict[str, Callable[[Follow], bool]] = {
        "抽奖":        is_lottery,
        "未分组":      lambda f: not f.groups,
        "互关":        lambda f: f.attribute == 6,
        "单向关注":    lambda f: f.attribute != 6,
        "特别关注":    lambda f: bool(f.special),
        "机构号":      lambda f: f.official == 1,
        "个人认证":    lambda f: f.official == 0,
        "无认证":      lambda f: f.official == -1,
        "关注超一年":  lambda f: f.follow_days > 365,
        "最近30天关注": lambda f: 0 < f.follow_days <= 30,
        "已注销":      lambda f: f.alive is False,
        "长期不更新":  lambda f: (f.alive is True and f.last_active
                                  and f.silent_days > ZOMBIE_DAYS),
    }
    groups = {name: [f for f in follows if rule(f)] for name, rule in rules.items()}

    # 内容分区。类别名直接用接口给的分区名加个「区」字，比如「游戏区」「动画区」，
    # 所以有什么类别完全由你的关注实际情况决定，不是我写死的一张清单。
    for f in follows:
        if f.area:
            groups.setdefault(f"{f.area}区", []).append(f)
        if f.area and f.sub_area:
            groups.setdefault(f"{f.area}区/{f.sub_area}", []).append(f)
        for w in f.hits:
            groups.setdefault(f"内容:{w}", []).append(f)

    # 二创散在鬼畜、MAD·AMV、影视剪辑等几个二级分区里，单独归一类
    groups["二创"] = [f for f in follows if f.recreation]
    groups["分区未知"] = [f for f in follows if f.checked and not f.area]
    groups["全部"] = list(follows)
    return groups


def print_report(follows: list[Follow], groups: dict[str, list[Follow]],
                 cache: FollowCache) -> None:
    checked = sum(1 for f in follows if f.checked)
    print("\n" + "=" * 62)
    print(f"关注总数 {len(follows)}   列表抓取于 {cache.fetched_at}")
    print(f"深度检查进度 {checked}/{len(follows)}"
          + ("（用 --deep 继续查注销和僵尸号）" if checked < len(follows) else ""))
    print("=" * 62)

    print("\n分类统计（一个人可能属于多个类别）：\n")
    for name in ("抽奖", "未分组", "互关", "单向关注", "特别关注",
                 "机构号", "个人认证", "无认证",
                 "关注超一年", "最近30天关注", "已注销", "长期不更新"):
        members = groups.get(name) or []
        note = ""
        if name in ("已注销", "长期不更新") and checked < len(follows):
            note = f"  ← 只统计了已查过的 {checked} 个"
        print(f"  {name:12s} {len(members):5d} 人{note}")

    # 内容分区分布。按人数排，只列有人的
    areas = sorted(((k, v) for k, v in groups.items()
                    if k.endswith("区") and "/" not in k),
                   key=lambda kv: -len(kv[1]))
    if areas:
        print("\n内容分区（只统计已深度检查过的）：\n")
        for name, members in areas:
            subs = sorted(((k.split("/", 1)[1], len(v)) for k, v in groups.items()
                           if k.startswith(name + "/")), key=lambda kv: -kv[1])
            detail = "  ".join(f"{n}({c})" for n, c in subs[:5])
            print(f"  {name:10s} {len(members):5d} 人   {detail}")
        if groups.get("二创"):
            print(f"  {'二创':10s} {len(groups['二创']):5d} 人   "
                  f"（鬼畜/MAD·AMV/影视剪辑等二级分区归并）")
        if groups.get("分区未知"):
            print(f"  {'分区未知':10s} {len(groups['分区未知']):5d} 人   "
                  f"（查过但没有投稿，或分区取不到）")

        content = sorted(((k[3:], len(v)) for k, v in groups.items()
                          if k.startswith("内容:")), key=lambda kv: -kv[1])
        if content:
            print("\n具体在做什么（从最近投稿标题里认的，仅供参考）：\n")
            line = "  ".join(f"{n}({c})" for n, c in content[:14])
            print(f"  {line}")

    for name in ("已注销", "长期不更新", "抽奖"):
        members = groups.get(name) or []
        if not members:
            continue
        print(f"\n【{name}】前几个：")
        for f in members[:5]:
            extra = ""
            if name == "长期不更新" and f.last_active:
                extra = f"  最后动态 {time.strftime('%Y-%m-%d', time.localtime(f.last_active))}"
            if f.area:
                extra += f"  [{f.area}区" + (f"/{f.sub_area}" if f.sub_area else "") + "]"
            print(f"    {f.uname or '(取不到名字)'}  {f.url}{extra}")
        if len(members) > 5:
            print(f"    … 还有 {len(members) - 5} 个")

    print("\n" + "=" * 62)
    print("想取关某一类：python follow.py --unfollow 类别名")
    print("=" * 62)


# ============================================================
# 5. 取关 + 命令行入口
# ============================================================

def unfollow(bili: Bili, mid: int) -> None:
    """取关。act=2 就是取消关注。"""
    bili.post("https://api.bilibili.com/x/relation/modify",
              {"fid": mid, "act": 2, "re_src": 11})


def do_unfollow(bili: Bili, cache: FollowCache, name: str, members: list[Follow],
                limit: int, dry_run: bool, auto_yes: bool) -> None:
    if not members:
        print(f"「{name}」这一类是空的，没什么可取关的。")
        return

    picked = members[:limit] if limit else members
    print(f"\n准备取关「{name}」里的 {len(picked)} 个"
          + (f"（这一类共 {len(members)} 个，本次只处理前 {limit} 个）" if limit and
             len(members) > limit else "") + "：\n")
    for f in picked[:15]:
        print(f"    {f.uname or '(取不到名字)'}  {f.url}")
    if len(picked) > 15:
        print(f"    … 还有 {len(picked) - 15} 个")

    mutual = [f for f in picked if f.attribute == 6]
    if mutual and name != "互关":
        print(f"\n  ⚠ 其中 {len(mutual)} 个是互关的，取关会断掉互关关系。")

    if dry_run:
        print("\n【演练】上面这些都没动。去掉 --dry-run 才会真取关。")
        return

    if not auto_yes:
        print("\n  ⚠ 取关不可逆：重新关注会丢掉原来的关注时间，分组也要重新分。")
        if input("确认取关？(输 yes 回车继续，其他任意键取消) ").strip().lower() != "yes":
            print("已取消。")
            return

    # 撞风控不硬停——歇一会儿接着取同一个人。几百个一路取下来必然被挡几次，
    # 一挡就退出的话每次都得人工重跑，几百个永远清不完。
    ok, failed, naps = 0, 0, 0
    i = 0
    while i < len(picked):
        f = picked[i]
        try:
            unfollow(bili, f.mid)
        except ApiError as exc:
            cache.save()
            if exc.is_auth:
                print(f"\n  Cookie 失效，停下了：{exc}", flush=True)
                break
            if exc.is_risk:
                naps += 1
                if naps > RISK_MAX_RETRY:
                    print(f"\n歇了 {RISK_MAX_RETRY} 次还是被挡，今天先到这"
                          f"（已取关 {ok} 个，跑同样的命令会从剩下的接着取）：{exc}",
                          flush=True)
                    break
                wait = RISK_COOLDOWN * naps
                print(f"\n撞到风控（{exc}），歇 {wait // 60} 分钟再接着取 —— "
                      f"已取关 {ok}/{len(picked)}，第 {naps}/{RISK_MAX_RETRY} 次",
                      flush=True)
                time.sleep(wait)
                continue            # 同一个人重来，别漏掉
            # 别的错（这人已经不在关注列表里了之类）跳过就行，不用歇
            failed += 1
            i += 1
            print(f"  [{i}/{len(picked)}] 失败 {f.uname or f.mid}: {exc}", flush=True)
            continue
        naps = 0
        cache.drop(f.mid)
        ok += 1
        i += 1
        print(f"  [{i}/{len(picked)}] 已取关 {f.uname or f.mid}", flush=True)
        if ok % 10 == 0:
            cache.save()
        time.sleep(random.uniform(UNFOLLOW_MIN_DELAY, UNFOLLOW_MAX_DELAY))

    cache.save()
    print(f"\n取关完成：成功 {ok} 个，失败 {failed} 个。")


def main() -> int:
    parser = argparse.ArgumentParser(description="B 站关注管理：分类 + 整组取关")
    parser.add_argument("--refresh", action="store_true", help="重新抓关注列表（默认用缓存）")
    parser.add_argument("--deep", type=int, metavar="N", default=0,
                        help="深度检查 N 个还没查过的（找注销号和僵尸号）")
    parser.add_argument("--unfollow", metavar="类别", help="取关某一类，类别名见分类报告")
    parser.add_argument("--limit", type=int, default=0, help="本次最多取关几个，0=不限")
    parser.add_argument("--dry-run", action="store_true", help="只看会取关谁，不动手")
    parser.add_argument("-y", "--yes", action="store_true", help="跳过确认")
    parser.add_argument("-v", "--verbose", action="store_true", help="打印详细日志")
    parser.add_argument("-c", "--config", default=str(ROOT / "config.json"))
    parser.add_argument("--cache", default=str(ROOT / "follows.json"))
    args = parser.parse_args()

    fix_console_encoding()
    setup_logging(args.verbose)
    cfg = load_config(args.config)
    cache = FollowCache(args.cache)
    rec = Record(ROOT / "record.json")
    bili = Bili(cfg["cookie"], cfg["min_delay"], cfg["max_delay"])

    try:
        bili.ensure_buvid()
        print(f"当前账号：{bili.login_check()} (uid={bili.mid})")

        follows = cache.load()
        if args.refresh or not follows:
            if not follows:
                print("还没有关注列表缓存，先抓一遍（4000+ 个关注大约要一两分钟）…")
            tag_names = fetch_tags(bili)
            follows = fetch_all(bili)
            cache.save_follows(follows, tag_names)
            follows = cache.load()

        if args.deep:
            words = CONTENT_WORDS + list(cfg.get("content_words") or [])
            deep_check(bili, follows, cache, args.deep, words)
            follows = cache.load()

        groups = build_groups(follows, rec, cache)

        if args.unfollow:
            members = groups.get(args.unfollow)
            if members is None:
                print(f"\n没有「{args.unfollow}」这个类别。可用的类别：")
                print("  " + "  ".join(groups))
                return 2
            do_unfollow(bili, cache, args.unfollow, members,
                        args.limit, args.dry_run, args.yes)
        else:
            print_report(follows, groups, cache)
        return 0

    except ApiError as exc:
        log.error("接口报错，已停下：%s", exc)
        return 1
    except KeyboardInterrupt:
        print("\n已中断，进度已存盘。")
        return 130
    finally:
        cache.save()


if __name__ == "__main__":
    sys.exit(main())
