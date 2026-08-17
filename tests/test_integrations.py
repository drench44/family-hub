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
    assert ids["chores"]["kind"] == "chores"
    assert ids["todos"]["kind"] == "todos"
    # external services are tagged as integrations
    assert ids["weather"]["group"] == "integration"
    assert ids["cameras"]["group"] == "integration"


def test_laundry_available_only_with_config_and_token():
    # not configured at all
    assert fi.laundry_configured(_cfg(), {}) is False
    # configured but no HA token in the env -> inert, not available
    laundry = {"ha_base": "http://ha:8123", "machines": [
        {"id": "washer", "label": "Washer", "kind": "washer",
         "status_entity": "sensor.w_status", "remaining_entity": "sensor.w_rem"}]}
    cfg = _cfg(laundry=laundry)
    assert fi.laundry_configured(cfg, {}) is False
    # configured + token -> available, tagged an integration (not a feature)
    env = {"HA_TOKEN": "secret"}
    assert fi.laundry_configured(cfg, env) is True
    avail = {i["id"]: i for i in fi.available_integrations(cfg, env)}
    assert avail["laundry"]["available"] is True
    assert avail["laundry"]["group"] == "integration"
    assert avail["laundry"]["kind"] == "laundry"
    # token alone (no config block) is not enough
    assert fi.laundry_configured(_cfg(), env) is False


def test_laundry_config_cleaning():
    # _clean_laundry drops malformed machines and rejects empty blocks, so a
    # config typo can't crash the app or leave a half-configured integration.
    from family_hub.config import _clean_laundry
    ok = {"ha_base": "http://ha:8123/", "machines": [
        {"id": "washer", "status_entity": "s.a", "remaining_entity": "s.b"},
        {"id": "", "status_entity": "s.c", "remaining_entity": "s.d"},   # no id
        {"id": "x", "status_entity": "", "remaining_entity": "s.e"},     # no status
        "not-a-dict",
        {"id": "dryer", "label": "Dryer", "kind": "dryer",
         "status_entity": "s.f", "remaining_entity": "s.g"}]}
    cleaned = _clean_laundry(ok)
    assert cleaned["ha_base"] == "http://ha:8123"          # trailing / stripped
    assert [m["id"] for m in cleaned["machines"]] == ["washer", "dryer"]
    assert cleaned["machines"][0]["label"] == "washer"     # label defaults to id
    assert cleaned["machines"][0]["kind"] == "washer"      # kind defaults
    assert cleaned["machines"][1]["kind"] == "dryer"
    # rejected shapes -> None (no laundry integration)
    assert _clean_laundry(None) is None
    assert _clean_laundry("nope") is None
    assert _clean_laundry({"ha_base": ""}) is None
    assert _clean_laundry({"ha_base": "http://ha", "machines": []}) is None
    assert _clean_laundry({"ha_base": "http://ha",
                           "machines": [{"id": "w"}]}) is None
