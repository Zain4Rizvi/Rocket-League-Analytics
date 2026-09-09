#!/usr/bin/env python3

"""
Rocket League Replay Visualizer - 3D Interactive
==================================================

Reads a parsed Rocket League replay CSV and generates a single self-contained
HTML file with an interactive, real-time 3D playback of the match.

Features:
- Interactive orbit camera
- Play/pause, scrubbing, speed control
- Ball-follow camera & Live boost HUD
- Fading player trails & Team-based player colors
- Team coverage tetrahedrons
- NEW: Toggleable Team Centroid + Ball Displacement Lines
- NEW: Toggleable Real-Time Spatial Team Pressure Field Grid
- NEW: 3D Arena Boundaries with Ceiling & Rounded Rocket League Corners

Expected CSV columns:
    time,player,actor_id,pos_x,pos_y,pos_z,vel_x,vel_y,vel_z,boost,throttle,steer

Only time / player / pos_x / pos_y / pos_z are required.

Usage:
    python rl_replay_3d.py
    python rl_replay_3d.py -i input.csv -o output.html
    python rl_replay_3d.py -i input.csv -o output.html --fps 30

If -i is omitted, the first CSV in:
    base_dir / "Replay Data" / "Parsed CSVs"
is used, along with the JSON file with the same name.
"""

import argparse
import json
import math
import os
import urllib.request
from pathlib import Path

import numpy as np
import pandas as pd


# ============================================================
# Rocket League field dimensions
# ============================================================

FIELD_WIDTH = 8192          # x axis: -4096 .. 4096
FIELD_LENGTH = 10240        # y axis: -5120 .. 5120
FIELD_HEIGHT = 2048         # z axis: 0 .. 2048
CORNER_RADIUS = 1152        # Standard RL rounded corner radius

GOAL_WIDTH = 1786
GOAL_DEPTH = 880
GOAL_HEIGHT = 642.75

BALL_RADIUS = 92.75

CAR_LENGTH = 120.0
CAR_WIDTH = 84.0
CAR_HEIGHT = 38.0


# ============================================================
# Team definitions
# ============================================================

SCRIPT_DIR = Path(__file__).resolve().parent
BASE_DIR = SCRIPT_DIR.parent
REPLAY_DATA_DIR = BASE_DIR / "Replay Data" / "Parsed CSVs"

ORANGE_COLOR = "#ff7a00"
BLUE_COLOR = "#2f80ed"
BALL_COLOR = "#f2c14e"

ORANGE_TEAM = []
BLUE_TEAM = []


def get_team(player_name):
    if player_name in ORANGE_TEAM:
        return "orange"
    if player_name in BLUE_TEAM:
        return "blue"
    return None


def get_player_color(player_name):
    team = get_team(player_name)
    if team == "orange":
        return ORANGE_COLOR
    if team == "blue":
        return BLUE_COLOR
    return "#aaaaaa"


# ============================================================
# Data loading & Resampling
# ============================================================

def load_team_setup(json_path):
    global ORANGE_TEAM, BLUE_TEAM

    with open(json_path, "r", encoding="utf-8") as f:
        data = json.load(f)

    ORANGE_TEAM = data.get("ORANGE_TEAM", [])
    BLUE_TEAM = data.get("BLUE_TEAM", [])

    if not ORANGE_TEAM and not BLUE_TEAM:
        raise ValueError(
            f"JSON file '{json_path}' does not contain ORANGE_TEAM or BLUE_TEAM."
        )


def find_default_csv():
    csv_files = sorted(REPLAY_DATA_DIR.glob("*.csv"))

    if not csv_files:
        raise FileNotFoundError(
            f"No CSV files found in '{REPLAY_DATA_DIR}'."
        )

    return csv_files[0]


def load_data(csv_path):
    df = pd.read_csv(csv_path)

    required = {"time", "player", "pos_x", "pos_y"}
    missing = required - set(df.columns)

    if missing:
        raise ValueError(
            f"CSV is missing required columns: {sorted(missing)}"
        )

    if "pos_z" not in df.columns:
        df["pos_z"] = 0.0

    df = df.dropna(subset=["pos_x", "pos_y"]).copy()
    df["time"] = df["time"].astype(float)
    df["pos_z"] = df["pos_z"].fillna(0.0)

    if df.empty:
        raise ValueError("No rows with valid pos_x/pos_y found in the CSV.")

    return df


def resample_entity(sub, grid):
    t = sub["time"].to_numpy()
    x = sub["pos_x"].to_numpy()
    y = sub["pos_y"].to_numpy()
    z = sub["pos_z"].to_numpy()

    fx = np.interp(grid, t, x, left=x[0], right=x[-1])
    fy = np.interp(grid, t, y, left=y[0], right=y[-1])
    fz = np.interp(grid, t, z, left=z[0], right=z[-1])

    fb = None
    if "boost" in sub.columns and sub["boost"].notna().any():
        b = sub["boost"].ffill().bfill().to_numpy()
        fb = np.interp(grid, t, b, left=b[0], right=b[-1])

    return fx, fy, fz, fb


