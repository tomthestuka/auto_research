"""The store contract: the data stores a run reads and writes, from the ``stores:`` block.

A store is bytes behind the S3 contract plus a Postgres catalog. ``local``: the runtime starts
versitygw (POSIX backend over ``root``, with its standalone IAM) and Postgres, all state in
``state_dir`` (``store.launch``). ``remote``: an S3 endpoint and a catalog DSN that already run.
Config holds the NAMES of the environment variables carrying the admin secrets, never a secret.

Bound once per process like the toolkit contract (``toolkit_contract``): ``stores()`` binds the
configured block on first use, ``bind`` binds a configuration held as data, ``reset`` unbinds.
"""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Annotated, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, ValidationError, field_validator, model_validator

from auto_research.config_paths import orchestration_yaml

#: The store a unit uses unless it names another (human ruling 2026-11-10: "obsdb" is renamed "db").
DEFAULT_STORE = "db"

#: The region every request is signed for (versitygw's default; a remote store sets its own).
LOCAL_REGION = "us-east-1"

#: A Unix socket path holds at most 107 bytes (``sun_path`` is 108 with its NUL).
SOCKET_PATH_MAX = 107

_BUCKET = re.compile(r"[a-z0-9][a-z0-9-]{1,61}[a-z0-9]")
_VARIABLE = re.compile(r"[A-Z_][A-Z0-9_]*")
_PG_NAME = re.compile(r"[a-z_][a-z0-9_]{0,62}")


class AdminEnv(BaseModel):
    """The environment variables holding a store's admin secrets, by name."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    s3_access_key: str = Field(description="Variable holding the S3 root (admin) access key id.")
    s3_secret_key: str = Field(description="Variable holding the S3 root (admin) secret key.")
    catalog_password: str = Field(description="Variable holding the catalog admin role's password.")

    @field_validator("s3_access_key", "s3_secret_key", "catalog_password")
    @classmethod
    def _is_a_variable_name(cls, name: str) -> str:
        if not _VARIABLE.fullmatch(name):
            raise ValueError(f"{name!r} is not an environment variable name (A-Z, 0-9, _)")
        return name

    def values(self) -> dict[str, str]:
        """``{field: secret}`` read from the process environment; raises naming every unset variable."""
        from dotenv import load_dotenv

        from auto_research.env import _ENV_FILE

        load_dotenv(_ENV_FILE, override=False)
        missing = [name for name in self.model_dump().values() if not os.environ.get(name)]
        if missing:
            raise RuntimeError(f"set {', '.join(missing)} in .env (the store's admin secrets)")
        return {field: os.environ[name] for field, name in self.model_dump().items()}


class StoreRoles(BaseModel):
    """Which store role each unit holds, until unit definitions name it (Phase 3 step 3): a unit whose
    name matches a ``producer`` pattern (``fnmatch``) is a producer, every other unit a reader."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    producer: tuple[str, ...] = Field(("analyst_worker_*",),
                                      description="Unit name patterns holding the producer role.")

    @field_validator("producer")
    @classmethod
    def _patterns(cls, patterns: tuple[str, ...]) -> tuple[str, ...]:
        if any(not p.strip() for p in patterns):
            raise ValueError("a producer pattern is empty")
        return patterns


