# -*- coding: utf-8 -*-
"""
KugouOSC —— 酷狗音乐 → VRChat 聊天框
           歌名 / 歌手 / 进度 / 原文歌词 / 中文翻译 / CPU·GPU 名称与占用
           硬件信息每 20 秒出现一次，显示数秒后自动隐去
"""

import asyncio
import re
import subprocess
import sys
import time
from datetime import datetime, timezone

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
    print("    pip install python-osc requests psutil")
    sys.exit(1)

try:
    import psutil
    PSUTIL_OK = True
except ImportError:
    PSUTIL_OK = False
    print("[!] 没装 psutil，硬件信息将不可用。安装：pip install psutil")


# ============================ 用户配置 ============================
VRC_IP   = "127.0.0.1"
VRC_PORT = 9000

MULTILINE        = True
SHOW_PROGRESS    = True
SHOW_LYRIC       = True
SHOW_TRANSLATION = True
SHOW_HW          = True     # 是否显示硬件信息
SHOW_HW_NAME     = True     # 硬件行是否带 CPU / GPU 型号名
SHOW_WHEN_PAUSED = True

CHATBOX_MIN_INTERVAL = 1.5
CHATBOX_MAX_LEN      = 142

HW_REFRESH = 5.0            # 硬件占用数值刷新间隔(秒)

# ── 硬件信息出现节奏 ──
HW_ROTATE       = True      # True：每轮才出现；False：一直显示
HW_SHOW_SECONDS = 3.0       # 每次显示多少秒
HW_GAP_SECONDS  = 17.0      # 间隔多少秒 → 与上面相加 = 20 秒一轮

BAR_WIDTH = 12
BAR_FULL  = "="
BAR_EMPTY = "-"
BAR_HEAD  = ">"

LYRIC_ORDER     = ("netease", "lrclib")
FALLBACK_TO_ANY = True
LYRIC_OFFSET    = 0.0

SEND_AVATAR_PARAMS = False
PARAM_PROGRESS = "/avatar/parameters/KugouProgress"
PARAM_PLAYING  = "/avatar/parameters/KugouPlaying"
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
def parse_lrc(lrc_text):
    out = []
    for raw in (lrc_text or "").splitlines():
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
    return out


class Track:
    def __init__(self):
        self.lines = []
        self.trans = []

    def at(self, t):
        return self._pick(self.lines, t)

    def trans_at(self, t):
        return self._pick(self.trans, t)

    @staticmethod
    def _pick(lines, t):
        if not lines:
            return ""
        cur = ""
        for ts, txt in lines:
            if ts <= t + 0.3:
                cur = txt
            else:
                break
        return cur

    def has_trans(self):
        return bool(self.trans)


_SESSION = requests.Session()
_SESSION.headers.update({
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                  "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36",
})
_lrc_cache = {}


def _norm(s):
    return re.sub(r"[\s\-_·・（）()\[\]【】]", "", (s or "")).lower()


def _lyric_netease(title, artist):
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
        params={"id": best["id"], "lv": 1, "kv": 1, "tv": 1},
        headers={"Referer": "https://music.163.com/"},
        timeout=6,
    )
    data = r2.json()
    lrc = (data.get("lrc") or {}).get("lyric") or ""
    tly = (data.get("tlyric") or {}).get("lyric") or ""

    if not re.search(r"\[\d{1,3}:\d{1,2}", lrc):
        return None
    if tly and not re.search(r"\[\d{1,3}:\d{1,2}", tly):
        tly = ""
    return (lrc, tly)


def _lyric_lrclib(title, artist, album="", duration=0.0):
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
                return (synced, "")
    except Exception:
        pass

    try:
        r = _SESSION.get("https://lrclib.net/api/search",
                         params={"q": f"{title} {artist}".strip()}, timeout=8)
        if r.status_code == 200:
            for item in r.json():
                if item.get("syncedLyrics"):
                    return (item["syncedLyrics"], "")
    except Exception:
        pass
    return None


def fetch_lrc(title, artist, album="", duration=0.0):
    key = f"{title}|{artist}".lower()
    if key in _lrc_cache:
        return _lrc_cache[key]
    if len(_lrc_cache) > 200:
        _lrc_cache.clear()

    result = None
    for src in LYRIC_ORDER:
        try:
            if src == "netease":
                result = _lyric_netease(title, artist)
            elif src == "lrclib":
                result = _lyric_lrclib(title, artist, album, duration)
        except Exception as e:
            print(f"    [歌词源 {src}] 出错：{e}")
            result = None
        if result:
            print(f"    [歌词] 命中源：{src}")
            break

    _lrc_cache[key] = result
    return result


