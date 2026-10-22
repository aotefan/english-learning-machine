# Contributing

Please describe the problem, expected behavior and a small reproducible example. Use synthetic text/audio and remove credentials, device identities, personal speech and infrastructure details from reports.

1. Follow [setup](docs/quickstart.md) and restore the pinned upstream sources.
2. Change `scripts/`, `debug/`, or the appropriate `integrations/*/files/` source. For firmware, follow upstream `AGENTS.md` and board rules.
3. If changing an integration file, update its SHA-256 in `integrations/manifest.json`, then restore in a new checkout. Do not overwrite a modified upstream worktree.
4. Run relevant Python tests and firmware host tests. Build affected firmware variants for firmware changes; compilation does not validate real audio or hardware.
5. Run a secret scan, review staged files, and describe test coverage and remaining hardware limits in your pull request.

Do not add raw production evidence, private Git history, model weights, firmware binaries or generated credentials. Git commits should use your preferred public or GitHub noreply identity.

The recommended cloud stack uses Python and plain browser JavaScript. Introducing Node.js or PHP requires checking official supported versions and security advisories at that time, including the actual build and runtime versions.
