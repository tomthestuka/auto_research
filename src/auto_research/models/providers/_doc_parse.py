"""Local document parsing for vendors with no usable document channel.

**Why this exists.** Anthropic and OpenAI both ingest a PDF as *text plus a rendered image of
every page* — their own docs say so ("The system converts each page of the document into an
image. The text from each page is extracted and provided alongside each page's image";
"The API extracts both text and page images and sends both to the model"). Alibaba publishes no
route we can use: its Create-a-response reference accepts ``input_file`` by ``file_url`` (a public
URL) only, and restricts PDF input to ``qwen3.5-ocr``. Our papers are local files with no public
URL. So for that seat the same two channels have to be assembled here instead, which is what this
module does — it is not a downgrade bolted onto one vendor, it is the other two vendors' own
behaviour reproduced where the vendor does not provide it.

**Why not extract the embedded images.** Scientific plots are *vector* content: matplotlib's PDF
backend emits path operators (``m``/``l``/``c``) from ``draw_path``, and only ever writes an image
XObject from the structurally separate ``draw_image``, which an ordinary line/scatter/contour plot
never reaches. TikZ/pgfplots and R's ``pdf()`` device behave the same way. Measured on the paper
this was built for (Krawczynski et al. 2022): **11 of 15 figure pages carry no embedded raster at
all**. Pulling embedded images out of such a document silently yields almost nothing, which is the
worst kind of failure — it looks like it worked. Docling instead *renders* the page and crops the
region its layout model detected, so vector and raster figures are equally recoverable.

**Why the result is cached on disk.** A ReAct loop re-sends its accumulated attachments on every
turn, so a parse in the request path would re-run on every turn of the reviewer's loop; the paper
this was built for takes ~40 s. The cache is keyed on the document's ``sha256`` — content, not
path — so the same paper parsed under two run directories is parsed once. It is written under the
runtime directory rather than held in memory so it survives the process, lands in the run archive
an offline scoring pass reads, and can be inspected by a human after the fact.

**What is recorded beside it.** ``manifest.json`` names the Docling version and the options that
produced the crops. A benchmark reads these artifacts long after the run, and a figure whose
provenance is "some version of some parser" is not evidence.
"""

from __future__ import annotations

import hashlib
import json
import shutil
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from auto_research.schemas import DocumentRef, FigureRef

#: Rendering scale for cropped figures. Docling's ``images_scale`` is a multiplier on 72 DPI, so
#: 2.5 puts us at 180 DPI — inside the 150-200 DPI band that document-parsing tools converge on
#: (Docling itself feeds its OCR stage 216 DPI; olmOCR/LightOnOCR express the same trade as a
#: pixel budget of ~1024-1540 px on the long edge). Below ~150 DPI axis tick labels and dense
#: mathematical notation stop being legible to a vision encoder; past ~300 DPI the accuracy return
#: flattens while payload and vision-token cost keep climbing.
IMAGES_SCALE = 2.5

#: Characters of extracted text one document may contribute.
#:
#: Substituted text leaves the media byte plan and becomes ordinary request text, which nothing
#: above bounds, and a ReAct loop re-sends it every turn — the shape recorded in CLAUDE.md 13 for
#: an unbounded ``web_fetch`` payload, where one 159,668-character page dominated a worker's
#: context and was re-cleared on every subsequent turn. The 51-page paper this was built for
#: extracts to ~83,000 characters, so this holds a full paper and still bounds a pathological one.
MAX_TEXT_CHARS = 200_000

#: Figure crops one document may contribute, oldest-first.
#:
#: A cap rather than a budget because these are *added* to a request whose real media ceiling is
#: enforced downstream by the media plan: this stops one document flooding the block count before
#: that plan ever sees it, and the plan still evicts against the byte ceiling afterwards.
MAX_FIGURES = 20


@dataclass(frozen=True)
class ParsedDocument:
    """One document rendered into the two channels a vendor without a file channel can take."""

    text: str
    figures: tuple[FigureRef, ...]
    truncated: bool
    """Whether ``text`` was cut at :data:`MAX_TEXT_CHARS`; the note says so when it was."""


def cache_root() -> Path:
    """Where parsed documents are kept. Under the runtime dir, never inside either repo."""
    from auto_research.env import runtime_data_dir

    # Resolved: crops cached here are read back component by component without following a link
    # (provenance.store.read_receipted), so a configured spelling through a symlink would refuse them.
    return (runtime_data_dir() / "parsed_docs").resolve()


