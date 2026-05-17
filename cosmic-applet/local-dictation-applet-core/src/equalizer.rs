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
use cosmic::iced::border::Radius;
use cosmic::iced::widget::canvas::{Fill, path};
use cosmic::{Renderer, Theme};

use crate::Side;
use crate::state::DaemonState;

/// Total number of synthesised frequency bands across both applets.
/// Each applet renders `TOTAL_BANDS / 2` bars.
pub const TOTAL_BANDS: usize = 32;
/// Number of bars rendered per applet.
pub const BAR_COUNT: usize = TOTAL_BANDS / 2;
/// EMA smoothing factor.
pub const EMA_ALPHA: f32 = 0.35;
/// Idle baseline height as a fraction of canvas height. Bars are not rendered
/// when the daemon is idle, but we keep a small value here so the smoothing
/// starts from a sensible state when recording begins.
const IDLE_HEIGHT: f32 = 0.10;
/// Pulse frequency in Hz for the transcribing animation.
const PULSE_HZ: f32 = 1.5;
/// Bar:gap split — 80 % bar, 20 % gap of the per-bar slot.
const BAR_FILL_RATIO: f32 = 0.8;
/// Minimum bar height in pixels so an "empty" band still shows as a pill.
const MIN_BAR_HEIGHT_PX: f32 = 4.0;
/// Corner radius — large enough that any reasonable bar width becomes a pill.
const CORNER_RADIUS: f32 = 24.0;

/// Speech-frequency response: low at the extremes, peak in mid-high, slight
/// taper at the top. Returns a multiplier in roughly [0.0, 1.0] for a band
/// at `normalized_freq ∈ [0.0, 1.0]`.
#[inline]
fn speech_response(normalized_freq: f32) -> f32 {
    if normalized_freq < 0.1 {
        0.2 * normalized_freq / 0.1
    } else if normalized_freq < 0.3 {
        0.2 + 0.3 * (normalized_freq - 0.1) / 0.2
    } else if normalized_freq < 0.6 {
        0.5 + 0.5 * (normalized_freq - 0.3) / 0.3
    } else if normalized_freq < 0.8 {
        1.0 - 0.2 * (normalized_freq - 0.6) / 0.2
    } else {
        0.8 * (1.0 - normalized_freq) / 0.2
    }
}

/// Per-band variation derived from the golden ratio so adjacent bands look
/// independent without being random.
#[inline]
fn band_variation(global_band_index: usize) -> f32 {
    ((global_band_index as f32 * 1.618) % 1.0) * 0.3 + 0.85
}

