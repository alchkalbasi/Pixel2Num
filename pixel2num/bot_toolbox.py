"""Mattermost REST client and report/input helpers used by the bot."""
import io
import zipfile
from collections import Counter

import requests
from openpyxl import load_workbook

XLSX_TYPE = (
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
IMAGE_TYPES = {
    "image/png", "image/jpeg", "image/webp", "image/tiff", "image/bmp"}


class InputError(Exception):
    """Safe, user-facing validation error."""


def bounded_content(response, limit):
    """Read a streamed response, failing if it is bigger than limit bytes."""
    response.raise_for_status()
    result = bytearray()
    for chunk in response.iter_content(64 * 1024):
        result.extend(chunk)
        if len(result) > limit:
            raise InputError("A file exceeds the configured size limit.")
    return bytes(result)


def validate_report(data):
    """Check that data looks like a real .xlsx without macros."""
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            names = set(archive.namelist())
            required = {"[Content_Types].xml", "xl/workbook.xml",
                        "_rels/.rels"}
            if not required <= names:
                raise ValueError("Missing workbook parts")
            if any(n.lower().endswith("vbaproject.bin") for n in names):
                raise ValueError("Macros are not allowed")
    except (zipfile.BadZipFile, ValueError) as error:
        raise InputError(
            "The extractor did not produce a valid .xlsx workbook."
        ) from error


def summarize_report(data):
    """Counts of the Status column (succeeded/failed/repeated).

    Never returns codes.
    """
    sheet = load_workbook(io.BytesIO(data), read_only=True).active
    # Status is the 4th column; skip the header row.
    counts = Counter(
        row[3] for row in sheet.iter_rows(min_row=2, values_only=True)
        if len(row) > 3)
    return ", ".join(
        f"{counts[k]} {k}" for k in ("succeeded", "failed", "repeated"))


class Mattermost:
    """Minimal Mattermost API v4 client (Bearer token)."""

    def __init__(self, config):
        self.url = config.mattermost_url.rstrip("/") + "/api/v4"
        self.session = requests.Session()
        self.session.headers["Authorization"] = (
            f"Bearer {config.mattermost_token}")

    def request(self, method, path, **kwargs):
        # Redirects are never followed, so the token cannot leak elsewhere.
        response = self.session.request(
            method, self.url + path, timeout=(10, 60),
            allow_redirects=False, **kwargs)
        response.raise_for_status()
        if 300 <= response.status_code < 400:
            response.close()
            raise InputError(
                "Mattermost redirected the request; check its configured "
                "URL.")
        return response

    def get(self, path, **kwargs):
        with self.request("GET", path, **kwargs) as response:
            return response.json()

    def download(self, file_id, limit):
        with self.request(
                "GET", f"/files/{file_id}", stream=True) as response:
            return bounded_content(response, limit)

    def post(self, channel_id, message, file_ids=None):
        payload = {"channel_id": channel_id, "message": message,
                   "file_ids": file_ids or []}
        with self.request("POST", "/posts", json=payload) as response:
            return response.json()

    def upload(self, channel_id, job_id, data):
        files = {"files": (f"pixel2num-{job_id}.xlsx", data, XLSX_TYPE)}
        with self.request("POST", "/files", data={"channel_id": channel_id},
                          files=files) as response:
            return response.json()["file_infos"][0]["id"]
