# Russian Edition notes

Baseline upstream commit: `ad5496f1b729d1d7e361dd972015d26c08b0e052`.

## Scope

The Russian edition deliberately keeps the media-generation pipeline compatible with upstream. Fork-specific changes are limited to:

- complete Russian WebUI localization;
- Russian-first WebUI defaults and branding;
- Russian project documentation;
- localization regression tests;
- an untouched copy of the upstream Chinese README for easier rebasing.

## Localization contract

`webui/i18n/ru.json` is a primary locale in this fork. It must contain every key from `webui/i18n/en.json`.

The regression test also verifies that runtime format placeholders and Markdown URLs remain compatible with the English source. If upstream adds a new English key, CI should fail until the Russian translation is added.

## Updating from upstream

```bash
git remote add upstream https://github.com/harry0703/MoneyPrinterTurbo.git
git fetch upstream
git merge upstream/main
python -m unittest test.services.test_webui_i18n
```

Resolve normal merge conflicts first. Then add translations for any new English UI keys reported by the localization test.

## Upstream attribution

Upstream project: https://github.com/harry0703/MoneyPrinterTurbo

The original `LICENSE` remains unchanged.
