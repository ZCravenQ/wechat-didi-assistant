# Windows 部署

适用于 Windows 10/11，以及有桌面会话的 Windows Server。使用与 Linux 相同的程序，保留微信确认下单、订单查询/取消、高德城市识别、长期记忆和本地管理界面。

## 1. 安装运行环境

- [Python 官方下载](https://www.python.org/downloads/windows/)：Python 3.11 或更新版本，安装时勾选加入 PATH。
- [Node.js 官方下载](https://nodejs.org/en/download)：Node.js 22 或更新的 LTS 版本，包含 npm。
- [下载仓库 ZIP](https://github.com/ZCravenQ/wechat-didi-assistant/archive/refs/heads/main.zip)，解压到自己的文件夹。也可以使用 Git 克隆。

安装后重新打开 **Command Prompt（CMD）**，进入解压后的项目目录，例如：

```cmd
cd /d "D:\Projects\wechat-didi-assistant-main"
python --version
node --version
npm --version
windows.cmd Install
```

默认以当前 Windows 用户安装，不需要管理员权限。入口脚本仅在本次 PowerShell 进程使用 `ExecutionPolicy Bypass`，不会修改系统执行策略；请只运行自己检查过的本仓库脚本。组织设备若限制脚本执行或任务计划，应遵循设备策略。

安装器只复制程序白名单和空配置模板，不复制源目录里的 `.env` 或 `.runtime`。默认目录为：

```text
%LOCALAPPDATA%\WechatDidiAssistant\
  app\                 程序及固定版本 npm 依赖
  config\.env          模型、滴滴、高德等配置
  data\                微信授权、SQLite、长期记忆、界面设置
  installation.json    本机 Python / Node.js 路径
```

安装目录的访问权限限制为当前用户、SYSTEM 和本机管理员。不要将安装目录设为网盘共享文件夹或 Git 仓库。

## 2. 填写配置并授权微信

```cmd
notepad "%LOCALAPPDATA%\WechatDidiAssistant\config\.env"
```

填写 `LLM_BASE_URL`、`LLM_API_KEY`、`LLM_MODEL`、`DIDI_MCP_KEY`，全国地点搜索需要 `AMAP_API_KEY`。申请链接和各项配置见 [README 的配置表](README.md#必需配置与申请链接)。先保留 `DIDI_ENV=sandbox`；值不要额外加引号。

回到源码目录的 CMD，依次执行：

```cmd
windows.cmd MemoryCheck
windows.cmd Tools
windows.cmd Login
windows.cmd Run
```

`MemoryCheck` 用临时文件检查官方记忆组件；`Tools` 检查滴滴权限；`Login` 输出腾讯二维码内容/图片地址，用自己的微信扫码确认；`Run` 启动微信服务。登录二维码和凭证不要发到公开 Issue。

保持运行窗口打开，浏览器访问 [http://127.0.0.1:37691](http://127.0.0.1:37691)。终止前台服务按 **Ctrl+C**；关闭窗口、电脑休眠或注销登录会中断服务。电脑需要持续联网。

也可以只启动管理界面，或者先在终端测试对话：

```cmd
windows.cmd UI
windows.cmd Console
```

`Run` / `UI` / `Console` 每次只启动一个。重新登录和安装更新前先停止服务。需要真实叫车时，在完成沙箱验证后修改 `DIDI_ENV=production` 并重启；每一单仍需微信确认。

## 3. 可选：登录 Windows 后自动运行

先配置 Key 并完成微信扫码，再运行：

```cmd
windows.cmd EnableAutostart
```

这会创建仅属于当前用户的计划任务，**从下次登录该 Windows 账号开始**隐藏运行。任务不保存密码、不使用管理员权限；不是未登录即可运行的系统服务。当前前台窗口仍需按正常方式停止，以免重复运行。

后台输出记录在 `data\service.log`，可能含运行状态信息，请勿公开。计划任务在进程异常退出后最多重试 3 次，间隔 1 分钟；它不会重新授权或重放未确认订单。长期运行时请定期检查日志大小并在停止服务后归档。

停止后台任务并删除自动启动设置：

```cmd
windows.cmd DisableAutostart
```

此命令保留配置和记忆。如果组织策略拒绝创建任务，可使用前台 `windows.cmd Run`。Windows Server 如需在无人登录时运行，需另行配置合适的服务账号/服务宿主；当前脚本不保存账号密码。

## 4. 自定义目录、更新与备份

可选择独立目录，之后每次使用相同参数：

```cmd
windows.cmd Install -InstallRoot "D:\Apps\WechatDidiAssistant"
windows.cmd Login -InstallRoot "D:\Apps\WechatDidiAssistant"
windows.cmd Run -InstallRoot "D:\Apps\WechatDidiAssistant"
```

更新时先停止前台服务或执行 `DisableAutostart`，备份 `config` 和 `data`，下载新版到一个新的源码文件夹，再执行 `windows.cmd Install`，指向原安装目录。已有 Key、微信授权和记忆会保留；重新启用自动启动需再次执行 `EnableAutostart`。

源码文件夹可在安装后移走；已安装的入口位于 `%LOCALAPPDATA%\WechatDidiAssistant\app\windows.cmd`。不要从这个 `app` 目录执行安装更新。Python / Node.js 安装路径改变后，从新源码目录重新安装以更新路径记录。

备份前停止服务以保证 SQLite 和记忆一致。备份包含 Key、登录凭证和个人信息，不要上传 GitHub。不要通过删除数据库处理下单超时，应先在滴滴 App 核实订单。

## 5. 常见问题

| 现象 | 处理 |
| --- | --- |
| 找不到 Python / Node.js / npm | 安装上面的运行环境，重新打开 CMD；检查 PATH |
| Python 命令打开 Microsoft Store | 安装真实 Python，调整 PATH / 应用执行别名；安装脚本也支持没有 `python.exe` 时使用 `py -3` |
| Node 版本检查失败 | 升级至 Node.js 22+，重新打开 CMD |
| 提示已有服务或安装时目录被占用 | 停止前台窗口和本项目计划任务后再试 |
| 管理端口被占用 | 在 `config\.env` 修改 `ADMIN_PORT`，重启并访问新端口 |
| 高德 Key 改了却未生效 | 管理界面保存值优先于 `.env`；在界面直接修改 Key |
| Git 上传/下载报错 | 部署可直接下载 ZIP，不依赖 Git |

管理界面只绑定 `127.0.0.1`，不需要允许 Windows 防火墙开放入站管理端口。远程 Windows 服务器可通过远程桌面在服务器本机浏览器管理，或使用已配置的 SSH 本地端口映射；不要直接将管理页面暴露到公网。
