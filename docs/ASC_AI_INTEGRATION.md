# Интеграция MoneyPrinterTurbo RU с ASC-AI

Этот форк использует ASC-AI как локальный AI control plane, а MoneyPrinterTurbo — как production/editor слой.

## Архитектура

1. Пользователь вводит тему или готовый сценарий.
2. MoneyPrinterTurbo вызывает ASC-AI Prompt Intelligence: POST /api/v1/prompt-intelligence/director/plan.
3. Director использует только локальный Qwen через ManagedInference; GPU admission выдаёт GPU Scheduler.
4. Director возвращает сценарий и план сцен. Большинство сцен — LOCAL_IMAGE, ограниченное число важных сцен — LOCAL_VIDEO.
5. MoneyPrinterTurbo вызывает Image Adapter, а не ComfyUI напрямую.
6. Image Adapter получает Scheduler lease и создаёт изображение.
7. Для LOCAL_VIDEO исходное изображение помещается в /srv/ai-data/input, после чего Image Adapter запускает локальный Wan I2V.
8. MoneyPrinterTurbo собирает сцены MoviePy/FFmpeg, делает локальную озвучку и Whisper-субтитры.

## Local-only policy

При asc_ai.local_only = true запрещены облачные/платные LLM, облачные TTS, облачная генерация изображений/видео, удалённая AI-музыка и автоматический Upload-Post.

Разрешены ASC-AI Director, ASC-AI Image Adapter, локальные материалы, self-hosted Chatterbox Multilingual, local faster-whisper и локальная музыка.

## Русская озвучка

Используйте Chatterbox TTS API в self-hosted multilingual режиме с DEVICE=cpu.
MoneyPrinterTurbo ожидает OpenAI-compatible endpoint http://127.0.0.1:4123/v1.
В Docker override адрес автоматически меняется на host.docker.internal:4123/v1.

## Запуск

    git clone https://github.com/tornado909/MoneyPrinterTurbo.git
    cd MoneyPrinterTurbo
    cp config.example.toml config.toml
    docker compose -f docker-compose.yml -f docker-compose.asc-ai.yml up -d --build

WebUI: http://127.0.0.1:8501
API: http://127.0.0.1:8080/docs

MoneyPrinterTurbo контейнер не получает GPU. /srv/ai-data монтируется для обмена локальными артефактами с ASC-AI.

## Проверка

- GET http://127.0.0.1:8094/health
- GET http://127.0.0.1:8094/api/v1/prompt-intelligence/director/capabilities
- GET http://127.0.0.1:8091/health
- Chatterbox: http://127.0.0.1:4123

Если Director сообщает not_configured, в ASC-AI должен быть включён PROMPT_LOCAL_ENABLED=true и настроен PROMPT_LOCAL_LLM_URL.

## Производственная стратегия

Director экономит GPU: обычная сцена — локальная Krea/Lustify картинка с лёгким zoom/pan; важная динамическая сцена — локальный Wan I2V. Количество I2V-сцен ограничено. Если I2V не удался, готовое изображение используется как fallback.
