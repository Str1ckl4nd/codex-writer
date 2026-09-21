# Codex Writer · 接管

用于 **Mac + Windows、用户自行配置 SSH** 场景的 Codex 任务写入者诊断与交接工具。

SSH-only writer diagnostics and explicit handoff for one Mac-hosted Codex task
and a Windows controller. Independent, experimental, and not affiliated with OpenAI.

> 当前仓库为 **private 开发预览版**，不是稳定发行版。默认禁止交接，打开面板或自动刷新不会自动转移写入权。不要用重要任务做首次验收。

## 为什么要用它

你在 Mac 上保存和运行 Codex 任务，又通过 Windows 的 Codex 桌面端经 SSH 打开同一台 Mac。
切回 Mac 后，可能看得到任务，却因为另一个后台进程仍占有写入权而不能继续。

“SSH 能连接”“任务能显示”“当前客户端能写入”是三个不同条件。
盲目关闭所有 Codex 进程可能中断其他任务；直接删除锁文件也不是安全的交接方式。

Codex Writer 的用途是把这些状态分开呈现：**谁在操作、数据在哪台机器、哪个进程占用任务、
交接会影响哪些任务**，再由用户明确选择是否应用。

## 明确适用的场景

- 一个用户管理一台 Mac 和一台 Windows 电脑，使用自己的系统账户和 SSH 配置。
- 任务数据保存在 Mac；Windows 已通过 **SSH 项目连接**访问这台 Mac。
- 希望查看真实占用者，或者在两个桌面控制端之间交接同一个 Mac 任务的写入权。
- 原控制端有活动任务或子代理时，需要确认影响范围、记录中断，并按需请求继续。

**任务文件和代码不会因此迁移到 Windows。** 本工具不是把任务的执行环境搬到另一台电脑，
也不允许两个进程同时写入同一任务。

### 不适用的场景

- 两台电脑各有一份独立任务历史，需要复制、合并、备份或双向同步。
- OpenAI 官方同账号设备配对、手机远控、只能经过 OpenAI 中转连接的机器。
- 不配置 SSH，只希望软件扫描局域网、登录 Tailscale 账号或自动穿透网络。
- 多用户协作、多台 Windows/多台 Mac 的通用管理，以及分布式锁服务。
- 绕过登录、付费额度、安全审批、系统权限或 Codex 的父子代理所有权规则。

## 功能

- Mac 原生表格和 Windows 面板，分栏显示任务名称、ID、创建时间、当前占用主机。
- 自动刷新、SSH 主机重新读取、基本排错和本地日志。
- 分开显示“SSH 已配置”“代理已连接”“满足接管条件”。
- 选择目标、核对完整影响列表，再通过新鲜状态校验应用交接。
- 可选强制接管：核对进程身份和共享进程影响范围后关闭原控制端。
- 中断恢复回执：对子代理的继续请求交给其主代理处理；**已提交请求不等于全部恢复成功**。

当前列表显示 Mac 最近 60 条主任务及已占用主任务；底层索引读取范围更大，见数据说明。

## SSH-only 边界

机器列表来自用户 `~/.ssh/config` 的具名主机块，不来自 Codex 账号设备列表。
跨机检查和交接控制全部走 SSH；Mac 上的本地状态读取仍使用本地系统接口和进程间通信。

不读取 OpenAI 登录令牌，不提供账号发现开关，不调用账号机器 API，也不自动回退到 OpenAI 中转。
跨网请自行准备 Tailscale 或 VPN；软件不会替你注册设备、开放公网端口或关闭主机指纹检查。

“SSH-only”只描述本工具的机器发现与跨机控制，**不表示 Codex 模型调用离线**。
恢复任务可能由 Codex 正常访问模型服务并消耗额度。

## 安装、编译和开始使用

先阅读 [安装与编译指南](docs/installation.md) 和 [SSH / Tailscale / VPN 配置](docs/ssh-networking.md)。

从有权限访问此私有仓库的账户获取源码：

