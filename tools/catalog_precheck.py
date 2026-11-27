#!/usr/bin/env python3
"""Mechanical pre-check of a catalog PR, as one Markdown comment (docs/CATALOG_REVIEW.md, "Automated pre-check").

  python3 tools/catalog_precheck.py --base BASE --head HEAD [--out report.md] [--body pr-body.txt] [--registry-check]

BASE and HEAD are git refs of the checkout it runs in (HEAD fetched, never checked out). The report covers what a reviewer
used to do by hand for each added or changed entry in catalog/plugins/:
  - the tier (docs/CATALOG_REVIEW.md, "Who decides what") and any trust field or non-catalog file the PR touches;
  - the entry's own rules (catalog_build.check_entry), plain-ASCII summary;
  - the screenshot: a raw.githubusercontent.com URL pinned to a tag or commit, in the plugin's own repo, that loads as an image;
  - the newest release's zips: SHA-256, `catalog_check.py --catalog` (errors and warnings, run with these tools), files that
    look like ROMs or samples, the licence text in the zip against `license`;
  - a "Tested on" claim in the summary against the plugin repo's tested.json (a record with the entry's id).
--registry-check also runs `catalog_build.py --check-registry` (these tools) on the PR's catalog/ files, copied out of git blobs
into a temp directory. What it leaves to a person is listed at the end: names and branding, licence doubt, GPL source, trust fields.

Safe to run on a fork's PR: it reads the PR's files only through `git show` (a symlink or a non-regular file is skipped), parses
them as JSON, and never executes anything from the PR or from a downloaded zip (zips are read with zipfile and catalog_check).
It always exits 0 once the report is written: it informs, it does not gate. Standard library only.
"""
import argparse
import fnmatch
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import catalog_build  # noqa: E402
import catalog_check  # noqa: E402

MARK = "<!-- catalog-precheck -->"
LIGHT = {"style", "tags", "summary", "screenshot", "tip"}   # fields a tier 1 update may change
TRUST = ("featured", "tier", "depends", "conflicts", "devices", "retire", "flags_reviewed", "previous_repos")   # tier 3
TRUST_FILES = {"downloads.json", "yanked.json", "confirmed.json", "added.json", "patches.json"}   # maintainers only
BRANCHES = {"main", "master", "head", "develop", "dev", "trunk"}
SUSPECT = (".rom", ".bin", ".eprom", ".mid", ".midi", ".sf2", ".syx", ".wav", ".aif", ".aiff", ".flac")
MAX_ZIP = 120 << 20        # bytes: a larger release zip is listed, not downloaded
MAX_RELEASE_CHECKS = 8     # entries whose release is downloaded per run; the rest say so
TESTED_CLAIM = re.compile(r"\b(tested|verified)\s+(on|with)\b", re.I)
RAW = re.compile(r"https://raw\.githubusercontent\.com/([^/]+)/([^/]+)/([^/]+)/(.+)")
HEX = re.compile(r"[0-9a-f]{7,40}")
SHA = re.compile(r"\b[0-9a-f]{64}\b")
PASS, NOTE, CHECK, FAIL = "pass", "note", "CHECK", "FAIL"
ICON = {PASS: "✅", NOTE: "ℹ️", CHECK: "⚠️", FAIL: "❌"}


# ---------------------------------------------------------------- git (the PR is data: read through blobs only)
def git(*args):
    r = subprocess.run(["git"] + list(args), capture_output=True, text=True)
    return r.stdout if r.returncode == 0 else None


def read_blob(ref, path):
    """The text of a regular file at ref:path, or None: missing, a symlink, a submodule or not UTF-8."""
    ls = git("ls-tree", ref, "--", path)
    if not ls:
        return None
    mode = ls.split()[0]
    if mode not in ("100644", "100755"):
        return None
    out = subprocess.run(["git", "cat-file", "blob", ref + ":" + path], capture_output=True)
    if out.returncode != 0:
        return None
    try:
        return out.stdout.decode("utf-8")
    except UnicodeDecodeError:
        return None


