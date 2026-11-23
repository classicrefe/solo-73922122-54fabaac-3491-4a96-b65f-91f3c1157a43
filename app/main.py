"""FastAPI surface for the gazetteer web-archive index."""
from __future__ import annotations

import os

from fastapi import FastAPI, File, HTTPException, Path, Query, Response, UploadFile

from .db import Store
from .ingest import IngestError, VolumeFiles, replace_volume
from .review import ReviewError, build_review_zip

VOLUME_ID = Path(
    description="volume number/label",
    min_length=1,
    max_length=100,
    pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$",
)


def create_app(data_dir: str) -> FastAPI:
    store = Store(os.path.join(data_dir, "index.sqlite3"))
    files = VolumeFiles(os.path.join(data_dir, "volumes"))

    app = FastAPI(
        title="Gazetteer Web-Archive Index",
        description=(
            "Indexes uncompressed WARC volumes (response/revisit records with "
            "sha1 payload digests, unencoded HTTP bodies only) and reports "
            "between which two evidenced captures a URL's body changed."
        ),
        version="1.0.0",
    )
    app.state.store = store
    app.state.files = files

    @app.get("/", include_in_schema=False)
    def root():
        return {"service": "gazetteer-web-archive-index", "docs": "/docs"}

    @app.get("/healthz", include_in_schema=False)
    def healthz():
        return {"status": "ok"}

    @app.put("/volumes/{volume}/warc", summary="Upload or wholly replace a volume")
    async def put_volume(volume: str = VOLUME_ID, file: UploadFile = File(...)):
        """Store an uncompressed WARC as *volume*.

        A volume with the same number is replaced wholesale; if the upload
        fails validation or conflicts with other volumes, the old volume is
        kept untouched.
        """
        data = await file.read()
        try:
            return replace_volume(store, files, volume, data, file.filename or "")
        except IngestError as exc:
            raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc

    @app.get("/volumes", summary="List stored volumes")
    def list_volumes():
        return {"volumes": [dict(row) for row in store.list_volumes()]}

    @app.delete("/volumes/{volume}", summary="Remove a volume and its captures")
    def delete_volume(volume: str = VOLUME_ID):
        if not store.delete_volume(volume):
            raise HTTPException(status_code=404, detail=f"no such volume: {volume}")
        files.remove(volume)
        return {"deleted": volume}

    @app.get(
        "/volumes/{volume}/review.zip",
        summary="Download a review package (ZIP) for lending out a volume",
        responses={
            200: {"content": {"application/zip": {}},
                  "description": "ZIP with the original volume, the revisit "
                                 "manifest and the attached evidence bodies."},
            404: {"description": "No such volume."},
            409: {"description": "A needed original volume is missing or "
                                 "diverged from its catalog digest; the "
                                 "offending volume numbers are reported and "
                                 "no package is delivered."},
        },
    )
    def review_package(volume: str = VOLUME_ID):
        """Build a self-contained review package for *volume*.

        The ZIP contains the original volume (``{volume}.warc``), a revisit
        manifest (``manifest.json``) marking each revisit as evidenced
        inside the volume (``in_volume``), evidenced by an attached body
        (``attached``) or unevidenced even in the library (``missing``),
        and one digest-verified HTTP body per attached digest
        (``bodies/{digest}.bin``).  Source volumes' capture records and
        HTTP headers stay in the library.  Manifest, original volume and
        attached bodies always correspond to one committed state.
        """
        try:
            data = build_review_zip(store, files, volume)
        except ReviewError as exc:
            raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc
        return Response(
            content=data,
            media_type="application/zip",
            headers={"Content-Disposition":
                     f'attachment; filename="{volume}-review.zip"'},
        )

    @app.get("/changes", summary="Find between which captures a URL's body changed")
    def changes(url: str = Query(description="exact WARC-Target-URI to inspect")):
        """Compare adjacent *evidenced* captures of *url* by capture time.

        A response capture is evidence only when its stored sha1 payload
        digest matches the archived body; a revisit is evidence only when a
        digest-verified response with the same digest exists in the volumes
        currently stored.  Returns every pair of neighbouring evidenced
        captures whose bodies differ, plus the captures lacking evidence.
        """
        rows = store.captures_for_url(url)
        captures = [
            {
                "ts": row["ts"],
                "type": row["type"],
                "digest": row["digest"],
                "evidenced": bool(row["evidenced"]),
                "volume": row["volume"],
                "http_status": row["http_status"],
            }
            for row in rows
        ]
        evidenced = [c for c in captures if c["evidenced"]]
        changes_out = [
            {
                "from_ts": a["ts"],
                "to_ts": b["ts"],
                "from_digest": a["digest"],
                "to_digest": b["digest"],
            }
            for a, b in zip(evidenced, evidenced[1:])
            if a["digest"] != b["digest"]
        ]
        return {
            "url": url,
            "changes": changes_out,
            "unevidenced": [c["ts"] for c in captures if not c["evidenced"]],
            "captures": captures,
        }

    return app


app = create_app(os.environ.get("DATA_DIR", "data"))
