# -*- coding: utf-8 -*-
"""
KugouOSC —— 把酷狗音乐正在播放的「歌名 / 歌手 / 进度 / 歌词」
           实时推送到 VRChat 聊天框（Chatbox）

依赖：
    pip install python-osc requests winsdk
若 winsdk 装不上（新版 Python），改用：
    pip install python-osc requests winrt-runtime winrt-Windows.Media.Control
                winrt-Windows.Foundation winrt-Windows.Foundation.Collections

用法：
    1. 打开酷狗音乐并播放歌曲（建议先把歌拖回开头）
    2. 双击 run.bat（或命令行执行 python kugou_osc.py）
    3. 进入 VRChat，头顶聊天框即可看到
"""

import asyncio
import re
import sys
import time
from datetime import datetime, timezone

# 防止控制台编码报错
try:
    sys.stdout.reconfigure(errors="replace")
except Exception:
    pass

if sys.platform != "win32":
    print("本程序依赖 Windows 系统媒体控制接口（SMTC），只能在 Windows 上运行。")
    sys.exit(1)

try:
    import requests
    from pythonosc.udp_client import SimpleUDPClient
except ImportError as e:
    print(f"缺少依赖：{e}")
    print("请先双击 install.bat 安装依赖，或执行：")
    print("    pip install python-osc requests winsdk")
    sys.exit(1)


# ============================ 用户配置 ============================
VRC_IP   = "127.0.0.1"      # VRChat 在同一台电脑就保持默认
VRC_PORT = 9000             # VRChat OSC 默认接收端口

MULTILINE        = True     # True：歌名 / 进度 / 歌词 分三行
SHOW_PROGRESS    = True     # 显示进度条
SHOW_LYRIC       = True     # 显示歌词
SHOW_WHEN_PAUSED = True     # 暂停时是否继续显示

CHATBOX_MIN_INTERVAL = 1.5  # 两次推送最短间隔（秒）。VRChat 有限流，别小于 1.5
CHATBOX_MAX_LEN      = 142  # 聊天框字符上限（VRChat 上限 144，留点余量）

BAR_WIDTH = 12              # 进度条宽度
BAR_FULL  = "="             # 已完成部分
BAR_EMPTY = "-"             # 未完成部分
BAR_HEAD  = ">"             # 当前位置箭头，不想用就改成 ""

LYRIC_ORDER     = ("netease", "lrclib")   # 歌词源优先级
FALLBACK_TO_ANY = True      # 找不到酷狗会话时，是否退而显示其它播放器

# 歌词时间偏移（秒）。歌词显示比实际早，就调大；晚，就调小。
# 例：显示太快 1.5 秒 → 改成 1.5；显示太慢 → 改成 -1.5
LYRIC_OFFSET = 0.0

SEND_AVATAR_PARAMS = False  # 想在 Avatar 上用进度条就改 True
PARAM_PROGRESS = "/avatar/parameters/KugouProgress"  # Float 0~1
PARAM_PLAYING  = "/avatar/parameters/KugouPlaying"   # Bool
# ==================================================================


# ---------------------- 导入系统媒体控制接口 ----------------------
MediaManager = None
PlaybackStatus = None

try:
    from winrt.windows.media.control import (
        GlobalSystemMediaTransportControlsSessionManager as MediaManager,
        GlobalSystemMediaTransportControlsSessionPlaybackStatus as PlaybackStatus,
    )
except ImportError:
    try:
        from winsdk.windows.media.control import (
            GlobalSystemMediaTransportControlsSessionManager as MediaManager,
            GlobalSystemMediaTransportControlsSessionPlaybackStatus as PlaybackStatus,
        )
    except ImportError:
        print("无法导入系统媒体控制接口，请执行下面任意一条命令：")
        print("    pip install winsdk")
        print("    pip install winrt-runtime winrt-Windows.Media.Control "
              "winrt-Windows.Foundation winrt-Windows.Foundation.Collections")
        sys.exit(1)


# ============================ 歌词模块 ============================
class Lyrics:
    """解析并查询 LRC 歌词"""

    def __init__(self):
        self.lines = []          # [(秒, 文本), ...]

    def load(self, lrc_text):
        out = []
        for raw in lrc_text.splitlines():
            stamps = re.findall(r"\[(\d{1,3}):(\d{1,2}(?:[.:]\d{1,3})?)\]", raw)
            if not stamps:
                continue
            text = re.sub(r"\[\d{1,3}:\d{1,2}(?:[.:]\d{1,3})?\]", "", raw).strip()
            if not text:
                continue
            for mm, ss in stamps:
                try:
                    sec = int(mm) * 60 + float(ss.replace(":", "."))
                except ValueError:
                    continue
                out.append((sec, text))
        out.sort(key=lambda x: x[0])
        self.lines = out

    def at(self, t):
        if not self.lines:
            return ""
        cur = ""
        for ts, txt in self.lines:
            if ts <= t + 0.3:
                cur = txt
            else:
                break
        return cur


_SESSION = requests.Session()
_SESSION.headers.update({
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                  "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36",
})
_lrc_cache = {}


