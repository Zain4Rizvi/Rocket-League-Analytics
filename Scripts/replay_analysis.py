#!/usr/bin/env python3
"""
Deterministic replay analysis
=============================

Every number the AI coaching layer reports is computed here, in ordinary Python.
This module has no LLM dependencies and can be imported and tested without an
API key.

Each analysis returns a pair:

    (llm_payload, display_payload)

`llm_payload` is a small dict of scalars that goes into the model's context.
`display_payload` holds downsampled series for frontend charts and never
reaches the model.

Replays are addressed by their *stem* - the shared filename of
`Replay Data/Parsed CSVs/<stem>.csv` and `Replay Data/Game Metadata/<stem>.json`.
"""

import json
import threading
from functools import lru_cache
from pathlib import Path

import numpy as np
import pandas as pd

import rl_replay_3d

BASE_DIR = Path(__file__).resolve().parent.parent
CSV_DIR = BASE_DIR / "Replay Data" / "Parsed CSVs"
METADATA_DIR = BASE_DIR / "Replay Data" / "Game Metadata"

FIELD_LENGTH = rl_replay_3d.FIELD_LENGTH          # 10240, y in [-5120, 5120]
THIRD = FIELD_LENGTH / 6.0                        # +/- 1706.67 marks the thirds
GOAL_LINE = FIELD_LENGTH / 2.0                    # 5120

# A row represents the interval until the next sample. Replays contain long
# dead periods (goal celebrations, replays) where actors are deleted; the
# parser forward-fills across them, so those rows describe frozen cars rather
# than play. Anything longer than this is excluded from time-weighted metrics.
MAX_SAMPLE_GAP = 0.2

BOOST_MAX_RAW = 255.0                             # CSV boost is 0-255, not 0-100
SUPERSONIC = 2200.0                               # uu/s
UU_PER_S_TO_KPH = 0.036                           # 1 uu == 1 cm

# BLUE defends y = -5120 and attacks +y; ORANGE is the mirror. Verified against
# goal-line crossings on the sample replay: all four BLUE goals cross +y and all
# three ORANGE goals cross -y.
ATTACK_SIGN = {"BLUE": 1.0, "ORANGE": -1.0}

# rl_replay_3d keeps the team rosters in module globals that load_team_setup
# mutates, so the load + build_payload pair has to be atomic across threads.
_ANALYTICS_LOCK = threading.Lock()


class ReplayNotFound(Exception):
    """The stem has no parsed CSV or metadata JSON on disk."""


def resolve_paths(stem):
    """Resolve a stem to its CSV and metadata paths, refusing path escapes."""
    safe = Path(str(stem)).name
    csv_path = (CSV_DIR / f"{safe}.csv").resolve()
    json_path = (METADATA_DIR / f"{safe}.json").resolve()
    if csv_path.parent != CSV_DIR.resolve() or json_path.parent != METADATA_DIR.resolve():
        raise ReplayNotFound(f"Invalid replay id: {stem}")
    if not csv_path.exists() or not json_path.exists():
        raise ReplayNotFound(
            f"No parsed data for replay '{safe}'. It may need to be re-uploaded."
        )
    return csv_path, json_path


class ReplayContext:
    """Loaded telemetry and metadata for one replay, with derived columns."""

    def __init__(self, stem, df, metadata):
        self.stem = stem
        self.metadata = metadata
        self.t0 = float(df["time"].min())

        df = df.copy()
        # Viewer-relative time. The viewer's simTime starts at 0 on the first
        # sample, so every timestamp this module emits is normalised here, once,
        # rather than in each analysis.
        df["t"] = df["time"] - self.t0
        df["boost_pct"] = df["boost"] / BOOST_MAX_RAW * 100.0
        df["speed"] = np.sqrt(
            df["vel_x"].fillna(0.0) ** 2
            + df["vel_y"].fillna(0.0) ** 2
            + df["vel_z"].fillna(0.0) ** 2
        )
        self.df = df

        self.team_of = {}
        for name in metadata.get("BLUE_TEAM", []):
            self.team_of[name] = "BLUE"
        for name in metadata.get("ORANGE_TEAM", []):
            self.team_of[name] = "ORANGE"

        self.players = [
            name for name in df["player"].unique()
            if name != "BALL" and name in self.team_of
        ]
        self.duration = float(df["t"].max())
        self._analytics = None

    @property
    def ball(self):
        return self.df[self.df["player"] == "BALL"].sort_values("t")

    def frame_of(self, name):
        return self.df[self.df["player"] == name].sort_values("t")

    @property
    def analytics(self):
        """The existing coach-console analytics block, computed on demand.

        Reused so AI answers agree with what the viewer's coach panel shows.
        Held behind a lock because rl_replay_3d resolves teams through module
        globals.
        """
        if self._analytics is None:
            _, json_path = resolve_paths(self.stem)
            with _ANALYTICS_LOCK:
                rl_replay_3d.load_team_setup(json_path)
                payload = rl_replay_3d.build_payload(
                    self.df, 30, self.metadata
                )
            self._analytics = payload.get("analytics", {})
        return self._analytics

    def resolve_player(self, name):
        """Match a player name case-insensitively, or raise with the roster."""
        if not name:
            return None
        for candidate in self.players:
            if candidate.lower() == str(name).lower():
                return candidate
        raise ValueError(
            f"Unknown player '{name}'. This match has: {', '.join(self.players)}"
        )


