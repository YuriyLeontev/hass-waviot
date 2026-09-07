#!/usr/bin/env python3
"""WAVIoT discovery: shows modem info and which channels return data.

Usage:
    python3 waviot_discover.py --key <API> --modem <MODEM>
    python3 waviot_discover.py --key ... --modem ... --raw   # dump raw get_values JSON

Only stdlib is used, no pip packages needed.
"""
import argparse
import json
import sys
import time
import urllib.parse
import urllib.request

BASE = "https://lk.waviot.ru"

CANDIDATE_CHANNELS = [
    "electro_ac_p_lsum",
    "electro_ac_p_lsum_t1",
    "electro_ac_p_lsum_t2",
    "electro_ac_p_lsum_t3",
    "electro_ac_p_lsum_t4",
    "electro_ac_p_lsum_tsum",
]


def call(api_type: str, method: str, key: str, **params):
    params["key"] = key
    qs = urllib.parse.urlencode({k: v for k, v in params.items() if v is not None})
    url = f"{BASE}/api.{api_type}/{method}/?{qs}"
    with urllib.request.urlopen(url, timeout=30) as resp:
        return json.loads(resp.read().decode())


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--key", required=True, help="API key from lk.waviot.ru")
    ap.add_argument("--modem", required=True, help="HEX modem id, e.g. ABCDEF")
    ap.add_argument("--days", type=int, default=90, help="lookback window")
    ap.add_argument("--raw", action="store_true", help="dump raw get_values JSON")
    args = ap.parse_args()

    now = int(time.time())
    frm = now - args.days * 86400

    print("=== modem/info ===")
    info = call("modem", "info", args.key, id=args.modem)
    modem = info.get("modem") or {}
    print(json.dumps(modem, indent=2, ensure_ascii=False))

    print("\n=== data/get_values (registrators) ===")
    gv = call("data", "get_values", args.key, modem_id=args.modem, **{"from": frm, "to": now})
    if args.raw:
        print(json.dumps(gv, indent=2, ensure_ascii=False))
    else:
        regs = gv.get("registrators")
        if isinstance(regs, dict):
            for reg_id, reg in regs.items():
                if isinstance(reg, dict):
                    keys = {k: type(v).__name__ for k, v in reg.items()}
                    print(f"registrator {reg_id}: {keys}")
                    if "channel" in reg:
                        print(f"  channel = {reg['channel']}")
        else:
            print(f"registrators type: {type(regs).__name__} (use --raw to inspect)")

    print("\n=== channel probe ===")
    for ch in CANDIDATE_CHANNELS:
        try:
            data = call(
                "data", "get_modem_channel_values", args.key,
                modem_id=args.modem, channel=ch, **{"from": frm, "to": now},
            )
            values = data.get("values") or {}
            if values:
                stamps = sorted(int(t) for t in values)
                first_ts, last_ts = stamps[0], stamps[-1]
                last_val = values[str(last_ts)] if isinstance(values, dict) else None
                fmt = lambda t: time.strftime("%Y-%m-%d %H:%M", time.localtime(t))
                span_days = (last_ts - first_ts) / 86400
                # Median gap tells apart "the server kept N years" from
                # "the meter has only been reporting for a few months".
                gaps = sorted(b - a for a, b in zip(stamps, stamps[1:]))
                step_h = (gaps[len(gaps) // 2] / 3600) if gaps else 0
                print(f"  {ch:28s} {len(stamps):5d} points, last: {last_val} @ {fmt(last_ts)}")
                print(
                    f"  {'':28s} first: {fmt(first_ts)}, span: {span_days:.0f} d, "
                    f"median step: {step_h:.1f} h"
                )
            else:
                print(f"  {ch:28s} no data")
        except Exception as exc:  # noqa: BLE001
            print(f"  {ch:28s} error: {exc}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
