"""Link-preview operations (Open Graph cards and their image).

SSRF protections live in :mod:`server.link_preview` (URL allowlist, public-IP
resolver, redirect re-validation, size caps); the operations only translate
its outcomes.  Both are ``async`` (network fetch) and run on the caller's loop.
"""

from __future__ import annotations

from server import blobs
from server import link_preview as lp
from server.api import ApiError, ChatAPI, operation
from server.models_api import BlobOut, LinkPreviewOut, LinksPreviewRequest


@operation("links.preview", request=LinksPreviewRequest, response=LinkPreviewOut)
async def links_preview(api: ChatAPI, req: LinksPreviewRequest) -> LinkPreviewOut:
    """Preview card of *url*.  ``invalid_request`` 'URL not allowed' for a URL
    previews may not fetch (SSRF guard), ``not_found`` 'No preview' otherwise."""
    try:
        preview = await lp.get_preview(req.url)
    except lp.BlockedURLError:
        raise ApiError("invalid_request", "URL not allowed") from None
    if preview is None:
        raise ApiError("not_found", "No preview")
    return LinkPreviewOut(
        url=preview["url"],
        title=preview["title"],
        description=preview["description"],
        site_name=preview["site_name"],
        has_image=bool(preview.get("image_url") or preview.get("local_image")),
    )


@operation("links.preview_image", request=LinksPreviewRequest, response=BlobOut)
async def links_preview_image(api: ChatAPI, req: LinksPreviewRequest) -> BlobOut:
    """The raster image advertised by the page's own preview, as a blob.

    Only that image is ever served (no generic fetch proxy); the bytes are
    sniffed (no SVG) and stored content-addressed under the cache directory.
    """
    try:
        result = await lp.get_preview_image(req.url)
    except lp.BlockedURLError:
        raise ApiError("invalid_request", "URL not allowed") from None
    if result is None:
        raise ApiError("not_found", "No preview image")
    data, content_type = result
    path = lp.materialize_image(data, content_type)
    if path is None:
        raise ApiError("not_found", "No preview image")
    return blobs.blob_for(path, content_type)
