"""The browser UI for socharness: static files only, served by ``socharnessd`` (T13).

The UI is plain ES modules, CSS, fonts and icons under ``static/``. There is no
build step and nothing is fetched from the network at runtime: every library is
vendored under ``static/vendor/`` (see ``static/vendor/VENDOR.md``).

What a server must do to host it (the daemon, the T14 mock, or fpgahub in hub mode):

- serve ``static/`` at the UI root, with ``index.html`` for the root itself;
- serve the API at ``api/v1/`` RELATIVE to that root (``/`` + ``api/v1`` on the
  daemon). A host that puts the API elsewhere sets
  ``<meta name="socharness-api-base" content="...">`` in ``index.html``;
- send the ``MEDIA_TYPES`` below (a module script with a wrong type does not load);
- ideally send ``SECURITY_HEADERS``: the UI is written to run under that CSP.

``mount_static(app)`` does all of that for a FastAPI/Starlette app. Mount it
AFTER the API routes: a mount at ``/`` matches every path.
"""

from __future__ import annotations

import mimetypes
from pathlib import Path
from typing import Any

STATIC_DIR = Path(__file__).resolve().parent / "static"

#: File types in ``static/`` and the media type each must be served with.
MEDIA_TYPES: dict[str, str] = {
    ".html": "text/html; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".woff2": "font/woff2",
    ".svg": "image/svg+xml",
    ".json": "application/json",
    ".md": "text/markdown; charset=utf-8",
}

#: The page makes no cross-origin request and runs no inline script. Styles stay
#: 'unsafe-inline' because xterm.js creates <style> elements for its renderer.
CONTENT_SECURITY_POLICY = (
    "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
    "img-src 'self' data:; font-src 'self'; connect-src 'self'; object-src 'none'; "
    "base-uri 'none'; frame-ancestors 'none'; form-action 'none'"
)

SECURITY_HEADERS: dict[str, str] = {
    "Content-Security-Policy": CONTENT_SECURITY_POLICY,
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "Cache-Control": "no-cache",
}


def static_dir() -> Path:
    """The directory holding ``index.html`` (installed as package data)."""
    return STATIC_DIR


def register_media_types() -> None:
    """Teach ``mimetypes`` the types above (some hosts map ``.js`` or ``.woff2`` oddly)."""
    for ext, media in MEDIA_TYPES.items():
        mimetypes.add_type(media.split(";")[0], ext)


def mount_static(app: Any, path: str = "/", *, name: str = "socharness-web") -> None:
    """Serve the UI from ``path`` on a Starlette/FastAPI ``app``, with the right headers.

    Call it after the API routes are added. Every static response carries
    ``SECURITY_HEADERS`` and the media type from ``MEDIA_TYPES``.
    """
    from starlette.staticfiles import StaticFiles

    register_media_types()

    class _UiFiles(StaticFiles):
        def file_response(self, full_path: Any, *args: Any, **kwargs: Any) -> Any:
            response = super().file_response(full_path, *args, **kwargs)
            media = MEDIA_TYPES.get(Path(str(full_path)).suffix.lower())
            if media:
                response.headers["content-type"] = media
            for key, value in SECURITY_HEADERS.items():
                response.headers[key] = value
            return response

    app.mount(path, _UiFiles(directory=str(STATIC_DIR), html=True), name=name)


__all__ = ["CONTENT_SECURITY_POLICY", "MEDIA_TYPES", "SECURITY_HEADERS", "STATIC_DIR",
           "mount_static", "register_media_types", "static_dir"]
