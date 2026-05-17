// SPDX-License-Identifier: MIT
//
// Shared types for the local-dictation COSMIC applet.

use serde::Deserialize;

/// Which of the two flanking applet instances we are. Only the binary's
/// thin `main.rs` decides this — everything else is shared.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Side {
    Left,
    Right,
}

/// Daemon state mirror. Driven by `Event::Hello` and `Event::State`.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum DaemonState {
    Idle,
    Recording,
    Transcribing,
    Error,
}

impl DaemonState {
    /// Parse the lowercase JSON string variant. Unknown values fall back to Idle.
    pub fn parse(s: &str) -> Self {
        match s {
            "idle" => Self::Idle,
            "recording" => Self::Recording,
            "transcribing" => Self::Transcribing,
            "error" => Self::Error,
            _ => Self::Idle,
        }
    }
}

/// One newline-delimited JSON event from the daemon broadcast socket.
///
/// Wire format (from `local_dictation/events.py`):
/// - `{"type":"hello","version":1,"daemon_state":"...","ts":<f64>}`
/// - `{"type":"state","state":"...","ts":<f64>}`
/// - `{"type":"meter","peak_db":<f64>,"rms_db":<f64>,"ts":<f64>}`
/// - `{"type":"transcript_partial","committed":"...","tentative":"...","ts":<f64>}`
/// - `{"type":"transcript_final","text":"...","ts":<f64>}`
///
/// The transcript_* variants are only emitted during a `--live` session.
/// The applet does not currently render them but accepts them so the
/// subscriber loop does not log warnings.
#[derive(Clone, Debug, Deserialize)]
#[serde(tag = "type", rename_all = "snake_case")]
pub enum Event {
    Hello {
        version: u32,
        daemon_state: String,
        ts: f64,
    },
    State {
        state: String,
        ts: f64,
    },
    Meter {
        peak_db: f64,
        rms_db: f64,
        ts: f64,
    },
    TranscriptPartial {
        committed: String,
        tentative: String,
        ts: f64,
    },
    TranscriptFinal {
        text: String,
        ts: f64,
    },
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn decodes_hello() {
        let json = r#"{"type":"hello","version":1,"daemon_state":"idle","ts":1.5}"#;
        let ev: Event = serde_json::from_str(json).unwrap();
        match ev {
            Event::Hello {
                version,
                daemon_state,
                ts,
            } => {
                assert_eq!(version, 1);
                assert_eq!(daemon_state, "idle");
                assert_eq!(ts, 1.5);
            }
            other => panic!("expected Hello, got {other:?}"),
        }
    }

    #[test]
    fn decodes_state_recording() {
        let json = r#"{"type":"state","state":"recording","ts":1700000000.25}"#;
        let ev: Event = serde_json::from_str(json).unwrap();
        match ev {
            Event::State { state, ts } => {
                assert_eq!(state, "recording");
                assert_eq!(ts, 1700000000.25);
            }
            other => panic!("expected State, got {other:?}"),
        }
    }

    #[test]
    fn decodes_meter_clamped_minus120() {
        let json = r#"{"type":"meter","peak_db":-120.0,"rms_db":-120.0,"ts":42.0}"#;
        let ev: Event = serde_json::from_str(json).unwrap();
        match ev {
            Event::Meter {
                peak_db, rms_db, ..
            } => {
                assert_eq!(peak_db, -120.0);
                assert_eq!(rms_db, -120.0);
            }
            other => panic!("expected Meter, got {other:?}"),
        }
    }

    #[test]
    fn decodes_meter_normal_levels() {
        let json = r#"{"type":"meter","peak_db":-6.5,"rms_db":-18.0,"ts":7.0}"#;
        let ev: Event = serde_json::from_str(json).unwrap();
        match ev {
            Event::Meter {
                peak_db, rms_db, ..
            } => {
                assert!((peak_db - -6.5).abs() < 1e-9);
                assert!((rms_db - -18.0).abs() < 1e-9);
            }
            other => panic!("expected Meter, got {other:?}"),
        }
    }

    #[test]
    fn rejects_unknown_type() {
        let json = r#"{"type":"garbage","ts":0.0}"#;
        let res: Result<Event, _> = serde_json::from_str(json);
        assert!(res.is_err());
    }

    #[test]
    fn decodes_transcript_partial() {
        let json = r#"{"type":"transcript_partial","committed":"hello","tentative":"world","ts":1.0}"#;
        let ev: Event = serde_json::from_str(json).unwrap();
        match ev {
            Event::TranscriptPartial { committed, tentative, ts } => {
                assert_eq!(committed, "hello");
                assert_eq!(tentative, "world");
                assert_eq!(ts, 1.0);
            }
            other => panic!("expected TranscriptPartial, got {other:?}"),
        }
    }

    #[test]
    fn decodes_transcript_final() {
        let json = r#"{"type":"transcript_final","text":"hello world","ts":2.0}"#;
        let ev: Event = serde_json::from_str(json).unwrap();
        match ev {
            Event::TranscriptFinal { text, ts } => {
                assert_eq!(text, "hello world");
                assert_eq!(ts, 2.0);
            }
            other => panic!("expected TranscriptFinal, got {other:?}"),
        }
    }

    #[test]
    fn parses_daemon_state_variants() {
        assert_eq!(DaemonState::parse("idle"), DaemonState::Idle);
        assert_eq!(DaemonState::parse("recording"), DaemonState::Recording);
        assert_eq!(
            DaemonState::parse("transcribing"),
            DaemonState::Transcribing
        );
        assert_eq!(DaemonState::parse("error"), DaemonState::Error);
        assert_eq!(DaemonState::parse("???"), DaemonState::Idle);
    }
}
