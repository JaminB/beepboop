"""Generate ``openapi.json`` for ``pnpm --dir ui gen:api`` (docs/WEB_UI.md §8).

``beepboop api openapi --out ui/openapi.json`` writes the document from a throwaway app
(no database needed), with keys sorted so CI can diff it.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

from beepboop.api.app import create_app
from beepboop.api.deps import ApiDeps

DEFAULT_OUT = Path("ui") / "openapi.json"


def openapi_document(version: str = "0.0.0") -> dict[str, Any]:
    """The OpenAPI document of an app built without a database (schemas only)."""
    deps = ApiDeps(
        db=cast(Any, None),
        registry=cast(Any, None),
        policy_state=lambda: cast(Any, None),
        clock=lambda: datetime.now(UTC),
        version=version,
    )
    app = create_app(deps)
    return json.loads(json.dumps(app.openapi(), sort_keys=True))


def write_openapi(out: Path = DEFAULT_OUT, version: str = "0.0.0") -> Path:
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(
        json.dumps(openapi_document(version), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    return out


__all__ = ["DEFAULT_OUT", "openapi_document", "write_openapi"]
