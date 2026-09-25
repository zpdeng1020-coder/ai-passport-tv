#include "av_channel_policy.h"

#include <string.h>

// Length of a channel id, bounded, without strnlen.
//
// strnlen is POSIX rather than C, so glibc hides it behind a feature-test macro
// and a strict -std=c11 build does not see it at all. That is a fault this
// repository has already had once, in ui_menu.c, where it passed locally on
// macOS and failed every Linux CI run.
static size_t bounded_length(const char *id)
{
    size_t length = 0;
    while (length < AV_CHANNEL_POLICY_ID_MAX && id[length] != '\0') {
        length++;
    }
    return length;
}

// A usable id is non-empty and terminated inside the field. Reaching the field's
// end without a terminator means the id does not fit, and truncating it would be
// worse than refusing: two different channels could then share one mark.
static bool id_usable(const char *id)
{
    if (id == NULL) {
        return false;
    }
    size_t length = bounded_length(id);
    return length > 0 && length < AV_CHANNEL_POLICY_ID_MAX;
}

static int find(const av_channel_policy_t *policy, const char *channel)
{
    for (unsigned i = 0; i < policy->count; i++) {
        if (strcmp(policy->id[i], channel) == 0) {
            return (int)i;
        }
    }
    return -1;
}

void av_channel_policy_reset(av_channel_policy_t *policy)
{
    memset(policy, 0, sizeof(*policy));
}

bool av_channel_policy_has_shown(const av_channel_policy_t *policy, const char *channel)
{
    if (!id_usable(channel)) {
        return false;
    }
    return find(policy, channel) >= 0;
}

void av_channel_policy_note_picture(av_channel_policy_t *policy, const char *channel)
{
    if (!id_usable(channel)) {
        return;
    }
    if (find(policy, channel) >= 0) {
        return;   // already known; do not consume a second slot
    }
    // A full set drops its oldest mark rather than refusing the new one. The
    // alternative -- keeping the first sixteen for ever -- would mean a viewer
    // who has watched many channels finds the ones they are actually using now
    // unprotected, which is the wrong end of the set to lose.
    unsigned slot;
    if (policy->count < AV_CHANNEL_POLICY_MAX) {
        slot = policy->count++;
    } else {
        slot = policy->next;
        policy->next = (policy->next + 1u) % AV_CHANNEL_POLICY_MAX;
    }
    memcpy(policy->id[slot], channel, bounded_length(channel) + 1u);
}

bool av_channel_policy_should_skip(av_channel_policy_t *policy,
                                   const char *channel,
                                   bool server_answered,
                                   bool showed_picture,
                                   unsigned *streak,
                                   unsigned limit,
                                   unsigned channel_count)
{
    // A picture is the strongest evidence there is, so it both clears the count
    // and marks the channel. Recording here rather than at the call site is
    // deliberate: the two answers have to agree, and one function is the only
    // way to guarantee it.
    if (showed_picture) {
        av_channel_policy_note_picture(policy, channel);
        *streak = 0;
        return false;
    }
    // Never reaching the server is not a property of this channel.
    if (!server_answered) {
        *streak = 0;
        return false;
    }
    // The channel has produced a picture at some point, so it is a working
    // channel having a bad moment, not a dead entry. Retrying it is the whole
    // point: a source that is merely unstable is the ordinary case, and stepping
    // away from it loses the viewer's place and their choice along with it.
    if (av_channel_policy_has_shown(policy, channel)) {
        *streak = 0;
        return false;
    }
    // Nothing to step to, so the count stays put rather than climbing towards a
    // limit that could never be acted on.
    if (channel_count <= 1u) {
        return false;
    }
    if (++(*streak) < limit) {
        return false;
    }
    *streak = 0;
    return true;
}
