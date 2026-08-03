"""Argument parsing and the subcommands."""

from __future__ import annotations

import argparse
import contextlib
import dataclasses
import os
import shutil
import signal
import sys
import time

from . import (build, catalog, config, container, fetch, ff, jfserver,
               livetv, origin, provision, recipes, verify)
from .jfapi import Jellyfin

DEFAULT_ROOT = os.environ.get("STDJFLIB_ROOT", "")


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="stdjflib",
        description="Build a standard Jellyfin QA library.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="Tiers:\n" + "\n".join(
            f"  {t:9} {config.TIER_HELP[t]}" for t in config.TIERS),
    )
    sub = p.add_subparsers(dest="command", required=True)

    def common(sp):
        sp.add_argument("root", nargs="?" if DEFAULT_ROOT else None,
                        default=DEFAULT_ROOT or None,
                        help="library output directory "
                             "(or set STDJFLIB_ROOT)")
        sp.add_argument("--tier", choices=config.TIERS, default="standard")
        sp.add_argument("--ffmpeg", default="ffmpeg")
        sp.add_argument("--ffprobe", default="ffprobe")
        sp.add_argument("-j", "--jobs", type=int, default=0,
                        help="parallel ffmpeg processes (default: half the CPUs)")
        sp.add_argument("--only", action="append", default=[],
                        metavar="LIBRARY",
                        help="build only this library folder; repeatable")
        sp.add_argument("--font", default=None,
                        help="TTF used for burned-in labels and bitmap subtitles")
        sp.add_argument("-v", "--verbose", action="store_true",
                        help="print every ffmpeg command line")
        return sp

    b = common(sub.add_parser("build", help="build or resume the library"))
    b.add_argument("--dry-run", action="store_true",
                   help="say what would be built and downloaded, write nothing")
    b.add_argument("--hwaccel", choices=("nvenc",), default=None,
                   help="use the GPU for large files. Faster, but the output "
                        "is not byte-identical to a software build, so a "
                        "library built this way will not match one built "
                        "without it.")
    b.add_argument("--no-keep-cache", action="store_true",
                   help="delete downloaded archives after unpacking")
    b.add_argument("--use-artwork", action="store_true",
                   help="back the artwork with photographs from picsum.photos "
                        "(Unsplash licence, credited in ATTRIBUTION.md) so "
                        "the library looks like a real one in screenshots. "
                        "Downloads up to ~105 MB, cached; the type stamp "
                        "comes off, so prefer the drawn artwork when the "
                        "shape of an image is what is being tested.")
    b.add_argument("--bulk", type=int, default=None, metavar="N",
                   help=(f"items per Bulk * library, for scale testing "
                         f"(paging, virtualised scroll, thumbnail cache, "
                         f"search, sort). Defaults to {config.DEFAULT_BULK} at "
                         f"the full tier and 0 below it; --bulk 0 disables, "
                         f"and any N overrides either way."))
    b.add_argument("--stream-origin", default=None, metavar="URL",
                   help=(f"base URL the local-origin .strm fixtures should "
                         f"name (default: {origin.default_base_url()}). It is "
                         f"written into the files, so set it here if Jellyfin "
                         f"will not be on this machine — a container wants "
                         f"http://host.containers.internal:{origin.DEFAULT_PORT}."))

    a = common(sub.add_parser(
        "artwork",
        help="redraw every image in a library that is already built"))
    a.add_argument("--bulk", type=int, default=None, metavar="N",
                   help="items per Bulk * library (default: whatever the "
                        "manifest says was built)")
    a.add_argument("--use-artwork", action="store_true", default=None,
                   help="back the artwork with photographs from picsum.photos "
                        "(Unsplash licence, credited in ATTRIBUTION.md) so "
                        "the library looks like a real one in screenshots. "
                        "Downloads up to ~105 MB, cached; the type stamp "
                        "comes off, so prefer the drawn artwork when the "
                        "shape of an image is what is being tested.")
    a.add_argument("--drawn-artwork", dest="use_artwork", action="store_false",
                   help="go back to the drawn artwork, with its type stamps")
    # Tier and bulk come from the manifest unless they are given, so a redraw
    # cannot quietly cover less of the library than is on disk.
    a.set_defaults(tier=None)

    common(sub.add_parser("verify", help="check a built library against its manifest"))

    ls = sub.add_parser("list", help="list what a tier contains, without building")
    ls.add_argument("--tier", choices=config.TIERS, default="standard")

    d = sub.add_parser("doctor", help="report what this machine can build")
    d.add_argument("--ffmpeg", default="ffmpeg")

    c = common(sub.add_parser("clean", help="delete built content"))
    c.add_argument("--cache", action="store_true",
                   help="also delete the download cache")
    c.add_argument("--yes", action="store_true",
                   help="skip the confirmation prompt")

    def server_args(sp):
        sp.add_argument("root", nargs="?" if DEFAULT_ROOT else None,
                        default=DEFAULT_ROOT or None,
                        help="the built library (or set STDJFLIB_ROOT)")
        sp.add_argument("--password", default=provision.DEFAULT_PASSWORD,
                        help="password for the QA accounts")
        sp.add_argument("--no-scan", action="store_true",
                        help="do not trigger a library scan")
        sp.add_argument("--replace-libraries", action="store_true",
                        help="delete and recreate libraries that already exist")
        sp.add_argument("--chapter-images", action="store_true",
                        help="extract chapter images during the scan (slow)")
        sp.add_argument("--trickplay", action="store_true",
                        help="generate trickplay tiles during the scan (very slow)")
        sp.add_argument("--server-name", default="stdjflib QA",
                        help="the name the server reports")
        sp.add_argument("-v", "--verbose", action="store_true",
                        help="show the build output")
        sp.add_argument("--no-stream-origin", action="store_true",
                        help="do not serve the local .strm origin. Those two "
                             "fixtures then resolve and do not play, which is "
                             "a state worth being able to reach on purpose.")
        sp.add_argument("--live-tv", action="store_true",
                        help="also run faketvsource and wire it up as a tuner, "
                             "so the client's Live TV screens become testable")
        sp.add_argument("--tuner-type", choices=livetv.TUNER_TYPES,
                        default="m3u",
                        help="M3U and HDHomeRun are separate code paths in "
                             "Jellyfin, so they are worth testing separately "
                             "(default: m3u)")
        sp.add_argument("--tuner-count", type=int, default=0, metavar="N",
                        help="simulated tuner limit; tune more channels than "
                             "this and the source answers 503, like a real "
                             "tuner out of capacity (0 = unlimited)")
        sp.add_argument("--faketv-source", default=None,
                        help="path to a faketvsource checkout")
        sp.add_argument("--faketv-port", type=int, default=livetv.DEFAULT_PORT)
        return sp

    s = server_args(sub.add_parser(
        "serve", help="build and run Jellyfin from source, then set it up"))
    s.add_argument("--source", default=os.path.expanduser("~/Desktop/jellyfin"),
                   help="path to a Jellyfin source checkout")
    s.add_argument("--state", default=None,
                   help="where the server keeps its data (default: a "
                        "per-library directory under the system temp dir, "
                        "printed on start — the library itself is often on a "
                        "network mount, which SQLite does not survive). "
                        "Delete it for a fresh server.")
    s.add_argument("--port", type=int, default=jfserver.DEFAULT_PORT)
    s.add_argument("--no-build", action="store_true",
                   help="use the existing build instead of compiling")
    s.add_argument("--artifacts", default=None,
                   help="build output directory (default: alongside --state). "
                        "Never inside the Jellyfin checkout.")
    s.add_argument("--fresh", action="store_true",
                   help="delete the server state first, for a factory-fresh run")
    s.add_argument("--stop-after-setup", action="store_true",
                   help="shut the server down once it is provisioned")

    pr = server_args(sub.add_parser(
        "provision", help="set up an already-running Jellyfin server"))
    pr.add_argument("--server", default="http://127.0.0.1:8096",
                    help="base URL of the running server")
    pr.add_argument("--live-tv-host", default=None, metavar="HOST",
                    help="hostname or IP the *server* should use to reach "
                         "faketvsource running here (default: 127.0.0.1, "
                         "which only works when the server is on this machine)")
    pr.add_argument("--media-root", default=None,
                    help="path the *server* sees the library at, when that "
                         "differs from this machine's (containers, remote mounts)")

    ct = server_args(sub.add_parser(
        "container", help="run Jellyfin in a container, then set it up"))
    ct.add_argument("--runtime", choices=container.RUNTIMES, default=None,
                    help="podman or docker (default: whichever is on PATH)")
    ct.add_argument("--image", default=container.DEFAULT_IMAGE)
    ct.add_argument("--name", default=container.DEFAULT_NAME)
    ct.add_argument("--port", type=int, default=8096)
    ct.add_argument("--state", default=None,
                    help="host directory for the server's config and cache "
                         "(default: a per-library directory under the system "
                         "temp dir, printed on start)")
    ct.add_argument("--fresh", action="store_true",
                    help="delete the server state first")
    ct.add_argument("--no-pull", action="store_true",
                    help="use the local image without checking for a newer one")
    ct.add_argument("--keep-running", action="store_true",
                    help="leave the container up and return, instead of "
                         "following it until Ctrl-C")
    ct.add_argument("--arg", action="append", default=[], dest="extra_args",
                    metavar="ARG",
                    help="extra argument for the container runtime; repeatable")

    st = sub.add_parser("container-stop",
                        help="stop and remove the QA container")
    st.add_argument("--runtime", choices=container.RUNTIMES, default=None)
    st.add_argument("--name", default=container.DEFAULT_NAME)

    sub.add_parser("accounts",
                   help="list the test accounts and what each one is for")
    return p


