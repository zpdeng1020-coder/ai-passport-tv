// strnlen is POSIX, not C. macOS exposes it through <string.h> without being
// asked, glibc only exposes it when a feature-test macro asks for POSIX 2008 --
// so this file built on macOS and failed on the Linux CI runner with
// "implicit declaration of function 'strnlen'". The macro has to come before
// any header is included, which is why it is the first thing in the file.
#define _POSIX_C_SOURCE 200809L

#include "ui_menu.h"

#include <stdlib.h>
#include <string.h>

// How long the overlay stays up without a gesture. The banner is deliberately
// shorter than the menu: it carries one fact the viewer already knows.
#define UI_MENU_TIMEOUT_MS 12000
#define UI_MENU_BANNER_MS 5000
#define UI_MENU_STATUS_MS 8000
// The backlight page stays up longer than the status page: it is a control the
// viewer is working, so it should not vanish between presses.
#define UI_MENU_BRIGHTNESS_MS 12000
// How long the volume indicator stays up after the last change. Long enough to
// read while the button is held, short enough not to sit over the picture.
#define UI_MENU_VOLUME_MS 1500

// A channel id must stay printable and short enough for the device table; the
// name is only ever drawn, so it is bounded by the font and the screen.
static bool usable(const ui_menu_entry_t *entry)
{
    size_t id_length = strnlen(entry->id, UI_MENU_ID_MAX);
    if (id_length == 0 || id_length >= UI_MENU_ID_MAX) {
        return false;
    }
    for (size_t i = 0; i < id_length; i++) {
        if (entry->id[i] < '!' || entry->id[i] > '~') {
            return false;
        }
    }
    // A name is required but never a reason to drop the channel: an overlong one
    // is truncated where it is drawn, so only an empty name disqualifies an entry.
    return strnlen(entry->name, UI_MENU_NAME_MAX) > 0;
}

// Keep the highlight inside the window the caller can draw. Without this the
// selection walks off the bottom of the screen and the viewer presses a key
// with nothing visibly happening.
static void follow_selection(ui_menu_t *menu, unsigned rows)
{
    if (rows == 0) {
        return;               // the caller has not said how much fits
    }
    if (menu->selected < menu->scroll) {
        menu->scroll = menu->selected;
    } else if (menu->selected >= menu->scroll + rows) {
        menu->scroll = menu->selected - rows + 1;
    }
}

unsigned ui_menu_visible_rows(int screen_height, int row_height)
{
    if (screen_height <= 0 || row_height <= 0) {
        return 0;
    }
    unsigned rows = (unsigned)(screen_height / row_height);
    if (rows > UI_MENU_MAX) {
        rows = UI_MENU_MAX;
    }
    return rows;
}

void ui_menu_init(ui_menu_t *menu)
{
    if (!menu) {
        return;
    }
    memset(menu, 0, sizeof(*menu));
    menu->view = UI_VIEW_VIDEO;
}

bool ui_menu_load(ui_menu_t *menu, const ui_menu_entry_t *entries, unsigned count,
                  const char *current)
{
    if (!menu || !entries || count == 0 || count > UI_MENU_MAX) {
        return false;
    }
    // Copy the entries that can be shown and skip the rest. Refusing the whole
    // list because one entry is malformed meant a single odd channel name could
    // disable the menu completely, with only a log line to show for it.
    unsigned kept = 0;
    for (unsigned i = 0; i < count; i++) {
        if (usable(&entries[i])) {
            kept++;
        }
    }
    if (kept == 0) {
        return false;         // nothing usable: keep whatever was there before
    }
    // Build into a local first: a rejected list must leave the previous one in
    // place, or a malformed CONFIG would empty the menu mid-session.
    // Heap, not stack: at up to UI_MENU_MAX entries this is far past the stack
    // of any caller, and a stack copy here is the same overflow that crashed the
    // device inside config_valid. Freed on every path below.
    ui_menu_entry_t *staged = malloc(sizeof(ui_menu_entry_t) * (size_t)kept);
    if (!staged) {
        return false;
    }
    unsigned next = 0;
    for (unsigned i = 0; i < count; i++) {
        if (usable(&entries[i])) {
            staged[next++] = entries[i];
        }
    }
    count = kept;

    // Keep the highlight on the playing channel so opening the list does not
    // silently move it, and keep an existing highlight when the list is
    // otherwise unchanged and still names it.
    unsigned selected = 0;
    bool found = false;
    if (current) {
        for (unsigned i = 0; i < count; i++) {
            if (strcmp(staged[i].id, current) == 0) {
                selected = i;
                found = true;
                break;
            }
        }
    }
    if (!found && menu->count) {
        const char *was = ui_menu_selected(menu);
        if (was) {
            for (unsigned i = 0; i < count; i++) {
                if (strcmp(staged[i].id, was) == 0) {
                    selected = i;
                    found = true;
                    break;
                }
            }
        }
    }

    memcpy(menu->entries, staged, count * sizeof(staged[0]));
    free(staged);
    menu->count = count;
    menu->selected = selected;
    // Start from the top of the window and pull it to the highlight. The old
    // code only clamped scroll downwards and never advanced it, so once the
    // selection passed the last visible row it drew off-screen.
    menu->scroll = 0;
    follow_selection(menu, menu->visible_rows);
    return true;
}

const char *ui_menu_selected(const ui_menu_t *menu)
{
    if (!menu || menu->count == 0 || menu->selected >= menu->count) {
        return NULL;
    }
    return menu->entries[menu->selected].id;
}

