# Требования → план Foundation R1

Источник: `PokerEye-Foundation-R1-ONE-SHOT.zip` (`FOUNDATION-20260818-R1`).
Архив содержит только этот чеклист, без `patch/apply_patch.py` и без кодового payload.

Ниже — исходный план. Пункты, помеченные автором как `[x]`, не все присутствуют в текущем `poker-eye-v2` дереве: Nuxt 4 `web/console`, localhost CONNECT egress и self-applying patch в этом репозитории отсутствуют. Рабочий runtime — HMN1 + v6 router + embedded console-v3.

## P0 — корректность Trainer

- [x] Один table lifecycle имеет один startup worker.
- [x] Повторный `↻ PokerEYE` коалесцируется и имеет cooldown.
- [x] Перед следующей попыткой закрываются stream/task/channel и освобождается lease.
- [x] Orphan/duplicate leases периодически исправляются supervisor'ом.
- [x] Backend startup имеет timeout, точную причину и последовательный retry.
- [x] Нулевая неинициализированная величина stack не запускает forced exit.
- [x] Закрытие/disconnect/stale session приводит к deterministic cleanup.

## P0 — CC/action pipeline

- [x] Для каждого action сохраняются requested action, amount, table, latency и result.
- [x] Timeout получает reason и безопасный fallback: CHECK только когда разрешён, иначе FOLD.
- [x] Fallback не подменяет неизвестное действие и не отправляет агрессивную ставку.

## P0 — PokerEYE backend

- [x] Отдельный localhost CONNECT egress service на VPS.
- [x] gRPC Trainer направляется через него независимо от HMN1/Console.
- [x] Allowlist target и отдельный egress log.
- [x] Опциональный upstream SOCKS/HTTP proxy задаётся только в VPS env-файле.

## P1 — наблюдаемость

- [x] Atomic runtime state для devices/tables/accounts/actions/errors/fuel.
- [x] Per-session руки по NLH/PLO4/PLO5/PLO6.
- [x] Error counters и последние причины.
- [x] Backend/fuel/lease/CC telemetry.
- [x] Sanitized diagnostic download без secret/token/account credential.
- [x] Offline analyzer для events/technical logs и PCAP inventory.

## P1 — Console

- [x] Один поддерживаемый Nuxt 4/Tailwind 4 source в `web/console`.
- [x] Header: версия, fuel, health, active tables/hands/issues.
- [x] Главная таблица по эмуляторам с подсветкой состояния.
- [x] Детализация по table sessions.
- [x] Логи с фильтрацией.
- [x] Admin controls: retry backend, safe close/release/dismiss, router reset, Trainer restart, diagnostics.
- [x] Profit не выдумывается: до появления достоверного protocol event отображается `—`.

## P1 — эксплуатация

- [x] Один self-applying `patch/apply_patch.py`.
- [x] Очистка известных hotfix/backup/cache/build/log artifacts.
- [x] Сохранение `config/`, `secrets/`, `.dist/baseline/` и последнего APK.
- [x] Никаких backup-копий.
- [x] Локальные tests/compile, VPS deploy, HMN1/API/egress/test acceptance.
- [x] `patch/` удаляется только после полного успеха.

## Live acceptance после установки

Система считается принятой в реальной игре, когда одновременно открытые table sessions получают разные active leases, не оставляют stale `open/account —`, не вызывают low-stack до посадки и проходят action pipeline без `action_failed`. Patch автоматизирует серверные acceptance gates, но реальный Coin table load проверяется уже на запущенных эмуляторах и отражается в Console/diagnostics.
