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

import sys
import subprocess
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
PARSER_SCRIPT = SCRIPT_DIR / "Replay to CSV Parser.py"
VISUALIZER_SCRIPT = SCRIPT_DIR / "rl_replay_3d.py"

def run_pipeline(replay_filename=None):
    print("=" * 60)
    print("STARTING ROCKET LEAGUE REPLAY PIPELINE")
    print("=" * 60)

    # Step 1: Run Replay to CSV Parser
    cmd_parser = [sys.executable, str(PARSER_SCRIPT)]
    if replay_filename:
        cmd_parser.extend(["--input", str(replay_filename)])

    print(f"\n[1/2] Running Replay to CSV Parser...")
    print(f"Command: {' '.join(cmd_parser)}")
    res1 = subprocess.run(cmd_parser)

    if res1.returncode != 0:
        print(f"\n[!] Parser step failed with return code {res1.returncode}. Aborting pipeline.")
        sys.exit(res1.returncode)

    print("\n[+] Parser step completed successfully.")

# Step 2: Run 3D Visualizer
    cmd_vis = [sys.executable, str(VISUALIZER_SCRIPT)]

    if replay_filename:
        csv_filename = f"{Path(replay_filename).stem}.csv"
        cmd_vis.extend(["-i", csv_filename])

    print(f"\n[2/2] Running 3D Visualizer (rl_replay_3d.py)...")
    print(f"Command: {' '.join(cmd_vis)}")

    res2 = subprocess.run(cmd_vis)

    if res2.returncode != 0:
        print(f"\n[!] Visualization step failed with return code {res2.returncode}.")
        sys.exit(res2.returncode)

    print("\n[+] 3D Visualization generated successfully!")
    print("=" * 60)
    print("PIPELINE COMPLETE")
    print("=" * 60)

if __name__ == "__main__":
    replay_file = sys.argv[1] if len(sys.argv) > 1 else None
    run_pipeline(replay_file)