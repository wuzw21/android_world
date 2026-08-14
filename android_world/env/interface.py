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

"""Environment interface for real-time interaction Android."""

import abc
import base64
import dataclasses
import io
import json
import os
import subprocess
import time
from typing import Any, Optional, Self
import urllib.parse
import urllib.request

from absl import logging
from android_env.components import action_type
from android_world.env import actuation
from android_world.env import adb_utils
from android_world.env import android_world_controller
from android_world.env import json_action
from android_world.env import representation_utils
import dm_env
import numpy as np


def _get_no_op_action() -> dict[str, Any]:
  """Creates a no-op action; used to retrieve screen & UI tree."""
  return {
      'action_type': np.array(action_type.ActionType.LIFT, dtype=np.int32),
      'touch_position': np.array((0.0, 0.0)),
  }


@dataclasses.dataclass(frozen=True)
class State:
  """State of the Android environment.

  Attributes:
    pixels: RGB array of current screen.
    forest: Raw UI forest; see android_world_controller.py for more info.
    ui_elements: Processed children and stateful UI elements extracted from
      forest.
    auxiliaries: Additional information about the state.
  """

  pixels: np.ndarray
  forest: Any
  ui_elements: list[representation_utils.UIElement]
  auxiliaries: dict[str, Any] | None = None

  @classmethod
  def create_and_infer_elements(
      cls,
      pixels: np.ndarray,
      forest: Any,
      screen_size: Optional[tuple[int, int]] = None,
  ) -> Self:
    """Creates a new instance, inferring UI elements from the forest."""

    elements = representation_utils.forest_to_ui_elements(
        forest, screen_size=screen_size
    )
    return cls(pixels, forest, elements)


def _omniflow_use_oob_get_state() -> bool:
  raw_backend = (
      os.environ.get('OMNIFLOW_OBSERVE_BACKEND', 'androidworld')
      .strip()
      .lower()
      .replace('-', '_')
  )
  return raw_backend in {'oob', 'oob_native', 'oob_http', 'oob_get_state'}


def _omniflow_controller_adb_serial(
    controller: android_world_controller.AndroidWorldController,
) -> str:
  serial = os.environ.get('ANDROID_SERIAL', '').strip()
  if serial:
    return serial
  try:
    port = (
        controller.env._coordinator._simulator._config.emulator_launcher.emulator_console_port
    )
    if port:
      return f'emulator-{int(port)}'
  except Exception:  # pylint: disable=broad-exception-caught
    pass
  return ''


def _omniflow_run_adb(
    controller: android_world_controller.AndroidWorldController,
    adb_args: list[str],
    *,
    timeout_sec: float = 30.0,
) -> subprocess.CompletedProcess[str]:
  adb = os.environ.get('ADB_PATH') or 'adb'
  command = [adb]
  serial = _omniflow_controller_adb_serial(controller)
  if serial:
    command.extend(['-s', serial])
  command.extend(adb_args)
  return subprocess.run(
      command,
      text=True,
      stdout=subprocess.PIPE,
      stderr=subprocess.PIPE,
      check=False,
      timeout=timeout_sec,
  )


def _omniflow_read_oob_debug_get_state(
    controller: android_world_controller.AndroidWorldController,
) -> dict[str, Any]:
  package_name = os.environ.get(
      'OMNIFLOW_OOB_PACKAGE', 'cn.com.omnimind.bot.debug'
  ).strip()
  receiver = os.environ.get(
      'OMNIFLOW_OOB_GET_STATE_RECEIVER', '.DebugGetStateReceiver'
  ).strip()
  if receiver.startswith('.'):
    component = f'{package_name}/{receiver}'
  elif '/' in receiver:
    component = receiver
  else:
    component = f'{package_name}/.{receiver}'
  result_path = 'files/debug-get-state-result.json'
  _omniflow_run_adb(
      controller,
      ['shell', 'run-as', package_name, 'rm', '-f', result_path],
      timeout_sec=10.0,
  )
  broadcast = _omniflow_run_adb(
      controller,
      [
          'shell',
          'am',
          'broadcast',
          '-a',
          f'{package_name}.RUN_GET_STATE',
          '-n',
          component,
          '--ez',
          'includeXml',
          'true',
          '--ez',
          'includeScreenshot',
          'true',
          '--ez',
          'includeIndexedContext',
          'false',
          '--ei',
          'maxXmlChars',
          '200000',
      ],
      timeout_sec=30.0,
  )
  if broadcast.returncode != 0:
    return {
        'success': False,
        'error': 'OOB debug get_state broadcast failed: '
        + (broadcast.stderr or broadcast.stdout or '').strip(),
    }
  deadline = time.monotonic() + 30.0
  last_error = ''
  while time.monotonic() < deadline:
    read_result = _omniflow_run_adb(
        controller,
        ['shell', 'run-as', package_name, 'cat', result_path],
        timeout_sec=10.0,
    )
    stdout = str(read_result.stdout or '').strip()
    if read_result.returncode == 0 and stdout:
      try:
        payload = json.loads(stdout)
      except json.JSONDecodeError as exc:
        return {
            'success': False,
            'error': f'OOB debug get_state returned invalid JSON: {exc}',
            'raw_tail': stdout[-1000:],
        }
      if isinstance(payload, dict):
        return payload
      return {'success': False, 'error': 'OOB debug get_state returned non-object JSON'}
    last_error = (read_result.stderr or read_result.stdout or '').strip()
    time.sleep(0.5)
  return {
      'success': False,
      'error': 'OOB debug get_state result was not written: ' + last_error[-500:],
  }


