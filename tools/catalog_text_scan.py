#!/usr/bin/env python3
"""Scan the zips the catalog lists for device-side text the MPC cannot read: CR line endings or a UTF-8 BOM.

  tools/catalog_text_scan.py [--registry catalog/plugins] [--jobs 8] [--json report.json] [--limit N] [--quiet]
                             [--all-versions]

The device's sh, awk and sha256sum stop at a CR ("Unexpected token") and read a BOM as part of the first line, and a
package built on Windows shipped exactly that once (docs/RELEASING.md). tools/release.py and tools/release_addin.py
refuse to write such a package now, and tools/catalog_check.py reports one it is given; this scans the packages that
are already published, so the catalog can be cleaned up and a broken release re-issued.

For every registry entry it lists the repo's releases (GitHub API; set GITHUB_TOKEN above 60 requests an hour), takes
the newest release that ships a per-build zip (-mpc-armv7.zip / -mpc-aarch64.zip, or the entry's asset_pattern) and
checks install.sh, uninstall.sh, plugin_list.awk, addin-lib.sh, addin.manifest, SHA256SUMS, MODES and every other
.sh/.awk in the package. Only the zip's central directory and those members are fetched, over HTTP range requests:
nothing is downloaded whole, nothing is written to the catalog. Addins and plugins are both covered; entries that
build for themselves (no published zip) are skipped. --all-versions scans every release that ships a zip, not just
the newest (slower, and useful once, to find the packages that predate the check). A package the catalog already
yanks (catalog/yanked.json) is reported as yanked and never as a problem, so the scan asks for a re-release, not for a
yank of something already pulled. --yanked "" checks those too.

Severity follows tools/catalog_check.py: the named device files at the top of a package are problems (the device could
not install), while a payload's own .sh/.awk is a warning (the plugin may never run it on the device). Releases under
previous_repos are scanned too, and a draft release is not.

Exit status is 1 when a package has a device-text problem and 2 when the run could not read something it should
have (a network or API failure is not a defect), so a workflow can tell the two apart.
"""
import argparse
import concurrent.futures
import fnmatch
import io
import json
import os
import sys
import threading
import time
import urllib.request
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from catalog_check import DEVICE_TEXT_FILES, device_text_problems  # noqa: E402
from catalog_build import GitHub, load_registry  # noqa: E402

UA = {"User-Agent": "mpc-vst-catalog-scan"}
WHOLE_ZIP = 12 * 1024 * 1024   # a package this small is fetched in one request; a range read costs ~30 round trips


class HttpFile:
    """A remote file that zipfile can seek in, without downloading it: zipfile reads the end of the file for the
    central directory and then only the members it opens, and every read here is one HTTP range request."""

    def __init__(self, url):
        self.pos = 0
        try:
            with urllib.request.urlopen(urllib.request.Request(url, method="HEAD", headers=UA), timeout=60) as r:
                self.url, size = r.geturl(), r.headers.get("Content-Length")
            self.size = int(size)
        except Exception:   # no HEAD, no Content-Length: ask for the first byte and read the total from Content-Range
            with urllib.request.urlopen(urllib.request.Request(url, headers=dict(UA, Range="bytes=0-0")), timeout=60) as r:
                self.url = r.geturl()
                self.size = int(r.headers["Content-Range"].rsplit("/", 1)[1])

    def read(self, n=-1):
        if n is None or n < 0:
            n = self.size - self.pos
        n = min(n, self.size - self.pos)
        if n <= 0:
            return b""
        h = dict(UA, Range="bytes=%d-%d" % (self.pos, self.pos + n - 1))
        with urllib.request.urlopen(urllib.request.Request(self.url, headers=h), timeout=120) as r:
            data = r.read(n)
        self.pos += len(data)
        return data

    def seek(self, offset, whence=0):
        self.pos = max(0, {0: 0, 1: self.pos, 2: self.size}[whence] + offset)
        return self.pos

    def tell(self):
        return self.pos

    def seekable(self):
        return True

    def close(self):
        pass


def wanted(entry, name):
    """Whether this asset of a release is one users install (the per-build zips of the entry, addins included)."""
    pat = entry.get("asset_pattern")
    if not pat:
        return name.endswith(("-mpc-armv7.zip", "-mpc-aarch64.zip"))
    return fnmatch.fnmatch(name, pat) or ("armv7" in pat and fnmatch.fnmatch(name, pat.replace("armv7", "aarch64")))


