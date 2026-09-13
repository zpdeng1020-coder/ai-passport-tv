<p align="right">
  <a href="CHANGELOG.zh_CN.md">简体中文</a> · <strong>English</strong>
</p>

# Changelog

## Unreleased

- The server is now a download-and-run: single-file executables for Linux, macOS and Windows (`tv-server-*`), double-clicked with no Python to install. The first run fetches ffmpeg itself (about 21-31 MB, once; a machine that already has ffmpeg never touches the network), announcing the size beforehand and verifying a pinned sha256 -- a mismatch writes and runs nothing. ffmpeg is not bundled because those builds are `--enable-gpl`, and redistributing one carries the obligation to supply its source, whereas fetching it on the user's own machine does not. The executables are distributed through the releases page rather than committed.

- Server data is now separate from the code location: `tools/datadir.py` resolves a writable directory from `TV_DATA_DIR`, then the program's own directory when a real write succeeds, then the per-user location, and the chosen path is printed at start-up. The two used to share one `__file__`-derived path, which coincides only from a checkout; bundled, it names a temporary directory deleted on exit, so a saved channel list was lost. Children now find the `server` package through `PYTHONPATH` rather than the working directory, which separates those two duties.

- Fixed the first save of a channel list not restarting the media server: the check compared two timestamps, which requires the file to already exist, while a new data directory starts empty and the first save is exactly the case of a file appearing. Also fixed the restart passing a channel name from the old table -- a first save replaces the built-in channels entirely, so that name is never in the new one, and the server treats `--channel` as a required choice, exiting with nothing listening. It now falls back to the first entry of the new table.

- Windows console encoding: every message this program prints is Chinese, and a Windows console defaults to the system code page, so on an English system (cp1252) the first character raised `UnicodeEncodeError` and ended the process -- the user's first sight of the program was a crash. `tools/console.py` sets UTF-8 before anything is printed, called from all four entry points; a test reproduces the failure with a cp1252 stream, and a scan keeps a new script from being missed.

- Repository CI fixes: `main/ui_menu.c` used the POSIX `strnlen`, which glibc exposes only behind a feature-test macro, so macOS passed locally while Linux CI failed continuously; adding `_POSIX_C_SOURCE` was checked by cross-compiling to aarch64-linux-gnu and reproducing the original errors without it. Added a three-platform build workflow that starts each build as a smoke test, sharing a release concurrency group with the firmware workflow so the two cannot rewrite one release at the same time and lose files.

- Renamed the server side from `av` to `tv`: the module `server/av_server.py` to `tv_server.py`, along with the executable, release assets, environment variables and documentation. The firmware's `av_` prefix is unchanged -- that is audio/video codec work, which is not what this project does -- as are the cross-device `AV_PAIRING_TOKEN` and the firmware module name `av_protocol`.

- README: added the download-and-run path, the Windows firewall rule for port 8096 (found by measurement -- Windows blocks inbound connections by default and the device only says it cannot connect, which gives no hint of a firewall), the unsigned-program prompt and how to get past it, and replaced "what has not been tested" with a per-item account of what has been verified.

- Added real-time live channel playback to the local prototype: a `live` server subcommand transcodes an allowlisted channel with ffmpeg (paced with `-re`), and the device switches channels by reconnecting with a new channel name. The channel list travels in CONFIG under a key separate from the audio channel count; the two must not be merged, because a collision fails device validation and ends every session. Channel addresses come from an unverified community playlist and are intended for a private LAN test only.

- Changed FAV1 video input to baseline 160x120 YUV420 JPEG with x2 nearest-neighbor 320x240 display, retaining 12 fps/24 KiB and PCM/timestamps. Import preserves display aspect ratio with centered black borders. Corrected MCU stripe clipping/final-row handling without increasing DMA RAM; added full-frame boundary and actual media tests. Existing 320x240 media must be regenerated; device speed remains unmeasured.
- Added an opt-in local JPEG/PCM playback prototype with bounded FAV1 framing, a standard-library test media server, raw LCD ownership, cooperative playback tasks, and host tests. The original demo remains the default. Audio timing is explicitly estimated; physical synchronization and sustained performance require device measurements. Private network configuration and generated media remain outside version control.

- Added the supplied 80-byte CW2017 profile for the specified 520 mAh cell, including content/update-flag checks, verified writes, the required restart sequence, and bounded SOC-readiness polling.

- Expanded the environment bootstrap document: added Espressif's Git service mirror (`git.espressif.com.cn`) as the preferred mainland-China route for ESP-IDF v5.5.3 and its submodules, documented submodule long-wait/timeout handling, in-place repair, and the pinned-commit shallow fetch for large submodules such as `esp32-wifi-lib`, warned about stale per-repository Jihulab `insteadOf` residue, and added the official offline release archive as a last-resort fallback (learned from `esp-mosaico/esp-mosaico-vibe`).