def resolve_bulk(requested: int | None, tier: str) -> int:
    """How many bulk items to build.

    The bulk libraries are a full-tier feature, but an explicit `--bulk`
    always wins — including `--bulk 0` to leave them out of a full build, and
    a non-zero N to pull them into a smaller one.
    """
    if requested is not None:
        return max(0, requested)
    return config.DEFAULT_BULK if tier == "full" else 0


def _config_from(args) -> config.BuildConfig:
    return config.BuildConfig(
        root=os.path.abspath(args.root),
        tier=getattr(args, "tier", "standard"),
        ffmpeg=args.ffmpeg,
        ffprobe=getattr(args, "ffprobe", "ffprobe"),
        jobs=getattr(args, "jobs", 0),
        dry_run=getattr(args, "dry_run", False),
        verbose=getattr(args, "verbose", False),
        font_file=getattr(args, "font", None) or config.find_font(),
        only=tuple(getattr(args, "only", []) or ()),
        keep_cache=not getattr(args, "no_keep_cache", False),
        hwaccel=getattr(args, "hwaccel", None),
        bulk=resolve_bulk(getattr(args, "bulk", None),
                          getattr(args, "tier", "standard")),
        use_artwork=bool(getattr(args, "use_artwork", False)),
        stream_origin=getattr(args, "stream_origin", None) or "",
    )


