"""The wall's dim settings (dimming.py): the household dim hours and the
"nobody is home" read from Home Assistant. Pure helpers plus the one async
read, driven through httpx.MockTransport (no network)."""
import asyncio

import httpx
import pytest

from family_hub import dimming


# --- dim hours -------------------------------------------------------------

def test_parse_hm_reads_24_hour_times_and_rejects_the_rest():
    assert dimming.parse_hm("00:00") == 0
    assert dimming.parse_hm("06:30") == 390
    assert dimming.parse_hm("23:59") == 23 * 60 + 59
    for bad in ("24:00", "6:00", "06:60", "", None, 600, "06-00", " 06:00"):
        assert dimming.parse_hm(bad) is None, bad


def test_hours_default_is_ten_at_night_to_six_in_the_morning():
    assert dimming.DIM_DEFAULT == {"on": True, "start": "22:00", "end": "06:00"}
    # nothing stored yet: the wall behaves exactly as before the setting
    assert dimming.clean_hours(None) == dimming.DIM_DEFAULT


def test_clean_hours_keeps_a_good_record_and_repairs_a_bad_one(caplog):
    good = {"on": False, "start": "21:30", "end": "07:00"}
    assert dimming.clean_hours(good) == good
    # a damaged record falls back per field, loudly, never crashes the wall
    with caplog.at_level("WARNING"):
        got = dimming.clean_hours({"on": "yes", "start": "25:00", "end": "07:00"})
    assert got == {"on": True, "start": "22:00", "end": "07:00"}
    assert "dim hours" in caplog.text
    assert dimming.clean_hours(["not", "a", "dict"]) == dimming.DIM_DEFAULT


def test_merge_hours_changes_only_what_was_sent():
    cur = dict(dimming.DIM_DEFAULT)
    # Off keeps the times, so On brings back the same window
    off = dimming.merge_hours(cur, {"on": False})
    assert off == {"on": False, "start": "22:00", "end": "06:00"}
    assert dimming.merge_hours(off, {"on": True}) == dimming.DIM_DEFAULT
    # a window that does not cross midnight is fine
    day = dimming.merge_hours(cur, {"start": "13:00", "end": "15:00"})
    assert day == {"on": True, "start": "13:00", "end": "15:00"}


@pytest.mark.parametrize("patch", [
    {"start": "9pm"}, {"end": "24:00"}, {"on": "true"},
    {"start": "06:00"},                 # same as the end: no window at all
    {},                                 # nothing to change
])
def test_merge_hours_refuses_bad_input_in_plain_words(patch):
    with pytest.raises(ValueError) as e:
        dimming.merge_hours(dict(dimming.DIM_DEFAULT), patch)
    assert str(e.value)
    assert chr(0x2014) not in str(e.value)   # no long dashes


def test_in_window_handles_windows_that_cross_midnight_and_ones_that_dont():
    night = {"on": True, "start": "22:00", "end": "06:00"}
    m = dimming.parse_hm
    assert dimming.in_window(m("22:00"), night)
    assert dimming.in_window(m("23:59"), night)
    assert dimming.in_window(m("00:00"), night)
    assert dimming.in_window(m("05:59"), night)
    assert not dimming.in_window(m("06:00"), night)   # end is exclusive
    assert not dimming.in_window(m("21:59"), night)
    nap = {"on": True, "start": "13:00", "end": "15:30"}
    assert dimming.in_window(m("13:00"), nap)
    assert dimming.in_window(m("15:29"), nap)
    assert not dimming.in_window(m("15:30"), nap)
    assert not dimming.in_window(m("00:30"), nap)
    off = dict(night, on=False)
    assert not dimming.in_window(m("23:00"), off)


# --- who is home -----------------------------------------------------------

def test_zone_count_reads_the_number_of_people_home():
    assert dimming.zone_count({"state": "0"}) == 0
    assert dimming.zone_count({"state": "2"}) == 2
    assert dimming.zone_count({"state": "3.0"}) == 3
    for bad in ({"state": "unknown"}, {"state": "unavailable"}, {"state": "-1"},
                {"state": None}, {"state": 2.5}, {}, None, "2", {"state": "nan"}):
        assert dimming.zone_count(bad) is None, bad


def test_person_home_reads_home_away_and_unknown():
    assert dimming.person_home({"state": "home"}) is True
    assert dimming.person_home({"state": "not_home"}) is False
    assert dimming.person_home({"state": "Work"}) is False   # another zone
    for bad in ({"state": "unknown"}, {"state": "unavailable"}, {"state": ""},
                {"state": 1}, {}, None):
        assert dimming.person_home(bad) is None, bad


