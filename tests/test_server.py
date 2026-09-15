"""The server-side pieces: auth headers, library options, accounts.

Nothing here needs a running Jellyfin. The assertions encode what was learned
from a live 12.0 server, so a regression shows up as a failing test rather
than as a silently misconfigured library.
"""

import json
import os
import tempfile
import unittest
from unittest import mock

from stdjflib import config, jfapi, jfserver, provision


class TestAuthHeaders(unittest.TestCase):
    def test_token_goes_inside_the_authorization_header(self):
        """Jellyfin 12 answers 401 to X-Emby-Token; verified against a server."""
        jf = jfapi.Jellyfin("http://example")
        jf.token = "abc123"
        headers = jf._headers()
        self.assertIn('Token="abc123"', headers["Authorization"])
        self.assertNotIn("X-Emby-Token", headers)

    def test_no_token_before_login(self):
        headers = jfapi.Jellyfin("http://example")._headers()
        self.assertNotIn("Token=", headers["Authorization"])
        self.assertIn("MediaBrowser ", headers["Authorization"])

    def test_client_identity_is_always_present(self):
        """The wizard endpoints reject a request with no client identity."""
        for token in (None, "t"):
            jf = jfapi.Jellyfin("http://example")
            jf.token = token
            auth = jf._headers()["Authorization"]
            for field in ("Client=", "Device=", "DeviceId=", "Version="):
                self.assertIn(field, auth)


class TestLibraryOptions(unittest.TestCase):
    def setUp(self):
        self.options = provision.library_options()

    def test_every_item_type_gets_an_entry(self):
        """The gate is 'is there a TypeOptions', so a missing type falls back
        to the server defaults, which have the internet providers on."""
        got = {t["Type"] for t in self.options["TypeOptions"]}
        self.assertEqual(got, set(provision.ITEM_TYPES))

    def test_fetcher_lists_are_empty_not_absent(self):
        """An empty list disables every remote fetcher; a missing key does not."""
        for entry in self.options["TypeOptions"]:
            with self.subTest(entry["Type"]):
                self.assertEqual(entry["MetadataFetchers"], [])

    def test_no_remote_image_provider_is_ever_listed(self):
        """`ImageFetchers` is the one list that is not simply emptied.

        `CanRefreshImages` returns early for an `ILocalImageProvider` and
        nothing else, so a *local* extractor — one that derives a picture from
        the file on disk — is gated by the same array as TMDB. Books need
        theirs: a book's cover is the extractor's output and there is no NFO
        to put one in. The invariant is therefore not "empty" but "nothing
        here talks to the internet".
        """
        for entry in self.options["TypeOptions"]:
            with self.subTest(entry["Type"]):
                for name in entry["ImageFetchers"]:
                    self.assertNotIn(name, provision.REMOTE_PROVIDERS)
                # Order has to name the same providers, or a fetcher that is
                # enabled sits at the bottom of a list it is not on.
                self.assertEqual(entry["ImageFetchers"],
                                 entry["ImageFetcherOrder"])

    def test_only_books_get_a_local_image_extractor(self):
        """Everywhere else the drawn artwork is the fixture, and an embedded
        thumbnail winning over it would replace one quietly."""
        enabled = {t["Type"] for t in self.options["TypeOptions"]
                   if t["ImageFetchers"]}
        self.assertEqual(enabled, {"Book"})

    def test_nfo_reader_stays_enabled(self):
        """Local providers must survive; the NFOs are the whole metadata source."""
        self.assertEqual(self.options["DisabledLocalMetadataReaders"], [])
        self.assertIn("Nfo", self.options["LocalMetadataReaderOrder"])

    def test_nfos_are_read_not_rewritten(self):
        self.assertFalse(self.options["SaveLocalMetadata"])
        self.assertEqual(self.options["MetadataSavers"], [])

    def test_filenames_win_over_embedded_titles(self):
        """The path conventions are what is under test."""
        self.assertFalse(self.options["EnableEmbeddedTitles"])
        self.assertFalse(self.options["EnableEmbeddedEpisodeInfos"])

    def test_expensive_extraction_is_opt_in(self):
        self.assertFalse(self.options["EnableChapterImageExtraction"])
        self.assertFalse(self.options["EnableTrickplayImageExtraction"])
        on = provision.library_options(chapter_images=True, trickplay=True)
        self.assertTrue(on["EnableChapterImageExtraction"])
        self.assertTrue(on["EnableTrickplayImageExtraction"])