# ========================= 硬件监控 =========================
_CPU_NAME = ""
_GPU_NAME = ""


def _get_cpu_name():
    try:
        import winreg
        k = winreg.OpenKey(
            winreg.HKEY_LOCAL_MACHINE,
            r"HARDWARE\DESCRIPTION\System\CentralProcessor\0"
        )
        name, _ = winreg.QueryValueEx(k, "ProcessorNameString")
        winreg.CloseKey(k)
        name = re.sub(r"\s+", " ", name).strip()
        if name:
            name = name.replace("(R)", "").replace("(TM)", "")
            name = re.sub(r"\s+", " ", name).strip()
            return name
    except Exception as e:
        print(f"    [CPU名] 注册表失败：{e}")

    try:
        out = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             "(Get-CimInstance Win32_Processor).Name"],
            capture_output=True, text=True, timeout=8,
            creationflags=0x08000000,
        )
        if out.returncode == 0:
            lines = [l.strip() for l in out.stdout.strip().splitlines() if l.strip()]
            if lines:
                return lines[0]
    except Exception as e:
        print(f"    [CPU名] WMI 失败：{e}")

    try:
        import platform
        n = platform.processor()
        if n:
            return n
    except Exception:
        pass
    return ""


def _get_gpu_name():
    try:
        out = subprocess.run(
            ["nvidia-smi", "--query-gpu=name", "--format=csv,noheader"],
            capture_output=True, text=True, timeout=3,
            creationflags=0x08000000,
        )
        if out.returncode == 0:
            names = [v.strip() for v in out.stdout.strip().splitlines() if v.strip()]
            if names:
                return names[0].replace("NVIDIA ", "").replace("GeForce ", "")
    except Exception:
        pass

    try:
        out = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             "(Get-CimInstance Win32_VideoController).Name"],
            capture_output=True, text=True, timeout=10,
            creationflags=0x08000000,
        )
        if out.returncode == 0:
            names = [v.strip() for v in out.stdout.strip().splitlines() if v.strip()]
            names = [n for n in names if "Microsoft" not in n] or names
            if names:
                return names[0]
    except Exception as e:
        print(f"    [GPU名] WMI 失败：{e}")
    return ""


