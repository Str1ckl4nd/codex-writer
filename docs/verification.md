# Verification status

These checks describe the current source, not a guarantee of safe operation in
every environment. Handoff remains disabled by default.

## Verified with isolated fixtures

- 119 Python unit/fixture checks in an isolated Linux VM.
- Multiple Windows/macOS controller identities sharing one backend; distinct
  hosts with the same PID; ambiguous alias/address rejection; full source-host
  impact lists; preserving the selected controller and shared server.
- Handoff selection for already-owned and unowned tasks, destination-side
  controller cleanup, and rejecting a shared destination as an exclusive success.
- Windows standalone packaging: only allowlisted code/config fields travel;
  a fake backend runs without prior installation and its temporary files are
  removed after normal exit. No real Codex task was used for this test.
- Interruption receipts, no duplicate continuation for unchanged tasks,
  unknown-delivery guards, source PID/start and lock-scope checks.
- SSH-only discovery: no account-device API, token lookup, cloud cache, unknown
  host probing or revival of old account-discovery settings.
- PowerShell parsing, 9 Windows controller fixtures, and 4 standalone-package
  checks; these make no live SSH or GUI calls.
- Required user documentation, local Markdown links, isolated log paths and
  strict host-key options.
- Source-release checks excluding real configuration, credentials, task
  databases, runtime logs and compiled artifacts from the source tree.

## Native build

The schema-5 macOS application compiles and passes ad-hoc signature verification.
The build writes only to the independent project build directory. Compilation
and signature checks are not native GUI or real handoff acceptance.

## Not verified end-to-end

- A real multi-controller Windows/macOS deployment and fresh-user installation.
- Windows PowerShell 5.1 through a real SSH connection into the temporary backend;
  PowerShell package tests ran in an isolated PowerShell runtime.
- Remote macOS desktop closure on a live user's application.
- Complete GUI workflows, real task interruption, or every child-agent recovery.
- Sudden network loss or process termination in every stage. Abrupt termination
  can leave temporary code directories or incomplete receipts.
- GitHub Actions execution. Hosted CI is manual-only.

The data backend currently supports macOS; multiple-controller support is not
a claim of arbitrary-OS storage support or synchronization of independent task
databases. Revalidate on disposable, authorized tasks before relying on a new
Codex release. Automated checks are not a comprehensive security or legal audit.