def cmd_list(args) -> int:
    tier = args.tier
    rs = recipes.for_tier(tier)
    groups: dict[str, list] = {}
    for r in rs:
        groups.setdefault(r.group, []).append(r)

    print(f"Tier {tier}: {len(rs)} generated files, "
          f"{len(catalog.for_tier(tier))} downloads "
          f"({fetch.human(catalog.estimated_bytes(tier))})")
    print()
    for group, items in groups.items():
        print(f"{group} ({len(items)})")
        for r in items:
            bits = [r.container]
            if r.video:
                bits.append(f"{r.video.width}x{r.video.height}")
            if r.audios:
                bits.append(f"{len(r.audios)}a")
            if r.subs:
                bits.append(f"{len(r.subs)}s")
            print(f"  {r.title:38} {' '.join(bits)}")
        print()
    downloads = catalog.for_tier(tier)
    if downloads:
        print(f"Downloads ({len(downloads)})")
        for s in downloads:
            if s.kind == "subtitle":
                continue
            print(f"  {s.title:38} {fetch.human(s.size):>10}  {s.licence}")
        subs = [s for s in downloads if s.kind == "subtitle"]
        if subs:
            print(f"  {'(subtitle files)':38} {len(subs)} files")
    return 0


def cmd_doctor(args) -> int:
    print(f"ffmpeg   {ff.version(args.ffmpeg)}")
    caps = ff.capabilities(args.ffmpeg)
    if not caps:
        print("  ! could not run ffmpeg — nothing can be built")
        return 1

    font = config.find_font()
    print(f"font     {font or '! none found'}")
    if not font:
        print("         labels and bitmap subtitles will be skipped")

    print()
    print("Encoders needed by the full tier:")
    wanted = sorted({e for r in recipes.all_recipes() for e in r.encoders})
    missing = [e for e in wanted if e not in caps]
    for enc in wanted:
        print(f"  {'ok ' if enc in caps else 'MISSING'} {enc}")
    print()
    hw = [e for e in ("h264_nvenc", "hevc_nvenc", "av1_nvenc") if e in caps]
    print(f"hardware {' '.join(hw) if hw else 'none (nvenc unavailable)'}")

    if missing:
        print()
        print(f"{len(missing)} encoder(s) missing; those recipes will be "
              f"skipped rather than failing the build.")
    return 0


