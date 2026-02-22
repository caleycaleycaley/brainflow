use std::fs;
use std::path::PathBuf;
use std::time::Duration;

use brainflow::board_shim::{
    self, get_exg_channels, get_sampling_rate,
    get_timestamp_channel, BoardShim,
};
use brainflow::brainflow_input_params::BrainFlowInputParamsBuilder;
use brainflow::data_filter::{perform_bandpass, perform_rolling_filter};
use brainflow::{AggOperations, BoardIds, BrainFlowPresets, FilterTypes, IpProtocolTypes};
use eframe::egui;
use egui_plot::{Line, Plot, PlotBounds, PlotPoints, VLine};
use num::FromPrimitive;
use serde::{Deserialize, Serialize};

const MAX_PULL_PER_TICK: usize = 4096;
const SETTINGS_VERSION: u32 = 1;

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum AppMode {
    Minimal,
    Enhanced,
}

#[derive(Debug, Clone)]
struct GuiConfig {
    board_id: i32,
    master_board: i32,
    ip_address: String,
    ip_port: usize,
    ip_protocol: IpProtocolTypes,
    timeout: usize,
    x_sec: f64,
    x_sec_from_cli: bool,
    channels: Vec<usize>,
    channels_from_cli: bool,
    buffer_size: usize,
}

impl Default for GuiConfig {
    fn default() -> Self {
        Self {
            board_id: 66,
            master_board: 51,
            ip_address: "localhost".to_string(),
            ip_port: 3390,
            ip_protocol: IpProtocolTypes::Edx,
            timeout: 15,
            x_sec: 4.0,
            x_sec_from_cli: false,
            channels: vec![1, 2, 3, 4],
            channels_from_cli: false,
            buffer_size: 45000,
        }
    }
}

#[derive(Debug, Clone)]
struct ViewState {
    x_sec: f64,
    y_range_v: f64,
    dc_correction_enabled: bool,
    bandpass_enabled: bool,
    sweep_pointer_enabled: bool,
    line_color: egui::Color32,
    background_color: egui::Color32,
    last_error: Option<String>,
}

impl Default for ViewState {
    fn default() -> Self {
        Self {
            x_sec: 4.0,
            y_range_v: 0.15,
            dc_correction_enabled: false,
            bandpass_enabled: false,
            sweep_pointer_enabled: false,
            line_color: egui::Color32::from_rgb(80, 220, 160),
            background_color: egui::Color32::from_rgb(12, 16, 22),
            last_error: None,
        }
    }
}

#[derive(Debug, Clone, Serialize, Deserialize)]
struct PersistedViewSettings {
    version: u32,
    x_sec: f64,
    y_range_v: f64,
    dc_correction_enabled: bool,
    bandpass_enabled: bool,
    sweep_pointer_enabled: bool,
    line_color_rgba: [u8; 4],
    background_color_rgba: [u8; 4],
}

impl PersistedViewSettings {
    fn from_view(view: &ViewState) -> Self {
        Self {
            version: SETTINGS_VERSION,
            x_sec: view.x_sec,
            y_range_v: view.y_range_v,
            dc_correction_enabled: view.dc_correction_enabled,
            bandpass_enabled: view.bandpass_enabled,
            sweep_pointer_enabled: view.sweep_pointer_enabled,
            line_color_rgba: view.line_color.to_array(),
            background_color_rgba: view.background_color.to_array(),
        }
    }
}

struct ConnectionState {
    board: Option<BoardShim>,
    connected: bool,
    effective_board: Option<BoardIds>,
    sampling_rate: usize,
    timestamp_channel: Option<usize>,
    channel_rows: Vec<usize>,
    channel_labels: Vec<String>,
}

impl Default for ConnectionState {
    fn default() -> Self {
        Self {
            board: None,
            connected: false,
            effective_board: None,
            sampling_rate: 1,
            timestamp_channel: None,
            channel_rows: Vec::new(),
            channel_labels: Vec::new(),
        }
    }
}

