"""Building the server in a container.

Nothing here runs podman, for `test_web.py`'s reason. The isolation flags are
what this module exists to provide, so they are asserted one by one.
"""

import json
import os
import tempfile
import unittest
from unittest import mock

from stdjflib import jfbuild


def _ok(*_args, **_kw):
    return mock.Mock(returncode=0, stderr="", stdout="")


class TestIsolation(unittest.TestCase):
    def setUp(self):
        self.out = tempfile.TemporaryDirectory()
        self.addCleanup(self.out.cleanup)
        os.makedirs(os.path.join(self.out.name, "publish"))
        open(os.path.join(self.out.name, "publish", "jellyfin"), "w").close()

    def argv(self) -> list:
        with mock.patch("shutil.which", return_value="/usr/bin/podman"), \
             mock.patch("subprocess.run", side_effect=_ok) as run:
            jfbuild.build("/src/jellyfin", self.out.name, say=lambda *a: None)
        return run.call_args_list[0].args[0]

    def test_the_source_is_mounted_read_only(self):
        self.assertIn("/src/jellyfin:/src:ro", self.argv())

    def test_capabilities_are_dropped(self):
        argv = self.argv()
        self.assertIn("--cap-drop=ALL", argv)
        self.assertIn("no-new-privileges", argv)

    def test_the_only_writable_mount_is_the_output(self):
        argv = self.argv()
        writable = [argv[i + 1] for i, a in enumerate(argv)
                    if a == "-v" and not argv[i + 1].endswith(":ro")]
        self.assertEqual(writable, [f"{self.out.name}:/out"])

    def test_the_nuget_cache_lives_in_the_output(self):
        self.assertIn("NUGET_PACKAGES=/out/nuget", self.argv())

    def test_the_copy_does_not_try_to_restore_owners(self):
        """Without CAP_CHOWN, tar as root fails on the first unmapped group."""
        self.assertIn("--no-same-owner", jfbuild.SCRIPT)

    def test_old_build_output_in_the_checkout_is_left_behind(self):
        self.assertIn("--exclude=obj", jfbuild.SCRIPT)
        self.assertIn("--exclude=bin", jfbuild.SCRIPT)

    def test_the_container_builds_somewhere_it_can_write(self):
        self.assertIn("/tmp/build", jfbuild.SCRIPT)

    def test_the_publish_is_self_contained(self):
        """The official image's entrypoint is the apphost; there is no
        shared runtime in it to fall back on."""
        self.assertIn("--self-contained", jfbuild.SCRIPT)

    def test_the_output_is_moved_into_place_rather_than_written_in_place(self):
        """A half-written publish must never look like a finished one."""
        self.assertLess(jfbuild.SCRIPT.index("-o /out/publish.part"),
                        jfbuild.SCRIPT.index("mv /out/publish.part /out/publish"))

    def test_no_working_directory_is_preset(self):
        self.assertNotIn("-w", self.argv())


class TestSdkImage(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)

    def _global(self, text):
        with open(os.path.join(self.dir.name, "global.json"), "w") as fh:
            fh.write(text)
        return jfbuild.sdk_image(self.dir.name)

    def test_follows_global_json(self):
        self.assertEqual(self._global('{"sdk": {"version": "10.0.0"}}'),
                         "mcr.microsoft.com/dotnet/sdk:10.0")
        self.assertEqual(self._global('{"sdk": {"version": "11.0.100"}}'),
                         "mcr.microsoft.com/dotnet/sdk:11.0")

    def test_falls_back_rather_than_failing(self):
        default = f"mcr.microsoft.com/dotnet/sdk:{jfbuild.DEFAULT_SDK}"
        self.assertEqual(jfbuild.sdk_image(self.dir.name), default)
        self.assertEqual(self._global("not json"), default)
        self.assertEqual(self._global('{"sdk": {}}'), default)


class TestRuntimeIdentifier(unittest.TestCase):
    def test_known_machines(self):
        for machine, rid in (("x86_64", "linux-x64"), ("aarch64", "linux-arm64")):
            with self.subTest(machine), \
                 mock.patch("platform.machine", return_value=machine):
                self.assertEqual(jfbuild.runtime_identifier(), rid)

    def test_an_unknown_machine_says_so(self):
        with mock.patch("platform.machine", return_value="sparc64"):
            with self.assertRaises(jfbuild.BuildFailed):
                jfbuild.runtime_identifier()


class TestCache(unittest.TestCase):
    def setUp(self):
        self.out = tempfile.TemporaryDirectory()
        self.src = tempfile.TemporaryDirectory()
        self.addCleanup(self.out.cleanup)
        self.addCleanup(self.src.cleanup)

    def _built(self, revision="abc"):
        os.makedirs(jfbuild.publish_dir(self.out.name), exist_ok=True)
        open(os.path.join(jfbuild.publish_dir(self.out.name), "jellyfin"), "w").close()
        with mock.patch("stdjflib.web.revision", return_value=revision):
            stamp = jfbuild._wanted(self.src.name)
        with open(jfbuild._stamp_path(self.out.name), "w") as fh:
            json.dump(stamp, fh)

    def _current(self, revision="abc"):
        with mock.patch("stdjflib.web.revision", return_value=revision):
            return jfbuild.is_current(self.out.name, self.src.name)

    def test_nothing_built_is_not_current(self):
        self.assertFalse(self._current())

    def test_a_matching_build_is_current(self):
        self._built()
        self.assertTrue(self._current())

    def test_a_new_commit_or_a_dirty_tree_rebuilds(self):
        self._built("abc")
        self.assertFalse(self._current("def"))
        self.assertFalse(self._current("abc-dirty"))

    def test_a_different_sdk_rebuilds(self):
        self._built()
        with open(os.path.join(self.src.name, "global.json"), "w") as fh:
            fh.write('{"sdk": {"version": "11.0.0"}}')
        self.assertFalse(self._current())


class TestFailures(unittest.TestCase):
    def test_no_podman_raises(self):
        with mock.patch("shutil.which", return_value=None):
            with self.assertRaises(jfbuild.BuildFailed):
                jfbuild.build("/src", tempfile.mkdtemp(), say=lambda *a: None)

    def test_a_failed_build_names_the_error_lines(self):
        failed = mock.Mock(returncode=1, stderr="",
                           stdout="noise\nFoo.cs(1,1): error CS1002: ; expected\n")
        with mock.patch("shutil.which", return_value="/usr/bin/podman"), \
             mock.patch("subprocess.run", return_value=failed):
            with self.assertRaisesRegex(jfbuild.BuildFailed, "CS1002"):
                jfbuild.build("/src", tempfile.mkdtemp(), say=lambda *a: None)

    def test_a_build_that_produced_nothing_is_a_failure(self):
        with mock.patch("shutil.which", return_value="/usr/bin/podman"), \
             mock.patch("subprocess.run", side_effect=_ok):
            with self.assertRaisesRegex(jfbuild.BuildFailed, "no jellyfin"):
                jfbuild.build("/src", tempfile.mkdtemp(), say=lambda *a: None)


if __name__ == "__main__":
    unittest.main()
