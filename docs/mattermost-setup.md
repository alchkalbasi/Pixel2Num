# Mattermost setup and Docker deployment

## Requirements

- Mattermost API v4 with `GET /users/me/channels` (introduced in server 6.1); use a currently supported server release.
- A Mattermost System Admin, Docker Engine with Compose v2, and a backend implementing [the contract](backend-contract.md).
- Outbound connectivity from the container to both services. No inbound bot port, webhook, plugin, or slash-command registration is needed.

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

Edit `.env` with your values:

```dotenv
MATTERMOST_URL=https://chat.example.com
MATTERMOST_TOKEN=your-bot-access-token
BACKEND_URL=https://extraction.example.com/v1/reports
BACKEND_TOKEN=your-backend-service-token
FORMAT_ID=default
```

Use the Mattermost site URL including any deployment subpath, without `/api/v4`. Use the full backend conversion endpoint. Neither URL may contain embedded credentials, query parameters, or fragments. TLS verification is enabled; for private CAs, mount a CA bundle and set `REQUESTS_CA_BUNDLE` to its container path.

The example `http://backend:8000/v1/reports` requires a backend service on the same Docker network. **The supplied Compose file runs only the bot.** Use a reachable DNS name/IP for an external backend. Container `localhost` refers to the bot container. For a Linux host service, add `extra_hosts: ["host.docker.internal:host-gateway"]` to the bot service and use `http://host.docker.internal:8000/v1/reports`; the service must bind to an interface accessible from Docker and permit that traffic.

| Variable | Default | Purpose |
| --- | --- | --- |
| `BACKEND_TOKEN` | empty | Optional backend bearer token |
| `FORMAT_ID` | `default` | Agreed backend template |
| `ALLOWED_USER_IDS` | empty | Comma-separated permitted Mattermost user IDs; empty permits anyone able to DM the bot |
| `POLL_SECONDS` | `5` | Delay between cycles |
| `BACKEND_TIMEOUT` | `180` | Backend socket read timeout, seconds |
| `MAX_IMAGES` | `20` | Images per batch |
| `MAX_IMAGE_BYTES` | `10485760` | 10 MiB per image |
| `MAX_BATCH_BYTES` | `52428800` | 50 MiB per batch |
| `MAX_REPORT_BYTES` | `20971520` | 20 MiB per workbook |
| `DATA_DIR` | `/app/data` in Docker | Keep this path with the supplied Compose volume |

```sh
docker compose up -d --build
docker compose logs -f bot
docker compose ps
```

Look for `Bot connected; polling direct messages`. This confirms initial authentication, not successful extraction. There is no HTTP health endpoint; monitor logs and test delivery.

The container runs as UID/GID `10001`, with read-only root filesystem and writable named state volume. Any replacement bind mount must be writable by that UID. `.env` is excluded from Git and Docker builds.

## Live smoke test

1. DM `help` to `pixel2num`.
2. Send two images in one message; confirm “Collected 2 image(s).” Send a third in another message and confirm the count becomes 3.
3. Send `status`, then `process` as a separate plain message. Confirm a job ID appears.
4. Check that the backend receives three `images` parts, the job ID, and the agreed template. Confirm a downloadable `.xlsx` arrives in the same DM.
5. Open the workbook and compare columns and values with the images.
6. Test a second user's DM to verify isolation. Send an unsupported file and verify rejection. Test `clear` on a pending batch.
7. In a test environment, cause a backend failure, restore it, and send `retry`. Restart the bot with pending images and confirm `status` retains them.

## Operations and troubleshooting

- Authentication failure: verify the token and enabled bot account. Startup errors cause Compose to restart the container.
- Upload/post failure: inspect channel membership, file restrictions, and create-post/upload permissions.
- No response: confirm this is a DM, the sender is allowed, networking works, and extraction is not occupying the worker. Inspect cycle errors in logs.
- Failed job: check backend logs using the displayed job ID, endpoint, auth, content type, XLSX structure, and size limits. Send `retry` after correcting it.
- Connection/TLS errors: check DNS, container routing, proxies, and trusted CA certificates. The bot intentionally omits service response bodies and credentials from logs.
- Slow extraction: increase `BACKEND_TIMEOUT` and Compose `stop_grace_period` as appropriate. For sustained concurrency, extend the design to separate workers before adding replicas.

After changing `.env`, run `docker compose up -d --force-recreate bot`. Stop with `docker compose down`; state remains in the named volume. **`docker compose down -v` deletes state**, including pending batches and retry data. Back up the volume while stopped or use SQLite's online backup API. Restore with the same Mattermost server and bot identity; a different account needs a fresh volume.

File delivery uses the [file upload endpoint](https://docs.mattermost.com/api/reference/upload-file), followed by [creating a post with file IDs](https://docs.mattermost.com/api/reference/create-post). Incoming webhooks cannot upload a native workbook attachment. Polling follows the official [channel](https://github.com/mattermost/mattermost/blob/master/api/v4/source/channels.yaml) and [post](https://github.com/mattermost/mattermost/blob/master/api/v4/source/posts.yaml) API definitions.