def _omniflow_read_oob_get_state(
    controller: android_world_controller.AndroidWorldController,
) -> dict[str, Any]:
  oob_url = os.environ.get('OMNIFLOW_OOB_DEVICE_URL', '').strip().rstrip('/')
  if not oob_url:
    return _omniflow_read_oob_debug_get_state(controller)
  query = urllib.parse.urlencode({
      'includeXml': 'true',
      'includeScreenshot': 'true',
      'includeIndexedContext': 'false',
      'maxXmlChars': '200000',
      'filterOverlay': 'true',
  })
  opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
  with opener.open(f'{oob_url}/get_state?{query}', timeout=10) as response:
    payload = json.loads(response.read().decode('utf-8'))
  return payload if isinstance(payload, dict) else {}


def _omniflow_int_payload(
    payload: dict[str, Any],
    keys: tuple[str, ...],
    default_value: int,
) -> int:
  for key in keys:
    try:
      value = int(payload.get(key) or 0)
    except (TypeError, ValueError):
      value = 0
    if value > 0:
      return value
  return default_value


def _omniflow_blank_pixels(
    payload: dict[str, Any],
    controller: android_world_controller.AndroidWorldController,
) -> np.ndarray:
  fallback_width = 1
  fallback_height = 1
  try:
    fallback_width, fallback_height = controller.logical_screen_size
  except Exception:  # pylint: disable=broad-exception-caught
    pass
  width = _omniflow_int_payload(
      payload,
      ('display_width', 'xml_display_width', 'width'),
      int(fallback_width or 1),
  )
  height = _omniflow_int_payload(
      payload,
      ('display_height', 'xml_display_height', 'height'),
      int(fallback_height or 1),
  )
  return np.zeros((max(1, height), max(1, width), 3), dtype=np.uint8)


def _omniflow_decode_oob_pixels(
    payload: dict[str, Any],
    controller: android_world_controller.AndroidWorldController,
) -> np.ndarray:
  screenshot = payload.get('screenshot')
  if isinstance(screenshot, dict):
    encoded = (
        screenshot.get('data')
        or screenshot.get('data_uri')
        or screenshot.get('dataUri')
        or screenshot.get('image_base64')
    )
  else:
    encoded = screenshot if isinstance(screenshot, str) else ''
  if isinstance(encoded, str) and encoded.strip():
    raw = encoded.strip()
    if raw.startswith('data:image/') and ',' in raw:
      raw = raw.split(',', 1)[1]
    try:
      image_bytes = base64.b64decode(raw, validate=False)
      from PIL import Image  # pylint: disable=import-outside-toplevel

      image = Image.open(io.BytesIO(image_bytes)).convert('RGB')
      return np.asarray(image)
    except Exception as exc:  # pylint: disable=broad-exception-caught
      logging.warning('OmniFlow OOB screenshot decode failed: %s', exc)
  return _omniflow_blank_pixels(payload, controller)


def _omniflow_oob_state(
    controller: android_world_controller.AndroidWorldController,
) -> State | None:
  if not _omniflow_use_oob_get_state():
    return None
  try:
    payload = _omniflow_read_oob_get_state(controller)
    if payload.get('success') is False:
      logging.warning('OmniFlow OOB get_state failed: %s', payload)
      return None
    xml_text = str(payload.get('xml') or '').strip()
    if not xml_text:
      logging.warning('OmniFlow OOB get_state returned empty XML.')
      return None
    ui_elements = representation_utils.xml_dump_to_ui_elements(xml_text)
    pixels = _omniflow_decode_oob_pixels(payload, controller)
    return State(
        pixels=pixels,
        forest=None,
        ui_elements=ui_elements,
        auxiliaries={
            'observe_backend': 'oob_get_state',
            'package_name': str(payload.get('package_name') or ''),
            'activity_name': str(payload.get('activity_name') or ''),
            'xml': xml_text,
            'raw_state': payload,
        },
    )
  except Exception as exc:  # pylint: disable=broad-exception-caught
    logging.warning('OmniFlow OOB get_state failed: %s', exc)
    return None


