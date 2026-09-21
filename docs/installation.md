# 安装与编译

只需选择一个操作端运行 Codex Writer，不要求每个 SSH 控制端安装本工具。
远端进程检测和关闭使用随 SSH 请求发送的一次性辅助代码，不部署常驻服务。

当前数据读取后端支持 macOS；可以核验若干个 Windows/macOS SSH 控制端。
Mac 入口在本机任务环境工作；Windows 入口通过 SSH 临时执行自带后端。
本工具不把不同机器的任务库同步或合并。

## 1. 环境要求

| 位置 | 要求 |
| --- | --- |
| Mac 操作端 / 数据主机 | Python 3.9+、OpenSSH、系统 `lsof`、`jq`、已有 Codex/ChatGPT 桌面环境 |
| Mac 编译 | Xcode Command Line Tools、`xcrun swiftc`、AppKit、可用的 `python3` |
| Windows 操作端 | 原生 Windows PowerShell 5.1+、WinForms/.NET、OpenSSH Client |
| 被管理的 Windows 控制端 | SSH Server、PowerShell、已有 Codex 桌面及其 SSH 项目连接；不要求安装本工具 |
| 被管理的 Mac 控制端 | SSH Server、Python 3.9+、已有 Codex 桌面及其 SSH 项目连接；不要求安装本工具 |
| 网络 | 用户自行维护的 LAN、Tailscale 或 VPN，以及正确的 SSH 用户、密钥、指纹和访问规则 |

Windows 面板不在 WSL 内运行。Linux 目前用于隔离模拟测试，不是受支持的数据后端。
若组织策略限制脚本或远程管理，应使用获准的部署方式，不要关闭安全策略绕过限制。

## 2. 获取源码

```sh
git clone https://github.com/Str1ckl4nd/codex-writer.git
cd codex-writer
```

源码不包含用户配置、运行日志、任务数据库、登录材料或编译产物。
不要分享自己的 GitHub/SSH 凭据来替代仓库或机器的正常访问授权。

## 3. Mac：编译与安装是分开的

只编译：

```sh
sh scripts/build-macos.sh
```

产物仅为仓库内的 `dist/Session Writer.app`。脚本会更新该构建目录和 ad-hoc 签名，
但不安装、不启动应用、不改变任务写入权，也不覆盖其他已安装应用。

构建使用当前 CPU 架构；ad-hoc 签名校验不等于 Developer ID 签名、公证或通用架构发行。
若系统要求确认来源，按正规系统流程处理，不要全局关闭 Gatekeeper。

需要安装时才运行：

```sh
sh scripts/install-macos.sh
```

| 位置 | 内容 |
| --- | --- |
| `~/Applications/Session Writer.app` | 独立应用 |
| `~/.local/share/session-writer/` | 本地后台与辅助代码 |
| `~/.local/bin/swriter` | 短命令；已有同名命令保留 |
| `~/.config/session-writer/config.json` | 首次复制的示例配置；已有配置保留 |

已有目标应用或后台目录时，安装器会停止，不自动覆盖。先人工备份需要升级的本工具安装，
不要把其他软件的目录当作安装目标。

## 4. Windows：单端安装，不预装远端后台

在原生 Windows PowerShell 的源码目录运行：

```powershell
powershell -NoProfile -File .\scripts\install-windows.ps1
```

面板使用 PowerShell/WinForms，运行时加载少量界面辅助类型，不需要另行编译 EXE。
安装器把 Python 后端源码一起放在 Windows 工具目录里，供 SSH 请求按需发送：

