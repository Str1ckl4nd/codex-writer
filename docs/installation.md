# 安装与编译

适用范围：一台 Mac 保存 Codex 任务，一台 Windows 通过 SSH 控制同一台 Mac。
这不是安装后就自动发现同账号电脑的产品。先准备好 SSH，再安装面板。

当前只提供源码准备流程，没有经过签名、公证和真实双机验收的正式安装包。
初次使用保持 `enable_handoff: false`；不要拿重要任务做首轮测试。

## 1. 环境要求

| 环境 | 要求 |
| --- | --- |
| Mac 运行 | Python 3.9+、OpenSSH、系统 `lsof`、`jq`、已安装 Codex/ChatGPT 桌面应用 |
| Mac 编译 | Xcode Command Line Tools，能使用 `xcrun swiftc` 和 AppKit；构建脚本的 `python3` 可用 |
| Windows 运行 | 原生 Windows PowerShell 5.1+、WinForms/.NET、OpenSSH Client、已安装 Codex/ChatGPT 桌面应用 |
| 双向管理 | Mac 的 SSH 服务，以及可供 Mac 检查/关闭 Windows 控制端的 Windows OpenSSH Server |
| 网络 | 用户自行维护的局域网或 Tailscale/VPN；正确的 SSH 用户、密钥、主机指纹和访问规则 |

Windows 面板不在 WSL 里运行。Linux 目前仅用于隔离的模拟测试，不是生产后端。
若组织策略禁止脚本执行或远程管理，应使用获准的部署方式，不要关闭安全策略绕过限制。

## 2. 获取源码

仅获得仓库访问权限的账户可获取当前私有源码；使用你已有的 GitHub Git 凭据：

```sh
git clone https://github.com/Str1ckl4nd/codex-writer.git
cd codex-writer
```

仓库不包含运行时依赖、用户配置、日志、Codex 登录信息或编译产物。
不要为了让其他人拉取代码而把自己的 GitHub/SSH 凭据发给对方。

## 3. 先配置 SSH

按 [SSH / Tailscale / VPN 指引](ssh-networking.md) 完成这两条连接：

- Windows → Mac：面板发送请求，Codex 桌面端访问 Mac 的 SSH 项目。
- Mac → Windows：识别 Windows 控制端；经用户确认后才能关闭精确匹配的原控制端。

在 Mac SSH 配置里登记 `windows-pc`，在 Windows SSH 配置里登记 `mac`，或使用你自己的
具名别名并同步修改下一节的配置。不使用 OpenAI 同账号设备配对作为替代通道。

当前被动发现器只读取主 SSH 配置文件中的具体 `Host` 块；请在这些块中直接写出实际
LAN/VPN IP。只存在于 `Include`、通配默认项或条件 `Match` 中的配置不能用于当前身份关联。
SSH 自身可用的 DNS/跳板配置，不代表本工具能安全关联被隐藏或转换的对端身份。

## 4. Mac：只编译，或安装独立版本

### 只编译

在源码目录运行：

```sh
sh scripts/build-macos.sh
```

输出仅为 `dist/Session Writer.app`。脚本会更新这个候选包及其临时签名，但：

- 不复制到 `/Applications` 或用户应用目录。
- 不启动应用，不修改任务写入权。
- 不覆盖原来的“接管”、Codex 或原个人工具目录。

构建使用本机 CPU 架构，进行 ad-hoc 签名和签名校验；这不是 Developer ID 签名、公证或
通用架构发行承诺。Gatekeeper 仍可能要求用户按系统正规流程确认来源，不应全局禁用保护。

### 手动安装独立版本

确认要安装新版本后才运行：

```sh
sh scripts/install-macos.sh
```

安装器会先构建，然后使用以下独立路径：

| 位置 | 内容 |
| --- | --- |
| `~/Applications/Session Writer.app` | 候选应用 |
| `~/.local/share/session-writer/` | Windows 经 SSH 调用的 Mac 后台 |
| `~/.local/bin/swriter` | 短命令入口；若已有同名命令则保留，不覆盖 |
| `~/.config/session-writer/config.json` | 首次复制的示例配置；已有配置保留 |

若上述应用或后台安装目录已存在，安装器会停止。请先人工备份独立版本再升级，勿指定
原软件目录作为替代安装位置。默认程序名保留为 **Session Writer**，仓库名为 **codex-writer**。

## 5. Windows：安装面板，无需编译 EXE

在原生 Windows PowerShell 的源码目录运行：

```powershell
powershell -NoProfile -File .\scripts\install-windows.ps1
```

面板使用系统 PowerShell/WinForms，运行时加载少量界面辅助类型，不要求另行安装 C++/Swift
编译器，也不生成单独的 EXE。

