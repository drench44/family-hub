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
