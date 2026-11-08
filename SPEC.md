# SPEC 001 — Run the extractor inside the bot (Option A)

Status: **IN PROGRESS** (approved 2026-11-03; implementation on branch `develop`)
Owner: kian-abdipour
Last updated: 2026-11-03

This file is the source of truth for this change. Code and tests follow the spec. If they disagree, fix the spec first and then the code. Each requirement has an ID (for example `R-RANGE-2`). Tests and commits cite those IDs.

---

## 1. Problem

The operations team copies recharge codes from photos of top-up cards into Excel by hand. Two pieces already exist:

- **The extractor**, `pixel2num/extract_numbers.py`. It is a command-line script that takes a folder of `START-END.jpg` photos and writes an `.xlsx` file. It uses PaddleOCR, OpenCV and openpyxl. It works well, apart from a few small bugs.
- **The bot**, in `pixel2num/bot.py`, `pixel2num/api.py` (now `bot_toolbox.py`) and `pixel2num/config.py`. It is a Mattermost DM bot that collects images and sends them over HTTP to `BACKEND_URL`. No backend exists.

Nothing connects the two.

## 2. Goal

Operators DM the bot their card photos, type `process`, and get the extractor's Excel report back in the same DM. Everything runs as **one program in one container**, with no HTTP API between the bot and the extractor.

### Users and scale
- About 10 internal users on one team. The program is not published.
- Jobs run one at a time. The bot does not answer while a job runs, and it catches up afterwards. This is acceptable at this scale (see §8).

### Non-goals (out of scope for SPEC 001)
- Improving OCR accuracy or fixing the extractor's known small bugs. That work gets its own spec later. This spec requires **no change in extraction results**.
- An HTTP API, async workers, a GPU, or more than one replica.
- Zip or folder upload.
- Reading the handwritten Persian numbers on the cards.

---

## 3. Definitions

| Term | Meaning |
| --- | --- |
| **Range** | `START-END` (integers, `START ≤ END`). It gives the counter of the top card and the bottom card in one photo. `count = END − START + 1`. |
| **Image entry** | One image in a user's pending batch: Mattermost file ID, original name, MIME type, size, and range. |
| **Extractor** | The library form of `extract_numbers.py` (§5). |
| **Report** | The `.xlsx` workbook made by the extractor's `write_excel`. |

---

## 4. Requirements

### 4.1 How users give the range — `R-RANGE`

- **R-RANGE-1 (filename is the only source).** Every image's file stem must match the extractor's `NAME_RE` (`^\s*(\d+)\s*-\s*(\d+)\s*$`), for example `81-100.jpg`: `START-END`, integers. That is the range. Captions and message text are never used to find a range.
- **R-RANGE-2 (invalid name stops the process).** If any image in a message does not have a valid `START-END` filename, the whole message is rejected and **nothing from it is added to the batch**. The bot replies that the file name must be changed and names the offending file(s), for example: `"IMG_1234.jpg" is not a valid file name. Rename it to START-END (for example 81-100.jpg), where START and END are the first and last card counters, and send it again. Nothing from this message was added.` Images already in the batch are untouched, and no job is started.
- **R-RANGE-3 (valid files in the same message).** Rejection is all-or-nothing per message (consistent with R-INTAKE-2). The reply lists every invalid file in that message, not just the first.
- **R-RANGE-4 (sanity bounds).** A range is also invalid, and rejected the same way, if `END < START` or if `count > MAX_CARDS_PER_IMAGE` (new setting, default `40`). This catches typos like `81-1000`. The reply says which bound failed.
- **R-RANGE-5 (stored with the image).** The parsed range is saved in the image entry in SQLite when the image is collected. It is not worked out again at processing time.
- **R-RANGE-6 (acknowledgement).** The "Collected N image(s)" reply also lists the ranges that message added, for example `Collected 3 image(s). Added: 81-100.` This lets the user spot mistakes before typing `process`.
- **R-RANGE-8 (overlapping ranges).** Ranges that overlap inside one batch (for example `81-100` and `95-110`) are allowed. The "Collected" reply warns about them, and duplicate codes are marked `repeated` as usual.
- **R-RANGE-7 (text is never a range, still a command).** An image message's text is ignored, as the README says today. A caption such as `process` on an image message does not start a job.

> README is unchanged on this point: captions stay ignored.

### 4.2 Image intake — `R-INTAKE`

- **R-INTAKE-1.** Accepted types: PNG, JPEG, WebP, TIFF and **BMP**. Adding BMP matches the extractor's `IMAGE_EXTS`.
- **R-INTAKE-2.** The existing rules stay: the whole message is validated before anything is added, size and count limits apply, attachments must belong to the post, messages are deduplicated, and the batch is kept per user and per DM.
- **R-INTAKE-3.** The default `MAX_IMAGES` goes from 20 to **50**. Operators often send more than 20 photos at once.

