"""Where modules come from: a GitHub zip download (no git needed) or a
local folder (KIT_SOURCE / --source, used for testing)."""
import io
import os
import tempfile
import urllib.request
import zipfile

from . import system

DEFAULT_REPO = "kevaldeepak/kit"
DEFAULT_REF = "main"
REPO_DIR = os.path.join(system.CACHE_DIR, "repo")


def default_source():
    repo = os.environ.get("KIT_REPO") or DEFAULT_REPO
    ref = os.environ.get("KIT_REF") or DEFAULT_REF
    return f"github:{repo}@{ref}"


def describe(src):
    return src[len("github:"):] if src.startswith("github:") else src


def refresh(src):
    """Copy the latest snapshot of `src` into the cache. Raises on failure."""
    if src.startswith("github:"):
        repo, _, ref = src[len("github:"):].partition("@")
        url = f"https://codeload.github.com/{repo}/zip/refs/heads/{ref or DEFAULT_REF}"
        req = urllib.request.Request(url, headers={"User-Agent": "kit/1.0"})
        with urllib.request.urlopen(req, timeout=60) as r:
            data = r.read()
        with tempfile.TemporaryDirectory() as tmp:
            zipfile.ZipFile(io.BytesIO(data)).extractall(tmp)
            tops = [d for d in os.listdir(tmp) if os.path.isdir(os.path.join(tmp, d))]
            if len(tops) != 1:
                raise RuntimeError("unexpected archive layout")
            system.copy_tree(os.path.join(tmp, tops[0]), REPO_DIR)
    else:
        path = os.path.abspath(os.path.expanduser(src))
        if not os.path.isdir(os.path.join(path, "modules")):
            raise RuntimeError(f"{path} doesn't look like a kit repo (no modules/ folder)")
        if os.path.normcase(path) != os.path.normcase(REPO_DIR):
            system.copy_tree(path, REPO_DIR)


def manifests(root=REPO_DIR):
    """{name: manifest} for every module in the snapshot."""
    out = {}
    mdir = os.path.join(root, "modules")
    if not os.path.isdir(mdir):
        return out
    for name in sorted(os.listdir(mdir)):
        mf = os.path.join(mdir, name, "module.json")
        if os.path.isfile(mf):
            m = system.read_json(mf)
            m.setdefault("name", name)
            m.setdefault("version", "0.0.0")
            m.setdefault("description", "")
            m.setdefault("requires", [])
            m.setdefault("platforms", ["linux", "macos", "windows"])
            m["_dir"] = os.path.join(mdir, name)
            out[m["name"]] = m
    return out


def core_version(root=REPO_DIR):
    p = os.path.join(root, "kit", "__init__.py")
    for line in system.read_text(p, "").splitlines():
        if line.startswith("VERSION"):
            return line.split("=", 1)[1].strip().strip("\"'")
    return "0.0.0"
