"""Command line: `order-desk <command>`.

    init            create the database and load the demo data
    demo            run one full order on a demo database, offline (no keys)
    serve           start the MCP server (dealer + shop endpoints)
    sender          start the outbox sender (Telegram messages)
    scheduler       start the timed jobs (expiry, reminders)
    devbot          start the reference agent on Telegram (--role dealer|shop)
    export-skills   write the Hermes skill and SOUL files into ./hermes
    new-token       print a random token for .env
"""

from __future__ import annotations

import argparse
import secrets
import sys
from pathlib import Path

from order_desk.config import load_settings


def cmd_init(args) -> None:
    from order_desk.db.connection import Database
    from order_desk.db.seed import seed_demo

    settings = load_settings(args.env)
    db = Database(settings.db_path)
    db.initialize()
    seeded = seed_demo(db, settings)
    with db.read() as conn:
        counts = {t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
                  for t in ("products", "dealers", "staff", "bills")}
    print(f"Database: {settings.db_path}")
    print("Demo data loaded." if seeded else "Demo data was already there (staff list updated).")
    print("Rows:", ", ".join(f"{k}={v}" for k, v in counts.items()))
    if counts["staff"] == 0:
        print("WARNING: no staff. Set OWNER_TELEGRAM_IDS in .env and run init again.")


def cmd_demo(args) -> None:
    from order_desk.demo import run_demo

    run_demo(Path(args.workdir))


def cmd_serve(args) -> None:
    from order_desk.mcp_server.server import main

    main(load_settings(args.env))


def cmd_sender(args) -> None:
    from order_desk.workers.outbox_sender import main

    main(load_settings(args.env))


def cmd_scheduler(args) -> None:
    from order_desk.workers.scheduler import main

    main(load_settings(args.env))


def cmd_devbot(args) -> None:
    from order_desk.agent.dev_bot import main

    main(load_settings(args.env), args.role)


def cmd_export_skills(args) -> None:
    from order_desk.agent.skills_export import export

    for path in export(Path(args.out)):
        print("wrote", path)


def cmd_new_token(_args) -> None:
    print(secrets.token_hex(32))


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="order-desk", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--env", default=".env", help="path to the .env file (default: .env)")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("init").set_defaults(fn=cmd_init)
    demo = sub.add_parser("demo")
    demo.add_argument("--workdir", default="data/demo", help="folder for the demo database")
    demo.set_defaults(fn=cmd_demo)
    sub.add_parser("serve").set_defaults(fn=cmd_serve)
    sub.add_parser("sender").set_defaults(fn=cmd_sender)
    sub.add_parser("scheduler").set_defaults(fn=cmd_scheduler)
    devbot = sub.add_parser("devbot")
    devbot.add_argument("--role", choices=["dealer", "shop"], required=True)
    devbot.set_defaults(fn=cmd_devbot)
    export = sub.add_parser("export-skills")
    export.add_argument("--out", default="hermes")
    export.set_defaults(fn=cmd_export_skills)
    sub.add_parser("new-token").set_defaults(fn=cmd_new_token)
    args = parser.parse_args(argv)
    args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
