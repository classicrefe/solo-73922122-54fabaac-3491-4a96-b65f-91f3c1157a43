"""Ingest pipeline: WARC bytes -> capture rows -> atomic volume swap."""
from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone

from . import warc
from .db import Store


class IngestError(Exception):
    """An upload failed; the previously stored volume (if any) is kept."""

    status_code = 422

    def __init__(self, detail):
        super().__init__(str(detail))
        self.detail = detail


class CompressedWarcError(IngestError):
    status_code = 400


class NotWarcError(IngestError):
    status_code = 400


class MalformedWarcError(IngestError):
    status_code = 422


class DuplicateCaptureError(IngestError):
    status_code = 409


@dataclass
class Capture:
    url: str
    ts: str
    type: str               # 'response' | 'revisit'
    digest: str             # canonical sha1/base32 payload digest
    valid: int | None       # response: 1/0; revisit: None (evidence derived at query time)
    http_status: int | None


@dataclass
class ParsedWarc:
    captures: list[Capture] = field(default_factory=list)
    skipped: list[dict] = field(default_factory=list)


def _classify(rec: warc.WarcRecord, parsed: ParsedWarc) -> None:
    rtype = rec.headers.get("warc-type", "").strip().lower()
    if rtype not in ("response", "revisit"):
        parsed.skipped.append({
            "warc_type": rtype or "(missing)",
            "reason": "record type is not processed",
        })
        return
    uri = rec.headers.get("warc-target-uri", "").strip()
    if not uri:
        raise warc.WarcFormatError(f"{rtype} record without WARC-Target-URI")
    raw_date = rec.headers.get("warc-date")
    if not raw_date:
        raise warc.WarcFormatError(f"{rtype} record for {uri!r} without WARC-Date")
    ts = warc.parse_warc_date(raw_date)
    raw_digest = rec.headers.get("warc-payload-digest")
    if not raw_digest:
        parsed.skipped.append({
            "warc_type": rtype, "url": uri,
            "reason": "no WARC-Payload-Digest",
        })
        return
    digest = warc.normalize_payload_digest(raw_digest)
    if digest is None:
        parsed.skipped.append({
            "warc_type": rtype, "url": uri,
            "reason": "payload digest is not sha1",
        })
        return
    if rtype == "revisit":
        parsed.captures.append(Capture(uri, ts, "revisit", digest, None, None))
        return
    # WARC-Type: response — the payload is the archived HTTP response body.
    ctype = rec.headers.get("content-type", "").split(";")[0].strip().lower()
    if ctype != "application/http":
        parsed.skipped.append({
            "warc_type": rtype, "url": uri,
            "reason": f"unsupported WARC Content-Type {ctype!r}",
        })
        return
    msg = warc.split_http_message(rec.content)
    payload = warc.extract_payload(msg)
    if payload is None:
        parsed.skipped.append({
            "warc_type": rtype, "url": uri,
            "reason": "HTTP body is encoded (only unencoded bodies are indexed)",
        })
        return
    valid = 1 if warc.payload_digest(payload) == digest else 0
    parsed.captures.append(Capture(uri, ts, "response", digest, valid, msg.status))


def parse_warc(data: bytes) -> ParsedWarc:
    """Parse and validate a whole uncompressed WARC file."""
    if data[:2] == b"\x1f\x8b":
        raise CompressedWarcError(
            "gzip-compressed WARC rejected: upload an uncompressed .warc file")
    head = data.lstrip()[:8]
    if head and not head.startswith(b"WARC/"):
        raise NotWarcError("not a WARC file (missing WARC/1.x version line)")
    parsed = ParsedWarc()
    try:
        for rec in warc.iter_warc_records(data):
            _classify(rec, parsed)
    except warc.WarcFormatError as exc:
        raise MalformedWarcError(f"invalid WARC: {exc}") from exc
    seen: dict[tuple[str, str], None] = {}
    for cap in parsed.captures:
        key = (cap.url, cap.ts)
        if key in seen:
            raise DuplicateCaptureError(
                f"duplicate capture inside file: {cap.url!r} at {cap.ts}")
        seen[key] = None
    return parsed


class VolumeFiles:
    """Original WARC files kept on disk, one file per volume （原卷留档）."""

    def __init__(self, directory: str):
        self.directory = directory
        os.makedirs(directory, exist_ok=True)

    def path(self, volume: str) -> str:
        return os.path.join(self.directory, f"{volume}.warc")

    def stage(self, volume: str, data: bytes) -> str:
        """Write the upload next to the live file and fsync it."""
        tmp = self.path(volume) + ".tmp"
        with open(tmp, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        return tmp

    def commit(self, volume: str, tmp: str) -> None:
        os.replace(tmp, self.path(volume))

    def discard(self, tmp: str) -> None:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass

    def remove(self, volume: str) -> None:
        try:
            os.unlink(self.path(volume))
        except FileNotFoundError:
            pass


def replace_volume(store: Store, files: VolumeFiles, volume: str,
                   data: bytes, filename: str) -> dict:
    """Validate the upload, then swap the volume atomically.

    Any failure before or during the database transaction leaves the old
    volume (index rows and archived file) untouched.
    """
    parsed = parse_warc(data)
    tmp = files.stage(volume, data)
    volume_row = {
        "filename": filename,
        "sha256": hashlib.sha256(data).hexdigest(),
        "size_bytes": len(data),
        "records": len(parsed.captures),
        "ingested_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    try:
        conflicts = store.swap_volume(volume, parsed.captures, volume_row)
    except BaseException:
        files.discard(tmp)
        raise
    if conflicts:
        files.discard(tmp)
        raise DuplicateCaptureError(
            {"detail": "captures already exist in other volumes", "conflicts": conflicts})
    files.commit(volume, tmp)
    responses = sum(1 for c in parsed.captures if c.type == "response")
    return {
        "volume": volume,
        "stored_file": files.path(volume),
        "records_indexed": len(parsed.captures),
        "responses": responses,
        "responses_verified": sum(1 for c in parsed.captures
                                  if c.type == "response" and c.valid == 1),
        "responses_digest_failed": sum(1 for c in parsed.captures
                                       if c.type == "response" and c.valid == 0),
        "revisits": len(parsed.captures) - responses,
        "records_skipped": len(parsed.skipped),
        "skipped": parsed.skipped,
    }
