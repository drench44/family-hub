"""Tests for the REAL GoogleCalendarClient adapter — previously untested, and
its two most failure-prone behaviors: pagination (a dropped nextPageToken
silently truncates the calendar) and token refresh that rewrites token.json on
disk (a regression can corrupt the token and break auth permanently).

The google libraries are mocked so no network/credentials are needed, but the
client's own pagination/refresh logic runs for real.
"""
import logging
from unittest import mock

from family_hub.calendar_sync import GoogleCalendarClient


def test_configured_false_when_token_absent(tmp_path):
    assert GoogleCalendarClient(str(tmp_path / "nope.json")).configured() is False


def test_configured_corrupt_token_warns_once_then_stays_quiet(tmp_path, caplog):
    """A PRESENT-but-unparseable token must (a) fail closed to unconfigured and
    (b) log the real cause — but only ONCE, since configured() runs every sync
    tick and a persistently-corrupt file must not spam a stack trace each cycle."""
    tok = tmp_path / "token.json"
    tok.write_text("{ not valid json")
    client = GoogleCalendarClient(str(tok))
    with caplog.at_level(logging.WARNING, logger="family_hub.calendar"):
        assert client.configured() is False   # present but unparseable -> not connected
        assert client.configured() is False   # a later tick, still broken
    warns = [r for r in caplog.records if "did not parse" in r.getMessage()]
    assert len(warns) == 1, "a persistently-corrupt token warns once, not every tick"


def _fake_service():
    return mock.MagicMock()


def test_fetch_events_follows_pagination():
    svc = _fake_service()
    page1 = {"items": [{"id": "e1"}, {"id": "e2"}], "nextPageToken": "PAGE2"}
    page2 = {"items": [{"id": "e3"}]}   # no nextPageToken -> stop
    svc.events.return_value.list.return_value.execute.side_effect = [page1, page2]
    with mock.patch("googleapiclient.discovery.build", return_value=svc), \
         mock.patch.object(GoogleCalendarClient, "_creds", return_value="creds"):
        items = GoogleCalendarClient("/tmp/tok.json").fetch_events("cal", "lo", "hi")
    assert [i["id"] for i in items] == ["e1", "e2", "e3"]   # both pages collected
    calls = svc.events.return_value.list.call_args_list
    assert len(calls) == 2
    assert calls[0].kwargs["pageToken"] is None             # first page: no token
    assert calls[1].kwargs["pageToken"] == "PAGE2"          # second page: the token


def test_fetch_events_single_page():
    svc = _fake_service()
    svc.events.return_value.list.return_value.execute.return_value = {"items": [{"id": "only"}]}
    with mock.patch("googleapiclient.discovery.build", return_value=svc), \
         mock.patch.object(GoogleCalendarClient, "_creds", return_value="creds"):
        items = GoogleCalendarClient("/tmp/tok.json").fetch_events("cal", "lo", "hi")
    assert [i["id"] for i in items] == ["only"]
    assert svc.events.return_value.list.return_value.execute.call_count == 1


def test_fetch_calendar_colors_follows_pagination():
    svc = _fake_service()
    page1 = {"items": [{"id": "a", "backgroundColor": "#111"}], "nextPageToken": "P2"}
    page2 = {"items": [{"id": "b", "backgroundColor": "#222"},
                       {"id": "c"}]}   # no color -> skipped
    svc.calendarList.return_value.list.return_value.execute.side_effect = [page1, page2]
    with mock.patch("googleapiclient.discovery.build", return_value=svc), \
         mock.patch.object(GoogleCalendarClient, "_creds", return_value="creds"):
        colors = GoogleCalendarClient("/tmp/tok.json").fetch_calendar_colors()
    assert colors == {"a": "#111", "b": "#222"}   # both pages, color-less entry dropped


def test_creds_refreshes_and_rewrites_token_when_expired(tmp_path):
    token = tmp_path / "token.json"
    token.write_text('{"old": true}')
    fake_creds = mock.MagicMock()
    fake_creds.expired = True
    fake_creds.refresh_token = "rt"
    fake_creds.to_json.return_value = '{"refreshed": true}'
    with mock.patch("google.oauth2.credentials.Credentials") as Creds, \
         mock.patch("google.auth.transport.requests.Request"):
        Creds.from_authorized_user_file.return_value = fake_creds
        result = GoogleCalendarClient(str(token))._creds()
    fake_creds.refresh.assert_called_once()
    assert token.read_text() == '{"refreshed": true}'   # token.json rewritten with the fresh token
    assert result is fake_creds


