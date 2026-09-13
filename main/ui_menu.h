// Overlay menu and banner state. Pure logic: no ESP-IDF, no drawing, no keys.
// The caller feeds it gestures and reads back what should be on screen, which
// keeps the behaviour testable on the development machine.
#pragma once
#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

// The menu holds as many channels as CONFIG may carry. A smaller number here was
// left behind when the device limit rose, and ui_menu_load then rejected the
// whole list, which silently disabled the menu.
#include "av_protocol.h"
#define UI_MENU_MAX AV_CHANNEL_MAX
// 16 glyphs of 16 pixels already overflow a 320-pixel row, so this is well past
// what can be shown; a longer name is drawn truncated, never refused.
#define UI_MENU_NAME_MAX 48u
#define UI_MENU_ID_MAX 16u

typedef enum {
    UI_VIEW_VIDEO = 0,   // playing; a banner may still be up
    UI_VIEW_MENU,        // the channel list is open
    UI_VIEW_STATUS,      // the status page is up
    UI_VIEW_BRIGHTNESS,  // the backlight level is being set
} ui_view_t;

// What the overlay wants the caller to do next.
typedef enum {
    UI_ACTION_NONE = 0,
    UI_ACTION_SWITCH,    // switch to ui_menu_selected()
    UI_ACTION_CLOSE,     // leave the overlay and resume the session
} ui_action_t;

typedef struct {
    char id[UI_MENU_ID_MAX];
    char name[UI_MENU_NAME_MAX];
} ui_menu_entry_t;

typedef struct {
    ui_view_t view;
    ui_menu_entry_t entries[UI_MENU_MAX];
    unsigned count;
    unsigned selected;
    unsigned scroll;      // index of the first visible row
    unsigned visible_rows; // rows the caller can draw; 0 means it has not said
    // Milliseconds left before the overlay closes itself; 0 means "no timeout".
    int64_t timeout_ms;
    // Set when the user picked an entry, cleared by ui_menu_take_action().
    ui_action_t action;
    // A banner shown over the video after a switch, and its remaining time.
    char banner[UI_MENU_NAME_MAX];
    int64_t banner_ms;
    // The volume indicator shown while the volume is being changed, and its
    // remaining time. It carries the level rather than reading it back, so the
    // display shows what was asked for at the moment it was asked for.
    unsigned volume;
    int64_t volume_ms;
    // Which backlight step is in use, as an index into AV_BRIGHTNESS_LEVELS.
    // The menu carries it so the brightness page can be drawn from the same
    // state the key handler updates, with no second source of truth.
    unsigned brightness;
} ui_menu_t;

// Rows the list can show at once, given the screen height and row height.
unsigned ui_menu_visible_rows(int screen_height, int row_height);

void ui_menu_init(ui_menu_t *menu);
// Replace the channel list. `current` marks which id is playing now; the
// selection starts there, so opening the menu does not move the highlight.
// Returns false and leaves the menu untouched if any entry is unusable.
bool ui_menu_load(ui_menu_t *menu, const ui_menu_entry_t *entries, unsigned count,
                  const char *current);
// Open the list. No effect when the list is empty.
void ui_menu_open(ui_menu_t *menu);
// Open the status page.
void ui_menu_open_status(ui_menu_t *menu);
// Open the backlight page. Unlike the list this needs no channel data, so it
// opens on a device that has not received a channel list yet.
void ui_menu_open_brightness(ui_menu_t *menu);
// Restart the current page's timeout. Used by a page whose viewer is still
// adjusting something, so the page does not close under their hand: the caller
// keeps the timer in the module that owns it rather than writing the field.
void ui_menu_keep_open(ui_menu_t *menu);
void ui_menu_close(ui_menu_t *menu);

// One gesture. `delta` is +1 for down and -1 for up; ignored outside list views.
void ui_menu_step(ui_menu_t *menu, int delta);
// The same, but told how many rows are on screen, so the highlight can be kept
// in view. Scrolling cannot be done without that number.
void ui_menu_scroll_by(ui_menu_t *menu, int delta, unsigned rows);
// Tell the menu how many rows it may show, so it can keep the highlight visible.
void ui_menu_set_rows(ui_menu_t *menu, unsigned rows);
// Confirm the highlighted entry. Inside the list this asks for a switch.
void ui_menu_confirm(ui_menu_t *menu);

// Advance timers. Returns true when something changed and the screen must be
// redrawn; the caller passes the real elapsed milliseconds.
bool ui_menu_tick(ui_menu_t *menu, int64_t elapsed_ms);

// The id the caller should switch to, read once. Clears the pending action.
ui_action_t ui_menu_take_action(ui_menu_t *menu, char *out, size_t out_size);

// Show the banner naming the channel that is now playing, if it differs from
// `previous`. Called after a switch completes.
void ui_menu_banner(ui_menu_t *menu, const char *name, const char *previous);

// Show the volume indicator at `level` percent and restart its timeout. Every
// press calls this, so holding the button keeps the display up instead of
// letting it expire mid-press.
void ui_menu_show_volume(ui_menu_t *menu, unsigned level);

// Whether the volume indicator should be drawn now.
bool ui_menu_volume_visible(const ui_menu_t *menu);


// The id currently highlighted, or NULL when the list is empty.
const char *ui_menu_selected(const ui_menu_t *menu);
