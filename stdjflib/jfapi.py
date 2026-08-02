"""A small Jellyfin API client, and the provisioning that uses it.

Standard library only, like the rest of this. Everything here was written
against the server source in `../jellyfin`, and the two places where the
obvious approach silently does nothing are called out where they happen.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request

CLIENT = "stdjflib"
DEVICE = "provisioner"
DEVICE_ID = "stdjflib-provisioner"
VERSION = "1.0.0"


class ApiError(RuntimeError):
    def __init__(self, method, path, status, body):
        self.status = status
        self.body = body
        super().__init__(f"{method} {path} -> {status}: {body[:400]}")


class Jellyfin:
    """Just enough of the API to stand a server up from nothing."""

    def __init__(self, base_url: str, timeout: int = 60):
        self.base = base_url.rstrip("/")
        self.timeout = timeout
        self.token: str | None = None
        self.user_id: str | None = None

    # -- plumbing ---------------------------------------------------------

    def _headers(self) -> dict:
        """The one auth header shape Jellyfin 12 accepts.

        The token goes *inside* the `Authorization` value. The old
        `X-Emby-Token` header is still read by `AuthorizationContext`, but only
        as a fallback when the Authorization header carries no token at all —
        and on a 12.0 server sending it alongside a token-less Authorization
        header still comes back 401. Verified against a live server: token in
        Authorization succeeds, `X-Emby-Token` with or without an Authorization
        header does not.
        """
        parts = [f'Client="{CLIENT}"', f'Device="{DEVICE}"',
                 f'DeviceId="{DEVICE_ID}"', f'Version="{VERSION}"']
        if self.token:
            parts.append(f'Token="{self.token}"')
        return {"Authorization": "MediaBrowser " + ", ".join(parts),
                "Accept": "application/json"}

    def request(self, method: str, path: str, *, body=None, params=None,
                expect_json: bool = True):
        url = self.base + path
        if params:
            url += "?" + urllib.parse.urlencode(params, doseq=True)
        data = None
        headers = self._headers()
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"

        req = urllib.request.Request(url, data=data, headers=headers,
                                     method=method)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                raw = resp.read()
        except urllib.error.HTTPError as exc:
            raise ApiError(method, path, exc.code,
                           exc.read().decode("utf-8", "replace")) from None
        if not expect_json or not raw:
            return None
        try:
            return json.loads(raw.decode("utf-8"))
        except ValueError:
            return None

    def get(self, path, **kw):
        return self.request("GET", path, **kw)

    def post(self, path, **kw):
        return self.request("POST", path, **kw)

    # -- lifecycle --------------------------------------------------------

    def wait_until_up(self, timeout: int = 240, interval: float = 1.0,
                      still_alive=None) -> dict:
        """Poll the one endpoint that answers before setup is done.

        The payload has to be real, not merely a response. A socket that
        accepts a connection and closes it without a body — a previous server
        still shutting down on the same port is the usual cause — yields an
        empty read, and treating that as "up" makes everything after it fail
        with a connection error that points nowhere near the actual problem.

        `still_alive` is an optional predicate; when it stops being true the
        wait ends immediately rather than burning the full timeout on a server
        that has already died.
        """
        deadline = time.time() + timeout
        last = None
        while time.time() < deadline:
            if still_alive is not None and not still_alive():
                raise RuntimeError("the server process exited during startup")
            try:
                info = self.get("/System/Info/Public")
                if isinstance(info, dict) and info.get("Id"):
                    return info
                last = f"incomplete response: {info!r}"
            except (ApiError, urllib.error.URLError, OSError, TimeoutError) as exc:
                last = exc
            time.sleep(interval)
        raise TimeoutError(f"server did not answer within {timeout}s ({last})")

    def needs_setup(self) -> bool:
        info = self.get("/System/Info/Public") or {}
        return not info.get("StartupWizardCompleted", False)

    def run_startup_wizard(self, *, server_name: str, username: str,
                           password: str, remote_access: bool = True) -> None:
        """The four calls the first-run wizard makes, in order.

        These endpoints only accept unauthenticated requests while the wizard
        is incomplete, so this has to happen before anything else.
        """
        self.post("/Startup/Configuration", body={
            "ServerName": server_name,
            "UICulture": "en-US",
            "MetadataCountryCode": "US",
            "PreferredMetadataLanguage": "en",
        }, expect_json=False)
        # GET /Startup/User initialises the default user record; the POST then
        # renames it and sets the password. Skipping the GET leaves the wizard
        # with nothing to update.
        self.get("/Startup/User")
        self.post("/Startup/User", body={"Name": username,
                                         "Password": password},
                  expect_json=False)
        self.post("/Startup/RemoteAccess",
                  body={"EnableRemoteAccess": remote_access},
                  expect_json=False)
        self.post("/Startup/Complete", expect_json=False)

    def login(self, username: str, password: str) -> dict:
        result = self.post("/Users/AuthenticateByName",
                           body={"Username": username, "Pw": password})
        self.token = result["AccessToken"]
        self.user_id = result["User"]["Id"]
        return result

    # -- libraries --------------------------------------------------------

    def virtual_folders(self) -> list:
        return self.get("/Library/VirtualFolders") or []

    def add_library(self, name: str, collection_type: str, paths: list[str],
                    options: dict) -> None:
        self.post("/Library/VirtualFolders",
                  params={"name": name, "collectionType": collection_type,
                          "paths": paths, "refreshLibrary": "false"},
                  body={"LibraryOptions": options},
                  expect_json=False)

    def remove_library(self, name: str) -> None:
        self.request("DELETE", "/Library/VirtualFolders",
                     params={"name": name, "refreshLibrary": "false"},
                     expect_json=False)

    def refresh_library(self) -> None:
        self.post("/Library/Refresh", expect_json=False)

    def scheduled_tasks(self) -> list:
        return self.get("/ScheduledTasks") or []

    def wait_for_scan(self, timeout: int = 3600, interval: float = 3.0,
                      on_progress=None) -> bool:
        """Block until the library scan task stops running.

        Polls the scheduled task rather than guessing, and gives the task a
        moment to *start* first — asking immediately after triggering a
        refresh reliably catches it Idle and returns straight away.
        """
        time.sleep(3)
        deadline = time.time() + timeout
        while time.time() < deadline:
            tasks = self.scheduled_tasks()
            scan = next((t for t in tasks
                         if t.get("Key") == "RefreshLibrary"), None)
            if scan is None:
                return False
            if scan.get("State") == "Idle":
                return True
            if on_progress:
                on_progress(scan.get("CurrentProgressPercentage") or 0.0)
            time.sleep(interval)
        return False

    def counts(self) -> dict:
        return self.get("/Items/Counts") or {}

    # -- users ------------------------------------------------------------

    def users(self) -> list:
        return self.get("/Users") or []

    def create_user(self, name: str, password: str | None) -> dict:
        # The API takes the password here, but a null/empty one is only
        # honoured by omitting it — passing "" sets a real empty password on
        # some versions and errors on others.
        body = {"Name": name}
        if password:
            body["Password"] = password
        return self.post("/Users/New", body=body)

    def set_policy(self, user_id: str, policy: dict) -> None:
        self.post(f"/Users/{user_id}/Policy", body=policy, expect_json=False)

    def set_user_config(self, user_id: str, config: dict) -> None:
        self.post(f"/Users/{user_id}/Configuration", body=config,
                  expect_json=False)

    def default_policy(self) -> dict:
        """A full policy object, so a partial update cannot blank a field."""
        return {
            "IsAdministrator": False,
            "IsHidden": False,
            "IsDisabled": False,
            "EnableUserPreferenceAccess": True,
            "EnableRemoteAccess": True,
            "EnableLiveTvAccess": True,
            "EnableLiveTvManagement": False,
            "EnableMediaPlayback": True,
            "EnableAudioPlaybackTranscoding": True,
            "EnableVideoPlaybackTranscoding": True,
            "EnablePlaybackRemuxing": True,
            "EnableContentDeletion": False,
            "EnableContentDownloading": True,
            "EnableSyncTranscoding": True,
            "EnableMediaConversion": False,
            "EnableAllDevices": True,
            "EnableAllChannels": True,
            "EnableAllFolders": True,
            "EnabledFolders": [],
            "EnableCollectionManagement": False,
            "EnableSubtitleManagement": False,
            "EnableRemoteControlOfOtherUsers": False,
            "EnableSharedDeviceControl": True,
            "EnablePublicSharing": False,
            "InvalidLoginAttemptCount": 0,
            "LoginAttemptsBeforeLockout": -1,
            "MaxActiveSessions": 0,
            "RemoteClientBitrateLimit": 0,
            "BlockedTags": [],
            "AllowedTags": [],
            "BlockUnratedItems": [],
            "AccessSchedules": [],
            "SyncPlayAccess": "CreateAndJoinGroups",
            "AuthenticationProviderId":
                "Jellyfin.Server.Implementations.Users.DefaultAuthenticationProvider",
            "PasswordResetProviderId":
                "Jellyfin.Server.Implementations.Users.DefaultPasswordResetProvider",
        }
