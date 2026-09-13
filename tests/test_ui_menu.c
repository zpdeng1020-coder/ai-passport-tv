// Host tests for overlay menu state: selection, scrolling, timeouts and the
// banner rules. Pure logic, so no device is needed.
#include "ui_menu.h"

#include <stdio.h>
#include <stdlib.h>
#include <string.h>

static int failures;

static void check(int condition, const char *what)
{
    if (!condition) {
        printf("FAIL: %s\n", what);
        failures++;
    }
}

static ui_menu_entry_t ENTRIES[] = {
    { "cctv1", "CCTV1" },
    { "cctv5", "CCTV5" },
    { "cctv9", "CCTV9" },
    { "cgtn", "CGTN" },
    { "dongfang", "Dongfang" },
};
#define ENTRY_COUNT (sizeof(ENTRIES) / sizeof(ENTRIES[0]))

static ui_menu_t loaded(const char *current)
{
    ui_menu_t menu;
    ui_menu_init(&menu);
    check(ui_menu_load(&menu, ENTRIES, (unsigned)ENTRY_COUNT, current),
          "the sample list loads");
    return menu;
}

static void test_open_selects_the_playing_channel(void)
{
    ui_menu_t menu = loaded("cctv9");
    check(menu.selected == 2, "the highlight starts on the channel playing now");

    // Opening must not move the highlight; the list opens where the viewer is.
    ui_menu_open(&menu);
    check(menu.view == UI_VIEW_MENU, "opening switches to the list view");
    check(strcmp(ui_menu_selected(&menu), "cctv9") == 0, "the playing channel stays selected");
}

static void test_stepping_wraps_and_keeps_the_selection_sane(void)
{
    ui_menu_t menu = loaded("cctv1");
    ui_menu_open(&menu);

    ui_menu_step(&menu, 1);
    check(strcmp(ui_menu_selected(&menu), "cctv5") == 0, "down moves to the next channel");
    ui_menu_step(&menu, -1);
    check(strcmp(ui_menu_selected(&menu), "cctv1") == 0, "up moves back");

    // Past the start it wraps to the end rather than sticking.
    ui_menu_step(&menu, -1);
    check(strcmp(ui_menu_selected(&menu), "dongfang") == 0, "up from the first wraps to the last");

    // And past the end back to the start.
    for (unsigned i = 0; i < ENTRY_COUNT; i++) {
        ui_menu_step(&menu, 1);
    }
    check(strcmp(ui_menu_selected(&menu), "dongfang") == 0,
          "a full cycle returns to the same entry");
}

static void test_confirm_reports_the_highlighted_channel(void)
{
    ui_menu_t menu = loaded("cctv1");
    ui_menu_open(&menu);
    ui_menu_step(&menu, 1);
    ui_menu_step(&menu, 1);
    ui_menu_confirm(&menu);

    char id[UI_MENU_ID_MAX] = "";
    ui_action_t action = ui_menu_take_action(&menu, id, sizeof(id));
    check(action == UI_ACTION_SWITCH, "confirming asks for a switch");
    check(strcmp(id, "cctv9") == 0, "the switch names the highlighted channel");
    check(menu.view == UI_VIEW_VIDEO, "the list closes after the choice");

    // The action is consumed exactly once, so a switch cannot repeat.
    check(ui_menu_take_action(&menu, id, sizeof(id)) == UI_ACTION_NONE,
          "the action is cleared after it is read");
}

static void test_menu_times_out_but_not_the_video(void)
{
    ui_menu_t menu = loaded("cctv1");
    ui_menu_open(&menu);
    check(!ui_menu_tick(&menu, 100), "a short delay changes nothing");
    check(ui_menu_tick(&menu, 12000), "the menu closes itself after its timeout");
    check(menu.view == UI_VIEW_VIDEO, "the timeout returns to the video");

    // In the video view there is nothing to time out, so ticks report no change.
    check(!ui_menu_tick(&menu, 60000), "the video view never times out");
}

