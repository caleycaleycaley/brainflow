#!/usr/bin/env python3

from __future__ import annotations

import argparse, base64, csv, importlib, json, statistics, subprocess, sys, tempfile, time
from pathlib import Path
from typing import Any
import grpc


def compile_proto(proto: Path, out_dir: Path) -> None:
    from grpc_tools import protoc
    import pkg_resources
    inc = Path(pkg_resources.resource_filename('grpc_tools', '_proto'))
    ret = protoc.main(['grpc_tools.protoc', f'-I{proto.parent}', f'-I{inc}', f'--python_out={out_dir}', f'--grpc_python_out={out_dir}', str(proto)])
    if ret != 0:
        raise RuntimeError(f'protoc failed={ret}')


def load_proto(proto: Path):
    gen = Path(tempfile.mkdtemp(prefix='edx_time_probe_'))
    compile_proto(proto, gen)
    sys.path.insert(0, str(gen))
    return importlib.import_module('EdigRPC_pb2'), importlib.import_module('EdigRPC_pb2_grpc')


def msg_dict(msg: Any) -> dict:
    from google.protobuf.json_format import MessageToDict
    return MessageToDict(msg, preserving_proto_field_name=True)


def ts_to_unix(ts) -> float | None:
    if ts is None:
        return None
    return float(ts.seconds) + float(ts.nanos) / 1e9


def parse_csv_int(s: str) -> list[int]:
    out = [int(x.strip()) for x in s.split(',') if x.strip()]
    if not out:
        raise ValueError('empty int csv')
    return out


def parse_csv_float(s: str) -> list[float]:
    out = [float(x.strip()) for x in s.split(',') if x.strip()]
    if not out:
        raise ValueError('empty float csv')
    return out


def mean(v):
    return float(statistics.mean(v)) if v else None


def std(v):
    return float(statistics.pstdev(v)) if len(v) > 1 else 0.0 if len(v) == 1 else None


def choose_dev(pb2, stub, serial_filter: str):
    resp = stub.DeviceManager_GetDevices(pb2.DeviceManager_GetDevicesRequest(), timeout=6.0)
    devs = list(resp.DeviceInfoList)
    if not devs:
        raise RuntimeError('no devices')
    if serial_filter:
        t = serial_filter.upper()
        for d in devs:
            if t in (d.Serial or '').upper() or t in (d.Key or '').upper():
                return d
        raise RuntimeError(f'no serial match {serial_filter}')
    for d in devs:
        s = (d.Serial or '').upper()
        if 'EE511' in s or 'EE-511' in s:
            return d
    return devs[0]


def autoprobe_winner(args, out_dir: Path) -> dict:
    out = out_dir / 'edx_setmode_autoprobe_pre.json'
    cmd = [sys.executable, str((Path(__file__).parent / 'edx_setmode_autoprobe.py').resolve()), '--endpoint', args.endpoint, '--proto', str(Path(args.proto).resolve()), '--serial-filter', args.serial_filter, '--out', str(out)]
    subprocess.run(cmd, check=True, capture_output=True, text=True)
    data = json.loads(out.read_text(encoding='utf-8'))
    w = data.get('winner')
    for a in data.get('attempts', []):
        if a.get('name') == w and a.get('ok'):
            c = a.get('config') or {}
            return {'shape_name': w, 'active': c.get('active'), 'ranges': c.get('ranges'), 'stim': c.get('stim', '')}
    raise RuntimeError('no winner config')

