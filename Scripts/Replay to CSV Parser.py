import argparse
import json
import subprocess
import sys
from pathlib import Path
import pandas as pd

# ============================================================
# Local Configurations & Constants
# ============================================================
# Directory setup (resolves paths relative to this script's directory)
SCRIPT_DIR = Path(__file__).resolve().parent.parent
BASE_DIR = SCRIPT_DIR  # Adjust if your base directory is a parent folder

REPLAY_FOLDER = BASE_DIR / "Replay Data" / "Raw Replays"
PARSED_CSV_FOLDER = BASE_DIR / "Replay Data" / "Parsed CSVs"
GAME_METADATA_FOLDER = BASE_DIR / "Replay Data" / "Game Metadata"
RROCKET_EXE = BASE_DIR / "Scripts" / "rrrocket.exe"

# Filtering threshold
MIN_PLAYER_ROWS = 100


def ensure_folders_exist():
    """Ensure output directories exist before writing files."""
    REPLAY_FOLDER.mkdir(parents=True, exist_ok=True)
    PARSED_CSV_FOLDER.mkdir(parents=True, exist_ok=True)
    GAME_METADATA_FOLDER.mkdir(parents=True, exist_ok=True)


# ============================================================
# Stage 1: In-Memory Replay Execution & Loading
# ============================================================

def parse_replay_to_json_in_memory(replay_path: Path) -> dict:
    """
    Executes rrrocket.exe against the provided .replay file and captures
    its stdout directly into memory as a JSON dictionary object.
    """
    if not RROCKET_EXE.exists():
        print(f"[!] rrrocket.exe not found at: {RROCKET_EXE}")
        sys.exit(1)

    try:
        result = subprocess.run(
            [str(RROCKET_EXE), "-n", str(replay_path)],
            capture_output=True,
            check=True,
            text=False  # Capture raw bytes to handle UTF-8/UTF-16 encoding safely
        )
    except subprocess.CalledProcessError as e:
        err_msg = e.stderr.decode("utf-8", errors="replace").strip() if e.stderr else str(e)
        raise RuntimeError(f"rrrocket failed: {err_msg}") from e

    raw = result.stdout

    # Handle encoding variations from rrrocket output
    if raw.startswith(b"\xff\xfe") or raw.startswith(b"\xfe\xff"):
        encoding = "utf-16"
    else:
        encoding = "utf-8-sig"

    text = raw.decode(encoding)

    try:
        return json.loads(text)
    except json.JSONDecodeError as e:
        trimmed = text.rstrip("\\ \t\r\n")
        if trimmed != text:
            try:
                return json.loads(trimmed)
            except json.JSONDecodeError:
                pass
        raise ValueError(f"Could not parse stdout from {replay_path.name} as JSON: {e}") from e


def extract_game_metadata(data: dict) -> dict:
    """
    Extracts team assignments, goal timing, and general match details 
    from the replay's top-level properties header.
    """
    props = data.get("properties", {})

    blue_team = []
    orange_team = []
    player_stats = []

    # Map team index: Team 0 = Blue, Team 1 = Orange
    for player in props.get("PlayerStats", []):
        p_name = player.get("Name")
        p_team = player.get("Team")

        if p_team == 0:
            blue_team.append(p_name)
        elif p_team == 1:
            orange_team.append(p_name)

        player_stats.append({
            "name": p_name,
            "team": "BLUE" if p_team == 0 else "ORANGE",
            "score": player.get("Score"),
            "goals": player.get("Goals"),
            "assists": player.get("Assists"),
            "saves": player.get("Saves"),
            "shots": player.get("Shots"),
            "bBot": player.get("bBot", False)
        })

    # Goals structure extraction
    goals_data = []
    for g in props.get("Goals", []):
        goals_data.append({
            "frame": g.get("frame"),
            "player_name": g.get("PlayerName"),
            "team": "BLUE" if g.get("PlayerTeam") == 0 else "ORANGE"
        })

    metadata = {
        "match_id": props.get("Id"),
        "match_guid": props.get("MatchGUID"),
        "date": props.get("Date"),
        "map_name": props.get("MapName"),
        "match_type": props.get("MatchType"),
        "team_scores": {
            "BLUE": props.get("Team0Score", 0),
            "ORANGE": props.get("Team1Score", 0)
        },
        "BLUE_TEAM": blue_team,
        "ORANGE_TEAM": orange_team,
        "goals": goals_data,
        "player_stats": player_stats
    }

    return metadata


# ============================================================
# Stage 2 (a): JSON Dict -> Tidy DataFrame
# ============================================================