def cmd_clean(args) -> int:
    cfg = _config_from(args)
    if not os.path.isdir(cfg.root):
        print(f"nothing at {cfg.root}")
        return 0
    # Clean removes what is on disk, not what this invocation's tier would
    # build — so the bulk folders go too, even when --bulk was not passed.
    everything = {**config.LIBRARIES, **config.BULK_LIBRARIES}
    targets = [cfg.path(name) for name in everything if cfg.wants(name)]
    targets += [cfg.path(config.MANIFEST), cfg.path(config.ATTRIBUTION),
                cfg.path("README.md")]
    if args.cache:
        targets.append(cfg.cache())

    existing = [t for t in targets if os.path.exists(t)]
    if not existing:
        print("nothing to remove")
        return 0
    print("About to delete:")
    for t in existing:
        print(f"  {t}")

    if args.yes:
        pass
    elif not sys.stdin.isatty():
        # Without a terminal there is nobody to answer, and input() would
        # block forever — which for a delete is the worst way to fail.
        print("refusing to delete without a terminal; pass --yes if you mean it",
              file=sys.stderr)
        return 1
    else:
        reply = input("Type 'yes' to confirm: ").strip().lower()
        if reply != "yes":
            print("aborted")
            return 1
    for t in existing:
        if os.path.isdir(t):
            shutil.rmtree(t)
        else:
            os.unlink(t)
    print(f"removed {len(existing)} paths")
    return 0


def cmd_build(args) -> int:
    cfg = _config_from(args)
    build.run(cfg)
    return 0


def cmd_artwork(args) -> int:
    """Redraw the images of an existing library, in place.

    Every builder runs, with the media steps switched off. That is the point:
    the artwork lands wherever a build would have put it, including image
    types added since this library was built — which a pass over the files on
    disk could not discover, because those files are not there yet.
    """
    root = os.path.abspath(args.root)
    previous = build.read_manifest(root)
    if not previous:
        print(f"no manifest under {root} — is this a stdjflib library?",
              file=sys.stderr)
        return 1
    if args.tier is None:
        args.tier = previous.get("tier", "standard")
    if args.bulk is None:
        args.bulk = previous.get("bulk", 0)
    if args.use_artwork is None:
        # Whatever the library already is, unless told otherwise — a redraw
        # should not silently swap a photographic library back to drawn.
        args.use_artwork = previous.get("use_artwork", False)

    cfg = dataclasses.replace(_config_from(args), artwork_only=True)
    if not cfg.font_file:
        print("no font found, so there is nothing to draw with — pass --font",
              file=sys.stderr)
        return 1
    build.run(cfg)
    return 0


