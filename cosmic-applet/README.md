# local-dictation COSMIC applet

Two COSMIC panel applet binaries that visualise the state of the
`local-dictation` daemon (Plan 0001) as a small 7-bar equalizer. Two distinct
IDs (`local-dictation-applet-left` and `local-dictation-applet-right`) so the
user can flank `com.system76.CosmicAppletTime` in the panel.

## libcosmic version

Pinned to `pop-os/libcosmic` rev
`564ef834cec33a948dc10c9b401cf29db5d18373`. This is the same rev that
`pop-os/cosmic-applets` `epoch-1.0.13` (released against COSMIC 1.0 stable)
locks against. Bump only after re-checking `cosmic-applets`' `Cargo.lock` for
the rev that matches the new COSMIC release.

The rev is recorded in `Cargo.lock` (committed). Cargo.toml itself does not
pin the rev because `cosmic-panel-config` pulls `cosmic-config` (a libcosmic
sub-crate) without a rev, and cargo refuses to unify two `git` sources of the
same URL when one has a rev. Pinning via the lock file is the workaround
cosmic-applets uses too.

If you want to bump the libcosmic rev, run:

```
cargo update -p libcosmic --precise <full-sha>
```

and update this section.

## Build

```
cargo build --release
```

Produces:

* `target/release/local-dictation-applet-left`
* `target/release/local-dictation-applet-right`

## Test

```
cargo test --lib
```

Doctests are skipped here because the workspace has none and the system
`rustdoc` ships a broken `libLLVM` link on this machine.

## Runtime

Each binary connects to the daemon's broadcast Unix socket at
`$XDG_RUNTIME_DIR/local-dictation-events.sock` (newline-delimited JSON). The
subscriber reconnects on EOF / refused with capped exponential backoff
(200 ms → 5 s).

These are panel applets — `cosmic::applet::run` requires a COSMIC panel
context to bring up a Wayland surface. There is no `--standalone` mode in
this version; visual testing happens via the panel during Phase 4 of the
plan.

## Install

Use the repo-root `install.sh --with-applet` (Phase 4, not in this crate).
