# MoneyPrinterTurbo RU + ASC-AI Director

Русская редакция [MoneyPrinterTurbo](https://github.com/harry0703/MoneyPrinterTurbo), развиваемая как локальный AI production pipeline для ASC-AI.

**Стандартный путь форка:**

`тема / сценарий → Director → scene plan → Krea/Lustify → выборочные Wan I2V сцены → локальный QC → Chatterbox → Whisper → FFmpeg → готовый ролик`

Русский | [English](README-en.md) | [日本語](README-ja.md) | [简体中文](README-upstream-zh.md) | [Upstream](https://github.com/harry0703/MoneyPrinterTurbo)

## Чем этот форк отличается

- **Полный русский WebUI:** русская локаль является primary edition и должна полностью покрывать английские UI-ключи.
- **ASC-AI — источник по умолчанию:** `video_source = "asc_ai"`.
- **Director по умолчанию:** локальный Qwen3-8B создаёт сценарий и production-ready scene plan.
- **Никакого прямого ComfyUI:** изображения и I2V идут только через ASC-AI Image Adapter.
- **GPU Scheduler обязателен:** Director получает lease перед локальным LLM inference; Krea/Lustify, Wan и Qwen3-VL используют существующий ASC-AI control plane.
- **Строгий local-only:** облачные LLM/TTS/image/video/music providers и автоматическая сторонняя публикация блокируются в production-пути.
- **Локальный QC:** Prompt Intelligence регистрирует артефакты, Visual Analyzer/Qwen3-VL анализирует фактический результат без выдуманного semantic score.
- **Экономия GPU:** большая часть сцен может быть статичной генерацией + лёгким zoom/pan; число Wan I2V сцен задаётся Director budget.
- **Production manifest:** для каждой задачи сохраняется `production-manifest.json` с попытками генерации, QC, fallback и фактическими выходами.
- **Устойчивое ASC-AI-развёртывание:** production overlay включает Redis с AOF для очереди/состояний задач.
- Китайский README upstream сохранён как `README-upstream-zh.md` для удобной синхронизации.

## Что нужно локально

Для полного ASC-AI production режима должны быть доступны:

- GPU Scheduler — `:8090`;
- local Qwen / `prompt-llm-local` — внутренний `:8080`;
- Image Adapter — `:8091`;
- Prompt Intelligence — `:8094`;
- Visual Analyzer — `:8095`;
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
  up -d --build
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
- цель ролика.

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

Форк рассчитан на self-hosted **Chatterbox Multilingual** через OpenAI-compatible API. В ASC-AI Docker overlay запросы по умолчанию идут на:

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
