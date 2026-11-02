#!/usr/bin/env python3
"""Download original tracker module files from zxart.ee.

zxart.ee was rebuilt as an Angular SPA and the old "download all music" button
and its zip endpoint are gone. The original files are still served though --
the browser calls /tunes/?action=tunesByElement... and every item carries an
`originalFileUrl` pointing at the real .pt3/.pt2/.psc file.

Usage:
    python zxart_download.py 37405              # -> ./mast/1998/..., ./mast/unknown/...
    python zxart_download.py 37405 -o Mast --formats pt3 pt2
    python zxart_download.py 37405 --dry-run

Files land in <author>/<year>/<original upload name>; the format is not part of
the path and tune ids stay out of the filenames -- see index.md / index.json.
"""

from __future__ import annotations

import argparse
import json
import http.client
import re
import sys
import threading
import time
import urllib.parse
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

API = "https://zxart.ee/tunes/"
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)
RETRIES = 6
UNSAFE = re.compile(r'[\\/:*?"<>|]')


def fetch(url: str, timeout: int = 60) -> bytes:
    """GET a URL over a fresh connection, retrying hard.

    Two zxart.ee quirks make the naive path unreliable:
      * the TLS endpoint intermittently drops the handshake
        (SSL UNEXPECTED_EOF_WHILE_READING) on roughly one connection in five;
      * urllib's opener hits that failure far more often than a bare
        http.client connection does, so we build the connection ourselves.
    """
    parsed = urllib.parse.urlsplit(url)
    path = parsed.path + (("?" + parsed.query) if parsed.query else "")
    last: Exception | None = None
    for attempt in range(RETRIES):
        conn = None
        try:
            if parsed.scheme == "https":
                conn = http.client.HTTPSConnection(parsed.hostname, parsed.port or 443, timeout=timeout)
            else:
                conn = http.client.HTTPConnection(parsed.hostname, parsed.port or 80, timeout=timeout)
            conn.request("GET", path, headers={"User-Agent": UA, "Accept": "*/*"})
            resp = conn.getresponse()
            body = resp.read()
            if resp.status >= 400:
                raise RuntimeError(f"HTTP {resp.status} for {url}")
            return body
        except (OSError, http.client.HTTPException, RuntimeError) as exc:
            last = exc
            time.sleep(min(0.5 * (attempt + 1), 3.0))
        finally:
            if conn is not None:
                conn.close()
    raise RuntimeError(f"failed after {RETRIES} tries: {url} ({last})")


def list_tunes(author_id: str, limit: int = 1000) -> list[dict]:
    params = urllib.parse.urlencode(
        {
            "action": "tunesByElement",
            "elementId": str(author_id),
            "start": "0",
            "limit": str(limit),
        }
    )
    data = json.loads(fetch(f"{API}?{params}"))
    items = data.get("items") or []
    total = data.get("total")
    if total is not None and len(items) < total:
        # the endpoint can cap page size; walk the rest
        while len(items) < total:
            more = urllib.parse.urlencode(
                {
                    "action": "tunesByElement",
                    "elementId": str(author_id),
                    "start": str(len(items)),
                    "limit": str(limit),
                }
            )
            page = json.loads(fetch(f"{API}?{more}")).get("items") or []
            if not page:
                break
            items.extend(page)
    return items


def local_name(item: dict, names: dict[int, str] | None = None) -> str:
    """Return the on-disk filename: the original upload name, minus the id.

    The URL looks like /file/id:44811/filename:Mast_-_Insomnia_%281999%29.pt3
    -- the filename is a path segment, not a query parameter, and it is the
    name the author uploaded under. Tune ids belong in the index, not the
    filename, so the id in the URL path is never used here.
    """
    raw = urllib.parse.urlparse(item.get("originalFileUrl") or "")
    name = ""
    if "filename:" in raw.path:
        name = urllib.parse.unquote(raw.path.split("filename:", 1)[1])
    elif raw.query:
        for part in raw.query.split("&"):
            if part.startswith("filename="):
                name = urllib.parse.unquote(part[len("filename=") :])
                break
    if not name:
        ext = (item.get("format") or "bin").lower()
        name = f"{UNSAFE.sub('_', item.get('title') or 'untitled')}.{ext}"
    return UNSAFE.sub("_", name).strip()


def build_names(items: list[dict]) -> dict[int, str]:
    """Map tune id -> filename, disambiguating same-named uploads.

    Two tunes can be uploaded under the same filename in the same year (Mast
    has two different "Mast_-_Insomnia_(1999).pt3"). Ids stay out of the
    filename, so repeats get a letter suffix instead: (a), (b), ...
    """
    names: dict[int, str] = {}
    groups: dict[tuple[str, str], list[dict]] = {}
    for item in items:
        key = (year_dir(item), local_name(item).lower())
        groups.setdefault(key, []).append(item)
    for (_, _), group in groups.items():
        if len(group) == 1:
            names[group[0]["id"]] = local_name(group[0])
            continue
        for index, item in enumerate(sorted(group, key=lambda i: i["id"])):
            stem = local_name(item)
            suffix = chr(ord("a") + index)
            dot = stem.rfind(".")
            if dot > 0:
                stem = f"{stem[:dot]} ({suffix}){stem[dot:]}"
            else:
                stem = f"{stem} ({suffix})"
            names[item["id"]] = stem
    return names


def year_dir(item: dict) -> str:
    year = (item.get("year") or "").strip()
    return year if re.fullmatch(r"\d{4}", year) else "unknown"


