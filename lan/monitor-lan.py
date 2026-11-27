#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
monitor-lan.py — 手机监控电脑 · 局域网一体版
=================================================
一台电脑上同时跑「服务器 + 本机采集/控制」，手机连上同一个热点（或局域网）
用浏览器打开电脑 IP 即可远程查看和控制，不需要公网、不需要额外的 Linux 服务器。

特点：
  1. 单文件单进程：aiohttp 服务器 + 本机屏幕采集/摄像头/键鼠控制合体
  2. 首次运行自动生成随机 token 和 config.json，并写出「访问地址.txt」
  3. 纯局域网 HTTP/WS，无外网依赖，手机连电脑热点也能用
  4. 保留原远程中继能力：别的电脑上的 agent 也可以连进来（可选）

用法：
  python monitor-lan.py                 # 按 config.json 启动
  python monitor-lan.py --port 8080     # 临时换端口
  python monitor-lan.py --demo          # 合成画面（无屏幕环境自测）
  python monitor-lan.py --no-agent      # 只跑服务器，不采集本机

依赖：pip install -r requirements.txt
配置：同目录 config.json（首次运行自动创建）
"""
import argparse
import asyncio
import base64
import io
import datetime
import hashlib
import ipaddress
import json
import logging
import os
import platform
import re
import secrets
import shutil
import socket
import ssl
import subprocess
import sys
import zipfile
import threading
import time
from urllib.parse import quote, unquote

import aiohttp
from aiohttp import web, WSMsgType

# Windows 控制台偶尔不是 UTF-8，中文日志别把程序整崩
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass


# ---------- 路径（支持 PyInstaller 单文件 exe） ----------

FROZEN = getattr(sys, "frozen", False)
if FROZEN:
    BASE_DIR = os.path.dirname(os.path.abspath(sys.executable))
    RESOURCE_DIR = getattr(sys, "_MEIPASS", BASE_DIR)
else:
    BASE_DIR = os.path.dirname(os.path.abspath(__file__))
    RESOURCE_DIR = BASE_DIR

CONFIG_PATH = os.path.join(BASE_DIR, "config.json")
EXAMPLE_CONFIG = os.path.join(RESOURCE_DIR, "config.example.json")
STATIC_DIR = os.path.join(RESOURCE_DIR, "static")
PROOF_DIR = os.path.join(BASE_DIR, "proof")
AUDIO_DIR = os.path.join(BASE_DIR, "audio")
ACCESS_FILE = os.path.join(BASE_DIR, "访问地址.txt")
LOG_FILE = os.path.join(BASE_DIR, "monitor-lan.log")
GUARDIAN_LOG = os.path.join(BASE_DIR, "guardian.log")
HEARTBEAT_FILE = os.path.join(BASE_DIR, "monitor-heartbeat.json")
UPDATES_DIR = os.path.join(BASE_DIR, "updates")
STATIC_OVERRIDE_DIR = os.path.join(BASE_DIR, "static")   # 热更新后的页面放这里，优先于 exe 内置页面
APP_VERSION = "4.0"
UPDATE_MAX_BYTES = 250 * 1024 * 1024
UPDATE_PAYLOAD_EXTS = (".exe", ".py")
UPDATE_STATIC_EXTS = (".html", ".js", ".css", ".svg", ".png", ".ico", ".woff2", ".json", ".txt")


# ---------- 日志（pythonw / exe 无窗口时写文件，否则看不到任何报错） ----------

def _setup_logging():
    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")
    if sys.stderr is not None:
        handler = logging.StreamHandler(sys.stderr)
    else:
        try:
            # 超过 2MB 就滚动一次，避免长期后台运行撑爆磁盘
            if os.path.exists(LOG_FILE) and os.path.getsize(LOG_FILE) > 2 * 1024 * 1024:
                try:
                    os.replace(LOG_FILE, LOG_FILE + ".1")
                except Exception:
                    pass
            handler = logging.FileHandler(LOG_FILE, encoding="utf-8")
        except Exception:
            handler = logging.NullHandler()
    handler.setFormatter(fmt)
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.addHandler(handler)


_setup_logging()
log = logging.getLogger("monitor-lan")


# ---------- 配置 ----------

DEFAULT_CONFIG = {
    "host": "0.0.0.0",
    "port": 8000,
    "token": "",              # 留空则首次运行自动生成
    "name": "",               # 设备名，留空用电脑主机名
    "source": "screen",       # 启动时采集源：screen / camera
    "fps": 3,
    "camera_index": 0,
    "jpeg_quality": 70,
    "motion_threshold": 8.0,
    "demo": False,            # 合成画面（调试用）
    "https": False,           # 自签 HTTPS（手机麦克风/按住说话需要）
    "notify": {               # 锁屏推送（局域网一般用不到，默认关闭）
        "channel": "off",     # off / ntfy / serverchan
        "topic": "",
        "server": "https://ntfy.sh",
        "sendkey": "",
    },
}

_PLACEHOLDER_TOKENS = {"", "CHANGE_ME_STRONG_TOKEN", "改成一段很长的随机字符串", "和服务器 config.json 保持一致"}


def _load_json_file(path):
    with open(path, encoding="utf-8-sig") as f:
        return json.load(f)


def save_config(cfg):
    try:
        tmp = CONFIG_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)
        os.replace(tmp, CONFIG_PATH)
    except Exception as e:
        log.error("写入 config.json 失败: %s", e)


def load_config():
    """读取/创建 config.json；token 缺失时自动生成并落盘。"""
    cfg = json.loads(json.dumps(DEFAULT_CONFIG))  # 深拷贝
    created = not os.path.exists(CONFIG_PATH)
    if created:
        # 首次运行：优先用 exe 旁的 config.default.json（可预设端口/token），
        # 没有再用 config.example.json / 内置默认值
        preset = os.path.join(BASE_DIR, "config.default.json")
        src = preset if os.path.exists(preset) else (
            EXAMPLE_CONFIG if os.path.exists(EXAMPLE_CONFIG) else None)
        if src:
            try:
                cfg.update(_load_json_file(src))
                log.info("未找到 config.json，已按 %s 创建", os.path.basename(src))
            except Exception as e:
                log.error("读取预设配置 %s 失败: %s", src, e)
    elif os.path.exists(CONFIG_PATH):
        try:
            user = _load_json_file(CONFIG_PATH)
            notify = user.pop("notify", None)
            if isinstance(notify, dict):
                cfg["notify"].update(notify)
            cfg.update(user)
        except Exception as e:
            log.error("读取 config.json 失败（使用默认配置）: %s", e)
            try:
                shutil.copyfile(CONFIG_PATH, CONFIG_PATH + ".bad")
                log.warning("原文件已备份为 config.json.bad")
            except Exception:
                pass

    need_save = created
    if str(cfg.get("token") or "").strip() in _PLACEHOLDER_TOKENS:
        cfg["token"] = secrets.token_urlsafe(18)
        need_save = True
        log.info("已生成随机访问令牌，并写入 config.json")
    if str(cfg.get("token") or "") == "CHANGE_ME_STRONG_TOKEN":
        cfg["token"] = secrets.token_urlsafe(18)
        need_save = True
    if need_save:
        save_config(cfg)
    return cfg


CONFIG = load_config()
CFG = CONFIG          # 本机采集部分沿用 agent 里的 CFG 命名


def parse_args(argv=None):
    ap = argparse.ArgumentParser(description="手机监控电脑 · 局域网一体版")
    ap.add_argument("--host", help="监听地址，默认取 config.json 的 host（0.0.0.0）")
    ap.add_argument("--port", type=int, help="监听端口，默认取 config.json 的 port（8000）")
    ap.add_argument("--demo", action="store_true", help="使用合成画面，便于无屏幕环境自测")
    ap.add_argument("--no-agent", action="store_true", help="只启动服务器，不采集本机画面")
    ap.add_argument("--https", action="store_true", help="启用自签 HTTPS（麦克风/按住说话需要）")
    ap.add_argument("--probe", action="store_true", help="只做摄像头检测并写出 摄像头检测.txt，然后退出")
    ap.add_argument("--run", action="store_true", help=argparse.SUPPRESS)
    ap.add_argument("--no-guardian", action="store_true", help="不启用守护进程，直接运行（调试用）")
    return ap.parse_args(argv)


ARGS = parse_args()
if ARGS.host:
    CONFIG["host"] = ARGS.host
if ARGS.port and ARGS.port != CONFIG["port"]:
    CONFIG["port"] = ARGS.port
    save_config(CONFIG)   # 写回 config.json，防火墙脚本/访问地址才对得上
    log.info("端口已改为 %s 并写入 config.json（防火墙规则请重新运行 开放防火墙.bat）", ARGS.port)
if ARGS.demo:
    CONFIG["demo"] = True
if ARGS.https:
    CONFIG["https"] = True
NO_AGENT = ARGS.no_agent


# ---------- Agent ⇄ Hub 通道 ----------

def send_json(ws, obj):
    """给手机浏览器（aiohttp WebSocket）发 JSON"""
    return ws.send_str(json.dumps(obj, ensure_ascii=False))


async def link_send(link, obj):
    """给 Agent 发 JSON（本机引擎用）"""
    await link.agent_send_json(obj)


async def run_blocking(func, *args):
    """把会阻塞的同步操作丢到线程池，别卡住推流/控制通道（兼容 Python 3.8）"""
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, func, *args)


class NetLink:
    """远程电脑端 Agent 的通道（原来的 WSS 连接）"""

    def __init__(self, ws):
        self.ws = ws

    async def agent_send_json(self, obj):
        await self.ws.send_str(json.dumps(obj, ensure_ascii=False))

    async def agent_send_bytes(self, data):
        await self.ws.send_bytes(data)

    async def server_send_json(self, obj):
        await self.agent_send_json(obj)

    async def agent_send_frame(self, header, data, important=False):
        await self.agent_send_json(header)
        await self.agent_send_bytes(data)

    async def _recv(self):
        async for msg in self.ws:
            if msg.type == WSMsgType.TEXT:
                yield ("text", msg.data)
            elif msg.type == WSMsgType.BINARY:
                yield ("binary", msg.data)
            elif msg.type == WSMsgType.ERROR:
                return

    # 同一条连接：本端收到的就是对端发的，两个方向共用
    def agent_recv(self):
        return self._recv()

    def server_recv(self):
        return self._recv()


class LocalLink:
    """本机引擎与 Hub 之间的进程内通道（不经过网络）"""

    def __init__(self):
        self._up = asyncio.Queue()     # Agent -> Hub
        self._down = asyncio.Queue()   # Hub -> Agent

    # ---- Agent 端 ----
    async def agent_send_json(self, obj):
        self._up.put_nowait(("text", json.dumps(obj, ensure_ascii=False)))

    async def agent_send_bytes(self, data):
        self._up.put_nowait(("binary", data))

    async def agent_recv(self):
        """Agent 收：Hub 下发的指令"""
        while True:
            yield await self._down.get()

    # ---- Hub 端 ----
    async def server_send_json(self, obj):
        self._down.put_nowait(("text", json.dumps(obj, ensure_ascii=False)))

    async def server_recv(self):
        """Hub 收：Agent 上报的消息"""
        while True:
            yield await self._up.get()

    MAX_PENDING = 12   # 待转发上限：约 4 秒的画面（3fps）

    async def agent_send_frame(self, header, data, important=False):
        """发一帧画面；手机网络慢时丢掉旧帧防止内存无限增长（存证帧不丢）"""
        if not important and self._up.qsize() >= self.MAX_PENDING:
            kept = []
            while True:
                try:
                    kind, payload = self._up.get_nowait()
                except asyncio.QueueEmpty:
                    break
                if kind == "binary":
                    continue
                try:
                    if json.loads(payload).get("type") in ("frame", "proof"):
                        continue
                except Exception:
                    pass
                kept.append((kind, payload))
            for item in kept:
                self._up.put_nowait(item)
        self._up.put_nowait(("text", json.dumps(header, ensure_ascii=False)))
        self._up.put_nowait(("binary", data))


# ---------- Hub（在线设备 / 观看者） ----------

class Hub:
    def __init__(self):
        self.agents = {}          # name -> {"link": link, "os": ..., "screen_w": ..., "screen_h": ...}
        self.viewers = set()      # ws 集合
        self.viewer_choice = {}   # ws -> 选中的设备 name（None 表示跟随默认）
        self.frames = {}          # name -> {source: bytes} 最新一帧
        self.pending_proof = {}   # name -> ts
        self.agent_stream = {}    # name -> 当前观看该设备的 viewer 数（节能统计）
        self.last_frame_ts = {}   # name -> 最近一次收到画面的时间（卡死看门狗）
        self.audio_state = {}     # name -> 最近一次音频播放状态


hub = Hub()


def device_list():
    """生成在线设备列表（带系统信息）"""
    out = []
    for name, a in hub.agents.items():
        out.append({"name": name, "os": a.get("os", "unknown"),
                    "screen_w": a.get("screen_w", 1920), "screen_h": a.get("screen_h", 1080)})
    return out


def check_token(request):
    token = request.query.get("token", "")
    try:
        return secrets.compare_digest(token, str(CONFIG["token"]))
    except Exception:
        return False


async def push_notify(text):
    """把通知推到手机。渠道在 config.json 的 notify 里配置。返回给页面的提示文案。"""
    n = CONFIG.get("notify") or {}
    channel = n.get("channel", "off")
    try:
        if channel == "ntfy":
            topic = n.get("topic", "").strip()
            if not topic:
                return "未配置 ntfy 主题（config.json notify.topic）"
            server = n.get("server", "https://ntfy.sh").rstrip("/")
            async with aiohttp.ClientSession() as s:
                await s.post(f"{server}/{topic}", data=text.encode("utf-8"))
            log.info("已通过 ntfy 推送: %s", text)
            return "已推送到 ntfy，手机请查看通知栏"
        elif channel == "serverchan":
            sendkey = n.get("sendkey", "").strip()
            if not sendkey:
                return "未配置 Server酱 SendKey"
            async with aiohttp.ClientSession() as s:
                await s.post(
                    f"https://sctapi.ftqq.com/{sendkey}.send",
                    data={"title": "电脑监控通知", "desp": text},
                )
            log.info("已通过 Server酱 推送: %s", text)
            return "已推送到微信（Server酱）"
        else:
            log.info("通知(未配置推送渠道): %s", text)
            return "未配置锁屏推送渠道，仅电脑日志可见"
    except Exception as e:
        log.error("通知发送失败: %s", e)
        return f"通知发送失败: {e}"


def viewer_wants(v, agent_name):
    """判断该 viewer 是否要看这个设备"""
    choice = hub.viewer_choice.get(v)
    if choice is None:
        return list(hub.agents)[0] == agent_name if hub.agents else False
    return choice == agent_name


def viewer_targets(agent_name):
    for v in list(hub.viewers):
        if viewer_wants(v, agent_name):
            yield v


def drop_viewer(v):
    hub.viewers.discard(v)
    hub.viewer_choice.pop(v, None)


# 手机页面卡住/断网时 send 可能一直挂着，把整条推流堵死（表现为画面定格、刷新也没用）。
# 每个 viewer 的发送最多等 3 秒，超时就丢弃这个 viewer，保住其他人的画面。
SEND_TIMEOUT = 3.0


async def _close_ws(ws):
    try:
        await asyncio.wait_for(ws.close(), 1.0)
    except Exception:
        pass


async def try_send_json(ws, obj, timeout=SEND_TIMEOUT):
    try:
        await asyncio.wait_for(send_json(ws, obj), timeout)
        return True
    except asyncio.CancelledError:
        raise
    except Exception as e:
        log.warning("手机端发送失败，已丢弃这个连接（%s）", e)
        drop_viewer(ws)
        asyncio.create_task(_close_ws(ws))
        return False


async def try_send_bytes(ws, data, timeout=SEND_TIMEOUT):
    try:
        await asyncio.wait_for(ws.send_bytes(data), timeout)
        return True
    except asyncio.CancelledError:
        raise
    except Exception as e:
        log.warning("手机端收画面卡住（超过 %.1f 秒），已丢弃这个连接（%s）", timeout, e)
        drop_viewer(ws)
        asyncio.create_task(_close_ws(ws))
        return False


async def recompute_streams():
    """节能：统计每个设备的观看人数，0↔非0 翻转时通知 agent 暂停/恢复推流"""
    for name in list(hub.agents):
        count = sum(1 for v in hub.viewers if viewer_wants(v, name))
        prev = hub.agent_stream.get(name, -1)
        if count == prev:
            continue
        hub.agent_stream[name] = count
        try:
            await hub.agents[name]["link"].server_send_json(
                {"type": "stream_ctrl", "on": count > 0})
            log.info("设备 %s 观看人数 %s -> %s，推流%s", name, prev, count,
                     "恢复" if count > 0 else "暂停(节能)")
        except Exception as e:
            log.error("下发推流控制失败 %s: %s", name, e)


async def broadcast_devices():
    """把当前在线设备列表发给所有 viewer"""
    devs = device_list()
    for v in list(hub.viewers):
        await try_send_json(v, {"type": "devices", "list": devs})


async def agent_loop(link):
    """处理电脑端（本机或远程）：注册、把画面转发给 viewer、响应控制指令"""
    name = None
    try:
        async for kind, payload in link.server_recv():
            if kind == "text":
                try:
                    data = json.loads(payload)
                except Exception:
                    continue
                t = data.get("type")
                if t == "hello" and name is None:
                    base = str(data.get("name") or "pc")[:64]
                    name = base
                    suffix = 2
                    while name in hub.agents:
                        name = f"{base}#{suffix}"
                        suffix += 1
                    if name != base:
                        log.warning("设备名 %s 已存在，本次注册为 %s", base, name)
                    hub.agents[name] = {
                        "link": link,
                        "source": "screen",
                        "os": data.get("os", "unknown"),
                        "screen_w": data.get("screen_w", 1920),
                        "screen_h": data.get("screen_h", 1080),
                    }
                    hub.frames.setdefault(name, {})
                    hub.last_frame_ts[name] = time.time()
                    log.info("电脑端上线: %s (%s)", name, data.get("os", "unknown"))
                    await broadcast_devices()
                    await recompute_streams()
                elif name is not None and t == "frame":
                    if name not in hub.agents:
                        continue
                    hub.agents[name]["source"] = data.get("source", "screen")
                    dropped = False
                    for v in viewer_targets(name):
                        if not await try_send_json(
                                v, {"type": "frame", "source": data.get("source"), "agent": name}):
                            dropped = True
                    if dropped:
                        await recompute_streams()
                elif name is not None and t in (
                        "stats", "motion", "dir_list", "file_data", "chat",
                        "process_list", "toast", "audio_state", "camera_probe",
                        "input_lock_state"):
                    if t == "audio_state":
                        hub.audio_state[name] = data
                    dropped = False
                    for v in viewer_targets(name):
                        if not await try_send_json(v, data):
                            dropped = True
                    if dropped:
                        await recompute_streams()
                    if t == "toast":
                        log.info("%s: %s", name, data.get("text", ""))
                elif t == "proof" and name is not None:
                    # 下一帧二进制是存证截图，暂存时间戳
                    hub.pending_proof[name] = data.get("ts", int(time.time()))
                elif t == "notify":
                    await push_notify(data.get("text", "电脑端触发通知"))
            elif kind == "binary" and name is not None:
                # 存证截图？
                ts = hub.pending_proof.pop(name, None)
                if ts:
                    try:
                        d = time.strftime("%Y-%m-%d", time.localtime(ts))
                        sd = os.path.join(PROOF_DIR, name, d)
                        os.makedirs(sd, exist_ok=True)
                        stem = time.strftime("%H-%M-%S", time.localtime(ts))
                        path = os.path.join(sd, stem + ".jpg")
                        n = 1
                        while os.path.exists(path):
                            path = os.path.join(sd, f"{stem}-{n}.jpg")
                            n += 1
                        with open(path, "wb") as f:
                            f.write(payload)
                    except Exception as e:
                        log.error("存证失败: %s", e)
                    continue
                if name not in hub.agents:
                    continue
                source = hub.agents[name]["source"]
                hub.frames[name][source] = payload
                hub.last_frame_ts[name] = time.time()
                dropped = False
                for v in viewer_targets(name):
                    if not await try_send_bytes(v, payload):
                        dropped = True
                if dropped:
                    await recompute_streams()
    finally:
        if name is not None:
            entry = hub.agents.get(name)
            if entry is not None and entry.get("link") is link:
                hub.agents.pop(name, None)
                hub.agent_stream.pop(name, None)
                hub.pending_proof.pop(name, None)
                hub.frames.pop(name, None)
                hub.last_frame_ts.pop(name, None)
                hub.audio_state.pop(name, None)
                # 选中这台设备的 viewer 改回跟随默认
                for v, c in list(hub.viewer_choice.items()):
                    if c == name:
                        hub.viewer_choice[v] = None
                log.info("电脑端离线: %s", name)
                await broadcast_devices()
                await recompute_streams()
            else:
                log.warning("设备 %s 已被新连接接管，跳过旧连接清理", name)


async def send_cached_frame(ws):
    """viewer 刚连上时，补发它当前选中设备「当前采集源」的最新一帧"""
    choice = hub.viewer_choice.get(ws)
    if hub.agents:
        target = choice if choice in hub.agents else list(hub.agents)[0]
        hub.viewer_choice[ws] = target
        source = hub.agents[target].get("source", "screen")
        data = hub.frames.get(target, {}).get(source)
        if data:
            if not await try_send_json(ws, {"type": "frame", "source": source, "agent": target}):
                return
            await try_send_bytes(ws, data)
        audio = hub.audio_state.get(target)
        if audio:
            await try_send_json(ws, audio)


async def viewer_session(request, ws):
    """处理手机端连接：选择设备、接收画面、转发控制指令、触发通知"""
    hub.viewers.add(ws)
    try:
        await try_send_json(ws, {"type": "devices", "list": device_list()})
        await send_cached_frame(ws)
        await recompute_streams()
        async for msg in ws:
            if msg.type == WSMsgType.TEXT:
                try:
                    data = json.loads(msg.data)
                except Exception:
                    continue
                t = data.get("type")
                if t == "select":
                    target = data.get("name")
                    if target in hub.agents:
                        hub.viewer_choice[ws] = target
                        await try_send_json(ws, {"type": "selected", "name": target})
                        await send_cached_frame(ws)
                        await recompute_streams()
                    else:
                        await try_send_json(ws, {"type": "toast", "text": f"设备 {target} 不在线"})
                elif t == "ping":
                    await try_send_json(ws, {"type": "pong", "ts": data.get("ts")})
                elif t == "resync":
                    # 手机端发现画面停住时自动请求：恢复推流 + 补发最新帧
                    await recompute_streams()
                    await send_cached_frame(ws)
                elif t == "set_source":
                    target = data.get("source", "screen")
                    choice = hub.viewer_choice.get(ws)
                    if choice not in hub.agents and hub.agents:
                        choice = list(hub.agents)[0]
                    if choice in hub.agents:
                        try:
                            await hub.agents[choice]["link"].server_send_json(
                                {"type": "set_source", "source": target})
                        except Exception:
                            pass
                elif t == "notify":
                    result = await push_notify(data.get("text", "手机端手动测试通知"))
                    await try_send_json(ws, {"type": "toast", "text": result})
                elif t in ("overlay", "exec", "open_path", "clipboard", "blackout",
                           "mouse", "list_dir", "download", "chat", "wallpaper",
                           "snapshot", "key", "brightness", "process_list",
                           "kill_process", "proof_interval", "tts", "popup",
                           "alarm", "draw", "draw_clear", "audio_play", "audio_pause",
                           "audio_resume", "audio_stop", "audio_volume", "audio_loop",
                           "audio_force_stop", "camera_probe", "input_lock",
                           "wallpaper_restore",
                           "input_lock_query"):
                    choice = hub.viewer_choice.get(ws)
                    if choice not in hub.agents and hub.agents:
                        choice = list(hub.agents)[0]
                    if choice in hub.agents:
                        await hub.agents[choice]["link"].server_send_json(data)
                    else:
                        await try_send_json(ws, {"type": "toast", "text": "没有在线电脑"})
            elif msg.type == WSMsgType.ERROR:
                break
    finally:
        drop_viewer(ws)
        await recompute_streams()


async def ws_handler(request):
    token = request.query.get("token", "")
    role = request.query.get("role", "")
    ok = False
    try:
        ok = secrets.compare_digest(token, str(CONFIG["token"]))
    except Exception:
        ok = False
    if not ok or role not in ("agent", "viewer"):
        log.warning("鉴权失败: role=%s from=%s", role, request.remote)
        return web.Response(status=401, text="unauthorized")
    ws = web.WebSocketResponse(heartbeat=30, max_msg_size=64 * 1024 * 1024)
    await ws.prepare(request)
    if role == "agent":
        await agent_loop(NetLink(ws))
    else:
        await viewer_session(request, ws)
    return ws


async def index(request):
    # 热更新过的页面放在 BASE_DIR/static/，优先使用；没有再回退到 exe 内置页面
    page = os.path.join(STATIC_OVERRIDE_DIR, "index.html")
    if not os.path.isfile(page):
        page = os.path.join(STATIC_DIR, "index.html")
    if not os.path.exists(page):
        return web.Response(status=500, text="static/index.html 缺失，请重新安装/打包")
    return web.FileResponse(page, headers={"Cache-Control": "no-store, must-revalidate",
                                          "Pragma": "no-cache", "Expires": "0"})


async def health(request):
    remote = request.remote or ""
    if remote not in ("127.0.0.1", "::1") and not check_token(request):
        return web.Response(status=401, text="unauthorized")
    return web.json_response({
        "ok": True,
        "agents": len(hub.agents),
        "viewers": len(hub.viewers),
        "devices": list(hub.agents),
        "port": CONFIG["port"],
    })


async def proof_list(request):
    """列出某设备某天的存证截图（需要 token）"""
    if not check_token(request):
        return web.Response(status=401, text="unauthorized")
    agent = request.query.get("agent", "")
    date = request.query.get("date", time.strftime("%Y-%m-%d"))
    if ".." in agent or ".." in date or "/" in agent or "\\" in agent or "/" in date or "\\" in date:
        return web.Response(status=400, text="bad")
    d = os.path.realpath(os.path.join(PROOF_DIR, agent, date))
    root = os.path.realpath(PROOF_DIR)
    if not (d == root or d.startswith(root + os.sep)):
        return web.Response(status=400, text="bad")
    files = []
    token_q = quote(str(CONFIG["token"]), safe="")
    if os.path.isdir(d):
        for fn in sorted(os.listdir(d)):
            if fn.endswith(".jpg"):
                files.append("/proof_file?agent=" + quote(agent, safe="") + "&date=" + quote(date, safe="")
                             + "&f=" + quote(fn, safe="") + "&token=" + token_q)
    return web.json_response({"date": date, "files": files})


async def proof_file(request):
    if not check_token(request):
        return web.Response(status=401, text="unauthorized")
    agent = request.query.get("agent", "")
    date = request.query.get("date", "")
    fn = request.query.get("f", "")
    # 防目录穿越
    for part in (fn, agent, date):
        if ".." in part or "/" in part or "\\" in part:
            return web.Response(status=400, text="bad")
    path = os.path.realpath(os.path.join(PROOF_DIR, agent, date, fn))
    root = os.path.realpath(PROOF_DIR)
    if not (path == root or path.startswith(root + os.sep)):
        return web.Response(status=400, text="bad")
    if not os.path.isfile(path):
        return web.Response(status=404, text="not found")
    return web.FileResponse(path)


# ======================================================================
# 本机采集 / 控制引擎（来自原 agent，改成进程内通道）
# ======================================================================

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
        self.last_fail = 0.0
        self.fail_reason = ""
        self.black_frames = 0
        self.black_warned = False

    def _open(self):
        idx = int(CFG.get("camera_index", 0))
        if idx < 0:                       # -1 = 自动扫描
            for candidate in range(0, 5):
                if self._open_index(candidate):
                    CFG["_camera_index_used"] = candidate
                    return True
            return False
        if self._open_index(idx):
            CFG["_camera_index_used"] = idx
            return True
        # 配置的编号不行就自动试其他编号（不少笔记本内置摄像头是 1 而不是 0）
        for candidate in range(0, 5):
            if candidate == idx:
                continue
            if self._open_index(candidate):
                CFG["_camera_index_used"] = candidate
                return True
        return False

    def _open_index(self, idx):
        cv2 = self.cv2
        # Windows 下默认 MSMF 经常打不开或初始化很慢，优先 DirectShow
        if sys.platform == "win32":
            backends = [("DSHOW", cv2.CAP_DSHOW), ("MSMF", cv2.CAP_MSMF), ("默认", cv2.CAP_ANY)]
        else:
            backends = [("默认", cv2.CAP_ANY)]
        for label, backend in backends:
            cap = None
            try:
                log.info("尝试打开摄像头：编号 %s，后端 %s", idx, label)
                cap = cv2.VideoCapture(idx, backend)
                if cap is None or not cap.isOpened():
                    raise RuntimeError("无法打开设备")
                # 预热：部分摄像头前几帧是黑的/读不到
                ok, frame = False, None
                for _ in range(10):
                    ok, frame = cap.read()
                    if ok:
                        break
                    time.sleep(0.1)
                if not ok:
                    raise RuntimeError("能打开但读不到画面（可能被其他程序占用）")
                self.cap = cap
                log.info("摄像头已打开（后端 %s，编号 %s）", label, idx)
                self.fail_reason = ""
                return True
            except Exception as e:
                try:
                    if cap is not None:
                        cap.release()
                except Exception:
                    pass
                log.warning("摄像头后端 %s 打开失败: %s", label, e)
        return False

    def _ensure(self):
        if self.cap is not None:
            try:
                if self.cap.isOpened():
                    return True
            except Exception:
                pass
            self.release()
        now = time.time()
        if now - self.last_fail < 5:   # 失败后 5 秒内不反复尝试，避免卡顿和刷日志
            return False
        try:
            import cv2
        except ImportError:
            self.last_fail = now
            self.fail_reason = "摄像头组件没装：请安装 opencv-python-headless（新版 exe 已内置）"
            log.warning(self.fail_reason)
            return False
        self.cv2 = cv2
        try:
            ok = self._open()
        except Exception as e:
            ok = False
            log.error("摄像头初始化异常: %s", e)
        if not ok:
            self.last_fail = now
            self.fail_reason = ("摄像头打不开：请检查 1) Windows 设置 → 隐私和安全性 → 相机 → "
                                "允许桌面应用访问相机；2) 关掉微信/QQ/相机等占用程序；"
                                "3) 设备管理器/外接 USB 摄像头是否正常")
            log.warning("摄像头打不开（配置编号 %s）", CFG.get("camera_index", 0))
        return ok

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
            ok, frame = False, None
            for _ in range(3):
                ok, frame = self.cap.read()
                if ok:
                    break
                time.sleep(0.05)
            if not ok:
                log.warning("摄像头读帧失败，释放后重试")
                self.release()
                self.last_fail = time.time()
                return None
            try:
                if frame is not None and frame.mean() < 3:
                    self.black_frames += 1
                    if self.black_frames >= 30 and not self.black_warned:
                        self.black_warned = True
                        log.warning("摄像头已打开但连续收到全黑画面：检查相机隐私设置或被其他程序遮挡")
                else:
                    self.black_frames = 0
            except Exception:
                pass
            ok, buf = self.cv2.imencode(".jpg", frame, [self.cv2.IMWRITE_JPEG_QUALITY, CFG["jpeg_quality"]])
            return buf.tobytes() if ok else None
        except Exception as e:
            log.error("摄像头采集失败: %s", e)
            self.release()
            self.last_fail = time.time()
            return None


# ---------- 字幕 & 黑屏（单 Tk 根 + 队列调度，避免跨线程 Tcl 崩溃） ----------

import queue as _queue
_tk_q = _queue.Queue()
_tk_root = None
_overlay_win = None
_blackout_win = None


def _tk_main():
    global _tk_root
    global _tk_dead
    try:
        import tkinter as tk
    except Exception as e:
        log.error("tkinter 不可用: %s", e)
        _tk_dead = True
        return
    try:
        _tk_root = tk.Tk()
    except Exception as e:
        log.error("图形界面不可用（字幕/弹窗/涂鸦/黑屏会失效）: %s", e)
        _tk_dead = True
        return
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


_tk_lock = threading.Lock()
_tk_dead = False


def _start_tk():
    if _tk_dead:
        return
    with _tk_lock:
        if _tk_dead:
            return
        for t in threading.enumerate():
            if t.name == "monitor-tk" and t.is_alive():
                return
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
            def esc(e=None):
                global _blackout_win
                try: root.destroy()
                except Exception: pass
                _blackout_win = None
            root.bind("<Escape>", esc)
            try:
                root.focus_force()          # 抢焦点，电脑上按 Esc 才能立刻解除
            except Exception:
                pass
            root.after(30 * 60 * 1000, esc)  # 30 分钟兜底，防止人不在被永久黑屏
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
            link_send(_ws_ref, {"type": "chat", "from": "agent", "text": text}), _loop
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

WALLPAPER_FILE = os.path.join(BASE_DIR, "wallpaper.jpg")
WALLPAPER_BACKUP = os.path.join(BASE_DIR, "wallpaper-backup.json")


def _get_win_wallpaper():
    try:
        import ctypes
        buf = ctypes.create_unicode_buffer(2048)
        ctypes.windll.user32.SystemParametersInfoW(0x0073, 2048, buf, 0)   # SPI_GETDESKWALLPAPER
        return buf.value
    except Exception:
        return ""


def _backup_wallpaper_once():
    try:
        if os.path.isfile(WALLPAPER_BACKUP):
            return
        old = _get_win_wallpaper()
        if old and os.path.isfile(old):
            with open(WALLPAPER_BACKUP, "w", encoding="utf-8") as f:
                json.dump({"path": old}, f, ensure_ascii=False)
            log.info("已记录原壁纸路径: %s", old)
    except Exception as e:
        log.warning("记录原壁纸失败: %s", e)


def _set_win_wallpaper(path):
    import ctypes
    ok = ctypes.windll.user32.SystemParametersInfoW(20, 0, path, 3)  # SET + UPDATEINIFILE|SENDCHANGE
    if not ok:
        raise RuntimeError("SystemParametersInfo 失败（文件可能不是有效 JPG）")


def _set_wallpaper_style_fill():
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Control Panel\Desktop", 0,
                            winreg.KEY_SET_VALUE) as k:
            winreg.SetValueEx(k, "WallpaperStyle", 0, winreg.REG_SZ, "10")   # 填充
            winreg.SetValueEx(k, "TileWallpaper", 0, winreg.REG_SZ, "0")
    except Exception:
        pass


def set_wallpaper(b64data):
    """手机传图 → 转 JPEG → 设为电脑壁纸；第一次修改前记住原壁纸，方便恢复"""
    try:
        if not b64data:
            return False
        raw = base64.b64decode(b64data)
        if not raw:
            return False
        try:
            from PIL import Image
            import io as _io
            im = Image.open(_io.BytesIO(raw)).convert("RGB")
            im.thumbnail((3840, 2160))
            im.save(WALLPAPER_FILE, "JPEG", quality=92)
        except Exception:
            with open(WALLPAPER_FILE, "wb") as f:
                f.write(raw)
        if sys.platform == "win32":
            _backup_wallpaper_once()
            _set_win_wallpaper(WALLPAPER_FILE)
            _set_wallpaper_style_fill()
        elif sys.platform == "darwin":
            subprocess.run(["osascript", "-e",
                            f'tell application "System Events" to set picture of every desktop to "{WALLPAPER_FILE}"'])
        else:
            log.warning("当前系统不支持自动换壁纸（仅 Windows/macOS）")
            return False
        log.info("壁纸已更换（%.1f KB）", len(raw) / 1024)
        return True
    except Exception as e:
        log.error("换壁纸失败: %s", e)
        return False


def restore_wallpaper():
    """恢复第一次换壁纸前记录的原壁纸"""
    try:
        if not os.path.isfile(WALLPAPER_BACKUP):
            log.warning("没有记录原壁纸（还没用本程序换过壁纸？）")
            return False
        with open(WALLPAPER_BACKUP, encoding="utf-8") as f:
            path = (json.load(f) or {}).get("path", "")
        if not path or not os.path.isfile(path):
            log.warning("原壁纸文件不存在: %s", path)
            return False
        if sys.platform == "win32":
            _set_win_wallpaper(path)
        elif sys.platform == "darwin":
            subprocess.run(["osascript", "-e",
                            f'tell application "System Events" to set picture of every desktop to "{path}"'])
        else:
            return False
        log.info("已恢复原壁纸: %s", path)
        return True
    except Exception as e:
        log.error("恢复壁纸失败: %s", e)
        return False


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
        mods = []
        for m in data.get("modifiers", []):
            if m == "ctrl": mods.append(Key.ctrl)
            elif m == "alt": mods.append(Key.alt)
            elif m == "shift": mods.append(Key.shift)
            elif m == "win": mods.append(Key.cmd)
        if k is None:
            # 字母/数字等单字符也能按（Ctrl+C / Ctrl+V / Ctrl+Z / Win+D 这些组合键要用）
            if len(key_name) != 1:
                return
            k = key_name
        for m in mods: kb.press(m)
        kb.press(k); kb.release(k)
        for m in reversed(mods): kb.release(m)
        log.info("按键: %s mods=%s", key_name, mods)
    except Exception as e:
        log.error("按键失败: %s", e)


_tts_procs = []


def do_tts(text):
    """TTS 语音播报（记录进程，方便强制停止）"""
    try:
        import subprocess
        text = text.replace("'", "''")
        proc = subprocess.Popen([
            "powershell", "-Command",
            f"Add-Type -AssemblyName System.Speech; "
            f"$s=New-Object System.Speech.Synthesis.SpeechSynthesizer; "
            f"$s.Speak('{text}')"
        ])
        _tts_procs.append(proc)
        _tts_procs[:] = [x for x in _tts_procs if x.poll() is None]
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
        x = max(0, sw - 440)
        win.geometry(f"420x96+{x}+70")
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
        try:
            wait = max(1, int(delay))
        except Exception:
            wait = 60
        _t.sleep(wait)
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


def _draw_alive():
    try:
        return _draw_win is not None and bool(_draw_win.winfo_exists())
    except Exception:
        return False


def _draw_reset():
    global _draw_win, _draw_canvas
    if _draw_win is not None:
        try:
            _draw_win.destroy()
        except Exception:
            pass
    _draw_win = None
    _draw_canvas = None


def _draw_hide():
    """隐藏画布（保留内容）；窗口已经被关掉就直接重置，下次能重建"""
    global _draw_win
    try:
        if _draw_alive():
            _draw_win.withdraw()
            _draw_win.update_idletasks()
        else:
            _draw_reset()
    except Exception:
        _draw_reset()


def _draw_init():
    global _draw_win, _draw_canvas
    import tkinter as tk
    if _draw_win is not None:
        try:
            _draw_win.destroy()
        except Exception:
            pass
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
    _draw_win.bind("<Escape>", lambda e: _draw_hide())
    _draw_win.withdraw()
    log.info("涂鸦画布已创建 %sx%s", sw, sh)


def _draw_stroke(x1, y1, x2, y2, color):
    """在画布上画一段；窗口被关掉过也能自动重建"""
    global _draw_win, _draw_canvas
    if not _draw_alive() or _draw_canvas is None:
        _draw_reset()
        _draw_init()
    _draw_win.deiconify()
    _draw_win.lift()
    _draw_win.update_idletasks()
    _draw_win.update()
    _draw_canvas.create_line(x1, y1, x2, y2, fill=color, width=6, capstyle="round", smooth=True)


def draw_segment(x1, y1, x2, y2, color="#ff3b30"):
    def work():
        try:
            _draw_stroke(x1, y1, x2, y2, color)
            log.info("涂鸦线段: (%s,%s)->(%s,%s) %s", x1, y1, x2, y2, color)
        except Exception as e:
            # 画布被系统/用户关掉后再画会抛 TclError：清掉重来一次
            log.error("涂鸦窗口异常（%s），重建画布", e)
            try:
                _draw_reset()
                _draw_stroke(x1, y1, x2, y2, color)
                log.info("涂鸦画布已重建")
            except Exception as e2:
                log.error("涂鸦重建失败: %s", e2)
    _post(work)


def draw_clear():
    def work():
        try:
            if _draw_canvas is not None and _draw_alive():
                _draw_canvas.delete("all")
                _draw_hide()
            else:
                _draw_reset()
        except Exception as e:
            log.error("清空涂鸦失败: %s", e)
            _draw_reset()
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


# ---------- 输入锁：屏蔽物理鼠标/键盘，远程控制（注入事件）仍然有效 ----------

_input_lock = {"on": False, "thread": None, "thread_id": 0, "timer": None, "state": None}


def _input_lock_on():
    if sys.platform != "win32":
        log.warning("输入锁仅支持 Windows")
        return False
    if _input_lock["on"]:
        return True
    try:
        import ctypes
        from ctypes import wintypes
        user32 = ctypes.windll.user32
        kernel32 = ctypes.windll.kernel32
        WH_KEYBOARD_LL, WH_MOUSE_LL = 13, 14
        LLKHF_INJECTED, LLMHF_INJECTED = 0x10, 0x01
        LRESULT = ctypes.c_ssize_t
        HOOKPROC = ctypes.WINFUNCTYPE(LRESULT, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM)

        class KBDLLHOOKSTRUCT(ctypes.Structure):
            _fields_ = [("vkCode", wintypes.DWORD), ("scanCode", wintypes.DWORD),
                        ("flags", wintypes.DWORD), ("time", wintypes.DWORD),
                        ("dwExtraInfo", ctypes.c_void_p)]

        class POINT(ctypes.Structure):
            _fields_ = [("x", wintypes.LONG), ("y", wintypes.LONG)]

        class MSLLHOOKSTRUCT(ctypes.Structure):
            _fields_ = [("pt", POINT), ("mouseData", wintypes.DWORD),
                        ("flags", wintypes.DWORD), ("time", wintypes.DWORD),
                        ("dwExtraInfo", ctypes.c_void_p)]

        def kb_proc(nCode, wParam, lParam):
            if nCode == 0:
                try:
                    kb = ctypes.cast(lParam, ctypes.POINTER(KBDLLHOOKSTRUCT)).contents
                    if not (kb.flags & LLKHF_INJECTED):
                        return 1          # 物理键盘：吞掉
                except Exception:
                    pass
            return user32.CallNextHookEx(None, nCode, wParam, lParam)

        def ms_proc(nCode, wParam, lParam):
            if nCode == 0:
                try:
                    ms = ctypes.cast(lParam, ctypes.POINTER(MSLLHOOKSTRUCT)).contents
                    if not (ms.flags & LLMHF_INJECTED):
                        return 1          # 物理鼠标：吞掉
                except Exception:
                    pass
            return user32.CallNextHookEx(None, nCode, wParam, lParam)

        kb_cb = HOOKPROC(kb_proc)
        ms_cb = HOOKPROC(ms_proc)
        hk = user32.SetWindowsHookExW(WH_KEYBOARD_LL, kb_cb, None, 0)
        hm = user32.SetWindowsHookExW(WH_MOUSE_LL, ms_cb, None, 0)
        if not hk or not hm:
            log.error("安装输入钩子失败（错误码 %s）", kernel32.GetLastError())
            if hk:
                user32.UnhookWindowsHookEx(hk)
            if hm:
                user32.UnhookWindowsHookEx(hm)
            return False
        st = {"hooks": (hk, hm), "cbs": (kb_cb, ms_cb)}
        _input_lock["state"] = st

        def loop():
            _input_lock["thread_id"] = kernel32.GetCurrentThreadId()
            msg = wintypes.MSG()
            while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
                user32.TranslateMessage(ctypes.byref(msg))
                user32.DispatchMessageW(ctypes.byref(msg))
            user32.UnhookWindowsHookEx(hk)
            user32.UnhookWindowsHookEx(hm)
            log.info("输入钩子已卸载")

        t = threading.Thread(target=loop, daemon=True, name="pcm-input-lock")
        t.start()
        timer = threading.Timer(600, lambda: _input_lock_off(auto=True))
        timer.daemon = True
        timer.start()
        _input_lock.update(on=True, thread=t, timer=timer)
        log.info("已屏蔽外部鼠标/键盘（手机远程控制不受影响；10 分钟自动解除）")
        return True
    except Exception as e:
        log.error("启用输入锁失败: %s", e)
        return False


def _input_lock_off(auto=False):
    tid = _input_lock.get("thread_id")
    if sys.platform == "win32" and tid:
        try:
            import ctypes
            ctypes.windll.user32.PostThreadMessageW(tid, 0x0012, 0, 0)   # WM_QUIT
        except Exception:
            pass
    timer = _input_lock.get("timer")
    if timer is not None:
        try:
            timer.cancel()
        except Exception:
            pass
    was = _input_lock["on"]
    _input_lock.update(on=False, thread=None, thread_id=0, timer=None)
    if was:
        log.info("已解除输入屏蔽%s", "（超时自动）" if auto else "")
    return True


def _input_lock_state():
    return {"type": "input_lock_state", "on": bool(_input_lock["on"]), "auto_unlock_sec": 600}


# ---------- 摄像头诊断 ----------

def _camera_privacy():
    """读 Windows 相机隐私开关（桌面应用）"""
    if sys.platform != "win32":
        return None
    out = {}
    try:
        import winreg
        targets = [
            ("HKCU", winreg.HKEY_CURRENT_USER,
             r"Software\Microsoft\Windows\CurrentVersion\CapabilityAccessManager\ConsentStore\webcam"),
            ("HKLM", winreg.HKEY_LOCAL_MACHINE,
             r"SOFTWARE\Microsoft\Windows\CurrentVersion\CapabilityAccessManager\ConsentStore\webcam"),
            ("HKCU-NonPackaged", winreg.HKEY_CURRENT_USER,
             r"Software\Microsoft\Windows\CurrentVersion\CapabilityAccessManager\ConsentStore\webcam\NonPackaged"),
            ("HKLM-NonPackaged", winreg.HKEY_LOCAL_MACHINE,
             r"SOFTWARE\Microsoft\Windows\CurrentVersion\CapabilityAccessManager\ConsentStore\webcam\NonPackaged"),
        ]
        for name, hive, path in targets:
            try:
                with winreg.OpenKey(hive, path) as k:
                    try:
                        v, _ = winreg.QueryValueEx(k, "Value")
                        out[name] = v
                    except Exception:
                        pass
            except Exception:
                pass
    except Exception as e:
        out["error"] = str(e)
    return out


def camera_probe():
    """返回一份摄像头检测报告，方便定位打不开的原因"""
    report = {"platform": sys.platform, "cv2": None, "count": 0, "results": [],
              "hint": "", "privacy": _camera_privacy()}
    try:
        import cv2
        report["cv2"] = cv2.__version__
    except Exception as e:
        report["error"] = "OpenCV 不可用: %s" % e
        report["hint"] = "更新到最新 exe（内置摄像头组件），或手动 pip install opencv-python-headless"
        return report
    try:
        import cv2
        backends = [("DSHOW", cv2.CAP_DSHOW), ("MSMF", cv2.CAP_MSMF), ("ANY", cv2.CAP_ANY)]
        for idx in range(0, 4):
            for label, be in backends:
                try:
                    cap = cv2.VideoCapture(idx, be)
                    opened = bool(cap and cap.isOpened())
                    ok = False
                    if opened:
                        ok, _frame = cap.read()
                    cap.release() if cap else None
                    report["results"].append({"index": idx, "backend": label, "opened": opened, "read": bool(ok)})
                    if ok:
                        report["count"] += 1
                        report["hint"] = "摄像头可用：编号 %d（把 config.json 的 camera_index 设为 %d）" % (idx, idx)
                        return report
                except Exception as e:
                    report["results"].append({"index": idx, "backend": label, "error": str(e)[:120]})
    except Exception as e:
        report["error"] = str(e)
    if not report["hint"]:
        report["hint"] = ("没有可用摄像头。检查：1) Windows 设置 → 隐私和安全性 → 相机 → "
                          "允许桌面应用访问相机；2) 关掉微信/QQ/相机等占用摄像头的程序；"
                          "3) 设备管理器里摄像头是否正常；4) 台式机外接摄像头换个 USB 口")
    return report


# ---------- 喊话 / 放音（Windows MCI，支持播放/暂停/继续/停止/音量/循环） ----------

_audio_alias = "pcm_audio"
_audio_state = {"playing": False, "paused": False, "name": "", "loop": False, "volume": 80}


def _mci(cmd):
    """执行一条 Windows MCI 命令，返回 (错误码, 输出/错误信息)"""
    try:
        import ctypes
        buf = ctypes.create_unicode_buffer(512)
        r = ctypes.windll.winmm.mciSendStringW(cmd, buf, 511, 0)
        if r != 0:
            err = ctypes.create_unicode_buffer(256)
            try:
                ctypes.windll.winmm.mciGetErrorStringW(r, err, 255)
                return r, err.value
            except Exception:
                return r, buf.value
        return 0, buf.value
    except Exception as e:
        return -1, str(e)


def _audio_open(path):
    _mci("close " + _audio_alias)
    ext = os.path.splitext(path)[1].lower()
    tries = ([" type waveaudio", " type mpegvideo", ""] if ext == ".wav"
             else [" type mpegvideo", "", " type waveaudio"])
    err = ""
    for extra in tries:
        r, err = _mci('open "%s"%s alias %s' % (path, extra, _audio_alias))
        if r == 0:
            return True
    log.error("音频打开失败: %s", err)
    return False


def audio_play(name, loop=False, volume=None):
    if sys.platform != "win32":
        log.warning("音频播放目前只支持 Windows：%s", name)
        return False
    safe = os.path.basename(str(name or ""))
    path = os.path.join(AUDIO_DIR, safe)
    if not safe or not os.path.isfile(path):
        log.warning("音频文件不存在: %s", path)
        return False
    try:
        audio_stop()
        if not _audio_open(path):
            return False
        if volume is not None:
            audio_set_volume(volume)
        r, err = _mci("play " + _audio_alias)
        if r != 0:
            log.error("播放音频失败: %s", err)
            _mci("close " + _audio_alias)
            return False
        _audio_state.update(playing=True, paused=False, name=safe, loop=bool(loop))
        log.info("播放音频: %s（循环=%s，音量=%s%%）", safe, bool(loop), _audio_state["volume"])
        return True
    except Exception as e:
        log.error("播放音频异常: %s", e)
        return False


def audio_pause():
    if sys.platform != "win32" or not _audio_state["playing"] or _audio_state["paused"]:
        return
    r, err = _mci("pause " + _audio_alias)
    if r != 0:
        log.error("暂停音频失败（MCI %s: %s）", r, err)
    _audio_state["paused"] = True
    log.info("音频已暂停")


def audio_resume():
    if sys.platform != "win32" or not _audio_state["playing"] or not _audio_state["paused"]:
        return
    r, err = _mci("resume " + _audio_alias)
    if r != 0:
        log.error("继续音频失败（MCI %s: %s）", r, err)
    _audio_state["paused"] = False
    log.info("音频已继续")


def audio_stop():
    if sys.platform != "win32":
        return False
    r1, e1 = _mci("stop " + _audio_alias)
    r2, e2 = _mci("close " + _audio_alias)
    if r1 != 0 and r2 != 0:
        log.warning("停止音频返回错误（stop %s:%s / close %s:%s），再试 close all",
                    r1, e1, r2, e2)
        _mci("close all")
    if _audio_state["playing"] or _audio_state["name"]:
        log.info("音频已停止")
    _audio_state.update(playing=False, paused=False, name="")
    return True


def _kill_orphan_tts():
    """结束以前残留的 TTS 朗读进程（app 重启后旧 PowerShell 可能还在念）"""
    if sys.platform != "win32":
        return
    try:
        subprocess.run([
            "powershell", "-NoProfile", "-Command",
            "Get-CimInstance Win32_Process -Filter \"Name='powershell.exe'\" | "
            "Where-Object { $_.CommandLine -like '*SpeechSynthesizer*' } | "
            "ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }"
        ], capture_output=True, timeout=15)
    except Exception as e:
        log.warning("清理残留 TTS 失败: %s", e)


def audio_force_stop():
    """强制停止所有由本程序发出的声音：MCI 播放 + TTS 朗读（关不掉时的兜底）"""
    stopped = False
    for cmd in ("stop " + _audio_alias, "close " + _audio_alias, "close all"):
        try:
            r, _err = _mci(cmd)
            if r == 0:
                stopped = True
        except Exception:
            pass
    if _audio_state.get("playing") or _audio_state.get("name"):
        stopped = True
    _audio_state.update(playing=False, paused=False, name="")
    for proc in list(_tts_procs):
        try:
            if proc.poll() is None:
                proc.terminate()
                stopped = True
        except Exception:
            pass
    _tts_procs.clear()
    _kill_orphan_tts()
    if stopped:
        log.info("已强制停止所有声音（MCI + TTS + 残留进程）")
    return stopped


def audio_set_volume(level):
    try:
        level = max(0, min(100, int(level)))
    except Exception:
        return
    _audio_state["volume"] = level
    if sys.platform != "win32":
        return
    r, _ = _mci("setaudio %s volume to %d" % (_audio_alias, level * 10))
    if r != 0:
        # 某些格式不支持 MCI 音量，退回系统波形音量（注意：会影响电脑全局音量）
        try:
            import ctypes
            v = int(level / 100 * 0xFFFF)
            ctypes.windll.winmm.waveOutSetVolume(0, v | (v << 16))
        except Exception:
            pass
    log.info("音频音量: %s%%", level)


async def _send_audio_state(link):
    msg = dict(_audio_state)
    msg["type"] = "audio_state"
    await link_send(link, msg)


# ======================================================================
# 本机引擎：推流 / 状态 / 存证 / 启动
# ======================================================================


def get_screen_size():
    """优先用 mss 的真实像素尺寸（否则高分屏缩放时手机点击坐标会偏）"""
    try:
        import mss
        sct = mss.MSS() if hasattr(mss, "MSS") else mss.mss()
        try:
            mon = sct.monitors[1]
            return int(mon["width"]), int(mon["height"])
        finally:
            try:
                sct.close()
            except Exception:
                pass
    except Exception:
        pass
    try:
        import ctypes
        user32 = ctypes.windll.user32
        return int(user32.GetSystemMetrics(0)), int(user32.GetSystemMetrics(1))
    except Exception:
        return 1920, 1080


def enable_dpi_awareness():
    """让 tkinter / mss / 鼠标坐标使用同一套真实像素"""
    if sys.platform != "win32":
        return
    try:
        import ctypes
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(2)
        except Exception:
            ctypes.windll.user32.SetProcessDPIAware()
    except Exception:
        pass


# ---------- 合成画面（--demo，无屏幕环境自测用） ----------

def make_demo_grabber():
    from PIL import Image, ImageDraw
    state = {"n": 0}

    def grab():
        try:
            w, h = 1280, 720
            img = Image.new("RGB", (w, h), (13, 17, 26))
            d = ImageDraw.Draw(img)
            state["n"] += 1
            d.rectangle([40, 40, w - 40, h - 40], outline=(109, 141, 255), width=3)
            d.text((70, 90), "PHONE-PC MONITOR  ·  DEMO MODE", fill=(109, 141, 255))
            d.text((70, 130), time.strftime("%Y-%m-%d %H:%M:%S"), fill=(230, 236, 245))
            d.text((70, 170), "frame #%d" % state["n"], fill=(140, 160, 190))
            buf = io.BytesIO()
            img.save(buf, "JPEG", quality=int(CFG.get("jpeg_quality", 70)))
            return buf.getvalue()
        except Exception as e:
            log.error("合成画面失败: %s", e)
            return None

    return grab


def make_null_grabber():
    warned = {"done": False}

    def grab():
        if not warned["done"]:
            warned["done"] = True
            log.error("截屏不可用（缺 mss 或无图形界面）。远程控制仍可用；"
                      "要画面请先 pip install mss 后重启，或加 --demo 试试")
        return None

    return grab


async def listen(link, state, screen_grab, cam):
    """接收手机端下发的指令；任何一条指令出错都不影响后续指令"""
    async for kind, payload in link.agent_recv():
        if kind != "text":
            continue
        try:
            data = json.loads(payload)
        except Exception:
            continue
        try:
            await dispatch(link, state, screen_grab, cam, data)
        except Exception as e:
            log.error("处理手机指令失败（%s）: %s", data.get("type"), e)


async def dispatch(link, state, screen_grab, cam, data):
    t = data.get("type")
    if t == "set_source":
        src = data.get("source", "screen")
        state["source"] = src
        if src == "camera":
            await link_send(link, {"type": "toast", "text": "正在打开摄像头（首次可能要几秒）…"})
            ok = await run_blocking(cam._ensure)
            if not ok:
                await link_send(link, {"type": "toast",
                                       "text": cam.fail_reason or "摄像头不可用"})
        elif src == "screen":
            cam.release()   # 无人观看处于暂停状态时切换，也要把摄像头放掉
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
        await run_blocking(run_action, data.get("action", ""))
    elif t == "open_path":
        await run_blocking(open_path, data.get("target", ""))
    elif t == "clipboard":
        set_clipboard(data.get("text", ""))
    elif t == "blackout":
        set_blackout(bool(data.get("on", False)))
    elif t == "mouse":
        do_mouse(data)
    elif t == "list_dir":
        res = await run_blocking(list_dir, data.get("path", ""))
        res["type"] = "dir_list"
        await link_send(link, res)
    elif t == "download":
        res = await run_blocking(read_file_b64, data.get("path", ""))
        res["type"] = "file_data"
        await link_send(link, res)
    elif t == "chat":
        handle_chat(data.get("text", ""))
    elif t == "wallpaper":
        ok = await run_blocking(set_wallpaper, data.get("data", ""))
        await link_send(link, {"type": "toast",
                               "text": "壁纸已更换" if ok else "换壁纸失败（看电脑端日志）"})
    elif t == "wallpaper_restore":
        ok = await run_blocking(restore_wallpaper)
        await link_send(link, {"type": "toast",
                               "text": "已恢复原壁纸" if ok else "恢复失败：没有找到原壁纸记录"})
    elif t == "snapshot":
        await take_snapshot(link, screen_grab, cam, state["source"])
    elif t == "key":
        do_key(data)
    elif t == "brightness":
        await run_blocking(do_brightness, data.get("level", 50))
    elif t == "process_list":
        procs = await run_blocking(list_processes)
        await link_send(link, {"type": "process_list", "processes": procs})
    elif t == "kill_process":
        await run_blocking(kill_process, data.get("pid", 0))
    elif t == "proof_interval":
        try:
            state["proof_interval"] = max(0, int(data.get("seconds", 0)))
        except Exception:
            state["proof_interval"] = 0
        log.info("自动存证间隔: %s秒", state["proof_interval"])
    elif t == "tts":
        await run_blocking(do_tts, data.get("text", ""))
    elif t == "audio_play":
        ok = await run_blocking(audio_play, data.get("name", ""),
                                bool(data.get("loop", False)), data.get("volume"))
        if not ok:
            await link_send(link, {"type": "toast",
                                   "text": "音频播放失败：文件不存在，或电脑端不支持该格式"})
        await _send_audio_state(link)
    elif t == "audio_pause":
        await run_blocking(audio_pause)
        await _send_audio_state(link)
    elif t == "audio_resume":
        await run_blocking(audio_resume)
        await _send_audio_state(link)
    elif t == "audio_stop":
        await run_blocking(audio_stop)
        await _send_audio_state(link)
    elif t == "audio_volume":
        await run_blocking(audio_set_volume, data.get("level", 80))
        await _send_audio_state(link)
    elif t == "audio_loop":
        _audio_state["loop"] = bool(data.get("loop", False))
        await _send_audio_state(link)
    elif t == "audio_force_stop":
        await run_blocking(audio_force_stop)
        await _send_audio_state(link)
    elif t == "input_lock":
        on = bool(data.get("on", False))
        if on:
            await run_blocking(_input_lock_on)
        else:
            await run_blocking(_input_lock_off)
        await link_send(link, _input_lock_state())
    elif t == "input_lock_query":
        await link_send(link, _input_lock_state())
    elif t == "camera_probe":
        await link_send(link, {"type": "toast", "text": "正在检测摄像头…"})
        report = await run_blocking(camera_probe)
        log.info("摄像头检测结果: %s", report.get("hint", report))
        await link_send(link, {"type": "camera_probe", "report": report})
    elif t == "popup":
        do_popup(data.get("text", ""), data.get("duration", 5))
    elif t == "alarm":
        do_alarm(data.get("delay", 60), data.get("text", ""))
    elif t == "draw":
        draw_segment(data.get("x1", 0), data.get("y1", 0), data.get("x2", 0),
                     data.get("y2", 0), data.get("color", "#ff3b30"))
    elif t == "draw_clear":
        draw_clear()
    elif t == "stream_ctrl":
        on = bool(data.get("on", True))
        state["stream_on"] = on
        if on:
            state["stream_event"].set()
            state["stats_event"].set()   # 有人看了，立刻补一次状态
        else:
            state["stream_event"].clear()
        log.info("推流%s", "恢复" if on else "暂停（无人观看，静默节能）")


def _net_io():
    """读网络收发字节数；个别系统/容器读不到就返回 None"""
    try:
        import psutil
        return psutil.net_io_counters()
    except Exception:
        return None


async def report_stats(link, state):
    """定时上报 CPU/内存/网速（读不到的项显示 --，任务本身永不退出）"""
    psutil_missing_logged = False
    last = _net_io()
    last_t = time.time()
    start = time.time()
    while True:
        # 无人观看时降频到 60s 一次（静默节能），有人观看 3s 一次；
        # 有人刚连上来（stats_event）则立刻补一次，手机打开就能看到数据
        interval = 3 if state.get("stream_on", True) else 60
        try:
            await asyncio.wait_for(state["stats_event"].wait(), timeout=interval)
            state["stats_event"].clear()
        except asyncio.TimeoutError:
            pass
        now = time.time()
        up = down = -1.0
        net = _net_io()
        if net is not None:
            if last is not None:
                dt = max(now - last_t, 0.001)
                up = (net.bytes_sent - last.bytes_sent) / dt / 1024
                down = (net.bytes_recv - last.bytes_recv) / dt / 1024
            last = net
        last_t = now
        cpu = mem = -1
        try:
            import psutil
            cpu = round(psutil.cpu_percent(interval=0), 1)
            mem = round(psutil.virtual_memory().percent, 1)
        except ImportError:
            if not psutil_missing_logged:
                psutil_missing_logged = True
                log.warning("未安装 psutil，状态栏 CPU/内存显示 --（不影响其它功能）")
        except Exception:
            pass
        try:
            await link_send(link, {
                "type": "stats",
                "cpu": cpu,
                "mem": mem,
                "up_kbps": round(up, 1),
                "down_kbps": round(down, 1),
                "uptime": int(now - start),
                "active": (now - last_activity) < 30,
            })
        except Exception:
            return


async def stream(link, state, screen_grab, cam):
    """推流 + 移动侦测；切到屏幕时自动释放摄像头"""
    interval = 1.0 / max(1, int(CFG.get("fps", 3)))
    prev_small = None
    frame_count = 0
    motion_cooldown = 0
    last_src = state["source"]
    last_restart = state.get("restart_at", 0)
    last_proof = time.time()
    while True:
        if state.get("restart_at", 0) != last_restart:
            last_restart = state.get("restart_at", 0)
            try:
                cam.release()
            except Exception:
                pass
            if state["source"] == "camera":
                cam._ensure()
            prev_small = None
            frame_count = 0
            log.info("推流已按看门狗要求重新开始")
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
                await link.agent_send_frame({"type": "frame", "source": src}, jpg)
                # 移动检测（每 15 帧一次，只对摄像头）
                frame_count += 1
                if frame_count % 15 == 0 and src == "camera":
                    try:
                        from PIL import Image
                        import io as _io
                        img = Image.open(_io.BytesIO(jpg)).convert("L").resize((80, 60))
                        small = list(img.getdata())
                        if prev_small:
                            diff = sum(abs(a - b) for a, b in zip(small, prev_small)) / len(small)
                            if diff > CFG.get("motion_threshold", 8.0) and motion_cooldown == 0:
                                await link_send(link, {"type": "motion",
                                                       "text": "摄像头检测到画面变化"})
                                motion_cooldown = 10  # 约 3 秒冷却（按帧数算，3fps）
                        prev_small = small
                    except Exception:
                        pass
                if motion_cooldown > 0:
                    motion_cooldown -= 1
            except Exception as e:
                log.error("推流失败: %s", e)
                await asyncio.sleep(1)
                continue
        # 自动存证截图
        proof_sec = state.get("proof_interval", 0)
        if proof_sec > 0 and time.time() - last_proof > proof_sec:
            last_proof = time.time()
            await take_snapshot(link, screen_grab, cam, state["source"])
        await asyncio.sleep(interval)


async def take_snapshot(link, screen_grab, cam, source):
    """手机点「存证截图」时，截一张当前画面交给 Hub 保存"""
    jpg = screen_grab() if source == "screen" else cam.grab()
    if jpg:
        await link.agent_send_frame({"type": "proof", "ts": int(time.time())}, jpg, important=True)


# ---------- 本机地址 / 访问提示 ----------

def local_ipv4():
    """本机所有非回环 IPv4（名字, 地址）"""
    found = []
    try:
        import psutil
        for ifname, addrs in psutil.net_if_addrs().items():
            for a in addrs:
                try:
                    if a.family == socket.AF_INET and a.address and not a.address.startswith("127."):
                        found.append((ifname, a.address))
                except Exception:
                    pass
    except Exception:
        pass
    if not found:
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.settimeout(0.2)
            try:
                s.connect(("8.8.8.8", 80))
                found.append(("", s.getsockname()[0]))
            finally:
                s.close()
        except Exception:
            pass
    # 去重 + 私有地址优先 + 热点网段（Windows 移动热点 192.168.137.x）最优先
    seen = set()
    uniq = []
    for ifname, ip in found:
        if ip in seen or ip.startswith("169.254."):
            continue
        seen.add(ip)
        uniq.append((ifname, ip))

    def rank(item):
        ip = item[1]
        if ip.startswith("192.168.137."):
            return 0
        if ip.startswith(("192.168.", "10.")) or ip.startswith("172."):
            return 1
        return 2

    virtual_hints = ("vethernet", "vmware", "virtualbox", "hyper-v", "wsl",
                     "tailscale", "zerotier", "loopback", "tap-", "tun", "vpn",
                     "docker", "br-", "utun")

    def full_rank(item):
        ifname, ip = item
        r = rank(item)
        name = (ifname or "").lower()
        if any(h in name for h in virtual_hints):
            r += 10
        return r

    uniq.sort(key=full_rank)
    return uniq


def access_urls(ips=None):
    if ips is None:
        ips = local_ipv4()
    port = CONFIG["port"]
    token = quote(str(CONFIG["token"]), safe="")
    scheme = "https" if HTTPS_ENABLED else "http"
    if not ips:
        ips = [("", "127.0.0.1")]
    return [f"{scheme}://{ip}:{port}/?token={token}" for _, ip in ips]


def write_access_file(urls):
    try:
        lines = [
            "手机监控电脑 · 局域网版",
            "",
            "手机和电脑连同一个热点（或同一个局域网）后，",
            "用手机浏览器打开下面任意一个地址（token 已带在地址里，自动登录）：",
            "",
        ]
        lines += ["  " + u for u in urls]
        lines += [
            "",
            "打不开时检查：1) 电脑防火墙是否放行（可运行 开放防火墙.bat）",
            "              2) 手机和电脑是否真的在同一个热点/局域网",
            "              3) config.json 里的端口是否被占用",
            "",
            "更新时间: " + time.strftime("%Y-%m-%d %H:%M:%S"),
        ]
        tmp = ACCESS_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8-sig") as f:
            f.write("\n".join(lines) + "\n")
        os.replace(tmp, ACCESS_FILE)
    except Exception as e:
        log.error("写访问地址文件失败: %s", e)


def print_urls(urls):
    log.info("=" * 62)
    log.info("手机访问地址（手机与电脑在同一热点/局域网）：")
    for u in urls:
        log.info("  %s", u)
    log.info("上面地址里已含 token，打开自动登录；完整列表也写进了「访问地址.txt」")
    log.info("=" * 62)


async def network_watch():
    """网络变化时刷新访问地址（比如启动后才开热点 / 换网）"""
    last = None
    while True:
        try:
            ips = local_ipv4()
            key = tuple(sorted(ip for _, ip in ips))
            if key != last:
                last = key
                urls = access_urls(ips)
                print_urls(urls)
                write_access_file(urls)
        except Exception as e:
            log.error("刷新网络地址失败: %s", e)
        await asyncio.sleep(10)


# ---------- 启动 ----------

async def supervise(name, coro_factory, delay=2.0):
    """后台任务崩了别拖着整个程序：记录原因并自动重启（带退避，避免刷屏）"""
    backoff = delay
    while True:
        started = time.time()
        try:
            await coro_factory()
            log.warning("任务 %s 结束，%.0f 秒后重启", name, backoff)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.error("任务 %s 异常: %s（%.0f 秒后重启）", name, e, backoff)
        if time.time() - started > 30:
            backoff = delay          # 稳定跑过一阵子，重置退避
        await asyncio.sleep(backoff)
        backoff = min(backoff * 2, 30)


ENGINE_STATE = {}
HTTPS_ENABLED = False


def ensure_https_cert():
    """生成/复用自签证书（手机浏览器用麦克风需要安全上下文）"""
    cert_path = os.path.join(BASE_DIR, "cert.pem")
    key_path = os.path.join(BASE_DIR, "key.pem")
    if os.path.isfile(cert_path) and os.path.isfile(key_path):
        return cert_path, key_path
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    now = datetime.datetime.utcnow()
    san = [x509.IPAddress(ipaddress.ip_address("127.0.0.1")),
           x509.DNSName("localhost")]
    try:
        san.append(x509.DNSName(socket.gethostname().lower()))
    except Exception:
        pass
    for _ifname, ip in local_ipv4():
        try:
            san.append(x509.IPAddress(ipaddress.ip_address(ip)))
        except Exception:
            pass
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Phone PC Monitor")])
    cert = (x509.CertificateBuilder()
            .subject_name(subject).issuer_name(subject)
            .public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - datetime.timedelta(days=1))
            .not_valid_after(now + datetime.timedelta(days=3650))
            .add_extension(x509.SubjectAlternativeName(san), critical=False)
            .sign(key, hashes.SHA256()))
    with open(key_path, "wb") as f:
        f.write(key.private_bytes(serialization.Encoding.PEM,
                                  serialization.PrivateFormat.PKCS8,
                                  serialization.NoEncryption()))
    with open(cert_path, "wb") as f:
        f.write(cert.public_bytes(serialization.Encoding.PEM))
    try:
        os.chmod(key_path, 0o600)
    except Exception:
        pass
    log.info("已生成自签 HTTPS 证书（手机首次打开需要点“继续前往”）")
    return cert_path, key_path


async def stream_watchdog():
    """兜底：手机在看着、却超过 15 秒没收到画面时，主动恢复推流"""
    while True:
        await asyncio.sleep(5)
        try:
            now = time.time()
            for name in list(hub.agents):
                if sum(1 for v in hub.viewers if viewer_wants(v, name)) <= 0:
                    continue
                if hub.agent_stream.get(name, 0) <= 0:
                    continue
                last = hub.last_frame_ts.get(name, 0)
                if last and now - last <= 15:
                    continue
                log.warning("设备 %s 超过 %d 秒没有画面，正在恢复推流",
                            name, 0 if not last else int(now - last))
                hub.last_frame_ts[name] = now
                try:
                    await hub.agents[name]["link"].server_send_json(
                        {"type": "stream_ctrl", "on": True})
                except Exception as e:
                    log.error("恢复推流指令失败: %s", e)
                st = ENGINE_STATE.get("state")
                if st is not None:
                    st["stream_event"].set()
                    st["restart_at"] = now      # 让 stream 重新开摄像头/采集
                await broadcast_devices()
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.error("推流看门狗异常: %s", e)


async def audio_monitor(link):
    """跟踪电脑端音频播放状态，状态变化时同步给手机（播完/循环）"""
    last = None
    while True:
        await asyncio.sleep(1.2)
        if sys.platform != "win32":
            continue
        try:
            st = dict(_audio_state)
            if st.get("playing"):
                r, mode = _mci("status %s mode" % _audio_alias)
                if r == 0 and mode.strip() == "stopped" and not st.get("paused"):
                    if st.get("loop"):
                        _mci("seek %s to start" % _audio_alias)
                        _mci("play %s" % _audio_alias)
                    else:
                        _mci("close %s" % _audio_alias)
                        _audio_state.update(playing=False, paused=False, name="")
                        st = dict(_audio_state)
            key = (st.get("playing"), st.get("paused"), st.get("name"), st.get("loop"), st.get("volume"))
            if key != last:
                last = key
                msg = dict(st)
                msg["type"] = "audio_state"
                await link_send(link, msg)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.error("音频状态同步失败: %s", e)


async def start_engine():
    """启动本机采集引擎，返回需要随服务关闭的任务列表"""
    if CFG.get("demo"):
        log.warning("DEMO 模式：使用合成画面，不采集真实屏幕")
        screen_grab = make_demo_grabber()
    else:
        screen_grab = make_screen_grabber()
        if screen_grab is None:
            screen_grab = make_null_grabber()

    cam = Camera()
    state = {
        "source": CFG.get("source", "screen"),
        "stream_on": True,
        "stream_event": asyncio.Event(),
        "stats_event": asyncio.Event(),
        "proof_interval": 0,
    }
    state["stream_event"].set()
    if state["source"] == "camera":
        cam._ensure()

    link = LocalLink()
    set_ws(link, asyncio.get_running_loop())
    name = str(CFG.get("name") or "").strip() or socket.gethostname()
    sw, sh = get_screen_size()

    hello = {
        "type": "hello", "role": "agent", "name": name,
        "os": platform.system().lower(), "screen_w": sw, "screen_h": sh,
    }

    async def local_agent_loop():
        # 每次（重新）进入都先注册，崩溃重启后页面能自动恢复
        await link_send(link, hello)
        await agent_loop(link)

    start_presence_listener()
    tasks = [
        asyncio.create_task(supervise("agent-loop", local_agent_loop), name="agent-loop"),
        asyncio.create_task(supervise("listen", lambda: listen(link, state, screen_grab, cam)),
                             name="agent-listen"),
        asyncio.create_task(supervise("stream", lambda: stream(link, state, screen_grab, cam)),
                             name="agent-stream"),
        asyncio.create_task(supervise("stats", lambda: report_stats(link, state)),
                             name="agent-stats"),
    ]
    ENGINE_STATE["state"] = state
    ENGINE_STATE["cam"] = cam
    tasks.append(asyncio.create_task(supervise("audio", lambda: audio_monitor(link)),
                                     name="agent-audio"))
    log.info("本机采集已启动: %s（%sx%s，屏幕 %s）", name, sw, sh, "摄像头" if state["source"] == "camera" else "屏幕")
    return tasks


AUDIO_EXTS = {".wav", ".mp3", ".wma", ".m4a", ".ogg", ".flac", ".aac"}
AUDIO_MAX_BYTES = 30 * 1024 * 1024


def _safe_audio_name(filename):
    # 浏览器/ multipart 可能把中文文件名做百分号编码，先还原
    base = os.path.basename(unquote(str(filename or "")))
    stem, ext = os.path.splitext(base)
    ext = ext.lower()
    if ext not in AUDIO_EXTS:
        return None
    stem = re.sub(r"[^0-9A-Za-z_\u4e00-\u9fa5-]+", "_", stem).strip("_")[:48] or "audio"
    return stem + ext


async def upload_audio(request):
    """手机上传音频到电脑（限 30MB，token 校验）"""
    if not check_token(request):
        return web.Response(status=401, text="unauthorized")
    try:
        form = await request.post()
    except Exception as e:
        return web.json_response({"error": "上传解析失败: %s" % e}, status=400)
    f = form.get("file")
    if f is None or not getattr(f, "filename", ""):
        return web.json_response({"error": "没有收到音频文件"}, status=400)
    name = _safe_audio_name(f.filename)
    if not name:
        return web.json_response({"error": "格式不支持，请用 mp3 / wav / wma / m4a"}, status=400)
    try:
        raw = f.file.read()
    except Exception as e:
        return web.json_response({"error": "读取上传内容失败: %s" % e}, status=400)
    if not raw:
        return web.json_response({"error": "文件是空的"}, status=400)
    if len(raw) > AUDIO_MAX_BYTES:
        return web.json_response({"error": "文件太大（最大 30MB）"}, status=413)
    os.makedirs(AUDIO_DIR, exist_ok=True)
    stem, ext = os.path.splitext(name)
    path = os.path.join(AUDIO_DIR, name)
    n = 1
    while os.path.exists(path):
        path = os.path.join(AUDIO_DIR, "%s-%d%s" % (stem, n, ext))
        n += 1
    try:
        with open(path, "wb") as fp:
            fp.write(raw)
    except Exception as e:
        return web.json_response({"error": "保存失败: %s" % e}, status=500)
    log.info("收到音频: %s（%.1f KB）", os.path.basename(path), len(raw) / 1024)
    return web.json_response({"ok": True, "name": os.path.basename(path), "size": len(raw)})


async def audio_list(request):
    """已上传的音频列表（token 校验）"""
    if not check_token(request):
        return web.Response(status=401, text="unauthorized")
    os.makedirs(AUDIO_DIR, exist_ok=True)
    items = []
    try:
        for fn in os.listdir(AUDIO_DIR):
            p = os.path.join(AUDIO_DIR, fn)
            if not os.path.isfile(p):
                continue
            try:
                items.append({"name": fn, "size": os.path.getsize(p), "mtime": os.path.getmtime(p)})
            except Exception:
                pass
    except Exception as e:
        log.error("音频列表失败: %s", e)
    items.sort(key=lambda x: -x["mtime"])
    return web.json_response({"files": items[:50],
                              "state": dict(_audio_state)})


# ---------- 热更新：手机浏览器上传更新包，电脑端自己替换/重启 ----------

UPDATE_MANIFEST_NAMES = ("update-manifest.json", "manifest.json")


def _sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _runtime_mode():
    return "exe" if FROZEN else "source"


def _read_pending_update():
    path = os.path.join(UPDATES_DIR, "pending.json")
    if not os.path.isfile(path):
        return None
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def _write_pending_update(data):
    os.makedirs(UPDATES_DIR, exist_ok=True)
    tmp = os.path.join(UPDATES_DIR, "pending.json.tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)
    os.replace(tmp, os.path.join(UPDATES_DIR, "pending.json"))


def _clear_pending_update(keep_payload=False, keep_static=False):
    if not keep_static:
        try:
            shutil.rmtree(os.path.join(UPDATES_DIR, "static"), ignore_errors=True)
        except Exception:
            pass
    names = ["pending.json", "payload.tmp"]
    if not keep_payload:
        names.append("payload.bin")
    for name in names:
        try:
            os.remove(os.path.join(UPDATES_DIR, name))
        except Exception:
            pass


def _cleanup_updates_on_start():
    """启动时清掉上次没被用掉的更新文件（重启脚本已经用完）"""
    try:
        if not os.path.isfile(os.path.join(UPDATES_DIR, "pending.json")):
            shutil.rmtree(os.path.join(UPDATES_DIR, "static"), ignore_errors=True)
            for name in ("payload.bin", "upload-tmp.zip"):
                p = os.path.join(UPDATES_DIR, name)
                if os.path.isfile(p):
                    os.remove(p)
    except Exception:
        pass


def _safe_member(name):
    """拒绝绝对路径 / 目录穿越 / 盘符，返回安全相对路径"""
    name = (name or "").replace("\\", "/")
    if not name or name.startswith("/") or ":" in name.split("/")[0]:
        return None
    parts = [p for p in name.split("/") if p not in ("", ".")]
    if not parts or any(p == ".." for p in parts):
        return None
    return "/".join(parts)


def _zip_read_manifest(zf):
    names = [n for n in zf.namelist() if not n.endswith("/")]
    for want in UPDATE_MANIFEST_NAMES:
        for n in names:
            if n == want or n.endswith("/" + want):
                try:
                    return json.loads(zf.read(n).decode("utf-8-sig"))
                except Exception:
                    return None
    return None


def _zip_find_payload(zf, wanted):
    base = os.path.basename(str(wanted or ""))
    for n in zf.namelist():
        if n.endswith("/"):
            continue
        if os.path.basename(n) == base:
            safe = _safe_member(n)
            if safe:
                return safe
    return None


def _is_writable_dir(path):
    try:
        os.makedirs(path, exist_ok=True)
        probe = os.path.join(path, ".pcm_write_test")
        with open(probe, "w") as f:
            f.write("x")
        os.remove(probe)
        return True
    except Exception:
        return False


async def update_info(request):
    """当前版本 + 待应用更新（token 校验）"""
    if not check_token(request):
        return web.Response(status=401, text="unauthorized")
    pending = _read_pending_update()
    if pending:
        pending = {k: pending.get(k) for k in
                   ("version", "file", "sha256", "size", "notes", "uploaded_at",
                    "has_payload", "static_files")}
    return web.json_response({
        "version": APP_VERSION,
        "mode": _runtime_mode(),
        "exe": os.path.abspath(sys.executable) if FROZEN else os.path.abspath(__file__),
        "can_update": _is_writable_dir(BASE_DIR),
        "pending": pending,
    })


async def upload_update(request):
    """接收更新包（zip：update-manifest.json + exe/py，可选 static/）"""
    if not check_token(request):
        return web.Response(status=401, text="unauthorized")
    try:
        form = await request.post()
    except Exception as e:
        return web.json_response({"error": "上传解析失败: %s" % e}, status=400)
    f = form.get("file")
    if f is None or not getattr(f, "filename", ""):
        return web.json_response({"error": "没有收到更新包"}, status=400)
    try:
        raw = f.file.read()
    except Exception as e:
        return web.json_response({"error": "读取上传内容失败: %s" % e}, status=400)
    if not raw:
        return web.json_response({"error": "更新包是空的"}, status=400)
    if len(raw) > UPDATE_MAX_BYTES:
        return web.json_response({"error": "更新包太大（最大 %d MB）" % (UPDATE_MAX_BYTES // 1048576)},
                                 status=413)
    if not raw.startswith(b"PK"):
        return web.json_response({"error": "更新包必须是 zip 文件（从 Releases 下载的 zip）"}, status=400)

    os.makedirs(UPDATES_DIR, exist_ok=True)
    tmp_zip = os.path.join(UPDATES_DIR, "upload-tmp.zip")
    with open(tmp_zip, "wb") as fp:
        fp.write(raw)
    try:
        with zipfile.ZipFile(tmp_zip) as zf:
            manifest = _zip_read_manifest(zf)
            if not isinstance(manifest, dict) or not manifest.get("version"):
                return web.json_response(
                    {"error": "更新包里缺少 update-manifest.json（或没有 version）"}, status=400)
            version = str(manifest.get("version"))[:64]
            wanted = str(manifest.get("file") or "")
            payload_name = _zip_find_payload(zf, wanted) if wanted else None
            if wanted and not payload_name:
                return web.json_response(
                    {"error": "更新包里找不到清单指定的文件：%s" % wanted}, status=400)
            payload_ext = os.path.splitext(payload_name)[1].lower() if payload_name else ""
            if payload_name and payload_ext not in UPDATE_PAYLOAD_EXTS:
                return web.json_response({"error": "更新包里的程序文件类型不支持：%s" % payload_ext},
                                         status=400)
            if payload_name and _runtime_mode() == "exe" and payload_ext != ".exe":
                return web.json_response(
                    {"error": "当前是 exe 版，请上传包含 phone-pc-monitor.exe 的更新包"}, status=400)
            if payload_name and _runtime_mode() == "source" and payload_ext != ".py":
                return web.json_response(
                    {"error": "当前是源码版，请上传包含 monitor-lan.py 的更新包"}, status=400)

            _clear_pending_update()
            payload_size = 0
            has_payload = False
            if payload_name:
                out = os.path.join(UPDATES_DIR, "payload.bin")
                with zf.open(payload_name) as src, open(out, "wb") as dst:
                    while True:
                        chunk = src.read(1024 * 1024)
                        if not chunk:
                            break
                        dst.write(chunk)
                        payload_size += len(chunk)
                has_payload = True
                log.info("更新包已接收：%s → payload.bin（%.1f MB）", payload_name, payload_size / 1048576)

            # 静态页面（有就热替换，不用改 exe）
            static_files = []
            for n in zf.namelist():
                if n.endswith("/") or not n.startswith("static/"):
                    continue
                rel = _safe_member(n[len("static/"):])
                if not rel or os.path.splitext(rel)[1].lower() not in UPDATE_STATIC_EXTS:
                    continue
                dst = os.path.join(UPDATES_DIR, "static", rel)
                os.makedirs(os.path.dirname(dst), exist_ok=True)
                with zf.open(n) as src, open(dst, "wb") as out:
                    out.write(src.read())
                static_files.append(rel)

            digest = _sha256_file(os.path.join(UPDATES_DIR, "payload.bin")) if has_payload else ""
            want_hash = str(manifest.get("sha256") or "").strip().lower()
            if has_payload and want_hash and digest != want_hash:
                _clear_pending_update()
                return web.json_response(
                    {"error": "更新包校验失败：sha256 不一致（文件可能下载不完整）", "expect": want_hash,
                     "actual": digest}, status=400)

            info = {
                "version": version,
                "notes": str(manifest.get("notes") or "")[:500],
                "file": payload_name or "",
                "sha256": digest or want_hash,
                "size": payload_size,
                "has_payload": has_payload,
                "static_files": static_files,
                "uploaded_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            }
            _write_pending_update(info)
            log.info("更新包已就绪：版本 %s（程序 %s，静态文件 %d 个）",
                     version, has_payload, len(static_files))
            return web.json_response({"ok": True, "pending": info, "current_version": APP_VERSION})
    except zipfile.BadZipFile:
        return web.json_response({"error": "zip 文件损坏"}, status=400)
    except Exception as e:
        log.error("处理更新包失败: %s", e)
        return web.json_response({"error": "处理更新包失败: %s" % e}, status=500)
    finally:
        try:
            os.remove(tmp_zip)
        except Exception:
            pass


def _restart_after_update():
    """响应发出后重启：exe 用 PowerShell 等本进程退出再替换（备份/校验/回滚）；源码版直接重载"""
    if FROZEN:
        target = os.path.abspath(sys.executable)
        payload = os.path.join(UPDATES_DIR, "payload.bin")
        backup = os.path.join(os.path.dirname(target), "phone-pc-monitor.old.exe")
        log_path = os.path.join(UPDATES_DIR, "apply-update.log")
        ps1 = os.path.join(UPDATES_DIR, "apply-update.ps1")
        port = int(CONFIG.get("port", 8000))
        contents = """$ErrorActionPreference = 'SilentlyContinue'
