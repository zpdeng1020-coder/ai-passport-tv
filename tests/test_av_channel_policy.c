// When to give up on a channel, tested without a device.
//
// This is the rule behind a fault a viewer reported: a channel that was playing
// would drop out for a moment, and the device would leave it and move on. The
// count of fruitless sessions said "this channel has produced nothing twice",
// which is also what a channel with a dead source looks like -- so an unstable
// one was being thrown away along with the viewer's choice.
//
// What separates the two is whether the channel ever worked. That is what these
// cases pin down, along with the earlier rule it must not undo: a channel with
// nothing behind it still gets stepped over, or the viewer is trapped on the
// test pattern.
#include "av_channel_policy.h"

#include <assert.h>
#include <stdio.h>
#include <string.h>

#define AUTO_LIMIT 2u
#define USER_LIMIT 4u

// One session, and the answer to "step to the next channel".
static bool session(av_channel_policy_t *policy, const char *channel,
                    bool server_answered, bool showed_picture,
                    unsigned *streak, unsigned limit, unsigned count)
{
    return av_channel_policy_should_skip(policy, channel, server_answered,
                                         showed_picture, streak, limit, count);
}

static void test_a_channel_that_showed_is_never_abandoned(void)
{
    // The reported fault. The channel plays, then stutters repeatedly. Every one
    // of these sessions ends without a picture, and the old rule stepped away
    // after the second.
    av_channel_policy_t proven;
    av_channel_policy_reset(&proven);
    unsigned streak = 0;

    assert(!session(&proven, "ch001", true, true, &streak, AUTO_LIMIT, 5));
    for (int attempt = 0; attempt < 10; attempt++) {
        assert(!session(&proven, "ch001", true, false, &streak, AUTO_LIMIT, 5));
    }
}

static void test_a_channel_that_never_showed_is_still_skipped(void)
{
    // The fault the original rule was written for, which must survive: a dead
    // entry would otherwise leave the viewer on the test pattern for ever.
    av_channel_policy_t proven;
    av_channel_policy_reset(&proven);
    unsigned streak = 0;

    assert(!session(&proven, "ch002", true, false, &streak, AUTO_LIMIT, 5));
    assert(session(&proven, "ch002", true, false, &streak, AUTO_LIMIT, 5));
}

static void test_the_count_restarts_after_a_picture(void)
{
    // A channel that fails once, works, then fails once is not on its second
    // strike. Without the reset the count would accumulate across good sessions
    // and a working channel would eventually be skipped.
    av_channel_policy_t proven;
    av_channel_policy_reset(&proven);
    unsigned streak = 0;

    assert(!session(&proven, "ch003", true, false, &streak, AUTO_LIMIT, 5));
    assert(!session(&proven, "ch003", true, true, &streak, AUTO_LIMIT, 5));
    assert(!session(&proven, "ch003", true, false, &streak, AUTO_LIMIT, 5));
}

static void test_never_reaching_the_server_is_not_the_channels_fault(void)
{
    // Every channel fails identically against a wrong address, so stepping
    // would walk the whole table while the viewer watches the name change. The
    // waiting screen also has to stay up: the status page is drawn over it, and
    // that page is the only way to the setup screen.
    av_channel_policy_t proven;
    av_channel_policy_reset(&proven);
    unsigned streak = 0;

    for (int attempt = 0; attempt < 20; attempt++) {
        assert(!session(&proven, "ch004", false, false, &streak, AUTO_LIMIT, 5));
    }
}

static void test_a_channel_the_viewer_chose_gets_longer(void)
{
    // Three fruitless sessions are not enough to step away from a deliberate
    // choice; the fourth is. Below the limit the count carries, at the limit it
    // is spent.
    av_channel_policy_t proven;
    av_channel_policy_reset(&proven);
    unsigned streak = 0;

    assert(!session(&proven, "ch005", true, false, &streak, USER_LIMIT, 5));
    assert(!session(&proven, "ch005", true, false, &streak, USER_LIMIT, 5));
    assert(!session(&proven, "ch005", true, false, &streak, USER_LIMIT, 5));
    assert(session(&proven, "ch005", true, false, &streak, USER_LIMIT, 5));
    // And the count is spent, not left at the limit: the next channel starts
    // from zero rather than being stepped over on its first bad session.
    assert(!session(&proven, "ch005", true, false, &streak, USER_LIMIT, 5));
}

static void test_a_list_of_one_is_never_stepped_over(void)
{
    // There is nowhere to go, so the count must not climb towards a limit that
    // could never be acted on. Leaving it climbing would mean that adding a
    // second channel later found the count already spent.
    av_channel_policy_t proven;
    av_channel_policy_reset(&proven);
    unsigned streak = 0;

    for (int attempt = 0; attempt < 10; attempt++) {
        assert(!session(&proven, "ch006", true, false, &streak, AUTO_LIMIT, 1));
    }
    assert(streak == 0);
    // Proving the set was left alone as well: a second channel has not been
    // marked by any of the above.
    assert(!av_channel_policy_has_shown(&proven, "ch006"));
}

