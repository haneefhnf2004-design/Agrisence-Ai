"""
File intelligence: search, info, organise-by-type (preview -> approve -> undo), backup, duplicates.

All paths go through tool_runner._safe_path, so nothing outside the user's home folder is touched.
Nothing is ever deleted: organise only moves (never overwrites, writes an undo manifest), backup only
copies into a zip.
"""
import fnmatch
import hashlib
import json
import os
import shutil
import time
import zipfile

CATEGORIES = {
    "Images": {"jpg", "jpeg", "png", "gif", "bmp", "webp", "svg", "heic", "tiff", "ico"},
    "Videos": {"mp4", "mkv", "mov", "avi", "wmv", "webm", "flv"},
    "Audio": {"mp3", "wav", "flac", "aac", "ogg", "m4a"},
    "Documents": {"pdf", "doc", "docx", "txt", "rtf", "odt", "md"},
    "Spreadsheets": {"xls", "xlsx", "xlsm", "csv", "ods", "tsv"},
    "Presentations": {"ppt", "pptx", "odp", "key"},
    "Archives": {"zip", "rar", "7z", "tar", "gz", "bz2"},
    "Installers": {"exe", "msi", "msix", "appx"},
    "Code": {"py", "js", "jsx", "ts", "tsx", "html", "css", "java", "c", "cpp", "cs", "go", "rs", "php", "json", "ipynb", "sql"},
}
SKIP_DIRS = {"node_modules", ".git", "__pycache__", ".venv", "venv", "AppData", "$Recycle.Bin", ".cache"}
BACKUP_DIR = os.path.join(os.path.expanduser("~"), "Documents", "Nexora Backups")


def _safe(p):
    from tool_runner import _safe_path
    return _safe_path(str(p))


def _category(name):
    ext = os.path.splitext(name)[1].lower().lstrip(".")
    for cat, exts in CATEGORIES.items():
        if ext in exts:
            return cat
    return "Other"


def search(query, root=None, content=False, ext=None, limit=40, time_budget=12.0):
    """Find files whose name matches `query` (substring or glob). content=True also greps text files."""
    if not query or not str(query).strip():
        raise ValueError("Give something to search for.")
    root = _safe(root or os.path.expanduser("~"))
    q = str(query).strip().lower()
    is_glob = any(c in q for c in "*?[")
    exts = {e.lower().lstrip(".") for e in (ext.split(",") if isinstance(ext, str) else (ext or []))} - {""}
    limit = max(1, min(int(limit or 40), 200))
    t0, scanned, hits = time.monotonic(), 0, []
    for dirpath, dirs, names in os.walk(root):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS and not d.startswith(".")]
        for n in names:
            scanned += 1
            e = os.path.splitext(n)[1].lower().lstrip(".")
            if exts and e not in exts:
                continue
            nl = n.lower()
            match = fnmatch.fnmatch(nl, q) if is_glob else q in nl
            where = "name"
            fp = os.path.join(dirpath, n)
            if not match and content and e in {"txt", "md", "py", "js", "ts", "jsx", "tsx", "json", "csv", "html", "css", "log", "ini", "yaml", "yml"}:
                try:
                    if os.path.getsize(fp) < 2 * 1024 * 1024:
                        with open(fp, encoding="utf-8", errors="ignore") as f:
                            if q in f.read().lower():
                                match, where = True, "content"
                except OSError:
                    pass
            if match:
                try:
                    st = os.stat(fp)
                    hits.append({"path": fp, "size_kb": round(st.st_size / 1024, 1), "matched": where,
                                 "modified": time.strftime("%Y-%m-%d %H:%M", time.localtime(st.st_mtime))})
                except OSError:
                    continue
                if len(hits) >= limit:
                    return {"results": hits, "scanned": scanned, "complete": False, "reason": "limit"}
        if time.monotonic() - t0 > time_budget:
            return {"results": hits, "scanned": scanned, "complete": False, "reason": "time budget reached"}
    return {"results": hits, "scanned": scanned, "complete": True}


def info(path):
    p = _safe(path)
    if not os.path.exists(p):
        raise ValueError(f"Not found: {p}")
    st = os.stat(p)
    res = {"path": p, "is_folder": os.path.isdir(p),
           "modified": time.strftime("%Y-%m-%d %H:%M", time.localtime(st.st_mtime)),
           "created": time.strftime("%Y-%m-%d %H:%M", time.localtime(st.st_ctime))}
    if os.path.isdir(p):
        total = files = 0
        by_cat = {}
        for dirpath, dirs, names in os.walk(p):
            dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
            for n in names:
                try:
                    sz = os.path.getsize(os.path.join(dirpath, n))
                except OSError:
                    continue
                files += 1
                total += sz
                c = _category(n)
                by_cat[c] = by_cat.get(c, 0) + 1
            if files > 200000:
                break
        res.update({"files": files, "size_mb": round(total / 1048576, 1), "by_type": by_cat})
    else:
        res.update({"size_kb": round(st.st_size / 1024, 1), "type": _category(p)})
    return res