def compute_yaw(fx, fy, min_speed=4.0):
    n = len(fx)
    yaw = np.zeros(n)
    last = 0.0

    for i in range(n - 1):
        dx = fx[i + 1] - fx[i]
        dy = fy[i + 1] - fy[i]

        if math.hypot(dx, dy) >= min_speed:
            last = math.atan2(dx, dy)

        yaw[i] = last

    yaw[-1] = last
    return yaw.tolist()


def round_list(arr, ndigits=1):
    return [round(float(v), ndigits) for v in arr]


def build_payload(df, fps):
    t_min = df["time"].min()
    t_max = df["time"].max()
    dt = 1.0 / fps

    grid = np.arange(t_min, t_max, dt)
    if len(grid) < 2:
        grid = np.array([t_min, t_max])

    entities = df["player"].unique().tolist()
    entities.sort(key=lambda p: (p != "BALL", p))

    payload_entities = []

    for e in entities:
        sub = df[df["player"] == e].sort_values("time")
        fx, fy, fz, fb = resample_entity(sub, grid)
        is_ball = (e == "BALL")

        entry = {
            "name": e,
            "type": "ball" if is_ball else "car",
            "team": None if is_ball else get_team(e),
            "color": BALL_COLOR if is_ball else get_player_color(e),
            "x": round_list(fx, 1),
            "y": round_list(fy, 1),
            "z": round_list(fz, 1),
        }

        if not is_ball:
            entry["yaw"] = round_list(compute_yaw(fx, fy), 3)

        entry["boost"] = round_list(fb, 1) if fb is not None else None
        payload_entities.append(entry)

    return {
        "meta": {
            "fps": fps,
            "dt": dt,
            "frames": len(grid),
            "duration": float(grid[-1] - grid[0]),
        },
        "field": {
            "width": FIELD_WIDTH,
            "length": FIELD_LENGTH,
            "height": FIELD_HEIGHT,
            "corner_radius": CORNER_RADIUS,
            "goal_width": GOAL_WIDTH,
            "goal_depth": GOAL_DEPTH,
            "goal_height": GOAL_HEIGHT,
            "ball_radius": BALL_RADIUS,
            "car_length": CAR_LENGTH,
            "car_width": CAR_WIDTH,
            "car_height": CAR_HEIGHT,
        },
        "teams": {
            "orange": {
                "players": ORANGE_TEAM,
                "color": ORANGE_COLOR,
                "goalY": FIELD_LENGTH / 2,
            },
            "blue": {
                "players": BLUE_TEAM,
                "color": BLUE_COLOR,
                "goalY": -FIELD_LENGTH / 2,
            },
        },
        "entities": payload_entities,
    }


# ============================================================
# HTML Template
# ============================================================

