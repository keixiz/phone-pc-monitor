# -*- coding: utf-8 -*-
"""
手机远程监控 - 电脑端 Agent
"""
import asyncio
import json
import logging
import os
import platform
import socket
import subprocess
import sys
import time
from ctypes import windll, Structure, c_ulong, c_int, POINTER, byref

try:
    import configparser
    _HAS_CFG = True
except ImportError:
    _HAS_CFG = False

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("agent")

CFG = {
    "server": "wss://你的域名/ws",
    "token": "和服务器 config.json 保持一致",
    "source": "screen",
    "fps": 3,
    "camera_index": 0,
    "jpeg_quality": 70,
    "motion_threshold": 8,
}

CONFIG_FILES = [
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json"),
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.example.json"),
]

for _cf in CONFIG_FILES:
    if os.path.exists(_cf):
        try:
            with open(_cf, "r", encoding="utf-8") as _f:
                _u = json.load(_f)
            if isinstance(_u, dict):
                CFG.update({k: v for k, v in _u.items() if k in CFG})
        except Exception:
            pass
        break

# ---------- 屏幕采集 ----------
_screen = None
def make_screen_grabber():
    try:
        import mss
    except ImportError:
        try:
            from PIL import ImageGrab
            def grab():
                im = ImageGrab.grab()
                buf = io.BytesIO()
                im.save(buf, "JPEG", quality=CFG["jpeg_quality"])
                return buf.getvalue()
            log.info("使用 Pillow ImageGrab 采集屏幕")
            return grab
        except Exception as e:
            log.error("屏幕采集依赖缺失: %s", e)
            return None
    global _screen
    _screen = mss.mss()
    def grab():
        import io as _io
        mon = _screen.monitors[1]
        img = _screen.grab(mon)
        from PIL import Image
        im = Image.frombytes("RGB", img.size, img.bgra, "raw", "BGRX")
        buf = _io.BytesIO()
        im.save(buf, "JPEG", quality=CFG["jpeg_quality"])
        return buf.getvalue()
    log.info("使用 mss 采集屏幕")
    return grab

import io

# ---------- 摄像头（懒加载，点击才调用） ----------
class Camera:
    def __init__(self):
        self.cap = None
        self.index = CFG["camera_index"]
    def _ensure(self):
        if self.cap is None:
            try:
                import cv2
                self.cap = cv2.VideoCapture(self.index)
                log.info("摄像头已打开")
            except ImportError:
                log.warning("未安装 opencv，摄像头不可用")
                self.cap = None
    def grab(self):
        self._ensure()
        if self.cap is None:
            return None
        ok, frame = self.cap.read()
        if not ok:
            log.error("摄像头采集失败")
            return None
        import cv2
        ok, buf = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, CFG["jpeg_quality"]])
        if not ok:
            return None
        return buf.tobytes()
    def release(self):
        if self.cap is not None:
            try:
                import cv2
                self.cap.release()
            except Exception:
                pass
            self.cap = None
            log.info("摄像头已关闭")

# ---------- 键鼠 ----------
_kb = None
_mouse = None
try:
    from pynput import keyboard, mouse
    _kb = keyboard.Controller()
    _mouse = mouse.Controller()
    _MOUSE_OK = True
except ImportError:
    _MOUSE_OK = False
    log.warning("未安装 pynput，鼠标键盘远程控制不可用")

_vol_key_map = {
    "volume_up": 0xAF,
    "volume_down": 0xAE,
    "volume_mute": 0xAD,
}

def _win_vol_key(vk):
    if sys.platform.startswith("win") and hasattr(windll.user32, "keybd_event"):
        windll.user32.keybd_event(vk, 0, 0, 0)
        time.sleep(0.05)
        windll.user32.keybd_event(vk, 0, 2, 0)
        return True
    return False