### 4.3 Extractor as a library — `R-LIB`

- **R-LIB-1 (no import side effects).** Importing the module must not load PaddleOCR. The OCR engine is created lazily by an explicit call (for example `get_ocr()` or `Bridge.load()`), and only once per process.
- **R-LIB-2 (no global mutation).** `CODE_LEN` and `LAYOUT` become parameters with the same defaults (`None`, `"auto"`). The CLI passes its flags through. The library never changes module globals.
- **R-LIB-3 (explicit inputs).** There is a library entry point that takes a list of `(display_name, path, start, count)` in report order and returns the records. **The range comes from the argument, not the filename**, so the bot can use safe temp names (R-JOB-3). The CLI keeps its existing folder → filename → range behaviour by building that list itself.
- **R-LIB-4 (report bytes).** `write_excel` can write to a binary stream, so the bot gets `bytes` without a temp `.xlsx` file. The CLI still writes to a path.
- **R-LIB-5 (no stdout in library calls).** Progress and warnings go through `logging` (logger `pixel2num.extract`), not `print`. The CLI turns this into the same console output as before. Log lines **must not contain charge codes** (see R-SEC-1). The CLI's "REPEATED codes" console summary is the only exception, because it runs only in the operator's own terminal.
- **R-LIB-6 (CLI keeps working).** `python -m pixel2num.extract_numbers <folder> <out.xlsx> [--debug] [--layout=…] [--len=…]` behaves as it does today.
- **R-LIB-7 (same results).** For the same images and ranges, the refactored library returns the same records as today's script: image, counter, code and status, in the same order. This is checked by the harness parity test (HARNESS §L2).

### 4.4 Job processing in the bot — `R-JOB`

- **R-JOB-1 (in-process call).** The bot's `Backend` HTTP client is replaced by a local `Bridge` object (`pixel2num/bridge.py`) with the same shape: `convert(job, images) -> bytes`. The job state machine in `Bot.work()` (`queued → processing → ready → uploaded → done/failed`) stays as it is.
- **R-JOB-2 (order).** Images are passed to the extractor in batch order (the order the messages were received, then attachment order within a message). The report rows are then **sorted by the Counter column, from smallest to biggest**. Rows with the same counter (overlapping ranges) keep batch order. A row without a counter (bad file name, CLI only) goes last. The sort happens after reading, so extraction results do not change. *(Changed after SPEC 001 rollout: previously rows followed batch order.)*
- **R-JOB-3 (temp files).** Each job writes its images to its own temp directory under `/tmp` (tmpfs in Compose), named `NNN.<ext>` by position. Original filenames are never used as paths, so duplicate names can't overwrite each other. The **Image** column in the report shows the original filename (for example `81-100.jpg`). The directory is deleted when the job finishes, whether it succeeds or fails.
- **R-JOB-4 (no debug output).** Debug drawing is always off in bot mode.
- **R-JOB-5 (cross-image duplicates).** `mark_repeated` runs over the whole job, as the CLI does, so a code repeated anywhere in the batch is marked `repeated`.
- **R-JOB-6 (job summary).** The "report is ready" message also gives the counts: `N succeeded, N failed, N repeated`. It never contains codes.
- **R-JOB-7 (failure).** Any exception from the extractor marks the job `failed`, logs only the job ID and exception type, and tells the user to type `retry`. Retrying the same job runs the extraction again from the saved file IDs.
- **R-JOB-8 (report check kept).** The bot still runs `validate_report` on the bytes before uploading.

### 4.5 OCR lifecycle — `R-OCR`

- **R-OCR-1 (warm at startup).** On start, after Mattermost authentication succeeds, the bot loads PaddleOCR once and logs `OCR engine ready`. Polling starts only after that.
- **R-OCR-2 (offline models).** OCR models are **built into the Docker image** at build time. At runtime the container needs no internet access for models and writes nothing outside `/app/data` and `/tmp`.
- **R-OCR-3 (pinned versions).** `paddleocr`, `paddlepaddle`, `opencv-python-headless`, `openpyxl` and `numpy` are pinned to exact versions in `requirements.txt`. The Python base image version is one with official `paddlepaddle` wheels for linux/amd64. The harness checks this (HARNESS §L3).

### 4.6 Configuration — `R-CFG`