def scan_asset(url, entry_id="", yanked=()):
    """(version, problems, warnings, is_yanked) for one package. The version is the one in its manifest, which is
    exactly what catalog/yanked.json keys on, so the scan skips what the catalog has already pulled."""
    f = HttpFile(url)
    if f.size <= WHOLE_ZIP:   # most packages are small: one GET here beats a seek per member
        with urllib.request.urlopen(urllib.request.Request(f.url, headers=UA), timeout=300) as r:
            src = io.BytesIO(r.read())
    else:
        src = f
    problems, warnings = [], []
    with zipfile.ZipFile(src) as z:
        names = [n for n in z.namelist() if not n.endswith("/")]
        manifest = next((n for n in names if n.count("/") == 1 and n.endswith("/mpc-plugin.json")), None)
        version = ""
        if manifest:
            try:
                version = str(json.loads(z.read(manifest)).get("version") or "")
            except (ValueError, KeyError, TypeError):
                version = ""
        for n in names:
            base = n.rsplit("/", 1)[-1]
            if n.count("/") == 1 and base in DEVICE_TEXT_FILES:   # the root files the device runs: an error
                problem = device_text_problems(n, z.read(n))
                if problem:
                    problems.append(problem)
            elif base.lower().endswith((".sh", ".awk")):          # a payload's own script: a warning, as in catalog_check
                problem = device_text_problems(n, z.read(n))
                if problem:
                    warnings.append(problem)
    pulled = bool(version and entry_id and ("%s@%s" % (entry_id, version) in yanked or "%s@*" % entry_id in yanked))
    return version, problems, warnings, pulled


