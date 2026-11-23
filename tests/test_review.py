"""Tests for the review-package endpoint (GET /volumes/{volume}/review.zip)."""
from __future__ import annotations

import hashlib
import io
import json
import threading
import zipfile

import pytest
from fastapi.testclient import TestClient

from app.db import Store
from app.ingest import VolumeFiles, replace_volume
from app.main import create_app
from app.review import ReviewError, build_review_zip
from tests.warcbuild import b32, response_record, revisit_record

URL = "http://gazetteer.example.cn/notice.html"
V1 = "馆藏公告 第一版：本馆藏有民国县志三十种。".encode()
V2 = "馆藏公告 第二版：新增解放后府志十二种。".encode()
V3 = "馆藏公告 第三版：数字化平台上线。".encode()

T1 = "2024-03-01T09:00:00Z"
T2 = "2024-06-01T09:00:00Z"
T3 = "2024-09-01T09:00:00Z"
T4 = "2024-12-01T09:00:00Z"
T5 = "2025-03-01T09:00:00Z"


@pytest.fixture()
def client(tmp_path):
    app = create_app(str(tmp_path / "data"))
    with TestClient(app) as test_client:
        test_client.data_dir = tmp_path / "data"
        yield test_client


def put(client, volume, payload: bytes, filename="vol.warc"):
    return client.put(f"/volumes/{volume}/warc",
                      files={"file": (filename, payload, "application/warc")})


def get_review(client, volume):
    return client.get(f"/volumes/{volume}/review.zip")


def unzip(resp) -> tuple[dict, dict[str, bytes]]:
    assert resp.status_code == 200, resp.text
    assert resp.headers["content-type"] == "application/zip"
    assert "attachment" in resp.headers["content-disposition"]
    with zipfile.ZipFile(io.BytesIO(resp.content)) as zf:
        members = {name: zf.read(name) for name in zf.namelist()}
    manifest = json.loads(members.pop("manifest.json"))
    return manifest, members


def bodies_in(members: dict[str, bytes]) -> dict[str, bytes]:
    return {name[len("bodies/"):-len(".bin")]: data
            for name, data in members.items() if name.startswith("bodies/")}


# ------------------------------------------------------------- structure --

def test_review_package_marks_all_three_evidence_kinds(client):
    # vol2: t3 revisit evidenced inside vol2 itself (V3 at t4),
    #       t4 revisit evidenced only by vol1 (V1 at t1) -> attached,
    #       t5 revisit evidenced nowhere -> missing
    put(client, "vol1", response_record(URL, T1, V1))
    warc2 = (response_record(URL, T2, V3)
             + revisit_record(URL, T3, b32(V3))
             + revisit_record(URL, T4, b32(V1))
             + revisit_record(URL, T5, b32(V2)))
    put(client, "vol2", warc2)

    manifest, members = unzip(get_review(client, "vol2"))

    assert manifest["volume"] == "vol2"
    assert manifest["volume_sha256"] == hashlib.sha256(warc2).hexdigest()
    # 原卷: the original volume is in the package, byte-for-byte
    assert members["vol2.warc"] == warc2

    by_ts = {r["ts"]: r for r in manifest["revisits"]}
    assert set(by_ts) == {T3, T4, T5}
    assert by_ts[T3]["evidence"] == "in_volume"
    assert "body" not in by_ts[T3]
    assert by_ts[T4]["evidence"] == "attached"
    assert by_ts[T4]["body"] == f"bodies/{b32(V1)}.bin"
    assert by_ts[T5]["evidence"] == "missing"
    assert "body" not in by_ts[T5]
    for entry in manifest["revisits"]:
        assert set(entry) <= {"url", "ts", "digest", "evidence", "body"}
        assert entry["url"] == URL

    # only the externally-evidenced digest is attached, as raw HTTP body
    bodies = bodies_in(members)
    assert bodies == {b32(V1): V1}
    assert not bodies[b32(V1)].startswith(b"HTTP/")


def test_review_package_dedups_bodies_by_digest(client):
    put(client, "vol1", response_record(URL, T1, V1))
    warc2 = (revisit_record(URL, T2, b32(V1))
             + revisit_record(URL, T3, b32(V1)))  # same digest twice
    put(client, "vol2", warc2)
    manifest, members = unzip(get_review(client, "vol2"))
    assert [r["evidence"] for r in manifest["revisits"]] == ["attached", "attached"]
    assert list(bodies_in(members)) == [b32(V1)]  # 同摘要只附一份


def test_review_package_is_deterministic_for_same_state(client):
    put(client, "vol1", response_record(URL, T1, V1))
    put(client, "vol2", revisit_record(URL, T2, b32(V1)))
    first = get_review(client, "vol2").content
    second = get_review(client, "vol2").content
    assert first == second


def test_review_package_unknown_volume_is_404(client):
    resp = get_review(client, "nope")
    assert resp.status_code == 404
    assert "nope" in resp.text


