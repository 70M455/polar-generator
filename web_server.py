#!/usr/bin/env python3
"""
Howdoo Polar Diagram Web Server - Standalone (geen Flask nodig)
Eenvoudige HTTP server met NMEA data collector + performance berekening
"""

import socket
import threading
import json
import time
import math
from datetime import datetime
from collections import deque
import os
import sys

# Configuratie
NMEA_HOST = '192.168.1.138'
NMEA_PORT = 10110
HTTP_PORT = 5000
BUFFER_SIZE = 1024
DATA_HISTORY = deque(maxlen=300)
TEMPLATE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'templates')
PRELOADED_POLAR_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'polar_preloaded.txt')
SMOOTHING_WINDOW = 30  # Average NMEA data over 30 seconds
POLAR_TABLE_REFRESH = 600  # Reload measured polars every 10 minutes

BUCKET_MAXLEN = 200  # Max metingen per (TWA, TWS)-cel in geheugen
MOTOR_HYSTERESIS = 5 * 60   # seconds — motor-stand blijft 5 min actief na laatste detectie
_motor_last_detected = 0.0  # timestamp van de laatste motor-detectie
MOTOR_DETECTION_ENABLED = True  # kan handmatig uitgeschakeld worden via de UI

CURRENT_SESSION = {
    'active': False,
    'sails': '',
    'start_time': None,
    'buckets': {},    # {(twa, tws): deque(maxlen=BUCKET_MAXLEN)} — real-time gebuckete STW-waarden
    'total': 0,       # Totaal aantal opgeslagen metingen (voor weergave)
    'stw_max': 0.0,   # Maximale STW in deze sessie
    'stw_sum': 0.0,   # Som van STW-waarden (voor gemiddelde)
    'stw_n': 0,       # Aantal STW-samples
}

def _parse_predictwind_txt(filepath):
    """Parse a Predictwind-format polar .txt file.
    Returns (vpp_table, polar_winds) or (None, None) if the file is missing or invalid.
    File format per line: TWS<tab>0<tab>0<tab>TWA1<tab>speed1<tab>TWA2<tab>speed2...
    """
    twa_tws_speed = {}
    winds_set = set()
    try:
        with open(filepath, 'r') as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                parts = line.split('\t')
                if len(parts) < 5:
                    continue
                try:
                    tws = int(float(parts[0]))
                    winds_set.add(tws)
                    i = 3  # skip TWS, 0, 0
                    while i + 1 < len(parts):
                        twa = int(float(parts[i]))
                        speed = float(parts[i + 1])
                        twa_tws_speed[(twa, tws)] = speed
                        i += 2
                except (ValueError, IndexError):
                    continue
    except FileNotFoundError:
        return None, None
    if not twa_tws_speed:
        return None, None
    polar_winds = sorted(winds_set)
    all_twas = sorted(set(twa for twa, _ in twa_tws_speed))
    vpp_table = {
        twa: [twa_tws_speed.get((twa, tws), 0.0) for tws in polar_winds]
        for twa in all_twas
    }
    return vpp_table, polar_winds


# Nautitech 40 VPP brondata: TWA -> [snelheid per windkracht]
# Source: nautitech40_polar.csv (VPP at 52% righting moment, 11.1T displacement)
# This table is the built-in fallback — it is overridden at startup by polar_preloaded.txt
POLAR_WINDS = [4, 6, 8, 10, 12, 16, 20, 25, 30]
_VPP_TABLE = {
    45:  [1.67, 2.54, 3.39, 4.19, 4.90, 8.40, 6.96, 7.06, 6.72],
    52:  [2.12, 3.15, 4.12, 4.99, 5.78, 7.26, 8.03, 8.33, 8.47],
    60:  [2.41, 3.55, 4.59, 5.51, 6.37, 7.91, 8.90, 9.35, 9.66],
    70:  [3.16, 4.57, 5.79, 6.92, 7.92, 9.07, 9.85, 10.50, 11.03],
    80:  [3.34, 4.79, 6.05, 7.21, 8.22, 9.81, 10.60, 11.62, 12.36],
    90:  [3.38, 4.83, 6.10, 7.26, 8.27, 10.32, 11.45, 12.66, 13.59],
    100: [3.29, 4.71, 5.95, 7.10, 8.11, 10.09, 12.20, 13.43, 14.75],
    110: [3.09, 4.48, 5.71, 6.85, 7.89, 9.61, 11.83, 14.23, 14.25],
    120: [2.92, 4.26, 5.45, 6.56, 7.59, 8.96, 10.84, 13.76, 13.09],
    135: [2.57, 3.78, 4.90, 5.93, 6.91, 7.94, 9.49, 11.71, 11.65],
    150: [2.16, 3.21, 4.21, 5.14, 6.02, 7.09, 8.50, 10.32, 10.68],
    165: [1.89, 2.82, 3.73, 4.59, 5.40, 6.48, 7.84, 9.46, 10.17],
    180: [1.76, 2.63, 3.48, 4.30, 5.08, 5.94, 7.22, 8.71, 10.09],
}
# Override with polar_preloaded.txt if present — allows swapping polars without code changes
_loaded_vpp, _loaded_winds = _parse_predictwind_txt(PRELOADED_POLAR_FILE)
if _loaded_vpp is not None:
    _VPP_TABLE = _loaded_vpp
    POLAR_WINDS = _loaded_winds
    print(f"Preloaded polar loaded from file: {len(_VPP_TABLE)} angles, winds {POLAR_WINDS}")
else:
    print("polar_preloaded.txt not found — using built-in VPP table")
_VPP_ANGLES = sorted(_VPP_TABLE.keys())

# Uitgebreide polartabel in stappen van 5 graden (35–180), geïnterpoleerd vanuit VPP data
POLAR_ANGLES = list(range(35, 181, 5))
POLAR_TABLE = {}
for _twa in POLAR_ANGLES:
    if _twa in _VPP_TABLE:
        POLAR_TABLE[_twa] = _VPP_TABLE[_twa]
    else:
        # Interpoleer tussen de twee omringende VPP hoeken
        _lo = max((a for a in _VPP_ANGLES if a <= _twa), default=_VPP_ANGLES[0])
        _hi = min((a for a in _VPP_ANGLES if a >= _twa), default=_VPP_ANGLES[-1])
        if _lo == _hi:
            POLAR_TABLE[_twa] = _VPP_TABLE[_lo]
        else:
            _t = (_twa - _lo) / (_hi - _lo)
            POLAR_TABLE[_twa] = [round(s0 + _t * (s1 - s0), 2)
                                  for s0, s1 in zip(_VPP_TABLE[_lo], _VPP_TABLE[_hi])]

