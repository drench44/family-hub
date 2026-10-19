# A used all-in-one PC as the wall

This is what our own wall runs on, and the setup we'd recommend first. An
all-in-one touchscreen PC is one box: screen, touch, speakers, Wi-Fi and power
are already built in. There's nothing to wire up or mount behind the panel.
Used business and home all-in-ones are easy to find, and one is often cheaper
than a Raspberry Pi plus a decent touchscreen, and faster too.

If you'd rather build a Pi appliance, see
[raspberry-pi-kiosk.md](raspberry-pi-kiosk.md).

## What we run

| Part | Ours |
|---|---|
| Machine | HP All-in-One 24 (24-cb1xxx), bought used for $150 |
| Screen | 23.8", 1920×1080, 10-point touch. The wall layout is built for exactly this size |
| CPU | Intel Core i3-1215U (12th gen) |
| Memory | 16 GB DDR4 (it came with one 8 GB stick; see below) |
| OS | Debian 13 with the GNOME desktop |
| Browser | Firefox ESR, full screen |

The hub itself runs on a separate server. The all-in-one only shows it in a
browser, so it doesn't need to be powerful. (It can run the hub too, if you
don't have another always-on machine.)

## What to look for when buying

- **Touch.** Check the listing says touchscreen. Many all-in-ones are sold in
  touch and non-touch versions that look the same.
- **1920×1080 or larger.** The wall layout is designed at 1920 wide.
- **An Intel or AMD CPU from the last several years** and at least 8 GB of
  memory. Anything that plays YouTube smoothly is plenty.
- **Two memory slots** if you can. The built-in graphics share system memory,
  so a second stick (two matched sticks run in dual channel) makes the screen
  noticeably smoother. Ours came with one 8 GB stick and an empty slot; a used
  matching stick fixed that cheaply.

## Setup tips (learned the hard way)

These are specific to all-in-ones running a desktop Linux. Most apply to any
always-on wall screen.

### 1. Turn sleep off completely

Some all-in-ones never wake back up from suspend. Ours didn't: it went to sleep
overnight and stayed dark until it was power-cycled. Turn off every kind of
sleep, not just the desktop setting:

```bash
sudo systemctl mask sleep.target suspend.target hibernate.target hybrid-sleep.target
sudo mkdir -p /etc/systemd/logind.conf.d
printf '[Login]\nIdleAction=ignore\nHandleSuspendKey=ignore\nHandleLidSwitch=ignore\nHandleHibernateKey=ignore\n' \
  | sudo tee /etc/systemd/logind.conf.d/no-sleep.conf
```

In GNOME, also turn off screen blanking and the lock screen
(Settings → Power: Screen Blank = Never, Automatic Suspend = Off; Settings →
Privacy → Screen Lock: off).

### 2. Dim by brightness, never by "screen off"

On ours, a real display power-off (screensaver blanking, DPMS) left the panel
dark for good: it never woke up again. Setting the backlight brightness to 0
goes just as dark and comes back instantly. So the nightly "sleep" is only a
brightness change, and the machine and dashboard stay running all night.

The backlight lives in sysfs. A tiny script and a timer are enough:

```bash
# /usr/local/bin/wall-brightness.sh <0-100>
#!/bin/bash
BL=$(ls -d /sys/class/backlight/* | head -1)
MAX=$(cat "$BL/max_brightness")
echo 0 > "$BL/bl_power" 2>/dev/null   # clear a stuck power-down
echo $(( MAX * $1 / 100 )) > "$BL/brightness"
```

Run it as root from cron or a systemd timer. Ours goes to 100% at 6am, 60% at
6pm and 0 at 10pm. On our panel, anything from about 86% to 100% looks the
same, so a visible evening dim needs to be well below that.

### 3. Open the browser full screen at login

- Turn on automatic login for the wall user, so a power cut or reboot comes
  back to the dashboard with no password prompt.
- Add a login item (in GNOME, a `.desktop` file in `~/.config/autostart/`)
  that starts the browser on the hub's address.
- Set the browser's home page to the hub, turn off "restore previous session",
  and turn off pinch-zoom so a stray two-finger touch can't zoom the wall.

We use Firefox in full screen rather than a locked kiosk mode, so the family can
still open a YouTube tab and come back. F11 (on a keyboard) shows the tabs.

Make the home page `http://<your-hub>/?kiosk=1`. That turns on the hub's
on-screen keyboard and pointer hiding for the wall; see
[on-screen-keyboard.md](on-screen-keyboard.md).

### 4. Cameras in Firefox

The camera tiles use H.264 over WebRTC. Firefox needs its OpenH264 plugin for
that. It normally downloads it by itself; check that "OpenH264 Video Codec"
is listed and enabled under Add-ons and themes → Plugins. Chrome and Chromium
play H.264 without a plugin.

### 5. Power on after an outage

In the BIOS, set the "after power loss" option to **Power On**, so the wall
comes back by itself after a power cut. On ours the touchscreen doesn't work
in the BIOS, so plug in a USB keyboard for this one step.

## Touch quirk worth knowing

On our all-in-one under GNOME on Wayland, the touchscreen reaches Firefox as
the mouse pointer, so the page can't tell it's a touchscreen. That's why the
wall needs `?kiosk=1` instead of detecting touch, and why GNOME's own touch
keyboard is not used. Details are in
[on-screen-keyboard.md](on-screen-keyboard.md).
