#!/usr/bin/env python3
"""Try multiple Amplifier_SetMode payload shapes and detect accepted format."""

from __future__ import annotations

import argparse
import importlib
import json
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Dict, List, Tuple

import grpc


def _compile_proto(proto_path: Path, out_dir: Path) -> None:
    from grpc_tools import protoc
    import pkg_resources

    grpc_include = Path(pkg_resources.resource_filename("grpc_tools", "_proto"))
    result = protoc.main(
        [
            "grpc_tools.protoc",
            f"-I{proto_path.parent}",
            f"-I{grpc_include}",
            f"--python_out={out_dir}",
            f"--grpc_python_out={out_dir}",
            str(proto_path),
        ]
    )
    if result != 0:
        raise RuntimeError(f"protoc failed with code={result}")


def _load_proto_modules(proto_file: Path):
    gen_root = Path(tempfile.mkdtemp(prefix="edx_setmode_probe_"))
    _compile_proto(proto_file, gen_root)
    sys.path.insert(0, str(gen_root))
    pb2 = importlib.import_module("EdigRPC_pb2")
    pb2_grpc = importlib.import_module("EdigRPC_pb2_grpc")
    return pb2, pb2_grpc


def _call(name: str, fn, req, timeout: float = 6.0) -> Tuple[bool, str]:
    try:
        fn(req, timeout=timeout)
        return True, ""
    except grpc.RpcError as exc:
        return False, f"{exc.code().name}: {exc.details()}"


def _choose_device(pb2: Any, stub: Any, serial_filter: str) -> Any:
    resp = stub.DeviceManager_GetDevices(pb2.DeviceManager_GetDevicesRequest(), timeout=6.0)
    devices = list(resp.DeviceInfoList)
    if not devices:
        raise RuntimeError("no devices returned by DeviceManager_GetDevices")
    if serial_filter:
        token = serial_filter.upper()
        for d in devices:
            if token in (d.Serial or "").upper() or token in (d.Key or "").upper():
                return d
        raise RuntimeError(f"no device matches serial filter '{serial_filter}'")
    for d in devices:
        s = (d.Serial or "").upper()
        if "EE511" in s or "EE-511" in s:
            return d
    return devices[0]


def _build_candidates(pb2: Any, channels_resp: Any, rates_resp: Any, ranges_resp: Any) -> List[Tuple[str, Dict[str, Any]]]:
    ch_list = list(channels_resp.ChannelList)
    rate_list = list(rates_resp.RateList)
    rate = 500.0 if 500.0 in rate_list else (rate_list[0] if rate_list else 500.0)
    all_channel_indices = [int(ch.ChannelIndex) for ch in ch_list]
    seq_channel_indices = list(range(len(ch_list)))

    helper_ranges: Dict[int, float] = {}
    for key in [int(pb2.Referential), int(pb2.Auxiliary), int(pb2.Bipolar)]:
        if key in ranges_resp.RangeMap and len(ranges_resp.RangeMap[key].Values) > 0:
            helper_ranges[key] = float(ranges_resp.RangeMap[key].Values[0])

    explicit_ee511_ranges = {
        int(pb2.Referential): 0.15,
        int(pb2.Bipolar): 2.5,
    }

    return [
        (
            "helper_like_seq_channels",
            {
                "active": seq_channel_indices,
                "ranges": helper_ranges,
                "rate": rate,
                "buffer": int(rate),
                "ready": 10,
                "stim": "",
            },
        ),
        (
            "helper_like_real_channels",
            {
                "active": all_channel_indices,
                "ranges": helper_ranges,
                "rate": rate,
                "buffer": int(rate),
                "ready": 10,
                "stim": "",
            },
        ),
        (
            "explicit_ee511_ranges_real_channels",
            {
                "active": all_channel_indices,
                "ranges": explicit_ee511_ranges,
                "rate": rate,
                "buffer": 1024,
                "ready": 5,
                "stim": "",
            },
        ),
        (
            "no_ranges_real_channels",
            {
                "active": all_channel_indices,
                "ranges": None,
                "rate": rate,
                "buffer": 1024,
                "ready": 5,
                "stim": "",
            },
        ),
        (
            "no_streamparams",
            {
                "active": None,
                "ranges": None,
                "rate": None,
                "buffer": None,
                "ready": None,
                "stim": "",
            },
        ),
    ]


