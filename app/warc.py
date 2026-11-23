"""Uncompressed WARC reading and HTTP payload extraction.

Scope is deliberately narrow — only what the archive index needs:

* iterate records of an *uncompressed* WARC 1.0/1.1 file,
* normalise ``WARC-Payload-Digest`` (sha1 only),
* split an archived HTTP response and recover its unencoded payload
  (chunked transfer coding is undone; content-encoded bodies are out
  of scope and reported as such).
"""
from __future__ import annotations

import base64
import hashlib
import io
import re
from dataclasses import dataclass
from datetime import datetime, timezone


class WarcFormatError(Exception):
    """Raised when the WARC input (or an archived HTTP message) is malformed."""


@dataclass
class WarcRecord:
    headers: dict[str, str]
    content: bytes


@dataclass
class HttpMessage:
    status: int | None
    headers: dict[str, str]
    body: bytes


class _LineReader:
    """readline() with pushback over a binary stream."""

    def __init__(self, fp):
        self._fp = fp
        self._pushed: list[bytes] = []

    def readline(self) -> bytes:
        if self._pushed:
            return self._pushed.pop()
        return self._fp.readline()

    def unread(self, line: bytes) -> None:
        self._pushed.append(line)


def _parse_header_block(lines: list[bytes]) -> dict[str, str]:
    """Parse RFC-style header lines into a dict (lower-cased names, last wins)."""
    headers: dict[str, str] = {}
    current: str | None = None
    for raw in lines:
        if raw[:1] in (b" ", b"\t") and current is not None:
            headers[current] += " " + raw.decode("utf-8", "replace").strip()
            continue
        name, sep, value = raw.partition(b":")
        if not sep:
            raise WarcFormatError(f"malformed header line: {raw[:80]!r}")
        current = name.decode("ascii", "replace").strip().lower()
        headers[current] = value.decode("utf-8", "replace").strip()
    return headers


def iter_warc_records(data: bytes):
    """Yield :class:`WarcRecord` objects from an uncompressed WARC byte string."""
    fp = io.BytesIO(data)
    reader = _LineReader(fp)
    while True:
        line = reader.readline()
        if line == b"":
            return
        if line.strip() == b"":
            continue  # record separator or leading blank lines
        if not line.startswith((b"WARC/1.0", b"WARC/1.1")):
            raise WarcFormatError(f"expected WARC version line, got: {line[:60]!r}")
        header_lines: list[bytes] = []
        while True:
            hline = reader.readline()
            if hline == b"":
                raise WarcFormatError("unexpected EOF inside WARC headers")
            if hline in (b"\r\n", b"\n"):
                break
            header_lines.append(hline.rstrip(b"\r\n"))
        headers = _parse_header_block(header_lines)
        raw_len = headers.get("content-length", "")
        if not raw_len.isdigit():
            raise WarcFormatError("WARC record without a valid Content-Length")
        length = int(raw_len)
        content = fp.read(length)
        if len(content) != length:
            raise WarcFormatError("truncated WARC record content")
        # Consume the record separator (blank line(s)); push back anything else.
        sep = reader.readline()
        while sep != b"" and sep.strip() == b"":
            sep = reader.readline()
        if sep != b"":
            reader.unread(sep)
        yield WarcRecord(headers=headers, content=content)


_SHA1_B32_RE = re.compile(r"^[A-Z2-7]{32}$")


def normalize_payload_digest(value: str) -> str | None:
    """Normalise a ``WARC-Payload-Digest`` value.

    Returns the canonical form (uppercase base32, no padding) for sha1
    digests, ``None`` for other algorithms, and raises
    :class:`WarcFormatError` when the value is malformed.
    """
    algo, sep, digest = value.partition(":")
    if not sep or not algo.strip() or not digest.strip():
        raise WarcFormatError(f"malformed WARC-Payload-Digest: {value!r}")
    if algo.strip().lower() != "sha1":
        return None
    norm = digest.strip().rstrip("=").upper()
    if not _SHA1_B32_RE.match(norm):
        raise WarcFormatError(f"malformed sha1 payload digest: {value!r}")
    return norm


def payload_digest(payload: bytes) -> str:
    """Canonical sha1/base32 digest of a payload, matching WARC convention."""
    return base64.b32encode(hashlib.sha1(payload).digest()).decode("ascii").rstrip("=")


def parse_warc_date(value: str) -> str:
    """Parse a WARC-Date and return a canonical UTC ISO-8601 string."""
    text = value.strip()
    try:
        dt = datetime.fromisoformat(text)
    except ValueError as exc:
        raise WarcFormatError(f"malformed WARC-Date: {value!r}") from exc
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    dt = dt.astimezone(timezone.utc)
    timespec = "seconds" if dt.microsecond == 0 else "microseconds"
    return dt.isoformat(timespec=timespec).replace("+00:00", "Z")


def split_http_message(content: bytes) -> HttpMessage:
    """Split an archived HTTP response into status, headers and raw body."""
    idx = content.find(b"\r\n\r\n")
    sep = 4
    if idx < 0:
        idx = content.find(b"\n\n")
        sep = 2
    if idx < 0:
        raise WarcFormatError("HTTP message without header terminator")
    head, body = content[:idx], content[idx + sep:]
    lines = head.split(b"\r\n") if b"\r\n" in head else head.split(b"\n")
    status_line = lines[0].decode("latin-1")
    if not status_line.startswith("HTTP/"):
        raise WarcFormatError(f"not an HTTP response: {status_line[:60]!r}")
    parts = status_line.split(" ", 2)
    status = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else None
    headers = _parse_header_block(lines[1:])
    return HttpMessage(status=status, headers=headers, body=body)


def _dechunk(body: bytes) -> bytes:
    """Undo HTTP ``Transfer-Encoding: chunked``."""
    out = bytearray()
    pos = 0
    while True:
        eol = body.find(b"\r\n", pos)
        if eol < 0:
            raise WarcFormatError("malformed chunked body (size line)")
        size_text = body[pos:eol].split(b";", 1)[0].strip()
        try:
            size = int(size_text, 16)
        except ValueError as exc:
            raise WarcFormatError("malformed chunked body (size)") from exc
        pos = eol + 2
        if size == 0:
            return bytes(out)
        chunk = body[pos:pos + size]
        if len(chunk) != size or body[pos + size:pos + size + 2] != b"\r\n":
            raise WarcFormatError("malformed chunked body (data)")
        out += chunk
        pos += size + 2


def extract_payload(msg: HttpMessage) -> bytes | None:
    """Return the unencoded HTTP payload of an archived response.

    ``None`` means the body is out of scope for this index because it is
    content-encoded (``Content-Encoding`` other than identity) or carries
    a non-chunked transfer coding.  Chunked transfer coding is undone
    before the payload digest is verified.
    """
    ce = msg.headers.get("content-encoding")
    if ce and ce.strip().lower() != "identity":
        return None
    body = msg.body
    te = msg.headers.get("transfer-encoding")
    if te:
        codings = [c.strip().lower() for c in te.split(",") if c.strip()]
        while codings and codings[-1] == "chunked":
            body = _dechunk(body)
            codings.pop()
        if any(c != "identity" for c in codings):
            return None
    return body
