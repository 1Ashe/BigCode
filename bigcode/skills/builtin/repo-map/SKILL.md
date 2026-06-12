---
name: repo-map
description: Map a repository before making code changes.
version: 1
---

# Repo Map

Use this skill when a task requires understanding an unfamiliar repository, locating ownership boundaries, or planning a change across multiple modules.

Workflow:

1. Inspect the top-level file tree and package manifests.
2. Identify entrypoints, tests, configuration, and likely owner modules.
3. Summarize the smallest relevant map before making implementation decisions.
4. Prefer existing local patterns over new abstractions.
