# MediaTek PPE accounting regression inputs

`linux-6.18/` contains GPL-2.0-only function excerpts from Linux v6.18.60 after
applying the official ImmortalWrt `4765b657625e` patch stack. Tests apply the
actual patches from `fix_bugs/mediatek-6.18/` to a copy, then compile the
original and patched functions with hardware IO stubs from `harness.c.in`.

The harness demonstrates unsigned underflow, repeated expired-slot
accounting, ordinary increments, and final accounting before hardware
deletion. It does not test MMIO, driver concurrency, or real packet forwarding.