def _expired_creds(new_json):
    fake = mock.MagicMock()
    fake.expired = True
    fake.refresh_token = "rt"
    fake.to_json.return_value = new_json
    return fake


def test_creds_rewrite_is_atomic_and_owner_only(tmp_path):
    """token.json was truncated and rewritten in place: a crash or full disk
    mid-write left a half file, which reads as "not connected" until someone
    re-runs setup. It must be written to a temp file and swapped in whole,
    and a secret stays mode 600."""
    import os
    import stat
    token = tmp_path / "token.json"
    token.write_text('{"old": true}')
    os.chmod(token, 0o644)
    before = os.stat(token).st_ino
    with mock.patch("google.oauth2.credentials.Credentials") as Creds, \
         mock.patch("google.auth.transport.requests.Request"):
        Creds.from_authorized_user_file.return_value = _expired_creds('{"new": 1}')
        GoogleCalendarClient(str(token))._creds()
    assert token.read_text() == '{"new": 1}'
    assert stat.S_IMODE(os.stat(token).st_mode) == 0o600
    assert os.stat(token).st_ino != before, "swapped in, not rewritten in place"
    assert sorted(p.name for p in tmp_path.iterdir()) == ["token.json"], \
        "no temp file left behind"


def test_creds_failed_rewrite_keeps_the_old_token(tmp_path):
    """If the swap fails, the previous (still refreshable) token must be left
    exactly as it was, with no temp file littering the data dir. The refreshed
    creds are still returned for this tick."""
    token = tmp_path / "token.json"
    token.write_text('{"old": true}')
    fake = _expired_creds('{"new": 1}')
    with mock.patch("google.oauth2.credentials.Credentials") as Creds, \
         mock.patch("google.auth.transport.requests.Request"), \
         mock.patch("family_hub.calendar_sync.os.replace",
                    side_effect=OSError("disk full")):
        Creds.from_authorized_user_file.return_value = fake
        result = GoogleCalendarClient(str(token))._creds()   # must not raise
    assert result is fake
    assert token.read_text() == '{"old": true}'
    assert sorted(p.name for p in tmp_path.iterdir()) == ["token.json"]


def test_creds_does_not_rewrite_when_still_valid(tmp_path):
    token = tmp_path / "token.json"
    original = '{"still": "valid"}'
    token.write_text(original)
    fake_creds = mock.MagicMock()
    fake_creds.expired = False       # not expired -> no refresh, no rewrite
    fake_creds.refresh_token = "rt"
    with mock.patch("google.oauth2.credentials.Credentials") as Creds, \
         mock.patch("google.auth.transport.requests.Request"):
        Creds.from_authorized_user_file.return_value = fake_creds
        GoogleCalendarClient(str(token))._creds()
    fake_creds.refresh.assert_not_called()
    assert token.read_text() == original   # file untouched when the token is still good


def _load_google_auth_script(monkeypatch, token_json):
    """scripts/google-auth.py with the OAuth flow stubbed (no browser, no
    network): the stub hands back credentials whose to_json() is `token_json`."""
    import importlib.util
    import sys
    import types
    from pathlib import Path

    creds = mock.MagicMock()
    creds.to_json.return_value = token_json
    flow = mock.MagicMock()
    flow.run_local_server.return_value = creds
    fake = types.ModuleType("google_auth_oauthlib.flow")
    fake.InstalledAppFlow = mock.MagicMock()
    fake.InstalledAppFlow.from_client_secrets_file.return_value = flow
    monkeypatch.setitem(sys.modules, "google_auth_oauthlib",
                        types.ModuleType("google_auth_oauthlib"))
    monkeypatch.setitem(sys.modules, "google_auth_oauthlib.flow", fake)
    path = Path(__file__).resolve().parents[1] / "scripts" / "google-auth.py"
    spec = importlib.util.spec_from_file_location("google_auth_script", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_google_auth_script_writes_token_owner_only(tmp_path, monkeypatch):
    """token.json is a secret. It was written with the default umask (often
    world-readable) and in place; it now goes through the same atomic 0600
    writer the app uses when it refreshes the token."""
    import os
    import stat

    mod = _load_google_auth_script(monkeypatch, '{"token": "t"}')
    (tmp_path / "client_secret.json").write_text("{}")
    tok = tmp_path / "token.json"
    tok.write_text("old")
    os.chmod(tok, 0o644)
    monkeypatch.setattr(mod, "CLIENT_SECRET", str(tmp_path / "client_secret.json"))
    monkeypatch.setattr(mod, "TOKEN", str(tok))
    mod.main()
    assert tok.read_text() == '{"token": "t"}'
    assert stat.S_IMODE(os.stat(tok).st_mode) == 0o600