static void test_proof_survives_leaving_and_returning(void)
{
    // The reason this is a set and not a single id. A viewer who watches one
    // channel, watches another, and comes back must find the first one still
    // protected. With one remembered id the mark would have been overwritten and
    // the original channel abandoned on its next stutter.
    av_channel_policy_t proven;
    av_channel_policy_reset(&proven);
    unsigned streak = 0;

    assert(!session(&proven, "chA", true, true, &streak, AUTO_LIMIT, 5));
    assert(!session(&proven, "chB", true, true, &streak, AUTO_LIMIT, 5));
    assert(av_channel_policy_has_shown(&proven, "chA"));
    assert(av_channel_policy_has_shown(&proven, "chB"));
    // Back to A, now stuttering.
    for (int attempt = 0; attempt < 10; attempt++) {
        assert(!session(&proven, "chA", true, false, &streak, AUTO_LIMIT, 5));
    }
}

static void test_the_oldest_mark_is_dropped_when_the_set_fills(void)
{
    // Bounded memory. The mark that goes is the oldest, because a viewer who has
    // watched more channels than the set holds has left the earliest one behind.
    av_channel_policy_t proven;
    av_channel_policy_reset(&proven);

    char id[AV_CHANNEL_POLICY_ID_MAX];
    for (unsigned i = 0; i < AV_CHANNEL_POLICY_MAX; i++) {
        snprintf(id, sizeof(id), "c%u", i);
        av_channel_policy_note_picture(&proven, id);
    }
    assert(av_channel_policy_has_shown(&proven, "c0"));
    assert(proven.count == AV_CHANNEL_POLICY_MAX);

    // One more, which must displace the oldest rather than be refused.
    av_channel_policy_note_picture(&proven, "new");
    assert(av_channel_policy_has_shown(&proven, "new"));
    assert(!av_channel_policy_has_shown(&proven, "c0"));
    assert(av_channel_policy_has_shown(&proven, "c1"));
    // Still bounded.
    assert(proven.count == AV_CHANNEL_POLICY_MAX);
}

static void test_marking_twice_does_not_consume_two_slots(void)
{
    // A channel watched for hours reports a picture every session. If each
    // report claimed a slot, the set would hold one channel and sixteen copies
    // of it, and every other channel would look unproven.
    av_channel_policy_t proven;
    av_channel_policy_reset(&proven);

    for (int i = 0; i < 20; i++) {
        av_channel_policy_note_picture(&proven, "chX");
    }
    assert(proven.count == 1);
    av_channel_policy_note_picture(&proven, "chY");
    assert(proven.count == 2);
    assert(av_channel_policy_has_shown(&proven, "chX"));
}

static void test_an_id_that_does_not_fit_is_refused_not_truncated(void)
{
    // A truncated id could equal a different channel's id, which would mark the
    // wrong channel as proven -- the exact fault this module prevents, arriving
    // through the back door.
    av_channel_policy_t proven;
    av_channel_policy_reset(&proven);

    char too_long[AV_CHANNEL_POLICY_ID_MAX + 8];
    memset(too_long, 'x', sizeof(too_long) - 1);
    too_long[sizeof(too_long) - 1] = '\0';

    av_channel_policy_note_picture(&proven, too_long);
    assert(proven.count == 0);          // nothing was stored
    assert(!av_channel_policy_has_shown(&proven, too_long));

    // The id that shares its prefix is not accidentally marked either.
    char prefix[AV_CHANNEL_POLICY_ID_MAX];
    memcpy(prefix, too_long, AV_CHANNEL_POLICY_ID_MAX - 1);
    prefix[AV_CHANNEL_POLICY_ID_MAX - 1] = '\0';
    assert(!av_channel_policy_has_shown(&proven, prefix));
}

static void test_an_empty_or_missing_id_is_not_a_channel(void)
{
    // An empty id is the state before the first session names one, and it must
    // not become a mark that a later empty id matches.
    av_channel_policy_t proven;
    av_channel_policy_reset(&proven);

    av_channel_policy_note_picture(&proven, "");
    assert(proven.count == 0);
    assert(!av_channel_policy_has_shown(&proven, ""));
    av_channel_policy_note_picture(&proven, NULL);
    assert(proven.count == 0);
}

static void test_reset_forgets_everything(void)
{
    av_channel_policy_t proven;
    av_channel_policy_reset(&proven);
    av_channel_policy_note_picture(&proven, "chZ");
    assert(av_channel_policy_has_shown(&proven, "chZ"));

    av_channel_policy_reset(&proven);
    assert(proven.count == 0);
    assert(!av_channel_policy_has_shown(&proven, "chZ"));
}

int main(void)
{
    test_a_channel_that_showed_is_never_abandoned();
    test_a_channel_that_never_showed_is_still_skipped();
    test_the_count_restarts_after_a_picture();
    test_never_reaching_the_server_is_not_the_channels_fault();
    test_a_channel_the_viewer_chose_gets_longer();
    test_a_list_of_one_is_never_stepped_over();
    test_proof_survives_leaving_and_returning();
    test_the_oldest_mark_is_dropped_when_the_set_fills();
    test_marking_twice_does_not_consume_two_slots();
    test_an_id_that_does_not_fit_is_refused_not_truncated();
    test_an_empty_or_missing_id_is_not_a_channel();
    test_reset_forgets_everything();
    printf("test_av_channel_policy: all cases passed\n");
    return 0;
}
