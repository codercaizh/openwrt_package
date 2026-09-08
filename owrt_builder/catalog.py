"""OpenWrt package metadata and package-local Kconfig catalogues.

OpenWrt generates ``tmp/.packageinfo`` and ``tmp/.config-package.in`` after
feeds are installed.  Those generated files are the primary source here:
macros such as ``luci.mk`` can otherwise hide packages and their options from a
simple Makefile parser.  A narrow Makefile fallback is retained for an
unprepared source and is marked non-authoritative in the result.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from bisect import bisect_right
import json
from pathlib import Path
import re
from typing import Any, Iterable, Iterator, Mapping, Sequence

from .devices import DeviceSpec


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _symbol(value: str) -> str:
    value = value.strip()
    return value if value.startswith("CONFIG_") else f"CONFIG_{value}"


def _bare_symbol(value: str) -> str:
    value = _symbol(value)
    return value[len("CONFIG_") :]


def _unquote(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
        return value[1:-1]
    return value


def _prompt(value: str) -> tuple[str | None, str | None]:
    """Return prompt and a trailing conditional expression."""

    text = value.strip()
    if not text:
        return None, None
    if text[0] in {'"', "'"}:
        quote = text[0]
        escaped = False
        end = None
        for index in range(1, len(text)):
            char = text[index]
            if char == quote and not escaped:
                end = index
                break
            escaped = char == "\\" and not escaped
            if char != "\\":
                escaped = False
        if end is None:
            return _unquote(text), None
        prompt = _unquote(text[: end + 1])
        rest = text[end + 1 :].strip()
    else:
        match = re.match(r"([^\s]+)(?:\s+(.*))?$", text)
        if not match:
            return text, None
        prompt, rest = match.group(1), (match.group(2) or "").strip()
    if rest.startswith("if "):
        return prompt, rest[3:].strip()
    return prompt, rest or None


@dataclass(frozen=True)
class KconfigDefault:
    value: str
    condition: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {"value": self.value, "condition": self.condition}


@dataclass
class KconfigOption:
    symbol: str
    kind: str = "unknown"
    prompt: str | None = None
    prompt_condition: str | None = None
    package: str | None = None
    depends_on: list[str] = field(default_factory=list)
    selects: list[str] = field(default_factory=list)
    implies: list[str] = field(default_factory=list)
    defaults: list[KconfigDefault] = field(default_factory=list)
    ranges: list[str] = field(default_factory=list)
    help_text: str = ""
    choice: str | None = None
    visible_if: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "kind": self.kind,
            "prompt": self.prompt,
            "prompt_condition": self.prompt_condition,
            "package": self.package,
            "depends_on": list(self.depends_on),
            "selects": list(self.selects),
            "implies": list(self.implies),
            "defaults": [item.to_dict() for item in self.defaults],
            "ranges": list(self.ranges),
            "help": self.help_text,
            "choice": self.choice,
            "visible_if": list(self.visible_if),
        }


@dataclass
class PackageMetadata:
    name: str
    path: str = ""
    feed: str = "core"
    title: str = ""
    section: str = ""
    category: str = ""
    submenu: str = ""
    depends: tuple[str, ...] = ()
    repository: str = ""
    architecture: str = ""
    description: str = ""
    menu: str = ""
    metadata_source: str = "generated"
    options: list[KconfigOption] = field(default_factory=list)

    @property
    def symbol(self) -> str:
        return _symbol(f"PACKAGE_{self.name}")

    @property
    def is_plugin(self) -> bool:
        return self.name.startswith(("luci-app-", "luci-theme-"))

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "symbol": self.symbol,
            "path": self.path,
            "feed": self.feed,
            "title": self.title,
            "section": self.section,
            "category": self.category,
            "submenu": self.submenu,
            "depends": list(self.depends),
            "repository": self.repository,
            "architecture": self.architecture,
            "description": self.description,
            "menu": self.menu,
            "metadata_source": self.metadata_source,
            "is_plugin": self.is_plugin,
            "options": [option.to_dict() for option in self.options],
        }


@dataclass
class Catalog:
    root: Path
    packages: list[PackageMetadata]
    authoritative: bool
    generated_files: tuple[str, ...] = ()
    metadata_source: str = "generated"
    generated_at: str = field(default_factory=_now)
    _symbol_index: dict[str, tuple[str, bool, KconfigOption | None]] = field(
        default_factory=dict,
        init=False,
        repr=False,
        compare=False,
    )

    def __post_init__(self) -> None:
        """Build the immutable lookup used by config validation.

        A generated OpenWrt catalog can contain twelve thousand packages and
        several hundred package-local symbols.  Config validation runs once
        per line of a fragment and again for the native resolved config, so a
        repeated package/option scan becomes needlessly expensive.  Keep the
        same first-owner-wins behavior as the old linear scan while indexing
        each symbol once when the catalog is loaded.
        """

        index: dict[str, tuple[str, bool, KconfigOption | None]] = {}
        for package in self.packages:
            index.setdefault(package.symbol, (package.name, True, None))
            for option in package.options:
                index.setdefault(option.symbol, (package.name, False, option))
        self._symbol_index = index

    def package(self, name: str) -> PackageMetadata | None:
        return next((item for item in self.packages if item.name == name), None)

    def symbol_info(self, symbol: str) -> tuple[str | None, bool, KconfigOption | None]:
        """Return ``(package, is_base, option)`` for a config symbol."""

        return self._symbol_index.get(_symbol(symbol), (None, False, None))

    def plugins(self) -> list[PackageMetadata]:
        return [item for item in self.packages if item.is_plugin]

    @property
    def package_names(self) -> tuple[str, ...]:
        return tuple(item.name for item in self.packages)

    def option(self, package: str, option: str) -> KconfigOption | None:
        wanted = _symbol(option)
        owner, is_base, candidate = self.symbol_info(wanted)
        if owner == package and not is_base:
            return candidate
        # Keep the historical package-scoped behavior for a malformed or
        # forked catalog that exposes the same child symbol in two packages;
        # the owner index remains the fast path for normal generated metadata.
        item = self.package(package)
        if item is None:
            return None
        return next((candidate for candidate in item.options if candidate.symbol == wanted), None)

    def to_dict(self) -> dict[str, Any]:
        return {
            "root": str(self.root),
            "authoritative": self.authoritative,
            "metadata_source": self.metadata_source,
            "generated_files": list(self.generated_files),
            "generated_at": self.generated_at,
            "packages": [item.to_dict() for item in self.packages],
        }

    def write(self, path: str | Path) -> Path:
        destination = Path(path)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(
            json.dumps(self.to_dict(), ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        return destination

    @classmethod
    def from_dict(cls, value: Mapping[str, Any], *, root: str | Path | None = None) -> "Catalog":
        """Restore the immutable catalog stored beside a source snapshot."""

        packages: list[PackageMetadata] = []
        for raw in value.get("packages", []) or []:
            if not isinstance(raw, Mapping) or not raw.get("name"):
                continue
            options: list[KconfigOption] = []
            for option_raw in raw.get("options", []) or []:
                if not isinstance(option_raw, Mapping) or not option_raw.get("symbol"):
                    continue
                defaults = [
                    KconfigDefault(str(item.get("value", "")), item.get("condition"))
                    if isinstance(item, Mapping)
                    else KconfigDefault(str(item))
                    for item in option_raw.get("defaults", []) or []
                ]
                options.append(
                    KconfigOption(
                        symbol=str(option_raw["symbol"]),
                        kind=str(option_raw.get("kind", "unknown")),
                        prompt=option_raw.get("prompt"),
                        prompt_condition=option_raw.get("prompt_condition"),
                        package=option_raw.get("package"),
                        depends_on=[str(item) for item in option_raw.get("depends_on", []) or []],
                        selects=[str(item) for item in option_raw.get("selects", []) or []],
                        implies=[str(item) for item in option_raw.get("implies", []) or []],
                        defaults=defaults,
                        ranges=[str(item) for item in option_raw.get("ranges", []) or []],
                        help_text=str(option_raw.get("help", "")),
                        choice=option_raw.get("choice"),
                        visible_if=[str(item) for item in option_raw.get("visible_if", []) or []],
                    )
                )
            packages.append(
                PackageMetadata(
                    name=str(raw["name"]),
                    path=str(raw.get("path", "")),
                    feed=str(raw.get("feed", "core")),
                    title=str(raw.get("title", "")),
                    section=str(raw.get("section", "")),
                    category=str(raw.get("category", "")),
                    submenu=str(raw.get("submenu", "")),
                    depends=tuple(str(item) for item in raw.get("depends", []) or []),
                    repository=str(raw.get("repository", "")),
                    architecture=str(raw.get("architecture", "")),
                    description=str(raw.get("description", "")),
                    menu=str(raw.get("menu", "")),
                    metadata_source=str(raw.get("metadata_source", value.get("metadata_source", "generated"))),
                    options=options,
                )
            )
        return cls(
            root=Path(root or value.get("root", ".")).expanduser().resolve(),
            packages=packages,
            authoritative=bool(value.get("authoritative", False)),
            generated_files=tuple(str(item) for item in value.get("generated_files", []) or []),
            metadata_source=str(value.get("metadata_source", "generated")),
            generated_at=str(value.get("generated_at", _now())),
        )

    @classmethod
    def read(cls, path: str | Path) -> "Catalog":
        catalog_path = Path(path).expanduser().resolve()
        value = json.loads(catalog_path.read_text(encoding="utf-8"))
        if not isinstance(value, Mapping):
            raise ValueError(f"invalid catalog JSON: {catalog_path}")
        # The catalogue is serialized while its source is still in a staging
        # directory and the whole snapshot is then renamed atomically.  The
        # staging path stored in the JSON is historical metadata; resolve the
        # live sibling source directory when it exists.
        snapshot_source = catalog_path.parent / "source"
        root = snapshot_source if snapshot_source.is_dir() else None
        return cls.from_dict(value, root=root)


@dataclass
class _KconfigContext:
    kind: str
    indent: int
    value: Any = None


def _parse_kconfig(text: str) -> list[KconfigOption]:
    """Parse the Kconfig subset needed for package option metadata.

    This is intentionally a parser for generated package Kconfig, not a
    general Kconfig evaluator.  Expressions are retained verbatim for the
    native defconfig step to evaluate later.
    """

    options: list[KconfigOption] = []
    stack: list[_KconfigContext] = []
    help_option: KconfigOption | None = None
    help_indent = -1
    choice_counter = 0
    choice_name: str | None = None

    def current_option() -> KconfigOption | None:
        for context in reversed(stack):
            if context.kind == "option":
                return context.value
        return None

    def current_choice() -> str | None:
        for context in reversed(stack):
            if context.kind == "choice":
                return context.value
        return None

    def pop_for_indent(indent: int) -> None:
        while stack and stack[-1].indent >= indent:
            stack.pop()

    def pop_for_option(indent: int) -> None:
        # A choice remains active while its config entries are emitted at the
        # same indentation level.  Only the preceding option/menu context is
        # closed when the next config symbol starts.
        while stack and stack[-1].kind in {"option", "menu", "if"} and stack[-1].indent >= indent:
            stack.pop()

    lines = text.splitlines()
    for line_no, raw in enumerate(lines, 1):
        if not raw.strip():
            if help_option is not None and help_option.help_text:
                help_option.help_text += "\n"
            continue
        indent = len(raw) - len(raw.lstrip(" \t"))
        stripped = raw.strip()

        if help_option is not None and indent > help_indent:
            line = stripped
            help_option.help_text = (help_option.help_text + "\n" + line).strip()
            continue
        help_option = None
        help_indent = -1

        if stripped.startswith("#"):
            continue
        if stripped == "endchoice":
            while stack:
                context = stack.pop()
                if context.kind == "choice":
                    break
            continue
        if stripped in {"endmenu", "endif"}:
            while stack and stack[-1].kind not in {"menu", "if"}:
                stack.pop()
            if stack:
                stack.pop()
            continue

        # OpenWrt package symbols retain package hyphens (for example
        # PACKAGE_luci-app-passwall), although upstream Kconfig symbols more
        # commonly use underscores.
        match = re.match(r"(?:menuconfig|config)\s+([A-Za-z0-9_.-]+)$", stripped)
        if match:
            pop_for_option(indent)
            candidate = _symbol(match.group(1))
            option = KconfigOption(symbol=candidate, choice=current_choice())
            # Kept as parser-local provenance so generated package contexts
            # can associate non-PACKAGE_* symbols (for example NODEJS_20 or
            # ZABBIX_SQLITE) with the package block that emitted them.  It is
            # intentionally not serialized in the public option DTO.
            setattr(option, "_line_no", line_no)
            options.append(option)
            stack.append(_KconfigContext("option", indent, option))
            continue

        match = re.match(r"choice(?:\s+([A-Za-z0-9_.-]+))?$", stripped)
        if match:
            pop_for_indent(indent)
            choice_counter += 1
            choice_name = match.group(1) or f"choice-{choice_counter}"
            stack.append(_KconfigContext("choice", indent, choice_name))
            continue
        if stripped.startswith("menu "):
            pop_for_indent(indent)
            stack.append(_KconfigContext("menu", indent, stripped[5:].strip()))
            continue
        if stripped.startswith("if "):
            pop_for_indent(indent)
            stack.append(_KconfigContext("if", indent, stripped[3:].strip()))
            continue
        if stripped.startswith("source "):
            continue

        option = current_option()
        if option is None:
            continue
        match = re.match(r"(bool|tristate|string|int|hex)(?:\s+(.*))?$", stripped)
        if match:
            option.kind = match.group(1)
            if match.group(2):
                option.prompt, option.prompt_condition = _prompt(match.group(2))
            continue
        match = re.match(r"prompt\s+(.+)$", stripped)
        if match:
            option.prompt, option.prompt_condition = _prompt(match.group(1))
            continue
        match = re.match(r"default\s+(.+)$", stripped)
        if match:
            value, condition = _prompt(match.group(1))
            option.defaults.append(KconfigDefault(value or "", condition))
            continue
        match = re.match(r"def_(bool|tristate)\s+(.+)$", stripped)
        if match:
            option.kind = match.group(1)
            value, condition = _prompt(match.group(2))
            option.defaults.append(KconfigDefault(value or "", condition))
            continue
        match = re.match(r"depends\s+on\s+(.+)$", stripped)
        if match:
            option.depends_on.append(match.group(1).strip())
            continue
        match = re.match(r"select\s+(.+)$", stripped)
        if match:
            option.selects.append(match.group(1).strip())
            continue
        match = re.match(r"imply\s+(.+)$", stripped)
        if match:
            option.implies.append(match.group(1).strip())
            continue
        match = re.match(r"range\s+(.+)$", stripped)
        if match:
            option.ranges.append(match.group(1).strip())
            continue
        match = re.match(r"visible\s+if\s+(.+)$", stripped)
        if match:
            option.visible_if.append(match.group(1).strip())
            continue
        if stripped in {"help", "---help---"} or stripped.startswith("help "):
            help_option = option
            help_indent = indent
            continue
        if stripped.startswith("option "):
            # Keep Kconfig's extra flags in the help-free metadata only when
            # they carry useful semantics; type is already handled above.
            continue

    return options


def _parse_packageinfo(path: Path) -> list[PackageMetadata]:
    text = path.read_text(encoding="utf-8", errors="replace")
    rows: list[PackageMetadata] = []
    # metadata.pl emits @@ between records.  Accept a final record without it.
    chunks = re.split(r"^@@\s*$", text, flags=re.MULTILINE)
    for chunk in chunks:
        fields: dict[str, str] = {}
        current: str | None = None
        for raw in chunk.splitlines():
            match = re.match(r"^([A-Za-z][A-Za-z0-9_-]*):\s?(.*)$", raw)
            if match:
                current = match.group(1).lower()
                fields[current] = match.group(2).rstrip()
            elif current == "description" and raw.strip():
                fields[current] += "\n" + raw.rstrip()
        name = fields.get("package", "").strip()
        if not name:
            continue
        source = fields.get("source", "").strip()
        path_text = source.split("/Makefile", 1)[0].strip()
        # For packages generated through ``luci.mk`` the package archive
        # ``Source`` is intentionally empty.  OpenWrt still records the
        # originating Makefile in ``Source-Makefile``; using it preserves the
        # local feed/repository in the Web catalogue and also covers package
        # overrides installed through ``package/feeds``.
        if not path_text:
            source_makefile = fields.get("source-makefile", "").strip()
            path_text = source_makefile.split("/Makefile", 1)[0].strip()
        feed = _feed_for_path(path_text)
        if feed == "core" and fields.get("repository", "").strip():
            # Generated package metadata often uses an archive name in
            # ``Source`` instead of a repository path.  ``Repository`` is the
            # authoritative feed label in that form.
            feed = fields["repository"].strip()
        depends = tuple(item for item in fields.get("depends", "").split() if item)
        rows.append(
            PackageMetadata(
                name=name,
                path=path_text,
                feed=feed,
                title=fields.get("title", "").strip(),
                section=fields.get("section", "").strip(),
                category=fields.get("category", "").strip(),
                submenu=fields.get("submenu", "").strip(),
                depends=depends,
                repository=fields.get("repository", "").strip(),
                architecture=fields.get("architecture", "").strip(),
                description=fields.get("description", "").strip(),
                menu=fields.get("menu", "").strip(),
                metadata_source="generated",
            )
        )
    return rows


def _feed_for_path(path: str) -> str:
    clean = path.strip().lstrip("./")
    parts = clean.split("/")
    # ``scripts/feeds install`` exposes a package as
    # ``package/feeds/<feed>/<name>``.  The second component is the literal
    # directory ``feeds``; the actual feed name is the third component.
    if len(parts) >= 3 and parts[0] == "package" and parts[1] == "feeds":
        return parts[2]
    if len(parts) >= 2 and parts[0] == "feeds":
        return parts[1]
    if len(parts) >= 2 and parts[0] == "package":
        return parts[1]
    return "core"


def _iter_makefiles(root: Path) -> Iterator[Path]:
    roots = [root / "package", root / "feeds"]
    visited: set[tuple[int, int]] = set()
    ignored = {".git", "build_dir", "staging_dir", "tmp", "bin", "dl", "target", "toolchain"}
    for scan_root in roots:
        if not scan_root.exists():
            continue
        for directory, dirs, files in __import__("os").walk(scan_root, followlinks=True):
            directory_path = Path(directory)
            try:
                stat = directory_path.stat()
                inode = (stat.st_dev, stat.st_ino)
                if inode in visited:
                    dirs[:] = []
                    continue
                visited.add(inode)
            except OSError:
                dirs[:] = []
                continue
            dirs[:] = [item for item in dirs if item not in ignored]
            if "Makefile" in files:
                yield directory_path / "Makefile"


def _parse_raw_makefiles(root: Path) -> dict[str, PackageMetadata]:
    result: dict[str, PackageMetadata] = {}
    for makefile in _iter_makefiles(root):
        try:
            text = makefile.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        rel_dir = makefile.parent.relative_to(root).as_posix()
        blocks = list(
            re.finditer(
                r"^\s*define\s+Package/([^\s/]+)(?:/([^\s]+))?\s*$",
                text,
                flags=re.MULTILINE,
            )
        )
        for index, match in enumerate(blocks):
            name, suffix = match.group(1), match.group(2)
            end = re.search(r"^\s*endef\s*$", text[match.end() :], flags=re.MULTILINE)
            if end is None:
                continue
            body = text[match.end() : match.end() + end.start()]
            if suffix == "config":
                continue
            fields: dict[str, str] = {}
            pending = ""
            for raw in body.splitlines():
                line = raw.strip()
                if not line:
                    continue
                if pending:
                    line = pending + line
                    pending = ""
                if line.endswith("\\"):
                    pending = line[:-1].rstrip() + " "
                    continue
                field_match = re.match(r"([A-Za-z][A-Za-z0-9_-]*)\s*:?=\s*(.*)$", line)
                if field_match:
                    fields[field_match.group(1).upper()] = field_match.group(2).strip()
            source_path = rel_dir
            result.setdefault(
                name,
                PackageMetadata(
                    name=name,
                    path=source_path,
                    feed=_feed_for_path(source_path),
                    title=_unquote(fields.get("TITLE", "")),
                    section=_unquote(fields.get("SECTION", "")),
                    category=_unquote(fields.get("CATEGORY", "")),
                    submenu=_unquote(fields.get("SUBMENU", "")),
                    depends=tuple(fields.get("DEPENDS", "").split()),
                    repository=_unquote(fields.get("REPOSITORY", "")),
                    description=_unquote(fields.get("DESCRIPTION", "")),
                    menu=_unquote(fields.get("MENU", "")),
                    metadata_source="makefile",
                ),
            )
    # Raw package config blocks are associated by package name.  A generated
    # catalogue supersedes these, but they make the fallback useful offline.
    for makefile in _iter_makefiles(root):
        try:
            text = makefile.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for match in re.finditer(
            r"^\s*define\s+Package/([^\s/]+)/config\s*$",
            text,
            flags=re.MULTILINE,
        ):
            end = re.search(r"^\s*endef\s*$", text[match.end() :], flags=re.MULTILINE)
            if end is None or match.group(1) not in result:
                continue
            body = text[match.end() : match.end() + end.start()]
            result[match.group(1)].options.extend(_parse_kconfig(body))
    return result


def _associate_options(options: Sequence[KconfigOption], packages: Mapping[str, PackageMetadata]) -> None:
    symbols = sorted(
        ((package.symbol, package.name) for package in packages.values()),
        key=lambda item: len(item[0]),
        reverse=True,
    )
    for option in options:
        if not option.symbol.startswith("CONFIG_PACKAGE_"):
            continue
        for package_symbol, package_name in symbols:
            if option.symbol == package_symbol or option.symbol.startswith(package_symbol + "_"):
                option.package = package_name
                if option.symbol != package_symbol:
                    packages[package_name].options.append(option)
                break


def _parse_generated_options(path: Path, packages: Mapping[str, PackageMetadata]) -> None:
    text = path.read_text(encoding="utf-8", errors="replace")
    options = _parse_kconfig(text)

    # A package's ``define Package/<name>/config`` block is expanded by
    # OpenWrt into a Kconfig region whose child symbols are not required to
    # share the package's ``PACKAGE_<name>_`` prefix.  Common examples are
    # ``NODEJS_20`` and ``ZABBIX_SQLITE``.  Use the top-level package config
    # boundaries in the generated file to retain those options as well.
    lines = text.splitlines()
    package_headers: list[tuple[int, int, str]] = []
    header_rows: list[tuple[int, str]] = []
    for line_no, raw in enumerate(lines, 1):
        # Generated package Kconfig uses both ``config`` and ``menuconfig``
        # for the package's top-level symbol.  Treat both as block headers;
        # otherwise the children of a ``menuconfig PACKAGE_*`` block would be
        # attributed to the preceding package when ownership is determined by
        # source-line ranges.
        match = re.match(
            r"^(\s*)(?:config|menuconfig)\s+PACKAGE_([A-Za-z0-9_.+-]+)\s*$",
            raw,
        )
        if match:
            header_rows.append((len(match.group(1)), line_no))
    if header_rows:
        package_indent = min(indent for indent, _line in header_rows)
        for indent, line_no in header_rows:
            if indent != package_indent:
                continue
            raw = lines[line_no - 1].strip()
            package_name = raw.split(None, 1)[1][len("PACKAGE_") :]
            if package_name in packages:
                package_headers.append((line_no, package_indent, package_name))

    if not package_headers:
        # A fork may emit package symbols without the top-level package
        # blocks.  Prefix matching is a useful fallback there, although it
        # is deliberately avoided when block boundaries are available:
        # ``PACKAGE_dnsmasq_full_*`` otherwise gets mistaken for the shorter
        # ``dnsmasq`` package instead of its owning ``dnsmasq-full`` block.
        _associate_options(options, packages)
        return
    # ``.config-package.in`` contains thousands of package blocks.  Looking
    # through every header for every option makes catalog preparation
    # quadratic (and becomes painfully slow on a real feed set), while the
    # headers are already ordered by source line.  A binary search gives each
    # option its surrounding package block in logarithmic time.
    header_lines = [item[0] for item in package_headers]
    option_symbols = {
        package_name: {item.symbol for item in package.options}
        for package_name, package in packages.items()
    }
    for option in options:
        line_no = int(getattr(option, "_line_no", 0))
        if not line_no:
            continue
        header_index = bisect_right(header_lines, line_no) - 1
        if header_index < 0:
            continue
        package_name = package_headers[header_index][2]
        package = packages[package_name]
        if option.symbol == package.symbol:
            option.package = package_name
            continue
        # The generated block is authoritative for ownership.  In
        # particular, a child symbol may share a prefix with another package
        # (``dnsmasq`` versus ``dnsmasq-full``) and cannot safely be assigned
        # by symbol-prefix matching.
        option.package = package_name
        if option.symbol not in option_symbols[package_name]:
            package.options.append(option)
            option_symbols[package_name].add(option.symbol)


def _merge_raw_fields(generated: PackageMetadata, raw: PackageMetadata | None) -> None:
    if raw is None:
        return
    # Generated metadata wins.  Raw Makefile fields only fill gaps left by a
    # fork's metadata generator, which is useful for display but cannot create
    # a package absent from .packageinfo.
    for field_name in (
        "path", "feed", "title", "section", "category", "submenu", "repository",
        "architecture", "description", "menu",
    ):
        if not getattr(generated, field_name) and getattr(raw, field_name):
            setattr(generated, field_name, getattr(raw, field_name))
    if not generated.depends and raw.depends:
        generated.depends = raw.depends


def scan_catalog(root: str | Path) -> Catalog:
    """Scan one prepared OpenWrt source tree.

    Generated metadata and config files are preferred together.  If either is
    absent, package Makefiles are used only as a clearly non-authoritative
    fallback; callers must run native ``make defconfig`` before a build.
    """

    source_root = Path(root).expanduser().resolve()
    if not source_root.is_dir():
        raise FileNotFoundError(source_root)
    packageinfo = source_root / "tmp/.packageinfo"
    config_package = source_root / "tmp/.config-package.in"
    generated_metadata = packageinfo.is_file()
    generated_config = config_package.is_file()

    raw = _parse_raw_makefiles(source_root)
    if generated_metadata:
        packages_list = _parse_packageinfo(packageinfo)
        packages: dict[str, PackageMetadata] = {item.name: item for item in packages_list}
        for name, item in packages.items():
            _merge_raw_fields(item, raw.get(name))
    else:
        packages = raw

    if generated_config and packages:
        _parse_generated_options(config_package, packages)
    elif not generated_config:
        # Raw options were parsed with their source package, but constrain the
        # fallback to package symbols so random Makefile Kconfig is not exposed.
        for item in packages.values():
            item.options = [
                option for option in item.options
                if option.symbol.startswith("CONFIG_PACKAGE_")
            ]

    for package in packages.values():
        # De-duplicate options from duplicate include paths deterministically.
        seen: set[str] = set()
        unique: list[KconfigOption] = []
        for option in package.options:
            if option.symbol not in seen:
                seen.add(option.symbol)
                unique.append(option)
        package.options = sorted(unique, key=lambda item: item.symbol)

    ordered = sorted(packages.values(), key=lambda item: item.name)
    generated_files = tuple(
        str(path.relative_to(source_root))
        for path in (packageinfo, config_package)
        if path.is_file()
    )
    return Catalog(
        root=source_root,
        packages=ordered,
        authoritative=generated_metadata and generated_config,
        generated_files=generated_files,
        metadata_source="generated" if generated_metadata else "makefile",
    )


__all__ = [
    "Catalog",
    "DeviceSpec",
    "KconfigDefault",
    "KconfigOption",
    "PackageMetadata",
    "scan_catalog",
]
