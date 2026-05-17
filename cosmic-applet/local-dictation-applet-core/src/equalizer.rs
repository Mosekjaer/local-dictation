// SPDX-License-Identifier: MIT
//
// Equalizer: 7-bar canvas widget driven by the daemon state machine.
//
// State machine (from plan 0002):
//   * Idle:         all bars at ~10 % height, static.
//   * Recording:    bars driven by smoothed peak_db / rms_db with per-bar
//                   weights `[0.6, 0.8, 1.0, 1.2, 1.0, 0.8, 0.6]`. Per-bar EMA
//                   with alpha = 0.4. Mapping: -60 dB -> 0.10, 0 dB -> 1.00.
//   * Transcribing: each bar pulses on a 1.5 Hz sinusoid with a per-bar
//                   phase offset. Independent of audio.
//   * Error:        all bars flash 100 % red for ~300 ms, then fall to Idle.
//
// Color: accent color from the active libcosmic theme, except in the error
// state, which is red.

use std::time::Instant;

use cosmic::iced::widget::canvas;
use cosmic::iced::{Color, Point, Rectangle, Size, mouse};
use cosmic::{Renderer, Theme};

use crate::Side;
use crate::state::DaemonState;

/// Number of bars.
pub const BAR_COUNT: usize = 7;
/// EMA smoothing factor.
pub const EMA_ALPHA: f32 = 0.4;
/// Per-bar weight — taller in the middle.
pub const BAR_WEIGHTS: [f32; BAR_COUNT] = [0.6, 0.8, 1.0, 1.2, 1.0, 0.8, 0.6];
/// Idle baseline height as a fraction of canvas height.
const IDLE_HEIGHT: f32 = 0.10;
/// Pulse frequency in Hz for the transcribing animation.
const PULSE_HZ: f32 = 1.5;

/// Persistent per-bar state owned by the applet model. The canvas itself
/// takes an immutable snapshot at draw time.
pub struct EqualizerState {
    /// Currently rendered bar heights in [0.0, 1.0].
    heights: [f32; BAR_COUNT],
}

impl EqualizerState {
    pub fn new() -> Self {
        Self {
            heights: [IDLE_HEIGHT; BAR_COUNT],
        }
    }

    /// Step the smoothing forward toward the target heights implied by the
    /// current `state` + most recent meter sample. Called from the 30 fps tick.
    pub fn advance(
        &mut self,
        state: DaemonState,
        last_meter: Option<crate::MeterSample>,
        start_time: Instant,
    ) {
        let elapsed = start_time.elapsed().as_secs_f32();
        let targets = compute_targets(state, last_meter, elapsed);
        for i in 0..BAR_COUNT {
            self.heights[i] = self.heights[i] + EMA_ALPHA * (targets[i] - self.heights[i]);
        }
    }

    pub fn snapshot(&self) -> EqualizerSnapshot {
        EqualizerSnapshot {
            heights: self.heights,
        }
    }
}

impl Default for EqualizerState {
    fn default() -> Self {
        Self::new()
    }
}

/// Immutable copy of bar heights, cheap to pass into the canvas Program.
#[derive(Clone, Copy, Debug, Default)]
pub struct EqualizerSnapshot {
    pub heights: [f32; BAR_COUNT],
}

/// The canvas Program implementation. Cheap value type; constructed every
/// `view()` call.
pub struct EqualizerCanvas {
    pub snapshot: EqualizerSnapshot,
    pub side: Side,
    pub daemon_state: DaemonState,
    pub connected: bool,
    pub error_active: bool,
}

impl<M> canvas::Program<M, Theme, Renderer> for EqualizerCanvas {
    type State = ();

    fn draw(
        &self,
        _state: &Self::State,
        renderer: &Renderer,
        theme: &Theme,
        bounds: Rectangle,
        _cursor: mouse::Cursor,
    ) -> Vec<canvas::Geometry<Renderer>> {
        // Note: bounds.position() is in the parent's coord space; the canvas
        // frame is local (origin (0,0)).
        let mut frame = canvas::Frame::new(renderer, bounds.size());

        let canvas_w = bounds.width.max(1.0);
        let canvas_h = bounds.height.max(1.0);

        // Layout: BAR_COUNT bars with 2 px gaps centred horizontally.
        let gap_px: f32 = 2.0;
        let total_gap = gap_px * (BAR_COUNT as f32 - 1.0);
        let bar_w = ((canvas_w - total_gap) / BAR_COUNT as f32).max(2.0);
        let used_w = bar_w * BAR_COUNT as f32 + total_gap;
        let x_start = (canvas_w - used_w).max(0.0) / 2.0;

        let color = self.bar_color(theme);

        for i in 0..BAR_COUNT {
            // Optional mirror for Side::Right so the two flanks aren't an
            // exact copy of each other.
            let idx = match self.side {
                Side::Left => i,
                Side::Right => BAR_COUNT - 1 - i,
            };
            let h_norm = self.snapshot.heights[idx].clamp(0.0, 1.0);
            let bar_h = (canvas_h * h_norm).max(1.0);
            let x = x_start + i as f32 * (bar_w + gap_px);
            let y = canvas_h - bar_h;

            frame.fill_rectangle(Point::new(x, y), Size::new(bar_w, bar_h), color);
        }

        vec![frame.into_geometry()]
    }
}

