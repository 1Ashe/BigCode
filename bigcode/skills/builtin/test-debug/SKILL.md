---
name: test-debug
description: Diagnose failing tests and build checks with focused evidence.
version: 1
---

# Test Debug

Use this skill when tests, linters, type checks, or build commands fail.

Workflow:

1. Capture the exact command and the first actionable failure.
2. Trace the failure to the smallest relevant code path.
3. Fix the underlying cause before broad refactors.
4. Re-run the narrow failing check, then the broader suite when risk warrants it.
