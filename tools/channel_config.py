#!/usr/bin/env python3
"""Local web page for choosing which channels the device offers.

Reads a live-stream playlist, shows every channel it finds, and writes the ones
you tick, in the order you arrange them, to channels.txt. The media server reads
that file at startup, so nothing needs recompiling or reflashing.

    python3 tools/channel_config.py            # then open http://127.0.0.1:8097
    python3 tools/channel_config.py --port 8097 --bind 0.0.0.0

Standard library only, matching the rest of the server. It listens on a private
address and is meant for the same trusted LAN as the media server: there is no
authentication and no TLS, so do not expose it to the internet.
"""

from __future__ import annotations

import argparse
import errno
import html
import ipaddress
import json
import re
import sys
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

# This page writes the channel list, so where that file goes has to be the data
# directory and not anything derived from this file's own location. The
# difference only shows up in a bundled build, where `__file__` names a temporary
# directory that the runtime deletes on exit -- saving there would look like it
# worked and lose the edit. The bootstrap below is the same one tools/launch.py
# needs: run as a script, this file's directory is on the search path rather than
# the repository root.
_BOOTSTRAP_ROOT = Path(__file__).resolve().parents[1]
if str(_BOOTSTRAP_ROOT) not in sys.path:
    sys.path.insert(0, str(_BOOTSTRAP_ROOT))

from tools import datadir  # noqa: E402  (resolves only after the path above)
from tools.certs import use_system_ca  # noqa: E402
from tools.console import use_utf8  # noqa: E402

# This page's messages are Chinese too, and on Windows the console's default
# encoding cannot represent them. See tools/console.py.
use_utf8()

# This page is also the one that loads a playlist over HTTPS, so it is the one
# where a missing certificate bundle shows up first -- as "无法获取" for an
# address that works in every browser. See tools/certs.py.
use_system_ca()

# Resolved on each use rather than once at import. It used to be a module-level
# constant, which fixes the path at whatever moment this module happens to be
# imported -- and in the bundled build that is before the program has decided
# where its data goes. Reading it late costs one directory check and removes the
# dependence on import order entirely.
def channels_file() -> Path:
    """The channel table this page reads and writes."""
    return datadir.channels_file()


DEFAULT_SOURCE = "https://live.zhoujie218.top/tv/iptv4.m3u"

# The device's real limits, mirrored from main/av_protocol.h and server/live.py.
#
# These are the numbers the page warns against, so they have to be the device's
# and not a rounder-looking guess. They were 320 and 24576 -- two and a half
# times the truth -- while a comment here claimed they mirrored the firmware. A
# page that shows the wrong ceiling does not protect anyone from it: it reports
# a selection as fine that the device cannot accept at all.
TV_CHANNEL_MAX = 128
TV_CHANNEL_ID_MAX = 16
TV_CONTROL_MAX = 7168
FOLLOW_TIMEOUT_S = 20
MAX_PLAYLIST_BYTES = 4 * 1024 * 1024
DEFAULT_USER_AGENT = "AptvPlayer-UA"
# `http-user-agent="..."` on an #EXTINF line or `#EXTVLCOPT:http-user-agent=...`.
USER_AGENT_RE = re.compile(r"""http-user-agent\s*=\s*["']([^"']*)["']""", re.IGNORECASE)
EXTVLCOPT_UA_RE = re.compile(r"""#EXTVLCOPT:http-user-agent\s*=\s*([^\r\n]*)""", re.IGNORECASE)
# Where ffmpeg lives, and the two do-not-wait-past times used when checking a
# channel.
#
# Two, not one, because "slow" and "dead" are different answers and a single
# limit has to get one of them wrong. Most channels produce a frame in well under
# a second; a handful take longer -- one in the table this was found in needed
# over fifteen. With one fifteen-second limit those were reported dead, and
# "剔成失效" then deleted channels that work. Waiting longer for everything would
# make a full check take an hour, since a genuinely dead address is dead only
# after the timeout expires.
#
# So: give up quietly at SLOW, but keep going to DEAD before saying so. Only
# DEAD is reported as a failure; SLOW is reported as slow, which is a fact the
# operator can act on and a false one they cannot.
FFMPEG = "ffmpeg"
PROBE_SLOW_S = 15
PROBE_DEAD_S = 40


def is_private(address: str) -> bool:
    try:
        return ipaddress.ip_address(address).is_private
    except ValueError:
        return address in ("localhost",)


