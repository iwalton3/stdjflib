"""Point a Jellyfin server at a built library and give it test accounts.

The libraries are created with internet metadata genuinely switched off, and
that turns out to need care — see `library_options`. The accounts exist to
make the client-side permission paths reachable without hand-clicking through
the dashboard every time you reset a server.
"""

from __future__ import annotations

import json
import os

from . import config, livetv
from .jfapi import ApiError, Jellyfin


def _say(msg: str = "") -> None:
    """Flushed, because stderr is not buffered and stdout is when piped.

    Without this an error prints above the progress line that explains what
    was being attempted, and the log reads as though the failure happened
    somewhere it did not.
    """
    print(msg, flush=True)

# Every item type Jellyfin will look for a remote provider for. A TypeOptions
# entry has to exist for each one, because the check is "is there a TypeOptions
# for this library" and not "is this type listed".
ITEM_TYPES = (
    "Movie", "Series", "Season", "Episode", "MusicVideo", "Video",
    "MusicAlbum", "MusicArtist", "Audio", "AudioBook",
    "Book", "BoxSet", "Photo", "PhotoAlbum", "Trailer",
)

DEFAULT_PASSWORD = "stdjflib"

# Every provider that talks to the internet, as the server names them (which is
# not what it names the plugins — the "TMDb" plugin registers "TheMovieDb").
# Enumerated from a live server via /Libraries/AvailableOptions; everything else
# it offers is a local extractor that reads the file itself and should stay on.
REMOTE_PROVIDERS = [
    "TheMovieDb",
    "The Open Movie Database",
    "MusicBrainz",
    "TheAudioDB",
]

# Types the server keeps global metadata options for. Wider than ITEM_TYPES on
# purpose: this list is the fallback that catches anything whose library
# options come back null.
SERVER_METADATA_TYPES = (
    "Movie", "Series", "Season", "Episode", "MusicVideo", "Video",
    "MusicAlbum", "MusicArtist", "Audio", "AudioBook", "Book", "BoxSet",
    "Photo", "PhotoAlbum", "Trailer",
)

