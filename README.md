# poker-eye-v2

Минимальный стабильный trainer без GUI/web/admin/Telegram: один понятный CMD
на локальном target-PC, короткий операторский статус, runtime-транспорт
UDP IPv4 broadcast discovery (1.25 s) + authenticated TCP LAN (без ADB).

## Сейчас

- Рабочий baseline `ready_v6` не изменяется и остаётся production fallback.
- Ядро v2 (stdlib-only): протокол framing/HMAC, UDP discovery, аутентифицированный
  TCP, recyclable номера столов, генерации, heartbeat, планировщик CC-действий
  (ровно 3 явные попытки, stale-ACK защита, human-like delays с extra-вероятностью
  для 60BB+), Coin SFS2X wire codec и CC-резолвер (портировано из проверенного
  legacy), состояние стола/стрита, нормализованная модель событий Coin/EYE,
  hint watchdog, guard для аномалии call=0 (state-gap, CHECK вместо CALL 0),
  append-only ledger, bounded PCAP ring (4×64MiB/стол, 256MiB cap, BPF фильтр),
  иерархия логов logs/run_<id>/devices/<emulator>/session_<id>/tables/table_01.
- Запуск: `RUN.cmd` (секрет через `POKEREYE_V2_SECRET`, по умолчанию
  `change-me-local-only` для локальной разработки).

## Документация

- [CONCEPT.md](CONCEPT.md) — концепция и контракт надёжности.
- [docs/PROTOCOL_REFERENCE_PCAP.md](docs/PROTOCOL_REFERENCE_PCAP.md) — разбор
  reference PCAP (NLH4 HU single-board): framing, команды, CC-схема.
- [docs/MIGRATION_MAP.md](docs/MIGRATION_MAP.md) — карта переноса из legacy.
- [docs/CAPABILITY_GAPS.md](docs/CAPABILITY_GAPS.md) — double-board исключён
  явно, не заявляем поддержку.

## Правила

1. Не запускать destructive build в `C:\projects\pokereye\coin`.
2. Не заменять baseline APK без runtime-проверки на тестовом эмуляторе.
3. Не считать наличие listener порта доказательством hook traffic.
4. ADB — только lab control/observation, никогда runtime-транспорт.
5. PCAP/логи/секреты/APK — вне Git.
6. Сначала простой stdout и локальный JSONL-journal, затем адаптеры.

## Тесты

```cmd
python -m unittest discover
```

Модульные тесты доказывают инварианты ядра; release-заявления требуют
реального эмуляторного evidence (discovery, TCP, hook, hint→CC→ACK→ledger,
reconnect, multitable).


## Public bootstrap (target-PC)

The trainer can also accept an authenticated emulator bootstrap on
`0.0.0.0:19037` (publicly forwarded as `37.192.228.101:19037`). It allocates
the lowest free callback port in `54300..54399`, returns a generation/token,
and keeps the callback authenticated. The public endpoint is served by this
local target-PC; no VPS-side change and no ADB runtime transport are involved.

```cmd
RUN.cmd --public-host 37.192.228.101 --bootstrap-port 19037
```

The forwarded public bootstrap and callback range must point to this machine.
The Android bridge tries LAN broadcast first and bootstraps through the public
endpoint when LDPlayer NAT cannot deliver broadcast. Runtime action routing
still requires active hook traffic and real EYE/backend integration evidence.

