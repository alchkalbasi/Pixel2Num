"""L1 unit tests: bot logic with a fake Mattermost and a fake bridge."""
import io
import json
import logging
import tempfile
import unittest
import zipfile
from dataclasses import replace
from unittest.mock import Mock

import requests
from openpyxl import Workbook

from pixel2num.bot import Bot
from pixel2num.bot_toolbox import (
    InputError, bounded_content, summarize_report, validate_report)
from pixel2num.config import Config

SECRET_CODE = "123456789012345"
# file id -> valid START-END name
RANGES = {"a": "1-5", "b": "6-10", "c": "11-15", "d": "16-20"}


def workbook():
    """A small valid report with one succeeded, failed and repeated row."""
    wb = Workbook()
    ws = wb.active
    ws.append(["Image", "Counter", "Charge Code", "Status"])
    ws.append(["1-5.png", 1, SECRET_CODE, "succeeded"])
    ws.append(["1-5.png", 2, "", "failed"])
    ws.append(["1-5.png", 3, SECRET_CODE, "repeated"])
    stream = io.BytesIO()
    wb.save(stream)
    return stream.getvalue()


def fake_package():
    """A zip with only the parts validate_report looks for."""
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w") as archive:
        for name in ("[Content_Types].xml", "xl/workbook.xml", "_rels/.rels"):
            archive.writestr(name, "<test/>")
    return stream.getvalue()


class FakeMattermost:
    """In-memory stand-in for the Mattermost client."""

    def __init__(self):
        self.channels = [{"id": "dm", "type": "D"},
                         {"id": "other", "type": "D"},
                         {"id": "public", "type": "O"}]
        self.posts = []
        self.info = {}
        self.sent = []
        self.uploads = []
        self.fail_delivery = False

    def get(self, path, params=None):
        if path == "/users/me":
            return {"id": "bot"}
        if path == "/users/me/channels":
            return self.channels
        if path.startswith("/files/"):
            return self.info[path.split("/")[2]]
        # Channel posts, newest first, paged with "before" like the real API.
        cid = path.split("/")[2]
        posts = sorted([p for p in self.posts if p["channel_id"] == cid],
                       key=lambda p: (p["create_at"], p["id"]), reverse=True)
        if params.get("before"):
            index = next(i for i, p in enumerate(posts)
                         if p["id"] == params["before"])
            posts = posts[index + 1:]
        posts = posts[:params["per_page"]]
        return {"order": [p["id"] for p in posts],
                "posts": {p["id"]: p for p in posts}}

    def download(self, file_id, limit):
        return b"image"

    def upload(self, cid, job_id, data):
        self.uploads.append((cid, job_id, data))
        return "uploaded-file"

    def post(self, cid, message, file_ids=None):
        if file_ids and self.fail_delivery:
            raise requests.ConnectionError()
        self.sent.append((cid, message, file_ids))


class BotTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.config = Config(
            "https://chat.test", "mm-secret", data_dir=self.temp.name)
        self.mm = FakeMattermost()
        self.bridge = Mock()
        self.bridge.convert.return_value = workbook()
        self.bot = Bot(self.config, self.mm, self.bridge)
        self.counter = 0

    def tearDown(self):
        self.bot.db.close()
        self.temp.cleanup()

    def add(self, text="", files=(), cid="dm", uid="user"):
        """Add a post to the fake server. Files in RANGES get a valid
        START-END name; other file IDs are used as the file name."""
        self.counter += 1
        post = {"id": f"post-{self.counter:04}", "channel_id": cid,
                "user_id": uid, "message": text, "file_ids": list(files),
                "create_at": self.bot.started + self.counter, "type": ""}
        self.mm.posts.append(post)
        for fid in files:
            name = RANGES[fid] + ".png" if fid in RANGES else fid + ".png"
            self.mm.info[fid] = {"post_id": post["id"],
                                 "mime_type": "image/png", "size": 5,
                                 "name": name}
        return post

    def test_batch_multiple_messages_delivery_and_deduplication(self):
        self.add(files=["a", "b"])
        self.add(files=["c"])
        process = self.add("process")
        self.bot.run_once()
        self.bot.run_once()
        self.bridge.convert.assert_called_once()
        job, images = self.bridge.convert.call_args.args
        self.assertEqual(job["id"], process["id"])
        self.assertEqual([i[0] for i in images],
                         ["1-5.png", "6-10.png", "11-15.png"])
        self.assertEqual(len(self.mm.uploads), 1)
        self.assertEqual(self.mm.sent[-1][0], "dm")
        self.assertEqual(self.mm.sent[-1][2], ["uploaded-file"])
        row = self.bot.db.execute("SELECT * FROM jobs").fetchone()
        self.assertEqual(row["state"], "done")
        self.assertIsNone(row["result"])

    def test_isolation_public_channels_and_bot_messages(self):
        self.add(files=["a"])
        self.add(files=["b"], cid="other", uid="second-user")
        self.add(files=["c"], cid="public")
        self.add(files=["d"], uid="bot")
        self.add("process", cid="other", uid="second-user")
        self.bot.run_once()
        self.assertEqual(self.mm.uploads[0][0], "other")
        self.assertEqual(
            self.bridge.convert.call_args.args[1][0][0], "6-10.png")
        rows = self.bot.db.execute("SELECT * FROM batches").fetchall()
        self.assertEqual(len(rows), 1)
        self.assertEqual(json.loads(rows[0]["files"])[0]["id"], "a")

    def test_invalid_message_is_atomic_and_limits_apply(self):
        self.add(files=["a", "bad"])
        self.mm.info["bad"]["mime_type"] = "application/pdf"
        self.bot.run_once()
        self.assertEqual(
            self.bot.db.execute("SELECT COUNT(*) FROM batches").fetchone()[0],
            0)
        self.bot.config = replace(self.config, max_images=1)
        self.add(files=["b", "c"])
        self.bot.run_once()
        self.assertIn("Batch limit", self.mm.sent[-1][1])

    def test_failed_extraction_retries_same_job(self):
        self.bridge.convert.side_effect = RuntimeError()
        self.add(files=["a"])
        process = self.add("process")
        self.bot.run_once()
        self.assertEqual(
            self.bot.db.execute("SELECT state FROM jobs").fetchone()[0],
            "failed")
        self.bridge.convert.side_effect = None
        self.add("retry")
        self.bot.run_once()
        self.assertEqual(
            self.bridge.convert.call_args.args[0]["id"], process["id"])
        self.assertEqual(
            self.bot.db.execute("SELECT state FROM jobs").fetchone()[0],
            "done")

    def test_delivery_retry_reuses_uploaded_workbook(self):
        self.add(files=["a"])
        self.add("process")
        self.mm.fail_delivery = True
        self.bot.run_once()
        self.mm.fail_delivery = False
        self.add("retry")
        self.bot.run_once()
        self.bridge.convert.assert_called_once()
        self.assertEqual(len(self.mm.uploads), 1)
        self.assertEqual(self.mm.sent[-1][2], ["uploaded-file"])

    def test_restart_pending_batch_and_interrupted_job(self):
        self.add(files=["a"])
        self.bot.run_once()
        self.bot.db.close()
        self.bot = Bot(self.config, self.mm, self.bridge)
        self.add("process")
        self.bot.poll()
        self.bot.consume()
        with self.bot.db:
            self.bot.db.execute("UPDATE jobs SET state='processing'")
        self.bot.db.close()
        self.bot = Bot(self.config, self.mm, self.bridge)
        self.bot.run_once()
        self.bridge.convert.assert_called_once()

    def test_pagination_and_same_timestamp(self):
        for i in range(205):
            self.add("help")["create_at"] = self.bot.started + 10
        self.bot.poll()
        self.bot.consume()
        self.bot.poll()
        self.assertEqual(
            self.bot.db.execute("SELECT COUNT(*) FROM inbox").fetchone()[0],
            205)
        self.assertEqual(
            self.bot.db.execute("SELECT COUNT(*) FROM outbox").fetchone()[0],
            205)

    def test_allowlist_and_clear(self):
        self.bot.config = replace(
            self.config, allowed_user_ids=frozenset(["user"]))
        self.add(files=["a"], uid="blocked")
        self.add(files=["b"])
        self.add("clear")
        self.add("process")
        self.bot.run_once()
        self.bridge.convert.assert_not_called()
        self.assertIn("Send images first", self.mm.sent[-1][1])

    def test_missing_attachment_does_not_block_later_messages(self):
        self.add(files=["missing"])
        self.add("help")
        original_get = self.mm.get
        response = requests.Response()
        response.status_code = 404

        def get(path, **kwargs):
            if path == "/files/missing/info":
                raise requests.HTTPError(response=response)
            return original_get(path, **kwargs)

        self.mm.get = get
        self.bot.run_once()
        self.assertIn("unavailable", self.mm.sent[0][1])
        self.assertIn("START-END", self.mm.sent[1][1])
        self.assertEqual(
            self.bot.db.execute(
                "SELECT COUNT(*) FROM inbox WHERE done=0").fetchone()[0],
            0)

    def test_invalid_filename_rejects_whole_message(self):
        self.add(files=["a"])
        self.add(files=["b", "IMG_1234"])
        self.bot.run_once()
        reply = [m for _, m, _ in self.mm.sent if "Cannot use" in m][0]
        self.assertIn("IMG_1234.png", reply)
        self.assertIn("START-END", reply)
        self.assertIn("Nothing from this message was added", reply)
        batch = self.bot.db.execute("SELECT files FROM batches").fetchone()[0]
        self.assertEqual([f["id"] for f in json.loads(batch)], ["a"])
        self.assertEqual(
            self.bot.db.execute("SELECT COUNT(*) FROM jobs").fetchone()[0], 0)

    def test_all_invalid_files_listed_and_bounds(self):
        self.add(files=["IMG_1", "9-3", "1-1000"])
        self.bot.run_once()
        reply = self.mm.sent[-1][1]
        for name in ("IMG_1.png", "9-3.png", "1-1000.png"):
            self.assertIn(name, reply)
        self.assertEqual(
            self.bot.db.execute("SELECT COUNT(*) FROM batches").fetchone()[0],
            0)

    def test_caption_never_supplies_range_or_command(self):
        self.add("81-100", files=["IMG_9"])
        self.add("process", files=["a"])
        self.bot.run_once()
        self.assertIn("Cannot use", self.mm.sent[0][1])
        self.assertEqual(
            self.bot.db.execute("SELECT COUNT(*) FROM jobs").fetchone()[0], 0)
        self.bridge.convert.assert_not_called()

    def test_range_stored_and_listed_in_reply(self):
        self.add(files=["a", "b"])
        self.bot.run_once()
        self.assertIn("Added: 1-5, 6-10.", self.mm.sent[0][1])
        files = json.loads(
            self.bot.db.execute("SELECT files FROM batches").fetchone()[0])
        self.assertEqual(
            [(f["start"], f["count"]) for f in files], [(1, 5), (6, 5)])

    def test_overlapping_ranges_warn_but_are_allowed(self):
        self.add(files=["a"])
        self.add(files=["5-9"])
        self.bot.run_once()
        self.assertIn("overlapping", self.mm.sent[-1][1])
        batch = self.bot.db.execute("SELECT files FROM batches").fetchone()[0]
        self.assertEqual(len(json.loads(batch)), 2)

    def test_bmp_accepted_and_default_limit(self):
        self.assertEqual(self.config.max_images, 50)
        self.add(files=["a"])
        self.mm.info["a"]["mime_type"] = "image/bmp"
        self.bot.run_once()
        self.assertIn("Collected 1", self.mm.sent[-1][1])

    def test_bridge_gets_ranges_and_ready_message_has_counts_only(self):
        self.add(files=["a", "b"])
        self.add("process")
        with self.assertLogs(level="INFO") as logs:
            self.bot.run_once()
            logging.getLogger().info("end")
        job, images = self.bridge.convert.call_args.args
        self.assertEqual([(i[3], i[4]) for i in images], [(1, 5), (6, 5)])
        ready = self.mm.sent[-1][1]
        self.assertIn("1 succeeded, 1 failed, 1 repeated", ready)
        everything = (ready + "".join(logs.output)
                      + "".join(m for _, m, _ in self.mm.sent))
        self.assertNotIn(SECRET_CODE, everything)

    def test_invalid_report_from_bridge_fails_job(self):
        self.bridge.convert.return_value = b"not a workbook"
        self.add(files=["a"])
        self.add("process")
        self.bot.run_once()
        self.assertEqual(
            self.bot.db.execute("SELECT state FROM jobs").fetchone()[0],
            "failed")

    def test_old_job_without_range_uses_file_name(self):
        self.add(files=["a"])
        self.add("process")
        self.bot.poll()
        self.bot.consume()
        with self.bot.db:
            files = json.loads(
                self.bot.db.execute("SELECT files FROM jobs").fetchone()[0])
            for f in files:
                del f["start"], f["count"]
            self.bot.db.execute(
                "UPDATE jobs SET files=?", (json.dumps(files),))
        self.bot.work()
        images = self.bridge.convert.call_args.args[1]
        self.assertEqual([(i[3], i[4]) for i in images], [(1, 5)])


class TransportTests(unittest.TestCase):
    def test_stream_size_limit(self):
        response = Mock()
        response.iter_content.return_value = [b"123", b"456"]
        with self.assertRaises(InputError):
            bounded_content(response, 5)

    def test_invalid_report_rejected(self):
        with self.assertRaises(InputError):
            validate_report(b'{"error":"not a workbook"}')
        validate_report(workbook())
        validate_report(fake_package())

    def test_summary_has_counts_only(self):
        summary = summarize_report(workbook())
        self.assertEqual(summary, "1 succeeded, 1 failed, 1 repeated")
        self.assertNotIn(SECRET_CODE, summary)


if __name__ == "__main__":
    unittest.main()