```sh
git clone https://github.com/Str1ckl4nd/codex-writer.git
cd codex-writer
```

Mac **只编译，不安装、不启动**：

```sh
sh scripts/build-macos.sh
```

产物是仓库内的 `dist/Session Writer.app`。保留这个内部应用名是为了沿用独立安装目录；
仓库名称是 `codex-writer`。编译不会覆盖你原来的“接管”或 Codex。

需要安装独立版本时，才手动运行：

```sh
# Mac
sh scripts/install-macos.sh
```

```powershell
# Windows：原生 Windows PowerShell 5.1+，不是 WSL
powershell -NoProfile -File .\scripts\install-windows.ps1
```

Windows 版本是 PowerShell/WinForms 面板，不需要单独编译 EXE。
两端都要编辑自己的 `~/.config/session-writer/config.json`，填写 SSH 别名、Mac 后台路径和 Python 路径。
先保持 `enable_handoff: false`，核对只读快照；明确验收两端身份后再开启。

Mac 安装后的短命令为 `swriter`；Windows 从开始菜单打开 **Session Writer**。
若 `swriter` 不在 PATH，使用 `~/.local/bin/swriter`。

## 会读取、保存和传输哪些数据

完整说明：[数据访问与隐私](PRIVACY.md)。

| 范围 | 实际用途 |
| --- | --- |
| 本工具配置、SSH 配置 | 路径、主机别名、地址、用户和端口；OpenSSH 自行使用用户配置的认证材料 |
| Codex 本地任务索引 | 任务 ID、名称/标题、摘要、时间、来源、归档状态和代理关系 |
| 系统进程、文件句柄、连接信息 | 主机名、PID、父进程、启动时间、命令行、锁文件名和 SSH 对端地址 |
| Codex 本地状态接口 | 运行状态和回合 ID；桌面快照可能临时包含对话历史，不保证从不接收对话内容 |
| 本地日志、身份缓存和交接回执 | 排障、核对影响范围、防止重复接管或重复发送继续 |
| SSH 两端传输 | Windows 身份摘要、任务列表、诊断结果、交接指令和按需导出的日志 |

没有项目自己的遥测、广告、远端日志收集或维护者服务器。
结构化日志过滤不等于彻底匿名化；主机名、任务名称、任务 ID、IP 或错误说明仍可能敏感。

## 风险与验收状态

交接可能关闭原 Codex 客户端，影响同一进程中的多个任务，并中断尚未完成的工作。
强制接管不是事务回滚；已启动的命令可能继续运行，已发生的文件改动不会自动恢复。

- 已验证：隔离 VM 中的单元/模拟测试、PowerShell 解析与控制逻辑；Mac 候选应用编译和签名检查。
- 尚未验证：全新用户环境安装、两台真实机器上的完整 GUI 操作、真实任务强制交接及全部子代理恢复。
- GitHub 托管 CI 仅手动触发，不在私有仓库首次推送时自动运行。
- 具体记录见 [验证状态](docs/verification.md)。测试通过不等于真实双机交接已通过验收。

Codex 的本地数据格式和桌面通信可能随版本改变。身份、状态或送达结果不能确认时，
应停止并检查日志，不要删除锁、清掉回执或反复点击强制接管。

## 免责声明、版权与贡献

- [免责声明](DISCLAIMER.md)：独立项目、实验性质、操作风险、无保证和责任范围。
- [版权与第三方声明](NOTICE)：版权归属、第三方软件边界、商标与 AI 辅助开发说明。
- [MIT License](LICENSE)：Copyright (c) 2026 Str1ckl4nd and Codex Writer contributors。
- [安全说明](SECURITY.md) · [贡献指南](CONTRIBUTING.md) · [相关工具](docs/ecosystem.md)。

MIT 是代码的许可，不是 OpenAI 服务的授权或背书，也不把当前私有仓库变成公开仓库。
后续公开仓库或发布安装包需要维护者另行确认；不会因构建或推送自动公开。