static void test_banner_only_when_the_channel_actually_changed(void)
{
    ui_menu_t menu = loaded("cctv1");
    ui_menu_banner(&menu, "CCTV5", "CCTV1");
    check(menu.banner_ms > 0, "a real change raises the banner");
    check(strcmp(menu.banner, "CCTV5") == 0, "the banner names the new channel");

    ui_menu_t same = loaded("cctv1");
    ui_menu_banner(&same, "CCTV1", "CCTV1");
    check(same.banner_ms == 0, "reconnecting to the same channel raises no banner");

    // The banner clears itself and reports the change.
    check(ui_menu_tick(&menu, 5000), "the banner expires");
    check(menu.banner_ms == 0, "the banner is gone after its time");
    check(menu.view == UI_VIEW_VIDEO, "the banner does not disturb the video view");
}

static void test_unusable_entries_are_skipped_not_fatal(void)
{
    ui_menu_t menu = loaded("cctv1");

    // A malformed entry must not cost the viewer the whole menu: the rest of the
    // list is still usable, and refusing everything was how one odd channel name
    // could disable the list entirely.
    ui_menu_entry_t mixed[3] = { { "ok1", "Fine" }, { "has space", "Broken" },
                                 { "ok2", "Also fine" } };
    check(ui_menu_load(&menu, mixed, 3, "ok1"), "a list with one bad entry still loads");
    check(menu.count == 2, "only the usable entries are kept");
    check(strcmp(ui_menu_selected(&menu), "ok1") == 0, "the highlight follows the current id");

    // A 16-byte id is one the device table cannot hold, so it is dropped.
    ui_menu_entry_t too_long[2] = { { "0123456789abcdef", "Sixteen" }, { "ok", "Fine" } };
    check(ui_menu_load(&menu, too_long, 2, NULL), "a list with only that id plus a good one loads");
    check(menu.count == 1 && strcmp(ui_menu_selected(&menu), "ok") == 0,
          "the unusable id is dropped and the good one kept");

    // An empty name disqualifies an entry; an overlong one is only truncated
    // where it is drawn, so it stays.
    ui_menu_entry_t empty_name[2] = { { "ok", "" }, { "also", "Kept" } };
    check(ui_menu_load(&menu, empty_name, 2, NULL), "an empty name drops just that entry");
    check(menu.count == 1 && strcmp(ui_menu_selected(&menu), "also") == 0,
          "the entry without a name is the one that goes");

    // Nothing usable at all: keep what was already there rather than empty it.
    unsigned before = menu.count;
    ui_menu_entry_t all_bad[1] = { { "has space", "Broken" } };
    check(!ui_menu_load(&menu, all_bad, 1, NULL), "a wholly unusable list is refused");
    check(menu.count == before, "a refused list leaves the previous one intact");

    check(!ui_menu_load(&menu, ENTRIES, 0, NULL), "an empty list is refused");
}

static void test_list_capacity_is_enforced(void)
{
    ui_menu_t menu;
    ui_menu_init(&menu);
    // Heap, not a stack array: at UI_MENU_MAX entries this is already far larger
    // than a stack should carry, which is the mistake this whole round is fixing.
    ui_menu_entry_t *big = malloc(sizeof(ui_menu_entry_t) * (UI_MENU_MAX + 1));
    check(big != NULL, "the oversized list could be built");
    if (!big) {
        return;
    }
    for (unsigned i = 0; i <= UI_MENU_MAX; i++) {
        snprintf(big[i].id, UI_MENU_ID_MAX, "c%u", i);
        snprintf(big[i].name, UI_MENU_NAME_MAX, "Channel %u", i);
    }
    check(!ui_menu_load(&menu, big, UI_MENU_MAX + 1, NULL),
          "more channels than the device accepts is refused");
    free(big);
}

