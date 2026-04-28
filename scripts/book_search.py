"""Search books across public APIs: Open Library, Google Books, Gutendex.

Run from repo root: ``python scripts/book_search.py "query"``
When published to Kragen logical storage: path ``/library/python/scripts/book_search.py``.
"""

from __future__ import annotations

import argparse
import html
import json
import os
import re
import subprocess
import sys
from typing import Any
from urllib.parse import urljoin
from xml.etree import ElementTree as ET

import httpx

OPEN_LIBRARY = "https://openlibrary.org/search.json"
GOOGLE_BOOKS = "https://www.googleapis.com/books/v1/volumes"
GUTENDEX = "https://gutendex.com/books/"
FLIBUSTA = "http://flibusta.site/"
PREFERRED_DOWNLOAD_ORDER = ("epub", "fb2", "mobi", "txt", "html", "pdf")
DEFAULT_HTTP_TIMEOUT = 20.0


def _fetch_json(client: httpx.Client, url: str, params: dict[str, Any]) -> dict[str, Any]:
    r = client.get(url, params=params, timeout=30.0)
    r.raise_for_status()
    return r.json()


def search_open_library(client: httpx.Client, query: str, limit: int) -> list[dict[str, Any]]:
    data = _fetch_json(client, OPEN_LIBRARY, {"q": query, "limit": limit})
    out: list[dict[str, Any]] = []
    for doc in data.get("docs", [])[:limit]:
        title = doc.get("title", "")
        authors = doc.get("author_name") or []
        if isinstance(authors, str):
            authors = [authors]
        year = doc.get("first_publish_year")
        key = doc.get("key", "")
        out.append(
            {
                "source": "open_library",
                "title": title,
                "authors": authors,
                "first_publish_year": year,
                "url": f"https://openlibrary.org{key}" if key else None,
                "download": {},
            }
        )
    return out


def search_google_books(client: httpx.Client, query: str, limit: int) -> list[dict[str, Any]]:
    data = _fetch_json(client, GOOGLE_BOOKS, {"q": query, "maxResults": min(limit, 40)})
    out: list[dict[str, Any]] = []
    for item in data.get("items", [])[:limit]:
        vi = item.get("volumeInfo", {})
        title = vi.get("title", "")
        authors = vi.get("authors") or []
        ids = item.get("id", "")
        preview = vi.get("previewLink") or vi.get("infoLink")
        out.append(
            {
                "source": "google_books",
                "title": title,
                "authors": authors,
                "published_date": vi.get("publishedDate"),
                "url": preview or (f"https://books.google.com/books?id={ids}" if ids else None),
                "download": {},
            }
        )
    return out


def search_gutendex(client: httpx.Client, query: str, limit: int) -> list[dict[str, Any]]:
    data = _fetch_json(client, GUTENDEX, {"search": query})
    out: list[dict[str, Any]] = []
    for book in data.get("results", [])[:limit]:
        title = book.get("title", "")
        authors = [a.get("name", "") for a in book.get("authors", [])]
        gid = book.get("id")
        formats = book.get("formats") or {}
        dl: dict[str, str] = {}
        if isinstance(formats, dict):
            for k, v in formats.items():
                if not isinstance(k, str) or not isinstance(v, str):
                    continue
                lk = k.lower()
                if "epub" in lk:
                    dl["epub"] = v
                elif "fb2" in lk:
                    dl["fb2"] = v
                elif "mobi" in lk:
                    dl["mobi"] = v
                elif "text/plain" in lk or "txt" in lk:
                    dl["txt"] = v
                elif "html" in lk:
                    dl["html"] = v
                elif "pdf" in lk:
                    dl["pdf"] = v
        out.append(
            {
                "source": "gutendex",
                "title": title,
                "authors": authors,
                "download_count": book.get("download_count"),
                "url": f"https://www.gutenberg.org/ebooks/{gid}" if gid else None,
                "download": dl,
            }
        )
    return out


