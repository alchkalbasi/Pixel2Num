import json
import logging
import sqlite3
import time
from pathlib import Path

import requests

from .api import IMAGE_TYPES, Backend, InputError, Mattermost

LOG = logging.getLogger(__name__)
HELP = "Send PNG, JPEG, WebP or TIFF images in this direct message, then type `process`. Commands: `status`, `clear` (pending images only), `retry` (latest failed job), `help`. Send commands as plain messages, without a slash."


class Bot:
    def __init__(self, config, mattermost=None, backend=None):
        self.config = config
        self.mm = mattermost or Mattermost(config)
        self.backend = backend or Backend(config)
        Path(config.data_dir).mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(Path(config.data_dir) / "state.sqlite3")
        self.db.row_factory = sqlite3.Row
        self.db.executescript("""
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value INTEGER);
            CREATE TABLE IF NOT EXISTS cursors (channel_id TEXT PRIMARY KEY, value INTEGER);
            CREATE TABLE IF NOT EXISTS inbox (id TEXT PRIMARY KEY, payload TEXT, created INTEGER, done INTEGER DEFAULT 0);
            CREATE TABLE IF NOT EXISTS batches (channel_id TEXT, user_id TEXT, files TEXT, PRIMARY KEY(channel_id,user_id));
            CREATE TABLE IF NOT EXISTS jobs (id TEXT PRIMARY KEY, channel_id TEXT, user_id TEXT, files TEXT, state TEXT, result BLOB, uploaded_id TEXT, created INTEGER);
            CREATE TABLE IF NOT EXISTS outbox (id INTEGER PRIMARY KEY, channel_id TEXT, message TEXT);
        """)
        with self.db:
            self.db.execute("INSERT OR IGNORE INTO settings VALUES ('started', ?)", (int(time.time() * 1000),))
            self.db.execute("UPDATE jobs SET state='queued' WHERE state='processing'")
        self.started = self.db.execute("SELECT value FROM settings WHERE key='started'").fetchone()[0]
        self.user_id = self.mm.get("/users/me")["id"]

    def say(self, channel_id, message):
        self.db.execute("INSERT INTO outbox(channel_id,message) VALUES (?,?)", (channel_id, message))

    def poll(self):
        for channel in self.mm.get("/users/me/channels"):
            if channel["type"] != "D" or channel.get("delete_at", 0):
                continue
            cid = channel["id"]
            cursor = self.db.execute("SELECT value FROM cursors WHERE channel_id=?", (cid,)).fetchone()
            cutoff = (cursor[0] if cursor else self.started) - 1000
            posts = {}
            params = {"per_page": 100}
            while True:
                page = self.mm.get(f"/channels/{cid}/posts", params=params)
                ordered = [page["posts"][pid] for pid in page["order"]]
                for post in ordered:
                    if post["create_at"] >= cutoff:
                        posts[post["id"]] = post
                if not ordered or min(p["create_at"] for p in ordered) < cutoff:
                    break
                before = ordered[-1]["id"]
                if params.get("before") == before:
                    raise RuntimeError("Mattermost pagination did not advance")
                params = {"per_page": 100, "before": before}
            with self.db:
                for post in posts.values():
                    if post["channel_id"] != cid or post["user_id"] == self.user_id or post.get("delete_at") or post.get("type"):
                        continue
                    if self.config.allowed_user_ids and post["user_id"] not in self.config.allowed_user_ids:
                        continue
                    self.db.execute("INSERT OR IGNORE INTO inbox(id,payload,created) VALUES (?,?,?)", (post["id"], json.dumps(post), post["create_at"]))
                if posts:
                    self.db.execute("INSERT OR REPLACE INTO cursors VALUES (?,?)", (cid, max(p["create_at"] for p in posts.values())))

    def handle(self, post):
        cid, uid = post["channel_id"], post["user_id"]
        row = self.db.execute("SELECT files FROM batches WHERE channel_id=? AND user_id=?", (cid, uid)).fetchone()
        files = json.loads(row[0]) if row else []
        incoming = post.get("file_ids", [])
        if incoming:
            # Validate the whole message before changing the batch.
            added = []
            for fid in dict.fromkeys(incoming):
                if any(f["id"] == fid for f in files):
                    continue
                info = self.mm.get(f"/files/{fid}/info")
                if info.get("post_id") != post["id"]:
                    raise InputError("An attachment does not belong to this message.")
                if info.get("mime_type") not in IMAGE_TYPES:
                    raise InputError("Only PNG, JPEG, WebP and TIFF images are supported. This message was not added.")
                if not 0 < info["size"] <= self.config.max_image_bytes:
                    raise InputError("An image exceeds the size limit. This message was not added.")
                added.append({"id": fid, "name": Path(info["name"]).name, "mime": info["mime_type"], "size": info["size"]})
            combined = files + added
            if len(combined) > self.config.max_images or sum(f["size"] for f in combined) > self.config.max_batch_bytes:
                raise InputError("Batch limit reached. Type `process` or `clear` before sending more images. This message was not added.")
            self.db.execute("INSERT OR REPLACE INTO batches VALUES (?,?,?)", (cid, uid, json.dumps(combined)))
            self.say(cid, f"Collected {len(combined)} image(s). Type `process` when ready.")
            return
        command = post.get("message", "").strip().lower()
        if command == "clear":
            self.db.execute("DELETE FROM batches WHERE channel_id=? AND user_id=?", (cid, uid))
            self.say(cid, "Pending images cleared. Submitted jobs are unchanged.")
        elif command == "process":
            if not files:
                self.say(cid, "Send images first, then type `process`.")
                return
            self.db.execute("INSERT INTO jobs(id,channel_id,user_id,files,state,created) VALUES (?,?,?,?, 'queued',?)", (post["id"], cid, uid, json.dumps(files), post["create_at"]))
            self.db.execute("DELETE FROM batches WHERE channel_id=? AND user_id=?", (cid, uid))
            self.say(cid, f"Queued {len(files)} image(s). Job: `{post['id']}`.")
        elif command == "retry":
            job = self.db.execute("SELECT * FROM jobs WHERE channel_id=? AND user_id=? AND state='failed' ORDER BY created DESC LIMIT 1", (cid, uid)).fetchone()
            if job:
                state = "uploaded" if job["uploaded_id"] else "ready" if job["result"] else "queued"
                self.db.execute("UPDATE jobs SET state=? WHERE id=?", (state, job["id"]))
                self.say(cid, f"Retrying job `{job['id']}`.")
            else:
                self.say(cid, "There is no failed job to retry.")
        elif command == "status":
            job = self.db.execute("SELECT id,state FROM jobs WHERE channel_id=? AND user_id=? ORDER BY created DESC LIMIT 1", (cid, uid)).fetchone()
            self.say(cid, f"Pending images: {len(files)}." + (f" Latest job: `{job['id']}` — {job['state']}." if job else ""))
        else:
            self.say(cid, HELP)

    def consume(self):
        blocked_channels = set()
        for row in self.db.execute("SELECT * FROM inbox WHERE done=0 ORDER BY created,id").fetchall():
            post = json.loads(row["payload"])
            if post["channel_id"] in blocked_channels:
                continue
            try:
                with self.db:
                    self.handle(post)
                    self.db.execute("UPDATE inbox SET done=1, payload=NULL WHERE id=?", (row["id"],))
            except InputError as error:
                with self.db:
                    self.say(post["channel_id"], str(error))
                    self.db.execute("UPDATE inbox SET done=1, payload=NULL WHERE id=?", (row["id"],))
            except requests.RequestException as error:
                status = error.response.status_code if error.response is not None else None
                if status in (400, 403, 404, 410):
                    with self.db:
                        self.say(post["channel_id"], "An attachment is unavailable or inaccessible. This message was not added; please resend the images.")
                        self.db.execute("UPDATE inbox SET done=1, payload=NULL WHERE id=?", (row["id"],))
                else:
                    # Preserve this conversation's order without blocking other users.
                    blocked_channels.add(post["channel_id"])
                    LOG.warning("Message %s deferred (%s)", row["id"], type(error).__name__)

    def work(self):
        job = self.db.execute("SELECT * FROM jobs WHERE state IN ('queued','ready','uploaded') ORDER BY created,id LIMIT 1").fetchone()
        if not job:
            return
        try:
            state, result, uploaded_id = job["state"], job["result"], job["uploaded_id"]
            if state == "queued":
                with self.db:
                    self.db.execute("UPDATE jobs SET state='processing' WHERE id=?", (job["id"],))
                images, total = [], 0
                for file in json.loads(job["files"]):
                    content = self.mm.download(file["id"], self.config.max_image_bytes)
                    total += len(content)
                    if total > self.config.max_batch_bytes:
                        raise InputError("The batch exceeds the configured size limit.")
                    images.append((file["name"], content, file["mime"]))
                result = self.backend.convert(job, images)
                with self.db:
                    self.db.execute("UPDATE jobs SET state='ready',result=? WHERE id=?", (result, job["id"]))
                state = "ready"
            if state == "ready":
                uploaded_id = self.mm.upload(job["channel_id"], job["id"], result)
                with self.db:
                    self.db.execute("UPDATE jobs SET state='uploaded',uploaded_id=? WHERE id=?", (uploaded_id, job["id"]))
            self.mm.post(job["channel_id"], f"Your Excel report is ready. Job: `{job['id']}`.", [uploaded_id])
            with self.db:
                self.db.execute("UPDATE jobs SET state='done',result=NULL,files='[]' WHERE id=?", (job["id"],))
        except Exception as error:
            # Never log response bodies, tokens, image contents or backend URLs.
            LOG.error("Job %s failed (%s)", job["id"], type(error).__name__)
            with self.db:
                self.db.execute("UPDATE jobs SET state='failed' WHERE id=?", (job["id"],))
                detail = str(error) if isinstance(error, InputError) else "A service request failed; contact the bot operator if it persists."
                self.say(job["channel_id"], f"Job `{job['id']}` failed. {detail} Type `retry` to retry this job.")

    def flush(self):
        blocked_channels = set()
        for row in self.db.execute("SELECT * FROM outbox ORDER BY id").fetchall():
            if row["channel_id"] in blocked_channels:
                continue
            try:
                self.mm.post(row["channel_id"], row["message"])
            except requests.RequestException as error:
                blocked_channels.add(row["channel_id"])
                LOG.warning("Reply %s deferred (%s)", row["id"], type(error).__name__)
                continue
            with self.db:
                self.db.execute("DELETE FROM outbox WHERE id=?", (row["id"],))

    def run_once(self):
        try:
            self.poll()
        except requests.RequestException as error:
            LOG.warning("Polling deferred (%s)", type(error).__name__)
        self.consume()
        self.flush()
        self.work()
        self.flush()
