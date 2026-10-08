import base64
import collections
import gzip
import hashlib
import json
import math
import random
import time
import uuid
from typing import Dict, List, Optional, Tuple, Union
from coordinate_utils import Coordinate, coordinates_match, validate_coordinate

SPORT_STATIC_SALT = "2slhe02lsfiwowlcixisla_sls-_slaor"


# --- Physiological / device estimates -------------------------------------
#
# These keep the aggregate fields the app renders (消耗/爬升高度/平均功率/配速/步幅)
# mutually consistent with totalDis / totalTime / totalSteps, instead of being
# hard-coded constants that never change between runs.

# Fallback body weight used when the caller does not supply one.  The app asks
# users to "完善体重信息" precisely because calorie depends on it.
DEFAULT_BODY_WEIGHT_KG = 60.0

# Official calorie coefficient: kcal = 1.036 * kg * km.
KCAL_PER_KG_KM = 1.036

# Horizontal running power coefficient (W per kg per m/s).
RUNNING_POWER_COEFF = 0.98

# Elevation steps smaller than this are treated as GPS/barometer noise.
ELE_NOISE_M = 0.15

DEFAULT_HEIGHT_CM = 172.0


def estimate_calorie(total_distance_m: float, total_time_sec: int,
                     weight_kg: float = DEFAULT_BODY_WEIGHT_KG,
                     total_ascent_m: int = 0) -> int:
    """Energy expenditure in 千卡 (kcal).

    Official app formula: kcal = 1.036 * weight(kg) * distance(km).
    """
    if total_distance_m <= 0:
        return 0
    return int(round(max(0.0, weight_kg) * (total_distance_m / 1000.0) * KCAL_PER_KG_KM))


def estimate_avg_power(total_distance_m: float, total_time_sec: int,
                       total_ascent_m: int = 0,
                       weight_kg: float = DEFAULT_BODY_WEIGHT_KG) -> int:
    """Average running power in 瓦 (W).

    Official app formula: W = 0.98 * weight(kg) * speed(m/s).
    """
    if total_distance_m <= 0 or total_time_sec <= 0:
        return 0
    return int(round(max(0.0, weight_kg) * (total_distance_m / total_time_sec)
                     * RUNNING_POWER_COEFF))


def estimate_stride_cm(total_distance_m: float, total_steps: int) -> float:
    """Average stride length in 厘米 (cm) = distance / steps."""
    if total_steps <= 0:
        return 0.0
    return round(total_distance_m / total_steps * 100.0, 1)


def elevation_gain(elevations) -> float:
    """Cumulative climb, ignoring sub-ELE_NOISE_M jitter (official口径)."""
    gain = 0.0
    prev = None
    for e in elevations:
        e = float(e or 0.0)
        if prev is not None and e - prev > ELE_NOISE_M:
            gain += e - prev
        prev = e
    return round(gain, 2)


def estimate_total_ascent(total_distance_m: float,
                          rng: Optional[random.Random] = None) -> int:
    """Fallback climb estimate when no elevation profile is available."""
    if total_distance_m <= 0:
        return 0
    rng = rng or random
    return int(round((total_distance_m / 1000.0) * rng.uniform(3.0, 9.0)))


# --- WGS-84 → GCJ-02 (Mars coordinates) ------------------------------------

_GCJ_A = 6378245.0
_GCJ_EE = 0.00669342162296594323


def _out_of_china(lat: float, lon: float) -> bool:
    return not (73.66 < lon < 135.05 and 3.86 < lat < 53.55)


def _gcj_transform_lat(x: float, y: float) -> float:
    ret = (-100.0 + 2.0 * x + 3.0 * y + 0.2 * y * y + 0.1 * x * y
           + 0.2 * math.sqrt(abs(x)))
    ret += (20.0 * math.sin(6.0 * x * math.pi) + 20.0 * math.sin(2.0 * x * math.pi)) * 2.0 / 3.0
    ret += (20.0 * math.sin(y * math.pi) + 40.0 * math.sin(y / 3.0 * math.pi)) * 2.0 / 3.0
    ret += (160.0 * math.sin(y / 12.0 * math.pi) + 320.0 * math.sin(y * math.pi / 30.0)) * 2.0 / 3.0
    return ret


def _gcj_transform_lon(x: float, y: float) -> float:
    ret = (300.0 + x + 2.0 * y + 0.1 * x * x + 0.1 * x * y + 0.1 * math.sqrt(abs(x)))
    ret += (20.0 * math.sin(6.0 * x * math.pi) + 20.0 * math.sin(2.0 * x * math.pi)) * 2.0 / 3.0
    ret += (20.0 * math.sin(x * math.pi) + 40.0 * math.sin(x / 3.0 * math.pi)) * 2.0 / 3.0
    ret += (150.0 * math.sin(x / 12.0 * math.pi) + 300.0 * math.sin(x / 30.0 * math.pi)) * 2.0 / 3.0
    return ret


def wgs84_to_gcj02(lat: float, lon: float) -> Tuple[float, float]:
    """Convert WGS-84 to GCJ-02, as the app stores/renders on the map."""
    if _out_of_china(lat, lon):
        return lat, lon
    dlat = _gcj_transform_lat(lon - 105.0, lat - 35.0)
    dlon = _gcj_transform_lon(lon - 105.0, lat - 35.0)
    rad_lat = lat / 180.0 * math.pi
    magic = math.sin(rad_lat)
    magic = 1 - _GCJ_EE * magic * magic
    sqrt_magic = math.sqrt(magic)
    dlat = (dlat * 180.0) / ((_GCJ_A * (1 - _GCJ_EE)) / (magic * sqrt_magic) * math.pi)
    dlon = (dlon * 180.0) / (_GCJ_A / sqrt_magic * math.cos(rad_lat) * math.pi)
    return round(lat + dlat, 7), round(lon + dlon, 7)


