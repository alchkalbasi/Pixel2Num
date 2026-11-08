# HARNESS — how SPEC 001 is checked

Status: **IN USE**

This file defines the checks that prove [SPEC.md](SPEC.md) is met. A change is not finished until its level passes. The levels run from fast and cheap to slow and real, and each level assumes the one before it passes.

| Level | What | Needs Paddle? | Needs Docker? | Needs Mattermost? | Who runs it |
| --- | --- | --- | --- | --- | --- |
| L0 | Static and import checks | no | no | no | Claude / CI, every change |
| L1 | Unit tests (bot logic, fake bridge) | no | no | no | Claude / CI, every change |
| L2 | Extractor parity on real photos | yes | no | no | Claude locally, when the extractor changes |
| L3 | Container build and runtime | yes (in image) | yes | no | Claude locally, when deploy files change |
| L4 | Live smoke test | yes | yes | yes | Human, once before rollout |

---

## Environments

```sh
# Bot-only venv: fast, no Paddle. Used for L0 and L1.
python3 -m venv .venv
.venv/bin/pip install -r requirements-dev.txt     # requests, openpyxl, numpy (no Paddle, no OpenCV)

# Full venv: used for L2. Same pinned versions as the Docker image.
python3 -m venv .venv-ocr
.venv-ocr/bin/pip install -r requirements.txt
```

`requirements.txt` is the full runtime set (R-OCR-3). `requirements-dev.txt` is the subset needed to import and test the bot without OCR. The point is that L1 never depends on Paddle.

---

## L0 — Static and import checks

```sh
.venv/bin/python -m compileall -q pixel2num tests
.venv/bin/python -c "import pixel2num.bot, pixel2num.bridge"
```

Pass criteria:
- Everything compiles.
- **R-LIB-1:** importing the bot and the adapter in the bot-only venv works without Paddle installed. This proves the OCR import is lazy.
- `grep -rn "print(" pixel2num/` finds matches only inside the CLI `main()` in `extract_numbers.py` (R-LIB-5).

## L1 — Unit tests

```sh
.venv/bin/python -m unittest discover -s tests -v
```

Rules:
- Use only the `unittest` standard library. The existing tests already do.
- Bot tests use the existing `FakeMattermost` and a `Mock` bridge that records the `(display_name, start, count)` it gets and returns a small valid workbook.
- Pure library functions (`parse_name`, `mark_repeated`, `write_excel` to a stream, range parsing) are tested directly. They don't need OCR.
- All existing tests in `tests/test_bot.py` must still pass, except `TransportTests.test_multipart_contract_and_auth_separation`. That test covers the removed HTTP backend and is deleted together with it (R-JOB-1).

Required new cases (names are suggestions):

| Test | Covers |
| --- | --- |
| `test_range_from_filename` | R-RANGE-1 |
| `test_invalid_filename_rejects_whole_message` (batch unchanged, no job started, reply says to rename to `START-END`) | R-RANGE-2, R-INTAKE-2 |
| `test_all_invalid_files_listed_in_reply` (valid + 2 invalid files in one message: nothing added, both invalid names listed) | R-RANGE-3 |
| `test_caption_never_supplies_range` (`IMG.jpg` + caption `81-100` is rejected) | R-RANGE-1, R-RANGE-2 |
| `test_range_bounds_end_before_start_and_too_many_cards` | R-RANGE-4 |
| `test_range_persisted_across_restart` | R-RANGE-5 |
| `test_collect_reply_lists_ranges` | R-RANGE-6 |
| `test_caption_is_not_a_command` (image + caption `process` does not start a job) | R-RANGE-7 |
| `test_overlapping_ranges_warn_but_are_allowed` | R-RANGE-8 |
| `test_bmp_accepted_and_default_limit` | R-INTAKE-1, R-INTAKE-3 |
| `test_default_max_images_is_50` | R-INTAKE-3 |
| `test_bridge_receives_batch_order_and_ranges` | R-JOB-1, R-JOB-2 |
| `test_duplicate_filenames_do_not_collide` (two `image.png` give two distinct temp files) | R-JOB-3 |
| `test_temp_dir_removed_on_success_and_failure` | R-JOB-3, R-SEC-2 |
| `test_ready_message_has_counts_not_codes` | R-JOB-6, R-SEC-1 |
| `test_bridge_exception_fails_job_then_retry_reruns` | R-JOB-7 |
| `test_invalid_workbook_from_bridge_rejected` | R-JOB-8 |
| `test_no_codes_in_logs` (run a job with `assertLogs` and check that no fake code string appears) | R-SEC-1, R-LIB-5 |

## L2 — Extractor parity on real photos

Goal: **R-LIB-7**. The refactor must not change what the extractor reads.

Fixtures (never committed, per R-SEC-3):
```
tests/fixtures/cards/          # git-ignored; or set PIXEL2NUM_FIXTURES=/path/outside/repo
    81-100.jpg ...             # 5–10 real photos named START-END, covering right/left/middle layouts + one hard case
    baseline.json              # records produced by the ORIGINAL script (step 0), committed nowhere
```

