# Related tools — documentation review, 2026-09-21

This is a bounded review of project documentation, not installation, benchmarking
or a claim that no other implementation exists.

- [FoxClaw](https://github.com/foxden-app/foxclaw/blob/main/docs/zh/user-manual.md): a mobile/chat controller with
  an explicitly documented force-takeover path for a terminal-owned Codex thread,
  including PID/directory checks and confirmation. This is genuine functional
  overlap. Force takeover is specifically limited to Linux/WSL interactive CLI
  owners and rejects app-server processes, remote clients and multi-thread lock
  holders. It is not a drop-in Mac/Windows desktop writer-selection panel.
- [Codex Away live-thread handoff](https://github.com/akibrhast/codex-away/blob/main/docs/live-thread-handoff.md):
  describes the same cross-process writer conflict and a manually verified
  terminal/phone handoff. Its main role is managing Mac remote availability;
  automatic handoff while an existing desktop writer remains alive is explicitly
  not claimed as completed in that document.
- [Codex Handoff](https://github.com/Raf4ik/codex-handoff): Mac/Windows encrypted
  state backup and transfer. Its documented safety model requires Codex to be
  closed before capture/apply. This is state synchronization, not the same live
  writer-selection operation.
- [codex-workspace-sync](https://github.com/Companionh/codex-workspace-sync): a
  Windows-first, server-authoritative workspace/session system with leases and
  recovery tooling. It requires a sync hub and is a broader synchronization design.
- [agent-sync](https://github.com/lidongpeng36/agent-sync): SSH-based agent data
  synchronization; useful adjacent work, not evidence of a matching desktop
  writer takeover UX.

[Upstream issue 46652](https://github.com/openai/codex/issues/46652), opened
2026-09-19, reports the single-writer user experience and requests an explicit
handoff/takeover mechanism. It is a community report, not a confirmed statement
about which release first introduced the behavior.

Do not market Session Writer as the first Codex handoff or session-sync tool.
Its intended focus is a standalone, explicit SSH-controller ownership/diagnostic panel.
Reliability, compatibility and clear interruption receipts matter more than a
novelty claim; there is no comparative reliability evidence yet.
