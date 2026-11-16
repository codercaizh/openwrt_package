"""Prepare, restore and publish the master firmware cache layers in Actions."""

from __future__ import annotations

import argparse
from dataclasses import asdict, replace
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import shutil
import subprocess
from urllib.request import urlopen

from . import cache_archive
from .cache_versions import (
    BRANCH, REF, REPOSITORY, UPSTREAM, SHA, Candidate, Version, ancestor, github,
    has_newer, inventory, maximal, select,
)
from .devices import load_catalog
from .feeds import FEED_SPECS, prepare_feeds_sync
from .local_patches import patch_fingerprint
from .paths import repository_root
from .sources import (
    PREPARATION_VERSION, SourceManager, _restore_tracked_download_seeds,
    _tracked_download_entries, stage_download_seeds,
)
from .upstream_versions import _latest_tailscale_tag, _tailscale_tag_commit


SCHEMA = 3
SLOT = "ci-device"
WORKER = "/workspace/work"
LAYERS = ("source", "host", "toolchain", "dl")
MIB = 1024**2
LAYER_LIMIT = {"source": 500, "host": 1900, "toolchain": 1600, "dl": 2100}
UNCACHEABLE_SOURCE = {"dl", "build_dir", "staging_dir", "bin", "tmp", "logs", "artifacts"}
DL_NAME = re.compile(r"[A-Za-z0-9_+@][A-Za-z0-9_+@.,~-]*\Z")
CONFIG_SYMBOL = re.compile(r"\bCONFIG_[A-Za-z0-9_]+\b")
ERRORS = (OSError, ValueError, KeyError, TypeError, RuntimeError, subprocess.SubprocessError)


def log(message: str) -> None:
    print(message, flush=True)