class _Store(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str = Field(description="The store's name: receipts and unit definitions name it.")
    buckets: tuple[str, ...] = Field(min_length=1, description="The buckets the store serves.")
    synthetic_buckets: tuple[str, ...] = Field(
        (), description="Buckets holding synthetic data (a subset of `buckets`): a result that read an "
                        "object there is marked synthetic (`store.records`).")
    admin_env: AdminEnv
    roles: StoreRoles = Field(default_factory=StoreRoles, description="Which units are producers.")

    def role_of(self, unit: str) -> str:
        """The store role ``unit`` holds: ``producer`` when a ``roles.producer`` pattern matches it,
        else ``reader``."""
        from fnmatch import fnmatchcase

        return "producer" if any(fnmatchcase(unit, p) for p in self.roles.producer) else "reader"

    @field_validator("name")
    @classmethod
    def _name_is_an_identifier(cls, name: str) -> str:
        if not (name.isascii() and name.isidentifier()):
            raise ValueError(f"store name {name!r} is not an ASCII identifier")
        return name

    @field_validator("buckets")
    @classmethod
    def _bucket_names(cls, buckets: tuple[str, ...]) -> tuple[str, ...]:
        bad = [b for b in buckets if not _BUCKET.fullmatch(b)]
        if bad:
            raise ValueError(f"{', '.join(map(repr, bad))}: a bucket name is 3-63 lowercase letters, digits "
                             "and '-', starting and ending with a letter or digit")
        if len(set(buckets)) != len(buckets):
            raise ValueError("a bucket is listed twice")
        return buckets

    @model_validator(mode="after")
    def _synthetic_are_buckets(self) -> "_Store":
        unknown = [b for b in self.synthetic_buckets if b not in self.buckets]
        if unknown:
            raise ValueError(f"synthetic_buckets {', '.join(map(repr, unknown))} not in buckets; list each "
                             "synthetic bucket under buckets too")
        return self

    def admin_s3(self) -> tuple[str, str]:
        """The admin ``(access key id, secret key)``, read from the environment."""
        values = self.admin_env.values()
        return values["s3_access_key"], values["s3_secret_key"]

    def admin_client(self, service: str = "s3"):
        """A boto3 client signed with the admin key: ``s3`` at the store's endpoint, ``iam`` at a local
        store's IAM API (a remote store's: AWS's own)."""
        import boto3
        from botocore.config import Config

        key, secret = self.admin_s3()
        endpoint = self.s3_endpoint if service == "s3" else getattr(self, "iam_endpoint", None)
        return boto3.client(service, endpoint_url=endpoint, region_name=self.region, aws_access_key_id=key,
                            aws_secret_access_key=secret,
                            config=Config(s3={"addressing_style": "path"}, connect_timeout=5, read_timeout=30,
                                          retries={"max_attempts": 2}))


class CatalogSettings(BaseModel):
    """A local store's catalog database."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    database: str = Field("db", description="The catalog database's name.")
    admin_user: str = Field("ar_admin", description="The superuser initdb creates; the runtime's admin role.")

    @field_validator("database", "admin_user")
    @classmethod
    def _pg_name(cls, name: str) -> str:
        if not _PG_NAME.fullmatch(name):
            raise ValueError(f"{name!r} is not a lowercase Postgres name ([a-z_][a-z0-9_]*)")
        return name


class Ports(BaseModel):
    """The 127.0.0.1 ports of a local store's servers."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    s3: int = Field(ge=1024, le=65535, description="The S3 gateway.")
    iam: int = Field(ge=1024, le=65535, description="The IAM API (the runtime issues keys there).")
    catalog: int = Field(ge=1024, le=65535, description="Postgres.")

    @model_validator(mode="after")
    def _distinct(self) -> "Ports":
        if len({self.s3, self.iam, self.catalog}) != 3:
            raise ValueError("ports.s3, ports.iam and ports.catalog are three different ports")
        return self


class LocalStore(_Store):
    """A store whose servers the runtime starts: versitygw over ``root`` and Postgres, state in
    ``state_dir`` (``postgres/``, ``versions/``, ``iam/``, ``logs/``), sockets in ``socket_dir``."""

    kind: Literal["local"]
    root: Path = Field(description="The objects folder served as S3: each top folder is a bucket.")
    state_dir: Path = Field(description="The store's own state; its file system must take user xattrs.")
    socket_dir: Path = Field(description="A short folder for the IAM and Postgres Unix sockets, which no "
                                         "jail mounts.")
    ports: Ports
    catalog: CatalogSettings = Field(default_factory=CatalogSettings)

    @field_validator("root", "state_dir", "socket_dir")
    @classmethod
    def _absolute(cls, path: Path) -> Path:
        path = path.expanduser()
        if not path.is_absolute():
            raise ValueError(f"{str(path)!r} is not an absolute path")
        return Path(os.path.normpath(path))

    @model_validator(mode="after")
    def _placement(self) -> "LocalStore":
        # Compared as the folders they are: a symlink cannot alias one into another.
        paths = {name: Path(os.path.realpath(p)) for name, p in
                 (("root", self.root), ("state_dir", self.state_dir), ("socket_dir", self.socket_dir))}
        for a, pa in paths.items():
            for b, pb in paths.items():
                if a != b and pa.is_relative_to(pb):
                    raise ValueError(f"{a} {pa} lies inside {b} {pb}; give each its own folder")
        longest = max(len(str(p).encode()) for p in (self.iam_socket, self.catalog_socket))
        if longest > SOCKET_PATH_MAX:
            raise ValueError(f"socket_dir {self.socket_dir} is too long: its sockets' paths reach {longest} "
                             f"bytes, a Unix socket path holds {SOCKET_PATH_MAX}; choose a shorter folder")
        return self

    @property
    def s3_endpoint(self) -> str:
        return f"http://127.0.0.1:{self.ports.s3}"

    @property
    def iam_endpoint(self) -> str:
        return f"http://127.0.0.1:{self.ports.iam}"

    @property
    def region(self) -> str:
        return LOCAL_REGION

    @property
    def iam_socket(self) -> Path:
        """The IAM service's private endpoint, which the gateway resolves identities through."""
        return self.socket_dir / "iam.sock"

    @property
    def catalog_socket(self) -> Path:
        return self.socket_dir / f".s.PGSQL.{self.ports.catalog}"

    @property
    def access_log(self) -> Path:
        """The gateway's access log (requester key, operation, key, version id per request)."""
        return self.state_dir / "logs" / "access.log"

    @property
    def adapters(self) -> dict[str, str]:
        """What issues keys and what records reads, for the key (S4) and receipt (S5) adapters."""
        return {"keys": "versitygw_iam", "reads": "access_log"}

    def admin_dsn(self, dbname: str | None = None) -> str:
        """The admin role's libpq conninfo (password included), over the Unix socket; ``dbname``
        defaults to the catalog database."""
        from psycopg.conninfo import make_conninfo

        return make_conninfo(host=str(self.socket_dir), port=self.ports.catalog,
                             dbname=dbname or self.catalog.database, user=self.catalog.admin_user,
                             password=self.admin_env.values()["catalog_password"])


class RemoteStore(_Store):
    """A store already served elsewhere: an S3 endpoint and a Postgres catalog."""

    kind: Literal["remote"]
    endpoint: str = Field(pattern=r"^https?://\S+$", description="The S3 endpoint URL.")
    region: str = Field("us-east-1", min_length=1)
    catalog_dsn: str = Field(description="libpq conninfo naming host, dbname and user; the password comes "
                                         "from admin_env.catalog_password.")

    @field_validator("catalog_dsn")
    @classmethod
    def _dsn(cls, dsn: str) -> str:
        from psycopg.conninfo import conninfo_to_dict

        try:
            fields = conninfo_to_dict(dsn)
        except Exception as exc:  # noqa: BLE001 - psycopg raises ProgrammingError on a malformed string
            raise ValueError(f"catalog_dsn does not parse ({exc})") from exc
        if "password" in fields:
            raise ValueError("catalog_dsn holds a password; name its variable in admin_env.catalog_password")
        missing = [k for k in ("host", "dbname", "user") if k not in fields]
        if missing:
            raise ValueError(f"catalog_dsn names no {', '.join(missing)}")
        return dsn

    @property
    def s3_endpoint(self) -> str:
        return self.endpoint

    @property
    def adapters(self) -> dict[str, str]:
        return {"keys": "aws_sts", "reads": "none"}

    def admin_dsn(self, dbname: str | None = None) -> str:
        """``catalog_dsn`` with the admin password (and ``dbname``, when given)."""
        from psycopg.conninfo import make_conninfo

        extra = {"dbname": dbname} if dbname else {}
        return make_conninfo(self.catalog_dsn, password=self.admin_env.values()["catalog_password"], **extra)


Store = Annotated[LocalStore | RemoteStore, Field(discriminator="kind")]
_STORES = TypeAdapter(list[Store])


def _parse(config: dict, where: str) -> list[LocalStore | RemoteStore]:
    block = config.get("stores")
    if not isinstance(block, list) or not block:
        raise ValueError(f"{where}: `stores:` is a list of at least one store (found "
                         f"{type(block).__name__ if not isinstance(block, list) else 'an empty list'})")
    try:
        stores = _STORES.validate_python(block)
    except ValidationError as exc:
        raise ValueError(f"{where}: `stores:` is invalid: {exc}") from exc
    names = [s.name for s in stores]
    if len(set(names)) != len(names):
        raise ValueError(f"{where}: a store name is declared twice ({', '.join(names)})")
    local = [s for s in stores if isinstance(s, LocalStore)]
    for i, a in enumerate(local):
        for b in local[i + 1:]:
            mine = [Path(os.path.realpath(p)) for p in (a.root, a.state_dir, a.socket_dir)]
            theirs = [Path(os.path.realpath(p)) for p in (b.root, b.state_dir, b.socket_dir)]
            if any(p.is_relative_to(q) or q.is_relative_to(p) for p in mine for q in theirs):
                raise ValueError(f"{where}: stores {a.name} and {b.name} share a folder; each needs its own "
                                 "root, state_dir and socket_dir")
            if {a.ports.s3, a.ports.iam, a.ports.catalog} & {b.ports.s3, b.ports.iam, b.ports.catalog}:
                raise ValueError(f"{where}: stores {a.name} and {b.name} share a port")
    return stores


#: The stores this process runs on, bound by the first read of the configured file (``reset`` unbinds).
_BOUND: list[LocalStore | RemoteStore] | None = None


def stores(config: dict | None = None) -> list[LocalStore | RemoteStore]:
    """The declared stores. Without ``config``: the process's bound block, bound on first use from the
    configured ``orchestration.yaml``. With ``config``: that configuration's block, parsed, never bound.
    Raises ``ValueError`` when the block is absent or invalid."""
    global _BOUND
    if config is not None:
        return _parse(config, "orchestration config")
    if _BOUND is None:
        path = orchestration_yaml()
        try:
            with path.open("r", encoding="utf-8") as handle:
                data = yaml.safe_load(handle) or {}
        except OSError as exc:
            raise ValueError(f"{path}: cannot read the stores ({exc})") from exc
        _BOUND = _parse(data, str(path))
    return _BOUND


def admin_env_names(config: dict | None = None) -> frozenset[str]:
    """Every environment variable naming a store's admin secret (each declared store's ``admin_env``):
    what no process acting for a model may inherit (an author harness, a REPL started from this
    process's environment). With no ``stores:`` block, none."""
    try:
        declared = stores(config)
    except ValueError:
        return frozenset()
    return frozenset(name for s in declared for name in s.admin_env.model_dump().values())


def without_admin_secrets(env: Mapping[str, str]) -> dict[str, str]:
    """``env`` without any store admin secret (:func:`admin_env_names`)."""
    names = admin_env_names()
    return {key: value for key, value in env.items() if key not in names}


def store(name: str = DEFAULT_STORE, config: dict | None = None) -> LocalStore | RemoteStore:
    """The declared store ``name`` (default ``db``); raises ``ValueError`` when none has that name."""
    for entry in stores(config):
        if entry.name == name:
            return entry
    raise ValueError(f"no store named {name!r} is declared in `stores:`")


def bind(config: dict) -> list[LocalStore | RemoteStore]:
    """Bind the process to ``config``'s ``stores:`` block (validated) and return it."""
    global _BOUND
    _BOUND = _parse(config, "orchestration config")
    return _BOUND


def reset() -> None:
    """Unbind the process's stores; the next read binds the configured block again."""
    global _BOUND
    _BOUND = None
