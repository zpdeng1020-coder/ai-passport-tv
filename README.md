English | [简体中文](README.zh_CN.md)

# Live TV on the AI Passport

Turns a FoloToy AI Passport into a small network television. Three buttons on the device are enough to change channels, adjust volume and brightness, and choose which channels it offers.

As with the official firmware, the device's factory identity is preserved; flashing does not touch it.

## What you need

| Thing | Notes |
| --- | --- |
| **An AI Passport** | Same hardware, different firmware |
| **A computer** | Any ordinary laptop or desktop — Windows, macOS or Linux. Called "the computer" below |
| **The same WiFi** | The device and the computer on one network |
| **A USB cable** | For flashing; unplug it afterwards |

The computer stays on while you watch. It is what converts the television signal into something the device can display. Switch it off and the device sits on the waiting screen. Nothing has to be installed on it beforehand -- the download in the next step brings what it needs.

## How it works

The device cannot decode video on its own, and no amount of firmware work changes that. So the computer does the real work: it pulls one channel from the internet, converts it into pictures and sound the device can use directly, and sends them over the local network. The device draws the pictures and plays the sound.

That explains a few things:

- **Changing channel takes several seconds.** The computer starts converting again from scratch.
- **The computer has to stay on.** It is the actual signal source.
- **Channels come from public community streams**, fetched by the computer. They go dead, and they come back.

## Step 1: Flash the firmware

**The easy way needs no software installed** — it runs in the browser:

1. Open <https://zpdeng1020-coder.github.io/ai-passport-tv/flash/> in **Chrome or Edge**
2. Plug the device in
3. Press the button and pick the device's serial port when asked
4. **Hold the power button for about 2 seconds to switch off, then hold it for about half a second to switch on again**

The device has a battery, so unplugging the cable will not restart it. Skip step 4 and the screen stays dark.

That page writes only the region the firmware occupies and **never erases the device**; it checks the write range itself and refuses to start if the range would reach the identity data.

---

Or flash it by hand — same result:

1. Download **`FoloToy-AI-Passport-tv.bin`** from this repository's **Releases** page.

   It contains the bootloader, the partition table and the application, and is written in one go from address `0` — you do not have to work out offsets.

2. Install a flashing tool. With Python, this is the short way:

   ```sh
   pip install esptool
   ```

3. Plug the device in and flash it:

   ```sh
   esptool.py --chip esp32c3 --port <your serial port> write_flash 0x0 FoloToy-AI-Passport-tv.bin
   ```

   On macOS `<your serial port>` looks like `/dev/cu.usbmodem101`, on Windows like `COM3`, on Linux like `/dev/ttyUSB0`. List the serial ports before and after plugging the device in; the new one is it.

> **Never use `erase-flash`.** The device holds factory-written identity data that cannot be recovered once erased. The command above writes from `0` to about `0x182000` (1.5 MB), while that identity data sits at `0x356000` — the distance between them is what makes it safe. `erase-flash` clears the whole chip, including that region.

## Step 2: Start the service

One program runs on the computer. It does two jobs: it pulls a television channel off the internet and converts it into something the device can display, and it serves a small web page for choosing which channels are offered. There are two ways to get it, and they do exactly the same thing.

### Download one file

Go to this repository's **Releases** page and take the one for your computer:

| Your computer | Download |
| --- | --- |
| **Windows** | `tv-server-windows-amd64.exe` |
| **Mac** (M-series chip) | `tv-server-macos-arm64.zip` |
| **Linux** | `tv-server-linux-x86_64.zip` |

The Mac and Linux downloads are archives -- **unzip them before running**. The archive is what keeps the "executable" marking on the file: sent as a bare download that marking is lost on the way, and the system refuses to open it.

> **Intel Macs have no ready-made file** — use the "run it from the source" route below. The build machines are all Apple silicon and cannot produce an Intel binary. Saying so is better than offering a download that will not start.

Put it in an empty folder and double-click it. **Nothing needs to be installed first** — the first run fetches the video converter it needs (about 21–31 MB, once, and never again).

The first time you open it, the system stops it. That is not a sign of a damaged file: this project has no code-signing certificate, so the system cannot identify it. To get past it:

**Windows**: click "More info", then "Run anyway".

**macOS**: a dialog appears saying "tv-server-macos-arm64" was not opened. The buttons are in your system language; the labels below are the English ones, and the left-hand and right-hand buttons are the same two whichever language they are in.

1. Click the **left-hand** of the two buttons ("Done"). **Do not click the right-hand one** ("Move to Trash"): that deletes the program, it does not get past the block.
2. Open **System Settings → Privacy & Security**, scroll down, and find the entry saying "tv-server-macos-arm64" was blocked to protect the Mac.
3. Click **Open Anyway** — the only button on that row — and confirm once.

You only have to do this the first time; after that it opens normally.

### Or run it from the source

With Python and ffmpeg already installed, clone the repository and run:

```sh
./run.sh          # macOS / Linux
```

On Windows, double-click `run.bat`. Anything missing is reported with the install command for your system.

### What you will see

**The program's own messages are in Chinese** — it is written for people who read Chinese, and the Chinese README is the main one. What follows is that output rendered in English so you know what to look for; the real lines say the same things in Chinese, and the parts worth copying — addresses, ports, paths — are identical either way.

```
Data directory: /Users/you/Downloads/ai-passport-tv
Channel page: http://127.0.0.1:8097
  Open this in a browser on this computer to choose and reorder channels.

Listening on 192.168.1.20:8096

Server address to enter on the device:
    my-laptop.local:8096
```

