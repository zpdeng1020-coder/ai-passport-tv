<p align="right">
  <a href="CI-build-and-release.zh_CN.md">简体中文</a> · <strong>English</strong>
</p>

# Automated Build and Release

`.github/workflows/build-firmware.yml` builds and publishes firmware for tags and supports manual dispatch. Ordinary branch pushes do not trigger it. Keep this page synchronized with the workflow.

The build job restores ccache, runs `./tools/validate.sh --firmware` with ESP-IDF 5.5.3 for ESP32-C3, verifies the bootloader at `0x0`, partition table at `0x8000`, application at `0x10000`, 8 MB Flash arguments, and the protected Flash layout, then uploads `FoloToy-AI-Passport-full.bin`. A separate least-privilege release job publishes that artifact only for a tag.

All Actions are pinned to full commit SHAs. The build job has `contents: read`; only the tag release job receives `contents: write`.

## Browser flashing

Open `https://ai-passport.folotoy.cn/tools/web-flasher/`, connect the USB JTAG/serial device, select the release's merged `FoloToy-AI-Passport-full.bin`, choose a baud rate such as 460800, and write it from `0x0`. The browser performs local writing and verification; it does not upload the firmware file.

For board and flashing details, see [the hardware development guide](../../hardware-design/AI_HARDWARE_DEVELOPMENT_GUIDE.md).

## Release title

When this repository publishes firmware for several different applications from
the same source tree, a bare version number does not tell a user which app a
release is for. Give each tag a name that carries the version and the app, and
make sure the release title shows both.

- **Tag naming convention**: name tags as `v<version>-<app-name>` in
  lowercase-kebab-case, e.g. `v0.1.0-voice-keychain`, `v1.0.0-pocket-pomodoro`.
  The `<app-name>` is the application this release builds (see the
  `plays/<username>/<app-name>/` archive naming). A tag that only says a version
  is ambiguous when several apps share the tree.
- **After the release is published, confirm the release title.** The workflow
  sets the title to the tag name, so a correctly-named tag already reads
  `v0.1.0-voice-keychain`. If the tag did not include the app, or the title is
  not obvious at a glance, edit the release (GitHub: `Edit release`; GitLab:
  edit the tag) so the title is `<version> <app-name>`, e.g. `v0.1.0 Voice
  Keychain`. One quick scan of the release list should distinguish which app a
  release is for.
- **Keep title and tag consistent.** Use `<version>-<app-name>` so the app name
  is visible in both the tag list and the release list. Do not rely on a
  human-readable body alone to carry the app name.

## Release notes

A tag-triggered release succeeds only when the merged firmware and its release
notes travel together. After the release is published, write release notes that
explain the build to a user who may not have read the repository. Cover three
things:

- **What's new**: the features, behaviors, or fixes this release adds or
  changes compared with the previous one. Keep it user-facing, not a commit log.
- **How to build**: how to produce and verify the merged firmware
  (`./tools/validate.sh --firmware` or `idf.py build`), and the artifact file to
  flash (`FoloToy-AI-Passport-full.bin` flashed from `0x0`).
- **How to use**: how to flash the build (the browser flasher above) and the key
  interactions or hardware requirements of the release.

Write the release notes in English (and a Simplified Chinese version where the
project is bilingual) and link them from the GitHub/GitLab release. Keep them
consistent with `docs/CHANGELOG.md` for user-visible behavior.

## Building the server (`.github/workflows/build-server.yml`)

The server is a second, independently distributed program, built by its own workflow. This page is kept in step with that one as well.

- **Triggers**: tag pushes, pull requests touching `server/`, `tools/`, `packaging/`, `tests/` or `channels.txt`, and manual dispatch. Ordinary branch pushes do not trigger it.
- **Three-platform matrix**: Linux x86_64, macOS arm64 and Windows x86_64, each built on its own runner. PyInstaller cannot cross-build, so this is a matrix rather than one job with flags. `fail-fast: false`, because one platform failing is a fact about that platform and cancelling the others hides whether the same change broke them too.
- **Smoke test immediately after each build**: the artifact is started on the spot and checked for four things -- `--help` returns successfully, starting in an empty directory brings up both services, the media server's port accepts a connection, and a channel table appears beside the program.
  This step is not optional. Every defect found in this feature was of the form "builds cleanly, dies on start": PyInstaller ships what its static analysis can see, so a module reached only by name at run time is silently left out, and a pipeline that reported only "build succeeded" would have published every one of them.
- **Artifacts**: `build-server/tv-server-<system>-<arch>[.exe]`, published to the release for a tag and not committed, like the firmware.
- **A shared release concurrency group with the firmware workflow**: a release is updated by reading it and writing it back, so two workflows writing the same tag's release at once lose files. Both use the same `concurrency` group to queue, and `cancel-in-progress` stays `false` -- cancelling a release job halfway is how a published release ends up missing an asset.

## Related documents

- Firmware publishing to the community: [publish-to-community.md](../release/publish-to-community.md)
- Post-release follow-up: [project-completion.md](../release/project-completion.md)
