# Security policy / 安全说明

This repository ships source and empty configuration templates, not a hosted service. Keep real credentials in `.secrets/` with directory mode 700 and file mode 600. Do not commit speech recordings, device tokens, databases, logs, firmware/NVS dumps or private deployment archives.

Use HTTPS/WSS for remote access, a unique random device token per device, and a strong random administrator password. The current admin credential format uses a SHA-256 digest, not a slow password KDF; retain the generated high-entropy password, protect the digest file, and do not substitute a weak human password. Keep the server bound to loopback unless a properly configured proxy is in place.

Before publishing updates, inspect `git diff --cached`, run a secret scanner such as Gitleaks, and check the staged files for private infrastructure names and personal data. `.gitignore` is a secondary guard; it does not remove tracked secrets or past commits.

Report vulnerabilities using this repository's private vulnerability reporting feature in GitHub's Security tab. Do not include working secrets or private recordings in public issues. If a credential is exposed, revoke it at its issuer; deleting a file from the latest commit is insufficient.

No independent security certification is claimed. Experimental full-duplex behavior and device stability limits are documented in the README.