static void test_a_full_channel_list_loads(void)
{
    // The case that actually matters: as many channels as the device accepts
    // must fit, because that is what the server sends.
    ui_menu_entry_t *many = malloc(sizeof(ui_menu_entry_t) * UI_MENU_MAX);
    check(many != NULL, "the full-size list could be built");
    if (!many) {
        return;
    }
    for (unsigned i = 0; i < UI_MENU_MAX; i++) {
        snprintf(many[i].id, UI_MENU_ID_MAX, "c%u", i);
        // A long Chinese name, as the real channel table has.
        snprintf(many[i].name, UI_MENU_NAME_MAX, "\u6d4b\u8bd5\u9891\u9053%u", i);
    }
    ui_menu_t menu;
    ui_menu_init(&menu);
    check(ui_menu_load(&menu, many, UI_MENU_MAX, "c0"),
          "a list of exactly the maximum size loads");
    check(menu.count == UI_MENU_MAX, "every entry is kept");
    free(many);
}

static void test_a_channel_missing_from_the_list_is_handled(void)
{
    // The playing channel is not in the new list, e.g. after a source change.
    ui_menu_t menu = loaded("removed");
    check(menu.selected == 0, "an unknown current channel falls back to the top");
    check(strcmp(ui_menu_selected(&menu), "cctv1") == 0, "and the top entry is selected");

    // A later load keeps the highlight on the same channel when it survives.
    ui_menu_t kept = loaded("cctv9");
    ui_menu_load(&kept, ENTRIES, (unsigned)ENTRY_COUNT, NULL);
    check(strcmp(ui_menu_selected(&kept), "cctv9") == 0,
          "a reload without a current channel keeps the highlight");
}

static void test_open_with_no_list_does_nothing(void)
{
    ui_menu_t menu;
    ui_menu_init(&menu);
    ui_menu_open(&menu);
    check(menu.view == UI_VIEW_VIDEO, "an empty list cannot be opened");
    ui_menu_step(&menu, 1);
    ui_menu_confirm(&menu);
    char id[UI_MENU_ID_MAX];
    check(ui_menu_take_action(&menu, id, sizeof(id)) == UI_ACTION_NONE,
          "an empty list produces no action");
}

static void test_scrolling_keeps_the_highlight_on_screen(void)
{
    ui_menu_t menu = loaded("cctv1");
    ui_menu_set_rows(&menu, 3);      // a three-row window over five entries
    ui_menu_open(&menu);

    ui_menu_step(&menu, 1);
    check(menu.scroll == 0 && menu.selected == 1, "moving inside the window does not scroll");

    ui_menu_step(&menu, 1);
    ui_menu_step(&menu, 1);
    check(menu.selected == 3, "the highlight reaches the fourth entry");
    // Without this the selection would sit off the bottom of a three-row window.
    check(menu.scroll == 1, "the window follows the highlight down");
    check(menu.selected >= menu.scroll && menu.selected < menu.scroll + 3,
          "the highlight stays inside the visible window");

    // And the same going back up.
    for (int i = 0; i < 4; i++) {
        ui_menu_step(&menu, -1);
    }
    check(menu.selected < menu.scroll + 3 && menu.selected >= menu.scroll,
          "the window follows the highlight up as well");

    // A window that was never declared must not scroll into nonsense.
    ui_menu_t blank = loaded("cctv1");
    ui_menu_open(&blank);
    ui_menu_step(&blank, 1);
    check(blank.scroll == 0, "an undeclared window leaves the scroll at the top");
}

