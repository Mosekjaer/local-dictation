// SPDX-License-Identifier: MIT
//
// Shared core for the local-dictation COSMIC applets.
// See plan: `.plans/0002-cosmic-applet-equalizer.md`.

pub mod equalizer;
pub mod state;
pub mod subscriber;

pub use state::{DaemonState, Event, Side};

use std::hash::Hash;
use std::sync::Mutex;
use std::time::{Duration, Instant};

use cosmic::app::{Core, Task};
use cosmic::iced::futures::{SinkExt, channel::mpsc as iced_mpsc};
use cosmic::iced::widget::canvas;
use cosmic::iced::{self, Length, Subscription, stream};
use cosmic::widget::autosize;
use cosmic::{Element, Theme};

use crate::equalizer::EqualizerState;
use crate::subscriber::SubscriberMsg;

/// Runtime entry point for both binary crates.
///
/// Initialises logging then hands control to libcosmic's applet runtime.
pub fn run_applet(side: Side) -> cosmic::iced::Result {
    let env = env_logger::Env::default()
        .filter_or("LOCAL_DICTATION_APPLET_LOG", "info")
        .write_style_or("LOCAL_DICTATION_APPLET_LOG_STYLE", "always");
    env_logger::init_from_env(env);

    log::info!("starting local-dictation-applet ({side:?})");
    cosmic::applet::run::<Applet>(AppletFlags { side })
}

/// Flags passed into the applet at init time.
#[derive(Clone, Debug)]
pub struct AppletFlags {
    pub side: Side,
}

/// The Iced application model.
pub struct Applet {
    core: Core,
    side: Side,
    daemon_state: DaemonState,
    connected: bool,
    /// Equalizer rendering state (per-bar smoothing + animation phase).
    eq: Mutex<EqualizerState>,
    /// Last meter event we received. Used to drive the recording animation.
    last_meter: Option<MeterSample>,
    /// Wall-clock origin used to drive the animation tick (`Instant::now()`).
    start_time: Instant,
    /// Wall-clock instant the most recent error transition happened, for the
    /// red flash overlay.
    error_started_at: Option<Instant>,
}

#[derive(Clone, Copy, Debug)]
pub struct MeterSample {
    pub peak_db: f64,
    pub rms_db: f64,
}

#[derive(Clone, Debug)]
pub enum Message {
    Subscriber(SubscriberPayload),
    Tick,
}

/// Subset of `SubscriberMsg` that is `Clone` (libcosmic requires `Message: Clone`).
#[derive(Clone, Debug)]
pub enum SubscriberPayload {
    Event(Event),
    Disconnected,
    Reconnected,
}

impl From<SubscriberMsg> for SubscriberPayload {
    fn from(m: SubscriberMsg) -> Self {
        match m {
            SubscriberMsg::Event(e) => Self::Event(e),
            SubscriberMsg::Disconnected => Self::Disconnected,
            SubscriberMsg::Reconnected => Self::Reconnected,
        }
    }
}

impl cosmic::Application for Applet {
    type Executor = cosmic::SingleThreadExecutor;
    type Flags = AppletFlags;
    type Message = Message;

    // Different APP_ID per side is set by the binary entry points via the
    // panel ID-to-binary lookup; libcosmic requires a single const value here,
    // so we use a shared umbrella ID. The panel keys off the binary name on
    // $PATH, not this string.
    const APP_ID: &'static str = "io.github.local_dictation.Applet";

    fn core(&self) -> &Core {
        &self.core
    }

    fn core_mut(&mut self) -> &mut Core {
        &mut self.core
    }

    fn init(core: Core, flags: Self::Flags) -> (Self, Task<Self::Message>) {
        let model = Applet {
            core,
            side: flags.side,
            daemon_state: DaemonState::Idle,
            connected: false,
            eq: Mutex::new(EqualizerState::new()),
            last_meter: None,
            start_time: Instant::now(),
            error_started_at: None,
        };
        (model, Task::none())
    }

    fn style(&self) -> Option<iced::theme::Style> {
        Some(cosmic::applet::style())
    }

    fn subscription(&self) -> Subscription<Self::Message> {
        // 30 fps redraw tick. We always tick (cheap) so smoothing keeps decaying
        // even with no fresh events.
        let tick = iced::time::every(Duration::from_millis(33)).map(|_| Message::Tick);

        let socket = Subscription::run_with(
            SubscriberSubId(self.side),
            |_id: &SubscriberSubId| {
                stream::channel(64, |mut out: iced_mpsc::Sender<Message>| async move {
                    let (tx, mut rx) =
                        tokio::sync::mpsc::channel::<SubscriberMsg>(64);
                    let _handle = subscriber::spawn(tx);
                    while let Some(msg) = rx.recv().await {
                        let payload: SubscriberPayload = msg.into();
                        if out.send(Message::Subscriber(payload)).await.is_err() {
                            break;
                        }
                    }
                })
            },
        );

        Subscription::batch([tick, socket])
    }

