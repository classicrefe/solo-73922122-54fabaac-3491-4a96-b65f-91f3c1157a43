"""End-to-end tests for the gazetteer web-archive index."""
from __future__ import annotations

import gzip

import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from tests.warcbuild import (b32, chunked, record, response_record,
                             revisit_record, warcinfo_record)

URL = "http://gazetteer.example.cn/notice.html"
V1 = "馆藏公告 第一版：本馆藏有民国县志三十种。".encode()
V2 = "馆藏公告 第二版：新增解放后府志十二种。".encode()
V3 = "馆藏公告 第三版：数字化平台上线。".encode()

T1 = "2024-03-01T09:00:00Z"
T2 = "2024-06-01T09:00:00Z"
T3 = "2024-09-01T09:00:00Z"
T4 = "2024-12-01T09:00:00Z"


@pytest.fixture()
def client(tmp_path):
    app = create_app(str(tmp_path / "data"))
    with TestClient(app) as test_client:
        test_client.data_dir = tmp_path / "data"
        yield test_client


def put(client, volume, payload: bytes, filename="vol.warc"):
    return client.put(f"/volumes/{volume}/warc",
                      files={"file": (filename, payload, "application/warc")})


def changes(client, url=URL):
    resp = client.get("/changes", params={"url": url})
    assert resp.status_code == 200
    return resp.json()


# ---------------------------------------------------------------- ingest --

def test_upload_indexes_responses_and_reports_verification(client):
    warc = (warcinfo_record()
            + response_record(URL, T1, V1)
            + response_record(URL, T2, V2))
    resp = put(client, "vol1", warc)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["records_indexed"] == 2
    assert body["responses_verified"] == 2
    assert body["responses_digest_failed"] == 0
    assert body["records_skipped"] == 1  # the warcinfo record
    # 原卷留档: the original WARC file is kept on disk
    assert (client.data_dir / "volumes" / "vol1.warc").read_bytes() == warc


def test_changes_between_two_evidenced_captures(client):
    put(client, "vol1", response_record(URL, T1, V1) + response_record(URL, T2, V2))
    out = changes(client)
    assert [(c["from_ts"], c["to_ts"]) for c in out["changes"]] == [(T1, T2)]
    assert out["unevidenced"] == []


def test_no_change_when_bodies_identical(client):
    put(client, "vol1", response_record(URL, T1, V1) + response_record(URL, T2, V1))
    assert changes(client)["changes"] == []


def test_digest_mismatch_is_unevidenced_and_skipped_in_chain(client):
    bad = response_record(URL, T2, V2, digest=b32(b"something-else"))
    warc = response_record(URL, T1, V1) + bad + response_record(URL, T3, V3)
    resp = put(client, "vol1", warc)
    assert resp.status_code == 200
    assert resp.json()["responses_digest_failed"] == 1
    out = changes(client)
    # the corrupt capture is not evidence: change is reported t1 -> t3
    assert [(c["from_ts"], c["to_ts"]) for c in out["changes"]] == [(T1, T3)]
    assert out["unevidenced"] == [T2]


def test_revisit_evidenced_by_verified_response_in_other_volume(client):
    put(client, "vol1", response_record(URL, T1, V1))
    warc2 = revisit_record(URL, T2, b32(V1)) + response_record(URL, T3, V3)
    resp = put(client, "vol2", warc2)
    assert resp.status_code == 200
    out = changes(client)
    assert [(c["from_ts"], c["to_ts"]) for c in out["changes"]] == [(T2, T3)]
    assert out["unevidenced"] == []


def test_revisit_not_evidenced_by_failed_validation(client):
    # response body fails its digest check -> cannot evidence a revisit
    put(client, "vol1", response_record(URL, T1, V1, digest=b32(b"forged")))
    put(client, "vol2", revisit_record(URL, T2, b32(b"forged")))
    out = changes(client)
    assert out["unevidenced"] == [T1, T2]


def test_revisit_does_not_evidence_another_revisit(client):
    put(client, "vol1", revisit_record(URL, T1, b32(V1)))
    put(client, "vol2", revisit_record(URL, T2, b32(V1)))
    assert changes(client)["unevidenced"] == [T1, T2]


def test_revisit_without_matching_body_is_unevidenced(client):
    put(client, "vol1", revisit_record(URL, T1, b32(V1)))
    out = changes(client)
    assert out["changes"] == []
    assert out["unevidenced"] == [T1]


def test_revisit_loses_evidence_when_last_matching_body_removed(client):
    put(client, "vol1", response_record(URL, T1, V1) + response_record(URL, T2, V2))
    put(client, "vol2", revisit_record(URL, T3, b32(V2)))
    assert changes(client)["unevidenced"] == []
    # replace vol1 with a volume that no longer contains the V2 body
    put(client, "vol1", response_record(URL, T1, V1))
    out = changes(client)
    assert out["unevidenced"] == [T3]
    # evidence for the revisit in vol2 disappears without touching vol2
    volumes = {v["volume"]: v["records"] for v in client.get("/volumes").json()["volumes"]}
    assert volumes == {"vol1": 1, "vol2": 1}


