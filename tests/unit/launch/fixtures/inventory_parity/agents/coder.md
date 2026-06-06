---
name: coder
description: Features and refactors
mode: subagent
model: composer
model-policies:
  - match: {alias: gpt55}
    override: {model: gpt-5.5}
---

Coder body.
