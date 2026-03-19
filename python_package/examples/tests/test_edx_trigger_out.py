"""
ANT Neuro EDX: trigger output test.

Configures and fires trigger pulses on the EDX board (id 67) via
BrainFlow config_board("edx:trigger_config/start/stop") commands,
then monitors the marker channel for received trigger markers.

Configurable parameters: output channel, duty cycle, pulse frequency,
pulse count, burst frequency, burst count. Also probes amplifier state
after stop_stream and release_session for diagnostic purposes.

Requires a real ANT Neuro device and the EDX gRPC server running.
Use a trigger loopback cable to verify marker reception.

Usage:
    python python_package/examples/tests/test_edx_trigger_out.py
    python python_package/examples/tests/test_edx_trigger_out.py --channel 1 --duration 10
    python python_package/examples/tests/test_edx_trigger_out.py --pulse-freq 20 --verbose
"""

import argparse
import json
import signal
import sys
import time
from collections import Counter

from brainflow.board_shim import BoardShim, BrainFlowInputParams, IpProtocolTypes


def parse_args():
    p = argparse.ArgumentParser(description="ANT EDX trigger-out test")
    p.add_argument("--ip", default="localhost", help="EDX gRPC host (default: localhost)")
    p.add_argument("--port", type=int, default=3390, help="EDX gRPC port (default: 3390)")
    p.add_argument("--board-id", type=int, default=67, help="BrainFlow board id (default: 67 = ANT_NEURO_EDX)")
    p.add_argument("--master-board", type=int, default=51, help="Master board id (default: 51)")
    p.add_argument("--channel", type=int, default=0, help="Trigger output channel index (default: 0)")
    p.add_argument("--duty-cycle", type=float, default=50.0, help="Duty cycle percent 0-100 (default: 50)")
    p.add_argument("--pulse-freq", type=float, default=10.0, help="Pulse frequency Hz (default: 10)")
    p.add_argument("--pulse-count", type=int, default=5, help="Pulse count (default: 5)")
    p.add_argument("--burst-freq", type=float, default=1.0, help="Burst frequency Hz (default: 1)")
    p.add_argument("--burst-count", type=int, default=3, help="Burst count (default: 3)")
    p.add_argument("--duration", type=float, default=5.0, help="Seconds to collect data while trigger runs (default: 5)")
    p.add_argument("--timeout", type=int, default=5, help="BrainFlow timeout in seconds (default: 5)")
    p.add_argument("--verbose", action="store_true", help="Enable BrainFlow debug logging")
    return p.parse_args()