Procedure:
1. **Step 0, before any refactor:** run the original script from the baseline commit on the fixture folder. Save its records (image, counter, code, status) to `tests/fixtures/baseline.json`. A copy of the original script is kept in `tests/fixtures/extract_numbers_ORIGINAL.py` (git-ignored) for regenerating it. The helper that runs it is throwaway and never shipped.
2. After every extractor change, run:
   ```sh
   PIXEL2NUM_FIXTURES=tests/fixtures/cards .venv-ocr/bin/python -m unittest tests.test_extract_parity -v
   ```
   The test runs the **library entry point** (R-LIB-3) on the same images and ranges and asserts the records equal `baseline.json` exactly.
3. It also runs the **CLI** (R-LIB-6) into a temp `.xlsx`, reads it back with openpyxl, and asserts the same rows. It also checks that column C is a text cell (`number_format == "@"`).
4. If the fixtures are missing, the test **skips** with a clear message. It never fails, so L1 stays runnable everywhere.

If a later spec deliberately changes extraction results, the baseline is regenerated in that spec's own commit, and the reason is stated.

## L3 — Container

```sh
docker compose build
# R-OCR-3: Paddle imports and the engine loads, with NO network (proves R-OCR-2):
docker run --rm --network none --read-only --tmpfs /tmp pixel2num-bot \
  python -c "from pixel2num.bridge import Bridge; Bridge.load_default(); print('ok')"
# Parity inside the image, with fixtures mounted read-only:
docker run --rm --network none --read-only --tmpfs /tmp \
  -v "$PWD/tests/fixtures/cards:/fixtures:ro" -e PIXEL2NUM_FIXTURES=/fixtures \
  -v "$PWD/tests:/app/tests:ro" pixel2num-bot \
  python -m unittest tests.test_extract_parity -v
docker image ls pixel2num-bot      # record image size
```

The command names above (`Bridge.load_default`, the image tag) are placeholders. Step 4 fixes the real ones and updates this file.

Pass criteria:
- The image builds from a clean cache.
- The OCR engine loads with `--network none`, a read-only root filesystem, and UID 10001.
- Parity passes inside the image, which proves the Docker and venv versions agree.
- **Measure and record** in the table below: image size, idle RSS after `OCR engine ready`, and peak RSS and wall time for one 20-image job. Use these to set the Compose `mem_limit` and `stop_grace_period` (R-DEPLOY-1).

| Measurement | Value | Date |
| --- | --- | --- |
| Image size | _tbd_ | |
| Idle RSS | _tbd_ | |
| Peak RSS, 20 images | _tbd_ | |
| Wall time, 20 images | _tbd_ | |

## L4 — Live smoke test (human)

On the real Mattermost server, with a test account and the bot running from the Compose file:

- [ ] `help` replies. The logs show `Bot connected` and then `OCR engine ready`.
- [ ] Send 2 photos named `81-100.jpg` and `101-120.jpg` in one message. The reply lists both ranges.
- [ ] Send 1 phone photo (`IMG_….jpg`), even with caption `121-140`. The message is rejected, the reply tells the user to rename the file to `START-END`, and `status` still shows 2 images.
- [ ] Send a message with one valid and one invalid file name. Nothing is added and the invalid file is named in the reply.
- [ ] Send a `.bmp`. It is accepted.
- [ ] `process` replies with the job ID and then the `.xlsx` with succeeded/failed/repeated counts. The workbook rows match the photos, and the Image column shows the file names.
- [ ] While a job runs, a second user sends `status`. They get an answer once the job finishes.
- [ ] Restart the container with images pending. `status` still shows them.
- [ ] `docker compose logs bot` contains **no charge codes**.

Sign-off: name, date, and image tag go here.

---

## Traceability (kept current; every SPEC ID must appear)

| Requirement | Level / test |
| --- | --- |
| R-RANGE-1…7 | L1 range tests; L4 |
| R-INTAKE-1…3 | L1 |
| R-LIB-1 | L0 import check |
| R-LIB-2…5 | L1 (library unit tests), L0 print grep |
| R-LIB-6, R-LIB-7 | L2 parity (library + CLI), L3 parity in image |
| R-JOB-1…8 | L1; L4 (R-JOB-2 counter sort: `test_records_are_sorted_by_counter` in the L2 file) |
| R-OCR-1 | L1 (startup order with fake) + L4 log check |
| R-OCR-2, R-OCR-3 | L3 |
| R-CFG-1…3 | Review (no automated test; the config test was removed on purpose) |
| R-SEC-1 | L1 log test; L4 log check |
| R-SEC-2 | L1 temp-dir test |
| R-SEC-3 | `.gitignore` entry + review |
| R-SEC-4, R-DEPLOY-1 | L3 (run flags match Compose) |
| R-DEPLOY-2 | Review |
