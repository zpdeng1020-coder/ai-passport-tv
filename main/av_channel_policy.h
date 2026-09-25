// Which channel to try next when one will not produce a picture.
//
// Kept apart from the player so it can be tested on a host: the decision is
// three booleans and a counter, but getting it wrong is not visible in a build.
// A device that gives up on a working channel looks exactly like a device whose
// source has gone down, and the viewer has no way to tell which happened.
#pragma once
#include <stdbool.h>
#include <stddef.h>

// Channel ids kept as "has shown a picture at least once".
//
// A set rather than one id, because the case worth protecting is a return trip:
// a viewer who watches one channel, watches a second, and comes back to the
// first must not find it treated as unproven. Sixteen is a budget, not a
// measurement -- it is 256 bytes, and a viewer who has watched more than sixteen
// channels since power-on has already left the earliest one behind. When the set
// fills, the oldest mark is dropped.
#define AV_CHANNEL_POLICY_MAX 16u
// Must equal AV_CHANNEL_ID_MAX; av_player.c asserts that it does.
#define AV_CHANNEL_POLICY_ID_MAX 16u

typedef struct {
    char id[AV_CHANNEL_POLICY_MAX][AV_CHANNEL_POLICY_ID_MAX];
    unsigned count;   // marks held, never above MAX
    unsigned next;    // where a new mark goes once count reaches MAX
} av_channel_policy_t;

// Forget every mark. For tests and for a fresh channel table.
void av_channel_policy_reset(av_channel_policy_t *policy);

// Whether this channel has ever produced a picture since the marks were reset.
bool av_channel_policy_has_shown(const av_channel_policy_t *policy, const char *channel);

// Remember that this channel produced a picture. An id that does not fit the
// field is refused rather than truncated, because a truncated id could match a
// different channel and mark the wrong one as proven.
void av_channel_policy_note_picture(av_channel_policy_t *policy, const char *channel);

// Whether to step to the next channel after a session ended.
//
// *streak carries the count of consecutive fruitless sessions between calls and
// is owned by the caller; this function resets or advances it. `limit` is how
// many such sessions are tolerated first, and differs between a channel the
// viewer chose and one reached automatically, because stepping away from a
// deliberate choice reads as the button having done nothing.
//
// `server_answered` separates a broken channel from a broken connection. A
// session that never reached the server says nothing about the channel: every
// channel fails the same way, so stepping would walk the whole table while the
// viewer watches the name change. It also has to stay put for a second reason --
// the waiting screen is what the status page is drawn over, and that page is the
// only route to the setup screen where a wrong address is corrected.
//
// `showed_picture` is this session's answer, and it also records the mark: a
// channel that has just proven itself is entered into the set here, which keeps
// the two from drifting apart at the call site.
bool av_channel_policy_should_skip(av_channel_policy_t *policy,
                                   const char *channel,
                                   bool server_answered,
                                   bool showed_picture,
                                   unsigned *streak,
                                   unsigned limit,
                                   unsigned channel_count);
