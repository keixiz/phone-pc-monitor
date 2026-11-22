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
import json
import logging
import os
import platform
import secrets
import shutil
import socket
import subprocess
import sys
import threading
import time
from urllib.parse import quote

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
ACCESS_FILE = os.path.join(BASE_DIR, "访问地址.txt")
LOG_FILE = os.path.join(BASE_DIR, "monitor-lan.log")


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
        try:
            await send_json(v, {"type": "devices", "list": devs})
        except Exception:
            drop_viewer(v)


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
                    log.info("电脑端上线: %s (%s)", name, data.get("os", "unknown"))
                    await broadcast_devices()
                    await recompute_streams()
                elif name is not None and t == "frame":
                    if name not in hub.agents:
                        continue
                    hub.agents[name]["source"] = data.get("source", "screen")
                    for v in viewer_targets(name):
                        try:
                            await send_json(
                                v,
                                {"type": "frame", "source": data.get("source"), "agent": name},
                            )
                        except Exception:
                            drop_viewer(v)
                elif name is not None and t in (
                        "stats", "motion", "dir_list", "file_data", "chat",
                        "process_list", "toast"):
                    for v in viewer_targets(name):
                        try:
                            await send_json(v, data)
                        except Exception:
                            drop_viewer(v)
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
                for v in viewer_targets(name):
                    try:
                        await v.send_bytes(payload)
                    except Exception:
                        drop_viewer(v)
    finally:
        if name is not None:
            entry = hub.agents.get(name)
            if entry is not None and entry.get("link") is link:
                hub.agents.pop(name, None)
                hub.agent_stream.pop(name, None)
                hub.pending_proof.pop(name, None)
                hub.frames.pop(name, None)
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
            await send_json(ws, {"type": "frame", "source": source, "agent": target})
            await ws.send_bytes(data)


async def viewer_session(request, ws):
    """处理手机端连接：选择设备、接收画面、转发控制指令、触发通知"""
    hub.viewers.add(ws)
    try:
        await send_json(ws, {"type": "devices", "list": device_list()})
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
                        await send_json(ws, {"type": "selected", "name": target})
                        await send_cached_frame(ws)
                        await recompute_streams()
                    else:
                        await send_json(ws, {"type": "toast", "text": f"设备 {target} 不在线"})
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
                    await send_json(ws, {"type": "toast", "text": result})
                elif t in ("overlay", "exec", "open_path", "clipboard", "blackout",
                           "mouse", "list_dir", "download", "chat", "wallpaper",
                           "snapshot", "key", "brightness", "process_list",
                           "kill_process", "proof_interval", "tts", "popup",
                           "alarm", "draw", "draw_clear"):
                    choice = hub.viewer_choice.get(ws)
                    if choice not in hub.agents and hub.agents:
                        choice = list(hub.agents)[0]
                    if choice in hub.agents:
                        await hub.agents[choice]["link"].server_send_json(data)
                    else:
                        await send_json(ws, {"type": "toast", "text": "没有在线电脑"})
            elif msg.type == WSMsgType.ERROR:
                break
    finally:
        hub.viewers.discard(ws)
        hub.viewer_choice.pop(ws, None)
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
    page = os.path.join(STATIC_DIR, "index.html")
    if not os.path.exists(page):
        return web.Response(status=500, text="static/index.html 缺失，请重新安装/打包")
    return web.FileResponse(page)


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
        except Exception as e:
            self.cap = None
            log.error("摄像头初始化失败: %s", e)
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
        if src == "camera" and not cam._ensure():
            # 摄像头不可用别让手机端一脸茫然：画面卡住时给个提示
            await link_send(link, {"type": "toast",
                                   "text": "摄像头不可用：装 opencv-python numpy，或检查是否被占用"})
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
        await run_blocking(set_wallpaper, data.get("data", ""))
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
    if not ips:
        ips = [("", "127.0.0.1")]
    return [f"http://{ip}:{port}/?token={token}" for _, ip in ips]


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
    log.info("本机采集已启动: %s（%sx%s，屏幕 %s）", name, sw, sh, "摄像头" if state["source"] == "camera" else "屏幕")
    return tasks


async def main():
    enable_dpi_awareness()

    app = web.Application()
    app.router.add_get("/", index)
    app.router.add_get("/ws", ws_handler)
    app.router.add_get("/health", health)
    app.router.add_get("/proof_list", proof_list)
    app.router.add_get("/proof_file", proof_file)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, CONFIG["host"], CONFIG["port"])
    try:
        await site.start()
    except OSError as e:
        log.error("端口 %s 启动失败（可能被占用或没权限）: %s", CONFIG["port"], e)
        await runner.cleanup()
        raise SystemExit(1)

    log.info("服务已启动: 监听 %s:%s", CONFIG["host"], CONFIG["port"])
    if NO_AGENT:
        log.warning("--no-agent：只提供中继服务器，不采集本机画面")

    tasks = []
    if not NO_AGENT:
        try:
            tasks = await start_engine()
        except Exception as e:
            log.error("本机采集启动失败（服务器继续运行）: %s", e)
    tasks.append(asyncio.create_task(network_watch(), name="net-watch"))

    try:
        await asyncio.Event().wait()
    finally:
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await runner.cleanup()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        log.info("已退出")
