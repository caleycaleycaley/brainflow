"""
ANT Neuro EDX: mode changes without active streaming.

Tests whether config_board("impedance_mode:X") works when stream is not running,
and whether starting a stream after a mode change picks up that mode.

Usage:
    python python_package/examples/tests/test_mode_without_streaming.py
    python python_package/examples/tests/test_mode_without_streaming.py --board-id 81 --verbose
"""

import argparse
import sys
import time

from brainflow.board_shim import BoardShim, BrainFlowInputParams, BrainFlowError, IpProtocolTypes


def make_board(args):
    params = BrainFlowInputParams()
    params.ip_address = args.ip
    params.ip_port = args.port
    params.ip_protocol = IpProtocolTypes.EDX.value
    if args.master_board >= 0:
        params.master_board = args.master_board
    params.timeout = args.timeout
    return BoardShim(args.board_id, params)


def step(name):
    print(f"\n--- {name} ---")


def main():
    p = argparse.ArgumentParser(description="BrainFlow mode-without-streaming test")
    p.add_argument("--ip", default="localhost")
    p.add_argument("--port", type=int, default=3390)
    p.add_argument("--board-id", type=int, default=81)
    p.add_argument("--master-board", type=int, default=-100)
    p.add_argument("--timeout", type=int, default=15)
    p.add_argument("--verbose", action="store_true")
    args = p.parse_args()

    if args.verbose:
        BoardShim.enable_dev_board_logger()
    else:
        BoardShim.enable_board_logger()

    board = make_board(args)

    step("1. prepare_session")
    board.prepare_session()
    print("  OK")

    step("2. config_board('impedance_mode:1') WITHOUT start_stream")
    try:
        resp = board.config_board("impedance_mode:1")
        print(f"  Response: {resp}")
        print("  No error — mode set while idle")
    except BrainFlowError as e:
        print(f"  Error: {e}")

    step("3. start_stream after impedance mode set")
    try:
        board.start_stream()
        time.sleep(2.0)
        data = board.get_board_data()
        print(f"  Got {data.shape[1]} samples — is this impedance data?")
        # Check if any rows have impedance-like values (high ohm values)
        if data.shape[1] > 0:
            last = data[:, -1]
            nonzero = sum(1 for v in last if v != 0.0)
            print(f"  Non-zero values in last sample: {nonzero}/{len(last)}")
    except BrainFlowError as e:
        print(f"  Error starting stream: {e}")

    step("4. stop_stream")
    try:
        board.stop_stream()
        print("  OK")
    except BrainFlowError as e:
        print(f"  Error: {e}")

    step("5. config_board('impedance_mode:0') while stopped")
    try:
        resp = board.config_board("impedance_mode:0")
        print(f"  Response: {resp}")
    except BrainFlowError as e:
        print(f"  Error: {e}")

    step("6. start_stream in EEG mode")
    try:
        board.start_stream()
        time.sleep(1.0)
        data = board.get_board_data()
        print(f"  Got {data.shape[1]} EEG samples")
        assert data.shape[1] > 0, "No data"
    except BrainFlowError as e:
        print(f"  Error: {e}")

    step("7. cleanup")
    try:
        board.stop_stream()
    except Exception:
        pass
    board.release_session()
    print("  Released")

    step("8. reconnect probe")
    time.sleep(1.0)
    board2 = make_board(args)
    try:
        board2.prepare_session()
        print("  Reconnected OK")
        board2.release_session()
    except BrainFlowError as e:
        print(f"  Reconnect FAILED: {e}")

    print(f"\n{'='*50}")
    print("MODE-WITHOUT-STREAMING TESTS COMPLETE")
    print(f"{'='*50}")


if __name__ == "__main__":
    main()