def cmd_verify(args) -> int:
    cfg = _config_from(args)
    report = verify.run(cfg)
    print(f"re-probed {report.files} generated files against their recipes "
          f"and {report.images} images against the shape their type calls for")
    if report.streams:
        print(f"re-read {report.streams} stream files for the URL Jellyfin "
              f"would take from each")
    for note in report.notes:
        print(f"  note: {note}")
    if not report.problems:
        print("no problems")
        return 0
    print(f"{len(report.problems)} problem(s):")
    for problem in report.problems:
        print(f"  {problem}")
    return 1


def main(argv=None) -> int:
    args = _parser().parse_args(argv)
    handler = {
        "build": cmd_build, "verify": cmd_verify, "list": cmd_list,
        "artwork": cmd_artwork,
        "doctor": cmd_doctor, "clean": cmd_clean,
        "serve": cmd_serve, "provision": cmd_provision,
        "accounts": cmd_accounts, "container": cmd_container,
        "container-stop": cmd_container_stop,
    }[args.command]
    try:
        return handler(args)
    except KeyboardInterrupt:
        print("\ninterrupted — rerun to resume", file=sys.stderr)
        return 130


@contextlib.contextmanager
def _stop_on_signals():
    """Make SIGTERM and SIGHUP unwind the way Ctrl-C already does.

    Python's default SIGTERM action kills the interpreter where it stands: no
    exception is raised, so no `finally` runs and nothing is cleaned up. Every
    child here is started with `start_new_session=True` — deliberately, so
    that stopping one can signal a whole process group rather than leaving the
    dotnet host behind — and that same isolation means a signal sent to *this*
    process never reaches them.

    Those two facts together are the bug: `kill <pid>` on a `serve` leaves a
    Jellyfin holding port 8096 and a faketvsource holding 8409, both still
    scanning the library, and the next run fails with "port is busy" rather
    than with anything that points at the cause. Ctrl-C was fine and a kill
    was not, which is the sort of difference nobody discovers until a script
    is doing the killing.

    SIGINT already arrives as KeyboardInterrupt, so raising the same exception
    means there is one shutdown path to get right instead of two.

    Each handler restores the default disposition before raising, so a second
    signal kills outright — the escape hatch if the shutdown itself wedges.
    """
    handled = {}
    for name in ("SIGTERM", "SIGHUP"):     # SIGHUP does not exist on Windows
        signum = getattr(signal, name, None)
        if signum is not None:
            handled[signum] = name

    def handler(signum, _frame):
        signal.signal(signum, signal.SIG_DFL)
        raise KeyboardInterrupt(handled.get(signum, signum))

    previous = {}
    for signum in handled:
        try:
            previous[signum] = signal.signal(signum, handler)
        except (OSError, ValueError):
            # Not the main thread, or a platform without this signal. Losing
            # the handler is worth less than refusing to run.
            pass
    try:
        yield
    finally:
        for signum, old in previous.items():
            with contextlib.suppress(OSError, ValueError):
                signal.signal(signum, old)


def _start_faketv(args, state: str, public_host: str | None):
    """Start faketvsource, or explain why it cannot be started.

    Returns (instance, url_for_the_server) or (None, None).
    """
    if not getattr(args, "live_tv", False):
        return None, None
    source = livetv.find_source(args.faketv_source)
    if not source:
        raise RuntimeError(
            "--live-tv needs a faketvsource checkout; none found. "
            "Pass --faketv-source /path/to/faketvsource.")
    if not livetv.has_ffmpeg():
        raise RuntimeError("--live-tv needs ffmpeg on PATH")

    fake = livetv.FakeTv(source, state, port=args.faketv_port,
                         tuner_count=args.tuner_count,
                         public_host=public_host, verbose=args.verbose)
    print(f"Starting faketvsource from {source}", flush=True)
    fake.start()
    channels = fake.wait_until_up()
    print(f"  {channels} channels on {fake.public_url}"
          f"{'' if public_host is None else ' (as the server sees it)'}",
          flush=True)
    return fake, fake.public_url