static void test_confirm_then_take_yields_the_id_once(void)
{
    // The exact order the firmware uses: a click sets the action, the key pump
    // collects it in the same pass. Pressing OK did nothing on the device, so
    // this covers the hand-off rather than the state change alone.
    ui_menu_t menu = loaded("cctv1");
    ui_menu_set_rows(&menu, 8);
    ui_menu_open(&menu);
    ui_menu_scroll_by(&menu, 1, 8);
    ui_menu_scroll_by(&menu, 1, 8);
    ui_menu_confirm(&menu);

    char id[UI_MENU_ID_MAX] = "";
    ui_action_t first = ui_menu_take_action(&menu, id, sizeof(id));
    check(first == UI_ACTION_SWITCH, "the click is collected as a switch");
    check(strcmp(id, "cctv9") == 0, "the switch carries the highlighted id");
    check(menu.view == UI_VIEW_VIDEO, "choosing closes the list");

    // And it must not fire twice: a duplicate switch would reconnect to the
    // same channel and look like the button had done nothing.
    char again[UI_MENU_ID_MAX] = "";
    check(ui_menu_take_action(&menu, again, sizeof(again)) == UI_ACTION_NONE,
          "a second read yields nothing");
    check(again[0] == '\0', "the second read leaves the buffer empty");
}

static void test_confirm_survives_a_long_list_and_scrolling(void)
{
    // The device case: many channels, the highlight deep in the list, confirmed.
    const unsigned count = UI_MENU_MAX;
    ui_menu_entry_t *many = malloc(sizeof(ui_menu_entry_t) * count);
    check(many != NULL, "the long list could be built");
    if (!many) {
        return;
    }
    for (unsigned i = 0; i < count; i++) {
        snprintf(many[i].id, UI_MENU_ID_MAX, "c%u", i);
        snprintf(many[i].name, UI_MENU_NAME_MAX, "\u9891\u9053%u", i);
    }
    ui_menu_t menu;
    ui_menu_init(&menu);
    check(ui_menu_load(&menu, many, count, "c0"), "the long list loads");
    ui_menu_set_rows(&menu, 8);
    ui_menu_open(&menu);

    // Walk to entry 40 and confirm; the id must survive the scroll arithmetic.
    for (int i = 0; i < 40; i++) {
        ui_menu_scroll_by(&menu, 1, 8);
    }
    check(menu.selected == 40, "the highlight reached the fortieth entry");
    check(menu.scroll == 33, "the window scrolled to keep it visible");
    ui_menu_confirm(&menu);

    char id[UI_MENU_ID_MAX] = "";
    check(ui_menu_take_action(&menu, id, sizeof(id)) == UI_ACTION_SWITCH,
          "a deep entry can still be confirmed");
    check(strcmp(id, "c40") == 0, "the id is the fortieth entry");
    free(many);
}

static void test_visible_rows_never_exceeds_the_list(void)
{
    check(ui_menu_visible_rows(240, 20) == 12, "rows fit the screen height");
    check(ui_menu_visible_rows(240, 0) == 0, "a zero row height is rejected");
    // A window taller than the list must still not claim more rows than there
    // are entries, or the drawing loop would walk past the end.
    check(ui_menu_visible_rows((int)UI_MENU_MAX * 20 + 100, 20) == UI_MENU_MAX,
          "the row count never exceeds the list capacity");
}

int main(void)
{
    test_open_selects_the_playing_channel();
    test_stepping_wraps_and_keeps_the_selection_sane();
    test_confirm_reports_the_highlighted_channel();
    test_menu_times_out_but_not_the_video();
    test_banner_only_when_the_channel_actually_changed();
    test_unusable_entries_are_skipped_not_fatal();
    test_list_capacity_is_enforced();
    test_a_full_channel_list_loads();
    test_a_channel_missing_from_the_list_is_handled();
    test_open_with_no_list_does_nothing();
    test_scrolling_keeps_the_highlight_on_screen();
    test_confirm_then_take_yields_the_id_once();
    test_confirm_survives_a_long_list_and_scrolling();
    test_visible_rows_never_exceeds_the_list();

    if (failures) {
        printf("%d check(s) failed\n", failures);
        return 1;
    }
    printf("overlay menu tests: PASS\n");
    return 0;
}