- 程序：`%LOCALAPPDATA%\Programs\SessionWriter\`。
- 配置：`%USERPROFILE%\.config\session-writer\config.json`。
- 开始菜单：**Session Writer**。
- 日志：`%LOCALAPPDATA%\SessionWriter\logs\`，与原个人工具分开。

若目标程序目录已存在，安装器会停止；已有配置与快捷方式不会被覆盖。安装器不修改 SSH
密钥、网络防火墙或 PowerShell 执行策略。如果中途失败，检查提示及这些独立路径后再处理，
不要盲目覆盖重装。

## 6. 填写两端配置

安装器只复制示例，不会把示例自动变成真实连接。两台机器分别编辑自己的
`~/.config/session-writer/config.json`，以 [config.example.json](../config.example.json) 为模板：

| 字段 | 填什么 |
| --- | --- |
| `codex_home` | Mac 任务数据目录，默认 `~/.codex` |
| `codex_app` | Mac 上实际应用路径，如 `/Applications/ChatGPT.app`；按本机安装调整 |
| `codex_binary`（可选） | Mac 后台二进制的自定义绝对路径；默认从应用路径推导 |
| `windows_ssh_alias` | Mac SSH 配置里的 Windows 主别名 |
| `windows_peer_aliases` | 同一 Windows 的明确别名列表，必须包含主别名；不要填其他电脑 |
| `mac_ssh_alias` | Windows SSH 配置和 Codex SSH 项目连接共同使用的 Mac 别名 |
| `mac_backend_path` | Mac 上独立安装后台的完整路径，示例中的 `YOUR_MAC_USER` 必须替换 |
| `mac_python` | Windows 远程执行 Mac 后台使用的 Python 绝对路径，需支持 Python 3.9+ |
| `enable_handoff` | 首次必须为 `false`；实际接管开关在 Mac 后台的配置中生效 |
| `excluded_thread_ids` | 可留空；只影响展示，不会把共享进程的实际影响从确认范围中抹掉 |

`SESSION_WRITER_CONFIG` 可指定其他配置文件；Mac 的 `SESSION_WRITER_STATE_DIR` 可指定独立状态目录。
Mac UI 还接受 `SESSION_WRITER_PYTHON`、`SESSION_WRITER_BACKEND` 环境覆盖。GUI 与 SSH 的环境可能不同，
不要仅在一个交互式 shell 里设置变量后，就假设所有启动方式都会继承。优先使用默认可共享的
配置位置；若默认 `/usr/bin/python3` 不满足要求，应明确配置相应启动环境。

旧 `account_discovery` 配置已无作用；旧账号设备缓存不会被读取。真实配置不得提交 Git。

## 7. 首次运行与应用

Mac 可以先在源码目录读取快照：

```sh
python3 app/session_writer.py snapshot --format json
```

此命令对任务只读，但会写本工具日志/缓存，也可能通过配置的 SSH 查询 Windows 身份。
如果找不到任务索引，应检查 `codex_home`；不能据此认定 Windows 离线。

安装后，Mac 打开 **Session Writer** 或执行 `~/.local/bin/swriter`；Windows 从开始菜单打开
**Session Writer**。在 Windows Codex 桌面端通过 SSH 项目连接打开对应的 Mac 任务，再刷新面板。

先逐项核对操作端主机、任务存储主机、当前占用者和 Windows 控制端。只有确认无误后，才在 Mac
配置中开启 `enable_handoff`。选中任务、选择目标、点击“应用”，核对完整影响范围再确认。
关闭原客户端可能影响多条任务；“强制接管并继续”必须另行选择，不是默认恢复策略。

## 8. 编译、测试和验收不是一回事

在符合你执行环境规则的隔离环境中运行：

```sh
PYTHONPATH=app python3 -m unittest discover -s tests
python3 scripts/release_check.py
```

PowerShell 解析和纯控制逻辑检查：

```powershell
pwsh -NoProfile -File scripts/check-powershell.ps1
```

这些检查使用模拟数据，不会接管真实任务。GitHub Actions 只允许手动触发；使用托管 runner
前自行检查配额、费用和执行环境政策。真实 GUI、真实双机交接和子代理恢复需要单独验收，
不得拿模拟测试替代。当前完成与未完成范围见 [verification.md](verification.md)。

## 9. 卸载与保留回执

先关闭本工具，保留需要核对的交接回执。然后人工处理本节列出的独立应用、程序目录和快捷方式。
Mac 的状态位于 `~/.local/state/session-writer/`；Windows 的状态位于上述独立日志目录。
配置和状态可以先备份，不要求删除。不要删除 `.codex`、SSH 配置、原“接管”或整个用户目录。
