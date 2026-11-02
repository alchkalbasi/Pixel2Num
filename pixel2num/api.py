import io
import json
import zipfile

import requests

XLSX_TYPE = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
IMAGE_TYPES = {"image/png", "image/jpeg", "image/webp", "image/tiff"}


class InputError(Exception):
    """Safe, user-facing validation error."""


def bounded_content(response, limit):
    response.raise_for_status()
    result = bytearray()
    for chunk in response.iter_content(64 * 1024):
        result.extend(chunk)
        if len(result) > limit:
            raise InputError("A file exceeds the configured size limit.")
    return bytes(result)


def validate_report(data):
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            names = set(archive.namelist())
            if not {"[Content_Types].xml", "xl/workbook.xml", "_rels/.rels"} <= names:
                raise ValueError("Missing workbook parts")
            if any(n.lower().endswith("vbaproject.bin") for n in names):
                raise ValueError("Macros are not allowed")
    except (zipfile.BadZipFile, ValueError) as error:
        raise InputError("The backend did not return an .xlsx workbook.") from error


class Mattermost:
    def __init__(self, config):
        self.url = config.mattermost_url.rstrip("/") + "/api/v4"
        self.session = requests.Session()
        self.session.headers["Authorization"] = f"Bearer {config.mattermost_token}"

    def request(self, method, path, **kwargs):
        response = self.session.request(method, self.url + path, timeout=(10, 60), allow_redirects=False, **kwargs)
        response.raise_for_status()
        if 300 <= response.status_code < 400:
            response.close()
            raise InputError("Mattermost redirected the request; check its configured URL.")
        return response

    def get(self, path, **kwargs):
        with self.request("GET", path, **kwargs) as response:
            return response.json()

    def download(self, file_id, limit):
        with self.request("GET", f"/files/{file_id}", stream=True) as response:
            return bounded_content(response, limit)

    def post(self, channel_id, message, file_ids=None):
        with self.request("POST", "/posts", json={"channel_id": channel_id, "message": message, "file_ids": file_ids or []}) as response:
            return response.json()

    def upload(self, channel_id, job_id, data):
        with self.request("POST", "/files", data={"channel_id": channel_id}, files={"files": (f"pixel2num-{job_id}.xlsx", data, XLSX_TYPE)}) as response:
            return response.json()["file_infos"][0]["id"]


class Backend:
    def __init__(self, config):
        self.config = config
        self.session = requests.Session()
        if config.backend_token:
            self.session.headers["Authorization"] = f"Bearer {config.backend_token}"

    def convert(self, job, images):
        c = self.config
        metadata = {"job_id": job["id"], "user_id": job["user_id"], "channel_id": job["channel_id"], "format_id": c.format_id}
        # A separate session ensures the Mattermost token never reaches the backend.
        with self.session.post(c.backend_url, data={"metadata": json.dumps(metadata)}, files=[("images", image) for image in images], headers={"Idempotency-Key": job["id"]}, timeout=(10, c.backend_timeout), stream=True, allow_redirects=False) as response:
            if response.status_code != 200:
                response.raise_for_status()
                raise InputError("The backend must return HTTP 200 with an .xlsx file.")
            if response.headers.get("Content-Type", "").split(";")[0].strip().lower() != XLSX_TYPE:
                raise InputError("The backend returned an unexpected content type.")
            data = bounded_content(response, c.max_report_bytes)
        validate_report(data)
        return data