def _search_flibusta_opds(client: httpx.Client, query: str, limit: int) -> list[dict[str, Any]]:
    resp = client.get(
        urljoin(FLIBUSTA, "opds/opensearch"),
        params={"searchTerm": query},
        timeout=30.0,
    )
    resp.raise_for_status()
    root = ET.fromstring(resp.text)
    ns = {"atom": "http://www.w3.org/2005/Atom"}

    out: list[dict[str, Any]] = []
    for entry in root.findall("atom:entry", ns)[:limit]:
        title = (entry.findtext("atom:title", default="", namespaces=ns) or "").strip()
        authors = [
            (name.text or "").strip()
            for name in entry.findall("atom:author/atom:name", ns)
            if (name.text or "").strip()
        ]
        href = None
        for link in entry.findall("atom:link", ns):
            href = link.attrib.get("href")
            if href:
                break
        out.append(
            {
                "source": "flibusta",
                "title": title,
                "authors": authors,
                "url": urljoin(FLIBUSTA, href) if href else None,
                "download": {},
            }
        )
    return out


def _search_flibusta_html(client: httpx.Client, query: str, limit: int) -> list[dict[str, Any]]:
    resp = client.get(
        urljoin(FLIBUSTA, "booksearch"),
        params={"ask": query},
        timeout=30.0,
    )
    resp.raise_for_status()
    out: list[dict[str, Any]] = []

    for href, raw_title in re.findall(
        r'<a[^>]+href="(/b/\d+)"[^>]*>(.*?)</a>',
        resp.text,
        flags=re.IGNORECASE | re.DOTALL,
    ):
        title = html.unescape(re.sub(r"<[^>]+>", "", raw_title)).strip()
        if not title:
            continue
        out.append(
            {
                "source": "flibusta",
                "title": title,
                "authors": [],
                "url": urljoin(FLIBUSTA, href),
                "download": {},
            }
        )
        if len(out) >= limit:
            break
    return out


def search_flibusta(client: httpx.Client, query: str, limit: int) -> list[dict[str, Any]]:
    """Search Flibusta using both OPDS and HTML, preferring `/b/<id>` links."""
    rows: list[dict[str, Any]] = []
    try:
        rows.extend(_search_flibusta_opds(client, query, limit))
    except (httpx.HTTPError, ET.ParseError):
        pass
    try:
        rows.extend(_search_flibusta_html(client, query, limit))
    except httpx.HTTPError:
        pass

    # Prefer direct book pages (/b/<id>) because they are likelier to expose formats.
    def _score(item: dict[str, Any]) -> tuple[int, str]:
        url = str(item.get("url") or "")
        return (0 if re.search(r"/b/\d+", url) else 1, item.get("title", ""))

    dedup: dict[str, dict[str, Any]] = {}
    for row in sorted(rows, key=_score):
        key = str(row.get("url") or "") + "|" + str(row.get("title") or "")
        dedup.setdefault(key, row)
        if len(dedup) >= limit:
            break
    return list(dedup.values())


def _slugify(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9а-яА-ЯёЁ._ -]+", "_", value, flags=re.UNICODE).strip()
    cleaned = re.sub(r"\s+", "_", cleaned)
    return cleaned[:120] or "book"


def _detect_ext_from_url(url: str) -> str:
    base = url.split("?", 1)[0].split("#", 1)[0].lower()
    for ext in ("epub", "fb2", "mobi", "txt", "html", "htm", "pdf", "zip"):
        if base.endswith("." + ext):
            return ext
    return "bin"


def _extract_download_links_from_html(html_text: str) -> dict[str, str]:
    """Extract likely download links and map them to normalized format names."""
    found: dict[str, str] = {}
    for href in re.findall(r'href="([^"]+)"', html_text, flags=re.IGNORECASE):
        abs_href = urljoin(FLIBUSTA, href)
        lower = abs_href.lower()
        host = abs_href.split("/", 3)[2].lower() if "://" in abs_href else ""
        if host.startswith("mobile."):
            # Mobile portal links are often navigational and not direct downloads.
            continue
        if "/read" in lower:
            continue
        if "epub" in lower:
            found.setdefault("epub", abs_href)
        elif "fb2" in lower:
            found.setdefault("fb2", abs_href)
        elif "mobi" in lower:
            found.setdefault("mobi", abs_href)
        elif "txt" in lower:
            found.setdefault("txt", abs_href)
        elif "pdf" in lower:
            found.setdefault("pdf", abs_href)
        elif "html" in lower:
            found.setdefault("html", abs_href)
    return found