def test_review_package_reflects_state_after_volume_swap(client):
    put(client, "vol1", response_record(URL, T1, V1) + response_record(URL, T2, V2))
    put(client, "vol2", revisit_record(URL, T3, b32(V2)) + revisit_record(URL, T4, b32(V1)))
    manifest, members = unzip(get_review(client, "vol2"))
    assert [r["evidence"] for r in manifest["revisits"]] == ["attached", "attached"]
    assert bodies_in(members) == {b32(V2): V2, b32(V1): V1}
    # swap vol1 for a volume without the V2 body: t3 loses its evidence
    put(client, "vol1", response_record(URL, T1, V1))
    manifest, members = unzip(get_review(client, "vol2"))
    assert [r["evidence"] for r in manifest["revisits"]] == ["missing", "attached"]
    assert bodies_in(members) == {b32(V1): V1}


def test_review_package_of_volume_without_revisits_has_empty_manifest(client):
    warc1 = response_record(URL, T1, V1)
    put(client, "vol1", warc1)
    manifest, members = unzip(get_review(client, "vol1"))
    assert manifest["revisits"] == []
    assert members == {"vol1.warc": warc1}


# ------------------------------------------------------- missing volumes --

def test_missing_lent_volume_file_refuses_delivery_and_names_volume(client):
    put(client, "vol1", response_record(URL, T1, V1))
    (client.data_dir / "volumes" / "vol1.warc").unlink()
    resp = get_review(client, "vol1")
    assert resp.status_code == 409
    assert resp.json()["detail"]["volumes"] == [
        {"volume": "vol1", "problem": "original volume file missing"}]


def test_diverged_lent_volume_file_refuses_delivery_and_names_volume(client):
    put(client, "vol1", response_record(URL, T1, V1))
    # out-of-band edit: file no longer matches the catalog sha256
    (client.data_dir / "volumes" / "vol1.warc").write_bytes(
        response_record(URL, T2, V2))
    resp = get_review(client, "vol1")
    assert resp.status_code == 409
    assert resp.json()["detail"]["volumes"] == [
        {"volume": "vol1", "problem": "volume file does not match catalog sha256"}]


def test_missing_evidence_volume_file_refuses_delivery_and_names_volume(client):
    put(client, "vol1", response_record(URL, T1, V1))
    put(client, "vol2", revisit_record(URL, T2, b32(V1)))
    (client.data_dir / "volumes" / "vol1.warc").unlink()
    resp = get_review(client, "vol2")
    assert resp.status_code == 409
    volumes = resp.json()["detail"]["volumes"]
    assert {v["volume"] for v in volumes} == {"vol1"}


# -------------------------------------------------- consistency under swap --

def _check_package(package: bytes, vol2_warc: bytes):
    """Every delivered package must be internally consistent."""
    with zipfile.ZipFile(io.BytesIO(package)) as zf:
        members = {name: zf.read(name) for name in zf.namelist()}
    manifest = json.loads(members.pop("manifest.json"))
    # 原卷与清单同源: enclosed volume matches the manifest's digest
    assert members.pop("vol2.warc") == vol2_warc
    assert manifest["volume_sha256"] == hashlib.sha256(vol2_warc).hexdigest()
    bodies = bodies_in(members)
    attached = {r["digest"] for r in manifest["revisits"]
                if r["evidence"] == "attached"}
    # exactly the attached digests are enclosed, one member each
    assert set(bodies) == attached
    for digest, body in bodies.items():
        assert b32(body) == digest  # 校验通过的 HTTP 正文
        assert body in (V1, V2)
    for entry in manifest["revisits"]:
        if entry["evidence"] == "attached":
            assert entry["body"] == f"bodies/{entry['digest']}.bin"
        else:
            assert "body" not in entry
    # in this scenario the only possible states are: both revisits attached
    # (vol1 holds V1+V2) or t3 missing / t4 attached (vol1b holds only V1)
    states = [r["evidence"] for r in manifest["revisits"]]
    assert states in (["attached", "attached"], ["missing", "attached"])


def test_packages_are_consistent_while_volumes_are_swapped(tmp_path):
    store = Store(str(tmp_path / "data" / "index.sqlite3"))
    files = VolumeFiles(str(tmp_path / "data" / "volumes"))
    vol1 = response_record(URL, T1, V1) + response_record(URL, T2, V2)
    vol1b = response_record(URL, T1, V1)
    vol2 = revisit_record(URL, T3, b32(V2)) + revisit_record(URL, T4, b32(V1))
    replace_volume(store, files, "vol1", vol1, "vol1.warc")
    replace_volume(store, files, "vol2", vol2, "vol2.warc")

    stop = threading.Event()
    errors: list[Exception] = []

    def swapper():
        rounds = 0
        while not stop.is_set():
            data = vol1b if rounds % 2 else vol1
            try:
                replace_volume(store, files, "vol1", data, "vol1.warc")
            except Exception as exc:  # pragma: no cover - failure path
                errors.append(exc)
                return
            rounds += 1

    thread = threading.Thread(target=swapper)
    thread.start()
    delivered = 0
    refused = 0
    try:
        for _ in range(60):
            try:
                package = build_review_zip(store, files, "vol2")
            except ReviewError:
                refused += 1  # 整包不交付 is allowed; inconsistency is not
            else:
                _check_package(package, vol2)
                delivered += 1
    finally:
        stop.set()
        thread.join()
    assert not errors
    assert delivered > 0
