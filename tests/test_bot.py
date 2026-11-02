import io
import json
import tempfile
import unittest
import zipfile
from dataclasses import replace
from unittest.mock import Mock

import requests

from pixel2num.api import Backend, InputError, XLSX_TYPE, bounded_content, validate_report
from pixel2num.bot import Bot
from pixel2num.config import Config


def workbook():
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w") as archive:
        for name in ("[Content_Types].xml", "xl/workbook.xml", "_rels/.rels"):
            archive.writestr(name, "<test/>")
    return stream.getvalue()


class FakeMattermost:
    def __init__(self):
        self.channels = [{"id": "dm", "type": "D"}, {"id": "other", "type": "D"}, {"id": "public", "type": "O"}]
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
        cid = path.split("/")[2]
        posts = sorted([p for p in self.posts if p["channel_id"] == cid], key=lambda p: (p["create_at"], p["id"]), reverse=True)
        if params.get("before"):
            index = next(i for i, p in enumerate(posts) if p["id"] == params["before"])
            posts = posts[index + 1:]
        posts = posts[:params["per_page"]]
        return {"order": [p["id"] for p in posts], "posts": {p["id"]: p for p in posts}}

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
        self.config = Config("https://chat.test", "mm-secret", "https://backend.test/reports", data_dir=self.temp.name)
        self.mm = FakeMattermost()
        self.backend = Mock()
        self.backend.convert.return_value = workbook()
        self.bot = Bot(self.config, self.mm, self.backend)
        self.counter = 0

    def tearDown(self):
        self.bot.db.close()
        self.temp.cleanup()

    def add(self, text="", files=(), cid="dm", uid="user"):
        self.counter += 1
        post = {"id": f"post-{self.counter:04}", "channel_id": cid, "user_id": uid, "message": text, "file_ids": list(files), "create_at": self.bot.started + self.counter, "type": ""}
        self.mm.posts.append(post)
        for fid in files:
            self.mm.info[fid] = {"post_id": post["id"], "mime_type": "image/png", "size": 5, "name": fid + ".png"}
        return post

    def test_batch_multiple_messages_delivery_and_deduplication(self):
        self.add(files=["a", "b"])
        self.add(files=["c"])
        process = self.add("process")
        self.bot.run_once()
        self.bot.run_once()
        self.backend.convert.assert_called_once()
        job, images = self.backend.convert.call_args.args
        self.assertEqual(job["id"], process["id"])
        self.assertEqual([i[0] for i in images], ["a.png", "b.png", "c.png"])
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
        self.assertEqual(self.backend.convert.call_args.args[1][0][0], "b.png")
        rows = self.bot.db.execute("SELECT * FROM batches").fetchall()
        self.assertEqual(len(rows), 1)
        self.assertEqual(json.loads(rows[0]["files"])[0]["id"], "a")

    def test_invalid_message_is_atomic_and_limits_apply(self):
        self.add(files=["a", "bad"])
        self.mm.info["bad"]["mime_type"] = "application/pdf"
        self.bot.run_once()
        self.assertEqual(self.bot.db.execute("SELECT COUNT(*) FROM batches").fetchone()[0], 0)
        self.bot.config = replace(self.config, max_images=1)
        self.add(files=["b", "c"])
        self.bot.run_once()
        self.assertIn("Batch limit", self.mm.sent[-1][1])

    def test_failed_backend_retries_same_job(self):
        self.backend.convert.side_effect = requests.Timeout()
        self.add(files=["a"])
        process = self.add("process")
        self.bot.run_once()
        self.assertEqual(self.bot.db.execute("SELECT state FROM jobs").fetchone()[0], "failed")
        self.backend.convert.side_effect = None
        self.add("retry")
        self.bot.run_once()
        self.assertEqual(self.backend.convert.call_args.args[0]["id"], process["id"])
        self.assertEqual(self.bot.db.execute("SELECT state FROM jobs").fetchone()[0], "done")

    def test_delivery_retry_reuses_uploaded_workbook(self):
        self.add(files=["a"])
        self.add("process")
        self.mm.fail_delivery = True
        self.bot.run_once()
        self.mm.fail_delivery = False
        self.add("retry")
        self.bot.run_once()
        self.backend.convert.assert_called_once()
        self.assertEqual(len(self.mm.uploads), 1)
        self.assertEqual(self.mm.sent[-1][2], ["uploaded-file"])

    def test_restart_pending_batch_and_interrupted_job(self):
        self.add(files=["a"])
        self.bot.run_once()
        self.bot.db.close()
        self.bot = Bot(self.config, self.mm, self.backend)
        self.add("process")
        self.bot.poll()
        self.bot.consume()
        with self.bot.db:
            self.bot.db.execute("UPDATE jobs SET state='processing'")
        self.bot.db.close()
        self.bot = Bot(self.config, self.mm, self.backend)
        self.bot.run_once()
        self.backend.convert.assert_called_once()

    def test_pagination_and_same_timestamp(self):
        for i in range(205):
            self.add("help")["create_at"] = self.bot.started + 10
        self.bot.poll()
        self.bot.consume()
        self.bot.poll()
        self.assertEqual(self.bot.db.execute("SELECT COUNT(*) FROM inbox").fetchone()[0], 205)
        self.assertEqual(self.bot.db.execute("SELECT COUNT(*) FROM outbox").fetchone()[0], 205)

    def test_allowlist_and_clear(self):
        self.bot.config = replace(self.config, allowed_user_ids=frozenset(["user"]))
        self.add(files=["a"], uid="blocked")
        self.add(files=["b"])
        self.add("clear")
        self.add("process")
        self.bot.run_once()
        self.backend.convert.assert_not_called()
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
        self.assertIn("Send PNG", self.mm.sent[1][1])
        self.assertEqual(self.bot.db.execute("SELECT COUNT(*) FROM inbox WHERE done=0").fetchone()[0], 0)


class TransportTests(unittest.TestCase):
    def test_multipart_contract_and_auth_separation(self):
        config = Config("https://chat.test", "mattermost-secret", "https://backend.test/reports", backend_token="backend-secret")
        backend = Backend(config)
        response = Mock()
        response.status_code = 200
        response.headers = {"Content-Type": XLSX_TYPE}
        response.iter_content.return_value = [workbook()]
        context = Mock()
        context.__enter__ = Mock(return_value=response)
        context.__exit__ = Mock(return_value=False)
        backend.session.post = Mock(return_value=context)
        backend.convert({"id": "job", "user_id": "u", "channel_id": "c"}, [("a.png", b"image", "image/png")])
        kwargs = backend.session.post.call_args.kwargs
        self.assertEqual(kwargs["headers"]["Idempotency-Key"], "job")
        self.assertEqual(kwargs["files"][0][0], "images")
        self.assertEqual(json.loads(kwargs["data"]["metadata"])["format_id"], "default")
        self.assertEqual(backend.session.headers["Authorization"], "Bearer backend-secret")
        self.assertFalse(kwargs["allow_redirects"])

    def test_stream_size_limit(self):
        response = Mock()
        response.iter_content.return_value = [b"123", b"456"]
        with self.assertRaises(InputError):
            bounded_content(response, 5)

    def test_invalid_report_rejected(self):
        with self.assertRaises(InputError):
            validate_report(b'{"error":"not a workbook"}')
        validate_report(workbook())


if __name__ == "__main__":
    unittest.main()