def do_key(key, modifiers=None, char=None):
    """发送按键。音量键在 Windows 下用虚拟键码，其余走 pynput"""
    mods = modifiers or []
    if key in _vol_key_map and _win_vol_key(_vol_key_map[key]):
        return
    if _kb is None:
        log.warning("键盘未就绪")
        return
    try:
        mod_keys = []
        for m in mods:
            mod_keys.append({"ctrl": "ctrl", "alt": "alt", "shift": "shift", "win": "cmd"}.get(m))
        if char is not None:
            _kb.type(char)
            return
        k = {"esc": "esc", "enter": "enter", "backspace": "backspace", "space": "space",
             "tab": "tab", "left": "left", "right": "right", "up": "up", "down": "down",
             "f5": "f5", "volume_up": "volume_up", "volume_down": "volume_down",
             "volume_mute": "volume_mute", "d": "d", "c": "c", "v": "v", "x": "x", "z": "z"}.get(key, key)
        with _kb.pressed(*[m for m in mod_keys if m]):
            _kb.tap(k)
    except Exception as e:
        log.error("按键失败: %s", e)

def move_mouse(x=None, y=None, dx=None, dy=None, click=None):
    if _mouse is None:
        log.warning("鼠标未就绪")
        return
    try:
        if dx is not None:
            _mouse.move(int(dx), int(dy))
        elif x is not None:
            _mouse.position = (int(x), int(y))
        if click == "left":
            _mouse.click(mouse.Button.left, 1)
        elif click == "right":
            _mouse.click(mouse.Button.right, 1)
    except Exception as e:
        log.error("鼠标操作失败: %s", e)

# ---------- 系统操作 ----------
def do_system(action):
    try:
        if action == "lock":
            subprocess.Popen("rundll32.exe user32.dll,LockWorkStation", shell=True)
        elif action == "shutdown":
            subprocess.Popen("shutdown /s /t 5", shell=True)
        elif action == "reboot":
            subprocess.Popen("shutdown /r /t 5", shell=True)
        elif action == "cancel_shutdown":
            subprocess.Popen("shutdown /a", shell=True)
    except Exception as e:
        log.error("系统操作失败: %s", e)

def set_brightness(level):
    try:
        import ctypes
        if sys.platform.startswith("win"):
            try:
                import win32api, win32con
                win32api.SetSystemBrightness(int(level))
                return
            except Exception:
                pass
        subprocess.Popen(f"brightness {int(level)}", shell=True)
    except Exception as e:
        log.error("亮度调节失败: %s", e)

# ---------- 通知 / 弹窗 ----------
def do_notify(title, text):
    try:
        from plyer import notification
        notification.notify(title=title, message=text, timeout=5)
    except Exception:
        try:
            import ctypes
            ctypes.windll.user32.MessageBoxW(0, text, title, 0x40)
        except Exception as e:
            log.error("通知失败: %s", e)

def do_popup(text, duration=5):
    try:
        import tkinter as tk
        from tkinter import ttk
        root = tk.Tk()
        root.overrideredirect(True)
        root.attributes("-topmost", True)
        root.attributes("-alpha", 0.95)
        root.geometry("420x96+{}+{}".format(root.winfo_screenwidth() - 440, 100))
        root.configure(bg="#1c1c1e")
        lab = tk.Label(root, text=text, bg="#1c1c1e", fg="white", font=("Microsoft YaHei", 16),
                       wraplength=400, justify="left")
        lab.pack(fill="both", expand=True, padx=16, pady=12)
        root.lift()
        root.after(int(duration * 1000), root.destroy)
        root.mainloop()
    except Exception as e:
        log.error("弹窗失败: %s", e)

