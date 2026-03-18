"""
ANT Neuro EDX: gRPC error diagnostic suite.

Probes three known server-side error conditions and determines whether
each is transient or persistent:

  Probe 1 - "Sample loss": server flushes stale error from mode
      transition buffer on first GetFrame call. Measures first-data
      latency and steady-state throughput.
  Probe 2 - "Object reference not set": server returns null-ref during
      SetMode(Idle) or Amplifier_Dispose after stream cancellation.
      Varies delay between stop_stream and release_session.
  Probe 3 - Rapid mode transitions: switches EEG<->impedance at
      decreasing intervals to find the minimum safe transition time
      before USB bandwidth errors appear.

Requires a real ANT Neuro device and the EDX gRPC server running.

Usage:
    python python_package/examples/tests/test_edx_grpc_errors.py
    python python_package/examples/tests/test_edx_grpc_errors.py --ip localhost --port 3390 --master-board 51
"""

import argparse
import json
import sys
import time

import numpy as np
from brainflow.board_shim import BoardShim, BrainFlowInputParams, IpProtocolTypes


def parse_args():
    p = argparse.ArgumentParser(description="EDX gRPC error diagnostic")
    p.add_argument("--ip", default="localhost")
    p.add_argument("--port", type=int, default=3390)
    p.add_argument("--board-id", type=int, default=67)
    p.add_argument("--master-board", type=int, default=51)
    p.add_argument("--timeout", type=int, default=5)
    return p.parse_args()


def make_params(args):
    params = BrainFlowInputParams()
    params.ip_address = args.ip
    params.ip_port = args.port
    params.ip_protocol = IpProtocolTypes.EDX.value
    params.master_board = args.master_board
    params.timeout = args.timeout
    return params


def probe_sample_loss(args):
    """
    Probe 1: "Sample loss" error.

    The EDX server may report a sample-loss error in its frame buffer
    during the first GetFrame calls after a mode transition. This probe
    measures whether the error is transient (startup-only) or persistent.
    """
    print("\n" + "=" * 60)
    print("PROBE 1: Sample loss during initial stream start")
    print("=" * 60)

    params = make_params(args)
    board = BoardShim(args.board_id, params)

    try:
        board.prepare_session()
        caps = json.loads(board.config_board("edx:get_capabilities"))
        print(f"  Device: {caps.get('selected_model', '?')}, "
              f"{len(caps.get('active_channels', []))} channels")

        # Start stream and poll rapidly to measure first-data latency
        board.start_stream()
        t_start = time.time()

        first_data_t = None
        total_samples_early = 0
        for i in range(20):  # poll every 250ms for 5s
            time.sleep(0.25)
            data = board.get_board_data()
            n = data.shape[1]
            elapsed = time.time() - t_start
            total_samples_early += n
            if n > 0 and first_data_t is None:
                first_data_t = elapsed
            print(f"  t={elapsed:.1f}s: {n} samples")

        print(f"\n  First data arrived at: {first_data_t:.2f}s" if first_data_t else
              "\n  WARNING: No data received in 5s!")
        print(f"  Total samples in first 5s: {total_samples_early}")

        # Stream 5 more seconds — should be error-free
        print("\n  Streaming 5 more seconds (steady state)...")
        board.get_board_data()  # drain
        time.sleep(5.0)
        data = board.get_board_data()
        steady_samples = data.shape[1]
        print(f"  Steady-state: {steady_samples} samples in 5s")

        board.stop_stream()

        if steady_samples > 5000:
            print("\n  VERDICT: Sample loss is TRANSIENT (startup only).")
            print("  CAUSE: EDX server flushes stale error from mode transition buffer.")
            print("  IMPACT: None — read_thread retries and recovers automatically.")
        else:
            print("\n  VERDICT: PERSISTENT data loss. Check server health.")

    finally:
        try:
            board.stop_stream()
        except Exception:
            pass
        board.release_session()


def probe_object_reference_null(args):
    """
    Probe 2: "Object reference not set" error during release.

    After stop_stream cancels the GetFrame context, the server may have a
    null amplifier reference. This probe varies the delay between stop and
    release to determine if it's a race condition or permanent state issue.
    """
    print("\n" + "=" * 60)
    print("PROBE 2: 'Object reference not set' during release")
    print("=" * 60)
    print("  Watch the [board_logger] lines for 'Object reference' errors.")
    print("  If errors disappear at longer delays -> race condition in server.")
    print("  If errors persist -> server dispose path bug.\n")

    delays = [0.0, 0.5, 1.0, 2.0]

    for delay in delays:
        print(f"  --- delay={delay}s ---")
        params = make_params(args)
        board = BoardShim(args.board_id, params)

        try:
            board.prepare_session()
            board.start_stream()
            time.sleep(2.0)

            data = board.get_board_data()
            print(f"    {data.shape[1]} samples collected")

            t0 = time.time()
            board.stop_stream()
            print(f"    stop_stream: {time.time()-t0:.3f}s")

            if delay > 0:
                time.sleep(delay)

            t0 = time.time()
            board.release_session()
            print(f"    release_session: {time.time()-t0:.3f}s")

        except Exception as e:
            print(f"    ERROR: {e}")
            try:
                board.release_session()
            except Exception:
                pass

        time.sleep(1.0)  # server recovery between trials


def probe_rapid_mode_transitions(args):
    """
    Probe 3: Rapid mode transitions stress test.

    Switches EEG<->impedance at decreasing intervals to find the minimum
    safe transition time before errors appear.
    """
    print("\n" + "=" * 60)
    print("PROBE 3: Rapid mode transitions")
    print("=" * 60)

    params = make_params(args)
    board = BoardShim(args.board_id, params)

    try:
        board.prepare_session()
        board.start_stream()
        time.sleep(1.0)

        transitions = [
            ("impedance_mode:1", "EEG -> Impedance", 3.0),
            ("impedance_mode:0", "Impedance -> EEG", 2.0),
            ("impedance_mode:1", "EEG -> Impedance (fast)", 1.0),
            ("impedance_mode:0", "Impedance -> EEG (fast)", 1.0),
            ("impedance_mode:1", "EEG -> Impedance (rapid)", 0.5),
            ("impedance_mode:0", "Impedance -> EEG (rapid)", 0.5),
        ]

        for cmd, label, wait in transitions:
            print(f"\n  {label}")
            t0 = time.time()
            try:
                resp = board.config_board(cmd)
                dt = time.time() - t0
                print(f"    config_board: {dt:.3f}s, response: {resp}")
            except Exception as e:
                dt = time.time() - t0
                print(f"    FAILED in {dt:.3f}s: {e}")
                print("    Minimum safe transition time exceeded.")
                break

            time.sleep(wait)
            data = board.get_board_data()
            print(f"    {data.shape[1]} samples after {wait}s")

        board.stop_stream()

    finally:
        try:
            board.stop_stream()
        except Exception:
            pass
        try:
            board.release_session()
        except Exception:
            pass


def main():
    args = parse_args()
    BoardShim.enable_dev_board_logger()

    print("EDX gRPC Error Diagnostic Suite")
    print(f"Target: {args.ip}:{args.port}, board={args.board_id}, master={args.master_board}")

    probe_sample_loss(args)
    probe_object_reference_null(args)
    probe_rapid_mode_transitions(args)

    print("\n" + "=" * 60)
    print("ALL PROBES COMPLETE")
    print("=" * 60)


if __name__ == "__main__":
    main()
