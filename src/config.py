"""Load non-secret settings from config.yaml. Secrets come from the environment."""
import os

import yaml


class ConfigError(Exception):
    pass


def load_config(path="config.yaml"):
    if not os.path.exists(path):
        raise ConfigError(
            f"Config file not found: {path}. Copy config.example.yaml to config.yaml."
        )
    with open(path, "r") as f:
        cfg = yaml.safe_load(f) or {}
    return cfg