def _start_origin(args, root: str, *, from_container: str | None = None):
    """Serve the local `.strm` origin, if this library has one.

    Returns the running server or None. Never fatal: a library built before
    the origin fixtures existed simply has nothing to serve, and a port
    already taken costs those two fixtures rather than the whole run.
    """
    if getattr(args, "no_stream_origin", False):
        return None
    base = build.read_manifest(root).get("stream_origin")
    if not base:
        return None
    server = origin.Origin(root, port=origin.port_of(base))
    files = server.files()
    if not files:
        return None

    ok, why = origin.describe_reachability(base, from_container=from_container)
    if not ok:
        print(f"  ! {why}", flush=True)
    if origin.port_in_use(server.port):
        print(f"  ! port {server.port} is busy, so the local-origin stream "
              f"fixtures will not play", flush=True)
        return None
    try:
        server.start()
    except OSError as exc:
        print(f"  ! could not serve the stream origin: {exc}", flush=True)
        return None
    print(f"Stream origin: {len(files)} file(s) on {base}", flush=True)
    return server


def _provision_kwargs(args) -> dict:
    return {
        "password": args.password,
        "chapter_images": args.chapter_images,
        "trickplay": args.trickplay,
        "replace": args.replace_libraries,
        "scan": not args.no_scan,
        "server_name": args.server_name,
        "tuner_type": args.tuner_type,
        "tuner_count": args.tuner_count,
    }


def cmd_provision(args) -> int:
    with _stop_on_signals():
        return _provision_run(args)


def _provision_run(args) -> int:
    root = os.path.abspath(args.root)
    state = config.runtime_dir(root, "faketv")
    fake = None
    origin_server = None
    try:
        # A server elsewhere cannot reach 127.0.0.1 here, so make the operator
        # say how it should reach us rather than guessing wrong and producing
        # a tuner that saves fine and never plays.
        fake, url = _start_faketv(args, state, args.live_tv_host)
        origin_server = _start_origin(args, root)
        jf = Jellyfin(args.server)
        provision.provision(jf, root, media_root=args.media_root,
                            live_tv_url=url, **_provision_kwargs(args))
        _print_connection(args.server, args.password)
        if fake:
            print(f"\nfaketvsource is running as long as this command is.")
            print("  Ctrl-C to stop it.")
            try:
                while fake.alive():
                    time.sleep(1)
            except KeyboardInterrupt:
                print("\nStopping.")
        return 0
    except RuntimeError as exc:
        print(f"\n{exc}", file=sys.stderr)
        return 1
    finally:
        if origin_server:
            origin_server.stop()
        if fake:
            fake.stop()


def cmd_serve(args) -> int:
    # The signal handlers go on outside everything, so they are already in
    # place before the first child is started and still in place while the
    # `finally` below is stopping it.
    with _stop_on_signals():
        return _serve(args)


