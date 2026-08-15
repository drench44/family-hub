# Running the wall on a Raspberry Pi (touch kiosk)

This turns a Raspberry Pi + a touchscreen into a seamless wall appliance:
it boots straight into the dashboard, fullscreen, no browser chrome, no
mouse cursor, and never sleeps. Copy-paste example files live in
[`docs/kiosk/`](kiosk/).

The hub itself runs on a separate always-on Linux box (see the main
[README](../README.md)). The Pi is just a display — it points Chromium at
your hub's URL. They can be the same machine, but a small dedicated Pi keeps
the wall independent of where the hub runs.

## Hardware

- **Raspberry Pi 4 or 5.** A Pi 5 (4GB is plenty) runs Chromium fullscreen
  smoothly; a Pi 4 works too.
- **A touchscreen.** Most portable/panel touchscreens need **three**
  connections, not one:
  1. **Video** — HDMI. Note the Pi 4/5 use **micro-HDMI** (the smallest
     type), *not* mini-HDMI — a mini plug won't seat. Use a micro-HDMI →
     HDMI cable/adapter, in the port **nearest the USB-C power** (HDMI0).
  2. **Touch** — a separate **USB** cable from the screen to the Pi. HDMI
     carries only the picture; touch is USB. This is the #1 "touch doesn't
     work" cause.
  3. **Power** — the screen's own supply. Power the screen from a wall
     charger, **not** from the Pi's USB ports, or you may brown out the Pi
     (undervoltage, random glitches).

## 1. Base OS

Flash **Raspberry Pi OS (Bookworm)** with Raspberry Pi Imager. In the
Imager's settings (gear icon) it's easiest to pre-set: hostname, your user +
password, Wi-Fi, **enable SSH**, and your locale. First boot then comes up
on the network ready to configure.

Set it to boot straight to the desktop, logged in:

```bash
sudo raspi-config nonint do_boot_behaviour B4   # desktop autologin
```

## 2. Use the X11 backend (important for cursor hiding)

Pi OS Bookworm defaults to a **Wayland** compositor (Wayfire/labwc), which
has no reliable "always hide the cursor" tool. The battle-tested kiosk path
is the **X11/Openbox** backend, where `unclutter` and `xset` just work:

```bash
sudo raspi-config nonint do_wayland W1   # W1 = X11, W2 = Wayfire, W3 = Labwc
```

(Interactively: `sudo raspi-config` → *Advanced Options* → *Wayland* → *X11*.)

## 3. Install the cursor-hider

```bash
sudo apt-get update
sudo apt-get install -y unclutter-xfixes
```

`unclutter-xfixes` (the modern fork) supports `--hide-on-touch`, which keeps
the cursor hidden even when a **touch tap** moves the pointer. Classic
`unclutter` flashes the arrow on every tap, so prefer the xfixes build.

## 4. Install the kiosk launcher + autostart

Copy [`docs/kiosk/kiosk.sh`](kiosk/kiosk.sh) to `~/kiosk.sh`, then **edit the
`URL=` line** to point at your hub (e.g. `http://192.168.1.50:8138`):

```bash
cp kiosk.sh ~/kiosk.sh
nano ~/kiosk.sh          # set URL="http://<your-hub>:8138"
chmod +x ~/kiosk.sh
```

Copy [`docs/kiosk/familyhub-kiosk.desktop`](kiosk/familyhub-kiosk.desktop) to
the XDG autostart folder and fix the path to your home:

```bash
mkdir -p ~/.config/autostart
cp familyhub-kiosk.desktop ~/.config/autostart/
sed -i "s#/home/YOUR_USER#$HOME#" ~/.config/autostart/familyhub-kiosk.desktop
```

## 5. Reboot

```bash
sudo reboot
```

It should boot to a black screen and then the dashboard, fullscreen, with no
cursor. What the launcher does:

- **`--kiosk`** — true fullscreen, no tabs or address bar.
- **`unclutter-xfixes --hide-on-touch`** — cursor hidden, even on tap.
- **`xset s off -dpms s noblank`** — the display never sleeps.
- **a `while` loop** — if Chromium ever crashes it relaunches automatically,
  so the wall self-heals.
- **`--disable-pinch --overscroll-history-navigation=0`** — touch gestures
  can't accidentally zoom the page or swipe "back" off the dashboard.

## Performance

A Pi 5 has plenty of headroom for this (idle temps, no throttling, gigabytes
of RAM free). If the wall feels sluggish, it's almost always Chromium not
using the GPU, **not** the hardware. The launcher already passes
`--ignore-gpu-blocklist --enable-gpu-rasterization --enable-zero-copy`.
To confirm acceleration is active, open `chrome://gpu` and look for
"Hardware accelerated" on the Graphics Feature Status list. Check thermals
and power with:

```bash
vcgencmd measure_temp      # display temperature
vcgencmd get_throttled     # 0x0 = healthy; non-zero = undervoltage/throttling
```

A non-zero `get_throttled` almost always means an underpowered supply — use
the official Pi supply, and don't power the touchscreen off the Pi.

## Troubleshooting

| Symptom | Cause / fix |
|---|---|
| Screen black / "no signal" | Wrong HDMI connector (Pi uses **micro**-HDMI, not mini), or plug into **HDMI0** (nearest USB-C) and reboot with it already connected. |
| Picture works, touch doesn't | The touch **USB** cable isn't connected to the Pi. HDMI is video only. |
| Cursor flashes on tap | You're using classic `unclutter`; install `unclutter-xfixes` and use `--hide-on-touch`. |
| Boots to desktop, no kiosk | Autologin not set (step 1), or the autostart `Exec=` path is wrong (must be your real home dir). |
| Random glitches / undervoltage | Screen is drawing power from the Pi — give the screen its own supply. |
| Hub loads on your PC but not the Pi | The Pi can't reach the hub — check they're on networks that route to each other; `curl http://<your-hub>:8138/health` from the Pi should return `200`. |
