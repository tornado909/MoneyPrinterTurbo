# Интеграция MoneyPrinterTurbo RU с ASC-AI

MoneyPrinterTurbo используется как production/editor слой, а ASC-AI — как локальный AI control plane. Платные AI API в production-пути не требуются.

## Production pipeline

1. Пользователь вводит тему или готовый сценарий.
2. **Director** создаёт сценарий и scene plan локальным `Qwen3-8B-Q4_K_M`.
3. Перед обращением к `prompt-llm-local` Director создаёт job/stage и получает **GPU Scheduler lease** (`:8090`). Прямой неучтённый GPU-доступ не используется.
4. Director опционально получает bounded MediaWiki research и canonical visual identity из Character Hub (`:8096`), затем ограничивает число дорогих `LOCAL_VIDEO` сцен; остальные сцены идут через `LOCAL_IMAGE`.
5. Изображения генерируются только через **Image Adapter** (`:8091`), который сам работает с Scheduler и ComfyUI.
6. Для динамических сцен Image Adapter запускает локальный Wan I2V. MoneyPrinterTurbo не обращается к ComfyUI напрямую.
7. Каждый полученный материал регистрируется в **Prompt Intelligence artifact registry** (`:8094`). Adaptive still-QC вызывает **Visual Analyzer** (`:8095`) только для контрольных/character/retry кадров; Wan-видео анализируется всегда.
8. QC использует sealed `technical_assessment`. Visual Analyzer намеренно не выдаёт выдуманный prompt-similarity score; для пропущенных still VLM-pass manifest явно фиксирует `visual_analysis_skipped=true`.
9. Не прошедшее технический QC изображение получает новый idempotency key и реально перегенерируется. Перед Wan enqueue проверяется совместимость aspect ratio; несовместимая вертикальная сцена сразу уходит в still fallback без расхода GPU.
10. MoneyPrinterTurbo собирает ролик MoviePy/FFmpeg, делает локальную озвучку и локальные Whisper-субтитры.

## Local-only policy

При `asc_ai.local_only = true` production-путь работает fail-closed:

- запрещены облачные/платные LLM;
- запрещены облачные TTS;
- запрещены облачные генераторы изображений и видео;
- запрещены удалённые AI-music providers;
- запрещён автоматический Upload-Post;
- если Director выключен, для запуска без облачного LLM необходимо передать готовый сценарий.

Разрешены GPU Scheduler, локальный Qwen3-8B Director, Prompt Intelligence, Image Adapter, Krea/Lustify, Wan, Visual Analyzer/Qwen3-VL, self-hosted Chatterbox, local faster-whisper и локальные пользовательские материалы.

## Director и Prompt Intelligence

В ASC-AI нет публичного `/director/plan`. Director принадлежит MoneyPrinterTurbo как orchestration layer. Для текстового планирования он использует тот же scheduler-managed `prompt-llm-local`, что и ASC-AI ManagedInference. Prompt Intelligence остаётся каноническим registry/knowledge/artifact layer.

## ASC-AI endpoints

- GPU Scheduler: `http://127.0.0.1:8090`
- Prompt local LLM: `http://127.0.0.1:8080`
- Image Adapter: `http://127.0.0.1:8091`
- Prompt Intelligence: `http://127.0.0.1:8094`
- Visual Analyzer: `http://127.0.0.1:8095`
- Character Hub: `http://127.0.0.1:8096`

В Docker overlay используются внутренние имена ASC-AI: `gpu-scheduler:8090`, `prompt-llm-local:8080`, `image-adapter:8091`, `prompt-intelligence:8094`, `visual-analyzer:8095`, `character-chat:8096`. Chatterbox намеренно остаётся host-local CPU endpoint: `http://host.docker.internal:4123/v1`.

## Запуск на ASC-AI

    git clone https://github.com/tornado909/MoneyPrinterTurbo.git
    cd MoneyPrinterTurbo
    cp config.example.toml config.toml
    docker compose -f docker-compose.yml -f docker-compose.asc-ai.yml -f docker-compose.chatterbox.yml up -d --build
    bash scripts/bootstrap-chatterbox-ru-voice.sh /path/to/russian-reference.wav

Контейнеры MoneyPrinterTurbo присоединяются к external network `asc-ai-stack_default` и монтируют `/srv/ai-data` для обмена артефактами. Сам MoneyPrinterTurbo GPU не получает.

