"""The human's ingest of files already on disk as store products (item S11; human ruling 3, 2026-11-11).

Files a human made outside the pipeline (derived data sets) enter a store once, each set as one
product of a fixed recipe, so a tool reads them by record like any other product:

```
mapping (YAML, the human's)          plan(store, mapping)                 ingest(store, planned, conn=, s3=)
  products:                            each pattern → the files below its    claim (as the human's login)
    - bucket, kind, subject,           folder, each hashed (SHA-256)          → PUT each file create-only
      folder, files: [pattern ...]     recipe = {kind, subject, TOOL, "1",      <kind>/<subject>/<short hash>[.<n>]/<path>
                                         settings {folder, files {path: sha256}}, inputs []}
                                                                               → recipe.json last → commit
```

Domain-neutral: bucket, kind and subject are the mapping's strings; a file keeps its path below the
folder; a product's ``settings`` (optional) are facts the human records about the files, kept in the
recipe beside ``folder`` and ``files``. The recipe's tool is :data:`TOOL`, never a manifest key, so an ingested product never becomes
"current" by itself (the reconciler promotes only a declared toolkit's products); the human may move a
pointer onto it (``move-current``). The settings name each file with its SHA-256: other bytes under the
same names are another product. The provenance fields a product carries (toolkit commit, environment)
read :data:`PROVENANCE`: no toolkit code made these bytes. The reconciler verifies the products like any
other (``python -m auto_research.store.reconcile``).
"""

from __future__ import annotations

import base64
import glob
import hashlib
import io
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, StrictStr, field_validator

#: The recipe's tool: the human's ingest, never a manifest key.
TOOL = "auto_research.store.ingest"
ALGORITHM_VERSION = "1"
#: What an ingested product records as its toolkit commit and environment: none made it.
PROVENANCE = "human-ingest"
#: The hex digits of a product folder's short hash (lengthened while another product of the same
#: kind and subject shares it), as the toolkit's helper names its folders.
SHORT_HASH = 8
#: S3's largest single PUT; a larger file goes up in parts of :data:`PART_SIZE`, each with its SHA-256
#: (a composite checksum: the store checks each part; the reconciler hashes the whole object itself).
SINGLE_PUT_MAX = 5 * 1024 ** 3
PART_SIZE = 256 * 1024 ** 2
#: How long one request may take: a large upload's completion assembles its parts in the gateway's folder
#: (measured on NFS: 33 s for 9.7 GB), and a timed-out completion retried answers without its version.
READ_TIMEOUT_S = 3600
#: The recipe settings the ingest itself writes; a product's own ``settings`` take other names.
RESERVED_SETTINGS = ("folder", "files")
_CHUNK = 1 << 20


class IngestError(RuntimeError):
    """A mapping, a file or the store refused the ingest; the message names what to do."""


def _plain(value: str, what: str) -> str:
    if not value or value != value.strip() or "/" in value or value in (".", ".."):
        raise ValueError(f"{what} is one plain word (no '/', no surrounding spaces, not . or ..), not {value!r}")
    return value


