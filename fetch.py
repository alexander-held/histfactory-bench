"""Download the public pyhf workspaces indexed at pyhf.github.io/public-probability-models.

The index only points to HEPData records, the files are HEPData resources (plain JSON
or archives). The JSON files are stored gzip-compressed in data/ (gitignored), the
downloads themselves are not kept. manifest.json records provenance.
"""

import gzip
import hashlib
import json
import re
import shutil
import tarfile
import time
import zipfile
from email.message import Message
from pathlib import Path

import requests

INDEX = "https://pyhf.github.io/public-probability-models/atlas.json"
HEPDATA = "https://www.hepdata.net"
ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"
MANIFEST = ROOT / "manifest.json"

ARCHIVE_EXT = (".tar", ".tar.gz", ".tgz", ".tar.xz", ".txz", ".tar.bz2", ".zip")
SKIP_TYPES = {"png", "pdf", "jpg", "jpeg", "gif", "eps", "svg"}
# archives are only downloaded if they look like they hold likelihoods
KEYWORDS = re.compile(r"likelihood|workspace|hs3|histfactory|statistical|json|llh", re.I)
DEST = {"workspace": "pyhf", "patchset": "pyhf", "jsonpatch": "pyhf", "hs3": "hs3"}


def open_url(url):
    # HEPData rejects urllib's User-Agent, requests is accepted
    for attempt in range(4):
        try:
            response = requests.get(url, stream=True, timeout=300)
            if response.status_code < 500 and response.status_code != 429:
                response.raise_for_status()
                return response
            error = requests.HTTPError(f"{response.status_code} for {url}")
        except (requests.ConnectionError, requests.Timeout) as e:
            error = e
        time.sleep(5 * 2**attempt)
    raise error


def get_json(url):
    with open_url(url) as response:
        return response.json()


def attachment_name(response):
    message = Message()
    message["content-disposition"] = response.headers.get("content-disposition", "")
    return message.get_filename()


def sha256(path):
    with path.open("rb") as f:
        return hashlib.file_digest(f, "sha256").hexdigest()


def strip_archive_ext(name):
    for ext in ARCHIVE_EXT:
        if name.lower().endswith(ext):
            return name[: -len(ext)]
    return name


def classify(raw):
    try:
        obj = json.loads(raw)
    except (ValueError, RecursionError):
        return "invalid"
    if isinstance(obj, list):
        is_patch = obj and all(isinstance(op, dict) and "op" in op for op in obj)
        return "jsonpatch" if is_patch else "other"
    if not isinstance(obj, dict):
        return "other"
    if {"channels", "observations", "measurements"} <= obj.keys():
        return "workspace"
    if {"patches", "metadata"} <= obj.keys():
        return "patchset"
    metadata = obj.get("metadata")
    if {"distributions", "likelihoods"} & obj.keys() or (
        isinstance(metadata, dict) and "hs3_version" in metadata
    ):
        return "hs3"
    return "other"


def unpack(archive, dest, depth=0):
    if zipfile.is_zipfile(archive):
        with zipfile.ZipFile(archive) as z:
            z.extractall(dest)
    else:
        with tarfile.open(archive) as t:
            t.extractall(dest, filter="data")
    if depth >= 3:
        return
    for p in sorted(dest.rglob("*")):
        if p.is_file() and not p.is_symlink() and p.name.lower().endswith(ARCHIVE_EXT):
            try:
                unpack(p, p.with_name(strip_archive_ext(p.name)), depth + 1)
            except (tarfile.TarError, zipfile.BadZipFile, OSError) as e:
                print(f"    cannot unpack nested {p.name}: {e}")
            p.unlink()


def cached(name, resource):
    """Result of an earlier run for this resource, if its files are still there."""
    cache = DATA / "cache" / name / f"{resource['id']}.json"
    if not cache.exists():
        return None
    result = json.loads(cache.read_text())
    return result if all((ROOT / f["path"]).exists() for f in result["files"]) else None


def download(resource, skipped):
    """Fetch one HEPData resource if it can hold a likelihood, return its local path."""
    location, rid = resource["location"], str(resource["id"])
    file_type = resource.get("file_type") or ""
    if not location.startswith("/record/resource/") or file_type.lower() in SKIP_TYPES:
        return None
    if rid in skipped:
        return None
    target_dir = DATA / "tmp" / rid / "download"
    if target_dir.exists():  # left over from an interrupted run
        done = [p for p in target_dir.iterdir() if p.suffix != ".part"]
        if done:
            return done[0]
    with open_url(HEPDATA + location) as response:
        filename = Path(attachment_name(response) or rid).name
        lower = filename.lower()
        text = f"{filename} {resource.get('file_description')} {file_type}"
        if not (lower.endswith(".json") or (lower.endswith(ARCHIVE_EXT) and KEYWORDS.search(text))):
            skipped[rid] = filename
            return None
        print(f"    downloading {filename}")
        target_dir.mkdir(parents=True, exist_ok=True)
        part = target_dir / (filename + ".part")
        with part.open("wb") as f:
            for chunk in response.iter_content(1 << 20):
                f.write(chunk)
    part.rename(target_dir / filename)
    return target_dir / filename