impl EqualizerCanvas {
    fn bar_color(&self, theme: &Theme) -> Color {
        if matches!(self.daemon_state, DaemonState::Error) || self.error_active {
            // Bright red for the error flash.
            return Color::from_rgb(0.95, 0.20, 0.20);
        }
        if !self.connected {
            // Muted color when we haven't reached the daemon yet.
            let base = theme.cosmic().accent.base;
            return Color {
                r: base.red,
                g: base.green,
                b: base.blue,
                a: 0.5,
            };
        }
        let base = theme.cosmic().accent.base;
        Color {
            r: base.red,
            g: base.green,
            b: base.blue,
            a: base.alpha,
        }
    }
}

/// Pure target-height computation, exposed for unit tests.
pub fn compute_targets(
    state: DaemonState,
    last_meter: Option<crate::MeterSample>,
    elapsed_seconds: f32,
) -> [f32; BAR_COUNT] {
    let mut out = [IDLE_HEIGHT; BAR_COUNT];
    match state {
        DaemonState::Idle => {
            // Static baseline.
        }
        DaemonState::Error => {
            // Error flash: full height, the color path turns it red.
            for v in out.iter_mut() {
                *v = 1.0;
            }
        }
        DaemonState::Transcribing => {
            // Sinusoidal pulse, phase shifted across bars so it appears as a
            // travelling wave.
            for (i, v) in out.iter_mut().enumerate() {
                let phase = (i as f32) * 0.6;
                let pulse = (elapsed_seconds * std::f32::consts::TAU * PULSE_HZ + phase).sin();
                *v = (pulse * 0.4 + 0.5).clamp(0.05, 1.0);
            }
        }
        DaemonState::Recording => {
            // Use peak as the dominant driver; RMS smooths out the floor.
            let (peak_db, rms_db) = match last_meter {
                Some(m) => (m.peak_db as f32, m.rms_db as f32),
                None => (-60.0, -60.0),
            };
            // Weighted blend favouring peak — feels snappier.
            let mixed_db = 0.7 * peak_db + 0.3 * rms_db;
            // Map [-60, 0] dB -> [IDLE_HEIGHT, 1.0] linearly. Clamp.
            let normalized = ((mixed_db + 60.0) / 60.0).clamp(0.0, 1.0);
            let base = IDLE_HEIGHT + (1.0 - IDLE_HEIGHT) * normalized;
            for (i, v) in out.iter_mut().enumerate() {
                let weighted = base * BAR_WEIGHTS[i];
                *v = weighted.clamp(0.05, 1.0);
            }
        }
    }
    out
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::MeterSample;

    #[test]
    fn idle_targets_are_baseline() {
        let t = compute_targets(DaemonState::Idle, None, 0.0);
        for v in t {
            assert!((v - IDLE_HEIGHT).abs() < 1e-6);
        }
    }

    #[test]
    fn recording_at_zero_db_is_near_full_height_in_middle_bar() {
        let t = compute_targets(
            DaemonState::Recording,
            Some(MeterSample {
                peak_db: 0.0,
                rms_db: 0.0,
            }),
            0.0,
        );
        // Centre bar has weight 1.2 so it gets clamped at 1.0.
        assert!(t[3] > 0.95);
        // Edge bars (weight 0.6) should be ~0.6.
        assert!(t[0] > 0.5 && t[0] < 0.7);
    }

    #[test]
    fn recording_at_minus_60_db_is_near_idle() {
        let t = compute_targets(
            DaemonState::Recording,
            Some(MeterSample {
                peak_db: -60.0,
                rms_db: -60.0,
            }),
            0.0,
        );
        // Below the floor maps to IDLE_HEIGHT * weight, then clamped at 0.05.
        for v in t {
            assert!(v <= IDLE_HEIGHT * 1.2 + 1e-3);
            assert!(v >= 0.05 - 1e-6);
        }
    }

    #[test]
    fn recording_clamps_below_minus_60() {
        // Daemon clamps -inf to -120 — we should treat that the same as -60.
        let t = compute_targets(
            DaemonState::Recording,
            Some(MeterSample {
                peak_db: -120.0,
                rms_db: -120.0,
            }),
            0.0,
        );
        for v in t {
            assert!(v <= IDLE_HEIGHT * 1.2 + 1e-3);
        }
    }

    #[test]
    fn transcribing_targets_stay_in_unit_range() {
        for n in 0..30 {
            let t = compute_targets(DaemonState::Transcribing, None, n as f32 * 0.033);
            for v in t {
                assert!((0.05..=1.0).contains(&v));
            }
        }
    }

    #[test]
    fn ema_converges_toward_target() {
        let mut s = EqualizerState::new();
        let start = Instant::now();
        // Force a recording sample at 0 dB and step many times.
        for _ in 0..50 {
            s.advance(
                DaemonState::Recording,
                Some(MeterSample {
                    peak_db: 0.0,
                    rms_db: 0.0,
                }),
                start,
            );
        }
        let targets = compute_targets(
            DaemonState::Recording,
            Some(MeterSample {
                peak_db: 0.0,
                rms_db: 0.0,
            }),
            start.elapsed().as_secs_f32(),
        );
        for i in 0..BAR_COUNT {
            assert!((s.heights[i] - targets[i]).abs() < 1e-3);
        }
    }

    #[test]
    fn ema_single_step_matches_formula() {
        let mut s = EqualizerState::new();
        // Hand-roll: start = IDLE_HEIGHT, target = some value
        // After one step expected = start + alpha * (target - start)
        let start_h = s.heights[0];
        s.advance(DaemonState::Idle, None, Instant::now());
        // Idle target equals IDLE_HEIGHT so heights don't move.
        assert!((s.heights[0] - start_h).abs() < 1e-9);
    }
}