def _read_gpu_usage():
    try:
        out = subprocess.run(
            ["nvidia-smi",
             "--query-gpu=utilization.gpu",
             "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=3,
            creationflags=0x08000000,
        )
        if out.returncode == 0:
            vals = [v.strip() for v in out.stdout.strip().splitlines() if v.strip()]
            if vals:
                return max(int(float(v)) for v in vals)
    except Exception:
        pass
    return None


def get_hw_text(with_name=True):
    parts = []

    def named(base, name):
        if with_name and name:
            return f"{base} {name}"
        return base

    if PSUTIL_OK:
        try:
            u = psutil.cpu_percent(interval=None)
            parts.append(named(f"CPU {u:.0f}%", _CPU_NAME))
        except Exception:
            pass
        try:
            parts.append(f"MEM {psutil.virtual_memory().percent:.0f}%")
        except Exception:
            pass

    gpu = _read_gpu_usage()
    if gpu is not None:
        parts.append(named(f"GPU {gpu}%", _GPU_NAME))
    elif _GPU_NAME:
        parts.append(named("GPU", _GPU_NAME))

    return "  ".join(parts)
# ============================================================


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


def truncate(s, limit):
    if limit <= 0:
        return ""
    if len(s) <= limit:
        return s
    if limit <= 1:
        return "…"
    return s[:limit - 1] + "…"


# ============================ 主循环 ============================
async def main():
    global _CPU_NAME, _GPU_NAME

    client = SimpleUDPClient(VRC_IP, VRC_PORT)
    manager = await MediaManager.request_async()

    track      = Track()
    lyric_key  = None
    last_title = None
    last_lyric = None
    last_text  = ""
    last_send  = 0.0

    fallback_key   = None
    fallback_pos   = 0.0
    fallback_start = time.time()

    hw_text      = ""
    last_hw_time = 0.0

    hw_cycle_start = time.time()
    hw_was_shown   = False

    if PSUTIL_OK:
        try:
            psutil.cpu_percent(interval=None)
        except Exception:
            pass

    print("[*] 正在读取硬件信息……")
    _CPU_NAME = _get_cpu_name()
    _GPU_NAME = _get_gpu_name()
    print(f"[*] CPU: {_CPU_NAME or '读取失败'}")
    print(f"[*] GPU: {_GPU_NAME or '读取失败'}")

    print("[*] 已启动，正在监听播放器……")
    print(f"[*] OSC 目标：{VRC_IP}:{VRC_PORT}")
    print(f"[*] 硬件信息每 {HW_GAP_SECONDS + HW_SHOW_SECONDS:.0f} 秒显示 "
          f"{HW_SHOW_SECONDS:.0f} 秒")
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

        # 换歌抓歌词
        key = f"{title}|{artist}"
        if key != lyric_key:
            lyric_key = key
            track = Track()
            last_lyric = None
            loop = asyncio.get_running_loop()
            res = await loop.run_in_executor(
                None, fetch_lrc, title, artist, album, 0.0
            )
            if res:
                lrc, tly = res
                track.lines = parse_lrc(lrc)
                track.trans = parse_lrc(tly) if tly else []
                print(f"    已加载歌词 {len(track.lines)} 行"
                      f"（翻译 {len(track.trans)} 行）")
            else:
                print("    没有找到同步歌词")

        # 时间轴
        try:
            tl = session.get_timeline_properties()
            sys_pos = timeline_position(tl, playing)
            sys_len = tl.end_time.total_seconds() if tl.end_time else 0.0
        except Exception:
            sys_pos, sys_len = 0.0, 0.0

        now_ts = time.time()
        if sys_len > 1:
            pos, length = sys_pos, sys_len
            fallback_key   = key
            fallback_pos   = pos
            fallback_start = now_ts
        else:
            if key != fallback_key:
                fallback_key   = key
                fallback_pos   = 0.0
                fallback_start = now_ts
            if playing:
                pos = fallback_pos + (now_ts - fallback_start)
            else:
                fallback_pos   = fallback_pos + (now_ts - fallback_start)
                fallback_start = now_ts
                pos = fallback_pos
            length = (track.lines[-1][0] + 8) if track.lines else 0.0

        if length > 0 and pos > length:
            fallback_pos   = 0.0
            fallback_start = now_ts
            pos = 0.0

        if not playing and not SHOW_WHEN_PAUSED:
            continue

        cur_lyric = track.at(pos + LYRIC_OFFSET) if track.lines else ""
        cur_trans = ""
        if SHOW_TRANSLATION and track.has_trans():
            cur_trans = track.trans_at(pos + LYRIC_OFFSET)

        # 硬件数值刷新
        if SHOW_HW and now_ts - last_hw_time >= HW_REFRESH:
            last_hw_time = now_ts
            hw_text = get_hw_text()

        # 轮播：这一帧是否显示硬件行
        show_hw_now = False
        if SHOW_HW and hw_text:
            if not HW_ROTATE:
                show_hw_now = True
            else:
                cycle = HW_SHOW_SECONDS + HW_GAP_SECONDS
                phase = (now_ts - hw_cycle_start) % cycle
                show_hw_now = (phase < HW_SHOW_SECONDS)

        if show_hw_now and not hw_was_shown:
            print(f"    [硬件] {hw_text}")
            hw_was_shown = True
        elif not show_hw_now:
            hw_was_shown = False

        # 拼装
        head = ("♪ " if playing else "|| ") + title
        if artist:
            head += f" - {artist}"

        sep = "\n" if MULTILINE else "  |  "

        def build(lyric_text, trans_text, hw=True):
            p = [head]
            if SHOW_PROGRESS:
                if length > 0:
                    p.append(f"[{make_bar(pos, length)}] "
                             f"{fmt_time(pos)}/{fmt_time(length)}")
                else:
                    p.append(fmt_time(pos))
            if SHOW_LYRIC and lyric_text:
                p.append(lyric_text)
            if SHOW_TRANSLATION and trans_text:
                p.append(trans_text)
            if SHOW_HW and hw and hw_text:
                p.append(hw_text)
            return sep.join(p)

        def assemble(include_hw):
            t = build(cur_lyric, cur_trans, include_hw)

            if len(t) > CHATBOX_MAX_LEN and cur_trans:
                fixed = build(cur_lyric, "", include_hw)
                room = CHATBOX_MAX_LEN - len(fixed) - len(sep)
                t = build(cur_lyric, truncate(cur_trans, max(0, room)), include_hw)

            if len(t) > CHATBOX_MAX_LEN and cur_lyric:
                fixed = build("", "", include_hw)
                room = CHATBOX_MAX_LEN - len(fixed) - len(sep)
                t = build(truncate(cur_lyric, max(0, room)), "", include_hw)

            if len(t) > CHATBOX_MAX_LEN:
                t = truncate(t, CHATBOX_MAX_LEN)
            return t

        text = assemble(include_hw=show_hw_now)

        # 发送
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
