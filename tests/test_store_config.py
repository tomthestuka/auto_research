"""The ``stores:`` block (``auto_research.store.config``): what it accepts, what it refuses, how it binds."""

from __future__ import annotations

import pytest
import yaml

from auto_research.config_paths import CONFIG_DIR_ENV, config_dir
from auto_research.store import config as store_config
from auto_research.store.config import LocalStore, RemoteStore

ADMIN = {"s3_access_key": "T_S3_KEY", "s3_secret_key": "T_S3_SECRET", "catalog_password": "T_PG_PASSWORD"}


@pytest.fixture(autouse=True)
def _unbound():
    store_config.reset()
    yield
    store_config.reset()


def _local(tmp_path, **fields) -> dict:
    entry = {"name": "db", "kind": "local", "root": str(tmp_path / "root"), "state_dir": str(tmp_path / "state"),
             "socket_dir": "/tmp/ars-x", "ports": {"s3": 18070, "iam": 18076, "catalog": 18432},
             "buckets": ["ixpe"], "admin_env": ADMIN}
    entry.update(fields)
    return {k: v for k, v in entry.items() if v is not None}


def _remote(**fields) -> dict:
    entry = {"name": "db", "kind": "remote", "endpoint": "https://s3.example.org", "buckets": ["ixpe"],
             "catalog_dsn": "host=pg.example.org port=5432 dbname=db user=ar_admin", "admin_env": ADMIN}
    entry.update(fields)
    return entry


def test_the_shipped_blocks_parse():
    for name in ("orchestration.yaml.example", "orchestration.yaml"):
        path = config_dir() / name
        if not path.exists():
            continue
        declared = store_config.stores(yaml.safe_load(path.read_text()))
        assert [s.name for s in declared] == ["db"], name


def test_a_local_and_a_remote_store_read_their_endpoints_and_secrets(tmp_path, monkeypatch):
    monkeypatch.setenv("T_S3_KEY", "k")
    monkeypatch.setenv("T_S3_SECRET", "s")
    monkeypatch.setenv("T_PG_PASSWORD", "p w")
    local, remote = store_config.stores({"stores": [_local(tmp_path), _remote(name="aws")]})
    assert isinstance(local, LocalStore) and isinstance(remote, RemoteStore)
    assert local.s3_endpoint == "http://127.0.0.1:18070" and local.iam_endpoint == "http://127.0.0.1:18076"
    assert local.iam_socket.parent == local.socket_dir and local.admin_s3() == ("k", "s")
    from psycopg.conninfo import conninfo_to_dict

    assert conninfo_to_dict(local.admin_dsn()) == {"host": "/tmp/ars-x", "port": "18432", "dbname": "db",
                                                   "user": "ar_admin", "password": "p w"}
    assert conninfo_to_dict(remote.admin_dsn(dbname="postgres"))["dbname"] == "postgres"
    assert remote.s3_endpoint == "https://s3.example.org"
    assert local.adapters["keys"] != remote.adapters["keys"]


def test_an_unset_secret_is_named(tmp_path, monkeypatch):
    monkeypatch.delenv("T_S3_SECRET", raising=False)
    monkeypatch.setenv("T_S3_KEY", "k")
    local = store_config.stores({"stores": [_local(tmp_path)]})[0]
    with pytest.raises(RuntimeError, match="T_S3_SECRET"):
        local.admin_s3()


@pytest.mark.parametrize("change, message", [
    ({"stores": None}, "list of at least one store"),
    ({"stores": []}, "list of at least one store"),
    ({"extra": {"surprise": 1}}, "Extra inputs are not permitted"),
    ({"buckets": ["IXPE"]}, "bucket name"),
    ({"buckets": ["ixpe", "ixpe"]}, "listed twice"),
    ({"buckets": []}, "at least 1"),
    ({"name": "my-db"}, "not an ASCII identifier"),
    ({"kind": "nfs"}, "does not match any of the expected tags"),
    ({"root": "relative/root"}, "not an absolute path"),
    ({"state_dir": "ROOT/state"}, "state_dir .* lies inside root"),
    ({"socket_dir": "/tmp/" + "s" * 100}, "too long"),
    ({"ports": {"s3": 2000, "iam": 2000, "catalog": 2001}}, "three different ports"),
    ({"ports": {"s3": 80, "iam": 2000, "catalog": 2001}}, "greater than or equal to 1024"),
    ({"admin_env": {**ADMIN, "s3_secret_key": "lower"}}, "not an environment variable name"),
    ({"catalog": {"database": "Bad-Name"}}, "lowercase Postgres name"),
])
def test_a_malformed_local_store_is_refused(tmp_path, change, message):
    if "extra" in change:
        block = {"stores": [_local(tmp_path, surprise=1)]}
    elif "stores" in change:
        block = change
    else:
        fields = {k: (v.replace("ROOT", str(tmp_path / "root")) if isinstance(v, str) else v)
                  for k, v in change.items()}
        block = {"stores": [_local(tmp_path, **fields)]}
    with pytest.raises(ValueError, match=message):
        store_config.stores(block)


@pytest.mark.parametrize("change, message", [
    ({"catalog_dsn": "host=h dbname=db user=u password=secret"}, "holds a password"),
    ({"catalog_dsn": "host=h user=u"}, "names no dbname"),
    ({"endpoint": "s3.example.org"}, "should match pattern"),
])
def test_a_malformed_remote_store_is_refused(change, message):
    with pytest.raises(ValueError, match=message):
        store_config.stores({"stores": [_remote(**change)]})


def test_two_stores_sharing_a_name_folder_or_port_are_refused(tmp_path):
    a = _local(tmp_path)
    with pytest.raises(ValueError, match="declared twice"):
        store_config.stores({"stores": [a, _remote()]})
    b = _local(tmp_path, name="db2", root=str(tmp_path / "root2"), state_dir=str(tmp_path / "state2"),
               socket_dir="/tmp/ars-y", ports={"s3": 18071, "iam": 18077, "catalog": 18432})
    with pytest.raises(ValueError, match="share a port"):
        store_config.stores({"stores": [a, b]})
    b["ports"] = {"s3": 18071, "iam": 18077, "catalog": 18433}
    b["state_dir"] = str(tmp_path / "state")
    with pytest.raises(ValueError, match="share a folder"):
        store_config.stores({"stores": [a, b]})


def test_the_block_binds_once_and_store_finds_by_name(tmp_path, monkeypatch):
    (tmp_path / "config").mkdir()
    path = tmp_path / "config" / "orchestration.yaml"
    path.write_text(yaml.safe_dump({"stores": [_local(tmp_path)]}))
    monkeypatch.setenv(CONFIG_DIR_ENV, str(tmp_path / "config"))
    assert store_config.store().name == "db"
    path.write_text(yaml.safe_dump({"stores": [_local(tmp_path, name="other")]}))
    assert store_config.store().name == "db", "the bound block changed under the process"
    with pytest.raises(ValueError, match="no store named 'other'"):
        store_config.store("other")
    store_config.reset()
    assert store_config.store("other").name == "other"
    store_config.bind({"stores": [_remote(name="aws")]})
    assert store_config.store("aws").kind == "remote"
