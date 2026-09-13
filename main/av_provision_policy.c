#include "av_provision_policy.h"

av_boot_mode_t av_boot_mode(bool has_network, bool server_configured)
{
    // Only a missing network forces setup at start-up. The reason is that setup
    // is the only screen reachable without a network: the way back to it is the
    // status page, which is drawn over playback, and playback cannot start
    // without a network. A device that came up here and left setup without a
    // network would have no way to return to it.
    //
    // A missing server address is different. The device can join its network and
    // run; the session fails to connect, and the waiting screen it leaves behind
    // still owns the panel that the status page is drawn over. So the viewer can
    // open the status page and reach setup from there, whenever they choose.
    // Forcing setup in that case would also mean a viewer who left it was pushed
    // straight back, with an open access point up almost continuously.
    (void)server_configured;
    return has_network ? AV_BOOT_PLAY : AV_BOOT_SETUP;
}

bool av_setup_expired(uint32_t started_ms, uint32_t now_ms, uint32_t limit_ms)
{
    if (limit_ms == 0) {
        return false;
    }
    // Unsigned subtraction, so this stays correct across the millisecond counter
    // wrapping, which happens about every 49 days. Comparing the timestamps
    // directly would make a wrapped counter look like it had run out.
    return (uint32_t)(now_ms - started_ms) >= limit_ms;
}
