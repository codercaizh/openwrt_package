from __future__ import annotations

import hashlib
import json
from pathlib import Path
import subprocess

import pytest

from owrt_builder import local_patches
from owrt_builder.build import BuildEngine
from owrt_builder.sources import PREPARATION_VERSION, PreparedSource, SourceError, SourceManager


def fixture_bundle(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path]:
    bundle = tmp_path / 'bundle'
    overlay = bundle / 'overlay'
    (overlay / 'files/usr/sbin').mkdir(parents=True)
    (overlay / 'files/usr/sbin/iptv-hnat-diag').write_text('#!/bin/sh\nexit 0\n')
    (bundle / 'reviewed.json').write_text(json.dumps({
        'sha256': {'driver.c': hashlib.sha256(b'original\n').hexdigest()},
    }))
    source = tmp_path / 'source'
    source.mkdir()
    (source / 'driver.c').write_bytes(b'original\n')
    monkeypatch.setattr(local_patches, 'bundle_root', lambda: bundle)
    return bundle, source


def test_patch_install_is_scoped_and_content_addressed(tmp_path, monkeypatch):
    bundle, source = fixture_bundle(tmp_path, monkeypatch)
    assert local_patches.install_local_patches(source, 'armv8') == ''
    assert not (source / 'files').exists()
    original = local_patches.install_local_patches(source, local_patches.SOURCE_ID)
    assert (source / 'files/etc/iptv-hnat-diag.version').read_text().strip() == original
    helper = source / 'files/usr/sbin/iptv-hnat-diag'
    assert helper.stat().st_mode & 0o111
    (bundle / 'overlay/files/usr/sbin/iptv-hnat-diag').write_text('#!/bin/sh\nexit 1\n')
    assert local_patches.patch_fingerprint(local_patches.SOURCE_ID) != original


def test_upstream_drift_fails_before_overlay_is_written(tmp_path, monkeypatch):
    _, source = fixture_bundle(tmp_path, monkeypatch)
    (source / 'driver.c').write_text('changed upstream\n')
    with pytest.raises(ValueError, match='upstream review'):
        local_patches.install_local_patches(source, local_patches.SOURCE_ID)
    assert not (source / 'files').exists()


def test_old_patch_fingerprint_cannot_be_reused(tmp_path, monkeypatch):
    fixture_bundle(tmp_path, monkeypatch)
    sid = local_patches.SOURCE_ID
    snapshot = tmp_path / 'state' / sid / 'snapshots' / 'old'
    (snapshot / 'source').mkdir(parents=True)
    (snapshot / 'catalog.json').write_text('{}')
    (snapshot / 'manifest.json').write_text(json.dumps({
        'preparation_version': PREPARATION_VERSION, 'source_id': sid,
        'snapshot_id': 'old', 'source_commit': 'test', 'local_patch_id': 'obsolete',
    }))
    manager = SourceManager(tmp_path / 'state')
    with pytest.raises(SourceError, match='obsolete local kernel patches'):
        manager.get_snapshot('old')


def test_changed_patch_cleans_only_linux_and_stamp_requires_success(tmp_path):
    engine = BuildEngine(repo_root=Path(__file__).resolve().parents[1], workspace=tmp_path / 'work', use_docker=False)
    root = tmp_path / 'source'
    (root / 'files/etc').mkdir(parents=True)
    version = root / 'files/etc/iptv-hnat-diag.version'
    version.write_text('v1\n')
    config = tmp_path / 'generated.config'
    config.write_text('CONFIG_TARGET_mediatek=y\n')
    commands = []
    engine._run_checked = lambda command, **kwargs: commands.append(command)
    engine._run_compile_with_diagnostics = lambda *args, **kwargs: None
    prepared = PreparedSource(local_patches.SOURCE_ID, 'test', root, tmp_path / 'catalog', 'test')
    def run():
        engine._run_pipeline(engine.catalog.resolve('n60pro'), prepared, root, config, lambda _: None, None, jobs=1)
    run()
    assert commands == [['make', 'defconfig'], ['make', 'target/linux/clean'], ['make', 'download']]
    commands.clear()
    run()
    assert ['make', 'target/linux/clean'] not in commands
    version.write_text('v2\n')
    def fail(*args, **kwargs):
        raise RuntimeError('compile failed')
    engine._run_compile_with_diagnostics = fail
    with pytest.raises(RuntimeError, match='compile failed'):
        run()
    assert (root / 'build_dir/.iptv-hnat-build-id').read_text().strip() == 'v1'


def test_diagnostic_helper_is_valid_posix_shell():
    helper = local_patches.bundle_root() / 'overlay/files/usr/sbin/iptv-hnat-diag'
    subprocess.run(['sh', '-n', str(helper)], check=True)