def parse_replay_dict(data: dict) -> pd.DataFrame:
    """Returns None if network_frames is null (undecodable replay)."""
    if data.get("network_frames") is None:
        return None

    objects = data["objects"]
    frames = data["network_frames"]["frames"]

    actors = {}
    pri_names = {}
    car_to_pri = {}
    boost_comp_to_car = {}

    car_state = {}
    ball_state = {}

    rows = []

    def car_row(car_id, t):
        st = car_state.get(car_id, {})
        pri = car_to_pri.get(car_id)
        name = pri_names.get(pri, f"unknown_pri_{pri}")
        return {
            "time": t,
            "player": name,
            "actor_id": car_id,
            "pos_x": st.get("pos_x"), "pos_y": st.get("pos_y"), "pos_z": st.get("pos_z"),
            "vel_x": st.get("vel_x"), "vel_y": st.get("vel_y"), "vel_z": st.get("vel_z"),
            "boost": st.get("boost"),
            "throttle": st.get("throttle"),
            "steer": st.get("steer"),
        }

    for fr in frames:
        t = fr["time"]

        for na in fr["new_actors"]:
            actors[na["actor_id"]] = objects[na["object_id"]]

        for aid in fr["deleted_actors"]:
            actors.pop(aid, None)

        touched_cars = set()
        ball_touched = False

        for ua in fr["updated_actors"]:
            oname = objects[ua["object_id"]]
            attr = ua["attribute"]
            aid = ua["actor_id"]

            if oname == "Engine.PlayerReplicationInfo:PlayerName":
                pri_names[aid] = attr["String"]

            elif oname == "Engine.Pawn:PlayerReplicationInfo":
                car_to_pri[aid] = attr["ActiveActor"]["actor"]

            elif oname == "TAGame.CarComponent_TA:Vehicle":
                boost_comp_to_car[aid] = attr["ActiveActor"]["actor"]

            elif oname == "TAGame.RBActor_TA:ReplicatedRBState":
                rb = attr["RigidBody"]
                loc = rb["location"]
                vel = rb.get("linear_velocity")
                target = None
                if actors.get(aid) == "Archetypes.Car.Car_Default":
                    target = car_state.setdefault(aid, {})
                    touched_cars.add(aid)
                elif actors.get(aid) == "Archetypes.Ball.Ball_Default":
                    target = ball_state
                    ball_touched = True
                if target is not None:
                    target["pos_x"], target["pos_y"], target["pos_z"] = loc["x"], loc["y"], loc["z"]
                    if vel:
                        target["vel_x"], target["vel_y"], target["vel_z"] = vel["x"], vel["y"], vel["z"]

            elif oname == "TAGame.CarComponent_Boost_TA:ReplicatedBoost":
                car_id = boost_comp_to_car.get(aid)
                if car_id is not None:
                    st = car_state.setdefault(car_id, {})
                    st["boost"] = attr["ReplicatedBoost"]["boost_amount"]
                    touched_cars.add(car_id)

            elif oname == "TAGame.Vehicle_TA:ReplicatedThrottle":
                st = car_state.setdefault(aid, {})
                st["throttle"] = attr["Byte"]
                touched_cars.add(aid)

            elif oname == "TAGame.Vehicle_TA:ReplicatedSteer":
                st = car_state.setdefault(aid, {})
                st["steer"] = attr["Byte"]
                touched_cars.add(aid)

        for car_id in touched_cars:
            rows.append(car_row(car_id, t))

        if ball_touched:
            rows.append({
                "time": t, "player": "BALL", "actor_id": 0,
                "pos_x": ball_state.get("pos_x"), "pos_y": ball_state.get("pos_y"), "pos_z": ball_state.get("pos_z"),
                "vel_x": ball_state.get("vel_x"), "vel_y": ball_state.get("vel_y"), "vel_z": ball_state.get("vel_z"),
                "boost": None, "throttle": None, "steer": None,
            })

    return pd.DataFrame(rows)


# ============================================================
# Stage 2 (b): Gap-Fill / Complete DataFrame
# ============================================================