def legacy_validate_coordinate(latitude: float, longitude: float) -> Coordinate:
    """Validate and normalize the single coordinate source used by a run."""
    return validate_coordinate(latitude, longitude)


def compute_sport_signature(field_map: Dict[str, Union[str, int, float, bool, None]],
                            salt: str = SPORT_STATIC_SALT) -> str:
    valid_items = []
    for k, v in field_map.items():
        if v is None:
            continue
        if k.lower() == "signature":
            continue
        valid_items.append((k, str(v).lower() if isinstance(v, bool) else str(v)))
    
    valid_items.sort(key=lambda x: (x[0].lower(), x[0]))
    canonical_str = "&".join(f"{k}={v}" for k, v in valid_items)
    to_hash = (canonical_str + salt).encode("utf-8")
    return hashlib.md5(to_hash).hexdigest().lower()


def compute_original_sign(field_map: Dict[str, Union[str, int, float, bool, None]]) -> str:
    valid_items = []
    for k, v in field_map.items():
        if v is None:
            continue
        if k.lower() == "signature":
            continue
        valid_items.append((k, str(v).lower() if isinstance(v, bool) else str(v)))
    
    valid_items.sort(key=lambda x: x[0])
    return "&".join(f"{k}={v}" for k, v in valid_items)


def gzip_base64_encode(data: Union[str, bytes]) -> str:
    if isinstance(data, str):
        data_bytes = data.encode("utf-8")
    else:
        data_bytes = data
    compressed = gzip.compress(data_bytes)
    return base64.b64encode(compressed).decode("ascii")


def gzip_base64_decode(b64_str: str) -> str:
    compressed = base64.b64decode(b64_str)
    decompressed = gzip.decompress(compressed)
    return decompressed.decode("utf-8")


class StepBucket:
    def __init__(self, steps_num: int, begin_time: int, end_time: int,
                 flag: int = 0, max_diff: float = 0.0, min_diff: float = 0.0,
                 avg_diff: float = 0.0, state: int = 0, queue_num: int = 1,
                 bucket_id: int = 0):
        self.stepsNum = steps_num
        self.beginTime = begin_time
        self.endTime = end_time
        self.flag = flag
        self.maxDiff = max_diff
        self.minDiff = min_diff
        self.avgDiff = avg_diff
        self.state = state
        self.queueNum = queue_num
        self.id = bucket_id

    def to_dict(self) -> dict:
        # Key set matches the real device payload exactly (avgDiff=0, maxDiff=0,
        # minDiff=1000, state=0, flag=start_ms).
        return {
            "stepsNum": self.stepsNum,
            "beginTime": self.beginTime,
            "endTime": self.endTime,
            "flag": self.flag,
            "id": self.id,
            "maxDiff": self.maxDiff,
            "minDiff": self.minDiff,
            "avgDiff": self.avgDiff,
            "state": self.state,
            "queueNum": self.queueNum
        }


class SpeedBucket:
    def __init__(self, distance: float, begin_time: int, end_time: int,
                 flag: int = 0, state: int = 0, queue_num: int = 1,
                 bucket_id: int = 0):
        self.distance = distance
        self.beginTime = begin_time
        self.endTime = end_time
        self.flag = flag
        self.state = state
        self.queueNum = queue_num
        self.id = bucket_id

    def to_dict(self) -> dict:
        return {
            "distance": self.distance,
            "beginTime": self.beginTime,
            "endTime": self.endTime,
            "flag": self.flag,
            "id": self.id,
            "queueNum": self.queueNum,
            "state": self.state
        }


class GpsPoint:
    def __init__(self, lat: float, lon: float, time_ms: int,
                 speed: float = 0.0, accuracy: float = 5.0, is_valid: bool = True,
                 ele: float = 0.0, cadence: float = 0.0, stride_cm: float = 0.0,
                 cum_dist: float = 0.0, cum_steps: float = 0.0, index: int = 0):
        self.lat = lat
        self.lon = lon
        self.time = time_ms
        self.speed = speed          # m/s
        self.accuracy = accuracy
        self.isValid = is_valid
        self.ele = ele              # metres
        self.cadence = cadence      # steps / minute
        self.stride_cm = stride_cm
        self.cum_dist = cum_dist    # metres travelled so far
        self.cum_steps = cum_steps  # cumulative steps (float)
        self.index = index

    def to_dict(self) -> dict:
        # Minimal shape kept for the record body / local validation.
        return {
            "lat": self.lat,
            "lon": self.lon,
            "time": self.time,
            "speed": self.speed,
            "accuracy": self.accuracy,
            "isValid": self.isValid
        }

    def pace_min_per_km(self) -> float:
        return (1000.0 / (self.speed * 60.0)) if self.speed > 0 else 0.0

    def to_obs_dict(self, start_ms: int, pt_type: int = 1) -> dict:
        """Real-device OBS point (27 keys, GCJ-02 coordinates)."""
        glat, glng = wgs84_to_gcj02(self.lat, self.lon)
        pace = round(self.pace_min_per_km(), 4)
        t_rel = int(round((self.time - start_ms) / 1000.0))
        return {
            "avgSpeed": pace,
            "bdA": round(self.ele, 2),
            "bdD": 0.0,
            "bdG": 0,
            "bdS": 0.0,
            "coorType": "gcj02",
            "count": 1,
            "dtr": 0.0,
            "flag": start_ms,
            "gLat": glat,
            "gLng": glng,
            "gainTime": 0,
            "id": int(self.index),
            "lat": -1.0,
            "lng": -1.0,
            "locType": 1,
            "locationId": "",
            "queueNum": 0,
            "radius": 0.0,
            "speed": pace,
            "state": 0,
            "stepDistance": 0.0,
            "totalDis": round(self.cum_dist, 4),
            "totalTime": t_rel,
            "type": int(pt_type),
            "validDis": round(self.cum_dist, 4),
            "validTime": t_rel,
        }


