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
         "av_demo.c"
         "net_demo.c"
         "jpeg_bench.c"
         "av_protocol.c"
         "av_adpcm.c"
         "ui_text.c"
         "ui_menu.c"
         "av_provision.cpp"
         "av_provision_policy.c"
         "av_settings.c"
         "av_channel_policy.c"
         "av_server_addr.c"
         "av_store.c"
    INCLUDE_DIRS "."
    # The non-ASCII glyph table, written by tools/make_font.py. Embedded as a
    # blob rather than included as a header: the same data as C source is about
    # a megabyte of hex, which every rebuild would reparse. ui_text.c reads it.
    EMBED_FILES "ui_font_cjk.bin"
    REQUIRES bsp bt esp_driver_usb_serial_jtag esp_event esp_hw_support esp_netif
             esp_timer esp_wifi nvs_flash json esp_rom
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
# av_player.c guards the capture with #ifdef, so the option has to reach it as
# a definition and not only as a Kconfig symbol.
if(CONFIG_AV_SCREEN_CAPTURE)
    target_compile_definitions(${COMPONENT_LIB} PRIVATE AV_SCREEN_CAPTURE=1)
endif()
# The playback demo's clip, embedded only when the demo is asked for. Generated
# by tools/make_demo_clip.py; it is 1.2 MB, so a build without the option must
# not carry it.
if(CONFIG_AV_DEMO_PLAYBACK)
    if(NOT EXISTS "${CMAKE_CURRENT_SOURCE_DIR}/demo_clip.bin")
        message(FATAL_ERROR "main/demo_clip.bin is missing. Run: python tools/make_demo_clip.py")
    endif()
    target_add_binary_data(${COMPONENT_LIB} "demo_clip.bin" BINARY)
endif()

if(CONFIG_AV_JPEG_BENCH)
    if(NOT EXISTS "${CMAKE_CURRENT_SOURCE_DIR}/jpeg_test.bin")
        message(FATAL_ERROR "main/jpeg_test.bin is missing. Run: python tools/make_jpeg_clip.py")
    endif()
    target_add_binary_data(${COMPONENT_LIB} "jpeg_test.bin" BINARY)
endif()
