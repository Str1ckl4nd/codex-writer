# Architecture

The Mac backend reads process/FD ownership and the Codex task index. Windows sends
locally observed controller metadata over its authenticated, explicitly configured
SSH connection. The backend checks the source address, freshness and structure.
It does not derive the writer from the computer that happened to open the panel.

`runtime_config.py` separates code, local config, Codex data and mutable state.
`discovery_adapter.py` inventories concrete user SSH config entries and links
canonical Unix socket paths to proxies and SSH peers. It never receives an auth
RPC callback, queries a cloud account API, reads Codex's device registry, or
loads an old account-discovery cache. IDs are `local` and `ssh:<configured-alias>`.
`ssh_bridge.py` performs one bounded request to the configured Windows alias;
the personal installation's private broker code is not included.
`writer_service.py` plans and verifies handoffs. `writer_force.py` records intended
interruptions before stopping an approved source. `writer_desktop.py` uses a small
version-checked desktop IPC client; it never injects raw model history or bypasses
parent-owned subagent input restrictions.

The app-server loading and turn lifecycle are separate operations. Public
documentation: [Codex app-server](https://learn.chatgpt.com/docs/app-server).
Desktop IPC remains a version-sensitive local integration. It is distinct from
cross-machine SSH transport and does not provide an OpenAI cloud relay fallback.

Machine inventory and handoff have intentionally separate gates:

1. A concrete user SSH config entry makes a host visible, not online or selectable.
2. An observed SSH/proxy connection associates the host with a local service.
3. The configured Windows controller needs exact process identity and ownership
   checks before it becomes selectable. Other registered hosts are inventory only
   in this two-computer version. Unregistered peers remain unknown owners.

Snapshot schema 4 exposes `discoveryMode: ssh-only` and contains no account/relay
fields. Both native clients reject earlier snapshot schemas. Removed `account:`
or `paired:` target IDs cannot authorize a handoff. Refresh performs no network
scan and no machine registration. Tailscale/VPN enrollment and connectivity are
user responsibilities; model execution's normal service traffic is separate.

Continuation receipts use `parent_submitted` for recovery instructions delivered
to a parent agent. This means local agent delegation, not an OpenAI network relay.

## Source provenance

The shipped application code was authored for this utility. Standard OS process,
SSH and IPC interfaces are used. Protocol behavior was observed/documented; no
extracted OpenAI application source, binary, generated proprietary source dump,
logo or icon is shipped. The personal SSH broker dependency was replaced with
a utility-specific standard-library SSH adapter, rather than redistributed.

External runtimes (Python, PowerShell, jq, OpenSSH, AppKit/Codex) are user-installed
dependencies with their own licenses. They are not relicensed by this project.
