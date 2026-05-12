# Polar Generator

A live sailing polar diagram web app running on a **Raspberry Pi Zero**. It reads real-time NMEA 0183 data from a B&G chartplotter, builds a measured polar from your actual boat speed, and serves a web UI accessible from any device on the boat's network.

![Polar Generator UI](https://img.shields.io/badge/platform-Raspberry%20Pi%20Zero-red) ![Python](https://img.shields.io/badge/python-3.11-blue) ![License](https://img.shields.io/badge/license-MIT-green)

## Features

- **Live polar table** — measured 90th-percentile boat speed per (TWA, TWS) cell, compared against a preloaded reference polar
- **Polar diagram** — real-time chart with live boat position dot
- **VMG targets** — optimal upwind/downwind angle and speed per wind strength
- **Performance meter** — current speed vs your own measured polar
- **Motor detection** — automatic engine detection with manual override toggle
- **Session recording** — start/stop sailing sessions; each measurement is timestamped and stored per cell
- **Click-to-inspect** — click any cell in the polar table to see individual measurements and timestamps
- **Cell clear / table reset** — remove bad measurements with backup-first protection
- **Export to Predictwind** — download your measured polar as a `.txt` file ready to load into Predictwind
- **JSON backup / restore** — export and import all session data

## How it works

```
B&G Zeus (NMEA 0183 TCP) → Raspberry Pi Zero → Web browser
                                    ↓
                          Averages TWA, TWS, STW
                          over 30-second window
                                    ↓
                          Buckets measurements into
                          (TWA, TWS) grid cells
                                    ↓
                          Computes 90th percentile
                          per cell → measured polar
```

NMEA sentences used: `MWV` (wind), `VHW` (boat speed), `VTG` (speed over ground).

## Hardware

- Raspberry Pi Zero (any model with Wi-Fi)
- B&G chartplotter or any NMEA 0183 source accessible via TCP

## Installation

### 1. Clone the repo

```bash
git clone https://github.com/70M455/polar-generator.git
cd polar-generator
```

### 2. Configure NMEA source

Edit `web_server.py` and set the IP and port of your NMEA TCP source:

```python
NMEA_HOST = '192.168.1.138'
NMEA_PORT = 10110
```

### 3. Set your preloaded polar

Replace `polar_preloaded.txt` with your boat's polar in Predictwind format:

```
TWS<tab>0<tab>0<tab>TWA1<tab>speed1<tab>TWA2<tab>speed2...
```

One line per wind speed (e.g. 4, 6, 8, 10, 12, 16, 20, 25, 30 knots). The included file contains the Nautitech 40 VPP polar.

### 4. Install as a systemd service

```bash
sudo nano /etc/systemd/system/polar-generator.service
```

```ini
[Unit]
Description=Polar Generator Web Server
After=network.target

[Service]
ExecStart=/usr/bin/python3 /home/pi/polar-generator/web_server.py
WorkingDirectory=/home/pi/polar-generator
Restart=always
User=pi

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl enable polar-generator
sudo systemctl start polar-generator
```

### 5. Open the UI

Navigate to `http://<pi-ip>:5000` from any browser on the same network.

## Polar table grid

Measurements are bucketed to the nearest grid point:

| Parameter | Values |
|-----------|--------|
| TWA | 35° – 180° in 5° steps |
| TWS | 4, 6, 8, 10, 12, 16, 20, 25, 30 kn (from preloaded polar) |
| Tolerance | TWA ± 3°, TWS ± half-step |

A cell is updated when:
- A session is active
- Motor detection is off (automatic or manual override)
- TWA and TWS fall within the cell's tolerance

The **90th percentile** of all measurements in a cell is used as the polar speed, filtering out slow outliers caused by bad trim or sea state.

## Sail configurations

The dropdown supports multiple sail plans. Each configuration builds its own independent polar:

- Full sail with jib
- Full sail with gennaker
- Full sail with parasailor
- 1st reef / 2nd reef / 3rd reef
- 2nd reef 60% jib / 3rd reef 60% jib
- Jib only / Gennaker only / Parasailor only

## Motor detection

The app detects motoring and suspends polar recording automatically:

- **TWA < 20°** — impossible to sail this close to the wind
- **STW > 1.5 × polar reference speed** — far exceeding theoretical maximum

A 5-minute hysteresis prevents false re-enables after stopping the engine. The toggle button in the UI lets you disable detection entirely when needed (e.g. motor-sailing in light air but still wanting to record data).

## Exporting to Predictwind

Click **Export to Predictwind** in the Backup & Restore section. The downloaded `.txt` file uses your measured polar where data exists, and falls back to the preloaded reference polar for cells not yet measured. Load it directly into Predictwind's custom polar editor.

## Preloaded polar format

The `polar_preloaded.txt` file is read at startup. To use a different boat's polar, replace the file and restart the service — no code changes needed.

Format (tab-separated):
```
4	0	0	45	1.5	52	1.91	60	2.17	...	180	1.76
6	0	0	45	2.29	...
```

## No external dependencies

`web_server.py` uses only the Python standard library — no Flask, no pip installs required. Runs comfortably on a Pi Zero with 512 MB RAM.

## License

MIT
