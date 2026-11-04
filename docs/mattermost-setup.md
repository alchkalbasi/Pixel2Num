# Mattermost setup and Docker deployment

## Requirements

- Mattermost API v4 with `GET /users/me/channels` (introduced in server 6.1); use a currently supported server release.
- A Mattermost System Admin and Docker Engine with Compose v2. No separate backend service is needed: the extractor is part of the bot.
- A server with enough memory for PaddleOCR (the Compose file limits the container to 4 GB) and a few GB of disk for the image.
- Outbound connectivity from the container to Mattermost. No inbound bot port, webhook, plugin, or slash-command registration is needed.

## Create the account

1. As a System Admin, open **System Console → Integrations → Bot Accounts** and enable **Enable Bot Account Creation**.
2. From the product menu, open **Integrations → Bot Accounts → Add Bot Account**.
3. Set username `pixel2num`, display name `Pixel2Num`, and role **Member**. System Admin and permission to post to all channels are unnecessary.
4. Create the account and securely copy the access token from the success screen before closing it.
5. If needed for discovery, invite the bot to the relevant team through **Invite People → Invite Member** using its username. Users open a DM with the bot; this implementation ignores channels and group messages.

Bot accounts need channel membership to upload files; a DM with the bot supplies it. Ensure your permission scheme permits reading the conversation, creating posts, and uploading files. Server file limits must accommodate images and workbooks.

Official references: [bot accounts](https://docs.mattermost.com/developers/integrate/reference/bot-accounts), [integration settings](https://docs.mattermost.com/administration-guide/configure/integrations-configuration-settings).

## Configure and start

From the repository root:

```sh
cp .env.example .env
chmod 600 .env
```

Edit `.env` with your values (only these two are required):

```dotenv
MATTERMOST_URL=https://chat.example.com
MATTERMOST_TOKEN=your-bot-access-token
```

Use the Mattermost site URL including any deployment subpath, without `/api/v4`. It may not contain embedded credentials, query parameters, or fragments. TLS verification is enabled; for private CAs, mount a CA bundle and set `REQUESTS_CA_BUNDLE` to its container path.

| Variable | Default | Purpose |
| --- | --- | --- |
| `ALLOWED_USER_IDS` | empty | Comma-separated permitted Mattermost user IDs; empty permits anyone able to DM the bot |
| `POLL_SECONDS` | `5` | Delay between cycles |
| `MAX_IMAGES` | `50` | Images per batch |
| `MAX_CARDS_PER_IMAGE` | `40` | Largest range (`END - START + 1`) accepted in one file name |
| `MAX_IMAGE_BYTES` | `10485760` | 10 MiB per image |
| `MAX_BATCH_BYTES` | `52428800` | 50 MiB per batch |
| `MAX_REPORT_BYTES` | `20971520` | 20 MiB per workbook |
| `CODE_LEN` | empty | Force the code length (for example `15`); empty detects it for each image |
| `LAYOUT` | `auto` | Card layout: `auto`, `right`, `left` or `middle` |
| `DATA_DIR` | `/app/data` in Docker | Keep this path with the supplied Compose volume |

The old `BACKEND_URL`, `BACKEND_TOKEN`, `FORMAT_ID` and `BACKEND_TIMEOUT` settings were removed. They are ignored if left in an old `.env`.

```sh
docker compose up -d --build
docker compose logs -f bot
docker compose ps
```

Look for `Bot connected; loading OCR engine`, then `OCR engine ready; polling direct messages`. The first line confirms Mattermost authentication, the second that the OCR engine is loaded (the first start can take a minute). Neither proves extraction accuracy. There is no HTTP health endpoint; monitor logs and test delivery.

The container runs as UID/GID `10001`, with read-only root filesystem, a writable named state volume and a tmpfs `/tmp` for per-job temporary images. The OCR models are inside the image. Any replacement bind mount must be writable by that UID. `.env` is excluded from Git and Docker builds.

## Live smoke test

1. DM `help` to `pixel2num`.
2. Send two images named like `81-100.jpg` and `101-120.jpg` in one message; confirm the reply lists both ranges. Send a third in another message and confirm the count becomes 3.
3. Send a phone photo with its original name (`IMG_….jpg`). Confirm the whole message is rejected, the reply names the file and explains the `START-END` rule, and `status` still shows 3 pending images.
4. Send `status`, then `process` as a separate plain message. Confirm a job ID appears and later a downloadable `.xlsx` with succeeded/failed/repeated counts arrives in the same DM.
5. Open the workbook and compare codes and counters with the photos.
6. Test a second user's DM to verify isolation. Send an unsupported file and verify rejection. Test `clear` on a pending batch.
7. Cause a failure in a test environment (for example stop the container mid-job), restart, and send `retry` if needed. Restart the bot with pending images and confirm `status` retains them.
8. Check `docker compose logs bot` contains no charge codes.

## Operations and troubleshooting

- Authentication failure: verify the token and enabled bot account. Startup errors cause Compose to restart the container.
- Upload/post failure: inspect channel membership, file restrictions, and create-post/upload permissions.
- No response: confirm this is a DM, the sender is allowed, networking works, and extraction is not occupying the worker. Inspect cycle errors in logs.
- Failed job: check `docker compose logs bot` using the displayed job ID (logs show only the job ID and error type). Send `retry` after correcting the cause.
- Connection/TLS errors: check DNS, container routing, proxies, and trusted CA certificates. The bot intentionally omits service response bodies and credentials from logs.
- Slow extraction or out-of-memory restarts: raise the Compose `mem_limit` and `stop_grace_period`. The bot does not answer while a job runs. For sustained concurrency, move the extractor to separate workers before adding replicas.
- Files rejected as badly named: rename them to `START-END` (for example `81-100.jpg`).

After changing `.env`, run `docker compose up -d --force-recreate bot`. Stop with `docker compose down`; state remains in the named volume. **`docker compose down -v` deletes state**, including pending batches and retry data. Back up the volume while stopped or use SQLite's online backup API. Restore with the same Mattermost server and bot identity; a different account needs a fresh volume.

File delivery uses the [file upload endpoint](https://docs.mattermost.com/api/reference/upload-file), followed by [creating a post with file IDs](https://docs.mattermost.com/api/reference/create-post). Incoming webhooks cannot upload a native workbook attachment. Polling follows the official [channel](https://github.com/mattermost/mattermost/blob/master/api/v4/source/channels.yaml) and [post](https://github.com/mattermost/mattermost/blob/master/api/v4/source/posts.yaml) API definitions.