class Probe:
    def __init__(self, args):
        self.args = args
        self.out = Path(args.out_dir).resolve()
        self.out.mkdir(parents=True, exist_ok=True)
        self.pb2, pb2_grpc = load_proto(Path(args.proto).resolve())
        self.stub = pb2_grpc.EdigRPCStub(grpc.insecure_channel(args.endpoint))
        self.trace = args.trace_id or time.strftime('%Y%m%d-%H%M%S')
        self.handle = None
        self.selected_dev = None
        self.rpc_rows = []
        self.frame_rows = []
        self.combo_rows = []
        self.pktmon_started = False
        self.pktmon_error = None

    def recreate_handle(self):
        if self.selected_dev is None:
            raise RuntimeError('cannot recreate handle: selected device is unknown')
        cr = self.rpc(
            'recovery',
            'recovery',
            'recovery',
            'Controller_CreateDevice(recover)',
            self.stub.Controller_CreateDevice,
            self.pb2.Controller_CreateDeviceRequest(DeviceInfoList=[self.selected_dev]),
        )
        self.handle = int(cr.AmplifierHandle)

    def rpc(self, combo, recipe, phase, name, fn, req):
        t0 = time.time()
        req_json = msg_dict(req) if self.args.capture_rpc_payloads else {}
        req_bin = req.SerializeToString()
        req_b64 = base64.b64encode(req_bin).decode('ascii') if self.args.capture_rpc_bytes else None
        code, details, resp_json, resp_bin, resp_b64 = 'OK', '', {}, b'', None
        try:
            resp = fn(req, timeout=self.args.rpc_timeout_sec)
            if self.args.capture_rpc_payloads:
                resp_json = msg_dict(resp)
            resp_bin = resp.SerializeToString()
            if self.args.capture_rpc_bytes:
                resp_b64 = base64.b64encode(resp_bin).decode('ascii')
            return resp
        except grpc.RpcError as e:
            code = e.code().name if e.code() else 'UNKNOWN'
            details = str(e.details() or '')
            raise
        finally:
            t1 = time.time()
            self.rpc_rows.append({
                'trace_id': self.trace, 'combo_id': combo, 'recipe_name': recipe, 'phase': phase,
                'rpc_name': name, 'request_wall_time_unix': t0, 'response_wall_time_unix': t1,
                'elapsed_ms': int((t1 - t0) * 1000), 'grpc_status_code': code, 'grpc_status_details': details,
                'request_proto_json': req_json, 'response_proto_json': resp_json,
                'request_proto_bytes_len': len(req_bin), 'response_proto_bytes_len': len(resp_bin),
                'request_proto_b64': req_b64, 'response_proto_b64': resp_b64,
            })

    def set_mode(self, combo, recipe, phase, mode_name, mode_enum, cfg, sr, dr, bs):
        req = self.pb2.Amplifier_SetModeRequest(AmplifierHandle=self.handle, Mode=mode_enum, StimParams=cfg.get('stim', ''))
        active = cfg.get('active')
        if active is not None:
            sp = self.pb2.StreamParams(ActiveChannels=active, SamplingRate=float(sr), BufferSize=int(bs), DataReadyPercentage=int(dr))
            ranges = cfg.get('ranges')
            if isinstance(ranges, dict):
                for k, v in ranges.items():
                    sp.Ranges[int(k)] = float(v)
            req.StreamParams.CopyFrom(sp)
        self.rpc(combo, recipe, phase, f'Amplifier_SetMode({mode_name})', self.stub.Amplifier_SetMode, req)

    def poll(self, combo, recipe, phase, mode_expected, sr, dr, bs, seconds, trigger_candidates, active, cum_combo):
        end = time.time() + seconds
        cum_phase = 0
        p_start = p_start_pc = p_host = None
        starts, starts_pc, dstart, dstartpc, dhost = [], [], [], [], []
        eeg_frames = non_eeg_frames = matrix_bad = marker_frames = 0
        call_idx = 0
        while time.time() < end:
            host = time.time()
            req = self.pb2.Amplifier_GetFrameRequest(AmplifierHandle=self.handle)
            try:
                resp = self.rpc(combo, recipe, phase, 'Amplifier_GetFrame', self.stub.Amplifier_GetFrame, req)
            except grpc.RpcError:
                call_idx += 1
                time.sleep(self.args.poll_sleep_sec)
                continue
            for f_idx, f in enumerate(resp.FrameList):
                rows, cols, dlen = int(f.Matrix.Rows), int(f.Matrix.Cols), len(f.Matrix.Data)
                ok = rows >= 0 and cols >= 0 and dlen == rows * cols
                if not ok:
                    matrix_bad += 1
                ftype = int(f.FrameType)
                ftype_name = self.pb2.AmplifierFrameType.Name(ftype) if ftype in self.pb2.AmplifierFrameType.values() else str(ftype)
                st = ts_to_unix(f.Start) if f.HasField('Start') else None
                stpc = ts_to_unix(f.StartPcTime) if f.HasField('StartPcTime') else None
                if st is not None: starts.append(st)
                if stpc is not None: starts_pc.append(stpc)
                ds = dspc = dh = None
                if p_start is not None and st is not None:
                    ds = st - p_start; dstart.append(ds)
                if p_start_pc is not None and stpc is not None:
                    dspc = stpc - p_start_pc; dstartpc.append(dspc)
                if p_host is not None:
                    dh = host - p_host; dhost.append(dh)
                if st is not None: p_start = st
                if stpc is not None: p_start_pc = stpc
                p_host = host
                markers = [int(m.TimeMarkerCode) for m in f.TimeMarkers]
                marker_times = [ts_to_unix(m.Start) if m.HasField('Start') else None for m in f.TimeMarkers]
                if markers: marker_frames += 1
                samples = rows if ok else 0
                if ftype_name == 'AmplifierFrameType_EEG':
                    eeg_frames += 1; cum_phase += samples; cum_combo += samples
                else:
                    non_eeg_frames += 1
                trig_val = None
                if ok and trigger_candidates and active:
                    trig_cols = [i for i, ch in enumerate(active) if ch in trigger_candidates]
                    if trig_cols and rows > 0:
                        trig_val = float(f.Matrix.Data[trig_cols[0]])
                self.frame_rows.append({
                    'trace_id': self.trace, 'combo_id': combo, 'recipe_name': recipe, 'phase': phase,
                    'call_index': call_idx, 'frame_index_in_call': f_idx, 'sampling_rate': float(sr),
                    'data_ready_percentage': int(dr), 'buffer_size': int(bs), 'mode_expected': mode_expected,
                    'frame_type': ftype_name, 'start_unix': st, 'start_pc_unix': stpc, 'host_recv_unix': host,
                    'rows': rows, 'cols': cols, 'data_len': dlen, 'matrix_ok': ok, 'samples_in_frame': samples,
                    'cumulative_samples_phase': cum_phase, 'cumulative_samples_combo': cum_combo,
                    'estimated_frame_duration_sec': (rows / sr if sr > 0 and rows >= 0 else 0.0),
                    'timemarkers_count': len(markers), 'timemarkers_codes': json.dumps(markers),
                    'timemarkers_start_times': json.dumps(marker_times),
                    'trigger_channel_present': bool(trigger_candidates), 'trigger_channel_indices': json.dumps(trigger_candidates),
                    'trigger_value_observed': trig_val, 'brainflow_marker_channel_candidate_value': markers[0] if markers else None,
                    'host_minus_start': (host - st if st is not None else None),
                    'host_minus_start_pc': (host - stpc if stpc is not None else None),
                    'delta_start_from_prev': ds, 'delta_start_pc_from_prev': dspc, 'delta_host_recv_from_prev': dh,
                })
            call_idx += 1
            time.sleep(self.args.poll_sleep_sec)
        stats = {
            'frames_total': eeg_frames + non_eeg_frames, 'frames_eeg': eeg_frames, 'frames_non_eeg': non_eeg_frames,
            'matrix_bad_count': matrix_bad, 'marker_frames': marker_frames,
            'start_present': len(starts), 'start_pc_present': len(starts_pc),
            'monotonic_start': all(starts[i] >= starts[i - 1] for i in range(1, len(starts))) if starts else True,
            'monotonic_start_pc': all(starts_pc[i] >= starts_pc[i - 1] for i in range(1, len(starts_pc))) if starts_pc else True,
            'delta_start_mean': mean(dstart), 'delta_start_std': std(dstart),
            'delta_start_pc_mean': mean(dstartpc), 'delta_host_mean': mean(dhost),
            'cumulative_samples_phase': cum_phase,
        }
        return cum_combo, stats

    def classify_gap(self, pre_end, post_begin, pause_sec):
        out = {'gap_start_sec': None, 'gap_startpc_sec': None, 'gap_hostrecv_sec': None, 'classification': 'insufficient_data'}
        if pre_end.get('start') is not None and post_begin.get('start') is not None:
            out['gap_start_sec'] = float(post_begin['start'] - pre_end['start'])
        if pre_end.get('start_pc') is not None and post_begin.get('start_pc') is not None:
            out['gap_startpc_sec'] = float(post_begin['start_pc'] - pre_end['start_pc'])
        if pre_end.get('host') is not None and post_begin.get('host') is not None:
            out['gap_hostrecv_sec'] = float(post_begin['host'] - pre_end['host'])
        g = out['gap_start_sec']
        if g is None:
            return out
        if g < 0:
            out['classification'] = 'clock_reset/rebase'
        elif g >= max(0.5, pause_sec * 0.5):
            out['classification'] = 'expected_pause_gap'
        else:
            out['classification'] = 'unexpected_drift'
        return out

    def phase_edges(self, combo, recipe, phase):
        rows = [r for r in self.frame_rows if r['combo_id'] == combo and r['recipe_name'] == recipe and r['phase'] == phase]
        if not rows:
            return {}, {}
        first, last = rows[0], rows[-1]
        end = {'start': last['start_unix'], 'start_pc': last['start_pc_unix'], 'host': last['host_recv_unix']}
        begin = {'start': first['start_unix'], 'start_pc': first['start_pc_unix'], 'host': first['host_recv_unix']}
        return end, begin

    def run_recipe(self, combo, recipe, sr, dr, bs, cfg, trigger_candidates):
        active = cfg.get('active')
        cum = 0
        out = {'recipe_name': recipe, 'sampling_rate': sr, 'data_ready_percentage': dr, 'buffer_size': bs, 'shape_name': cfg.get('shape_name'), 'phases': {}, 'status': 'ok', 'error': None}
        for attempt in range(2):
            try:
                self.set_mode(combo, recipe, 'set_eeg_pre', 'Eeg', self.pb2.AmplifierMode_Eeg, cfg, sr, dr, bs)
                cum, out['phases']['eeg_pre'] = self.poll(combo, recipe, 'eeg_pre', 'eeg', sr, dr, bs, self.args.phase_seconds_eeg, trigger_candidates, active, cum)
                if recipe == 'standard':
                    self.set_mode(combo, recipe, 'set_impedance', 'Impedance', self.pb2.AmplifierMode_Impedance, cfg, sr, dr, bs)
                    cum, out['phases']['impedance_pause'] = self.poll(combo, recipe, 'impedance_pause', 'impedance', sr, dr, bs, self.args.phase_seconds_impedance_standard, trigger_candidates, active, cum)
                elif recipe == 'idle_pause':
                    self.set_mode(combo, recipe, 'set_idle', 'Idle', self.pb2.AmplifierMode_Idle, cfg, sr, dr, bs)
                    cum, out['phases']['idle_pause'] = self.poll(combo, recipe, 'idle_pause', 'idle', sr, dr, bs, self.args.pause_seconds, trigger_candidates, active, cum)
                else:
                    self.set_mode(combo, recipe, 'set_impedance', 'Impedance', self.pb2.AmplifierMode_Impedance, cfg, sr, dr, bs)
                    cum, out['phases']['impedance_pause'] = self.poll(combo, recipe, 'impedance_pause', 'impedance', sr, dr, bs, self.args.pause_seconds, trigger_candidates, active, cum)
                self.set_mode(combo, recipe, 'set_eeg_post', 'Eeg', self.pb2.AmplifierMode_Eeg, cfg, sr, dr, bs)
                cum, out['phases']['eeg_post'] = self.poll(combo, recipe, 'eeg_post', 'eeg', sr, dr, bs, self.args.phase_seconds_eeg, trigger_candidates, active, cum)
                pre_end, pre_begin = self.phase_edges(combo, recipe, 'eeg_pre')
                post_end, post_begin = self.phase_edges(combo, recipe, 'eeg_post')
                out['pause_gap_metrics'] = self.classify_gap(pre_end, post_begin, self.args.pause_seconds)
                out['cumulative_samples_before_pause'] = int(out['phases'].get('eeg_pre', {}).get('cumulative_samples_phase', 0))
                out['cumulative_samples_after_resume'] = int(out['phases'].get('eeg_post', {}).get('cumulative_samples_phase', 0))
                pre_samples = out['cumulative_samples_before_pause']
                post_samples = out['cumulative_samples_after_resume']
                pre_expected_elapsed = max(pre_samples - 1, 0) / float(sr) if sr > 0 else None
                post_expected_elapsed = max(post_samples - 1, 0) / float(sr) if sr > 0 else None
                pre_observed_elapsed = None
                post_observed_elapsed = None
                if pre_end.get('start') is not None and pre_begin.get('start') is not None:
                    pre_observed_elapsed = float(pre_end['start'] - pre_begin['start'])
                if post_end.get('start') is not None and post_begin.get('start') is not None:
                    post_observed_elapsed = float(post_end['start'] - post_begin['start'])
                out['elapsed_checks'] = {
                    'pre_expected_elapsed_from_samples': pre_expected_elapsed,
                    'pre_observed_elapsed_from_start': pre_observed_elapsed,
                    'post_expected_elapsed_from_samples': post_expected_elapsed,
                    'post_observed_elapsed_from_start': post_observed_elapsed,
                    'pre_drift_sec': (pre_observed_elapsed - pre_expected_elapsed) if pre_observed_elapsed is not None and pre_expected_elapsed is not None else None,
                    'post_drift_sec': (post_observed_elapsed - post_expected_elapsed) if post_observed_elapsed is not None and post_expected_elapsed is not None else None,
                }
                gap_sec = out['pause_gap_metrics'].get('gap_start_sec')
                missing_est = None
                if gap_sec is not None and sr > 0:
                    # Estimate how many EEG samples are absent across the discontinuity.
                    missing_est = max(0, int(round(max(0.0, float(gap_sec) - (1.0 / float(sr))) * float(sr))))
                out['missing_samples_estimate'] = missing_est
                out['missing_frames_detected'] = bool(missing_est and missing_est > 0)
                assertions = {
                    'resume_monotonic_start': bool(out['phases']['eeg_post'].get('monotonic_start', False)),
                    'resume_monotonic_start_pc': bool(out['phases']['eeg_post'].get('monotonic_start_pc', False)),
                    'no_matrix_corruption': int(out['phases']['eeg_pre'].get('matrix_bad_count', 0)) == 0 and int(out['phases']['eeg_post'].get('matrix_bad_count', 0)) == 0,
                }
                if recipe == 'idle_pause':
                    assertions['idle_no_eeg_accumulation_expected'] = int(out['phases']['idle_pause'].get('frames_eeg', 0)) == 0
                else:
                    assertions['impedance_has_non_eeg'] = int(out['phases']['impedance_pause'].get('frames_non_eeg', 0)) > 0
                out['assertions'] = assertions
                out['pass'] = all(bool(v) for v in assertions.values())
                break
            except grpc.RpcError as e:
                text = f"{e.code().name}: {e.details()}"
                if attempt == 0 and 'amplifier with id' in text.lower():
                    try:
                        self.recreate_handle()
                        continue
                    except Exception as recover_err:
                        out['status'] = 'error'
                        out['error'] = f"{text}; recover failed: {recover_err}"
                        out['pass'] = False
                        break
                out['status'] = 'error'; out['error'] = text; out['pass'] = False
                break
            except Exception as e:
                out['status'] = 'error'; out['error'] = str(e); out['pass'] = False
                break
        return out

    def selected_recipes(self):
        return ['standard', 'idle_pause', 'impedance_pause'] if self.args.phase_recipe == 'all' else [self.args.phase_recipe]

    def combo_score(self, recipes):
        s = 0.0
        for r in recipes:
            if r.get('pass'): s += 1.0
            if r.get('status') == 'ok': s += 0.25
            if r.get('pause_gap_metrics', {}).get('classification') == 'expected_pause_gap': s += 0.25
        return s

    def run(self):
        summary = {'trace_id': self.trace, 'endpoint': self.args.endpoint, 'timestamp_unix': time.time(), 'recipes': self.selected_recipes(), 'combos': [], 'errors': []}
        if self.args.capture_pktmon:
            try:
                subprocess.run(['pktmon', 'stop'], capture_output=True, text=True)
                subprocess.run(['pktmon', 'filter', 'remove'], capture_output=True, text=True)
                subprocess.run(['pktmon', 'filter', 'add', '-p', str(self.args.endpoint_port)], check=True, capture_output=True, text=True)
                subprocess.run(['pktmon', 'start', '--etw', '--pkt-size', '0'], check=True, capture_output=True, text=True)
                self.pktmon_started = True
            except Exception as e:
                self.pktmon_error = f'pktmon start failed: {e}'
        cfg = autoprobe_winner(self.args, self.out)
        summary['setmode_winner'] = cfg.get('shape_name')
        try:
            dev = choose_dev(self.pb2, self.stub, self.args.serial_filter)
            self.selected_dev = dev
            summary['selected'] = {'key': dev.Key, 'serial': dev.Serial}
            cr = self.rpc('bootstrap', 'bootstrap', 'bootstrap', 'Controller_CreateDevice', self.stub.Controller_CreateDevice, self.pb2.Controller_CreateDeviceRequest(DeviceInfoList=[dev]))
            self.handle = int(cr.AmplifierHandle)
            ch = self.rpc('bootstrap', 'bootstrap', 'bootstrap', 'Amplifier_GetChannelsAvailable', self.stub.Amplifier_GetChannelsAvailable, self.pb2.Amplifier_GetChannelsAvailableRequest(AmplifierHandle=self.handle))
            rates = self.rpc('bootstrap', 'bootstrap', 'bootstrap', 'Amplifier_GetSamplingRatesAvailable', self.stub.Amplifier_GetSamplingRatesAvailable, self.pb2.Amplifier_GetSamplingRatesAvailableRequest(AmplifierHandle=self.handle))
            trig = [int(c.ChannelIndex) for c in ch.ChannelList if int(c.ChannelPolarity) in (int(self.pb2.Receiver), int(self.pb2.Transmitter)) or 'TRIGGER' in (c.Name or '').upper()]
            summary['trigger_candidates'] = trig
            avail = sorted(float(x) for x in rates.RateList)
            req_rates = parse_csv_float(self.args.sample_rates)
            sel_rates = [r for r in req_rates if r in avail] or ([avail[0]] if avail else [500.0])
            drs = parse_csv_int(self.args.data_ready_pcts)
            bss = parse_csv_int(self.args.buffer_sizes)
            idx = 0
            for sr in sel_rates:
                for dr in drs:
                    for bs in bss:
                        combo = f"combo_{idx:04d}_sr{int(sr)}_dr{dr}_bs{bs}"; idx += 1
                        rs = [self.run_recipe(combo, r, sr, dr, bs, cfg, trig) for r in self.selected_recipes()]
                        row = {'combo_id': combo, 'sampling_rate': sr, 'data_ready_percentage': dr, 'buffer_size': bs, 'recipes': rs, 'combo_score': self.combo_score(rs)}
                        self.combo_rows.append(row); summary['combos'].append(row)
        except Exception as e:
            summary['errors'].append(str(e))
        finally:
            if self.handle is not None:
                try:
                    self.set_mode('cleanup', 'cleanup', 'cleanup', 'Idle', self.pb2.AmplifierMode_Idle, {'active': None, 'ranges': None, 'stim': ''}, 500.0, 5, 1024)
                except Exception:
                    pass
                try:
                    self.rpc('cleanup', 'cleanup', 'cleanup', 'Amplifier_Dispose', self.stub.Amplifier_Dispose, self.pb2.Amplifier_DisposeRequest(AmplifierHandle=self.handle))
                except Exception:
                    pass
            self.write_artifacts(summary)
            if self.args.capture_pktmon:
                etl = self.out / 'pktmon.etl'
                txt = self.out / 'pktmon.txt'
                try:
                    if self.pktmon_started:
                        subprocess.run(['pktmon', 'stop'], check=True, capture_output=True, text=True)
                        default_etl = Path('PktMon.etl')
                        if default_etl.exists():
                            default_etl.replace(etl)
                        if etl.exists():
                            subprocess.run(['pktmon', 'format', str(etl), '-o', str(txt)], check=True, capture_output=True, text=True)
                except Exception as e:
                    self.pktmon_error = (self.pktmon_error + ' | ' if self.pktmon_error else '') + f'pktmon stop/format failed: {e}'
                summary['packet_capture'] = {
                    'enabled': True,
                    'started': self.pktmon_started,
                    'etl': str(etl),
                    'txt': str(txt),
                    'error': self.pktmon_error,
                }
                (self.out / 'summary.json').write_text(json.dumps(summary, indent=2), encoding='utf-8')
        return summary

    def write_artifacts(self, summary):
        rpc_file = self.out / 'rpc_log.jsonl'
        with rpc_file.open('w', encoding='utf-8') as f:
            for r in self.rpc_rows:
                f.write(json.dumps(r, ensure_ascii=False) + '\n')

        csv_file = self.out / 'frame_timing.csv'
        if self.frame_rows:
            with csv_file.open('w', newline='', encoding='utf-8') as f:
                w = csv.DictWriter(f, fieldnames=list(self.frame_rows[0].keys()))
                w.writeheader()
                for r in self.frame_rows:
                    w.writerow(r)
        else:
            csv_file.write_text('', encoding='utf-8')

        (self.out / 'combo_summaries.json').write_text(json.dumps(self.combo_rows, indent=2), encoding='utf-8')
        rank = sorted([
            {
                'combo_id': c['combo_id'], 'sampling_rate': c['sampling_rate'],
                'data_ready_percentage': c['data_ready_percentage'], 'buffer_size': c['buffer_size'],
                'combo_score': c['combo_score'],
                'all_recipes_pass': all(r.get('pass', False) for r in c.get('recipes', [])),
            }
            for c in self.combo_rows
        ], key=lambda x: (x['combo_score'], x['all_recipes_pass']), reverse=True)
        summary['parameter_stability_ranking'] = rank[:20]
        summary['pause_resume_robustness_ranking'] = rank[:20]
        summary['global_timestamp_alignment_recommendation'] = {
            'canonical_timeline': 'Start + i/sampling_rate',
            'transport_diagnostic_clock': 'StartPcTime',
            'discontinuity_policy': {'idle': 'expected gap; no backfill', 'impedance': 'expected gap for EEG timeline; no backfill'},
            'gui_pointer_policy': 'advance by true new sample count only (dedup overlap)',
        }
        (self.out / 'summary.json').write_text(json.dumps(summary, indent=2), encoding='utf-8')


