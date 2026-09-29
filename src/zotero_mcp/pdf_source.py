"""Resolve a Zotero key to a PDF file on disk, plus the keys around it.

``zotero-cli grep``, ``sections`` and ``index`` all start from a key the user
typed. That key may name the parent item or one attachment on it, and each
command needs to know both: the engines read the attachment's file, while
notes and index headers hang off the parent. ``tools.read_pdf._get_pdf_path``
resolves the file but returns neither key, so this module repeats its lookup
order and adds them.

Lookup order, as in ``_get_pdf_path``: local storage first (nothing to clean
up), then the multi-source downloader (local -> WebDAV -> Zotero cloud), which
writes a working copy into a directory this module owns.
"""

from __future__ import annotations

import os
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass

from zotero_mcp import cli_json as _cli_json


@dataclass(frozen=True)
class ResolvedPdf:
    """A PDF on disk and where it came from.

    ``parent_key`` is the regular item the attachment hangs off. When the
    caller named a standalone attachment (no parent) it is that attachment's
    own key. ``is_temp`` says whether ``path`` is a working copy this module
    removes on exit; a file in the user's Zotero storage is never removed.
    """

    parent_key: str
    attachment_key: str
    path: str
    title: str
    is_temp: bool


def _no_pdf(key: str) -> _cli_json.CliError:
    return _cli_json.CliError(f"No PDF attachment found for item: {key}",
                              code="no_pdf_attachment")


def _clean_key(key: str) -> str:
    key = (key or "").strip()
    if not key:
        raise _cli_json.CliError("Error: item key cannot be empty.", code="empty_item_key")
    return key


def parent_key(key: str) -> str:
    """The regular item a key belongs to.

    An attachment key gives its parent's key. Any other key, including a
    standalone attachment with no parent, comes back unchanged. Needs no file:
    notes are created on the parent, so ``index push`` and ``index show``
    resolve the key here and never touch the PDF.
    """
    from zotero_mcp import library as _library

    key = _clean_key(key)
    item = _library.get_library_backend().get_item(key)
    data = (item or {}).get("data", {})
    if data.get("itemType") == "attachment":
        return data.get("parentItem") or key
    return key


def _title_of(key: str, fallback: str) -> str:
    """A key's own title, or ``fallback`` when it cannot be looked up.

    Best effort only: an attachment's title is usually "PDF" or "Full Text
    PDF", so a result names the parent item instead when it can.
    """
    try:
        from zotero_mcp import library as _library

        item = _library.get_library_backend().get_item(key)
        return ((item or {}).get("data", {}).get("title") or "").strip() or fallback
    except Exception:
        return fallback


def _resolve_local(key: str) -> ResolvedPdf | None:
    """The PDF in local Zotero storage, or None to fall through to download."""
    try:
        from zotero_mcp import utils as _utils
        from zotero_mcp.config import load_config
        from zotero_mcp.local_db import LocalZoteroReader

        if not _utils.is_local_mode():
            return None
        with LocalZoteroReader(db_path=load_config().resolve_zotero_db_path()) as reader:
            # The key may name the PDF attachment itself. Attachments have no
            # children, so the parent scan below would come up empty (#372).
            attachment = reader.get_attachment_by_key(key)
            if attachment and "pdf" in (attachment["content_type"] or "").lower():
                resolved = reader.resolve_attachment_file(key)
                if resolved:
                    parent = attachment.get("parent_key") or key
                    title = attachment["title"] or key
                    if parent != key:
                        parent_item = reader.get_item_by_key(parent)
                        title = (parent_item.title if parent_item else None) or title
                    return ResolvedPdf(parent, key, str(resolved), title, False)

            local_item = reader.get_item_by_key(key)
            if local_item:
                for att_key, _path, ctype in reader._iter_parent_attachments(local_item.item_id):
                    if ctype == "application/pdf":
                        resolved = reader.resolve_attachment_file(att_key)
                        if resolved:
                            return ResolvedPdf(key, att_key, str(resolved),
                                               local_item.title or key, False)
    except Exception:
        pass
    return None


def _download(key: str) -> ResolvedPdf | None:
    """Download the PDF into a fresh temp directory, or None if there is none."""
    from zotero_mcp import client as _client
    from zotero_mcp import library as _library
    from zotero_mcp import utils as _utils
    from zotero_mcp.tools.read_pdf import _TMPDIR_PREFIX, _cleanup_path

    item = _library.get_library_backend().get_item(key)
    if item is None:
        return None
    zot = _client.get_zotero_client()
    # PDF only: the engines paginate, so a markdown-first attachment_priority
    # must not hand them a file they cannot read.
    attachment = _client.get_attachment_details(zot, item, priority=("pdf",))
    if not attachment:
        return None

    filename = attachment.filename or f"{attachment.key}.pdf"
    if not filename.lower().endswith(".pdf"):
        if "pdf" not in (attachment.content_type or "").lower():
            return None

    data = (item or {}).get("data", {})
    if data.get("itemType") == "attachment":
        parent = data.get("parentItem") or key
        title = _title_of(parent, attachment.title) if parent != key else attachment.title
    else:
        parent = key
        title = (data.get("title") or "").strip() or attachment.title

    tmpdir = tempfile.mkdtemp(prefix=_TMPDIR_PREFIX)
    probe = os.path.join(tmpdir, os.path.basename(filename))
    try:
        download = _client.download_attachment_file(
            attachment.key,
            tmpdir,
            os.path.basename(filename),
            local_client=_client.get_local_zotero_client(),
            web_client=None if _utils.is_local_mode() else zot,
        )
    except Exception:
        _cleanup_path(probe)
        raise

    if download.path and download.path.exists() and download.path.stat().st_size > 0:
        return ResolvedPdf(parent, attachment.key, str(download.path), title or key, True)

    _cleanup_path(probe)
    return None


@contextmanager
def resolved_pdf(key: str, ctx) -> Iterator[ResolvedPdf]:
    """Yield the PDF for an item key or an attachment key.

    Removes a downloaded working copy on exit, and never a file in the user's
    library. Raises ``CliError`` with code ``no_pdf_attachment`` when the key
    has no readable PDF.
    """
    key = _clean_key(key)
    ctx.info(f"Resolving PDF for {key}")
    pdf = _resolve_local(key) or _download(key)
    if pdf is None:
        raise _no_pdf(key)
    try:
        yield pdf
    finally:
        if pdf.is_temp:
            from zotero_mcp.tools.read_pdf import _cleanup_path

            _cleanup_path(pdf.path)