# Computed performance data (shared between threads)
# Polar-afhankelijke velden starten als None — worden alleen gevuld als er gemeten data is
PERFORMANCE = {
    'vmg': 0,
    'target_twa_upwind': None,
    'target_twa_downwind': None,
    'target_twa': None,
    'target_stw': None,
    'performance_pct': None,
    'polar_stw': None,
    'max_vmg_upwind': None,
    'max_vmg_downwind': None,
}

# Huidig bekeken zeilconfiguratie (bijgehouden vanuit polar-table verzoeken)
CURRENT_SAIL = ''


def _interp(x, x0, x1, y0, y1):
    """Linear interpolation between two points"""
    if x1 == x0:
        return y0
    return y0 + (y1 - y0) * (x - x0) / (x1 - x0)


def _interp_wind(speeds, tws):
    """Interpolate a speed list across POLAR_WINDS for a given TWS"""
    if tws <= POLAR_WINDS[0]:
        return speeds[0] * (tws / POLAR_WINDS[0]) if POLAR_WINDS[0] > 0 else 0
    if tws >= POLAR_WINDS[-1]:
        return speeds[-1]
    for i in range(len(POLAR_WINDS) - 1):
        if POLAR_WINDS[i] <= tws <= POLAR_WINDS[i + 1]:
            return _interp(tws, POLAR_WINDS[i], POLAR_WINDS[i + 1], speeds[i], speeds[i + 1])
    return 0


def get_polar_speed(twa, tws):
    """Bilinear interpolation of Nautitech 40 polar table (TWA x TWS)"""
    if tws <= 0:
        return 0
    norm_twa = abs(twa)
    if norm_twa > 180:
        norm_twa = 360 - norm_twa

    # Clamp to table range
    if norm_twa <= POLAR_ANGLES[0]:
        return _interp_wind(POLAR_TABLE[POLAR_ANGLES[0]], tws) * (norm_twa / POLAR_ANGLES[0])
    if norm_twa >= POLAR_ANGLES[-1]:
        return _interp_wind(POLAR_TABLE[POLAR_ANGLES[-1]], tws)

    # Find bracketing angles and interpolate
    for i in range(len(POLAR_ANGLES) - 1):
        if POLAR_ANGLES[i] <= norm_twa <= POLAR_ANGLES[i + 1]:
            s1 = _interp_wind(POLAR_TABLE[POLAR_ANGLES[i]], tws)
            s2 = _interp_wind(POLAR_TABLE[POLAR_ANGLES[i + 1]], tws)
            return _interp(norm_twa, POLAR_ANGLES[i], POLAR_ANGLES[i + 1], s1, s2)
    return 0


def calculate_vmg(stw, twa):
    """Calculate Velocity Made Good"""
    norm_twa = abs(twa)
    if norm_twa > 180:
        norm_twa = 360 - norm_twa
    return stw * math.cos(math.radians(norm_twa))


def find_optimal_twa(tws, mode='upwind'):
    """Find the TWA that maximizes VMG for given wind speed"""
    best_vmg = 0
    best_twa = 0

    if mode == 'upwind':
        search_range = range(POLAR_ANGLES[0], 91)
    else:
        search_range = range(90, POLAR_ANGLES[-1] + 1)

    for twa in search_range:
        polar_speed = get_polar_speed(twa, tws)
        if mode == 'upwind':
            vmg = polar_speed * math.cos(math.radians(twa))
        else:
            vmg = polar_speed * math.cos(math.radians(180 - twa))

        if vmg > best_vmg:
            best_vmg = vmg
            best_twa = twa

    return best_twa, best_vmg


def _interp_measured_wind(sail, angle, tws):
    """Gemeten snelheid bij een vast rooster-hoek, geïnterpoleerd naar de huidige TWS.
    Geeft None als er geen metingen zijn voor deze hoek."""
    measured = MEASURED_POLARS.get(sail, {})
    pts = sorted([(w, measured[f"{angle}_{w}"]['speed'])
                  for w in POLAR_WINDS if f"{angle}_{w}" in measured])
    if not pts:
        return None
    # Schaal lineair naar nul wind — fysisch verantwoord voor lage windsnelheden
    if tws <= pts[0][0]:
        return pts[0][1] * (tws / pts[0][0]) if pts[0][0] > 0 else pts[0][1]
    # Geen extrapolatie boven de hoogste gemeten windsnelheid
    if tws > pts[-1][0]:
        return None
    for i in range(len(pts) - 1):
        if pts[i][0] <= tws <= pts[i + 1][0]:
            t = (tws - pts[i][0]) / (pts[i + 1][0] - pts[i][0])
            return pts[i][1] + t * (pts[i + 1][1] - pts[i][1])
    return None


def get_measured_polar_speed(sail, twa, tws):
    """Bilineaire interpolatie van gemeten polar voor het opgegeven zeil.
    Geeft None terug als er onvoldoende meetdata is."""
    measured = MEASURED_POLARS.get(sail, {})
    if not measured:
        return None

    norm_twa = abs(twa)
    if norm_twa > 180:
        norm_twa = 360 - norm_twa

    # Bepaal welke rooster-hoeken gemeten data hebben
    angles_with_data = [a for a in POLAR_ANGLES
                        if any(f"{a}_{w}" in measured for w in POLAR_WINDS)]
    if not angles_with_data:
        return None

    lo_a = max((a for a in angles_with_data if a <= norm_twa), default=None)
    hi_a = min((a for a in angles_with_data if a >= norm_twa), default=None)

    if lo_a is None:
        return _interp_measured_wind(sail, hi_a, tws)
    if hi_a is None or lo_a == hi_a:
        return _interp_measured_wind(sail, lo_a, tws)

    s_lo = _interp_measured_wind(sail, lo_a, tws)
    s_hi = _interp_measured_wind(sail, hi_a, tws)

    if s_lo is None and s_hi is None:
        return None
    if s_lo is None:
        return round(s_hi, 2)
    if s_hi is None:
        return round(s_lo, 2)

    t = (norm_twa - lo_a) / (hi_a - lo_a)
    return round(s_lo + t * (s_hi - s_lo), 2)