The line worth copying is the one under the last heading — the `name:port` pair. **Note it down**, because it goes into the device in the next step. It is this computer's own name, so it keeps working when the router hands out a different address. There is a fallback address on the following lines, in the form `192.168.x.x:8096`, for a network where the name does not resolve.

The first line names the folder where the channel list is kept: the program's own folder, or the per-user location when that folder cannot be written to. The channel page address is what you open later to change which channels are offered.

### If the device cannot connect, check the firewall first

**Windows blocks incoming connections by default.** The channel page opens fine in a browser on that computer while the device cannot reach the server at all — and the device only says it cannot connect, which says nothing about a firewall. This is the most common thing to go wrong.

The first time you run it, Windows usually asks whether to allow the program through the firewall: tick **private networks**. If that prompt was dismissed, or never appeared, add the rule by hand:

```powershell
# In an administrator PowerShell
New-NetFirewallRule -DisplayName "AI Passport TV" -Direction Inbound -Protocol TCP -LocalPort 8096 -Action Allow -Profile Private
```

macOS and Linux normally need nothing here.

## Step 3: Connect the device

On its first boot the device enters setup by itself, showing colour bars and a network name like `FoloToy-XXXX`.

1. **Join that network from a phone** (no password). The phone may warn that there is no internet access — choose to stay connected.
2. **Open `192.168.4.1` in a browser.**
3. Fill in two things:
   - your **home WiFi name and password**
   - the **server address**: the line you noted down, e.g. `my-laptop.local:8096`
4. Press connect. The device restarts, the hotspot disappears, and a picture appears after a few seconds.

If it does not enter setup by itself, or you need to change the address later: **double-click ↑** for the status page, then **hold ↑** on that page.

## Day-to-day use

Three buttons, six gestures:

| Action | What it does |
| --- | --- |
| **Short press ↑ / ↓** | Change channel |
| **Long press ↑ / ↓** | Adjust volume, in steps of 10% |
| **Short press OK** | Open the channel list (↑↓ to move, OK to choose) |
| **Long press OK** | Adjust brightness |
| **Double-click ↑** | Status page: current channel, WiFi signal, battery |
| **Hold ↑ on the status page** | Enter setup again |

Volume and brightness are remembered across power cycles.

## Changing channels

The channel list can be changed without reflashing anything.

Starting the service prints a "channel page" address, like `http://127.0.0.1:8097`. **Open it in the browser on that same computer.** You will see:

- on the left, channels found in the live-stream source
- on the right, the channels the device currently has

Add, remove and reorder, then save. The service restarts itself, the device reconnects, and the new list takes effect.

There is also a button that checks every channel and marks the ones that no longer play. On the page it is labelled in Chinese, like the rest of the interface — it is the one next to Save. Public streams do die; press it now and then and drop the dead ones.

The channel list is a plain text file, `channels.txt`. Editing it by hand works just as well — restart the service afterwards. The two ways are equivalent.

## When something goes wrong

**The device says it cannot reach the server**

The service is not running, or the address is wrong. Double-click ↑, then hold ↑, and enter it again. Prefer the computer's name (`my-laptop.local:8096`) over its IP address.

**The picture freezes, or it stays on "connecting"**

Look at the service window on the computer for an error. Streams do go dead — use the channel page's check button, or try another channel.

**The screen flickers and the channel name jumps rapidly**

The current channel has gone dead and the device is working through the list. It settles after a moment, or you can change channel by hand.

**The setup page will not open**

The phone may have switched back to the home WiFi after joining `FoloToy-XXXX`. Turn off auto-join, or switch back manually.

**Changing channel is slow**

That is expected. Every channel change means the computer starts pulling and converting again. Network streams are the slowest case.

## How far this has been tested

Saying what has actually been run, and what has not, is more useful than a general assurance.

**Run for real**

- **Windows 11, end to end**: the downloaded `.exe` was started on a machine with no Python and no ffmpeg. It fetched ffmpeg itself, brought up both services, and served all 127 channels. That was a real machine, not a simulation.
- **macOS, end to end**: built, started, streamed, and the channel list was saved and picked up after the automatic restart.
- The server is built by CI on Linux, macOS and Windows, and each build is started once as a smoke test before it is published.

**Not verified**

- **Nobody has used this on Linux.** The build and the tests pass there in CI, but a CI runner is not a desktop and no one has clicked through it.
- **The Intel macOS build has never run on an Intel Mac**; it is only built in CI.
- Apple's system may block an unsigned program, and how that looks varies by macOS version and settings. The paragraph above describes the common case.

**To be expected**

- Public streams go dead. That is the normal state of things, not a fault — use the channel page's check button to drop the dead ones.
- Changing channel takes several seconds, because the computer starts converting again from the beginning.

## Building the firmware yourself

Requires ESP-IDF 5.5.3. From the repository root:

```sh
./tools/validate.sh --prototype
```

This builds, verifies, and writes the result to `build/FoloToy-AI-Passport-prototype-public.bin` — the full image flashed in step 1.

All configuration happens on the device's own setup page; nothing is compiled in.

## Licence

Based on the [official FoloToy AI Passport firmware](https://github.com/folotoy/ai-passport) (MIT). The hardware design belongs to FoloToy.

The server needs [ffmpeg](https://ffmpeg.org/), a separate program you install yourself. Channel addresses come from public community streams; this repository hosts no content and makes no promise about their availability.

For personal use at home.
