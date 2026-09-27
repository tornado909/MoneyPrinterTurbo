# ASC-AI Production Acceptance

This document is the acceptance checklist for the Russian ASC-AI edition. It is
kept in the repository so the pull-request CI validates the same contract that
is expected on the `asc-ai` production host.

## Automated CI

The repository workflow must pass:

- Python compileall;
- Ruff;
- full pytest suite with coverage;
- Redis-backed task/state tests;
- Windows import smoke;
- Russian i18n completeness.

## Local-only invariants

- Cloud LLM/TTS/image/video/music providers are not reachable from the strict
  `asc_ai.local_only=true` production path.
- All text inference uses scheduler-managed local Qwen.
- Image and Wan generation go through Image Adapter rather than ComfyUI.
- Visual analysis forbids external egress.
- Prompt Intelligence `/plans` is not used in strict local-only mode because
  the current router may have an external fallback.
- Chatterbox is CPU-only and the selected Russian voice must exist with
  `language=ru`.

## Director invariants

- Scene narration is a verbatim ordered partition of the final script.
- Scene plans are typed and malformed Qwen JSON gets at most one bounded repair.
- Real TTS duration deterministically retimes scenes to 2..15 seconds.
- A `LOCAL_VIDEO` scene consumes one validated five-second
  `wan_i2v_default.v1` GPU job and is trimmed/extended locally afterwards.
- Scene order, duration, transition and short overlay callouts are executed by
  the production path, not stored as metadata only.
- Character Hub identity references and local LoRAs reach Image Adapter.
- Failed Wan generation or unsupported aspect can fall back to the approved
  still without starting additional paid/external work.

## Reliability invariants

- API production supports persistent `Idempotency-Key`.
- Redis queued API jobs resume after restart.
- Interrupted in-flight API jobs become explicit retryable failures instead of
  remaining forever in `processing`.
- WebUI in-memory jobs use separate restart recovery semantics.
- Production writes `director-plan.json`, `director-execution-plan.json` and
  `production-manifest.json` with bounded execution provenance.

## Host acceptance

After CI is green, the final host acceptance should run one short 9:16 Russian
video through `POST /api/v1/asc-ai/production` and verify:

1. Scheduler lease/reclaim for Qwen, Krea/Lustify, optional Wan and Qwen3-VL.
2. No unexpected external AI egress.
3. `ru-default` Chatterbox voice and local Whisper subtitles.
4. Final video duration follows real voiceover timing.
5. Manifest contains scene workflow, QC, timing and fallback provenance.