void ui_menu_open(ui_menu_t *menu)
{
    if (!menu || menu->count == 0) {
        return;
    }
    menu->view = UI_VIEW_MENU;
    menu->timeout_ms = UI_MENU_TIMEOUT_MS;
    menu->action = UI_ACTION_NONE;
}

void ui_menu_open_brightness(ui_menu_t *menu)
{
    if (!menu) {
        return;
    }
    menu->view = UI_VIEW_BRIGHTNESS;
    // Same timeout discipline as the status page: the page closes itself, so a
    // viewer who walks away cannot leave the arrows bound to brightness and be
    // unable to change channel.
    menu->timeout_ms = UI_MENU_STATUS_MS;
    menu->action = UI_ACTION_NONE;
}

void ui_menu_keep_open(ui_menu_t *menu)
{
    if (!menu || menu->view == UI_VIEW_VIDEO) {
        return;   // the video view has no page timeout to extend
    }
    // Whichever page is up gets the same treatment as a scroll: the clock
    // restarts, so a viewer mid-adjustment is never cut off.
    menu->timeout_ms = (menu->view == UI_VIEW_BRIGHTNESS) ? UI_MENU_BRIGHTNESS_MS
                                                          : UI_MENU_TIMEOUT_MS;
}

void ui_menu_open_status(ui_menu_t *menu)
{
    if (!menu) {
        return;
    }
    menu->view = UI_VIEW_STATUS;
    menu->timeout_ms = UI_MENU_STATUS_MS;
    menu->action = UI_ACTION_NONE;
}

void ui_menu_close(ui_menu_t *menu)
{
    if (!menu) {
        return;
    }
    menu->view = UI_VIEW_VIDEO;
    menu->timeout_ms = 0;
}

void ui_menu_set_rows(ui_menu_t *menu, unsigned rows)
{
    if (!menu) {
        return;
    }
    menu->visible_rows = rows;
    follow_selection(menu, rows);
}

void ui_menu_step(ui_menu_t *menu, int delta)
{
    ui_menu_scroll_by(menu, delta, menu->visible_rows);
}

void ui_menu_scroll_by(ui_menu_t *menu, int delta, unsigned rows)
{
    if (!menu || menu->view != UI_VIEW_MENU || menu->count == 0 || delta == 0) {
        return;
    }
    if (rows) {
        menu->visible_rows = rows;
    }
    // Wrap around: reaching either end and continuing is more natural than
    // stopping dead, and it is the only way back without holding a key through
    // the whole list.
    int count = (int)menu->count;
    int next = ((int)menu->selected + delta) % count;
    if (next < 0) {
        next += count;
    }
    menu->selected = (unsigned)next;
    follow_selection(menu, menu->visible_rows);
    menu->timeout_ms = UI_MENU_TIMEOUT_MS;
}

void ui_menu_confirm(ui_menu_t *menu)
{
    if (!menu || menu->view != UI_VIEW_MENU || menu->count == 0) {
        return;
    }
    menu->action = UI_ACTION_SWITCH;
}

bool ui_menu_tick(ui_menu_t *menu, int64_t elapsed_ms)
{
    if (!menu || elapsed_ms <= 0) {
        return false;
    }
    bool changed = false;
    if (menu->banner_ms > 0) {
        menu->banner_ms -= elapsed_ms;
        if (menu->banner_ms <= 0) {
            menu->banner_ms = 0;
            changed = true;
        }
    }
    if (menu->volume_ms > 0) {
        menu->volume_ms -= elapsed_ms;
        if (menu->volume_ms <= 0) {
            menu->volume_ms = 0;
            changed = true;
        }
    }
    if (menu->view != UI_VIEW_VIDEO && menu->timeout_ms > 0) {
        menu->timeout_ms -= elapsed_ms;
        if (menu->timeout_ms <= 0) {
            menu->timeout_ms = 0;
            ui_menu_close(menu);
            changed = true;
        }
    }
    return changed;
}

ui_action_t ui_menu_take_action(ui_menu_t *menu, char *out, size_t out_size)
{
    if (!menu) {
        return UI_ACTION_NONE;
    }
    ui_action_t action = menu->action;
    menu->action = UI_ACTION_NONE;
    if (out && out_size) {
        out[0] = '\0';
    }
    if (action == UI_ACTION_SWITCH && out && out_size) {
        const char *id = ui_menu_selected(menu);
        if (!id || strnlen(id, UI_MENU_ID_MAX) >= out_size) {
            return UI_ACTION_NONE;  // cannot name it, so do not pretend to
        }
        memcpy(out, id, strlen(id) + 1);
        ui_menu_close(menu);  // the list has done its job
    }
    return action;
}

void ui_menu_show_volume(ui_menu_t *menu, unsigned level)
{
    if (!menu) {
        return;
    }
    menu->volume = level > 100u ? 100u : level;
    menu->volume_ms = UI_MENU_VOLUME_MS;
}

bool ui_menu_volume_visible(const ui_menu_t *menu)
{
    return menu && menu->volume_ms > 0;
}

void ui_menu_banner(ui_menu_t *menu, const char *name, const char *previous)
{
    if (!menu || !name) {
        return;
    }
    // Nothing changed, so a banner would only cover the picture to repeat what
    // the viewer already had: a single-channel list, or a reconnect that landed
    // on the same channel.
    if (previous && strcmp(name, previous) == 0) {
        return;
    }
    strncpy(menu->banner, name, sizeof(menu->banner) - 1);
    menu->banner[sizeof(menu->banner) - 1] = '\0';
    menu->banner_ms = UI_MENU_BANNER_MS;
}

