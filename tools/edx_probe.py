#!/usr/bin/env python3
"""Probe EDX gRPC server capabilities and derive safe defaults.

This tool is protocol-first: it talks directly to EdigRPC and logs each API call,
capability payload, and recommended config profile for EE-511 style devices.

Safety guarantees:
- If a handle is created, tool attempts `Amplifier_SetMode(Idle)` before exit.
- Tool always attempts `Amplifier_Dispose` before exit.
"""

from __future__ import annotations

import argparse
import importlib
import json
import os
import signal
import sys
import tempfile
import time
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Any, Dict, List, Optional

import grpc


@dataclass
class CallRecord:
    call: str
    ok: bool
    elapsed_ms: int
    request: Dict[str, Any]
    summary: Dict[str, Any]
    error: Optional[str] = None


class ProbeError(RuntimeError):
    pass


def _norm_float(value: float) -> float:
    return float(f"{float(value):.6g}")


def _channel_polarity_name(pb2: Any, value: int) -> str:
    mapping = {
        int(pb2.Referential): "Referential",
        int(pb2.Bipolar): "Bipolar",
        int(pb2.Receiver): "Receiver",
        int(pb2.Transmitter): "Transmitter",
    }
    return mapping.get(int(value), f"Unknown({int(value)})")


def _message_to_dict(msg: Any) -> Dict[str, Any]:
    from google.protobuf.json_format import MessageToDict

    return MessageToDict(msg, preserving_proto_field_name=True)


def _compile_proto(proto_path: Path, out_dir: Path) -> None:
    try:
        from grpc_tools import protoc
    except Exception as exc:  # pragma: no cover
        raise ProbeError(
            "grpcio-tools is required. Install with: python -m pip install grpcio-tools"
        ) from exc

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
        raise ProbeError(f"Failed to compile proto, grpc_tools.protoc returned {result}")


def _load_proto_modules(proto_file: Path):
    gen_root = Path(tempfile.mkdtemp(prefix="edx_probe_"))
    _compile_proto(proto_file, gen_root)
    sys.path.insert(0, str(gen_root))
    pb2 = importlib.import_module("EdigRPC_pb2")
    pb2_grpc = importlib.import_module("EdigRPC_pb2_grpc")
    return pb2, pb2_grpc


class EdxProbe:
    def __init__(self, endpoint: str, pb2: Any, pb2_grpc: Any, timeout: float):
        self.pb2 = pb2
        self.timeout = timeout
        self.channel = grpc.insecure_channel(endpoint)
        self.stub = pb2_grpc.EdigRPCStub(self.channel)
        self.records: List[CallRecord] = []
        self.handle: Optional[int] = None

    def _call(self, name: str, fn, req) -> Any:
        t0 = time.time()
        req_dict = _message_to_dict(req)
        try:
            resp = fn(req, timeout=self.timeout)
            elapsed = int((time.time() - t0) * 1000)
            resp_dict = _message_to_dict(resp)
            summary = {
                "keys": sorted(resp_dict.keys()),
            }
            # add tiny shape hints for common responses
            if "device_info_list" in resp_dict:
                summary["device_count"] = len(resp_dict["device_info_list"])
            if "channel_list" in resp_dict:
                summary["channel_count"] = len(resp_dict["channel_list"])
            if "rate_list" in resp_dict:
                summary["rate_count"] = len(resp_dict["rate_list"])
            if "frame_list" in resp_dict:
                summary["frame_count"] = len(resp_dict["frame_list"])
            self.records.append(
                CallRecord(
                    call=name,
                    ok=True,
                    elapsed_ms=elapsed,
                    request=req_dict,
                    summary=summary,
                )
            )
            return resp
        except grpc.RpcError as exc:
            elapsed = int((time.time() - t0) * 1000)
            err = f"{exc.code().name}: {exc.details()}"
            self.records.append(
                CallRecord(
                    call=name,
                    ok=False,
                    elapsed_ms=elapsed,
                    request=req_dict,
                    summary={},
                    error=err,
                )
            )
            raise

    def safe_idle_and_dispose(self) -> None:
        if self.handle is None:
            return
        try:
            req = self.pb2.Amplifier_SetModeRequest(
                AmplifierHandle=self.handle,
                Mode=self.pb2.AmplifierMode_Idle,
            )
            self._call("Amplifier_SetMode(Idle)", self.stub.Amplifier_SetMode, req)
        except Exception:
            pass
        try:
            req = self.pb2.Amplifier_DisposeRequest(AmplifierHandle=self.handle)
            self._call("Amplifier_Dispose", self.stub.Amplifier_Dispose, req)
        except Exception:
            pass
        self.handle = None


def choose_device(devices: List[Any], serial_filter: str) -> Any:
    if not devices:
        raise ProbeError("No devices returned by DeviceManager_GetDevices")
    if serial_filter:
        for d in devices:
            if serial_filter.upper() in (d.Serial or "").upper() or serial_filter.upper() in (d.Key or "").upper():
                return d
        raise ProbeError(f"No device matched serial/key filter '{serial_filter}'")
    for d in devices:
        s = (d.Serial or "").upper()
        if "EE511" in s or "EE-511" in s:
            return d
    return devices[0]


