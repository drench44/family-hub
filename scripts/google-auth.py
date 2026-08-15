#!/usr/bin/env python3
"""One-time Google Calendar OAuth — run ON THE MAC, not the box.

Setup (once, in the Google Cloud console):
  1. Create a project and enable the **Google Calendar API**.
  2. Configure the OAuth consent screen (External, add yourself as a test user).
  3. Create an OAuth client of type **Desktop app**. Download its JSON and save
     it next to this script as `client_secret.json`.

Then:
  cd scripts
  python3 google-auth.py

A browser opens; approve read-only Calendar access. This writes `token.json`
here and prints the scp command to copy it onto the box's data dir.

BOTH `client_secret.json` AND `token.json` are secrets — never commit either.
They are git-ignored; keep them out of the repo. The box only needs token.json.
"""
import os

from google_auth_oauthlib.flow import InstalledAppFlow

SCOPES = ["https://www.googleapis.com/auth/calendar.readonly"]
HERE = os.path.dirname(os.path.abspath(__file__))
CLIENT_SECRET = os.path.join(HERE, "client_secret.json")
TOKEN = os.path.join(HERE, "token.json")


def main() -> None:
    if not os.path.exists(CLIENT_SECRET):
        raise SystemExit(
            f"Missing {CLIENT_SECRET}. Download the Desktop OAuth client JSON "
            "from the Google Cloud console and save it there first "
            "(see this script's docstring).")
    flow = InstalledAppFlow.from_client_secrets_file(CLIENT_SECRET, scopes=SCOPES)
    creds = flow.run_local_server(port=0)
    with open(TOKEN, "w", encoding="utf-8") as f:
        f.write(creds.to_json())
    print(f"\nWrote {TOKEN}")
    print("Copy it to the box (mode 600 on arrival):")
    print("  scp token.json YOUR-SERVER:~/family-hub/data/")
    print("\nThen add your calendars to config.json and restart the container.")


if __name__ == "__main__":
    main()
