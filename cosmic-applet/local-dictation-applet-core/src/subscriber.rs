// SPDX-License-Identifier: MIT
//
// Async subscriber: connects to the daemon's broadcast socket and forwards
// decoded events to the Iced runtime via an mpsc channel.
//
// Contract from `local_dictation/events.py` (verbatim from plan 0002):
//   * Socket path: $XDG_RUNTIME_DIR/local-dictation-events.sock
//   * Permissions: 0600 (enforced by the daemon).
//   * Protocol: connect, then read newline-delimited UTF-8 JSON forever.
//   * On connect, server sends one `hello` event within ~100 ms.
//   * Server may disconnect a slow subscriber without warning (EOF).
//
// Reconnect policy: capped exponential backoff
// 200 ms -> 400 ms -> 800 ms -> 1600 ms -> 3200 ms, then cap at 5 s.
// Reset on every successful connect.

use std::path::PathBuf;
use std::time::Duration;

use tokio::io::{AsyncBufReadExt, BufReader};
use tokio::net::UnixStream;
use tokio::sync::mpsc::Sender;
use tokio::task::JoinHandle;

use crate::state::Event;

/// Messages the subscriber task pushes into the Iced runtime.
#[derive(Debug)]
pub enum SubscriberMsg {
    /// One successfully decoded event from the daemon.
    Event(Event),
    /// We just lost (or failed to obtain) the connection.
    /// Emitted at most once per disconnect.
    Disconnected,
    /// We just established (or re-established) the connection.
    Reconnected,
}

/// Resolve the broadcast socket path:
/// `$XDG_RUNTIME_DIR/local-dictation-events.sock`, with fallback
/// to `/run/user/<uid>/local-dictation-events.sock` when the env var is unset.
pub fn socket_path() -> PathBuf {
    let dir = std::env::var_os("XDG_RUNTIME_DIR")
        .map(PathBuf::from)
        .unwrap_or_else(|| {
            // SAFETY: getuid is always safe; only this thread, no signal handler.
            let uid = unsafe { libc_getuid() };
            PathBuf::from(format!("/run/user/{uid}"))
        });
    dir.join("local-dictation-events.sock")
}

// Tiny shim to avoid pulling in the full `libc` crate. The applet otherwise
// has no need for it.
extern "C" {
    fn getuid() -> u32;
}
#[inline]
unsafe fn libc_getuid() -> u32 {
    getuid()
}

/// Spawn the subscriber task on the current tokio runtime.
///
/// The task loops forever, reconnecting on every error / EOF.
/// Drop the receiver of `out` (or use a runtime shutdown) to stop it.
pub fn spawn(out: Sender<SubscriberMsg>) -> JoinHandle<()> {
    tokio::spawn(async move {
        run(out).await;
    })
}

async fn run(out: Sender<SubscriberMsg>) {
    // Start in a "disconnected" notional state — first emission of
    // `Disconnected` happens lazily after the first connect failure so the UI
    // doesn't briefly flash "no daemon" when the daemon is in fact up.
    let mut backoff = BackoffIter::new();
    // Tracks whether we have already emitted Disconnected since the last
    // successful connect, so we don't spam the model on every retry.
    let mut announced_disconnect = false;
    let path = socket_path();

    loop {
        match UnixStream::connect(&path).await {
            Ok(stream) => {
                backoff.reset();
                if out.send(SubscriberMsg::Reconnected).await.is_err() {
                    return;
                }
                // Reset the latch so the next disconnect is announced. The
                // value is read by the Err branch below on subsequent retries.
                #[allow(unused_assignments)]
                {
                    announced_disconnect = false;
                }

                // Read newline-delimited JSON until EOF / IO error.
                let reader = BufReader::new(stream);
                let mut lines = reader.lines();
                loop {
                    match lines.next_line().await {
                        Ok(Some(line)) => {
                            let trimmed = line.trim();
                            if trimmed.is_empty() {
                                continue;
                            }
                            match serde_json::from_str::<Event>(trimmed) {
                                Ok(ev) => {
                                    if out.send(SubscriberMsg::Event(ev)).await.is_err() {
                                        return; // receiver gone, shut down.
                                    }
                                }
                                Err(err) => {
                                    log::warn!(
                                        "skipping malformed event line: {err} ({trimmed:?})"
                                    );
                                }
                            }
                        }
                        Ok(None) => {
                            log::debug!("daemon closed connection (EOF)");
                            break;
                        }
                        Err(err) => {
                            log::debug!("read error on daemon socket: {err}");
                            break;
                        }
                    }
                }

                // Disconnected — announce once, then back off and retry.
                if out.send(SubscriberMsg::Disconnected).await.is_err() {
                    return;
                }
                announced_disconnect = true;
                tokio::time::sleep(backoff.next()).await;
            }
            Err(err) => {
                log::debug!("connect to {} failed: {err}", path.display());
                if !announced_disconnect {
                    if out.send(SubscriberMsg::Disconnected).await.is_err() {
                        return;
                    }
                    announced_disconnect = true;
                }
                tokio::time::sleep(backoff.next()).await;
            }
        }
    }
}

/// Capped exponential backoff: 200 ms, 400 ms, 800 ms, 1.6 s, 3.2 s, then 5 s.
pub struct BackoffIter {
    next_ms: u64,
}

impl BackoffIter {
    pub fn new() -> Self {
        Self { next_ms: 200 }
    }

    pub fn reset(&mut self) {
        self.next_ms = 200;
    }

    pub fn next(&mut self) -> Duration {
        let now = self.next_ms;
        // Double, then clamp at 5000 ms.
        self.next_ms = (self.next_ms * 2).min(5000);
        Duration::from_millis(now)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn backoff_progression_caps_at_5s() {
        let mut b = BackoffIter::new();
        let seq: Vec<u64> = (0..7).map(|_| b.next().as_millis() as u64).collect();
        // 200, 400, 800, 1600, 3200, 5000, 5000
        assert_eq!(seq, vec![200, 400, 800, 1600, 3200, 5000, 5000]);
    }

    #[test]
    fn backoff_reset_returns_to_initial() {
        let mut b = BackoffIter::new();
        for _ in 0..5 {
            let _ = b.next();
        }
        b.reset();
        assert_eq!(b.next().as_millis() as u64, 200);
    }

    #[test]
    fn socket_path_uses_runtime_dir() {
        // SAFETY: tests are single-threaded by default in cargo; we only mutate
        // an env var local to this process. If you parallelise tests, isolate
        // this one with --test-threads=1 or use a guard.
        let saved = std::env::var_os("XDG_RUNTIME_DIR");
        // SAFETY: same-thread mutation only.
        unsafe { std::env::set_var("XDG_RUNTIME_DIR", "/tmp/test-runtime") };
        let p = socket_path();
        assert_eq!(p, PathBuf::from("/tmp/test-runtime/local-dictation-events.sock"));
        // restore
        match saved {
            Some(v) => unsafe { std::env::set_var("XDG_RUNTIME_DIR", v) },
            None => unsafe { std::env::remove_var("XDG_RUNTIME_DIR") },
        }
    }
}
