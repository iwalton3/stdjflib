"""Building the browser UI in a container.

Nothing here runs podman — the build takes minutes and needs the network, and
the test suite is supposed to need neither. What is checked is the shape of
the thing: that the container is invoked with the isolation this module exists
to provide, that the fallbacks return rather than raise, and that the cache
notices a source change. The isolation flags are the actual deliverable, so
they are asserted one by one rather than as "some arguments were passed".
"""

import json
import os
import tempfile
import unittest
from unittest import mock

from stdjflib import jfserver, web


class TestIsolation(unittest.TestCase):
    """The container arguments are the point of the module."""

    def argv(self) -> list:
        with mock.patch("shutil.which", return_value="/usr/bin/podman"), \
             mock.patch("subprocess.run") as run, \
             tempfile.TemporaryDirectory() as out:
            run.return_value = mock.Mock(returncode=0, stderr="", stdout="")
            os.makedirs(os.path.join(out, "dist"))
            open(os.path.join(out, "dist", "index.html"), "w").close()
            web.build("/src/jellyfin-web", out, say=lambda *a: None)
            return run.call_args_list[0].args[0]

    def test_the_source_is_mounted_read_only(self):
        # The one flag that keeps a compromised install script out of the
        # checkout. Everything else is defence in depth; this is the wall.
        self.assertIn("/src/jellyfin-web:/src:ro", self.argv())

    def test_capabilities_are_dropped(self):
        argv = self.argv()
        self.assertIn("--cap-drop=ALL", argv)
        self.assertIn("no-new-privileges", argv)

    def test_install_scripts_are_off(self):
        # Where a compromised dependency does its work. `npm ci` is the only
        # command it applies to; jellyfin-web's own build needs no hook.
        self.assertIn("--ignore-scripts", web.SCRIPT)
        self.assertIn("npm ci", web.SCRIPT)

    def test_the_only_writable_mount_is_the_output(self):
        writable = [a for a in self.argv()
                    if isinstance(a, str) and ":/" in a and a.startswith("/")
                    and not a.endswith(":ro")]
        self.assertEqual(len(writable), 1, writable)
        self.assertTrue(writable[0].endswith(":/out"))

    def test_the_container_builds_somewhere_it_can_write(self):
        # The image's `/` is dr-xr-xr-x, and dropping every capability takes
        # CAP_DAC_OVERRIDE with them — so root cannot create `/build` and the
        # run dies with a permission error that reads like a podman fault.
        self.assertNotIn("mkdir -p /build\n", web.SCRIPT)
        self.assertIn("/tmp/build", web.SCRIPT)

    def test_no_working_directory_is_preset(self):
        # podman refuses to start at all when `-w` names a directory the image
        # does not have, and exits 126 before anything runs.
        self.assertNotIn("-w", self.argv())

    def test_the_output_is_moved_into_place_rather_than_written_in_place(self):
        # A build interrupted halfway must not leave a half-bundle that
        # `is_current` would then serve. The move is the commit.
        self.assertIn("dist.part", web.SCRIPT)
        self.assertLess(web.SCRIPT.index("cp -a dist/."),
                        web.SCRIPT.index("mv /out/dist.part /out/dist"))


class TestFallbacks(unittest.TestCase):
    """`ensure` never raises. A server with no UI is a working server."""

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.out = os.path.join(self.dir.name, "out")

    def test_disabled_says_so_and_builds_nothing(self):
        path, why = web.ensure("/src/jellyfin", self.out, enabled=False)
        self.assertIsNone(path)
        self.assertIn("--no-web", why)

    def test_no_checkout_is_not_an_error(self):
        path, why = web.ensure(os.path.join(self.dir.name, "jellyfin"),
                               self.out)
        self.assertIsNone(path)
        self.assertIn("no jellyfin-web checkout", why)

    def test_no_podman_is_not_an_error_and_docker_is_not_offered(self):
        src = os.path.join(self.dir.name, "jellyfin-web")
        os.makedirs(src)
        open(os.path.join(src, "package.json"), "w").close()
        with mock.patch("stdjflib.web.engine", return_value=None):
            path, why = web.ensure(os.path.join(self.dir.name, "jellyfin"),
                                   self.out)
        self.assertIsNone(path)
        # Says which engine is missing, and says plainly that the other one
        # will not be used — falling back to a rootful Docker daemon would run
        # the build as root on the host, which is worse than not
        # containerising at all.
        self.assertIn("podman", why)
        self.assertIn("not a substitute", why)

    def test_docker_is_never_the_engine(self):
        with mock.patch("shutil.which") as which:
            which.side_effect = lambda name: f"/usr/bin/{name}"
            self.assertTrue(web.engine().endswith("podman"))
            self.assertEqual([c.args[0] for c in which.call_args_list],
                             ["podman"])

    def test_a_failed_build_is_reported_and_not_raised(self):
        src = os.path.join(self.dir.name, "jellyfin-web")
        os.makedirs(src)
        open(os.path.join(src, "package.json"), "w").close()
        with mock.patch("stdjflib.web.engine", return_value="/usr/bin/podman"), \
             mock.patch("stdjflib.web.build",
                        side_effect=web.BuildFailed("npm exploded")):
            path, why = web.ensure(os.path.join(self.dir.name, "jellyfin"),
                                   self.out)
        self.assertIsNone(path)
        self.assertIn("npm exploded", why)