def digest(data) -> str:
    return hashlib.sha256(json.dumps(data, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def checksum(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def load(path: Path) -> dict:
    if not path.is_file() or path.is_symlink():
        raise ValueError(f"missing regular manifest {path}")
    value = json.loads(path.read_text())
    if not isinstance(value, dict):
        raise ValueError(f"invalid manifest type {path}")
    return value


def save(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, sort_keys=True) + "\n")


def tracked_files(root: Path, names: list[str], *, exclude_git: bool = False):
    for name in sorted(set(names)):
        path = root / name
        if path.is_symlink() or path.is_file():
            yield name, path
        elif path.is_dir():
            for directory, dirs, files in os.walk(path):
                dirs.sort()
                if exclude_git and ".git" in dirs:
                    dirs.remove(".git")
                directory = Path(directory)
                for item in sorted(files + [entry for entry in dirs if (directory / entry).is_symlink()]):
                    candidate = directory / item
                    yield candidate.relative_to(root).as_posix(), candidate


def fingerprints(root: Path, names: list[str], *, exclude_git: bool = False) -> dict:
    return {
        name: ["link", str(path.readlink())] if path.is_symlink() else
        [path.stat().st_mode & 0o777, checksum(path)]
        for name, path in tracked_files(root, names, exclude_git=exclude_git)
    }


def git_head(url: str, ref: str) -> str:
    result = subprocess.check_output(
        ["git", "ls-remote", "--exit-code", url, ref], text=True, timeout=60
    )
    sha, _, actual = result.strip().partition("\t")
    if not SHA.fullmatch(sha) or actual != ref:
        raise ValueError(f"{url} did not resolve exactly {ref}")
    return sha


def feed_pins(upstream: str) -> dict:
    url = f"https://raw.githubusercontent.com/{UPSTREAM}/{upstream}/feeds.conf.default"
    with urlopen(url, timeout=45) as response:
        lines = response.read().decode().splitlines()
    native = []
    refs: dict[tuple[str, str], str] = {}

    def resolve(url: str, ref: str) -> str:
        if (url, ref) not in refs:
            refs[url, ref] = git_head(url, ref)
        return refs[url, ref]

    for line in lines:
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        kind, name, address = line.split()
        if kind not in {"src-git", "src-git-full"} or not re.fullmatch(r"[a-zA-Z0-9_-]+", name):
            raise ValueError(f"unsupported native feed entry {line}")
        remote, branch_marker, revision = address.partition(";")
        if not branch_marker:
            remote, _, revision = address.partition("^")
        if not re.fullmatch(r"https://github\.com/[\w.-]+/[\w.-]+", remote):
            raise ValueError(f"unsupported native feed origin {remote}")
        ref = f"refs/heads/{revision}" if revision else "HEAD"
        sha = revision if SHA.fullmatch(revision) else resolve(remote, ref)
        native.append([kind, name, remote, sha])
    custom = {
        item.name: item.commit or resolve(item.url, f"refs/heads/{item.branch}" if item.branch else "HEAD")
        for item in FEED_SPECS
    }
    tag = _latest_tailscale_tag()
    return {"native": native, "custom": custom, "tailscale": [tag, _tailscale_tag_commit(tag)]}


def verify_feeds(root: Path, status, pins: dict) -> dict[str, str]:
    config = root / "feeds.conf"
    if config.exists() or config.is_symlink():
        raise ValueError("pinned preparation requires the standard upstream feeds.conf.default")
    config.write_text("".join(f"{kind} {name} {url}^{sha}\n" for kind, name, url, sha in pins["native"]))
    commits = prepare_feeds_sync(
        root, status=status,
        feed_specs=tuple(replace(item, commit=pins["custom"][item.name]) for item in FEED_SPECS),
        pinned_tailscale=tuple(pins["tailscale"]),
    )
    expected = {
        **{f"default:{name}": sha for _, name, _, sha in pins["native"]},
        **pins["custom"],
        "tailscale-source": pins["tailscale"][1],
    }
    if commits != expected:
        raise ValueError(f"prepared feed commits differ from the pins: {commits} != {expected}")
    makefile = root / "include/download.mk"
    text = makefile.read_text()
    needle = "  download: $(DL_DIR)/$(FILE)"
    if text.count(needle) != 1:
        raise ValueError("upstream Download macro has changed")
    guard = (repository_root() / "scripts/owrt-cache-download.mk").read_text().rstrip()
    shutil.copyfile(repository_root() / "scripts/owrt-cache-download.sh", root / "scripts/owrt-cache-download.sh")
    makefile.write_text(text.replace(needle, f"{guard}\n{needle}"))
    return commits


def set_output(key: str, value: str) -> None:
    if output := os.environ.get("GITHUB_OUTPUT"):
        with open(output, "a", encoding="utf-8") as stream:
            stream.write(f"{key}={value}\n")
    log(f"CACHE_OUTPUT {key}={value}")


class Cache:
    def __init__(self, workspace: Path):
        self.workspace = workspace
        self.root = workspace / "cache"
        self.transfer = self.root / "transfer"
        self.plan_path = self.transfer / "plan.json"
        self.sources = self.root / "source-workspaces"
        self.host = self.root / "host-tools/master"
        self.device = self.root / "builds" / SLOT
        self.dl = self.root / "dl"

    @property
    def plan(self) -> dict:
        return load(self.plan_path)

    def archive(self, family: str) -> Path:
        return self.transfer / f"{family}.tar.zst"

    def prefix(self, family: str, plan: dict) -> str:
        scope_field = {"source": "source_id", "toolchain": "device"}.get(family)
        scope = plan[scope_field] if scope_field else "master"
        return f"owrt-{family}-v{SCHEMA}-linux-x64-{scope}-"

    def candidates(self, family: str, plan: dict) -> list[Candidate]:
        prefix = self.prefix(family, plan)
        candidates = (Candidate.read(prefix, item["key"]) for item in inventory(prefix))
        return list(dict.fromkeys(item for item in candidates if item is not None))

    def choose(self, family: str, plan: dict, *, fallback: bool = False) -> str:
        try:
            candidates = self.candidates(family, plan)
            version = Version(**plan["version"])
            identity = plan["identities"][family]
            if fallback:
                choices = maximal(candidates, version)
                if len({item.version for item in choices}) == 1 and len(choices) > 1:
                    choices = [item for item in choices if item.identity == identity]
                selected = choices[0] if len(choices) == 1 else None
            else:
                selected = select(candidates, version, identity)
            if selected is not None:
                return selected.key
            log(f"CACHE_RESTORE family={family} decision=cold reason=no_unique_compatible_key")
        except ERRORS as exc:
            log(f"CACHE_RESTORE family={family} decision=cold reason={exc}")
        return ""

    def create_plan(self, device: str, pinned: str = "") -> None:
        if not trusted():
            raise ValueError("cache plan is restricted to the master workflow")
        catalog = load_catalog()
        spec = catalog.resolve(device)
        source = catalog.source_for(spec)
        builder = os.environ["GITHUB_SHA"]
        upstream = pinned or git_head(source.url, "refs/heads/master")
        if not SHA.fullmatch(builder) or not SHA.fullmatch(upstream):
            raise ValueError("builder and upstream must be full Git SHAs")
        if pinned and not ancestor(UPSTREAM, upstream, git_head(source.url, "refs/heads/master")):
            raise ValueError("pinned upstream is outside master history")
        supplied_pins = os.environ.get("PINNED_FEEDS")
        pins = json.loads(supplied_pins) if pinned and supplied_pins else feed_pins(upstream)
        set_output("upstream", upstream)
        set_output("feed_pins", json.dumps(pins, separators=(",", ":")))
        checkout = repository_root()
        prep = fingerprints(checkout, [
            "owrt_builder/feeds.py", "owrt_builder/sources.py",
            "owrt_builder/upstream_versions.py", "owrt_builder/catalog.py",
            "owrt_builder/source_sync.py", "scripts/owrt-cache-download.mk", "scripts/owrt-cache-download.sh",
        ])
        plan = {
            "device": spec.key, "target": spec.target, "source_id": source.id,
            "upstream_url": source.url, "pins": pins, "preparation": PREPARATION_VERSION,
            "patch": patch_fingerprint(source.id),
            "version": asdict(Version(builder, upstream)),
            "identities": {
                "dl": digest([upstream, pins, fingerprints(checkout, ["configs"])]),
                "source": digest([source.id, upstream, pins, prep, patch_fingerprint(source.id)]),
            },
        }
        plan["keys"] = {
            "source": self.choose("source", plan, fallback=True),
            "dl": self.choose("dl", plan, fallback=True),
        }
        save(self.plan_path, plan)
        self.report_keys(plan, "source", "dl")

    @staticmethod
    def report_keys(plan: dict, *families: str) -> None:
        for family in families:
            set_output(family, plan["keys"][family])

    def manifest(self, family: str, plan: dict) -> dict:
        return {
            "schema": SCHEMA, "family": family, "repository": REPOSITORY, "branch": BRANCH,
            "identity": plan["identities"][family], "version": plan["version"], "ready": True,
            "source_id": plan["source_id"], "upstream_url": plan["upstream_url"],
            "pins": plan["pins"], "patch": plan["patch"], "preparation": plan["preparation"],
            "device": plan["device"], "target": plan["target"], "environment": plan["environment"],
            "run": os.environ["GITHUB_RUN_ID"], "attempt": os.environ["GITHUB_RUN_ATTEMPT"],
        }

    def allowed(self, family: str, path: str) -> bool:
        parts = path.split("/")
        if parts == ["ci-manifest.json"]:
            return True
        if family == "source":
            return parts[0] in {"manifest.json", "catalog.json"} and len(parts) == 1 or (
                parts[0] == "source" and (len(parts) == 1 or parts[1] not in UNCACHEABLE_SOURCE)
            )
        if family == "dl":
            return all(DL_NAME.fullmatch(part) and not part.endswith((".tmp", ".partial", ".dl", ".hash", ".lock", ".log"))
                       for part in parts)
        if family == "host":
            return parts[0] in {"build_dir", "staging_dir"} and (len(parts) == 1 or parts[1] == "host")
        return parts[0] in {"build_dir", "staging_dir"} and (
            len(parts) == 1 or parts[1] == "hostpkg" or parts[1].startswith("toolchain-")
        )

    def restore(self, family: str, plan: dict, root: Path, verify) -> dict | None:
        key = plan["keys"].get(family)
        if not key or not self.archive(family).exists():
            return None
        restored = None
        try:
            candidate = Candidate.read(self.prefix(family, plan), key)
            if candidate is None:
                raise ValueError("cache key lacks a valid version")

            def validate(path: Path) -> None:
                manifest = load(path / "ci-manifest.json")
                expected = {"schema": SCHEMA, "family": family, "repository": REPOSITORY,
                            "branch": BRANCH, "ready": True, "identity": candidate.identity,
                            "version": asdict(candidate.version)}
                if any(manifest.get(name) != value for name, value in expected.items()):
                    raise ValueError("manifest provenance differs from the selected key")
                verify(path, manifest)

            restored = cache_archive.unpack(
                self.archive(family), self.transfer, lambda path: self.allowed(family, path),
                validate, compiler=family in {"host", "toolchain"},
            )
            metadata = load(restored / "ci-manifest.json")
            (restored / "ci-manifest.json").unlink()
            cache_archive.install(restored, root, sorted(entry.name for entry in restored.iterdir()))
            log(f"CACHE_RESTORE family={family} key={key} decision=accepted path={root}")
            return metadata
        except ERRORS as exc:
            log(f"CACHE_RESTORE family={family} key={key} decision=cold reason={exc}")
            return None
        finally:
            self.archive(family).unlink(missing_ok=True)
            if restored is not None:
                shutil.rmtree(restored)

    def prepare(self) -> None:
        plan = self.plan
        package_versions = subprocess.check_output(["dpkg-query", "-W"], text=True)
        plan["environment"] = digest([
            platform.machine(), platform.system(), WORKER, SLOT, os.getuid() == 0,
            checksum(repository_root() / "docker/Dockerfile"), sorted(package_versions.splitlines()),
        ])
        catalog = load_catalog()
        spec = catalog.resolve(plan["device"])
        manager = SourceManager(self.sources, source_specs=catalog.sources)
        upstream = plan["version"]["upstream"]

        def verify_source(path: Path, manifest: dict) -> None:
            preparation = load(path / "manifest.json")
            source = path / "source"
            git = source / ".git"
            verify_git(source, manifest["version"]["upstream"])
            if manifest["source_id"] != spec.source_id or manifest["upstream_url"] != plan["upstream_url"]:
                raise ValueError("source scope or upstream URL changed")
            if (
                not (path / "catalog.json").is_file() or (path / "catalog.json").is_symlink()
                or not git.is_dir() or git.is_symlink()
                or not re.fullmatch(r"[0-9a-f]{64}", preparation["snapshot_id"])
                or preparation["source_commit"] != manifest["version"]["upstream"]
                or preparation["source_id"] != spec.source_id
                or manifest["tree"] != digest(fingerprints(source, source_names(source)))
                or manifest["catalog"] != checksum(path / "catalog.json")
            ):
                raise ValueError("source tree, catalog or Git metadata is incomplete")

        seed = self.transfer / "seed"
        metadata = self.restore("source", plan, seed, verify_source)
        prepared = None
        if metadata and metadata["identity"] == plan["identities"]["source"]:
            snapshot_id = load(seed / "manifest.json")["snapshot_id"]
            final = self.sources / spec.source_id / "snapshots" / snapshot_id
            final.parent.mkdir(parents=True, exist_ok=True)
            os.rename(seed, final)
            try:
                tree = final / "source"
                _restore_tracked_download_seeds(tree, _tracked_download_entries(tree))
                prepared = manager._read_prepared(final, expected_source_id=spec.source_id)
                manager._publish(spec.source_id, final)
            except ERRORS as exc:
                shutil.rmtree(final)
                metadata = None
                log(f"CACHE_RESTORE family=source decision=cold reason={exc}")
        if prepared is None:
            prepared = manager.prepare_source(
                spec, pinned_commit=upstream, baseline=seed / "source" if metadata else None,
                feed_preparer=lambda path, status: verify_feeds(path, status, plan["pins"]),
                status=lambda event: log(f"[{event['phase']}] {event['message']}"),
            )
        if seed.is_dir():
            shutil.rmtree(seed)
        plan["source_cached"] = bool(metadata and metadata["identity"] == plan["identities"]["source"])
        plan["snapshot"] = prepared.snapshot_id
        plan["feeds"] = dict(prepared.feed_commits)
        source = prepared.path

        def verify_dl(path: Path, manifest: dict) -> None:
            if download_entries(path) != manifest["downloads"]:
                raise ValueError("download file checksums or sizes differ")

        dl_metadata = self.restore("dl", plan, self.dl, verify_dl)
        if not dl_metadata:
            self.legacy_llvm(source)
        stage_download_seeds(source, self.dl)
        plan["download_baseline"] = dl_metadata["downloads"] if dl_metadata else {}
        plan["download_same_inputs"] = bool(dl_metadata and dl_metadata["identity"] == plan["identities"]["dl"])
        log(f"CACHE_DOWNLOAD restored_files={len(plan['download_baseline'])} "
            f"restored_bytes={sum(item[0] for item in plan['download_baseline'].values())}")
        common = ["Makefile", "rules.mk", "Config.in", "config", "include", "scripts"]
        plan["host_files"] = fingerprints(source, [*common, "tools"])
        plan["toolchain_files"] = fingerprints(
            source, [*common, "toolchain", "target/Config.in", "target/config", f"target/linux/{spec.target.split('/')[0]}"],
        )
        # Device config files are included before defconfig; the resolved ABI is
        # checked again before Make uses any restored toolchain.
        config_input = digest(fingerprints(repository_root(), ["configs"]))
        plan["toolchain_inputs"] = digest([
            plan["environment"], spec.key, spec.target, config_input, plan["toolchain_files"],
        ])
        plan["identities"].update(
            host=digest([plan["environment"], plan["host_files"], config_input, plan["pins"]]),
            toolchain=digest([plan["toolchain_inputs"], plan["pins"]]),
        )
        save(self.plan_path, plan)
        log(f"CACHE_BASELINE builder={plan['version']['builder']} upstream={upstream} "
            f"feeds={digest(plan['feeds'])} environment={plan['environment']} source={plan['snapshot']}")

    def legacy_llvm(self, source: Path) -> None:
        # The old action supplies exactly this path and nothing in the v2 compiler layers.
        legacy = self.dl / "llvm-project-22.1.3.src.tar.xz"
        if legacy.exists() or legacy.is_symlink():
            makefile = (source / "tools/llvm-bpf/Makefile").read_text()
            expected = re.search(r"(?m)^PKG_HASH:=([a-f0-9]{64})$", makefile)
            version = re.search(r"(?m)^PKG_VERSION:=22\.1\.3$", makefile)
            if not expected or not version or legacy.is_symlink() or checksum(legacy) != expected[1]:
                legacy.unlink()
                log("CACHE_DOWNLOAD legacy LLVM rejected by current recipe")
            else:
                log("CACHE_DOWNLOAD legacy LLVM verified against current recipe")

    def compiler_keys(self) -> None:
        plan = self.plan
        plan["keys"].update(
            host=self.choose("host", plan, fallback=True),
            toolchain=self.choose("toolchain", plan, fallback=True),
        )
        save(self.plan_path, plan)
        self.report_keys(plan, "host", "toolchain")

    def restore_compilers(self) -> None:
        plan = self.plan

        def verify_host(path: Path, manifest: dict) -> None:
            require_environment(manifest, plan)
            require_dirs(path, ["build_dir/host", "staging_dir/host"])
            expected = plan["host_files"]
            previous = manifest["files"]
            changes = {name for name in expected.keys() | previous.keys() if expected.get(name) != previous.get(name)}
            if changes and any(not name.startswith("tools/llvm-bpf/") for name in changes):
                raise ValueError("Host inputs outside LLVM-BPF changed")
            plan["llvm_input_changed"] = bool(changes)
            validate_times(manifest, previous)

        def verify_toolchain(path: Path, manifest: dict) -> None:
            require_environment(manifest, plan)
            for name in ("device", "target", "source_id"):
                if manifest[name] != plan[name]:
                    raise ValueError(f"toolchain {name} changed")
            if manifest["inputs"] != plan["toolchain_inputs"]:
                raise ValueError("toolchain inputs changed")
            require_toolchain(path)
            validate_times(manifest, plan["toolchain_files"])

        plan["host_restore"] = self.restore("host", plan, self.host, verify_host)
        build = self.device / "openwrt"
        plan["toolchain_restore"] = self.restore("toolchain", plan, build, verify_toolchain)
        if plan["toolchain_restore"]:
            save(self.device / "cache.json", {
                "schema_version": 1, "device": SLOT, "source_id": plan["source_id"],
                "snapshot_id": plan["snapshot"], "source_commit": plan["version"]["upstream"],
                "ready": True,
            })
        save(self.plan_path, plan)

    def stage(self, family: str) -> None:
        """Pack one eligible layer after a successful build."""
        plan = self.plan
        result = load(self.transfer / "compiled.json")
        completed = [load(path) for path in (self.workspace / "tasks").glob("*/result.json")]
        if not any(row.get("status") == "success" and row.get("device") == plan["device"]
                   and row.get("snapshot_id") == plan["snapshot"] for row in completed):
            raise ValueError("no successful firmware result for the selected snapshot")
        archive = self.archive(family)
        archive.unlink(missing_ok=True)
        if family in {"host", "dl"} and plan["device"] != "netcore_n60-pro":
            log(f"CACHE_PUBLISH family={family} decision=skip reason=n60pro_is_shared_layer_writer")
            return
        try:
            usage = github(REPOSITORY, "actions/cache/usage")["active_caches_size_in_bytes"]
            free = shutil.disk_usage(self.workspace).free
            if usage >= budget() or free <= 2 * LAYER_LIMIT[family] * MIB:
                raise ValueError(f"repository_usage={usage} disk_free={free} budget={budget()}")
            excluded = []
            if family == "source":
                root = self.sources / plan["source_id"] / "snapshots" / plan["snapshot"]
                source = root / "source"
                paths = ["source", "manifest.json", "catalog.json"] if not plan["source_cached"] else []
                excluded = [f"source/{name}" for name in UNCACHEABLE_SOURCE]
                metadata = {
                    "tree": digest(fingerprints(source, source_names(source))),
                    "catalog": checksum(root / "catalog.json"),
                } if paths else {}
            elif family == "dl":
                root = self.dl
                downloads = download_entries(root)
                new_bytes = sum(value[0] for name, value in downloads.items() if plan["download_baseline"].get(name) != value)
                log(f"CACHE_DOWNLOAD final_files={len(downloads)} new_or_changed_bytes={new_bytes}")
                paths = sorted(downloads) if downloads != plan["download_baseline"] else []
                metadata = {"downloads": downloads}
            else:
                root = self.host if family == "host" else self.device / "openwrt"
                metadata = result[family]
                paths = []
                if not metadata["reused"]:
                    if family == "host":
                        paths = ["build_dir/host", "staging_dir/host"]
                        require_dirs(root, paths)
                    else:
                        require_toolchain(root)
                        paths = toolchain_paths(root)
            if not paths:
                log(f"CACHE_PUBLISH family={family} decision=skip reason=existing_compatible_layer_or_empty")
                return
            manifest = root / "ci-manifest.json"
            save(manifest, {**self.manifest(family, plan), **metadata})
            try:
                cache_archive.pack(archive, root, ["ci-manifest.json", *paths], exclude=excluded)
            finally:
                manifest.unlink(missing_ok=True)
            cache_archive.inspect(archive, lambda item: self.allowed(family, item),
                                  compiler=family in {"host", "toolchain"})
            size = archive.stat().st_size
            if size > LAYER_LIMIT[family] * MIB or usage + size > budget():
                raise ValueError(f"compressed_size={size} repository_usage={usage} budget={budget()}")
            log(f"CACHE_SIZE family={family} compressed_bytes={size}")
        except ERRORS as exc:
            archive.unlink(missing_ok=True)
            log(f"CACHE_PUBLISH family={family} decision=skip reason={exc}")

    def publish(self, family: str) -> None:
        """Check the authoritative heads again immediately before the save action."""
        set_output("key", "")
        if not trusted() or not self.archive(family).is_file():
            return
        try:
            plan = self.plan
            version = Version(**plan["version"])
            latest = self.candidates(family, plan)
            head = Version(
                git_head(f"https://github.com/{REPOSITORY}.git", REF),
                git_head(plan["upstream_url"], "refs/heads/master"),
            )
            log(f"CACHE_PUBLISH_CHECK family={family} build={version} current={head} "
                f"candidates={[item.key for item in latest]}")
            if not version.precedes(head) or has_newer(latest, version):
                log(f"CACHE_PUBLISH family={family} decision=SKIP_STALE_PUBLISH")
                return
            if family in {"source", "dl"} and any(
                item.version == version and item.identity != plan["identities"][family] for item in latest
            ) and feed_pins(version.upstream) != plan["pins"]:
                log(f"CACHE_PUBLISH family={family} decision=SKIP_STALE_PUBLISH reason=moved_feeds")
                return
            key = f"{self.prefix(family, plan)}{version.builder}-{version.upstream}-{plan['identities'][family]}"
            if any(item.key == key for item in latest):
                log(f"CACHE_PUBLISH family={family} decision=skip reason=immutable_key_exists key={key}")
                return
            usage = github(REPOSITORY, "actions/cache/usage")["active_caches_size_in_bytes"]
            if usage + self.archive(family).stat().st_size > budget():
                log(f"CACHE_PUBLISH family={family} decision=skip reason=repository_cache_budget")
                return
            log(f"CACHE_PUBLISH family={family} decision=PUBLISH_IMMUTABLE key={key}")
            set_output("key", key)
        except ERRORS as exc:
            log(f"CACHE_PUBLISH family={family} decision=skip reason={exc}")

    def audit(self, family: str, key: str) -> None:
        if not key:
            return
        try:
            row = next((item for item in inventory(key) if item["key"] == key), None)
            if row is None:
                log(f"CACHE_PUBLISH family={family} decision=unconfirmed key={key}")
            else:
                log(f"CACHE_PUBLISH family={family} decision=confirmed key={key} "
                    f"bytes={row['size_in_bytes']} ref={row['ref']}")
        except ERRORS as exc:
            log(f"CACHE_PUBLISH family={family} decision=unconfirmed reason={exc}")
        finally:
            self.archive(family).unlink(missing_ok=True)


def trusted() -> bool:
    return (
        os.environ.get("GITHUB_REPOSITORY") == REPOSITORY and os.environ.get("GITHUB_REF") == REF
        and os.environ.get("GITHUB_EVENT_NAME") in {"workflow_dispatch", "schedule", "push"}
    )


def budget() -> int:
    # Reserve headroom for two writers and the small Actions outer archive.
    return int((os.environ.get("OWRT_CACHE_BUDGET_MIB") or "8704")) * MIB


def source_names(source: Path) -> list[str]:
    return [item.name for item in source.iterdir() if item.name not in UNCACHEABLE_SOURCE]


def verify_git(root: Path, expected: str) -> None:
    git_dir = root / ".git"
    for directory, dirs, files in os.walk(root):
        if ".git" not in dirs:
            continue
        dot_git = Path(directory) / ".git"
        dirs.remove(".git")
        config = dot_git / "config"
        if config.is_symlink() or not config.is_file():
            raise ValueError(f"missing regular Git configuration: {config}")
        keys = subprocess.check_output(
            ["git", "config", "--no-includes", "--file", str(config), "--list", "--name-only"],
            text=True,
        ).splitlines()
        if any(not re.fullmatch(
            r"core\.(?:repositoryformatversion|filemode|bare|logallrefupdates|sparsecheckout|sparsecheckoutcone)"
            r"|remote\.origin\.(?:url|fetch|promisor|partialclonefilter)"
            r"|branch\.[\w./-]+\.(?:remote|merge)|extensions\.worktreeconfig",
            key,
        ) for key in keys):
            raise ValueError(f"unsupported option in cached Git configuration: {config}")
        forbidden = ("objects/info/alternates", "objects/info/http-alternates", "info/grafts", "refs/replace")
        if any((dot_git / item).exists() for item in forbidden):
            raise ValueError(f"cached Git objects redirect outside the repository: {dot_git}")
        hooks = dot_git / "hooks"
        if hooks.is_symlink() or (hooks.is_dir() and any(not item.name.endswith(".sample") for item in hooks.iterdir())):
            raise ValueError(f"cache contains a Git hook: {hooks}")
    if not git_dir.is_dir() or git_dir.is_symlink():
        raise ValueError("cached worktree lacks local Git metadata")
    actual = subprocess.check_output(
        ["git", "-C", str(root), "-c", "core.fsmonitor=false", "rev-parse", "--verify", "HEAD^{commit}"],
        text=True,
    ).strip()
    if actual != expected:
        raise ValueError("cached Git HEAD differs from the selected commit")


def download_entries(root: Path) -> dict:
    return {
        name: [path.stat().st_size, checksum(path)]
        for name, path in tracked_files(root, [item.name for item in root.iterdir()] if root.is_dir() else [])
        if path.is_file() and not path.is_symlink() and path.stat().st_size > 0
        and all(DL_NAME.fullmatch(part) and not part.endswith((".tmp", ".partial", ".dl", ".hash", ".lock", ".log"))
                for part in name.split("/"))
    }


def require_environment(manifest: dict, plan: dict) -> None:
    if manifest["environment"] != plan["environment"]:
        raise ValueError("Docker toolchain, architecture, user mode or workspace path changed")


def require_dirs(root: Path, names: list[str]) -> None:
    for name in names:
        path = root / name
        if not path.is_dir() or path.is_symlink() or not any(path.iterdir()):
            raise ValueError(f"incomplete compiler layer: {name}")


def require_toolchain(root: Path) -> None:
    require_dirs(root, ["build_dir", "staging_dir"])
    working = {path.name for path in (root / "build_dir").glob("toolchain-*") if path.is_dir()}
    installed = {path.name for path in (root / "staging_dir").glob("toolchain-*") if (path / "bin").is_dir()}
    if not working.intersection(installed):
        raise ValueError("matching build and installed cross-toolchain directories are missing")


def toolchain_paths(root: Path) -> list[str]:
    return sorted(
        path.relative_to(root).as_posix()
        for parent in ("build_dir", "staging_dir")
        for path in (root / parent).glob("*")
        if (path.name.startswith("toolchain-") or path.name == "hostpkg") and path.is_dir() and not path.is_symlink()
    )


def validate_times(manifest: dict, files: dict) -> None:
    if set(manifest["mtimes"]) != {name for name, value in files.items() if value[0] != "link"}:
        raise ValueError("incomplete source timestamp baseline")
    if any(type(value) is not int or value <= 0 for value in manifest["mtimes"].values()):
        raise ValueError("invalid source timestamp baseline")


def mtime_map(root: Path, files: dict) -> dict:
    return {name: (root / name).stat().st_mtime_ns for name, value in files.items() if value[0] != "link"}


def restore_mtimes(root: Path, old: dict, current: dict) -> None:
    for name, mtime in old["mtimes"].items():
        if current.get(name) == old["files"].get(name):
            path = root / name
            stat = path.stat()
            if stat.st_mtime_ns != mtime:
                os.utime(path, ns=(stat.st_atime_ns, mtime))


class CompilerCache:
    """Gate compiler state at the normal Make entry point."""

    def __init__(self, workspace: Path, source: Path):
        self.cache = Cache(workspace)
        self.plan = self.cache.plan
        self.source = source

    def before_defconfig(self) -> None:
        host, toolchain = (self.plan.get(f"{family}_restore") for family in ("host", "toolchain"))
        # OpenWrt hashes the physical path and mtime of source files into .prepared.
        if toolchain:
            restore_mtimes(self.source, toolchain, self.plan["toolchain_files"])
        if host:
            restore_mtimes(self.source, host, self.plan["host_files"])

    def after_defconfig(self) -> list[str]:
        raw = (self.source / ".config").read_text()
        settings = {
            line.removeprefix("# ").split("=", 1)[0].split()[0]: line
            for line in raw.splitlines() if line.startswith(("CONFIG_", "# CONFIG_"))
        }
        host_symbols = set()
        for _, path in tracked_files(self.source, ["tools", "include/host-build.mk"]):
            if path.name in {"Makefile", "host-build.mk"} and path != self.source / "tools/Makefile":
                host_symbols.update(CONFIG_SYMBOL.findall(path.read_text(errors="replace")))
        host_symbols.update({"CONFIG_AUTOREBUILD", "CONFIG_AUTOREMOVE"})
        host_config = digest({name: settings.get(name) for name in host_symbols})
        abi = digest({name: value for name, value in settings.items() if not name.startswith("CONFIG_PACKAGE_")})
        hostpkg_config = digest([raw, self.plan["snapshot"], self.plan["identities"]["host"]])
        host = self.plan.get("host_restore")
        toolchain = self.plan.get("toolchain_restore")
        host_ok = bool(host and host["config"] == host_config)
        toolchain_ok = bool(toolchain and toolchain["config"] == abi)
        hostpkg_ok = bool(toolchain_ok and toolchain["hostpkg_config"] == hostpkg_config)
        if host and not host_ok:
            for path in (self.cache.host / "build_dir/host", self.cache.host / "staging_dir/host"):
                shutil.rmtree(path)
                path.mkdir()
        if toolchain and not toolchain_ok:
            for name in toolchain_paths(self.source):
                if Path(name).name.startswith("toolchain-"):
                    shutil.rmtree(self.source / name)
        if toolchain and not hostpkg_ok:
            for name in ("build_dir/hostpkg", "staging_dir/hostpkg"):
                shutil.rmtree(self.source / name, ignore_errors=True)
        llvm_changed = host_ok and self.plan.get("llvm_input_changed")
        log(f"CACHE_CHECK host_compatible={host_ok} toolchain_compatible={toolchain_ok} "
            f"hostpkg_compatible={hostpkg_ok} llvm_inputs_changed={bool(llvm_changed)}")
        self.result = {
            "host": {"config": host_config, "reused": host_ok and not llvm_changed,
                     "files": self.plan["host_files"], "mtimes": mtime_map(self.source, self.plan["host_files"])},
            "toolchain": {
                "config": abi, "reused": toolchain_ok, "inputs": self.plan["toolchain_inputs"],
                "hostpkg_config": hostpkg_config, "hostpkg_reused": hostpkg_ok,
                "files": self.plan["toolchain_files"], "mtimes": mtime_map(self.source, self.plan["toolchain_files"]),
            },
        }
        return ["tools/llvm-bpf/clean", "tools/bindgen/clean"] if llvm_changed else []

    def record(self, make: dict) -> None:
        save(self.cache.transfer / "compiled.json", {**self.result, "make": make})


def stamp_status(source: Path, host: Path) -> dict:
    result = {}
    for root, pattern in ((host / "build_dir/host", "llvm-project-*"),
                          (source / "build_dir", "toolchain-*")):
        for directory in root.glob(pattern):
            folders = [directory] if pattern.startswith("llvm") else list(directory.iterdir())
            for folder in folders:
                if folder.is_dir():
                    for item in folder.glob(".*"):
                        if item.is_file() and (item.name.startswith(".prepared") or item.name in {".built", ".configured"}):
                            result[f"{directory.name}/{folder.name}/{item.name}"] = item.stat().st_mtime_ns
    for root in (host / "staging_dir/host/stamp", *source.glob("staging_dir/toolchain-*/stamp")):
        for item in root.glob("*"):
            if item.is_file() and any(name in item.name for name in ("llvm", "gcc", "binutils", "musl")):
                result[f"{root.parent.name}/stamp/{item.name}"] = item.stat().st_mtime_ns
    return result



def verify_warm(cache: Cache) -> None:
    plan = cache.plan
    result = load(cache.transfer / "compiled.json")
    make = result["make"]
    llvm = {name for name in make["before"] if name.startswith("llvm-project-") and name.endswith("/.built")}
    toolchain = {name for name in make["before"] if name.startswith("toolchain-") and name.endswith("/.built")}
    if (
        not plan["source_cached"] or not plan["download_baseline"]
        or not result["host"]["reused"] or not result["toolchain"]["reused"]
        or not llvm or not toolchain
        or bool((llvm | toolchain).intersection(make["changed"]))
        or make["commands"]["llvm_c_cpp"] != 0
    ):
        raise ValueError("N60 Pro warm build did not preserve all four layers and compiler stamps")
    log("CACHE_VALIDATION warm source/download/host/toolchain restored; LLVM and toolchain built stamps unchanged")

def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", default=os.environ.get("OWRT_WORKSPACE", ".owrt"), type=Path)
    parser.add_argument("phase", choices=(
        "plan", "prepare", "compiler-keys", "restore-compilers", "stage", "publish", "audit", "verify-warm"))
    parser.add_argument("value", nargs="?", default="")
    parser.add_argument("option", nargs="?", default="")
    args = parser.parse_args()
    cache = Cache(args.workspace.resolve())
    if args.phase == "plan":
        cache.create_plan(args.value, args.option)
    elif args.phase == "prepare":
        cache.prepare()
    elif args.phase == "compiler-keys":
        cache.compiler_keys()
    elif args.phase == "restore-compilers":
        cache.restore_compilers()
    elif args.phase == "stage":
        cache.stage(args.value)
    elif args.phase == "publish":
        cache.publish(args.value)
    elif args.phase == "audit":
        cache.audit(args.value, args.option)
    elif args.phase == "verify-warm":
        verify_warm(cache)


if __name__ == "__main__":
    main()
