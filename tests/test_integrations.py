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
    # chores/todos and the chore wallet are always-on core features, so
    # they're included alongside whatever's actually configured (weather here),
    # and so is the hub's own backup switch
    assert {i["id"] for i in fi.available_only(cfg, {})} == \
        {"weather", "chores", "rewards", "todos", "backups"}


def test_backups_in_registry_as_feature():
    """Always listed, so its switch always shows; the job itself only runs
    once a backup folder is mounted (app._backup_why_off)."""
    row = next(i for i in fi.available_integrations(_cfg(), {}) if i["id"] == "backups")
    assert row == {"id": "backups", "kind": "backups", "name": "Backups",
                   "available": True, "group": "feature"}


def test_rewards_in_registry_as_feature():
    rewards = next(i for i in fi.available_integrations(_cfg(), {})
                   if i["id"] == "rewards")
    assert rewards == {"id": "rewards", "kind": "rewards", "name": "Chore wallet",
                       "available": True, "group": "feature"}


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


def test_laundry_stays_listed_without_a_token_and_says_needs_auth():
    # 2026-09-17: a lost .env emptied HA_TOKEN and laundry dropped OUT of the
    # registry, so the wall blanked the card and settings showed a hub with no
    # laundry at all. Configured is configured; the missing token is a HEALTH
    # state (needs_auth), not an absence.
    assert fi.laundry_configured(_cfg(), {}) is False      # nothing configured
    assert fi.laundry_needs_auth(_cfg(), {}) is False      # ...so nothing broken
    laundry = {"ha_base": "http://ha:8123", "machines": [
        {"id": "washer", "label": "Washer", "kind": "washer",
         "status_entity": "sensor.w_status", "remaining_entity": "sensor.w_rem"}]}
    cfg = _cfg(laundry=laundry)
    assert fi.laundry_configured(cfg, {}) is True
    assert fi.laundry_needs_auth(cfg, {}) is True
    assert {i["id"] for i in fi.available_only(cfg, {})} >= {"laundry"}, \
        "a tokenless laundry must still reach the settings menu"
    # configured + token -> healthy, tagged an integration (not a feature)
    env = {"HA_TOKEN": "secret"}
    assert fi.laundry_needs_auth(cfg, env) is False
    avail = {i["id"]: i for i in fi.available_integrations(cfg, env)}
    assert avail["laundry"]["available"] is True
    assert avail["laundry"]["group"] == "integration"
    assert avail["laundry"]["kind"] == "laundry"
    # token alone (no config block) is not enough
    assert fi.laundry_configured(_cfg(), env) is False


def test_a_whitespace_token_counts_as_no_token_everywhere():
    # "   " used to be truthy in the registry and falsy in the startup guard,
    # so the hub could report "HA_TOKEN is empty" about a card it was showing.
    laundry = {"ha_base": "http://ha:8123", "machines": [
        {"id": "washer", "label": "Washer", "kind": "washer",
         "status_entity": "sensor.w_status", "remaining_entity": "sensor.w_rem"}]}
    cfg = _cfg(laundry=laundry)
    assert fi.ha_token({"HA_TOKEN": "  \t "}) == ""
    assert fi.ha_token({"HA_TOKEN": " tok "}) == "tok"
    assert fi.laundry_needs_auth(cfg, {"HA_TOKEN": "   "}) is True


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
    cleaned, err = _clean_laundry(ok)
    assert err is None
    assert cleaned["ha_base"] == "http://ha:8123"          # trailing / stripped
    assert [m["id"] for m in cleaned["machines"]] == ["washer", "dryer"]
    assert cleaned["machines"][0]["label"] == "washer"     # label defaults to id
    assert cleaned["machines"][0]["kind"] == "washer"      # kind defaults
    assert cleaned["machines"][1]["kind"] == "dryer"
    # No laundry block at all is the only SILENT None: that hub simply has no
    # laundry. Every other rejection carries a reason, because someone asked
    # for laundry and did not get it, and an integration that merely fails to
    # appear is the hardest kind of broken to notice.
    assert _clean_laundry(None) == (None, None)
    for raw in ("nope", {"ha_base": ""}, {"ha_base": "http://ha", "machines": []},
                {"ha_base": "http://ha", "machines": [{"id": "w"}]}):
        cleaned, err = _clean_laundry(raw)
        assert cleaned is None and err, raw


def test_fleet_available_reflects_config():
    # present iff the `fleet` config block is set — no env/credential gate,
    # unlike laundry/caldav (availability is config presence alone).
    cfg = _cfg(fleet=None)
    avail = {i["id"]: i for i in fi.available_integrations(cfg, {})}
    assert avail["fleet"]["available"] is False
    assert avail["fleet"]["group"] == "integration"
    assert avail["fleet"]["kind"] == "fleet"

    cfg = _cfg(fleet={"base": "http://fleet"})
    avail = {i["id"]: i for i in fi.available_integrations(cfg, {})}
    assert avail["fleet"]["available"] is True


