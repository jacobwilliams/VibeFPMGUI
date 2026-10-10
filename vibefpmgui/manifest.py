"""Reading fpm manifests and resolving the dependency tree (no Qt in here).

The tree is built by starting at the main ``fpm.toml`` and following every
dependency to its own ``fpm.toml``.  Where a dependency lives on disk is taken
from (in order of preference):

1. ``path = "..."`` in the dependency spec (relative to the declaring package),
2. ``build/cache.toml`` written by fpm after it resolves the dependencies,
3. the default fpm download location ``build/dependencies/<name>``.

Dependencies that have not been downloaded yet are still shown, but flagged as
missing (run ``fpm update`` or ``fpm build`` to fetch them).
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path

# Names that fpm treats as metapackages when given as ``name = "*"``.
METAPACKAGES = {
    "openmp", "stdlib", "minpack", "mpi", "hdf5", "netcdf", "blas", "lapack",
}

# Node kinds
ROOT = "root"
GIT = "git"
PATH = "path"
REGISTRY = "registry"
META = "metapackage"
MISSING = "missing"


@dataclass
class Package:
    """A node in the dependency graph."""

    name: str
    kind: str
    version: str = ""
    directory: Path | None = None
    manifest: Path | None = None
    license: str = ""
    author: str = ""
    description: str = ""
    homepage: str = ""
    git: str = ""
    git_ref: str = ""
    git_rev: str = ""
    error: str = ""


@dataclass
class Edge:
    parent: str
    child: str
    dev: bool = False  # from [dev-dependencies] / test or example targets


@dataclass
class DependencyGraph:
    root: str
    packages: dict[str, Package] = field(default_factory=dict)
    edges: list[Edge] = field(default_factory=list)

    def children(self, name: str) -> list[Edge]:
        return [e for e in self.edges if e.parent == name]

    def parents(self, name: str) -> list[Edge]:
        return [e for e in self.edges if e.child == name]


def load_toml(path: Path) -> dict:
    with open(path, "rb") as f:
        return tomllib.load(f)


def _read_version(value, package_dir: Path) -> str:
    """fpm allows ``version`` to be a string or the name of a file holding it."""
    if not isinstance(value, str):
        return ""
    candidate = package_dir / value
    if candidate.is_file():
        try:
            return candidate.read_text().strip().splitlines()[0].strip()
        except (OSError, IndexError):
            return value
    return value


def _as_text(value) -> str:
    if isinstance(value, list):
        return ", ".join(str(v) for v in value)
    return "" if value is None else str(value)


def _collect_deps(data: dict, is_root: bool) -> list[tuple[str, object, bool]]:
    """Return ``(name, spec, dev)`` for every dependency a manifest declares.

    Dev-dependencies and per-target dependencies are only used by fpm for the
    root project, so they are ignored for downloaded packages.
    """
    deps: dict[str, tuple[object, bool]] = {}

    def add(table, dev):
        if not isinstance(table, dict):
            return
        for name, spec in table.items():
            # A normal dependency wins over a dev one of the same name.
            if name not in deps or (deps[name][1] and not dev):
                deps[name] = (spec, dev)

    add(data.get("dependencies"), False)
    if is_root:
        add(data.get("dev-dependencies"), True)
        for target in data.get("executable", []) or []:
            add(target.get("dependencies"), False)
        for section in ("test", "example"):
            for target in data.get(section, []) or []:
                add(target.get("dependencies"), True)
    return [(name, spec, dev) for name, (spec, dev) in deps.items()]


def _fill_from_manifest(pkg: Package, data: dict) -> None:
    if pkg.directory is not None:
        pkg.version = _read_version(data.get("version"), pkg.directory) or pkg.version
    pkg.license = _as_text(data.get("license"))
    pkg.author = _as_text(data.get("author"))
    pkg.description = _as_text(data.get("description"))
    pkg.homepage = _as_text(data.get("homepage"))


def resolve(manifest_path: str | Path) -> DependencyGraph:
    """Build the dependency graph for the project whose manifest is given.

    Raises ``OSError``/``tomllib.TOMLDecodeError`` if the main manifest can't
    be read; problems with dependency manifests are recorded on the nodes.
    """
    manifest_path = Path(manifest_path).resolve()
    root_dir = manifest_path.parent
    data = load_toml(manifest_path)

    root_name = str(data.get("name", root_dir.name))
    graph = DependencyGraph(root=root_name)
    root = Package(root_name, ROOT, directory=root_dir, manifest=manifest_path)
    _fill_from_manifest(root, data)
    graph.packages[root_name] = root

    cache: dict = {}
    cache_file = root_dir / "build" / "cache.toml"
    if cache_file.is_file():
        try:
            cache = load_toml(cache_file)
        except (OSError, tomllib.TOMLDecodeError):
            cache = {}

    # Breadth-first so the first spec seen for a package (closest to the
    # root) is the one used, which matches how fpm resolves duplicates.
    queue: list[tuple[Package, dict]] = [(root, data)]
    while queue:
        parent, parent_data = queue.pop(0)
        is_root = parent is root
        for name, spec, dev in _collect_deps(parent_data, is_root):
            graph.edges.append(Edge(parent.name, name, dev))
            if name in graph.packages:
                continue
            pkg, pkg_data = _locate(name, spec, parent, root_dir, cache.get(name, {}))
            graph.packages[name] = pkg
            if pkg_data is not None:
                queue.append((pkg, pkg_data))
    return graph


def _locate(name, spec, parent: Package, root_dir: Path, cached: dict):
    """Create the Package for a dependency and load its manifest if present."""
    if isinstance(spec, str):
        # ``openmp = "*"`` style metapackage (or a bare registry version).
        kind = META if name in METAPACKAGES else REGISTRY
        return Package(name, kind, version="" if spec == "*" else spec), None
    if not isinstance(spec, dict):
        return Package(name, MISSING, error=f"Unrecognised dependency spec: {spec!r}"), None

    pkg = Package(name, GIT)
    directory: Path | None = None
    if "path" in spec:
        pkg.kind = PATH
        directory = ((parent.directory or root_dir) / str(spec["path"])).resolve()
    else:
        if "git" in spec:
            pkg.git = str(spec["git"])
            for key in ("tag", "branch", "rev"):
                if key in spec:
                    pkg.git_ref = f"{key} {spec[key]}"
                    break
        else:
            pkg.kind = REGISTRY
            pkg.version = str(spec.get("v", ""))
        if cached.get("proj-dir"):
            directory = (root_dir / str(cached["proj-dir"])).resolve()
        else:
            directory = root_dir / "build" / "dependencies" / name
        pkg.git = pkg.git or str(cached.get("git", ""))
        pkg.git_rev = str(cached.get("rev", ""))
    if cached.get("version") and not pkg.version:
        pkg.version = str(cached["version"])

    manifest = directory / "fpm.toml"
    if not manifest.is_file():
        pkg.directory = directory
        if pkg.kind == PATH:
            pkg.error = f"No fpm.toml found in {directory}"
        else:
            pkg.error = "Not downloaded yet (run fpm update or fpm build)."
        pkg.kind = MISSING
        return pkg, None

    pkg.directory = directory
    pkg.manifest = manifest
    try:
        data = load_toml(manifest)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        pkg.error = f"Could not read {manifest}: {exc}"
        return pkg, None
    _fill_from_manifest(pkg, data)
    return pkg, data


def project_features(data: dict) -> dict[str, list[str]]:
    """Features declared in a manifest's ``[features]`` table, each with the
    (dotted) properties it sets, e.g. ``{"mpi": ["dependencies.mpi", "flags"]}``."""
    table = data.get("features")
    if not isinstance(table, dict):
        return {}

    def paths(value, prefix=""):
        if isinstance(value, dict):
            out = []
            for key, sub in value.items():
                out += paths(sub, f"{prefix}.{key}" if prefix else str(key))
            return out
        return [prefix]

    return {str(name): paths(value) for name, value in table.items()}


def project_profiles(data: dict) -> dict[str, list[str]]:
    """Feature profiles declared in ``[profiles]`` (name -> list of features)."""
    table = data.get("profiles")
    if not isinstance(table, dict):
        return {}
    return {str(name): [str(f) for f in value] if isinstance(value, list) else []
            for name, value in table.items()}
