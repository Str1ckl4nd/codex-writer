# Architecture

Codex Writer has one operator-facing tool instance and a selected task-data host.
The data backend currently supports macOS. The Mac UI runs it locally; the
Windows UI can send a bounded application package over SSH and run it temporarily.
Neither path requires every controller to install or run Codex Writer.

```text
One tool UI (Mac or Windows)
  └─ Task-data backend (local Mac, or temporary backend over SSH)
       ├─ Existing Codex task index, writer handles, and local control socket
       ├─ SSH → Windows controller: one-shot PowerShell helper
       └─ SSH → Mac controller: one-shot Python helper
```

The remote endpoints already need SSH, the relevant runtime and their normal
Codex environment. The helpers do not install a service or leave a tool daemon.
Temporary backend code is normally removed at exit; abrupt termination may
leave a temporary directory. Operational logs and receipts intentionally persist.

## Explicit host identity

`ssh_controllers` declares multiple controller records: a canonical SSH alias,
platform, optional aliases for the same physical machine, and the alias that
controller uses to access the data host. At most 32 controller records are
accepted per configuration. Duplicate ownership of one alias is rejected.

Machine inventory comes from concrete entries in the data host's SSH configuration.
Unregistered peers and ambiguous address/alias mappings cannot authorize a stop.
No OpenAI account directory, device-pairing registry, token lookup, network scan,
or relay fallback is involved. IDs are `local` and `ssh:<configured-alias>`.

Remote queries are bounded and read-only. Connected controllers are inspected
with bounded concurrency; each controller has a separate, configuration-bound
identity cache. Closing a controller rechecks its hostname, PID, start identity
and exact Codex/SSH process ancestry.

## Shared services are not one physical machine

Several SSH clients can share the same app-server and therefore the same writer
PID. The task table reports a shared owner rather than picking an arbitrary host.

Before a handoff, a revision binds the task, file-handle identity, affected tasks,
controller identities, configured routes, source connections and destination.
The confirmation lists the remote Codex windows that would close.

- Moving to the local desktop closes all verified source controllers and then
  releases the source writer process, subject to the confirmed scope.
- Moving to a selected SSH controller closes other verified source controllers
  while preserving the target and its shared server.
- Postflight verifies the actual writer and the uniquely remaining target
  connection and controller identity. An extra or changed connection is failure,
  not evidence of successful handoff.
- The local result record documents the verified handoff; editing that record
  alone never establishes write ownership.

Task storage is not copied or merged. Supporting multiple controllers does not
mean supporting concurrent writers or a federated database across computers.

## Interruption and recovery

`writer_force.py` persists the intended interruption scope before taking action.
It keeps the selected shared server alive when possible. Turns still running on
that preserved server do not receive duplicate continuation requests.

Interrupted child agents are addressed through their parent. Receipts distinguish
writer acquisition, submission, uncertain delivery and parent-mediated continuation.
`parent_submitted` describes agent delegation, not a cloud network relay.

Unknown identities, changed scope, incomplete recovery receipts and uncertain
delivery remain fail-closed. No helper disables Codex approval rules or writes
raw model history to bypass parent ownership.

## Components and protocol boundaries

- `runtime_config.py`: explicit controller registry, local configuration and state paths.
- `discovery_adapter.py`: SSH inventory and socket/proxy/peer correlation.
- `ssh_bridge.py`, `windows_controller.ps1`, `mac_controller.py`: one-shot remote helpers.
- `remote_bootstrap.py`: bounded application-file/config allowlist for a temporary backend.
- `writer_service.py`: observations, confirmation, release and postflight.
- `writer_desktop.py`: version-checked local desktop IPC, separate from SSH transport.

Snapshot schema 5 exposes `discoveryMode: ssh-only`. Both interfaces reject earlier
schemas. Legacy single-Windows configuration remains a compatibility input;
it no longer defines the number of supported controllers.

Public app-server documentation: [Codex app-server](https://learn.chatgpt.com/docs/app-server).
Desktop IPC and persisted task metadata remain version-sensitive integrations,
not a stable-API or service-authorization guarantee.

## Source and dependencies

The repository ships project application code, not extracted OpenAI desktop
source, application bundles, authentication files, logos or a private SSH broker.
Python, PowerShell, OpenSSH, jq, AppKit and Codex are user-installed dependencies
with their own licenses. See [NOTICE](../NOTICE) and [data access](../PRIVACY.md).
