"""Print the OpenAPI schema as JSON: `python -m app.openapi_export > openapi.json`.
The frontend generates its typed client from this (pnpm gen:api)."""

from __future__ import annotations

import json
import sys

from app.config import Settings
from app.main import create_app

if __name__ == "__main__":
    app = create_app(Settings(env="development", log_level="ERROR"))
    sys.stdout.write(json.dumps(app.openapi(), indent=2, sort_keys=True) + "\n")
