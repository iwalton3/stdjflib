"""Argument parsing and the subcommands."""

from __future__ import annotations

import argparse
import os
import shutil
import sys
import time

from . import (build, catalog, config, fetch, ff, jfserver, provision,
               recipes, verify)
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
    b.add_argument("--bulk", type=int, default=None, metavar="N",
                   help=(f"items per Bulk * library, for scale testing "
                         f"(paging, virtualised scroll, thumbnail cache, "
                         f"search, sort). Defaults to {config.DEFAULT_BULK} at "
                         f"the full tier and 0 below it; --bulk 0 disables, "
                         f"and any N overrides either way."))

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
        return sp

    s = server_args(sub.add_parser(
        "serve", help="build and run Jellyfin from source, then set it up"))
    s.add_argument("--source", default=os.path.expanduser("~/Desktop/jellyfin"),
                   help="path to a Jellyfin source checkout")
    s.add_argument("--state", default=None,
                   help="where the server keeps its data (default: "
                        "<root>/.stdjflib/jellyfin). Delete it for a fresh server.")
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
    pr.add_argument("--media-root", default=None,
                    help="path the *server* sees the library at, when that "
                         "differs from this machine's (containers, remote mounts)")

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


def cmd_verify(args) -> int:
    cfg = _config_from(args)
    checked, problems = verify.run(cfg)
    print(f"verified {checked} generated files against their recipes")
    if not problems:
        print("no problems")
        return 0
    print(f"{len(problems)} problem(s):")
    for p in problems:
        print(f"  {p}")
    return 1


def main(argv=None) -> int:
    args = _parser().parse_args(argv)
    handler = {
        "build": cmd_build, "verify": cmd_verify, "list": cmd_list,
        "doctor": cmd_doctor, "clean": cmd_clean,
        "serve": cmd_serve, "provision": cmd_provision,
        "accounts": cmd_accounts,
    }[args.command]
    try:
        return handler(args)
    except KeyboardInterrupt:
        print("\ninterrupted — rerun to resume", file=sys.stderr)
        return 130


def _provision_kwargs(args) -> dict:
    return {
        "password": args.password,
        "chapter_images": args.chapter_images,
        "trickplay": args.trickplay,
        "replace": args.replace_libraries,
        "scan": not args.no_scan,
        "server_name": args.server_name,
    }


def cmd_provision(args) -> int:
    root = os.path.abspath(args.root)
    jf = Jellyfin(args.server)
    provision.provision(jf, root, media_root=args.media_root,
                        **_provision_kwargs(args))
    _print_connection(args.server, args.password)
    return 0


def cmd_serve(args) -> int:
    root = os.path.abspath(args.root)
    state = os.path.abspath(
        args.state or os.path.join(root, config.STATE_DIR, "jellyfin"))
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

    try:
        jf = Jellyfin(instance.url)
        try:
            provision.provision(jf, root, still_alive=instance.alive,
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
    except KeyboardInterrupt:
        print("\nStopping.")
        return 0
    finally:
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
