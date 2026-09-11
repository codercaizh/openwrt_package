"""Pure catalog shaping and validation helpers used by Web routes."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence


def normalize_catalog(value: Any) -> list[dict[str, Any]]:
    if hasattr(value, "to_dict") and callable(value.to_dict):
        value = value.to_dict()
    if isinstance(value, Mapping):
        value = value.get("items") or value.get("packages") or value.get("catalog") or []
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        return []
    result: list[dict[str, Any]] = []
    for raw in value:
        if not isinstance(raw, Mapping):
            continue
        item = {str(key): _json_safe(val) for key, val in raw.items()}
        symbol = str(item.get("symbol") or item.get("name") or item.get("package") or "").strip()
        if not symbol:
            continue
        item["symbol"] = symbol
        item.setdefault("name", symbol)
        item.setdefault("title", item.get("prompt") or symbol)
        if not item.get("category"):
            package_name = str(item.get("name") or symbol.removeprefix("CONFIG_PACKAGE_"))
            item["category"] = "luci-app" if package_name.startswith("luci-app-") else ("luci-theme" if package_name.startswith("luci-theme-") else "other")
        item.setdefault("type", "bool")
        item.setdefault("default", False if item["type"] in {"bool", "boolean"} else "")
        item.setdefault("depends", item.get("depends_on") or [])
        item.setdefault("options", item.get("suboptions") or [])
        result.append(item)
    return sorted(result, key=lambda item: (str(item.get("category")), str(item.get("title")), item["symbol"]))


def filter_catalog(items: Iterable[dict[str, Any]], query: str, category: str) -> list[dict[str, Any]]:
    query = query.strip().casefold()
    category = category.strip().casefold()
    result = []
    for item in items:
        item_category = str(item.get("category", "other")).casefold()
        if category and category != "all" and item_category != category:
            continue
        haystack = " ".join(str(item.get(key, "")) for key in ("symbol", "name", "title", "description")).casefold()
        if query and query not in haystack:
            continue
        result.append(item)
    return result


def canonical_package_names(items: Sequence[dict[str, Any]], packages: Sequence[str]) -> list[str]:
    """Resolve accepted package names/symbols to package names.

    The browser always submits names, but accepting ``CONFIG_PACKAGE_*`` here
    keeps the API compatible with small scripts while ensuring the build core
    never receives a Kconfig symbol where it expects a package name.
    Unknown values are intentionally omitted; callers use ``validate_options``
    to return the corresponding structured errors before persisting a job.
    """

    aliases: dict[str, str] = {}
    for item in items:
        name = str(item.get("name") or "").strip()
        symbol = str(item.get("symbol") or "").strip()
        if not name and symbol.startswith("CONFIG_PACKAGE_"):
            name = symbol.removeprefix("CONFIG_PACKAGE_")
        if not name:
            continue
        aliases[name] = name
        if symbol:
            aliases[symbol] = name
        aliases[f"CONFIG_PACKAGE_{name}"] = name
    result: list[str] = []
    for package in packages:
        canonical = aliases.get(str(package))
        if canonical and canonical not in result:
            result.append(canonical)
    return result


def validate_options(items: Sequence[dict[str, Any]], packages: Sequence[str], options: Mapping[str, Any]) -> list[dict[str, Any]]:
    by_symbol: dict[str, dict[str, Any]] = {}
    package_aliases: dict[str, str] = {}
    for item in items:
        symbol = str(item.get("symbol") or "")
        name = str(item.get("name") or symbol.removeprefix("CONFIG_PACKAGE_") or "")
        if symbol:
            by_symbol[symbol] = item
        if name:
            by_symbol[name] = item
            package_aliases[name] = name
        if symbol.startswith("CONFIG_PACKAGE_"):
            package_aliases[symbol] = name
            package_aliases[symbol.removeprefix("CONFIG_PACKAGE_")] = name
    issues: list[dict[str, Any]] = []
    selected: set[str] = set()
    for raw_package in packages:
        symbol = str(raw_package)
        item = by_symbol.get(symbol)
        canonical = package_aliases.get(symbol)
        if item is None or canonical is None:
            issues.append({"kind": "unknown_package", "symbol": symbol, "message": f"目录中不存在包 {symbol}"})
            continue
        selected.add(canonical)
    # Keep the owning package with every option.  A package-local Kconfig
    # symbol is not necessarily prefixed with CONFIG_PACKAGE_<name> (feed
    # metadata can expose symbols such as CONFIG_NODEJS_20), so deriving the
    # owner from the symbol alone would allow an option for an unselected
    # package to pass the HTTP-side checks and fail later in BuildEngine.
    known_options: dict[str, tuple[dict[str, Any], str | None]] = {}
    for item in items:
        for raw in item.get("options", []) or []:
            if not isinstance(raw, Mapping):
                continue
            option = {str(k): _json_safe(v) for k, v in raw.items()}
            key = str(option.get("symbol") or option.get("name") or "")
            if key:
                owner = option.get("package") or option.get("owner") or item.get("name")
                # Accept the native CONFIG_ spelling as well as the short
                # spelling used by a few older clients.  The value sent to
                # BuildEngine is still rendered with the canonical CONFIG_
                # prefix.
                known_options[key] = (option, str(owner) if owner else None)
                if key.startswith("CONFIG_"):
                    known_options[key.removeprefix("CONFIG_")] = (option, str(owner) if owner else None)
    for key, value in options.items():
        option_record = known_options.get(str(key))
        if option_record is None:
            issues.append({"kind": "unknown_option", "symbol": key, "message": f"目录中不存在子选项 {key}"})
            continue
        option, owner = option_record
        if owner:
            owner_name = package_aliases.get(owner, owner.removeprefix("CONFIG_PACKAGE_").removeprefix("PACKAGE_"))
            if owner_name not in selected:
                issues.append({
                    "kind": "option_unselected_package",
                    "symbol": key,
                    "package": owner_name,
                    "message": f"子选项 {key} 属于未选中的包 {owner_name}",
                })
                continue
        type_name = str(option.get("type") or option.get("kind") or "string").lower()
        valid = True
        if type_name in {"bool", "boolean"}:
            valid = isinstance(value, bool)
        elif type_name in {"int", "integer"}:
            valid = isinstance(value, int) and not isinstance(value, bool)
        elif type_name in {"choice", "enum"}:
            choices = option.get("choices") or option.get("values") or []
            allowed = {str(x.get("value") if isinstance(x, Mapping) else x) for x in choices}
            valid = isinstance(value, str) and (not allowed or value in allowed)
        elif type_name == "hex":
            valid = isinstance(value, str) and bool(re.fullmatch(r"0[xX][0-9a-fA-F]+", value))
        elif type_name == "tristate":
            valid = isinstance(value, str) and value in {"y", "m", "n"}
        elif type_name == "string":
            valid = isinstance(value, str)
        if not valid:
            issues.append({"kind": "invalid_value", "symbol": key, "message": f"子选项 {key} 的值类型不正确"})
    # Static dependency feedback is intentionally advisory; authoritative
    # auto-dependency/cannot-remove results come from core defconfig.
    for symbol in selected:
        item = by_symbol.get(symbol)
        for dependency in _dependencies(item.get("depends") if item else None):
            if dependency not in selected and dependency in by_symbol:
                issues.append({"kind": "dependency", "symbol": symbol, "depends_on": dependency, "message": f"{symbol} 依赖 {dependency}，defconfig 可能自动加入"})
    return issues


def public_catalog(items: Sequence[dict[str, Any]], default_packages: Iterable[str] = ()) -> list[dict[str, Any]]:
    """Return selectable UI entries without leaking thousands of internals."""

    defaults = {str(name) for name in default_packages}
    result: list[dict[str, Any]] = []
    for raw in items:
        name = str(raw.get("name") or raw.get("symbol") or "")
        if not name:
            continue
        is_plugin = bool(raw.get("is_plugin")) or name.startswith(("luci-app-", "luci-theme-"))
        if not is_plugin and name not in defaults:
            continue
        item = dict(raw)
        item["category"] = "luci-app" if name.startswith("luci-app-") else (
            "luci-theme" if name.startswith("luci-theme-") else "other"
        )
        result.append(item)
    return result


def _dependencies(value: Any) -> list[str]:
    if isinstance(value, str):
        return [part for part in re.findall(r"[A-Za-z0-9_.+/-]+", value) if part not in {"y", "m", "n", "and", "or", "not"}]
    if isinstance(value, Sequence):
        return [str(item) for item in value if isinstance(item, (str, int))]
    return []


def _json_safe(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, Mapping):
        return {str(k): _json_safe(v) for k, v in value.items()}
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        return [_json_safe(v) for v in value]
    return str(value)


__all__ = [
    "canonical_package_names",
    "filter_catalog",
    "normalize_catalog",
    "public_catalog",
    "validate_options",
]