def diff_files(base, head):
    """[(status, path)] this PR changes (three-dot: the PR's own changes, not what main gained since)."""
    out = git("diff", "--name-status", "--no-renames", base + "..." + head)
    if out is None:
        out = git("diff", "--name-status", "--no-renames", base, head) or ""
    rows = []
    for line in out.splitlines():
        parts = line.split("\t")
        if len(parts) == 2:
            rows.append((parts[0][0], parts[1]))
    return rows


def registry_check(head):
    """The output of `catalog_build.py --check-registry` on head's catalog/ (regular files only), run with these tools."""
    root = tempfile.mkdtemp(prefix="precheck-catalog-")
    names = git("ls-tree", "-r", "--name-only", head, "--", "catalog") or ""
    for path in names.splitlines():
        if ".." in path.split("/") or not path.startswith("catalog/"):
            continue
        text = read_blob(head, path)
        if text is None:
            continue
        dest = os.path.join(root, path)
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        with open(dest, "w", encoding="utf-8") as f:
            f.write(text)
    c = os.path.join(root, "catalog")
    r = subprocess.run([sys.executable, os.path.join(HERE, "catalog_build.py"), "--check-registry",
                        "--registry", os.path.join(c, "plugins"), "--collections", os.path.join(c, "collections"),
                        "--listings", os.path.join(c, "listings"), "--yanked", os.path.join(c, "yanked.json"),
                        "--added", os.path.join(c, "added.json"), "--confirmed", os.path.join(c, "confirmed.json")],
                       capture_output=True, text=True, cwd=root)
    return ((r.stderr + r.stdout).replace(root, "<PR>").strip() or "no output") + ("" if r.returncode == 0 else "\n(exit %d)" % r.returncode)


def repo_entry_count(head, repo):
    """How many registry entries at head name this repo (a repo with several needs "id" in its tested.json)."""
    out = git("grep", "-l", "-i", "-E", r'"repo"[[:space:]]*:[[:space:]]*"%s"' % re.escape(repo), head, "--", "catalog/plugins")
    return len([l for l in (out or "").splitlines() if l.strip()]) or 1


# ---------------------------------------------------------------- pure checks
def tier_of_change(old, new):
    """(tier, why) for one entry: 2 new entry, 1 only light fields changed, 3 a trust field or the owner changed, 2 other."""
    if old is None:
        return 2, "new entry"
    keys = sorted(k for k in set(old) | set(new) if old.get(k) != new.get(k))
    if not keys:
        return 1, "no change"
    trust = [k for k in keys if k in TRUST or k in ("repo", "author") or (k == "collection")]
    if trust:
        return 3, "changes " + ", ".join(trust) + ": a maintainer decides"
    if set(keys) <= LIGHT:
        return 1, "entry update (" + ", ".join(keys) + ")"
    return 2, "changes " + ", ".join(k for k in keys if k not in LIGHT) + " (release re-checked)"


