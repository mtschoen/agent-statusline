"""Shared state-save/restore helpers for verify_walker*.py scripts."""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import statusline_lib.walker as walker_module


def fake_expanduser_for(tmp, original):
    """os.path.expanduser replacement routing "~" at `tmp`, everything else
    through `original`. Shared by verify_walker*.py scripts that need a fake
    HOME for _walker_root_list()'s platform-detection paths."""

    def fake_expanduser(path):
        return tmp if path == "~" else original(path)

    return fake_expanduser


def save_walker_state():
    return {
        "shutil_which": walker_module.shutil.which,
        "os_path_isfile": walker_module.os.path.isfile,
        "os_path_isdir": walker_module.os.path.isdir,
        "os_path_realpath": walker_module.os.path.realpath,
        "run_captured": walker_module.run_captured,
        "environ": dict(os.environ),
        "roots_config_path": walker_module._WALKER_ROOTS_CONFIG_PATH,
        "os_path_expanduser": walker_module.os.path.expanduser,
    }


def restore_walker_state(state):
    walker_module.shutil.which = state["shutil_which"]
    walker_module.os.path.isfile = state["os_path_isfile"]
    walker_module.os.path.isdir = state["os_path_isdir"]
    walker_module.os.path.realpath = state["os_path_realpath"]
    walker_module.run_captured = state["run_captured"]
    walker_module.os.path.expanduser = state["os_path_expanduser"]
    for key in list(os.environ.keys()):
        if key not in state["environ"]:
            del os.environ[key]
    for key, value in state["environ"].items():
        os.environ[key] = value
    walker_module._WALKER_ROOTS_CONFIG_PATH = state["roots_config_path"]