def _client(states: dict, status: int = 200):
    """A client whose HA answers each entity from `states` (a dict body, or an
    int status code to fail with)."""
    def handler(req):
        assert req.headers["Authorization"] == "Bearer tok"
        ent = req.url.path.rsplit("/", 1)[-1]
        got = states.get(ent, 404)
        if isinstance(got, int):
            return httpx.Response(got)
        return httpx.Response(status, json=got)
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def _read(states, people=()):
    async def go():
        async with _client(states) as c:
            return await dimming.read_presence(c, "http://ha", "tok", list(people))
    return asyncio.run(go())


def test_read_presence_uses_zone_home_by_default():
    assert _read({"zone.home": {"state": "0"}}) == {"ok": True, "home": 0}
    assert _read({"zone.home": {"state": "2"}}) == {"ok": True, "home": 2}


def test_read_presence_fails_soft_when_ha_cannot_be_read():
    got = _read({"zone.home": {"state": "unavailable"}})
    assert got["ok"] is False and got["error"]
    got = _read({})                       # 404: the entity is gone
    assert got["ok"] is False and "zone.home" in got["error"]
    got = _read({"zone.home": 401})
    assert got["ok"] is False and got["auth"] is True


def test_read_presence_with_a_people_list_counts_who_is_home():
    people = ["person.a", "person.b"]
    assert _read({"person.a": {"state": "not_home"},
                  "person.b": {"state": "home"}}, people) == {"ok": True, "home": 1}
    assert _read({"person.a": {"state": "not_home"},
                  "person.b": {"state": "Work"}}, people) == {"ok": True, "home": 0}
    # one person home is enough to know someone is home, even if the other
    # can't be read
    assert _read({"person.a": {"state": "home"}}, people) == {"ok": True, "home": 1}
    # but nobody-home needs every person read: an unknown one could be home
    got = _read({"person.a": {"state": "not_home"},
                 "person.b": {"state": "unknown"}}, people)
    assert got["ok"] is False and "person.b" in got["error"]


def test_read_presence_survives_a_transport_error():
    def handler(req):
        raise httpx.ConnectError("no route")

    async def go():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
            return await dimming.read_presence(c, "http://ha", "tok", [])
    got = asyncio.run(go())
    assert got["ok"] is False and got["auth"] is False


# --- the away block the wall reads ------------------------------------------

def test_away_block_dims_only_on_a_fresh_read_of_nobody_home():
    now = 1000.0
    fresh = now - 10
    blk = dimming.away_block
    assert blk(enabled=True, token=True, snap={"ok": True, "home": 0},
               at=fresh, now=now) == {"enabled": True, "state": "away",
                                      "home": 0, "dim": True}
    assert blk(enabled=True, token=True, snap={"ok": True, "home": 2},
               at=fresh, now=now)["dim"] is False
    assert blk(enabled=True, token=True, snap={"ok": True, "home": 2},
               at=fresh, now=now)["state"] == "home"
    # HA unreadable: never dim for this reason
    un = blk(enabled=True, token=True, snap={"ok": False, "error": "x"},
             at=fresh, now=now)
    assert un["state"] == "unreadable" and un["dim"] is False
    # a stale answer (the watcher stopped) is the same as unreadable
    old = blk(enabled=True, token=True, snap={"ok": True, "home": 0},
              at=now - dimming.FRESH_S - 1, now=now)
    assert old["state"] == "unreadable" and old["dim"] is False
    # no answer yet
    assert blk(enabled=True, token=True, snap=None, at=None, now=now)["state"] == "checking"
    # no token
    nt = blk(enabled=True, token=False, snap={"ok": True, "home": 0}, at=fresh, now=now)
    assert nt["state"] == "needs_auth" and nt["dim"] is False
    # switched off: never dims, whatever HA says
    off = blk(enabled=False, token=True, snap={"ok": True, "home": 0}, at=fresh, now=now)
    assert off == {"enabled": False, "state": "off", "home": None, "dim": False}


def test_demo_presence_is_a_canned_someone_home_answer():
    d = dimming.demo_away(enabled=True)
    assert d == {"enabled": True, "state": "home", "home": 2, "dim": False}
    assert dimming.demo_away(enabled=False)["state"] == "off"
