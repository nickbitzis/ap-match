"""Capture README screenshots with headless Chrome. Start the server first: make run.

Usage: uv run python scripts/screenshot.py --port 8887
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import urllib.request
from pathlib import Path

CHROME_PATHS = [
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "google-chrome",
    "chromium",
]
DOCS = Path(__file__).resolve().parents[1] / "docs"


def chrome() -> str:
    for c in CHROME_PATHS:
        if Path(c).exists() or shutil.which(c):
            return c
    raise SystemExit("Chrome not found")


def shoot(url: str, out: Path, width: int, height: int, dark: bool = False) -> None:
    args = [
        chrome(),
        "--headless=new",
        "--disable-gpu",
        "--hide-scrollbars",
        f"--window-size={width},{height}",
        "--virtual-time-budget=8000",
        f"--screenshot={out}",
    ]
    if dark:
        args += ["--force-dark-mode", "--blink-settings=preferredColorScheme=0"]
    else:
        args.append("--blink-settings=preferredColorScheme=1")
    subprocess.run([*args, url], check=True, capture_output=True, timeout=60)
    print(f"wrote {out}")


def pick_invoice(base: str) -> str:
    """An open price exception on a three-way match with more than one receipt shows the most on one screen."""
    with urllib.request.urlopen(f"{base}/api/exceptions?type=price&limit=200") as r:
        rows = json.load(r)["rows"]
    for e in rows:
        with urllib.request.urlopen(f"{base}/api/invoices/{e['doc_id']}") as r:
            d = json.load(r)
        if d["match"]["mode"] == "three_way" and len(d["receipts"]) >= 2:
            return d["id"]
    return rows[0]["doc_id"]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8887)
    ap.add_argument("--out", type=Path, default=DOCS)
    a = ap.parse_args()
    base = f"http://127.0.0.1:{a.port}"
    a.out.mkdir(exist_ok=True)
    inv = pick_invoice(base)
    shoot(f"{base}/#/invoice/{inv}", a.out / "screenshot.png", 1400, 1250)
    shoot(f"{base}/#/queue", a.out / "queue.png", 1400, 1000)
    shoot(f"{base}/#/payments", a.out / "payments.png", 1400, 1100, dark=True)
    shoot(f"{base}/#/metrics", a.out / "metrics.png", 1400, 1500)


if __name__ == "__main__":
    main()
