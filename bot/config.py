"""Load config.yaml and merge secrets from environment variables."""
from __future__ import annotations

import os
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent


class ConfigError(RuntimeError):
    pass


def load_config(path: str | os.PathLike | None = None) -> dict:
    cfg_path = Path(path) if path else ROOT / "config.yaml"
    with open(cfg_path, "r", encoding="utf-8") as fh:
        cfg = yaml.safe_load(fh) or {}

    cfg["_root"] = str(ROOT)

    # --- secrets -----------------------------------------------------------
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    cfg["telegram"] = {"token": token, "chat_id": chat_id}

    rpc = cfg.setdefault("sources", {}).setdefault("rpc", {})
    for chain, env in (("ethereum", "ETH_RPC_URL"), ("bsc", "BSC_RPC_URL")):
        urls = list(rpc.get(chain) or [])
        custom = os.environ.get(env, "").strip()
        if custom:
            urls = [custom] + [u for u in urls if u != custom]
        rpc[chain] = urls

    # --- absolute file paths ------------------------------------------------
    files = cfg.setdefault("files", {})
    for key, default in (("journal", "data/journal.csv"),
                         ("history", "data/history.csv"),
                         ("state", "data/state.json")):
        files[key] = str(ROOT / files.get(key, default))
    files.setdefault("history_keep_days", 10)
    return cfg


def require_telegram(cfg: dict) -> None:
    tg = cfg.get("telegram", {})
    if not tg.get("token") or not tg.get("chat_id"):
        raise ConfigError(
            "TELEGRAM_BOT_TOKEN і TELEGRAM_CHAT_ID не задані. "
            "Додайте їх у GitHub → Settings → Secrets and variables → Actions."
        )
