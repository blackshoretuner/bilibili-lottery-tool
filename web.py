#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""B 站工具合集 · 本地网页版

    python web.py            启动后自动打开浏览器
    python web.py --port 8800 --no-open

比命令行强的地方是能**逐条勾选**：扫出来的抽奖可以只挑想参与的，
关注可以只挑想取关的，不用整类一刀切。

只用 Python 标准库起服务，不装 Flask 之类的东西，所以依赖还是只有 requests。
所有实际动作都复用 main.py / follow.py，这里只负责界面和调度。

从上往下分成 5 块，想改哪块直接搜标题。
"""

from __future__ import annotations

import argparse
import json
import logging
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import follow as F
import main as M

log = logging.getLogger("web")


# ============================================================
# 1. 后台任务：一次只跑一个
# ============================================================

class Job:
    """后台跑着的那个任务。

    同一时间只允许一个——几个任务并发对 B 站发写请求，等于自己给自己刷风控。
    """

    def __init__(self):
        self.lock = threading.Lock()
        self.name = ""
        self.status = "idle"        # idle / running / done / error
        self.logs: list[str] = []
        self.result: dict = {}
        self.started = 0.0

    @property
    def running(self) -> bool:
        return self.status == "running"

    def start(self, name: str, fn) -> bool:
        """启动任务，已经有任务在跑就返回 False。"""
        with self.lock:
            if self.running:
                return False
            self.name, self.status = name, "running"
            self.logs, self.result, self.started = [], {}, time.time()

        def wrapper():
            handler = _JobLogHandler(self)
            root = logging.getLogger()
            root.addHandler(handler)
            try:
                self.result = fn(self) or {}
                self.status = "done"
            except Exception as exc:                       # noqa: BLE001
                self.say(f"× 出错了：{exc}")
                self.result = {"error": str(exc)}
                self.status = "error"
                log.exception("任务 %s 失败", name)
            finally:
                root.removeHandler(handler)

        threading.Thread(target=wrapper, daemon=True).start()
        return True

    def say(self, line: str) -> None:
        self.logs.append(line)
        del self.logs[:-400]        # 只留最近 400 行，别让内存无限涨

    def snapshot(self) -> dict:
        return {"name": self.name, "status": self.status, "logs": self.logs[-120:],
                "result": self.result, "elapsed": int(time.time() - self.started)}


class _JobLogHandler(logging.Handler):
    """把脚本里 log.info 那些话也收进任务日志，界面上能看到进度。"""

    def __init__(self, job: Job):
        super().__init__(logging.INFO)
        self.job = job

    def emit(self, record):
        try:
            self.job.say(record.getMessage())
        except Exception:                                   # noqa: BLE001
            pass


# ============================================================
# 2. 会话：客户端、配置、缓存
# ============================================================

class Session:
    def __init__(self, config: str, cache: str, record: str):
        self.cfg = M.load_config(config)
        self.cache_path, self.record_path = cache, record
        self.bili = M.Bili(self.cfg["cookie"], self.cfg["min_delay"], self.cfg["max_delay"])
        self.bili.ensure_buvid()
        self.uname = self.bili.login_check()
        # 上一次扫出来的抽奖，勾选参与时要按 id 找回来
        self.targets: dict[str, M.Target] = {}

    @property
    def record(self) -> M.Record:
        return M.Record(self.record_path)

    @property
    def cache(self) -> F.FollowCache:
        return F.FollowCache(self.cache_path)

    # ---- 抽奖 ----

    def scan_lotteries(self, job: Job) -> dict:
        job.say("开始扫描抽奖…")
        rec = self.record
        todo, summary = M.scan(self.bili, self.cfg, rec)
        rec.save()
        self.targets = {t.dyn_id: t for t in todo}
        job.say(f"扫完了：{len(todo)} 个可参与")
        return {"summary": {"scanned": summary.scanned, "lotteries": summary.lotteries,
                            "skip_done": summary.skip_done, "skip_drawn": summary.skip_drawn,
                            "skip_old": summary.skip_old, "skip_self": summary.skip_self},
                "items": [_target_json(t) for t in todo]}

    def join_lotteries(self, job: Job, dyn_ids: list[str]) -> dict:
        picked = [self.targets[i] for i in dyn_ids if i in self.targets]
        if not picked:
            job.say("没有可参与的（可能扫描结果过期了，重新扫一次）")
            return {"joined": []}

        job.say(f"开始参与 {len(picked)} 个。每个动作之间要随机等 "
                f"{self.cfg['min_delay']}~{self.cfg['max_delay']} 秒，慢是正常的。")
        rec = self.record
        summary = M.Summary()
        results = M.join_all(self.bili, self.cfg, rec, picked, summary)
        rec.save()
        for dyn_id in dyn_ids:
            self.targets.pop(dyn_id, None)
        return {"stopped": summary.stopped,
                "joined": [{"url": r.target.url, "uname": r.target.uname,
                            "ok": r.ok, "followed": r.followed, "reposted": r.reposted,
                            "commented": r.commented, "rpid": r.rpid,
                            "comment_check": r.comment_check, "error": r.error}
                           for r in results]}

    # ---- 关注 ----

    def fetch_follows(self, job: Job) -> dict:
        job.say("开始抓关注列表，几千个关注要两三分钟…")
        tag_names = F.fetch_tags(self.bili)
        follows = F.fetch_all(self.bili)
        cache = self.cache
        cache.save_follows(follows, tag_names)
        return {"count": len(follows)}

    def deep_check(self, job: Job, n: int) -> dict:
        cache = self.cache
        follows = cache.load()
        if not follows:
            job.say("还没有关注列表，先点「抓取关注列表」")
            return {"checked": 0}
        words = F.CONTENT_WORDS + list(self.cfg.get("content_words") or [])
        job.say(f"开始深度检查，最多 {n} 个…")
        done = F.deep_check(self.bili, follows, cache, n, words)
        return {"checked": done}

    def follow_report(self) -> dict:
        cache = self.cache
        follows = cache.load()
        groups = F.build_groups(follows, self.record, cache)
        checked = sum(1 for f in follows if f.checked)
        rows = sorted(((name, len(members)) for name, members in groups.items() if members),
                      key=lambda kv: -kv[1])
        return {"total": len(follows), "checked": checked,
                "fetched_at": cache.fetched_at,
                "groups": [{"name": n, "count": c} for n, c in rows]}

    def group_members(self, name: str) -> dict:
        cache = self.cache
        follows = cache.load()
        groups = F.build_groups(follows, self.record, cache)
        members = groups.get(name) or []
        return {"name": name, "count": len(members),
                "items": [_follow_json(f) for f in members[:600]]}

    def unfollow(self, job: Job, mids: list[int]) -> dict:
        cache = self.cache
        job.say(f"开始取关 {len(mids)} 个，每个之间要等一会儿…")
        ok, failed = 0, []
        for i, mid in enumerate(mids, 1):
            try:
                self.bili.pause()
                F.unfollow(self.bili, mid)
                cache.drop(mid)
                ok += 1
                job.say(f"[{i}/{len(mids)}] 已取关 {mid}")
            except M.ApiError as exc:
                failed.append({"mid": mid, "error": str(exc)})
                job.say(f"[{i}/{len(mids)}] 失败 {mid}: {exc}")
                if exc.is_auth or exc.is_risk:
                    job.say("触发风控或 Cookie 失效，停下了。")
                    break
            if i % 10 == 0:
                cache.save()
        cache.save()
        return {"ok": ok, "failed": failed}


def _target_json(t: M.Target) -> dict:
    return {"dyn_id": t.dyn_id, "url": t.url, "uname": t.uname or str(t.uid),
            "reason": t.reason, "pub_at": t.pub_at, "draw_at": t.draw_at,
            "brief": t.brief, "source": t.source}


def _follow_json(f: F.Follow) -> dict:
    area = (f"{f.area}区" + (f"/{f.sub_area}" if f.sub_area else "")) if f.area else ""
    return {"mid": f.mid, "uname": f.uname or f"(uid {f.mid})", "url": f.url,
            "area": area, "hits": f.hits,
            "mutual": f.attribute == 6,
            "alive": f.alive,
            "follow_days": int(f.follow_days),
            "silent_days": int(f.silent_days) if f.last_active else None}


# ============================================================
# 3. 页面
# ============================================================

PAGE = r"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>B 站工具合集</title>
<style>
:root{--bg:#f6f7f9;--card:#fff;--line:#e3e6ea;--ink:#1f2328;--dim:#6b7280;
      --pink:#fb7299;--ok:#1a7f37;--bad:#c1121f}
*{box-sizing:border-box}
body{margin:0;font:14px/1.6 -apple-system,"Segoe UI","Microsoft YaHei",sans-serif;
     background:var(--bg);color:var(--ink)}
header{background:var(--card);border-bottom:1px solid var(--line);padding:12px 20px;
       display:flex;align-items:center;gap:14px;position:sticky;top:0;z-index:5}
header b{font-size:16px}
header .who{color:var(--dim);font-size:13px}
nav{display:flex;gap:6px;margin-left:auto}
nav button{border:1px solid var(--line);background:var(--card);padding:6px 14px;
           border-radius:6px;cursor:pointer;font-size:13px}
nav button.on{background:var(--pink);border-color:var(--pink);color:#fff}
main{padding:20px;max-width:1180px;margin:0 auto}
.card{background:var(--card);border:1px solid var(--line);border-radius:10px;
      padding:16px 18px;margin-bottom:16px}
.card h2{margin:0 0 4px;font-size:15px}
.card p.hint{margin:0 0 12px;color:var(--dim);font-size:13px}
button.act{background:var(--pink);color:#fff;border:0;padding:8px 16px;border-radius:6px;
           cursor:pointer;font-size:13px}
button.act[disabled]{opacity:.45;cursor:not-allowed}
button.ghost{background:var(--card);color:var(--ink);border:1px solid var(--line)}
button.danger{background:var(--bad)}
input[type=number],input[type=text]{border:1px solid var(--line);border-radius:6px;
      padding:7px 9px;font:inherit;width:110px}
table{width:100%;border-collapse:collapse;font-size:13px}
th,td{text-align:left;padding:8px 6px;border-bottom:1px solid var(--line);vertical-align:top}
th{color:var(--dim);font-weight:600;font-size:12px}
td.brief{color:var(--dim)}
.tag{display:inline-block;background:#f1f3f5;border-radius:4px;padding:1px 6px;
     font-size:12px;margin-right:4px}
.tag.pink{background:#ffeef3;color:#b4325c}
.stat{display:flex;gap:18px;flex-wrap:wrap;margin-bottom:12px;font-size:13px;color:var(--dim)}
.stat b{color:var(--ink)}
.chips{display:flex;flex-wrap:wrap;gap:8px}
.chip{border:1px solid var(--line);background:var(--card);border-radius:20px;
      padding:5px 13px;cursor:pointer;font-size:13px}
.chip:hover{border-color:var(--pink)}
.chip.on{background:var(--pink);border-color:var(--pink);color:#fff}
.chip i{color:var(--dim);font-style:normal;margin-left:4px}
.chip.on i{color:#ffe0eb}
pre.logs{background:#1f2328;color:#d1d5da;padding:12px;border-radius:8px;font-size:12px;
     max-height:260px;overflow:auto;white-space:pre-wrap;margin:12px 0 0}
.bar{display:flex;gap:10px;align-items:center;flex-wrap:wrap}
.right{margin-left:auto}
.warn{background:#fff8e1;border:1px solid #ffe08a;border-radius:8px;padding:10px 12px;
      font-size:13px;margin-bottom:12px}
.ok{color:var(--ok)}.bad{color:var(--bad)}
.hide{display:none}
</style></head><body>
<header>
  <b>B 站工具合集</b><span class="who" id="who">连接中…</span>
  <nav>
    <button data-tab="lottery" class="on">参与抽奖</button>
    <button data-tab="follow">管理关注</button>
  </nav>
</header>
<main>

<section id="tab-lottery">
  <div class="card">
    <h2>1. 扫描抽奖</h2>
    <p class="hint">从抽奖话题、指定 UP、你的关注流里找转发抽奖。只是看，不会动手。</p>
    <div class="bar">
      <button class="act" id="btn-scan">开始扫描</button>
      <span id="scan-stat" class="hint"></span>
    </div>
  </div>
  <div class="card hide" id="card-lot">
    <h2>2. 挑要参与的</h2>
    <p class="hint">判定是靠正文关键词猜的，<b>请先看一眼「正文摘要」确认真是抽奖</b>，再勾选。</p>
    <div class="bar" style="margin-bottom:10px">
      <button class="act ghost" id="lot-all">全选</button>
      <button class="act ghost" id="lot-none">全不选</button>
      <span class="right"><button class="act" id="btn-join">参与勾选的（<span id="lot-n">0</span>）</button></span>
    </div>
    <table id="lot-table"></table>
  </div>
</section>

<section id="tab-follow" class="hide">
  <div class="card">
    <h2>1. 关注列表</h2>
    <div class="stat" id="fl-stat"></div>
    <div class="bar">
      <button class="act" id="btn-fetch">抓取 / 刷新关注列表</button>
      <button class="act ghost" id="btn-report">刷新分类</button>
      <span style="margin-left:12px"></span>
      深度检查
      <input type="number" id="deep-n" value="200" min="1" max="4000">
      <button class="act ghost" id="btn-deep">开始</button>
      <span class="hint">找注销号、僵尸号，顺便认出内容分区。查过的会记住。</span>
    </div>
  </div>
  <div class="card">
    <h2>2. 选一个类别</h2>
    <p class="hint">类别是按你关注的实际情况生成的。内容分区要先做过深度检查才会出现。</p>
    <div class="chips" id="fl-chips"></div>
  </div>
  <div class="card hide" id="card-fl">
    <h2>3. 挑要取关的 —— <span id="fl-name"></span></h2>
    <div class="warn">取关不可逆：重新关注会丢掉原来的关注时间，分组也要重新分。</div>
    <div class="bar" style="margin-bottom:10px">
      <button class="act ghost" id="fl-all">全选</button>
      <button class="act ghost" id="fl-none">全不选</button>
      <span class="right"><button class="act danger" id="btn-unfollow">取关勾选的（<span id="fl-n">0</span>）</button></span>
    </div>
    <table id="fl-table"></table>
  </div>
</section>

<div class="card" id="card-job">
  <h2 id="job-name">运行日志</h2>
  <pre class="logs" id="job-logs">（空闲）</pre>
</div>
</main>
<script>
const $ = s => document.querySelector(s);
const api = async (path, body) => {
  const r = await fetch(path, body ? {method:'POST', headers:{'Content-Type':'application/json'},
                                      body: JSON.stringify(body)} : {});
  const j = await r.json();
  if (j.error) alert('出错了：' + j.error);
  return j;
};
let busy = false, lots = [], flItems = [], curGroup = '';

// ---- 选项卡 ----
document.querySelectorAll('nav button').forEach(b => b.onclick = () => {
  document.querySelectorAll('nav button').forEach(x => x.classList.toggle('on', x === b));
  $('#tab-lottery').classList.toggle('hide', b.dataset.tab !== 'lottery');
  $('#tab-follow').classList.toggle('hide', b.dataset.tab !== 'follow');
  if (b.dataset.tab === 'follow') loadReport();
});

// ---- 任务轮询 ----
async function poll() {
  const s = await api('/api/job');
  busy = s.status === 'running';
  document.querySelectorAll('button.act').forEach(b => b.disabled = busy);
  $('#job-name').textContent = s.name ? `运行日志 · ${s.name}` +
      (busy ? `（已跑 ${s.elapsed} 秒）` : '（完成）') : '运行日志';
  $('#job-logs').textContent = (s.logs || []).join('\n') || '（空闲）';
  $('#job-logs').scrollTop = 1e9;
  if (!busy && s.done_token && s.done_token !== window._tok) {
    window._tok = s.done_token;
    if (s.finished === 'scan') renderLots(s.result);
    if (s.finished === 'join') { alert(joinMsg(s.result)); $('#btn-scan').click(); }
    if (s.finished === 'fetch' || s.finished === 'deep') loadReport();
    if (s.finished === 'unfollow') { alert(`取关成功 ${s.result.ok} 个，失败 ${(s.result.failed||[]).length} 个`);
                                     loadReport(); $('#card-fl').classList.add('hide'); }
  }
  setTimeout(poll, busy ? 1000 : 2500);
}
function joinMsg(r) {
  const ok = (r.joined||[]).filter(x => x.ok).length;
  const lines = (r.joined||[]).map(x =>
    `${x.ok ? '✓' : '✗'} ${x.uname}  关${x.followed?'√':'×'} 转${x.reposted?'√':'×'} 评${x.commented?'√':'×'}`
    + (x.comment_check ? `\n    评论验证：${x.comment_check}` : '')
    + (x.error ? `\n    ${x.error}` : ''));
  return `参与完成：成功 ${ok} / ${(r.joined||[]).length}\n\n` + lines.join('\n')
       + (r.stopped ? `\n\n提前结束：${r.stopped}` : '');
}

// ---- 抽奖 ----
$('#btn-scan').onclick = () => api('/api/lottery/scan', {});
function renderLots(res) {
  lots = res.items || [];
  const s = res.summary || {};
  $('#scan-stat').textContent =
    `扫了 ${s.scanned} 条动态，判定为抽奖 ${s.lotteries} 个；跳过：参与过 ${s.skip_done}、`
    + `已开奖 ${s.skip_drawn}、太老 ${s.skip_old}、自己发的 ${s.skip_self}`;
  $('#card-lot').classList.toggle('hide', !lots.length);
  $('#lot-table').innerHTML =
    '<tr><th style="width:28px"></th><th>UP</th><th>命中</th><th>时间</th><th>正文摘要</th></tr>'
    + lots.map((t, i) => `<tr>
        <td><input type="checkbox" class="lot" data-i="${i}" checked></td>
        <td><a href="${t.url}" target="_blank">${esc(t.uname)}</a></td>
        <td><span class="tag pink">${esc(t.reason)}</span></td>
        <td>${esc(t.pub_at)}发布<br>${esc(t.draw_at)}</td>
        <td class="brief">${esc(t.brief)}</td></tr>`).join('');
  bindCount('.lot', '#lot-n');
}
$('#lot-all').onclick = () => toggleAll('.lot', true, '#lot-n');
$('#lot-none').onclick = () => toggleAll('.lot', false, '#lot-n');
$('#btn-join').onclick = () => {
  const ids = picked('.lot').map(i => lots[i].dyn_id);
  if (!ids.length) return alert('一个都没勾');
  if (!confirm(`确认参与 ${ids.length} 个抽奖？会真的关注 + 转发 + 评论。\n每个动作间隔 20~45 秒，预计 ${Math.ceil(ids.length*1.7)} 分钟。`)) return;
  api('/api/lottery/join', {dyn_ids: ids});
};

// ---- 关注 ----
$('#btn-fetch').onclick = () => { if (confirm('重新抓一遍关注列表？几千个关注要两三分钟。')) api('/api/follow/fetch', {}); };
$('#btn-report').onclick = loadReport;
$('#btn-deep').onclick = () => api('/api/follow/deep', {n: +$('#deep-n').value || 200});
async function loadReport() {
  const r = await api('/api/follow/report');
  $('#fl-stat').innerHTML = `关注总数 <b>${r.total}</b>　深度检查 <b>${r.checked}</b>/${r.total}`
      + `　列表抓取于 <b>${esc(r.fetched_at)}</b>`;
  $('#fl-chips').innerHTML = (r.groups||[]).map(g =>
      `<div class="chip${g.name===curGroup?' on':''}" data-n="${esc(g.name)}">${esc(g.name)}<i>${g.count}</i></div>`).join('')
      || '<span class="hint">还没有关注列表，先点上面「抓取 / 刷新关注列表」。</span>';
  document.querySelectorAll('#fl-chips .chip').forEach(c => c.onclick = () => openGroup(c.dataset.n));
}
async function openGroup(name) {
  curGroup = name;
  const r = await api('/api/follow/group?name=' + encodeURIComponent(name));
  flItems = r.items || [];
  $('#fl-name').textContent = `${name}（${r.count} 人${r.count > flItems.length ? '，只列出前 ' + flItems.length : ''}）`;
  $('#card-fl').classList.remove('hide');
  $('#fl-table').innerHTML =
    '<tr><th style="width:28px"></th><th>UP</th><th>内容分区</th><th>状态</th><th>关注时长</th></tr>'
    + flItems.map((f, i) => `<tr>
        <td><input type="checkbox" class="fl" data-i="${i}"></td>
        <td><a href="${f.url}" target="_blank">${esc(f.uname)}</a>
            ${f.mutual ? '<span class="tag">互关</span>' : ''}</td>
        <td>${f.area ? `<span class="tag">${esc(f.area)}</span>` : '<span class="hint">—</span>'}
            ${(f.hits||[]).map(h => `<span class="tag pink">${esc(h)}</span>`).join('')}</td>
        <td>${f.alive === false ? '<span class="bad">已注销/封禁</span>'
             : f.silent_days === null ? '<span class="hint">未检查</span>'
             : f.silent_days > 365 ? `<span class="bad">${f.silent_days} 天没动态</span>`
             : `<span class="ok">活跃</span>`}</td>
        <td>${f.follow_days} 天</td></tr>`).join('');
  document.querySelectorAll('#fl-chips .chip').forEach(c => c.classList.toggle('on', c.dataset.n === name));
  bindCount('.fl', '#fl-n');
}
$('#fl-all').onclick = () => toggleAll('.fl', true, '#fl-n');
$('#fl-none').onclick = () => toggleAll('.fl', false, '#fl-n');
$('#btn-unfollow').onclick = () => {
  const mids = picked('.fl').map(i => flItems[i].mid);
  if (!mids.length) return alert('一个都没勾');
  const mutual = picked('.fl').filter(i => flItems[i].mutual).length;
  let msg = `确认取关 ${mids.length} 个？这一步不可逆。`;
  if (mutual) msg += `\n\n其中 ${mutual} 个是互关的，取关会断掉互关关系。`;
  if (!confirm(msg)) return;
  api('/api/follow/unfollow', {mids});
};

// ---- 小工具 ----
function esc(s){return String(s??'').replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]))}
function picked(sel){return [...document.querySelectorAll(sel)].filter(c=>c.checked).map(c=>+c.dataset.i)}
function bindCount(sel,out){const f=()=>$(out).textContent=picked(sel).length;
  document.querySelectorAll(sel).forEach(c=>c.onchange=f);f()}
function toggleAll(sel,v,out){document.querySelectorAll(sel).forEach(c=>c.checked=v);
  $(out).textContent=picked(sel).length}

api('/api/state').then(s => $('#who').textContent = s.uname ? `${s.uname}（uid ${s.mid}）` : '未登录');
poll();
</script></body></html>
"""


