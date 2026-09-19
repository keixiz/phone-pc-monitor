#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
monitor-agent.py - PC-side agent for phone-to-PC monitoring.
Edit DEFAULT_CONFIG below or use config.json.
Run: python monitor-agent.py
"""
import asyncio, base64, io, json, logging, os, platform, socket, subprocess, sys, threading, time

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("monitor-agent")

BASE_DIR = os.path.dirname(os.path.abspath(sys.executable)) if getattr(sys,"frozen",False) else os.path.dirname(os.path.abspath(__file__))

DEFAULT_CONFIG = {
    "server": "wss://your-server/ws",
    "token": "CHANGE_ME_TOKEN",
    "source": "screen",
    "fps": 3,
    "camera_index": 0,
    "jpeg_quality": 70,
    "motion_threshold": 8.0,
}

def load_config():
    cfg = dict(DEFAULT_CONFIG)
    p = os.path.join(BASE_DIR,"config.json")
    if os.path.exists(p):
        try:
            with open(p, encoding="utf-8-sig") as f: cfg.update(json.load(f))
        except Exception as e: log.error("config: %s", e)
    return cfg

CFG = load_config()

# ---- Screen grab ----
def make_screen_grabber():
    try:
        import mss
    except ImportError:
        log.error("pip install mss Pillow"); return None
    sct = mss.mss()
    def grab():
        try:
            shot = sct.grab(sct.monitors[1])
            from PIL import Image
            img = Image.frombytes("RGB", shot.size, shot.bgra, "raw", "BGRX")
            buf = io.BytesIO(); img.save(buf,"JPEG",quality=CFG["jpeg_quality"])
            return buf.getvalue()
        except Exception as e: log.error("grab: %s",e); return None
    return grab

class Camera:
    def __init__(self): self.cap=None; self.cv2=None
    def _ensure(self):
        if self.cap is not None: return True
        try:
            import cv2; self.cv2=cv2
            self.cap=cv2.VideoCapture(CFG["camera_index"])
            if not self.cap.isOpened(): self.cap.release(); self.cap=None; return False
            return True
        except ImportError: return False
    def release(self):
        if self.cap is not None:
            try: self.cap.release()
            except: pass
            self.cap=None
    def grab(self):
        if not self._ensure(): return None
        try:
            ok=False; frame=None
            for _ in range(3): ok,frame=self.cap.read(); if ok: break
            if not ok: return None
            ok,buf=self.cv2.imencode(".jpg",frame,[self.cv2.IMWRITE_JPEG_QUALITY,CFG["jpeg_quality"]])
            return buf.tobytes() if ok else None
        except Exception as e: log.error("cam: %s",e); return None

# ---- Tk UI (single root + queue) ----
import queue as _queue
_tk_q=_queue.Queue(); _tk_root=None
def _tk_main():
    try: import tkinter as tk
    except: return
    global _tk_root; _tk_root=tk.Tk(); _tk_root.withdraw()
    def poll():
        try:
            while True:
                fn=_tk_q.get_nowait()
                try: fn()
                except Exception as e: log.error("ui: %s",e)
        except _queue.Empty: pass
        _tk_root.after(50,poll)
    poll(); _tk_root.mainloop()
def _start_tk():
    if not any(t.name=="monitor-tk" for t in threading.enumerate()):
        threading.Thread(target=_tk_main,name="monitor-tk",daemon=True).start()
def _post(fn): _start_tk(); _tk_q.put(fn)

def show_overlay(text,color="#fff",bg="#f00",speed=6,duration=10,pos="middle"):
    def work():
        import tkinter as tk
        root=tk.Toplevel(_tk_root); root.overrideredirect(True); root.attributes("-topmost",True); root.attributes("-alpha",0.92)
        sw=root.winfo_screenwidth(); sh=root.winfo_screenheight(); bh=90
        y0=0 if pos=="top" else (sh-bh if pos=="bottom" else (sh-bh)//2)
        root.geometry(f"{sw}x{bh}+0+{y0}"); root.config(bg=bg)
        cv=tk.Canvas(root,width=sw,height=bh,bg=bg,highlightthickness=0); cv.pack()
        x=sw; t=cv.create_text(x,bh//2,text=text,fill=color,font=("Microsoft YaHei",38,"bold"),anchor="w")
        def step():
            nonlocal x; x-=int(speed); bbox=cv.bbox(t)
            if bbox and bbox[2]<0: x=sw
            cv.coords(t,x,bh//2); root.after(30,step)
        step(); root.after(max(1000,int(float(duration)*1000)),root.destroy)
    _post(work)

def set_blackout(on):
    def work():
        import tkinter as tk
        if on:
            r=tk.Toplevel(_tk_root); r.attributes("-fullscreen",True); r.attributes("-topmost",True)
            r.overrideredirect(True); r.config(bg="black"); r.bind("<Escape>",lambda e:r.destroy())
    _post(work)

# ---- Chat ----
_chat_win=None;_chat_box=None;_chat_entry=None;_ws_ref=None;_loop=None
def set_ws(ws,loop): global _ws_ref,_loop; _ws_ref=ws; _loop=loop
def _chat_send(text):
    if not text or not _ws_ref: return
    try: asyncio.run_coroutine_threadsafe(send_json(_ws_ref,{"type":"chat","from":"agent","text":text}),_loop)
    except: pass
def _ensure_chat():
    global _chat_win,_chat_box,_chat_entry
    import tkinter as tk
    if _chat_win: return
    r=tk.Toplevel(_tk_root); _chat_win=r; r.title("Chat"); r.attributes("-topmost",True)
    sw=r.winfo_screenwidth(); r.geometry(f"340x440+{sw-360}+80"); r.config(bg="#1c2128")
    e=tk.Entry(r,bg="#21262d",fg="#e8edf2",insertbackground="#e8edf2",font=("Microsoft YaHei",11))
    e.pack(side="bottom",fill="x",padx=8,pady=(0,8)); e.bind("<Return>",lambda ev:_chat_submit()); _chat_entry=e
    b=tk.Text(r,bg="#0d1117",fg="#e8edf2",font=("Microsoft YaHei",11),wrap="word",state="disabled")
    b.pack(side="top",fill="both",expand=True,padx=8,pady=(8,4)); _chat_box=b
def _chat_submit():
    t=_chat_entry.get().strip()
    if not t: return
    _chat_entry.delete(0,"end"); _chat_append("Me",t); _chat_send(t)
def _chat_append(who,text):
    def work():
        _ensure_chat(); _chat_box.config(state="normal"); _chat_box.insert("end",who+": "+text+"\n"); _chat_box.see("end"); _chat_box.config(state="disabled")
    _post(work)
def handle_chat(text): _chat_append("Peer",text)

# ---- Wallpaper ----
def set_wallpaper(b64):
    try:
        raw=base64.b64decode(b64); path=os.path.join(BASE_DIR,"wallpaper_tmp.jpg")
        open(path,"wb").write(raw)
        if sys.platform=="win32":
            import ctypes; ctypes.windll.user32.SystemParametersInfoW(20,0,path,3)
    except Exception as e: log.error("wp: %s",e)

# ---- Mouse ----
_mouse=None; _mouse_err=None
def get_mouse():
    global _mouse,_mouse_err
    if _mouse is None and _mouse_err is None:
        try: from pynput.mouse import Controller; _mouse=Controller()
        except Exception as e: _mouse_err=str(e)
    return _mouse
def get_screen_size():
    try: import ctypes; return ctypes.windll.user32.GetSystemMetrics(0),ctypes.windll.user32.GetSystemMetrics(1)
    except: return 1920,1080
def do_mouse(data):
    m=get_mouse()
    if not m: return
    try:
        from pynput.mouse import Button
        if "x" in data and "y" in data: m.position=(int(data["x"]),int(data["y"]))
        elif data.get("dx") or data.get("dy"):
            pos=m.position; m.position=(int(pos[0]+data.get("dx",0)),int(pos[1]+data.get("dy",0)))
        c=data.get("click","none")
        if c=="left": m.click(Button.left)
        elif c=="right": m.click(Button.right)
    except Exception as e: log.error("mouse: %s",e)

# ---- Presence ----
last_activity=time.time()
def _on_activity(x=None,y=None,button=None,pressed=None,key=None): global last_activity; last_activity=time.time()
def start_presence_listener():
    try:
        from pynput import mouse, keyboard
        mouse.Listener(on_move=_on_activity,on_click=_on_activity).start()
        keyboard.Listener(on_press=_on_activity).start()
    except: pass

def do_key(data):
    try:
        from pynput.keyboard import Controller, Key
        kb=Controller()
        if "char" in data: kb.type(data["char"]); return
        kn=data.get("key","")
        k={"right":Key.right,"left":Key.left,"up":Key.up,"down":Key.down,"f5":Key.f5,"esc":Key.esc,"space":Key.space,"enter":Key.enter,"backspace":Key.backspace,"tab":Key.tab,"delete":Key.delete,"home":Key.home,"end":Key.end,"pageup":Key.page_up,"pagedown":Key.page_down,"volume_up":Key.media_volume_up,"volume_down":Key.media_volume_down,"volume_mute":Key.media_volume_mute}.get(kn)
        if not k: return
        mods=[]
        for mm in data.get("modifiers",[]):
            if mm=="ctrl": mods.append(Key.ctrl)
            elif mm=="alt": mods.append(Key.alt)
            elif mm=="shift": mods.append(Key.shift)
            elif mm=="win": mods.append(Key.cmd)
        for mm in mods: kb.press(mm)
        kb.press(k); kb.release(k)
        for mm in reversed(mods): kb.release(mm)
    except Exception as e: log.error("key: %s",e)

def do_tts(text):
    try:
        text=text.replace("'","''")
        subprocess.Popen(["powershell","-Command",f"Add-Type -AssemblyName System.Speech; $s=New-Object System.Speech.Synthesis.SpeechSynthesizer; $s.Speak('{text}')"])
    except Exception as e: log.error("tts: %s",e)

def do_popup(text,duration):
    def work():
        import tkinter as tk
        w=tk.Toplevel(_tk_root); w.overrideredirect(True); w.attributes("-topmost",True); w.attributes("-alpha",0.95)
        sw=w.winfo_screenwidth(); w.geometry(f"360x80+{sw-380}+80"); w.config(bg="#1a1a2e")
        tk.Label(w,text=text,bg="#1a1a2e",fg="#fff",font=("Microsoft YaHei",13),wraplength=340).pack(expand=True,fill="both",padx=10)
        w.after(int(duration or 5)*1000,w.destroy)
    _post(work)

def do_alarm(delay,text):
    def _alarm():
        time.sleep(max(1,int(delay))); do_popup(text or "alarm",10)
        try: subprocess.Popen(["powershell","-Command","[System.Media.SystemSounds]::Exclamation.Play()"])
        except: pass
    threading.Thread(target=_alarm,daemon=True).start()

# ---- Draw ----
_draw_canvas=None;_draw_win=None
def _draw_init():
    global _draw_win,_draw_canvas
    import tkinter as tk
    _draw_win=tk.Toplevel(_tk_root); _draw_win.overrideredirect(True); _draw_win.attributes("-topmost",True); _draw_win.attributes("-transparentcolor","black")
    sw=_draw_win.winfo_screenwidth(); sh=_draw_win.winfo_screenheight()
    _draw_win.geometry(f"{sw}x{sh}+0+0"); _draw_win.config(bg="black")
    _draw_canvas=tk.Canvas(_draw_win,bg="black",highlightthickness=0); _draw_canvas.pack(fill="both",expand=True)
    _draw_win.withdraw()
def draw_segment(x1,y1,x2,y2,color="#f00"):
    def work():
        global _draw_canvas
        if _draw_canvas is None: _draw_init()
        _draw_win.deiconify()
        _draw_canvas.create_line(x1,y1,x2,y2,fill=color,width=4,capstyle="round",smooth=True)
    _post(work)
def draw_clear():
    def work():
        if _draw_canvas is not None: _draw_canvas.delete("all"); _draw_win.withdraw()
    _post(work)

def do_brightness(level):
    try:
        level=max(0,min(100,int(level)))
        subprocess.run(["powershell","-Command",f"(Get-WmiObject -Namespace root/WMI -Class WmiMonitorBrightnessMethods).WmiSetBrightness(1,{level})"],capture_output=True,timeout=5)
    except: pass

def list_processes():
    try:
        import psutil
        procs=[]
        for p in psutil.process_iter(["pid","name","cpu_percent","memory_percent"]):
            try:
                i=p.info
                if i.get("name"): procs.append({"pid":i["pid"],"name":i["name"],"cpu":round(i.get("cpu_percent") or 0,1),"mem":round(i.get("memory_percent") or 0,1)})
            except: pass
        procs.sort(key=lambda x:x["mem"],reverse=True); return procs[:30]
    except: return []
def kill_process(pid):
    try: import psutil; psutil.Process(int(pid)).terminate()
    except: pass

def list_dir(path):
    if not path:
        if sys.platform=="win32": return {"cwd":"C:\\","entries":[{"name":"C:\\","is_dir":True,"size":0}]}
        path="/"
    path=os.path.normpath(path)
    if not os.path.isdir(path): return {"cwd":path,"entries":[],"error":"not found"}
    entries=[]
    try:
        for name in sorted(os.listdir(path)):
            full=os.path.join(path,name)
            try: entries.append({"name":name,"is_dir":os.path.isdir(full),"size":0 if os.path.isdir(full) else os.path.getsize(full)})
            except: continue
    except PermissionError: return {"cwd":path,"entries":[],"error":"no permission"}
    return {"cwd":path,"entries":entries}

def read_file_b64(path,max_bytes=8*1024*1024):
    try:
        size=os.path.getsize(path)
        if size>max_bytes: return {"error":"too large"}
        return {"name":os.path.basename(path),"size":size,"data":base64.b64encode(open(path,"rb").read()).decode()}
    except Exception as e: return {"error":str(e)}

def run_action(action):
    try:
        if action=="lock":
            if sys.platform=="win32": subprocess.run(["rundll32.exe","user32.dll,LockWorkStation"])
        elif action=="shutdown":
            if sys.platform=="win32": subprocess.run(["shutdown","/s","/t","5"])
        elif action=="reboot":
            if sys.platform=="win32": subprocess.run(["shutdown","/r","/t","5"])
        elif action=="cancel_shutdown":
            if sys.platform=="win32": subprocess.run(["shutdown","/a"])
    except: pass

def open_path(target):
    target=(target or "").strip()
    if not target: return
    if not os.path.exists(target) and "://" not in target and not target.startswith(("\\","/",".")):
        target="https://"+target
    try:
        if sys.platform=="win32": os.startfile(target)
        elif sys.platform=="darwin": subprocess.run(["open",target])
        else: subprocess.run(["xdg-open",target])
    except: pass

def set_clipboard(text):
    def work():
        try: _tk_root.clipboard_clear(); _tk_root.clipboard_append(text); _tk_root.update()
        except: pass
    _post(work)

try:
    from websockets.asyncio.client import connect as ws_connect
except ImportError:
    from websockets import connect as ws_connect

def send_json(ws,o): return ws.send(json.dumps(o,ensure_ascii=False))

async def listen(ws,state,screen_grab,cam):
    async for msg in ws:
        if isinstance(msg,str):
            try: data=json.loads(msg)
            except: continue
            t=data.get("type")
            if t=="set_source": state["source"]=data.get("source","screen")
            elif t=="overlay": show_overlay(data.get("text",""),data.get("color","#fff"),data.get("bg","#f00"),data.get("speed",6),data.get("duration",10),data.get("pos","middle"))
            elif t=="exec": run_action(data.get("action",""))
            elif t=="open_path": open_path(data.get("target",""))
            elif t=="clipboard": set_clipboard(data.get("text",""))
            elif t=="blackout": set_blackout(bool(data.get("on",False)))
            elif t=="mouse": do_mouse(data)
            elif t=="list_dir":
                r=list_dir(data.get("path","")); r["type"]="dir_list"; await send_json(ws,r)
            elif t=="download":
                r=read_file_b64(data.get("path","")); r["type"]="file_data"; await send_json(ws,r)
            elif t=="chat": handle_chat(data.get("text",""))
            elif t=="wallpaper": set_wallpaper(data.get("data",""))
            elif t=="snapshot": await take_snapshot(ws,screen_grab,cam,state["source"])
            elif t=="key": do_key(data)
            elif t=="brightness": do_brightness(data.get("level",50))
            elif t=="process_list": await send_json(ws,{"type":"process_list","processes":list_processes()})
            elif t=="kill_process": kill_process(data.get("pid",0))
            elif t=="proof_interval": state["proof_interval"]=int(data.get("seconds",0))
            elif t=="tts": do_tts(data.get("text",""))
            elif t=="popup": do_popup(data.get("text",""),data.get("duration",5))
            elif t=="alarm": do_alarm(data.get("delay",60),data.get("text",""))
            elif t=="draw": draw_segment(data.get("x1",0),data.get("y1",0),data.get("x2",0),data.get("y2",0),data.get("color","#f00"))
            elif t=="draw_clear": draw_clear()

async def report_stats(ws,state):
    try: import psutil
    except: return
    last=psutil.net_io_counters(); last_t=time.time(); start=time.time()
    while True:
        await asyncio.sleep(3); now=time.time(); net=psutil.net_io_counters(); dt=now-last_t
        up=(net.bytes_sent-last.bytes_sent)/dt/1024; down=(net.bytes_recv-last.bytes_recv)/dt/1024
        last=net; last_t=now
        try: await send_json(ws,{"type":"stats","cpu":round(psutil.cpu_percent(interval=0),1),"mem":round(psutil.virtual_memory().percent,1),"up_kbps":round(up,1),"down_kbps":round(down,1),"uptime":int(now-start),"active":(now-last_activity)<30})
        except: return

async def stream(ws,state,screen_grab,cam):
    interval=1.0/max(1,CFG["fps"])
    from PIL import Image; import io as _io
    prev_small=None; fc=0; mcd=0; last_src=state["source"]; last_proof=time.time()
    while True:
        src=state["source"]
        if src!=last_src:
            if src=="screen": cam.release()
            last_src=src
        jpg=screen_grab() if src=="screen" else cam.grab()
        if jpg:
            try:
                await send_json(ws,{"type":"frame","source":src}); await ws.send(jpg)
                fc+=1
                if fc%15==0 and src=="camera":
                    try:
                        img=Image.open(_io.BytesIO(jpg)).convert("L").resize((80,60)); small=list(img.getdata())
                        if prev_small:
                            diff=sum(abs(a-b) for a,b in zip(small,prev_small))/len(small)
                            if diff>CFG["motion_threshold"] and mcd==0: await send_json(ws,{"type":"motion","text":"motion detected"}); mcd=10
                        prev_small=small
                    except: pass
                if mcd>0: mcd-=1
            except: return
        ps=state.get("proof_interval",0)
        if ps>0 and time.time()-last_proof>ps:
            last_proof=time.time(); await take_snapshot(ws,screen_grab,cam,state["source"])
        await asyncio.sleep(interval)

async def take_snapshot(ws,screen_grab,cam,source):
    jpg=screen_grab() if source=="screen" else cam.grab()
    if jpg: await send_json(ws,{"type":"proof","ts":int(time.time())}); await ws.send(jpg)

async def run_agent():
    sg=make_screen_grabber()
    if sg is None: return
    cam=Camera(); state={"source":CFG["source"]}
    uri=CFG["server"]; sep="&" if "?" in uri else "?"
    uri=f"{uri}{sep}token={CFG['token']}&role=agent"
    retry=2
    while True:
        try:
            async with ws_connect(uri,max_size=64*1024*1024,ping_interval=20,ping_timeout=20) as ws:
                retry=2; set_ws(ws,asyncio.get_event_loop())
                sw,sh=get_screen_size()
                await send_json(ws,{"type":"hello","role":"agent","name":socket.gethostname(),"os":platform.system().lower(),"screen_w":sw,"screen_h":sh})
                start_presence_listener()
                tasks=[asyncio.create_task(listen(ws,state,sg,cam)),asyncio.create_task(stream(ws,state,sg,cam)),asyncio.create_task(report_stats(ws,state))]
                done,pending=await asyncio.wait(tasks,return_when=asyncio.FIRST_COMPLETED)
                for t in pending: t.cancel()
        except Exception as e: log.error("reconnect in %ss: %s",retry,e); await asyncio.sleep(retry); retry=min(retry*2,30)

if __name__=="__main__":
    asyncio.run(run_agent())