def find_optimal_twa_measured(sail, tws, mode='upwind'):
    """Zoek de TWA met de beste VMG op basis van gemeten polar.
    Geeft (None, None) terug als er onvoldoende meetdata is."""
    measured = MEASURED_POLARS.get(sail, {})
    if not measured:
        return None, None

    angles_with_data = [a for a in POLAR_ANGLES
                        if any(f"{a}_{w}" in measured for w in POLAR_WINDS)]

    search = [a for a in angles_with_data if a <= 90] if mode == 'upwind' \
             else [a for a in angles_with_data if a >= 90]
    if not search:
        return None, None

    best_vmg = 0
    best_twa = None
    for angle in search:
        speed = _interp_measured_wind(sail, angle, tws)
        if speed is None:
            continue
        vmg = speed * math.cos(math.radians(angle)) if mode == 'upwind' \
              else speed * math.cos(math.radians(180 - angle))
        if vmg > best_vmg:
            best_vmg = vmg
            best_twa = angle

    if best_twa is None:
        return None, None
    return best_twa, round(best_vmg, 2)


VMG_TABLE_WINDS = [5, 10, 15, 20, 25, 30]

def get_vmg_targets(sail, tws):
    """Berekent optimale VMG-hoek en bijbehorende bootsnelheid voor aan de wind en ruimer."""
    twa_up, _ = find_optimal_twa_measured(sail, tws, 'upwind')
    twa_down, _ = find_optimal_twa_measured(sail, tws, 'downwind')
    return {
        'tws': tws,
        'upwind_twa':   twa_up,
        'upwind_speed': round(_interp_measured_wind(sail, twa_up, tws), 2)
                        if twa_up is not None else None,
        'downwind_twa':   twa_down,
        'downwind_speed': round(_interp_measured_wind(sail, twa_down, tws), 2)
                          if twa_down is not None else None,
    }


def update_performance(data, sail=''):
    """Recalculate all performance metrics from current data.
    VMG is altijd beschikbaar; alle polar-afhankelijke waarden vereisen gemeten data."""
    twa = data.get('twa', 0)
    tws = data.get('tws', 0)
    stw = data.get('stw', 0)

    # VMG is pure meetkunde — altijd beschikbaar
    PERFORMANCE['vmg'] = round(calculate_vmg(stw, twa), 2)

    # Alle andere metrics vereisen gemeten polar data voor dit zeil
    ref_stw = get_measured_polar_speed(sail, twa, tws) if sail else None
    PERFORMANCE['polar_stw'] = ref_stw

    PERFORMANCE['performance_pct'] = round((stw / ref_stw) * 100, 1) \
        if ref_stw is not None and ref_stw > 0 else None

    twa_up, vmg_up = find_optimal_twa_measured(sail, tws, 'upwind') if sail else (None, None)
    PERFORMANCE['target_twa_upwind'] = twa_up
    PERFORMANCE['max_vmg_upwind'] = vmg_up

    twa_down, vmg_down = find_optimal_twa_measured(sail, tws, 'downwind') if sail else (None, None)
    PERFORMANCE['target_twa_downwind'] = twa_down
    PERFORMANCE['max_vmg_downwind'] = vmg_down

    norm_twa = abs(twa) if abs(twa) <= 180 else 360 - abs(twa)
    if norm_twa < 90:
        PERFORMANCE['target_twa'] = twa_up
        PERFORMANCE['target_stw'] = get_measured_polar_speed(sail, twa_up, tws) \
            if twa_up is not None and sail else None
    else:
        PERFORMANCE['target_twa'] = twa_down
        PERFORMANCE['target_stw'] = get_measured_polar_speed(sail, twa_down, tws) \
            if twa_down is not None and sail else None


def percentile90(speeds):
    """90e percentiel van een lijst snelheden"""
    s = sorted(speeds)
    idx = int(len(s) * 0.9)
    return round(s[min(idx, len(s) - 1)], 2)


def is_motoring(twa, tws, stw):
    """Detect motoring — no polar data should be recorded.

    TWA < 20°: impossible to sail this close to the wind.

    The old STW > 0.8*TWS check is removed: at low wind speeds (e.g. 4 kts)
    the polar target is already ~85% of TWS, so a simple ratio fires constantly
    in any lull and is not reliable.

    Instead, compare against the preloaded polar: if STW is > 150% of the
    theoretical maximum for this TWA/TWS, the engine is almost certainly running.
    """
    if abs(twa) < 20:
        return True
    if stw > 0.5 and tws > 1.0:
        ref = get_polar_speed(twa, tws)
        if ref and stw > ref * 1.5:
            return True
    return False


def is_motoring_hysteresis(twa, tws, stw):
    """Motor-detectie met hysterese. Wanneer MOTOR_DETECTION_ENABLED False is,
    wordt altijd False teruggegeven (handmatige override)."""
    global _motor_last_detected
    if not MOTOR_DETECTION_ENABLED:
        return False
    if is_motoring(twa, tws, stw):
        _motor_last_detected = time.time()
        return True
    return (time.time() - _motor_last_detected) < MOTOR_HYSTERESIS


def _bucket_measurements(measurements, measured):
    """Bucket a list of measurement dicts into the measured dict {(twa,tws): [stw]}"""
    for m in measurements:
        twa = m.get('twa', 0)
        tws = m.get('tws', 0)
        stw = m.get('stw', 0)
        if twa <= 0 or tws <= 0 or stw <= 0:
            continue
        best_angle = min(POLAR_ANGLES, key=lambda a: abs(a - twa))
        if abs(best_angle - twa) > 3:
            continue
        best_wind = min(POLAR_WINDS, key=lambda w: abs(w - tws))
        if abs(best_wind - tws) > 3:
            continue
        key = (best_angle, best_wind)
        if key not in measured:
            measured[key] = []
        measured[key].append(stw)