def _serve(args) -> int:
    root = os.path.abspath(args.root)
    state = os.path.abspath(args.state or config.runtime_dir(root, "jellyfin"))
    artifacts = os.path.abspath(args.artifacts or state + "-build")

    if args.fresh and os.path.isdir(state):
        print(f"Removing {state}")
        shutil.rmtree(state)

    if jfserver.port_in_use(args.port):
        # A server we just stopped can hold the port for a moment; give it a
        # few seconds before declaring the port taken.
        print(f"Port {args.port} is busy; waiting for it to free")
        for _ in range(10):
            time.sleep(1)
            if not jfserver.port_in_use(args.port):
                break
        else:
            print(f"Something is still listening on port {args.port}. "
                  f"Stop it, or pass --port.", file=sys.stderr)
            return 1

    dll = jfserver.dll_path(artifacts)
    if args.no_build:
        if not os.path.exists(dll):
            print(f"No build at {dll}; drop --no-build to compile it.",
                  file=sys.stderr)
            return 1
    else:
        print(f"Building Jellyfin from {args.source}")
        print(f"  output -> {artifacts}  (nothing is written into the checkout)")
        try:
            dll = jfserver.build(args.source, artifacts, verbose=args.verbose)
        except RuntimeError as exc:
            print(f"\n{exc}", file=sys.stderr)
            return 1

    web = jfserver.find_web_client(args.source)
    instance = jfserver.Instance(dll, state, port=args.port, web_dir=web,
                                 ffmpeg=shutil.which("ffmpeg"),
                                 verbose=args.verbose)
    print(f"Starting the server on {instance.url}")
    print(f"  state {state}")
    print(f"  web   {web or 'not built, running --nowebclient'}")
    if not web:
        print("        (the API is all a client needs; build jellyfin-web "
              "only if you want the browser UI)")
    instance.start()

    fake = None
    origin_server = None
    try:
        # Both processes are on this machine, so loopback is what the server
        # should use.
        fake, live_tv_url = _start_faketv(args, state, None)
        origin_server = _start_origin(args, root)
        jf = Jellyfin(instance.url)
        try:
            provision.provision(jf, root, still_alive=instance.alive,
                                live_tv_url=live_tv_url,
                                **_provision_kwargs(args))
        except Exception:
            if not instance.alive():
                print("\nThe server exited. Last of its log:\n",
                      file=sys.stderr)
                print(instance.log_tail(), file=sys.stderr)
            raise
        _print_connection(instance.url, args.password)

        if args.stop_after_setup:
            print("\nStopping the server (--stop-after-setup).")
            return 0
        print(f"\nServer log: {instance.log_path}")
        print("Ctrl-C to stop.")
        while instance.alive():
            time.sleep(1)
        print("\nThe server exited on its own. Last of its log:\n")
        print(instance.log_tail())
        return 1
    except RuntimeError as exc:
        print(f"\n{exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\nStopping.")
        return 0
    finally:
        if origin_server:
            origin_server.stop()
        if fake:
            fake.stop()
        instance.stop()


def _print_connection(url: str, password: str) -> None:
    print()
    print(f"  Server    {url}")
    print(f"  Sign in   {provision.ACCOUNTS[0]['name']} / {password}")
    print(f"  Accounts  {len(provision.ACCOUNTS)} — `stdjflib accounts` "
          f"explains what each is for")


def cmd_accounts(_args) -> int:
    print(f"{len(provision.ACCOUNTS)} test accounts, created by "
          f"`stdjflib serve` / `stdjflib provision`.")
    print(f"Default password: {provision.DEFAULT_PASSWORD}")
    print()
    for account in provision.ACCOUNTS:
        password = account["password"] or "(none)"
        print(f"{account['name']}  [{password}]")
        for line in account["why"].split(". "):
            line = line.strip().rstrip(".")
            if line:
                print(f"    {line}.")
        print()
    return 0


def cmd_container(args) -> int:
    with _stop_on_signals():
        return _container_run(args)


