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


LOSSLESS = {".pt3", ".pt2", ".pt1", ".psc", ".scl", ".ay", ".cop", ".etc", ".saa",
            ".stc", ".stp", ".ts", ".asc", ".gtr", ".vt2", ".st3", ".dmm", ".dst",
            ".mtc", ".psg", ".tfc", ".pt", ".snd", ".ayc", ".ym", ".flac", ".wav"}
AUDIO = {".mp3", ".ogg", ".oga", ".m4a", ".opus", ".wma"}
# A .wav on zxart is a rendered export, not tracker source, so it counts as
# audio when deciding whether a tracker project is the better download.
RENDERS = AUDIO | {".wav", ".flac"}


def ext_of(url: str) -> str:
    path = urllib.parse.urlparse(url or "").path
    return "." + path.rsplit(".", 1)[-1].lower() if "." in path else ""


def pick_source(item: dict) -> tuple[str, str]:
    """Choose the file to download: the tracker project over a lossy render.

    For most tunes `originalFileUrl` *is* the module. But a handful of entries
    only ever had an MP3 uploaded as the "original", while the actual tracker
    project sits next to it in `trackerFileUrl` (Pyramex has 24 such tunes,
    mostly .cop/.etc/.saa). Downloading the mp3 there throws away the only
    real source material, so prefer the tracker file whenever the original is
    a lossy audio render.
    """
    original = item.get("originalFileUrl") or ""
    tracker = item.get("trackerFileUrl") or ""
    if tracker and ext_of(original) in RENDERS and ext_of(tracker) not in RENDERS:
        return tracker, "tracker"
    return original, "original"


def local_name(item: dict, names: dict[int, str] | None = None) -> str:
    """Return the on-disk filename: the original upload name, minus the id.

    The URL looks like /file/id:44811/filename:Mast_-_Insomnia_%281999%29.pt3
    -- the filename is a path segment, not a query parameter, and it is the
    name the author uploaded under. Tune ids belong in the index, not the
    filename, so the id in the URL path is never used here.
    """
    url, _ = pick_source(item)
    raw = urllib.parse.urlparse(url)
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


def list_authors(limit: int = 50) -> list[dict]:
    """Top musicians by site rating, aliases folded into their real author.

    /authorlist/ returns both authors and their aliases as separate rows with
    an identical musicRating, so the raw top 50 is really ~7 people. Rows carry
    realNameId pointing at the owning author; keying on that collapses them.
    """
    fetched = 0
    page = 500
    merged: dict[int, dict] = {}
    while fetched < 3000:
        params = urllib.parse.urlencode(
            {
                "elementId": "0",
                "start": str(fetched),
                "limit": str(page),
                "sorting": "musicRating,desc",
                "items": "music",
            }
        )
        data = json.loads(fetch(f"https://zxart.ee/authorlist/?{params}"))
        rows = data.get("items") or []
        if not rows:
            break
        for row in rows:
            owner = row.get("realNameId") or row["id"]
            name = row.get("realName") if row.get("realNameId") else row.get("title")
            entry = merged.setdefault(
                owner,
                {"id": owner, "name": name or row.get("title"), "rating": row.get("musicRating"), "aliases": []},
            )
            if row.get("entityType") == "authorAlias" and row.get("title"):
                entry["aliases"].append(row["title"])
        fetched += len(rows)
        if fetched >= (data.get("total") or 0):
            break
    ranked = sorted(merged.values(), key=lambda a: -(a.get("rating") or 0))
    return ranked[:limit]


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
    url, _kind = pick_source(item)
    payload = fetch(url)
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
        url, kind = pick_source(i)
        rows.append(
            {
                "id": i["id"],
                "title": i.get("title"),
                "format": i.get("format"),
                "year": i.get("year"),
                "file": f"{year_dir(i)}/{name}",
                "downloadedFrom": kind,
                "downloadedUrl": url,
                "originalFileUrl": i.get("originalFileUrl"),
                "trackerFileUrl": i.get("trackerFileUrl"),
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


def run_author(author_id: str, args) -> int:
    print(f"\n=== author {author_id}", file=sys.stderr)
    items = list_tunes(author_id)
    if not items:
        print("no tunes found -- check the author id", file=sys.stderr)
        return 1

    out_root = Path(args.out) if args.out else Path(author_slug(author_id))
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

    rescued = [i for i in items if pick_source(i)[1] == "tracker"]
    if rescued:
        print(
            f"{len(rescued)} tune(s) stored as audio but have a tracker project -- "
            f"downloading the tracker file instead: "
            + ", ".join(f"{i['id']}:{ext_of(pick_source(i)[0])}" for i in rescued[:8])
            + (" ..." if len(rescued) > 8 else ""),
            file=sys.stderr,
        )
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
                if args.verbose:
                    print(f"{status:4} {name}")

    print(
        f"done: ok={done['ok']} skip={done['skip']} fail={done['fail']} -> {out_root}",
        file=sys.stderr,
    )
    if failures:
        (out_root / "failed.txt").write_text("\n".join(failures), encoding="utf-8")
        print(f"failures written to {out_root / 'failed.txt'}", file=sys.stderr)
        return 2
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument(
        "author_id",
        nargs="*",
        help="numeric author id(s), e.g. 37405 from /author/37405/music",
    )
    p.add_argument(
        "--top",
        type=int,
        help="instead of ids, take the N highest-rated musicians (aliases folded)",
    )
    p.add_argument(
        "-o",
        "--out",
        help="author folder to write into (default: the author's slug, e.g. ./mast)",
    )
    p.add_argument("--formats", nargs="+", help="only these formats, e.g. pt3 pt2")
    p.add_argument("--limit", type=int, default=0, help="max tunes to download per author")
    p.add_argument("--workers", type=int, default=6, help="parallel downloads (default 6)")
    p.add_argument("--dry-run", action="store_true", help="list tunes, download nothing")
    p.add_argument("--overwrite", action="store_true", help="re-download existing files")
    p.add_argument("-v", "--verbose", action="store_true", help="print every file")
    args = p.parse_args()

    ids: list[str] = list(args.author_id)
    if args.top:
        ranked = list_authors(args.top)
        print(f"top {len(ranked)} musicians by rating:", file=sys.stderr)
        for i, a in enumerate(ranked, 1):
            alias = f" (+{', '.join(a['aliases'])})" if a["aliases"] else ""
            print(f"  {i:>2}. {a['name']}{alias}  [{a['id']}]", file=sys.stderr)
        ids += [str(a["id"]) for a in ranked]
    if not ids:
        p.error("give at least one author id or --top N")

    worst = 0
    for author_id in ids:
        if len(ids) > 1 and args.out:
            p.error("-o only makes sense with a single author")
        try:
            worst = max(worst, run_author(author_id, args))
        except Exception as exc:  # noqa: BLE001
            print(f"author {author_id} failed: {exc}", file=sys.stderr)
            worst = max(worst, 1)
    return worst


if __name__ == "__main__":
    sys.exit(main())
