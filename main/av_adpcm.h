// IMA ADPCM decoding for the FAV1 audio packet: no ESP-IDF dependencies.
#pragma once
#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

// An audio block is a four-byte state header followed by the samples, four bits
// each, two to a byte with the earlier sample in the LOW nibble:
//
//   [s16 predictor, big-endian][u8 step index][u8 zero][nibbles...]
//
// The header is the decoder's state *before the first sample of the block*, not
// the first sample itself, so a block holds exactly the samples it is sized for
// and every block can be decoded without the one before it. Blocks that follow
// one another from a continuous encoder carry the state that decoding the
// previous block would have reached, so the two ways of decoding agree; the
// header is what lets a block stand alone.
#define AV_ADPCM_HEADER_BYTES 4u
#define AV_ADPCM_INDEX_MAX 88u

// False when the header cannot have come from an encoder: a step index past the
// table, or a reserved byte that is not zero. The receiver ends the session on
// false, the same rule every other malformed packet gets.
bool av_adpcm_header_valid(const uint8_t block[AV_ADPCM_HEADER_BYTES]);

// Decode `samples` samples from `block` into `out`. The block must be
// AV_ADPCM_HEADER_BYTES + (samples+1)/2 bytes long and its header valid; the
// caller owns both checks. An out-of-range step index is clamped rather than
// trusted, so a header that skipped the check cannot index outside the table.
void av_adpcm_decode(const uint8_t *block, size_t samples, int16_t *out);