def _norm(s):
    return re.sub(r"[\s\-_·・（）()\[\]【】]", "", (s or "")).lower()


def _lyric_netease(title, artist):
    """网易云音乐歌词源（中文歌命中率高）"""
    r = _SESSION.get(
        "https://music.163.com/api/search/get/web",
        params={"s": f"{title} {artist}".strip(), "type": 1, "limit": 10, "offset": 0},
        headers={"Referer": "https://music.163.com/"},
        timeout=6,
    )
    songs = (r.json().get("result") or {}).get("songs") or []
    if not songs:
        return None

    nt, na = _norm(title), _norm(artist)
    best, best_score = None, -1
    for s in songs:
        name = _norm(s.get("name", ""))
        artists = _norm(" ".join(a.get("name", "") for a in s.get("artists", [])))
        score = 0
        if name == nt:
            score += 10
        elif nt and (nt in name or name in nt):
            score += 5
        if na and (na in artists or artists in na):
            score += 8
        if score > best_score:
            best, best_score = s, score

    if best is None:
        best = songs[0]

    r2 = _SESSION.get(
        "https://music.163.com/api/song/lyric",
        params={"id": best["id"], "lv": 1, "kv": 1, "tv": -1},
        headers={"Referer": "https://music.163.com/"},
        timeout=6,
    )
    lrc = (r2.json().get("lrc") or {}).get("lyric") or ""
    return lrc if re.search(r"\[\d{1,3}:\d{1,2}", lrc) else None


def _lyric_lrclib(title, artist, album="", duration=0.0):
    """lrclib.net 歌词源（英文歌 / 冷门歌命中率高）"""
    try:
        params = {"track_name": title}
        if artist:
            params["artist_name"] = artist
        if album:
            params["album_name"] = album
        if duration and duration > 5:
            params["duration"] = int(duration)
        r = _SESSION.get("https://lrclib.net/api/get", params=params, timeout=8)
        if r.status_code == 200:
            synced = r.json().get("syncedLyrics")
            if synced:
                return synced
    except Exception:
        pass

    try:
        r = _SESSION.get("https://lrclib.net/api/search",
                         params={"q": f"{title} {artist}".strip()}, timeout=8)
        if r.status_code == 200:
            for item in r.json():
                if item.get("syncedLyrics"):
                    return item["syncedLyrics"]
    except Exception:
        pass
    return None


def fetch_lrc(title, artist, album="", duration=0.0):
    key = f"{title}|{artist}".lower()
    if key in _lrc_cache:
        return _lrc_cache[key]
    if len(_lrc_cache) > 200:
        _lrc_cache.clear()

    lrc = None
    for src in LYRIC_ORDER:
        try:
            if src == "netease":
                lrc = _lyric_netease(title, artist)
            elif src == "lrclib":
                lrc = _lyric_lrclib(title, artist, album, duration)
        except Exception as e:
            print(f"    [歌词源 {src}] 出错：{e}")
            lrc = None
        if lrc:
            print(f"    [歌词] 命中源：{src}")
            break

    _lrc_cache[key] = lrc
    return lrc


# ============================ 工具函数 ============================
def fmt_time(sec):
    sec = max(0, int(sec))
    return f"{sec // 60}:{sec % 60:02d}"


def make_bar(pos, length, width=BAR_WIDTH):
    if length <= 0:
        return ""
    ratio = min(1.0, max(0.0, pos / length))
    filled = max(0, min(width, int(round(ratio * width))))
    if BAR_HEAD and filled < width:
        return BAR_FULL * filled + BAR_HEAD + BAR_EMPTY * (width - filled - 1)
    return BAR_FULL * filled + BAR_EMPTY * (width - filled)


def timeline_position(tl, playing):
    """SMTC 的 position 是「上次更新那一刻」的值，播放中需要补上流逝时间"""
    pos = tl.position.total_seconds()
    if playing and tl.last_updated_time is not None:
        try:
            lu = tl.last_updated_time
            if lu.tzinfo is None:
                lu = lu.replace(tzinfo=timezone.utc)
            delta = (datetime.now(timezone.utc) - lu).total_seconds()
            if 0 <= delta < 5:
                pos += delta
        except Exception:
            pass
    return max(0.0, pos)


def pick_session(manager):
    """优先挑酷狗的会话"""
    try:
        sessions = list(manager.get_sessions())
    except Exception:
        sessions = []

    for s in sessions:
        app = (getattr(s, "source_app_user_model_id", "") or "").lower()
        if "kugou" in app or "kgmusic" in app:
            return s

    if FALLBACK_TO_ANY:
        try:
            return manager.get_current_session()
        except Exception:
            return None
    return None