    fn update(&mut self, message: Self::Message) -> Task<Self::Message> {
        match message {
            Message::Tick => {
                // Drive the per-bar EMA forward toward its target. The widget
                // also reads `start_time` for the transcribing pulse.
                if let Ok(mut eq) = self.eq.lock() {
                    eq.advance(self.daemon_state, self.last_meter, self.start_time);
                }
                // Clear the error flash after ~300 ms.
                if let Some(started) = self.error_started_at {
                    if started.elapsed() > Duration::from_millis(300) {
                        self.error_started_at = None;
                        // Transition the visible state down to Idle once the
                        // flash is done, unless the daemon told us otherwise
                        // in the meantime.
                        if matches!(self.daemon_state, DaemonState::Error) {
                            self.daemon_state = DaemonState::Idle;
                        }
                    }
                }
            }
            Message::Subscriber(SubscriberPayload::Event(ev)) => {
                self.apply_event(ev);
            }
            Message::Subscriber(SubscriberPayload::Reconnected) => {
                self.connected = true;
            }
            Message::Subscriber(SubscriberPayload::Disconnected) => {
                self.connected = false;
                // Don't override an error state; if we never heard, show idle.
                if !matches!(self.daemon_state, DaemonState::Error) {
                    self.daemon_state = DaemonState::Idle;
                }
                self.last_meter = None;
            }
        }
        Task::none()
    }

    fn view(&self) -> Element<'_, Self::Message> {
        // When the daemon is idle and no error flash is in flight, the applet
        // is hidden — return a zero-sized space so the panel reflows around it.
        let visible = !matches!(self.daemon_state, DaemonState::Idle)
            || self.error_started_at.is_some();
        if !visible {
            return autosize::autosize(
                cosmic::widget::Space::new()
                    .width(Length::Fixed(0.0))
                    .height(Length::Fixed(0.0)),
                AUTOSIZE_ID.clone(),
            )
            .into();
        }

        // The equalizer canvas is the entire applet surface. We bake the side
        // into the program so left/right can differ visually.
        let program = equalizer::EqualizerCanvas {
            // Snapshot the eq state for the canvas Program. Mutex contention
            // here is between the 30 fps tick and the draw — both on the
            // single-threaded executor, so contention is effectively nil.
            snapshot: self.eq.lock().map(|s| s.snapshot()).unwrap_or_default(),
            side: self.side,
            daemon_state: self.daemon_state,
            connected: self.connected,
            error_active: self.error_started_at.is_some(),
        };

        // Layout: 31 narrow bars (~3 px + 1 px gap) need ~120 px usable;
        // 140 px gives a small horizontal margin. Use the panel's suggested
        // size for height so we match neighbour applets like CosmicAppletTime.
        let (_, h) = self.core.applet.suggested_size(true);
        let panel_h = h.max(20) as f32;
        let canvas_widget = canvas(program)
            .width(Length::Fixed(140.0))
            .height(Length::Fixed(panel_h));

        autosize::autosize(canvas_widget, AUTOSIZE_ID.clone()).into()
    }
}

impl Applet {
    fn apply_event(&mut self, ev: Event) {
        match ev {
            Event::Hello {
                daemon_state, ..
            } => {
                self.daemon_state = DaemonState::parse(&daemon_state);
                if matches!(self.daemon_state, DaemonState::Error) {
                    self.error_started_at = Some(Instant::now());
                }
            }
            Event::State { state, .. } => {
                let new_state = DaemonState::parse(&state);
                if matches!(new_state, DaemonState::Error)
                    && !matches!(self.daemon_state, DaemonState::Error)
                {
                    self.error_started_at = Some(Instant::now());
                }
                self.daemon_state = new_state;
                if !matches!(new_state, DaemonState::Recording) {
                    self.last_meter = None;
                }
            }
            Event::Meter {
                peak_db, rms_db, ..
            } => {
                self.last_meter = Some(MeterSample { peak_db, rms_db });
            }
        }
    }
}

static AUTOSIZE_ID: std::sync::LazyLock<cosmic::widget::Id> =
    std::sync::LazyLock::new(|| cosmic::widget::Id::new("local-dictation-applet-autosize"));

// Subscription id type. Hash determines subscription identity; we key off side
// so left and right are independent subscriptions (cheap; both connect to the
// same daemon socket).
#[derive(Clone, Copy)]
struct SubscriberSubId(Side);
impl Hash for SubscriberSubId {
    fn hash<H: std::hash::Hasher>(&self, state: &mut H) {
        "local-dictation-applet-subscriber".hash(state);
        match self.0 {
            Side::Left => 0u8.hash(state),
            Side::Right => 1u8.hash(state),
        }
    }
}

// Keep `Theme` import used (some libcosmic feature combos warn otherwise).
#[allow(dead_code)]
fn _theme_marker(_t: &Theme) {}
