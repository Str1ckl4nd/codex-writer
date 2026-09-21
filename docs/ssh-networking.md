# SSH / Tailscale / VPN setup

Session Writer uses **user-registered SSH connections only** for machine
discovery and cross-machine control. It does not use OpenAI's account machine
directory, device pairing, relay service, or authentication tokens. A machine
shown in Codex's same-account Remote list is not automatically supported here.

The current pair is one Mac storing the tasks and one Windows controller. The
Windows client sends tool requests to the Mac over SSH. The Mac uses the configured
reverse SSH connection only for Windows controller inspection and confirmed
closure. Local Mac process/IPC inspection remains local.

## 1. Bring your own network and SSH service

Use your LAN or independently configure Tailscale or another VPN. This project
does not install, enroll, discover, or manage devices through those products.
VPN connectivity does not by itself start an SSH server; the destination must
also run the SSH service. See [Tailscale's connection guide](https://tailscale.com/docs/how-to/connect-to-devices).

For this Mac/Windows pair, use ordinary OpenSSH over the LAN/VPN addresses. This
does **not** require the separately named Tailscale SSH server feature, whose
supported server platforms differ from ordinary SSH over Tailscale.
[Tailscale SSH documentation](https://tailscale.com/docs/features/tailscale-ssh)

Enable the OS's SSH service only for the user accounts and networks you intend
to administer: [macOS Remote Login](https://support.apple.com/en-au/guide/mac-help/mchlp1066/mac)
and [Windows OpenSSH](https://learn.microsoft.com/en-us/windows-server/administration/openssh/openssh_install_firstuse).
Keep host-key verification enabled. Do not expose Codex app-server/IPC listeners
directly to the internet. OpenAI's own [SSH connection guidance](https://learn.chatgpt.com/docs/remote-connections#connect-to-an-ssh-host)
also distinguishes SSH access from same-account Remote Control.

## 2. Register the pair yourself

These are examples, not usable credentials or addresses. Replace the example
addresses with the machines' actual LAN or VPN IP addresses and choose the
correct OS usernames and existing private-key files.

On the Mac, in `~/.ssh/config`:

```sshconfig
Host windows-pc
  HostName 192.0.2.10
  User YOUR_WINDOWS_USER
  IdentityFile ~/.ssh/YOUR_EXISTING_KEY
  IdentitiesOnly yes
  StrictHostKeyChecking yes
```

On Windows, in `%USERPROFILE%\.ssh\config`:

```sshconfig
Host mac
  HostName 192.0.2.20
  User YOUR_MAC_USER
  IdentityFile ~/.ssh/YOUR_EXISTING_KEY
  IdentitiesOnly yes
  StrictHostKeyChecking yes
```

Compare and trust each host's key fingerprint through a trusted channel before
using the tool. Unknown/changed host keys must be resolved by the user, never
silently accepted or bypassed. Keep keys outside the project and do not copy
Codex login files between devices for discovery.

The passive inventory reads concrete `Host` blocks in the main user config. It
does not evaluate `Include`, wildcard defaults, `Match exec`, or `ProxyCommand`.
Put the pair's literal `HostName` IP in those concrete blocks. DNS names may work
for SSH itself, but this version does not infer a writer identity from DNS or a
jump host that hides the original peer address. Add direct LAN/VPN aliases for
the pair; do not relax identity checks to compensate.

## 3. Check SSH before enabling handoff

From the Mac, check the Windows alias without sending a Codex prompt:

```sh
ssh -o BatchMode=yes -o StrictHostKeyChecking=yes windows-pc hostname
```

From Windows, check the Mac alias:

```powershell
ssh -o BatchMode=yes -o StrictHostKeyChecking=yes mac hostname
```

The returned OS hostnames should match the intended machines. Then add the Mac's
SSH project connection in the Codex desktop app on Windows and open the relevant
Mac-hosted task. Do not substitute the same-account **Control other devices**
pairing flow. Configure matching aliases in `config.json`, refresh **SSH 主机**,
and review the actual owner before enabling `enable_handoff` on the Mac.

The list distinguishes:

- **Configured**: a user SSH config entry exists; reachability is not proven.
- **Connected**: an SSH task proxy is observed; takeover is still gated.
- **Takeover-ready**: the configured Windows controller and process scope passed
  the checks for this operation. This is not a promise that unrelated work is idle.

If a machine is reachable only through OpenAI's same-account relay, it is out of
scope. Supply an SSH path or use OpenAI's own Remote features separately. There is
no automatic fallback, QR pairing, account-discovery switch, or relay troubleshooting
in Session Writer. Codex's ordinary authenticated model traffic is unaffected;
“SSH-only” describes this tool's discovery and cross-machine control, not offline AI.

中文边界：用户自行登记 SSH 主机，自行配置 Tailscale/VPN 和认证。我们不读取同账号
设备列表，不走 OpenAI 中转，不解决只能通过官方同账号远控连接的机器。