class TestRemoteProviders(unittest.TestCase):
    def test_the_known_remote_providers_are_listed(self):
        """Names as the server reports them, not as it names the plugins."""
        for name in ("TheMovieDb", "The Open Movie Database", "MusicBrainz",
                     "TheAudioDB"):
            self.assertIn(name, provision.REMOTE_PROVIDERS)

    def test_local_extractors_are_not_disabled(self):
        """These read the file itself and must keep working."""
        for name in ("Image Extractor", "Embedded Image Extractor",
                     "Screen Grabber", "EPUB Metadata"):
            self.assertNotIn(name, provision.REMOTE_PROVIDERS)

    def test_server_types_cover_the_library_types(self):
        """The server-wide list is the fallback for items with no library
        options — MusicArtist above all — so it must be at least as wide."""
        self.assertTrue(set(provision.ITEM_TYPES)
                        <= set(provision.SERVER_METADATA_TYPES))
        self.assertIn("MusicArtist", provision.SERVER_METADATA_TYPES)


class TestAccounts(unittest.TestCase):
    def test_names_are_unique(self):
        names = [a["name"] for a in provision.ACCOUNTS]
        self.assertEqual(len(names), len(set(names)))

    def test_first_account_is_the_administrator(self):
        """provision() signs in as ACCOUNTS[0]; a non-admin there breaks setup."""
        first = provision.ACCOUNTS[0]
        self.assertTrue(first["policy"].get("IsAdministrator"))
        self.assertEqual(first["password"], provision.GENERATED)

    def test_no_administrator_has_a_password_anyone_can_read(self):
        """An admin can install a plugin, which is code execution on the host,
        and anything in this table is public. So an administrator's password
        is generated per server, never written here."""
        for account in provision.ACCOUNTS:
            if account["policy"].get("IsAdministrator"):
                with self.subTest(account["name"]):
                    self.assertEqual(account["password"], provision.GENERATED)

    def test_the_admin_policy_only_grants(self):
        """ACCOUNTS[0]'s policy is applied to the account we are signed in as.

        Which is safe in exactly one direction. `UpdatePolicyAsync` writes
        permissions and revokes no session, so a policy that only ever grants
        cannot lock the provisioner out — but one that takes a right away, or
        sets `IsDisabled`, would break the Live TV setup and the library scan
        that run after it, and the failure would surface somewhere unrelated.

        So: no `False` anywhere in ACCOUNTS[0]. Turning something off for the
        admin is not a thing this fixture should want, and the one place it
        would be tempting — trimming a permission to model a restricted
        administrator — is what the other eleven accounts are for.
        """
        policy = provision.ACCOUNTS[0]["policy"]
        off = sorted(k for k, v in policy.items() if v is False)
        self.assertEqual(
            off, [],
            "ACCOUNTS[0] is the account provision authenticates as; its "
            "policy must only grant, and these switch something off: %s" % off)
        self.assertNotIn("IsDisabled", policy)

    def test_every_account_explains_itself(self):
        for account in provision.ACCOUNTS:
            with self.subTest(account["name"]):
                self.assertTrue(account["why"].strip())
                self.assertIn("policy", account)

    def test_policies_only_use_real_fields(self):
        """A typo here fails silently — the server ignores unknown keys."""
        known = set(jfapi.Jellyfin("http://x").default_policy())
        known |= {"MaxParentalRating"}  # optional, absent from the default
        for account in provision.ACCOUNTS:
            for key in account["policy"]:
                with self.subTest(f"{account['name']}.{key}"):
                    self.assertIn(key, known)

    def test_restricted_account_names_real_libraries(self):
        restricted = next(a for a in provision.ACCOUNTS
                          if a["name"] == "qa-restricted")
        for folder in restricted["folders"]:
            self.assertIn(folder, config.LIBRARIES)

    def test_the_interesting_states_are_covered(self):
        names = {a["name"] for a in provision.ACCOUNTS}
        for required in ("qa-admin", "qa-user", "qa-disabled", "qa-hidden",
                         "qa-nopassword", "qa-notranscode", "qa-noplayback"):
            self.assertIn(required, names)


