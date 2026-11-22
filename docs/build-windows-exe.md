# 用 GitHub Actions 打包 Windows exe（本地不用装 Python）

目标：在 GitHub 的 Windows 机器上自动生成 `phone-pc-monitor.exe`（单文件、无窗口/静默），你只负责下载。

## 前提

- 一个 GitHub 账号。
- 下面命令在有 git 的电脑/手机上执行（本机终端即可，不需要 Python）。

## 方案 A：仓库是你自己的（有推送权限）

在本仓库目录执行：

```bash
git add -A
git commit -m "feat: 局域网一体版 + 自动打包 Windows exe"
git push
```

推送成功后会**自动触发**打包；也可以手动点：

1. 打开仓库页面 → **Actions**
2. 左侧点 **打包 Windows exe** → 右上 **Run workflow** → 绿色按钮
3. 等 2~4 分钟，进入这次运行记录，页面底部 **Artifacts** → 下载 **phone-pc-monitor-windows**
4. 解压，得到 `phone-pc-monitor.exe` 和说明文件/热点脚本

## 方案 B：仓库不是你的（无推送权限）

1. 打开 https://github.com/keixiz/phone-pc-monitor → 右上 **Fork**（创建到你自己的账号下，例如 `<你的用户名>/phone-pc-monitor`）
2. 把本地仓库地址改到你的 fork，再推：
   ```bash
   git remote set-url origin https://github.com/<你的用户名>/phone-pc-monitor.git
   git add -A
   git commit -m "feat: 局域网一体版 + 自动打包 Windows exe"
   git push -u origin main
   ```
3. 打开你 fork 的仓库 → **Actions** → 允许启用工作流（首次会提示）→ 选 **打包 Windows exe** → **Run workflow**
4. 同样在运行记录底部下载 Artifact

## 想要「手机直接下载」的链接：发 Release

```bash
git tag v3.0
git push origin v3.0
```

工作流会把 `phone-pc-monitor-win.zip` 自动挂到 **Releases**，手机浏览器打开 Releases 页就能直接下。

## 拿到 exe 之后（目标电脑不用装 Python）

1. 解压到任意文件夹（例如 `桌面\PhonePC`），**路径别太深、别放 C 盘根目录**
2. 第一次：右键 `开放防火墙.bat` → **以管理员身份运行**（只做一次）
3. 双击 `phone-pc-monitor.exe`：**无窗口是正常的**，它就在后台运行
4. 同目录会自动生成：
   - `config.json`：端口、token、设备名、帧率
   - `访问地址.txt`：手机要打开的地址（含 token，自动登录）
   - `monitor-lan.log`：无窗口运行时的日志（排查问题看这里）
5. 手机连同一热点/局域网，打开 `访问地址.txt` 里的地址
6. 想开机自启：Win+R 输入 `shell:startup`，把 exe 的快捷方式拖进去

## 常见问题

| 现象 | 处理 |
|---|---|
| Actions 里没有「打包 Windows exe」 | 确认 `.github/workflows/build-windows-exe.yml` 已推送；fork 的仓库首次要去 Actions 页点启用 |
| 工作流失败 | 点开失败步骤看日志；最常见是 PyInstaller 打包步骤，把报错发我 |
| 下载的 exe 被杀毒报毒 | PyInstaller 单文件常见误报，加白名单；担心的话改用「装 Python + 后台启动.vbs」 |
| 双击 exe 没反应 | 正常，无窗口；看同目录 `monitor-lan.log` 和 `访问地址.txt`；确认任务管理器里有 phone-pc-monitor.exe |
| 手机打不开 | 防火墙放行了吗；`访问地址.txt` 的 IP 是否当前网段；端口是否被占用（日志里有） |
| 电脑开着但没有热点 | 电脑「移动热点」需要电脑本身有网络；没有外网就改用手机热点，让电脑连手机 |

## 不想装/打 exe 的替代

目标电脑装一次 Python（勾选 Add to PATH），双击 `启动.bat` 自动装依赖；以后用 `后台启动.vbs` 静默运行（同样无窗口，日志同样写 `monitor-lan.log`）。
