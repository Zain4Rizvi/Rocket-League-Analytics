#!/usr/bin/env python3
"""
AI coaching agent
=================

Wraps the deterministic analyses in `replay_analysis` as LangChain tools and
orchestrates them with a small LangGraph workflow.

Division of labour:

- Python computes every number. The model never calculates statistics.
- `gemini-2.0-flash-exp` decides which analyses to run and writes the coaching answer.
- `gemini-1.5-flash` does one structuring pass, selecting which already-computed
  moments back up the answer.

Only the compact `llm_payload` of each analysis enters the model's context.
Chart series travel straight to the browser and are never sent to the model.
"""

import json
import os
from typing import Annotated, Any, Optional, TypedDict

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from langchain_core.tools import StructuredTool
from langchain_google_genai import ChatGoogleGenerativeAI
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages
from langgraph.prebuilt import ToolNode
from pydantic import BaseModel, Field

import replay_analysis

PLANNER_MODEL = "gemini-3.6-flash"
STRUCTURING_MODEL = "gemini-3.6-flash"

# Two nodes per tool round, so this allows roughly six rounds before stopping.
RECURSION_LIMIT = 14
REQUEST_TIMEOUT = 120.0

SYSTEM_PROMPT = """You are a Rocket League coach analysing one replay.

Rules:
- Every number you state must come from a tool call. Never estimate, never
  calculate statistics yourself, and never invent a timestamp.
- Call the analyses you need, then write the answer. Prefer the fewest calls
  that genuinely answer the question; call several at once when they are
  independent.
- Times are in seconds from the start of the replay, matching the 3D viewer.
- This replay has no ball-touch, demolition or possession data. Do not discuss
  touches, dribbles, challenges or possession as if they were measured.
  The "first/second/third man" figures are a distance-to-ball proxy, not true
  rotation - say so when you lean on them.
- Use exact player names from the roster below.

Write like a coach talking to a player: lead with the answer, support it with
specific numbers and moments, and finish with at most two concrete things to
work on. Be concise - short paragraphs or a few bullets, no headings, no
preamble, and no restating the question.

Match context:
{context}"""

STRUCTURING_PROMPT = """Select the moments from the candidate list that best
support the coaching answer below. Choose at most 4, in time order. Copy their
`time` values exactly - never invent or adjust a time. Write each label as a
short phrase (under 8 words) describing why that moment matters for the answer.
If no candidate is relevant, return an empty list.

Coaching answer:
{answer}

Candidate moments:
{candidates}"""


class EvidenceItem(BaseModel):
    time: float = Field(description="Timestamp copied exactly from a candidate")
    label: str = Field(description="Short phrase explaining the moment")


class Evidence(BaseModel):
    items: list[EvidenceItem] = Field(default_factory=list)


class CoachState(TypedDict):
    messages: Annotated[list, add_messages]


def _describe_match(context):
    """One compact paragraph of match context for the system prompt."""
    metadata = context.metadata
    scores = metadata.get("team_scores", {})
    roster = []
    for team in ("BLUE", "ORANGE"):
        names = ", ".join(metadata.get(team + "_TEAM", []))
        roster.append(f"{team}: {names}")
    return (
        f"Final score BLUE {scores.get('BLUE')} - {scores.get('ORANGE')} ORANGE. "
        f"Length {context.duration:.0f}s. {' | '.join(roster)}. "
        "BLUE defends the -y goal and attacks +y; ORANGE is the mirror."
    )


def _build_tools(stem, charts):
    """Build the tool set for one replay.

    The stem is bound here rather than passed as a tool argument, so the model
    cannot get it wrong and it never occupies context. Chart payloads are
    appended to `charts` as a side effect.
    """

    def run(function, **kwargs):
        llm_payload, display_payload = function(stem, **kwargs)
        if display_payload:
            charts.append(display_payload)
        return json.dumps(llm_payload)

    def match_summary() -> str:
        """Official match result: score, goal times, and per-player goals,
        assists, saves and shots from the replay header. Cheap - start here when
        you need to know what actually happened."""
        return run(replay_analysis.match_summary)

    def boost_report(player: Optional[str] = None) -> str:
        """Boost economy: average boost, time spent starved or sitting at a full
        tank, and how often a player runs dry. Omit `player` for the whole
        lobby."""
        return run(replay_analysis.boost_report, player=player)

    def positioning_report(player: Optional[str] = None) -> str:
        """Field position and movement: time in each third, time behind the
        ball, distance to the ball, teammate spacing, speed, and a
        first/second/third man proxy. Omit `player` for the whole lobby."""
        return run(replay_analysis.positioning_report, player=player)

    def key_moments(start: Optional[float] = None,
                    end: Optional[float] = None) -> str:
        """Notable moments: goals, plus heuristic pressure and fast-transition
        windows. Pass `start` and `end` in seconds to focus on a passage."""
        return run(replay_analysis.key_moments, start=start, end=end)

    return [
        StructuredTool.from_function(match_summary),
        StructuredTool.from_function(boost_report),
        StructuredTool.from_function(positioning_report),
        StructuredTool.from_function(key_moments),
    ]