class TestLibrariesFromManifest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()

    def _manifest(self, libraries):
        path = os.path.join(self.dir, config.MANIFEST)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            json.dump({"libraries": libraries, "items": []}, fh)

    def test_only_folders_that_exist_are_offered(self):
        """A manifest entry whose folder was deleted must not be created."""
        self._manifest({"Movies": "movies", "Shows": "tvshows"})
        os.makedirs(os.path.join(self.dir, "Movies"), exist_ok=True)
        got = provision.libraries_from_manifest(self.dir)
        self.assertEqual(got, {"Movies": "movies"})

    def test_bulk_libraries_come_through(self):
        self._manifest({"Bulk Movies": "movies"})
        os.makedirs(os.path.join(self.dir, "Bulk Movies"), exist_ok=True)
        self.assertIn("Bulk Movies",
                      provision.libraries_from_manifest(self.dir))

    def test_falls_back_when_there_is_no_manifest(self):
        os.makedirs(os.path.join(self.dir, "Movies"), exist_ok=True)
        self.assertEqual(provision.libraries_from_manifest(self.dir),
                         {"Movies": "movies"})


class TestServerRunner(unittest.TestCase):
    def test_build_output_never_lands_in_the_checkout(self):
        """Old root-owned obj/ dirs make an in-tree build fail; stay out."""
        dll = jfserver.dll_path("/artifacts")
        self.assertTrue(dll.startswith("/artifacts"))

    def test_rejects_a_directory_that_is_not_jellyfin(self):
        self.assertFalse(jfserver.looks_like_jellyfin(tempfile.mkdtemp()))

    def test_nowebclient_when_no_web_assets(self):
        instance = jfserver.Instance("/x/jellyfin.dll", "/state", web_dir=None)
        self.assertIn("--nowebclient", instance.argv())
        instance = jfserver.Instance("/x/jellyfin.dll", "/state", web_dir="/web")
        self.assertNotIn("--nowebclient", instance.argv())
        self.assertIn("/web", instance.argv())

    def test_all_state_paths_are_under_the_state_dir(self):
        """Deleting one directory must give a factory-fresh server."""
        argv = jfserver.Instance("/x/jellyfin.dll", "/state").argv()
        for flag in ("--datadir", "--configdir", "--cachedir", "--logdir"):
            value = argv[argv.index(flag) + 1]
            with self.subTest(flag):
                self.assertTrue(value.startswith("/state"))


if __name__ == "__main__":
    unittest.main()


