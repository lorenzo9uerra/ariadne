# GHSA-5j29-h97v-84ch / CVE-2026-103435 — advisory record

Source: https://github.com/anthropics/claude-code/security/advisories/GHSA-5j29-h97v-84ch
Published: 2026-10-05. Package: npm @anthropic-ai/claude-code (< 2.1.129;
fixed 2.1.129). Severity: High (CVSS 7.7).
Title: "Arbitrary File Write via Write-Time Symlink Following (TOCTOU) in
Claude Code".

Description (as published):

Claude Code validated that a target file path resided within the project
working directory at permission-check time, but re-resolved the path at
write time without repeating that validation. This time-of-check to
time-of-use (TOCTOU) gap allowed an attacker who could write to the
workspace to atomically replace a project file with a symlink, causing
Claude Code to follow the symlink and write its output to an arbitrary
file outside the project sandbox. Exploitation required the ability to
win a race condition against the write operation and write access to the
shared workspace, enabling a lower-privileged attacker to redirect benign
edits to sensitive files (e.g., shell configuration) in a
higher-privileged session.

Weaknesses: CWE-22, CWE-61, CWE-367. Credit: hackerone.com/c_h4ck_0.

This record is provenance for the sandbox-02 workspace file service, an
original minimal implementation of the documented bug class (the product
itself is closed-source and is not redistributed here).
