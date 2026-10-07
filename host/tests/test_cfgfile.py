import os

import pytest

from conftest import CONFIG_PATH
from ptz.cfgfile import ConfigFiles
from ptz.config import ConfigError


def make(tmp_path):
    return ConfigFiles(CONFIG_PATH, str(tmp_path))


def test_default_until_edited_then_override(tmp_path):
    f = make(tmp_path)
    assert f.active == f.default and not f.has_override
    text = f.read()
    cfg = f.load_active()
    assert cfg.path == f.default and cfg.server.state_dir == str(tmp_path)
    warnings = f.save(text.replace("jog_velocity: 30", "jog_velocity: 25", 1))
    assert warnings == []
    assert f.has_override and f.active == f.override
    assert f.load_active().axes["pan"].jog_velocity == 25
    # the repository file is untouched
    assert "jog_velocity: 30" in open(CONFIG_PATH, encoding="utf-8").read()


def test_invalid_text_is_refused_and_nothing_written(tmp_path):
    f = make(tmp_path)
    cfg, err = f.validate("[axis pan]\nstep_pin: gpio99\n")
    assert cfg is None and err
    with pytest.raises(ConfigError):
        f.save("[axis pan\nbroken")
    assert not f.has_override
    assert not [p for p in os.listdir(tmp_path) if p.endswith(".cfg")]   # no temp left


def test_backups_restore_and_reset(tmp_path):
    f = make(tmp_path)
    original = f.read()
    f.save(original.replace("jog_velocity: 30", "jog_velocity: 20", 1))
    assert len(f.backups()) == 1                       # backup of the default
    assert f.read_backup(f.backups()[0]) == original
    with pytest.raises(ConfigError):
        f.read_backup("../../etc/passwd")
    f.reset()
    assert not f.has_override and f.active == f.default
    assert len(f.backups()) >= 1


def test_broken_edited_file_falls_back_to_default(tmp_path):
    f = make(tmp_path)
    with open(f.override, "w", encoding="utf-8") as fh:
        fh.write("[axis pan]\nstep_pin: nonsense\n")
    cfg = f.load_active()
    assert cfg.path == f.default
    assert "invalid" in f.load_error
