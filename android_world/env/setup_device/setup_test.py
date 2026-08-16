# Copyright 2026 The android_world Authors.
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
from android_env.components import errors
from android_env.proto import adb_pb2
from android_world.env import adb_utils
from android_world.env import android_world_controller
from android_world.env import interface
from android_world.env import tools
from android_world.env.setup_device import apps
from android_world.env.setup_device import setup
from android_world.utils import app_snapshot


class GetAppListToSetupTest(absltest.TestCase):

  def test_get_app_list_to_setup_none(self):
    self.assertIsNone(setup.get_app_list_to_setup(None))

  def test_get_app_list_to_setup_with_valid_ids(self):
    task_ids = ["ClockCreateTimer", "ContactsSearchContact"]
    expected_apps = (apps.ClockApp, apps.ContactsApp)
    self.assertCountEqual(setup.get_app_list_to_setup(task_ids), expected_apps)

  def test_get_app_list_to_setup_with_mixed_ids(self):
    task_ids = ["ClockCreateTimer", "InvalidTask", "DialerCallNumber"]
    expected_apps = (apps.ClockApp, apps.DialerApp)
    self.assertCountEqual(setup.get_app_list_to_setup(task_ids), expected_apps)

  def test_get_app_list_to_setup_falls_back_for_parameterized_task(self):
    self.assertIsNone(setup.get_app_list_to_setup(["OpenAppTaskEval"]))

  def test_get_app_list_to_setup_with_space_in_app_name(self):
    task_ids = ["AudioRecorderRecordAudio"]
    expected_apps = (apps.AudioRecorder,)
    self.assertCountEqual(setup.get_app_list_to_setup(task_ids), expected_apps)

  def test_get_app_list_to_setup_with_pascal_case_conversion(self):
    task_ids = ["SimpleCalendarProCreateEvent"]
    expected_apps = (apps.SimpleCalendarProApp,)
    self.assertCountEqual(setup.get_app_list_to_setup(task_ids), expected_apps)


class SetupTest(absltest.TestCase):

  def setUp(self):
    super().setUp()
    self.mock_issue_generic_request = self.enter_context(
        mock.patch.object(adb_utils, "issue_generic_request")
    )

  def tearDown(self):
    mock.patch.stopall()
    super().tearDown()

  @mock.patch.object(tools, "AndroidToolController")
  @mock.patch.object(setup, "download_and_install_apk")
  @mock.patch.object(app_snapshot, "save_snapshot")
  def test_setup_apps(self, mock_save_snapshot, mock_install_apk, unused_tools):
    env = mock.create_autospec(interface.AsyncEnv)
    mock_app_setups = {
        app_class: mock.patch.object(app_class, "setup").start()
        for app_class in setup._APPS
    }

    setup.setup_apps(env)

    for app_class in setup._APPS:
      if app_class.apk_names:  # 1P apps do not have APKs.
        mock_install_apk.assert_any_call(
            app_class.apk_names[0], env.controller.env
        )
      mock_app_setups[app_class].assert_any_call(env)
      mock_save_snapshot.assert_any_call(app_class.app_name, env.controller)

  @mock.patch.object(app_snapshot, "save_snapshot")
  def test_setup_app_retries_with_uiautomator_before_saving_snapshot(
      self, mock_save_snapshot
  ):
    controller = mock.Mock()
    controller._a11y_method = (
        android_world_controller.A11yMethod.A11Y_FORWARDER_APP
    )
    env = mock.Mock(controller=controller)
    seen_a11y_methods = []

    class RetryApp(apps.AppSetup):
      app_name = "retry_app"

      @classmethod
      def setup(cls, unused_env):
        seen_a11y_methods.append(controller._a11y_method)
        if len(seen_a11y_methods) == 1:
          raise ValueError('Target text "Skip" not found.')

    setup.setup_app(RetryApp, env)

    self.assertEqual(
        seen_a11y_methods,
        [
            android_world_controller.A11yMethod.A11Y_FORWARDER_APP,
            android_world_controller.A11yMethod.UIAUTOMATOR,
        ],
    )
    self.assertEqual(
        controller._a11y_method,
        android_world_controller.A11yMethod.A11Y_FORWARDER_APP,
    )
    mock_save_snapshot.assert_called_once_with(
        RetryApp.app_name, env.controller
    )

  @mock.patch.object(app_snapshot, "save_snapshot")
  def test_setup_app_does_not_save_snapshot_when_fallback_fails(
      self, mock_save_snapshot
  ):
    controller = mock.Mock()
    controller._a11y_method = (
        android_world_controller.A11yMethod.A11Y_FORWARDER_APP
    )
    env = mock.Mock(controller=controller)
    seen_a11y_methods = []

    class FailingApp(apps.AppSetup):
      app_name = "failing_app"

      @classmethod
      def setup(cls, unused_env):
        seen_a11y_methods.append(controller._a11y_method)
        raise ValueError('Target text "Skip" not found.')

    with self.assertRaisesRegex(ValueError, 'Target text'):
      setup.setup_app(FailingApp, env)

    self.assertEqual(
        seen_a11y_methods,
        [
            android_world_controller.A11yMethod.A11Y_FORWARDER_APP,
            android_world_controller.A11yMethod.UIAUTOMATOR,
        ],
    )
    self.assertEqual(
        controller._a11y_method,
        android_world_controller.A11yMethod.A11Y_FORWARDER_APP,
    )
    mock_save_snapshot.assert_not_called()