class AsyncEnv(abc.ABC):
  """Interface for interacting with a real-time Android device.

  Computing environments, such as Android, run in real-time, independently of
  the agent interacting with it. All observations and actions are asynchronous
  and OS does not pause when providing observations or when accepting actions.
  Changes from action execution may take some time to appear.
  """

  @property
  @abc.abstractmethod
  def controller(self) -> android_world_controller.AndroidWorldController:
    """Returns the controller for the environment."""

  @abc.abstractmethod
  def reset(self, go_home: bool = False) -> State:
    """Go home on reset.

    Args:
      go_home: Whether to go home during the reset.
    """

  @abc.abstractmethod
  def get_state(self, wait_to_stabilize: bool = False) -> State:
    """Gets the state of the environment; i.e., screenshot & UI tree.

    In practice this will usually be called after executing an action. Logic
    should be implemented, perhaps a simple time.sleep, to ensure the
    environment updates after the action.

    Args:
      wait_to_stabilize: Whether to wait for the screen to stabilize before
        returning state.

    Returns:
      Observation containing RGB array of screen, the accessibility forest,
        and UI elements derived from the forest. See android_world_controller.py
        for
        more detail.
    """

  def display_message(self, message: str, header: str = '') -> None:
    """Displays a message on the screen."""

  @abc.abstractmethod
  def ask_question(
      self, question: str, timeout_seconds: float = -1.0
  ) -> str | None:
    """Asks a question to a hypothetical user in the environment.

    Common uses are to ask a question to clarify the user-provided goal, to ask
    for help when the agent is stuck, or when there is ambiguity in the current
    screen.

    Args:
      question: The question to ask the user.
      timeout_seconds: The timeout in seconds to wait for a response. If
        negative, then wait indefinitely.

    Returns:
      The response from the user or None if the user did not answer within the
      timeout.
    """

  @abc.abstractmethod
  def execute_action(self, action: json_action.JSONAction) -> None:
    """Executes action on the environment."""

  @property
  @abc.abstractmethod
  def foreground_activity_name(self) -> str:
    """Returns the activity name of the app currently opened in foreground."""

  @property
  @abc.abstractmethod
  def device_screen_size(self) -> tuple[int, int]:
    """Returns the screen size of the environment in pixels: (width, height)."""

  @property
  @abc.abstractmethod
  def logical_screen_size(self) -> tuple[int, int]:
    """Retrieves the logical screen size of the Android device.

    While the physical size is a fixed attribute of the display, the logical
    size is flexible and varies based on system settings such as the orientation
    or if the resolution is changed.

    Returns: The (width, height) in pixels, denoting the logical dimensions of
    the screen. Width and height values are aligned with the device's current
    orientation, meaning width is always logical horizontal direction (like in
    the landscape orientation width will be the physical vertical direction).
    """

  @abc.abstractmethod
  def close(self) -> None:
    """Closes the environment."""

  @property
  @abc.abstractmethod
  def interaction_cache(self) -> str:
    """Returns the interaction cache of the environment."""

  @abc.abstractmethod
  def hide_automation_ui(self) -> None:
    """Hides any UI, such as screen coordinates,."""

  @property
  @abc.abstractmethod
  def orientation(self) -> int:
    """Returns the orientation of the environment.

    Returns: 0 for portrait, 1 for landscape, 2 for reverse portrait,
    3 for reverse landscape.
    """

  @property
  @abc.abstractmethod
  def physical_frame_boundary(self) -> tuple[int, int, int, int]:
    """Returns the physical frame boundary of the environment.

    Returns: First two integers are the coordinates for top left corner, last
    two are for lower right corner. All coordinates are given in portrait
    orientation.
    """


def _process_timestep(timestep: dm_env.TimeStep) -> State:
  """Parses timestep observation and returns State."""
  return State(
      pixels=timestep.observation['pixels'],
      forest=timestep.observation[
          android_world_controller.OBSERVATION_KEY_FOREST
      ],
      ui_elements=timestep.observation[
          android_world_controller.OBSERVATION_KEY_UI_ELEMENTS
      ],
      auxiliaries={},
  )