- **R-CFG-1.** Removed: `BACKEND_URL`, `BACKEND_TOKEN`, `FORMAT_ID`, `BACKEND_TIMEOUT`. They are no longer required, and startup ignores them if they are still set in an old `.env`.
- **R-CFG-2.** Added: `MAX_CARDS_PER_IMAGE` (default `40`), plus optional `CODE_LEN` (empty means auto) and `LAYOUT` (default `auto`, or one of `right|left|middle`). These are passed to the extractor (R-LIB-2). Invalid values fail at startup.
- **R-CFG-3.** `MAX_REPORT_BYTES` stays and applies to the generated workbook.

### 4.7 Security and data handling — `R-SEC`

Charge codes are worth money. Treat them like credentials.

- **R-SEC-1.** Codes never appear in logs, error messages or chat text. They only appear in the delivered `.xlsx`.
- **R-SEC-2.** Temp images and reports are deleted after each job (R-JOB-3). The report blob in SQLite is cleared after delivery, as it is today.
- **R-SEC-3.** No real card photos or real codes are committed to Git. Test fixtures with real cards live outside the repo, or in a git-ignored folder (HARNESS §L2).
- **R-SEC-4.** The container hardening stays as it is: read-only root filesystem, non-root UID 10001, `cap_drop: ALL`, no inbound ports.

### 4.8 Deployment — `R-DEPLOY`

- **R-DEPLOY-1.** `docker compose up -d --build` starts one container that does everything. Compose gets a memory limit (start at `4g` and adjust after the L3 measurement) and keeps `stop_grace_period` longer than the longest expected job.
- **R-DEPLOY-2.** `README.md` and `docs/mattermost-setup.md` are updated for the single-container design and the range rules. `docs/backend-contract.md` is deleted. Git history keeps it.

---

## 5. Design sketch (non-binding)

```
Mattermost DM ──poll──▶ Bot.handle()  ── resolve range (R-RANGE) ──▶ SQLite batch
                         Bot.work()   ── download ▶ /tmp/job-<id>/NNN.ext
                                       ── Bridge.convert() ── extract lib ── xlsx bytes
                                       ── validate_report ▶ upload ▶ post summary
```

- `pixel2num/extract_numbers.py` becomes the library and keeps its CLI `main()`. It is not split into several modules in this spec, to keep the diff reviewable.
- `pixel2num/bot_toolbox.py` (renamed from `api.py`, since it holds no API of its own): the `Backend` class is removed. The `Mattermost` client, `validate_report`, `bounded_content` and `InputError` stay.
- New `pixel2num/bridge.py`: a thin `Bridge` adapter (load once; `convert(job, images)` → temp dir → library → bytes).
- `Bot.__init__` already accepts an injected `backend`. Rename it to `bridge` so tests can inject a fake and don't need Paddle.

## 6. Delivery plan (each step is one reviewable commit)

0. Commit today's `extract_numbers.py` unchanged as the baseline, and record parity fixtures (HARNESS §L2).
1. Refactor the extractor into a library (R-LIB-*). The CLI and parity tests must pass.
2. Range resolution and intake changes (R-RANGE-*, R-INTAKE-*), with unit tests.
3. `Bridge` adapter, then switch the bot over from `Backend` (R-JOB-*, R-OCR-1, R-CFG-*), with unit tests using a fake bridge.
4. Docker, requirements and Compose (R-OCR-2/3, R-DEPLOY-1), plus the L3 checks.
5. Docs (R-DEPLOY-2), then the live smoke test (HARNESS §L4).

## 7. Acceptance

SPEC 001 is done when every requirement ID is covered in the HARNESS traceability table, all automated levels (L0–L3) pass, and the L4 live smoke checklist has been signed off once on the real Mattermost server.

## 8. Known limits (accepted)

- While a job runs, the bot does not poll or reply. With 50 images this could take minutes. Users are told this with the `Queued` reply ("Processing may take a few minutes; I'll reply when done.").
- A job can't be cancelled once it has started.
- Delivery is still at-least-once, as the README describes.

## 9. Decisions on the former open questions

1. **Overlapping ranges:** allowed, with a warning (R-RANGE-8).
2. **Row order:** sorted by Counter, smallest first (R-JOB-2; originally the order the images were sent).
3. **Parity photos:** the three sample photos (one per card type) live in git-ignored `tests/fixtures/cards/`. They are renamed `1-20`, `21-40`, `41-51` for the test only. Add a harder case when one is available.

## 10. Implementation notes

- `pixel2num/ranges.py` holds the `START-END` parser. The bot and the extractor share it, and it needs no OpenCV.
- The summary counts in the "report ready" message are read back from the workbook's Status column (`bot_toolbox.summarize_report`), so no extra database column is needed.
- A job queued before this change (no stored range) falls back to parsing the file name when it runs.
