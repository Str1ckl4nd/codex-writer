# Release checklist

Publishing source, publishing binaries, changing repository visibility and
installing software are separate actions. Never bundle live task data.

## Development

Use the current branch, check the destination for conflicting changes, and push
directly without force when safe. Do not open or merge a PR merely to publish
these changes. Stop if the remote branch has diverged. Keep GitHub-hosted CI
manual-only unless the maintainer explicitly changes the execution policy.

## Before a release

1. Confirm repository name, publishing account/visibility and MIT license choice.
2. Run fixture tests, PowerShell checks and `scripts/release_check.py`.
3. Review the complete initial commit and archive, not just the working tree.
4. Confirm no personal paths, real machine/task IDs, auth material, copied app
   source, caches, logs, historical backups or binaries enter the source archive.
5. Re-run controlled native Mac/Windows acceptance and disclose untested paths.
6. Configure a private security reporting channel and supported-version policy.
7. Obtain the appropriate maintainer decision before changing visibility or
   publishing release assets. Review earlier commits and Actions logs too;
   repository settings are not controlled by build scripts.

Installers preserve existing local configuration. They install under the separate
`session-writer` name, without replacing unrelated applications. Uninstall
by removing only the installed app/shortcut and tool directory. Keep the state
directory until unresolved handoff receipts have been reviewed.