def parser():
    p = argparse.ArgumentParser(description='EDX full-RPC timing probe and sweep runner')
    p.add_argument('--endpoint', default='localhost:3390')
    p.add_argument('--endpoint-port', type=int, default=3390)
    p.add_argument('--proto', default='src/board_controller/ant_neuro_edx/proto/EdigRPC.proto')
    p.add_argument('--serial-filter', default='')
    p.add_argument('--sample-rates', default='500,1000,2000')
    p.add_argument('--data-ready-pcts', default='1,2,5,10,20')
    p.add_argument('--buffer-sizes', default='32,64,128,256,512,1024')
    p.add_argument('--phase-recipe', choices=['standard', 'idle_pause', 'impedance_pause', 'all'], default='all')
    p.add_argument('--phase-seconds-eeg', type=int, default=5)
    p.add_argument('--phase-seconds-impedance-standard', type=int, default=3)
    p.add_argument('--pause-seconds', type=int, default=5)
    p.add_argument('--poll-sleep-sec', type=float, default=0.05)
    p.add_argument('--rpc-timeout-sec', type=float, default=6.0)
    p.add_argument('--capture-rpc-payloads', action='store_true', default=True)
    p.add_argument('--capture-rpc-bytes', action='store_true', default=False)
    p.add_argument('--capture-pktmon', action='store_true', default=False)
    p.add_argument('--trace-id', default='')
    p.add_argument('--out-dir', default='artifacts/edx-probe')
    return p


def main() -> int:
    args = parser().parse_args()
    pr = Probe(args)
    summary = pr.run()
    print(f"Wrote: {(Path(args.out_dir).resolve() / 'summary.json')}")
    print(f"Combos: {len(summary.get('combos', []))}")
    if summary.get('errors'):
        print('Errors:')
        for e in summary['errors']:
            print(f'  - {e}')
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
