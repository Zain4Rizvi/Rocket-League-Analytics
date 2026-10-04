import unittest

import numpy as np

from Scripts import rl_replay_3d


class ReplayThreatEventTests(unittest.TestCase):
    def build_analytics(self, ball_y, metadata=None):
        fps = 2
        grid = np.arange(len(ball_y), dtype=float) / fps
        entities = [
            {"type": "ball", "x": [0.0] * len(ball_y), "y": ball_y},
            {
                "type": "car",
                "name": "Blue Player",
                "team": "blue",
                "x": [0.0] * len(ball_y),
                "y": [0.0] * len(ball_y),
                "boost": [100.0] * len(ball_y),
            },
        ]
        return rl_replay_3d.build_coaching_analytics(entities, grid, fps, metadata)

    def test_repeated_peaks_in_same_half_emit_once(self):
        analytics = self.build_analytics([3000.0] * 20)

        blue_threats = [
            event for event in analytics["events"]
            if event["kind"] == "THREAT" and event["team"] == "BLUE"
        ]

        self.assertEqual(len(blue_threats), 1)

    def test_threat_event_starts_at_first_qualifying_sample(self):
        analytics = self.build_analytics([0.0] * 6 + [3000.0] * 14)
        threats = [event for event in analytics["events"] if event["kind"] == "THREAT"]

        self.assertEqual(len(threats), 1)
        self.assertEqual(threats[0]["frame"], 6)
        self.assertEqual(threats[0]["time"], 3.0)

    def test_midfield_return_rearms_team(self):
        scenarios = {
            "BLUE": [3000.0] * 6 + [0.0, -1000.0, -1000.0, 0.0] + [3000.0] * 12,
            "ORANGE": [-3000.0] * 6 + [0.0, 1000.0, 1000.0, 0.0] + [-3000.0] * 12,
        }

        for team, ball_y in scenarios.items():
            with self.subTest(team=team):
                analytics = self.build_analytics(ball_y)
                threats = [
                    event for event in analytics["events"]
                    if event["kind"] == "THREAT" and event["team"] == team
                ]
                self.assertEqual(len(threats), 2)

    def test_net_dwell_suppresses_threat_but_preserves_goal(self):
        analytics = self.build_analytics(
            [6000.0] * 10,
            {"goals": [{"frame": 0, "player_name": "Blue Player", "team": "BLUE"}]},
        )

        self.assertFalse(any(event["kind"] == "THREAT" for event in analytics["events"]))
        self.assertTrue(any(event["kind"] == "GOAL" for event in analytics["events"]))
        self.assertTrue(any(value >= 42 for value in analytics["series"]["blueThreat"]))

    def test_transition_event_starts_at_threshold_and_emits_once(self):
        analytics = self.build_analytics([0.0, 0.0, 0.0, 1100.0, 2400.0, 4000.0, 5800.0, 7600.0, 9400.0, 11200.0])
        transitions = [event for event in analytics["events"] if event["kind"] == "TRANSITION"]

        self.assertEqual(len(transitions), 1)
        self.assertEqual(transitions[0]["frame"], 3)
        self.assertEqual(transitions[0]["time"], 1.5)


if __name__ == "__main__":
    unittest.main()