"""Launch check (Z1e, user request 2026-11-04): every configured member takes one tiny image and one
tiny PDF through the real ``ModelHandle.call``.

The capability table (``models.capabilities``) says what each model takes; this check proves the
member's vendor answers a request carrying each medium, as the sanitiser shapes it for that member
(sent natively, parsed, or withheld with a note). One call per distinct provider/model and medium,
all concurrent, each bounded; each call's cost lands on the run ledger through the run's cost meter
like any other call. The check passes only when every call returns a normal response.
"""

from __future__ import annotations

import asyncio
import hashlib
import io
import tempfile
from datetime import UTC, datetime
from pathlib import Path

#: Ceiling on one probe call, retries included, so a wedged vendor fails the check instead of
#: holding the launch.
PROBE_TIMEOUT_S = 180

_PROMPT = "This is a launch check of what you can be sent. Reply with the single word OK."


def _tiny_png(path: Path) -> Path:
    from PIL import Image

    Image.new("RGB", (64, 48), (30, 90, 160)).save(path, format="PNG")
    return path


def _tiny_pdf(path: Path) -> Path:
    """A one-page PDF holding one line of text: real bytes every reader decodes."""
    stream = b"BT /F1 12 Tf 72 720 Td (launch check) Tj ET"
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
    xref = out.tell()
    out.write(f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode())
    for offset in offsets:
        out.write(f"{offset:010d} 00000 n \n".encode())
    out.write(f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode())
    path.write_bytes(out.getvalue())
    return path


def _probe_members() -> list[tuple[object, list[str]]]:
    """One handle per distinct chat endpoint among every configured role's members — provider,
    model, endpoint and key, the request's own coordinates — with every ``member (role)`` sharing
    it. A member driven by an author harness (the coder) is never a ``ModelHandle.call``, so it has
    no chat request to prove here."""
    from auto_research.models.registry import load_registry

    registry = load_registry()
    seen: dict[tuple, tuple[object, list[str]]] = {}
    for role in sorted(registry.config.roles):
        for handle in registry.resolve(role).handles:
            config = handle.config
            if config.harness is not None:
                continue
            key = (config.provider, config.model, config.base_url, config.key_env)
            seen.setdefault(key, (handle, []))[1].append(f"{config.name} (role {role})")
    return list(seen.values())


async def _probe(handle, medium: str, ref) -> str | None:
    """One call carrying ``ref``; ``None`` when the vendor answered, else what it said."""
    from auto_research.models.retry import transient_errors
    from auto_research.models.types import ModelMessage

    kwargs = {"images": [ref]} if medium == "an image" else {"documents": [ref]}
    heard: list[BaseException] = []
    token = transient_errors.set(heard)  # the call's task copies this context, the list with it
    try:
        await asyncio.wait_for(
            handle.call([ModelMessage(role="user", content=_PROMPT)], **kwargs),
            timeout=PROBE_TIMEOUT_S,
        )
    except TimeoutError:
        last = (f"; the last error retried: {type(heard[-1]).__name__}: {str(heard[-1])[:300]}"
                if heard else "")
        return f"no answer within {PROBE_TIMEOUT_S} s, retries included{last}"
    except Exception as exc:  # noqa: BLE001 — the vendor's error is the finding, named below
        return f"{type(exc).__name__}: {str(exc)[:300]}"
    finally:
        transient_errors.reset(token)
    return None


async def check_member_media() -> None:
    """Every configured member answers one image request and one PDF request (Z1e). Raises naming
    each member, medium and vendor error that did not."""
    from auto_research.schemas import DocumentRef, FigureRef

    members = _probe_members()
    with tempfile.TemporaryDirectory(prefix="media-probe-") as folder:
        png = _tiny_png(Path(folder) / "launch-check.png")
        pdf = _tiny_pdf(Path(folder) / "launch-check.pdf")
        now = datetime.now(UTC)
        figure = FigureRef(path=png, caption="launch check image", mime_type="image/png",
                           produced_by="preflight", created_at=now,
                           sha256=hashlib.sha256(png.read_bytes()).hexdigest())
        document = DocumentRef(path=pdf, title="launch check document", mime_type="application/pdf",
                               created_at=now, sha256=hashlib.sha256(pdf.read_bytes()).hexdigest())
        cases = [(handle, names, medium, ref) for handle, names in members
                 for medium, ref in (("an image", figure), ("a PDF", document))]
        errors = await asyncio.gather(*(_probe(handle, medium, ref) for handle, _, medium, ref in cases))
    failures = [
        f"{handle.config.provider}/{handle.config.model} for {', '.join(names)} with {medium}: {error}"
        for (handle, names, medium, _), error in zip(cases, errors) if error is not None
    ]
    if failures:
        raise RuntimeError(
            "these members did not answer a request carrying the medium named; check the "
            "member's entry in config/model_capabilities.yaml and its vendor's account: "
            + "; ".join(failures)
        )


__all__ = ["PROBE_TIMEOUT_S", "check_member_media"]
