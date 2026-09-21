# SSH / Tailscale / VPN 配置

Codex Writer 只使用用户自行登记的 SSH 连接，不使用 OpenAI 同账号设备目录、配对或中转。
工具只需要在操作端运行一份。其他控制端不必安装或打开本工具；SSH 请求会发送并执行
一次性进程检测/关闭辅助代码，而不是部署常驻代理。

当前数据后端支持 macOS；控制端可以是若干台 Windows 或 Mac。
Windows 入口可以按需在选定 Mac 上临时执行自带后端，不需要预装远端后台文件。
有多个独立任务库时应选择相应的数据主机配置；本工具不自动复制或合并这些数据库。

## 1. 用户准备网络和 SSH

使用局域网，或自行配置 Tailscale / 其他 VPN。VPN 连通不等于 SSH 服务已经启用；
目标仍需要 SSH 服务、正确用户、认证和访问规则。
[Tailscale 连接指引](https://tailscale.com/docs/how-to/connect-to-devices)

通常使用普通 OpenSSH 连接 LAN/VPN 地址即可，不要求启用另一个名为 “Tailscale SSH”
的服务；后者有自己的服务端平台与授权要求。
[Tailscale SSH 文档](https://tailscale.com/docs/features/tailscale-ssh)

SSH 服务设置参考 [macOS Remote Login](https://support.apple.com/en-au/guide/mac-help/mchlp1066/mac)
和 [Windows OpenSSH](https://learn.microsoft.com/en-us/windows-server/administration/openssh/openssh_install_firstuse)。
不要向公网直接暴露 Codex app-server 或本地 IPC。
[OpenAI 的 SSH 连接说明](https://learn.chatgpt.com/docs/remote-connections#connect-to-an-ssh-host)

本工具不登录 Tailscale 账号、不修改 VPN 策略，也不承诺底层网络不使用其自己的中继。
“不走 OpenAI 中转”描述的是本工具的接管通道，不是所有网络产品的实现。

## 2. 在数据主机上登记若干个控制端

下面都是占位示例。将地址替换为你实际的 LAN/VPN IP，填写正确的系统用户和已有密钥。

```sshconfig
Host office-pc
  HostName 192.0.2.10
  User YOUR_WINDOWS_USER
  IdentityFile ~/.ssh/YOUR_EXISTING_KEY
  StrictHostKeyChecking yes

Host travel-mac
  HostName 192.0.2.11
  User YOUR_MAC_USER
  IdentityFile ~/.ssh/YOUR_EXISTING_KEY
  StrictHostKeyChecking yes

Host lab-pc
  HostName 192.0.2.12
  User YOUR_WINDOWS_USER
  IdentityFile ~/.ssh/YOUR_EXISTING_KEY
  StrictHostKeyChecking yes
```

通过可信渠道核对并信任主机指纹。未知或变化的指纹应由用户解决，不可自动忽略。
本工具不把私钥、known_hosts 或 SSH 配置打包发送到其他机器。

被动发现器只读取主配置文件里的具体 `Host` 块，不执行 `Include`、通配规则、
`Match exec` 或 `ProxyCommand`。请在这些具体块里填写实际 IP。
DNS 或跳板连接能被 SSH 使用，不代表本工具能确认经过地址转换的原始控制端。
无法唯一关联时会停止，不靠主机名称相似来猜测。

## 3. 控制端连接任务数据主机

每个控制端用自己的 SSH 配置访问数据主机，别名可以不同。例如其中一台使用：

```sshconfig
Host work-mac
  HostName 192.0.2.20
  User YOUR_MAC_USER
  IdentityFile ~/.ssh/YOUR_EXISTING_KEY
  StrictHostKeyChecking yes
```

在该控制端的 Codex 桌面应用中建立相应的 **SSH 项目连接**。
这不是 OpenAI 的同账号 **Control other devices** 配对。

在工具配置中，用 `backend_alias` 指出每个控制端实际使用的数据主机别名：

```json
{
  "ssh_controllers": [
    {"alias": "office-pc", "platform": "windows", "backend_alias": "mac"},
    {"alias": "travel-mac", "platform": "mac", "backend_alias": "work-mac"},
    {"alias": "lab-pc", "platform": "windows", "backend_alias": "storage"}
  ],
  "enable_handoff": false
}
```

这里的 `alias` 在数据主机的 SSH 配置中解析；`backend_alias` 是对应控制端访问数据主机
使用的别名。可用 `peer_aliases` 为同一物理机器声明多个地址入口，但不能把同一别名分配
给不同机器，也不能用同一个来源 IP 猜测几台不同机器的身份。

## 4. 核对身份，再开启交接

先用普通 SSH 的只读命令核对正确的系统主机名，例如：

```sh
ssh -o BatchMode=yes -o StrictHostKeyChecking=yes office-pc hostname
```

工具中的状态含义：

- **已配置**：用户登记了 SSH 主机，尚不代表在线。
- **已连接**：观察到通往任务服务的 SSH 代理，尚不代表可安全接管。
- **可接管**：相应控制端进程及连接已核验；应用前还会重新检查。
- **共享 SSH**：若干个已登记控制端共用一个后台，不能把该 PID 归给任意一台。

在操作端配置中显式开启 `enable_handoff` 后，选择目标并核对完整影响列表。
切换到一个 SSH 控制端时保留目标及共享后台；切回本地桌面可能需要关闭多个源控制端。
成功以实际写入资源和目标身份核验为准，不是仅写入一条本地记录。

仅能经 OpenAI 同账号中转访问的机器仍不在支持范围内。
Codex 恢复任务时的正常模型网络流量与本工具的 SSH 通道是两回事。