class _App(apps.AppSetup):

  def __init__(self, apk_names, app_name):
    self.apk_names = apk_names
    self.app_name = app_name


class InstallApksTest(absltest.TestCase):

  def setUp(self):
    super().setUp()
    self.env = mock.create_autospec(interface.AsyncEnv)
    self.mock_issue_generic_request = self.enter_context(
        mock.patch.object(adb_utils, "issue_generic_request")
    )
    self.mockdownload_and_install_apk = self.enter_context(
        mock.patch.object(setup, "download_and_install_apk")
    )
    self.apps = [
        _App(apk_names=["apk1", "apk2"], app_name="App1"),
        _App(apk_names=[], app_name="App2"),  # No APKs
        _App(apk_names=["apk3"], app_name="App3"),
    ]
    setup._APPS = self.apps

  def test_install_all_apks_success(self):
    self.mockdownload_and_install_apk.return_value = None

    for app in self.apps:
      setup.maybe_install_app(app, self.env)

    expected_calls = [
        mock.call("apk1", self.env.controller.env),
        mock.call("apk3", self.env.controller.env),
    ]
    self.mockdownload_and_install_apk.assert_has_calls(
        expected_calls, any_order=True
    )

  def test_install_all_apks_success_with_fallback(self):
    def side_effect(apk_name, env):
      del env
      if apk_name == "apk1":
        raise errors.AdbControllerError
      return None

    self.mockdownload_and_install_apk.side_effect = side_effect

    for app in self.apps:
      setup.maybe_install_app(app, self.env)

    expected_calls = [
        mock.call("apk1", self.env.controller.env),
        mock.call("apk2", self.env.controller.env),
        mock.call("apk3", self.env.controller.env),
    ]
    self.mockdownload_and_install_apk.assert_has_calls(expected_calls)

  def test_vlc_prefers_x86_apk_on_x86_device(self):
    response = adb_pb2.AdbResponse(status=adb_pb2.AdbResponse.Status.OK)
    response.generic.output = b"x86_64\n"
    self.mock_issue_generic_request.return_value = response

    setup.maybe_install_app(apps.VlcApp, self.env)

    self.mockdownload_and_install_apk.assert_called_once_with(
        "org.videolan.vlc_13050408.apk", self.env.controller.env
    )

  def test_vlc_prefers_arm_apk_on_arm_device(self):
    response = adb_pb2.AdbResponse(status=adb_pb2.AdbResponse.Status.OK)
    response.generic.output = b"arm64-v8a\n"
    self.mock_issue_generic_request.return_value = response

    setup.maybe_install_app(apps.VlcApp, self.env)

    self.mockdownload_and_install_apk.assert_called_once_with(
        "org.videolan.vlc_13050407.apk", self.env.controller.env
    )