# ============================================================
# 4. 路由
# ============================================================

class Handler(BaseHTTPRequestHandler):
    session: Session
    job: Job
    finished: str = ""
    done_token: int = 0

    def log_message(self, fmt, *args):
        log.debug(fmt, *args)           # 别让每个请求都刷屏

    # ---- basic ----

    def _send(self, code: int, body: bytes, ctype: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj) -> None:
        self._send(200, json.dumps(obj, ensure_ascii=False).encode("utf-8"),
                   "application/json; charset=utf-8")

    def _launch(self, name: str, tag: str, fn) -> None:
        if not self.job.start(name, fn):
            return self._json({"error": "还有任务在跑，等它跑完再来"})
        # 记下这次跑的是哪种任务，前端拿到结果后才知道该怎么渲染
        type(self).finished = tag
        self._json({"ok": True})

    def _body(self) -> dict:
        n = int(self.headers.get("Content-Length") or 0)
        if not n:
            return {}
        try:
            return json.loads(self.rfile.read(n) or b"{}")
        except ValueError:
            return {}

    # ---- GET ----

    def do_GET(self):                                       # noqa: N802
        path = urlparse(self.path).path
        query = parse_qs(urlparse(self.path).query)
        s, job = self.session, self.job

        if path == "/":
            return self._send(200, PAGE.encode("utf-8"), "text/html; charset=utf-8")
        if path == "/api/state":
            return self._json({"uname": s.uname, "mid": s.bili.mid})
        if path == "/api/job":
            snap = job.snapshot()
            snap["finished"] = type(self).finished
            # 任务一跑完就换个 token，前端靠它判断「这次结果我还没处理过」
            snap["done_token"] = 0 if job.running else int(job.started)
            return self._json(snap)
        if path == "/api/follow/report":
            return self._json(s.follow_report())
        if path == "/api/follow/group":
            return self._json(s.group_members((query.get("name") or [""])[0]))
        return self._send(404, b"not found", "text/plain")

    # ---- POST ----

    def do_POST(self):                                      # noqa: N802
        path = urlparse(self.path).path
        body = self._body()
        s = self.session

        if path == "/api/lottery/scan":
            return self._launch("扫描抽奖", "scan", s.scan_lotteries)
        if path == "/api/lottery/join":
            ids = [str(i) for i in (body.get("dyn_ids") or [])]
            return self._launch("参与抽奖", "join", lambda j: s.join_lotteries(j, ids))
        if path == "/api/follow/fetch":
            return self._launch("抓取关注列表", "fetch", s.fetch_follows)
        if path == "/api/follow/deep":
            n = max(1, min(4000, int(body.get("n") or 200)))
            return self._launch("深度检查", "deep", lambda j: s.deep_check(j, n))
        if path == "/api/follow/unfollow":
            mids = [int(m) for m in (body.get("mids") or [])]
            return self._launch("批量取关", "unfollow", lambda j: s.unfollow(j, mids))
        return self._send(404, b"not found", "text/plain")


# ============================================================
# 5. 入口
# ============================================================

def main() -> int:
    parser = argparse.ArgumentParser(description="B 站工具合集 · 本地网页版")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--no-open", action="store_true", help="不自动打开浏览器")
    parser.add_argument("-v", "--verbose", action="store_true")
    parser.add_argument("-c", "--config", default=str(M.ROOT / "config.json"))
    parser.add_argument("--cache", default=str(M.ROOT / "follows.json"))
    parser.add_argument("--record", default=str(M.ROOT / "record.json"))
    args = parser.parse_args()

    M.fix_console_encoding()
    M.setup_logging(args.verbose)

    try:
        session = Session(args.config, args.cache, args.record)
    except M.ApiError as exc:
        print(f"× 连不上 B 站或 Cookie 失效：{exc}")
        return 1
    print(f"当前账号：{session.uname} (uid={session.bili.mid})")

    Handler.session, Handler.job = session, Job()
    url = f"http://127.0.0.1:{args.port}/"
    # 只监听本机：这个界面能操作你的账号，绝对不要暴露到局域网外
    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    print(f"界面已启动：{url}\n按 Ctrl+C 停止。")
    if not args.no_open:
        threading.Timer(0.5, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