def complete_replay_data(df: pd.DataFrame, min_player_rows: int) -> pd.DataFrame:
    player_counts = df["player"].value_counts()
    valid_players = player_counts[player_counts >= min_player_rows].index

    df = df[
        df["player"].isin(valid_players) | (df["player"] == "BALL")
    ].copy()

    if df["player"].nunique() < 2:
        return pd.DataFrame()  # Nothing meaningful left (e.g. only BALL survived)

    df = df.sort_values(["player", "time"]).reset_index(drop=True)

    telemetry_columns = [
        "pos_x", "pos_y", "pos_z",
        "vel_x", "vel_y", "vel_z",
        "boost", "throttle", "steer",
    ]

    df[telemetry_columns] = df.groupby("player", sort=False)[telemetry_columns].ffill()

    first_appearance = df.groupby("player")["time"].min()
    trim_time = first_appearance.max()

    df = df[df["time"] >= trim_time].copy()

    times = df["time"].drop_duplicates().sort_values()
    players = df[["player", "actor_id"]].drop_duplicates("player")

    complete_index = pd.MultiIndex.from_product(
        [times, players["player"]], names=["time", "player"]
    )
    complete_df = complete_index.to_frame(index=False)
    complete_df = complete_df.merge(players, on="player", how="left")

    state = df[["time", "player"] + telemetry_columns]
    complete_df = complete_df.merge(state, on=["time", "player"], how="left")

    complete_df = complete_df.sort_values(["player", "time"]).reset_index(drop=True)
    complete_df[telemetry_columns] = (
        complete_df.groupby("player", sort=False)[telemetry_columns].ffill()
    )

    complete_df = complete_df[
        ["time", "player", "actor_id"] + telemetry_columns
    ]
    complete_df = complete_df.sort_values(["time", "actor_id"]).reset_index(drop=True)

    return complete_df


# ============================================================
# Core Pipeline Execution
# ============================================================

def process_replay_in_memory(replay_path: Path, min_player_rows: int):
    """
    Runs Stage 1 + Stage 2 in-memory.
    Returns (status, complete_df, game_metadata)
    """
    json_data = parse_replay_to_json_in_memory(replay_path)
    metadata = extract_game_metadata(json_data)
    raw_df = parse_replay_dict(json_data)

    if raw_df is None:
        return "undecodable", None, None

    if raw_df.empty:
        return "no_rows", None, None

    complete_df = complete_replay_data(raw_df, min_player_rows)

    if complete_df.empty:
        return "too_few_players", None, None

    return "ok", complete_df, metadata


def resolve_replay_file(input_arg: str = None) -> Path:
    """Resolves the replay file based on Mode 1 or Mode 2."""
    ensure_folders_exist()

    if not REPLAY_FOLDER.exists():
        print(f"[!] REPLAY_FOLDER does not exist: {REPLAY_FOLDER}")
        sys.exit(1)

    if input_arg:
        # Mode 1: User specified a filename or path
        candidate = Path(input_arg)
        if not candidate.is_absolute():
            if (REPLAY_FOLDER / candidate).exists():
                candidate = REPLAY_FOLDER / candidate
            elif (SCRIPT_DIR / candidate).exists():
                candidate = SCRIPT_DIR / candidate
            else:
                candidate = Path.cwd() / candidate

        if not candidate.exists():
            print(f"[!] Input replay file not found: {input_arg}")
            sys.exit(1)
        return candidate
    else:
        # Mode 2: No file specified; pick one from REPLAY_FOLDER
        replay_files = sorted(REPLAY_FOLDER.glob("*.replay"))
        if not replay_files:
            print(f"[!] No .replay files found in: {REPLAY_FOLDER}")
            sys.exit(1)
        return replay_files[0]


def main():
    parser = argparse.ArgumentParser(description="Process .replay directly to completed CSV and JSON metadata.")
    parser.add_argument(
        "--input",
        type=str,
        default=None,
        help="Specific .replay file name or path. If omitted, picks one file from REPLAY_FOLDER."
    )
    parser.add_argument(
        "--min-player-rows",
        type=int,
        default=MIN_PLAYER_ROWS,
        help="Minimum rows a player needs to be retained."
    )
    args = parser.parse_args()

    replay_path = resolve_replay_file(args.input)
    output_csv_path = PARSED_CSV_FOLDER / (replay_path.stem + ".csv")
    output_json_path = GAME_METADATA_FOLDER / (replay_path.stem + ".json")

    print(f"Processing: {replay_path.name} ...", end=" ")

    try:
        status, complete_df, metadata = process_replay_in_memory(
            replay_path, min_player_rows=args.min_player_rows
        )
    except Exception as e:
        print("FAILED")
        print(f"Error: {e}")
        sys.exit(1)

    if status == "undecodable":
        print("SKIPPED (network_frames is null -- undecodable replay)")
        sys.exit(1)
    elif status == "no_rows":
        print("SKIPPED (no telemetry rows generated)")
        sys.exit(1)
    elif status == "too_few_players":
        print("SKIPPED (fewer than 2 valid players after filtering)")
        sys.exit(1)

    # Save CSV
    complete_df.to_csv(output_csv_path, index=False)
    
    # Save JSON Metadata
    with open(output_json_path, "w", encoding="utf-8") as f:
        json.dump(metadata, f, indent=2)

    print(f"done ({len(complete_df):,} rows)")
    print(f"Wrote CSV to: {output_csv_path}")
    print(f"Wrote Game Metadata to: {output_json_path}")


if __name__ == "__main__":
    main()