class TestContainer(unittest.TestCase):
    """Argument construction for the podman/docker path. No runtime needed."""

    def _box(self, **kw):
        from stdjflib import container

        return container.Container("/lib", "/state", runtime="podman", **kw)

    def test_media_is_mounted_read_only(self):
        argv = self._box().argv()
        media = [a for a in argv if a.startswith("/lib:")]
        self.assertEqual(len(media), 1)
        self.assertIn("ro", media[0].split(":")[-1])

    def test_config_and_cache_are_writable(self):
        argv = self._box().argv()
        for host, dest in (("/state/config", "/config"),
                           ("/state/cache", "/cache")):
            match = [a for a in argv if a.startswith(host + ":")]
            with self.subTest(dest):
                self.assertEqual(len(match), 1)
                self.assertIn(dest, match[0])
                self.assertNotIn("ro", match[0].split(":")[-1].split(","))

    def test_media_root_is_the_container_path_not_ours(self):
        """Provisioning must send the server its own path; ours scans to zero."""
        from stdjflib import container

        box = self._box()
        self.assertEqual(box.media_root, container.MEDIA_MOUNT)
        self.assertNotEqual(box.media_root, box.library)

    def test_port_is_published_on_loopback_only(self):
        """A bare `9000:8096` publishes on every interface."""
        argv = self._box(port=9000).argv()
        published = [argv[i + 1] for i, a in enumerate(argv) if a == "-p"]
        self.assertEqual(published, ["127.0.0.1:9000:8096"])

    def test_listen_adds_addresses_and_keeps_loopback(self):
        argv = self._box(port=9000, listen=("192.168.122.1", "127.0.0.1")).argv()
        published = [argv[i + 1] for i, a in enumerate(argv) if a == "-p"]
        self.assertEqual(published, ["127.0.0.1:9000:8096",
                                     "192.168.122.1:9000:8096"])

    def test_extra_args_land_before_the_image(self):
        """Anything after the image name is passed to the entrypoint instead."""
        from stdjflib import container

        argv = self._box(extra_args=("--memory", "2g")).argv()
        self.assertLess(argv.index("--memory"), argv.index(container.DEFAULT_IMAGE))
        self.assertEqual(argv[-1], container.DEFAULT_IMAGE)

    def test_runs_detached(self):
        self.assertIn("-d", self._box().argv())

    def test_runtime_selection(self):
        from stdjflib import container

        with self.assertRaises(container.ContainerError):
            container.pick_runtime("definitely-not-installed")

    def test_selinux_suffix_only_when_enforcing(self):
        """`:z` is harmless without SELinux but its absence is fatal with it."""
        from stdjflib import container

        original = container.selinux_enabled
        try:
            container.selinux_enabled = lambda: False
            self.assertEqual(container._mount("/a", "/b"), "/a:/b")
            self.assertEqual(container._mount("/a", "/b", read_only=True),
                             "/a:/b:ro")
            container.selinux_enabled = lambda: True
            self.assertEqual(container._mount("/a", "/b"), "/a:/b:z")
            self.assertEqual(container._mount("/a", "/b", read_only=True),
                             "/a:/b:ro,z")
        finally:
            container.selinux_enabled = original


class TestLiveTv(unittest.TestCase):
    """faketvsource wiring. Needs neither a server nor faketvsource."""

    def _fake(self, **kw):
        from stdjflib import livetv

        return livetv.FakeTv("/src", "/state", **kw)

    def test_public_url_defaults_to_loopback(self):
        self.assertEqual(self._fake().public_url, "http://127.0.0.1:8409")

    def test_public_url_is_told_to_faketvsource(self):
        """Without --public-url it builds stream URLs from the Host header,
        which is not reachable from inside a container."""
        fake = self._fake(public_host="host.containers.internal")
        self.assertIn("--public-url", fake.argv())
        self.assertIn("http://host.containers.internal:8409", fake.argv())
        # We still health-check it over loopback; only the server needs the
        # other name.
        self.assertEqual(fake.local_url, "http://127.0.0.1:8409")

    def test_no_public_url_flag_when_local(self):
        self.assertNotIn("--public-url", self._fake().argv())

    def test_tuner_count_is_passed_through(self):
        """Zero is meaningful (unlimited), so it must not be dropped."""
        for count in (0, 1, 4):
            argv = self._fake(tuner_count=count).argv()
            with self.subTest(count=count):
                self.assertIn("--tuner-count", argv)
                self.assertEqual(argv[argv.index("--tuner-count") + 1],
                                 str(count))

    def test_seed_is_passed_so_the_guide_is_stable(self):
        self.assertIn("--seed", self._fake().argv())

    def test_container_host_names_differ_by_runtime(self):
        from stdjflib import container, livetv

        for runtime in container.RUNTIMES:
            with self.subTest(runtime):
                self.assertIn(runtime, livetv.HOST_FROM_CONTAINER)
        self.assertNotEqual(livetv.HOST_FROM_CONTAINER["podman"],
                            livetv.HOST_FROM_CONTAINER["docker"])

    def test_bind_is_passed_only_when_asked(self):
        """faketvsource's own default is every interface, which a container
        needs and a local server does not."""
        self.assertNotIn("--host", self._fake().argv())
        argv = self._fake(bind="127.0.0.1").argv()
        self.assertEqual(argv[argv.index("--host") + 1], "127.0.0.1")


# What a 12.0 server writes on first start, trimmed.
_SERVER_NETWORK_XML = """<?xml version="1.0" encoding="utf-8"?>
<NetworkConfiguration xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" xmlns:xsd="http://www.w3.org/2001/XMLSchema">
  <InternalHttpPort>8096</InternalHttpPort>
  <EnableRemoteAccess>true</EnableRemoteAccess>
  <LocalNetworkAddresses />
  <VirtualInterfaceNames>
    <string>veth</string>
  </VirtualInterfaceNames>
</NetworkConfiguration>"""