def test_printer_card_rides_the_fleet_config_with_its_own_switch():
    # the printer card reads the same rollup, so it exists exactly when fleet
    # does, but as its own row so either card can be switched off alone
    avail = {i["id"]: i for i in fi.available_integrations(_cfg(fleet=None), {})}
    assert avail["printer"]["available"] is False
    avail = {i["id"]: i for i in
             fi.available_integrations(_cfg(fleet={"base": "http://fleet"}), {})}
    assert avail["printer"]["available"] is True
    assert avail["printer"]["kind"] == "printer"
    assert avail["printer"]["group"] == "integration"


def test_fleet_config_cleaning():
    from family_hub.config import _clean_fleet
    assert _clean_fleet(None) is None
    assert _clean_fleet("nope") is None
    assert _clean_fleet({}) is None
    assert _clean_fleet({"base": ""}) is None
    cleaned = _clean_fleet({"base": "http://fleet:3000/", "label": "Fleet"})
    assert cleaned == {"base": "http://fleet:3000", "label": "Fleet"}   # trailing / stripped
    # label is optional
    assert _clean_fleet({"base": "http://fleet:3000"}) == {"base": "http://fleet:3000"}


def test_fleet_config_cleaning_warns_loudly(caplog):
    # A malformed fleet block must not silently vanish the integration — the
    # operator's only debugging path otherwise is re-reading their JSON
    # character by character while the tile silently never appears.
    import logging
    from family_hub.config import _clean_fleet
    with caplog.at_level(logging.WARNING, logger="family_hub.config"):
        _clean_fleet({"label": "Fleet"})   # missing base
    msgs = " ".join(r.message for r in caplog.records)
    assert "dropping malformed config block" in msgs


def test_laundry_config_cleaning_warns_loudly(caplog):
    # Dropped machines and a dropped-whole-integration must say so in the log
    # (the operator's only debugging path otherwise is re-reading their JSON
    # character by character while the card silently never appears).
    import logging
    from family_hub.config import _clean_laundry
    with caplog.at_level(logging.DEBUG, logger="family_hub.config"):
        _clean_laundry({"ha_base": "http://ha", "machines": [
            {"id": "w", "status_entity": "s.a"},        # missing remaining_entity
            "not-a-dict"]})
    msgs = " ".join(r.message for r in caplog.records)
    assert "dropping machine entry" in msgs
    assert "dropping non-dict machine entry" in msgs
    assert "no valid machines survived" in msgs
    # ...and losing the WHOLE integration is an error, not a warning buried
    # among the per-entry ones.
    assert any(r.levelno >= logging.ERROR and "no valid machines survived" in r.message
               for r in caplog.records)


# --- away dimming: "dim the wall when nobody is home" ----------------------

def test_away_dim_is_listed_only_where_home_assistant_is_configured():
    """It has no Home Assistant of its own: it rides the laundry block's
    ha_base, or a `presence` block's. A hub with neither never shows it."""
    ids = lambda cfg: {i["id"] for i in fi.available_only(cfg, {"HA_TOKEN": "t"})}
    assert "away_dim" not in ids(_cfg())
    assert "away_dim" in ids(_cfg(laundry={"ha_base": "http://ha", "machines": []}))
    assert "away_dim" in ids(_cfg(presence={"ha_base": "http://ha", "people": []}))


def test_away_dim_is_off_until_someone_turns_it_on():
    row = next(i for i in fi.available_integrations(
        _cfg(presence={"ha_base": "http://ha", "people": []}), {})
        if i["id"] == "away_dim")
    assert row == {"id": "away_dim", "kind": "presence",
                   "name": "Dim when nobody is home", "available": True,
                   "group": "integration", "default_on": False}


def test_presence_base_prefers_the_presence_block():
    assert fi.presence_base(_cfg()) == ""
    assert fi.presence_base(_cfg(laundry={"ha_base": "http://l"})) == "http://l"
    assert fi.presence_base(_cfg(laundry={"ha_base": "http://l"},
                                 presence={"ha_base": "http://p", "people": []})) == "http://p"
    # a presence block that only lists people uses the laundry HA
    assert fi.presence_base(_cfg(laundry={"ha_base": "http://l"},
                                 presence={"ha_base": "", "people": ["person.a"]})) == "http://l"


def test_presence_config_cleaning(caplog):
    from family_hub.config import _clean_presence
    assert _clean_presence(None) is None
    assert _clean_presence({"ha_base": "http://ha/", "people": ["person.a", "person.b"]}) == \
        {"ha_base": "http://ha", "people": ["person.a", "person.b"]}
    assert _clean_presence({}) == {"ha_base": "", "people": []}
    with caplog.at_level("WARNING"):
        got = _clean_presence({"people": ["person.a", "sensor.x", 3, " person.b "]})
    assert got == {"ha_base": "", "people": ["person.a", "person.b"]}
    assert "sensor.x" in caplog.text
    with caplog.at_level("WARNING"):
        assert _clean_presence(["person.a"]) is None
    assert "presence" in caplog.text


def test_load_config_reads_the_presence_block(tmp_path):
    import json
    from family_hub.config import load_config
    p = tmp_path / "config.json"
    p.write_text(json.dumps({"presence": {"people": ["person.a"]}}))
    assert load_config(str(p)).presence == {"ha_base": "", "people": ["person.a"]}
    p.write_text(json.dumps({}))
    assert load_config(str(p)).presence is None
