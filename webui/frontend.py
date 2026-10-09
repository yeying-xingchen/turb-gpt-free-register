"""Serve Nuxt's generated SPA from the existing authenticated Flask service."""
from pathlib import Path

from flask import Response, abort, current_app, send_from_directory

_DEFAULT_DIST = Path(__file__).resolve().parents[1] / "frontend" / ".output" / "public"
_ADMIN_PAGES = ("accounts", "tasks", "mailboxes", "codex", "redemptions", "settings", "providers")
_PUBLIC_PAGES = ("upload",)


def frontend_dir() -> Path:
    return Path(current_app.config.get("NUXT_DIST_DIR", _DEFAULT_DIST))


def frontend_page():
    root = frontend_dir()
    if not (root / "index.html").is_file():
        return Response(
            '<!doctype html><html lang="zh-CN"><meta charset="utf-8">'
            '<title>请先构建前端</title><body><h1>Nuxt 前端尚未构建</h1>'
            '<p>在项目根目录执行：</p><pre>npm --prefix frontend ci\n'
            'npm --prefix frontend run build</pre><p>构建完成后刷新页面即可。</p></body></html>',
            status=503, mimetype="text/html", headers={"Cache-Control": "no-store"},
        )
    response = send_from_directory(root, "index.html", max_age=0)
    response.headers["Cache-Control"] = "no-store"
    response.headers["Referrer-Policy"] = "same-origin"
    return response


def register_frontend(app):
    app.config.setdefault("NUXT_DIST_DIR", _DEFAULT_DIST)
    app.config["AUTH_PUBLIC_ENDPOINTS"].update({"nuxt_asset", "nuxt_brand"})

    @app.get("/_nuxt/<path:filename>", endpoint="nuxt_asset")
    def nuxt_asset(filename):
        # send_from_directory rejects traversal and only exposes generated chunks.
        if filename.endswith(".map"):
            abort(404)
        immutable = filename != "builds/latest.json"
        response = send_from_directory(frontend_dir() / "_nuxt", filename, max_age=31536000 if immutable else 0)
        response.headers["Cache-Control"] = "public, max-age=31536000, immutable" if immutable else "no-cache"
        response.headers["X-Content-Type-Options"] = "nosniff"
        return response

    @app.get("/brand.svg", endpoint="nuxt_brand")
    def nuxt_brand():
        return send_from_directory(frontend_dir(), "brand.svg", max_age=3600)

    for page in _ADMIN_PAGES:
        app.add_url_rule(f"/{page}", endpoint=f"nuxt_{page}", view_func=frontend_page, strict_slashes=False)
    for page in _PUBLIC_PAGES:
        endpoint = f"public_{page}_page"
        app.config["AUTH_PUBLIC_ENDPOINTS"].add(endpoint)
        app.add_url_rule(f"/{page}", endpoint=endpoint, view_func=frontend_page, strict_slashes=False)
