import math
import unittest

import numpy as np

from Scripts import rl_replay_3d


class BallcamSamplerTests(unittest.TestCase):
    def test_default_on_change_holds_and_repeat_does_not_flip(self):
        grid = np.array([0.0, 1.0, 2.0, 3.0, 4.0])

        self.assertEqual(rl_replay_3d.sample_ballcam([], grid), [1, 1, 1, 1, 1])
        self.assertEqual(
            rl_replay_3d.sample_ballcam([[1.5, 0], [3.2, 1]], grid),
            [1, 1, 0, 0, 1],
        )
        self.assertEqual(
            rl_replay_3d.sample_ballcam([[1.5, 0], [2.0, 0]], grid),
            [1, 1, 0, 0, 0],
        )

    def test_identity_quaternion_faces_positive_x(self):
        self.assertAlmostEqual(
            rl_replay_3d.yaw_from_quaternion(0.0, 0.0, 0.0, 1.0),
            math.pi / 2,
        )
