#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
monitor-agent.py — 手机监控电脑 · 电脑端采集程序 v4
=================================================
功能：
  1. 截屏/摄像头推流
  2. 远程控制：锁屏/关机/重启/打开网址/剪贴板
  3. 黑屏模式
  4. 系统状态上报（CPU/内存/网速）
  5. 鼠标触控板
  6. 文件浏览与下载
  7. 移动侦测报警
  8. 屏幕滚动字幕

依赖：pip install -r requirements.txt
"""
import asyncio
import base64
import io
import json
import logging
import os
import platform
import shutil
import socket
import subprocess
import sys
import threading
import time

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("monitor-agent")

if getattr(sys, "frozen", False):
    # 打包成 exe 后，config.json 放在 exe 同目录
    BASE_DIR = os.path.dirname(os.path.abspath(sys.executable))
else:
    BASE_DIR = os.path.dirname(os.path.abspath(__file__))

DEFAULT_CONFIG = {
    "server": "wss://你的域名/ws",
    "token": "和服务器 config.json 保持一致",
    "source": "screen",
    "fps": 3,
    "camera_index": 0,
    "jpeg_quality": 70,
    "motion_threshold": 8.0,
}


def load_config():
    cfg = dict(DEFAULT_CONFIG)
    path = os.path.join(BASE_DIR, "config.json")
    if os.path.exists(path):
        try:
            with open(path, encoding="utf-8-sig") as f:
                cfg.update(json.load(f))
        except Exception as e:
            log.error("读取 config.json 失败: %s", e)
    return cfg


CFG = load_config()


# ---------- 截屏/摄像头 ----------

def make_screen_grabber():
    try:
        import mss
        from PIL import Image
    except ImportError:
        log.error("缺少依赖，请执行: pip install mss Pillow")
        return None
    sct = mss.MSS() if hasattr(mss, "MSS") else mss.mss()

    def grab():
        try:
            shot = sct.grab(sct.monitors[1])
            from PIL import Image
            img = Image.frombytes("RGB", shot.size, shot.bgra, "raw", "BGRX")
            buf = io.BytesIO()
            img.save(buf, "JPEG", quality=CFG["jpeg_quality"])
            return buf.getvalue()
        except Exception as e:
            log.error("截屏失败: %s", e)
            return None
    return grab


class Camera:
    """摄像头懒加载：切到摄像头才打开，切回屏幕就释放"""
    def __init__(self):
        self.cap = None
        self.cv2 = None

    def _ensure(self):
        if self.cap is not None:
            return True
        try:
            import cv2
            self.cv2 = cv2
            self.cap = cv2.VideoCapture(CFG["camera_index"])
            if not self.cap.isOpened():
                self.cap.release()
                self.cap = None
                log.warning("摄像头打不开")
                return False
            log.info("摄像头已打开")
            return True
        except ImportError:
            log.warning("未安装 opencv-python，摄像头不可用")
            return False

    def release(self):
        if self.cap is not None:
            try:
                self.cap.release()
            except Exception:
                pass
            self.cap = None
            log.info("摄像头已关闭")

    def grab(self):
        if not self._ensure():
            return None
        try:
            ok = False; frame = None
            for _ in range(3):
                ok, frame = self.cap.read()
                if ok: break
            if not ok:
                return None
            ok, buf = self.cv2.imencode(".jpg", frame, [self.cv2.IMWRITE_JPEG_QUALITY, CFG["jpeg_quality"]])
            return buf.tobytes() if ok else None
        except Exception as e:
            log.error("摄像头采集失败: %s", e)
            return None


# ---------- 字幕 & 黑屏（单 Tk 根 + 队列调度，避免跨线程 Tcl 崩溃） ----------

import queue as _queue
_tk_q = _queue.Queue()
_tk_root = None
_overlay_win = None
_blackout_win = None


def _tk_main():
    global _tk_root
    try:
        import tkinter as tk
    except Exception as e:
        log.error("tkinter 不可用: %s", e)
        return
    _tk_root = tk.Tk()
    _tk_root.withdraw()
    def poll():
        try:
            while True:
                fn = _tk_q.get_nowait()
                try: fn()
                except Exception as e: log.error("UI 任务: %s", e)
        except _queue.Empty:
            pass
        _tk_root.after(50, poll)
    poll()
    _tk_root.mainloop()


def _start_tk():
    if not any(t.name == "monitor-tk" for t in threading.enumerate()):
        t = threading.Thread(target=_tk_main, name="monitor-tk", daemon=True)
        t.start()


def _post(fn):
    _start_tk()
    _tk_q.put(fn)


def show_overlay(text, color="#ffffff", bg="#ff3b30", speed=6, duration=10, pos="middle"):
    """pos: top / middle / bottom"""
    def work():
        global _overlay_win
        import tkinter as tk
        if _overlay_win is not None:
            try: _overlay_win.destroy()
            except Exception: pass
            _overlay_win = None
        root = tk.Toplevel(_tk_root)
        _overlay_win = root
        root.overrideredirect(True)
        root.attributes("-topmost", True)
        root.attributes("-alpha", 0.92)
        sw = root.winfo_screenwidth(); sh = root.winfo_screenheight()
        bar_h = 90
        if pos == "top":
            y0 = 0
        elif pos == "bottom":
            y0 = sh - bar_h
        else:
            y0 = (sh - bar_h) // 2
        root.geometry(f"{sw}x{bar_h}+0+{y0}")
        root.config(bg=bg)
        canvas = tk.Canvas(root, width=sw, height=bar_h, bg=bg, highlightthickness=0)
        canvas.pack()
        x = sw
        txt = canvas.create_text(x, bar_h // 2, text=text, fill=color,
                                 font=("Microsoft YaHei", 38, "bold"), anchor="w")
        def step():
            nonlocal x
            x -= int(speed)
            bbox = canvas.bbox(txt)
            if bbox and bbox[2] < 0: x = sw
            canvas.coords(txt, x, bar_h // 2)
            root.after(30, step)
        step()
        def close():
            global _overlay_win
            try: root.destroy()
            except Exception: pass
            _overlay_win = None
        root.after(max(1000, int(float(duration) * 1000)), close)
    _post(work)


def set_blackout(on):
    def work():
        global _blackout_win
        import tkinter as tk
        if on:
            if _blackout_win is not None: return
            root = tk.Toplevel(_tk_root)
            _blackout_win = root
            root.attributes("-fullscreen", True)
            root.attributes("-topmost", True)
            root.overrideredirect(True)
            root.config(bg="black")
            def esc(e):
                global _blackout_win
                try: root.destroy()
                except Exception: pass
                _blackout_win = None
            root.bind("<Escape>", esc)
        else:
            if _blackout_win is not None:
                try: _blackout_win.destroy()
                except Exception: pass
                _blackout_win = None
    _post(work)


# ---------- 系统状态 ----------

# ---------- 聊天窗口 ----------

_chat_win = None
_chat_box = None
_chat_entry = None
_ws_ref = None
_loop = None


def set_ws(ws, loop):
    global _ws_ref, _loop
    _ws_ref = ws
    _loop = loop


def _chat_send(text):
    if not text or not _ws_ref or not _loop:
        return
    try:
        asyncio.run_coroutine_threadsafe(
            send_json(_ws_ref, {"type": "chat", "from": "agent", "text": text}), _loop
        )
    except Exception as e:
        log.error("聊天发送失败: %s", e)


def _ensure_chat_window():
    global _chat_win, _chat_box, _chat_entry
    import tkinter as tk
    if _chat_win is not None:
        return
    root = tk.Toplevel(_tk_root)
    _chat_win = root
    root.title("远程聊天")
    root.attributes("-topmost", True)
    sw = root.winfo_screenwidth(); sh = root.winfo_screenheight()
    root.geometry(f"340x440+{sw-360}+80")
    root.config(bg="#1c2128")
    entry = tk.Entry(root, bg="#21262d", fg="#e8edf2", insertbackground="#e8edf2",
                     font=("Microsoft YaHei", 11))
    entry.pack(side="bottom", fill="x", padx=8, pady=(0, 8))
    entry.bind("<Return>", lambda e: _chat_submit())
    _chat_entry = entry
    box = tk.Text(root, bg="#0d1117", fg="#e8edf2", insertbackground="#e8edf2",
                  font=("Microsoft YaHei", 11), wrap="word", state="disabled")
    box.pack(side="top", fill="both", expand=True, padx=8, pady=(8, 4))
    _chat_box = box
    def on_close():
        global _chat_win
        try: root.destroy()
        except Exception: pass
        _chat_win = None
    root.protocol("WM_DELETE_WINDOW", on_close)


def _chat_submit():
    text = _chat_entry.get().strip()
    if not text: return
    _chat_entry.delete(0, "end")
    _chat_append("我", text)
    _chat_send(text)


def _chat_append(sender, text):
    import tkinter as tk
    def work():
        _ensure_chat_window()
        _chat_box.config(state="normal")
        _chat_box.insert("end", sender + ": " + text + "\n")
        _chat_box.see("end")
        _chat_box.config(state="disabled")
    _post(work)


def handle_chat(text):
    _chat_append("对方", text)


# ---------- 换壁纸 ----------

def set_wallpaper(b64data):
    try:
        raw = base64.b64decode(b64data)
        path = os.path.join(BASE_DIR, "wallpaper_tmp.jpg")
        with open(path, "wb") as f:
            f.write(raw)
        if sys.platform == "win32":
            import ctypes
            SPI_SETDESKWALLPAPER = 20
            SPIF_UPDATEINIFILE = 1
            SPIF_SENDCHANGE = 2
            ctypes.windll.user32.SystemParametersInfoW(
                SPI_SETDESKWALLPAPER, 0, path, SPIF_UPDATEINIFILE | SPIF_SENDCHANGE
            )
        elif sys.platform == "darwin":
            subprocess.run(["osascript", "-e", f'tell application "System Events" to set picture of every desktop to "{path}"'])
        else:
            log.warning("当前系统不支持自动换壁纸")
        log.info("壁纸已更换")
    except Exception as e:
        log.error("换壁纸失败: %s", e)


def get_stats():
    try:
        import psutil
        return {
            "cpu": round(psutil.cpu_percent(interval=0.3), 1),
            "mem": round(psutil.virtual_memory().percent, 1),
        }
    except ImportError:
        return {"cpu": -1, "mem": -1}


# ---------- 鼠标 ----------

_mouse = None
_mouse_err = None
def get_mouse():
    global _mouse, _mouse_err
    if _mouse is None and _mouse_err is None:
        try:
            from pynput.mouse import Controller
            _mouse = Controller()
            log.info("pynput 鼠标初始化成功")
        except Exception as e:
            _mouse_err = str(e)
            log.error("pynput 鼠标初始化失败: %s（请 pip install pynput）", e)
    return _mouse


def get_screen_size():
    try:
        import ctypes
        user32 = ctypes.windll.user32
        return user32.GetSystemMetrics(0), user32.GetSystemMetrics(1)
    except Exception:
        return 1920, 1080


def do_mouse(data):
    """data: {x,y} 绝对定位 / {dx,dy} 相对移动 / {click} left/right/double"""
    m = get_mouse()
    if not m:
        log.warning("鼠标未就绪: %s", _mouse_err)
        return
    try:
        from pynput.mouse import Button
        if "x" in data and "y" in data:
            m.position = (int(data["x"]), int(data["y"]))
        elif data.get("dx") or data.get("dy"):
            pos = m.position
            m.position = (int(pos[0] + data.get("dx", 0)), int(pos[1] + data.get("dy", 0)))
        click = data.get("click", "none")
        if click == "left":
            m.click(Button.left)
        elif click == "right":
            m.click(Button.right)
        elif click == "double":
            m.click(Button.left, 2)
    except Exception as e:
        log.error("鼠标失败: %s", e)


# ---------- 在岗检测 ----------

last_activity = time.time()

def _on_activity(x=None, y=None, button=None, pressed=None, key=None):
    global last_activity
    last_activity = time.time()

def start_presence_listener():
    try:
        from pynput import mouse, keyboard
        mouse.Listener(on_move=_on_activity, on_click=_on_activity).start()
        keyboard.Listener(on_press=_on_activity).start()
        log.info("在岗检测已启动")
    except Exception as e:
        log.warning("在岗检测未启动: %s", e)


def do_key(data):
    """支持: key=特殊键名, modifiers=[ctrl/alt/shift/win], char=普通字符"""
    try:
        from pynput.keyboard import Controller, Key
        kb = Controller()
        # 普通字符输入
        if "char" in data:
            kb.type(data["char"])
            return
        # 特殊键
        key_name = data.get("key", "")
        # Windows 音量键：pynput 的 media 键在部分系统/无音频设备时不生效，改用虚拟键码模拟
        if sys.platform == "win32" and key_name in ("volume_up", "volume_down", "volume_mute"):
            try:
                import ctypes
                vk = {"volume_up": 0xAF, "volume_down": 0xAE, "volume_mute": 0xAD}[key_name]
                user32 = ctypes.windll.user32
                user32.keybd_event(vk, 0, 0, 0)
                user32.keybd_event(vk, 0, 2, 0)
                log.info("音量键(虚拟键码): %s", key_name)
            except Exception as e:
                log.error("音量键失败: %s", e)
            return
        k = {
            "right": Key.right, "left": Key.left,
            "up": Key.up, "down": Key.down,
            "f5": Key.f5, "esc": Key.esc, "space": Key.space,
            "enter": Key.enter, "backspace": Key.backspace,
            "tab": Key.tab, "delete": Key.delete,
            "home": Key.home, "end": Key.end,
            "pageup": Key.page_up, "pagedown": Key.page_down,
            "volume_up": Key.media_volume_up,
            "volume_down": Key.media_volume_down,
            "volume_mute": Key.media_volume_mute,
        }.get(key_name)
        if not k:
            return
        mods = []
        for m in data.get("modifiers", []):
            if m == "ctrl": mods.append(Key.ctrl)
            elif m == "alt": mods.append(Key.alt)
            elif m == "shift": mods.append(Key.shift)
            elif m == "win": mods.append(Key.cmd)
        for m in mods: kb.press(m)
        kb.press(k); kb.release(k)
        for m in reversed(mods): kb.release(m)
        log.info("按键: %s mods=%s", key_name, mods)
    except Exception as e:
        log.error("按键失败: %s", e)


def do_tts(text):
    """TTS 语音播报"""
    try:
        import subprocess
        text = text.replace("'", "''")
        subprocess.Popen([
            "powershell", "-Command",
            f"Add-Type -AssemblyName System.Speech; "
            f"$s=New-Object System.Speech.Synthesis.SpeechSynthesizer; "
            f"$s.Speak('{text}')"
        ])
        log.info("TTS: %s", text[:50])
    except Exception as e:
        log.error("TTS失败: %s", e)


def do_popup(text, duration):
    """屏幕角落弹窗气泡"""
    def work():
        import tkinter as tk
        win = tk.Toplevel(_tk_root)
        win.overrideredirect(True)
        win.attributes("-topmost", True)
        win.attributes("-alpha", 0.97)
        sw = win.winfo_screenwidth()
        win.geometry(f"420x96+{sw-440}+70")
        win.config(bg="#1a1a2e", highlightbackground="#ff3b30", highlightthickness=2)
        tk.Label(win, text=text, bg="#1a1a2e", fg="#ffffff",
                 font=("Microsoft YaHei", 14, "bold"), wraplength=400,
                 justify="left").pack(expand=True, fill="both", padx=14, pady=8)
        win.lift()
        win.update_idletasks()
        win.update()
        win.after(int(duration or 5) * 1000, win.destroy)
    _post(work)
    log.info("弹窗: %s", text[:50])


def do_alarm(delay, text):
    """N秒后闹钟"""
    def _alarm():
        import time as _t
        _t.sleep(max(1, int(delay)))
        do_popup(text or "闹钟提醒", 10)
        try:
            import subprocess
            subprocess.Popen(["powershell", "-Command", "[System.Media.SystemSounds]::Exclamation.Play()"])
        except Exception:
            pass
    threading.Thread(target=_alarm, daemon=True).start()
    log.info("闹钟: %s秒后提醒", delay)


# ---------- 涂鸦透明画布 ----------
_draw_canvas = None
_draw_win = None

def _draw_init():
    global _draw_win, _draw_canvas
    import tkinter as tk
    _draw_win = tk.Toplevel(_tk_root)
    _draw_win.overrideredirect(True)
    _draw_win.attributes("-topmost", True)
    sw = _draw_win.winfo_screenwidth(); sh = _draw_win.winfo_screenheight()
    _draw_win.geometry(f"{sw}x{sh}+0+0")
    _draw_win.config(bg="black")
    _draw_canvas = tk.Canvas(_draw_win, bg="black", highlightthickness=0)
    _draw_canvas.pack(fill="both", expand=True)
    # 关键：先映射窗口再设置透明色，Windows 下 transparentcolor 才生效
    _draw_win.update_idletasks()
    _draw_win.update()
    if sys.platform == "win32":
        _draw_win.attributes("-transparentcolor", "black")
    else:
        _draw_win.attributes("-alpha", 0.3)  # 非 Windows 用半透明兜底
    _draw_win.withdraw()
    log.info("涂鸦画布已创建 %sx%s", sw, sh)

def draw_segment(x1, y1, x2, y2, color="#ff3b30"):
    def work():
        global _draw_win, _draw_canvas
        if _draw_canvas is None:
            _draw_init()
        _draw_win.deiconify()
        _draw_win.lift()
        _draw_win.update_idletasks()
        _draw_win.update()
        _draw_canvas.create_line(x1, y1, x2, y2, fill=color, width=6, capstyle="round", smooth=True)
        log.info("涂鸦线段: (%s,%s)->(%s,%s) %s", x1, y1, x2, y2, color)
    _post(work)

def draw_clear():
    def work():
        if _draw_canvas is not None:
            _draw_canvas.delete("all")
            _draw_win.withdraw()
            _draw_win.update_idletasks()
    _post(work)


def do_brightness(level):
    """调屏幕亮度 0-100"""
    try:
        import subprocess
        level = max(0, min(100, int(level)))
        subprocess.run([
            "powershell", "-Command",
            f"(Get-WmiObject -Namespace root/WMI -Class WmiMonitorBrightnessMethods).WmiSetBrightness(1,{level})"
        ], capture_output=True, timeout=5)
        log.info("亮度: %s", level)
    except Exception as e:
        log.error("亮度失败: %s", e)


def list_processes():
    """列出进程（psutil 不可用时用 tasklist 兜底）"""
    try:
        import psutil
        procs = []
        for p in psutil.process_iter(['pid', 'name', 'cpu_percent', 'memory_percent']):
            try:
                info = p.info
                if info.get('name'):
                    procs.append({
                        'pid': info['pid'],
                        'name': info['name'],
                        'cpu': round(info.get('cpu_percent') or 0, 1),
                        'mem': round(info.get('memory_percent') or 0, 1),
                    })
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass
        procs.sort(key=lambda x: x['mem'], reverse=True)
        return procs[:30]
    except ImportError:
        return _list_processes_fallback()
    except Exception as e:
        log.error("进程列表失败: %s", e)
        return []


def _list_processes_fallback():
    """psutil 缺失时用 Windows tasklist 兜底"""
    try:
        out = subprocess.run(
            ["tasklist", "/FO", "CSV", "/NH"],
            capture_output=True, text=True, timeout=10
        ).stdout
        procs = []
        for line in out.splitlines():
            parts = line.strip().strip('"').split('","')
            if len(parts) >= 2:
                try:
                    procs.append({"pid": int(parts[1]), "name": parts[0],
                                  "cpu": 0.0, "mem": 0.0})
                except ValueError:
                    continue
        procs.sort(key=lambda x: x["pid"])
        return procs[:30]
    except Exception as e:
        log.error("进程列表兜底失败: %s", e)
        return []


def kill_process(pid):
    try:
        import psutil
        p = psutil.Process(int(pid))
        p.terminate()
        log.info("已结束进程 %s", pid)
    except Exception as e:
        log.error("结束进程失败: %s", e)


# ---------- 文件浏览 ----------

def list_dir(path):
    if not path:
        if sys.platform == "win32":
            return {"cwd": "C:\\", "entries": [{"name": "C:\\", "is_dir": True, "size": 0}]}
        path = "/"
    path = os.path.normpath(path)
    if not os.path.isdir(path):
        return {"cwd": path, "entries": [], "error": "目录不存在"}
    entries = []
    try:
        for name in sorted(os.listdir(path)):
            full = os.path.join(path, name)
            try:
                is_dir = os.path.isdir(full)
                size = 0 if is_dir else os.path.getsize(full)
                entries.append({"name": name, "is_dir": is_dir, "size": size})
            except PermissionError:
                continue
    except PermissionError:
        return {"cwd": path, "entries": [], "error": "无权限"}
    return {"cwd": path, "entries": entries}


def read_file_b64(path, max_bytes=8 * 1024 * 1024):
    try:
        size = os.path.getsize(path)
        if size > max_bytes:
            return {"error": f"文件过大 {size//1024//1024}MB，超过 {max_bytes//1024//1024}MB"}
        with open(path, "rb") as f:
            data = f.read()
        return {"name": os.path.basename(path), "size": size,
                "data": base64.b64encode(data).decode()}
    except Exception as e:
        return {"error": str(e)}


# ---------- 动作执行 ----------

def run_action(action):
    try:
        if action == "lock":
            if sys.platform == "win32":
                subprocess.run(["rundll32.exe", "user32.dll,LockWorkStation"])
            elif sys.platform == "darwin":
                subprocess.run(["/System/Library/CoreServices/Menu Extras/User.menu/Contents/Resources/CGSession", "-suspend"])
            else:
                subprocess.run(["loginctl", "lock-session"])
        elif action == "shutdown":
            if sys.platform == "win32":
                subprocess.run(["shutdown", "/s", "/t", "5"])
            else:
                subprocess.run(["shutdown", "-h", "+1"])
        elif action == "reboot":
            if sys.platform == "win32":
                subprocess.run(["shutdown", "/r", "/t", "5"])
            else:
                subprocess.run(["shutdown", "-r", "+1"])
        elif action == "cancel_shutdown":
            if sys.platform == "win32":
                subprocess.run(["shutdown", "/a"])
            else:
                subprocess.run(["shutdown", "-c"])
    except Exception as e:
        log.error("动作 %s 失败: %s", action, e)


def open_path(target):
    target = (target or "").strip()
    if not target:
        return
    # 本地文件/路径直接打开；看起来像网址的自动补 https://
    if not os.path.exists(target):
        if "://" not in target and not target.startswith(("\\", "/", ".")):
            target = "https://" + target
    try:
        if sys.platform == "win32":
            os.startfile(target)
        elif sys.platform == "darwin":
            subprocess.run(["open", target])
        else:
            subprocess.run(["xdg-open", target])
    except Exception as e:
        log.error("打开 %s 失败: %s", target, e)


def set_clipboard(text):
    def work():
        try:
            _tk_root.clipboard_clear()
            _tk_root.clipboard_append(text)
            _tk_root.update()
        except Exception as e:
            log.error("剪贴板失败: %s", e)
    _post(work)


# ---------- WebSocket ----------

try:
    from websockets.asyncio.client import connect as ws_connect
except ImportError:
    from websockets import connect as ws_connect


def send_json(ws, obj):
    return ws.send(json.dumps(obj, ensure_ascii=False))


async def listen(ws, state, screen_grab, cam):
    global _blackout_root
    async for msg in ws:
        if isinstance(msg, str):
            try:
                data = json.loads(msg)
            except Exception:
                continue
            t = data.get("type")
            if t == "set_source":
                state["source"] = data.get("source", "screen")
            elif t == "overlay":
                show_overlay(
                    text=data.get("text", ""),
                    color=data.get("color", "#ffffff"),
                    bg=data.get("bg", "#ff3b30"),
                    speed=data.get("speed", 6),
                    duration=data.get("duration", 10),
                    pos=data.get("pos", "middle"),
                )
            elif t == "exec":
                run_action(data.get("action", ""))
            elif t == "open_path":
                open_path(data.get("target", ""))
            elif t == "clipboard":
                set_clipboard(data.get("text", ""))
            elif t == "blackout":
                set_blackout(bool(data.get("on", False)))
            elif t == "mouse":
                do_mouse(data)
            elif t == "list_dir":
                res = list_dir(data.get("path", ""))
                res["type"] = "dir_list"
                await send_json(ws, res)
            elif t == "download":
                res = read_file_b64(data.get("path", ""))
                res["type"] = "file_data"
                await send_json(ws, res)
            elif t == "chat":
                handle_chat(data.get("text", ""))
            elif t == "wallpaper":
                set_wallpaper(data.get("data", ""))
            elif t == "snapshot":
                await take_snapshot(ws, screen_grab, cam, state["source"])
            elif t == "key":
                do_key(data)
            elif t == "brightness":
                do_brightness(data.get("level", 50))
            elif t == "process_list":
                procs = list_processes()
                await send_json(ws, {"type": "process_list", "processes": procs})
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
            elif t == "stream_ctrl":
                on = bool(data.get("on", True))
                state["stream_on"] = on
                if on:
                    state["stream_event"].set()
                else:
                    state["stream_event"].clear()
                log.info("推流%s（观看人数>0时恢复，节能模式）" if on else "推流已暂停（无人观看，静默节能）")


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
        # 无人观看时降频到 60s 一次（静默节能），有人观看 3s 一次
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
                # 移动检测（每 15 帧一次）
                frame_count += 1
                if frame_count % 15 == 0 and src == "camera":
                    try:
                        img = Image.open(_io.BytesIO(jpg)).convert("L").resize((80, 60))
                        small = list(img.getdata())
                        if prev_small:
                            diff = sum(abs(a - b) for a, b in zip(small, prev_small)) / len(small)
                            if diff > CFG["motion_threshold"] and motion_cooldown == 0:
                                await send_json(ws, {"type": "motion", "text": "摄像头检测到画面变化"})
                                motion_cooldown = 10  # 30 秒冷却
                        prev_small = small
                    except Exception:
                        pass
                if motion_cooldown > 0:
                    motion_cooldown -= 1
            except Exception:
                return
        # 自动存证截图
        proof_sec = state.get("proof_interval", 0)
        if proof_sec > 0 and time.time() - last_proof > proof_sec:
            last_proof = time.time()
            await take_snapshot(ws, screen_grab, cam, state["source"])
        await asyncio.sleep(interval)


async def take_snapshot(ws, screen_grab, cam, source):
    """手机点'存证截图'时，截一张当前画面发给服务器保存"""
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
            async with ws_connect(
                uri, max_size=64 * 1024 * 1024, ping_interval=20, ping_timeout=20
            ) as ws:
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


if __name__ == "__main__":
    try:
        asyncio.run(run_agent())
    except KeyboardInterrupt:
        log.info("已退出")