class VlcSetupTest(absltest.TestCase):

  def setUp(self):
    super().setUp()
    self.env = mock.create_autospec(interface.AsyncEnv)
    self.enter_context(mock.patch.object(apps.AppSetup, "setup"))
    self.enter_context(mock.patch.object(apps.time, "sleep"))
    self.enter_context(
        mock.patch.object(
            adb_utils, "get_adb_activity", return_value="org.videolan.vlc/.Start"
        )
    )
    self.enter_context(
        mock.patch.object(
            adb_utils, "extract_package_name", return_value="org.videolan.vlc"
        )
    )
    self.enter_context(mock.patch.object(adb_utils, "grant_permissions"))
    self.enter_context(mock.patch.object(adb_utils, "issue_generic_request"))
    self.enter_context(mock.patch.object(adb_utils, "close_app"))
    self.enter_context(
        mock.patch.object(
            apps.file_utils, "check_directory_exists", return_value=True
        )
    )
    controller_class = self.enter_context(
        mock.patch.object(tools, "AndroidToolController")
    )
    self.controller = controller_class.return_value

  def test_vlc_setup_accepts_home_screen_after_skip(self):
    apps.VlcApp.setup(self.env)

    self.controller.click_resource_id.assert_called_once_with(
        "org.videolan.vlc:id/skip_button"
    )
    self.controller.wait_for_resource_id.assert_called_once_with(
        "org.videolan.vlc:id/main_toolbar", timeout_sec=2.0
    )
    self.controller.click_element.assert_not_called()


class ClipperSetupTest(absltest.TestCase):

  def setUp(self):
    super().setUp()
    self.env = mock.create_autospec(interface.AsyncEnv)
    self.enter_context(mock.patch.object(apps.AppSetup, "setup"))
    self.enter_context(mock.patch.object(apps.time, "sleep"))
    self.mock_launch_app = self.enter_context(
        mock.patch.object(adb_utils, "launch_app")
    )
    self.mock_clear_permission_review = self.enter_context(
        mock.patch.object(adb_utils, "clear_legacy_permission_review_flags")
    )
    self.mock_close_app = self.enter_context(
        mock.patch.object(adb_utils, "close_app")
    )

  def test_setup_clears_permission_review_before_launch(self):
    def require_permission_review_cleared(*unused_args):
      self.mock_clear_permission_review.assert_called_once_with(
          apps.ClipperApp.package_name(), self.env.controller
      )

    self.mock_launch_app.side_effect = require_permission_review_cleared

    apps.ClipperApp.setup(self.env)

    self.mock_launch_app.assert_called_once_with(
        apps.ClipperApp.app_name, self.env.controller
    )
    self.mock_close_app.assert_called_once_with(
        apps.ClipperApp.app_name, self.env.controller
    )


class ExpenseSetupTest(absltest.TestCase):

  def setUp(self):
    super().setUp()
    self.env = mock.create_autospec(interface.AsyncEnv)
    self.enter_context(mock.patch.object(apps.AppSetup, "setup"))
    self.enter_context(mock.patch.object(apps.time, "sleep"))
    self.enter_context(mock.patch.object(adb_utils, "launch_app"))
    self.enter_context(mock.patch.object(adb_utils, "close_app"))
    controller_class = self.enter_context(
        mock.patch.object(tools, "AndroidToolController")
    )
    self.controller = controller_class.return_value

  def test_setup_waits_for_onboarding_and_initialized_home(self):
    apps.ExpenseApp.setup(self.env)

    self.assertEqual(
        self.controller.click_resource_id.call_args_list,
        [
            mock.call("com.arduia.expense:id/btn_continue"),
            mock.call("com.arduia.expense:id/btn_continue"),
        ],
    )
    self.controller.wait_for_resource_id.assert_called_once_with(
        "com.arduia.expense:id/fb_main_add", timeout_sec=10.0
    )
    self.controller.click_element.assert_not_called()


if __name__ == "__main__":
  absltest.main()