struct BufferState {
    ring_points: usize,
    write_idx: usize,
    history: Vec<Vec<f64>>,
    pointer: Vec<Vec<f64>>,
    pointer_points: usize,
}

impl Default for BufferState {
    fn default() -> Self {
        Self {
            ring_points: 1,
            write_idx: 0,
            history: Vec::new(),
            pointer: Vec::new(),
            pointer_points: 1,
        }
    }
}

impl BufferState {
    fn reset_for_channels(&mut self, n_channels: usize, srate: usize, x_sec: f64) {
        self.ring_points = ((10.0 * srate as f64).ceil() as usize).max(1);
        self.write_idx = 0;
        self.history = vec![Vec::new(); n_channels];
        self.pointer_points = ((x_sec * srate as f64).ceil() as usize).clamp(1, self.ring_points);
        self.pointer = vec![vec![0.0; self.pointer_points]; n_channels];
    }

    fn ensure_pointer_window(&mut self, srate: usize, x_sec: f64, n_channels: usize) {
        let requested =
            ((x_sec * srate as f64).ceil() as usize).clamp(1, self.ring_points.max(1));
        if self.pointer_points == requested && self.pointer.len() == n_channels {
            return;
        }
        self.pointer_points = requested;
        self.write_idx = 0;
        self.pointer = vec![vec![0.0; self.pointer_points]; n_channels];
    }
}

fn settings_path() -> Option<PathBuf> {
    #[cfg(windows)]
    {
        let appdata = std::env::var_os("APPDATA")?;
        return Some(PathBuf::from(appdata).join("brainflow").join("plot_real_time.json"));
    }
    #[cfg(not(windows))]
    {
        if let Some(xdg) = std::env::var_os("XDG_CONFIG_HOME") {
            return Some(PathBuf::from(xdg).join("brainflow").join("plot_real_time.json"));
        }
        let home = std::env::var_os("HOME")?;
        Some(
            PathBuf::from(home)
                .join(".config")
                .join("brainflow")
                .join("plot_real_time.json"),
        )
    }
}

fn load_persisted_view_settings() -> Result<Option<PersistedViewSettings>, String> {
    let Some(path) = settings_path() else {
        return Ok(None);
    };
    if !path.exists() {
        return Ok(None);
    }
    let raw = fs::read_to_string(&path).map_err(|e| format!("read {} failed: {e}", path.display()))?;
    let parsed = serde_json::from_str::<PersistedViewSettings>(&raw)
        .map_err(|e| format!("parse {} failed: {e}", path.display()))?;
    Ok(Some(parsed))
}

fn save_persisted_view_settings(settings: &PersistedViewSettings) -> Result<(), String> {
    let Some(path) = settings_path() else {
        return Ok(());
    };
    if let Some(parent) = path.parent() {
        fs::create_dir_all(parent).map_err(|e| format!("mkdir {} failed: {e}", parent.display()))?;
    }
    let payload = serde_json::to_vec_pretty(settings).map_err(|e| format!("serialize settings failed: {e}"))?;
    let tmp = path.with_extension("json.tmp");
    fs::write(&tmp, payload).map_err(|e| format!("write {} failed: {e}", tmp.display()))?;
    if path.exists() {
        fs::remove_file(&path).map_err(|e| format!("remove {} failed: {e}", path.display()))?;
    }
    fs::rename(&tmp, &path)
        .map_err(|e| format!("rename {} -> {} failed: {e}", tmp.display(), path.display()))?;
    Ok(())
}

struct RtApp {
    mode: AppMode,
    cfg: GuiConfig,
    view: ViewState,
    conn: ConnectionState,
    buffers: BufferState,
    dc_tails: Vec<Vec<f64>>,
    total_samples: usize,
    close_shutdown_started: bool,
}

impl RtApp {
    fn format_err<E: std::fmt::Display + std::fmt::Debug>(err: E) -> String {
        let text = err.to_string();
        if text.trim().is_empty() {
            format!("{err:?}")
        } else {
            text
        }
    }