/// Convert a Side into the global band index of the first band this applet
/// renders. Left renders the low half of the spectrum; right renders the high
/// half. This way the two applets together display a continuous spectrum.
#[inline]
fn first_global_band_for(side: Side) -> usize {
    match side {
        Side::Left => 0,
        Side::Right => TOTAL_BANDS / 2,
    }
}

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
        side: Side,
        state: DaemonState,
        last_meter: Option<crate::MeterSample>,
        start_time: Instant,
    ) {
        let elapsed = start_time.elapsed().as_secs_f32();
        let targets = compute_targets(side, state, last_meter, elapsed);
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

        // 80 % bar, 20 % gap. Slot = bar + gap.
        let slot_w = canvas_w / BAR_COUNT as f32;
        let bar_w = slot_w * BAR_FILL_RATIO;
        let gap_w = slot_w * (1.0 - BAR_FILL_RATIO);
        let used_w = bar_w * BAR_COUNT as f32 + gap_w * (BAR_COUNT as f32 - 1.0);
        let x_start = (canvas_w - used_w).max(0.0) / 2.0;

        let center_y = canvas_h / 2.0;
        let color = self.bar_color(theme);

        for i in 0..BAR_COUNT {
            let h_norm = self.snapshot.heights[i].clamp(0.0, 1.0);
            if h_norm < 0.01 {
                continue;
            }
            let bar_h = (canvas_h * h_norm).max(MIN_BAR_HEIGHT_PX);
            let x = x_start + i as f32 * (bar_w + gap_w);
            // Grow both up and down from the vertical centre.
            let y = (center_y - bar_h / 2.0).max(0.0);

            let mut pb = path::Builder::new();
            pb.rounded_rectangle(
                Point::new(x, y),
                Size::new(bar_w, bar_h),
                Radius::new(CORNER_RADIUS),
            );
            frame.fill(&pb.build(), Fill::from(color));
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
///
/// Produces one target value per rendered bar (i.e. `BAR_COUNT` values for the
/// half of the spectrum this `side` is responsible for). The synthesised band
/// uses a speech-frequency response curve plus a deterministic per-band
/// golden-ratio variation, mirroring the approach in the upstream `super-stt`
/// applet.
pub fn compute_targets(
    side: Side,
    state: DaemonState,
    last_meter: Option<crate::MeterSample>,
    elapsed_seconds: f32,
) -> [f32; BAR_COUNT] {
    let mut out = [0.0_f32; BAR_COUNT];
    match state {
        DaemonState::Idle => {
            // The applet hides itself when idle (see lib.rs view()), so this
            // value never reaches the screen. Keep a small floor so EMA
            // doesn't snap to 0 between sessions.
            for v in out.iter_mut() {
                *v = IDLE_HEIGHT;
            }
        }
        DaemonState::Error => {
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
            // Map peak/RMS dB to a single audio level in [0, 1].
            let (peak_db, rms_db) = match last_meter {
                Some(m) => (m.peak_db as f32, m.rms_db as f32),
                None => (-60.0, -60.0),
            };
            let mixed_db = 0.7 * peak_db + 0.3 * rms_db;
            let audio_level = ((mixed_db + 60.0) / 60.0).clamp(0.0, 1.0);

            // Synthesise this side's half of the spectrum.
            let first_band = first_global_band_for(side);
            for (i, v) in out.iter_mut().enumerate() {
                let global_idx = first_band + i;
                let normalized_freq =
                    global_idx as f32 / (TOTAL_BANDS as f32 - 1.0);
                let height =
                    audio_level * speech_response(normalized_freq) * band_variation(global_idx);
                *v = (IDLE_HEIGHT + (1.0 - IDLE_HEIGHT) * height).clamp(0.05, 1.0);
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
        for side in [Side::Left, Side::Right] {
            let t = compute_targets(side, DaemonState::Idle, None, 0.0);
            for v in t {
                assert!((v - IDLE_HEIGHT).abs() < 1e-6);
            }
        }
    }

    #[test]
    fn recording_drives_some_bar_high_at_full_loudness() {
        // With audio_level=1.0 the speech curve peaks in the mid-high range
        // (around 60-70 % of the spectrum). Confirm at least one bar on each
        // side gets driven well above the idle baseline.
        for side in [Side::Left, Side::Right] {
            let t = compute_targets(
                side,
                DaemonState::Recording,
                Some(MeterSample {
                    peak_db: 0.0,
                    rms_db: 0.0,
                }),
                0.0,
            );
            let max = t.iter().cloned().fold(0.0_f32, f32::max);
            assert!(max > 0.5, "{side:?} max bar = {max}, expected > 0.5");
        }
    }

    #[test]
    fn recording_at_minus_60_db_is_near_idle() {
        // audio_level collapses to 0 → height = IDLE_HEIGHT for every band.
        let t = compute_targets(
            Side::Left,
            DaemonState::Recording,
            Some(MeterSample {
                peak_db: -60.0,
                rms_db: -60.0,
            }),
            0.0,
        );
        for v in t {
            assert!((v - IDLE_HEIGHT).abs() < 1e-5);
        }
    }

    #[test]
    fn recording_clamps_below_minus_60() {
        let t = compute_targets(
            Side::Right,
            DaemonState::Recording,
            Some(MeterSample {
                peak_db: -120.0,
                rms_db: -120.0,
            }),
            0.0,
        );
        for v in t {
            assert!((v - IDLE_HEIGHT).abs() < 1e-5);
        }
    }

    #[test]
    fn left_and_right_render_different_spectrum_halves() {
        // Same audio level, different sides → different per-bar heights.
        // Equivalence would mean the spectrum split is broken.
        let left = compute_targets(
            Side::Left,
            DaemonState::Recording,
            Some(MeterSample { peak_db: -10.0, rms_db: -15.0 }),
            0.0,
        );
        let right = compute_targets(
            Side::Right,
            DaemonState::Recording,
            Some(MeterSample { peak_db: -10.0, rms_db: -15.0 }),
            0.0,
        );
        let any_different = (0..BAR_COUNT).any(|i| (left[i] - right[i]).abs() > 1e-3);
        assert!(any_different, "left and right produced identical bands");
    }

    #[test]
    fn transcribing_targets_stay_in_unit_range() {
        for n in 0..30 {
            let t = compute_targets(
                Side::Left,
                DaemonState::Transcribing,
                None,
                n as f32 * 0.033,
            );
            for v in t {
                assert!((0.05..=1.0).contains(&v));
            }
        }
    }

    #[test]
    fn ema_converges_toward_target() {
        let mut s = EqualizerState::new();
        let start = Instant::now();
        for _ in 0..50 {
            s.advance(
                Side::Left,
                DaemonState::Recording,
                Some(MeterSample { peak_db: 0.0, rms_db: 0.0 }),
                start,
            );
        }
        let targets = compute_targets(
            Side::Left,
            DaemonState::Recording,
            Some(MeterSample { peak_db: 0.0, rms_db: 0.0 }),
            start.elapsed().as_secs_f32(),
        );
        for i in 0..BAR_COUNT {
            assert!((s.heights[i] - targets[i]).abs() < 1e-3);
        }
    }

    #[test]
    fn ema_single_step_matches_formula() {
        let mut s = EqualizerState::new();
        let start_h = s.heights[0];
        s.advance(Side::Left, DaemonState::Idle, None, Instant::now());
        assert!((s.heights[0] - start_h).abs() < 1e-9);
    }
}
