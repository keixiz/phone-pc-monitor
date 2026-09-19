#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
monitor-server.py - Phone-to-PC monitoring server
WebSocket relay + static H5 hosting + proof screenshot storage.
Run: python3 monitor-server.py
Config: config.json (copy from config.example.json)
"""
import asyncio, json, logging, os, time
import aiohttp
from aiohttp import web, WSMsgType

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("monitor-server")
BASE_DIR = os.path.dirname(os.path.abspath(__file__))

DEFAULT_CONFIG = {
    "host": "0.0.0.0", "port": 8000,
    "token": "CHANGE_ME_STRONG_TOKEN",
    "notify": {"channel": "off", "topic": "", "server": "https://ntfy.sh", "sendkey": ""},
}

def load_config():
    cfg = json.loads(json.dumps(DEFAULT_CONFIG))
    p = os.path.join(BASE_DIR, "config.json")
    if os.path.exists(p):
        try:
            with open(p, encoding="utf-8") as f: cfg.update(json.load(f))
        except Exception as e: log.error("config: %s", e)
    if os.environ.get("MONITOR_TOKEN"): cfg["token"] = os.environ["MONITOR_TOKEN"]
    return cfg

CONFIG = load_config()
if CONFIG["token"] == "CHANGE_ME_STRONG_TOKEN":
    log.warning("!! Change token in config.json!")

class Hub:
    def __init__(self):
        self.agents = {}; self.viewers = set(); self.viewer_choice = {}
        self.frames = {}; self.pending_proof = {}
hub = Hub()

def send_json(ws, o): return ws.send_str(json.dumps(o, ensure_ascii=False))

def device_list():
    return [{"name": n, "os": a.get("os","?"), "screen_w": a.get("screen_w",1920), "screen_h": a.get("screen_h",1080)} for n,a in hub.agents.items()]

async def push_notify(text):
    n = CONFIG.get("notify",{}); ch = n.get("channel","off")
    try:
        if ch == "ntfy":
            topic = n.get("topic","").strip()
            if not topic: return "ntfy topic not configured"
            srv = n.get("server","https://ntfy.sh").rstrip("/")
            async with aiohttp.ClientSession() as s:
                await s.post(f"{srv}/{topic}", data=text.encode("utf-8"))
            return "pushed to ntfy"
        elif ch == "serverchan":
            sk = n.get("sendkey","").strip()
            if not sk: return "serverchan sendkey not configured"
            async with aiohttp.ClientSession() as s:
                await s.post(f"https://sctapi.ftqq.com/{sk}.send", data={"title":"Monitor","desp":text})
            return "pushed to WeChat"
        return "notify channel off"
    except Exception as e: return f"notify failed: {e}"

def viewer_wants(v, name):
    c = hub.viewer_choice.get(v)
    if c is None: return list(hub.agents)[0] == name if hub.agents else False
    return c == name

async def broadcast_devices():
    devs = device_list()
    for v in list(hub.viewers):
        try: await send_json(v, {"type":"devices","list":devs})
        except: hub.viewers.discard(v)

async def agent_session(request, ws):
    name = None
    try:
        async for msg in ws:
            if msg.type == WSMsgType.TEXT:
                try: data = json.loads(msg.data)
                except: continue
                t = data.get("type")
                if t == "hello" and name is None:
                    name = data.get("name") or "pc"
                    hub.agents[name] = {"ws":ws, "source":"screen", "os":data.get("os","?"), "screen_w":data.get("screen_w",1920), "screen_h":data.get("screen_h",1080)}
                    hub.frames.setdefault(name,{})
                    log.info("agent online: %s", name)
                    await broadcast_devices()
                elif name and t == "frame":
                    hub.agents[name]["source"] = data.get("source","screen")
                    for v in list(hub.viewers):
                        if not viewer_wants(v,name): continue
                        try: await send_json(v,{"type":"frame","source":data.get("source"),"agent":name})
                        except: hub.viewers.discard(v)
                elif t in ("stats","motion","dir_list","file_data","chat","process_list"):
                    for v in list(hub.viewers):
                        if not viewer_wants(v,name): continue
                        try: await send_json(v,data)
                        except: hub.viewers.discard(v)
                elif t == "proof": hub.pending_proof[name] = data.get("ts",int(time.time()))
                elif t == "notify": await push_notify(data.get("text",""))
            elif msg.type == WSMsgType.BINARY and name:
                ts = hub.pending_proof.pop(name,None)
                if ts:
                    try:
                        d = time.strftime("%Y-%m-%d",time.localtime(ts))
                        sd = os.path.join(BASE_DIR,"proof",name,d); os.makedirs(sd,exist_ok=True)
                        with open(os.path.join(sd,time.strftime("%H-%M-%S",time.localtime(ts))+".jpg"),"wb") as f: f.write(msg.data)
                    except Exception as e: log.error("proof: %s",e)
                    continue
                src = hub.agents[name]["source"]; hub.frames[name][src] = msg.data
                for v in list(hub.viewers):
                    if not viewer_wants(v,name): continue
                    try: await v.send_bytes(msg.data)
                    except: hub.viewers.discard(v)
    finally:
        if name:
            hub.agents.pop(name,None)
            for v,c in list(hub.viewer_choice.items()):
                if c == name: hub.viewer_choice[v] = None
            await broadcast_devices()

async def send_cached(ws):
    c = hub.viewer_choice.get(ws)
    if hub.agents:
        tgt = c if c in hub.agents else list(hub.agents)[0]
        hub.viewer_choice[ws] = tgt
        for src,d in hub.frames.get(tgt,{}).items():
            if d: await send_json(ws,{"type":"frame","source":src,"agent":tgt}); await ws.send_bytes(d)

async def viewer_session(request, ws):
    hub.viewers.add(ws)
    try:
        await send_json(ws,{"type":"devices","list":device_list()})
        await send_cached(ws)
        async for msg in ws:
            if msg.type == WSMsgType.TEXT:
                try: data = json.loads(msg.data)
                except: continue
                t = data.get("type")
                if t == "select":
                    n = data.get("name")
                    if n in hub.agents:
                        hub.viewer_choice[ws] = n; await send_json(ws,{"type":"selected","name":n}); await send_cached(ws)
                elif t == "set_source":
                    c = hub.viewer_choice.get(ws)
                    if c not in hub.agents and hub.agents: c = list(hub.agents)[0]
                    if c in hub.agents: await send_json(hub.agents[c]["ws"],data)
                elif t == "notify":
                    r = await push_notify(data.get("text","")); await send_json(ws,{"type":"toast","text":r})
                elif t in ("overlay","exec","open_path","clipboard","blackout","mouse","list_dir","download","chat","wallpaper","snapshot","key","brightness","process_list","kill_process","proof_interval","tts","popup","alarm","draw","draw_clear"):
                    c = hub.viewer_choice.get(ws)
                    if c not in hub.agents and hub.agents: c = list(hub.agents)[0]
                    if c in hub.agents: await send_json(hub.agents[c]["ws"],data)
                    else: await send_json(ws,{"type":"toast","text":"no agent online"})
    finally:
        hub.viewers.discard(ws); hub.viewer_choice.pop(ws,None)

async def ws_handler(request):
    tok = request.query.get("token",""); role = request.query.get("role","")
    if tok != CONFIG["token"] or role not in ("agent","viewer"):
        return web.Response(status=401, text="unauthorized")
    ws = web.WebSocketResponse(heartbeat=30, max_msg_size=64*1024*1024)
    await ws.prepare(request)
    if role == "agent": await agent_session(request,ws)
    else: await viewer_session(request,ws)
    return ws

async def index(request): return web.FileResponse(os.path.join(BASE_DIR,"static","index.html"))
async def health(request): return web.json_response({"ok":True,"agents":len(hub.agents),"viewers":len(hub.viewers)})
async def proof_list(request):
    a = request.query.get("agent",""); d = request.query.get("date",time.strftime("%Y-%m-%d"))
    dd = os.path.join(BASE_DIR,"proof",a,d); files = []
    if os.path.isdir(dd):
        for fn in sorted(os.listdir(dd)):
            if fn.endswith(".jpg"): files.append("/proof_file?agent="+a+"&date="+d+"&f="+fn)
    return web.json_response({"date":d,"files":files})
async def proof_file(request):
    a = request.query.get("agent",""); d = request.query.get("date",""); fn = request.query.get("f","")
    if ".." in fn or "/" in fn or "\\" in fn: return web.Response(status=400,text="bad")
    p = os.path.join(BASE_DIR,"proof",a,d,fn)
    if not os.path.isfile(p): return web.Response(status=404,text="not found")
    return web.FileResponse(p)

async def main():
    app = web.Application()
    app.router.add_get("/",index); app.router.add_get("/ws",ws_handler)
    app.router.add_get("/health",health)
    app.router.add_get("/proof_list",proof_list); app.router.add_get("/proof_file",proof_file)
    runner = web.AppRunner(app); await runner.setup()
    site = web.TCPSite(runner,CONFIG["host"],CONFIG["port"]); await site.start()
    log.info("server on %s:%s",CONFIG["host"],CONFIG["port"])
    try: await asyncio.Event().wait()
    finally: await runner.cleanup()

if __name__ == "__main__":
    asyncio.run(main())