$logPath = '%s'
function Log($m) { Add-Content -LiteralPath $logPath -Value ((Get-Date -Format o) + ' ' + $m) }
$pidToWait = %d
$target = '%s'
$payload = '%s'
$backup = '%s'
$port = %d
$updatesDir = Split-Path -Parent $logPath
$staticSrc = Join-Path $updatesDir 'static'
$baseDir = Split-Path -Parent $target
Log ('update start; waiting pid ' + $pidToWait)
for ($i = 0; $i -lt 240; $i++) {
  if (-not (Get-Process -Id $pidToWait -ErrorAction SilentlyContinue)) { break }
  Start-Sleep -Milliseconds 500
}
Log 'old process gone'
Get-Process | Where-Object { $_.Path -eq $target } | Stop-Process -Force -ErrorAction SilentlyContinue
Start-Sleep -Milliseconds 500
if (Test-Path -LiteralPath $target) {
  for ($i = 0; $i -lt 5; $i++) {
    try { Copy-Item -LiteralPath $target -Destination $backup -Force -ErrorAction Stop; break }
    catch { Start-Sleep -Seconds 1 }
  }
  Log 'backup done'
}
$want = (Get-FileHash -LiteralPath $payload -Algorithm SHA256).Hash.ToLower()
$ok = $false
for ($i = 0; $i -lt 20; $i++) {
  try {
    Copy-Item -LiteralPath $payload -Destination $target -Force -ErrorAction Stop
    $got = (Get-FileHash -LiteralPath $target -Algorithm SHA256).Hash.ToLower()
    if ($got -eq $want) { $ok = $true; break }
    Log 'hash mismatch after copy'
  } catch { }
  Start-Sleep -Seconds 1
}
if (-not $ok) {
  Log 'copy/verify failed; rollback'
  if (Test-Path -LiteralPath $backup) { Copy-Item -LiteralPath $backup -Destination $target -Force }
  Start-Process -FilePath $target -WorkingDirectory (Split-Path -Parent $target)
  Remove-Item -LiteralPath $PSCommandPath -Force -ErrorAction SilentlyContinue
  exit 1
}
if (Test-Path -LiteralPath $staticSrc) {
  try {
    New-Item -ItemType Directory -Force -Path (Join-Path $baseDir 'static') | Out-Null
    Copy-Item -Path (Join-Path $staticSrc '*') -Destination (Join-Path $baseDir 'static') -Recurse -Force -ErrorAction Stop
    Log 'static files copied'
  } catch { Log ('static copy failed: ' + $_.Exception.Message) }
  Remove-Item -LiteralPath $staticSrc -Recurse -Force -ErrorAction SilentlyContinue
}
Log 'copied and verified; starting new version'
Start-Process -FilePath $target -WorkingDirectory (Split-Path -Parent $target)
$up = $false
for ($i = 0; $i -lt 60; $i++) {
  Start-Sleep -Milliseconds 500
  try {
    $c = New-Object System.Net.Sockets.TcpClient
    $c.Connect('127.0.0.1', $port)
    if ($c.Connected) { $up = $true; $c.Close(); break }
    $c.Close()
  } catch { }
}
if (-not $up) {
  Log 'new version did not listen; rollback'
  Get-Process | Where-Object { $_.Path -eq $target } | Stop-Process -Force -ErrorAction SilentlyContinue
  Start-Sleep -Seconds 1
  if (Test-Path -LiteralPath $backup) { Copy-Item -LiteralPath $backup -Destination $target -Force }
  Start-Process -FilePath $target -WorkingDirectory (Split-Path -Parent $target)
  Log 'rollback done'
} else {
  Log 'update success'
}
Remove-Item -LiteralPath $PSCommandPath -Force -ErrorAction SilentlyContinue
""" % (log_path.replace("'", "''"), os.getpid(), target.replace("'", "''"),
       payload.replace("'", "''"), backup.replace("'", "''"), port)
        with open(ps1, "w", encoding="utf-8-sig") as f:
            f.write(contents)
        flags = 0x08000000 if sys.platform == "win32" else 0
        log.info("热更新：等待进程退出后替换 %s（有备份回滚，日志 %s）", target, log_path)
        subprocess.Popen(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
                          "-WindowStyle", "Hidden", "-File", ps1],
                         creationflags=flags, close_fds=True)
        time.sleep(0.5)
        log.info("热更新：退出旧进程，交给更新脚本替换文件")
        os._exit(42 if os.environ.get("PCM_GUARDIAN") == "1" else 0)
    else:
        log.info("热更新：重新加载源码")
        time.sleep(0.2)
        os.execv(sys.executable, [sys.executable, os.path.abspath(__file__)] + sys.argv[1:])


async def apply_update(request):
    """应用已上传的更新包（先热替换页面；有程序文件则重启）"""
    if not check_token(request):
        return web.Response(status=401, text="unauthorized")
    pending = _read_pending_update()
    if not pending:
        return web.json_response({"error": "没有待应用的更新包，请先上传"}, status=400)

    applied_static = []
    defer_static = False
    static_root = os.path.join(UPDATES_DIR, "static")
    if pending.get("has_payload") and FROZEN and not (os.environ.get("PCM_NO_RESTART") == "1"):
        # exe 升级：先别动正在运行的页面文件，等更新脚本在退出后一起拷贝
        defer_static = bool(pending.get("static_files"))
        if defer_static:
            log.info("热更新：页面文件将在重启后应用（%d 个）", len(pending["static_files"]))
    else:
        for rel in pending.get("static_files") or []:
            src = os.path.join(static_root, rel)
            if not os.path.isfile(src):
                continue
            dst = os.path.join(STATIC_OVERRIDE_DIR, rel)
            try:
                os.makedirs(os.path.dirname(dst), exist_ok=True)
                shutil.copyfile(src, dst)
                applied_static.append(rel)
            except Exception as e:
                log.error("热更新页面文件失败 %s: %s", rel, e)
        if applied_static:
            log.info("热更新：页面文件已替换 %s", applied_static)

    restart = False
    keep_payload = False
    if pending.get("has_payload"):
        payload = os.path.join(UPDATES_DIR, "payload.bin")
        if not os.path.isfile(payload):
            return web.json_response({"error": "更新文件丢失，请重新上传"}, status=400)
        digest = _sha256_file(payload)
        if pending.get("sha256") and digest != pending["sha256"]:
            return web.json_response({"error": "校验失败：更新文件被改动，请重新上传"}, status=400)
        if not (os.environ.get("PCM_NO_RESTART") == "1"):
            if FROZEN:
                if not _is_writable_dir(os.path.dirname(os.path.abspath(sys.executable))):
                    return web.json_response(
                        {"error": "程序目录没有写权限，无法自动替换；请把程序放到可写目录或用管理员运行"},
                        status=400)
                keep_payload = True     # 重启脚本要等进程退出后再用这个文件
            else:
                # 源码模式：先就地替换当前脚本，再重启进程
                try:
                    shutil.copyfile(payload, os.path.abspath(__file__))
                except Exception as e:
                    return web.json_response({"error": "替换源码失败: %s" % e}, status=500)
            restart = True
            asyncio.get_running_loop().call_later(0.8, _restart_after_update)
            log.info("热更新：版本 %s 即将重启生效", pending.get("version"))
        else:
            log.warning("PCM_NO_RESTART=1：只做校验，不真正重启")
    elif not applied_static:
        return web.json_response({"error": "更新包里没有可应用的内容"}, status=400)

    _clear_pending_update(keep_payload=keep_payload, keep_static=defer_static)
    return web.json_response({
        "ok": True,
        "version": pending.get("version"),
        "static": applied_static,
        "restart": restart,
        "message": ("正在重启并应用更新，几秒后手机会自动重连" if restart else "页面已更新，刷新即可"),
    })


async def discard_update(request):
    if not check_token(request):
        return web.Response(status=401, text="unauthorized")
    _clear_pending_update()
    return web.json_response({"ok": True})


_instance_mutex = None


def acquire_single_instance():
    """Windows 下用命名互斥量判断是否已有实例；重复启动静默退出，不弹任何窗"""
    global _instance_mutex
    if sys.platform != "win32":
        return True
    try:
        import ctypes
        kernel32 = ctypes.windll.kernel32
        _instance_mutex = kernel32.CreateMutexW(None, False, "PhonePCMonitor_SingleInstance")
        if kernel32.GetLastError() == 183:      # ERROR_ALREADY_EXISTS
            log.warning("检测到已有实例在运行，本次启动直接退出（不弹窗）")
            return False
    except Exception as e:
        log.warning("单实例检测失败（忽略）: %s", e)
    return True


def _mutex_exists():
    if sys.platform != "win32":
        return False
    try:
        import ctypes
        kernel32 = ctypes.windll.kernel32
        h = kernel32.OpenMutexW(0x00100000, False, "PhonePCMonitor_SingleInstance")
        if h:
            kernel32.CloseHandle(h)
            return True
    except Exception:
        pass
    return False


def _guardian_logger():
    logg = logging.getLogger("guardian")
    if logg.handlers:
        return logg
    logg.setLevel(logging.INFO)
    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")
    try:
        if os.path.exists(GUARDIAN_LOG) and os.path.getsize(GUARDIAN_LOG) > 2 * 1024 * 1024:
            try:
                os.replace(GUARDIAN_LOG, GUARDIAN_LOG + ".1")
            except Exception:
                pass
        h = logging.FileHandler(GUARDIAN_LOG, encoding="utf-8")
    except Exception:
        h = logging.StreamHandler()
    h.setFormatter(fmt)
    logg.addHandler(h)
    return logg


def _heartbeat_alive(pid):
    """主程序是否还在正常打心跳（防卡死）"""
    try:
        with open(HEARTBEAT_FILE, encoding="utf-8") as f:
            data = json.load(f)
        if int(data.get("pid", -1)) != pid:
            return True     # 文件可能还是旧的，先不杀
        return (time.time() - float(data.get("ts", 0))) < 180
    except Exception:
        return True


def run_guardian(argv):
    """保底进程：守护主程序；崩溃/卡死自动拉起；更新退出时不干扰替换并让更新脚本接管"""
    g = _guardian_logger()
    current = {"proc": None}

    def _stop(signum, frame):
        g.info("收到退出信号 %s，正在结束主程序", signum)
        try:
            if current["proc"] is not None and current["proc"].poll() is None:
                current["proc"].terminate()
        except Exception:
            pass
        os._exit(0)

    try:
        import signal as _signal
        for _sig in (_signal.SIGINT, _signal.SIGTERM):
            try:
                _signal.signal(_sig, _stop)
            except Exception:
                pass
    except Exception:
        pass
    if _mutex_exists():
        g.info("已有实例在运行，守护进程退出（不弹窗）")
        return 0
    env = dict(os.environ, PCM_GUARDIAN="1")
    if FROZEN:
        base = [sys.executable]
    else:
        base = [sys.executable, os.path.abspath(__file__)]
    extra = [a for a in argv if a not in ("--run", "--no-guardian")]
    cmd = base + ["--run"] + extra
    restarts = []
    g.info("守护进程启动，准备拉起主程序: %s", " ".join(cmd[:3]))
    while True:
        try:
            child = subprocess.Popen(cmd, cwd=BASE_DIR, env=env)
        except Exception as e:
            g.error("拉起主程序失败: %s", e)
            old_exe = os.path.join(BASE_DIR, "phone-pc-monitor.old.exe")
            if os.path.isfile(old_exe):
                g.warning("尝试用旧版本 %s 启动", old_exe)
                try:
                    subprocess.Popen([old_exe], cwd=BASE_DIR, env=env)
                    return 0
                except Exception as e2:
                    g.error("旧版本启动也失败: %s", e2)
            return 1
        current["proc"] = child
        g.info("主程序已启动，pid=%s", child.pid)
        rc = None
        while True:
            rc = child.poll()
            if rc is not None:
                break
            if not _heartbeat_alive(child.pid):
                g.error("主程序超过 180 秒无心跳，判定卡死，强制重启")
                try:
                    child.kill()
                except Exception:
                    pass
                rc = -9
                break
            time.sleep(2)
        if rc == 0:
            g.info("主程序正常退出，守护进程结束")
            return 0
        if rc == 42:
            g.info("主程序因热更新退出（code 42），守护进程退出并交由更新脚本替换文件")
            return 0
        now = time.time()
        restarts = [t for t in restarts if now - t < 600]
        g.warning("主程序异常退出 code=%s（近 10 分钟第 %d 次）", rc, len(restarts) + 1)
        if len(restarts) >= 5:
            g.error("10 分钟内崩溃超过 5 次，可能新版本有问题")
            old_exe = os.path.join(BASE_DIR, "phone-pc-monitor.old.exe")
            if os.path.isfile(old_exe):
                g.warning("尝试用旧版本 %s 启动", old_exe)
                try:
                    subprocess.Popen([old_exe], cwd=BASE_DIR, env=env)
                    return 0
                except Exception as e:
                    g.error("旧版本启动失败: %s", e)
            g.error("放弃重启，请查看 monitor-lan.log 和 guardian.log")
            return 1
        restarts.append(now)
        time.sleep(min(2 * len(restarts), 12))


async def heartbeat_loop():
    while True:
        try:
            tmp = HEARTBEAT_FILE + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump({"pid": os.getpid(), "ts": time.time()}, f)
            os.replace(tmp, HEARTBEAT_FILE)
        except Exception:
            pass
        await asyncio.sleep(10)


def run_camera_probe_cli():
    """--probe：写一份人可读的检测报告到程序目录，方便离线排查"""
    report = camera_probe()
    lines = ["手机监控电脑 · 摄像头检测", "时间: " + time.strftime("%Y-%m-%d %H:%M:%S")]
    lines.append("系统: %s   OpenCV: %s" % (report.get("platform"), report.get("cv2") or "不可用"))
    if report.get("error"):
        lines.append("错误: " + str(report["error"]))
    pr = report.get("privacy")
    if pr:
        lines.append("相机隐私设置: %s" % json.dumps(pr, ensure_ascii=False))
    lines.append("")
    lines.append("检测过程:")
    for r in report.get("results", []):
        lines.append("  编号 %s / 后端 %s: opened=%s read=%s %s" % (
            r.get("index"), r.get("backend"), r.get("opened"), r.get("read"),
            r.get("error") or ""))
    lines.append("")
    lines.append("结论: " + (report.get("hint") or "未知"))
    text = "\n".join(lines) + "\n"
    path = os.path.join(BASE_DIR, "摄像头检测.txt")
    try:
        with open(path, "w", encoding="utf-8-sig") as f:
            f.write(text)
        log.info("摄像头检测报告已写入 %s", path)
    except Exception as e:
        log.error("写入摄像头检测报告失败: %s", e)
    try:
        print(text)
    except Exception:
        pass


async def main():
    enable_dpi_awareness()
    if getattr(ARGS, "probe", False):
        run_camera_probe_cli()
        return
    if not acquire_single_instance():
        os._exit(0)
    _cleanup_updates_on_start()
    _kill_orphan_tts()

    app = web.Application(client_max_size=UPDATE_MAX_BYTES + 8 * 1024 * 1024)
    app.router.add_get("/", index)
    app.router.add_get("/ws", ws_handler)
    app.router.add_get("/health", health)
    app.router.add_get("/proof_list", proof_list)
    app.router.add_get("/proof_file", proof_file)
    app.router.add_post("/upload_audio", upload_audio)
    app.router.add_get("/audio_list", audio_list)
    app.router.add_get("/version", update_info)
    app.router.add_post("/upload_update", upload_update)
    app.router.add_post("/apply_update", apply_update)
    app.router.add_post("/discard_update", discard_update)

    ssl_ctx = None
    global HTTPS_ENABLED
    if CONFIG.get("https"):
        try:
            cert_path, key_path = ensure_https_cert()
            ssl_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            ssl_ctx.load_cert_chain(cert_path, key_path)
            HTTPS_ENABLED = True
            log.info("已启用 HTTPS（自签证书）：手机首次打开会提示不安全，点“高级 → 继续前往”即可")
        except Exception as e:
            log.error("HTTPS 启用失败（%s），回退到 HTTP", e)
            ssl_ctx = None
            HTTPS_ENABLED = False

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, CONFIG["host"], CONFIG["port"], ssl_context=ssl_ctx)
    try:
        await site.start()
    except OSError as e:
        log.warning("端口 %s 被占用（可能已有实例在运行），本次启动退出: %s", CONFIG["port"], e)
        await runner.cleanup()
        os._exit(0)

    log.info("服务已启动: 监听 %s:%s（版本 %s，%s 模式）", CONFIG["host"], CONFIG["port"],
             APP_VERSION, _runtime_mode())
    if NO_AGENT:
        log.warning("--no-agent：只提供中继服务器，不采集本机画面")

    tasks = []
    if not NO_AGENT:
        try:
            tasks = await start_engine()
        except Exception as e:
            log.error("本机采集启动失败（服务器继续运行）: %s", e)
    tasks.append(asyncio.create_task(network_watch(), name="net-watch"))
    tasks.append(asyncio.create_task(stream_watchdog(), name="stream-watchdog"))
    tasks.append(asyncio.create_task(heartbeat_loop(), name="heartbeat"))

    try:
        await asyncio.Event().wait()
    finally:
        try:
            audio_force_stop()
        except Exception:
            pass
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await runner.cleanup()


if __name__ == "__main__":
    try:
        if ARGS.run or ARGS.probe or ARGS.no_guardian:
            if ARGS.probe:
                asyncio.run(main())
            else:
                asyncio.run(main())
        else:
            run_guardian(sys.argv[1:])
    except KeyboardInterrupt:
        log.info("已退出")