def screenshot_rows(e, tag_names, probe, own_repos):
    """Rows for the entry's screenshot. tag_names: set of tags of the URL's repo, or None when they could not be read."""
    url = e.get("screenshot")
    if not url:
        return [(NOTE, "no `screenshot` (optional; the card and the Plugin Manager show it)")]
    m = RAW.fullmatch(url)
    rows = []
    if not m:
        rows.append((CHECK, "screenshot is not a `raw.githubusercontent.com` URL pinned to a tag or commit"))
    else:
        owner, repo, ref, _path = m.groups()
        if ref == "refs" and _path.startswith(("heads/", "tags/")):
            ref = _path.split("/")[1]
            kind_branch = _path.startswith("heads/")
        else:
            kind_branch = False
        if kind_branch or ref.lower() in BRANCHES:
            rows.append((FAIL, "screenshot is pinned to the branch `%s`: the image can change under the card; pin it to a tag or commit" % ref))
        elif HEX.fullmatch(ref):
            rows.append((PASS if len(ref) == 40 else NOTE,
                         "screenshot pinned to commit `%s`" % ref[:12] + ("" if len(ref) == 40 else " (a short SHA: the full 40 characters cannot become ambiguous)")))
        elif tag_names is not None and ref in tag_names:
            rows.append((PASS, "screenshot pinned to tag `%s`" % ref))
        elif tag_names is None:
            rows.append((CHECK, "screenshot ref `%s`: could not list the repo's tags to confirm it is a tag" % ref))
        else:
            rows.append((CHECK, "screenshot ref `%s` is not a tag of `%s/%s`: pin it to a tag or a commit" % (ref, owner, repo)))
        if ("%s/%s" % (owner, repo)).lower() not in own_repos:
            rows.append((CHECK, "screenshot is from `%s/%s`, not this entry's repo: check it is the author's own image" % (owner, repo)))
    status, ctype, size, dims = probe(url)
    if status != 200:
        rows.append((FAIL, "screenshot does not load (HTTP %s)" % status))
    elif not (ctype or "").startswith("image/"):
        rows.append((FAIL, "screenshot is `%s`, not an image" % ctype))
    else:
        rows.append((PASS, "screenshot loads (%s%s, %d KiB): **open it** and check it shows the plugin's MPC page and no manufacturer logo"
                     % (ctype, ", %dx%d" % dims if dims else "", size // 1024)))
    return rows


def license_families(text):
    """The licence families a licence text is: GPL, LGPL, AGPL, MIT, Apache, BSD, MPL (a set)."""
    t = " ".join(text.split())
    fam = set()
    if re.search(r"GNU AFFERO GENERAL PUBLIC LICENSE", t, re.I):
        fam.add("AGPL")
    elif re.search(r"GNU LESSER GENERAL PUBLIC LICENSE", t, re.I):
        fam.add("LGPL")
    elif re.search(r"GNU GENERAL PUBLIC LICENSE", t, re.I):
        fam.add("GPL")
    if "Permission is hereby granted, free of charge" in t:
        fam.add("MIT")
    if re.search(r"Apache License", t):
        fam.add("Apache")
    if "Redistribution and use in source and binary forms" in t:
        fam.add("BSD")
    if "Mozilla Public License" in t:
        fam.add("MPL")
    return fam


def spdx_families(lic):
    """The families named by an SPDX expression ("MIT AND GPL-3.0-only" -> {MIT, GPL})."""
    out = set()
    for tok in re.split(r"[\s()]+", lic):
        if tok in ("", "AND", "OR", "WITH"):
            continue
        for name in ("AGPL", "LGPL", "GPL", "MIT", "Apache", "BSD", "MPL"):
            if tok.startswith(name):
                out.add(name)
                break
    return out


def zip_scan(zpath):
    """{licences: {name: families}, suspicious: [names], skins: n} of a release zip, read without extracting."""
    lic, sus, skins = {}, [], 0
    with zipfile.ZipFile(zpath) as zf:
        for info in zf.infolist():
            if info.is_dir():
                continue
            name, base = info.filename, os.path.basename(info.filename)
            low = base.lower()
            if low.startswith(("license", "licence", "copying", "notice")) and info.file_size < (1 << 20):
                lic[name] = license_families(zf.read(name).decode("utf-8", "replace"))
            if low.endswith(SUSPECT):
                sus.append(name)
            if "/Plugin Skins/" in "/" + name and low.endswith(".json"):
                skins += 1
    return {"licences": lic, "suspicious": sus, "skins": skins}


def licence_rows(e, scan):
    rows = []
    want = spdx_families(e["license"])
    have = set()
    for fams in scan["licences"].values():
        have |= fams
    names = ", ".join(sorted(os.path.basename(n) for n in scan["licences"])) or "none"
    copyleft = bool(want & {"GPL", "LGPL", "AGPL"})
    if not scan["licences"]:
        rows.append((CHECK if copyleft else NOTE, "the zip carries no licence text (%s): the licence should travel with the binary%s"
                     % (e["license"], "; it is required for a GPL or LGPL plugin" if copyleft else "")))
    elif want and not (want & have):
        rows.append((CHECK, "`license` is %s but the licence text in the zip reads as %s (files: %s)"
                     % (e["license"], "/".join(sorted(have)) or "unrecognised", names)))
    else:
        rows.append((PASS, "licence text in the zip (%s) is consistent with `license` %s" % (names, e["license"])))
    return rows


def tested_rows(e, records, repo_entries):
    claim = bool(TESTED_CLAIM.search(e.get("summary", "")))
    mine = [r for r in records if isinstance(r, dict) and (r.get("id") == e["id"] or ("id" not in r and repo_entries == 1))]
    rows = []
    if claim and not mine:
        rows.append((CHECK, "the summary claims testing, but `tested.json` has no record for `%s`%s"
                     % (e["id"], " (this repo has %d entries, so each record needs an `\"id\"`)" % repo_entries if repo_entries > 1 else "")))
    elif claim:
        rows.append((PASS, "summary's testing claim has a `tested.json` record (%s)"
                     % "; ".join("%s %s" % (r.get("device", "?"), r.get("version", "?")) for r in mine[:3])))
    elif mine:
        rows.append((NOTE, "`tested.json` has %d record(s) for this entry (it lists as Verified once the newest release is covered)" % len(mine)))
    if repo_entries > 1 and any(isinstance(r, dict) and "id" not in r for r in records):
        rows.append((NOTE, "some `tested.json` records have no `\"id\"`: in a repo with several entries they count for none of them"))
    return rows


# ---------------------------------------------------------------- network (injected in tests)
def http_probe(url):
    """(status, content-type, bytes, (w, h) or None) of a URL; the body is read up to 20 MB."""
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "mpc-vst-catalog-precheck"})
        with urllib.request.urlopen(req, timeout=60) as r:
            body = r.read(20 << 20)
            ctype = r.headers.get("Content-Type", "")
            dims = None
            if body[:8] == b"\x89PNG\r\n\x1a\n" and len(body) >= 24:
                dims = (int.from_bytes(body[16:20], "big"), int.from_bytes(body[20:24], "big"))
            return r.status, ctype.split(";")[0].strip(), len(body), dims
    except urllib.error.HTTPError as ex:
        return ex.code, "", 0, None
    except Exception:
        return 0, "", 0, None


def newest_release(gh, e):
    """(release, [assets]) for the newest published release with an asset matching the entry's asset_pattern."""
    pat = e.get("asset_pattern", catalog_build.ZIP_PATTERN)
    other = pat.replace("armv7", "aarch64") if "armv7" in pat else pat.replace("aarch64", "armv7")
    for rel in gh.list_releases(e["repo"]):
        if rel.get("draft"):
            continue
        assets = rel.get("assets", [])
        hit = [a for a in assets if fnmatch.fnmatch(a["name"], pat)]
        if hit:
            hit += [a for a in assets if other != pat and fnmatch.fnmatch(a["name"], other)]
            return rel, hit
    return None, []


def release_rows(e, gh, work, body_hashes):
    rows = []
    if e.get("distribution", "release") != "release":
        return [(NOTE, "distribution `%s`: no release zip to download (build steps are checked by a person)" % e["distribution"])]
    try:
        rel, assets = newest_release(gh, e)
    except Exception as ex:
        return [(CHECK, "could not list the releases of `%s` (%s)" % (e["repo"], ex))]
    if not rel:
        sdk = [a for r in gh.list_releases(e["repo"]) for a in r.get("assets", []) if a["name"].endswith("-mpc-sdk.zip")]
        if sdk:
            return [(NOTE, "no per-build zip, but an Akai SDK zip (`%s`): the catalog converts it itself (docs/CATALOG.md)" % sdk[0]["name"])]
        return [(FAIL, "no release has an asset matching `%s`" % e.get("asset_pattern", catalog_build.ZIP_PATTERN))]
    rows.append((NOTE if rel.get("prerelease") else PASS,
                 "release `%s`%s, %d matching zip(s)" % (rel.get("tag_name"), " (a pre-release: the entry starts on the beta channel)" if rel.get("prerelease") else "", len(assets))))
    for a in assets:
        label = "`%s`" % a["name"]
        if a.get("size", 0) > MAX_ZIP:
            rows.append((CHECK, "%s is %d MiB: not downloaded here" % (label, a["size"] >> 20)))
            continue
        dest = os.path.join(work, a["name"])
        try:
            gh.download(a, dest)
        except Exception as ex:
            rows.append((FAIL, "%s could not be downloaded (%s)" % (label, ex)))
            continue
        with open(dest, "rb") as f:
            sha = hashlib.sha256(f.read()).hexdigest()
        text = "SHA-256 `%s`" % sha
        if body_hashes is not None:
            text += " (matches the PR text)" if sha in body_hashes else " (not in the PR text%s)" % ("" if body_hashes else ": it names no SHA-256")
        try:
            errors, warnings, record = catalog_check.check(dest, True, e["id"], e["repo"])
            scan = zip_scan(dest)
        except zipfile.BadZipFile:
            rows.append((FAIL, "%s is not a valid zip" % label))
            continue
        if errors:
            rows.append((FAIL, "%s %s: `catalog_check.py --catalog` errors: %s" % (label, text, "; ".join(errors[:4]))))
            continue
        if warnings:
            rows.append((CHECK, "%s %s: `catalog_check.py --catalog` OK with %d warning(s): %s%s" % (
                label, text, len(warnings), "; ".join(warnings[:3]),
                " (an install template older than this repo's means a rebuild: docs/CATALOG_REVIEW.md item 9)" if any("template" in w for w in warnings) else "")))
        else:
            rows.append((PASS, "%s %s: `catalog_check.py --catalog` OK, 0 warnings" % (label, text)))
        if scan["suspicious"]:
            rows.append((CHECK, "%s holds files that may be ROMs or samples: %s" % (label, ", ".join("`%s`" % os.path.basename(n) for n in scan["suspicious"][:6]))))
        if record and record.get("flags"):
            rows.append((CHECK, "%s uses risky calls %s: a maintainer sets `flags_reviewed`" % (label, ", ".join(sorted(record["flags"])) if isinstance(record["flags"], (list, set, tuple, dict)) else record["flags"])))
        if a is assets[0]:
            rows += licence_rows(e, scan)
    return rows


# ---------------------------------------------------------------- one entry, one PR
def precheck_entry(e, old, gh, work, probe, head, body_hashes=None, release_checks=True):
    """{id, name, tier, why, rows} for one added or changed registry entry."""
    tier, why = tier_of_change(old, e)
    rows = []
    for p in catalog_build.check_entry(e):
        rows.append((FAIL, "entry: " + p))
    if not rows:
        rows.append((PASS, "entry fields valid; summary %d of %d characters" % (len(e["summary"]), catalog_build.MAX_SUMMARY)))
    if any(ord(c) > 127 for c in e.get("summary", "")):
        rows.append((CHECK, "summary has non-ASCII punctuation (plain ASCII only)"))
    own = {e["repo"].lower()} | {r.lower() for r in e.get("previous_repos", []) if isinstance(r, str)}
    tags = None
    m = RAW.fullmatch(e.get("screenshot", "") or "")
    if m:
        try:
            tags = {t["name"] for t in gh.list_tags("%s/%s" % m.group(1, 2))}
        except Exception:
            tags = None
    rows += screenshot_rows(e, tags, probe, own)
    if old is not None and tier == 1:
        rows.append((NOTE, "tier 1 update: the release is not re-checked"))
    elif release_checks:
        rows += release_rows(e, gh, work, body_hashes)
    else:
        rows.append((CHECK, "release not downloaded here (limit of %d entries per run): check it by hand" % MAX_RELEASE_CHECKS))
    try:
        records = gh.tested(e["repo"])
    except Exception:
        records = []
    rows += tested_rows(e, records, repo_entry_count(head, e["repo"]))
    return {"id": e["id"], "name": e.get("name", e["id"]), "tier": tier, "why": why, "rows": rows,
            "license": e.get("license", ""), "author": e.get("author", "")}


# ---------------------------------------------------------------- the PR
def load_entry(ref, path):
    raw = read_blob(ref, path)
    if raw is None:
        return None
    try:
        v = json.loads(raw)
    except ValueError:
        return None
    return v if isinstance(v, dict) else None


def render(results, registry_log, nonplugin):
    levels = [l for r in results for l, _ in r["rows"]]
    worst = 2 if FAIL in levels else 1 if CHECK in levels else 0
    head = ["❌ **has failures**", "⚠️ **has points to look at**", "✅ **no automated findings**"][0 if worst == 2 else 1 if worst == 1 else 2]
    out = [MARK, "## Catalog pre-check", "",
           "Automated, mechanical checks (`tools/catalog_precheck.py`): %s. This is not a review and not an approval; a person "
           "still decides the items at the end." % head, ""]
    for r in results:
        out += ["### `%s` (%s): tier %d, %s" % (r["id"], r["name"], r["tier"], r["why"]), "", "| | Check |", "|---|---|"]
        out += ["| %s | %s |" % (ICON[l], t.replace("|", "\\|")) for l, t in r["rows"]]
        out.append("")
    if registry_log:
        out += ["### Registry check", "", "```", registry_log.strip()[-1500:], "```", ""]
    out += ["### For a person", ""]
    if nonplugin:
        out += ["- Files this PR touches beyond plugin entries: " + "; ".join(nonplugin) + "."]
    for r in results:
        out.append("- `%s`: is the name `%s` free of another author's or company's name (docs/CATALOG_REVIEW.md items 4, 10)? Does the "
                   "screenshot show the plugin's own MPC page?" % (r["id"], r["name"]))
        if spdx_families(r["license"]) & {"GPL", "LGPL", "AGPL"}:
            out.append("- `%s` is %s: is the complete source of the released binary published (item 12)?" % (r["id"], r["license"]))
        if r["tier"] == 3:
            out.append("- `%s`: %s." % (r["id"], r["why"]))
    out += ["- Any licence doubt, someone else's product, or ownership doubt goes to a maintainer (\"Hand it to a maintainer\").", "",
            "<sub>Releases can change after this ran: a maintainer re-checks the head SHA and the release's SHA-256 before merging.</sub>"]
    return "\n".join(out) + "\n"


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--base", required=True)
    ap.add_argument("--head", required=True)
    ap.add_argument("--out", default="-")
    ap.add_argument("--body", help="the PR description (SHA-256s in it are compared with the zips)")
    ap.add_argument("--registry-check", action="store_true", help="also run catalog_build.py --check-registry on the PR's catalog/ files")
    a = ap.parse_args()
    gh = catalog_build.GitHub(os.environ.get("GITHUB_TOKEN"))
    body_hashes = None
    if a.body and os.path.exists(a.body):
        with open(a.body, encoding="utf-8", errors="replace") as f:
            body_hashes = set(SHA.findall(f.read().lower()))
    results, nonplugin, work = [], [], tempfile.mkdtemp(prefix="precheck-")
    checks = 0
    for status, path in diff_files(a.base, a.head):
        if status == "D":
            nonplugin.append("deletes `%s`" % path)
            continue
        if re.fullmatch(r"catalog/plugins/[^/]+\.json", path):
            e = load_entry(a.head, path)
            if e is None or not isinstance(e.get("id"), str):
                nonplugin.append("`%s` is not a valid entry file" % path)
                continue
            old = load_entry(a.base, path) if status == "M" else None
            needs = old is None or tier_of_change(old, e)[0] != 1
            allowed = needs and checks < MAX_RELEASE_CHECKS
            checks += 1 if needs else 0
            results.append(precheck_entry(e, old, gh, work, http_probe, a.head, body_hashes, release_checks=allowed))
        elif path.startswith("catalog/collections/"):
            nonplugin.append("collection `%s` (a new or changed collection is a maintainer decision)" % os.path.basename(path))
        elif path.startswith("catalog/listings/") or path == "catalog/alt_os.json":
            nonplugin.append("`%s` (Other sources and Alt OS entries always go to a maintainer)" % path)
        elif os.path.basename(path) in TRUST_FILES and path.startswith("catalog/"):
            nonplugin.append("trust file `%s` (maintainers only)" % path)
        elif path.startswith("catalog/"):
            nonplugin.append("`%s`" % path)
        else:
            nonplugin.append("`%s` (outside catalog/: tier 4, a code reviewer)" % path)
    log = registry_check(a.head) if a.registry_check else ""
    report = render(results, log, nonplugin)
    if a.out == "-":
        sys.stdout.write(report)
    else:
        with open(a.out, "w") as f:
            f.write(report)


if __name__ == "__main__":
    main()
