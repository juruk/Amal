---
name: Amal Task
about: Submit a task for the Amal agent with an explicit Acceptance check
title: "[Amal] "
labels: []
assignees: []
---

# Amal Task

## Title
(Краток опис – што очекуваш да се смени/изработи)

## Body / Context
(Детали – линкови, фајлови, барања, ограничувања)

## Acceptance (YAML)
```yaml
ACCEPT:
  cmd: python sample_app/app.py
  expect_contains: Hello, Phase 8!
  timeout: 30