class FivePoint:
    def __init__(self, lat: float, lon: float, is_fixed: int = 1,
                 is_pass: bool = True, point_name: str = "", position: int = 0):
        self.lat = lat
        self.lon = lon
        self.isFixed = is_fixed
        self.isPass = is_pass
        self.pointName = point_name
        self.position = position

    def to_dict(self) -> dict:
        return {
            "lat": self.lat,
            "lon": self.lon,
            "isFixed": self.isFixed,
            "isPass": self.isPass,
            "pointName": self.pointName,
            "position": self.position
        }


def _largest_remainder(total: int, weights: List[float]) -> List[int]:
    """Split an integer total across buckets so the parts always sum to total."""
    if not weights:
        return []
    s = sum(weights)
    if s <= 0:
        weights = [1.0] * len(weights)
        s = float(len(weights))
    exact = [total * w / s for w in weights]
    parts = [int(math.floor(x)) for x in exact]
    leftover = total - sum(parts)
    order = sorted(range(len(exact)), key=lambda i: exact[i] - parts[i], reverse=True)
    for i in order[:leftover]:
        parts[i] += 1
    return parts


def _weighted_split(total: float, weights: List[float]) -> List[float]:
    """Split a float total across buckets proportional to weights."""
    if not weights:
        return []
    s = sum(weights)
    if s <= 0:
        weights = [1.0] * len(weights)
        s = float(len(weights))
    return [total * w / s for w in weights]


def _effort_weights(num_buckets: int, rng: random.Random) -> List[float]:
    """Per-10s effort profile: gentle warm-up, steady middle, small kick.

    Bucket values are proportional to these weights, so the cadence and speed
    series rise and fall together instead of being flat lines, while the caller
    keeps their totals exact via _largest_remainder / _weighted_split.
    """
    if num_buckets <= 1:
        return [1.0]
    weights = []
    for i in range(num_buckets):
        t = i / (num_buckets - 1)
        envelope = 0.90 + 0.18 * math.sin(math.pi * t)
        weights.append(envelope * rng.uniform(0.92, 1.08))
    return weights


def _haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    r = 6371000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = p2 - p1
    dl = math.radians(lon2 - lon1)
    a = (math.sin(dphi / 2.0) ** 2
         + math.cos(p1) * math.cos(p2) * math.sin(dl / 2.0) ** 2)
    return 2.0 * r * math.asin(min(1.0, math.sqrt(a)))


def _point_at_distance(path, cum, d):
    """Interpolate position along a closed polyline at cumulative distance d."""
    total = cum[-1]
    if total <= 0:
        return path[0]
    d = d % total
    lo, hi = 0, len(cum) - 1
    while lo + 1 < hi:
        mid = (lo + hi) // 2
        if cum[mid] <= d:
            lo = mid
        else:
            hi = mid
    span = cum[hi] - cum[lo]
    f = 0.0 if span <= 1e-12 else (d - cum[lo]) / span
    la1, lo1 = path[lo]
    la2, lo2 = path[hi]
    return la1 + (la2 - la1) * f, lo1 + (lo2 - lo1) * f


def _build_route(start_lat, start_lon, target_len_m, rng, n_dense=900):
    """Organic closed loop (Fourier-modulated radius) scaled to target length.

    Avoids the "perfect circle" fingerprint: harmonics 2-5 with random phases
    produce a plausible campus ring while keeping the start anchored.
    """
    harmonics = [(k, rng.uniform(0.03, 0.12), rng.uniform(0.0, 2.0 * math.pi))
                 for k in range(2, 6)]
    xy = []
    for i in range(n_dense):
        th = 2.0 * math.pi * i / n_dense
        r = 1.0 + sum(a * math.cos(k * th + ph) for k, a, ph in harmonics)
        xy.append((r * math.cos(th), r * math.sin(th)))

    def seg(a, b):
        return math.hypot(a[0] - b[0], a[1] - b[1])

    length = (sum(seg(xy[i - 1], xy[i]) for i in range(1, len(xy)))
              + seg(xy[-1], xy[0]))
    scale = target_len_m / max(length, 1e-9)
    x0, y0 = xy[0]
    m_lat = 111320.0
    m_lon = 111320.0 * math.cos(math.radians(start_lat))
    path = [(start_lat + (y - y0) * scale / m_lat,
             start_lon + (x - x0) * scale / m_lon) for x, y in xy]
    cum = [0.0]
    for i in range(1, len(path)):
        cum.append(cum[-1] + _haversine_m(path[i - 1][0], path[i - 1][1],
                                          path[i][0], path[i][1]))
    cum.append(cum[-1] + _haversine_m(path[-1][0], path[-1][1],
                                      path[0][0], path[0][1]))
    path.append(path[0])
    return path, cum


