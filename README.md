# Rocket League Analytics

A local Rocket League replay analyzer. Upload a `.replay` file, process its player and ball telemetry, and view the match as an interactive 3D simulation in your browser.

Generated replays include a coaching console with playback-synchronized goal-threat, momentum, and ball-control trends; clickable goal and transition events; player impact reports; boost-economy indicators; and explainable automated review recommendations. These are derived from the replay telemetry and are intended as coaching signals, not official RLCS statistics.

**Live Demo:** [Rocket League Analytics](https://rocket-league-analytics.onrender.com/)

## Setup

Use Python 3.13 or newer. Install the Python dependencies:

```powershell
pip install -r requirements.txt
```

The replay parser also requires `Scripts/rrrocket.exe`.

## Run the Website

From the project root:

```powershell
python .\server.py
```

Open [http://localhost:8000](http://localhost:8000), then drag and drop a `.replay` file into the upload area. The server saves the replay, runs the processing pipeline, and displays the generated simulation.

Press `Ctrl+C` in the server terminal to stop it. To use another port:

```powershell
python .\server.py 8000
```

## Command-Line Pipeline

To process a replay without the website:

```powershell
python .\Scripts\run_pipeline.py -i "Replay Data\Raw Replays\match.replay" -o "Replay Data\Replay HTMLs\match.html"
```

The pipeline creates:

- Parsed telemetry CSV files in `Replay Data/Parsed CSVs`
- Match metadata JSON files in `Replay Data/Game Metadata`
- Interactive HTML simulations in `Replay Data/Replay HTMLs`

## Project Structure

- `server.py` - Local upload server and web API
- `frontend/` - Upload interface
- `Scripts/Replay to CSV Parser.py` - Replay parsing and telemetry extraction
- `Scripts/rl_replay_3d.py` - Interactive 3D HTML generation
- `Scripts/run_pipeline.py` - Runs the parser and visualizer together
