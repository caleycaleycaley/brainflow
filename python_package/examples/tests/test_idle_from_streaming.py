"""
ANT Neuro EDX: idle transition behavior test.

Tests stop_stream() behavior and edge cases around idle transitions.

Usage:
    python python_package/examples/tests/test_idle_from_streaming.py
    python python_package/examples/tests/test_idle_from_streaming.py --board-id 81 --verbose
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
    p = argparse.ArgumentParser(description="BrainFlow idle transition test")
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

    step("1. prepare + start_stream")
    board.prepare_session()
    board.start_stream()
    time.sleep(1.0)
    data = board.get_board_data()
    print(f"  Streaming OK, got {data.shape[1]} samples")

    step("2. stop_stream (timing)")
    t0 = time.time()
    board.stop_stream()
    dt = time.time() - t0
    print(f"  stop_stream took {dt:.3f}s")

    step("3. start_stream again after stop")
    t0 = time.time()
    board.start_stream()
    dt = time.time() - t0
    print(f"  start_stream took {dt:.3f}s")
    time.sleep(1.0)
    data = board.get_board_data()
    print(f"  Got {data.shape[1]} samples — stream resumed OK")

    step("4. stop_stream twice")
    board.stop_stream()
    print("  First stop OK")
    try:
        board.stop_stream()
        print("  Second stop OK (no error)")
    except BrainFlowError as e:
        print(f"  Second stop raised: {e}")

    step("5. release_session without stop_stream")
    # Release current board first, then create fresh one
    board.release_session()
    print("  Released previous board")
    time.sleep(1.0)
    board = make_board(args)
    board.prepare_session()
    board.start_stream()
    time.sleep(0.5)
    print("  Streaming, now calling release_session directly...")
    t0 = time.time()
    board.release_session()
    dt = time.time() - t0
    print(f"  release_session (no stop) took {dt:.3f}s")

    step("6. reconnect after release-without-stop")
    time.sleep(1.0)
    board2 = make_board(args)
    try:
        board2.prepare_session()
        print("  Reconnected OK — no 'Amplifier in use'")
        board2.release_session()
    except BrainFlowError as e:
        print(f"  Reconnect FAILED: {e}")

    print(f"\n{'='*50}")
    print("IDLE TRANSITION TESTS COMPLETE")
    print(f"{'='*50}")


if __name__ == "__main__":
    main()
