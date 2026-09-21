# Verification status — private development preview

The independent candidate has not been installed over the personal tool. Its
source repository is private; no public release or installer asset is published.

Verified during preparation:

- 94 Python fixture/unit checks in an isolated Linux VM, including configuration
  isolation, opt-in mutation guards, SSH argument construction, no replay after
  uncertain close, socket symlinks, writer scope and continuation receipts.
- SSH-only discovery regressions: registration without Codex account state,
  add/remove on refresh, no cloud/auth/cache file reads, no HTTP discovery path,
  no re-enabling account discovery via old config, no unregistered-host probes,
  rejection of obsolete cloud target IDs, and both clients' schema-4 guards.
  Configuration alone never grants takeover eligibility.
- Repository checks: required user-facing documents and relative links, separate
  Windows log storage, strict host-key checking in Windows requests, and no
  automatic hosted CI trigger during private preparation.
- PowerShell source parsing and 9 pure Windows controller fixtures, also in the VM.
- Source-release guard on the selected text files.
- Native macOS application compilation and ad-hoc signature verification,
  refreshed after the SSH-only changes. This is a build-only check, not native
  GUI or real handoff acceptance.

Not verified:

- The new portable SSH adapter has not been used to interrupt a real task.
- A fresh installation on someone else's Mac/Windows pair has not been exercised.
- Native UI behavior and child-agent continuation in the public candidate have
  not been tested end-to-end.
- GitHub Actions is manual-only and has not run on GitHub. Private-repository
  pushes do not automatically consume hosted-runner time.
- Automated release checks are not a comprehensive security audit or a legal
  determination of rights. Making the repository public or releasing binaries
  still requires a separate maintainer decision and review.

The personal precursor's operational results must not be presented as proof for
this refactored release. Validate on disposable, authorized sessions first.
