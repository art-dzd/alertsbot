---
id: TASK-2
title: Не блокировать alertsbot медленной историей
status: Done
assignee: []
created_date: '2026-10-26T18:53:00Z'
updated_date: '2026-10-26T18:57:00Z'
labels: []
dependencies: []
priority: high
type: bug
ordinal: 2000
---

## Description

<!-- SECTION:DESCRIPTION:BEGIN -->
Медленная SQLite-запись не должна держать event loop и /healthz; схема один раз; таймаут readyz пересыльщика; время чистки после успеха.
<!-- SECTION:DESCRIPTION:END -->

## Acceptance Criteria
<!-- AC:BEGIN -->
- [x] #1 Тест: медленный insert не блокирует второй /notify и /healthz
- [x] #2 Схема создаётся один раз
- [x] #3 Таймаут readyz >11 и < SSH
- [x] #4 Время чистки после успеха
- [x] #5 CI-гейт зелёный, коммит без push
<!-- AC:END -->

## Implementation Notes

<!-- SECTION:NOTES:BEGIN -->
Красный прогон до правки: 4 failed (event loop, schema, cleanup, readyz). После: 109 passed, ruff/mypy чистые. Схема создаётся один раз; readyz пересыльщика 12 с; время чистки после успеха.
<!-- SECTION:NOTES:END -->

## Final Summary

<!-- SECTION:FINAL_SUMMARY:BEGIN -->
Медленная SQLite больше не блокирует event loop; схема один раз; readyz 12 с; cleanup после успеха. Гейт зелёный, коммит без push.
<!-- SECTION:FINAL_SUMMARY:END -->
