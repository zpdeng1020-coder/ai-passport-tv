#include "av_store.h"
#include "av_server_addr.h"
#include "av_settings.h"

#include <esp_log.h>
#include <nvs.h>

static const char *TAG = "av_store";

// Separate from the provisioning component's "wifi" namespace on purpose: it
// owns those keys and reads them while joining a network, and a second writer
// with its own idea of the layout would be a defect waiting to happen.
#define AV_STORE_NAMESPACE "avsettings"
#define AV_STORE_KEY_VOLUME "volume"
#define AV_STORE_KEY_BRIGHT "bright"
#define AV_STORE_KEY_MAGIC  "rev"

// Bumped if the meaning of a stored value ever changes. A record written by a
// different revision is ignored rather than reinterpreted.
#define AV_STORE_REVISION 1u

static bool open_readonly(nvs_handle_t *handle)
{
    esp_err_t e = nvs_open(AV_STORE_NAMESPACE, NVS_READONLY, handle);
    if (e != ESP_OK) {
        // Absent on a device that has never been adjusted, which is not an error.
        ESP_LOGI(TAG, "No stored settings yet (%s)", esp_err_to_name(e));
        return false;
    }
    return true;
}

bool av_store_load(uint8_t *volume_percent, uint8_t *brightness_percent)
{
    if (volume_percent) {
        *volume_percent = (uint8_t)AV_VOLUME_DEFAULT_PERCENT;
    }
    if (brightness_percent) {
        *brightness_percent = (uint8_t)AV_BRIGHTNESS_DEFAULT_PERCENT;
    }

    nvs_handle_t handle;
    if (!open_readonly(&handle)) {
        return true;   // defaults already written out; nothing to report
    }

    uint32_t revision = 0;
    bool usable = nvs_get_u32(handle, AV_STORE_KEY_MAGIC, &revision) == ESP_OK
                  && revision == AV_STORE_REVISION;

    uint8_t volume = 0, bright = 0;
    if (usable) {
        usable = nvs_get_u8(handle, AV_STORE_KEY_VOLUME, &volume) == ESP_OK
                 && nvs_get_u8(handle, AV_STORE_KEY_BRIGHT, &bright) == ESP_OK;
        // Volume is a percentage of the codec's range, so any 0..100 is valid.
        // Brightness is one of a fixed set, so a value that is not one of them
        // means the store was written by a build with different levels, and the
        // nearest level is a better answer than refusing the whole record.
        if (usable && volume > 100u) {
            usable = false;
        }
    }
    nvs_close(handle);

    if (!usable) {
        ESP_LOGW(TAG, "Stored settings unusable; using defaults");
        return true;
    }
    if (volume_percent) {
        *volume_percent = volume;
    }
    if (brightness_percent) {
        uint8_t level = av_brightness_valid_percent(bright)
                        ? bright
                        : av_brightness_percent(
                              av_brightness_nearest_index(bright));
        *brightness_percent = level;
    }
    ESP_LOGI(TAG, "Restored settings: volume %u%%, brightness %u%%",
             volume, brightness_percent ? *brightness_percent : 0u);
    return true;
}

bool av_store_server_addr_load(av_server_addr_t *addr)
{
    if (!addr) {
        return false;
    }

    // The same namespace and key the setup page writes through. Reading it here
    // rather than keeping a second copy means the page and the player can never
    // disagree about which server to use.
    nvs_handle_t handle;
    if (nvs_open("wifi", NVS_READONLY, &handle) != ESP_OK) {
        return false;
    }

    // Matches the ceiling the page enforces on the field, so a value that could
    // be submitted can always be read back. The parser rejects anything longer
    // than it can hold, so an oversized stored value fails rather than truncates.
    char text[256] = {0};
    size_t size = sizeof(text);
    esp_err_t e = nvs_get_str(handle, "ota_url", text, &size);
    nvs_close(handle);
    if (e != ESP_OK) {
        ESP_LOGI(TAG, "No server address stored yet (%s)", esp_err_to_name(e));
        return false;
    }

    av_server_addr_t parsed;
    if (!av_server_addr_parse(text, &parsed) || !av_server_addr_usable(&parsed)) {
        // Said plainly, because the cure is to open the setup page and correct
        // the field -- not something the device can work around on its own.
        ESP_LOGW(TAG, "Stored server address is unusable; open the setup page");
        return false;
    }

    // No success line here. The caller reads this once per session so that a new
    // address takes effect without a restart, and announcing it from inside would
    // print the same line every few seconds. Reporting it is the caller's job.
    *addr = parsed;
    return true;
}

bool av_store_save(uint8_t volume_percent, uint8_t brightness_percent)
{
    nvs_handle_t handle;
    esp_err_t e = nvs_open(AV_STORE_NAMESPACE, NVS_READWRITE, &handle);
    if (e != ESP_OK) {
        ESP_LOGW(TAG, "Cannot open settings store: %s", esp_err_to_name(e));
        return false;
    }
    if (volume_percent > 100u) {
        volume_percent = 100u;
    }
    // Written last, so a record interrupted midway is rejected on the next boot
    // rather than read as a half-valid pair.
    e = nvs_set_u8(handle, AV_STORE_KEY_VOLUME, volume_percent);
    if (e == ESP_OK) {
        e = nvs_set_u8(handle, AV_STORE_KEY_BRIGHT, brightness_percent);
    }
    if (e == ESP_OK) {
        e = nvs_set_u32(handle, AV_STORE_KEY_MAGIC, AV_STORE_REVISION);
    }
    if (e == ESP_OK) {
        e = nvs_commit(handle);
    }
    nvs_close(handle);
    if (e != ESP_OK) {
        ESP_LOGW(TAG, "Settings not saved: %s", esp_err_to_name(e));
        return false;
    }
    return true;
}