def load_measured_polars():
    """Load all session data and build per-sail measured polar tables.
    Also includes current active session measurements."""
    sessions_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'sessions')
    measured = {}   # (twa, tws) -> [speeds]
    methods = {}    # (sails, twa, tws) -> 'p90' | 'avg'

    # Load saved sessions from disk
    if os.path.isdir(sessions_dir):
        for fname in os.listdir(sessions_dir):
            if not fname.endswith('.json'):
                continue
            try:
                with open(os.path.join(sessions_dir, fname), 'r') as f:
                    session = json.load(f)
            except Exception:
                continue
            sails = session.get('sails', '')
            if not sails:
                continue
            if sails not in measured:
                measured[sails] = {}
            # Compact format: pre-bucketed polar_data
            if 'polar_data' in session:
                for key, cell in session['polar_data'].items():
                    parts = key.split('_')
                    twa, tws = int(parts[0]), int(parts[1])
                    count = cell.get('count', 1)
                    method = cell.get('method', 'avg')
                    if (twa, tws) not in measured[sails]:
                        measured[sails][(twa, tws)] = []
                    measured[sails][(twa, tws)].extend([cell['speed']] * count)
                    # p90 wint van avg als minstens één sessie p90 heeft
                    mk = (sails, twa, tws)
                    if methods.get(mk) != 'p90':
                        methods[mk] = method
            else:
                # Legacy format: raw measurements — wordt als p90 herberekend
                _bucket_measurements(session.get('measurements', []), measured[sails])

    # Include current active session (in-memory, al gebuckete deques)
    if CURRENT_SESSION['active'] and CURRENT_SESSION['sails']:
        sails = CURRENT_SESSION['sails']
        if sails not in measured:
            measured[sails] = {}
        for (twa, tws), samples in CURRENT_SESSION['buckets'].items():
            if (twa, tws) not in measured[sails]:
                measured[sails][(twa, tws)] = []
            measured[sails][(twa, tws)].extend([s[0] for s in samples])
            methods[(sails, twa, tws)] = 'p90'

    result = {}
    for sails, data in measured.items():
        result[sails] = {}
        for (twa, tws), speeds in data.items():
            mk = (sails, twa, tws)
            method = methods.get(mk, 'p90')
            result[sails][f"{twa}_{tws}"] = {
                'speed': percentile90(speeds),
                'count': len(speeds),
                'method': method
            }
    return result


MEASURED_POLARS = load_measured_polars()


def get_polar_table_for_sail(sail_config):
    """Return polar table with measured data overlaid where available."""
    measured = MEASURED_POLARS.get(sail_config, {})
    table = {}
    for twa in POLAR_ANGLES:
        table[twa] = {}
        for i, tws in enumerate(POLAR_WINDS):
            key = f"{twa}_{tws}"
            if key in measured:
                table[twa][tws] = {
                    'speed': measured[key]['speed'],
                    'source': 'measured',
                    'count': measured[key]['count'],
                    'method': measured[key].get('method', 'avg')
                }
            else:
                table[twa][tws] = {
                    'speed': round(POLAR_TABLE[twa][i], 2),
                    'source': 'polar',
                    'count': 0
                }
    return table


class PerformanceUpdater:
    """Updates performance calculations and reloads polar table periodically"""

    def __init__(self):
        self.running = False
        self.thread = None

    def update_loop(self):
        last_polar_reload = time.time()
        while self.running:
            try:
                avg = collector.averaged_data
                # Gebruik actief-sessie-zeil als prioriteit, anders het bekeken zeil
                sail_for_perf = CURRENT_SESSION['sails'] if CURRENT_SESSION['active'] else CURRENT_SAIL
                update_performance(avg, sail_for_perf)

                # Polar bucketing op basis van 30s gemiddelde (1x per seconde)
                if CURRENT_SESSION['active'] and not avg.get('motoring', False):
                    twa = avg.get('twa', 0)
                    tws = avg.get('tws', 0)
                    stw = avg.get('stw', 0)
                    if twa > 0 and tws > 0 and stw > 0:
                        # Track sessie-statistieken (alle geldige zeil-momenten)
                        if stw > CURRENT_SESSION['stw_max']:
                            CURRENT_SESSION['stw_max'] = stw
                        CURRENT_SESSION['stw_sum'] += stw
                        CURRENT_SESSION['stw_n'] += 1

                        best_angle = min(POLAR_ANGLES, key=lambda a: abs(a - twa))
                        best_wind  = min(POLAR_WINDS,  key=lambda w: abs(w - tws))
                        wi = POLAR_WINDS.index(best_wind)
                        neighbors = []
                        if wi > 0: neighbors.append(POLAR_WINDS[wi - 1])
                        if wi < len(POLAR_WINDS) - 1: neighbors.append(POLAR_WINDS[wi + 1])
                        tws_tol = min(abs(best_wind - n) for n in neighbors) / 2 if neighbors else 3
                        if abs(best_angle - twa) <= 3 and abs(best_wind - tws) <= tws_tol:
                            key = (best_angle, best_wind)
                            if key not in CURRENT_SESSION['buckets']:
                                CURRENT_SESSION['buckets'][key] = deque(maxlen=BUCKET_MAXLEN)
                            CURRENT_SESSION['buckets'][key].append((stw, datetime.now().isoformat()))
                            CURRENT_SESSION['total'] += 1

                # Reload measured polars every 10 minutes
                if time.time() - last_polar_reload > POLAR_TABLE_REFRESH:
                    global MEASURED_POLARS
                    MEASURED_POLARS = load_measured_polars()
                    last_polar_reload = time.time()
                    print("Polar tabel herladen")

                time.sleep(1)
            except Exception as e:
                print(f"Performance update error: {e}")
                time.sleep(1)

    def start(self):
        if not self.running:
            self.running = True
            self.thread = threading.Thread(target=self.update_loop, daemon=True)
            self.thread.start()

    def stop(self):
        self.running = False
        if self.thread:
            self.thread.join(timeout=5)


