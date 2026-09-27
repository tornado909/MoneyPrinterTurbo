# MoneyPrinterTurbo RU + ASC-AI Director

Русская редакция [MoneyPrinterTurbo](https://github.com/harry0703/MoneyPrinterTurbo), развиваемая как локальный AI production pipeline для ASC-AI.

**Стандартный путь форка:**

`тема / сценарий → Director → scene plan → public/local asset routing → Krea/Lustify → выборочные Wan I2V сцены → scene QC → Chatterbox → Whisper → FFmpeg → Final QA → готовый ролик`

Русский | [English](README-en.md) | [日本語](README-ja.md) | [简体中文](README-upstream-zh.md) | [Upstream](https://github.com/harry0703/MoneyPrinterTurbo)

## Чем этот форк отличается

- **Полный русский WebUI:** русская локаль является primary edition и должна полностью покрывать английские UI-ключи.
- **ASC-AI — источник по умолчанию:** `video_source = "asc_ai"`.
- **Director по умолчанию:** локальный Qwen3-8B создаёт typed production-ready scene plan; malformed JSON получает одну bounded repair-попытку, а public MediaWiki research кэшируется и при необходимости добирается из fallback-языка без платного AI API.
- **Никакого прямого ComfyUI:** изображения и I2V идут только через ASC-AI Image Adapter.
- **GPU Scheduler обязателен:** Director получает lease перед локальным LLM inference; Krea/Lustify, Wan и Qwen3-VL используют существующий ASC-AI control plane.
- **Строгий local-only:** облачные LLM/TTS/image/video/music providers и автоматическая сторонняя публикация блокируются в production-пути; внутренние service URL не могут быть подменены внешним доменом.
- **Cost-aware public egress:** бесплатные MediaWiki research и Wikimedia Commons factual stills включены по умолчанию, ограничены по объёму и не используют AI API. Для приватных задач оба канала можно отключить независимо; AI-инференс всегда остаётся локальным.
- **Локальный QC + Final QA:** Prompt Intelligence регистрирует каждый артефакт; adaptive policy экономит Qwen3-VL на still-сценах, Wan-видео проверяются всегда, а готовый MP4 перед `complete` проходит CPU structural gate и по умолчанию один локальный Visual Analyzer pass с отчётом `final-qc.json`.
- **Экономия GPU:** большая часть сцен может быть статичной генерацией + лёгким zoom/pan; bounded `PUBLIC_IMAGE` сцены реально обходят diffusion через лицензированные Wikimedia Commons материалы с local fallback; число Wan I2V сцен задаётся Director budget, а несовместимый aspect ratio отсекается до GPU enqueue.
- **Character Hub continuity:** Director может закрепить одного canonical персонажа между сценами через reference artifacts/LoRA из ASC-AI Character Hub.
- **Production evidence:** сохраняются Director plan/execution plan, `production-manifest.json`, итоговый `final-qc.json` и, для реально использованных Wikimedia-кадров, credits JSON/TXT.
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

# Production: используется образ форка из GHCR; пока он не опубликован,
# Compose собирает тот же fork локально из текущего checkout.
MPT_BIND_HOST=0.0.0.0 docker compose \
  -f docker-compose.release.yml \
  -f docker-compose.asc-ai.yml \
  -f docker-compose.chatterbox.yml \
  up -d

# Development из текущего checkout:
# docker compose -f docker-compose.yml -f docker-compose.asc-ai.yml \
#   -f docker-compose.chatterbox.yml up -d --build

# Один раз загрузите русский reference voice:
bash scripts/bootstrap-chatterbox-ru-voice.sh /path/to/russian-reference.wav
```

WebUI (при `MPT_BIND_HOST=0.0.0.0`):

```text
http://<ASC-AI-IP>:8501
```

API:

```text
http://<ASC-AI-IP>:8080/docs
```

Без `MPT_BIND_HOST` release-compose безопасно публикует эти порты только на `127.0.0.1`.

Перед первой генерацией в WebUI нажмите **«Проверить ASC-AI»**. Проверяются Scheduler, local Qwen, Image Adapter, Prompt Intelligence, Visual Analyzer и Chatterbox.

Подробная архитектура и требования: [docs/ASC_AI_INTEGRATION.md](docs/ASC_AI_INTEGRATION.md).

## Director

В режиме **ASC-AI Director** доступны:

- целевая длительность ролика;
- максимальное число дорогих Wan I2V сцен;
- общий визуальный стиль;
- целевая аудитория;
- цель ролика;
- bounded public research (включён cost-aware default, можно отключить для private task);
- bounded Wikimedia visual search (включён cost-aware default, можно отключить отдельно);
- необязательный canonical персонаж из Character Hub.

Director возвращает структурированный план:

```text
script
└── scenes[]
    ├── narration
    ├── duration_seconds
    ├── visual_strategy: LOCAL_IMAGE | LOCAL_VIDEO | PUBLIC_IMAGE
    ├── visual_prompt
    ├── public_media_query
    ├── motion_prompt
    ├── transition: cut | fade_* | slide_* | zoom_*
    └── overlay_text: короткий экранный callout
```

Если пользователь уже передал готовый сценарий, Director не переписывает его, а только строит визуальный production plan. После готовой TTS-дорожки scene durations детерминированно подгоняются под фактическую длину озвучки без второго LLM-запроса. Переходы и короткие `overlay_text` callout'ы запекаются в scene clips перед финальной склейкой; ошибка такого post-process не уничтожает уже прошедший QC исходный кадр.

## Headless API для ASC-AI Agent

Все endpoints используют ту же `app.api_key`-защиту, что и остальной API:

```text
GET  /api/v1/asc-ai/capabilities
GET  /api/v1/asc-ai/health
GET  /api/v1/asc-ai/characters
GET  /api/v1/asc-ai/characters/{character_id}
POST /api/v1/asc-ai/director/plan
POST /api/v1/asc-ai/production
POST /api/v1/asc-ai/production/{task_id}/retry
```

`POST /director/plan` выполняет только research/Character Hub preflight + scheduler-managed Qwen planning и **не запускает Image Adapter/Wan render**.

`POST /production` выполняет полный local-only preflight, преобразует простой ASC-AI request в штатный `TaskVideoRequest` и ставит render в **тот же TaskManager/Redis queue**, что обычный `/videos`. Ответ возвращает обычный `task_id`; статус читается через `GET /api/v1/tasks/{task_id}`.

При рестарте API **queued** задачи остаются в Redis и автоматически снова заполняют свободные worker slots. Задача, которая уже была извлечена из очереди и выполнялась в момент падения процесса, не запускается слепо второй раз: она становится `retryable=true` с `failed_stage=startup_recovery`. Повтор выполняется через `POST /api/v1/asc-ai/production/{task_id}/retry`; endpoint идемпотентен и создаёт ровно одного child-task с новым ID и ссылкой `retry_of`.

Старый `/api/v1/asc-ai/tasks/{task_id}/retry` сохранён как совместимый alias и вызывает тот же canonical handler.

Redis сохраняет queued jobs и durable JSON-safe snapshot запроса. После рестарта queued jobs автоматически продолжаются; task, который уже был взят worker'ом и оборвался вместе с процессом, помечается `retryable=true` вместо опасного автоматического replay. Его можно безопасно пересоздать через `POST /asc-ai/production/{task_id}/retry`; старая задача получит ссылку `retried_as` на новую.

Для agent retries поддерживается persistent header `Idempotency-Key` (8–128 символов). Один и тот же ключ + тот же payload возвращает тот же детерминированный `task_id` без нового render; повтор ключа с другим payload возвращает `409`. Claim хранится в task state и переживает рестарт API.

## Cost-aware public sources

По умолчанию Director может отправлять **только тему/поисковый запрос** в MediaWiki и Wikimedia Commons. Это не AI API и не требует ключей/оплаты. Commons-кандидаты проходят license policy, relevance ranking, локальный QC и всегда имеют fallback на локальную генерацию. Для закрытых корпоративных тем public egress можно полностью отключить двумя переключателями **Public Research** и **Public Media**.

## Local-only policy

При `[asc_ai] local_only = true`:

- нельзя случайно уйти в OpenAI/OpenRouter/Claude/Kimi и другие cloud LLM;
- ASC-AI service endpoints принимаются только на loopback/private/link-local, встроенных Docker-hostnames, `*.local` или явно перечисленных `local_service_hosts`;
- нельзя выбрать платный cloud video/image provider;
- нельзя выбрать cloud TTS;
- нельзя использовать удалённую AI-музыку;
- Prompt Intelligence `/plans` не вызывается, потому что текущий ASC-AI router допускает внешний fallback;
- если Director отключён, требуется готовый пользовательский сценарий.

Допустимы локальные материалы и локальная музыка.

Публичный HTTP отделён от AI-egress policy. В этой cost-aware редакции bounded MediaWiki/Wikimedia Commons включены по умолчанию, но в WebUI/Headless API есть два независимых task-level переключателя, которыми их можно полностью отключить для приватной темы. Выбор фиксируется в queued task и в `director-plan.json → public_egress`, поэтому последующее изменение UI не меняет уже поставленную задачу.

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
- `director-execution-plan.json` — plan после детерминированной подгонки к реальной TTS-длительности;
- `production-manifest.json` — фактическое выполнение сцен, QC, fallback и выходные материалы;
- `final-qc.json` — финальный structural/VLM acceptance готового MP4;
- `public-media-credits.json` / TXT — attribution только для реально использованных Commons-материалов.

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
