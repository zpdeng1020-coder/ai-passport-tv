// WiFi setup for a device that has no network stored yet.
//
// The player's credentials used to be compiled in, which works for the one
// device they were built for and not for anyone else: an image published without
// them can never join a network. This module lets the device be told which
// network to use. It tries the stored one, and when there is none it raises its
// own access point with a configuration page.
//
// Provisioning and playback do not run at the same time. A session holds about
// 84 KB of internal RAM in large contiguous blocks, and the access point, web
// server and DNS responder need their own share of what is left; the two
// together do not fit on this chip. av_provision_start_ap() is therefore called
// with no session running, and the panel belongs to whoever is drawing -- this
// module never touches the display.
//
// The implementation is C++ because it is built on esp-wifi-connect, which is a
// C++ component. This header is the C boundary; nothing else in the firmware
// needs to be.
#pragma once
#include <stdbool.h>
#include <stddef.h>

#ifdef __cplusplus
extern "C" {
#endif

// Whether this build can provision at all. False when the component is compiled
// out, which keeps the original compiled-in-credentials behaviour available.
bool av_provision_available(void);

// Bring up the WiFi stack. Safe to call once, early, before the display is in
// use. Returns false if the stack could not be started, which is fatal for both
// playback and provisioning.
//
// `has_stored_network` receives whether a network is known after this call: a
// network saved by an earlier provisioning run, or the credentials compiled into
// this build when none were saved. When it is false the device has nothing to
// join and must be provisioned.
bool av_provision_init(bool *has_stored_network);

// Join the stored network. Returns false when there is nothing to join.
bool av_provision_join_stored(void);

// Keep the radio awake between beacons.
//
// The video stream is latency-sensitive: a modem that sleeps between beacons
// delivers packets in bursts with gaps that the player reads as a stalled
// stream and acts on by reconnecting. Playback therefore needs power saving off,
// which is what the firmware did before the WiFi stack moved behind this
// interface.
void av_provision_keep_radio_awake(void);

// Whether the station currently has an address on a network.
bool av_provision_connected(void);

// The name of the station's network, for the status page. Empty when not
// connected. Writes at most `size` bytes.
void av_provision_network_name(char *out, size_t size);

// Raise the configuration access point. Non-blocking. Playback should already
// have stopped, so the memory it held is available. Returns false if the access
// point could not be started.
bool av_provision_start_ap(void);

// Take the access point down and release the web server, DNS responder and
// sockets it holds. Safe to call when not provisioning.
//
// This does NOT bring the network connection back. Raising the access point
// stops the station (WifiManager::StartConfigAp), and stopping the access point
// does not restart it (StopConfigAp only stops the AP), so a device that leaves
// setup without calling av_provision_join_stored() is left with its radio idle
// and never rejoins. Leaving setup therefore means calling both.
void av_provision_stop_ap(void);

// Whether the configuration access point is up.
bool av_provision_ap_running(void);

// The access point's name, for the screen that tells the user what to join.
// Writes at most `size` bytes.
void av_provision_ap_name(char *out, size_t size);

// The address of the configuration page.
void av_provision_ap_url(char *out, size_t size);

// Whether a phone has finished joining the access point and been given an
// address. Joining the access point and getting an address from it are separate
// steps, and only the second means the page can be opened.
bool av_provision_client_ready(void);

// Whether a network has been stored by the setup page since this call to
// av_provision_start_ap(). This is what ends setup: the page stores credentials
// only after checking that the device can actually join the network with them,
// so a stored network means the job is done.
bool av_provision_has_network(void);

// Called from the WiFi event task when the station loses a network it had. The
// callback must not block.
void av_provision_on_disconnected(void (*callback)(void));

// Called when the station has an address on a network.
void av_provision_on_connected(void (*callback)(void));

#ifdef __cplusplus
}
#endif
