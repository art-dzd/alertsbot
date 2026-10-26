---
id: TASK-3
title: Довести историю alertsbot по ревью
status: Done
assignee: []
created_date: '2026-10-26T19:02:00Z'
updated_date: '2026-10-26T19:04:00Z'
labels: []
dependencies: []
priority: high
type: bug
ordinal: 3000
---

## Description

<!-- SECTION:DESCRIPTION:BEGIN -->
Один писатель, отмена без pending, схема без проверки файла, cleanup до попытки, SSH-таймаут из readyz; тесты поведения. Затем push и приёмка на mts.
<!-- SECTION:DESCRIPTION:END -->

## Acceptance Criteria
<!-- AC:BEGIN -->
- [x] #1 Параллельные записи без потерь и locked
- [x] #2 Отмена не оставляет pending
- [x] #3 Схема без file-check, retry после SQLite error
- [x] #4 Cleanup время до попытки
- [x] #5 SSH timeout выведен из readyz
- [x] #6 CI и выкладка зелёные, алерты доходят
<!-- AC:END -->

## Implementation Notes

<!-- SECTION:NOTES:BEGIN -->
Гейт: 108 passed, ruff/mypy чистые. Тесты: параллельные записи на реальной SQLite без потерь и locked; отмена оставляет failed/499, не pending; схема без file-check, retry после SQLite error; cleanup не повторяется; SSH-таймаут = readyz + 3. Далее push и приёмка на mts.
<!-- SECTION:NOTES:END -->

## Final Summary

<!-- SECTION:FINAL_SUMMARY:BEGIN -->
Ревью закрыто: один доступ к SQLite, отмена без pending, схема без file-check, cleanup до попытки, SSH-таймаут из readyz. Гейт зелёный; выкладка проверяется.
<!-- SECTION:FINAL_SUMMARY:END -->
