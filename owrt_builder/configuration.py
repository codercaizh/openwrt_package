"""Configuration fragments, package option validation and native defconfig.

Static checks make the UI useful, but they never replace Kconfig.  The
``validate_with_defconfig`` entry point runs the source tree's own ``make
defconfig`` and compares the resulting values so dependencies, impossible
removals and choices are visible to the caller.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import os
from pathlib import Path
import re
import shlex
import subprocess
import tempfile
from typing import Any, Callable, Iterable, Iterator, Mapping, MutableMapping, Sequence

from .catalog import Catalog, KconfigOption


class ConfigurationError(ValueError):
    """A configuration fragment cannot be composed or parsed."""


@dataclass(frozen=True)
class ConfigEntry:
    symbol: str
    value: str
    line_no: int = 0
    raw: str = ""
    explicit: bool = True

    @property
    def is_package(self) -> bool:
        return self.symbol.startswith("CONFIG_PACKAGE_")

    def to_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "value": self.value,
            "line": self.line_no,
            "explicit": self.explicit,
        }


class ConfigDocument(Mapping[str, str]):
    """Parsed config preserving the last value for each symbol."""

    def __init__(self, entries: Sequence[ConfigEntry], source: str = "") -> None:
        self.entries = tuple(entries)
        self.source = source
        self._values = {entry.symbol: entry.value for entry in self.entries}

    def __getitem__(self, key: str) -> str:
        return self._values[key]

    def __iter__(self) -> Iterator[str]:
        return iter(self._values)

    def __len__(self) -> int:
        return len(self._values)

    def get_entry(self, symbol: str) -> ConfigEntry | None:
        wanted = _symbol(symbol)
        for entry in reversed(self.entries):
            if entry.symbol == wanted:
                return entry
        return None

    def package_entries(self) -> tuple[ConfigEntry, ...]:
        return tuple(entry for entry in self.entries if entry.is_package)

    def to_dict(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "values": dict(self._values),
            "entries": [entry.to_dict() for entry in self.entries],
        }


@dataclass(frozen=True)
class ConfigIssue:
    code: str
    message: str
    severity: str = "error"
    symbol: str | None = None
    package: str | None = None
    value: str | None = None
    details: Mapping[str, Any] = field(default_factory=dict)

    @property
    def error(self) -> bool:
        return self.severity == "error"

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "message": self.message,
            "severity": self.severity,
            "symbol": self.symbol,
            "package": self.package,
            "value": self.value,
            "details": dict(self.details),
        }


@dataclass
class ConfigValidation:
    valid: bool
    authoritative: bool
    requested: ConfigDocument
    resolved: ConfigDocument | None = None
    issues: list[ConfigIssue] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "valid": self.valid,
            "authoritative": self.authoritative,
            "requested": self.requested.to_dict(),
            "resolved": self.resolved.to_dict() if self.resolved else None,
            "issues": [issue.to_dict() for issue in self.issues],
        }


def _symbol(value: str) -> str:
    value = str(value).strip()
    return value if value.startswith("CONFIG_") else f"CONFIG_{value}"


def _read_text(value: str | os.PathLike[str] | ConfigDocument) -> tuple[str, str]:
    if isinstance(value, ConfigDocument):
        return "\n".join(f"{entry.symbol}={entry.value}" for entry in value.entries), value.source
    if isinstance(value, os.PathLike):
        path = Path(value)
        return path.read_text(encoding="utf-8"), str(path)
    text = str(value)
    maybe_path = Path(text)
    if "\n" not in text and "\r" not in text and maybe_path.is_file():
        return maybe_path.read_text(encoding="utf-8"), str(maybe_path)
    return text, "<fragment>"


def parse_config(value: str | os.PathLike[str] | ConfigDocument) -> ConfigDocument:
    """Parse native ``.config`` syntax, including ``is not set`` lines."""

    if isinstance(value, ConfigDocument):
        return value
    text, source = _read_text(value)
    entries: list[ConfigEntry] = []
    for line_no, raw in enumerate(text.splitlines(), 1):
        # OpenWrt package symbols intentionally preserve package names, and
        # those names commonly contain hyphens (for example
        # ``CONFIG_PACKAGE_luci-app-passwall``).  Kconfig also permits dots
        # in symbols.  Restricting this expression to ``[A-Za-z0-9_]``
        # silently dropped the very package and sub-option entries the Web
        # validator is meant to inspect.
        unset = re.match(r"^\s*#\s*(CONFIG_[A-Za-z0-9_.-]+)\s+is\s+not\s+set\s*$", raw)
        if unset:
            entries.append(ConfigEntry(unset.group(1), "n", line_no, raw, True))
            continue
        match = re.match(r"^\s*(CONFIG_[A-Za-z0-9_.-]+)=(.*)$", raw)
        if not match:
            continue
        entries.append(ConfigEntry(match.group(1), match.group(2).strip(), line_no, raw, True))
    return ConfigDocument(entries, source)


def compose_fragment(
    fragment: str | os.PathLike[str],
    *,
    defconfig_dir: str | os.PathLike[str] | None = None,
) -> str:
    """Compose an optional ``#CONFIG_APPEND=...`` fragment explicitly.

    The old shell workflow appends a board-specific file before the selected
    config.  A missing requested append is an error so a typo cannot silently
    produce a generic image.
    """

    append_text, fragment_text = compose_fragment_parts(
        fragment,
        defconfig_dir=defconfig_dir,
    )
    if not append_text:
        return fragment_text
    return append_text + "\n" + fragment_text


def compose_fragment_parts(
    fragment: str | os.PathLike[str],
    *,
    defconfig_dir: str | os.PathLike[str] | None = None,
) -> tuple[str, str]:
    """Return ``(CONFIG_APPEND text, device fragment text)``.

    ``CONFIG_APPEND`` is a board baseline, rather than part of the device's
    selectable plugin fragment.  Callers that need to edit package symbols
    must keep these two pieces separate so an explicit Web package list cannot
    remove hardware-required packages from the append file.
    """

    path = Path(fragment)
    text = path.read_text(encoding="utf-8")
    match = re.search(r"^\s*#CONFIG_APPEND=([^\s#]+)\s*$", text, flags=re.MULTILINE)
    if not match:
        return "", text
    if defconfig_dir is None:
        raise ConfigurationError(f"{path} requests CONFIG_APPEND but no defconfig directory was supplied")
    append_name = match.group(1)
    append_path = Path(defconfig_dir) / append_name
    if not append_path.is_file():
        raise ConfigurationError(f"requested CONFIG_APPEND file not found: {append_path}")
    return append_path.read_text(encoding="utf-8"), text


def _package_for_symbol(catalog: Catalog, symbol: str) -> tuple[str | None, bool]:
    """Return the explicit catalog owner and whether ``symbol`` is a base.

    Package-local Kconfig symbols are not required to share the package
    symbol's prefix (for example ``CONFIG_NODEJS_20`` may belong to the
    ``node`` package).  Ownership therefore comes from generated metadata,
    never from string-prefix guesses.
    """

    package, is_base, _option = catalog.symbol_info(symbol)
    return package, is_base


def _owned_entries(
    document: ConfigDocument,
    catalog: Catalog,
) -> Iterator[tuple[ConfigEntry, str, bool, KconfigOption | None]]:
    """Yield only symbols explicitly owned by a catalog package."""

    for entry in document.entries:
        package, is_base, option = catalog.symbol_info(entry.symbol)
        if package is None:
            continue
        yield entry, package, is_base, option


def _option_value(value: str) -> str:
    return value.strip()


def _validate_value(option: KconfigOption, value: str) -> str | None:
    value = _option_value(value)
    if option.kind == "bool":
        return None if value in {"y", "n"} else "bool expects y or n"
    if option.kind == "tristate":
        return None if value in {"y", "m", "n"} else "tristate expects y, m or n"
    if option.kind == "string":
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
            return None
        return "string expects a quoted value"
    if option.kind == "int":
        return None if re.fullmatch(r"[-+]?\d+", value) else "int expects a decimal integer"
    if option.kind == "hex":
        return None if re.fullmatch(r"(?:0[xX])?[0-9a-fA-F]+", value) else "hex expects hexadecimal digits"
    if option.kind in {"unknown", ""}:
        return "option type is unavailable"
    # Kconfig can grow additional scalar types.  Preserve them rather than
    # incorrectly rejecting a value the native parser understands.
    return None


def _requested_options(
    document: ConfigDocument,
    catalog: Catalog,
) -> tuple[list[tuple[ConfigEntry, str | None, bool, KconfigOption | None]], list[ConfigIssue]]:
    results: list[tuple[ConfigEntry, str | None, bool, KconfigOption | None]] = []
    issues: list[ConfigIssue] = []
    for entry in document.entries:
        package, is_base, option = catalog.symbol_info(entry.symbol)
        if package is None:
            # Target/base configuration symbols are outside the package
            # catalogue and are validated by native Kconfig instead.
            continue
        if not is_base and option is None:
            issues.append(
                ConfigIssue(
                    "unknown_option",
                    f"{entry.symbol} is not a config option of {package}",
                    symbol=entry.symbol,
                    package=package,
                    value=entry.value,
                )
            )
            continue
        results.append((entry, package, is_base, option))
    return results, issues


def _compare_resolved(
    requested: ConfigDocument,
    resolved: ConfigDocument,
    catalog: Catalog,
) -> list[ConfigIssue]:
    issues: list[ConfigIssue] = []
    requested_values = {
        entry.symbol: entry.value
        for entry, _package, _is_base, _option in _owned_entries(requested, catalog)
    }
    resolved_values = {
        entry.symbol: entry.value
        for entry, _package, _is_base, _option in _owned_entries(resolved, catalog)
    }
    for symbol, requested_value in requested_values.items():
        if symbol not in resolved_values:
            continue
        actual = resolved_values[symbol]
        package, is_base, option = catalog.symbol_info(symbol)
        if requested_value == "n" and actual != "n":
            issues.append(
                ConfigIssue(
                    "cannot_remove",
                    f"native defconfig kept {symbol}={actual} after an explicit n",
                    symbol=symbol,
                    package=package,
                    value=actual,
                )
            )
        elif requested_value in {"y", "m"} and actual != requested_value:
            code = "choice_ignored" if package and not is_base and option and option.choice else "config_changed"
            issues.append(
                ConfigIssue(
                    code,
                    f"native defconfig resolved {symbol}={actual}, requested {requested_value}",
                    symbol=symbol,
                    package=package,
                    value=actual,
                )
            )

    for symbol, actual in resolved_values.items():
        if actual not in {"y", "m"} or symbol in requested_values:
            continue
        package, is_base, _option = catalog.symbol_info(symbol)
        if package is None:
            continue
        issues.append(
            ConfigIssue(
                "auto_dependency",
                f"native defconfig added {symbol}={actual}",
                severity="warning",
                symbol=symbol,
                package=package,
                value=actual,
            )
        )
    # A choice can leave the requested option at n while another option in its
    # group is selected; report that as choice_ignored with the sibling value.
    for package in catalog.packages:
        by_choice: dict[str, list[KconfigOption]] = {}
        for option in package.options:
            if option.choice:
                by_choice.setdefault(option.choice, []).append(option)
        for choice, options in by_choice.items():
            requested_yes = [
                option.symbol for option in options if requested_values.get(option.symbol) in {"y", "m"}
            ]
            if not requested_yes:
                continue
            selected = [option.symbol for option in options if resolved_values.get(option.symbol) in {"y", "m"}]
            if selected and not any(item in selected for item in requested_yes):
                issues.append(
                    ConfigIssue(
                        "choice_ignored",
                        f"choice {choice} selected {', '.join(selected)} instead of requested {', '.join(requested_yes)}",
                        symbol=requested_yes[0],
                        package=package.name,
                        details={"choice": choice, "selected": selected, "requested": requested_yes},
                    )
                )
    return issues


def validate_fragment(
    catalog: Catalog,
    fragment: str | os.PathLike[str] | ConfigDocument,
    *,
    resolved: str | os.PathLike[str] | ConfigDocument | None = None,
    authoritative: bool | None = None,
) -> ConfigValidation:
    """Validate package selections/options against one catalog snapshot."""

    requested = parse_config(fragment)
    checked, issues = _requested_options(requested, catalog)
    for entry, package, is_base, option in checked:
        if option is None:
            # Base package symbols are always tristate in generated OpenWrt
            # package config; reject malformed values before native defconfig.
            if entry.value not in {"y", "m", "n"}:
                issues.append(
                    ConfigIssue(
                        "invalid_value",
                        f"package selection {entry.symbol} expects y, m or n",
                        symbol=entry.symbol,
                        package=package,
                        value=entry.value,
                    )
                )
            continue
        problem = _validate_value(option, entry.value)
        if problem:
            issues.append(
                ConfigIssue(
                    "invalid_type",
                    f"{entry.symbol}: {problem}",
                    symbol=entry.symbol,
                    package=package,
                    value=entry.value,
                    details={"kind": option.kind},
                )
            )

    resolved_doc = parse_config(resolved) if resolved is not None else None
    if resolved_doc is not None:
        issues.extend(_compare_resolved(requested, resolved_doc, catalog))
    is_authoritative = bool(authoritative) if authoritative is not None else resolved_doc is not None
    valid = not any(issue.error for issue in issues)
    return ConfigValidation(valid, is_authoritative, requested, resolved_doc, issues)


def _default_runner(
    args: Sequence[str], *, cwd: Path, env: Mapping[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(list(args), cwd=str(cwd), env=dict(env) if env else None, text=True, capture_output=True, check=True)


def validate_with_defconfig(
    source_root: str | os.PathLike[str],
    fragment: str | os.PathLike[str] | ConfigDocument,
    *,
    catalog: Catalog,
    build_dir: str | os.PathLike[str] | None = None,
    runner: Callable[..., subprocess.CompletedProcess[str]] | None = None,
    make_command: Sequence[str] = ("make",),
    env: Mapping[str, str] | None = None,
) -> ConfigValidation:
    """Run native ``make defconfig`` and compare its final .config.

    ``build_dir`` should be a per-task output directory.  If omitted a
    temporary output directory is used and removed after validation.  The
    source snapshot itself is never used as a mutable task directory unless a
    caller explicitly passes it as ``build_dir``.
    """

    root = Path(source_root).expanduser().resolve()
    if not root.is_dir():
        raise ConfigurationError(f"source root not found: {root}")
    requested = parse_config(fragment)
    owned_tmp: tempfile.TemporaryDirectory[str] | None = None
    if build_dir is None:
        owned_tmp = tempfile.TemporaryDirectory(prefix="owrt-defconfig-")
        work = Path(owned_tmp.name)
    else:
        work = Path(build_dir).expanduser().resolve()
        work.mkdir(parents=True, exist_ok=True)
    try:
        (work / ".config").write_text(
            "\n".join(entry.raw or f"{entry.symbol}={entry.value}" for entry in requested.entries) + "\n",
            encoding="utf-8",
        )
        command = [*make_command, "-C", str(root), f"O={work}", "defconfig"]
        run = runner or _default_runner
        try:
            result = run(command, cwd=work, env=env)
            if getattr(result, "returncode", 0) not in {0, None}:
                raise subprocess.CalledProcessError(result.returncode, command)
        except (OSError, subprocess.CalledProcessError) as exc:
            issue = ConfigIssue("defconfig_failed", f"native make defconfig failed: {exc}")
            return ConfigValidation(False, False, requested, None, [issue])
        final_path = work / ".config"
        if not final_path.is_file():
            issue = ConfigIssue("defconfig_missing", "native make defconfig produced no .config")
            return ConfigValidation(False, False, requested, None, [issue])
        resolved = parse_config(final_path)
        result = validate_fragment(catalog, requested, resolved=resolved, authoritative=True)
        return result
    finally:
        if owned_tmp is not None:
            owned_tmp.cleanup()


__all__ = [
    "ConfigDocument",
    "ConfigEntry",
    "ConfigIssue",
    "ConfigValidation",
    "ConfigurationError",
    "compose_fragment",
    "compose_fragment_parts",
    "parse_config",
    "validate_fragment",
    "validate_with_defconfig",
]