def _flibusta_guess_download_links(book_url: str) -> dict[str, str]:
    """Best-effort guessed direct links for Flibusta book id URLs (/b/<id>)."""
    m = re.search(r"/b/(\d+)", book_url)
    if not m:
        return {}
    bid = m.group(1)
    candidates = {
        # Common pattern: /b/<id>/<format>
        "epub": urljoin(FLIBUSTA, f"b/{bid}/epub"),
        "fb2": urljoin(FLIBUSTA, f"b/{bid}/fb2"),
        "mobi": urljoin(FLIBUSTA, f"b/{bid}/mobi"),
        "txt": urljoin(FLIBUSTA, f"b/{bid}/txt"),
        "html": urljoin(FLIBUSTA, f"b/{bid}/html"),
        # Alternate pattern observed on some mirrors: /<format>/<id>
        "epub_alt": urljoin(FLIBUSTA, f"epub/{bid}"),
        "fb2_alt": urljoin(FLIBUSTA, f"fb2/{bid}"),
        "mobi_alt": urljoin(FLIBUSTA, f"mobi/{bid}"),
    }
    out: dict[str, str] = {}
    # Keep normalized keys in preferred buckets.
    out["epub"] = candidates["epub"]
    out["fb2"] = candidates["fb2"]
    out["mobi"] = candidates["mobi"]
    out["txt"] = candidates["txt"]
    out["html"] = candidates["html"]
    # Also include alternates with explicit keys (used as fallback candidates later).
    out["epub_alt"] = candidates["epub_alt"]
    out["fb2_alt"] = candidates["fb2_alt"]
    out["mobi_alt"] = candidates["mobi_alt"]
    return out


def _flibusta_row_from_book_id(book_id: str) -> dict[str, Any]:
    bid = str(book_id).strip()
    if not re.fullmatch(r"\d+", bid):
        raise ValueError("--book-id must be numeric (example: 833673)")
    return {
        "source": "flibusta",
        "title": f"flibusta_book_{bid}",
        "authors": [],
        "url": urljoin(FLIBUSTA, f"b/{bid}"),
        "download": _flibusta_guess_download_links(urljoin(FLIBUSTA, f"b/{bid}")),
    }


def _collect_download_map(
    client: httpx.Client, row: dict[str, Any], *, request_timeout: float
) -> dict[str, str]:
    """Resolve downloadable formats for one result row."""
    source = str(row.get("source") or "")
    existing = row.get("download")
    if isinstance(existing, dict) and existing:
        return {str(k): str(v) for k, v in existing.items() if isinstance(v, str)}

    if source == "flibusta":
        url = str(row.get("url") or "")
        if not url:
            return {}
        found: dict[str, str] = {}
        try:
            r = client.get(url, timeout=request_timeout)
            r.raise_for_status()
            found.update(_extract_download_links_from_html(r.text))
        except httpx.HTTPError:
            # Some Flibusta book pages are slow/unavailable; keep fallback guesses.
            pass
        guessed = _flibusta_guess_download_links(url)
        for fmt, href in guessed.items():
            found.setdefault(fmt, href)
        return found

    return {}


def _extract_url_stub(payload: bytes) -> str | None:
    """Return embedded URL when response body is a URL-stub text."""
    try:
        text = payload.decode("utf-8", errors="ignore").strip()
    except Exception:  # noqa: BLE001
        return None
    if not text:
        return None
    m = re.search(r"(https?://\S+)", text, flags=re.IGNORECASE)
    if not m:
        return None
    return m.group(1).strip(" '\"\t\r\n")


def _download_file(
    client: httpx.Client,
    url: str,
    output_path: str,
    *,
    request_timeout: float,
    _depth: int = 0,
) -> bool:
    try:
        with client.stream("GET", url, timeout=request_timeout) as r:
            if r.status_code >= 400:
                return False
            ctype = (r.headers.get("content-type") or "").lower()
            with open(output_path, "wb") as fh:
                sample = bytearray()
                for chunk in r.iter_bytes():
                    if chunk:
                        if len(sample) < 512:
                            sample.extend(chunk[: 512 - len(sample)])
                        fh.write(chunk)
        size = os.path.getsize(output_path)
        if size <= 0:
            return False
        sample_bytes = bytes(sample[:512])
        sample_up = sample_bytes.upper()
        # Flibusta may return HTML/URL stubs with 200 status. Reject these as book payloads.
        if "text/html" in ctype and (b"<HTML" in sample_up or b"<!DOCTYPE HTML" in sample_up):
            try:
                os.remove(output_path)
            except OSError:
                pass
            return False
        if (
            sample_up.startswith(b"HTTP://")
            or sample_up.startswith(b"HTTPS://")
            or b"\nHTTP://" in sample_up[:128]
            or b"\nHTTPS://" in sample_up[:128]
        ):
            try:
                os.remove(output_path)
            except OSError:
                pass
            # Some mirrors return a plain-text redirect URL; follow it once/twice.
            if _depth < 2:
                next_url = _extract_url_stub(sample_bytes)
                if next_url:
                    return _download_file(
                        client,
                        next_url,
                        output_path,
                        request_timeout=request_timeout,
                        _depth=_depth + 1,
                    )
            return False
        return True
    except httpx.HTTPError:
        return False