# The accounts. Each one exists to make a specific client path reachable:
# not "some users", but "the states a client has to render correctly".
ACCOUNTS = [
    {
        "name": "qa-admin",
        "password": DEFAULT_PASSWORD,
        "why": ("Administrator, created by the first-run wizard. Everything "
                "allowed: dashboard, scheduled tasks, library management, "
                "recordings, deletion."),
        # Every management permission, because an administrator that cannot
        # administer is a trap rather than a fixture. The wizard makes this
        # account an admin and nothing more: `IsAdministrator` is its own
        # permission and gates none of the others, and there is no bypass
        # anywhere — UserPermissionHandler asks HasPermission and stops. So
        # without these, qa-admin could not delete an item, manage a
        # collection or schedule a recording.
        #
        # This policy IS applied, unlike every previous version of this entry,
        # which declared four of these and had them silently dropped. Safe
        # because it only ever grants: UpdatePolicyAsync writes permissions
        # and revokes no session, so the one thing that could lock the
        # provisioner out mid-run is a policy that takes rights away or sets
        # IsDisabled. Keep it a superset. The guard below re-authenticates
        # anyway if the session does not survive.
        "policy": {"IsAdministrator": True,
                   "EnableContentDeletion": True,
                   "EnableCollectionManagement": True,
                   "EnableSubtitleManagement": True,
                   "EnableLyricManagement": True,
                   "EnableLiveTvManagement": True,
                   "EnableRemoteControlOfOtherUsers": True,
                   "EnableMediaConversion": True},
    },
    {
        "name": "qa-user",
        "password": DEFAULT_PASSWORD,
        "why": ("An ordinary user with everything a non-admin can have. The "
                "control, and the non-admin that can manage recordings."),
        # "Everything allowed" has to include this or it is not the control.
        # EnableLiveTvManagement is a third Live TV permission, separate from
        # EnableLiveTvAccess, and a new user does not get it — so without this
        # no account on the server could schedule a recording and the entire
        # DVR surface was unreachable for every client. Every other account
        # still lacks it, which is the state a client has to render too.
        "policy": {"EnableLiveTvManagement": True},
    },
    {
        "name": "qa-nopassword",
        "password": None,
        "why": ("No password at all — common in home setups, and a login flow "
                "that assumes a password field is filled breaks here."),
        "policy": {},
    },
    {
        "name": "qa-restricted",
        "password": DEFAULT_PASSWORD,
        "why": ("Can see only the Movies and Shows libraries. Everything else "
                "must be absent from the home screen, not merely unplayable."),
        "policy": {"EnableAllFolders": False},
        "folders": ("Movies", "Shows"),
    },
    {
        "name": "qa-notranscode",
        "password": DEFAULT_PASSWORD,
        "why": ("Transcoding and remuxing both refused. Anything that cannot "
                "direct play must fail with a clear message rather than "
                "spinning — this is the account that finds the spinner."),
        "policy": {"EnableVideoPlaybackTranscoding": False,
                   "EnableAudioPlaybackTranscoding": False,
                   "EnablePlaybackRemuxing": False,
                   "EnableSyncTranscoding": False},
    },
    {
        "name": "qa-nodownload",
        "password": DEFAULT_PASSWORD,
        "why": ("Downloading and sync refused. The client's offline/download "
                "features must be hidden or refused, not offered and then "
                "failing."),
        "policy": {"EnableContentDownloading": False,
                   "EnableSyncTranscoding": False},
    },
    {
        "name": "qa-noplayback",
        "password": DEFAULT_PASSWORD,
        "why": ("Can browse but not play anything. Separates 'can list' from "
                "'can play' — clients routinely conflate the two."),
        "policy": {"EnableMediaPlayback": False},
    },
    {
        "name": "qa-kid",
        "password": DEFAULT_PASSWORD,
        "why": ("Parental rating capped and unrated items blocked, so most of "
                "the library is invisible. Exercises empty rows and empty "
                "libraries, which is where 'no items' rendering gets tested."),
        "policy": {"MaxParentalRating": 7,
                   "BlockUnratedItems": ["Movie", "Series", "Music", "Book",
                                         "Other"],
                   "EnableLiveTvAccess": False},
    },
    {
        "name": "qa-nosyncplay",
        "password": DEFAULT_PASSWORD,
        "why": "SyncPlay refused, so the client's SyncPlay entry points must go.",
        "policy": {"SyncPlayAccess": "None"},
    },
    {
        "name": "qa-onesession",
        "password": DEFAULT_PASSWORD,
        "why": ("One active session allowed. Logging in twice must evict the "
                "first, which a client has to notice and handle."),
        "policy": {"MaxActiveSessions": 1},
    },
    {
        "name": "qa-hidden",
        "password": DEFAULT_PASSWORD,
        "why": ("Hidden from the login list, but can still sign in by name. "
                "A client that only offers the public user list cannot reach "
                "this account at all."),
        "policy": {"IsHidden": True},
    },
    {
        "name": "qa-disabled",
        "password": DEFAULT_PASSWORD,
        "why": ("Disabled. Authentication must fail cleanly with a message, "
                "not hang or land the client in a half-signed-in state."),
        "policy": {"IsDisabled": True},
    },
]


