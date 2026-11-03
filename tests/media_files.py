"""Real, minimal media files for tests: the input validator withholds bytes that do not decode
(user ruling 2026-11-03), so a test that means to send a PDF or an image writes a real one."""

from __future__ import annotations

import io
from pathlib import Path


def pdf_bytes(text: str = "paper", *, pad: int = 0) -> bytes:
    """A one-page PDF that decodes, optionally padded to about ``pad`` more bytes (a comment line
    before the cross-reference table, so the file stays readable)."""
    stream = f"BT /F1 12 Tf 72 720 Td ({text}) Tj ET".encode("latin-1")
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R "
        b"/Resources << /Font << /F1 5 0 R >> >> >>",
        b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    out = io.BytesIO()
    out.write(b"%PDF-1.4\n")
    offsets = []
    for number, body in enumerate(objects, 1):
        offsets.append(out.tell())
        out.write(f"{number} 0 obj\n".encode() + body + b"\nendobj\n")
    if pad > 0:
        out.write(b"%" + b"p" * pad + b"\n")
    xref = out.tell()
    out.write(f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode())
    for offset in offsets:
        out.write(f"{offset:010d} 00000 n \n".encode())
    out.write(f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode())
    return out.getvalue()


def write_pdf(path: Path, text: str = "paper", *, pad: int = 0) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(pdf_bytes(text, pad=pad))
    return path


def png_bytes(size: tuple[int, int] = (8, 6), color: tuple[int, int, int] = (10, 120, 200)) -> bytes:
    from PIL import Image

    out = io.BytesIO()
    Image.new("RGB", size, color).save(out, "PNG")
    return out.getvalue()
