// WiFi setup, built on the esp-wifi-connect component. See av_provision.h for
// what this offers and why provisioning does not run alongside playback.
//
// This file exists because the component is C++ and the firmware is C. It is a
// translation layer: it decides which of the component's two modes to ask for,
// remembers what the display needs to show, and forwards the two network events
// the player acts on. Nothing here reimplements what the component already does.

#include "av_provision.h"
#include "av_config.h"

#include "ssid_manager.h"
#include "wifi_manager.h"

#include <atomic>
#include <cstdio>
#include <cstring>

#include <esp_event.h>
#include <esp_log.h>
#include <esp_netif.h>
#include <esp_wifi.h>
#include <nvs_flash.h>

namespace {

const char *TAG = "av_prov";

// Set while av_provision_start_ap() is deliberately taking the station down.
//
// This is not tidiness. The component reports WifiEvent::Disconnected both when
// a network is lost and when the station is stopped on purpose to raise the
// access point (wifi_manager.cc:173 and :242). The player treats a disconnect as
// "the session is over", so forwarding the deliberate one would tear down a
// session that is already ending and, worse, would do it while the access point
// is starting -- the confusing case where provisioning looks like a network
// failure. The callback is suppressed for the duration of the switch instead.
std::atomic<bool> s_switching_mode{false};

std::atomic<bool> s_initialized{false};
std::atomic<bool> s_ap_running{false};
// A phone has joined the access point and been given an address, so the
// configuration page can be opened. Association alone is not enough: the DHCP
// lease is what makes the page reachable, and the two events are separate.
std::atomic<bool> s_client_ready{false};

void (*s_on_disconnected)(void) = nullptr;
void (*s_on_connected)(void) = nullptr;

char s_ap_name[64];
char s_ap_url[64];

// AP client events. The component does not surface these, so they are handled
// here; the access point interface is created by esp_netif on demand, so the
// handler is registered after the access point starts.
void on_ap_client(void *arg, esp_event_base_t base, int32_t id, void *data)
{
    (void)arg;
    (void)data;
    if (base == IP_EVENT && id == IP_EVENT_AP_STAIPASSIGNED) {
        s_client_ready.store(true);
        ESP_LOGI(TAG, "A device joined the setup network and was given an address");
    } else if (base == WIFI_EVENT && id == WIFI_EVENT_AP_STADISCONNECTED) {
        s_client_ready.store(false);
        ESP_LOGI(TAG, "A device left the setup network");
    }
}

// Copy into a caller buffer, always terminated, never longer than it.
void copy_out(char *out, size_t size, const char *text)
{
    if (!out || size == 0) {
        return;
    }
    if (!text) {
        out[0] = '\0';
        return;
    }
    size_t length = strlen(text);
    if (length >= size) {
        length = size - 1;
    }
    memcpy(out, text, length);
    out[length] = '\0';
}

void handle_event(WifiEvent event, const std::string &data)
{
    switch (event) {
    case WifiEvent::Connected:
        ESP_LOGI(TAG, "Joined '%s'", data.c_str());
        if (s_on_connected) {
            s_on_connected();
        }
        break;
    case WifiEvent::Disconnected:
        if (s_switching_mode.load()) {
            // A stop we asked for, not a network that went away.
            ESP_LOGI(TAG, "Station stopped deliberately; not reporting a disconnect");
            break;
        }
        // The reason code is useful; the payload is never logged elsewhere.
        ESP_LOGW(TAG, "Lost the network (reason %s)", data.c_str());
        if (s_on_disconnected) {
            s_on_disconnected();
        }
        break;
    case WifiEvent::ConfigModeEnter:
        ESP_LOGI(TAG, "Setup access point is up");
        break;
    case WifiEvent::ConfigModeExit:
        ESP_LOGI(TAG, "Setup access point is down");
        break;
    default:
        break;
    }
}

}  // namespace

bool av_provision_available(void)
{
    return true;
}

bool av_provision_init(bool *has_stored_network)
{
    if (has_stored_network) {
        *has_stored_network = false;
    }
    if (s_initialized.load()) {
        if (has_stored_network) {
            *has_stored_network = !SsidManager::GetInstance().GetSsidList().empty();
        }
        return true;
    }

    // The component initializes NVS, the network interface, the default event
    // loop and the WiFi driver itself, so this module must not do any of that:
    // a second esp_wifi_init() or esp_netif_init() fails and leaves the radio
    // unusable. That is why the player's own WiFi start-up is gone rather than
    // kept alongside this.
    WifiManagerConfig config;
    config.ssid_prefix = "FoloToy";
    config.language = "zh-CN";
    auto &manager = WifiManager::GetInstance();
    if (!manager.Initialize(config)) {
        ESP_LOGE(TAG, "Could not start the WiFi stack");
        return false;
    }
    manager.SetEventCallback(handle_event);

    // Credentials that were compiled into this build are the local fallback.
    // They are written into the same store the setup page writes to, so there is
    // one place a network is kept and one code path that reads it. A build
    // published without credentials has none of these, and the list stays empty,
    // which is the signal to ask the user for a network instead.
    auto &ssids = SsidManager::GetInstance();
    bool stored = !ssids.GetSsidList().empty();
    if (!stored && AV_WIFI_SSID[0] != '\0' && AV_WIFI_PASSWORD[0] != '\0') {
        ssids.AddSsid(AV_WIFI_SSID, AV_WIFI_PASSWORD);
        stored = true;
        ESP_LOGI(TAG, "Using the network compiled into this build");
    }

    s_initialized.store(true);
    if (has_stored_network) {
        *has_stored_network = stored;
    }
    ESP_LOGI(TAG, "WiFi ready; %s network stored, heap=%u largest=%u",
             stored ? "a" : "no",
             (unsigned)esp_get_free_heap_size(),
             (unsigned)heap_caps_get_largest_free_block(MALLOC_CAP_INTERNAL));
    return true;
}

