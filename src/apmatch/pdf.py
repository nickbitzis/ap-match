"""Render invoice text into a simple one-page PDF with reportlab (optional extra: uv sync --extra pdf).

The PDF carries the same text as the text rendering, in a monospace font, which is how many supplier portals
export. Capture reads the text layer, so the parsers see the same characters."""

from __future__ import annotations

from pathlib import Path

from .model import Invoice


def write_pdfs(invoices: list[Invoice], out: Path, count: int = 5) -> list[Path]:
    try:
        from reportlab.lib.pagesizes import letter
        from reportlab.pdfgen import canvas
    except ImportError as exc:  # pragma: no cover - depends on install extras
        raise SystemExit("Install the pdf extra first: uv sync --extra pdf") from exc
    out.mkdir(parents=True, exist_ok=True)
    paths = []
    for inv in invoices[:count]:
        path = out / f"{inv.id}.pdf"
        c = canvas.Canvas(str(path), pagesize=letter)
        c.setFont("Courier", 8)
        y = 750
        for line in inv.text.splitlines():
            c.drawString(36, y, line)
            y -= 11
        c.showPage()
        c.save()
        paths.append(path)
    return paths