HTML_TEMPLATE = r"""<!DOCTYPE html>
<html lang="en">

<head>
<meta charset="UTF-8" />
<title>Rocket League Replay - 3D Visualizer</title>

<style>
html, body {
    margin: 0;
    height: 100%;
    background: #05070a;
    overflow: hidden;
    font-family: -apple-system, Segoe UI, Roboto, Arial, sans-serif;
}

#canvas-wrap {
    position: absolute;
    inset: 0;
}

#hud {
    position: absolute;
    top: 12px;
    left: 12px;
    z-index: 5;
    color: #eee;
    background: rgba(10, 12, 16, 0.65);
    border-radius: 10px;
    padding: 10px 14px;
    font-size: 12px;
    backdrop-filter: blur(4px);
}

#hud .row {
    display: flex;
    align-items: center;
    gap: 8px;
    margin: 3px 0;
}

#hud .swatch {
    width: 10px;
    height: 10px;
    border-radius: 50%;
    display: inline-block;
}

#hud .boostbar {
    width: 70px;
    height: 6px;
    background: #222;
    border-radius: 3px;
    overflow: hidden;
}

#hud .boostfill {
    height: 100%;
    background: linear-gradient(90deg, #6ee7ff, #38bdf8);
}

#controls {
    position: absolute;
    bottom: 14px;
    left: 50%;
    transform: translateX(-50%);
    z-index: 5;
    background: rgba(10, 12, 16, 0.75);
    border-radius: 12px;
    padding: 10px 16px;
    display: flex;
    align-items: center;
    gap: 8px;
    color: #eee;
    font-size: 12px;
    backdrop-filter: blur(4px);
    max-width: 95vw;
    overflow-x: auto;
}

#controls button {
    background: #1c2430;
    border: 1px solid #354055;
    color: #eee;
    border-radius: 6px;
    padding: 6px 10px;
    cursor: pointer;
    font-size: 11px;
    white-space: nowrap;
}

#controls button:hover {
    background: #2a3548;
}

#controls button.active {
    background: #36465f;
    border-color: #5d7092;
}

#scrub {
    flex: 1;
    min-width: 100px;
}

#speed {
    background: #1c2430;
    color: #eee;
    border: 1px solid #354055;
    border-radius: 6px;
    padding: 4px 6px;
    font-size: 11px;
}

#timeLabel {
    min-width: 80px;
    text-align: center;
    font-variant-numeric: tabular-nums;
}

#hint {
    position: absolute;
    top: 12px;
    right: 12px;
    z-index: 5;
    color: #9aa4b2;
    background: rgba(10, 12, 16, 0.5);
    border-radius: 10px;
    padding: 8px 12px;
    font-size: 11px;
    max-width: 210px;
    line-height: 1.4;
}
</style>
</head>

<body>

<div id="canvas-wrap"></div>
<div id="hud"></div>

<div id="hint">
    Drag to orbit &middot; scroll to zoom &middot; right-drag to pan
</div>

<div id="controls">
    <button id="playBtn">Pause</button>
    <span id="timeLabel">0.0 / 0.0s</span>
    <input id="scrub" type="range" min="0" max="1000" value="0" />
    <select id="speed">
        <option value="0.25">0.25x</option>
        <option value="0.5">0.5x</option>
        <option value="1" selected>1x</option>
        <option value="2">2x</option>
        <option value="4">4x</option>
    </select>
    <button id="followBtn">Follow Ball: Off</button>
    <button id="coverageBtn" class="active">Coverage: On</button>
    <button id="centroidBtn" class="active">Centroid: On</button>
    <button id="pressureBtn" class="active">Pressure: On</button>
</div>

<script>
__THREE_JS__
</script>

<script>
__ORBIT_JS__
</script>

<script>

const DATA = __DATA_JSON__;
const F = DATA.field;
const M = DATA.meta;

const wrap = document.getElementById("canvas-wrap");

const renderer = new THREE.WebGLRenderer({ antialias: true });
renderer.setPixelRatio(Math.min(window.devicePixelRatio, 2));
renderer.setSize(window.innerWidth, window.innerHeight);
wrap.appendChild(renderer.domElement);

const scene = new THREE.Scene();
scene.background = new THREE.Color(0x05070a);
scene.fog = new THREE.Fog(0x05070a, 6000, 16000);

const camera = new THREE.PerspectiveCamera(55, window.innerWidth / window.innerHeight, 10, 40000);
camera.position.set(0, 4200, -7200);

const controls = new THREE.OrbitControls(camera, renderer.domElement);
controls.target.set(0, 200, 0);
controls.enableDamping = true;
controls.dampingFactor = 0.08;
controls.maxDistance = 15000;
controls.minDistance = 500;
controls.update();

scene.add(new THREE.AmbientLight(0xffffff, 0.55));
const sun = new THREE.DirectionalLight(0xffffff, 1.0);
sun.position.set(2000, 5000, -2000);
scene.add(sun);

const halfW = F.width / 2;
const halfL = F.length / 2;
const arenaH = F.height || 2048;
const rCorner = F.corner_radius || 1152;

const pitchMat = new THREE.MeshStandardMaterial({ color: 0x0b3d1f, roughness: 1 });
const pitch = new THREE.Mesh(new THREE.PlaneGeometry(F.width, F.length), pitchMat);
pitch.rotation.x = -Math.PI / 2;
scene.add(pitch);

// Helper function to build rounded field boundary path
function generateArenaBoundaryPoints(segs = 12) {
    const pts = [];
    const xInner = halfW - rCorner;
    const yInner = halfL - rCorner;

    // Corner 1: Top-Right (Positive X, Positive Y)
    for (let i = 0; i <= segs; i++) {
        const a = (i / segs) * (Math.PI / 2);
        pts.push(new THREE.Vector2(xInner + rCorner * Math.sin(a), yInner + rCorner * Math.cos(a)));
    }
    // Corner 2: Bottom-Right (Positive X, Negative Y)
    for (let i = 0; i <= segs; i++) {
        const a = (i / segs) * (Math.PI / 2);
        pts.push(new THREE.Vector2(xInner + rCorner * Math.cos(a), -yInner - rCorner * Math.sin(a)));
    }
    // Corner 3: Bottom-Left (Negative X, Negative Y)
    for (let i = 0; i <= segs; i++) {
        const a = (i / segs) * (Math.PI / 2);
        pts.push(new THREE.Vector2(-xInner - rCorner * Math.sin(a), -yInner - rCorner * Math.cos(a)));
    }
    // Corner 4: Top-Left (Negative X, Positive Y)
    for (let i = 0; i <= segs; i++) {
        const a = (i / segs) * (Math.PI / 2);
        pts.push(new THREE.Vector2(-xInner - rCorner * Math.cos(a), yInner + rCorner * Math.sin(a)));
    }
    return pts;
}

const arena2DPts = generateArenaBoundaryPoints(12);

// Floor & Ceiling Outline
function buildArenaRing(yElev, color, opacity = 1.0) {
    const pts3D = arena2DPts.map(p => new THREE.Vector3(p.x, yElev, p.y));
    const geo = new THREE.BufferGeometry().setFromPoints(pts3D);
    const mat = new THREE.LineBasicMaterial({ color, transparent: opacity < 1.0, opacity });
    scene.add(new THREE.LineLoop(geo, mat));
}

// Floor boundary line
buildArenaRing(4, 0xffffff, 0.8);
// Ceiling boundary line
buildArenaRing(arenaH, 0x4a6382, 0.45);

// Vertical Wall Corner Pillars & Wireframe Grid Lines
const wallMat = new THREE.LineBasicMaterial({ color: 0x35485e, transparent: true, opacity: 0.35 });
for (let i = 0; i < arena2DPts.length; i += 3) {
    const p = arena2DPts[i];
    const pillarGeo = new THREE.BufferGeometry().setFromPoints([
        new THREE.Vector3(p.x, 0, p.y),
        new THREE.Vector3(p.x, arenaH, p.y)
    ]);
    scene.add(new THREE.Line(pillarGeo, wallMat));
}

// Center Line
{
    const geo = new THREE.BufferGeometry().setFromPoints([
        new THREE.Vector3(-halfW, 4, 0),
        new THREE.Vector3(halfW, 4, 0)
    ]);
    scene.add(new THREE.Line(geo, new THREE.LineBasicMaterial({ color: 0xffffff })));
}

// Center Circle
{
    const curve = new THREE.EllipseCurve(0, 0, 750, 750, 0, 2 * Math.PI, false, 0);
    const pts = curve.getPoints(64).map(p => new THREE.Vector3(p.x, 4, p.y));
    scene.add(new THREE.LineLoop(new THREE.BufferGeometry().setFromPoints(pts), new THREE.LineBasicMaterial({ color: 0xffffff })));
}

// Goal Wireframe Box Representation
function goalFrame(zSign, color) {
    const gw = F.goal_width;
    const gh = F.goal_height;
    const gd = F.goal_depth;
    const z0 = zSign * halfL;
    const z1 = zSign * (halfL + gd);

    const mat = new THREE.LineBasicMaterial({ color });
    const pts = [
        [-gw / 2, 0, z0], [-gw / 2, gh, z0], [gw / 2, gh, z0], [gw / 2, 0, z0],
        [-gw / 2, 0, z1], [-gw / 2, gh, z1], [gw / 2, gh, z1], [gw / 2, 0, z1]
    ];
    const idx = [[0, 1], [1, 2], [2, 3], [4, 5], [5, 6], [6, 7], [1, 5], [2, 6], [0, 4], [3, 7]];

    idx.forEach(([a, b]) => {
        const geo = new THREE.BufferGeometry().setFromPoints([
            new THREE.Vector3(...pts[a]),
            new THREE.Vector3(...pts[b])
        ]);
        scene.add(new THREE.Line(geo, mat));
    });
}

goalFrame(-1, 0x66d9ff);
goalFrame(1, 0xff9f45);

function makeLabelSprite(name, color) {
    const W = 180;
    const H = 64;
    const canvas = document.createElement("canvas");
    canvas.width = W;
    canvas.height = H;
    const ctx = canvas.getContext("2d");
    const texture = new THREE.CanvasTexture(canvas);
    const material = new THREE.SpriteMaterial({ map: texture, depthTest: false, transparent: true });
    const sprite = new THREE.Sprite(material);

    sprite.scale.set(270, 96, 1);
    sprite.renderOrder = 999;

    function draw(pct) {
        ctx.clearRect(0, 0, W, H);
        ctx.font = "bold 24px sans-serif";
        ctx.textAlign = "center";

        ctx.fillStyle = "#000000";
        ctx.globalAlpha = 0.7;
        for (const [dx, dy] of [[-1, -1], [1, -1], [-1, 1], [1, 1]]) {
            ctx.fillText(name, W / 2 + dx, 27 + dy);
        }

        ctx.globalAlpha = 1;
        ctx.fillStyle = "#ffffff";
        ctx.fillText(name, W / 2, 27);

        if (pct !== null && pct !== undefined) {
            const barW = 120;
            const barH = 8;
            const barX = (W - barW) / 2;
            const barY = 39;

            ctx.fillStyle = "#222833";
            ctx.fillRect(barX, barY, barW, barH);
            ctx.fillStyle = "#6ee7ff";
            ctx.fillRect(barX, barY, barW * Math.max(0, Math.min(100, pct)) / 100, barH);
            ctx.strokeStyle = "rgba(255,255,255,0.5)";
            ctx.strokeRect(barX, barY, barW, barH);
        }
        texture.needsUpdate = true;
    }

    draw(null);
    return { sprite, draw };
}

const TRAIL_LEN = 90;

const entities = DATA.entities.map(e => {
    const group = new THREE.Group();
    let mesh;

    if (e.type === "ball") {
        mesh = new THREE.Mesh(
            new THREE.SphereGeometry(F.ball_radius, 20, 16),
            new THREE.MeshStandardMaterial({ color: e.color, roughness: 0.4, metalness: 0.1 })
        );
    } else {
        mesh = new THREE.Mesh(
            new THREE.BoxGeometry(F.car_width, F.car_height, F.car_length),
            new THREE.MeshStandardMaterial({ color: e.color, roughness: 0.5 })
        );
    }

    group.add(mesh);
    scene.add(group);

    let label = null;
    if (e.type !== "ball") {
        label = makeLabelSprite(e.name, e.color);
        label.sprite.position.set(0, F.car_height * 3.4, 0);
        group.add(label.sprite);
    }

    const trailGeo = new THREE.BufferGeometry();
    const trailPos = new Float32Array(TRAIL_LEN * 3);
    trailGeo.setAttribute("position", new THREE.BufferAttribute(trailPos, 3));

    const trail = new THREE.Line(
        trailGeo,
        new THREE.LineBasicMaterial({ color: e.color, transparent: true, opacity: 0.35 })
    );
    trail.frustumCulled = false;
    scene.add(trail);

    return {
        def: e,
        group,
        mesh,
        label,
        trail,
        trailPos,
        trailCount: 0,
        trailStart: 0
    };
});

const hud = document.getElementById("hud");
hud.innerHTML = entities
    .filter(en => en.def.type !== "ball")
    .map(en => `<div class="row">
        <span class="swatch" style="background:${en.def.color}"></span>
        <span style="width:90px; display:inline-block;">${en.def.name}</span>
        <span class="boostbar">
            <span class="boostfill" id="hud-${en.def.name}" style="width:0%"></span>
        </span>
    </div>`)
    .join("");

function sampleAt(entity, tSec) {
    const frame = tSec / M.dt;
    const n = M.frames;

    const i0 = Math.max(0, Math.min(n - 1, Math.floor(frame)));
    const i1 = Math.min(n - 1, i0 + 1);
    const frac = Math.min(1, Math.max(0, frame - i0));

    const d = entity.def;
    const lerp = (a, b) => a + (b - a) * frac;

    const x = lerp(d.x[i0], d.x[i1]);
    const y = lerp(d.y[i0], d.y[i1]);
    const z = lerp(d.z[i0], d.z[i1]);

    let yaw = null;
    if (d.yaw) {
        let a = d.yaw[i0];
        let b = d.yaw[i1];
        let diff = b - a;
        if (diff > Math.PI) b -= 2 * Math.PI;
        if (diff < -Math.PI) b += 2 * Math.PI;
        yaw = lerp(a, b);
    }

    let boost = null;
    if (d.boost) {
        boost = lerp(d.boost[i0], d.boost[i1]);
    }

    return { x, y: z, z: y, yaw, boost, i0, i1, frac };
}

// ============================================================
// Coverage Tetrahedrons
// ============================================================

const coverage = {
    orange: {
        color: new THREE.Color(DATA.teams.orange.color),
        players: DATA.teams.orange.players,
        goalY: DATA.teams.orange.goalY,
        group: new THREE.Group(),
        mesh: null,
        edges: null,
        enabled: true
    },
    blue: {
        color: new THREE.Color(DATA.teams.blue.color),
        players: DATA.teams.blue.players,
        goalY: DATA.teams.blue.goalY,
        group: new THREE.Group(),
        mesh: null,
        edges: null,
        enabled: true
    }
};

scene.add(coverage.orange.group);
scene.add(coverage.blue.group);

const entityByName = {};
for (const en of entities) {
    entityByName[en.def.name] = en;
}

function createCoverageGeometry(team) {
    const positions = new Float32Array(4 * 3);
    const geometry = new THREE.BufferGeometry();
    geometry.setAttribute("position", new THREE.BufferAttribute(positions, 3));
    geometry.setIndex([0, 1, 2, 0, 3, 1, 1, 3, 2, 2, 3, 0]);
    geometry.computeVertexNormals();

    const material = new THREE.MeshBasicMaterial({
        color: team.color,
        transparent: true,
        opacity: 0.10,
        side: THREE.DoubleSide,
        depthWrite: false
    });

    const mesh = new THREE.Mesh(geometry, material);
    const edgeGeometry = new THREE.EdgesGeometry(geometry);
    const edgeMaterial = new THREE.LineBasicMaterial({
        color: team.color,
        transparent: true,
        opacity: 0.42,
        depthTest: true
    });

    const edges = new THREE.LineSegments(edgeGeometry, edgeMaterial);

    team.group.add(mesh);
    team.group.add(edges);
    team.mesh = mesh;
    team.edges = edges;
}

createCoverageGeometry(coverage.orange);
createCoverageGeometry(coverage.blue);

function updateCoverage(team) {
    if (!team.enabled) {
        team.group.visible = false;
        return;
    }

    const points = [];
    for (const playerName of team.players) {
        const entity = entityByName[playerName];
        if (!entity) {
            team.group.visible = false;
            return;
        }

        const s = sampleAt(entity, simTime);
        if (!Number.isFinite(s.x) || !Number.isFinite(s.y) || !Number.isFinite(s.z)) {
            team.group.visible = false;
            return;
        }

        points.push(new THREE.Vector3(s.x, s.y, s.z));
    }

    points.push(new THREE.Vector3(0, F.goal_height / 2, team.goalY));

    const positionAttribute = team.mesh.geometry.getAttribute("position");
    for (let i = 0; i < 4; i++) {
        positionAttribute.setXYZ(i, points[i].x, points[i].y, points[i].z);
    }

    positionAttribute.needsUpdate = true;
    team.mesh.geometry.computeVertexNormals();

    const newEdges = new THREE.EdgesGeometry(team.mesh.geometry);
    team.edges.geometry.dispose();
    team.edges.geometry = newEdges;

    team.group.visible = true;
}

// ============================================================
// Feature 10: Team Centroid + Ball Displacement
// ============================================================

const centroidGroup = new THREE.Group();
scene.add(centroidGroup);

function createTeamCentroidObject(colorHex) {
    const group = new THREE.Group();
    const mat = new THREE.MeshStandardMaterial({
        color: colorHex,
        transparent: true,
        opacity: 0.65,
        roughness: 0.2
    });
    const sphere = new THREE.Mesh(new THREE.SphereGeometry(120, 16, 16), mat);
    group.add(sphere);

    const lineGeo = new THREE.BufferGeometry().setFromPoints([
        new THREE.Vector3(0, 0, 0),
        new THREE.Vector3(0, 0, 0)
    ]);
    const lineMat = new THREE.LineDashedMaterial({
        color: colorHex,
        dashSize: 120,
        gapSize: 60,
        linewidth: 2,
        transparent: true,
        opacity: 0.8
    });
    const line = new THREE.Line(lineGeo, lineMat);
    group.add(line);

    centroidGroup.add(group);
    return { group, sphere, line, lineGeo, enabled: true };
}

const centroids = {
    orange: createTeamCentroidObject(DATA.teams.orange.color),
    blue: createTeamCentroidObject(DATA.teams.blue.color)
};

function updateCentroids(ballPos) {
    if (!centroids.orange.enabled) {
        centroidGroup.visible = false;
        return;
    }
    centroidGroup.visible = true;

    ["orange", "blue"].forEach(teamKey => {
        const teamObj = centroids[teamKey];
        const playerNames = DATA.teams[teamKey].players;

        let cx = 0, cy = 0, cz = 0, count = 0;
        playerNames.forEach(pName => {
            const ent = entityByName[pName];
            if (ent) {
                const s = sampleAt(ent, simTime);
                cx += s.x; cy += s.y; cz += s.z;
                count++;
            }
        });

        if (count > 0 && ballPos) {
            cx /= count; cy /= count; cz /= count;
            teamObj.sphere.position.set(cx, cy, cz);

            const linePts = [
                new THREE.Vector3(cx, cy, cz),
                new THREE.Vector3(ballPos.x, ballPos.y, ballPos.z)
            ];
            teamObj.lineGeo.setFromPoints(linePts);
            teamObj.line.computeLineDistances();
            teamObj.group.visible = true;
        } else {
            teamObj.group.visible = false;
        }
    });
}

// ============================================================
// Feature 11: Team Pressure Field Visualizer
// ============================================================

const GRID_X = 64;
const GRID_Y = 80;
const pressureCanvas = document.createElement("canvas");
pressureCanvas.width = GRID_X;
pressureCanvas.height = GRID_Y;
const pressureCtx = pressureCanvas.getContext("2d");

const pressureTexture = new THREE.CanvasTexture(pressureCanvas);
pressureTexture.minFilter = THREE.LinearFilter;
pressureTexture.magFilter = THREE.LinearFilter;

const pressureGeo = new THREE.PlaneGeometry(F.width, F.length);
const pressureMat = new THREE.MeshBasicMaterial({
    map: pressureTexture,
    transparent: true,
    opacity: 0.55,
    depthWrite: false
});

const pressureMesh = new THREE.Mesh(pressureGeo, pressureMat);
pressureMesh.rotation.x = -Math.PI / 2;
pressureMesh.position.y = 8;
scene.add(pressureMesh);

let pressureEnabled = true;

function updatePressureField() {
    if (!pressureEnabled) {
        pressureMesh.visible = false;
        return;
    }
    pressureMesh.visible = true;

    const imgData = pressureCtx.createImageData(GRID_X, GRID_Y);
    const data = imgData.data;

    const orangePlayers = [];
    const bluePlayers = [];

    entities.forEach(en => {
        if (en.def.type === "ball") return;
        const s0 = sampleAt(en, simTime);
        const s1 = sampleAt(en, Math.min(M.duration, simTime + 0.1));

        const vx = (s1.x - s0.x) / 0.1;
        const vz = (s1.z - s0.z) / 0.1;

        const info = {
            x: s0.x,
            z: s0.z,
            vx, vz,
            yaw: s0.yaw || 0,
            boost: (s0.boost !== null) ? s0.boost : 33.0
        };

        if (en.def.team === "orange") orangePlayers.push(info);
        if (en.def.team === "blue") bluePlayers.push(info);
    });

    const oR = 255, oG = 122, oB = 0;
    const bR = 47,  bG = 128, bB = 237;

    for (let gy = 0; gy < GRID_Y; gy++) {
        const worldZ = (gy / (GRID_Y - 1) - 0.5) * F.length;

        for (let gx = 0; gx < GRID_X; gx++) {
            const worldX = (gx / (GRID_X - 1) - 0.5) * F.width;

            let oVal = 0;
            orangePlayers.forEach(p => {
                oVal += calcPlayerInfluence(worldX, worldZ, p);
            });

            let bVal = 0;
            bluePlayers.forEach(p => {
                bVal += calcPlayerInfluence(worldX, worldZ, p);
            });

            const idx = (gy * GRID_X + gx) * 4;

            if (oVal > bVal) {
                const alpha = Math.min(1.0, (oVal - bVal) * 1.2);
                data[idx]     = oR;
                data[idx + 1] = oG;
                data[idx + 2] = oB;
                data[idx + 3] = Math.floor(alpha * 180);
            } else {
                const alpha = Math.min(1.0, (bVal - oVal) * 1.2);
                data[idx]     = bR;
                data[idx + 1] = bG;
                data[idx + 2] = bB;
                data[idx + 3] = Math.floor(alpha * 180);
            }
        }
    }

    pressureCtx.putImageData(imgData, 0, 0);
    pressureTexture.needsUpdate = true;
}

function calcPlayerInfluence(wx, wz, p) {
    const dx = wx - p.x;
    const dz = wz - p.z;
    const distSq = dx * dx + dz * dz;

    const baseRadius = 1400.0 + (p.boost * 10.0);
    const sigmaSq = baseRadius * baseRadius;

    let val = Math.exp(-distSq / sigmaSq);

    const speed = Math.hypot(p.vx, p.vz);
    if (speed > 50.0 && distSq > 1.0) {
        const dirX = dx / Math.sqrt(distSq);
        const dirZ = dz / Math.sqrt(distSq);
        const velX = p.vx / speed;
        const velZ = p.vz / speed;

        const dot = dirX * velX + dirZ * velZ;
        const directionFactor = 1.0 + 0.8 * Math.max(0.0, dot);
        val *= directionFactor;
    }

    return val;
}

// ============================================================
// Playback Control & Loop
// ============================================================

let playing = true;
let simTime = 0;
let lastWall = performance.now();
let speed = 1.0;

const playBtn = document.getElementById("playBtn");
const scrub = document.getElementById("scrub");
const timeLabel = document.getElementById("timeLabel");
const speedSel = document.getElementById("speed");
const followBtn = document.getElementById("followBtn");
const coverageBtn = document.getElementById("coverageBtn");
const centroidBtn = document.getElementById("centroidBtn");
const pressureBtn = document.getElementById("pressureBtn");

let following = false;
let scrubbing = false;

playBtn.onclick = () => {
    playing = !playing;
    playBtn.textContent = playing ? "Pause" : "Play";
    lastWall = performance.now();
};

speedSel.onchange = (e) => {
    speed = parseFloat(e.target.value);
};

scrub.oninput = () => {
    scrubbing = true;
    simTime = (parseFloat(scrub.value) / 1000) * M.duration;
};

scrub.onchange = () => {
    scrubbing = false;
};

followBtn.onclick = () => {
    following = !following;
    followBtn.textContent = `Follow Ball: ${following ? "On" : "Off"}`;
    followBtn.classList.toggle("active", following);
};

coverageBtn.onclick = () => {
    const act = !coverage.orange.enabled;
    coverage.orange.enabled = act;
    coverage.blue.enabled = act;
    coverageBtn.textContent = `Coverage: ${act ? "On" : "Off"}`;
    coverageBtn.classList.toggle("active", act);
};

centroidBtn.onclick = () => {
    const act = !centroids.orange.enabled;
    centroids.orange.enabled = act;
    centroids.blue.enabled = act;
    centroidBtn.textContent = `Centroid: ${act ? "On" : "Off"}`;
    centroidBtn.classList.toggle("active", act);
};

pressureBtn.onclick = () => {
    pressureEnabled = !pressureEnabled;
    pressureBtn.textContent = `Pressure: ${pressureEnabled ? "On" : "Off"}`;
    pressureBtn.classList.toggle("active", pressureEnabled);
};

window.onresize = () => {
    camera.aspect = window.innerWidth / window.innerHeight;
    camera.updateProjectionMatrix();
    renderer.setSize(window.innerWidth, window.innerHeight);
};

const ballEntity = entities.find(e => e.def.type === "ball");

function animate() {
    requestAnimationFrame(animate);

    const now = performance.now();
    const wallDt = (now - lastWall) / 1000;
    lastWall = now;

    if (playing && !scrubbing) {
        simTime += wallDt * speed;
        if (simTime > M.duration) simTime = 0;
    }

    if (!scrubbing && M.duration > 0) {
        scrub.value = Math.floor((simTime / M.duration) * 1000);
    }

    timeLabel.textContent = `${simTime.toFixed(1)} / ${M.duration.toFixed(1)}s`;

    let ballPos = null;

    for (const en of entities) {
        const s = sampleAt(en, simTime);
        en.group.position.set(s.x, s.y, s.z);

        if (en.def.type === "ball") {
            ballPos = s;
        }

        if (s.yaw !== null) {
            en.group.rotation.y = s.yaw;
        }

        if (en.label) {
            en.label.draw(s.boost);
        }

        if (s.boost !== null) {
            const hBar = document.getElementById(`hud-${en.def.name}`);
            if (hBar) hBar.style.width = `${Math.max(0, Math.min(100, s.boost))}%`;
        }

        const p = en.trailPos;
        const idx = (en.trailStart + en.trailCount) % TRAIL_LEN;
        p[idx * 3] = s.x;
        p[idx * 3 + 1] = s.y;
        p[idx * 3 + 2] = s.z;

        if (en.trailCount < TRAIL_LEN) {
            en.trailCount++;
        } else {
            en.trailStart = (en.trailStart + 1) % TRAIL_LEN;
        }

        en.trail.geometry.attributes.position.needsUpdate = true;
    }

    updateCoverage(coverage.orange);
    updateCoverage(coverage.blue);
    updateCentroids(ballPos);
    updatePressureField();

    if (following && ballEntity) {
        const bs = sampleAt(ballEntity, simTime);
        controls.target.set(bs.x, bs.y, bs.z);
    }

    controls.update();
    renderer.render(scene, camera);
}

animate();

</script>
</body>
</html>
"""

