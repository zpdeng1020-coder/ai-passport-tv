// Backlight level arithmetic, tested without a device.
//
// The cases worth having are the ends: stepping up from the brightest and down
// from the dimmest. Getting either wrong means a button that appears to do
// nothing, which is exactly the failure this whole feature was added to fix.
#include "av_settings.h"

#include <stdio.h>

static int failures;

static void check(int condition, const char *what)
{
    if (!condition) {
        printf("FAIL: %s\n", what);
        failures++;
    }
}

static void test_levels_are_ordered_and_in_range(void)
{
    check(AV_BRIGHTNESS_COUNT == 5, "five levels as chosen");
    for (unsigned i = 0; i < AV_BRIGHTNESS_COUNT; i++) {
        uint8_t level = AV_BRIGHTNESS_LEVELS[i];
        check(level > 0 && level <= 100, "every level is a usable percentage");
        if (i > 0) {
            check(level < AV_BRIGHTNESS_LEVELS[i - 1],
                  "levels descend, so the first is the brightest");
        }
    }
    // The dimmest level is the one that decides whether the screen is still
    // readable; zero would make the device impossible to recover by touch.
    check(AV_BRIGHTNESS_LEVELS[AV_BRIGHTNESS_COUNT - 1] > 0,
          "the dimmest level is not zero, so the screen stays readable");
}

static void test_step_up_from_the_brightest_wraps(void)
{
    // Up from index 0 must go somewhere, or the button does nothing at the top.
    unsigned next = av_brightness_step(0, true);
    check(next == AV_BRIGHTNESS_COUNT - 1,
          "stepping up from the brightest wraps to the dimmest");
}

static void test_step_down_from_the_dimmest_wraps(void)
{
    unsigned next = av_brightness_step(AV_BRIGHTNESS_COUNT - 1, false);
    check(next == 0, "stepping down from the dimmest wraps to the brightest");
}

static void test_a_step_is_reversible(void)
{
    // Any level, stepped one way and back the other, returns to where it was.
    // Without this a long hold could drift.
    for (unsigned i = 0; i < AV_BRIGHTNESS_COUNT; i++) {
        check(av_brightness_step(av_brightness_step(i, true), false) == i,
              "up then down returns to the same level");
        check(av_brightness_step(av_brightness_step(i, false), true) == i,
              "down then up returns to the same level");
    }
}

static void test_stepping_all_the_way_round_visits_every_level_once(void)
{
    unsigned index = 0;
    unsigned seen = 0;
    for (unsigned i = 0; i < AV_BRIGHTNESS_COUNT; i++) {
        check((seen & (1u << index)) == 0, "each level is visited once per cycle");
        seen |= 1u << index;
        index = av_brightness_step(index, false);
    }
    check(index == 0, "a full cycle returns to the start");
    check(seen == (1u << AV_BRIGHTNESS_COUNT) - 1u, "every level was visited");
}

static void test_out_of_range_index_is_clamped_not_wild(void)
{
    // A stored index from a build with more levels must not read past the table.
    check(av_brightness_percent(AV_BRIGHTNESS_COUNT) ==
              AV_BRIGHTNESS_LEVELS[AV_BRIGHTNESS_COUNT - 1],
          "an index past the end clamps to the dimmest level");
    check(av_brightness_percent(9999u) == AV_BRIGHTNESS_LEVELS[AV_BRIGHTNESS_COUNT - 1],
          "a wildly out of range index still clamps");
    // Stepping from a bad index starts from the brightest rather than computing
    // from a value that was never valid.
    check(av_brightness_step(AV_BRIGHTNESS_COUNT, false) == 1u,
          "stepping down from a bad index behaves as if starting at the brightest");
}

static void test_nearest_index_finds_every_level(void)
{
    for (unsigned i = 0; i < AV_BRIGHTNESS_COUNT; i++) {
        check(av_brightness_nearest_index(AV_BRIGHTNESS_LEVELS[i]) == i,
              "an exact level maps back to its own index");
    }
}

static void test_nearest_index_handles_values_between_levels(void)
{
    // 75 is 5 below 80 and 15 above 60, so it belongs to 80 (index 1).
    check(av_brightness_nearest_index(75) == 1, "a value between levels picks the closer one");
    // 50 is 10 from both 60 and 40. Ties must resolve to something stable, and
    // the first match wins, which is the brighter of the two.
    check(av_brightness_nearest_index(50) == 1 || av_brightness_nearest_index(50) == 2,
          "a tie resolves to one of the adjacent levels");
    check(av_brightness_nearest_index(0) == AV_BRIGHTNESS_COUNT - 1,
          "zero maps to the dimmest level, never below it");
    check(av_brightness_nearest_index(255) == 0, "255 maps to the brightest level");
}

static void test_valid_percent_rejects_anything_not_a_level(void)
{
    check(av_brightness_valid_percent(100), "100 is a level");
    check(av_brightness_valid_percent(20), "20 is a level");
    check(!av_brightness_valid_percent(0), "0 is not a level");
    check(!av_brightness_valid_percent(55), "an in-between value is not a level");
}

static void test_default_is_a_real_level(void)
{
    // The default has to survive the same validation a stored value does;
    // otherwise a fresh device would be rejected by its own check.
    check(av_brightness_valid_percent((uint8_t)AV_BRIGHTNESS_DEFAULT_PERCENT),
          "the default brightness is one of the levels");
}

int main(void)
{
    test_levels_are_ordered_and_in_range();
    test_step_up_from_the_brightest_wraps();
    test_step_down_from_the_dimmest_wraps();
    test_a_step_is_reversible();
    test_stepping_all_the_way_round_visits_every_level_once();
    test_out_of_range_index_is_clamped_not_wild();
    test_nearest_index_finds_every_level();
    test_nearest_index_handles_values_between_levels();
    test_valid_percent_rejects_anything_not_a_level();
    test_default_is_a_real_level();
    if (failures) {
        printf("test_av_settings: %d failure(s)\n", failures);
        return 1;
    }
    printf("test_av_settings: all cases passed\n");
    return 0;
}