def parse_playlist(text: str) -> list[dict]:
    """Pull (name, url) pairs out of an M3U playlist.

    Names come after the last comma of #EXTINF; the address is the next line
    that looks like a URL. Duplicate names are kept apart by the caller, which
    assigns ids, because a playlist legitimately lists one channel twice.
    """
    channels: list[dict] = []
    pending: str | None = None
    pending_agent: str = ""
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.startswith("#EXTINF"):
            _, _, tail = line.partition(",")
            name = tail.strip() or "unnamed"
            pending = name
            match = USER_AGENT_RE.search(line)
            pending_agent = match.group(1) if match else ""
            continue
        if line.startswith("#EXTVLCOPT:http-user-agent"):
            match = EXTVLCOPT_UA_RE.search(line)
            if match:
                pending_agent = match.group(1).strip()
            continue
        if line.startswith("#"):
            continue
        if line.startswith(("http://", "https://")):
            name = pending if pending else line.rsplit("/", 1)[-1]
            channels.append({"name": name, "url": line, "agent": pending_agent})
            pending = None
            pending_agent = ""
    return channels


def slug(index: int) -> str:
    """A short ASCII id. Chinese names cannot be ids: the device logs and the
    wire protocol both assume printable ASCII there."""
    return f"ch{index:03d}"


