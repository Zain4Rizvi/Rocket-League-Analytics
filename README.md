# Rocket League Analytics

An interactive Rocket League replay analyzer that turns `.replay` files into a 3D match visualization with automated gameplay analysis and AI-powered coaching.

**[Live Demo](https://rocket.zainrizvi.ca/)**

![Rocket League Analytics](assets/Example_Scene.png)

## What It Does

Upload a Rocket League replay and explore the entire match through an interactive 3D simulation.

The viewer includes:

* **3D Replay Visualization** — Watch the match unfold with player and ball movement, camera controls, and synchronized playback.
* **Goal & Event Timeline** — Jump between goals and important transitions directly from the replay.
* **Player Analysis** — Compare positioning, movement, momentum, and overall impact throughout the match.
* **Boost Analysis** — Track boost usage and identify inefficient boost management.
* **Gameplay Trends** — View goal-threat, momentum, field-position, and ball-control trends synchronized with the replay.
* **AI Coach** — Ask questions about the match in natural language and receive explanations backed by the replay's underlying telemetry.

## AI Coaching

The coaching system combines deterministic analysis with an LLM.

Instead of giving the model raw replay data and asking it to interpret everything itself, Python analysis tools calculate the underlying statistics first. The AI then selects the relevant analyses and explains their results.

For example, you can ask:

> "Who managed their boost worst?"

> "Was our positioning the problem?"

> "Talk me through the goals."

The coach can reference specific moments in the match and link them directly to the corresponding point in the 3D replay.

This approach keeps the numerical analysis deterministic while using the LLM primarily for **reasoning, context, and explanation**.

The generated insights are coaching signals derived from replay telemetry, not official RLCS statistics.

## Replay Analysis

The system extracts player and ball telemetry from each replay and uses it to generate features such as:

* Position and movement
* Speed and momentum
* Distance from the ball
* Distance from the goal
* Boost usage
* Field positioning
* Goal-threat trends
* Match transitions

These features power both the visualizations and the coaching system.

Some gameplay information is not available in the extracted telemetry, such as individual ball touches and demolitions. The coach therefore avoids presenting those as measured facts.

## How It Works

```text
Rocket League Replay
        ↓
   Replay Parser
        ↓
  Player / Ball Telemetry
        ↓
   Feature Extraction
        ↓
 ┌───────────────┬────────────────┐
 │               │                │
3D Visualization  Deterministic    AI Coach
                  Analysis         ↓
                                   Natural Language
                                   Explanations
```

The project is built primarily with **Python**, with a browser-based frontend and interactive **Three.js** visualization.

## Project Structure

```text
├── frontend/                  # Web interface
├── Scripts/
│   ├── Replay to CSV Parser.py
│   ├── rl_replay_3d.py        # 3D replay visualization
│   ├── run_pipeline.py        # Replay processing pipeline
│   ├── replay_analysis.py     # Deterministic gameplay analysis
│   └── coach_agent.py         # AI coaching agent
└── server.py                  # Backend server
```

## Demo

Try it with one of your own Rocket League replays:

**[rocket.zainrizvi.ca]**
