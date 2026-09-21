# Contributing

Do not change repository visibility, publish releases, run paid hosted jobs, or
replace an existing installation as an incidental development step.

Keep changes small and include fixture regressions. Never test by interrupting a
maintainer's real task. Do not commit runtime files, real configuration, hostnames,
account IDs or session histories. Use documentation IP addresses and synthetic UUIDs.

Preserve fail-closed identity checks and distinguish writer acquisition from
continuation delivery. Keep mutation opt-in and automatic refresh read-only with
respect to tasks. Changes to supported platforms, controller identity or storage
topology must document the corresponding ownership and authorization boundaries.

Run the test suite and release check before submitting changes. Native UI changes
also need platform-specific visual/accessibility review; compile-only results
must not be described as GUI acceptance.

When changing data access or storage, update PRIVACY.md and installation paths
in the same change. Preserve the MIT notice and add notices for any newly
introduced third-party material. See NOTICE and DISCLAIMER.md.
