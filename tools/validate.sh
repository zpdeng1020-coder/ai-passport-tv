#!/usr/bin/env bash
set -euo pipefail

mode="${1:---all}"
repo_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"

usage() {
    echo "Usage: $0 [--all|--static|--firmware|--prototype]" >&2
}

run_static_checks() {
    local actionlint_bin
    local test_dir

    python3 tools/check_repo.py

    actionlint_bin="${ACTIONLINT_BIN:-}"
    if [[ -z "${actionlint_bin}" ]]; then
        actionlint_bin="$(command -v actionlint || true)"
    fi
    if [[ -z "${actionlint_bin}" || ! -x "${actionlint_bin}" ]]; then
        actionlint_bin="$(./tools/install-actionlint.sh)"
    fi
    "${actionlint_bin}" -color .github/workflows/*.yml

    test_dir="$(mktemp -d /tmp/ai-passport-host-tests.XXXXXX)"
    "${CC:-cc}" -std=c11 -Wall -Wextra -Werror -Imain \
        tests/test_ui_pixel_math.c main/ui_pixel_math.c \
        -o "${test_dir}/test_ui_pixel_math"
    "${test_dir}/test_ui_pixel_math"
    "${CC:-cc}" -std=c11 -Wall -Wextra -Werror -Imain \
        tests/test_av_protocol.c main/av_protocol.c \
        -o "${test_dir}/test_av_protocol"
    "${test_dir}/test_av_protocol"
    # Overlay text and menu state: pure logic, so they run without a device.
    "${CC:-cc}" -std=c11 -Wall -Wextra -Werror -Imain \
        tests/test_ui_text.c main/ui_text.c \
        -o "${test_dir}/test_ui_text"
    "${test_dir}/test_ui_text"
    "${CC:-cc}" -std=c11 -Wall -Wextra -Werror -Imain \
        tests/test_ui_menu.c main/ui_menu.c \
        -o "${test_dir}/test_ui_menu"
    "${test_dir}/test_ui_menu"
    # Backlight levels: pure arithmetic, and the ends are where it goes wrong.
    "${CC:-cc}" -std=c11 -Wall -Wextra -Werror -Imain \
        tests/test_av_settings.c main/av_settings.c \
        -o "${test_dir}/test_av_settings"
    "${test_dir}/test_av_settings"
    # The server address arrives as text someone typed or pasted, so the cases
    # that matter are the messy ones. A wrong answer here is a device that
    # silently never connects, which is the hardest kind of fault to see.
    "${CC:-cc}" -std=c11 -Wall -Wextra -Werror -Imain \
        tests/test_av_server_addr.c main/av_server_addr.c \
        -o "${test_dir}/test_av_server_addr"
    "${test_dir}/test_av_server_addr"
    # Which mode the device starts in. Pure logic, and both answers lead to very
    # different behaviour on power-up.
    "${CC:-cc}" -std=c11 -Wall -Wextra -Werror -Imain \
        tests/test_av_provision_policy.c main/av_provision_policy.c \
        -o "${test_dir}/test_av_provision_policy"
    "${test_dir}/test_av_provision_policy"
    python3 tests/test_verify_firmware.py
    python3 tests/test_tv_server.py
    python3 tests/test_video_import.py
    # Where the writable data lives, and how the two answers it used to give --
    # code location and data location -- are kept apart. Getting that wrong is
    # silent: the program still runs and the user's channel list goes somewhere
    # they will not find it.
    python3 tests/test_datadir.py
    # Fetching ffmpeg on a machine that does not have it. The network is stubbed
    # out; what is checked is the platform choice, the hash check, and what
    # happens when either is wrong.
    python3 tests/test_ffmpeg_fetch.py
    # The sub-command protocol between the launcher and a bundled executable.
    # This is the part that only exists when packaged, and where four separate
    # failures were found by hand.
    python3 tests/test_subcommands.py
    # Chinese output on a console whose encoding cannot represent it. Invisible
    # on macOS and Linux, where the console is already UTF-8, and fatal on
    # Windows at the first line the program prints.
    python3 tests/test_console.py
    # The channel page, which had no tests until a user found that its
    # availability check called working channels dead. The names the page's
    # JavaScript reads and the names the server writes are connected by
    # nothing, so a typo in one is invisible until someone tries to use it.
    python3 tests/test_channel_config.py
    # The flashing instructions, which are written down three times -- the page,
    # the English README and the Chinese one -- and had drifted together onto a
    # step that does not work on this hardware. Nothing connected the three, so
    # this does.
    python3 tests/test_flash_instructions.py
    # Certificate authorities on a machine that is not the build machine. The
    # same shape as the console problem above -- invisible where the code was
    # written, fatal in the hands of whoever downloaded it -- and this one was
    # found by a user rather than by a test.
    python3 tests/test_certs.py
    # Live transcoding and the device/server CONFIG contract. Networked cases
    # skip themselves unless AV_LIVE_TEST=1, so this stays offline by default.
    python3 tests/test_live_transcode.py
    rm -rf "${test_dir}"
    echo "Host tests: PASS"
}

run_firmware_checks() (
    local validation_build_dir
    local defaults="${repo_root}/sdkconfig.defaults"
    local artifact="FoloToy-AI-Passport-full.bin"
    if [[ "${1:-demo}" == prototype ]]; then
        defaults="${defaults};${repo_root}/sdkconfig.av-prototype"
        artifact="FoloToy-AI-Passport-prototype-public.bin"
    fi

    if ! command -v idf.py >/dev/null 2>&1; then
        echo "ERROR: idf.py is not available; activate ESP-IDF 5.5.3 first." >&2
        return 1
    fi

    validation_build_dir="$(mktemp -d /tmp/ai-passport-firmware.XXXXXX)"
    trap 'case "${validation_build_dir}" in /tmp/ai-passport-firmware.*) rm -rf -- "${validation_build_dir}" ;; esac' EXIT

    SDKCONFIG_DEFAULTS="${defaults}" \
        idf.py -B "${validation_build_dir}" \
        -D AV_PUBLIC_BUILD=ON \
        -D "SDKCONFIG=${validation_build_dir}/sdkconfig" build
    idf.py -B "${validation_build_dir}" merge-bin \
        -o "${validation_build_dir}/FoloToy-AI-Passport-full.bin"
    python3 tools/verify_firmware.py "${validation_build_dir}"
    mkdir -p "${repo_root}/build"
    install -m 0644 \
        "${validation_build_dir}/FoloToy-AI-Passport-full.bin" \
        "${repo_root}/build/${artifact}"
    echo "Firmware build: PASS"
)

cd "${repo_root}"
case "${mode}" in
    --all)
        run_static_checks
        run_firmware_checks
        ;;
    --static)
        run_static_checks
        ;;
    --firmware)
        run_firmware_checks
        ;;
    --prototype)
        run_static_checks
        run_firmware_checks prototype
        ;;
    *)
        usage
        exit 2
        ;;
esac