def _candidate_moments(context):
    """Deterministic timestamps the structuring model may choose between."""
    candidates = [
        {"time": goal["time"],
         "what": f"{goal['team']} goal scored by {goal['scorer']}"}
        for goal in replay_analysis.goal_timeline(context)
    ]
    moments, _ = replay_analysis.key_moments(context.stem)
    for event in moments["events"]:
        if event["kind"] != "GOAL":
            candidates.append({"time": event["time"], "what": event["detail"]})
    candidates.sort(key=lambda item: item["time"])
    return candidates


def _text_of(message):
    """Extract plain text from a message, which may hold mixed content blocks."""
    content = message.content
    if isinstance(content, str):
        return content.strip()
    parts = [
        block.get("text", "")
        for block in content
        if isinstance(block, dict) and block.get("type") == "text"
    ]
    return "\n".join(parts).strip()


def _select_evidence(context, answer):
    """Ask the structuring model to pick supporting moments.

    Every candidate timestamp is computed deterministically; the model only
    chooses among them and writes the labels. Selections that do not match a
    real candidate are dropped.
    """
    candidates = _candidate_moments(context)
    if not candidates:
        return []
    try:
        structurer = ChatGoogleGenerativeAI(
            model=STRUCTURING_MODEL,
            temperature=0,
            google_api_key=os.environ.get("GEMINI_API_KEY"),
        ).with_structured_output(Evidence)
        selection = structurer.invoke([
            HumanMessage(STRUCTURING_PROMPT.format(
                answer=answer,
                candidates=json.dumps(candidates),
            ))
        ])
    except Exception as error:
        print(f"[coach] evidence selection failed: {error}")
        return []

    valid = {round(item["time"], 1) for item in candidates}
    return [
        {"time": item.time, "label": item.label}
        for item in selection.items
        if round(item.time, 1) in valid
    ][:4]


def answer_question(stem, question, player=None):
    """Answer a question about a replay, yielding progress events as it goes.

    Yields dicts the server streams to the browser:
      {"type": "tool", "name": ...}          an analysis ran
      {"type": "chart", "chart": ...}        a chart payload for the frontend
      {"type": "answer", "text": ...}        the coaching answer
      {"type": "evidence", "items": [...]}   clickable moments
      {"type": "error", "error": ...}        something went wrong
    """
    context = replay_analysis.load_context(stem)

    charts: list[Any] = []
    tools = _build_tools(stem, charts)

    planner = ChatGoogleGenerativeAI(
        model=PLANNER_MODEL,
        temperature=0,
        google_api_key=os.environ.get("GEMINI_API_KEY"),
    ).bind_tools(tools)

    def plan(state: CoachState):
        return {"messages": [planner.invoke(state["messages"])]}

    graph = StateGraph(CoachState)
    graph.add_node("plan", plan)
    graph.add_node("tools", ToolNode(tools))
    graph.add_edge(START, "plan")
    graph.add_conditional_edges(
        "plan",
        lambda state: "tools" if getattr(state["messages"][-1], "tool_calls", None) else END,
        {"tools": "tools", END: END},
    )
    graph.add_edge("tools", "plan")
    workflow = graph.compile()

    asked = question if not player else f"{question}\n\n(The user is asking about {player}.)"
    state = {
        "messages": [
            SystemMessage(SYSTEM_PROMPT.format(context=_describe_match(context))),
            HumanMessage(asked),
        ]
    }

    answer = ""
    sent_charts = 0
    for update in workflow.stream(state, {"recursion_limit": RECURSION_LIMIT},
                                  stream_mode="updates"):
        for node, payload in update.items():
            for message in payload.get("messages", []):
                if node == "plan" and isinstance(message, AIMessage):
                    for call in message.tool_calls or []:
                        yield {
                            "type": "tool",
                            "name": call["name"],
                            "args": {key: value
                                     for key, value in call["args"].items()
                                     if value is not None},
                        }
                    text = _text_of(message)
                    if text:
                        answer = text
        while sent_charts < len(charts):
            yield {"type": "chart", "chart": charts[sent_charts]}
            sent_charts += 1

    if not answer:
        yield {"type": "error",
               "error": "The coach ran out of analysis steps before answering."}
        return

    yield {"type": "answer", "text": answer}
    yield {"type": "evidence", "items": _select_evidence(context, answer)}


def available():
    """Whether the agent can run - lets the server fail helpfully."""
    return bool(os.environ.get("GEMINI_API_KEY"))


if __name__ == "__main__":
    import sys

    stem = sys.argv[1]
    question = sys.argv[2] if len(sys.argv) > 2 else "How did this match go?"
    for event in answer_question(stem, question):
        if event["type"] == "chart":
            print(f"[chart] {event['chart']['title']}")
        elif event["type"] == "tool":
            print(f"[tool] {event['name']} {event['args']}")
        elif event["type"] == "answer":
            print("\n" + event["text"])
        else:
            print(f"[{event['type']}] {json.dumps(event)[:400]}")