def parse_document(document: DocumentRef, *, root: Path | None = None) -> ParsedDocument | None:
    """Text and figure crops for ``document``, or ``None`` when it cannot be parsed.

    ``None`` rather than an empty result, so the caller can tell "this document is unavailable"
    from "this document is empty" and say which. A model told nothing invents the difference.

    Never raises: a parser failure is a note to the model and an event in the ledger, not a dead
    run. The caller decides how to say so.
    """
    # The bytes parsed are the bytes checked (F26): read once without following a symlink, and
    # cached under THEIR hash -- a document changed after its receipt is parsed as it is now, with
    # a note, and never cached under the receipted hash.
    from dataclasses import replace

    from auto_research.provenance.store import read_receipted

    read = read_receipted(document.path, document.sha256, label=document.title or document.path.name)
    if read.data is None:
        return None
    actual = hashlib.sha256(read.data).hexdigest()
    base = Path(root).resolve() / actual if root is not None else cache_root() / actual
    parsed = _read_cache(base)
    if parsed is None:
        try:
            parsed = _run_docling(document, base, data=read.data)
        except Exception:  # noqa: BLE001 — an unparseable attachment degrades, never crashes
            shutil.rmtree(base, ignore_errors=True)
            return None
    if read.note:
        parsed = replace(parsed, text=f"{read.note}\n\n{parsed.text}")
    return parsed


def _read_cache(base: Path) -> ParsedDocument | None:
    """Reload a previous parse, or ``None`` if absent or half-written.

    The manifest is written last, so its presence is what marks the entry complete — a parse
    interrupted midway leaves no manifest and is simply redone.
    """
    manifest_path = base / "manifest.json"
    if not manifest_path.is_file():
        return None
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        text = (base / "text.md").read_text(encoding="utf-8")
    except (OSError, json.JSONDecodeError):
        return None
    figures = []
    for entry in manifest.get("figures", []):
        path = base / entry["file"]
        if path.is_file():
            figures.append(FigureRef.model_validate({**entry["ref"], "path": path}))
    return ParsedDocument(
        text=text, figures=tuple(figures), truncated=bool(manifest.get("truncated"))
    )


#: Third-party loggers pinned to WARNING before a parse runs.
#:
#: Docling and RapidOCR log at INFO by default — model paths, device selection, a tqdm weight-
#: loading bar, all ANSI-coloured. That lands in the run's ``backend.log``, which is otherwise one
#: structured line per event (``[HH:MM:SS] #seq $cost team L<level> event: message``) and is read
#: by an offline scoring pass, by ``ledger_grep``, and — as attempt 6 showed — sometimes by an
#: agent. Roughly 15 lines per parse and 6 papers per reviewer is ~90 unstructured, escape-laden
#: lines per run.
#:
#: Same remedy as the 2026-07-16 httpx/httpcore pin, which was applied after Telegram's bot token
#: (carried in the request URL) reached every backend.log at INFO. WARNING and above still pass,
#: so a parse that actually goes wrong still says so.
#: Note the capitalisation: RapidOCR names its logger ``"RapidOCR"``, not after its package. It
#: also builds that logger **at import time** with ``propagate = False`` and its own colour
#: handler, so the pin has to happen *after* the Docling import chain has pulled it in — setting a
#: level before then is silently overwritten by their own ``setLevel(INFO)``. Verified by reading
#: ``rapidocr/utils/log.py`` after a first attempt at the guessed name changed nothing.
_QUIET_LOGGERS = ("docling", "docling_ibm_models", "docling_core", "RapidOCR")


def _quiet_third_party_logs() -> None:
    """Pin the parser's own loggers so a parse contributes no INFO chatter to the run log.

    ``rapidocr`` is imported explicitly first, and that is the whole trick: it builds its logger
    at *module import* with an unconditional ``setLevel(INFO)``, and Docling imports it lazily
    during conversion — so a level set before that import is simply overwritten when it happens.
    Importing it here forces the one-time setup to run now, after which the pin sticks (Python
    caches the module, so Docling's later import re-runs nothing). Measured: without the explicit
    import the pin changes nothing at all, 15 stderr lines either way.
    """
    import logging

    try:
        import rapidocr  # noqa: F401 — imported for its import-time logger setup, not for use
    except ImportError:  # pragma: no cover — a Docling install without it is not ours to assume
        pass
    for name in _QUIET_LOGGERS:
        logging.getLogger(name).setLevel(logging.WARNING)


