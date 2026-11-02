import os
from dataclasses import dataclass
from urllib.parse import urlparse


@dataclass(frozen=True)
class Config:
    mattermost_url: str
    mattermost_token: str
    backend_url: str
    backend_token: str = ""
    format_id: str = "default"
    data_dir: str = "data"
    poll_seconds: int = 5
    backend_timeout: int = 180
    max_images: int = 20
    max_image_bytes: int = 10 * 1024 * 1024
    max_batch_bytes: int = 50 * 1024 * 1024
    max_report_bytes: int = 20 * 1024 * 1024
    allowed_user_ids: frozenset[str] = frozenset()

    @classmethod
    def from_env(cls):
        values = {}
        for name in ("mattermost_url", "mattermost_token", "backend_url"):
            values[name] = os.environ[name.upper()].strip()
            if not values[name]:
                raise ValueError(f"{name.upper()} must not be empty")
        for name in ("mattermost_url", "backend_url"):
            url = urlparse(values[name])
            if url.scheme not in ("http", "https") or not url.netloc or url.username or url.password or url.query or url.fragment:
                raise ValueError(f"{name.upper()} must be an HTTP(S) URL without credentials, query or fragment")
        for name in ("backend_token", "format_id", "data_dir"):
            if name.upper() in os.environ:
                values[name] = os.environ[name.upper()]
        for name in ("poll_seconds", "backend_timeout", "max_images", "max_image_bytes", "max_batch_bytes", "max_report_bytes"):
            if name.upper() in os.environ:
                values[name] = int(os.environ[name.upper()])
                if values[name] <= 0:
                    raise ValueError(f"{name.upper()} must be positive")
        values["allowed_user_ids"] = frozenset(x.strip() for x in os.getenv("ALLOWED_USER_IDS", "").split(",") if x.strip())
        return cls(**values)