def _convert_to_epub(
    source_path: str, target_path: str, converter: str, *, timeout_seconds: int
) -> None:
    proc = subprocess.run(
        [converter, source_path, target_path],
        capture_output=True,
        text=True,
        check=False,
        timeout=timeout_seconds,
    )
    if proc.returncode != 0:
        stderr = (proc.stderr or "").strip()
        raise RuntimeError(f"Converter failed ({converter}): {stderr or 'unknown error'}")


SOURCES = {
    "open_library": search_open_library,
    "google_books": search_google_books,
    "gutendex": search_gutendex,
    "flibusta": search_flibusta,
}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "query",
        nargs="?",
        default=None,
        help="Search query (title, author, keywords).",
    )
    parser.add_argument(
        "--title",
        default=None,
        help="Optional title part. If provided, included into the final query.",
    )
    parser.add_argument(
        "--author",
        default=None,
        help="Optional author part. If provided, included into the final query.",
    )
    parser.add_argument(
        "--book-id",
        default=None,
        help="Direct Flibusta book id (numeric). Skips search and downloads this book.",
    )
    parser.add_argument(
        "-s",
        "--source",
        choices=list(SOURCES) + ["all"],
        default="all",
        help="API to query (default: all).",
    )
    parser.add_argument("-n", "--limit", type=int, default=5, help="Max results per source.")
    parser.add_argument("--json", action="store_true", help="Print JSON lines.")
    parser.add_argument(
        "--download",
        action="store_true",
        help="Download one matched book (prefer EPUB; convert if needed).",
    )
    parser.add_argument(
        "--pick",
        type=int,
        default=1,
        help="1-based index of result to download (default: 1).",
    )
    parser.add_argument(
        "--output-dir",
        default="downloads",
        help="Directory to save downloaded/converted file (default: downloads).",
    )
    parser.add_argument(
        "--convert-to-epub",
        action="store_true",
        help="If EPUB is unavailable, convert downloaded file to EPUB.",
    )
    parser.add_argument(
        "--converter",
        default="ebook-convert",
        help="Converter executable for --convert-to-epub (default: ebook-convert).",
    )
    parser.add_argument(
        "--convert-timeout",
        type=int,
        default=60,
        help="Max seconds for one conversion attempt (default: 60).",
    )
    parser.add_argument(
        "--http-timeout",
        type=float,
        default=DEFAULT_HTTP_TIMEOUT,
        help="HTTP timeout per request in seconds (default: 20).",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Print diagnostic information during download attempts.",
    )
    args = parser.parse_args()

    final_query = ""
    if args.book_id:
        if args.source not in ("flibusta", "all"):
            parser.error("--book-id is supported only with --source flibusta|all")
    else:
        parts = [x.strip() for x in (args.query, args.title, args.author) if x and str(x).strip()]
        if not parts:
            parser.error("Provide query or --title/--author, or use --book-id")
        final_query = " ".join(parts)

    names = list(SOURCES) if args.source == "all" else [args.source]
    combined: list[dict[str, Any]] = []

    try:
        with httpx.Client(
            headers={
                # Browser-like defaults increase compatibility for mirrors/proxies.
                "User-Agent": (
                    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
                ),
                "Accept": "*/*",
            },
            follow_redirects=True,
        ) as client:
            if args.book_id:
                combined = [_flibusta_row_from_book_id(args.book_id)]
            else:
                for name in names:
                    combined.extend(SOURCES[name](client, final_query, args.limit))
            if args.download:
                if not combined:
                    print("No results to download.", file=sys.stderr)
                    return 2
                idx = max(1, args.pick) - 1
                if idx >= len(combined):
                    print(
                        f"--pick {args.pick} is out of range, only {len(combined)} result(s).",
                        file=sys.stderr,
                    )
                    return 2
                row = combined[idx]
                dl_map = _collect_download_map(
                    client, row, request_timeout=max(3.0, args.http_timeout)
                )
                if not dl_map:
                    print("No downloadable links were found for selected result.", file=sys.stderr)
                    return 3
                os.makedirs(args.output_dir, exist_ok=True)
                title = _slugify(str(row.get("title") or "book"))

                candidates: list[tuple[str, str]] = []
                for fmt in PREFERRED_DOWNLOAD_ORDER:
                    if fmt in dl_map:
                        candidates.append((fmt, dl_map[fmt]))
                if not candidates:
                    # Use arbitrary links if map has only unknown format keys.
                    candidates.extend((k, v) for k, v in dl_map.items())
                # Hard cap: avoid very long runs on unstable mirrors.
                candidates = candidates[:8]

                downloaded_ext = ""
                downloaded_path = ""
                downloaded_url = ""
                conversion_errors: list[str] = []
                for fmt, dl_url in candidates:
                    if args.verbose:
                        print(f"Trying {fmt}: {dl_url}", file=sys.stderr)
                    norm_fmt = fmt.split("_", 1)[0] if fmt else ""
                    ext = norm_fmt or _detect_ext_from_url(dl_url)
                    path = os.path.join(args.output_dir, f"{title}.{ext}")
                    if _download_file(
                        client,
                        dl_url,
                        path,
                        request_timeout=max(3.0, args.http_timeout),
                    ):
                        if ext == "epub":
                            downloaded_ext = ext
                            downloaded_path = path
                            downloaded_url = dl_url
                            break
                        if args.convert_to_epub:
                            target_epub = os.path.join(args.output_dir, f"{title}.epub")
                            try:
                                _convert_to_epub(
                                    path,
                                    target_epub,
                                    args.converter,
                                    timeout_seconds=max(5, args.convert_timeout),
                                )
                            except Exception as conv_exc:  # noqa: BLE001
                                conversion_errors.append(
                                    f"{ext} from {dl_url}: {type(conv_exc).__name__}: {conv_exc}"
                                )
                                continue
                            downloaded_ext = "epub"
                            downloaded_path = target_epub
                            downloaded_url = dl_url
                            break
                        downloaded_ext = ext
                        downloaded_path = path
                        downloaded_url = dl_url
                        break
                if not downloaded_path:
                    print(
                        "Could not download from discovered links. "
                        "The source may require auth/captcha or block direct downloads.",
                        file=sys.stderr,
                    )
                    for fmt, dl_url in candidates[:8]:
                        print(f"  - tried {fmt}: {dl_url}", file=sys.stderr)
                    if conversion_errors:
                        print("Conversion errors:", file=sys.stderr)
                        for item in conversion_errors[:5]:
                            print(f"  - {item}", file=sys.stderr)
                    return 4

                if downloaded_ext == "epub":
                    print(f"Downloaded EPUB: {downloaded_path}")
                    return 0

                print(
                    "EPUB was not available. Downloaded "
                    f"{downloaded_ext.upper()}: {downloaded_path}. "
                    f"Source URL: {downloaded_url}. "
                    "Use --convert-to-epub to convert."
                )
                return 0
    except httpx.HTTPError as e:
        print(f"HTTP error: {e}", file=sys.stderr)
        return 1
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1

    if args.json:
        for row in combined:
            print(json.dumps(row, ensure_ascii=False))
        return 0

    for row in combined:
        authors = ", ".join(row.get("authors") or [])
        extra = ""
        if row["source"] == "open_library" and row.get("first_publish_year"):
            extra = f" ({row['first_publish_year']})"
        elif row["source"] == "google_books" and row.get("published_date"):
            extra = f" ({row['published_date']})"
        elif row["source"] == "gutendex" and row.get("download_count") is not None:
            extra = f" [downloads: {row['download_count']}]"
        url = row.get("url") or ""
        print(f"[{row['source']}] {row['title']}{extra}")
        if authors:
            print(f"    {authors}")
        if url:
            print(f"    {url}")
        print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
