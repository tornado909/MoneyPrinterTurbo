# MoneyPrinterTurbo RU

**Русскоязычная редакция [MoneyPrinterTurbo](https://github.com/harry0703/MoneyPrinterTurbo)** — генератора коротких видео по теме или сценарию: LLM → материалы → озвучка → субтитры → музыка → итоговый ролик.

Русский | [English](README-en.md) | [日本語](README-ja.md) | [简体中文](README-upstream-zh.md) | [Upstream](https://github.com/harry0703/MoneyPrinterTurbo)

> Форк сохраняет основной видеоконвейер и API upstream. Отличия сосредоточены на русской локализации, русских дефолтах, документации и проверках, которые не дают новым строкам интерфейса незаметно откатываться на английский.

## Что улучшено

- **Полный русский WebUI:** русский файл локализации покрывает все **570 из 570** ключей английского интерфейса.
- Переведены новые разделы **LLM/TTS, VoxCPM, MuAPI, Metaso MiniMax, Shengsuan Cloud и AI Video/LoomLoom**.
- **Русский язык интерфейса используется по умолчанию**, даже если браузер не прислал поддерживаемую locale.
- В `config.example.toml` стартовый язык WebUI — `ru`.
- Добавлены регрессионные проверки полноты русского перевода, форматных параметров вида `{amount}` / `{error}` и Markdown-ссылок.
- Китайский README upstream сохранён как `README-upstream-zh.md`, чтобы форк было проще синхронизировать.
- Основной видеоконвейер намеренно не переписан — обновления upstream должны переноситься с минимальным числом конфликтов.

## Быстрый старт

### Windows

```powershell
git clone https://github.com/tornado909/MoneyPrinterTurbo.git
cd MoneyPrinterTurbo
webui.bat
```

При первом запуске приложение создаёт локальный `config.toml` из примера. API-ключи храните только в локальном `config.toml` и не коммитьте их.

### Linux / macOS

```bash
git clone https://github.com/tornado909/MoneyPrinterTurbo.git
cd MoneyPrinterTurbo
chmod +x webui.sh
./webui.sh
```

### Docker

Используйте штатные `docker-compose*.yml` из upstream. Перед запуском проверьте локальный `config.toml` и параметры выбранных провайдеров.

## Русскоязычное видео

1. Введите тему или готовый сценарий.
2. Язык сценария можно оставить на автоопределении или выбрать `ru-RU`.
3. Выберите LLM-провайдера.
4. Выберите источник материалов: стоки, локальные файлы или поддерживаемая AI-генерация.
5. Для бесплатной Edge TTS / Azure TTS V1 в проекте доступны русские голоса, включая `ru-RU-SvetlanaNeural` и `ru-RU-DmitryNeural`.
6. Настройте субтитры, музыку и монтаж и запустите генерацию.

## Проверка локализации

```bash
python -m unittest test.services.test_webui_i18n
```

Если upstream добавит новый английский UI-ключ без русского перевода, проверка намеренно завершится ошибкой.

## Синхронизация с upstream

```bash
git remote add upstream https://github.com/harry0703/MoneyPrinterTurbo.git
git fetch upstream
git merge upstream/main
```

После merge снова запустите тест локализации и переведите новые ключи, если они появились.

Подробнее о принципах форка: [docs/RUSSIAN_EDITION.md](docs/RUSSIAN_EDITION.md).

## Лицензия и авторство

Основано на проекте **MoneyPrinterTurbo** автора **harry0703** и распространяется на условиях исходного файла [LICENSE](LICENSE). Все права и атрибуция upstream сохраняются.
