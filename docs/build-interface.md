# Build core interface

The web worker and `./owrt` use the same `owrt_builder.build` implementation.
Web code can construct the runtime as follows:

```python
catalog = load_catalog(repo_root / "configs" / "devices.toml")
engine = BuildEngine(repo_root, workspace, catalog=catalog)
request = BuildRequest(
    task_id=job_id,
    device=canonical_device,
    snapshot_id=prepared_snapshot_id,
    packages=selected_packages,  # None keeps the reviewed device defaults
    options=options,
    jobs=jobs,                    # validated integer in [1, effective logical CPUs]
    reuse_cache=reuse_cache,      # defaults to True for Web submissions
)
result = engine.run(request, log_callback, is_cancelled)
```

`BuildEngine.build(request, on_log=..., cancel_event=...)` is the primary
typed method. `run(request, callback, predicate)` is a compatibility adapter
for the queue worker. It returns `BuildResult`; its `to_dict()` includes
`success`, `status`, `artifacts`, `manifest_path`, `config_path`, source and
snapshot IDs, and the real exit code. `cancel(task_id)` removes the task's
Docker container and `inspect(task_id)` returns its Docker state or the state
from the persisted task result for restart reconciliation.

The authenticated Web API submits the same controls as JSON:

```json
{
  "device": "n60pro",
  "packages": ["luci-app-passwall"],
  "options": {},
  "parallel_jobs": 4,
  "reuse_cache": true
}
```

The Web field `parallel_jobs` is converted to the core DTO's `jobs`. It is
optional and defaults to the Web process's effective logical CPU count
(`GET /api/devices` → `runtime.max_parallel_jobs`). It must be an integer from
`1` through that maximum; booleans, decimal numbers and strings are rejected.
`reuse_cache` defaults to `true`. Both values are returned in
the job submission response and job history, including records migrated from
an older database.

`snapshot_id` is opaque. The build worker resolves it through
`SourceManager.get_snapshot()` and verifies it belongs to the requested
device's source. A request without an ID uses the source manager's current
immutable snapshot, preparing one only when none exists. The worker never
follows a moving `current` link after preparation.

The worker writes task files under `workspace/tasks/<task_id>/` and artifacts
under `workspace/artifacts/<device>/<task_id>/`. The Docker host mounts the
whole workspace at `/workspace/work`, so paths in a worker result translate
back to the host. MediaTek workers run with the caller's numeric UID/GID and
the default Docker network. The private `s905d`/`vplus` packager needs root for
loop/filesystem mounts, so its worker is privileged without host networking;
the host-side helper restores the workspace to the caller's UID/GID before the
build command returns. The persistent compiler tree is kept under
`workspace/cache/builds/<canonical-device>`. Source refreshes replace source
files while retaining `build_dir`, `staging_dir` and the shared download cache;
the ready marker is only set after compile and packaging finish. Fresh-cache
capacity uses same-device history, then other compiler trees, with an 8 GiB
first-build fallback. Other caches are removed in creation-time FIFO order
under their device flock, with each deletion logged. Legacy task trees are
adopted by in-place move only after result/manifest and symlink provenance
checks pass.

Package outputs are collected from the OpenWrt `bin` directory after the build.
Both `.apk` and `.ipk` files that are new or changed for the task are stored in
`packages.tar.gz`, preserving their paths relative to `bin`. An unchanged cache
does not produce an empty package archive. The previous `ipk-packages.tar.gz`
name remains readable for historical artifacts only.