    fn new(mode: AppMode, cfg: GuiConfig) -> Self {
        let mut view = ViewState::default();
        view.x_sec = cfg.x_sec.clamp(0.1, 10.0);
        let mut app = Self {
            mode,
            cfg,
            view,
            conn: ConnectionState::default(),
            buffers: BufferState::default(),
            dc_tails: Vec::new(),
            total_samples: 0,
            close_shutdown_started: false,
        };
        app.try_load_settings();
        app
    }

    fn try_load_settings(&mut self) {
        if !self.is_enhanced() {
            return;
        }
        match load_persisted_view_settings() {
            Ok(Some(saved)) => self.apply_persisted_settings(saved),
            Ok(None) => {}
            Err(err) => self.set_error(format!("settings load warning: {err}")),
        }
    }

    fn apply_persisted_settings(&mut self, saved: PersistedViewSettings) {
        if saved.version != SETTINGS_VERSION {
            return;
        }
        if !self.cfg.x_sec_from_cli {
            self.view.x_sec = saved.x_sec.clamp(0.1, 10.0);
        }
        self.view.y_range_v = saved.y_range_v.clamp(5e-6, 5.0);
        self.view.dc_correction_enabled = saved.dc_correction_enabled;
        self.view.bandpass_enabled = saved.bandpass_enabled;
        self.view.sweep_pointer_enabled = saved.sweep_pointer_enabled;
        self.view.line_color = egui::Color32::from_rgba_unmultiplied(
            saved.line_color_rgba[0],
            saved.line_color_rgba[1],
            saved.line_color_rgba[2],
            saved.line_color_rgba[3],
        );
        self.view.background_color = egui::Color32::from_rgba_unmultiplied(
            saved.background_color_rgba[0],
            saved.background_color_rgba[1],
            saved.background_color_rgba[2],
            saved.background_color_rgba[3],
        );
    }

    fn try_save_settings(&self) {
        if !self.is_enhanced() {
            return;
        }
        if let Err(err) = save_persisted_view_settings(&PersistedViewSettings::from_view(&self.view)) {
            eprintln!("settings save warning: {err}");
        }
    }

    fn board_id_from_i32(value: i32) -> Result<BoardIds, String> {
        BoardIds::from_i32(value).ok_or_else(|| format!("unsupported board id: {value}"))
    }

    fn is_enhanced(&self) -> bool {
        self.mode == AppMode::Enhanced
    }

    fn x_points(&self) -> usize {
        if self.is_enhanced() && self.view.sweep_pointer_enabled {
            self.buffers.pointer_points.max(1)
        } else {
            ((self.view.x_sec * self.conn.sampling_rate as f64).ceil() as usize).clamp(1, self.buffers.ring_points.max(1))
        }
    }

    fn set_error<S: Into<String>>(&mut self, msg: S) {
        let text = msg.into();
        eprintln!("{text}");
        self.view.last_error = Some(text);
    }

    fn disconnect(&mut self) {
        if let Some(board) = self.conn.board.take() {
            // Best-effort safe shutdown: stop stream first (drives idle), then release session (dispose).
            if let Err(err) = board.stop_stream() {
                self.set_error(format!("stop_stream during shutdown failed: {}", Self::format_err(err)));
            }
            if let Err(err) = board.release_session() {
                self.set_error(format!(
                    "release_session during shutdown failed: {}",
                    Self::format_err(err)
                ));
            }
        }
        self.conn.connected = false;
        self.conn.timestamp_channel = None;
        self.dc_tails.clear();
    }

    fn begin_async_close_shutdown(&mut self) {
        if self.close_shutdown_started {
            return;
        }
        self.close_shutdown_started = true;
        self.conn.connected = false;
        self.conn.timestamp_channel = None;
        self.dc_tails.clear();
        if let Some(board) = self.conn.board.take() {
            // Best-effort safe shutdown on close: stop stream first (idle), then release session (dispose).
            // Keep shutdown on the GUI thread to avoid FFI races during process teardown.
            let _ = board.stop_stream();
            let _ = board.release_session();
        }
    }

