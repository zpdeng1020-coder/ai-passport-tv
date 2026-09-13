// Remembering the volume and the backlight across power cycles.
//
// Without this the two settings are lost every time the device is switched off,
// which makes adjusting them close to pointless. They are small and written
// rarely, so they live in the existing NVS partition.
//
// This uses its own namespace. The provisioning component keeps the WiFi
// credentials in "wifi" and reads them at start-up; sharing that namespace would
// put two unrelated writers on the same keys.
#pragma once
#include <stdbool.h>
#include <stdint.h>

#include "av_server_addr.h"

// Load the stored settings. Safe to call when nothing has been stored, which is
// what a device that has never been adjusted looks like: the defaults are
// written to the outputs and the call still succeeds.
//
// `nvs_flash_init` must already have run. That happens as part of bringing up
// the WiFi stack, which the player does before it touches either setting.
bool av_store_load(uint8_t *volume_percent, uint8_t *brightness_percent);

// Save both settings. Returns false when the write failed, which the caller can
// report but does not need to act on: the values still apply for this session.
bool av_store_save(uint8_t volume_percent, uint8_t brightness_percent);

// Read where the streaming server is, as entered on the setup page.
//
// Returns false when nothing usable has been stored, which is the case for a
// device that has never been set up. The address is stored by the provisioning
// component -- the setup page's server field and its storage belong to it -- so
// this reads that value and parses it; it does not write.
bool av_store_server_addr_load(av_server_addr_t *addr);
