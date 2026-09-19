# Phone PC Monitor

通过手机浏览器远程查看和控制电脑：实时屏幕 / 摄像头画面、鼠标键盘远程操作、系统命令执行、屏幕字幕推送等。纯 Web 方案，手机端无需安装 App，打开浏览器即用。

> 本项目在开发过程中使用了 AI 编程助手（豆包）辅助编写，由作者主导架构设计与调试。

## 功能一览

**实时画面**
- 屏幕 / 摄像头切换，JPEG 帧通过 WebSocket 低延迟推流
- 多设备列表，一台服务器可同时挂多台电脑
- 实时帧率、CPU / 内存 / 网络速率显示
- 在岗 / 离开状态检测

**远程控制**
- 画面直接触摸操作鼠标（单击左键、双击右键、滑动移动）
- 触控板模式（滑动移动、单击左键、双击右键）
- 远程键盘输入、常用快捷键（Ctrl+C/V/X/Z、Alt+Tab、Win+D 等）
- PPT 翻页遥控（上一页 / 下一页 / F5 / Esc）

**系统操作**
- 锁屏、关机、重启、取消关机、黑屏
- 音量调节（+/−/静音）、亮度调节
- 剪贴板推送、打开网址 / 本地路径
- 进程列表查看与结束
- 文件浏览与下载
- 更换壁纸

**互动通知**
- 屏幕滚动字幕（自定义文字、背景 / 文字颜色、上中下位置、速度、时长）
- 屏幕角落弹窗气泡
- 双向聊天（手机 ↔ 电脑桌面弹窗）
- TTS 语音喊话（电脑朗读文字）
- 闹钟提醒（1 / 5 / 10 / 30 分钟）
- 屏幕涂鸦（在桌面上手绘、多种颜色、清空）
- 定时截图存证（5 / 15 / 30 分钟自动截图到服务器）

## 架构

```
手机浏览器 (H5)
     │  wss
     ▼
Linux 服务器 (aiohttp WebSocket 中继 + 静态文件 + 截图存储)
     │  wss
     ▼
Windows 电脑 Agent (mss 截屏 / OpenCV 摄像头 / pynput 键鼠)
```

三端解耦：服务器只做消息中继，不处理画面内容；电脑端负责采集和执行；手机端纯前端。

## 快速开始

### 1. 服务端（Linux）

```bash
cd server
pip3 install -r requirements.txt
cp config.example.json config.json
# 编辑 config.json，设置一个强随机 token
python3 monitor-server.py
```

通过 Nginx 反代并启用 WebSocket 升级即可对外提供服务，建议配 HTTPS。

### 2. 电脑端（Windows）

```bash
cd agent
pip install -r requirements.txt
pip install opencv-python numpy
cp config.example.json config.json
# 编辑 config.json，填入服务器地址和 token
python monitor-agent.py
```

也可用 PyInstaller 打包成 exe 静默运行（见 `build-exe.bat` 和 `start-hidden.vbs`）。

### 3. 手机端

浏览器打开服务器域名，输入 token 即可。支持"添加到主屏幕"。

## 技术栈

| 端 | 技术 |
|---|---|
| 服务端 | Python 3.8+ / aiohttp / WebSocket |
| 电脑端 | Python 3.8+ / mss / OpenCV / pynput / psutil / pywin32 |
| 手机端 | 原生 HTML / CSS / JavaScript（无框架） |

## 安全提醒

- 请务必使用强随机 token，并通过 HTTPS / WSS 部署
- 本工具用于监控你自己或已获授权的设备，滥用责任自负
- 开源版本中的服务器地址和 token 均为占位符，使用前请自行修改

## License

MIT