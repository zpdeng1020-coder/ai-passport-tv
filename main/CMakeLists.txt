idf_component_register(
    SRCS "main.c"
         "ui_pixel.c"
         "ui_pixel_math.c"
         "demo_display.c"
         "demo_button.c"
         "demo_audio.c"
         "demo_battery.c"
         "demo_radio.c"
         "demo_wifi.c"
         "demo_ble.c"
         "demo_low_power.c"
         "av_player.c"
         "av_protocol.c"
         "ui_text.c"
         "ui_menu.c"
         "av_provision.cpp"
         "av_provision_policy.c"
         "av_settings.c"
         "av_server_addr.c"
         "av_store.c"
    INCLUDE_DIRS "."
    # The non-ASCII glyph table, written by tools/make_font.py. Embedded as a
    # blob rather than included as a header: the same data as C source is about
    # a megabyte of hex, which every rebuild would reparse. ui_text.c reads it.
    EMBED_FILES "ui_font_cjk.bin"
    REQUIRES bsp bt esp_event esp_hw_support esp_netif esp_timer esp_wifi nvs_flash json esp_rom
             esp-wifi-connect
)
# The glyph blob is checked in, so a fresh clone builds without extra steps. Say
# what to run if it is ever missing: EMBED_FILES would otherwise fail with a
# path error that does not mention the generator.
if(NOT EXISTS "${CMAKE_CURRENT_SOURCE_DIR}/ui_font_cjk.bin")
    message(FATAL_ERROR
        "main/ui_font_cjk.bin is missing. Regenerate it with:\n"
        "    python3 tools/make_font.py --text-file channels.txt")
endif()
# Release/validation builds must never ingest a developer's private header.
if(AV_PUBLIC_BUILD)
    target_compile_definitions(${COMPONENT_LIB} PRIVATE AV_PUBLIC_BUILD=1)
endif()