    fn resolve_channel_labels(_effective: BoardIds, channel_rows: &[usize]) -> Vec<String> {
        channel_rows
            .iter()
            .map(|row| format!("ch {row}"))
            .collect::<Vec<String>>()
    }

    fn connect(&mut self) -> Result<(), String> {
        self.disconnect();
        self.view.last_error = None;
        self.total_samples = 0;

        let board_id = Self::board_id_from_i32(self.cfg.board_id)?;
        let master_board = Self::board_id_from_i32(self.cfg.master_board)?;
        let params = BrainFlowInputParamsBuilder::new()
            .master_board(master_board)
            .ip_address(&self.cfg.ip_address)
            .ip_port(self.cfg.ip_port)
            .ip_protocol(self.cfg.ip_protocol)
            .timeout(self.cfg.timeout)
            .build();

        let board = BoardShim::new(board_id, params).map_err(|e| format!("BoardShim::new failed: {}", Self::format_err(e)))?;
        let mut prepared = false;
        let mut last_prepare_err = String::new();
        for attempt in 1..=3 {
            match board.prepare_session() {
                Ok(_) => {
                    prepared = true;
                    break;
                }
                Err(err) => {
                    last_prepare_err = Self::format_err(err);
                    // EDX server can hold stale ownership briefly after prior client close.
                    if attempt < 3 {
                        let _ = board_shim::release_all_sessions();
                        std::thread::sleep(Duration::from_millis(300));
                        continue;
                    }
                }
            }
        }
        if !prepared {
            return Err(format!(
                "prepare_session failed after retries: {}. Check EDX server/client ownership (single active client).",
                last_prepare_err
            ));
        }
        if let Err(err) = board.start_stream(self.cfg.buffer_size, "") {
            let _ = board.release_session();
            return Err(format!("start_stream failed: {}", Self::format_err(err)));
        }

        let effective = board.get_board_id();
        let srate =
            get_sampling_rate(effective, BrainFlowPresets::DefaultPreset).map_err(|e| format!("get_sampling_rate failed: {}", Self::format_err(e)))?;
        let default_channels =
            get_exg_channels(effective, BrainFlowPresets::DefaultPreset).unwrap_or_default();
        let channel_rows = if self.cfg.channels_from_cli {
            self.cfg.channels.clone()
        } else if default_channels.is_empty() {
            self.cfg.channels.clone()
        } else {
            default_channels
        };
        if channel_rows.is_empty() {
            let _ = board.stop_stream();
            let _ = board.release_session();
            return Err("no channels available for plotting".to_string());
        }

        self.conn.connected = true;
        self.conn.board = Some(board);
        self.conn.effective_board = Some(effective);
        self.conn.sampling_rate = srate.max(1);
        self.conn.timestamp_channel =
            get_timestamp_channel(effective, BrainFlowPresets::DefaultPreset).ok();
        self.conn.channel_labels = Self::resolve_channel_labels(effective, &channel_rows);
        self.conn.channel_rows = channel_rows;
        self.buffers
            .reset_for_channels(self.conn.channel_rows.len(), self.conn.sampling_rate, self.view.x_sec);
        self.dc_tails = vec![Vec::new(); self.conn.channel_rows.len()];
        Ok(())
    }

    fn update_effective_sampling_rate_from_timestamps(
        &mut self,
        data: &ndarray::Array2<f64>,
        cols: usize,
    ) {
        let Some(ts_row) = self.conn.timestamp_channel else {
            return;
        };
        if cols < 2 || ts_row >= data.nrows() {
            return;
        }

        let t0 = data[[ts_row, 0]];
        let t1 = data[[ts_row, cols - 1]];
        if !t0.is_finite() || !t1.is_finite() || t1 <= t0 {
            return;
        }

        let observed = (cols as f64 - 1.0) / (t1 - t0);
        if !observed.is_finite() || !(10.0..=100000.0).contains(&observed) {
            return;
        }

        // Smooth sampling-rate estimate to avoid visible jitter in time axis.
        let prev = self.conn.sampling_rate as f64;
        let blended = (0.9 * prev) + (0.1 * observed);
        self.conn.sampling_rate = blended.round().clamp(1.0, 100000.0) as usize;
    }