def derive_profile(pb2: Any, selected: Any, channels_resp: Any, rates_resp: Any, ranges_resp: Any) -> Dict[str, Any]:
    referential = []
    bipolar = []
    triggers = []
    chan_by_idx = {}
    for ch in channels_resp.ChannelList:
        chan_by_idx[ch.ChannelIndex] = ch
        if ch.ChannelPolarity == pb2.Referential:
            referential.append(ch.ChannelIndex)
        elif ch.ChannelPolarity == pb2.Bipolar:
            bipolar.append(ch.ChannelIndex)
        elif ch.ChannelPolarity in (pb2.Receiver, pb2.Transmitter) or "TRIGGER" in (ch.Name or "").upper():
            triggers.append(ch.ChannelIndex)

    # Gather range candidates by polarity and keep full per-channel inventory
    ref_ranges = set()
    bip_ranges = set()
    range_map_by_channel: Dict[int, List[float]] = {}
    for idx, double_list in ranges_resp.RangeMap.items():
        ch = chan_by_idx.get(idx)
        vals = sorted({_norm_float(v) for v in double_list.Values})
        range_map_by_channel[int(idx)] = vals
        if not ch:
            continue
        if ch.ChannelPolarity == pb2.Referential:
            ref_ranges.update(vals)
        elif ch.ChannelPolarity == pb2.Bipolar:
            bip_ranges.update(vals)

    rates = list(rates_resp.RateList)

    serial_upper = (selected.Serial or "").upper()
    is_ee511 = ("EE511" in serial_upper) or ("EE-511" in serial_upper)

    # Start from requested defaults, then constrain to server capabilities.
    default_ref = 0.15
    default_bip = 2.5
    if ref_ranges and _norm_float(default_ref) not in ref_ranges:
        default_ref = sorted(ref_ranges)[0]
    if is_ee511 and abs(default_bip - (default_ref * 2.5)) > 1e-6:
        candidate_ref = _norm_float(default_bip / 2.5)
        if not ref_ranges or candidate_ref in ref_ranges:
            default_ref = candidate_ref
        else:
            default_bip = _norm_float(default_ref * 2.5)
    if bip_ranges and _norm_float(default_bip) not in bip_ranges:
        default_bip = sorted(bip_ranges)[0]

    default_rate = 500.0
    if rates and default_rate not in rates:
        default_rate = rates[0]

    channels_inventory = []
    for ch in sorted(channels_resp.ChannelList, key=lambda item: item.ChannelIndex):
        polarity_name = _channel_polarity_name(pb2, ch.ChannelPolarity)
        channels_inventory.append(
            {
                "channel_index": int(ch.ChannelIndex),
                "name": ch.Name,
                "polarity": polarity_name,
                "ranges": range_map_by_channel.get(int(ch.ChannelIndex), []),
            }
        )

    derived_bipolar_from_ref = sorted(
        {_norm_float(v * 2.5) for v in ref_ranges}
    ) if ref_ranges else []

    available_ranges = {
        "by_channel_index": {str(k): v for k, v in sorted(range_map_by_channel.items())},
        "by_polarity": {
            "referential": sorted(ref_ranges),
            "bipolar": sorted(bip_ranges),
        },
        "channels_inventory": channels_inventory,
    }
    if is_ee511:
        available_ranges["ee511_rule"] = {
            "description": "Bipolar range should be 2.5x referential range.",
            "derived_bipolar_from_referential": derived_bipolar_from_ref,
            "bipolar_reported_by_server": sorted(bip_ranges),
            "bipolar_missing_in_server_ranges": len(bip_ranges) == 0,
        }

    profile = {
        "device": {
            "key": selected.Key,
            "serial": selected.Serial,
        },
        "observed": {
            "referential_channel_count": len(referential),
            "bipolar_channel_count": len(bipolar),
            "trigger_channel_count": len(triggers),
            "sampling_rates": rates,
            "referential_ranges": sorted(ref_ranges),
            "bipolar_ranges": sorted(bip_ranges),
            "available_ranges": available_ranges,
        },
        "recommended": {
            "mode": "AmplifierMode_Eeg",
            "sampling_rate": default_rate,
            "data_ready_percentage": 5,
            "buffer_size": 1024,
            "referential_range": default_ref,
            "bipolar_range": default_bip,
            "active_channels": referential + bipolar,
            "trigger_channels": triggers,
        },
    }
    return profile