- 程序：`%LOCALAPPDATA%\Programs\SessionWriter\`。
- 配置：`%USERPROFILE%\.config\session-writer\config.json`。
- 开始菜单：**Session Writer**。
- 日志：`%LOCALAPPDATA%\SessionWriter\logs\`。

默认不设置 `mac_backend_path`。每次请求将受限的程序文件、允许的工具设置和操作参数
经 SSH 发送到选定数据主机，在临时目录运行并于正常退出时清理。不会安装远端工具、服务
或更新用户的 SSH/Codex 配置。异常终止可能留下临时代码目录，见 [数据说明](../PRIVACY.md)。

已有程序目录时安装器停止；已有配置和快捷方式不会被覆盖。安装器不修改 SSH 密钥、
防火墙或 PowerShell 执行策略。

## 5. 配置若干个控制端

先按 [SSH / Tailscale / VPN 指引](ssh-networking.md) 配置连接，再编辑运行入口使用的
`~/.config/session-writer/config.json`。以 [config.example.json](../config.example.json) 为模板：

```json
{
  "codex_home": "~/.codex",
  "codex_app": "/Applications/ChatGPT.app",
  "mac_ssh_alias": "mac",
  "mac_python": "/usr/bin/python3",
  "ssh_controllers": [
    {"alias": "office-pc", "platform": "windows", "backend_alias": "mac"},
    {"alias": "travel-mac", "platform": "mac", "backend_alias": "work-mac"},
    {"alias": "lab-pc", "platform": "windows", "backend_alias": "storage"}
  ],
  "enable_handoff": false,
  "excluded_thread_ids": []
}
```

| 字段 | 含义 |
| --- | --- |
| `codex_home` / `codex_app` | 当前数据主机上的任务目录与应用路径 |
| `codex_binary`（可选） | 数据主机上自定义 Codex 二进制绝对路径 |
| `mac_ssh_alias` | Windows 操作端访问数据主机使用的 SSH 别名 |
| `mac_python` | 数据主机上的 Python 绝对路径，需支持 Python 3.9+ |
| `ssh_controllers[].alias` | 数据主机 SSH 配置里的控制端主别名 |
| `ssh_controllers[].platform` | 控制端平台：`windows` 或 `mac` |
| `ssh_controllers[].backend_alias` | 此控制端的 Codex SSH 项目实际使用的数据主机别名 |
| `ssh_controllers[].peer_aliases`（可选） | 同一物理机器的其他 SSH 入口别名，必须包含主别名 |
| `enable_handoff` | 明确开启交接；首次保持 `false` |
| `excluded_thread_ids` | 只影响展示；不会从共享进程的影响确认中抹去任务 |
| `mac_backend_path`（可选） | 高级兼容模式：调用已经存在的远端后台。留空使用默认临时后端，无需远端安装 |

每份配置最多 32 个控制端，同一别名不能属于不同机器。具名 SSH 主机可以被列出，但只有
声明的平台、实际连接和进程身份均核验后才可接管；不能把“已配置”当作“在线”。

Windows 的默认临时后端使用本次操作端配置；不要求远端再维护一份工具配置。
数据主机用于核验和联系其他控制端的 SSH 条目、认证与访问权限仍由用户维护。
被动发现不解释 Include/Match/通配规则或猜测跳板后的原始身份，请使用具体 Host 块和实际 IP。

`SESSION_WRITER_CONFIG` 可覆盖入口的配置文件；数据主机的 `SESSION_WRITER_STATE_DIR`
可覆盖本工具状态目录。Mac UI 还接受 `SESSION_WRITER_PYTHON` 和 `SESSION_WRITER_BACKEND`。
GUI 和 SSH 不一定继承相同环境，不要假设交互式 shell 的设置自动适用于所有启动方式。

旧 `windows_ssh_alias` / `windows_peer_aliases` 仍可描述一个兼容控制端；
新增配置使用 `ssh_controllers`。账号发现开关和账号缓存不再被使用。不要提交真实配置。

## 6. 首次运行与交接

先用可丢弃任务验收，保持交接关闭。在 Mac 源码目录可读取快照：

```sh
python3 app/session_writer.py snapshot --format json
```

该操作对任务只读，但会查询已登记的相关 SSH 控制端，并更新工具缓存和日志。

Mac 打开 **Session Writer** 或执行 `~/.local/bin/swriter`；Windows 从开始菜单打开
**Session Writer**。其他机器只需要其正常的 Codex SSH 项目连接，不必同时打开本工具。

核对当前操作端、任务数据主机和实际占用者后，才开启 `enable_handoff`。
选择目标、点击“应用”、确认完整影响范围。若多个控制端共享后台，确认页面会列出需要关闭
的控制端，并保留选中的目标。强制恢复需另行选择，继续请求送达不等于全部代理已恢复。

## 7. 测试与真实验收

在符合执行环境规则的隔离环境运行：

```sh
PYTHONPATH=app python3 -m unittest discover -s tests
python3 scripts/release_check.py
```

```powershell
pwsh -NoProfile -File scripts/check-powershell.ps1
```

这些检查用模拟数据，不接管真实任务。GitHub Actions 仅手动触发。
真实 GUI、多台物理机器的完整 SSH 路径、强制交接和子代理恢复需要另行验收；
当前证据见 [验证状态](verification.md)。

## 8. 卸载

关闭本工具后，仅处理上述独立安装目录、应用和快捷方式。
先保留并核对未完成回执；数据主机的状态默认在 `~/.local/state/session-writer/`，
Windows 日志在独立的 `SessionWriter\logs` 目录。不要删除 Codex 任务库、SSH 配置或其他应用。
