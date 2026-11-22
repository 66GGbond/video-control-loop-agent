"""Local operator tools. Never exposed as web endpoints.

python3 -m backend.app.trial_admin pending
python3 -m backend.app.trial_admin unlock VISITOR_ID
python3 -m backend.app.trial_admin settle RESERVATION_ID --result used|released
"""
import argparse
import json
from .server import TRIAL


def main():
    parser = argparse.ArgumentParser(description="Manage trial locks and uncertain submissions on the server")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("pending")
    commands.add_parser("locked")
    unlock = commands.add_parser("unlock")
    unlock.add_argument("visitor")
    settle = commands.add_parser("settle")
    settle.add_argument("reservation")
    settle.add_argument("--result", choices=["used", "released"], required=True)
    args = parser.parse_args()
    with TRIAL.connect() as db:
        if args.command == "pending":
            rows = db.execute("SELECT id,visitor,seconds,job FROM reservations WHERE status='held'").fetchall()
            print(json.dumps([dict(r) for r in rows], ensure_ascii=False, indent=2))
        elif args.command == "locked":
            rows = db.execute("SELECT id,failures FROM visitors WHERE failures>=5").fetchall()
            print(json.dumps([dict(r) for r in rows], ensure_ascii=False, indent=2))
        elif args.command == "unlock":
            changed = db.execute("UPDATE visitors SET failures=0 WHERE id=?", (args.visitor,)).rowcount
            print(f"Unlocked {changed} visitor(s).")
        else:
            changed = db.execute("UPDATE reservations SET status=? WHERE id=? AND status='held'", (args.result, args.reservation)).rowcount
            print(f"Settled {changed} pending reservation(s).")


if __name__ == "__main__":
    main()
