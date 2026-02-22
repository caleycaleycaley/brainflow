# Rust Examples: Realtime GUI (EDX)

## Prerequisites

- EDX server must be running and reachable at `host:port` (default: `localhost:3390`).
- Device must be connected and available to a single active client.
- On Windows, ensure BrainFlow runtime DLLs are on `PATH`:
  - `$env:PATH="C:\DEV\brainflow-dev\compiled\Release;C:\DEV\brainflow-dev\rust_package\brainflow\lib;$env:PATH"`

## Quick Start

Run from `rust_package/brainflow`:

```bash
cargo run --release --example plot_real_time_min
```

```bash
cargo run --release --example plot_real_time
```

Legacy alias:

```bash
cargo run --release --example edx_gui_smoke
```

## Defaults

- `board_id=66` (`ANT_NEURO_EDX_BOARD`)
- `master_board=51` (`ANT_NEURO_EE_511_BOARD`)
- `ip_address=localhost`
- `ip_port=3390`
- `ip_protocol=edx` (optional for board `66`)
- `timeout=15`
- channels: tries EXG channels from board descriptor; falls back to CLI `--channels`

## CLI Arguments

- `--board-id` (default: `66`): BrainFlow board id.
- `--master-board` (default: `51`): descriptor source board id for EDX.
- `--ip-address` (default: `localhost`): EDX endpoint host.
- `--ip-port` (default: `3390`): EDX endpoint port.
- `--ip-protocol` (default: `edx`): `edx|tcp|udp|none` (optional for board `66`).
- `--timeout` (default: `15`): connection/session timeout in seconds.
- `--channels` (default: `1,2,3,4` fallback): comma-separated row indices.
- `--buffer-size` (default: `45000`): BrainFlow internal ring buffer length in samples.
- `--x-sec` (default: `4.0`, range `0.1..10.0`): GUI time window in seconds.

## Run With More Channels

```bash
cargo run --release --example plot_real_time -- --channels 1,2,3,4,5,6,7,8
```

## Run With Different Sampling Rates

Sampling rate is controlled via BrainFlow board config command.

```rust
use brainflow::board_shim::BoardShim;

// after create BoardShim and before start_stream:
board.prepare_session()?;
board.config_board("sampling_rate:500")?;
board.start_stream(45000, "")?;
```

Notes:

- Use rates supported by the connected amplifier/server capabilities.
- Realtime X-axis uses observed timestamp progression when available.

## GUI Controls

- Y range slider sets fixed bounds `[-V,+V]`; waveform clips outside view.
- X window slider controls visible time window (`0.1..10.0 s`).
- Sweep Pointer Mode uses overwrite-page rendering with wrap.
- DC correction applies causal FIR-style DC blocking via moving-average subtraction.
- Bandpass 1-45 Hz applies BrainFlow bandpass (`ButterworthZeroPhase`).
- Line/background color pickers are available in the top toolbar.

## Settings Persistence

`plot_real_time` persists UI settings per user and restores them on next run:

- `x_sec`, `y_range_v`
- DC correction, bandpass, sweep pointer toggles
- global line/background colors

Location:

- Windows: `%APPDATA%\brainflow\plot_real_time.json`
- Linux/macOS: `$XDG_CONFIG_HOME/brainflow/plot_real_time.json` (or `~/.config/brainflow/plot_real_time.json`)

Behavior:

- Missing file: defaults are used.
- Corrupt/unreadable file: warning is logged and defaults are used.
- CLI connection args still control board/master/endpoint/timeouts/channels/buffer.

## Troubleshooting

- Endpoint not reachable:
  - verify `--ip-address` / `--ip-port` and server process state.
- Missing or invalid `master_board`:
  - board `66` requires `--master-board`.
- Device already in use:
  - ensure only one active hardware client session.
- Time appears slow/incorrect:
  - verify server mode and requested sampling rate.
  - verify effective data rate from timestamp channel.
  - ensure no concurrent clients are consuming the same stream.

## Clean Shutdown

On app close, the example performs best-effort:

1. `stop_stream`
2. `release_session`

This is intended to leave the EDX session in a safe idle/disposed state.