class NMEACollector:
    """Verzamelt NMEA-data van B&G kaartplotter"""

    SMOOTHED_FIELDS = ['twa', 'tws', 'stw', 'sog', 'heading', 'depth',
                        'water_temp', 'air_temp', 'heel', 'trim', 'baro', 'rudder']

    def __init__(self):
        self.running = False
        self.thread = None
        self.latest_data = {
            'twa': 0,
            'tws': 0,
            'stw': 0,
            'heading': 0,
            'timestamp': datetime.now().isoformat(),
            'sog': 0,
            'depth': 0,
            'water_temp': 0,
            'air_temp': 0,
            'heel': 0,
            'trim': 0,
            'baro': 0,
            'rudder': 0,
            'lat': 0,
            'lon': 0
        }
        self._sample_buffer = deque(maxlen=SMOOTHING_WINDOW * 10)

    @property
    def averaged_data(self):
        """Return 30-second rolling average of numeric fields"""
        now = time.time()
        cutoff = now - SMOOTHING_WINDOW
        samples = [s for s in self._sample_buffer if s['_t'] >= cutoff]
        if not samples:
            return dict(self.latest_data)
        result = dict(self.latest_data)
        for field in self.SMOOTHED_FIELDS:
            vals = [s[field] for s in samples if field in s]
            if vals:
                result[field] = round(sum(vals) / len(vals), 4)
        return result

    def parse_nmea(self, sentence):
        """Parse NMEA 0183 sentence - handles II, WI, SD, GP talker IDs"""
        try:
            if not sentence.startswith('$'):
                return None

            if '*' in sentence:
                sentence = sentence[:sentence.index('*')]

            parts = sentence.split(',')
            msg_id = parts[0]
            sentence_type = msg_id[3:] if len(msg_id) >= 6 else ''

            data = {}

            if sentence_type == 'MWV':
                if len(parts) >= 6 and parts[5] == 'A':
                    wind_angle = float(parts[1]) if parts[1] else 0
                    wind_speed = float(parts[3]) if parts[3] else 0
                    ref = parts[2]
                    if ref == 'T':
                        if wind_angle > 180:
                            wind_angle = 360 - wind_angle
                        data['twa'] = wind_angle
                        data['tws'] = wind_speed

            elif sentence_type == 'MWD':
                if len(parts) >= 6:
                    if parts[5]:
                        data['tws'] = float(parts[5])

            elif sentence_type == 'VHW':
                if len(parts) >= 6:
                    if parts[1]:
                        data['heading'] = float(parts[1])
                    if parts[5]:
                        data['stw'] = float(parts[5])

            elif sentence_type == 'VTG':
                if len(parts) >= 6:
                    if parts[5]:
                        data['sog'] = float(parts[5])

            elif sentence_type == 'DBT':
                if len(parts) >= 4:
                    if parts[3]:
                        data['depth'] = float(parts[3])

            elif sentence_type == 'MTW':
                if len(parts) >= 2:
                    if parts[1]:
                        data['water_temp'] = float(parts[1])

            elif sentence_type == 'HDG':
                if len(parts) >= 2:
                    if parts[1]:
                        data['heading'] = float(parts[1])

            elif sentence_type == 'XDR':
                i = 1
                while i + 3 <= len(parts):
                    xdr_type = parts[i]
                    xdr_value = parts[i + 1]
                    xdr_unit = parts[i + 2]
                    xdr_name = parts[i + 3] if i + 3 < len(parts) else ''

                    if xdr_value:
                        if xdr_name == 'AIRTEMP' and xdr_type == 'C':
                            data['air_temp'] = float(xdr_value)
                        elif xdr_name == 'HEEL':
                            data['heel'] = float(xdr_value)
                        elif xdr_name == 'TRIM':
                            data['trim'] = float(xdr_value)
                        elif xdr_name == 'BARO':
                            data['baro'] = float(xdr_value)
                        elif xdr_name == 'RUDDER':
                            data['rudder'] = float(xdr_value)

                    i += 4

            elif sentence_type == 'GGA':
                if len(parts) >= 6:
                    if parts[2] and parts[4]:
                        lat = self._parse_latlon(parts[2], parts[3])
                        lon = self._parse_latlon(parts[4], parts[5])
                        data['lat'] = lat
                        data['lon'] = lon

            return data if data else None
        except Exception:
            return None

    def _parse_latlon(self, value, direction):
        """Parse NMEA lat/lon (DDMM.MMMM) to decimal degrees"""
        try:
            if len(value) < 4:
                return 0
            dot = value.index('.')
            deg_digits = dot - 2
            degrees = float(value[:deg_digits])
            minutes = float(value[deg_digits:])
            result = degrees + minutes / 60.0
            if direction in ('S', 'W'):
                result = -result
            return round(result, 6)
        except Exception:
            return 0

    def collect_loop(self):
        """Hoofdlus voor data verzameling"""
        while self.running:
            try:
                sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                sock.settimeout(10)
                print(f"Verbinding met NMEA server: {NMEA_HOST}:{NMEA_PORT}")
                sock.connect((NMEA_HOST, NMEA_PORT))
                print("NMEA verbinding OK!")

                buffer = ""
                while self.running:
                    try:
                        chunk = sock.recv(BUFFER_SIZE).decode('utf-8', errors='ignore')
                        if not chunk:
                            break

                        buffer += chunk
                        # Prevent buffer from growing unbounded
                        if len(buffer) > 10000:
                            buffer = buffer[-5000:]
                        lines = buffer.split('\n')
                        buffer = lines[-1]

                        for line in lines[:-1]:
                            parsed = self.parse_nmea(line.strip())
                            if parsed:
                                self.latest_data.update(parsed)
                                self.latest_data['timestamp'] = datetime.now().isoformat()
                                self.latest_data['motoring'] = is_motoring_hysteresis(
                                    self.latest_data.get('twa', 0),
                                    self.latest_data.get('tws', 0),
                                    self.latest_data.get('stw', 0)
                                )

                                sample = dict(self.latest_data)
                                sample['_t'] = time.time()
                                self._sample_buffer.append(sample)


                                DATA_HISTORY.append(self.latest_data.copy())

                    except socket.timeout:
                        continue
                    except Exception as e:
                        print(f"Parse error: {e}")
                        break

                sock.close()

            except Exception as e:
                print(f"NMEA Connection error: {e}")
                if self.running:
                    print("Herverbinding in 3 seconden...")
                    time.sleep(3)

    def start(self):
        if not self.running:
            self.running = True
            self.thread = threading.Thread(target=self.collect_loop, daemon=True)
            self.thread.start()

    def stop(self):
        self.running = False
        if self.thread:
            self.thread.join(timeout=5)