def read_selection() -> list[dict]:
    """The current channels.txt, so the page opens on what is already chosen.

    A fourth field is the User-Agent, and reading only three-field lines was not
    a harmless omission: those lines were skipped, so a channel that needs an
    agent to play -- 22 of them in the table this was found in -- did not appear
    under "chosen". Saving from the page then wrote the list back without them,
    and the addresses stayed in the file's history while leaving the file. The
    User-Agent is carried through to the page and back for the same reason: it
    is part of the channel, not a detail of how it is stored.
    """
    if not channels_file().is_file():
        return []
    selected = []
    for number, raw in enumerate(channels_file().read_text(encoding="utf-8").splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        parts = [part.strip() for part in line.split("|")]
        if len(parts) not in (3, 4):
            # A malformed line is left alone rather than guessed at. Silently
            # dropping it is how the fourth field went missing in the first
            # place; the save will not include it, and the operator can see why
            # by looking at the file.
            continue
        key, name, url = parts[0], parts[1], parts[2]
        agent = parts[3] if len(parts) == 4 else ""
        selected.append({"id": key, "name": name, "url": url,
                         "agent": agent, "line": number})
    return selected


def write_selection(entries: list[dict]) -> None:
    """Write channels.txt.

    Only what cannot be written at all is refused: an empty name, an address
    that is not a URL, or a field containing the separator, each of which would
    produce a file that reads back as something other than what was chosen.

    A list longer than the device accepts is *not* refused. The page warns about
    it prominently, and that is the whole of its job here. Turning the warning
    into a refusal would mean the page deciding what the operator is allowed to
    keep -- and they may be preparing a larger table for another device, keeping
    a channel they have not yet confirmed, or writing it by hand on the next
    line anyway. The file is a plain text file; the page is one way to edit it,
    not a gate in front of it.

    What protects the person who saves something too large is the server, which
    reads such a table without falling over and says what it did with it. See
    load_channels().
    """
    if not entries:
        raise ValueError("至少选择一个频道")
    seen = set()
    lines = [
        "# 由配置页面生成。每行： id | 显示名 | 地址 | User-Agent（可选）",
        "# 顺序就是设备上 UP/DOWN 换台的顺序。",
        "# 手动编辑同样有效，改完重启媒体服务即可。",
        "",
    ]
    for index, entry in enumerate(entries):
        name = str(entry.get("name", "")).strip()
        url = str(entry.get("url", "")).strip()
        # Written back whenever the channel has one. For a source that answers
        # only to a particular player, the address without its agent is a dead
        # channel that looks alive in every listing -- which is worse than a
        # channel that is obviously missing.
        agent = str(entry.get("agent", "")).strip()
        if not agent and url.startswith(("http://", "https://")):
            agent = DEFAULT_USER_AGENT
        if not name:
            raise ValueError(f"第 {index + 1} 项缺少频道名")
        if not url.startswith(("http://", "https://")):
            raise ValueError(f"第 {index + 1} 项地址必须以 http:// 或 https:// 开头")
        # The separator must not appear inside a field or the line cannot be read
        # back; refuse rather than write a file that parses differently.
        if "|" in name or "|" in url or "|" in agent:
            raise ValueError(f"第 {index + 1} 项含有 | 字符，无法保存")
        key = slug(index)
        if key in seen:
            raise ValueError("内部错误：频道 id 重复")
        seen.add(key)
        lines.append(f"{key} | {name} | {url}" + (f" | {agent}" if agent else ""))
    channels_file().write_text("\n".join(lines) + "\n", encoding="utf-8")


def estimate_config_bytes(names: list[str]) -> int:
    """Approximate size of the CONFIG packet the device must receive.

    It carries the whole list as JSON, so this is what decides whether the list
    fits; the per-entry cost is roughly the name plus the surrounding syntax.
    """
    payload = json.dumps(
        {"channel_list": [{"id": slug(i), "name": n} for i, n in enumerate(names)]},
        ensure_ascii=False, separators=(",", ":"))
    return len(payload.encode("utf-8"))


PAGE = """<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>频道配置</title>
<style>
  :root { color-scheme: light dark; --line: #8884; --accent: #2563eb; }
  * { box-sizing: border-box; }
  body { margin: 0; font: 15px/1.5 system-ui, -apple-system, "PingFang SC", sans-serif; }
  header { padding: 16px 20px; border-bottom: 1px solid var(--line); }
  h1 { margin: 0 0 4px; font-size: 19px; }
  .sub { opacity: .7; font-size: 13px; }
  main { padding: 20px; max-width: 1100px; }
  .row { display: flex; gap: 8px; flex-wrap: wrap; align-items: center; }
  input[type=text] { flex: 1; min-width: 280px; padding: 8px 10px;
    border: 1px solid var(--line); border-radius: 6px; font: inherit;
    background: transparent; color: inherit; }
  button { padding: 8px 14px; border: 1px solid var(--line); border-radius: 6px;
    background: transparent; color: inherit; font: inherit; cursor: pointer; }
  button:hover:not(:disabled) { border-color: var(--accent); color: var(--accent); }
  button:disabled { opacity: .4; cursor: default; }
  button.primary { background: var(--accent); border-color: var(--accent); color: #fff; }
  button.primary:hover:not(:disabled) { color: #fff; opacity: .9; }
  .panels { display: grid; grid-template-columns: 1fr 1fr; gap: 16px; margin-top: 20px; }
  /* Below this the two 320px columns plus the gap would overflow, so they stack.
     Set at the real threshold rather than a round number, so a narrow window
     still shows both lists side by side. */
  @media (max-width: 700px) { .panels { grid-template-columns: 1fr; } }
  .panel { border: 1px solid var(--line); border-radius: 8px; overflow: hidden; }
  .panel h2 { margin: 0; padding: 10px 12px; font-size: 14px;
    border-bottom: 1px solid var(--line); display: flex; justify-content: space-between; }
  .list { height: 420px; overflow-y: auto; }
  .item { display: flex; align-items: center; gap: 8px; padding: 7px 12px;
    border-bottom: 1px solid var(--line); }
  .item:hover { background: #8881; }
  .item .name { flex: 1; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .item .idx { opacity: .5; font-variant-numeric: tabular-nums; font-size: 12px;
    min-width: 2.4em; text-align: right; }
  .item button { padding: 2px 8px; font-size: 13px; }
  .status { margin-top: 16px; padding: 12px; border: 1px solid var(--line);
    border-radius: 8px; font-size: 14px; }
  .warn { color: #b45309; }
  .bad { color: #b91c1c; }
  .ok { color: #15803d; }
  .empty { padding: 20px; opacity: .6; text-align: center; }
  .item .name.dead { color: #b91c1c; text-decoration: line-through; opacity: .8; }
  /* Slow is not a fault: no strike-through, just a colour that says "this one
     costs you a wait". Styling it like a failure invites deleting a channel
     that works. */
  .item .name.slow { color: #b45309; }
  .actions { margin-top: 16px; display: flex; gap: 10px; align-items: center;
    flex-wrap: wrap; }
</style>
</head>
<body>
<header>
  <h1>频道配置</h1>
  <div class="sub">勾选要放进设备的频道，调整顺序，保存后重启媒体服务即可生效。</div>
</header>
<main>
  <div class="row">
    <input type="text" id="source" value="__SOURCE__" spellcheck="false">
    <button id="load">加载直播源</button>
  </div>

  <div class="panels">
    <div class="panel">
      <h2><span>源里的频道</span><span id="availCount"></span></h2>
      <div class="list" id="available"><div class="empty">点上面的按钮加载</div></div>
    </div>
    <div class="panel">
      <h2><span>设备上的频道</span><span id="selCount"></span></h2>
      <div class="list" id="selected"></div>
    </div>
  </div>

  <div class="status" id="status">尚未加载。</div>
  <div class="actions">
    <button class="primary" id="save">保存</button>
    <button id="check">检测失效</button>
    <button id="dropbad" disabled>剔除失效</button>
    <button id="clear">清空选择</button>
    <span class="sub" id="saved"></span>
  </div>
</main>
<script>
const MAX_CHANNELS = __MAX__;
const CONTROL_MAX = __CONTROL__;
let available = [];
let selected = [];
// Per-channel check result: 'ok' | 'slow' | 'dead'. Not a Set of failures,
// because a channel that works but is slow is a third thing worth showing --
// reporting it as dead is what made "剔除失效" delete working channels.
let health = new Map();

const $ = (id) => document.getElementById(id);
const esc = (s) => s.replace(/[&<>"]/g, (c) =>
  ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));

function render() {
  $('availCount').textContent = available.length ? available.length + ' 个' : '';
  $('selCount').textContent = selected.length + ' 个';

  $('available').innerHTML = available.length ? available.map((c, i) =>
    `<div class="item"><span class="name">${esc(c.name)}</span>
     <button data-add="${i}">添加</button></div>`).join('')
    : '<div class="empty">源里没有解析到频道</div>';

  $('selected').innerHTML = selected.length ? selected.map((c, i) => {
    const h = health.get(i);
    const cls = h === 'dead' ? ' dead' : (h === 'slow' ? ' slow' : '');
    const tag = h === 'dead' ? ' （失效）' : (h === 'slow' ? ' （较慢）' : '');
    return `<div class="item"><span class="idx">${i + 1}</span>
     <span class="name${cls}">${esc(c.name)}${tag}</span>
     <button data-up="${i}" ${i === 0 ? 'disabled' : ''}>↑</button>
     <button data-down="${i}" ${i === selected.length - 1 ? 'disabled' : ''}>↓</button>
     <button data-del="${i}">✕</button></div>`;
  }).join('')
    : '<div class="empty">还没有选择频道</div>';

  updateStatus();
}

function configBytes() {
  const payload = JSON.stringify({
    channel_list: selected.map((c, i) => ({id: 'ch' + String(i).padStart(3, '0'), name: c.name}))
  });
  return new TextEncoder().encode(payload).length;
}

function updateStatus() {
  const bytes = configBytes();
  const parts = [`已选 ${selected.length} / ${MAX_CHANNELS} 个频道`,
                 `下发报文约 ${bytes} 字节（上限 ${CONTROL_MAX}）`];
  // Warnings, not refusals: saving is allowed either way. So each message says
  // what will happen to the list rather than telling the reader to stop, since
  // they may have a reason to keep more than one device can show.
  let cls = 'ok', extra = '';
  if (selected.length > MAX_CHANNELS) {
    cls = 'bad';
    extra = `　超出设备上限：设备只会收到前 ${MAX_CHANNELS} 个，后面 ${selected.length - MAX_CHANNELS} 个不会显示。`;
  } else if (bytes > CONTROL_MAX) {
    cls = 'bad';
    extra = '　报文超出设备能收的上限，设备可能一个频道都收不到。减少数量或改用更短的频道名。';
  } else if (bytes > CONTROL_MAX * 0.8) {
    cls = 'warn';
    extra = `　接近上限，余量 ${CONTROL_MAX - bytes} 字节。再加频道要留意。`;
  } else if (selected.length > 60) {
    cls = 'warn';
    extra = '　频道较多，设备上只有上下两个键，翻台会比较费事。';
  } else if (selected.length === 0) {
    cls = ''; extra = '';
  }
  $('status').className = 'status ' + cls;
  $('status').textContent = parts.join('　·　') + extra;
}

$('available').addEventListener('click', (e) => {
  const i = e.target.dataset.add;
  if (i === undefined) return;
  selected.push(available[+i]);   // keeps its http-user-agent, needed to probe
  render();
});

$('selected').addEventListener('click', (e) => {
  const t = e.target.dataset;
  if (t.del !== undefined) selected.splice(+t.del, 1);
  else if (t.up !== undefined) {
    const i = +t.up; [selected[i - 1], selected[i]] = [selected[i], selected[i - 1]];
  } else if (t.down !== undefined) {
    const i = +t.down; [selected[i + 1], selected[i]] = [selected[i], selected[i + 1]];
  } else return;
  render();
});

$('clear').addEventListener('click', () => { selected = []; render(); });

// Availability is the one thing a playlist cannot tell you: these sources are
// community mirrors that die in batches, so a list that worked when it was
// imported can be half dead by the time it is used. Testing decodes a frame,
// which is the only honest check.

async function probeAll() {
  if (!selected.length) return;
  health.clear();
  $('check').disabled = true;
  $('save').disabled = true;
  $('dropbad').disabled = true;

  // Several at a time. One at a time was the obvious way to write it and the
  // wrong one: a dead address costs the full timeout before it answers, so a
  // list with many dead entries spends most of its time waiting rather than
  // working, and the page looks hung. Six at once is enough to keep the wait
  // near the time of the slowest single channel without opening so many
  // connections that the sources start refusing them.
  const LANES = 6;
  let done = 0;

  async function one(i) {
    const c = selected[i];
    try {
      // `c.agent`, which is what the field is called everywhere else -- the
      // playlist parser emits it, the table stores it, the saver writes it out.
      // This line read `c.ua`, which is undefined on every channel, so the
      // request always carried an empty user agent. That is not a small
      // mistake here: a source that only answers a particular player refuses
      // everyone else, the comment in channels.txt says so, and 22 of the
      // entries in the table this was found in are like that. The check
      // therefore reported working channels as dead, and the user -- who
      // tested one and watched it play -- was right and the page was wrong.
      const r = await fetch('/probe?url=' + encodeURIComponent(c.url)
                            + '&ua=' + encodeURIComponent(c.agent || ''));
      const d = await r.json();
      health.set(i, d.state || (d.ok ? 'ok' : 'dead'));
    } catch {
      // A failed request to our own page is not a verdict on the channel, but
      // leaving it out would silently drop it from the summary.
      health.set(i, 'dead');
    }
    done++;
    $('status').className = 'status';
    $('status').textContent = `检测中 ${done}/${selected.length}：${c.name}`;
    render();
  }

  let next = 0;
  const workers = Array.from({ length: Math.min(LANES, selected.length) }, async () => {
    while (next < selected.length) {
      const i = next++;
      await one(i);
    }
  });
  await Promise.all(workers);

  const dead = [...health.values()].filter(v => v === 'dead').length;
  const slow = [...health.values()].filter(v => v === 'slow').length;
  const good = selected.length - dead - slow;

  $('check').disabled = false;
  $('save').disabled = false;
  $('dropbad').disabled = dead === 0;

  const parts = [`可用 ${good} 个`];
  if (slow) parts.push(`较慢 ${slow} 个`);
  if (dead) parts.push(`失效 ${dead} 个`);
  $('status').className = 'status ' + (dead ? 'bad' : (slow ? 'warn' : 'ok'));
  let text = `检测完成：${parts.join('，')}。`;
  if (slow) {
    text += '「较慢」的频道可用，但打开需要十几秒以上，设备上会等更久。';
  }
  if (dead) {
    text += '失效的已标红划掉；点「剔除失效」只移除这些，「较慢」的会保留。';
  }
  $('status').textContent = text;
}

$('check').addEventListener('click', probeAll);

$('dropbad').addEventListener('click', () => {
  // Only the dead. A channel that works but is slow stays: it is a channel the
  // operator can decide about, and removing it for them is how a working source
  // got deleted by a timeout that was set too short.
  selected = selected.filter((_, i) => health.get(i) !== 'dead');
  health.clear();
  $('dropbad').disabled = true;
  render();
});

$('load').addEventListener('click', async () => {
  const url = $('source').value.trim();
  if (!url) return;
  $('status').textContent = '正在获取直播源…';
  $('load').disabled = true;
  try {
    const r = await fetch('/fetch?url=' + encodeURIComponent(url));
    const data = await r.json();
    if (data.error) throw new Error(data.error);
    available = data.channels;
    $('saved').textContent = '';
    render();
  } catch (err) {
    $('status').className = 'status bad';
    $('status').textContent = '加载失败：' + err.message;
  } finally {
    $('load').disabled = false;
  }
});

$('save').addEventListener('click', async () => {
  $('save').disabled = true;
  try {
    const r = await fetch('/save', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({channels: selected})
    });
    const data = await r.json();
    if (data.error) throw new Error(data.error);
    $('saved').textContent = '已保存到 ' + data.path + '（' + data.count + ' 个频道）';
    $('status').className = 'status ok';
    $('status').textContent = '保存成功。重启媒体服务后设备即使用这份列表。';
  } catch (err) {
    $('saved').textContent = '';
    $('status').className = 'status bad';
    $('status').textContent = '保存失败：' + err.message;
  } finally {
    $('save').disabled = false;
  }
});

// Open on whatever is already configured, so the page reflects reality.
(async () => {
  try {
    const r = await fetch('/current');
    const data = await r.json();
    selected = data.channels || [];
    render();
  } catch { render(); }
})();
</script>
</body>
</html>
"""


class Handler(BaseHTTPRequestHandler):
    server_version = "channel-config"

    def log_message(self, fmt, *args):
        sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))

    def _send(self, status: int, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        # A configuration page must never be cached: a stale copy would show the
        # previous selection as if it were current.
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, payload: dict, status: int = 200) -> None:
        self._send(status, json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                   "application/json; charset=utf-8")

    def do_GET(self) -> None:
        path, _, query = self.path.partition("?")
        if path in ("/", "/index.html"):
            page = (PAGE.replace("__SOURCE__", html.escape(DEFAULT_SOURCE, quote=True))
                        .replace("__MAX__", str(TV_CHANNEL_MAX))
                        .replace("__CONTROL__", str(TV_CONTROL_MAX)))
            self._send(200, page.encode("utf-8"), "text/html; charset=utf-8")
        elif path == "/current":
            self._json({"channels": read_selection()})
        elif path == "/fetch":
            self._fetch(query)
        elif path == "/probe":
            self._probe(query)
        else:
            self._json({"error": "未知路径"}, 404)

    def _fetch(self, query: str) -> None:
        from urllib.parse import parse_qs, urlparse
        values = parse_qs(query).get("url", [])
        if not values:
            self._json({"error": "缺少 url 参数"}, 400)
            return
        url = values[0]
        parsed = urlparse(url)
        if parsed.scheme not in ("http", "https") or not parsed.hostname:
            self._json({"error": "地址必须以 http:// 或 https:// 开头"}, 400)
            return
        # Anything reachable from this machine is reachable, including the LAN;
        # that is the point of the tool, so only the scheme is constrained.
        try:
            request = urllib.request.Request(url, headers={"User-Agent": "channel-config/1"})
            with urllib.request.urlopen(request, timeout=FOLLOW_TIMEOUT_S) as response:
                raw = response.read(MAX_PLAYLIST_BYTES + 1)
        except urllib.error.URLError as error:
            self._json({"error": f"无法获取：{error.reason}"})
            return
        except Exception as error:
            self._json({"error": f"无法获取：{type(error).__name__}"})
            return
        if len(raw) > MAX_PLAYLIST_BYTES:
            self._json({"error": "播放列表过大，已拒绝"})
            return
        channels = parse_playlist(raw.decode("utf-8", errors="replace"))
        if not channels:
            self._json({"error": "这个地址里没有解析到频道，确认是 M3U 播放列表"})
            return
        self._json({"channels": channels})

    def _probe(self, query: str) -> None:
        """Check one address by actually decoding a frame from it.

        Availability cannot be read off a playlist: community sources die in
        batches, and a list that worked when it was imported can be dead by the
        time it is used. A single frame is enough to tell a working source from
        a 404.

        Answers with three states rather than a yes/no, because the interesting
        case is the one a yes/no has to guess at: an address that is working but
        slow. Reporting it as dead leads to deleting a channel that plays;
        reporting it as fine hides that it will take a long time to open. The
        timing is measured here, so the page can say which it is.
        """
        from urllib.parse import parse_qs
        values = parse_qs(query).get("url", [])
        if not values:
            self._json({"error": "缺少 url 参数"}, 400)
            return
        url = values[0]
        raw_agent = parse_qs(query).get("ua", [""])[0]
        agent = raw_agent or (DEFAULT_USER_AGENT if url.startswith(("http://", "https://")) else "")
        if not url.startswith(("http://", "https://")):
            self._json({"error": "地址无效"}, 400)
            return
        import subprocess
        import time
        # Microseconds. ffmpeg's -rw_timeout counts microseconds, and this used
        # to pass seconds*1000 -- so a "forty second" budget was forty
        # milliseconds, and every address that needed a network round trip came
        # back as dead in about 0.4 s. The check had never worked at all; it was
        # not that the timeout was too short, but that the number meant
        # something else to the program receiving it.
        command = [FFMPEG, "-nostdin", "-hide_banner", "-loglevel", "quiet",
                   "-rw_timeout", str(PROBE_DEAD_S * 1000 * 1000)]
        if agent:
            command += ["-user_agent", agent]
        command += ["-i", url, "-map", "0:v:0", "-frames:v", "1",
                    "-f", "image2pipe", "-"]
        started = time.monotonic()
        try:
            # A hard ceiling above the address's own timeout, so this cannot
            # outlive the wait the page is showing.
            result = subprocess.run(command, capture_output=True,
                                    timeout=PROBE_DEAD_S + 4)
            ok = len(result.stdout) > 200
        except subprocess.TimeoutExpired:
            ok = False
        except OSError:
            self._json({"error": "找不到 ffmpeg，无法检测"}, 500)
            return
        elapsed = time.monotonic() - started
        # "slow" only when it worked: an address that failed is a failure no
        # matter how long it took.
        state = ("ok" if elapsed < PROBE_SLOW_S else "slow") if ok else "dead"
        self._json({"url": url, "ok": ok, "state": state,
                    "seconds": round(elapsed, 1)})

    def do_POST(self) -> None:
        if self.path != "/save":
            self._json({"error": "未知路径"}, 404)
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            self._json({"error": "请求长度无效"}, 400)
            return
        if not 0 < length <= 1024 * 1024:
            self._json({"error": "请求过大或为空"}, 400)
            return
        try:
            payload = json.loads(self.rfile.read(length))
            channels = payload["channels"]
            if not isinstance(channels, list):
                raise ValueError("channels 必须是数组")
            write_selection(channels)
        except (ValueError, KeyError, TypeError, json.JSONDecodeError) as error:
            self._json({"error": str(error)}, 400)
            return
        self._json({"path": str(channels_file()), "count": len(channels)})


