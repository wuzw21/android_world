# Copyright 2025 The android_world Authors.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

from unittest import mock

from absl.testing import absltest
from android_world.env import interface
from android_world.env import representation_utils
import numpy as np


class InterfaceTest(absltest.TestCase):

  @mock.patch("time.sleep", return_value=None)
  def test_ui_stability_true(self, unused_mocked_time_sleep):
    stable_ui_elements = [representation_utils.UIElement(text="StableElement")]
    states = [
        interface.State(
            ui_elements=stable_ui_elements,
            pixels=np.empty([1, 2, 3]),
            forest=None,
        )
        for _ in range(4)
    ]
    env = interface.AsyncAndroidEnv(mock.MagicMock())
    env._get_state = mock.MagicMock(side_effect=states)

    self.assertEqual(
        env._get_stable_state(
            stability_threshold=3, sleep_duration=0.1, timeout=1
        ),
        states[2],
    )

  @mock.patch('android_world.env.interface.time.sleep')
  @mock.patch('android_world.env.interface.time.time')
  def test_ui_stability_false_due_to_timeout(
      self, mocked_time, mocked_sleep
  ):
    clock = [0.0]
    mocked_time.side_effect = lambda: clock[0]
    mocked_sleep.side_effect = lambda duration: clock.__setitem__(
        0, clock[0] + duration
    )
    changing_ui_elements = [
        representation_utils.UIElement(text=f"Element{i}") for i in range(10)
    ]
    env = interface.AsyncAndroidEnv(mock.MagicMock())
    states = [
        interface.State(
            ui_elements=[elem], pixels=np.empty([1, 2, 3]), forest=None
        )
        for elem in changing_ui_elements
    ]
    env._get_state = mock.MagicMock(side_effect=states)
    self.assertEqual(
        env._get_stable_state(
            stability_threshold=3, sleep_duration=0.1, timeout=0.41
        ),
        states[5],
    )

  @mock.patch("time.sleep", return_value=None)
  def test_stability_fluctuates(self, unused_mocked_time_sleep):
    env = interface.AsyncAndroidEnv(mock.MagicMock())
    fluctuating_ui_elements = (
        [representation_utils.UIElement(text="Stable")] * 2
        + [representation_utils.UIElement(text="Unstable")]
        + [representation_utils.UIElement(text="Stable")] * 3
        + [representation_utils.UIElement(text="Unstable")]
    )
    states = [
        interface.State(
            ui_elements=[elem], pixels=np.empty([1, 2, 3]), forest=None
        )
        for elem in fluctuating_ui_elements
    ]
    env._get_state = mock.MagicMock(side_effect=states)
    cur = env._get_stable_state(
        stability_threshold=3, sleep_duration=0.5, timeout=2.5
    )
    self.assertEqual(
        cur,
        states[5],
    )

  def test_get_state_defaults_to_omniflow_oob_get_state(self):
    payload = {
        'success': True,
        'xml': (
            '<hierarchy>'
            '<node text="OOB Button" bounds="[1,2][3,4]" enabled="true" />'
            '</hierarchy>'
        ),
        'display_width': 10,
        'display_height': 20,
        'package_name': 'com.example',
        'activity_name': 'com.example/.MainActivity',
    }
    controller = mock.MagicMock()
    controller.logical_screen_size = (10, 20)
    env = interface.AsyncAndroidEnv(controller)

    with mock.patch.dict(
        'os.environ',
        {
            'OMNIFLOW_OBSERVE_BACKEND': 'oob',
            'OMNIFLOW_OOB_DEVICE_URL': '',
        },
        clear=False,
    ), mock.patch.object(
        interface,
        '_omniflow_read_oob_debug_get_state',
        return_value=payload,
    ) as read_oob:
      state = env.get_state()

    read_oob.assert_called_once_with(controller)
    controller.step.assert_not_called()
    self.assertEqual(state.auxiliaries['observe_backend'], 'oob_get_state')
    self.assertEqual(state.auxiliaries['package_name'], 'com.example')
    self.assertEqual(state.ui_elements[0].text, 'OOB Button')
    self.assertEqual(state.pixels.shape, (20, 10, 3))


if __name__ == "__main__":
  absltest.main()