@lru_cache(maxsize=2)
def load_context(stem):
    """Load a replay by stem. Cached - replays are immutable once parsed."""
    csv_path, json_path = resolve_paths(stem)
    df = rl_replay_3d.load_data(csv_path)
    with open(json_path, "r", encoding="utf-8") as handle:
        metadata = json.load(handle)
    return ReplayContext(Path(csv_path).stem, df, metadata)


# ============================================================
# Time weighting
# ============================================================

def sample_weights(times):
    """Seconds each sample represents, with dead-time gaps zeroed out."""
    times = np.asarray(times, dtype=float)
    if len(times) < 2:
        return np.zeros(len(times))
    gaps = np.diff(times)
    weights = np.append(gaps, np.median(gaps))
    weights[weights > MAX_SAMPLE_GAP] = 0.0
    return weights


def weighted_pct(mask, weights):
    total = weights.sum()
    if total <= 0:
        return 0.0
    return round(float(np.asarray(mask, dtype=float) @ weights / total * 100.0), 1)


def weighted_mean(values, weights):
    total = weights.sum()
    if total <= 0:
        return 0.0
    values = np.nan_to_num(np.asarray(values, dtype=float))
    return round(float(values @ weights / total), 1)


def downsample(times, values, limit=120):
    """Thin a series to at most `limit` points for charting."""
    times = np.asarray(times, dtype=float)
    values = np.nan_to_num(np.asarray(values, dtype=float))
    if len(times) > limit:
        idx = np.linspace(0, len(times) - 1, limit).astype(int)
        times, values = times[idx], values[idx]
    return [[round(float(t), 1), round(float(v), 1)] for t, v in zip(times, values)]


# ============================================================
# Goal timeline
# ============================================================

def goal_timeline(context):
    """Goal times derived from ball goal-line crossings.

    The replay header stores a frame index per goal, but replay frames are not
    uniformly spaced - dead time between goals stretches them - so the
    `frame / 30` conversion used elsewhere drifts by tens of seconds over a
    match. Detecting when the ball actually crosses |y| > 5120 gives the true
    time, and the sign of the crossing independently confirms who scored.

    Falls back to the frame estimate (flagged `approximate`) if the crossing
    count or team order disagrees with the header, which can happen with own
    goals.
    """
    goals = context.metadata.get("goals", [])
    ball = context.ball
    times = ball["t"].to_numpy()
    y = ball["pos_y"].to_numpy()

    inside = np.abs(y) > GOAL_LINE
    entries = np.where(inside & ~np.r_[False, inside[:-1]])[0]

    crossings = [
        {"time": round(float(times[i]), 1),
         "team": "BLUE" if y[i] > 0 else "ORANGE"}
        for i in entries
    ]

    usable = len(crossings) == len(goals) and all(
        crossing["team"] == goal.get("team")
        for crossing, goal in zip(crossings, goals)
    )

    timeline = []
    for index, goal in enumerate(goals):
        if usable:
            time_s, approximate = crossings[index]["time"], False
        else:
            time_s = round(float(goal.get("frame") or 0) / 30.0 - context.t0, 1)
            approximate = True
        timeline.append({
            "time": max(0.0, time_s),
            "team": goal.get("team"),
            "scorer": goal.get("player_name"),
            "approximate": approximate,
        })
    return timeline


# ============================================================
# Analyses
# ============================================================