class AsyncAndroidEnv(AsyncEnv):
  """Async environment interface using AndroidEnv to communicate with device."""

  interaction_cache = ''

  def __init__(
      self, controller: android_world_controller.AndroidWorldController
  ):
    self._controller = controller
    self._prior_state = None
    # Variable used to temporarily save interactions between agent and user.
    # Like when agent use answer action to answer user questions, we
    # use this to save the agent response. Or later on when agent has the
    # ability to ask user question, user's answer will be saved here as well.
    self.interaction_cache = ''

  @property
  def controller(self) -> android_world_controller.AndroidWorldController:
    return self._controller

  def reset(self, go_home: bool = False) -> State:
    if go_home:
      adb_utils.press_home_button(self.controller)
    self.interaction_cache = ''

    return _process_timestep(self.controller.reset())

  def _get_state(self):
    oob_state = _omniflow_oob_state(self.controller)
    if oob_state is not None:
      return oob_state
    return _process_timestep(self.controller.step(_get_no_op_action()))

  def _get_stable_state(
      self,
      stability_threshold: int = 3,
      sleep_duration: float = 0.5,
      timeout: float = 6.0,
  ) -> State:
    """Checks if the UI elements remain stable over a number of checks and returns the state.

    Args:
        stability_threshold: Number of consecutive checks where UI elements must
          remain the same to consider UI stable.
        sleep_duration: Minimum time in seconds between each check.
        timeout: Maximum time in seconds to wait for UI to become stable before
          giving up.

    Returns:
        The current state of the UI if stability is achieved within the timeout.
    """
    if not self._prior_state:
      self._prior_state = self._get_state()
    if stability_threshold <= 0:
      raise ValueError('Stability threshold must be a positive integer.')

    stable_checks = 1
    start_time = time.time()
    deadline = start_time + timeout

    while stable_checks < stability_threshold and time.time() < deadline:
      iteration_start_time = time.time()
      current_state = self._get_state()

      if self._prior_state.ui_elements == current_state.ui_elements:
        stable_checks += 1
        if stable_checks == stability_threshold:
          break  # Exit early if stability is achieved.
      else:
        stable_checks = 1  # Reset if any change is detected
        self._prior_state = current_state

      elapsed_time = time.time() - iteration_start_time
      remaining_sleep = sleep_duration - elapsed_time
      if remaining_sleep > 0:
        sleep_time = min(remaining_sleep, deadline - time.time())
        if sleep_time > 0:
          time.sleep(sleep_time)
      # If remaining_sleep <= 0, proceed immediately to the next iteration

    return current_state  # pylint: disable=undefined-variable

  def get_state(self, wait_to_stabilize: bool = False) -> State:
    if wait_to_stabilize:
      return self._get_stable_state()
    return self._get_state()

  def execute_action(self, action: json_action.JSONAction) -> None:
    if action.action_type == json_action.ANSWER:
      self.interaction_cache = action.text
      if action.text:
        self.display_message(action.text, header='Agent answered:')
      return
    if action.action_type == json_action.STATUS:
      # Do nothing if it is a termination action.
      return
    state = self.get_state(wait_to_stabilize=False)
    actuation.execute_adb_action(
        action,
        state.ui_elements,
        self.logical_screen_size,
        self.controller,
    )

  def hide_automation_ui(self) -> None:
    """Hides the coordinates on screen."""
    adb_utils.issue_generic_request(
        'shell settings put system pointer_location 0', self.controller
    )

  def display_message(self, message: str, header: str = '') -> None:
    adb_utils.send_android_intent(
        command='broadcast',
        action='com.example.ACTION_UPDATE_OVERLAY',
        env=self.controller,
        extras={'task_type_string': header, 'goal_string': message},
    )

  def ask_question(
      self, question: str, timeout_seconds: float = -1.0
  ) -> str | None:
    raise NotImplementedError('ask_question is not implemented.')

  @property
  def foreground_activity_name(self) -> str:
    activity = adb_utils.get_current_activity(self.controller)[0]
    if activity:
      return activity
    else:
      return ''

  @property
  def device_screen_size(self) -> tuple[int, int]:
    return self.controller.device_screen_size

  @property
  def logical_screen_size(self) -> tuple[int, int]:
    return adb_utils.get_logical_screen_size(self.controller)

  def close(self) -> None:
    try:
      self.controller.close()
    except:  # pylint: disable=bare-except
      logging.warning('Failed to close controller. Continuing.')

  @property
  def orientation(self) -> int:
    return adb_utils.get_orientation(self.controller)

  @property
  def physical_frame_boundary(self) -> tuple[int, int, int, int]:
    return adb_utils.get_physical_frame_boundary(self.controller)
