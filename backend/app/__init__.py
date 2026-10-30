"""Cloud Infra Job Portal backend.

For LOCAL development only (ENV unset or "local"), values from backend/.env
are loaded into the process environment before any module reads its settings.
Real environment variables always win, and nothing is loaded under pytest or in
production (where the ECS task definition provides the environment).
"""

import os
import sys
from pathlib import Path


def _load_local_dotenv() -> None:
    if os.environ.get("ENV", "local") != "local" or "pytest" in sys.modules:
        return
    path = Path(__file__).resolve().parents[1] / ".env"
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


_load_local_dotenv()