def library_options(*, chapter_images: bool = False,
                    trickplay: bool = False) -> dict:
    """Library options with internet metadata genuinely off.

    The obvious switch does not work. `LibraryOptions.EnableInternetProviders`
    exists in the DTO and is referenced nowhere else in the server — setting it
    false changes nothing at all.

    What actually gates a remote provider is
    `BaseItemManager.IsMetadataFetcherEnabled`, which reads:

        if (libraryTypeOptions is not null)
            return libraryTypeOptions.MetadataFetchers.Contains(name);

    So a `TypeOptions` whose `MetadataFetchers` is an *empty array* refuses
    every remote fetcher for that type — and because the branch turns on
    "is there a TypeOptions at all", a type with no entry silently falls back
    to the server-wide defaults, which have the internet providers on. Hence
    an entry per type in `ITEM_TYPES`.

    Local providers are unaffected: `CanRefreshMetadata` returns true for
    anything that is not an `IRemoteMetadataProvider` before it ever reaches
    that check, so the NFO reader still runs. Which is the whole point — the
    metadata has to come from the NFOs and nowhere else.
    """
    return {
        "Enabled": True,
        "EnablePhotos": True,
        "EnableRealtimeMonitor": False,   # nothing changes under us; save the watches
        "EnableChapterImageExtraction": chapter_images,
        "ExtractChapterImagesDuringLibraryScan": chapter_images,
        "EnableTrickplayImageExtraction": trickplay,
        "ExtractTrickplayImagesDuringLibraryScan": trickplay,
        "EnableLUFSScan": False,
        "SaveLocalMetadata": False,       # read our NFOs, do not rewrite them
        "EnableInternetProviders": False,  # vestigial; set for honesty, not effect
        "EnableAutomaticSeriesGrouping": False,
        # The path conventions are what is under test, so filenames must win
        # over whatever a muxer happened to write into the container.
        "EnableEmbeddedTitles": False,
        "EnableEmbeddedExtrasTitles": False,
        "EnableEmbeddedEpisodeInfos": False,
        "AutomaticRefreshIntervalDays": 0,
        "PreferredMetadataLanguage": "en",
        "MetadataCountryCode": "US",
        "SeasonZeroDisplayName": "Specials",
        "MetadataSavers": [],
        "DisabledLocalMetadataReaders": [],
        "LocalMetadataReaderOrder": ["Nfo"],
        "DisabledSubtitleFetchers": [],
        "SubtitleFetcherOrder": [],
        "SkipSubtitlesIfEmbeddedSubtitlesPresent": False,
        "SkipSubtitlesIfAudioTrackMatches": False,
        "SubtitleDownloadLanguages": [],
        "RequirePerfectSubtitleMatch": True,
        "SaveSubtitlesWithMedia": False,
        "AutomaticallyAddToCollection": False,
        "AllowEmbeddedSubtitles": "AllowAll",
        "TypeOptions": [
            {"Type": item_type,
             "MetadataFetchers": [], "MetadataFetcherOrder": [],
             "ImageFetchers": [], "ImageFetcherOrder": [],
             "ImageOptions": []}
            for item_type in ITEM_TYPES
        ],
    }


def disable_remote_providers(jf: Jellyfin) -> int:
    """Switch the internet providers off server-wide, as well as per library.

    Per-library `TypeOptions` is not sufficient on its own, and the gap is not
    theoretical — a first run leaks MusicBrainz lookups for every artist.

    `ProviderManager.CanRefreshMetadata` consults the library's options, but
    only when there *are* any:

        // Artists without a folder structure that are derived from metadata
        // have no real path in the library, so GetLibraryOptions returns null.
        if (item is MusicArtist && libraryTypeOptions is null)
            return true;

    A MusicArtist comes from tags rather than a folder, so it has no library
    path, so its options are null, so every provider is allowed through. The
    per-library switch cannot reach it.

    What does reach it is the other branch of `IsMetadataFetcherEnabled`:

        var itemConfig = GetMetadataOptionsForType(baseItem.GetType().Name);
        return itemConfig is null
            || !itemConfig.DisabledMetadataFetchers.Contains(name);

    So the server-wide `MetadataOptions` need every remote provider listed as
    disabled, for every type. Both layers are required: this one catches the
    items with no library options, the per-library one catches the rest.
    """
    server_config = jf.get("/System/Configuration") or {}
    options = {entry["ItemType"]: entry
               for entry in server_config.get("MetadataOptions") or []}

    for item_type in SERVER_METADATA_TYPES:
        entry = options.setdefault(item_type, {"ItemType": item_type})
        entry["DisabledMetadataFetchers"] = list(REMOTE_PROVIDERS)
        entry["DisabledImageFetchers"] = list(REMOTE_PROVIDERS)
        entry.setdefault("MetadataFetcherOrder", [])
        entry.setdefault("ImageFetcherOrder", [])
        entry.setdefault("DisabledSubtitleFetchers", [])
        entry.setdefault("SubtitleFetcherOrder", [])

    server_config["MetadataOptions"] = list(options.values())
    # Plugin repositories are the other thing that phones home on a timer.
    server_config["PluginRepositories"] = []
    jf.post("/System/Configuration", body=server_config, expect_json=False)
    return len(options)


def libraries_from_manifest(root: str) -> dict:
    """{folder: collection type} for what was actually built.

    Read from the manifest rather than assumed, so a partial build is
    provisioned as it is rather than as it was meant to be.
    """
    path = os.path.join(root, config.MANIFEST)
    try:
        with open(path, encoding="utf-8") as fh:
            libs = json.load(fh).get("libraries") or {}
    except (OSError, ValueError):
        libs = {}
    if not libs:
        libs = dict(config.LIBRARIES)
    return {name: kind for name, kind in libs.items()
            if os.path.isdir(os.path.join(root, name))}


