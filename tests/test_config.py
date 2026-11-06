import logging

import pytest
import yaml

from transferstage.applog import setup_logging
from transferstage.config import (
    DEFAULT_CONFIG_PATH,
    HARD_T_MAX_C,
    HARD_V_MAX,
    Config,
    ConfigError,
    config_from_dict,
    load_config,
    save_config,
)


def _write(tmp_path, data):
    p = tmp_path / "config.yaml"
    p.write_text(yaml.safe_dump(data), encoding="utf-8")
    return p


def test_default_config_file_loads():
    assert DEFAULT_CONFIG_PATH.exists()
    cfg = load_config()
    assert cfg.devices.z_serial == "27270114"
    assert cfg.devices.rotator_serial == "55524924"
    assert cfg.devices.psu_port == "COM4"
    assert cfg.devices.dmm_visa == "DMM"
    assert cfg.devices.simulate.z and cfg.devices.simulate.psu
    assert cfg.limits.t_max_c <= HARD_T_MAX_C
    assert cfg.limits.v_max <= HARD_V_MAX
    assert cfg.pid.output_high <= cfg.limits.v_max


def test_dataclass_defaults_are_valid():
    cfg = Config()
    assert cfg.limits.t_max_c == 300
    assert cfg.limits.v_max == 24
    assert all(cfg.devices.simulate.__dict__.values())


def test_rejects_t_max_400(tmp_path):
    with pytest.raises(ConfigError, match="t_max_c"):
        load_config(_write(tmp_path, {"limits": {"t_max_c": 400}}))


def test_rejects_v_max_30(tmp_path):
    with pytest.raises(ConfigError, match="v_max"):
        load_config(_write(tmp_path, {"limits": {"v_max": 30}}))


def test_rejects_output_high_above_v_max(tmp_path):
    data = {"limits": {"v_max": 12}, "pid": {"output_high": 20}}
    with pytest.raises(ConfigError, match="output_high"):
        load_config(_write(tmp_path, data))


def test_rejects_output_high_above_hard_limit():
    with pytest.raises(ConfigError):
        config_from_dict({"pid": {"output_high": 30}})


def test_rejects_unknown_key():
    with pytest.raises(ConfigError, match="unknown"):
        config_from_dict({"limits": {"t_maxx": 100}})


def test_partial_config_merges_defaults():
    cfg = config_from_dict({"limits": {"t_max_c": 150}, "keys": {"jog_up": "F9"}})
    assert cfg.limits.t_max_c == 150
    assert cfg.limits.v_max == 24
    assert cfg.keys["jog_up"] == "F9"
    assert "safe_lock" in cfg.keys


def test_unquoted_serial_becomes_string():
    cfg = config_from_dict({"devices": {"z_serial": 27270114}})
    assert cfg.devices.z_serial == "27270114"


def test_save_roundtrip(tmp_path):
    cfg = config_from_dict({"limits": {"t_max_c": 200}})
    p = save_config(cfg, tmp_path / "out.yaml")
    assert load_config(p) == cfg


def test_missing_explicit_file_raises(tmp_path):
    with pytest.raises(ConfigError, match="not found"):
        load_config(tmp_path / "nope.yaml")


def test_setup_logging_writes_file(tmp_path):
    log_file = setup_logging(tmp_path / "logs")
    logging.getLogger("transferstage.test").info("hello log")
    for h in logging.getLogger().handlers:
        h.flush()
    assert log_file.name == "app.log"
    assert "hello log" in log_file.read_text(encoding="utf-8")
    # second call must not duplicate handlers
    setup_logging(tmp_path / "logs")
    tagged = [h for h in logging.getLogger().handlers if getattr(h, "_transferstage_handler", False)]
    assert len(tagged) == 2