def _run_docling(document: DocumentRef, base: Path, *, data: bytes) -> ParsedDocument:
    """Convert with Docling and write the cache entry. Import is local: the orchestration
    environment must load without this dependency for every path that does not use it."""
    from docling.datamodel.pipeline_options import PdfPipelineOptions
    from docling.document_converter import DocumentConverter, PdfFormatOption
    from docling_core.types.doc import PictureItem

    from docling.datamodel.base_models import InputFormat

    # After the imports, never before: these loggers are constructed at import time.
    _quiet_third_party_logs()

    options = PdfPipelineOptions()
    options.images_scale = IMAGES_SCALE
    # Crops of the detected figure regions, taken from the rendered page — this is the setting
    # that makes a vector plot recoverable. Page images stay off: we send figures, not pages.
    options.generate_picture_images = True
    options.generate_page_images = False

    converter = DocumentConverter(
        format_options={InputFormat.PDF: PdfFormatOption(pipeline_options=options)}
    )
    # Parse the verified bytes from a private copy, never the live file the reference names.
    base.mkdir(parents=True, exist_ok=True)
    source = base / f"source{document.path.suffix.lower() or '.pdf'}"
    source.write_bytes(data)
    result = converter.convert(source)
    doc = result.document

    figures_dir = base / "figures"
    figures_dir.mkdir(parents=True, exist_ok=True)
    entries: list[dict] = []
    refs: list[FigureRef] = []
    for element, _level in doc.iterate_items():
        if not isinstance(element, PictureItem):
            continue
        if len(refs) >= MAX_FIGURES:
            break
        image = element.get_image(doc)
        if image is None:
            continue
        name = f"fig_{len(refs) + 1:03d}.png"
        path = figures_dir / name
        with path.open("wb") as handle:
            image.save(handle, "PNG")
        # Docling links a picture to its caption in the document graph, so the crop arrives
        # already labelled rather than matched by proximity after the fact.
        caption = (element.caption_text(doc) or "").strip()
        ref = FigureRef(
            path=path,
            caption=caption or f"Figure from {document.path.name} (no caption detected)",
            mime_type="image/png",
            produced_by=f"docling:{document.path.name}",
            created_at=datetime.now(UTC),
            sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
        )
        refs.append(ref)
        entries.append(
            {"file": f"figures/{name}", "ref": ref.model_dump(mode="json", exclude={"path"})}
        )

    text = doc.export_to_markdown()
    truncated = len(text) > MAX_TEXT_CHARS
    if truncated:
        cut = len(text) - MAX_TEXT_CHARS
        text = f"{text[:MAX_TEXT_CHARS]}\n\n[truncated: {cut} further characters not shown]"
    (base / "text.md").write_text(text, encoding="utf-8")

    # Written last: the manifest's presence is the completeness marker `_read_cache` trusts.
    (base / "manifest.json").write_text(
        json.dumps(
            {
                "source": str(document.path),
                "sha256": document.sha256,
                "parsed_at": datetime.now(UTC).isoformat(),
                "parser": _docling_version(),
                "images_scale": IMAGES_SCALE,
                "truncated": truncated,
                "figures": entries,
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    return ParsedDocument(text=text, figures=tuple(refs), truncated=truncated)


def _docling_version() -> str:
    """Version string stamped into the manifest; a figure with no recorded parser is not
    evidence a later reader can trust."""
    try:
        from importlib.metadata import version

        return f"docling {version('docling')}"
    except Exception:  # noqa: BLE001
        return "docling (version unavailable)"


def check_document_parser_ready() -> None:
    """Launch-time preflight: local document parsing works, for a run that will need it.

    Only checked when a configured role actually needs it — a member whose vendor accepts inline
    documents never parses anything locally, and a run without such a member should not be blocked
    on a dependency it will not use.

    Two failure tiers, deliberately different. A **missing dependency** is a hard failure: a run
    whose reviewer cannot read a paper is misconfigured, and finding out at the reviewer node
    wastes the whole analysis. **Absent model weights** is a *warning*: Docling downloads them on
    first use, so the run would still work — it would just stall mid-analysis on a ~400 MB fetch
    and depend on the network at a moment nothing else does. Warning rather than failure because
    it is satisfiable and unproven at the same time, which is what that tier exists for.
    """
    from auto_research.models import load_registry
    from auto_research.models.providers import provider_for
    from auto_research.preflight import PreflightWarning

    registry = load_registry()
    needs_parsing = False
    for role in registry.config.roles:
        for handle in registry.resolve(role).handles:
            config = handle.config
            if not getattr(config, "documents", False):
                continue
            try:
                vendor = getattr(provider_for(config.provider), "vendor", None)
            except Exception:  # noqa: BLE001 — a provider this run cannot build is not our check
                continue
            if vendor is not None and not vendor.accepts_inline_documents:
                needs_parsing = True
    if not needs_parsing:
        return

    try:
        import docling  # noqa: F401
    except ImportError as exc:
        raise ValueError(
            "a configured role is sent documents on a vendor with no usable file channel, so "
            "papers must be parsed locally, but `docling` is not installed. Install it with the "
            "CPU torch index (the CUDA build is multi-GB and buys nothing here):\n"
            "    pip install docling --extra-index-url https://download.pytorch.org/whl/cpu"
        ) from exc

    if not _weights_present():
        raise PreflightWarning(
            "docling is installed but its model weights are not cached yet, so the first paper a "
            "reviewer opens will stall on a ~400 MB download mid-analysis. Warm the cache before "
            "launching by parsing any PDF once."
        )


def _weights_present() -> bool:
    """Whether Docling's layout/table weights are already cached locally.

    Best-effort by design: this decides a *warning*, so a false negative costs one extra line at
    launch and a false positive costs a mid-run download — neither is worth a brittle check
    against the layout of somebody else's cache.
    """
    home = Path.home() / ".cache" / "huggingface" / "hub"
    if not home.is_dir():
        return False
    return any(entry.name.startswith("models--ds4sd") for entry in home.iterdir()) or any(
        entry.name.startswith("models--docling") for entry in home.iterdir()
    )