    fn dc_window_points(&self) -> usize {
        ((0.5 * self.conn.sampling_rate as f64).round() as usize).clamp(3, 2048)
    }

    fn apply_dc_block_fir(&mut self, chan_idx: usize, chunk: &mut [f64]) {
        if chunk.is_empty() {
            return;
        }
        let window = self.dc_window_points();
        if window <= 1 {
            return;
        }
        if chan_idx >= self.dc_tails.len() {
            return;
        }

        let tail = &mut self.dc_tails[chan_idx];
        let tail_len = tail.len();
        let mut extended = Vec::with_capacity(tail_len + chunk.len());
        extended.extend_from_slice(tail);
        extended.extend_from_slice(chunk);

        let mut low = extended.clone();
        if let Err(err) = perform_rolling_filter(&mut low, window, AggOperations::Mean) {
            self.set_error(format!(
                "dc correction failed: {}",
                Self::format_err(err)
            ));
            return;
        }

        for i in 0..chunk.len() {
            chunk[i] = extended[tail_len + i] - low[tail_len + i];
        }

        let keep = window.saturating_sub(1).min(extended.len());
        tail.clear();
        tail.extend_from_slice(&extended[extended.len() - keep..]);
    }

    fn append_history(&mut self, idx: usize, samples: &[f64]) {
        let buf = &mut self.buffers.history[idx];
        buf.extend_from_slice(samples);
        if buf.len() > self.buffers.ring_points {
            let overflow = buf.len() - self.buffers.ring_points;
            buf.drain(0..overflow);
        }
    }

    fn append_pointer_batch(&mut self, chunks: &[Vec<f64>]) {
        if chunks.is_empty() {
            return;
        }
        let sample_count = chunks[0].len();
        if sample_count == 0 {
            return;
        }
        for sample_idx in 0..sample_count {
            for (chan_idx, chunk) in chunks.iter().enumerate() {
                if sample_idx < chunk.len() {
                    self.buffers.pointer[chan_idx][self.buffers.write_idx] = chunk[sample_idx];
                }
            }
            self.buffers.write_idx =
                (self.buffers.write_idx + 1) % self.buffers.pointer_points.max(1);
        }
    }

    fn poll_data(&mut self) {
        if !self.conn.connected {
            return;
        }
        if self.is_enhanced() && self.view.sweep_pointer_enabled {
            self.buffers
                .ensure_pointer_window(self.conn.sampling_rate, self.view.x_sec, self.conn.channel_rows.len());
        }

        let data = {
            let Some(board) = &self.conn.board else {
                return;
            };

            let available = match board.get_board_data_count(BrainFlowPresets::DefaultPreset) {
                Ok(v) => v,
                Err(err) => {
                    self.set_error(format!(
                        "get_board_data_count failed: {}",
                        Self::format_err(err)
                    ));
                    self.disconnect();
                    return;
                }
            };
            if available == 0 {
                return;
            }
            let pull_points = available.min(MAX_PULL_PER_TICK).max(1);
            board.get_board_data(Some(pull_points), BrainFlowPresets::DefaultPreset)
        };

        let data = match data {
            Ok(data) => data,
            Err(err) => {
                self.set_error(format!("stream polling failed: {}", Self::format_err(err)));
                self.disconnect();
                return;
            }
        };

        let rows = data.nrows();
        let cols = data.ncols();
        if cols == 0 {
            return;
        }
        self.update_effective_sampling_rate_from_timestamps(&data, cols);
        self.total_samples += cols;

        let mut chunks: Vec<Vec<f64>> = Vec::with_capacity(self.conn.channel_rows.len());
        for chan_idx in 0..self.conn.channel_rows.len() {
            let row_idx = self.conn.channel_rows[chan_idx];
            if row_idx >= rows {
                self.set_error(format!("channel index {row_idx} out of range for {rows} rows"));
                chunks.push(Vec::new());
                continue;
            }

            let mut chunk = (0..cols).map(|c| data[[row_idx, c]]).collect::<Vec<f64>>();
            if self.is_enhanced() && self.view.dc_correction_enabled {
                self.apply_dc_block_fir(chan_idx, &mut chunk);
            }
            if self.is_enhanced() && self.view.bandpass_enabled {
                if let Err(err) = perform_bandpass(
                    &mut chunk,
                    self.conn.sampling_rate,
                    1.0,
                    45.0,
                    4,
                    FilterTypes::ButterworthZeroPhase,
                    0.0,
                ) {
                    self.set_error(format!("bandpass failed: {}", Self::format_err(err)));
                }
            }

            self.append_history(chan_idx, &chunk);
            chunks.push(chunk);
        }
        if self.is_enhanced() && self.view.sweep_pointer_enabled {
            self.append_pointer_batch(&chunks);
        }
    }

