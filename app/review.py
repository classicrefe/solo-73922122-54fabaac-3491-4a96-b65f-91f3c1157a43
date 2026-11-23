"""Review-package builder: lend one volume with evidence for its revisits.

When a volume leaves the library, the recipient cannot see the other
volumes, so revisits in the lent volume whose bodies live elsewhere can no
longer be checked.  ``build_review_zip`` produces a self-contained ZIP:

* ``{volume}.warc`` — the original volume, byte-for-byte as uploaded;
* ``manifest.json`` — the revisit manifest （重访清单）: every revisit of the
  volume with its URL, capture instant and payload digest, each marked
  ``in_volume`` （卷内正文作证）, ``attached`` （包内附带正文作证） or
  ``missing`` （馆内也缺证）;
* ``bodies/{digest}.bin`` — the digest-verified HTTP body for every
  ``attached`` digest, one member per digest.  Only the body leaves the
  library: the source volumes' capture records and HTTP headers stay
  behind, and the manifest never names the source volumes.

Consistency: volumes may be swapped while a package is being built.  The
manifest, the original volume and the attached bodies therefore all come
from one committed state — the index is read in a single SQLite snapshot
and every volume file used must hash to the sha256 the catalog recorded
for that snapshot.  If a needed original volume is missing or diverges
from its catalog digest, the build is retried against a fresh snapshot and
finally refused: no package is delivered and the offending volume numbers
are reported.
"""
from __future__ import annotations

import hashlib
import io
import json
import zipfile

from . import warc
from .db import Store
from .ingest import VolumeFiles

EVIDENCE_IN_VOLUME = "in_volume"
EVIDENCE_ATTACHED = "attached"
EVIDENCE_MISSING = "missing"

# How often a build may restart against a fresh snapshot when a volume
# file changed under it (concurrent swap) before giving up.
_ATTEMPTS = 3

# Fixed ZIP member timestamps: the package is a pure function of the
# committed state, so identical states yield byte-identical packages.
_ZIP_EPOCH = (1980, 1, 1, 0, 0, 0)


class ReviewError(Exception):
    """The review package could not be delivered."""

    status_code = 409

    def __init__(self, detail):
        super().__init__(str(detail))
        self.detail = detail


class UnknownVolumeError(ReviewError):
    status_code = 404


class VolumeFileError(ReviewError):
    """A needed original volume is missing or diverged from the catalog."""
    status_code = 409


def _verified_bodies(data: bytes, wanted: set[str]) -> dict[str, bytes]:
    """Digest-verified HTTP bodies in *data* whose digest is in *wanted*.

    The payload digest is recomputed here, so an attached body is verified
    at packaging time regardless of what the index recorded.
    """
    found: dict[str, bytes] = {}
    for rec in warc.iter_warc_records(data):
        if rec.headers.get("warc-type", "").strip().lower() != "response":
            continue
        ctype = rec.headers.get("content-type", "").split(";")[0].strip().lower()
        if ctype != "application/http":
            continue
        try:
            msg = warc.split_http_message(rec.content)
            payload = warc.extract_payload(msg)
        except warc.WarcFormatError:
            continue
        if payload is None:
            continue
        digest = warc.payload_digest(payload)
        if digest in wanted and digest not in found:
            found[digest] = payload
            if found.keys() >= wanted:
                break
    return found


def _writestr(zf: zipfile.ZipFile, name: str, data: bytes) -> None:
    info = zipfile.ZipInfo(name, date_time=_ZIP_EPOCH)
    info.compress_type = zipfile.ZIP_DEFLATED
    info.external_attr = 0o644 << 16
    zf.writestr(info, data)


def _zip_package(volume: str, snap: dict, warc_bytes: bytes,
                 bodies: dict[str, bytes]) -> bytes:
    manifest = {
        "volume": volume,
        "volume_sha256": hashlib.sha256(warc_bytes).hexdigest(),
        "warc_member": f"{volume}.warc",
        "revisits": [
            {
                "url": r["url"],
                "ts": r["ts"],
                "digest": r["digest"],
                "evidence": r["evidence"],
                **({"body": f"bodies/{r['digest']}.bin"}
                   if r["evidence"] == EVIDENCE_ATTACHED else {}),
            }
            for r in snap["revisits"]
        ],
    }
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        _writestr(zf, f"{volume}.warc", warc_bytes)
        _writestr(zf, "manifest.json",
                  json.dumps(manifest, ensure_ascii=False, indent=2).encode("utf-8"))
        for digest in sorted(bodies):
            _writestr(zf, f"bodies/{digest}.bin", bodies[digest])
    return buf.getvalue()


def build_review_zip(store: Store, files: VolumeFiles, volume: str) -> bytes:
    """Build the review ZIP for *volume*, or raise :class:`ReviewError`.

    Every attempt reads the index in one snapshot and then requires each
    needed volume file to match the sha256 recorded in that snapshot; a
    concurrent swap therefore either is invisible (the file still matches)
    or restarts the build.  After several restarts the build is refused
    and the offending volume numbers are reported.
    """
    problems: list[dict] = []
    for _attempt in range(_ATTEMPTS):
        snap = store.review_snapshot(volume)
        if snap is None:
            raise UnknownVolumeError(f"no such volume: {volume}")
        contents: dict[str, bytes] = {}
        problems = []
        for name, sha256 in snap["catalog"].items():
            try:
                data = files.read(name)
            except FileNotFoundError:
                problems.append({"volume": name, "problem": "original volume file missing"})
                continue
            if hashlib.sha256(data).hexdigest() != sha256:
                problems.append({"volume": name,
                                 "problem": "volume file does not match catalog sha256"})
                continue
            contents[name] = data
        if problems:
            continue
        wanted: dict[str, set[str]] = {}
        for entry in snap["revisits"]:
            if entry["evidence"] == EVIDENCE_ATTACHED:
                wanted.setdefault(snap["sources"][entry["digest"]], set()).add(entry["digest"])
        bodies: dict[str, bytes] = {}
        for name, digests in wanted.items():
            found = _verified_bodies(contents[name], digests)
            if found.keys() != digests:
                problems.append({"volume": name,
                                 "problem": "verified body not found in volume file"})
            bodies.update(found)
        if problems:
            continue
        return _zip_package(volume, snap, contents[volume], bodies)
    raise VolumeFileError({
        "detail": "required original volume missing or diverged from catalog digest",
        "volumes": problems,
    })
