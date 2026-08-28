"""Command line entry points."""

from __future__ import annotations

import argparse
import logging
import signal
import sys
from pathlib import Path

from .app import Application, build_application
from .config import ConfigError, load_config
from .frameio import FrameioError
from .logging_setup import setup_logging
from .resolver import NoMatch, ResolveError
from .state import STATUS_BASELINE, STATUS_NO_MATCH

log = logging.getLogger("frameio_export_watcher")


def _load(args: argparse.Namespace) -> Application:
    config = load_config(Path(args.config) if args.config else None)
    setup_logging(config.log_level, config.log_format)
    if args.dry_run:
        object.__setattr__(config, "dry_run", True)
    return build_application(config)


def cmd_run(args: argparse.Namespace) -> int:
    app = _load(args)
    service = app.service

    def handle(signum, _frame):  # noqa: ANN001 - signal handler signature
        log.info("received %s", signal.Signals(signum).name)
        service.stop()

    signal.signal(signal.SIGTERM, handle)
    signal.signal(signal.SIGINT, handle)
    try:
        service.run_forever()
    finally:
        app.close()
    return 0


def cmd_once(args: argparse.Namespace) -> int:
    app = _load(args)
    try:
        stats = app.service.run_cycle(wait=True, wait_for_stability=True)
    finally:
        app.close()
    print(
        f"seen={stats.seen} queued={stats.queued} uploaded={stats.uploaded} "
        f"no_match={stats.skipped_no_match} failed={stats.failed}"
    )
    for error in stats.errors:
        print(f"  error: {error}", file=sys.stderr)
    return 1 if stats.failed else 0


def cmd_baseline(args: argparse.Namespace) -> int:
    """Mark everything that exists now as handled, so only new files upload."""
    app = _load(args)
    dry_run = app.config.dry_run
    try:
        marked = app.service.baseline(dry_run=dry_run)
    finally:
        app.close()
    verb = "would mark" if dry_run else "marked"
    print(f"{verb} {marked} existing file(s) as already handled")
    if not dry_run and marked:
        print(f"release them again with: retry --status {STATUS_BASELINE}")
    return 0


def cmd_doctor(args: argparse.Namespace) -> int:
    """Check credentials, account access and the folder mapping end to end."""
    app = _load(args)
    problems = 0
    try:
        print(f"auth mode        : {app.config.auth.mode}")
        account_id = app.resolver.account_id()
        print(f"account          : {account_id}")

        projects = app.client.list_projects(account_id)
        print(f"projects visible : {len(projects)}")

        export_dirs = app.scanner.find_export_dirs()
        print(f"watch root       : {app.config.watch.root}")
        print(f"export folders   : {len(export_dirs)}")
        for export_dir in export_dirs[: args.limit]:
            try:
                outcome = app.resolver.resolve(export_dir.fields)
            except ResolveError as exc:
                problems += 1
                print(f"  {export_dir.path}\n      ERROR {exc}")
                continue
            if isinstance(outcome, NoMatch):
                print(f"  {export_dir.path}\n      no match -> {outcome.reason}")
            else:
                print(f"  {export_dir.path}\n      -> {outcome.display} ({outcome.folder_id})")
        if len(export_dirs) > args.limit:
            print(f"  ... and {len(export_dirs) - args.limit} more")
    except (FrameioError, ResolveError) as exc:
        print(f"FAILED: {exc}", file=sys.stderr)
        return 2
    finally:
        app.close()
    return 1 if problems else 0


def cmd_resolve(args: argparse.Namespace) -> int:
    """Show what a single server path maps to on Frame.io."""
    app = _load(args)
    try:
        target = Path(args.path).resolve()
        root = app.config.watch.root.resolve()
        try:
            relative = target.relative_to(root)
        except ValueError:
            print(f"{target} is not below the watch root {root}", file=sys.stderr)
            return 2

        parts = relative.parts
        template = app.config.watch.export_template
        fields = template.match(parts[: len(template.segments)])
        if fields is None:
            print(f"{target} does not match {template.raw}", file=sys.stderr)
            return 2
        print(f"fields   : {fields}")
        outcome = app.resolver.resolve(fields)
        if isinstance(outcome, NoMatch):
            print(f"frame.io : no match -- {outcome.reason}")
            return 1
        print(f"frame.io : {outcome.display}")
        print(f"folder id: {outcome.folder_id}")
    finally:
        app.close()
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    app = _load(args)
    try:
        counts = app.state.counts()
        if not counts:
            print("no files recorded yet")
        for status, count in sorted(counts.items()):
            print(f"{status:12} {count}")
        print()
        for record in app.state.recent(limit=args.limit, status=args.status):
            detail = record.last_error or record.frameio_file_id or ""
            print(f"{record.status:12} {record.path} {detail}")
    finally:
        app.close()
    return 0


def cmd_retry(args: argparse.Namespace) -> int:
    """Forget recorded outcomes so the next scan tries them again."""
    app = _load(args)
    try:
        records = app.state.recent(limit=100000, status=args.status)
        for record in records:
            app.state.forget(record.path)
        print(f"cleared {len(records)} record(s) with status {args.status}")
    finally:
        app.close()
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="frameio-export-watcher",
        description="Upload finished exports from the production server to Frame.io.",
    )
    parser.add_argument("-c", "--config", help="path to config.yaml")
    parser.add_argument(
        "--dry-run", action="store_true", help="resolve and log, but do not upload"
    )
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("run", help="watch continuously (the container default)").set_defaults(
        func=cmd_run
    )
    sub.add_parser("once", help="run a single scan and exit").set_defaults(func=cmd_once)

    sub.add_parser(
        "baseline",
        help="mark existing files as handled so only new ones are uploaded",
    ).set_defaults(func=cmd_baseline)

    doctor = sub.add_parser("doctor", help="verify credentials and folder mapping")
    doctor.add_argument("--limit", type=int, default=20, help="export folders to show")
    doctor.set_defaults(func=cmd_doctor)

    resolve = sub.add_parser("resolve", help="show the Frame.io target for one path")
    resolve.add_argument("path")
    resolve.set_defaults(func=cmd_resolve)

    status = sub.add_parser("status", help="show what has been uploaded or skipped")
    status.add_argument("--limit", type=int, default=20)
    status.add_argument("--status", help="filter by status")
    status.set_defaults(func=cmd_status)

    retry = sub.add_parser("retry", help="clear recorded outcomes so files are retried")
    retry.add_argument(
        "--status", default=STATUS_NO_MATCH, help="which status to clear (default: no_match)"
    )
    retry.set_defaults(func=cmd_retry)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except ConfigError as exc:
        setup_logging()
        log.error("configuration error: %s", exc)
        return 2
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