def organize_preview(folder):
    """What organise would do to the files directly inside `folder` (subfolders are left alone)."""
    f = _safe(folder)
    if not os.path.isdir(f):
        raise ValueError(f"Not a folder: {f}")
    plan = []
    for n in sorted(os.listdir(f)):
        src = os.path.join(f, n)
        if not os.path.isfile(src) or n.startswith(".") or n.startswith("~$") or n.lower() == "desktop.ini":
            continue
        cat = _category(n)
        plan.append({"file": n, "to": cat})
    summary = {}
    for p in plan:
        summary[p["to"]] = summary.get(p["to"], 0) + 1
    return {"folder": f, "moves": len(plan), "summary": summary, "plan": plan[:200]}


def _free_name(dst):
    if not os.path.exists(dst):
        return dst
    base, ext = os.path.splitext(dst)
    i = 2
    while os.path.exists(f"{base} ({i}){ext}"):
        i += 1
    return f"{base} ({i}){ext}"


def organize_apply(folder):
    prev = organize_preview(folder)
    f = prev["folder"]
    done, created = [], []
    for p in prev["plan"]:
        src = os.path.join(f, p["file"])
        if not os.path.isfile(src):
            continue
        cat_dir = os.path.join(f, p["to"])
        if not os.path.isdir(cat_dir):
            os.makedirs(cat_dir)
            created.append(cat_dir)
        dst = _free_name(os.path.join(f, p["to"], p["file"]))
        try:
            shutil.move(src, dst)
        except OSError:  # file open in another app: leave it, keep going, still write the undo manifest
            if os.path.exists(dst) and os.path.exists(src):
                os.remove(dst)  # drop the partial copy shutil made; the original is untouched
            continue
        if os.path.exists(dst):
            done.append({"from": src, "to": dst})
    manifest = os.path.join(f, f".nexora_organize_{time.strftime('%Y%m%d_%H%M%S')}.json")
    with open(manifest, "w", encoding="utf-8") as fh:
        json.dump({"folder": f, "moves": done, "created_dirs": created}, fh, indent=1)
    return {"moved": len(done), "summary": prev["summary"], "undo_manifest": manifest}


def organize_undo(manifest):
    m = _safe(manifest)
    with open(m, encoding="utf-8") as fh:
        data = json.load(fh)
    restored, skipped = 0, []
    for mv in reversed(data.get("moves", [])):
        src, dst = _safe(mv["to"]), _safe(mv["from"])
        if os.path.exists(src) and not os.path.exists(dst):
            shutil.move(src, dst)
            restored += 1
        else:
            skipped.append(mv["to"])
    for d in data.get("created_dirs", []):  # only folders organise itself created, and only if now empty
        d = _safe(d)
        if os.path.isdir(d) and not os.listdir(d):
            os.rmdir(d)
    return {"restored": restored, "skipped": skipped[:20]}


def backup(path, dest_dir=None):
    src = _safe(path)
    if not os.path.exists(src):
        raise ValueError(f"Not found: {src}")
    dest_dir = _safe(dest_dir) if dest_dir else BACKUP_DIR
    os.makedirs(dest_dir, exist_ok=True)
    name = os.path.basename(src.rstrip("\\/")) or "backup"
    out = os.path.join(dest_dir, f"{name}_{time.strftime('%Y%m%d_%H%M%S')}.zip")
    if os.path.commonpath([os.path.realpath(dest_dir), src]) == src:
        raise ValueError("The backup folder is inside the folder being backed up; choose another destination.")
    count = 0
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        if os.path.isfile(src):
            z.write(src, os.path.basename(src))
            count = 1
        else:
            for dirpath, dirs, names in os.walk(src):
                dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
                for n in names:
                    fp = os.path.join(dirpath, n)
                    try:
                        z.write(fp, os.path.relpath(fp, os.path.dirname(src)))
                        count += 1
                    except (PermissionError, OSError):
                        continue
    with zipfile.ZipFile(out) as z:
        bad = z.testzip()
    if bad:
        raise RuntimeError(f"Backup verification failed at {bad}")
    return {"backup": out, "files": count, "size_mb": round(os.path.getsize(out) / 1048576, 2), "verified": True}


def duplicates(folder, min_kb=1, limit=30):
    """Groups of identical files (same size, then same SHA-1). Read-only."""
    f = _safe(folder)
    by_size = {}
    for dirpath, dirs, names in os.walk(f):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
        for n in names:
            fp = os.path.join(dirpath, n)
            try:
                sz = os.path.getsize(fp)
            except OSError:
                continue
            if sz >= min_kb * 1024:
                by_size.setdefault(sz, []).append(fp)
    groups = []
    for sz, paths in by_size.items():
        if len(paths) < 2:
            continue
        by_hash = {}
        for p in paths:
            h = hashlib.sha1()
            try:
                with open(p, "rb") as fh:
                    for block in iter(lambda: fh.read(1 << 20), b""):
                        h.update(block)
            except OSError:
                continue
            by_hash.setdefault(h.hexdigest(), []).append(p)
        for same in by_hash.values():
            if len(same) > 1:
                groups.append({"size_kb": round(sz / 1024, 1), "files": same})
    groups.sort(key=lambda g: g["size_kb"] * (len(g["files"]) - 1), reverse=True)
    wasted = sum(g["size_kb"] * (len(g["files"]) - 1) for g in groups)
    return {"groups": groups[:limit], "group_count": len(groups), "wasted_mb": round(wasted / 1024, 1)}
