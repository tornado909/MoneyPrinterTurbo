# MoneyPrinterTurbo RU + ASC-AI Director

Русская редакция [MoneyPrinterTurbo](https://github.com/harry0703/MoneyPrinterTurbo), развиваемая как локальный AI production pipeline для ASC-AI.

**Стандартный путь форка:**

`тема / сценарий → Director → scene plan → Krea/Lustify → выборочные Wan I2V сцены → локальный QC → Chatterbox → Whisper → FFmpeg → готовый ролик`

Русский | [English](README-en.md) | [日本語](README-ja.md) | [简体中文](README-upstream-zh.md) | [Upstream](https://github.com/harry0703/MoneyPrinterTurbo)

## Чем этот форк отличается

- **Полный русский WebUI:** русская локаль является primary edition и должна полностью покрывать английские UI-ключи.
- **ASC-AI — источник по умолчанию:** `video_source = "asc_ai"`.
- **Director по умолчанию:** локальный Qwen3-8B создаёт typed production-ready scene plan; malformed JSON получает одну bounded repair-попытку, а public MediaWiki research кэшируется и при необходимости добирается из fallback-языка без платного AI API.
- **Никакого прямого ComfyUI:** изображения и I2V идут только через ASC-AI Image Adapter.
- **GPU Scheduler обязателен:** Director получает lease перед локальным LLM inference; Krea/Lustify, Wan и Qwen3-VL используют существующий ASC-AI control plane.
- **Строгий local-only:** облачные LLM/TTS/image/video/music providers и автоматическая сторонняя публикация блокируются в production-пути.
- **Локальный QC:** Prompt Intelligence регистрирует каждый артефакт; adaptive policy не грузит Qwen3-VL для каждого still, но всегда анализирует Wan-видео, Character Hub сцены, retries и контрольные кадры.
- **Экономия GPU:** большая часть сцен может быть статичной генерацией + лёгким zoom/pan; число Wan I2V сцен задаётся Director budget, а несовместимый aspect ratio отсекается до GPU enqueue.
- **Character Hub continuity:** Director может закрепить одного canonical персонажа между сценами через reference artifacts/LoRA из ASC-AI Character Hub.
- **Production manifest:** для каждой задачи сохраняется `production-manifest.json` с Scheduler job/stage/lease, seed/settings, artifact IDs, QC, fallback и фактическими выходами.
- **Headless API:** ASC-AI агент может планировать и запускать полный render через отдельный `/api/v1/asc-ai/*` namespace без знания внутреннего `VideoParams`.
- **Устойчивое ASC-AI-развёртывание:** production overlay включает Redis с AOF; queued API jobs возобновляются после рестарта, а оборванные in-flight jobs становятся явно retryable вместо вечного `processing`.
- Китайский README upstream сохранён как `README-upstream-zh.md` для удобной синхронизации.

## Что нужно локально

Для полного ASC-AI production режима должны быть доступны:

- GPU Scheduler — `:8090`;
- local Qwen / `prompt-llm-local` — внутренний `:8080`;
- Image Adapter — `:8091`;
- Prompt Intelligence — `:8094`;
- Visual Analyzer — `:8095`;
- Character Hub / Character Chat — `:8096` (нужен только при выборе постоянного персонажа);
- локальный OpenAI-compatible Chatterbox TTS — host `:4123`;
- FFmpeg;
- faster-whisper;
- общий каталог `/srv/ai-data`.

MoneyPrinterTurbo сам GPU не получает.

## Запуск на ASC-AI

```bash
git clone https://github.com/tornado909/MoneyPrinterTurbo.git
cd MoneyPrinterTurbo
cp config.example.toml config.toml

docker compose \
  -f docker-compose.yml \
  -f docker-compose.asc-ai.yml \
  -f docker-compose.chatterbox.yml \
  up -d --build

# Один раз загрузите русский reference voice:
bash scripts/bootstrap-chatterbox-ru-voice.sh /path/to/russian-reference.wav
```

WebUI:

```text
http://127.0.0.1:8501
```

API:

```text
http://127.0.0.1:8080/docs
```

Перед первой генерацией в WebUI нажмите **«Проверить ASC-AI»**. Проверяются Scheduler, local Qwen, Image Adapter, Prompt Intelligence, Visual Analyzer и Chatterbox.

Подробная архитектура и требования: [docs/ASC_AI_INTEGRATION.md](docs/ASC_AI_INTEGRATION.md).

## Director

В режиме **ASC-AI Director** доступны:

- целевая длительность ролика;
- максимальное число дорогих Wan I2V сцен;
- общий визуальный стиль;
- целевая аудитория;
- цель ролика;
- необязательный public research;
- необязательный canonical персонаж из Character Hub.

Director возвращает структурированный план:

```text
script
└── scenes[]
    ├── narration
    ├── duration_seconds
    ├── visual_strategy: LOCAL_IMAGE | LOCAL_VIDEO
    ├── visual_prompt
    ├── motion_prompt
    ├── transition
    └── overlay_text
```

Если пользователь уже передал готовый сценарий, Director не переписывает его, а только строит визуальный production plan.

## Headless API для ASC-AI Agent

Все endpoints используют ту же `app.api_key`-защиту, что и остальной API:

```text
GET  /api/v1/asc-ai/capabilities
GET  /api/v1/asc-ai/health
GET  /api/v1/asc-ai/characters
GET  /api/v1/asc-ai/characters/{character_id}
POST /api/v1/asc-ai/director/plan
POST /api/v1/asc-ai/production
```

`POST /director/plan` выполняет только research/Character Hub preflight + scheduler-managed Qwen planning и **не запускает Image Adapter/Wan render**.

`POST /production` выполняет полный local-only preflight, преобразует простой ASC-AI request в штатный `TaskVideoRequest` и ставит render в **тот же TaskManager/Redis queue**, что обычный `/videos`. Ответ возвращает обычный `task_id`; статус читается через `GET /api/v1/tasks/{task_id}`.

## Local-only policy

При `[asc_ai] local_only = true`:

- нельзя случайно уйти в OpenAI/OpenRouter/Claude/Kimi и другие cloud LLM;
- нельзя выбрать платный cloud video/image provider;
- нельзя выбрать cloud TTS;
- нельзя использовать удалённую AI-музыку;
- Prompt Intelligence `/plans` не вызывается, потому что текущий ASC-AI router допускает внешний fallback;
- если Director отключён, требуется готовый пользовательский сценарий.

Допустимы локальные материалы и локальная музыка.

## Локальная озвучка

Форк рассчитан на self-hosted **Chatterbox Multilingual** через OpenAI-compatible API. Рекомендуемый `docker-compose.chatterbox.yml` собирает CPU-only runtime из закреплённых commit SHA и подключает его к MoneyPrinterTurbo без GPU.

Русский production voice по умолчанию — `chatterbox:ru-default`. Он **обязан** существовать в voice library с `language=ru`; preflight проверяет это до постановки задачи в очередь. Загрузить reference sample можно один раз:

```bash
bash scripts/bootstrap-chatterbox-ru-voice.sh /path/to/russian-reference.wav
```

Без дополнительного TTS overlay ASC-AI Docker overlay может обращаться к уже запущенному host-local Chatterbox:

```text
http://host.docker.internal:4123/v1
```

Это сделано специально: TTS можно держать на CPU и не занимать VRAM, который нужен Director/Krea/Wan/Qwen3-VL.

## Результаты задачи

В каталоге задачи кроме обычных файлов появляются:

- `director-plan.json` — решение Director;
- `production-manifest.json` — фактическое выполнение сцен, QC, fallback и выходные материалы.

Это позволяет разбирать проблемную сцену отдельно, а не гадать, почему итоговый ролик получился хуже ожидаемого.

## Обычный запуск без ASC-AI

Совместимость с upstream сохранена. Windows/Linux/macOS запуск через `webui.bat` / `webui.sh` остаётся возможным, но строгие ASC-AI local-only defaults рассчитаны прежде всего на ваш production host.

## Тесты

Основные проверки:

```bash
python -m unittest test.services.test_asc_ai
python -m unittest test.services.test_controller_asc_ai
python -m unittest test.services.test_local_research
python -m unittest test.services.test_webui_i18n
```

Русская локаль должна полностью покрывать английскую и сохранять все форматные placeholders и Markdown-ссылки.

## Синхронизация с upstream

```bash
git remote add upstream https://github.com/harry0703/MoneyPrinterTurbo.git
git fetch upstream
git merge upstream/main
```

После merge сначала запускайте ASC-AI и i18n regression tests.

## Лицензия и авторство

Основано на **MoneyPrinterTurbo** автора **harry0703** и распространяется на условиях исходного [LICENSE](LICENSE). Атрибуция upstream сохраняется.
