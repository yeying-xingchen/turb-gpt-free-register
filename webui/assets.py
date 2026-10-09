"""Versioned, precompressed UI assets and negotiated HTML/JSON compression."""
from functools import lru_cache
import gzip
import hashlib
from pathlib import Path

from flask import Response, abort, request, url_for

_ASSET_DIR = Path(__file__).with_name("static")
_ASSETS = {
    "console.css": "text/css",
    "console.js": "text/javascript",
    "extract-links.css": "text/css",
    "extract-links.js": "text/javascript",
    "account-group-picker.css": "text/css",
    "account-group-picker.js": "text/javascript",
    "update-notice.js": "text/javascript",
}


@lru_cache(maxsize=16)
def _read_asset(path: Path, mtime_ns: int, size: int) -> tuple[bytes, bytes, str]:
    # Stat values invalidate the cache when a file changes, including in development.
    data = path.read_bytes()
    version = hashlib.sha256(data).hexdigest()[:20]
    return data, gzip.compress(data, compresslevel=6, mtime=0), version


def _asset(filename: str) -> tuple[bytes, bytes, str]:
    if filename not in _ASSETS:
        abort(404)
    path = _ASSET_DIR / filename
    stat = path.stat()
    return _read_asset(path, stat.st_mtime_ns, stat.st_size)


def init_ui_assets(app) -> None:
    def asset_url(filename: str) -> str:
        return url_for("ui_asset", filename=filename, v=_asset(filename)[2])

    app.jinja_env.globals["ui_asset_url"] = asset_url

    @app.get("/assets/<filename>")
    def ui_asset(filename: str):
        data, compressed, version = _asset(filename)
        use_gzip = request.accept_encodings["gzip"] > 0 and len(compressed) < len(data)
        response = Response(compressed if use_gzip else data, mimetype=_ASSETS[filename])
        if use_gzip:
            response.headers["Content-Encoding"] = "gzip"
        response.vary.add("Accept-Encoding")
        response.set_etag(f"{version}-{'gzip' if use_gzip else 'identity'}")
        if request.args.get("v") == version:
            response.headers["Cache-Control"] = "private, max-age=31536000, immutable"
        else:
            response.headers["Cache-Control"] = "no-cache"
        return response.make_conditional(request)

    @app.after_request
    def compress_response(response: Response):
        if (
            response.status_code != 200
            or response.direct_passthrough
            or response.is_streamed
            or response.headers.get("Content-Encoding")
            or response.mimetype not in {"application/json", "text/html"}
        ):
            return response
        response.vary.add("Accept-Encoding")
        if request.accept_encodings["gzip"] <= 0:
            return response
        data = response.get_data()
        if len(data) < 1024:
            return response
        compressed = gzip.compress(data, compresslevel=6, mtime=0)
        if len(compressed) >= len(data):
            return response
        response.set_data(compressed)
        response.headers["Content-Encoding"] = "gzip"
        if response.get_etag()[0]:
            response.add_etag(overwrite=True)
        return response
