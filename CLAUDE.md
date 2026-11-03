# CLAUDE.md

Pixel2Num is an internal tool for about 10 people on the operations team. Operators DM photos of top-up cards to a Mattermost bot, and the bot replies with an Excel file of the recharge codes it read using OCR. The bot and the OCR extractor run in **one Python process in one Docker container**. There is no HTTP API between them.

## Workflow: spec-driven

1. **[SPEC.md](SPEC.md) is the source of truth.** Before writing code, find the requirement IDs (for example `R-RANGE-2`) that the change implements. If the work isn't covered by the spec, or it conflicts with the spec, stop and propose a spec change first. Don't silently diverge.
2. **[HARNESS.md](HARNESS.md) defines "done".** Run the levels the change touches. Add the tests that HARNESS lists for the IDs you implement, and keep its traceability table up to date.
3. Work through the delivery plan in SPEC §6 one step at a time, with one commit per step. Commit messages cite IDs, for example `R-LIB-1..3: make extractor importable without Paddle`.
4. Don't answer SPEC §9's open questions on your own. Ask the user.

## Commands

```sh
.venv/bin/python -m unittest discover -s tests -v          # L1: fast, no Paddle
.venv-ocr/bin/python -m unittest tests.test_extract_parity  # L2: needs fixtures + Paddle
python -m pixel2num.extract_numbers <folder> out.xlsx       # extractor CLI (must keep working)
docker compose up -d --build && docker compose logs -f bot  # run
```

The full commands for each level are in HARNESS.md.

## Layout

- `pixel2num/bot.py` holds the polling loop, the per-user batches, and the SQLite state machine (`queued → processing → ready → uploaded → done/failed`). Keep this state machine. It already handles restarts and retries.
- `pixel2num/api.py` holds the Mattermost REST client, `validate_report`, and `InputError`. An `InputError` message is shown to the user, so it must be safe to display.
- `pixel2num/extract_numbers.py` is the OCR extractor library plus its CLI. It uses PaddleOCR, OpenCV and openpyxl.
- `pixel2num/extractor.py` (planned) is the thin adapter the bot calls with `convert(job, images) -> bytes`.
- `tests/` uses `unittest` only, with fakes for Mattermost and the extractor.

## Rules

- **Charge codes are money.** Never put a code in logs, exceptions, chat messages, test names, or commits. Log the job ID and the exception type only. Real card photos never go into Git (use `tests/fixtures/cards/`, which is git-ignored).
- **Paddle stays out of fast paths.** Importing any `pixel2num` module must not load PaddleOCR. L1 tests must pass without Paddle installed.
- **Extraction results don't change by accident.** Any edit to `extract_numbers.py` must pass the L2 parity test. Improving OCR accuracy needs its own spec.
- Never use a user-supplied filename as a filesystem path.
- Keep the container hardening as it is: read-only root filesystem, UID 10001, no inbound ports, models built into the image.
- Match the existing style: small, standard library first, no new frameworks. Don't add mmpy_bot, FastAPI, Celery, and so on.
- The runtime is Linux and Docker. Python and the dependency versions are pinned in `requirements.txt` and the `Dockerfile`, and they must match each other.
