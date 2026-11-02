# Security and private information

For vulnerabilities, use **Security → Report a vulnerability** on this GitHub
repository. Please do not disclose credentials, private recordings or working
access URLs in a public issue. Ordinary bugs and compatibility requests belong
in [Issues](https://github.com/mukw-labs/reolink-audio-library/issues).

When sharing diagnostics, remove camera usernames/passwords, HA access tokens,
signed preview URLs, public hostnames, IP addresses, device identifiers and
personal recording names. Never upload HA's `.storage` directory, `secrets.yaml`,
backups, camera recordings or packet captures. Model, hardware and firmware
versions are useful and do not require sharing a device serial number.

The integration reuses official Reolink credentials through owned sessions.
Preview URLs are authenticated or signed and should be treated as access links.
Preview caches are bounded, in memory and invalidated on relevant changes.
Uploads and recording changes require administrator actions; deletion requires
an explicit confirmation. No telemetry is added.

The repository ignores common local credential, evidence, backup, build and
environment files. Ignore rules do not remove files already committed. Review
both the working tree and Git history before publishing a fork or release.
CI runs a redacted secret scan of Git history alongside the software checks.