def main():
    args = parse_args()

    if args.verbose:
        BoardShim.enable_dev_board_logger()
    else:
        BoardShim.enable_board_logger()

    # --- connect ---
    params = BrainFlowInputParams()
    params.ip_address = args.ip
    params.ip_port = args.port
    params.ip_protocol = IpProtocolTypes.EDX.value
    params.master_board = args.master_board
    params.timeout = args.timeout

    board = BoardShim(args.board_id, params)

    # --- discover capabilities (no device needed) ---
    # EDX board uses master_board_role=descriptor_source, so data layout
    # comes from the master board, not the EDX board id.
    data_board = args.master_board

    print(f"\n[discover] EDX board (id={args.board_id}):")
    try:
        descr = BoardShim.get_board_descr(args.board_id)
        print(f"  Board descriptor: {json.dumps(descr, indent=2)}")
    except Exception as e:
        print(f"  [warn] get_board_descr failed: {e}")

    print(f"\n[discover] Master/data board (id={data_board}):")
    try:
        descr_master = BoardShim.get_board_descr(data_board)
        print(f"  Board descriptor: {json.dumps(descr_master, indent=2)}")
    except Exception as e:
        print(f"  [warn] get_board_descr failed: {e}")
    try:
        print(f"  Sampling rate:    {BoardShim.get_sampling_rate(data_board)}")
    except Exception:
        pass
    try:
        print(f"  EEG channels:     {BoardShim.get_eeg_channels(data_board)}")
    except Exception:
        pass
    try:
        marker_ch = BoardShim.get_marker_channel(data_board)
        print(f"  Marker channel:   {marker_ch}")
    except Exception:
        marker_ch = None
        print("  Marker channel:   not available")
    try:
        print(f"  Other channels:   {BoardShim.get_other_channels(data_board)}")
    except Exception:
        pass

    # --- connect ---
    print(f"\n[connect] board_id={args.board_id}  master_board={args.master_board}  ip={args.ip}:{args.port}")

    try:
        board.prepare_session()
    except Exception as e:
        print(f"[FAIL] prepare_session failed: {e}")
        print("       Is the EDX gRPC server running?")
        sys.exit(1)
    print("[connect] session prepared")

    # --- discover device capabilities (requires live connection) ---
    print("\n[discover] Querying device capabilities ...")
    try:
        resp = board.config_board("edx:get_capabilities")
        caps = json.loads(resp)
        print(f"  Device model:     {caps.get('selected_model', '?')}")
        print(f"  Sampling rates:   {caps.get('sampling_rates', [])}")
        print(f"  Active channels:  {caps.get('active_channels', [])}")
        channels = caps.get("channels", [])
        if channels:
            print(f"  Channel count:    {len(channels)}")
            trigger_channels = [ch for ch in channels if "trig" in ch.get("name", "").lower()]
            if trigger_channels:
                print(f"  Trigger channels: {trigger_channels}")
            else:
                print("  Trigger channels: (none named 'trig*' — use --channel to specify)")
    except Exception as e:
        print(f"  [warn] get_capabilities failed: {e}")

    board.start_stream()
    print("[connect] streaming started")

    # give the stream a moment to stabilize
    time.sleep(1.0)

    # --- handle Ctrl+C gracefully ---
    interrupted = False

    def _on_sigint(sig, frame):
        nonlocal interrupted
        interrupted = True
        print("\n[interrupt] Ctrl+C received, stopping ...")

    signal.signal(signal.SIGINT, _on_sigint)

    ok = True
    total_samples = 0
    marker_counts: Counter = Counter()

    try:
        # --- configure trigger ---
        config_cmd = (
            f"edx:trigger_config:{args.channel},"
            f"{args.duty_cycle},{args.pulse_freq},{args.pulse_count},"
            f"{args.burst_freq},{args.burst_count}"
        )
        print(f"\n[trigger] config_board(\"{config_cmd}\")")
        resp = board.config_board(config_cmd)
        print(f"[trigger] response: {resp}")
        resp_obj = json.loads(resp)
        assert resp_obj.get("status") == "ok", f"trigger_config failed: {resp}"

        # --- start trigger ---
        start_cmd = f"edx:trigger_start:{args.channel}"
        print(f"[trigger] config_board(\"{start_cmd}\")")
        resp = board.config_board(start_cmd)
        print(f"[trigger] response: {resp}")
        resp_obj = json.loads(resp)
        assert resp_obj.get("status") == "ok", f"trigger_start failed: {resp}"

        # --- collect data ---
        print(f"\n[collect] polling for {args.duration}s ...")
        poll_interval = 0.5
        elapsed = 0.0

        while elapsed < args.duration and not interrupted:
            time.sleep(poll_interval)
            elapsed += poll_interval
            data = board.get_board_data()
            n = data.shape[1]
            total_samples += n
            if marker_ch is not None and n > 0:
                markers = data[marker_ch]
                for v in markers:
                    if v != 0.0:
                        marker_counts[int(v)] += 1
            print(f"  [{elapsed:.1f}s] +{n} samples  (total {total_samples})", end="")
            if marker_counts:
                print(f"  markers so far: {dict(marker_counts)}", end="")
            print()

    except Exception as e:
        print(f"\n[FAIL] {e}")
        ok = False

    # --- cleanup: diagnose whether BrainFlow API properly idles the amp ---
    # C++ stop_stream() should call set_idle_mode() internally (line 876),
    # and release_session() should call set_idle_mode() + Amplifier_Dispose (lines 887-896).
    print("\n[cleanup] stop_stream ...")
    t0 = time.time()
    try:
        board.stop_stream()
        dt = time.time() - t0
        print(f"[cleanup] stop_stream returned OK in {dt:.3f}s")
    except Exception as e:
        dt = time.time() - t0
        print(f"[cleanup] stop_stream FAILED in {dt:.3f}s: {e}")

    # Probe amp state after stop_stream — is it idle or still in EEG mode?
    print("[diag] querying amp state after stop_stream ...")
    t0 = time.time()
    try:
        resp = board.config_board("edx:get_capabilities")
        dt = time.time() - t0
        caps = json.loads(resp)
        print(f"[diag] amp reachable after stop_stream ({dt:.3f}s), model={caps.get('selected_model', '?')}")
        print(f"[diag] active_channels={len(caps.get('active_channels', []))}")
    except Exception as e:
        dt = time.time() - t0
        print(f"[diag] get_capabilities after stop_stream FAILED ({dt:.3f}s): {e}")

    # --- summary ---
    print("\n" + "=" * 50)
    print("TRIGGER OUT TEST SUMMARY")
    print("=" * 50)
    print(f"  Channel:        {args.channel}")
    print(f"  Duty cycle:     {args.duty_cycle}")
    print(f"  Pulse freq:     {args.pulse_freq} Hz")
    print(f"  Pulse count:    {args.pulse_count}")
    print(f"  Burst freq:     {args.burst_freq} Hz")
    print(f"  Burst count:    {args.burst_count}")
    print(f"  Duration:       {args.duration}s")
    print(f"  Total samples:  {total_samples}")
    if marker_ch is not None:
        nonzero = sum(marker_counts.values())
        print(f"  Marker nonzero: {nonzero}")
        if marker_counts:
            print(f"  Marker values:  {dict(marker_counts)}")
        else:
            print("  Marker values:  (none observed - use loopback to verify)")
    else:
        print("  Marker channel: not available")
    print(f"  Result:         {'OK' if ok and not interrupted else 'INTERRUPTED' if interrupted else 'FAIL'}")
    print("=" * 50)

    # --- release session (should call set_idle_mode + Amplifier_Dispose) ---
    print("\n[cleanup] release_session ...")
    t0 = time.time()
    try:
        board.release_session()
        dt = time.time() - t0
        print(f"[cleanup] release_session returned OK in {dt:.3f}s")
    except Exception as e:
        dt = time.time() - t0
        print(f"[cleanup] release_session FAILED in {dt:.3f}s: {e}")

    # Probe amp state after release — can we still reach it? (new session needed)
    print("[diag] reconnecting to check amp state after release_session ...")
    t0 = time.time()
    try:
        probe = BoardShim(args.board_id, params)
        probe.prepare_session()
        resp = probe.config_board("edx:get_capabilities")
        dt = time.time() - t0
        caps = json.loads(resp)
        print(f"[diag] amp state after full cleanup ({dt:.3f}s): model={caps.get('selected_model', '?')}")
        print(f"[diag] active_channels={len(caps.get('active_channels', []))}")
        probe.release_session()
    except Exception as e:
        dt = time.time() - t0
        print(f"[diag] post-cleanup probe FAILED ({dt:.3f}s): {e}")

    print("[done]")

    if not ok or interrupted:
        sys.exit(1)


if __name__ == "__main__":
    main()
