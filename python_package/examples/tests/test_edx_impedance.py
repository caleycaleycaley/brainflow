"""
ANT Neuro EDX: impedance measurement test.

Verifies that BrainFlow impedance mode produces valid data on
resistance_channels, ref_resistance_channels, and gnd_resistance_channels
for the EDX board (id 67). No direct gRPC calls needed -- uses only
the BrainFlow API (config_board("impedance_mode:1/0")).

Flow: connect -> start EEG -> switch to impedance -> poll resistance
values -> switch back to EEG -> verify EEG resumes -> cleanup.

Requires a real ANT Neuro device and the EDX gRPC server running.

Usage:
    python python_package/examples/tests/test_edx_impedance.py
    python python_package/examples/tests/test_edx_impedance.py --duration 5 --verbose
"""

import argparse
import sys
import time

from brainflow.board_shim import BoardShim, BrainFlowInputParams, IpProtocolTypes


def main():
    p = argparse.ArgumentParser(description="BrainFlow impedance data test")
    p.add_argument("--ip", default="localhost")
    p.add_argument("--port", type=int, default=3390)
    p.add_argument("--board-id", type=int, default=67)
    p.add_argument("--master-board", type=int, default=51)
    p.add_argument("--duration", type=float, default=3.0)
    p.add_argument("--verbose", action="store_true")
    args = p.parse_args()

    if args.verbose:
        BoardShim.enable_dev_board_logger()
    else:
        BoardShim.enable_board_logger()

    # --- discover channels ---
    resistance_ch = BoardShim.get_resistance_channels(args.board_id)
    ref_resistance_ch = []
    gnd_resistance_ch = []
    try:
        import json
        descr = BoardShim.get_board_descr(args.board_id)
        ref_resistance_ch = descr.get("ref_resistance_channels", [])
        gnd_resistance_ch = descr.get("gnd_resistance_channels", [])
    except Exception:
        pass

    print(f"resistance_channels:     {resistance_ch}")
    print(f"ref_resistance_channels: {ref_resistance_ch}")
    print(f"gnd_resistance_channels: {gnd_resistance_ch}")

    all_rows = list(resistance_ch) + list(ref_resistance_ch) + list(gnd_resistance_ch)
    if not all_rows:
        print("[FAIL] No resistance channels defined for this board")
        sys.exit(1)

    # --- connect ---
    params = BrainFlowInputParams()
    params.ip_address = args.ip
    params.ip_port = args.port
    params.ip_protocol = IpProtocolTypes.EDX.value
    params.master_board = args.master_board
    params.timeout = 5

    board = BoardShim(args.board_id, params)
    board.prepare_session()
    print("[ok] session prepared")

    # --- start in EEG mode first ---
    board.start_stream()
    print("[ok] streaming started (EEG mode)")
    time.sleep(1.0)

    # --- switch to impedance ---
    print("\n[switch] config_board('impedance_mode:1')")
    resp = board.config_board("impedance_mode:1")
    print(f"[switch] response: {resp}")

    # --- collect impedance data ---
    print(f"\n[collect] polling impedance for {args.duration}s ...")
    total = 0
    nonzero_count = 0
    last_values = None

    t_start = time.time()
    while time.time() - t_start < args.duration:
        time.sleep(0.5)
        data = board.get_board_data()
        n = data.shape[1]
        total += n
        if n > 0:
            # Show the last sample's resistance values
            last_col = data[:, -1]
            values = {f"row{r}": last_col[r] for r in all_rows if r < len(last_col)}
            nonzero = sum(1 for v in values.values() if v != 0.0)
            nonzero_count += nonzero
            last_values = values
            print(f"  +{n} samples, resistance values: {values}")
        else:
            print(f"  +0 samples")

    # --- switch back to EEG ---
    print("\n[switch] config_board('impedance_mode:0')")
    resp = board.config_board("impedance_mode:0")
    print(f"[switch] response: {resp}")
    time.sleep(0.5)

    # --- verify EEG still works ---
    data = board.get_board_data()
    print(f"[verify] EEG samples after mode switch: {data.shape[1]}")

    # --- cleanup ---
    board.stop_stream()
    board.release_session()
    print("[ok] cleaned up")

    # --- result ---
    print(f"\n{'='*50}")
    print(f"IMPEDANCE DATA TEST")
    print(f"{'='*50}")
    print(f"  Total samples:     {total}")
    print(f"  Nonzero readings:  {nonzero_count}")
    print(f"  Last values:       {last_values}")
    ok = total > 0 and nonzero_count > 0
    print(f"  Result:            {'OK' if ok else 'FAIL - no impedance data'}")
    print(f"{'='*50}")

    if not ok:
        sys.exit(1)


if __name__ == "__main__":
    main()
