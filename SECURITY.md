# Safety and security

Only operate systems you own or are authorized to administer. A task can share
its app-server with other tasks; a handoff is potentially disruptive.

Keep handoff disabled until the host mapping is verified. Do not delete lock
files, weaken host-key verification, or remove uncertain-delivery receipts to
force a retry. Inspect the actual writer and interrupted tasks first.

Logs and receipts are local operational data. They can contain hostnames, task
IDs and process identifiers even though conversation text and credentials are
excluded. Review/redact them before sharing. Never attach authentication files,
SSH keys, task databases, or full session histories to an issue.

Do not post credentials or sensitive histories in issues. Use GitHub's private
vulnerability reporting when available. If no confidential reporting route is
listed, open an issue requesting contact instructions without disclosing details.
No reporting email is advertised by this project.

The application authenticates through existing local/SSH access; it is not a
security boundary against another process with the same OS user privileges.

Discovery accepts only local SSH configuration and observed SSH connections.
OpenAI same-account Remote Control, account device APIs, login-token access, and
automatic relay fallback are not supported. A cloud-visible device is not an
authorized SSH target. Obsolete account discovery settings/caches are ignored.
Tailscale or VPN setup is external to this tool; never weaken SSH host-key checks
or expose Codex's control sockets directly to make a machine reachable.
