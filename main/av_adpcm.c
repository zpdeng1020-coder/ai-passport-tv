#include "av_adpcm.h"

static const int16_t k_step[AV_ADPCM_INDEX_MAX + 1] = {
        7,     8,     9,    10,    11,    12,    13,    14,    16,    17,
       19,    21,    23,    25,    28,    31,    34,    37,    41,    45,
       50,    55,    60,    66,    73,    80,    88,    97,   107,   118,
      130,   143,   157,   173,   190,   209,   230,   253,   279,   307,
      337,   371,   408,   449,   494,   544,   598,   658,   724,   796,
      876,   963,  1060,  1166,  1282,  1411,  1552,  1707,  1878,  2066,
     2272,  2499,  2749,  3024,  3327,  3660,  4026,  4428,  4871,  5358,
     5894,  6484,  7132,  7845,  8630,  9493, 10442, 11487, 12635, 13899,
    15289, 16818, 18500, 20350, 22385, 24623, 27086, 29794, 32767,
};
// Indexed by the low three bits of the code; the sign bit does not move it.
static const int8_t k_index_step[8] = {-1, -1, -1, -1, 2, 4, 6, 8};

bool av_adpcm_header_valid(const uint8_t block[AV_ADPCM_HEADER_BYTES]) {
    return block && block[2] <= AV_ADPCM_INDEX_MAX && block[3] == 0;
}

void av_adpcm_decode(const uint8_t *block, size_t samples, int16_t *out) {
    int predictor = (int16_t)(((unsigned)block[0] << 8) | block[1]);
    int index = block[2] > AV_ADPCM_INDEX_MAX ? (int)AV_ADPCM_INDEX_MAX : block[2];
    const uint8_t *nibbles = block + AV_ADPCM_HEADER_BYTES;
    for (size_t i = 0; i < samples; i++) {
        unsigned code = (i & 1u) ? (nibbles[i >> 1] >> 4) : (nibbles[i >> 1] & 0x0f);
        int step = k_step[index];
        int diff = step >> 3;
        if (code & 4u) diff += step;
        if (code & 2u) diff += step >> 1;
        if (code & 1u) diff += step >> 2;
        predictor += (code & 8u) ? -diff : diff;
        if (predictor > 32767) predictor = 32767;
        else if (predictor < -32768) predictor = -32768;
        index += k_index_step[code & 7u];
        if (index < 0) index = 0;
        else if (index > (int)AV_ADPCM_INDEX_MAX) index = (int)AV_ADPCM_INDEX_MAX;
        out[i] = (int16_t)predictor;
    }
}
