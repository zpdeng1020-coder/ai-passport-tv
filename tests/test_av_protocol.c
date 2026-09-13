#include "av_protocol.h"
#include <assert.h>
#include <stdio.h>
#include <string.h>
static void header_tests(void) {
    av_header_t h={AV_VIDEO,0x12345678,0x23456789,0x3456789a,AV_VIDEO_MAX}, got;
    uint8_t wire[AV_HEADER_BYTES]; av_header_encode(wire,&h);
    assert(!memcmp(wire,"FAV1\1\4\0\0\x12\x34\x56\x78",12));
    assert(av_header_decode(wire,&got));
    assert(got.type==h.type && got.session==h.session && got.seq==h.seq && got.pts_ms==h.pts_ms && got.length==h.length);
    // Every fragmentation boundary reconstructs the same fixed header.
    for(unsigned cut=0;cut<=AV_HEADER_BYTES;cut++) {
        uint8_t copy[AV_HEADER_BYTES]; memcpy(copy,wire,cut); memcpy(copy+cut,wire+cut,AV_HEADER_BYTES-cut);
        assert(av_header_decode(copy,&got));
    }
    wire[0]='X'; assert(!av_header_decode(wire,&got)); wire[0]='F';
    wire[4]=2; assert(!av_header_decode(wire,&got)); wire[4]=1;
    wire[6]=1; assert(!av_header_decode(wire,&got)); wire[6]=0;
    wire[7]=1; assert(!av_header_decode(wire,&got)); wire[7]=0;
    for(unsigned type=0;type<256;type++) {
        h.type=type; h.length=640; av_header_encode(wire,&h);
        assert(av_header_decode(wire,&got)==(type>=AV_HELLO && type<=AV_ERROR && type!=AV_END));
    }
    // Boundaries are derived from the limits themselves, so raising the control
    // ceiling for a longer channel list does not silently invalidate this test.
    const uint32_t lengths[]={0,1,639,640,641,
                              AV_CONTROL_MAX-1,AV_CONTROL_MAX,AV_CONTROL_MAX+1,
                              AV_VIDEO_MAX,UINT32_MAX};
    for(unsigned type=1;type<=6;type++) for(unsigned i=0;i<sizeof(lengths)/sizeof(*lengths);i++) {
        uint32_t n=lengths[i]; h.type=type; h.length=n; av_header_encode(wire,&h);
        bool expected=type==AV_AUDIO?n==640
                     :type==AV_VIDEO?(n>0 && n<=AV_VIDEO_MAX)
                     :type==AV_END?n==0
                     :(n>0 && n<=AV_CONTROL_MAX);
        assert(av_header_decode(wire,&got)==expected);
    }
}
static void stream_tests(void) {
    av_stream_t s={0}; av_header_t h={AV_CONFIG,42,0,0,100};
    av_header_t bad=h; bad.session=0; assert(!av_stream_accept(&s,&bad));
    bad=h; bad.type=AV_AUDIO; assert(!av_stream_accept(&s,&bad));
    bad=h; bad.seq=1; assert(!av_stream_accept(&s,&bad));
    assert(av_stream_accept(&s,&h));
    h=(av_header_t){AV_AUDIO,42,1,0,640}; assert(av_stream_accept(&s,&h));
    h.seq=2; h.pts_ms=20; assert(av_stream_accept(&s,&h));
    h=(av_header_t){AV_VIDEO,42,3,0,100}; assert(av_stream_accept(&s,&h)); // cross-media PTS may fall
    h.seq=4; h.pts_ms=83; assert(av_stream_accept(&s,&h));
    h.seq=5; assert(!av_stream_accept(&s,&h)); // repeated video PTS
    h=(av_header_t){AV_AUDIO,42,5,40,640};
    bad=h; bad.session=43; assert(!av_stream_accept(&s,&bad));
    bad=h; bad.seq=6; assert(!av_stream_accept(&s,&bad));
    bad=h; bad.pts_ms=60; assert(!av_stream_accept(&s,&bad));
    assert(av_stream_accept(&s,&h));
    h=(av_header_t){AV_END,42,6,10000,0}; assert(av_stream_accept(&s,&h));
    assert(!av_stream_accept(&s,&h));
    memset(&s,0,sizeof(s)); h=(av_header_t){AV_CONFIG,43,0,0,100}; assert(av_stream_accept(&s,&h));
    h=(av_header_t){AV_AUDIO,43,1,0,640}; assert(av_stream_accept(&s,&h));
    s.next_seq=UINT32_MAX; h.seq=UINT32_MAX; h.pts_ms=20; assert(!av_stream_accept(&s,&h));
}
static void pixel_tests(void) {
    uint8_t stripe[AV_WIDTH*16*2]; memset(stripe,0xa5,sizeof(stripe));
    const uint8_t rgb[]={255,0,0,0,255,0,0,0,255,255,255,255};
    assert(av_pack_rgb888(stripe,16,0,16,3,16,rgb));
    const uint8_t expected[]={0xf8,0,0x07,0xe0,0,0x1f,0xff,0xff};
    assert(!memcmp(stripe,expected,sizeof(expected))); assert(stripe[8]==0xa5);
    assert(av_pack_rgb888(stripe,16,316,31,319,31,rgb));
    assert(!memcmp(stripe+sizeof(stripe)-8,expected,8));
    assert(!av_pack_rgb888(stripe,16,0,15,3,15,rgb));
    assert(!av_pack_rgb888(stripe,16,0,31,3,32,rgb));
    assert(!av_pack_rgb888(stripe,16,319,16,320,16,rgb));
    assert(!av_pack_rgb888(stripe,17,0,17,3,17,rgb));
    assert(!av_pack_rgb888(stripe,240,0,240,3,240,rgb));
}
static void scaled_pixel_tests(void) {
    uint8_t stripe[AV_WIDTH*16*2]; memset(stripe,0xa5,sizeof(stripe));
    const uint8_t rgb[]={255,0,0};
    assert(av_pack_rgb888_x2(stripe,16,0,8,0,8,rgb));
    assert(stripe[0]==0xf8 && stripe[1]==0 && stripe[2]==0xf8 && stripe[3]==0);
    assert(stripe[AV_WIDTH*2]==0xf8 && stripe[AV_WIDTH*2+3]==0);
    assert(stripe[4]==0xa5);
    assert(av_pack_rgb888_x2(stripe,224,159,119,159,119,rgb));
    assert(stripe[sizeof(stripe)-2]==0xf8 && stripe[sizeof(stripe)-1]==0);
    assert(!av_pack_rgb888_x2(stripe,16,0,7,0,7,rgb));
    assert(!av_pack_rgb888_x2(stripe,16,160,8,160,8,rgb));
    assert(!av_pack_rgb888_x2(stripe,16,0,16,0,16,rgb));
}
static void full_scaled_frame_tests(void) {
    enum { STRIPE_BYTES=AV_WIDTH*AV_STRIPE_ROWS*2, GUARD=32 };
    uint8_t buffers[2][STRIPE_BYTES+2*GUARD];
    uint8_t bitmap[16*16*3], frame[AV_WIDTH*AV_HEIGHT*2];
    memset(frame,0xa5,sizeof(frame));
    unsigned output_y=0, submissions=0;
    for(unsigned top=0;top<AV_VIDEO_HEIGHT;top+=AV_MCU_ROWS) {
        unsigned rows=AV_VIDEO_HEIGHT-top;
        if(rows>AV_MCU_ROWS) rows=AV_MCU_ROWS;
        unsigned stripes=rows*2/AV_STRIPE_ROWS;
        memset(buffers,0xa5,sizeof(buffers));
        for(unsigned left=0;left<AV_VIDEO_WIDTH;left+=16) {
            for(unsigned y=0;y<rows;y++) for(unsigned x=0;x<16;x++) {
                size_t i=(y*16+x)*3;
                bitmap[i]=(left+x)*3;
                bitmap[i+1]=(top+y)*5;
                bitmap[i+2]=(left+x)^(top+y);
            }
            for(unsigned i=0;i<stripes;i++)
                assert(av_pack_rgb888_x2(buffers[i]+GUARD,output_y+i*AV_STRIPE_ROWS,
                                         left,top,left+15,top+rows-1,bitmap));
        }
        for(unsigned i=0;i<2;i++) {
            for(unsigned j=0;j<GUARD;j++) {
                assert(buffers[i][j]==0xa5);
                assert(buffers[i][GUARD+STRIPE_BYTES+j]==0xa5);
            }
            if(i>=stripes) for(unsigned j=0;j<STRIPE_BYTES;j++)
                assert(buffers[i][GUARD+j]==0xa5); // Last MCU must not fill buffer 1.
        }
        for(unsigned i=0;i<stripes;i++) {
            memcpy(frame+output_y*AV_WIDTH*2,buffers[i]+GUARD,STRIPE_BYTES);
            output_y+=AV_STRIPE_ROWS;
            submissions++;
        }
    }
    assert(output_y==240 && submissions==15);
    // Independent per-pixel oracle checks horizontal/vertical duplication, every
    // MCU seam, RGB byte order, clipped last row and pixel (319,239).
    for(unsigned y=0;y<AV_HEIGHT;y++) for(unsigned x=0;x<AV_WIDTH;x++) {
        unsigned sx=x/2, sy=y/2;
        uint8_t red=sx*3, green=sy*5, blue=sx^sy;
        uint16_t expected=(red/8)*2048+(green/4)*32+blue/8;
        size_t i=(y*AV_WIDTH+x)*2;
        assert(frame[i]==(expected>>8) && frame[i+1]==(expected&255));
    }
    const uint8_t rgb[3]={255,0,0};
    uint8_t *stripe=buffers[0]+GUARD;
    memset(buffers,0xa5,sizeof(buffers));
    assert(!av_pack_rgb888_x2(NULL,0,0,0,0,0,rgb));
    assert(!av_pack_rgb888_x2(stripe,0,0,0,0,0,NULL));
    assert(!av_pack_rgb888_x2(stripe,1,0,0,0,0,rgb));
    assert(!av_pack_rgb888_x2(stripe,240,0,120,0,120,rgb));
    assert(!av_pack_rgb888_x2(stripe,UINT32_MAX,0,0,0,0,rgb));
    assert(!av_pack_rgb888_x2(stripe,0,1,0,0,0,rgb));
    assert(!av_pack_rgb888_x2(stripe,0,0,1,0,0,rgb));
    assert(!av_pack_rgb888_x2(stripe,0,0,0,UINT32_MAX,0,rgb));
    assert(!av_pack_rgb888_x2(stripe,224,0,112,0,120,rgb));
    assert(!av_pack_rgb888_x2(stripe,0,0,0,0,UINT32_MAX,rgb));
    for(unsigned j=0;j<sizeof(buffers[0]);j++) assert(buffers[0][j]==0xa5);
}
static void channel_tests(void) {
    const char *ids[]={"cctv1","cctv5","cgtn"};
    unsigned index=0; char out[AV_CHANNEL_ID_MAX];
    // Forward and backward must wrap, which is how UP/DOWN behave at the ends.
    assert(av_channel_step(ids,3,&index,1,out,sizeof(out)) && !strcmp(out,"cctv5") && index==1);
    assert(av_channel_step(ids,3,&index,-1,out,sizeof(out)) && !strcmp(out,"cctv1") && index==0);
    assert(av_channel_step(ids,3,&index,-1,out,sizeof(out)) && !strcmp(out,"cgtn") && index==2);
    assert(av_channel_step(ids,3,&index,1,out,sizeof(out)) && !strcmp(out,"cctv1") && index==0);
    // A single-entry list stays put instead of dividing by zero.
    unsigned single=0;
    assert(av_channel_step(ids,1,&single,1,out,sizeof(out)) && !strcmp(out,"cctv1") && single==0);
    // A resumed session may already be on a channel that is no longer listed.
    // "absent" must be distinguishable from index 0, or a removed channel makes
    // the first UP press skip it.
    assert(av_channel_index_of(ids,3,"cctv1")==0);
    assert(av_channel_index_of(ids,3,"cgtn")==2);
    assert(av_channel_index_of(ids,3,"removed")==3);
    assert(av_channel_index_of(ids,0,"cctv1")==0);
    assert(av_channel_index_of(ids,3,NULL)==3);
    assert(av_channel_index_of(NULL,3,"cctv1")==3);
    // Oversized ids and a too-small destination are refused, not truncated.
    const char *long_ids[]={"0123456789abcdefghij"};
    unsigned li=0; char small[4];
    assert(!av_channel_step(long_ids,1,&li,1,out,sizeof(out)));
    assert(!av_channel_step(ids,3,&index,1,small,sizeof(small)));
    assert(!av_channel_step(NULL,3,&index,1,out,sizeof(out)));
    assert(!av_channel_step(ids,0,&index,1,out,sizeof(out)));
    assert(!av_channel_step(ids,3,NULL,1,out,sizeof(out)));
    assert(!av_channel_step(ids,3,&index,0,out,sizeof(out)));
    assert(!av_channel_step(ids,3,&index,1,NULL,sizeof(out)));
}
static void json_tests(void) {
    const char *flat="{\"text\":\"[{\\\"}\\\\\",\"n\":1}";
    assert(av_json_depth_safe(flat,strlen(flat),4));
    assert(av_json_depth_safe("[[[[]]]]",8,4));
    assert(!av_json_depth_safe("[[[[[]]]]]",10,4));
    assert(!av_json_depth_safe("{",1,4));
    assert(!av_json_depth_safe("}",1,4));
    assert(!av_json_depth_safe("\"unterminated",13,4));
    assert(!av_json_depth_safe("{\"x\":\"a\n\"}",11,4));
    const char embedded[]={'{',0,'}'};
    assert(!av_json_depth_safe(embedded,sizeof(embedded),4));
    char bomb[1024]; memset(bomb,'[',512); memset(bomb+512,']',512);
    assert(!av_json_depth_safe(bomb,sizeof(bomb),4));
}
int main(void) { header_tests(); stream_tests(); pixel_tests(); scaled_pixel_tests(); full_scaled_frame_tests(); channel_tests(); json_tests(); puts("FAV1 parser/state/RGB stripe tests: PASS"); return 0; }