    fn filo_series(&self, idx: usize) -> Vec<f64> {
        let x_points = self.x_points();
        let history = &self.buffers.history[idx];
        if history.len() >= x_points {
            history[history.len() - x_points..].to_vec()
        } else {
            let mut out = vec![0.0; x_points - history.len()];
            out.extend_from_slice(history);
            out
        }
    }

    fn pointer_series(&self, idx: usize) -> Vec<f64> {
        let x_points = self.x_points();
        let pointer = &self.buffers.pointer[idx];
        if pointer.is_empty() {
            return vec![0.0; x_points];
        }
        pointer.clone()
    }

    fn draw_controls(&mut self, ui: &mut egui::Ui) {
        ui.horizontal(|ui| {
            ui.label(format!(
                "Board {} master {} endpoint {}:{}",
                self.cfg.board_id, self.cfg.master_board, self.cfg.ip_address, self.cfg.ip_port
            ));
            ui.separator();
            ui.label(if self.conn.connected { "Status: Connected" } else { "Status: Disconnected" });
            ui.separator();
            ui.label(format!("Samples: {}", self.total_samples));

            if ui.button("Reconnect").clicked() {
                if let Err(err) = self.connect() {
                    self.set_error(format!("connect failed: {err}"));
                }
            }
            if ui.button("Disconnect").clicked() {
                self.disconnect();
            }
        });

        if self.is_enhanced() {
            ui.horizontal(|ui| {
                ui.label("Y +/- (V)");
                ui.add(
                    egui::Slider::new(&mut self.view.y_range_v, 5e-6..=5.0)
                        .logarithmic(true),
                );
                ui.separator();
                ui.label("X window (sec)");
                ui.add(egui::Slider::new(&mut self.view.x_sec, 0.1..=10.0));
                ui.separator();
                ui.checkbox(&mut self.view.dc_correction_enabled, "DC correction");
                ui.checkbox(&mut self.view.bandpass_enabled, "Bandpass 1-45 Hz");
                ui.checkbox(&mut self.view.sweep_pointer_enabled, "Sweep Pointer Mode");
            });
            ui.horizontal(|ui| {
                ui.label("Line");
                ui.color_edit_button_srgba(&mut self.view.line_color);
                ui.separator();
                ui.label("Background");
                ui.color_edit_button_srgba(&mut self.view.background_color);
                if ui.button("Reset Colors").clicked() {
                    let defaults = ViewState::default();
                    self.view.line_color = defaults.line_color;
                    self.view.background_color = defaults.background_color;
                }
            });
        }

        if let Some(err) = &self.view.last_error {
            ui.colored_label(egui::Color32::RED, format!("Last error: {err}"));
        }
    }
}

impl Drop for RtApp {
    fn drop(&mut self) {
        if self.close_shutdown_started {
            self.conn.board = None;
            self.conn.connected = false;
            self.conn.timestamp_channel = None;
        } else {
            self.begin_async_close_shutdown();
        }
        self.try_save_settings();
    }
}

