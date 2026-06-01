"""Run the corruption × repair matrix.

For each corruption in corruptions.ALL_CORRUPTIONS, and for each repair
tier in repairs.ALL_REPAIRS:
    1. Copy /tmp/chroma_harness/baseline to a scratch dir
    2. Probe (should be healthy)
    3. Apply the corruption
    4. Probe (note the failure signature)
    5. Apply the repair
    6. Probe (note whether the repair healed it)
    7. Record the row

Output: stdout summary table + JSONL details to
    /tmp/chroma_harness/matrix.jsonl

Run: .venv/bin/python tests/repair_harness/run_matrix.py [--quick]
  --quick: skip Tier 3 (full re-embed) which is the slowest tier.
"""
from __future__ import annotations

import argparse
import json
import shutil
import time
from pathlib import Path

import corruptions
import probe
import repairs

BASELINE = Path("/tmp/chroma_harness/baseline")
SCRATCH_ROOT = Path("/tmp/chroma_harness/scratch")
RESULTS_PATH = Path("/tmp/chroma_harness/matrix.jsonl")


def reset_scratch(name: str) -> Path:
    scratch = SCRATCH_ROOT / name
    if scratch.exists():
        shutil.rmtree(scratch)
    SCRATCH_ROOT.mkdir(parents=True, exist_ok=True)
    shutil.copytree(BASELINE, scratch)
    return scratch / "chroma"


def short_probe(label: str, p: dict) -> str:
    if p["ok"]:
        return f"OK count={p['details'].get('count')}"
    if p["signal"]:
        return f"FAIL signal={p['signal']}"
    stage = p["details"].get("stage", "?") if isinstance(p["details"], dict) else "?"
    err = p["details"].get("type", "?") if isinstance(p["details"], dict) else "?"
    return f"FAIL stage={stage} err={err} exit={p['exit_code']}"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--quick", action="store_true")
    ap.add_argument("--only-corruption", help="Run only this corruption name")
    ap.add_argument("--only-repair", help="Run only this repair name")
    args = ap.parse_args()

    if not BASELINE.exists():
        raise SystemExit("Build baseline first: .venv/bin/python tests/repair_harness/build_baseline.py")

    RESULTS_PATH.unlink(missing_ok=True)
    rows = []

    corruption_fns = corruptions.ALL_CORRUPTIONS
    if args.only_corruption:
        corruption_fns = [f for f in corruption_fns if f.__name__ == args.only_corruption]
    repair_fns = repairs.ALL_REPAIRS
    if args.quick:
        repair_fns = [f for f in repair_fns if "full_reembed" not in f.__name__]
    if args.only_repair:
        repair_fns = [f for f in repair_fns if f.__name__ == args.only_repair]

    for corrupt_fn in corruption_fns:
        for repair_fn in repair_fns:
            name = f"{corrupt_fn.__name__}__via__{repair_fn.__name__}"
            print(f"\n=== {name} ===", flush=True)

            chroma_dir = reset_scratch(name)
            t0 = time.time()
            p_before = probe.probe_collection(chroma_dir)
            print(f"  pre-corrupt:  {short_probe('pre', p_before)}", flush=True)

            corrupt_desc = corrupt_fn(chroma_dir)
            p_corrupt = probe.probe_collection(chroma_dir)
            print(f"  post-corrupt: {short_probe('cor', p_corrupt)}  [{corrupt_desc}]", flush=True)

            repair_t0 = time.time()
            try:
                repair_desc = repair_fn(
                    chroma_dir,
                    collection_name="history",
                    src_dir=BASELINE.parent / "baseline" / "src",
                )
                repair_err = None
            except Exception as e:
                repair_desc = f"repair raised {type(e).__name__}: {e}"
                repair_err = repair_desc
            repair_dt = time.time() - repair_t0

            p_after = probe.probe_collection(chroma_dir)
            print(f"  post-repair:  {short_probe('aft', p_after)}  [{repair_desc}] dt={repair_dt:.1f}s", flush=True)

            healed = p_after["ok"] and (p_before["details"].get("count", 0) - p_after["details"].get("count", 0) <= 5)
            row = {
                "corruption": corrupt_fn.__name__,
                "repair": repair_fn.__name__,
                "pre": p_before,
                "post_corrupt": p_corrupt,
                "post_repair": p_after,
                "corrupt_desc": corrupt_desc,
                "repair_desc": repair_desc,
                "repair_err": repair_err,
                "repair_seconds": repair_dt,
                "healed": healed,
                "total_seconds": time.time() - t0,
            }
            rows.append(row)
            with RESULTS_PATH.open("a") as f:
                f.write(json.dumps(row) + "\n")

    # Summary table.
    print("\n\n=== MATRIX SUMMARY ===")
    repair_names = [f.__name__ for f in repair_fns]
    corruption_names = sorted({r["corruption"] for r in rows})
    col_w = max(len(c) for c in corruption_names) + 2
    h_w = max(len(r) for r in repair_names) + 2
    header = "Corruption".ljust(col_w) + "".join(r.ljust(h_w) for r in repair_names)
    print(header)
    print("-" * len(header))
    by_pair = {(r["corruption"], r["repair"]): r["healed"] for r in rows}
    for c in corruption_names:
        line = c.ljust(col_w)
        for r in repair_names:
            cell = "HEAL" if by_pair.get((c, r)) else "----"
            line += cell.ljust(h_w)
        print(line)

    print(f"\nDetails: {RESULTS_PATH}")


if __name__ == "__main__":
    main()
