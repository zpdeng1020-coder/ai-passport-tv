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

The computer stays on while you watch. It is what converts the television signal into something the device can display. Switch it off and the device sits on the waiting screen.

## How it works

The device cannot decode video on its own, and no amount of firmware work changes that. So the computer does the real work: it pulls one channel from the internet, converts it into pictures and sound the device can use directly, and sends them over the local network. The device draws the pictures and plays the sound.

That explains a few things:

- **Changing channel takes several seconds.** The computer starts converting again from scratch.
- **The computer has to stay on.** It is the actual signal source.
- **Channels come from public community streams**, fetched by the computer. They go dead, and they come back.

## Step 1: Flash the firmware

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

On the computer, in this repository's directory:

- **macOS / Linux:**

  ```sh
  ./run.sh
  ```

- **Windows:** double-click `run.bat`

The first run reports anything missing. Two things are needed: Python 3.9 or newer, and ffmpeg. If ffmpeg is absent it prints the install command for your system; install it once and it is done.

It prints two things: the address it is listening on, and the address to enter on the device.

**Its messages are in Chinese** — the program is written for people who read Chinese, and the Chinese README is the main one. The line to look for is the one ending in a `name:port` pair, and the line under it is what you copy:

```
Serving on 192.168.1.20:8096

Server address to enter on the device:
    my-laptop.local:8096
```

(The real output says those two English lines in Chinese. The `name:port` value is the part that matters and it is identical either way.)

**Note that `name:port` line down** — it goes into the device in the next step. It is this computer's own name, so it keeps working when the router hands out a different address.

The same window also prints a "channel page" address, used later to change which channels are offered.

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

## What has not been tested

- **`run.bat` has never been run on a real Windows machine.** Development happened on macOS only. It is deliberately thin — it locates Python and hands over to `tools/launch.py` — but treat it as unverified.
- Linux has been checked by reading the code, not by running it on a machine.

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
