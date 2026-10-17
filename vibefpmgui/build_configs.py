"""Build configurations: named sets of Build options for a project, kept in a
JSON file beside its fpm.toml (`FILE_NAME` unless another is opened), so
they can be shared (e.g. committed with the project).

The file::

    {
      "vibefpm": 1,
      "configurations": [
        {"name": "release (ifx)", "profile": "release", "features": [],
         "compiler": "ifx", "flags": "", "target": "", "extra_args": "",
         "program_args": "", "parallel_tests": 4, "coverage": false,
         "verbose": false}
      ]
    }

A configuration has every Build option but the fpm executable (a path on
this computer, kept in the app's settings). An option left out of a
configuration has its default; unknown keys are kept as they are.

The file's "options" (one set, for every configuration in it) are the GUI's
for the project: see `vibefpmgui.project_options`.
"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path

FILE_NAME = "vibefpm.json"
FORMAT = 1  # the file's "vibefpm" version
DEFAULT_PROFILE = "(default)"  # (as the Profile list shows it; "" in the file)


class ConfigError(Exception):
    """A configuration file that can't be read (the message says why)."""


@dataclass
class Configuration:
    name: str
    profile: str = DEFAULT_PROFILE
    features: list[str] = field(default_factory=list)
    compiler: str = ""
    flags: str = ""
    target: str = ""
    extra_args: str = ""
    program_args: str = ""
    parallel_tests: int = 1
    coverage: bool = False
    verbose: bool = False

    def to_json(self) -> dict:
        data = asdict(self)
        data["profile"] = "" if self.profile == DEFAULT_PROFILE else self.profile
        return data

    @classmethod
    def from_json(cls, data: dict) -> Configuration:
        """A configuration from the file (a value of the wrong type: its default)."""
        name = data.get("name")
        if not isinstance(name, str) or not name.strip():
            raise ConfigError("A configuration has no name.")
        config = cls(name.strip())
        for f in fields(cls):
            if f.name == "name" or f.name not in data:
                continue
            value, default = data[f.name], getattr(config, f.name)
            if isinstance(default, bool):
                ok = isinstance(value, bool)
            elif isinstance(default, int):
                ok = isinstance(value, int) and not isinstance(value, bool) and value >= 1
            elif isinstance(default, list):
                ok = isinstance(value, list) and all(isinstance(v, str) for v in value)
            else:
                ok = isinstance(value, str)
            if ok:
                setattr(config, f.name, list(value) if isinstance(value, list) else value)
        if not config.profile:
            config.profile = DEFAULT_PROFILE
        return config


def default_path(manifest: Path) -> Path:
    """Where a project's configurations go unless another file was opened."""
    return Path(manifest).parent / FILE_NAME


def manifest_for(path: Path) -> Path | None:
    """The fpm.toml a configuration file belongs to (beside it), if there is one."""
    manifest = Path(path).parent / "fpm.toml"
    return manifest if manifest.is_file() else None


def _read(path: Path) -> dict:
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except OSError as exc:
        raise ConfigError(f"Can't read {path}: {exc.strerror or exc}") from exc
    except (ValueError, UnicodeDecodeError) as exc:
        raise ConfigError(f"{Path(path).name} isn't valid JSON: {exc}") from exc
    if not isinstance(data, dict) or not isinstance(data.get("configurations", []), list):
        raise ConfigError(f"{Path(path).name} isn't a VibeFPM configuration file "
                          "(no \"configurations\" list).")
    return data


def load(path: Path) -> list[Configuration]:
    """The configurations in a file, in its order (none if it doesn't exist)."""
    if not Path(path).exists():
        return []
    configs, seen = [], set()
    for item in _read(path).get("configurations", []):
        if not isinstance(item, dict):
            raise ConfigError(f"{Path(path).name}: a configuration isn't an object.")
        config = Configuration.from_json(item)
        if config.name in seen:
            raise ConfigError(f"{Path(path).name}: two configurations are named “{config.name}”.")
        seen.add(config.name)
        configs.append(config)
    return configs


def _write(path: Path, data: dict):
    """Write the file whole or not at all (a temporary file, then renamed)."""
    path = Path(path)
    text = json.dumps(data, indent=2, ensure_ascii=False) + "\n"
    handle, temporary = tempfile.mkstemp(prefix=".vibefpm-", suffix=".json", dir=path.parent)
    try:
        with os.fdopen(handle, "w", encoding="utf-8", newline="\n") as f:
            f.write(text)
        os.replace(temporary, path)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise


def _with_configurations(path: Path, change) -> list[Configuration]:
    """Read the file (or start one), let ``change`` edit its list of
    configurations (as JSON objects), and write it back; the result. Other
    keys, and keys of a configuration this app doesn't know, are kept; a
    file that can't be read whole isn't written over (ConfigError)."""
    load(path)  # (every configuration checked first)
    data = _read(path) if Path(path).exists() else {}
    items = [item for item in data.get("configurations", []) if isinstance(item, dict)]
    change(items)
    data = {"vibefpm": data.get("vibefpm", FORMAT), **{k: v for k, v in data.items() if k != "vibefpm"},
            "configurations": items}
    _write(path, data)
    return load(path)


def save(path: Path, config: Configuration) -> list[Configuration]:
    """Add a configuration to the file, or replace the one with its name."""
    def change(items: list[dict]):
        new = config.to_json()
        for i, item in enumerate(items):
            if item.get("name") == config.name:
                items[i] = {**item, **new}
                return
        items.append(new)
    return _with_configurations(path, change)


def remove(path: Path, name: str) -> list[Configuration]:
    """Take the configuration named ``name`` out of the file."""
    return _with_configurations(path, lambda items: items.__setitem__(
        slice(None), [item for item in items if item.get("name") != name]))


def load_options(path: Path) -> dict | None:
    """The file's "options" (the GUI's, for the whole project: see
    `vibefpmgui.project_options`); {} if it has none, None if there's no file."""
    if not Path(path).exists():
        return None
    options = _read(path).get("options", {})
    if not isinstance(options, dict):
        raise ConfigError(f"{Path(path).name}: \"options\" isn't an object.")
    return options


def save_options(path: Path, options: dict):
    """Put ``options`` in the file (which must exist and be readable), its
    configurations and anything else in it kept."""
    load(path)  # (every configuration checked first: a file that can't be read isn't written over)
    data = _read(path)
    data["options"] = options
    _write(path, data)
