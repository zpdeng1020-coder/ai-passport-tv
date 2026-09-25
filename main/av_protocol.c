#include "av_protocol.h"
#include <string.h>
static uint32_t get32(const uint8_t *p) {
    return (uint32_t)p[0]<<24 | (uint32_t)p[1]<<16 | (uint32_t)p[2]<<8 | p[3];
}
static void put32(uint8_t *p, uint32_t n) {
    p[0]=n>>24; p[1]=n>>16; p[2]=n>>8; p[3]=n;
}
bool av_header_decode(const uint8_t p[AV_HEADER_BYTES], av_header_t *h) {
    // p[6] is the high half of the reserved field and stays zero. p[7] carries
    // the flags, which only AV_VIDEO has any use for.
    if (!p || !h || memcmp(p,"FAV1",4) || p[4]!=1 || p[6]) return false;
    h->type=p[5]; h->flags=p[7]; h->session=get32(p+8); h->seq=get32(p+12);
    h->pts_ms=get32(p+16); h->length=get32(p+20);
    switch(h->type) {
    case AV_HELLO: case AV_CONFIG: case AV_ERROR:
        if (h->flags) return false;
        return h->length>0 && h->length<=AV_CONTROL_MAX;
    case AV_END:
        if (h->flags) return false;
        return h->length==0;
    case AV_AUDIO:
        if (h->flags) return false;
        return h->length==AV_AUDIO_BYTES;
    case AV_PALETTE:
        if (h->flags) return false;
        return h->length==AV_PALETTE_BYTES;
    case AV_VIDEO:
        if (h->flags & ~AV_VIDEO_CONTINUES) return false;
        return h->length>0 && h->length<=AV_VIDEO_MAX;
    default: return false;
    }
}
void av_header_encode(uint8_t p[AV_HEADER_BYTES], const av_header_t *h) {
    memcpy(p,"FAV1",4); p[4]=1; p[5]=h->type; p[6]=0; p[7]=h->flags;
    put32(p+8,h->session); put32(p+12,h->seq);
    put32(p+16,h->pts_ms); put32(p+20,h->length);
}
bool av_stream_accept(av_stream_t *s, const av_header_t *h) {
    if (!s || !h || s->ended) return false;
    if (!s->configured) {
        if (h->type!=AV_CONFIG || !h->session || h->seq || h->pts_ms) return false;
        s->session=h->session; s->configured=true; s->next_seq=1; return true;
    }
    if (h->session!=s->session || h->seq!=s->next_seq || h->seq==UINT32_MAX) return false;
    switch(h->type) {
    case AV_AUDIO:
        if (h->pts_ms!=s->audio_next_pts || h->pts_ms>UINT32_MAX-AV_AUDIO_MS) return false;
        s->audio_seen=true; s->audio_next_pts=h->pts_ms+AV_AUDIO_MS; break;
    case AV_VIDEO:
        // A frame cut across several packets sends them all under one
        // timestamp, so only the packet that begins a frame moves the clock
        // forward. A continuation is refused when no frame is under way: there
        // would be nothing for it to continue, and accepting it would let a
        // stream whose first packet was lost look like a valid one.
        if (h->flags & AV_VIDEO_CONTINUES) {
            if (!s->video_seen) return false;
        } else {
            if (s->video_seen && h->pts_ms<=s->video_pts) return false;
            s->video_seen=true; s->video_pts=h->pts_ms;
        }
        break;
    case AV_PALETTE:
        // No timeline of its own; it takes its place in the sequence and
        // nothing else. The server sends one before the first frame, and may
        // send another at any point to retune the colours.
        break;
    case AV_END: case AV_ERROR: s->ended=true; break;
    default: return false;
    }
    s->next_seq++; return true;
}
uint32_t av_elapsed_ms(uint32_t now, uint32_t last) {
    uint32_t delta=now-last;
    return delta>INT32_MAX ? 0 : delta;
}
bool av_json_depth_safe(const char *json, size_t length, unsigned max_depth) {
    if (!json || !length || length>AV_CONTROL_MAX) return false;
    unsigned depth=0;
    bool string=false, escape=false;
    for(size_t i=0;i<length;i++) {
        unsigned char c=(unsigned char)json[i];
        if (!c) return false;
        if (string) {
            if (c<0x20) return false;
            if (escape) escape=false;
            else if (c=='\\') escape=true;
            else if (c=='\"') string=false;
        } else if (c=='\"') string=true;
        else if (c=='{' || c=='[') { if (++depth>max_depth) return false; }
        else if (c=='}' || c==']') { if (!depth) return false; depth--; }
    }
    return !depth && !string && !escape;
}
// Returns the matching index, or count when the id is absent. Callers must not
// treat 0 as "not found": with a removed channel that would make the first UP
// press jump to index 1 and leave index 0 reachable only by wrapping around.
unsigned av_channel_index_of(const char *const *ids, unsigned count, const char *id) {
    if (!ids || !id || !count) return count;
    for (unsigned i=0;i<count;i++)
        if (ids[i] && !strcmp(ids[i],id)) return i;
    return count;
}
bool av_channel_step(const char *const *ids, unsigned count, unsigned *index,
                     int delta, char *out, size_t out_size) {
    if (!ids || !count || !index || !out || out_size < 2 || !delta) return false;
    unsigned next = (unsigned)(((int)(*index % count) + delta + (int)count) % (int)count);
    const char *id = ids[next];
    size_t n = id ? strlen(id) : 0;
    if (!id || !n || n >= out_size || n > AV_CHANNEL_ID_MAX-1) return false;
    memcpy(out,id,n+1);
    *index = next;
    return true;
}
bool av_video_decode(const uint8_t *payload, size_t length, av_video_t *v) {
    if (!payload || !v || length < 2u) return false;
    unsigned first=payload[0], count=payload[1];
    // count==0 is not "an empty packet", it is a payload that says nothing;
    // refusing it keeps a stalled sender from looking like a silent one.
    if (!count || count>AV_STRIPES_PER_PACKET) return false;
    if (first>=AV_STRIPES || first+count>AV_STRIPES) return false;
    size_t table=2u+2u*(size_t)count;
    if (length<table) return false;
    size_t total=0;
    for (unsigned i=0;i<count;i++) {
        total += ((size_t)payload[2+2*i]<<8) | (size_t)payload[3+2*i];
    }
    // The lengths must account for the payload exactly. A short one would leave
    // bytes nobody reads and a long one would run off the end; either way the
    // frame being described is not the frame that was sent.
    if (total!=length-table) return false;
    v->first=first; v->count=count;
    v->table=payload+2; v->data=payload+table; v->data_length=total;
    return true;
}
bool av_video_stripe(const av_video_t *v, unsigned n,
                     const uint8_t **data, size_t *length) {
    if (!v || !data || !length || n>=v->count) return false;
    // The table sits in the payload before the data, so the lengths are read
    // from `table` and the bytes they describe from `data`. Reading them from
    // `data` would take the first stripe's own first bytes as a length.
    const uint8_t *table=v->table;
    size_t at=0;
    for (unsigned i=0;i<n;i++) {
        at += ((size_t)table[2*i]<<8) | (size_t)table[2*i+1];
    }
    if (at>v->data_length) return false;
    *data=v->data+at;
    *length=((size_t)table[2*n]<<8) | (size_t)table[2*n+1];
    return *length<=v->data_length-at;
}
void av_palette_decode(const uint8_t raw[AV_PALETTE_BYTES],
                       uint16_t out[AV_PALETTE_ENTRIES]) {
    for (unsigned i=0;i<AV_PALETTE_ENTRIES;i++) {
        out[i]=(uint16_t)(((uint16_t)raw[2*i]<<8) | raw[2*i+1]);
    }
}
uint16_t av_palette_rgb565(uint8_t index) {
    uint16_t r3=(index>>5)&0x7u, g3=(index>>2)&0x7u, b2=index&0x3u;
    // Each field is replicated to fill its slot so that full scale in maps to
    // full scale out: 3 bits into 5, 3 into 6, 2 into 5. Multiplying by 255/7
    // instead would differ by one count in 220 of the 256 entries and then
    // agree again once quantised to RGB565 -- checked against the palette read
    // back out of ffmpeg, which matched for all 256. Shifts, not multiplies.
    uint16_t r5=(uint16_t)((r3<<2)|(r3>>1));
    uint16_t g6=(uint16_t)((g3<<3)|g3);
    uint16_t b5=(uint16_t)((b2<<3)|(b2<<1)|(b2>>1));
    return (uint16_t)((r5<<11)|(g6<<5)|b5);
}
void av_expand_indexed(uint8_t *buf, size_t pixels, const uint16_t *palette) {
    // Backwards: see the note on the declaration. Walking forwards would
    // overwrite the index at buf[1] on the very first step.
    size_t i=pixels;
    while (i-- > 0) {
        uint16_t colour=palette[buf[i]];
        buf[2*i]=(uint8_t)(colour>>8);
        buf[2*i+1]=(uint8_t)(colour&0xffu);
    }
}