impl eframe::App for RtApp {
    fn update(&mut self, ctx: &egui::Context, _frame: &mut eframe::Frame) {
        if ctx.input(|i| i.viewport().close_requested()) {
            self.begin_async_close_shutdown();
            self.try_save_settings();
            return;
        }

        self.poll_data();

        egui::TopBottomPanel::top("top_panel").show(ctx, |ui| self.draw_controls(ui));

        egui::CentralPanel::default().show(ctx, |ui| {
            if let Some(board) = self.conn.effective_board {
                ui.label(format!("Effective board descriptor: {:?}", board));
            }

            egui::ScrollArea::vertical().show(ui, |ui| {
                let x_points = self.x_points();
                let y = self.view.y_range_v;
                let pointer_x = if self.conn.sampling_rate > 0 {
                    self.buffers.write_idx as f64 / self.conn.sampling_rate as f64
                } else {
                    0.0
                };

                for idx in 0..self.conn.channel_rows.len() {
                    let row_idx = self.conn.channel_rows[idx];
                    let label = &self.conn.channel_labels[idx];
                    ui.label(label);

                    let series = if self.is_enhanced() && self.view.sweep_pointer_enabled {
                        self.pointer_series(idx)
                    } else {
                        self.filo_series(idx)
                    };

                    let points = series
                        .iter()
                        .enumerate()
                        .map(|(i, v)| [i as f64 / self.conn.sampling_rate as f64, *v])
                        .collect::<Vec<[f64; 2]>>();
                    let line = Line::new(PlotPoints::from(points)).color(self.view.line_color);

                    egui::Frame::none()
                        .fill(self.view.background_color)
                        .show(ui, |ui| {
                            Plot::new(format!("plot_ch_{row_idx}"))
                                .height(120.0)
                                .allow_scroll(false)
                                .allow_zoom(false)
                                .allow_boxed_zoom(false)
                                .show_background(false)
                                .auto_bounds(egui::Vec2b::new(false, false))
                                .show(ui, |plot_ui| {
                                    plot_ui.set_plot_bounds(PlotBounds::from_min_max(
                                        [0.0, -y],
                                        [x_points as f64 / self.conn.sampling_rate as f64, y],
                                    ));
                                    plot_ui.line(line);
                                    if self.is_enhanced() && self.view.sweep_pointer_enabled {
                                        plot_ui.vline(VLine::new(pointer_x));
                                    }
                                })
                        });
                }
            });
        });

        ctx.request_repaint_after(Duration::from_millis(50));
    }

    fn on_exit(&mut self, _gl: Option<&eframe::glow::Context>) {
        self.begin_async_close_shutdown();
        self.try_save_settings();
    }
}

fn parse_channels(value: &str) -> Result<Vec<usize>, String> {
    let mut out = Vec::new();
    for token in value.split(',') {
        let trimmed = token.trim();
        if trimmed.is_empty() {
            continue;
        }
        out.push(
            trimmed
                .parse::<usize>()
                .map_err(|_| format!("invalid channel value: {trimmed}"))?,
        );
    }
    if out.is_empty() {
        return Err("channels list cannot be empty".to_string());
    }
    Ok(out)
}

fn parse_ip_protocol(value: &str) -> Result<IpProtocolTypes, String> {
    match value.trim().to_ascii_lowercase().as_str() {
        "none" | "no_ip_protocol" => Ok(IpProtocolTypes::NoIpProtocol),
        "udp" => Ok(IpProtocolTypes::Udp),
        "tcp" => Ok(IpProtocolTypes::Tcp),
        "edx" => Ok(IpProtocolTypes::Edx),
        _ => match value.parse::<i32>() {
            Ok(0) => Ok(IpProtocolTypes::NoIpProtocol),
            Ok(1) => Ok(IpProtocolTypes::Udp),
            Ok(2) => Ok(IpProtocolTypes::Tcp),
            Ok(3) => Ok(IpProtocolTypes::Edx),
            _ => Err(format!("invalid --ip-protocol: {value}")),
        },
    }
}

