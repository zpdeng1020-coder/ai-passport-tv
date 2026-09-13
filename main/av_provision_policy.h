// The decisions around setup mode, kept apart from the WiFi stack so they can be
// tested on a host. Nothing here touches ESP-IDF or the component.
#pragma once
#include <stdbool.h>
#include <stdint.h>

// What the firmware should do when it starts.
typedef enum {
    // A network is known: join it and play.
    AV_BOOT_PLAY = 0,
    // No network is known: raise the setup access point and wait to be told one.
    AV_BOOT_SETUP,
} av_boot_mode_t;

// How long the setup access point stays up with nobody completing it.
//
// It is an open access point, so leaving it up indefinitely means leaving an
// unauthenticated network in the room for as long as the device has power.
// Long enough for someone to find the network, open the page and type a
// password; not long enough to be abandoned without limit.
#define AV_SETUP_WINDOW_MS (15u * 60u * 1000u)

// Which mode to start in.
//
// `has_network` means a network is known from the setup page's store or from the
// credentials compiled into this build. `server_configured` means somewhere to
// stream from is known, from the setup page's store or from this build.
//
// Both are collected by the setup page, so either missing sends the device
// there. A device that can neither join a network nor find a server has nothing
// to show and no other way to be told, which is the state a published build
// starts in.
av_boot_mode_t av_boot_mode(bool has_network, bool server_configured);

// Whether a setup session that began at `started_ms` has run out of time.
// A `limit_ms` of zero means no limit, which is what a caller uses when it wants
// setup to stay until it is finished or cancelled.
bool av_setup_expired(uint32_t started_ms, uint32_t now_ms, uint32_t limit_ms);
