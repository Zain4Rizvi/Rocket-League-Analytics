#!/usr/bin/env python3
"""
Rocket League Replay Processing Pipeline
=========================================

Sequentially executes:
1. Replay to CSV Parser.py (Parses .replay file into CSV and metadata JSON)
2. rl_replay_3d.py (Generates interactive 3D HTML visualization)

Usage:
    python run_pipeline.py <file_name>
    python run_pipeline.py my_replay.replay
    python run_pipeline.py
"""

import argparse
import subprocess
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
PARSER_SCRIPT = SCRIPT_DIR / "Replay to CSV Parser.py"
VISUALIZER_SCRIPT = SCRIPT_DIR / "rl_replay_3d.py"

def run_pipeline(replay_filename=None, output_filename=None):
    print("=" * 60)
    print("STARTING ROCKET LEAGUE REPLAY PIPELINE")
    print("=" * 60)

    # Step 1: Run Replay to CSV Parser
    cmd_parser = [sys.executable, str(PARSER_SCRIPT)]
    if replay_filename:
        cmd_parser.extend(["--input", str(replay_filename)])

    print(f"\n[1/2] Running Replay to CSV Parser...")
    print(f"Command: {' '.join(cmd_parser)}")
    res1 = subprocess.run(cmd_parser, check=False)

    if res1.returncode != 0:
        raise RuntimeError(
            f"Parser step failed with return code {res1.returncode}."
        )

    print("\n[+] Parser step completed successfully.")

    # Step 2: Run 3D Visualizer
    cmd_vis = [sys.executable, str(VISUALIZER_SCRIPT)]

    if replay_filename:
        csv_filename = f"{Path(replay_filename).stem}.csv"
        cmd_vis.extend(["-i", csv_filename])
    if output_filename:
        cmd_vis.extend(["-o", str(output_filename)])

    print(f"\n[2/2] Running 3D Visualizer (rl_replay_3d.py)...")
    print(f"Command: {' '.join(cmd_vis)}")

    res2 = subprocess.run(cmd_vis, check=False)

    if res2.returncode != 0:
        raise RuntimeError(
            f"Visualization step failed with return code {res2.returncode}."
        )

    print("\n[+] 3D Visualization generated successfully!")
    print("=" * 60)
    print("PIPELINE COMPLETE")
    print("=" * 60)

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Process a Rocket League replay.")
    parser.add_argument("-i", "--input", help="Replay filename or path.")
    parser.add_argument("-o", "--output", help="Generated HTML filename or path.")
    args = parser.parse_args()

    try:
        run_pipeline(args.input, args.output)
    except RuntimeError as error:
        print(f"\n[!] {error}")
        raise SystemExit(1) from error