- Reorganized the documentation by function area with a dual entry point: the root `AGENTS.md` is now a thin router (hard constraints + task routing only) and the detailed AI workflow lives in `docs/development/ai-guide.md`; `agent-guide.md` was folded in. `docs/development/` gained a second level (`engineering/`, `ci/`, `release/`), and the `plays/` application archive and `experiences/` moved into a `docs/reference/` area with a dedicated README. Removed `docs/software-design/` (empty scaffold); folded the three `assets/{fonts,images,music}/README` leaves into the `assets/` README; flattened the six `project-completion` sub-documents into a single file; and unified each directory to a single README, eliminating every `INDEX` file and a duplicated experience index. All cross-references and bibliographic links were updated; no content was dropped.

- Removed the obsolete app/test partition at `0x700000` and its related
  bootloader, validation, and documentation requirements. The fixed protected
  `cardid` partition and its CI checks remain unchanged.
- Documented a release-title convention for multi-app releases: name tags as `v<version>-<app-name>` (e.g. `v0.1.0-voice-keychain`) so the release title carries the version and the app, and confirm the title after the release is published so a release list is scannable by app.
- Added a post-release follow-up workflow: an `issue-suggestions` skill for filing user feedback as issues against the upstream project, an `experience-pr` skill for submitting reusable development experience as a documentation PR, a `docs/experiences/` directory for per-entry experience files, and supporting `project-completion`, `file-issues`, and experience-index documents.
- Simplified the tracked repository root: moved GitHub-recognized community documents into `.github/`, moved the changelog into `docs/`, updated every reference, and added a root-document allowlist to repository checks.
- Repository-wide language policy: every maintained Markdown default `.md` file is English, Simplified Chinese uses a paired `.zh_CN.md`, and both provide language switches. Static checks reject missing peers, missing switches, and Chinese prose in English defaults.
- Phase one of the AI development workflow: streamlined task-based context routing, unified local/CI validation, added PR checks and a template, and committed the dependency lock for reproducible builds.
- PR review fixes: pinned GitHub Actions to full commit SHAs, split build/release jobs by least privilege, disabled persisted sync checkout credentials, added Feature Request and Usage Question forms, clarified private security-report fallback, and corrected stale README, CI-trigger, and branch descriptions.
- Changed commit titles, PR titles, and PR bodies from Chinese-default to English; updated the Chinese punctuation rule so it no longer applies to PR descriptions.
- Reworked `build-firmware.yml` to pass `SDKCONFIG_DEFAULTS=sdkconfig.defaults`, enable `partitions.csv`, preserve the 8 MB image header, merge a flashable `FoloToy-AI-Passport-full.bin`, publish only that artifact, and use Actions cache v5.
- Integrated upstream PR #6 to resolve PR #4 conflicts: Wi-Fi, Bluetooth LE, radio lifecycle, and low-power demos; a 3 MB factory partition; build/menu/configuration updates; hardware-guide coverage; and bilingual capability tables.
- Defined English imperative Conventional Commit formatting for both commits and PR titles.
- Removed stale sync-workflow template comments and generalized an irrelevant Redis TTL rule to cache components.
- Added Chinese punctuation, credential safety, and recoverable file-deletion conventions.
- Expanded source-comment requirements for functions, state, ownership, concurrency, timing, registers, and magic values.
- Removed AI execution instructions from product READMEs so they remain human-facing product and repository overviews.
- Added `docs/development/agent-guide.md` as the focused AI workflow guide.
- Updated `AGENTS.md`, `docs/INDEX.md`, and the development index for the agent guide.
- Documented why the root README path is reserved for fork owners and how GitHub README precedence supports it.
- Created `main-update` from the upstream-aligned baseline and combined the repository-structure, firmware-CI, and upstream-sync work.
- Corrected the merged documentation index, workflow path, project tree, and CI references.
- Moved CI documentation from software design to `docs/development/`.
- Moved fork-only documentation assets from `assets/docs/` to `docs/assets/`.
- Moved the upstream English/Chinese project READMEs under `docs/` and renamed the documentation catalog to `docs/INDEX.md`.
- Initialized `AGENTS.md`, `CLAUDE.md`, and `CHANGELOG.md`.
- Standardized the initial project README language filenames.
- Added the `docs/`, `assets/`, and `skills/` directory structure.
- Moved the upstream hardware guide into `docs/hardware-design/`.
- Standardized subdirectory README capitalization and introduced fork conventions.
- Allowed fork-owned root README and supplemental documentation content on fork `main`.
- Added and documented the fork-only supplemental-document directory.
- Moved the build CI document to its dedicated CI branch before consolidation.
- Documented clean-`main` reasons, the direct-development exception, and Actions enablement for forks.
- Split the original agent rules into contribution, development, and fork documents with a compact root index.
- Updated software-design and project README references for the new documentation structure.
- Added the documentation catalog and task-triggered routing based on the earlier repository model.
- Added bilingual contribution, code-of-conduct, security, and support documents tailored to this ESP-IDF and fork workflow.
