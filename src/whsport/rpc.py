"""机器可读的引擎接口：新行分隔的 JSON-RPC，走 stdin/stdout。

给 GUI（EUI-NEO）等外部前端调用。协议非常简单：

    → {"id": 1, "cmd": "ping"}
    ← {"id": 1, "ok": true, "data": {"pong": true}}

    → {"id": 2, "cmd": "run.preview", "params": {"distance": 2000, ...}}
    ← {"id": 2, "ok": true, "data": {"track": [[lat,lon], ...], "metrics": {...}}}

约定：**stdout 只输出 JSON**，诊断信息一律走 stderr；每次调用独立，进程常驻，
`SportClient` 按凭据缓存复用（避免重复登录）。

命令：
    ping / credentials / login / logout
    policy / score / history / rank
    run.preview   （纯本地生成，不联网、不提交）
    run.submit    （真正提交到服务器）
"""

from __future__ import annotations

import json
import random
import sys
import time
import traceback
from collections.abc import Callable
from typing import Any

from whsport.coordinate_utils import validate_coordinate
from whsport.credentials import (
    load_campus,
    load_credentials,
    resolve,
    save_credentials,
)

# 种子坐标：服务端按账号 unid 选择打卡点（实测中性坐标也能返回本校点位），
# 这个值只是占位；本地配置里若有 campus 则优先。
NEUTRAL_SEED = (39.9042, 116.4074)


def _log(msg: str) -> None:
    print(f"[engine] {msg}", file=sys.stderr, flush=True)