class IngestProduct(BaseModel):
    """One product of the mapping: ``files`` are paths or glob patterns relative to ``folder``."""
    model_config = ConfigDict(extra="forbid", frozen=True)

    bucket: str
    kind: str
    subject: str
    folder: Path
    files: tuple[str, ...]
    settings: dict[str, StrictStr] = {}

    @field_validator("settings")
    @classmethod
    def _own_names(cls, value: dict) -> dict:
        taken = [k for k in value if k in RESERVED_SETTINGS]
        if taken:
            raise ValueError(f"settings name {taken}, which the ingest writes itself; name them otherwise")
        return value

    @field_validator("kind", "subject")
    @classmethod
    def _word(cls, value: str, info) -> str:
        return _plain(value, info.field_name)

    @field_validator("folder")
    @classmethod
    def _absolute(cls, value: Path) -> Path:
        if not value.is_absolute():
            raise ValueError(f"folder is an absolute path, not {value}")
        return value

    @field_validator("files")
    @classmethod
    def _patterns(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if not value:
            raise ValueError("files lists at least one path or pattern")
        for pattern in value:
            if not pattern.strip() or Path(pattern).is_absolute() or ".." in Path(pattern).parts:
                raise ValueError(f"a files entry is a path or pattern inside the folder, not {pattern!r}")
        return value


class IngestMapping(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    products: tuple[IngestProduct, ...]

    @field_validator("products")
    @classmethod
    def _some(cls, value):
        if not value:
            raise ValueError("products lists at least one product")
        return value


def load_mapping(path: Path | str) -> IngestMapping:
    """The mapping file (YAML) as an :class:`IngestMapping`; raises :class:`IngestError` naming the fault."""
    import yaml
    from pydantic import ValidationError

    try:
        data = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
        return IngestMapping.model_validate(data)
    except (OSError, yaml.YAMLError, ValidationError) as exc:
        raise IngestError(f"mapping {path}: {exc}") from None


@dataclass(frozen=True)
class Planned:
    """One product as it will be stored: ``files`` maps the path below the folder to (file, sha256, size)."""
    bucket: str
    kind: str
    subject: str
    folder: Path
    files: dict[str, tuple[Path, str, int]]
    recipe: dict[str, Any]


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as handle:
        while chunk := handle.read(_CHUNK):
            h.update(chunk)
    return h.hexdigest()


def plan(store, mapping: IngestMapping) -> list[Planned]:
    """Each product of ``mapping`` resolved, hashed and checked against ``store``; nothing is written.
    Refused: a bucket the store does not declare, a folder that is not one, a pattern matching no
    regular file, a file outside its folder (a link included), a file named ``recipe.json``, a recipe two
    products share."""
    from auto_research.store.reconcile import RECIPE_FILE

    out: list[Planned] = []
    for product in mapping.products:
        where = f"{product.bucket}/{product.kind}/{product.subject}"
        if product.bucket not in store.buckets:
            raise IngestError(f"{where}: name a bucket of store {store.name} ({', '.join(store.buckets)})")
        folder = product.folder.resolve()
        if not folder.is_dir():
            raise IngestError(f"{where}: {product.folder} is not a folder")
        files: dict[str, tuple[Path, str, int]] = {}
        for pattern in product.files:
            # Relative to the folder, so a folder name holding glob characters is taken literally.
            matched = [folder / m for m in sorted(glob.glob(pattern, root_dir=folder, recursive=True))]
            matched = [p for p in matched if p.is_file()]
            if not matched:
                raise IngestError(f"{where}: {pattern!r} matches no file below {folder}")
            for path in matched:
                if path.is_symlink() or not path.resolve().is_relative_to(folder):
                    raise IngestError(f"{where}: {path} lies outside {folder} or is a link; name the file itself")
                rel = path.relative_to(folder).as_posix()
                if Path(rel).name == RECIPE_FILE:
                    raise IngestError(f"{where}: {rel}: {RECIPE_FILE} names the product's own record; rename it")
                size = path.stat().st_size
                if rel not in files:
                    files[rel] = (path, _sha256(path), size)
        recipe = {"kind": product.kind, "subject": product.subject, "tool": TOOL,
                  "algorithm_version": ALGORITHM_VERSION,
                  "settings": {**product.settings, "folder": str(folder),
                               "files": {rel: sha for rel, (_, sha, _) in sorted(files.items())}},
                  "inputs": []}
        if any(p.recipe == recipe for p in out):
            raise IngestError(f"{where}: the same files are listed twice (a recipe names no bucket)")
        out.append(Planned(product.bucket, product.kind, product.subject, folder, files, recipe))
    return out


def client(store):
    """The ingest's S3 client: the admin key, path-style, :data:`READ_TIMEOUT_S` per request and no retry
    (a retried completion is answered without its version id)."""
    import boto3
    from botocore.config import Config

    key, secret = store.admin_s3()
    return boto3.client("s3", endpoint_url=store.s3_endpoint, region_name=store.region, aws_access_key_id=key,
                        aws_secret_access_key=secret,
                        config=Config(s3={"addressing_style": "path"}, connect_timeout=10,
                                      read_timeout=READ_TIMEOUT_S, retries={"total_max_attempts": 1}))


def _code(exc) -> str:
    return str(getattr(exc, "response", {}).get("Error", {}).get("Code", ""))


async def _short(conn, recipe_hash: str, kind: str, subject: str) -> str:
    """The shortest prefix of ``recipe_hash``, from :data:`SHORT_HASH` digits, no other product of
    (kind, subject) shares (the toolkit helper's rule)."""
    cur = await conn.execute("SELECT recipe_hash FROM catalog.product WHERE kind = %s AND subject = %s "
                             "AND recipe_hash <> %s", (kind, subject, recipe_hash))
    others = [r[0] for r in await cur.fetchall()]
    n = SHORT_HASH
    while any(o.startswith(recipe_hash[:n]) for o in others):
        n += 1
    return recipe_hash[:n]


def _put_parts(s3, bucket: str, key: str, path: Path, sha: str, meta: dict, stop=None) -> str | None:
    """A multipart upload of ``path``, create-only, each part with its SHA-256; the version id, or None
    when the store's answer names none (the key already exists, 412, or an answer without its version):
    the caller then reads what the key holds. The bytes sent must hash to ``sha``, else the upload is
    aborted; so is it when ``stop`` (a ``threading.Event``) is set between parts."""
    from botocore.exceptions import ClientError

    from auto_research.store.reconcile import META_SHA256

    upload = s3.create_multipart_upload(Bucket=bucket, Key=key, Metadata={**meta, META_SHA256: sha},
                                        ChecksumAlgorithm="SHA256")["UploadId"]
    try:
        whole, parts = hashlib.sha256(), []
        with open(path, "rb") as handle:
            while chunk := handle.read(PART_SIZE):
                if stop is not None and stop.is_set():
                    raise IngestError(f"{path}: the ingest was stopped during its upload")
                whole.update(chunk)
                digest = base64.b64encode(hashlib.sha256(chunk).digest()).decode("ascii")
                answer = s3.upload_part(Bucket=bucket, Key=key, UploadId=upload, PartNumber=len(parts) + 1,
                                        Body=chunk, ChecksumAlgorithm="SHA256", ChecksumSHA256=digest)
                parts.append({"ETag": answer["ETag"], "PartNumber": len(parts) + 1, "ChecksumSHA256": digest})
        if whole.hexdigest() != sha:
            raise IngestError(f"{path} changed during its upload; ingest again")
        try:
            done = s3.complete_multipart_upload(Bucket=bucket, Key=key, UploadId=upload, IfNoneMatch="*",
                                                MultipartUpload={"Parts": parts})
        except ClientError as exc:
            if _code(exc) not in ("PreconditionFailed", "412"):
                raise
            try:
                s3.abort_multipart_upload(Bucket=bucket, Key=key, UploadId=upload)
            except ClientError:
                pass  # nothing left to abort
            return None
        return done.get("VersionId")
    except BaseException:
        try:
            s3.abort_multipart_upload(Bucket=bucket, Key=key, UploadId=upload)
        except Exception:  # noqa: BLE001 - the upload's own failure is the one raised
            pass
        raise


def _put(s3, bucket: str, key: str, source: Path | bytes, sha: str, size: int, meta: dict, stop=None) -> dict:
    """Store ``source`` (a file, or bytes) at ``key`` create-only with its SHA-256 (checked by the store) and ``meta``. A key
    already holding the same bytes (this production's earlier attempt) is reused; other bytes raise."""
    from botocore.exceptions import ClientError

    from auto_research.store.reconcile import META_SHA256

    if isinstance(source, Path) and size > SINGLE_PUT_MAX:
        version = _put_parts(s3, bucket, key, source, sha, meta, stop)
        if version is not None:
            return {"key": key, "version_id": version, "sha256": sha, "size": size}
        return _existing_same(s3, bucket, key, sha, size)
    try:
        with (open(source, "rb") if isinstance(source, Path) else io.BytesIO(source)) as body:
            done = s3.put_object(Bucket=bucket, Key=key, Body=body, IfNoneMatch="*", ChecksumAlgorithm="SHA256",
                                 ChecksumSHA256=base64.b64encode(bytes.fromhex(sha)).decode("ascii"),
                                 Metadata={**meta, META_SHA256: sha})
        return {"key": key, "version_id": done["VersionId"], "sha256": sha, "size": size}
    except ClientError as exc:
        if _code(exc) not in ("PreconditionFailed", "412"):
            raise
    return _existing_same(s3, bucket, key, sha, size)


def _existing_same(s3, bucket: str, key: str, sha: str, size: int) -> dict:
    """The latest version at ``key`` when it holds these bytes (this production's earlier attempt);
    other bytes raise."""
    from auto_research.store.reconcile import META_SHA256

    listing = s3.list_object_versions(Bucket=bucket, Prefix=key)
    latest = [v for v in listing.get("Versions", []) if v["Key"] == key and v.get("IsLatest")]
    if latest:
        head = s3.head_object(Bucket=bucket, Key=key, VersionId=latest[0]["VersionId"])
        if (head.get("Metadata") or {}).get(META_SHA256) == sha and int(head["ContentLength"]) == size:
            return {"key": key, "version_id": latest[0]["VersionId"], "sha256": sha, "size": size}
    raise IngestError(f"{bucket}/{key} already holds other bytes; ingest again (a new claim writes a new folder)")


async def ingest_one(store, planned: Planned, *, conn, s3) -> dict:
    """Store ``planned`` as one product, as the login ``conn`` holds (the human's): claim, upload each
    file create-only with the five metadata entries, ``recipe.json`` last, commit. A recipe the catalog
    already holds is answered as ``reused``. Any failure after the claim gives it up with the reason
    (``catalog.fail``) and raises :class:`IngestError`. Returns ``{status, recipe_hash, record, objects}``."""
    import asyncio
    import threading

    from psycopg.types.json import Jsonb

    from auto_research.store import reconcile as rc

    keys = ("store", "bucket", "key", "version_id")
    where = f"{planned.bucket}/{planned.kind}/{planned.subject}"
    # Every file hashed again before the claim: a file changed since the plan stops the ingest before
    # anything is stored (each upload then carries its SHA-256, which the store checks). A file going up
    # in parts is compared whole during its upload instead.
    changed = [rel for rel, (path, sha, size) in sorted(planned.files.items())
               if size <= SINGLE_PUT_MAX and _sha256(path) != sha]
    if changed:
        raise IngestError(f"{where}: {', '.join(changed)} changed since the plan hashed it; ingest again")
    claim = (await (await conn.execute("SELECT catalog.claim(%s)", (Jsonb(planned.recipe),))).fetchone())[0]
    status, recipe_hash = claim["status"], claim["recipe_hash"]
    if status == "reuse":
        markers = [o for o in claim["objects"] if o["key"].endswith("/" + rc.RECIPE_FILE)
                   and o["key"].count("/") == 3]
        if len(markers) != 1 or markers[0]["bucket"] != planned.bucket:
            raise IngestError(f"{where}: the catalog holds this recipe ({recipe_hash[:12]}) otherwise than one "
                              f"{rc.RECIPE_FILE} in bucket {planned.bucket}")
        return {"status": "reused", "recipe_hash": recipe_hash, "record": {k: markers[0][k] for k in keys},
                "objects": claim["objects"]}
    if status != "claimed":
        raise IngestError(f"{where}: the catalog holds this recipe ({recipe_hash[:12]}) as {status}"
                          f"{': ' + str(claim.get('reason')) if claim.get('reason') else ''}"
                          f"{' until ' + str(claim.get('lease_until')) if status == 'in_progress' else ''}")
    stop = threading.Event()  # set on any way out, so an upload thread an interrupt left running aborts
    try:
        generation = int(claim["generation"])
        prefix = f"{planned.kind}/{planned.subject}/{await _short(conn, recipe_hash, planned.kind, planned.subject)}"
        prefix += f".{generation}" if generation > 1 else ""
        meta = {rc.META_RECIPE_HASH: recipe_hash, rc.META_PRODUCER: claim["owner"],
                rc.META_TOOLKIT_COMMIT: PROVENANCE, rc.META_ENV_FINGERPRINT: PROVENANCE}
        stored = []
        for rel, (path, sha, size) in sorted(planned.files.items()):
            stored.append(await asyncio.to_thread(_put, s3, planned.bucket, f"{prefix}/{rel}", path, sha, size,
                                                  meta, stop))
        body = rc.RecipeFile(recipe_hash=recipe_hash, generation=generation, recipe=planned.recipe,
                             objects=stored).model_dump_json(indent=1).encode("utf-8")
        marker = await asyncio.to_thread(_put, s3, planned.bucket, f"{prefix}/{rc.RECIPE_FILE}", body,
                                         hashlib.sha256(body).hexdigest(), len(body), meta)
        objects = [{"store": store.name, "bucket": planned.bucket, **o} for o in [*stored, marker]]
        await conn.execute("SELECT catalog.commit(%s, %s, %s, %s)",
                           (recipe_hash, Jsonb(objects), PROVENANCE, PROVENANCE))
    except BaseException as exc:  # the claim is given up with the reason (an interrupt too), and the fault raised
        stop.set()
        try:
            await conn.execute("SELECT catalog.fail(%s, %s)", (recipe_hash, f"{type(exc).__name__}: {exc}"[:2000]))
        except Exception:  # noqa: BLE001 - the ingest's own failure is the one raised
            pass
        if isinstance(exc, IngestError) or not isinstance(exc, Exception):
            raise
        raise IngestError(f"{where}: the store refused the ingest ({type(exc).__name__}: {exc}); the claim is "
                          "given up, so the same mapping can be ingested again") from exc
    return {"status": "stored", "recipe_hash": recipe_hash, "record": {k: objects[-1][k] for k in keys},
            "objects": objects}


def describe(planned: Planned) -> dict:
    """What :func:`plan` resolved, for the human's check (no bytes, no secrets)."""
    return {"bucket": planned.bucket, "kind": planned.kind, "subject": planned.subject,
            "folder": str(planned.folder),
            "settings": {k: v for k, v in planned.recipe["settings"].items() if k not in RESERVED_SETTINGS}, "files": {rel: {"sha256": sha, "size": size}
                                                     for rel, (_, sha, size) in sorted(planned.files.items())}}
