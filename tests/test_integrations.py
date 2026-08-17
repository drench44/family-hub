from types import SimpleNamespace

from family_hub import integrations as fi


def _cfg(**kw):
    base = dict(calendars=[], cameras=[], go2rtc_base="",
                weather_base="", climate_base="")
    base.update(kw)
    return SimpleNamespace(**base)


def test_available_reflects_config_and_env():
    cfg = _cfg(calendars=[{"id": "a", "kind": "google"},
                          {"id": "b", "kind": "ics"}],
               weather_base="http://w", climate_base="", go2rtc_base="http://g")
    avail = {i["id"]: i for i in fi.available_integrations(cfg, {})}
    assert avail["google_calendar"]["available"] is True
    assert avail["ics_calendar"]["available"] is True
    assert avail["weather"]["available"] is True
    assert avail["climate"]["available"] is False      # not configured
    assert avail["cameras"]["available"] is True
    assert avail["icloud_caldav"]["available"] is False  # no creds


def test_caldav_available_only_with_credentials():
    cfg = _cfg()
    assert fi.caldav_configured({}) is False
    env = {"ICLOUD_CALDAV_USER": "bot@icloud.com",
           "ICLOUD_CALDAV_APP_PASSWORD": "x"}
    assert fi.caldav_configured(env) is True
    avail = {i["id"]: i for i in fi.available_integrations(cfg, env)}
    assert avail["icloud_caldav"]["available"] is True


def test_available_only_filters_to_configured():
    cfg = _cfg(weather_base="http://w")   # only weather configured
    # chores/todos are always-on core features, so they're included alongside
    # whatever's actually configured (weather here)
    assert {i["id"] for i in fi.available_only(cfg, {})} == \
        {"weather", "chores", "todos"}


def test_chores_and_todos_are_always_available_features():
    class Cfg:  # nothing configured: no calendars, cameras, weather, climate
        calendars = []
    ids = {i["id"]: i for i in fi.available_integrations(Cfg(), {})}
    # core features are available regardless of config
    assert ids["chores"]["available"] is True
    assert ids["todos"]["available"] is True
    assert ids["chores"]["group"] == "feature"
    assert ids["todos"]["group"] == "feature"
    # external services are tagged as integrations
    assert ids["weather"]["group"] == "integration"
    assert ids["cameras"]["group"] == "integration"