def main() -> int:
    parser = argparse.ArgumentParser(description="Probe EDX server and derive EE-511 defaults")
    parser.add_argument("--endpoint", default="localhost:3390", help="EDX gRPC endpoint host:port")
    parser.add_argument("--proto", default="src/board_controller/ant_neuro_edx/proto/EdigRPC.proto")
    parser.add_argument("--serial-filter", default="", help="Optional serial/key filter")
    parser.add_argument("--frame-probe-seconds", type=int, default=2)
    parser.add_argument(
        "--set-mode-probe",
        action="store_true",
        help="Attempt Amplifier_SetMode(Eeg) + frame polling. Disabled by default because some servers enforce model-specific stream keys.",
    )
    parser.add_argument("--out", default="edx_probe_report.json")
    args = parser.parse_args()

    proto_file = Path(args.proto).resolve()
    if not proto_file.exists():
        raise ProbeError(f"Proto file not found: {proto_file}")

    pb2, pb2_grpc = _load_proto_modules(proto_file)
    probe = EdxProbe(args.endpoint, pb2, pb2_grpc, timeout=5.0)

    def _cleanup(*_):
        probe.safe_idle_and_dispose()

    signal.signal(signal.SIGINT, _cleanup)
    signal.signal(signal.SIGTERM, _cleanup)

    report: Dict[str, Any] = {
        "endpoint": args.endpoint,
        "timestamp_unix": time.time(),
    }

    try:
        state_resp = probe._call("GetState", probe.stub.GetState, pb2.GetStateRequest())
        report["state_id"] = state_resp.Id

        devices_resp = probe._call(
            "DeviceManager_GetDevices",
            probe.stub.DeviceManager_GetDevices,
            pb2.DeviceManager_GetDevicesRequest(),
        )
        devices = list(devices_resp.DeviceInfoList)
        report["devices"] = [{"key": d.Key, "serial": d.Serial} for d in devices]

        selected = choose_device(devices, args.serial_filter)
        report["selected"] = {"key": selected.Key, "serial": selected.Serial}

        create_resp = probe._call(
            "Controller_CreateDevice",
            probe.stub.Controller_CreateDevice,
            pb2.Controller_CreateDeviceRequest(DeviceInfoList=[selected]),
        )
        probe.handle = create_resp.AmplifierHandle
        report["handle"] = probe.handle

        channels_resp = probe._call(
            "Amplifier_GetChannelsAvailable",
            probe.stub.Amplifier_GetChannelsAvailable,
            pb2.Amplifier_GetChannelsAvailableRequest(AmplifierHandle=probe.handle),
        )
        rates_resp = probe._call(
            "Amplifier_GetSamplingRatesAvailable",
            probe.stub.Amplifier_GetSamplingRatesAvailable,
            pb2.Amplifier_GetSamplingRatesAvailableRequest(AmplifierHandle=probe.handle),
        )
        ranges_resp = probe._call(
            "Amplifier_GetRangesAvailable",
            probe.stub.Amplifier_GetRangesAvailable,
            pb2.Amplifier_GetRangesAvailableRequest(AmplifierHandle=probe.handle),
        )
        modes_resp = probe._call(
            "Amplifier_GetModesAvailable",
            probe.stub.Amplifier_GetModesAvailable,
            pb2.Amplifier_GetModesAvailableRequest(AmplifierHandle=probe.handle),
        )
        power_resp = probe._call(
            "Amplifier_GetPower",
            probe.stub.Amplifier_GetPower,
            pb2.Amplifier_GetPowerRequest(AmplifierHandle=probe.handle),
        )

        report["modes"] = [int(m) for m in modes_resp.ModeList]
        report["power"] = _message_to_dict(power_resp)

        profile = derive_profile(pb2, selected, channels_resp, rates_resp, ranges_resp)
        report["profile"] = profile

        if args.set_mode_probe:
            set_req = pb2.Amplifier_SetModeRequest(
                AmplifierHandle=probe.handle,
                Mode=pb2.AmplifierMode_Eeg,
                StreamParams=pb2.StreamParams(
                    ActiveChannels=profile["recommended"]["active_channels"],
                    SamplingRate=profile["recommended"]["sampling_rate"],
                    BufferSize=profile["recommended"]["buffer_size"],
                    DataReadyPercentage=profile["recommended"]["data_ready_percentage"],
                ),
            )

            probe._call("Amplifier_SetMode(Eeg)", probe.stub.Amplifier_SetMode, set_req)

            frame_stats = []
            end_t = time.time() + max(1, args.frame_probe_seconds)
            while time.time() < end_t:
                fr = probe._call(
                    "Amplifier_GetFrame",
                    probe.stub.Amplifier_GetFrame,
                    pb2.Amplifier_GetFrameRequest(AmplifierHandle=probe.handle),
                )
                frame_stats.append(len(fr.FrameList))
                time.sleep(0.1)
            report["frame_probe"] = {
                "samples": len(frame_stats),
                "frame_counts": frame_stats,
                "max_frames_per_call": max(frame_stats) if frame_stats else 0,
            }

    except Exception as exc:
        report["error"] = str(exc)
    finally:
        probe.safe_idle_and_dispose()
        report["calls"] = [asdict(r) for r in probe.records]
        Path(args.out).write_text(json.dumps(report, indent=2), encoding="utf-8")
        print(f"Wrote report: {args.out}")
        if report.get("error"):
            print(f"Probe error: {report['error']}")
            return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