def test_evidence_survives_while_any_matching_body_remains(client):
    put(client, "vol1", response_record(URL, T1, V1))
    put(client, "vol2", response_record(URL, T2, V1))  # same body, second copy
    put(client, "vol3", revisit_record(URL, T3, b32(V1)))
    put(client, "vol1", response_record(URL, T4, V2))  # vol1 no longer holds V1
    out = changes(client)
    assert out["unevidenced"] == []  # vol2's copy still evidences the revisit


def test_volume_replacement_is_wholesale(client):
    put(client, "vol1", response_record(URL, T1, V1) + response_record(URL, T2, V2))
    put(client, "vol1", response_record(URL, T3, V3))
    out = changes(client)
    assert [c["ts"] for c in out["captures"]] == [T3]


def test_conflict_with_other_volume_rejected_and_old_kept(client):
    put(client, "vol1", response_record(URL, T1, V1))
    resp = put(client, "vol2", response_record(URL, T1, V2))  # same url+ts
    assert resp.status_code == 409
    assert "vol1" in resp.text
    volumes = [v["volume"] for v in client.get("/volumes").json()["volumes"]]
    assert volumes == ["vol1"]
    assert [c["ts"] for c in changes(client)["captures"]] == [T1]


def test_same_instant_different_urls_is_allowed(client):
    other = "http://gazetteer.example.cn/other.html"
    put(client, "vol1", response_record(URL, T1, V1))
    resp = put(client, "vol2", response_record(other, T1, V2))
    assert resp.status_code == 200


def test_duplicate_capture_inside_one_file_rejected(client):
    warc = response_record(URL, T1, V1) + response_record(URL, T1, V2)
    assert put(client, "vol1", warc).status_code == 409


def test_failed_replacement_keeps_old_volume(client):
    put(client, "vol1", response_record(URL, T1, V1))
    resp = put(client, "vol1", b"this is not a WARC file at all")
    assert resp.status_code == 400
    assert [c["ts"] for c in changes(client)["captures"]] == [T1]
    assert (client.data_dir / "volumes" / "vol1.warc").exists()


def test_gzipped_warc_rejected(client):
    warc = response_record(URL, T1, V1)
    resp = put(client, "vol1", gzip.compress(warc), filename="vol1.warc.gz")
    assert resp.status_code == 400
    assert "uncompressed" in resp.json()["detail"]


def test_malformed_record_rejected(client):
    warc = b"WARC/1.0\r\nWARC-Type: response\r\nContent-Length: nope\r\n\r\n"
    assert put(client, "vol1", warc).status_code == 422


def test_content_encoded_body_is_out_of_scope(client):
    http = response_record(URL, T1, b"\x1f\x8b fake-gzip",
                           http_headers=("Content-Encoding: gzip",))
    resp = put(client, "vol1", http)
    assert resp.status_code == 200
    body = resp.json()
    assert body["records_indexed"] == 0
    assert body["records_skipped"] == 1
    assert "encoded" in body["skipped"][0]["reason"]


def test_chunked_transfer_body_is_dechunked_and_verified(client):
    http = response_record(URL, T1, chunked(V1, sizes=(8, 9)),
                           http_headers=("Transfer-Encoding: chunked",),
                           digest=b32(V1))
    resp = put(client, "vol1", http)
    assert resp.status_code == 200
    assert resp.json()["responses_verified"] == 1


def test_non_sha1_digest_record_skipped(client):
    http = record("response", URL, T1, content=b"HTTP/1.1 200 OK\r\n\r\nbody",
                  digest=None,
                  extra_headers=("WARC-Payload-Digest: sha256:abcdef",))
    resp = put(client, "vol1", http)
    assert resp.status_code == 200
    assert resp.json()["records_indexed"] == 0
    assert resp.json()["skipped"][0]["reason"] == "payload digest is not sha1"


def test_record_without_digest_skipped(client):
    http = record("response", URL, T1, content=b"HTTP/1.1 200 OK\r\n\r\nbody")
    resp = put(client, "vol1", http)
    assert resp.json()["records_indexed"] == 0
    assert resp.json()["skipped"][0]["reason"] == "no WARC-Payload-Digest"


def test_delete_volume_removes_evidence(client):
    put(client, "vol1", response_record(URL, T1, V1))
    put(client, "vol2", revisit_record(URL, T2, b32(V1)))
    assert changes(client)["unevidenced"] == []
    resp = client.delete("/volumes/vol1")
    assert resp.status_code == 200
    assert changes(client)["unevidenced"] == [T2]
    assert client.delete("/volumes/vol1").status_code == 404


def test_unknown_url_has_no_captures(client):
    out = changes(client, "http://nowhere.example/")
    assert out == {"url": "http://nowhere.example/", "changes": [],
                   "unevidenced": [], "captures": []}


def test_fractional_and_offset_dates_normalised_to_utc(client):
    warc = response_record(URL, "2024-03-01T17:00:00+08:00", V1)
    put(client, "vol1", warc)
    assert [c["ts"] for c in changes(client)["captures"]] == [T1]