def main() -> int:
    parser = argparse.ArgumentParser(description="Auto-probe SetMode payload shape")
    parser.add_argument("--endpoint", default="localhost:3390")
    parser.add_argument("--proto", default="src/board_controller/ant_neuro_edx/proto/EdigRPC.proto")
    parser.add_argument("--serial-filter", default="")
    parser.add_argument("--out", default="edx_setmode_autoprobe.json")
    args = parser.parse_args()

    proto_file = Path(args.proto).resolve()
    pb2, pb2_grpc = _load_proto_modules(proto_file)
    channel = grpc.insecure_channel(args.endpoint)
    stub = pb2_grpc.EdigRPCStub(channel)

    report: Dict[str, Any] = {
        "endpoint": args.endpoint,
        "timestamp_unix": time.time(),
        "attempts": [],
    }
    handle = None

    try:
        selected = _choose_device(pb2, stub, args.serial_filter)
        report["selected"] = {"key": selected.Key, "serial": selected.Serial}

        create_resp = stub.Controller_CreateDevice(
            pb2.Controller_CreateDeviceRequest(DeviceInfoList=[selected]),
            timeout=6.0,
        )
        handle = int(create_resp.AmplifierHandle)
        report["handle"] = handle

        channels_resp = stub.Amplifier_GetChannelsAvailable(
            pb2.Amplifier_GetChannelsAvailableRequest(AmplifierHandle=handle),
            timeout=6.0,
        )
        rates_resp = stub.Amplifier_GetSamplingRatesAvailable(
            pb2.Amplifier_GetSamplingRatesAvailableRequest(AmplifierHandle=handle),
            timeout=6.0,
        )
        ranges_resp = stub.Amplifier_GetRangesAvailable(
            pb2.Amplifier_GetRangesAvailableRequest(AmplifierHandle=handle),
            timeout=6.0,
        )

        report["capabilities"] = {
            "channel_count": len(channels_resp.ChannelList),
            "rates": list(rates_resp.RateList),
            "range_keys": [int(k) for k in ranges_resp.RangeMap.keys()],
        }

        winner = None
        for name, cfg in _build_candidates(pb2, channels_resp, rates_resp, ranges_resp):
            req = pb2.Amplifier_SetModeRequest(
                AmplifierHandle=handle,
                Mode=pb2.AmplifierMode_Eeg,
                StimParams=cfg["stim"],
            )
            if cfg["active"] is not None:
                sp = pb2.StreamParams(
                    ActiveChannels=cfg["active"],
                    SamplingRate=float(cfg["rate"]),
                    BufferSize=int(cfg["buffer"]),
                    DataReadyPercentage=int(cfg["ready"]),
                )
                if cfg["ranges"] is not None:
                    for k, v in cfg["ranges"].items():
                        sp.Ranges[int(k)] = float(v)
                req.StreamParams.CopyFrom(sp)

            ok, error = _call("Amplifier_SetMode(Eeg)", stub.Amplifier_SetMode, req)
            item = {
                "name": name,
                "ok": ok,
                "error": error,
                "config": cfg,
            }
            if ok:
                frame_ok, frame_err = _call(
                    "Amplifier_GetFrame",
                    stub.Amplifier_GetFrame,
                    pb2.Amplifier_GetFrameRequest(AmplifierHandle=handle),
                )
                item["frame_probe_ok"] = frame_ok
                item["frame_probe_error"] = frame_err
                winner = name
                report["attempts"].append(item)
                break
            report["attempts"].append(item)

        report["winner"] = winner
    except Exception as exc:
        report["error"] = str(exc)
    finally:
        if handle is not None:
            try:
                stub.Amplifier_SetMode(
                    pb2.Amplifier_SetModeRequest(
                        AmplifierHandle=handle, Mode=pb2.AmplifierMode_Idle
                    ),
                    timeout=6.0,
                )
            except Exception:
                pass
            try:
                stub.Amplifier_Dispose(
                    pb2.Amplifier_DisposeRequest(AmplifierHandle=handle),
                    timeout=6.0,
                )
            except Exception:
                pass
        Path(args.out).write_text(json.dumps(report, indent=2), encoding="utf-8")
        print(f"Wrote report: {args.out}")

    return 0 if report.get("winner") else 1


if __name__ == "__main__":
    raise SystemExit(main())

