---
id: TASK-1
title: Проверить находки ревью alertsbot
status: Done
assignee: []
created_date: '2026-10-25T19:39:00Z'
updated_date: '2026-10-25T19:40:00Z'
labels: []
dependencies: []
priority: medium
type: docs
ordinal: 1000
---

## Description

<!-- SECTION:DESCRIPTION:BEGIN -->
Сверить 8 находок из /tmp/.../mismatch/alertsbot.md с кодом, конфигом и журналами; мелкие документационные расхождения исправить, крупные оставить с рекомендацией.
<!-- SECTION:DESCRIPTION:END -->

## Acceptance Criteria
<!-- AC:BEGIN -->
- [x] #1 По каждой находке есть вердикт и источник
- [x] #2 Мелкие подтверждённые документационные правки внесены
- [x] #3 Гейт pytest/ruff/mypy зелёный
- [x] #4 Коммит без push
<!-- AC:END -->

## Implementation Notes

<!-- SECTION:NOTES:BEGIN -->
Проверены все 8 находок.
Исправлено в alertsbot: README (история/ретраи/пересыльщик/выпуск), docs/deploy.md (пульс 10 минут, ERROR до startup), docs/testing.md (ERROR до startup).
Не правил: routing.md (admin/alerts, вопрос поведения resolved), проверка тела Telegram, лимит тела, ретраи asr-llm/Alice, общий токен — меняют логику/получателей/безопасность или вне репо.
Гейт: pytest 100 passed, ruff и mypy зелёные.
<!-- SECTION:NOTES:END -->

## Final Summary

<!-- SECTION:FINAL_SUMMARY:BEGIN -->
Подтверждены 8 находок. Документация alertsbot исправлена; поведенческие/внешние находки оставлены с рекомендациями. Гейт зелёный, коммит без push.
<!-- SECTION:FINAL_SUMMARY:END -->
