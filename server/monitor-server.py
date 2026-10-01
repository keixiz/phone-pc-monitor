#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
monitor-server.py — 手机监控电脑 · 服务器端
============================================
职责：
  1. WebSocket 中继：电脑端 Agent 与手机端 H5 都连到这里，按"设备"转发画面帧和控制指令
  2. 托管手机端 H5 页面（static/index.html）
  3. 通知出口：收到通知请求后，调用 ntfy / Server酱 推送到手机锁屏，并给页面 toast 反馈

依赖：pip install aiohttp
运行：python3 monitor-server.py
配置：同目录 config.json（可从 config.example.json 复制修改）
"""
import asyncio
import json
import logging
import os
import time

import aiohttp
from aiohttp import web, WSMsgType

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("monitor-server")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

DEFAULT_CONFIG = {
    "host": "0.0.0.0",
    "port": 8000,
    "token": "CHANGE_ME_STRONG_TOKEN",   # 电脑端与手机端共用的访问令牌，务必改掉
    "notify": {
        "channel": "off",                # off / ntfy / serverchan
        "topic": "",                     # ntfy: 订阅主题，如 monitor_abc123
        "server": "https://ntfy.sh",     # ntfy: 自建时改为 http://你的服务器IP:8091 之类
        "sendkey": "",                   # serverchan: sct.ftqq.com 的 SendKey
    },
}


def load_config():
    cfg = json.loads(json.dumps(DEFAULT_CONFIG))  # 深拷贝
    path = os.path.join(BASE_DIR, "config.json")
    if os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as f:
                cfg.update(json.load(f))
        except Exception as e:
            log.error("读取 config.json 失败: %s", e)
    env = os.environ.get("MONITOR_TOKEN")
    if env:
        cfg["token"] = env
    return cfg


CONFIG = load_config()

if CONFIG["token"] == "CHANGE_ME_STRONG_TOKEN":
    log.warning("!! 请修改 config.json 里的 token，否则任何人都能连上你的监控")


class Hub:
    """在线状态登记表"""

    def __init__(self):
        self.agents = {}       # name -> {"ws": ws, "source": "screen", "os": "..."}
        self.viewers = set()   # ws 集合
        self.viewer_choice = {}  # ws -> 选中的设备 name（None 表示跟随默认）
        self.frames = {}       # name -> {source: bytes} 最新一帧
        self.pending_proof = {}  # name -> ts
        self.agent_stream = {}   # name -> 当前观看该设备的 viewer 数（节能统计）


hub = Hub()


def send_json(ws, obj):
    return ws.send_str(json.dumps(obj, ensure_ascii=False))


def device_list():
    """生成在线设备列表（带系统信息）"""
    out = []
    for name, a in hub.agents.items():
        out.append({"name": name, "os": a.get("os", "unknown"),
                    "screen_w": a.get("screen_w", 1920), "screen_h": a.get("screen_h", 1080)})
    return out


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
            return "未配置锁屏推送渠道，仅服务器日志可见"
    except Exception as e:
        log.error("通知发送失败: %s", e)
        return f"通知发送失败: {e}"


def viewer_wants(v, agent_name):
    """判断该 viewer 是否要看这个设备"""
    choice = hub.viewer_choice.get(v)
    if choice is None:
        # 没手动选过：默认看第一台在线设备
        return list(hub.agents)[0] == agent_name if hub.agents else False
    return choice == agent_name


async def recompute_streams():
    """节能：统计每个设备的观看人数，0↔非0 翻转时通知 agent 暂停/恢复推流"""
    for name in list(hub.agents):
        count = sum(1 for v in hub.viewers if viewer_wants(v, name))
        prev = hub.agent_stream.get(name, -1)
        if count == prev:
            continue
        hub.agent_stream[name] = count
        try:
            await send_json(hub.agents[name]["ws"],
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
            hub.viewers.discard(v)


async def agent_session(request, ws):
    """处理电脑端连接：注册、按设备转发画面给 viewer、响应控制指令"""
    name = None
    try:
        async for msg in ws:
            if msg.type == WSMsgType.TEXT:
                try:
                    data = json.loads(msg.data)
                except Exception:
                    continue
                t = data.get("type")
                if t == "hello" and name is None:
                    name = data.get("name") or "pc"
                    hub.agents[name] = {
                        "ws": ws,
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
                    hub.agents[name]["source"] = data.get("source", "screen")
                    for v in list(hub.viewers):
                        if not viewer_wants(v, name):
                            continue
                        try:
                            await send_json(
                                v,
                                {"type": "frame", "source": data.get("source"), "agent": name},
                            )
                        except Exception:
                            hub.viewers.discard(v)
                elif t in ("stats", "motion", "dir_list", "file_data", "chat", "process_list"):
                    # 转发给正在看这台设备的 viewer
                    for v in list(hub.viewers):
                        if not viewer_wants(v, name):
                            continue
                        try:
                            await send_json(v, data)
                        except Exception:
                            hub.viewers.discard(v)
                elif t == "proof":
                    # 下一帧二进制是存证截图，暂存时间戳
                    hub.pending_proof[name] = data.get("ts", int(time.time()))
                elif t == "notify":
                    await push_notify(data.get("text", "电脑端触发通知"))
            elif msg.type == WSMsgType.BINARY and name is not None:
                # 存证截图？
                ts = hub.pending_proof.pop(name, None)
                if ts:
                    try:
                        d = time.strftime("%Y-%m-%d", time.localtime(ts))
                        sd = os.path.join(BASE_DIR, "proof", name, d)
                        os.makedirs(sd, exist_ok=True)
                        fn = time.strftime("%H-%M-%S", time.localtime(ts)) + ".jpg"
                        with open(os.path.join(sd, fn), "wb") as f:
                            f.write(msg.data)
                    except Exception as e:
                        log.error("存证失败: %s", e)
                    continue
                source = hub.agents[name]["source"]
                hub.frames[name][source] = msg.data
                for v in list(hub.viewers):
                    if not viewer_wants(v, name):
                        continue
                    try:
                        await v.send_bytes(msg.data)
                    except Exception:
                        hub.viewers.discard(v)
            elif msg.type == WSMsgType.ERROR:
                break
    finally:
        if name is not None:
            hub.agents.pop(name, None)
            # 选中这台设备的 viewer 改回跟随默认
            for v, c in list(hub.viewer_choice.items()):
                if c == name:
                    hub.viewer_choice[v] = None
            log.info("电脑端离线: %s", name)
            await broadcast_devices()
            await recompute_streams()


async def send_cached_frame(ws):
    """viewer 刚连上时，补发它当前选中设备的最新一帧"""
    choice = hub.viewer_choice.get(ws)
    if hub.agents:
        target = choice if choice in hub.agents else list(hub.agents)[0]
        hub.viewer_choice[ws] = target
        fr = hub.frames.get(target, {})
        for src, data in fr.items():
            if data:
                await send_json(ws, {"type": "frame", "source": src, "agent": target})
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
                        a = hub.agents[choice]
                        try:
                            await send_json(a["ws"], {"type": "set_source", "source": target})
                        except Exception:
                            pass
                elif t == "notify":
                    result = await push_notify(data.get("text", "手机端手动测试通知"))
                    await send_json(ws, {"type": "toast", "text": result})
                elif t in ("overlay", "exec", "open_path", "clipboard", "blackout",
                           "mouse", "list_dir", "download", "chat", "wallpaper", "snapshot", "key",
                           "brightness", "process_list", "kill_process", "proof_interval",
                           "tts", "popup", "alarm", "draw", "draw_clear"):
                    choice = hub.viewer_choice.get(ws)
                    if choice not in hub.agents and hub.agents:
                        choice = list(hub.agents)[0]
                    if choice in hub.agents:
                        await send_json(hub.agents[choice]["ws"], data)
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
    if token != CONFIG["token"] or role not in ("agent", "viewer"):
        log.warning("鉴权失败: role=%s from=%s", role, request.remote)
        return web.Response(status=401, text="unauthorized")
    ws = web.WebSocketResponse(heartbeat=30, max_msg_size=16 * 1024 * 1024)
    await ws.prepare(request)
    if role == "agent":
        await agent_session(request, ws)
    else:
        await viewer_session(request, ws)
    return ws


async def index(request):
    return web.FileResponse(os.path.join(BASE_DIR, "static", "index.html"))


async def health(request):
    return web.json_response({
        "ok": True,
        "agents": len(hub.agents),
        "viewers": len(hub.viewers),
        "devices": list(hub.agents),
    })


async def proof_list(request):
    """列出某设备某天的存证截图"""
    agent = request.query.get("agent", "")
    date = request.query.get("date", time.strftime("%Y-%m-%d"))
    d = os.path.join(BASE_DIR, "proof", agent, date)
    files = []
    if os.path.isdir(d):
        for fn in sorted(os.listdir(d)):
            if fn.endswith(".jpg"):
                files.append("/proof_file?agent=" + agent + "&date=" + date + "&f=" + fn)
    return web.json_response({"date": date, "files": files})


async def proof_file(request):
    agent = request.query.get("agent", "")
    date = request.query.get("date", "")
    fn = request.query.get("f", "")
    # 防目录穿越
    if ".." in fn or "/" in fn or "\\" in fn:
        return web.Response(status=400, text="bad")
    path = os.path.join(BASE_DIR, "proof", agent, date, fn)
    if not os.path.isfile(path):
        return web.Response(status=404, text="not found")
    return web.FileResponse(path)


async def main():
    app = web.Application()
    app.router.add_get("/", index)
    app.router.add_get("/ws", ws_handler)
    app.router.add_get("/health", health)
    app.router.add_get("/proof_list", proof_list)
    app.router.add_get("/proof_file", proof_file)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, CONFIG["host"], CONFIG["port"])
    await site.start()
    log.info("监控服务已启动: http://%s:%s （健康检查: /health）", CONFIG["host"], CONFIG["port"])
    log.info("手机端访问: http://你的域名或IP:%s/ 并输入 token", CONFIG["port"])
    try:
        await asyncio.Event().wait()
    finally:
        await runner.cleanup()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        log.info("已退出")
