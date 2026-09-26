# Интеграция MoneyPrinterTurbo RU с ASC-AI

MoneyPrinterTurbo используется как production/editor слой, а ASC-AI — как локальный AI control plane. Платные AI API в production-пути не требуются.

## Production pipeline

1. Пользователь вводит тему или готовый сценарий.
2. **Director** создаёт сценарий и scene plan локальным `Qwen3-8B-Q4_K_M`.
3. Перед обращением к `prompt-llm-local` Director создаёт job/stage и получает **GPU Scheduler lease** (`:8090`). Прямой неучтённый GPU-доступ не используется.
4. Director ограничивает число дорогих `LOCAL_VIDEO` сцен; остальные сцены идут через `LOCAL_IMAGE`.
5. Изображения генерируются только через **Image Adapter** (`:8091`), который сам работает с Scheduler и ComfyUI.
6. Для динамических сцен Image Adapter запускает локальный Wan I2V. MoneyPrinterTurbo не обращается к ComfyUI напрямую.
7. Каждый полученный материал регистрируется в **Prompt Intelligence artifact registry** (`:8094`) и проходит локальный **Visual Analyzer** (`:8095`) через канонический `/api/v1/evidence/analyze`.
8. QC использует sealed `technical_assessment`. Visual Analyzer намеренно не выдаёт выдуманный prompt-similarity score.
9. Не прошедшее технический QC изображение может быть один раз локально перегенерировано; неудачный I2V может откатиться на исходное изображение.
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

В Docker overlay используются внутренние имена: `gpu-scheduler:8090`, `prompt-llm-local:8080`, `image-adapter:8091`, `prompt-intelligence:8094`, `visual-analyzer:8095`, `chatterbox-tts:4123`.

## Запуск на ASC-AI

    git clone https://github.com/tornado909/MoneyPrinterTurbo.git
    cd MoneyPrinterTurbo
    cp config.example.toml config.toml
    docker compose -f docker-compose.yml -f docker-compose.asc-ai.yml up -d --build

Контейнеры MoneyPrinterTurbo присоединяются к external network `asc-ai-stack_default` и монтируют `/srv/ai-data` для обмена артефактами. Сам MoneyPrinterTurbo GPU не получает.

ASC-AI overlay также поднимает отдельный `redis:7-alpine` только во внутренней сети MoneyPrinterTurbo. Он хранит очередь и состояние задач с AOF; наружу порт Redis не публикуется.

WebUI: `http://127.0.0.1:8501`

API: `http://127.0.0.1:8080/docs`

## Preflight

    curl http://127.0.0.1:8090/health
    curl http://127.0.0.1:8080/health
    curl http://127.0.0.1:8091/health
    curl http://127.0.0.1:8094/health
    curl http://127.0.0.1:8095/health

Director дополнительно использует Scheduler API `/api/v1/jobs`, `/api/v1/stages/*` и `/api/v1/leases/*`.

## Локальная озвучка

Строгий local-only режим использует self-hosted Chatterbox через OpenAI-compatible `/v1/audio/speech`. ASC-AI overlay не предполагает несуществующий контейнер внутри основного стека: по умолчанию он обращается к локальному host endpoint `http://host.docker.internal:4123/v1`.

Поднимите официальный `travisvn/chatterbox-tts-api` в CPU-only режиме на хосте и убедитесь, что:

    curl http://127.0.0.1:4123/health

возвращает успешный ответ. CPU-режим выбран намеренно: озвучка не должна занимать VRAM и конкурировать с Director, Krea/Lustify, Wan или Qwen3-VL. Если сервис опубликован по другому адресу, задайте `MPT_CHATTERBOX_BASE_URL` перед запуском compose.

Preflight MoneyPrinterTurbo проверяет Chatterbox вместе с Scheduler, Prompt LLM, Image Adapter, Prompt Intelligence и Visual Analyzer.

## Экономия GPU

- сценарий + scene plan: один локальный Qwen3-8B inference;
- большинство сцен: Krea/Lustify image;
- image → лёгкий zoom/pan в MoneyPrinterTurbo;
- только выбранные Director сцены: Wan I2V;
- Qwen3-VL используется только для bounded QC;
- локальные LLM/VLM освобождают VRAM после inference согласно ASC-AI scheduler policy.

Такой режим значительно дешевле по вычислениям, чем генерация всего ролика через I2V, но оставляет Director возможность выделять действительно важные динамические сцены.