THREE_CDN = "https://cdnjs.cloudflare.com/ajax/libs/three.js/r128/three.min.js"
ORBIT_CDN = "https://cdn.jsdelivr.net/npm/three@0.128.0/examples/js/controls/OrbitControls.js"


def fetch_js(url):
    with urllib.request.urlopen(url) as resp:
        return resp.read().decode("utf-8")



def main():
    parser = argparse.ArgumentParser(
        description="Generate 3D Rocket League Replay Visualizer"
    )
    parser.add_argument(
        "-i",
        "--input",
        help="Input CSV path. If omitted, the first CSV in Replay Data/Parsed CSVs is used.",
    )
    parser.add_argument(
        "--fps",
        type=int,
        default=30,
        help="Resampling FPS",
    )
    args = parser.parse_args()

    if args.input:
        csv_path = Path(args.input)
    else:
        try:
            csv_path = find_default_csv()
        except FileNotFoundError as e:
            print(f"Error: {e}")
            return

    if not csv_path.exists():
        print(f"Error: Input file '{csv_path}' not found.")
        return

    # JSON has the same filename as the CSV, but with .json
    json_path = BASE_DIR / "Replay Data" / "Game Metadata" / f"{csv_path.stem}.json"

    if not json_path.exists():
        print(f"Error: Corresponding JSON file '{json_path}' not found.")
        return

    # HTML has the same filename as the CSV/JSON, but with .html
    output_dir = BASE_DIR / "Replay Data" / "Replay HTMLs"
    output_path = output_dir / f"{csv_path.stem}.html"

    print(f"Loading CSV: {csv_path}...")
    print(f"Loading team setup: {json_path}...")

    load_team_setup(json_path)
    df = load_data(csv_path)

    print("Building payload...")
    payload = build_payload(df, args.fps)

    print("Fetching Three.js libraries...")
    three_js = fetch_js(THREE_CDN)
    orbit_js = fetch_js(ORBIT_CDN)

    print("Generating HTML file...")

    html_out = HTML_TEMPLATE.replace("__THREE_JS__", three_js)
    html_out = html_out.replace("__ORBIT_JS__", orbit_js)
    html_out = html_out.replace("__DATA_JSON__", json.dumps(payload))

    output_path.parent.mkdir(parents=True, exist_ok=True)

    with open(output_path, "w", encoding="utf-8") as f:
        f.write(html_out)

    print(f"Done! Replay generated at: {os.path.abspath(output_path)}")


if __name__ == "__main__":
    main()
