# PPE accounting regression inputs

The three baseline patches are unmodified ImmortalWrt v25.12.2 generic
pending-6.12 patches. They exercise the installer's compatibility checks.

`linux/` contains GPL-2.0-only function excerpts from Linux v6.12.103 after
applying the official ImmortalWrt v25.12.2 patch stack. Unrelated declarations
and functions are omitted. Tests apply the actual vendored fixes to these
excerpts, then compile the original and patched functions with hardware IO
stubs from `harness.c.in`.

The harness does not embed the algorithms under test: markers are populated
from the original excerpts and the patched result. It demonstrates unsigned
underflow, repeated expired-slot accounting, ordinary increments, and final
accounting before hardware deletion. It does not test MMIO, driver concurrency,
or real packet forwarding.