class TestBindAddresses(unittest.TestCase):
    """`LocalNetworkAddresses` is the only thing that decides what Kestrel
    listens on; empty means every interface."""

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.dir.name, "config", "network.xml")

    def tearDown(self):
        self.dir.cleanup()

    def _addresses(self):
        from xml.etree import ElementTree

        root = ElementTree.parse(self.path).getroot()
        return [s.text for s in root.find("LocalNetworkAddresses")]

    def _write(self, **kw):
        jfserver.Instance("/x/jellyfin.dll", self.dir.name,
                          **kw)._write_network_config()

    def test_a_fresh_state_listens_on_loopback(self):
        self._write()
        self.assertEqual(self._addresses(), ["127.0.0.1"])

    def test_an_existing_file_is_corrected_and_otherwise_kept(self):
        """The server wrote this with every interface; a state directory like
        that must not keep listening on the LAN."""
        os.makedirs(os.path.dirname(self.path))
        with open(self.path, "w", encoding="utf-8") as fh:
            fh.write(_SERVER_NETWORK_XML)
        self._write(listen=("192.168.122.1",))
        self.assertEqual(self._addresses(), ["127.0.0.1", "192.168.122.1"])
        with open(self.path, encoding="utf-8") as fh:
            text = fh.read()
        self.assertIn("<string>veth</string>", text)
        self.assertIn("<InternalHttpPort>8096</InternalHttpPort>", text)

    def test_narrowing_again_drops_the_extra_address(self):
        self._write(listen=("192.168.122.1",))
        self._write()
        self.assertEqual(self._addresses(), ["127.0.0.1"])

    def test_no_kestrel_environment_variable(self):
        """Never consulted by Jellyfin, so setting one only misleads."""
        import inspect

        self.assertNotIn("Kestrel", inspect.getsource(jfserver.Instance.start))


class _PasswordApi:
    """Accepts exactly one password for the admin, like a server would."""

    def __init__(self, current, fail_status=401):
        self.current = current
        self.fail_status = fail_status
        self.logins = []
        self.changes = []

    def login(self, username, password):
        self.logins.append(password)
        if password != self.current:
            raise jfapi.ApiError("POST", "/Users/AuthenticateByName",
                                 self.fail_status, "")

    def change_own_password(self, current, new):
        if current != self.current:
            raise jfapi.ApiError("POST", "/Users/Password", 403, "")
        self.current = new
        self.changes.append(new)