def packages_of(src, entry, cache, all_versions=False):
    """[(repo, release tag, assets)] of the releases that ship this entry's zips: the newest one, or all of them.
    previous_repos (a plugin that moved) is read too; a tag the main repo is read from wins, as in the catalog build."""
    found, seen = [], set()
    for repo in [entry["repo"]] + [r for r in entry.get("previous_repos") or [] if r != entry["repo"]]:
        if repo not in cache:
            try:
                cache[repo] = src.list_releases(repo)
            except Exception as e:
                cache[repo] = e
        rels = cache[repo]
        if isinstance(rels, Exception):
            if repo == entry["repo"]:
                raise rels
            continue   # an old home that is gone: the main repo's releases are what matter
        for rel in rels:   # newest first
            assets = [a for a in rel.get("assets", []) if wanted(entry, a["name"])]
            tag = rel.get("tag_name") or ""
            if assets and tag not in seen:
                seen.add(tag)
                found.append((repo, tag, assets))
                if not all_versions:
                    break
    return found


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--registry", default=os.path.join(os.path.dirname(HERE), "catalog", "plugins"))
    ap.add_argument("--jobs", type=int, default=8, help="packages fetched at once (default 8)")
    ap.add_argument("--limit", type=int, help="only the first N entries, for a quick look")
    ap.add_argument("--json", metavar="FILE", help="write the report as JSON")
    ap.add_argument("--quiet", action="store_true", help="only the problem list and the summary")
    ap.add_argument("--all-versions", action="store_true", help="every release with a zip, not only the newest")
    ap.add_argument("--yanked", default=os.path.join(os.path.dirname(HERE), "catalog", "yanked.json"),
                    metavar="FILE", help="packages pulled from the catalog, reported separately ('' to check them too)")
    a = ap.parse_args()

    yanked = set()
    if a.yanked:
        try:
            with open(a.yanked) as f:
                yanked = set(json.load(f))
        except (OSError, ValueError) as ex:
            sys.exit("cannot read --yanked %s: %s" % (a.yanked, ex))

    entries, problems = load_registry(a.registry)
    if problems:
        for f, p in problems:
            print("registry: %s: %s" % (f, p), file=sys.stderr)
        sys.exit("the registry has problems: fix them first (tools/catalog_build.py --check-registry)")
    entries = [e for e in entries if e.get("distribution") != "build-yourself"]
    if a.limit:
        entries = entries[:a.limit]

    src = GitHub(os.environ.get("GITHUB_TOKEN"))
    cache, jobs, skipped, failed = {}, [], [], []
    for i, e in enumerate(entries, 1):
        if not a.quiet and (i % 25 == 0 or i == len(entries)):
            print("listing releases: %d/%d" % (i, len(entries)), file=sys.stderr, flush=True)
        try:
            found = packages_of(src, e, cache, a.all_versions)
        except Exception as ex:
            failed.append((e["id"], e.get("repo"), "cannot list releases: %s" % ex))
            continue
        if not found:
            skipped.append((e["id"], e.get("repo"), "no release with a per-build zip"))
            continue
        for repo, tag, assets in found:
            for asset in assets:
                jobs.append((e, repo, tag, asset))

    print("checking %d packages from %d entries" % (len(jobs), len(entries)), file=sys.stderr, flush=True)
    report, lock = [], threading.Lock()
    done = [0]

    def one(job):
        e, repo, tag, asset = job
        version, found, warned, pulled, error = "", [], [], False, ""
        for attempt in (1, 2):   # a dropped connection is not a defect: try once more before calling it unreadable
            try:
                version, found, warned, pulled = scan_asset(asset["browser_download_url"], e["id"], yanked)
                error = ""
                break
            except Exception as ex:
                error = "could not read the zip: %s" % ex
                if attempt == 1:
                    time.sleep(2)
        with lock:
            done[0] += 1
            if not a.quiet and done[0] % 25 == 0:
                print("checked %d/%d" % (done[0], len(jobs)), file=sys.stderr, flush=True)
        return {"id": e["id"], "kind": e.get("kind"), "repo": repo, "tag": tag, "version": version,
                "asset": asset["name"], "url": asset["browser_download_url"], "problems": found,
                "warnings": warned, "yanked": pulled, "error": error}

    with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, a.jobs)) as ex:
        report = list(ex.map(one, jobs))

    pulled = [r for r in report if r["yanked"]]
    bad = [r for r in report if r["problems"] and not r["yanked"]]
    unreadable = [r for r in report if r["error"] and not r["yanked"]]
    warned = [r for r in report if r["warnings"] and not r["yanked"]]
    for r in bad:
        print("PROBLEM %s (%s) %s %s" % (r["id"], r["kind"], r["repo"], r["asset"]))
        for p in r["problems"]:
            print("        " + p)
    for r in unreadable:
        print("UNREADABLE %s (%s) %s %s" % (r["id"], r["kind"], r["repo"], r["asset"]))
        print("        " + r["error"])
    for r in warned:
        print("WARNING %s (%s) %s %s" % (r["id"], r["kind"], r["repo"], r["asset"]))
        for w in r["warnings"]:
            print("        " + w)
    for r in pulled:
        if not a.quiet:
            print("yanked %s %s: not reported (catalog/yanked.json)" % (r["id"], r["version"] or r["asset"]),
                  file=sys.stderr)
    for i, repo, why in skipped:
        if not a.quiet:
            print("skipped %s (%s): %s" % (i, repo, why), file=sys.stderr)
    for i, repo, why in failed:
        print("could not read %s (%s): %s" % (i, repo, why), file=sys.stderr)
    kinds = sorted({r["kind"] for r in report if not r["yanked"]})
    print("%d packages checked%s, %d clean, %d with problems, %d with warnings, %d yanked, %d unreadable, "
          "%d entries skipped"
          % (len(report) - len(pulled), (" (" + ", ".join(kinds) + ")") if kinds else "",
             len(report) - len(pulled) - len(bad) - len(unreadable), len(bad), len(warned), len(pulled),
             len(unreadable), len(skipped)))
    if a.json:
        with open(a.json, "w", newline="\n") as f:
            json.dump({"checked": report, "skipped": skipped, "failed": failed}, f, indent=1)
            f.write("\n")
    if bad:
        sys.exit(1)
    if unreadable or failed:
        print("not a device-text verdict: %d package(s) and %d entr%s could not be read, so rerun before trusting the "
              "rest" % (len(unreadable), len(failed), "y" if len(failed) == 1 else "ies"), file=sys.stderr)
        sys.exit(2)


if __name__ == "__main__":
    main()