class SimpleHTTPServer:
    """Eenvoudige HTTP server zonder externe dependencies"""

    def __init__(self, port=5000):
        self.port = port
        self.running = False
        self.thread = None

    def get_html(self):
        """Read HTML template fresh from disk every time — no caching,
        so a new deploy takes effect immediately without a service restart."""
        template_path = os.path.join(TEMPLATE_DIR, 'polar_live.html')
        try:
            with open(template_path, 'r') as f:
                return f.read()
        except FileNotFoundError:
            return "<html><body><h1>Template not found: {}</h1></body></html>".format(template_path)

    def handle_request(self, client_socket, addr):
        global MEASURED_POLARS, CURRENT_SAIL, MOTOR_DETECTION_ENABLED
        try:
            client_socket.settimeout(5)
            request = client_socket.recv(4096).decode('utf-8', errors='ignore')
            if not request:
                return

            lines = request.split('\r\n')
            method_line = lines[0].split()

            if len(method_line) < 2:
                return

            path = method_line[1]
            method = method_line[0]

            if path == '/' or path == '/index.html':
                html = self.get_html()
                body_bytes = html.encode('utf-8')
                header = f"HTTP/1.1 200 OK\r\nContent-Type: text/html; charset=utf-8\r\nContent-Length: {len(body_bytes)}\r\nConnection: close\r\n\r\n"
                client_socket.sendall(header.encode('utf-8') + body_bytes)
                return

            elif path == '/api/current':
                _stw_n = CURRENT_SESSION.get('stw_n', 0)
                session_copy = {
                    'active': CURRENT_SESSION['active'],
                    'sails': CURRENT_SESSION['sails'],
                    'start_time': CURRENT_SESSION['start_time'],
                    'total': CURRENT_SESSION['total'],
                    'stw_max': round(CURRENT_SESSION.get('stw_max', 0.0), 2),
                    'stw_avg': round(CURRENT_SESSION['stw_sum'] / _stw_n, 2) if _stw_n > 0 else 0,
                }
                data = {
                    'timestamp': datetime.now().isoformat(),
                    'session': session_copy,
                    'current': collector.averaged_data,
                    'performance': dict(PERFORMANCE),
                    'active': CURRENT_SESSION['active'],
                    'total_measurements': CURRENT_SESSION['total'],
                    'motor_detection_enabled': MOTOR_DETECTION_ENABLED,
                }
                body = json.dumps(data)
                response = f"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: {len(body)}\r\nConnection: close\r\n\r\n{body}"

            elif path == '/api/motor-detection' and method == 'POST':
                try:
                    body_raw = request.split('\r\n\r\n')[1]
                    payload = json.loads(body_raw)
                    MOTOR_DETECTION_ENABLED = bool(payload.get('enabled', True))
                    body = json.dumps({'status': 'ok', 'motor_detection_enabled': MOTOR_DETECTION_ENABLED})
                    response = f"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: {len(body)}\r\nConnection: close\r\n\r\n{body}"
                except Exception as e:
                    body = json.dumps({'status': 'error', 'message': str(e)})
                    response = f"HTTP/1.1 400 Bad Request\r\nContent-Type: application/json\r\nContent-Length: {len(body)}\r\nConnection: close\r\n\r\n{body}"

            elif path == '/api/session/start' and method == 'POST':
                try:
                    body = request.split('\r\n\r\n')[1]
                    data = json.loads(body)
                    sails = data.get('sails', 'Unknown')
                    CURRENT_SESSION['active'] = True
                    CURRENT_SESSION['sails'] = sails
                    CURRENT_SESSION['start_time'] = datetime.now().isoformat()
                    CURRENT_SESSION['buckets'] = {}
                    CURRENT_SESSION['total'] = 0
                    CURRENT_SESSION['stw_max'] = 0.0
                    CURRENT_SESSION['stw_sum'] = 0.0
                    CURRENT_SESSION['stw_n'] = 0
                    DATA_HISTORY.clear()
                    resp_body = json.dumps({'status': 'started'})
                    response = f"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: {len(resp_body)}\r\nConnection: close\r\n\r\n{resp_body}"
                except Exception:
                    response = "HTTP/1.1 400 Bad Request\r\nConnection: close\r\n\r\n"

            elif path == '/api/session/stop' and method == 'POST':
                CURRENT_SESSION['active'] = False
                num_measurements = CURRENT_SESSION['total']
                sails = CURRENT_SESSION['sails']
                # Buckets zijn al klaar — direct p90 berekenen per cel
                polar_data = {}
                for (twa, tws), samples in CURRENT_SESSION['buckets'].items():
                    if samples:
                        speeds = [s[0] for s in samples]
                        polar_data[f"{twa}_{tws}"] = {
                            'speed': percentile90(speeds),
                            'count': len(samples),
                            'method': 'p90',
                            'samples': [[round(s[0], 2), s[1]] for s in samples]
                        }
                session_save = {
                    'sails': sails,
                    'start_time': CURRENT_SESSION['start_time'],
                    'polar_data': polar_data,
                    'total_measurements': num_measurements
                }
                # MEASURED_POLARS direct bijwerken zodat de polartabel
                # meteen klopt als de JS refresht na het stoppen
                MEASURED_POLARS = load_measured_polars()
                resp_body = json.dumps({'status': 'stopped', 'measurements': num_measurements})
                response = f"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: {len(resp_body)}\r\nConnection: close\r\n\r\n{resp_body}"
                # Bestandsschrijven in achtergrond (trage operatie)
                def _save_session(data):
                    try:
                        if data['polar_data']:
                            filename = f"session_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
                            filepath = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'sessions', filename)
                            os.makedirs(os.path.dirname(filepath), exist_ok=True)
                            with open(filepath, 'w') as f:
                                json.dump(data, f)
                        print(f"Sessie opgeslagen: {data['total_measurements']} metingen, {len(data['polar_data'])} polarpunten")
                    except Exception as e:
                        print(f"Fout bij opslaan sessie: {e}")
                threading.Thread(target=_save_session, args=(session_save,), daemon=True).start()

            elif path.startswith('/api/polar-table'):
                sail = 'Vol tuig met fok'
                if '?' in path:
                    qs = path.split('?', 1)[1]
                    for param in qs.split('&'):
                        if param.startswith('sail='):
                            from urllib.parse import unquote_plus
                            sail = unquote_plus(param[5:])
                # Bijhouden welk zeil de gebruiker bekijkt (voor performance berekening)
                if not CURRENT_SESSION['active']:
                    CURRENT_SAIL = sail
                table = get_polar_table_for_sail(sail)
                json_table = {}
                for twa, winds in table.items():
                    json_table[str(twa)] = {}
                    for tws, cell in winds.items():
                        json_table[str(twa)][str(tws)] = cell
                resp_data = {
                    'sail': sail,
                    'angles': POLAR_ANGLES,
                    'winds': POLAR_WINDS,
                    'table': json_table,
                    'measured_sails': list(MEASURED_POLARS.keys())
                }
                body = json.dumps(resp_data)
                response = f"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: {len(body)}\r\nConnection: close\r\n\r\n{body}"

            elif path == '/api/history':
                body = json.dumps(list(DATA_HISTORY))
                response = f"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: {len(body)}\r\nConnection: close\r\n\r\n{body}"

            elif path == '/api/export':
                sessions_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'sessions')
                sessions = []
                if os.path.isdir(sessions_dir):
                    for fname in sorted(os.listdir(sessions_dir)):
                        if not fname.endswith('.json'):
                            continue
                        try:
                            with open(os.path.join(sessions_dir, fname)) as f:
                                sessions.append({'filename': fname, 'data': json.load(f)})
                        except Exception:
                            pass
                export = {
                    'exported_at': datetime.now().isoformat(),
                    'version': 1,
                    'sessions': sessions
                }
                body = json.dumps(export)
                fname = f"howdoo_polar_backup_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
                response = (
                    f"HTTP/1.1 200 OK\r\n"
                    f"Content-Type: application/json\r\n"
                    f"Content-Disposition: attachment; filename=\"{fname}\"\r\n"
                    f"Content-Length: {len(body)}\r\n"
                    f"Connection: close\r\n\r\n{body}"
                )

            elif path == '/api/import' and method == 'POST':
                try:
                    # Lees de rest van de request (body na headers)
                    # Request is al volledig ingelezen via recv(4096) — lees meer indien nodig
                    raw = request
                    # Zoek Content-Length
                    content_length = 0
                    for line in lines[1:]:
                        if line.lower().startswith('content-length:'):
                            content_length = int(line.split(':', 1)[1].strip())
                    # Haal body op (alles na \r\n\r\n)
                    header_end = raw.find('\r\n\r\n')
                    body_bytes = raw[header_end + 4:].encode('latin-1')
                    # Lees resterende bytes indien nodig
                    while len(body_bytes) < content_length:
                        chunk = client_socket.recv(65536)
                        if not chunk:
                            break
                        body_bytes += chunk

                    # Multipart: zoek JSON-inhoud na de headers van het bestandsdeel
                    body_str = body_bytes.decode('utf-8', errors='ignore')
                    # Zoek begin van JSON (eerste '{')
                    json_start = body_str.find('{')
                    json_end = body_str.rfind('}')
                    if json_start == -1 or json_end == -1:
                        raise ValueError("Geen JSON gevonden in upload")
                    import_data = json.loads(body_str[json_start:json_end + 1])

                    if import_data.get('version') != 1 or 'sessions' not in import_data:
                        raise ValueError("Ongeldig backup formaat")

                    sessions_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'sessions')
                    os.makedirs(sessions_dir, exist_ok=True)
                    imported = 0
                    for entry in import_data['sessions']:
                        fname = entry.get('filename', '')
                        data = entry.get('data', {})
                        if fname and data:
                            with open(os.path.join(sessions_dir, fname), 'w') as f:
                                json.dump(data, f)
                            imported += 1
                    MEASURED_POLARS = load_measured_polars()
                    resp_body = json.dumps({'status': 'ok', 'imported': imported})
                    response = f"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: {len(resp_body)}\r\nConnection: close\r\n\r\n{resp_body}"
                    print(f"Import: {imported} sessies hersteld")
                except Exception as e:
                    print(f"Import fout: {e}")
                    resp_body = json.dumps({'status': 'error', 'message': str(e)})
                    response = f"HTTP/1.1 400 Bad Request\r\nContent-Type: application/json\r\nContent-Length: {len(resp_body)}\r\nConnection: close\r\n\r\n{resp_body}"

            elif path.startswith('/api/vmg-table'):
                from urllib.parse import unquote_plus
                sail = 'Vol tuig met fok'
                if '?' in path:
                    for param in path.split('?', 1)[1].split('&'):
                        if param.startswith('sail='):
                            sail = unquote_plus(param[5:])
                rows = [get_vmg_targets(sail, tws) for tws in VMG_TABLE_WINDS]
                body = json.dumps({'sail': sail, 'rows': rows})
                response = f"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: {len(body)}\r\nConnection: close\r\n\r\n{body}"

            elif path == '/api/cell/clear' and method == 'POST':
                try:
                    req = json.loads(request.split('\r\n\r\n')[1])
                    sail   = req.get('sail', '')
                    twa_c  = int(req.get('twa', 0))
                    tws_c  = int(req.get('tws', 0))
                    key_str = f"{twa_c}_{tws_c}"
                    sessions_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'sessions')
                    cleared = 0
                    if os.path.isdir(sessions_dir):
                        for fname in os.listdir(sessions_dir):
                            if not fname.endswith('.json'):
                                continue
                            fpath = os.path.join(sessions_dir, fname)
                            try:
                                with open(fpath) as f:
                                    sess = json.load(f)
                                if sess.get('sails') != sail:
                                    continue
                                if 'polar_data' in sess and key_str in sess['polar_data']:
                                    del sess['polar_data'][key_str]
                                    cleared += 1
                                    if sess['polar_data']:
                                        with open(fpath, 'w') as f:
                                            json.dump(sess, f)
                                    else:
                                        os.remove(fpath)  # sessie heeft geen data meer
                            except Exception:
                                pass
                    MEASURED_POLARS = load_measured_polars()
                    resp_body = json.dumps({'status': 'ok', 'cleared': cleared})
                    response = f"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: {len(resp_body)}\r\nConnection: close\r\n\r\n{resp_body}"
                except Exception as e:
                    resp_body = json.dumps({'status': 'error', 'message': str(e)})
                    response = f"HTTP/1.1 400 Bad Request\r\nContent-Type: application/json\r\nContent-Length: {len(resp_body)}\r\nConnection: close\r\n\r\n{resp_body}"

            elif path == '/api/polar/reset' and method == 'POST':
                try:
                    req = json.loads(request.split('\r\n\r\n')[1])
                    sail = req.get('sail', '')
                    sessions_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'sessions')
                    deleted = 0
                    if os.path.isdir(sessions_dir):
                        for fname in os.listdir(sessions_dir):
                            if not fname.endswith('.json'):
                                continue
                            fpath = os.path.join(sessions_dir, fname)
                            try:
                                with open(fpath) as f:
                                    sess = json.load(f)
                                if sess.get('sails') == sail:
                                    os.remove(fpath)
                                    deleted += 1
                            except Exception:
                                pass
                    MEASURED_POLARS = load_measured_polars()
                    resp_body = json.dumps({'status': 'ok', 'deleted': deleted})
                    response = f"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: {len(resp_body)}\r\nConnection: close\r\n\r\n{resp_body}"
                except Exception as e:
                    resp_body = json.dumps({'status': 'error', 'message': str(e)})
                    response = f"HTTP/1.1 400 Bad Request\r\nContent-Type: application/json\r\nContent-Length: {len(resp_body)}\r\nConnection: close\r\n\r\n{resp_body}"

            elif path.startswith('/api/polar-export'):
                from urllib.parse import unquote_plus
                sail = ''
                if '?' in path:
                    for param in path.split('?', 1)[1].split('&'):
                        if param.startswith('sail='):
                            sail = unquote_plus(param[5:])
                # Predictwind polar format: one line per TWS, tab-separated
                # TWS  0  0  TWA1  speed1  TWA2  speed2 ...
                # Angles used by Predictwind / VPP table
                export_angles = [45, 52, 60, 70, 80, 90, 100, 110, 120, 135, 150, 165, 180]
                lines = []
                for tws in POLAR_WINDS:
                    parts = [str(tws), '0', '0']
                    for twa in export_angles:
                        # Prefer measured data for this sail; fall back to VPP polar
                        measured_spd = get_measured_polar_speed(sail, twa, tws) if sail else None
                        spd = measured_spd if measured_spd is not None else round(get_polar_speed(twa, tws), 2)
                        parts.append(str(twa))
                        parts.append(str(spd))
                    lines.append('\t'.join(parts))
                body = '\n'.join(lines) + '\n'
                sail_slug = (sail or 'vpp').replace(' ', '_')
                fname = f"polar_howdoo_{sail_slug}.txt"
                body_bytes = body.encode('utf-8')
                response = (
                    f"HTTP/1.1 200 OK\r\n"
                    f"Content-Type: text/plain; charset=utf-8\r\n"
                    f"Content-Disposition: attachment; filename=\"{fname}\"\r\n"
                    f"Content-Length: {len(body_bytes)}\r\n"
                    f"Connection: close\r\n\r\n{body}"
                )

            elif path.startswith('/api/measurements'):
                from urllib.parse import unquote_plus
                sail = ''
                twa_req = 0
                tws_req = 0
                if '?' in path:
                    qs = path.split('?', 1)[1]
                    for param in qs.split('&'):
                        if param.startswith('sail='):
                            sail = unquote_plus(param[5:])
                        elif param.startswith('twa='):
                            try: twa_req = int(param[4:])
                            except Exception: pass
                        elif param.startswith('tws='):
                            try: tws_req = int(param[4:])
                            except Exception: pass

                key_str = f"{twa_req}_{tws_req}"
                sessions_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'sessions')
                all_samples = []

                if os.path.isdir(sessions_dir):
                    for fname in sorted(os.listdir(sessions_dir)):
                        if not fname.endswith('.json'):
                            continue
                        try:
                            with open(os.path.join(sessions_dir, fname)) as f:
                                sess = json.load(f)
                        except Exception:
                            continue
                        if sess.get('sails', '') != sail:
                            continue
                        if 'polar_data' in sess and key_str in sess['polar_data']:
                            cell = sess['polar_data'][key_str]
                            samples = cell.get('samples', [])
                            if samples:
                                for sample in samples:
                                    all_samples.append({
                                        'speed': sample[0],
                                        'timestamp': sample[1],
                                        'session': fname
                                    })
                            else:
                                # Oud formaat: geen individuele tijdstempels, alleen geaggregeerde waarde
                                all_samples.append({
                                    'speed': cell['speed'],
                                    'timestamp': None,
                                    'count': cell.get('count', 1),
                                    'method': cell.get('method', 'avg'),
                                    'session': fname
                                })

                # Include current active session
                if CURRENT_SESSION['active'] and CURRENT_SESSION['sails'] == sail:
                    bucket_key = (twa_req, tws_req)
                    if bucket_key in CURRENT_SESSION['buckets']:
                        for s in CURRENT_SESSION['buckets'][bucket_key]:
                            all_samples.append({
                                'speed': round(s[0], 2),
                                'timestamp': s[1],
                                'session': 'actief'
                            })

                # Nieuwste tijdstempels bovenaan; entries zonder tijdstempel achteraan
                known = sorted([s for s in all_samples if s['timestamp'] is not None],
                               key=lambda x: x['timestamp'], reverse=True)
                unknown = [s for s in all_samples if s['timestamp'] is None]
                all_samples = known + unknown
                body = json.dumps({'sail': sail, 'twa': twa_req, 'tws': tws_req, 'samples': all_samples})
                response = f"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: {len(body)}\r\nConnection: close\r\n\r\n{body}"

            else:
                response = "HTTP/1.1 404 Not Found\r\nConnection: close\r\n\r\n"

            client_socket.sendall(response.encode('utf-8'))

        except Exception as e:
            print(f"Request error: {e}")
        finally:
            try:
                client_socket.close()
            except Exception:
                pass

    def server_loop(self):
        server_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        server_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        server_socket.bind(('0.0.0.0', self.port))
        server_socket.listen(5)
        print(f"HTTP Server luistert op poort {self.port}")

        while self.running:
            try:
                server_socket.settimeout(1)
                client_socket, addr = server_socket.accept()
                thread = threading.Thread(target=self.handle_request, args=(client_socket, addr), daemon=True)
                thread.start()
            except socket.timeout:
                continue
            except Exception as e:
                if self.running:
                    print(f"Server error: {e}")
        server_socket.close()

    def start(self):
        if not self.running:
            self.running = True
            self.thread = threading.Thread(target=self.server_loop, daemon=True)
            self.thread.start()

    def stop(self):
        self.running = False
        if self.thread:
            self.thread.join(timeout=5)


# Main
if __name__ == '__main__':
    print("=" * 50)
    print("Howdoo Polar Diagram Web Server")
    print("=" * 50)

    collector = NMEACollector()
    collector.start()

    perf_updater = PerformanceUpdater()
    perf_updater.start()

    server = SimpleHTTPServer(HTTP_PORT)
    server.start()

    print(f"\nServer gestart!")
    print(f"Open: http://localhost:{HTTP_PORT}")
    print(f"Druk Ctrl+C om te stoppen\n")

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("\nServer gestopt")
        perf_updater.stop()
        collector.stop()
        server.stop()
        sys.exit(0)
