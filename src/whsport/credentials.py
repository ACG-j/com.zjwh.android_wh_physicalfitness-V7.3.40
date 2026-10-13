"""Local credential store for the sport-campus CLIs.

`login_cli.py` persists token / uid / unid here after a successful login, and
`run_cli.py` / `policy_cli.py` fall back to it when ``-t`` / ``-u`` are not
given on the command line.

The file lives outside the repo (``~/.config/zjwh_sport/credentials.json``,
override with ``ZJWH_SPORT_CONFIG_DIR``) and is written with 0600 permissions so
it is never committed accidentally.
"""

import json
import os

CONFIG_DIR = os.environ.get(
    "ZJWH_SPORT_CONFIG_DIR",
    os.path.join(os.path.expanduser("~"), ".config", "zjwh_sport"),
)
CRED_PATH = os.path.join(CONFIG_DIR, "credentials.json")


def load_credentials() -> dict:
    """Return the stored credentials, or an empty dict if none/invalid."""
    try:
        with open(CRED_PATH, encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def save_credentials(token=None, uid=None, unid=None, **extra) -> str:
    """Merge and persist credentials. Returns the file path (or "" if empty)."""
    if not token and not uid:
        return ""
    data = load_credentials()
    if token:
        data["token"] = str(token)
    if uid not in (None, ""):
        data["uid"] = str(uid)
    if unid not in (None, "", 0):
        data["unid"] = str(unid)
    for key, value in extra.items():
        if value not in (None, ""):
            data[key] = value
    os.makedirs(CONFIG_DIR, mode=0o700, exist_ok=True)
    tmp_path = CRED_PATH + ".tmp"
    try:
        with open(tmp_path, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=2)
        os.chmod(tmp_path, 0o600)
        os.replace(tmp_path, CRED_PATH)
    except OSError:
        return ""
    return CRED_PATH


def resolve(token=None, uid=None, unid=None):
    """Merge CLI values with the stored ones (CLI wins)."""
    data = load_credentials()
    token = token or data.get("token") or ""
    uid = uid or data.get("uid") or ""
    if unid in (None, "", 0, "0"):
        unid = data.get("unid") or unid or ""
    return token, uid, unid


def load_campus():
    """Return the stored campus seed (lat, lon), or None.

    用作拉取打卡点的「种子坐标」——不是起点，起点由路线自动生成。
    """
    data = load_credentials()
    lat, lon = data.get("campus_lat"), data.get("campus_lon")
    if lat in (None, "") or lon in (None, ""):
        return None
    try:
        return float(lat), float(lon)
    except (TypeError, ValueError):
        return None


def save_campus(lat, lon) -> str:
    """Persist the campus seed so later runs need no coordinates."""
    if lat in (None, "") or lon in (None, ""):
        return ""
    data = load_credentials()
    data["campus_lat"] = round(float(lat), 6)
    data["campus_lon"] = round(float(lon), 6)
    tmp_path = CRED_PATH + ".tmp"
    try:
        os.makedirs(CONFIG_DIR, mode=0o700, exist_ok=True)
        with open(tmp_path, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=2)
        os.chmod(tmp_path, 0o600)
        os.replace(tmp_path, CRED_PATH)
    except OSError:
        return ""
    return CRED_PATH