fn parse_args(mode: AppMode) -> Result<GuiConfig, String> {
    let mut cfg = GuiConfig::default();
    let args = std::env::args().skip(1).collect::<Vec<String>>();
    let mut i = 0usize;
    while i < args.len() {
        let key = &args[i];
        if key == "--help" || key == "-h" {
            return Err(format!(
                "Usage: cargo run --release --example {} -- [--board-id <int>] [--master-board <int>] [--ip-address <host>] [--ip-port <int>] [--ip-protocol <edx|tcp|udp|none|0..3>] [--timeout <sec>] [--x-sec <float>] [--channels <csv>] [--buffer-size <int>]",
                if mode == AppMode::Enhanced {
                    "plot_real_time"
                } else {
                    "plot_real_time_min"
                }
            ));
        }
        let value = args
            .get(i + 1)
            .ok_or_else(|| format!("missing value for {key}"))?;

        match key.as_str() {
            "--board-id" => {
                cfg.board_id = value
                    .parse::<i32>()
                    .map_err(|_| format!("invalid --board-id: {value}"))?;
            }
            "--master-board" => {
                cfg.master_board = value
                    .parse::<i32>()
                    .map_err(|_| format!("invalid --master-board: {value}"))?;
            }
            "--ip-address" => {
                if value.contains("://") || value.contains('/') {
                    return Err(format!(
                        "invalid --ip-address host value: {value}, provide host only"
                    ));
                }
                cfg.ip_address = value.clone();
            }
            "--ip-port" => {
                cfg.ip_port = value
                    .parse::<usize>()
                    .map_err(|_| format!("invalid --ip-port: {value}"))?;
                if cfg.ip_port == 0 || cfg.ip_port > 65535 {
                    return Err("--ip-port must be in range 1..65535".to_string());
                }
            }
            "--ip-protocol" => {
                cfg.ip_protocol = parse_ip_protocol(value)?;
            }
            "--timeout" => {
                cfg.timeout = value
                    .parse::<usize>()
                    .map_err(|_| format!("invalid --timeout: {value}"))?;
            }
            "--channels" => {
                cfg.channels = parse_channels(value)?;
                cfg.channels_from_cli = true;
            }
            "--buffer-size" => {
                cfg.buffer_size = value
                    .parse::<usize>()
                    .map_err(|_| format!("invalid --buffer-size: {value}"))?;
                if cfg.buffer_size == 0 {
                    return Err("--buffer-size must be > 0".to_string());
                }
            }
            "--x-sec" => {
                cfg.x_sec = value
                    .parse::<f64>()
                    .map_err(|_| format!("invalid --x-sec: {value}"))?;
                cfg.x_sec = cfg.x_sec.clamp(0.1, 10.0);
                cfg.x_sec_from_cli = true;
            }
            _ => return Err(format!("unknown argument: {key}")),
        }
        i += 2;
    }
    Ok(cfg)
}

fn run(mode: AppMode) {
    if let Err(err) = board_shim::enable_dev_board_logger() {
        eprintln!("failed to enable board logger: {err}");
    }

    let cfg = match parse_args(mode) {
        Ok(cfg) => cfg,
        Err(msg) => {
            eprintln!("{msg}");
            std::process::exit(2);
        }
    };

    let mut app = RtApp::new(mode, cfg);
    if let Err(err) = app.connect() {
        eprintln!("startup connection failed: {err}");
        std::process::exit(1);
    }

    let native_options = eframe::NativeOptions::default();
    let title = if mode == AppMode::Enhanced {
        "BrainFlow Rust Plot Realtime"
    } else {
        "BrainFlow Rust Plot Realtime Min"
    };
    if let Err(err) = eframe::run_native(title, native_options, Box::new(|_cc| Box::new(app))) {
        eprintln!("gui runtime failed: {err}");
        std::process::exit(1);
    }
}

pub fn run_minimal_from_args() {
    run(AppMode::Minimal);
}

pub fn run_enhanced_from_args() {
    run(AppMode::Enhanced);
}
