# phone-pc-monitor

📱 手机远程监控电脑系统 — 用浏览器实时查看、远程控制你的 Windows PC，支持多设备。

> 本项目由 **AI 辅助开发**（豆包 Doubao），从 MVP 到 v1.0 共迭代 12 个版本，全部代码由 AI 根据需求编写、调试和优化。

## 特性

### 实时监控
- 🖥 屏幕实时推流（JPEG，可调帧率）
- 📷 摄像头切换（懒加载，不占用时自动释放）
- 🖥 多设备同时管理
- 📊 系统状态（CPU / 内存 / 网速 / 在岗检测）
- 🎥 移动侦测报警

### 远程控制
- 👆 画面直接点控鼠标（点哪指哪）
- 🎯 鼠标触控板（备用）
- ⌨️ 远程键盘输入
- ⚡ 常用快捷键（Ctrl+C/V/X/Z、Alt+Tab、Win+D）
- 📽 PPT 翻页控制

### 系统操作
- 🔒 锁屏 / ⏻ 关机 / 🔄 重启
- 🌑 黑屏模式
- 🌐 打开网址 / 文件
- 📋 远程剪贴板
- 🖼 换壁纸
- 🔊 音量控制
- ☀️ 屏幕亮度
- 📋 进程管理（查看/结束进程）

### 互动通知
- 📢 滚动字幕（颜色/速度/位置可调）
- 💬 弹窗气泡
- 🔊 TTS 语音播报
- ⏰ 定时闹钟提醒
- 💬 双向文字聊天

### 其他
- 📸 手动/定时截图存证
- ✏️ 远程屏幕涂鸦
- 📁 远程文件浏览/下载

## 架构

```
手机浏览器 (H5)  ←→  WebSocket中继 (Linux服务器)  ←→  PC Agent (Windows)
                        aiohttp                      Python (mss+OpenCV+pynput)
```

- **Server**: aiohttp WebSocket 中继 + 静态文件托管
- **Agent**: Python 采集端，截屏/摄像头/键鼠控制
- **Web**: 纯 HTML/CSS/JS，深色现代 UI，无框架

## 快速开始

### 1. 服务端

```bash
cd server
pip install -r requirements.txt
cp config.example.json config.json  # 编辑修改 token
python monitor-server.py
```

Nginx 反代（需要 WebSocket）：
```nginx
location / {
    proxy_pass http://127.0.0.1:8000;
    proxy_http_version 1.1;
    proxy_set_header Upgrade $http_upgrade;
    proxy_set_header Connection "upgrade";
}
```

### 2. 电脑端 (Windows)

```bash
cd agent
pip install -r requirements.txt
python monitor-agent.py
```

或打包成 exe 批量部署：双击 `build-exe.bat`

### 3. 手机端

浏览器打开 `https://你的域名/`，输入 token。

## 技术栈

| 端 | 技术 |
|---|---|
| Server | Python aiohttp, WebSocket |
| Agent | mss (截屏), OpenCV (摄像头), pynput (键鼠), Pillow, psutil |
| Web | 原生 HTML/CSS/JS, 深色 UI |

## 安全提醒

⚠️ 请务必修改默认 token，建议使用 HTTPS/WSS 部署。

## License

MIT