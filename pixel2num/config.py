import os
from dataclasses import dataclass
from urllib.parse import urlparse


@dataclass(frozen=True)
class Config:
    mattermost_url: str
    mattermost_token: str
    data_dir: str = "data"
    poll_seconds: int = 5
    max_images: int = 50
    max_cards_per_image: int = 40
    code_len: int | None = None
    layout: str = "auto"
    max_image_bytes: int = 10 * 1024 * 1024
    max_batch_bytes: int = 50 * 1024 * 1024
    max_report_bytes: int = 20 * 1024 * 1024
    allowed_user_ids: frozenset[str] = frozenset()

    @classmethod
    def from_env(cls):
        values = {}
        for name in ("mattermost_url", "mattermost_token"):
            values[name] = os.environ[name.upper()].strip()
            if not values[name]:
                raise ValueError(f"{name.upper()} must not be empty")
        url = urlparse(values["mattermost_url"])
        if url.scheme not in ("http", "https") or not url.netloc or url.username or url.password or url.query or url.fragment:
            raise ValueError("MATTERMOST_URL must be an HTTP(S) URL without credentials, query or fragment")
        if "DATA_DIR" in os.environ:
            values["data_dir"] = os.environ["DATA_DIR"]
        for name in ("poll_seconds", "max_images", "max_cards_per_image", "max_image_bytes", "max_batch_bytes", "max_report_bytes"):
            if name.upper() in os.environ:
                values[name] = int(os.environ[name.upper()])
                if values[name] <= 0:
                    raise ValueError(f"{name.upper()} must be positive")
        if os.getenv("CODE_LEN", "").strip():
            values["code_len"] = int(os.environ["CODE_LEN"])
            if values["code_len"] <= 0:
                raise ValueError("CODE_LEN must be positive")
        if os.getenv("LAYOUT", "").strip():
            values["layout"] = os.environ["LAYOUT"].strip().lower()
            if values["layout"] not in ("auto", "right", "left", "middle"):
                raise ValueError("LAYOUT must be one of: auto, right, left, middle")
        values["allowed_user_ids"] = frozenset(x.strip() for x in os.getenv("ALLOWED_USER_IDS", "").split(",") if x.strip())
        return cls(**values)
