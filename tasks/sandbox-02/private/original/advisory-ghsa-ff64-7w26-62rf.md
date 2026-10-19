# GHSA-ff64-7w26-62rf / CVE-2026-25725 — advisory record

Source: https://github.com/anthropics/claude-code/security/advisories/GHSA-ff64-7w26-62rf
Published: 2026-02-06. Package: npm @anthropic-ai/claude-code (< v2.1.2;
fixed v2.1.2). Severity: High (CVSS 7.7).
Title: "Sandbox Escape via Persistent Configuration Injection in
settings.json".

Description (as published):

Claude Code's bubblewrap sandboxing mechanism failed to properly protect
the .claude/settings.json configuration file when it did not exist at
startup. While the parent directory was mounted as writable and
.claude/settings.local.json was explicitly protected with read-only
constraints, settings.json was not protected if it was missing. This
allowed malicious code running inside the sandbox to create this file and
inject persistent hooks (such as SessionStart commands) that would execute
with host privileges when Claude Code was restarted.

Weaknesses: CWE-501, CWE-668. Credit: hackerone.com/edbr.

This record is provenance for the settings-injection stage of the
sandbox-02 restricted command runner, an original minimal implementation
of the documented bug class (the product itself is closed-source and is
not redistributed here).
