# Backend integration contract

The backend owns OCR, numerical extraction, and Excel generation. The bot owns Mattermost, batching, transport, and delivery. Agree on columns, units, decimal separators, missing values, and confidence rules for each `FORMAT_ID`; the bot assumes no domain-specific schema.

## Request

`POST` to the full `BACKEND_URL`, for example `http://backend:8000/v1/reports`.

```http
Authorization: Bearer <BACKEND_TOKEN>
Idempotency-Key: <job_id>
Content-Type: multipart/form-data; boundary=<generated-by-client>
```

Authorization is omitted when `BACKEND_TOKEN` is empty. It is separate from the Mattermost credential. Use HTTPS across untrusted networks.

| Multipart field | Content |
| --- | --- |
| `images` (repeated) | One binary image per part, with filename and MIME type |
| `metadata` | JSON encoded as a text field |

```json
{
  "job_id": "mattermost-process-message-id",
  "user_id": "mattermost-user-id",
  "channel_id": "mattermost-dm-channel-id",
  "format_id": "default"
}
```

The job ID is the post ID of the `process` command. Files retain message/attachment order; equal message timestamps use post ID order. Filenames may repeat: identify images by position or generated ID. Treat filenames, metadata, and image contents as untrusted input. Never use supplied filenames directly as filesystem paths.

## Success

Return the complete workbook synchronously:

```http
HTTP/1.1 200 OK
Content-Type: application/vnd.openxmlformats-officedocument.spreadsheetml.sheet
Content-Disposition: attachment; filename="report.xlsx"

<binary .xlsx bytes>
```

The bot chooses `pixel2num-<job_id>.xlsx` as the delivered filename. JSON, download URLs, redirects, `202 Accepted`, and callback/webhook responses are not supported.

Defaults: 180-second socket read timeout and 20 MiB maximum report. The timeout measures socket inactivity, not total elapsed job time. The bot checks required XLSX ZIP entries and rejects macro-bearing archives; it does not validate cell semantics. Generate numeric cells as numbers and untrusted OCR text as strings, avoiding unintended formulas or external links.

## Failure and retry

Use non-200 responses for failures: for example `400` invalid request, `401`/`403` authentication, `413` size limit, `422` unreadable image/unsupported format, `429` capacity, or `500`/`503` server failure. The bot does not expose response bodies to users or logs; keep backend logs keyed by job ID.

Users retry by typing `retry`. Interrupted processing can also be resubmitted after bot restart. **Implement persistent idempotency using `Idempotency-Key`:** repeated requests for the same job must reuse its result rather than create separate billable or side-effecting jobs. Retain results for the supported retry window, and reject conflicting input under the same key. Keep `FORMAT_ID` stable while jobs are outstanding.

## Standalone contract test

```sh
curl --fail-with-body --request POST "$BACKEND_URL" \
  --header "Authorization: Bearer $BACKEND_TOKEN" \
  --header 'Idempotency-Key: local-contract-test-001' \
  --form 'metadata={"job_id":"local-contract-test-001","user_id":"test-user","channel_id":"test-channel","format_id":"default"}' \
  --form 'images=@sample-1.png;type=image/png' \
  --form 'images=@sample-2.jpg;type=image/jpeg' \
  --output report.xlsx
```

Omit Authorization when unused. Check the response content type and open the workbook to verify columns and numbers. Repeat the same request/key to confirm idempotency.
