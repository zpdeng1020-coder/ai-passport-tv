// Setup-mode decisions, tested without a device.
//
// These two functions decide whether a device offers its setup page and when it
// gives up on it. Both are cheap to get wrong in ways a build cannot catch: a
// wrong mode means a device that never joins a network, and a wrong expiry means
// an open access point left running.
#include "av_provision_policy.h"

#include <assert.h>
#include <stdio.h>

static void test_device_with_a_network_plays(void)
{
    // The ordinary case: a network is known and there is somewhere to stream
    // from, so play.
    assert(av_boot_mode(true, true) == AV_BOOT_PLAY);
}

static void test_device_with_a_network_but_no_server_plays(void)
{
    // Setup is forced only by a missing network, because it is the only screen
    // reachable without one. With a network the device runs, and the status page
    // -- drawn over playback -- is the way to setup if the address is needed.
    // Forcing it here would also mean a viewer who left was returned at once,
    // leaving the open access point up almost continuously.
    assert(av_boot_mode(true, false) == AV_BOOT_PLAY);
}

static void test_device_with_no_network_offers_setup(void)
{
    // The case this feature exists for: a published image has no credentials
    // compiled in, so it has no network and must ask for one.
    assert(av_boot_mode(false, true) == AV_BOOT_SETUP);
    assert(av_boot_mode(false, false) == AV_BOOT_SETUP);
}

static void test_setup_runs_out_after_its_window(void)
{
    uint32_t start = 1000u;
    assert(!av_setup_expired(start, start, AV_SETUP_WINDOW_MS));
    assert(!av_setup_expired(start, start + AV_SETUP_WINDOW_MS - 1u, AV_SETUP_WINDOW_MS));
    // The boundary counts as expired, so the window is never longer than stated.
    assert(av_setup_expired(start, start + AV_SETUP_WINDOW_MS, AV_SETUP_WINDOW_MS));
    assert(av_setup_expired(start, start + AV_SETUP_WINDOW_MS + 1u, AV_SETUP_WINDOW_MS));
}

static void test_zero_limit_means_no_expiry(void)
{
    assert(!av_setup_expired(0u, 0xFFFFFFFFu, 0u));
}

static void test_setup_expiry_survives_the_counter_wrapping(void)
{
    // The millisecond counter the device passes in wraps about every 49 days.
    // Starting just before the wrap and asking just after it must still measure
    // the elapsed time, not read the wrap as the window having passed.
    uint32_t start = 0xFFFFFFFFu - 1000u;   // 1 s before the wrap
    assert(!av_setup_expired(start, start + 500u, AV_SETUP_WINDOW_MS));
    assert(!av_setup_expired(start, 500u, AV_SETUP_WINDOW_MS));  // wrapped, 1500 ms on
    assert(av_setup_expired(start, AV_SETUP_WINDOW_MS, AV_SETUP_WINDOW_MS));
}

int main(void)
{
    test_device_with_a_network_plays();
    test_device_with_a_network_but_no_server_plays();
    test_device_with_no_network_offers_setup();
    test_setup_runs_out_after_its_window();
    test_zero_limit_means_no_expiry();
    test_setup_expiry_survives_the_counter_wrapping();
    printf("test_av_provision_policy: all cases passed\n");
    return 0;
}