class TestCache(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.out = os.path.join(self.dir.name, "out")
        os.makedirs(os.path.join(self.out, "dist"))
        open(os.path.join(self.out, "dist", "index.html"), "w").close()

    def write_stamp(self, revision: str) -> None:
        with open(web.stamp_path(self.out), "w", encoding="utf-8") as fh:
            json.dump({"revision": revision}, fh)

    def test_a_matching_revision_is_current(self):
        self.write_stamp("abc123")
        with mock.patch("stdjflib.web.revision", return_value="abc123"):
            self.assertTrue(web.is_current(self.out, "/src"))

    def test_a_changed_revision_is_not(self):
        self.write_stamp("abc123")
        with mock.patch("stdjflib.web.revision", return_value="def456"):
            self.assertFalse(web.is_current(self.out, "/src"))

    def test_a_bundle_with_no_index_is_not_current_whatever_the_stamp_says(self):
        self.write_stamp("abc123")
        os.unlink(os.path.join(self.out, "dist", "index.html"))
        with mock.patch("stdjflib.web.revision", return_value="abc123"):
            self.assertFalse(web.is_current(self.out, "/src"))

    def test_a_dirty_checkout_never_matches_a_clean_build(self):
        # Editing jellyfin-web and serving yesterday's bundle is the failure
        # this guards: it looks like the edit did nothing.
        def fake_run(argv, **kw):
            if "rev-parse" in argv:
                return mock.Mock(returncode=0, stdout="abc123\n")
            return mock.Mock(returncode=0, stdout=" M src/index.ts\n")

        with mock.patch("subprocess.run", side_effect=fake_run):
            self.assertEqual(web.revision("/src"), "abc123-dirty")


class TestServerWiring(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)

    def bundle(self, name: str) -> str:
        path = os.path.join(self.dir.name, name)
        os.makedirs(path)
        open(os.path.join(path, "index.html"), "w").close()
        return path

    def test_a_container_build_wins_over_a_dist_in_the_checkout(self):
        # Both exist and they are not the same thing: one was produced here
        # under known rules, the other by an npm run nobody here can see.
        built = self.bundle("built")
        source = os.path.join(self.dir.name, "src", "jellyfin")
        os.makedirs(os.path.join(self.dir.name, "src", "jellyfin-web"))
        checkout = self.bundle(os.path.join("src", "jellyfin-web", "dist"))
        self.assertTrue(os.path.exists(checkout))
        self.assertEqual(jfserver.find_web_client(source, built), built)

    def test_the_checkout_is_still_used_when_nothing_was_built(self):
        source = os.path.join(self.dir.name, "src", "jellyfin")
        os.makedirs(os.path.join(self.dir.name, "src", "jellyfin-web"),
                    exist_ok=True)
        checkout = self.bundle(os.path.join("src", "jellyfin-web", "dist"))
        self.assertEqual(jfserver.find_web_client(source, None), checkout)

    def test_no_bundle_anywhere_is_none_rather_than_a_crash(self):
        source = os.path.join(self.dir.name, "nothing", "jellyfin")
        self.assertIsNone(jfserver.find_web_client(source, None))

    def test_the_server_runs_nowebclient_when_there_is_none(self):
        instance = jfserver.Instance("/x/jellyfin.dll", self.dir.name,
                                     web_dir=None)
        with mock.patch("stdjflib.jfserver.find_dotnet", return_value="dotnet"):
            self.assertIn("--nowebclient", instance.argv())

    def test_the_server_is_pointed_at_the_bundle_when_there_is_one(self):
        built = self.bundle("built")
        instance = jfserver.Instance("/x/jellyfin.dll", self.dir.name,
                                     web_dir=built)
        with mock.patch("stdjflib.jfserver.find_dotnet", return_value="dotnet"):
            argv = instance.argv()
        self.assertNotIn("--nowebclient", argv)
        self.assertEqual(argv[argv.index("--webdir") + 1], built)


if __name__ == "__main__":
    unittest.main()