def match_summary(stem):
    """Official match result and roster, straight from the replay header."""
    context = load_context(stem)
    metadata = context.metadata

    players = []
    for entry in metadata.get("player_stats", []):
        players.append({
            "name": entry.get("name"),
            "team": entry.get("team"),
            "goals": entry.get("goals"),
            "assists": entry.get("assists"),
            "saves": entry.get("saves"),
            "shots": entry.get("shots"),
            "score": entry.get("score"),
            "is_bot": bool(entry.get("bBot")),
        })

    timeline = goal_timeline(context)
    llm = {
        "map": metadata.get("map_name"),
        "match_type": metadata.get("match_type"),
        "duration_s": round(context.duration, 1),
        "score": metadata.get("team_scores"),
        "teams": {
            "BLUE": metadata.get("BLUE_TEAM", []),
            "ORANGE": metadata.get("ORANGE_TEAM", []),
        },
        "players": players,
        "goals": timeline,
        "source": "replay header (official stats, not estimated)",
    }
    display = {
        "kind": "moments",
        "title": "Goals",
        "items": [
            {"time": goal["time"],
             "label": f"{goal['team']} goal - {goal['scorer']}"}
            for goal in timeline
        ],
    }
    return llm, display


def boost_report(stem, player=None):
    """Boost economy per player, time-weighted and excluding dead time."""
    context = load_context(stem)
    target = context.resolve_player(player)
    names = [target] if target else context.players

    rows, series = [], []
    coverage = []
    for name in names:
        frame = context.frame_of(name)
        times = frame["t"].to_numpy()
        boost = frame["boost_pct"].to_numpy()
        weights = sample_weights(times)
        span = times[-1] - times[0] if len(times) > 1 else 0.0
        if span > 0:
            coverage.append(weights.sum() / span * 100.0)

        # A depletion episode is a transition from having boost to having none.
        empty = boost <= 1.0
        episodes = int(np.sum(empty & ~np.r_[False, empty[:-1]]))

        rows.append({
            "name": name,
            "team": context.team_of.get(name),
            "average": weighted_mean(boost, weights),
            "pct_below_20": weighted_pct(boost < 20, weights),
            "pct_below_30": weighted_pct(boost < 30, weights),
            "pct_at_zero": weighted_pct(empty, weights),
            "pct_at_100": weighted_pct(boost >= 99, weights),
            "depletion_episodes": episodes,
        })
        series.append({"label": name, "team": context.team_of.get(name),
                       "points": downsample(times, boost)})

    llm = {
        "unit": "percent of a full tank (0-100)",
        "coverage_pct": round(float(np.mean(coverage)), 1) if coverage else 0.0,
        "players": rows,
        "note": "pct_at_100 is time spent at a full tank, where further pickups are wasted",
    }
    display = {"kind": "line", "title": "Boost over time", "y_label": "Boost %",
               "y_max": 100, "series": series}
    return llm, display


def positioning_report(stem, player=None):
    """Field position, speed and distance-rank ordering per player."""
    context = load_context(stem)
    target = context.resolve_player(player)
    names = [target] if target else context.players

    ball = context.ball
    ball_times = ball["t"].to_numpy()
    ball_y = ball["pos_y"].to_numpy()
    ball_x = ball["pos_x"].to_numpy()

    # Distance-to-ball per player on a shared time base, so teammates can be
    # ranked against each other at the same instant.
    distances, per_player = {}, {}
    for name in context.players:
        frame = context.frame_of(name)
        times = frame["t"].to_numpy()
        x = np.interp(times, ball_times, ball_x)
        y = np.interp(times, ball_times, ball_y)
        per_player[name] = (frame, times, x, y)
        distances[name] = np.hypot(frame["pos_x"].to_numpy() - x,
                                   frame["pos_y"].to_numpy() - y)

    reference = context.frame_of(context.players[0])["t"].to_numpy()
    aligned = {
        name: np.interp(reference, per_player[name][1], distances[name])
        for name in context.players
    }

    rows = []
    for name in names:
        frame, times, ball_x_at, ball_y_at = per_player[name]
        team = context.team_of.get(name)
        sign = ATTACK_SIGN.get(team, 1.0)
        weights = sample_weights(times)

        y = frame["pos_y"].to_numpy()
        progress = sign * y                      # positive means upfield
        behind_ball = sign * (ball_y_at - y) > 0
        speed = frame["speed"].to_numpy()

        teammates = [other for other in context.players
                     if context.team_of.get(other) == team]
        own = np.interp(reference, times, distances[name])
        rank = np.ones(len(reference))
        for other in teammates:
            if other != name:
                rank += (aligned[other] < own).astype(float)
        rank_weights = sample_weights(reference)

        # Nearest-teammate spacing, a rough read on whether a team is stacked.
        spacing = None
        if len(teammates) > 1:
            gaps = []
            for other in teammates:
                if other == name:
                    continue
                other_frame = context.frame_of(other)
                ox = np.interp(times, other_frame["t"].to_numpy(),
                               other_frame["pos_x"].to_numpy())
                oy = np.interp(times, other_frame["t"].to_numpy(),
                               other_frame["pos_y"].to_numpy())
                gaps.append(np.hypot(frame["pos_x"].to_numpy() - ox, y - oy))
            spacing = weighted_mean(np.min(np.vstack(gaps), axis=0), weights)

        rows.append({
            "name": name,
            "team": team,
            "pct_defensive_third": weighted_pct(progress < -THIRD, weights),
            "pct_middle_third": weighted_pct(np.abs(progress) <= THIRD, weights),
            "pct_offensive_third": weighted_pct(progress > THIRD, weights),
            "pct_behind_ball": weighted_pct(behind_ball, weights),
            "avg_distance_to_ball": weighted_mean(distances[name], weights),
            "avg_nearest_teammate_distance": spacing,
            "first_man_pct": weighted_pct(rank == 1, rank_weights),
            "second_man_pct": weighted_pct(rank == 2, rank_weights),
            "third_man_pct": weighted_pct(rank >= 3, rank_weights),
            "avg_speed_kph": round(weighted_mean(speed, weights) * UU_PER_S_TO_KPH, 1),
            "pct_supersonic": weighted_pct(speed >= SUPERSONIC, weights),
            "pct_airborne": weighted_pct(frame["pos_z"].to_numpy() > 300, weights),
        })

    llm = {
        "units": "distances in unreal units (uu); the field is 10240uu long",
        "players": rows,
        "note": (
            "first/second/third man is a distance-to-ball rank proxy, not true "
            "rotation; pct_airborne includes wall driving; this replay has no "
            "ball-touch data, so possession and challenges cannot be measured"
        ),
    }
    display = {
        "kind": "bars",
        "title": "Time by third of the field",
        "categories": ["Defensive", "Middle", "Offensive"],
        "series": [
            {"label": row["name"], "team": row["team"],
             "values": [row["pct_defensive_third"], row["pct_middle_third"],
                        row["pct_offensive_third"]]}
            for row in rows
        ],
    }
    return llm, display