def _container_run(args) -> int:
    root = os.path.abspath(args.root)
    state = os.path.abspath(args.state or config.runtime_dir(root, "container"))
    try:
        runtime = container.pick_runtime(args.runtime)
    except container.ContainerError as exc:
        print(exc, file=sys.stderr)
        return 1

    if args.fresh and os.path.isdir(state):
        print(f"Removing {state}")
        shutil.rmtree(state)

    box = container.Container(root, state, runtime=runtime, image=args.image,
                              name=args.name, port=args.port,
                              extra_args=tuple(args.extra_args),
                              verbose=args.verbose)
    # Flushed, because stderr is unbuffered and stdout is not when piped —
    # without this a runtime error appears above the header that explains it.
    print(f"Jellyfin in {runtime}", flush=True)
    print(f"  image {args.image}", flush=True)
    print(f"  state {state}", flush=True)
    print(f"  media {root} -> {box.media_root} (read-only)", flush=True)

    try:
        if not args.no_pull:
            print("  pulling", flush=True)
            box.pull()

        # Prove the mount before provisioning: a library the container cannot
        # read is created without complaint and then scans to nothing, which
        # looks like a Jellyfin fault rather than a mount one.
        ok, detail = box.check_library_visible()
        if not ok:
            print(f"\nThe container cannot read the library.\n  {detail}",
                  file=sys.stderr)
            return 1
        print(f"  visible: {detail}", flush=True)

        print("Starting the container", flush=True)
        box.start()
    except container.ContainerError as exc:
        print(f"\n{exc}", file=sys.stderr)
        return 1

    fake = None
    origin_server = None
    try:
        # faketvsource runs on the host; inside the container, 127.0.0.1 is the
        # container itself. Podman and Docker each publish a different name for
        # "the host", and neither resolves outside a container.
        fake, live_tv_url = _start_faketv(
            args, state, livetv.HOST_FROM_CONTAINER.get(runtime))
        # The origin has the same problem and cannot be fixed the same way:
        # its URL is already inside the `.strm` files. So this serves it and
        # says so when the address in those files is one the container cannot
        # reach, rather than letting the scan produce items that never play.
        origin_server = _start_origin(
            args, root, from_container=livetv.HOST_FROM_CONTAINER.get(runtime))
        jf = Jellyfin(box.url)
        try:
            # The server must be told its own path to the media, not ours.
            provision.provision(jf, root, media_root=box.media_root,
                                still_alive=box.alive,
                                live_tv_url=live_tv_url,
                                **_provision_kwargs(args))
        except Exception:
            if not box.alive():
                print("\nThe container exited. Last of its log:\n",
                      file=sys.stderr)
                print(box.logs(), file=sys.stderr)
            raise
        _print_connection(box.url, args.password)

        if args.keep_running:
            print(f"\nContainer {box.name} left running.")
            print(f"  logs  {runtime} logs -f {box.name}")
            print(f"  stop  ./stdjflib.py container-stop --runtime {runtime}")
            if fake:
                # Left running deliberately: killing it here would leave the
                # container pointing at a tuner that no longer answers, which
                # looks like a broken tuner rather than a stopped one.
                print(f"  faketvsource left running on {fake.public_url}"
                      f" (pid {fake.process.pid})")
                print(f"  stop  kill {fake.process.pid}")
                fake = None
            if origin_server:
                # The origin is a thread in *this* process, so unlike
                # faketvsource it cannot be left behind — returning here ends
                # it. Say so, because a container left running with the origin
                # gone is a library whose stream fixtures stop playing for a
                # reason nothing else would explain.
                print(f"  ! the stream origin stops with this command; "
                      f"the local-origin .strm fixtures will not play "
                      f"while the container outlives it")
            return 0
        print(f"\nFollowing {box.name}. Ctrl-C to stop and remove it.")
        while box.alive():
            time.sleep(2)
        print("\nThe container exited. Last of its log:\n")
        print(box.logs())
        return 1
    except RuntimeError as exc:
        print(f"\n{exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\nStopping.")
        return 0
    finally:
        if origin_server:
            origin_server.stop()
        if fake:
            fake.stop()
        if not args.keep_running:
            box.stop()
            box.remove()


def cmd_container_stop(args) -> int:
    try:
        runtime = container.pick_runtime(args.runtime)
    except container.ContainerError as exc:
        print(exc, file=sys.stderr)
        return 1
    box = container.Container(".", ".", runtime=runtime, name=args.name)
    if not box.exists():
        print(f"No container named {args.name}")
        return 0
    box.stop()
    box.remove()
    print(f"Stopped and removed {args.name}")
    return 0