class Engine:
    """持有可选的长生命周期 SportClient。"""

    def __init__(self) -> None:
        self._client = None
        self._client_key: tuple[str, str] | None = None
        self._checkin_cache: tuple[float, list] | None = None
        self._user_cache: dict | None = None

    # -- 凭据 ---------------------------------------------------------------
    def _client_for(self, token: str, uid: str, unid: int):
        from whsport.sport_client import SportClient
        key = (token, str(uid))
        if self._client is None or self._client_key != key:
            self._client = SportClient(uid=uid, token=token, unid=unid)
            self._client_key = key
        return self._client

    def _current_client(self):
        token, uid, unid = resolve()
        if not token or not uid:
            raise RuntimeError("未登录：本地没有可用凭据")
        return self._client_for(token, uid, int(unid or 0))

    # -- 命令 ---------------------------------------------------------------
    def cmd_ping(self, params: dict) -> dict:
        return {"pong": True, "version": 1}

    def cmd_credentials(self, params: dict) -> dict:
        token, uid, unid = resolve()
        data = load_credentials()
        return {"uid": uid, "unid": unid, "has_token": bool(token),
                "username": data.get("username", "")}

    def cmd_login(self, params: dict) -> dict:
        from whsport.sport_client import SportClient
        user = str(params.get("username", "")).strip()
        pwd = str(params.get("password", ""))
        if not user or not pwd:
            raise ValueError("username/password 不能为空")
        client = SportClient()
        resp = client.login(username=user, password=pwd,
                            progress_callback=lambda m: _log(m))
        data = resp.get("data")
        if resp.get("error") != 10000 or not isinstance(data, dict):
            raise RuntimeError(str(resp.get("message") or resp))
        path = save_credentials(token=data.get("token"), uid=data.get("uid"),
                                unid=data.get("unid"), username=user)
        self._client = None
        return {"saved": path, "uid": str(data.get("uid", "")),
                "unid": str(data.get("unid", ""))}

    def cmd_logout(self, params: dict) -> dict:
        self._client = None
        self._client_key = None
        return {"ok": True}

    def cmd_user_info(self, params: dict) -> dict:
        info = self._user_info()
        return {"unid": info.get("unid"), "campus": info.get("campusName"),
                "name": info.get("name"), "weight": info.get("weight"),
                "height": info.get("height")}

    def cmd_policy(self, params: dict) -> dict:
        client = self._current_client()
        mode = int(params.get("mode", 1))
        data = client.get_run_policy(mode).get("data") or {}
        rule = data.get("runRuleModel") or {}
        return {"policy": data.get("policy"), "rule": rule,
                "geoFences": (data.get("geoFence") or {}).get("geoFences") or []}

    def cmd_score(self, params: dict) -> dict:
        client = self._current_client()
        info = client.get_semester_info(1).get("data") or {}
        sid = int(info.get("sid") or 0)
        done = client.get_semester_completed(sid=sid).get("data") or {}
        return {"semester": done.get("sname") or info.get("sname"),
                "total": done.get("semesterDis"),
                "valid": done.get("semesterValidDis"),
                "goal": info.get("semesterValid")}

    def cmd_history(self, params: dict) -> dict:
        client = self._current_client()
        rows = client.get_run_history(
            page_num=int(params.get("page", 1)),
            page_size=int(params.get("size", 10)),
            sid=0, sport_type=int(params.get("sport_type", 1))).get("data") or []
        return {"items": [{"rrid": r.get("rrid"), "distance": r.get("totalDis"),
                           "time": r.get("totalTime"), "start": r.get("startTime")}
                          for r in rows]}

    def cmd_rank(self, params: dict) -> dict:
        client = self._current_client()
        data = client.get_main_rank(
            rank_type=int(params.get("type", 1)),
            sort_type=int(params.get("sort", 1)),
            gender=int(params.get("gender", 0))).get("data")
        return {"data": data}

    # -- 跑步 ---------------------------------------------------------------
    def _checkins(self, lat: float, lon: float) -> list:
        """拉取打卡点（只读），带 2 分钟缓存避免撞服务端限流。"""
        now = time.time()
        if self._checkin_cache and now - self._checkin_cache[0] < 120.0:
            return self._checkin_cache[1]
        client = self._current_client()
        cps = client._checkin_points(lat, lon)  # noqa: SLF001 - 内部只读接口
        usable = [c for c in cps
                  if c.get("glat") is not None and c.get("glon") is not None]
        if usable:
            self._checkin_cache = (now, usable)
        return usable

    def _user_info(self) -> dict:
        """账号资料（unid / 校区名 / 体重 / 身高），进程内缓存。"""
        if self._user_cache is not None:
            return self._user_cache
        try:
            data = self._current_client().get_user_info().get("data") or {}
            self._user_cache = data if isinstance(data, dict) else {}
        except Exception:  # noqa: BLE001 - 拿不到就用兜底值
            self._user_cache = {}
        return self._user_cache

    def _generate(self, params: dict):
        from whsport.running_protocol import (
            FivePoint,
            OutdoorRunRecordBuilder,
            gcj02_to_wgs84,
            generate_synthetic_gps_track,
            order_points_loop,
        )
        sport_type = int(params.get("sport_type", 1))
        info = self._user_info()
        # 体重/身高优先参数，其次账号资料（/api/v70300/user/info），最后兜底
        weight = float(params.get("weight") or info.get("weight") or 62.0)
        height = float(info.get("height") or 172.0)
        jr = random.Random()

        # 距离：真实记录本就不整齐，开启抖动时小幅上浮
        distance = float(params.get("distance") or 2000)
        if params.get("jitter", True):
            distance = float(int(round(distance * jr.uniform(1.005, 1.095))))

        # 用时 / 步数：界面上不再暴露，后台按配速/步频自动推算
        duration = int(params.get("duration") or 0)
        if duration <= 0:
            pace = jr.uniform(6.4, 7.0)                       # 分/公里
            duration = int(round(pace * (distance / 1000.0) * 60))
        steps = int(params.get("steps") or 0)
        if steps <= 0:
            cadence = jr.uniform(78.0, 84.0)                  # 步/分
            steps = int(round(cadence * duration / 60.0))

        # 种子坐标：服务端按账号 unid 选打卡点，这里只是占位
        lat_v, lon_v = params.get("lat"), params.get("lon")
        if lat_v is None or lon_v is None:
            lat_v, lon_v = load_campus() or NEUTRAL_SEED
        lat, lon = validate_coordinate(lat_v, lon_v)

        stop = int(time.time() * 1000)
        start = stop - duration * 1000

        # 计分跑：串打卡点；起点落在第一个打卡点旁边
        control, five, start_hint = None, None, None
        if sport_type == 1:
            usable = self._checkins(lat, lon)
            if usable:
                wgs = [gcj02_to_wgs84(float(c["glat"]), float(c["glon"])) for c in usable]
                control = order_points_loop(wgs)
                start_hint = wgs[0]
                five = [FivePoint(lat=float(c["lat"]), lon=float(c["lon"]),
                                  is_fixed=int(c.get("isFixed") or 0),
                                  point_name=str(c.get("pointName") or ""),
                                  position=i + 1, glat=float(c["glat"]),
                                  glon=float(c["glon"]),
                                  radius=float(c.get("radius") or 15.0),
                                  start_ms=start)
                        for i, c in enumerate(usable)]

        points, default_five = generate_synthetic_gps_track(
            lat, lon, int(round(distance)), duration, start,
            total_steps=steps, height_cm=height,
            control_points=control, start_hint=start_hint)
        if five is None:
            five = default_five
        _, record, _ = OutdoorRunRecordBuilder(
            uid=1, unid=0, sport_type=sport_type,
            sel_distance_m=int(round(distance)), sel_run_time_s=duration,
        ).build_record(int(round(distance)), duration, steps, start, stop,
                       points, five, weight_kg=weight,
                       policy=1 if params.get("policy", True) else 0)
        paces = [1000.0 / (p.speed * 60.0) for p in points if p.speed > 0]
        metrics = {
            "distance": record["totalDis"], "time": record["totalTime"],
            "steps": record["totalSteps"], "avg_step_freq": record["avgStepFreq"],
            "pace_min": min(paces), "pace_max": max(paces),
            "calorie": record["calorie"], "power": record["avgPower"],
            "ascent": record["totalAscent"],
            "start_ms": start, "stop_ms": stop,
        }
        return points, start, duration, metrics, (distance, duration, steps,
                                                  weight, lat, lon, sport_type)

    def cmd_run_preview(self, params: dict) -> dict:
        points, _start, _dur, metrics, _args = self._generate(params)
        paces = [round(1000.0 / (p.speed * 60.0), 3) for p in points if p.speed > 0]
        step = max(1, len(paces) // 64)          # 下采样，前端曲线更顺
        return {"metrics": metrics,
                "pace": paces[::step],
                "track": [[round(p.lat, 6), round(p.lon, 6)] for p in points]}

    def cmd_run_submit(self, params: dict) -> dict:
        _points, _start, _dur, metrics, args = self._generate(params)
        distance, duration, steps, weight, lat, lon, sport_type = args
        client = self._current_client()
        res = client.submit_outdoor_run(
            total_distance_m=distance, total_time_sec=duration, total_steps=steps,
            sport_type=sport_type, start_lat=lat, start_lon=lon, weight_kg=weight,
            policy=1 if params.get("policy", True) else 0)
        return {"result": res, "metrics": metrics}


COMMANDS: dict[str, str] = {
    "ping": "cmd_ping",
    "credentials": "cmd_credentials",
    "login": "cmd_login",
    "logout": "cmd_logout",
    "user.info": "cmd_user_info",
    "policy": "cmd_policy",
    "score": "cmd_score",
    "history": "cmd_history",
    "rank": "cmd_rank",
    "run.preview": "cmd_run_preview",
    "run.submit": "cmd_run_submit",
}


def dispatch(engine: Engine, req: dict) -> dict:
    rid = req.get("id")
    cmd = str(req.get("cmd") or "")
    params = req.get("params") or {}
    method: Callable[[dict], dict] | None = getattr(engine, COMMANDS.get(cmd, ""), None)
    if method is None:
        return {"id": rid, "ok": False, "error": f"未知命令: {cmd}"}
    try:
        return {"id": rid, "ok": True, "data": method(params)}
    except Exception as exc:  # noqa: BLE001 - 边界处统一转成 JSON 错误
        _log(traceback.format_exc(limit=3))
        return {"id": rid, "ok": False, "error": f"{type(exc).__name__}: {exc}"}


def main(argv=None) -> int:
    engine = Engine()
    _log("ready (JSON-RPC on stdin/stdout)")
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except json.JSONDecodeError as exc:
            resp: dict[str, Any] = {"id": None, "ok": False,
                                    "error": f"invalid JSON: {exc}"}
        else:
            resp = dispatch(engine, req)
        print(json.dumps(resp, ensure_ascii=False, separators=(",", ":")), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