ASC-AI overlay также поднимает отдельный `redis:7-alpine` только во внутренней сети MoneyPrinterTurbo. Он хранит очередь и состояние задач с AOF; наружу порт Redis не публикуется.

При рестарте API persisted **queued** задачи автоматически снова занимают свободные worker slots. Задача, которая уже была извлечена из очереди и выполнялась в момент падения процесса, не запускается повторно автоматически: её состояние переводится в `failed` с `failed_stage=startup_recovery`, `retryable=true` и `recovery_action=resubmit`. Это сознательная защита от двойной TTS/рендер/публикации. WebUI имеет отдельный in-process executor и recovery API-очереди его задачи не трогает.

WebUI: `http://127.0.0.1:8501`

API: `http://127.0.0.1:8080/docs`

## Preflight

    curl http://127.0.0.1:8090/health
    curl http://127.0.0.1:8080/health
    curl http://127.0.0.1:8091/health
    curl http://127.0.0.1:8094/health
    curl http://127.0.0.1:8095/health
    curl http://127.0.0.1:8096/health

Director дополнительно использует Scheduler API `/api/v1/jobs`, `/api/v1/stages/*` и `/api/v1/leases/*`.

## Локальная озвучка

Строгий local-only режим использует self-hosted Chatterbox через OpenAI-compatible `/v1/audio/speech`. Рекомендуемый `docker-compose.chatterbox.yml` собирает CPU-only сервис из закреплённых SHA Chatterbox API и multilingual runtime, поэтому он не занимает GPU Scheduler.

Русский voice `ru-default` должен быть добавлен в `/v1/voices` с метаданными `language=ru`. MoneyPrinterTurbo проверяет это через voice catalog до production; неизвестный voice больше не может тихо откатиться на English default.

Если Chatterbox уже запущен отдельно на хосте, базовый ASC-AI overlay по-прежнему умеет обращаться к `http://host.docker.internal:4123/v1`. Убедитесь, что:

    curl http://127.0.0.1:4123/health

возвращает успешный ответ. CPU-режим выбран намеренно: озвучка не должна занимать VRAM и конкурировать с Director, Krea/Lustify, Wan или Qwen3-VL. Если сервис опубликован по другому адресу, задайте `MPT_CHATTERBOX_BASE_URL` перед запуском compose.

Preflight MoneyPrinterTurbo проверяет Chatterbox вместе с Scheduler, Prompt LLM, Image Adapter, Prompt Intelligence и Visual Analyzer.

## Headless control plane

MoneyPrinterTurbo публикует отдельный authenticated namespace для ASC-AI Agent:

    GET  /api/v1/asc-ai/capabilities
    GET  /api/v1/asc-ai/health
    GET  /api/v1/asc-ai/characters
    GET  /api/v1/asc-ai/characters/{character_id}
    POST /api/v1/asc-ai/director/plan
    POST /api/v1/asc-ai/production

Director preview является planning-only: Scheduler/Qwen/Character Hub/research разрешены, но Image Adapter/Wan не запускаются.

Production endpoint делает полный local-only preflight и ставит задачу в штатный MoneyPrinterTurbo TaskManager. Возвращаемый `task_id` совместим с обычным:

    GET /api/v1/tasks/{task_id}

Для безопасных agent retries используйте:

    Idempotency-Key: <stable-key-8-to-128-chars>

Task ID детерминирован от ключа, а canonical request fingerprint сохраняется атомарно через Redis/Memory state. Повтор того же ключа и payload не ставит вторую задачу; тот же ключ с другим payload получает HTTP 409.

Никакой второй очереди или отдельного renderer lifecycle для ASC-AI API не создаётся.

## Экономия GPU

- сценарий + scene plan: один локальный Qwen3-8B inference;
- большинство сцен: Krea/Lustify image;
- image → лёгкий zoom/pan в MoneyPrinterTurbo;
- только выбранные Director сцены: Wan I2V;
- Qwen3-VL используется adaptive: первый/каждый N-й still, Character Hub сцены, retries и все Wan-видео;
- MediaWiki research — bounded public HTTP без AI API; результаты кэшируются на retry и при нехватке данных могут добираться из fallback-языка, source URLs остаются в provenance;
- локальные LLM/VLM освобождают VRAM после inference согласно ASC-AI scheduler policy.

Такой режим значительно дешевле по вычислениям, чем генерация всего ролика через I2V, но оставляет Director возможность выделять действительно важные динамические сцены.