def key_moments(stem, start=None, end=None):
    """Goals plus the coach console's threat and transition events."""
    context = load_context(stem)
    events = [
        {"time": goal["time"], "kind": "GOAL", "team": goal["team"],
         "detail": f"{goal['team']} goal scored by {goal['scorer']}"}
        for goal in goal_timeline(context)
    ]
    ball = context.ball
    ball_times = ball["t"].to_numpy()
    ball_y = ball["pos_y"].to_numpy()
    ball_speed = ball["speed"].to_numpy()
    last_threat = -99.0

    for event in context.analytics.get("events", []):
        kind = event.get("kind")
        time_s = float(event.get("time") or 0.0)
        if kind == "GOAL":
            continue  # replaced by the corrected timeline above

        index = int(np.argmin(np.abs(ball_times - time_s)))
        if kind == "THREAT":
            # The coaching model emits a threat sample twice a second, so
            # collapse runs into one moment.
            if time_s - last_threat < 4.0:
                continue
            last_threat = time_s
            # Its stored team label is unreliable, so read the pressure off the
            # ball instead: BLUE attacks +y, ORANGE attacks -y.
            team = "BLUE" if ball_y[index] > 0 else "ORANGE"
            detail = f"{team} pressure near the opposition goal"
        else:
            team = event.get("team")
            speed_kph = float(ball_speed[index]) * UU_PER_S_TO_KPH
            detail = f"Fast ball movement ({speed_kph:.0f} km/h)"

        events.append({"time": round(time_s, 1), "kind": kind,
                       "team": team, "detail": detail})

    if start is not None:
        events = [event for event in events if event["time"] >= float(start)]
    if end is not None:
        events = [event for event in events if event["time"] <= float(end)]
    events.sort(key=lambda event: event["time"])
    events = events[:20]

    llm = {
        "window": {"start": start, "end": end},
        "events": events,
        "note": (
            "GOAL times are measured from the ball crossing the goal line. "
            "THREAT and TRANSITION are heuristic windows from the replay's "
            "coaching model, not officially recorded events."
        ),
    }
    display = {
        "kind": "moments",
        "title": "Key moments",
        "items": [{"time": event["time"], "label": event["detail"]}
                  for event in events],
    }
    return llm, display


ANALYSES = {
    "match_summary": match_summary,
    "boost_report": boost_report,
    "positioning_report": positioning_report,
    "key_moments": key_moments,
}


if __name__ == "__main__":
    import sys

    stem = sys.argv[1] if len(sys.argv) > 1 else None
    if not stem:
        candidates = sorted(CSV_DIR.glob("*.csv"))
        if not candidates:
            raise SystemExit(f"No parsed replays in {CSV_DIR}")
        stem = candidates[0].stem

    for name, function in ANALYSES.items():
        llm, _ = function(stem)
        print(f"\n===== {name} =====")
        print(json.dumps(llm, indent=2)[:2000])
