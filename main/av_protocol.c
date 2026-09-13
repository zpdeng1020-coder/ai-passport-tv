#include "av_protocol.h"
#include <string.h>
static uint32_t get32(const uint8_t *p) {
    return (uint32_t)p[0]<<24 | (uint32_t)p[1]<<16 | (uint32_t)p[2]<<8 | p[3];
}
static void put32(uint8_t *p, uint32_t n) {
    p[0]=n>>24; p[1]=n>>16; p[2]=n>>8; p[3]=n;
}
bool av_header_decode(const uint8_t p[AV_HEADER_BYTES], av_header_t *h) {
    if (!p || !h || memcmp(p,"FAV1",4) || p[4]!=1 || p[6] || p[7]) return false;
    h->type=p[5]; h->session=get32(p+8); h->seq=get32(p+12);
    h->pts_ms=get32(p+16); h->length=get32(p+20);
    switch(h->type) {
    case AV_HELLO: case AV_CONFIG: case AV_ERROR:
        return h->length>0 && h->length<=AV_CONTROL_MAX;
    case AV_END: return h->length==0;
    case AV_AUDIO: return h->length==AV_AUDIO_BYTES;
    case AV_VIDEO: return h->length>0 && h->length<=AV_VIDEO_MAX;
    default: return false;
    }
}
void av_header_encode(uint8_t p[AV_HEADER_BYTES], const av_header_t *h) {
    memcpy(p,"FAV1",4); p[4]=1; p[5]=h->type; p[6]=p[7]=0;
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
        if (s->video_seen && h->pts_ms<=s->video_pts) return false;
        s->video_seen=true; s->video_pts=h->pts_ms; break;
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
bool av_pack_rgb888_x2(uint8_t *stripe, unsigned sy, unsigned l, unsigned t,
                       unsigned r, unsigned b, const uint8_t *rgb) {
    if (!stripe || !rgb || sy%AV_STRIPE_ROWS || sy>=AV_HEIGHT || l>r || t>b ||
        r>=AV_VIDEO_WIDTH || b>=AV_VIDEO_HEIGHT) return false;
    unsigned first=sy/2, end=(sy+AV_STRIPE_ROWS)/2;
    if (b<first || t>=end) return false;
    if (first<t) first=t;
    if (end>b+1) end=b+1;
    // Skip whole source rows, NOT destination-width rows. This also handles
    // the lower half of an MCU and the clipped 8-row MCU at source y=112.
    rgb+=(size_t)(first-t)*(r-l+1)*3;
    for (unsigned y=first;y<end;y++) for (unsigned x=l;x<=r;x++) {
        uint16_t c=((uint16_t)(rgb[0]&0xf8)<<8) | ((uint16_t)(rgb[1]&0xfc)<<3) | (rgb[2]>>3);
        size_t i=((y*2-sy)*AV_WIDTH+x*2)*2;
        stripe[i]=stripe[i+2]=stripe[i+AV_WIDTH*2]=stripe[i+AV_WIDTH*2+2]=c>>8;
        stripe[i+1]=stripe[i+3]=stripe[i+AV_WIDTH*2+1]=stripe[i+AV_WIDTH*2+3]=c;
        rgb+=3;
    }
    return true;
}
bool av_pack_rgb888(uint8_t *stripe, unsigned sy, unsigned l, unsigned t,
                    unsigned r, unsigned b, const uint8_t *rgb) {
    if (!stripe || !rgb || sy%16 || sy>=AV_HEIGHT || l>r || t>b ||
        r>=AV_WIDTH || t<sy || b>=sy+16 || b>=AV_HEIGHT) return false;
    for (unsigned y=t;y<=b;y++) for (unsigned x=l;x<=r;x++) {
        uint16_t c=((uint16_t)(rgb[0]&0xf8)<<8) | ((uint16_t)(rgb[1]&0xfc)<<3) | (rgb[2]>>3);
        size_t i=((y-sy)*AV_WIDTH+x)*2; stripe[i]=c>>8; stripe[i+1]=c; rgb+=3;
    }
    return true;
}
