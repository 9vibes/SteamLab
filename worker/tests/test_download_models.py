import hashlib
import io
import urllib.request
from types import SimpleNamespace

import pytest

from worker import download_models as models


class Response(io.BytesIO):
    def geturl(self):
        return "https://media.githubusercontent.com/model"


def install_response(monkeypatch, contents):
    calls = []

    def open_response(request, timeout):
        calls.append((request, timeout))
        return Response(contents)

    monkeypatch.setattr(models.urllib.request, "build_opener", lambda *_: SimpleNamespace(open=open_response))
    monkeypatch.setattr(models.time, "sleep", lambda _: None)
    return calls


def test_pinned_lfs_oids():
    assert len(models.COMMIT) == 40
    assert models.MODELS[0][2:] == (
        "8f2383e4dd3cfbb4553ea8718107fc0423210dc964f9f4280604804ed2552fa4", 232589)
    assert models.MODELS[1][2:] == (
        "0ba9fbfa01b5270c96627c4ef784da859931e02f04419c829e83484087c34e79", 38696353)


def test_atomic_verified_download(monkeypatch, tmp_path):
    data = b"verified bytes"
    calls = install_response(monkeypatch, data)
    target = tmp_path / "model.onnx"
    target.write_bytes(b"old")
    original_replace = models.os.replace

    def replace(source, destination):
        assert target.read_bytes() == b"old"
        assert source.read_bytes() == data
        original_replace(source, destination)

    monkeypatch.setattr(models.os, "replace", replace)
    models.download("https://example.com/model", target, sha256=hashlib.sha256(data).hexdigest(),
                    size=len(data), max_bytes=len(data))
    assert target.read_bytes() == data
    assert len(calls) == 1
    assert list(tmp_path.iterdir()) == [target]


@pytest.mark.parametrize("kwargs", [
    {"sha256": "0" * 64}, {"size": 100}, {"max_bytes": 2},
])
def test_bad_download_retries_without_replacing_existing(monkeypatch, tmp_path, kwargs):
    calls = install_response(monkeypatch, b"not a model")
    target = tmp_path / "model.onnx"
    target.write_bytes(b"existing")
    with pytest.raises(RuntimeError, match="Verified asset download failed"):
        models.download("https://example.com/model", target, **kwargs)
    assert len(calls) == 3
    assert target.read_bytes() == b"existing"
    assert list(tmp_path.iterdir()) == [target]


def test_https_only_and_redirect_downgrade(tmp_path):
    with pytest.raises(ValueError, match="HTTPS"):
        models.download("http://example.com/model", tmp_path / "model")
    with pytest.raises(ValueError, match="Insecure"):
        models.HTTPSOnlyRedirect().redirect_request(
            urllib.request.Request("https://example.com"), None, 302, "", {}, "http://example.com")


def test_retry_eventually_succeeds(monkeypatch, tmp_path):
    calls = 0

    def open_response(*_, **__):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise TimeoutError("secret URL must not be surfaced")
        return Response(b"okay")

    monkeypatch.setattr(models.urllib.request, "build_opener", lambda *_: SimpleNamespace(open=open_response))
    monkeypatch.setattr(models.time, "sleep", lambda _: None)
    models.download("https://example.com", tmp_path / "model", size=4)
    assert calls == 2


def test_deadline_cleans_temporary_file(monkeypatch, tmp_path):
    install_response(monkeypatch, b"bytes")
    clock = iter([0, 181])
    monkeypatch.setattr(models.time, "monotonic", lambda: next(clock))
    with pytest.raises(RuntimeError):
        models.download("https://example.com", tmp_path / "model", attempts=1)
    assert not list(tmp_path.iterdir())


def test_all_models_and_licenses_use_pinned_commit(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(models, "download", lambda url, target, **kw: calls.append((url, target, kw)))
    models.download_all(tmp_path)
    assert len(calls) == 5
    assert all(models.COMMIT in url and url.startswith("https://") for url, _, _ in calls)
    assert sum(target.name.endswith(".LICENSE") for _, target, _ in calls) == 3
    assert all(kw["max_bytes"] == kw["size"] for _, target, kw in calls if target.suffix == ".onnx")