def author_slug(author_id: str, override: str | None = None) -> str:
    """Folder name for the author: slugified title, e.g. 37405 -> "mast"."""
    if override:
        return UNSAFE.sub("_", override).strip() or f"author_{author_id}"
    try:
        detail = json.loads(fetch(f"https://zxart.ee/author-details/?id={author_id}"))
        title = (detail.get("title") or "").strip()
    except Exception:  # noqa: BLE001 - fall back to the id rather than abort
        title = ""
    if not title:
        return f"author_{author_id}"
    slug = re.sub(r"[^\w\-]+", "-", title, flags=re.UNICODE).strip("-_").lower()
    return slug or f"author_{author_id}"


def dest_path(out_root: Path, item: dict, name: str) -> Path:
    return out_root / year_dir(item) / name


def download(item: dict, out_root: Path, name: str, overwrite: bool = False) -> tuple[str, str]:
    dest = dest_path(out_root, item, name)
    dest.parent.mkdir(parents=True, exist_ok=True)
    if not overwrite and dest.exists() and dest.stat().st_size > 0:
        return "skip", str(dest.relative_to(out_root))
    payload = fetch(item["originalFileUrl"])
    if not payload:
        raise RuntimeError("empty response")
    tmp = dest.with_suffix(dest.suffix + ".part")
    tmp.write_bytes(payload)
    tmp.replace(dest)
    return "ok", str(dest.relative_to(out_root))


def write_index(items: list[dict], out_root: Path, names: dict[int, str]) -> None:
    rows = []
    for i in items:
        name = names.get(i["id"], local_name(i))
        rows.append(
            {
                "id": i["id"],
                "title": i.get("title"),
                "format": i.get("format"),
                "year": i.get("year"),
                "file": f"{year_dir(i)}/{name}",
                "originalFileUrl": i.get("originalFileUrl"),
                "mp3Url": i.get("mp3Url"),
            }
        )
    rows.sort(key=lambda r: ((r.get("year") or "0"), (r.get("title") or "").lower()))
    out_root.mkdir(parents=True, exist_ok=True)
    (out_root / "index.json").write_text(
        json.dumps(rows, ensure_ascii=False, indent=1), encoding="utf-8"
    )

    by_year: dict[str, list[dict]] = {}
    for r in rows:
        by_year.setdefault(year_dir({"year": r.get("year")}), []).append(r)
    lines = ["# zxart tunes", "", "Tune ids live here and in `index.json`, not in the filenames.", ""]
    for year in sorted(by_year, key=lambda y: (y == "unknown", y)):
        lines += [f"## {year}", "", "| id | title | format | file |", "|---|---|---|---|"]
        for r in by_year[year]:
            title = (r["title"] or "").replace("|", "/")
            lines.append(f"| {r['id']} | {title} | {r['format']} | {r['file']} |")
        lines.append("")
    (out_root / "index.md").write_text("\n".join(lines), encoding="utf-8")


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("author_id", help="numeric author id, e.g. 37405 from /author/37405/music")
    p.add_argument(
        "-o",
        "--out",
        help="author folder to write into (default: the author's slug, e.g. ./mast)",
    )
    p.add_argument("--formats", nargs="+", help="only these formats, e.g. pt3 pt2")
    p.add_argument("--limit", type=int, default=0, help="max tunes to download")
    p.add_argument("--workers", type=int, default=6, help="parallel downloads (default 6)")
    p.add_argument("--dry-run", action="store_true", help="list tunes, download nothing")
    p.add_argument("--overwrite", action="store_true", help="re-download existing files")
    args = p.parse_args()

    print(f"listing tunes for author {args.author_id} ...", file=sys.stderr)
    items = list_tunes(args.author_id)
    if not items:
        print("no tunes found -- check the author id", file=sys.stderr)
        return 1

    out_root = Path(args.out) if args.out else Path(author_slug(args.author_id))
    print(f"author folder: {out_root}", file=sys.stderr)
    missing = [i for i in items if not i.get("originalFileUrl")]
    items = [i for i in items if i.get("originalFileUrl")]
    if args.formats:
        wanted = {f.lower().lstrip(".") for f in args.formats}
        items = [i for i in items if (i.get("format") or "").lower() in wanted]
    if args.limit:
        items = items[: args.limit]

    counts: dict[str, int] = {}
    for i in items:
        k = (i.get("format") or "other").upper()
        counts[k] = counts.get(k, 0) + 1
    summary = ", ".join(f"{k} {v}" for k, v in sorted(counts.items()))
    print(f"{len(items)} tune(s): {summary}", file=sys.stderr)
    if missing:
        print(f"warning: {len(missing)} tune(s) have no originalFileUrl", file=sys.stderr)
    if args.dry_run:
        for i in items:
            print(f"{i['id']}\t{i.get('format')}\t{i.get('year')}\t{i.get('title')}")
        write_index(items, out_root, build_names(items))
        print(f"index written to {out_root}", file=sys.stderr)
        return 0

    names = build_names(items)
    write_index(items, out_root, names)

    lock = threading.Lock()
    done = {"ok": 0, "skip": 0, "fail": 0}
    failures: list[str] = []
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = {
            pool.submit(download, i, out_root, names[i["id"]], args.overwrite): i
            for i in items
        }
        for fut in as_completed(futures):
            item = futures[fut]
            try:
                status, name = fut.result()
            except Exception as exc:  # noqa: BLE001
                status, name = "fail", f"{item['id']} {item.get('title')}: {exc}"
                failures.append(name)
            with lock:
                done[status] += 1
                print(f"{status:4} {name}")

    print(
        f"\ndone: ok={done['ok']} skip={done['skip']} fail={done['fail']} -> {out_root}",
        file=sys.stderr,
    )
    if failures:
        (out_root / "failed.txt").write_text("\n".join(failures), encoding="utf-8")
        print(f"failures written to {out_root / 'failed.txt'}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
