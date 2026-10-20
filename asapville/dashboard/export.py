"""Self-contained replay page: the dashboard with the town's data baked in.

`asapville export --out town.html` writes one HTML file that needs no server. It is
the same page `serve` shows, minus the live feed, so it can be mailed or published.
"""

from __future__ import annotations

import json

from sqlalchemy.orm import Session

from asapville.config import Settings
from asapville.dashboard import snapshot
from asapville.dashboard.app import INDEX

DATA_TAG = '<script id="asapville-data" type="application/json"></script>'
# Lines a hosting page supplies itself (document skeleton, charset, viewport).
SKELETON_LINES = {
    "<!doctype html>", '<html lang="en">', "<head>", '<meta charset="utf-8">',
    '<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">',
    "</head>", "<body>", "</body>", "</html>",
}


def export_html(session: Session, settings: Settings, *, fragment: bool = False) -> str:
    """The dashboard page with this town's data embedded.

    `fragment=True` drops the document skeleton for hosts that wrap the file in
    their own (<!doctype>, <html>, <head>, <body>).
    """
    page = INDEX.read_text(encoding="utf-8")
    if DATA_TAG not in page:
        raise RuntimeError("dashboard page has no data slot")
    data = json.dumps(snapshot.bootstrap(session, settings), ensure_ascii=False,
                      separators=(",", ":")).replace("</", "<\\/")
    page = page.replace(DATA_TAG, DATA_TAG.replace("></script>", f">{data}</script>"))
    if fragment:
        page = "\n".join(line for line in page.splitlines() if line.strip() not in SKELETON_LINES)
    return page