# ============================ 主循环 ============================
async def main():
    client = SimpleUDPClient(VRC_IP, VRC_PORT)
    manager = await MediaManager.request_async()

    lyrics = Lyrics()
    lyric_key  = None
    last_title = None
    last_lyric = None
    last_text  = ""
    last_send  = 0.0

    # ── 内部计时兜底（酷狗不给进度时用）──
    fallback_key   = None          # 当前计时对应的歌
    fallback_pos   = 0.0           # 上次记录的秒数
    fallback_start = time.time()   # 上次记录时的墙钟

    print("[*] 已启动，正在监听播放器……")
    print(f"[*] OSC 目标：{VRC_IP}:{VRC_PORT}")
    print("[*] 按 Ctrl+C 退出\n")

    while True:
        await asyncio.sleep(0.4)

        session = pick_session(manager)
        if session is None:
            if last_title is not None:
                print("[*] 没有检测到正在播放的媒体")
                last_title   = None
                lyric_key    = None
                fallback_key = None
            continue

        try:
            props = await session.try_get_media_properties_async()
        except Exception:
            continue

        title  = (props.title or "").strip()
        artist = (props.artist or "").strip()
        album  = (props.album_title or "").strip()
        if not title:
            continue

        if title != last_title:
            print(f"[♪] {title} - {artist}")
            last_title = title

        try:
            playing = (session.get_playback_info().playback_status
                       == PlaybackStatus.PLAYING)
        except Exception:
            playing = False

        # ── 先抓歌词（A/B 都要用到）──
        key = f"{title}|{artist}"
        if key != lyric_key:
            lyric_key = key
            lyrics = Lyrics()
            last_lyric = None
            loop = asyncio.get_running_loop()
            lrc = await loop.run_in_executor(
                None, fetch_lrc, title, artist, album, 0.0
            )
            if lrc:
                lyrics.load(lrc)
                print(f"    已加载歌词 {len(lyrics.lines)} 行")
            else:
                print("    没有找到同步歌词")

        # ── 读取系统时间轴 ──
        try:
            tl = session.get_timeline_properties()
            sys_pos = timeline_position(tl, playing)
            sys_len = tl.end_time.total_seconds() if tl.end_time else 0.0
        except Exception:
            sys_pos, sys_len = 0.0, 0.0

        # ── A: 系统不给时长就自己计时；B: 用歌词末行当总长 ──
        now_ts = time.time()
        if sys_len > 1:
            # 系统给了完整数据，直接用
            pos, length = sys_pos, sys_len
            fallback_key   = key
            fallback_pos   = pos
            fallback_start = now_ts
        else:
            # 系统没给时长 → 走内部计时
            if key != fallback_key:
                fallback_key   = key
                fallback_pos   = 0.0
                fallback_start = now_ts
            if playing:
                pos = fallback_pos + (now_ts - fallback_start)
            else:
                # 暂停：冻结位置
                fallback_pos   = fallback_pos + (now_ts - fallback_start)
                fallback_start = now_ts
                pos = fallback_pos

            # B: 歌词最后一行时间 + 8 秒，当作总长度
            length = (lyrics.lines[-1][0] + 8) if lyrics.lines else 0.0

        # ── 播完一轮自动归零 ──
        if length > 0 and pos > length:
            fallback_pos   = 0.0
            fallback_start = now_ts
            pos = 0.0

        if not playing and not SHOW_WHEN_PAUSED:
            continue

        # ── 歌词查询（带上偏移校正）──
        cur_lyric = lyrics.at(pos + LYRIC_OFFSET) if lyrics.lines else ""

        # ── 拼装文本 ──
        head = ("♪ " if playing else "|| ") + title
        if artist:
            head += f" - {artist}"

        sep = "\n" if MULTILINE else "  |  "

        def build(lyric_text):
            p = [head]
            if SHOW_PROGRESS:
                if length > 0:
                    p.append(f"[{make_bar(pos, length)}] "
                             f"{fmt_time(pos)}/{fmt_time(length)}")
                else:
                    p.append(fmt_time(pos))
            if SHOW_LYRIC and lyric_text:
                p.append(lyric_text)
            return sep.join(p)

        text = build(cur_lyric)

        # 超长优先截断歌词行
        if len(text) > CHATBOX_MAX_LEN and cur_lyric:
            over = len(text) - CHATBOX_MAX_LEN
            keep = len(cur_lyric) - over - 1
            cur_lyric = (cur_lyric[:keep] + "…") if keep >= 4 else ""
            text = build(cur_lyric)
        if len(text) > CHATBOX_MAX_LEN:
            text = text[:CHATBOX_MAX_LEN - 1] + "…"
            cur_lyric = ""

        # ── 限流发送 ──
        now = time.time()
        changed = (text != last_text)
        lyric_changed = SHOW_LYRIC and (cur_lyric != last_lyric)

        if text and (
            (changed and now - last_send >= CHATBOX_MIN_INTERVAL)
            or (lyric_changed and now - last_send >= 0.9)
        ):
            try:
                client.send_message("/chatbox/input", [text, True, False])
                last_send  = now
                last_text  = text
                last_lyric = cur_lyric
            except Exception as e:
                print(f"    [OSC] 发送失败：{e}")

        # ── Avatar 参数（可选）──
        if SEND_AVATAR_PARAMS:
            try:
                ratio = (pos / length) if length > 0 else 0.0
                ratio = min(1.0, max(0.0, ratio))
                client.send_message(PARAM_PROGRESS, float(ratio))
                client.send_message(PARAM_PLAYING, bool(playing))
            except Exception:
                pass


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n[*] 已退出")
