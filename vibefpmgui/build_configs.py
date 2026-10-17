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

Beside the configurations, "new_configuration" is New configuration's Build
options (those not saved as one): a configuration without a name. Then
"options" (one set, for every configuration in it) are the GUI's for the
project: see `vibefpmgui.project_options`.

`ConfigFile` is a project's file as the app has it: read whole (a file that
can't be read whole isn't used, nor written over), and written a part at a
time, the rest kept as it is.
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

    def to_json(self, named: bool = True) -> dict:
        """As the file has it (``named`` False: New configuration's, without a name)."""
        data = asdict(self)
        data["profile"] = "" if self.profile == DEFAULT_PROFILE else self.profile
        if not named:
            del data["name"]
        return data

    @classmethod
    def from_json(cls, data: dict, name: str | None = None) -> Configuration:
        """A configuration from the file (a value of the wrong type: its
        default). ``name``: one without a name in the file (New configuration's)."""
        if name is None:
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


# Build options a file's "options" had before they went to "new_configuration"
# (the app's settings keys for them): moved there when it's read.
_OLD_OPTION_FIELDS = {"profile": "profile", "compiler": "compiler", "flags": "flags", "extra": "extra_args",
                      "target": "target", "program_args": "program_args", "verbose": "verbose",
                      "coverage": "coverage", "test_jobs": "parallel_tests", "features": "features"}


def _object(data: dict, key: str, path: Path) -> dict | None:
    value = data.get(key)
    if value is not None and not isinstance(value, dict):
        raise ConfigError(f"{Path(path).name}: \"{key}\" isn't an object.")
    return value


class ConfigFile:
    """A project's configuration file, as the app has it: ``configurations``,
    New configuration's Build options (``new_configuration``: None if the
    file has none) and the GUI's ``options`` (None: there's no file, or it
    can't be read, ``error`` saying why; then none of it is used, and it
    isn't written over)."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self.configurations: list[Configuration] = []
        self.new_configuration: Configuration | None = None
        self.options: dict | None = None
        self.error = ""
        self._written: tuple | None = None  # (options, new configuration) as the file has them

    def exists(self) -> bool:
        return self.path.is_file()

    def _load(self) -> tuple[list[Configuration], dict | None, dict | None]:
        """Everything in the file, checked (ConfigError if any of it can't be read)."""
        if not self.exists():
            return [], None, None
        configurations = load(self.path)
        data = _read(self.path)
        return configurations, _object(data, "options", self.path), _object(data, "new_configuration", self.path)

    def read(self) -> ConfigFile:
        """Read it all (the file's options and New configuration replace those had)."""
        try:
            configurations, options, new = self._load()
        except ConfigError as exc:
            self.configurations, self.options, self.new_configuration = [], None, None
            self.error, self._written = str(exc), None
            return self
        self.configurations, self.error = configurations, ""
        self._take_gui(options, new)
        return self

    def _take_gui(self, options: dict | None, new: dict | None):
        self._written = (options, new) if self.exists() else None
        self.options = dict(options or {}) if self.exists() else None
        new = dict(new or {})
        for key, name in _OLD_OPTION_FIELDS.items():  # (from a file written before: moved)
            if self.options is not None and key in self.options:
                new.setdefault(name, self.options.pop(key))
        self.new_configuration = Configuration.from_json(new, name="") if new else None

    def reread(self) -> bool:
        """Read it again (it was changed, e.g. in the editor): its
        configurations, and its options and New configuration if they're
        not those last read or written (True: they changed)."""
        try:
            configurations, options, new = self._load()
        except ConfigError as exc:
            self.configurations, self.error = [], str(exc)
            return False
        self.configurations, self.error = configurations, ""
        if self.exists() and (options, new) != self._written:
            self._take_gui(options, new)
            return True
        return False

    def start_options(self):
        """It was just made (the first configuration saved): it holds the
        project's options from now on (none yet)."""
        if self.options is None and self.exists() and not self.error:
            self.options, self._written = {}, ({}, None)

    def _check_writable(self):
        if self.error and self.exists():  # (not written over: fix it, or move it away)
            raise ConfigError(self.error)

    def save(self, config: Configuration):
        """Add a configuration, or replace the one with its name (OSError, ConfigError)."""
        self._check_writable()
        self.configurations = save(self.path, config)

    def remove(self, name: str):
        """Take a configuration out (OSError, ConfigError)."""
        self._check_writable()
        self.configurations = remove(self.path, name)

    def write_options(self, options: dict, new: Configuration) -> bool:
        """Write the options and New configuration, if they changed (True:
        written; OSError, ConfigError)."""
        if self.options is None or self.error:
            return False
        written = (options, new.to_json(named=False))
        if written == self._written:
            return False
        save_options(self.path, *written)
        self.options, self.new_configuration, self._written = dict(options), new, written
        return True


def save_options(path: Path, options: dict, new_configuration: dict):
    """Put ``options`` and New configuration's Build options in the file
    (which must exist and be readable), its configurations and anything else
    in it kept, in the order it's read: the configurations, New
    configuration, the options."""
    load(path)  # (every configuration checked first: a file that can't be read isn't written over)
    data = {k: v for k, v in _read(path).items() if k not in ("new_configuration", "options")}
    data["new_configuration"] = new_configuration
    data["options"] = options
    _write(path, data)