def process(name, resource, path):
    """Classify a download (unpacking archives), store its JSONs gzipped in data/pyhf, data/hs3."""
    work = path.parent.parent
    if path.name.lower().endswith(".json"):
        candidates = [(path, Path(path.name))]
    else:
        unpacked = work / "unpacked"
        shutil.rmtree(unpacked, ignore_errors=True)
        try:
            unpack(path, unpacked)
        except (tarfile.TarError, zipfile.BadZipFile, OSError) as e:
            print(f"    cannot unpack {path.name}: {e}")
        prefix = Path(strip_archive_ext(path.name))
        candidates = [
            (p, prefix / p.relative_to(unpacked))
            for p in sorted(unpacked.rglob("*.json"))
            if p.is_file() and not p.is_symlink()
        ]

    result = {
        "filename": path.name,
        "size": path.stat().st_size,
        "sha256": sha256(path),
        "files": [],
        "n_jsonpatch": 0,
        "n_invalid": 0,
        "n_other": 0,
    }
    for source, relative in candidates:
        raw = source.read_bytes()
        kind = classify(raw)
        if kind not in DEST:
            result["n_invalid" if kind == "invalid" else "n_other"] += 1
            continue
        target = DATA / DEST[kind] / name / relative.with_name(relative.name + ".gz")
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("wb") as f, gzip.GzipFile("", "wb", 6, f, mtime=0) as g:
            g.write(raw)
        if kind == "jsonpatch":  # can be thousands of signal points, only count them
            result["n_jsonpatch"] += 1
            continue
        # size and checksum are those of the uncompressed, published file
        result["files"].append(
            {
                "path": str(target.relative_to(ROOT)),
                "kind": kind,
                "size": len(raw),
                "sha256": hashlib.sha256(raw).hexdigest(),
            }
        )
    cache = DATA / "cache" / name / f"{resource['id']}.json"
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_text(json.dumps(result))
    shutil.rmtree(work)
    return result


def fetch_analysis(entry, skipped):
    name = entry["link"].rstrip("/").split("/")[-1]
    record_url = entry["hepdata"].split("?")[0]
    record = get_json(record_url + "?format=json")
    recid = record["recid"]
    analysis = {
        "title": entry["title"],
        "hepdata": record_url,
        "recid": recid,
        "version": None,
        "status": "no-likelihood-found",
        "resources": [],
        "files": [],
    }
    # newest version first, older ones only if it has no likelihood
    for version in range(record["version_count"], 0, -1):
        items = get_json(f"{HEPDATA}/record/resources/{recid}/{version}")["submission_items"]
        for resource in (r for item in items for r in item["resources"]):
            result = cached(name, resource)
            if result is None:
                path = download(resource, skipped)
                if path is None:
                    continue
                result = process(name, resource, path)
            files = result.pop("files")
            analysis["resources"].append(
                {
                    "id": resource["id"],
                    "doi": resource.get("doi"),
                    "description": resource.get("file_description"),
                    "version": version,
                    **result,
                }
            )
            analysis["files"] += [{**f, "resource": resource["id"]} for f in files]
        kinds = {f["kind"] for f in analysis["files"]}
        if kinds & {"workspace", "hs3"}:
            analysis["version"] = version
            analysis["status"] = "ok" if "workspace" in kinds else "hs3-only"
            break
    if not analysis["files"] and any(r["n_invalid"] for r in analysis["resources"]):
        analysis["status"] = "invalid-json"
    return name, analysis


def main():
    old = json.loads(MANIFEST.read_text())["analyses"] if MANIFEST.exists() else {}
    skipped_path = DATA / "cache" / "skipped.json"
    skipped = json.loads(skipped_path.read_text()) if skipped_path.exists() else {}

    analyses = {}
    for entry in get_json(INDEX):
        print(entry["link"].rstrip("/").split("/")[-1])
        try:
            name, analysis = fetch_analysis(entry, skipped)
        finally:
            skipped_path.parent.mkdir(parents=True, exist_ok=True)
            skipped_path.write_text(json.dumps(skipped))
        # keep validation results for unchanged files
        validated = {
            (f["path"], f["sha256"]): f["validation"]
            for f in old.get(name, {}).get("files", [])
            if "validation" in f
        }
        for f in analysis["files"]:
            if (f["path"], f["sha256"]) in validated:
                f["validation"] = validated[f["path"], f["sha256"]]
        analyses[name] = analysis
        kinds = [f["kind"] for f in analysis["files"]]
        print(
            f"    {analysis['status']}: {kinds.count('workspace')} workspaces, "
            f"{kinds.count('patchset')} patchsets, {kinds.count('hs3')} hs3"
        )

    MANIFEST.write_text(json.dumps({"index": INDEX, "analyses": analyses}, indent=1) + "\n")


if __name__ == "__main__":
    main()