bool av_provision_join_stored(void)
{
    if (!s_initialized.load()) {
        return false;
    }
    if (SsidManager::GetInstance().GetSsidList().empty()) {
        return false;
    }
    WifiManager::GetInstance().StartStation();
    return true;
}

bool av_provision_connected(void)
{
    return s_initialized.load() && WifiManager::GetInstance().IsConnected();
}

void av_provision_keep_radio_awake(void)
{
    if (!s_initialized.load()) {
        return;
    }
    // The component names the levels by intent, not by the driver constant:
    // PERFORMANCE is the one that maps to WIFI_PS_NONE (wifi_station.cc:390).
    WifiManager::GetInstance().SetPowerSaveLevel(WifiPowerSaveLevel::PERFORMANCE);
}

void av_provision_network_name(char *out, size_t size)
{
    copy_out(out, size, s_initialized.load()
                          ? WifiManager::GetInstance().GetSsid().c_str()
                          : "");
}

bool av_provision_start_ap(void)
{
    if (!s_initialized.load()) {
        return false;
    }
    // Keep the deliberate-stop notification away from the player: it means "the
    // session may end", and this transition is the session already having ended.
    s_switching_mode.store(true);
    WifiManager::GetInstance().StartConfigAp();
    s_switching_mode.store(false);

    s_client_ready.store(false);
    // Registered after the access point exists, because the interface it
    // delivers events on is created by esp_netif_create_default_wifi_ap() inside
    // the component.
    esp_event_handler_register(IP_EVENT, IP_EVENT_AP_STAIPASSIGNED,
                               on_ap_client, nullptr);
    esp_event_handler_register(WIFI_EVENT, WIFI_EVENT_AP_STADISCONNECTED,
                               on_ap_client, nullptr);

    std::string name = WifiManager::GetInstance().GetApSsid();
    std::string url = WifiManager::GetInstance().GetApWebUrl();
    copy_out(s_ap_name, sizeof(s_ap_name), name.c_str());
    copy_out(s_ap_url, sizeof(s_ap_url), url.c_str());
    s_ap_running.store(true);
    ESP_LOGI(TAG, "Setup network '%s' at %s; heap=%u largest=%u",
             s_ap_name, s_ap_url,
             (unsigned)esp_get_free_heap_size(),
             (unsigned)heap_caps_get_largest_free_block(MALLOC_CAP_INTERNAL));
    return true;
}

void av_provision_stop_ap(void)
{
    if (!s_initialized.load()) {
        return;
    }
    esp_event_handler_unregister(IP_EVENT, IP_EVENT_AP_STAIPASSIGNED, on_ap_client);
    esp_event_handler_unregister(WIFI_EVENT, WIFI_EVENT_AP_STADISCONNECTED, on_ap_client);
    s_switching_mode.store(true);
    WifiManager::GetInstance().StopConfigAp();
    s_switching_mode.store(false);
    s_client_ready.store(false);
    s_ap_running.store(false);
    s_ap_name[0] = '\0';
    s_ap_url[0] = '\0';
}

bool av_provision_ap_running(void)
{
    return s_ap_running.load();
}

void av_provision_ap_name(char *out, size_t size)
{
    copy_out(out, size, s_ap_name);
}

void av_provision_ap_url(char *out, size_t size)
{
    copy_out(out, size, s_ap_url);
}

bool av_provision_client_ready(void)
{
    return s_client_ready.load();
}

bool av_provision_has_network(void)
{
    if (!s_initialized.load() || !s_ap_running.load()) {
        return false;
    }
    // Finished when the component leaves setup mode by itself.
    //
    // The page saves the credentials, checks the device can actually join, and
    // then asks the component to exit setup. So setup mode ending is the
    // component's own statement that provisioning succeeded, and it is the only
    // signal that is true the second time an existing network is set up.
    //
    // Counting the saved networks cannot be used for that: the component
    // overwrites an entry whose SSID it already has rather than adding one
    // (ssid_manager.cc, AddSsid), so re-running setup for the same network left
    // the count unchanged and the screen sat on the setup page while the phone
    // reported success.
    return !WifiManager::GetInstance().IsConfigMode();
}

void av_provision_on_disconnected(void (*callback)(void))
{
    s_on_disconnected = callback;
}

void av_provision_on_connected(void (*callback)(void))
{
    s_on_connected = callback;
}