def do_tts(text):
    try:
        import win32com.client
        sp = win32com.client.Dispatch("SAPI.SpVoice")
        sp.Speak(text)
    except Exception:
        try:
            subprocess.Popen(["powershell", "-Command", f"Add-Type -AssemblyName System.Speech; "
                              f"(New-Object System.Speech.Synthesis.SpeechSynthesizer).Speak('{text}')"],
                             creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        except Exception as e:
            log.error("TTS 失败: %s", e)

def do_alarm(delay, text):
    def _t():
        time.sleep(delay)
        do_notify("闹钟提醒", text)
        do_popup(text, 8)
    import threading
    threading.Thread(target=_t, daemon=True).start()
    log.info("闹钟 %s 秒后提醒: %s", delay, text)

# ---------- 涂鸦（透明置顶画布） ----------
def _draw_init():
    try:
        import tkinter as tk
        root = tk.Tk()
        root.overrideredirect(True)
        root.attributes("-topmost", True)
        w, h = root.winfo_screenwidth(), root.winfo_screenheight()
        root.geometry(f"{w}x{h}+0+0")
        if sys.platform.startswith("win"):
            root.update_idletasks()
            root.update()
            root.attributes("-transparentcolor", "#010203")
        else:
            root.attributes("-alpha", 0.3)
        cv = tk.Canvas(root, width=w, height=h, bg="#010203", highlightthickness=0)
        cv.pack()
        return root, cv, w, h
    except Exception as e:
        log.error("涂鸦初始化失败: %s", e)
        return None

def draw_segment(x1, y1, x2, y2, color="#ff3b30"):
    try:
        d = _draw_state
        root, cv, w, h = d["root"], d["cv"], d["w"], d["h"]
        root.deiconify()
        root.lift()
        root.update()
        cv.create_line(x1, y1, x2, y2, fill=color, width=6, capstyle="round")
    except Exception:
        pass

def draw_clear():
    try:
        _draw_state["cv"].delete("all")
    except Exception:
        pass

def _draw_loop():
    import tkinter as tk
    _draw_state["root"].mainloop()

def _draw_start():
    import threading
    global _draw_state
    try:
        _draw_state = _draw_init()
        if _draw_state:
            threading.Thread(target=_draw_loop, daemon=True).start()
    except Exception:
        pass

_draw_state = {}

# ---------- 进程管理 ----------
def list_processes():
    try:
        import psutil
        procs = []
        for p in psutil.process_iter(["pid", "name", "cpu_percent", "memory_percent"]):
            try:
                procs.append({"pid": p.info["pid"], "name": p.info["name"],
                              "cpu": round(p.info["cpu_percent"] or 0, 1),
                              "mem": round(p.info["memory_percent"] or 0, 1)})
            except Exception:
                continue
        procs.sort(key=lambda x: -x["cpu"])
        return procs[:50]
    except ImportError:
        return _list_processes_fallback()

def _list_processes_fallback():
    """psutil 缺失时用 tasklist /bin/csv 兜底"""
    procs = []
    try:
        out = subprocess.check_output("tasklist /fo csv /nh", shell=True, text=True, errors="ignore",
                                      creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        import csv, io
        for row in csv.reader(io.StringIO(out)):
            if len(row) >= 5 and row[1].strip().isdigit():
                procs.append({"pid": int(row[1]), "name": row[0].strip(), "cpu": 0.0, "mem": 0.0})
    except Exception as e:
        log.error("进程列表失败: %s", e)
    return procs[:50]

def kill_process(pid):
    try:
        subprocess.Popen(f"taskkill /f /pid {int(pid)}", shell=True,
                         creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        log.info("已结束进程 %s", pid)
    except Exception as e:
        log.error("结束进程失败: %s", e)

# ---------- 文件 ----------
def list_dir(path):
    p = os.path.expanduser(path or ".")
    if not os.path.exists(p):
        return {"type": "dir_list", "error": "路径不存在"}
    if os.path.isfile(p):
        return {"type": "dir_list", "error": "是文件不是目录", "cwd": os.path.dirname(p)}
    try:
        entries = []
        for n in sorted(os.listdir(p)):
            fp = os.path.join(p, n)
            try:
                entries.append({"name": n, "is_dir": os.path.isdir(fp), "size": os.path.getsize(fp)})
            except Exception:
                continue
        return {"type": "dir_list", "cwd": p, "entries": entries}
    except Exception as e:
        return {"type": "dir_list", "error": str(e)}

def read_file(path):
    p = os.path.expanduser(path)
    if not os.path.exists(p):
        return None
    try:
        with open(p, "rb") as f:
            return f.read()
    except Exception:
        return None

# ---------- 壁纸 ----------
def set_wallpaper(data):
    """data 为 base64 图片内容"""
    try:
        import base64
        import tempfile
        raw = base64.b64decode(data)
        fd, path = tempfile.mkstemp(suffix=".jpg")
        with os.fdopen(fd, "wb") as f:
            f.write(raw)
        if sys.platform.startswith("win"):
            import ctypes
            ctypes.windll.user32.SystemParametersInfoW(20, 0, path, 3)
        log.info("壁纸已更换")
    except Exception as e:
        log.error("壁纸失败: %s", e)

# ---------- 网络状态 ----------
def get_stats():
    return {}

# ---------- 连接 ----------
_ws = None
_loop = None
_send_lock = None

def set_ws(ws, loop):
    global _ws, _loop, _send_lock
    _ws = ws
    _loop = loop
    _send_lock = asyncio.Lock()

async def send_json(ws, data):
    if ws is None or ws.closed:
        return
    async with _send_lock:
        try:
            await ws.send(json.dumps(data, ensure_ascii=False))
        except Exception:
            pass

last_activity = time.time()
def start_presence_listener():
    def _listen():
        global last_activity
        try:
            import ctypes
            class POINT(Structure):
                _fields_ = [("x", c_ulong), ("y", c_ulong)]
            while True:
                pt = POINT()
                ctypes.windll.user32.GetCursorPos(byref(pt))
                now = time.time()
                if (pt.x, pt.y) != getattr(_listen, "last", None):
                    last_activity = now
                    _listen.last = (pt.x, pt.y)
                time.sleep(2)
        except Exception:
            pass
    import threading
    threading.Thread(target=_listen, daemon=True).start()

# ---------- 主循环 ----------
async def listen(ws, state, screen_grab, cam):
    try:
        async for msg in ws:
            if msg.type == "text" or isinstance(msg.data, str):
                try:
                    data = json.loads(msg.data)
                except Exception:
                    continue
                t = data.get("type")
                if t == "set_source":
                    src = data.get("source", "screen")
                    state["source"] = src
                    log.info("切换画面源: %s", src)
                elif t == "mouse":
                    move_mouse(x=data.get("x"), y=data.get("y"), dx=data.get("dx"),
                               dy=data.get("dy"), click=data.get("click"))
                elif t == "key":
                    do_key(data.get("key", ""), data.get("modifiers"), data.get("char"))
                elif t == "exec":
                    do_system(data.get("action", ""))
                elif t == "brightness":
                    set_brightness(data.get("level", 70))
                elif t == "open_path":
                    target = data.get("target", "")
                    try:
                        os.startfile(target)
                    except Exception:
                        subprocess.Popen(f"start {target}", shell=True)
                elif t == "clipboard":
                    try:
                        import pyperclip
                        pyperclip.copy(data.get("text", ""))
                    except ImportError:
                        subprocess.Popen("powershell -Command \"Set-Clipboard -Value '%s'\"" % data.get("text", ""), shell=True)
                elif t == "blackout":
                    do_blackout(data.get("on", True))
                elif t == "wallpaper":
                    set_wallpaper(data.get("data", ""))
                elif t == "snapshot":
                    await take_snapshot(ws, screen_grab, cam, state["source"])
                elif t == "list_dir":
                    await send_json(ws, list_dir(data.get("path", "")))
                elif t == "download":
                    raw = read_file(data.get("path", ""))
                    if raw:
                        import base64
                        await send_json(ws, {"type": "file_data", "name": os.path.basename(data["path"]),
                                             "data": base64.b64encode(raw).decode()})
                    else:
                        await send_json(ws, {"type": "file_data", "error": "读取失败"})
                elif t == "process_list":
                    await send_json(ws, {"type": "process_list", "processes": list_processes()})
                elif t == "kill_process":
                    kill_process(data.get("pid", 0))
                elif t == "proof_interval":
                    state["proof_interval"] = int(data.get("seconds", 0))
                    log.info("自动存证间隔: %s秒", state["proof_interval"])
                elif t == "tts":
                    do_tts(data.get("text", ""))
                elif t == "popup":
                    do_popup(data.get("text", ""), data.get("duration", 5))
                elif t == "alarm":
                    do_alarm(data.get("delay", 60), data.get("text", ""))
                elif t == "draw":
                    draw_segment(data.get("x1",0), data.get("y1",0), data.get("x2",0), data.get("y2",0), data.get("color","#ff3b30"))
                elif t == "draw_clear":
                    draw_clear()
                elif t == "chat":
                    do_popup(f"{data.get('text', '')}", 6)
                elif t == "overlay":
                    do_overlay(data)
                elif t == "stream_ctrl":
                    on = bool(data.get("on", True))
                    state["stream_on"] = on
                    if on:
                        state["stream_event"].set()
                    else:
                        state["stream_event"].clear()
                    log.info("推流%s（观看人数>0时恢复，静默节能）" if on else "推流已暂停（无人观看，静默节能）")
    except Exception:
        pass

async def report_stats(ws, state):
    """定时上报 CPU/内存/网速"""
    try:
        import psutil
    except ImportError:
        log.warning("未安装 psutil，不显示系统状态")
        return
    last = psutil.net_io_counters()
    last_t = time.time()
    start = time.time()
    while True:
        await asyncio.sleep(3 if state.get("stream_on", True) else 60)
        now = time.time()
        net = psutil.net_io_counters()
        dt = now - last_t
        up = (net.bytes_sent - last.bytes_sent) / dt / 1024
        down = (net.bytes_recv - last.bytes_recv) / dt / 1024
        last = net; last_t = now
        try:
            await send_json(ws, {
                "type": "stats",
                "cpu": round(psutil.cpu_percent(interval=0), 1),
                "mem": round(psutil.virtual_memory().percent, 1),
                "up_kbps": round(up, 1),
                "down_kbps": round(down, 1),
                "uptime": int(now - start),
                "active": (now - last_activity) < 30,
            })
        except Exception:
            return

async def stream(ws, state, screen_grab, cam):
    """推流 + 移动侦测；切到屏幕时自动释放摄像头"""
    interval = 1.0 / max(1, CFG["fps"])
    from PIL import Image
    import io as _io
    prev_small = None
    frame_count = 0
    motion_cooldown = 0
    last_src = state["source"]
    last_proof = time.time()
    while True:
        if not state.get("stream_on", True):
            # 无人观看：完全静默，阻塞等待有人观看（零 CPU 占用）
            await state["stream_event"].wait()
            continue
        src = state["source"]
        if src != last_src:
            if src == "screen":
                cam.release()
            last_src = src
        jpg = screen_grab() if src == "screen" else cam.grab()
        if jpg:
            try:
                await send_json(ws, {"type": "frame", "source": src})
                await ws.send(jpg)
                frame_count += 1
                if frame_count % 15 == 0 and src == "camera":
                    try:
                        img = Image.open(_io.BytesIO(jpg)).convert("L").resize((80, 60))
                        small = list(img.getdata())
                        if prev_small:
                            diff = sum(abs(a - b) for a, b in zip(small, prev_small)) / len(small)
                            if diff > CFG["motion_threshold"] and motion_cooldown == 0:
                                await send_json(ws, {"type": "motion", "text": "摄像头检测到画面变化"})
                                motion_cooldown = 10
                        prev_small = small
                    except Exception:
                        pass
                if motion_cooldown > 0:
                    motion_cooldown -= 1
            except Exception:
                return
        proof_sec = state.get("proof_interval", 0)
        if proof_sec > 0 and time.time() - last_proof > proof_sec:
            last_proof = time.time()
            await take_snapshot(ws, screen_grab, cam, state["source"])
        await asyncio.sleep(interval)

async def take_snapshot(ws, screen_grab, cam, source):
    jpg = screen_grab() if source == "screen" else cam.grab()
    if jpg:
        await send_json(ws, {"type": "proof", "ts": int(time.time())})
        await ws.send(jpg)

async def run_agent():
    screen_grab = make_screen_grabber()
    if screen_grab is None:
        log.error("屏幕采集初始化失败，程序退出")
        return
    cam = Camera()
    start_source = CFG["source"]
    if start_source == "camera":
        cam._ensure()
    stream_ev = asyncio.Event()
    stream_ev.set()
    state = {"source": start_source, "stream_on": True, "stream_event": stream_ev}

    uri = CFG["server"]
    sep = "&" if "?" in uri else "?"
    uri = f"{uri}{sep}token={CFG['token']}&role=agent"

    retry = 2
    while True:
        try:
            async with ws_connect(uri, max_size=64 * 1024 * 1024, ping_interval=20, ping_timeout=20) as ws:
                log.info("已连接服务器: %s", CFG["server"])
                retry = 2
                set_ws(ws, asyncio.get_event_loop())
                sw, sh = get_screen_size()
                await send_json(ws, {
                    "type": "hello", "role": "agent",
                    "name": socket.gethostname(),
                    "os": platform.system().lower(),
                    "screen_w": sw, "screen_h": sh,
                })
                start_presence_listener()
                tasks = [
                    asyncio.create_task(listen(ws, state, screen_grab, cam)),
                    asyncio.create_task(stream(ws, state, screen_grab, cam)),
                    asyncio.create_task(report_stats(ws, state)),
                ]
                done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
                for t in pending: t.cancel()
                for t in done: t.result()
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.error("连接异常，%s 秒后重连: %s", retry, e)
            await asyncio.sleep(retry)
            retry = min(retry * 2, 30)

def get_screen_size():
    try:
        import ctypes
        user32 = ctypes.windll.user32
        return user32.GetSystemMetrics(0), user32.GetSystemMetrics(1)
    except Exception:
        return 1920, 1080

def do_overlay(data):
    """屏幕滚动字幕"""
    try:
        import tkinter as tk
        text = data.get("text", "")
        color = data.get("color", "#ffffff")
        bg = data.get("bg", "#ff3b30")
        speed = int(data.get("speed", 8))
        duration = int(data.get("duration", 10))
        pos = data.get("pos", "middle")
        root = tk.Tk()
        root.overrideredirect(True)
        root.attributes("-topmost", True)
        w, h = root.winfo_screenwidth(), root.winfo_screenheight()
        y = 60 if pos == "top" else (h - 90 if pos == "bottom" else h // 2)
        bw = min(max(len(text) * 34, 220), w - 40)
        root.geometry(f"{bw}x70+0+{y}")
        root.configure(bg=bg)
        lab = tk.Label(root, text=text, bg=bg, fg=color, font=("Microsoft YaHei", 22, "bold"))
        lab.pack(expand=True, fill="both")
        def _move():
            if not root.winfo_exists():
                return
            x = lab.winfo_x() - speed
            if x + bw < 0:
                root.destroy()
                return
            lab.place(x=x, y=0)
            root.after(30, _move)
        lab.place(x=w, y=0)
        root.lift()
        root.after(duration * 1000, root.destroy)
        root.after(50, _move)
        root.mainloop()
    except Exception as e:
        log.error("字幕失败: %s", e)

def do_blackout(on):
    try:
        import tkinter as tk
        root = tk.Tk()
        root.overrideredirect(True)
        root.attributes("-topmost", True)
        root.geometry(f"{root.winfo_screenwidth()}x{root.winfo_screenheight()}+0+0")
        root.configure(bg="black")
        root.attributes("-alpha", 0.9 if on else 0.0)
        if on:
            root.after(100, lambda: root.lift())
            root.mainloop()
        else:
            root.destroy()
    except Exception as e:
        log.error("黑屏失败: %s", e)

def main():
    import websockets
    from websockets.asyncio.client import connect as ws_connect
    asyncio.run(run_agent())

if __name__ == "__main__":
    try:
        import websockets
        try:
            from websockets.asyncio.client import connect as ws_connect
        except ImportError:
            from websockets import connect as ws_connect
        asyncio.run(run_agent())
    except KeyboardInterrupt:
        log.info("已退出")
