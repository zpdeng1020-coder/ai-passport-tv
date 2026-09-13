// Backlight levels and the rules for stepping between them.
//
// Kept apart from the hardware so the arithmetic can be tested on the host: the
// interesting part is not writing a PWM register, it is that stepping up from
// the brightest level and down from the dimmest both have to land somewhere
// sensible rather than falling off the end.
#pragma once
#include <stdbool.h>
#include <stdint.h>

// Levels are a fixed set of steps rather than a continuous percentage.
//
// Continuous would let the viewer reach zero, which turns the panel black and
// takes away the very screen they would need to raise it again. Steps also make
// the useful range reachable in a press or two instead of a long hold, because
// the range that matters for reading in a dark room or by a window is only a few
// values wide.
extern const uint8_t AV_BRIGHTNESS_LEVELS[];
extern const unsigned AV_BRIGHTNESS_COUNT;

// Defaults for a device that has never been adjusted: the values this firmware
// used before either setting could be changed or remembered, so a device with no
// stored settings behaves exactly as it did before.
#define AV_BRIGHTNESS_DEFAULT_PERCENT 40u
#define AV_VOLUME_DEFAULT_PERCENT 55u

// The percentage for `index`, clamped into the table, so a stored value from a
// build with a different number of levels still yields a usable setting.
uint8_t av_brightness_percent(unsigned index);

// The index of the level closest to `percent`, for reading a stored value back.
unsigned av_brightness_nearest_index(uint8_t percent);

// Step to the next level. `up` is brighter, otherwise dimmer. Both ends wrap, so
// a held button cycles rather than sticking.
unsigned av_brightness_step(unsigned index, bool up);

// Whether `percent` is one of the levels, used to reject a corrupted store
// rather than applying it.
bool av_brightness_valid_percent(uint8_t percent);