class TestAdminPassword(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        patcher = mock.patch.object(tempfile, "tempdir", self.dir.name)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_generated_once_then_reused(self):
        path = provision.admin_password_file(os.path.join(self.dir.name, "s"))
        first = provision.load_admin_password(path)
        self.assertEqual(provision.load_admin_password(path), first)
        self.assertGreaterEqual(len(first), 20)
        self.assertNotEqual(first, provision.DEFAULT_PASSWORD)
        self.assertEqual(os.stat(path).st_mode & 0o777, 0o600)

    def test_each_server_state_gets_its_own(self):
        one = provision.load_admin_password(
            provision.admin_password_file(os.path.join(self.dir.name, "a")))
        two = provision.load_admin_password(
            provision.admin_password_file(os.path.join(self.dir.name, "b")))
        self.assertNotEqual(one, two)

    def test_a_saved_override_is_what_loads_next(self):
        path = provision.admin_password_file(os.path.join(self.dir.name, "s"))
        provision.load_admin_password(path)
        provision.save_admin_password(path, "chosen")
        self.assertEqual(provision.load_admin_password(path), "chosen")

    def test_a_local_server_is_published_by_port(self):
        path = provision.publish_connection("http://127.0.0.1:8097", "secret")
        self.assertEqual(path, config.connection_file(8097))
        self.assertEqual(os.stat(path).st_mode & 0o777, 0o600)
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        self.assertEqual(data["admin"], provision.ACCOUNTS[0]["name"])
        self.assertEqual(data["admin_password"], "secret")
        self.assertEqual(data["password"], provision.DEFAULT_PASSWORD)
        self.assertIn("qa-nopassword", data["no_password"])

    def test_a_remote_server_is_not_published(self):
        """Keyed by port: a remote 8096 would overwrite the local 8096."""
        self.assertIsNone(
            provision.publish_connection("http://192.168.1.5:8096", "secret"))

    def test_the_saved_password_signs_in_without_changing_anything(self):
        api = _PasswordApi("new")
        provision.sign_in_admin(api, "qa-admin", "new", say=lambda *_: None)
        self.assertEqual(api.changes, [])

    def test_the_old_fixed_password_is_replaced(self):
        api = _PasswordApi(provision.DEFAULT_PASSWORD)
        provision.sign_in_admin(api, "qa-admin", "new", say=lambda *_: None)
        self.assertEqual(api.current, "new")
        self.assertEqual(api.logins[-1], "new")

    def test_neither_password_is_an_error_that_says_what_to_do(self):
        api = _PasswordApi("someone-elses")
        with self.assertRaisesRegex(RuntimeError, "--admin-password"):
            provision.sign_in_admin(api, "qa-admin", "new",
                                    say=lambda *_: None)
        self.assertEqual(api.changes, [])

    def test_anything_but_a_refusal_is_not_retried(self):
        api = _PasswordApi("someone-elses", fail_status=500)
        with self.assertRaises(jfapi.ApiError):
            provision.sign_in_admin(api, "qa-admin", "new",
                                    say=lambda *_: None)
        self.assertEqual(api.logins, ["new"])

    def test_the_cli_takes_an_admin_password_and_listen_addresses(self):
        from stdjflib import cli

        for command in ("serve", "container"):
            with self.subTest(command):
                args = cli._parser().parse_args(
                    [command, "/lib", "--admin-password", "x",
                     "--listen", "192.168.122.1"])
                self.assertEqual(args.admin_password, "x")
                self.assertEqual(args.listen, ["192.168.122.1"])
                self.assertIsNone(
                    cli._parser().parse_args([command, "/lib"]).admin_password)


class _RecordingApi:
    """Minimal stand-in for the API client, capturing what was posted."""

    def __init__(self):
        self.posted = []

    def get(self, path, **kw):
        return {}

    def post(self, path, **kw):
        self.posted.append((path, kw.get("body") or {}))
        return {"Id": "x"}

    def request(self, *args, **kw):
        return None

    def body(self, path):
        return next(b for p, b in self.posted if p == path)


class TestLiveTvConfigure(unittest.TestCase):
    def _configure(self, tuner_type):
        from stdjflib import livetv

        api = _RecordingApi()
        livetv.configure(api, "http://h:8409", tuner_type=tuner_type,
                         say=lambda *_: None)
        return api

    def test_tuner_url_differs_by_type(self):
        """HDHomeRun discovers from the base URL; M3U wants the playlist."""
        for tuner_type, expected in (("m3u", "http://h:8409/playlist.m3u"),
                                     ("hdhomerun", "http://h:8409")):
            tuner = self._configure(tuner_type).body("/LiveTv/TunerHosts")
            with self.subTest(tuner_type):
                self.assertEqual(tuner["Url"], expected)
                self.assertEqual(tuner["Type"], tuner_type)

    def test_guide_always_points_at_the_xml(self):
        listings = self._configure("hdhomerun").body("/LiveTv/ListingProviders")
        self.assertEqual(listings["Type"], "xmltv")
        self.assertEqual(listings["Path"], "http://h:8409/guide.xml")

    def test_category_keywords_are_supplied(self):
        """Jellyfin keyword-matches these to set IsNews/IsSports/IsKids/
        IsMovie; leaving them to chance leaves those flags to chance."""
        listings = self._configure("m3u").body("/LiveTv/ListingProviders")
        for key in ("NewsCategories", "SportsCategories", "KidsCategories",
                    "MovieCategories"):
            with self.subTest(key):
                self.assertTrue(listings[key])

    def test_rejects_an_unknown_tuner_type(self):
        from stdjflib import livetv

        with self.assertRaises(ValueError):
            livetv.configure(None, "http://h", tuner_type="satellite")


class _TaskApi:
    """A server with one scheduled task, which runs for a few polls."""

    def __init__(self, tasks=None, runs_for: int = 0):
        self.tasks = [dict(t) for t in (tasks or [])]
        self.runs_for = runs_for
        self.posted = []
        self.polls = 0

    def scheduled_tasks(self):
        self.polls += 1
        out = []
        for task in self.tasks:
            state = "Running" if self.polls <= self.runs_for else "Idle"
            out.append({**task, "State": state})
        return out

    def post(self, path, **kw):
        self.posted.append(path)
        return None


def _no_sleep():
    from unittest import mock

    return mock.patch("stdjflib.jfapi.time.sleep")


def _task_api(**kw):
    """A real `Jellyfin` with only its two HTTP calls replaced, so the
    methods under test are the shipped ones."""
    from stdjflib.jfapi import Jellyfin

    jf = Jellyfin("http://h")
    stub = _TaskApi(**kw)
    jf.scheduled_tasks = stub.scheduled_tasks
    jf.post = stub.post
    return jf, stub


class TestScheduledTaskHelpers(unittest.TestCase):
    """`Jellyfin.task`/`start_task`/`wait_for_task`, bound to real methods."""

    def test_start_task_posts_to_the_tasks_id(self):
        jf, stub = _task_api(tasks=[{"Key": "K", "Id": "abc"}])
        self.assertTrue(jf.start_task("K"))
        self.assertEqual(stub.posted, ["/ScheduledTasks/Running/abc"])

    def test_a_missing_task_is_reported_not_posted(self):
        """Absent is an answer: Live TV's tasks only exist once it is set up."""
        jf, stub = _task_api(tasks=[{"Key": "Other", "Id": "abc"}])
        self.assertFalse(jf.start_task("K"))
        self.assertEqual(stub.posted, [])

    def test_wait_polls_until_the_task_goes_idle(self):
        jf, stub = _task_api(tasks=[{"Key": "K", "Id": "abc"}], runs_for=3)
        with _no_sleep():
            self.assertTrue(jf.wait_for_task("K", interval=0, settle=0))
        self.assertEqual(stub.polls, 4)

    def test_wait_gives_up_on_a_task_the_server_does_not_have(self):
        jf, _ = _task_api(tasks=[])
        with _no_sleep():
            self.assertFalse(jf.wait_for_task("K", interval=0, settle=0))

    def test_wait_for_scan_is_the_same_wait(self):
        """One implementation, so a fix to the polling reaches both callers."""
        from stdjflib import jfapi

        jf, _ = _task_api(tasks=[{"Key": jfapi.SCAN_TASK, "Id": "abc"}])
        seen = {}
        jf.wait_for_task = lambda key, **kw: seen.setdefault("key", key)
        jf.wait_for_scan()
        self.assertEqual(seen["key"], jfapi.SCAN_TASK)


class TestOptimizeDatabase(unittest.TestCase):
    """The step that stops a freshly built server answering folder queries
    with the planner statistics of an empty one."""

    def test_it_runs_the_servers_own_task(self):
        from stdjflib import jfapi, provision

        jf, stub = _task_api(tasks=[{"Key": jfapi.OPTIMIZE_TASK, "Id": "opt"}])
        with _no_sleep():
            self.assertTrue(provision.optimize_database(
                jf, say=lambda *_: None))
        self.assertEqual(stub.posted, ["/ScheduledTasks/Running/opt"])

    def test_a_server_without_the_task_is_survivable(self):
        from stdjflib import provision

        jf, stub = _task_api(tasks=[{"Key": "RefreshLibrary", "Id": "x"}])
        said = []
        with _no_sleep():
            self.assertFalse(provision.optimize_database(jf, say=said.append))
        self.assertEqual(stub.posted, [])
        self.assertTrue(any("optimization task" in s for s in said))

    def test_the_key_is_the_one_the_server_uses(self):
        """`OptimizeDatabaseTask.Key`, not the class name and not a guess."""
        from stdjflib import jfapi

        self.assertEqual(jfapi.OPTIMIZE_TASK, "OptimizeDatabaseTask")