def main(argv: list[str] | None = None) -> int:
    """Run the page until interrupted.

    `argv` is optional and defaults to None, meaning "read sys.argv" -- which is
    how argparse behaves on its own and what running this file directly relies
    on. It exists because inside a bundled executable this module is not a
    program but a function that one process calls on behalf of another: the real
    sys.argv there begins with the executable's internal sub-command, which
    argparse would reject as an unknown argument.
    """
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--bind", default="127.0.0.1",
                        help="listen address (default: loopback only)")
    parser.add_argument("--port", type=int, default=8097)
    # Suppresses the start-up line. Used when something else starts this page
    # and prints the address itself: two lines saying the same thing a few
    # lines apart is what the reader least needs. Running this file directly
    # leaves the flag off, and then it does announce where it is.
    parser.add_argument("--quiet", action="store_true",
                        help="do not print the address (the caller prints it)")
    args = parser.parse_args(argv)

    if not is_private(args.bind) and args.bind != "0.0.0.0":
        print(f"拒绝监听 {args.bind}：只允许回环或本网地址", file=sys.stderr)
        return 1

    # A port already in use is an ordinary thing -- the previous run was not
    # closed, most often -- and it produced a twenty-line Python traceback
    # ending in "OSError: [Errno 48] Address already in use". That is the wrong
    # language for the reader and the wrong length for the message: what
    # happened, and what to do, is one sentence.
    try:
        server = ThreadingHTTPServer((args.bind, args.port), Handler)
    except OSError as error:
        if error.errno == errno.EADDRINUSE:
            print(f"端口 {args.port} 已被占用——多半是上一次的程序还没关干净。",
                  file=sys.stderr)
            print("把之前的窗口关掉，或重启电脑后再试。", file=sys.stderr)
        else:
            print(f"配置页打不开（errno={error.errno}）。", file=sys.stderr)
        return 1
    shown = "127.0.0.1" if args.bind in ("0.0.0.0", "::") else args.bind
    # Nothing is printed when the caller is going to say it. The save path used
    # to appear here as well, which is where the file lives rather than
    # something the reader acts on, and it appeared again a few lines away in
    # the parent process's output.
    if not args.quiet:
        print(f"挑频道：在浏览器里打开 http://{shown}:{args.port}", flush=True)
        print(flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止。")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