def provision(jf: Jellyfin, root: str, *, password: str = DEFAULT_PASSWORD,
              server_name: str = "stdjflib QA",
              chapter_images: bool = False, trickplay: bool = False,
              replace: bool = False, scan: bool = True,
              media_root: str | None = None, still_alive=None,
              live_tv_url: str | None = None, tuner_type: str = "m3u",
              tuner_count: int = 0, say=_say) -> dict:
    """Set up a server from whatever state it is in. Safe to re-run."""
    say("Waiting for the server")
    info = jf.wait_until_up(still_alive=still_alive)
    say(f"  {info.get('ServerName')} {info.get('Version')}")

    admin = ACCOUNTS[0]["name"]
    if jf.needs_setup():
        say("Running the first-time setup wizard")
        jf.run_startup_wizard(server_name=server_name, username=admin,
                              password=password)
        say(f"  administrator: {admin}")
    else:
        say("Server is already set up")

    jf.login(admin, password)

    count = disable_remote_providers(jf)
    say(f"Internet metadata off for {count} item types, server-wide")

    # The server may see the library at a different path than we do — a
    # container mount, most obviously — so what goes into the API is not
    # necessarily where this script read the manifest from.
    server_root = media_root or root
    wanted = libraries_from_manifest(root)
    existing = {f["Name"] for f in jf.virtual_folders()}

    say(f"Libraries ({len(wanted)})")
    for name, kind in wanted.items():
        if name in existing:
            if not replace:
                say(f"  = {name} (already present)")
                continue
            jf.remove_library(name)
        jf.add_library(name, kind, [os.path.join(server_root, name)],
                       library_options(chapter_images=chapter_images,
                                       trickplay=trickplay))
        say(f"  + {name} ({kind})")

    say(f"Accounts ({len(ACCOUNTS)})")
    folder_ids = {f["Name"]: f["ItemId"] for f in jf.virtual_folders()}
    have = {u["Name"]: u for u in jf.users()}
    created = []
    for account in ACCOUNTS:
        name = account["name"]
        user = have.get(name)
        if user is None:
            try:
                user = jf.create_user(name, account["password"])
            except ApiError as exc:
                say(f"  ! {name}: {exc}")
                continue
        policy = jf.default_policy()
        policy.update(account["policy"])
        if "folders" in account:
            policy["EnabledFolders"] = [folder_ids[f]
                                        for f in account["folders"]
                                        if f in folder_ids]
        jf.set_policy(user["Id"], policy)
        pw = account["password"] or "(no password)"
        say(f"  {name:16} {pw:12} {account['why'].splitlines()[0]}")
        created.append({"name": name, "password": account["password"],
                        "id": user["Id"], "why": account["why"]})

    # We just rewrote the policy of the account we are signed in as. Nothing in
    # UpdatePolicyAsync revokes a session, and ACCOUNTS[0]'s policy only ever
    # grants — but everything after this point (Live TV, the scan) needs the
    # token, and a cheap authenticated call is a better way to find out than
    # the next real request failing somewhere unrelated.
    try:
        jf.get("/System/Info")
    except ApiError:
        say("  (admin session did not survive its own policy; signing in again)")
        jf.login(admin, password)

    result = {"server": jf.base, "admin": admin, "password": password,
              "libraries": wanted, "accounts": created}

    if live_tv_url:
        say("Live TV")
        result["live_tv"] = livetv.configure(
            jf, live_tv_url, tuner_type=tuner_type, tuner_count=tuner_count,
            replace=True, say=say)
        say("  refreshing the guide")
        # Channels do not exist until this runs, so a client pointed at the
        # server before it finishes sees an empty Live TV section rather than
        # a loading one.
        done = livetv.refresh_guide(jf, say=say)
        if not done:
            say("  ! guide refresh did not finish in time")
        result["live_tv"].update(livetv.summarise(jf, say=say))

    if scan:
        say("Scanning the library")
        jf.refresh_library()
        done = jf.wait_for_scan(
            on_progress=lambda p: print(f"\r  {p:5.1f}%", end="", flush=True))
        print("\r" + " " * 20 + "\r", end="")
        say("  scan finished" if done else "  scan still running (timed out)")
        counts = jf.counts()
        result["counts"] = counts
        interesting = [(k, v) for k, v in sorted(counts.items()) if v]
        say("Item counts")
        for key, value in interesting:
            say(f"  {key:22} {value}")

    return result