def _speed_profile(n: int, base_speed: float, fitness: float,
                   rng: random.Random) -> List[float]:
    """Instantaneous speed (m/s): warm-up, fatigue, gait waves, terrain, noise.

    The curve is deliberately *flat* (real device feedback: a jagged pace chart
    is an obvious giveaway). Mean is normalised to base_speed.
    """
    speeds = []
    for i in range(n):
        t = i / max(1, n - 1)
        warm = 0.92 + 0.08 * min(1.0, t / 0.03)
        fade = (1.0 - (0.025 * (1.0 - fitness)) * ((t - 0.6) / 0.4)) if t > 0.6 else 1.0
        wave = (1.0
                + 0.020 * math.sin(2 * math.pi * t * 7.0)
                + 0.013 * math.sin(2 * math.pi * t * 23.0 + 1.1)
                + 0.006 * math.sin(2 * math.pi * t * 61.0))
        terrain = 1.0 + 0.014 * math.sin(2 * math.pi * t * 1.8 + 0.4)
        v = base_speed * warm * fade * wave * terrain * (1.0 + rng.gauss(0.0, 0.016))
        if rng.random() < 1.0 / 130.0:
            v *= rng.uniform(0.93, 0.96)
        speeds.append(max(0.6, v))
    w = max(2, n // 120)
    smoothed = []
    for i in range(n):
        a, b = max(0, i - w), min(n, i + w + 1)
        smoothed.append(sum(speeds[a:b]) / (b - a))
    m = sum(smoothed) / len(smoothed)
    if m > 1e-9:
        smoothed = [v * base_speed / m for v in smoothed]
    return smoothed


def _sample_deltas(n: int, total_s: float, rng: random.Random) -> List[float]:
    """Sampling intervals (s) summing exactly to total_s.

    Real devices keep ~80% of samples on the nominal grid and scatter the rest,
    so the integer-second timestamps are irregular rather than a perfect grid.
    """
    m = n - 1
    if m <= 0:
        return []
    if total_s <= m * 2.5:
        return [total_s / m] * m
    dt = total_s / m
    deltas = [dt if rng.random() < 0.80
              else float(rng.choice((1.0, 2.0, 3.0, 4.0, 6.0, 7.0, 8.0)))
              for _ in range(m)]
    loose = [i for i, s in enumerate(deltas) if s != dt]
    delta = total_s - sum(deltas)
    if loose and abs(delta) > 1e-9:
        per = delta / len(loose)
        for i in loose:
            deltas[i] = max(0.5, deltas[i] + per)
    resid = total_s - sum(deltas)
    if abs(resid) > 1e-6 and sum(deltas) > 0:
        k = total_s / sum(deltas)
        deltas = [s * k for s in deltas]
    return deltas


def _ensure_cumulative(points: List[GpsPoint], total_distance_m: float,
                       total_steps: int) -> None:
    """Backfill per-point cumulative distance/steps if a caller omitted them."""
    if not points:
        return
    if not any(p.cum_dist for p in points):
        cum = [0.0]
        for i in range(1, len(points)):
            cum.append(cum[-1] + _haversine_m(points[i - 1].lat, points[i - 1].lon,
                                              points[i].lat, points[i].lon))
        if cum[-1] > 0:
            k = total_distance_m / cum[-1]
            cum = [c * k for c in cum]
        for p, c in zip(points, cum):
            p.cum_dist = round(c, 3)
    if not any(p.cum_steps for p in points) and total_distance_m > 0:
        for p in points:
            p.cum_steps = round(p.cum_dist / total_distance_m * total_steps, 2)
        points[-1].cum_steps = float(total_steps)


def _ten_sec_windows(points: List[GpsPoint], start_ms: int, total_time: int,
                     rrid: int = 0, queue_seq: bool = True,
                     total_steps: Optional[int] = None,
                     total_distance: Optional[float] = None):
    """Divide the run into 10 s windows using whole-second quota carry.

    Returns (speed_windows, step_windows). Distance and steps are accumulated
    with carry so the totals are exact and no value is ever a flat constant.
    When ``total_steps`` / ``total_distance`` are given the values are scaled
    so sum(speedPerTenSec.distance) == total_distance and
    sum(stepsPerTenSec) == total_steps hold exactly (the server checks steps).
    """
    segs = []
    for i in range(1, len(points)):
        a, b = points[i - 1], points[i]
        dt = (b.time - a.time) / 1000.0
        if dt <= 1e-9:
            continue
        segs.append([dt, b.cum_dist - a.cum_dist, b.cum_steps - a.cum_steps])

    seed = ((rrid % 100000) * 1000) if rrid else 60000
    n_win = max(1, int(total_time // 10))
    speed_windows = []
    raw_steps = []
    idx = 0
    rem = list(segs[0]) if segs else [0.0, 0.0, 0.0]
    for k in range(n_win):
        begin = start_ms + k * 10000
        hi = min((k + 1) * 10, total_time)
        end = start_ms + hi * 1000
        left = float(hi - k * 10)
        dist = steps = 0.0
        while left > 1e-9 and (idx < len(segs) or rem[0] > 1e-9):
            if rem[0] <= 1e-9:
                idx += 1
                if idx < len(segs):
                    rem = list(segs[idx])
                continue
            take = min(left, rem[0])
            f = take / rem[0]
            dist += rem[1] * f
            steps += rem[2] * f
            rem = [rem[0] - take, rem[1] - rem[1] * f, rem[2] - rem[2] * f]
            left -= take
        qn = k if queue_seq else 0
        win_id = seed + hi
        speed_windows.append({"beginTime": begin, "distance": max(dist, 0.0),
                              "endTime": end, "flag": start_ms, "id": win_id,
                              "queueNum": qn, "state": 0})
        raw_steps.append(max(steps, 0.0))

    # Make the distance totals exact even when total_time is not a multiple of
    # 10 (the trailing partial window would otherwise be dropped).
    if total_distance is not None and speed_windows:
        raw_dist_sum = sum(w["distance"] for w in speed_windows)
        if raw_dist_sum > 0:
            k = float(total_distance) / raw_dist_sum
            for w in speed_windows:
                w["distance"] = w["distance"] * k
    for w in speed_windows:
        w["distance"] = round(w["distance"], 4)

    if total_steps is not None and raw_steps and sum(raw_steps) > 0:
        counts = _largest_remainder(int(total_steps), raw_steps)
    else:
        counts = [int(s) for s in raw_steps]

    step_windows = []
    for k, win in enumerate(speed_windows):
        step_windows.append({"avgDiff": 0.0, "beginTime": win["beginTime"],
                             "endTime": win["endTime"], "flag": start_ms,
                             "id": win["id"], "maxDiff": 0.0, "minDiff": 1000.0,
                             "queueNum": win["queueNum"], "state": 0,
                             "stepsNum": counts[k]})
    return speed_windows, step_windows


def build_laps(points: List[GpsPoint], start_ms: int) -> List[dict]:
    """Per-kilometre laps (the app's "每公里数据"), contiguous and exact."""
    laps = []
    if len(points) < 2:
        return laps
    ds = [p.cum_dist for p in points]
    ts = [(p.time - start_ms) / 1000.0 for p in points]
    es = [p.ele for p in points]
    ss = [p.cum_steps for p in points]
    total_d = ds[-1]
    if total_d <= 0:
        return laps
    alt0 = es[0]

    def _at(d):
        if d <= ds[0]:
            return ts[0], es[0], ss[0]
        if d >= total_d:
            return ts[-1], es[-1], ss[-1]
        lo, hi = 0, len(ds) - 1
        while lo + 1 < hi:
            mid = (lo + hi) // 2
            if ds[mid] <= d:
                lo = mid
            else:
                hi = mid
        span = ds[hi] - ds[lo]
        f = 0.0 if span <= 1e-9 else (d - ds[lo]) / span
        return (ts[lo] + (ts[hi] - ts[lo]) * f,
                es[lo] + (es[hi] - es[lo]) * f,
                ss[lo] + (ss[hi] - ss[lo]) * f)

    def _gain(d_a, d_b):
        eles = [_at(d_a)[1]]
        for i, d in enumerate(ds):
            if d <= d_a:
                continue
            if d >= d_b:
                break
            eles.append(es[i])
        eles.append(_at(d_b)[1])
        return elevation_gain(eles)

    bounds = []
    k = 0
    while (k + 1) * 1000.0 <= total_d + 1e-6:
        bounds.append([k * 1000.0, (k + 1) * 1000.0])
        k += 1
    tail = total_d - k * 1000.0
    if tail > 0 and bounds:
        t_tail = _at(total_d)[0] - _at(k * 1000.0)[0]
        if tail >= 1.0 and t_tail >= 2.0:
            bounds.append([k * 1000.0, total_d])
        else:
            bounds[-1][1] = total_d
    if not bounds:
        bounds = [[0.0, total_d]]

    prev_cum = 0
    for i, (d_a, d_b) in enumerate(bounds):
        t_a, e_a, s_a = _at(d_a)
        t_b, e_b, s_b = _at(d_b)
        lap_d = d_b - d_a
        raw_t = max(1e-9, t_b - t_a)
        cum = int(round(t_b))
        lap_dur = cum - prev_cum
        prev_cum = cum
        lap_steps = int(max(0.0, s_b - s_a))
        laps.append({
            "avgCadence": round(lap_steps / (raw_t / 60.0), 2),
            "avgPace": round((raw_t / 60.0) / max(lap_d / 1000.0, 0.001), 2),
            "avgStride": round(lap_d / max(1, lap_steps) * 100.0, 2),
            "cumulativeDuration": cum,
            "distance": round(lap_d, 4),
            "duration": lap_dur,
            "elevationGain": _gain(d_a, d_b),
            "endAltAbs": round(e_b, 2),
            "endAltRel": round(e_b - alt0, 2),
            "flag": start_ms,
            "id": i + 1,
            "isFullLap": lap_d >= 1000.0 - 1e-9,
            "lapIndex": i + 1,
            "step": lap_steps,
        })
    return laps


def generate_synthetic_gps_track(start_lat: float, start_lon: float,
                                 total_distance_m: int, total_time_sec: int,
                                 start_time_ms: int,
                                 total_steps: Optional[int] = None,
                                 weight_kg: float = DEFAULT_BODY_WEIGHT_KG,
                                 height_cm: float = DEFAULT_HEIGHT_CM,
                                 rng: Optional[random.Random] = None
                                 ) -> Tuple[List[GpsPoint], List[FivePoint]]:
    start_lat, start_lon = validate_coordinate(start_lat, start_lon)
    if total_distance_m <= 0:
        raise ValueError("total_distance_m must be positive")
    if total_time_sec <= 0:
        raise ValueError("total_time_sec must be positive")
    rng = rng or random
    if total_steps is None:
        total_steps = max(1, int(round(total_distance_m * 1.05)))

    n = max(2, int(total_time_sec) // 5)
    base_speed = total_distance_m / total_time_sec
    fitness = rng.uniform(0.25, 0.85)

    speeds = _speed_profile(n, base_speed, fitness, rng)
    deltas = _sample_deltas(n, float(total_time_sec), rng)
    if len(deltas) != n - 1:
        deltas = [float(total_time_sec) / (n - 1)] * (n - 1)

    cum = [0.0]
    for i in range(1, n):
        cum.append(cum[-1] + 0.5 * (speeds[i - 1] + speeds[i]) * deltas[i - 1])
    if cum[-1] > 0:
        k = total_distance_m / cum[-1]
        cum = [c * k for c in cum]
    cum[-1] = float(total_distance_m)

    path, path_cum = _build_route(start_lat, start_lon, float(total_distance_m), rng)

    e_base = rng.uniform(15.0, 130.0)
    a1, a2 = rng.uniform(2.0, 5.0), rng.uniform(0.5, 1.2)
    p1, p2 = rng.uniform(0.0, 2 * math.pi), rng.uniform(0.0, 2 * math.pi)
    w1, w2 = rng.uniform(1000.0, 2500.0), rng.uniform(350.0, 700.0)

    # Cadence follows speed weakly (v**0.16), then is scaled so the integral
    # equals the requested total number of steps exactly.
    cad_prof = [((max(0.35, speeds[i]) / base_speed) ** 0.16) * (1.0 + rng.gauss(0.0, 0.012))
                for i in range(n)]
    integral = sum(0.5 * (cad_prof[i - 1] + cad_prof[i]) / 60.0 * deltas[i - 1]
                   for i in range(1, n))
    cad_scale = (float(total_steps) / integral) if integral > 0 else 0.0
    cadence = [min(200.0, max(128.0, c * cad_scale)) for c in cad_prof]

    steps_cum = [0.0]
    for i in range(1, n):
        steps_cum.append(steps_cum[-1] + 0.5 * (cadence[i - 1] + cadence[i]) / 60.0 * deltas[i - 1])
    if steps_cum[-1] > 0:
        sf = float(total_steps) / steps_cum[-1]
        steps_cum = [s * sf for s in steps_cum]

    drift = 1.5 / 111320.0
    cos_lat = math.cos(math.radians(start_lat))
    points = []
    elapsed_ms = 0.0
    for i in range(n):
        if i > 0:
            elapsed_ms += deltas[i - 1] * 1000.0
        pt_time = int(round(start_time_ms + elapsed_ms))
        lat, lon = _point_at_distance(path, path_cum, cum[i])
        if i > 0:
            lat += rng.gauss(0.0, drift)
            lon += rng.gauss(0.0, drift / cos_lat)
        ele = (e_base + a1 * math.sin(2 * math.pi * cum[i] / w1 + p1)
               + a2 * math.sin(2 * math.pi * cum[i] / w2 + p2) + rng.gauss(0.0, 0.20))
        cad = cadence[i]
        stride = (speeds[i] / (cad / 60.0) * 100.0) if cad > 0 else 0.0
        stride = max(height_cm * 0.42, min(height_cm * 1.15, stride))
        acc = min(25.0, max(1.5, rng.gauss(5.0, 2.0)))
        points.append(GpsPoint(round(lat, 6), round(lon, 6), pt_time,
                               round(speeds[i], 2), accuracy=round(acc, 1),
                               ele=round(ele, 2), cadence=round(cad, 1),
                               stride_cm=round(stride, 1), cum_dist=round(cum[i], 3),
                               cum_steps=round(steps_cum[i], 2), index=i))

    # Devices report barometer-filtered altitude, not the raw per-fix value;
    # smoothing keeps totalAscent from being inflated by GPS noise.
    if len(points) >= 5:
        raw_ele = [p.ele for p in points]
        for i in range(len(points)):
            a, b = max(0, i - 2), min(len(points), i + 3)
            points[i].ele = round(sum(raw_ele[a:b]) / (b - a), 2)

    # Five fixed checkpoints; the first doubles as the run anchor.
    five_points = []
    for i in range(5):
        idx = min(len(points) - 1, int(i * len(points) / 5))
        pt = points[idx]
        five_points.append(FivePoint(pt.lat, pt.lon, is_fixed=1, is_pass=True,
                                     point_name=f"FixedPoint_{i+1}", position=i+1))
    return points, five_points


def validate_outdoor_record_consistency(record: dict,
                                        tolerance: float = 1e-6) -> dict:
    """Validate that record coordinates all describe the same track.

    This is intentionally local and side-effect free so callers can run it in
    dry-run mode before any network request is made.
    """
    if not isinstance(record, dict):
        raise ValueError("record must be a dictionary")
    try:
        track = json.loads(record.get("allLocJson") or "[]")
        fixed = json.loads(record.get("fivePointJson") or "[]")
    except (TypeError, json.JSONDecodeError) as exc:
        raise ValueError("record location fields must contain valid JSON") from exc
    if not isinstance(track, list) or not track:
        raise ValueError("allLocJson must contain at least one point")
    if not isinstance(fixed, list) or len(fixed) != 5:
        raise ValueError("fivePointJson must contain exactly five points")
    if any(not isinstance(point, dict) for point in track):
        raise ValueError("allLocJson points must be objects")
    if any(not isinstance(point, dict) for point in fixed):
        raise ValueError("fivePointJson points must be objects")

    anchor = validate_coordinate(record.get("latitude"), record.get("longitude"))
    first = validate_coordinate(track[0].get("lat"), track[0].get("lon"))
    if not coordinates_match(anchor.latitude, anchor.longitude,
                             first.latitude, first.longitude, tolerance):
        raise ValueError("record anchor does not match the first track point")

    track_coordinates = [
        validate_coordinate(point.get("lat"), point.get("lon"))
        for point in track
    ]
    for point in fixed:
        fixed_coord = validate_coordinate(point.get("lat"), point.get("lon"))
        if not any(coordinates_match(fixed_coord.latitude, fixed_coord.longitude,
                                     track_point.latitude, track_point.longitude,
                                     tolerance) for track_point in track_coordinates):
            raise ValueError("a fixed point is not present in the full track")

    return {"trackPoints": len(track), "fixedPoints": len(fixed),
            "latitude": first.latitude, "longitude": first.longitude}


class IndoorRunRecordBuilder:
    def __init__(self, uid: int, unid: int, sel_min_time: int = 1200, min_steps: int = 2000,
                 uuid_str: Optional[str] = None):
        self.uid = uid
        self.unid = unid
        self.sel_min_time = sel_min_time
        self.min_steps = min_steps
        self.uuid_str = uuid_str or str(uuid.uuid4())

    def build_record(self, total_time_sec: int, total_steps: int,
                      start_time_ms: int, stop_time_ms: int,
                      latitude: float = 0.0, longitude: float = 0.0,
                      step_buckets: Optional[List[StepBucket]] = None,
                      calorie: int = 0,
                      rng: Optional[random.Random] = None) -> Tuple[str, dict, dict]:
        rng = rng or random
        complete = (total_time_sec >= self.sel_min_time) and (total_steps >= self.min_steps)
        if complete:
            un_complete_reason = 0
        elif total_time_sec < self.sel_min_time:
            un_complete_reason = 10
        else:
            un_complete_reason = 11

        avg_step_freq = round(total_steps * 60.0 / total_time_sec) if total_time_sec > 0 else 0

        sign_map = {
            "avgStepFreq": avg_step_freq,
            "complete": complete,
            "latitude": latitude,
            "longitude": longitude,
            "maxRunTime": 7200,
            "minSteps": self.min_steps,
            "policy": 1,
            "selRunTime": self.sel_min_time,
            "selectedUnid": self.unid,
            "sportType": 5,
            "startTime": start_time_ms,
            "stopTime": stop_time_ms,
            "totalSteps": total_steps,
            "totalTime": total_time_sec,
            "unCompleteReason": un_complete_reason,
            "uuid": self.uuid_str
        }

        signature = compute_sport_signature(sign_map)

        if step_buckets is None:
            step_buckets = []
            num_buckets = max(1, total_time_sec // 10)
            weights = _effort_weights(num_buckets, rng)
            step_counts = _largest_remainder(total_steps, weights)
            for i in range(num_buckets):
                b_start = start_time_ms + i * 10000
                b_end = min(stop_time_ms, b_start + 10000)
                step_buckets.append(StepBucket(step_counts[i], b_start, b_end, queue_num=i + 1))

        steps_json = json.dumps([b.to_dict() for b in step_buckets], separators=(",", ":"))

        save_record = {
            "address": "",
            "allLocJson": "",
            "avgPower": 0,
            "avgStepFreq": avg_step_freq,
            "calorie": calorie,
            "complete": complete,
            "errorCode": 0,
            "faceCheck": 0,
            "fivePointJson": "",
            "geeToken": "",
            "getPrize": False,
            "goalId": None,
            "isUpload": False,
            "latitude": latitude,
            "longitude": longitude,
            "maxRunTime": 7200,
            "minSteps": self.min_steps,
            "policy": 1,
            "recordUrl": "",
            "roomId": 0,
            "segmentJson": "",
            "selDistance": 0,
            "selRunTime": self.sel_min_time,
            "selectedUnid": self.unid,
            "signature": signature,
            "speed": 0,
            "speedPerTenSec": [],
            "sportType": 5,
            "startTime": start_time_ms,
            "status": 1,
            "stepsPerTenSec": [b.to_dict() for b in step_buckets],
            "stopTime": stop_time_ms,
            "themeId": 0,
            "totalAscent": 0,
            "totalDis": 0,
            "totalSteps": total_steps,
            "totalTime": total_time_sec,
            "uid": self.uid,
            "unCompleteReason": un_complete_reason,
            "unauthorized": 0,
            "useMobilityTools": 0,
            "uuid": self.uuid_str,
            "validDis": 0,
            "validTime": total_time_sec
        }

        cos_payload = {
            "uid": gzip_base64_encode(str(self.uid)),
            "uuid": gzip_base64_encode(self.uuid_str),
            "step_json": gzip_base64_encode(steps_json)
        }

        return "/api/v68/indoor/save/record", save_record, cos_payload


class OutdoorRunRecordBuilder:
    def __init__(self, uid: int, unid: int, sport_type: int = 1,
                 sel_distance_m: int = 2000, sel_run_time_s: int = 1200,
                 uuid_str: Optional[str] = None):
        self.uid = uid
        self.unid = unid
        self.sport_type = sport_type
        self.sel_distance_m = sel_distance_m
        self.sel_run_time_s = sel_run_time_s
        self.uuid_str = uuid_str or str(uuid.uuid4())

    def build_record(self, total_distance_m: int, total_time_sec: int, total_steps: int,
                     start_time_ms: int, stop_time_ms: int,
                     gps_points: List[GpsPoint],
                     five_points: Optional[List[FivePoint]] = None,
                     calorie: Optional[int] = None, avg_power: Optional[int] = None,
                     total_ascent: Optional[int] = None,
                     weight_kg: float = DEFAULT_BODY_WEIGHT_KG,
                     rng: Optional[random.Random] = None,
                     room_id: int = 0) -> Tuple[str, dict, dict]:
        if not gps_points:
            raise ValueError("gps_points cannot be empty")

        # The record-level location is deliberately derived from the same first
        # point that is serialized into allLocJson.  Do not accept a second,
        # independent coordinate source here.
        anchor = validate_coordinate(gps_points[0].lat, gps_points[0].lon)
        anchor_lat, anchor_lon = anchor

        rng = rng or random
        _ensure_cumulative(gps_points, float(total_distance_m), int(total_steps))

        # 10 s windows share the device's whole-second quota-carry semantics, so
        # sum(stepsPerTenSec) == totalSteps and sum(speedPerTenSec) == totalDis
        # hold exactly (the server checks the step sum).
        speed_windows, step_windows = _ten_sec_windows(
            gps_points, start_time_ms, total_time_sec, rrid=0, queue_seq=True,
            total_steps=int(total_steps), total_distance=float(total_distance_m))
        window_steps = sum(w["stepsNum"] for w in step_windows)
        if window_steps > 0:
            total_steps = window_steps

        complete = (total_distance_m >= self.sel_distance_m) and (total_time_sec >= self.sel_run_time_s)
        un_complete_reason = 0 if complete else 10
        avg_step_freq = round(total_steps * 60.0 / total_time_sec) if total_time_sec > 0 else 0
        # `speed` is the app's 毫-分/公里 unit: pace (min/km) × 1000.
        speed_val = (int(round((total_time_sec / 60.0) / (total_distance_m / 1000.0) * 1000.0))
                     if total_distance_m > 0 and total_time_sec > 0 else 0)

        if total_ascent is None:
            total_ascent = (int(round(elevation_gain([p.ele for p in gps_points])))
                            if any(p.ele for p in gps_points)
                            else estimate_total_ascent(total_distance_m, rng))
        if calorie is None:
            calorie = estimate_calorie(total_distance_m, total_time_sec, weight_kg)
        if avg_power is None:
            avg_power = estimate_avg_power(total_distance_m, total_time_sec,
                                           total_ascent, weight_kg)

        sign_map = {
            "address": "",
            "avgPower": avg_power,
            "avgStepFreq": avg_step_freq,
            "calorie": calorie,
            "complete": complete,
            "errorCode": 0,
            "faceCheck": 0,
            "geeToken": "",
            "getPrize": False,
            # Java reflection signs String.valueOf(null), not an omitted field.
            "goalId": "null",
            "policy": 1,
            "selDistance": self.sel_distance_m,
            "selRunTime": self.sel_run_time_s,
            "selectedUnid": self.unid,
            "speed": speed_val,
            "sportType": self.sport_type,
            "startTime": start_time_ms,
            "status": 1,
            "stopTime": stop_time_ms,
            "themeId": 0,
            "totalAscent": total_ascent,
            "totalDis": total_distance_m,
            "totalSteps": total_steps,
            "totalTime": total_time_sec,
            "uid": self.uid,
            "unCompleteReason": un_complete_reason,
            "unauthorized": 0,
            "useMobilityTools": 0,
            "uuid": self.uuid_str,
            "validDis": total_distance_m,
            "validTime": total_time_sec
        }
        if self.sport_type == 3:
            sign_map["roomId"] = room_id

        signature = compute_sport_signature(sign_map)
        original_sign = compute_original_sign(sign_map)

        speed_buckets = [
            SpeedBucket(w["distance"], w["beginTime"], w["endTime"], flag=w["flag"],
                        state=w["state"], queue_num=w["queueNum"], bucket_id=w["id"])
            for w in speed_windows
        ]
        step_buckets = [
            StepBucket(w["stepsNum"], w["beginTime"], w["endTime"], flag=w["flag"],
                       max_diff=w["maxDiff"], min_diff=w["minDiff"],
                       avg_diff=w["avgDiff"], state=w["state"],
                       queue_num=w["queueNum"], bucket_id=w["id"])
            for w in step_windows
        ]

        all_loc_json = json.dumps([p.to_dict() for p in gps_points], separators=(",", ":"))
        five_pt_json = json.dumps([p.to_dict() for p in (five_points or [])], separators=(",", ":"))
        speed_json = json.dumps([b.to_dict() for b in speed_buckets], separators=(",", ":"))
        step_json = json.dumps([b.to_dict() for b in step_buckets], separators=(",", ":"))

        # Per-kilometre laps (the app's "每公里数据") and the rich OBS track.
        laps = build_laps(gps_points, start_time_ms)
        obs_points = [p.to_obs_dict(start_time_ms) for p in gps_points]
        if obs_points:
            obs_points[0] = gps_points[0].to_obs_dict(start_time_ms, pt_type=5)
            obs_points[-1] = gps_points[-1].to_obs_dict(start_time_ms, pt_type=6)
        run_wrap = json.dumps({
            "allLocJson": json.dumps(obs_points, separators=(",", ":")),
            "useZip": False,
        }, separators=(",", ":"), ensure_ascii=False)
        obs_speed_json = json.dumps([dict(w, queueNum=0) for w in speed_windows],
                                    separators=(",", ":"))
        obs_step_json = json.dumps([dict(w, queueNum=0) for w in step_windows],
                                   separators=(",", ":"))
        laps_json = json.dumps(laps, separators=(",", ":"))

        save_record = {
            "address": "",
            "allLocJson": all_loc_json,
            "avgPower": avg_power,
            "avgStepFreq": avg_step_freq,
            "calorie": calorie,
            "complete": complete,
            "errorCode": 0,
            "faceCheck": 0,
            "fivePointJson": five_pt_json,
            "geeToken": "",
            "getPrize": False,
            "goalId": None,
            "isUpload": False,
            "latitude": anchor_lat,
            "longitude": anchor_lon,
            "maxRunTime": 7200,
            "minSteps": 2000,
            "originalSign": original_sign,
            "policy": 1,
            "recordUrl": "",
            "roomId": room_id,
            "segmentJson": "[]",
            "selDistance": self.sel_distance_m,
            "selRunTime": self.sel_run_time_s,
            "selectedUnid": self.unid,
            "signature": signature,
            "speed": speed_val,
            "speedPerTenSec": [b.to_dict() for b in speed_buckets],
            "sportType": self.sport_type,
            "startTime": start_time_ms,
            "status": 1,
            "stepsPerTenSec": [b.to_dict() for b in step_buckets],
            "stopTime": stop_time_ms,
            "themeId": 0,
            "totalAscent": total_ascent,
            "totalDis": total_distance_m,
            "totalSteps": total_steps,
            "totalTime": total_time_sec,
            "uid": self.uid,
            "unCompleteReason": un_complete_reason,
            "unauthorized": 0,
            "useMobilityTools": 0,
            "uuid": self.uuid_str,
            "validDis": total_distance_m,
            "validTime": total_time_sec
        }

        validate_outdoor_record_consistency(save_record)

        cos_payload = {
            "rrid": gzip_base64_encode(""),
            "uid": gzip_base64_encode(str(self.uid)),
            "uuid": gzip_base64_encode(self.uuid_str),
            "run_data": gzip_base64_encode(run_wrap),
            "step_freq_json": gzip_base64_encode(obs_step_json),
            "speed_json": gzip_base64_encode(obs_speed_json),
            "segment_json": gzip_base64_encode(""),
            "fixed_point_json": gzip_base64_encode(five_pt_json),
            "runFaceCheck": gzip_base64_encode(""),
            "extension_json": gzip_base64_encode(""),
            "laps_json": gzip_base64_encode(laps_json)
        }

        return "/api/v70260/runnings/save/record", save_record, cos_payload
