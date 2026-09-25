// FAV1 framing, the channel list, and the indexed picture.
//
// The picture half is the half worth testing hardest. A palette entry one count
// out, or an expansion that walks the wrong way, still produces something that
// looks like a picture -- so a person watching cannot tell a bug from a bad
// signal, and a build that compiles tells you nothing at all. The cases below
// pin the colours down exactly, and walk the expansion over a pattern chosen so
// that an overwrite would show.
#include "av_protocol.h"
#include <assert.h>
#include <stdio.h>
#include <string.h>

// Field order for the header initialisers below: type, flags, session, seq,
// pts_ms, length.
static void header_tests(void) {
    av_header_t h={AV_VIDEO,0,0x12345678,0x23456789,0x3456789a,AV_VIDEO_MAX}, got;
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
    // p[6] is reserved and has to stay zero whatever the kind.
    wire[6]=1; assert(!av_header_decode(wire,&got)); wire[6]=0;
    // No kind accepts a flag except AV_VIDEO, and it accepts only bit 0.
    for(unsigned type=0;type<256;type++) {
        // A length that is legal for the kinds this loop expects to be
        // accepted, so that what is being tested is the kind and not the size.
        // AV_END is expected to be refused here and its only legal length is
        // zero, so it is given a length it cannot have rather than the one it
        // must: handing it zero would make it a valid packet and the loop would
        // fail on its own scaffolding instead of on the decoder.
        h.type=type;
        h.length = type==AV_AUDIO ? AV_AUDIO_BYTES
                 : type==AV_END ? 0u : 640u;
        if(type==AV_END) h.length=1u;
        h.flags=0; av_header_encode(wire,&h);
        bool expect = type>=AV_HELLO && type<=AV_PALETTE
                   && type!=AV_END && type!=AV_PALETTE;
        assert(av_header_decode(wire,&got)==expect);
    }
    h.type=AV_VIDEO; h.length=100; h.flags=AV_VIDEO_CONTINUES; av_header_encode(wire,&h);
    assert(av_header_decode(wire,&got) && got.flags==AV_VIDEO_CONTINUES);
    h.flags=0x80; av_header_encode(wire,&h);
    assert(!av_header_decode(wire,&got));
    h.flags=0;
    // Boundaries are derived from the limits themselves, so raising the control
    // ceiling for a longer channel list does not silently invalidate this test.
    const uint32_t lengths[]={0,1,AV_AUDIO_BYTES-1,AV_AUDIO_BYTES,
                              AV_AUDIO_BYTES+1,
                              AV_CONTROL_MAX-1,AV_CONTROL_MAX,AV_CONTROL_MAX+1,
                              AV_VIDEO_MAX,UINT32_MAX};
    for(unsigned type=1;type<=AV_PALETTE;type++) {
        if(type==AV_END || type==AV_PALETTE) continue;
        for(unsigned i=0;i<sizeof(lengths)/sizeof(*lengths);i++) {
            uint32_t n=lengths[i]; h.type=type; h.length=n; h.flags=0; av_header_encode(wire,&h);
            bool expected=type==AV_AUDIO?n==AV_AUDIO_BYTES
                         :type==AV_VIDEO?(n>0 && n<=AV_VIDEO_MAX)
                         :(n>0 && n<=AV_CONTROL_MAX);
            assert(av_header_decode(wire,&got)==expected);
        }
    }
    // A palette has exactly one legal length.
    h.type=AV_PALETTE; h.flags=0;
    h.length=AV_PALETTE_BYTES; av_header_encode(wire,&h); assert(av_header_decode(wire,&got));
    h.length=AV_PALETTE_BYTES-2; av_header_encode(wire,&h); assert(!av_header_decode(wire,&got));
    h.length=AV_PALETTE_BYTES+2; av_header_encode(wire,&h); assert(!av_header_decode(wire,&got));
    // An end packet carries nothing.
    h.type=AV_END; h.length=0; av_header_encode(wire,&h); assert(av_header_decode(wire,&got));
    h.length=1; av_header_encode(wire,&h); assert(!av_header_decode(wire,&got));
}
static void stream_tests(void) {
    av_stream_t s={0}; av_header_t h={AV_CONFIG,0,42,0,0,100};
    av_header_t bad=h; bad.session=0; assert(!av_stream_accept(&s,&bad));
    bad=h; bad.type=AV_AUDIO; assert(!av_stream_accept(&s,&bad));
    bad=h; bad.seq=1; assert(!av_stream_accept(&s,&bad));
    assert(av_stream_accept(&s,&h));
    h=(av_header_t){AV_AUDIO,0,42,1,0,AV_AUDIO_BYTES}; assert(av_stream_accept(&s,&h));
    h.seq=2; h.pts_ms=AV_AUDIO_MS; assert(av_stream_accept(&s,&h));
    h=(av_header_t){AV_VIDEO,0,42,3,0,100}; assert(av_stream_accept(&s,&h)); // cross-media PTS may fall
    h.seq=4; h.pts_ms=83; assert(av_stream_accept(&s,&h));
    h.seq=5; assert(!av_stream_accept(&s,&h)); // repeated video PTS
    // A frame split across packets shares one timestamp and says so with the
    // flag; without the flag the same packet is a repeat and is refused.
    h.seq=5; h.flags=AV_VIDEO_CONTINUES; assert(av_stream_accept(&s,&h));
    h.seq=6; h.flags=AV_VIDEO_CONTINUES; assert(av_stream_accept(&s,&h));
    h.seq=7; h.flags=0; assert(!av_stream_accept(&s,&h));
    h.seq=7; h.pts_ms=166; assert(av_stream_accept(&s,&h));
    h=(av_header_t){AV_AUDIO,0,42,8,2*AV_AUDIO_MS,AV_AUDIO_BYTES};
    bad=h; bad.session=43; assert(!av_stream_accept(&s,&bad));
    bad=h; bad.seq=9; assert(!av_stream_accept(&s,&bad));
    bad=h; bad.pts_ms=3*AV_AUDIO_MS; assert(!av_stream_accept(&s,&bad));
    assert(av_stream_accept(&s,&h));
    // A palette takes its place in the sequence and touches no timeline, so it
    // may arrive between any two packets.
    h=(av_header_t){AV_PALETTE,0,42,9,0,AV_PALETTE_BYTES}; assert(av_stream_accept(&s,&h));
    h=(av_header_t){AV_END,0,42,10,10000,0}; assert(av_stream_accept(&s,&h));
    assert(!av_stream_accept(&s,&h));
    // A continuation with no frame under way has nothing to continue. Accepting
    // it would let a stream whose first packet was lost look like a valid one.
    memset(&s,0,sizeof(s)); h=(av_header_t){AV_CONFIG,0,7,0,0,100}; assert(av_stream_accept(&s,&h));
    h=(av_header_t){AV_VIDEO,AV_VIDEO_CONTINUES,7,1,50,100}; assert(!av_stream_accept(&s,&h));
    memset(&s,0,sizeof(s)); h=(av_header_t){AV_CONFIG,0,43,0,0,100}; assert(av_stream_accept(&s,&h));
    h=(av_header_t){AV_AUDIO,0,43,1,0,AV_AUDIO_BYTES}; assert(av_stream_accept(&s,&h));
    s.next_seq=UINT32_MAX; h.seq=UINT32_MAX; h.pts_ms=AV_AUDIO_MS;
    assert(!av_stream_accept(&s,&h));
}
static void palette_tests(void) {
    // The 3-3-2 mapping at the ends and at the corners. Red is the high field,
    // and getting that backwards is the mistake that leaves the picture looking
    // plausible while every colour in it is wrong.
    assert(av_palette_rgb565(0x00)==0x0000);   // black
    assert(av_palette_rgb565(0x03)==0x001f);   // blue, full scale
    assert(av_palette_rgb565(0xe0)==0xf800);   // red, full scale
    assert(av_palette_rgb565(0xe3)==0xf81f);   // magenta
    assert(av_palette_rgb565(0x1c)==0x07e0);   // green, full scale
    assert(av_palette_rgb565(0xff)==0xffff);   // white
    assert(av_palette_rgb565(0xfc)==0xffe0);   // yellow
    assert(av_palette_rgb565(0x1f)==0x07ff);   // cyan
    // Every field carries a distinct value: no two of the 256 entries may come
    // out the same, which is what a field that was read from the wrong bits
    // would produce.
    for(unsigned k=0;k<256;k++) for(unsigned j=k+1;j<256;j++) {
        assert(av_palette_rgb565((uint8_t)k)!=av_palette_rgb565((uint8_t)j));
    }
    // Each field reaches its own full scale, or white comes out grey, and each
    // field moves only with its own bits -- changing the blue bits must leave
    // red and green exactly where they were.
    for(unsigned k=0;k<256;k++) {
        uint16_t c=av_palette_rgb565((uint8_t)k);
        uint16_t other=av_palette_rgb565((uint8_t)(k^0x03u));
        assert((c>>11)==(other>>11) && ((c>>5)&0x3f)==((other>>5)&0x3f));
    }
    assert((av_palette_rgb565(0xe0)>>11)==0x1f);   // red field at full scale
    assert(((av_palette_rgb565(0x1c)>>5)&0x3f)==0x3f); // green field
    assert((av_palette_rgb565(0x03)&0x1f)==0x1f);  // blue field
    // A transmitted palette is 256 big-endian pairs and must survive in order.
    uint8_t raw[AV_PALETTE_BYTES];
    for(unsigned i=0;i<AV_PALETTE_ENTRIES;i++) {
        uint16_t v=(uint16_t)(i*7+1);
        raw[2*i]=(uint8_t)(v>>8); raw[2*i+1]=(uint8_t)(v&0xff);
    }
    uint16_t table[AV_PALETTE_ENTRIES];
    av_palette_decode(raw,table);
    for(unsigned i=0;i<AV_PALETTE_ENTRIES;i++) assert(table[i]==(uint16_t)(i*7+1));
}
static void expansion_tests(void) {
    static uint16_t table[AV_PALETTE_ENTRIES];
    for(unsigned i=0;i<AV_PALETTE_ENTRIES;i++) table[i]=av_palette_rgb565((uint8_t)i);

    // In place, and backwards. Neither property is visible in the result of a
    // single value, so every position holds a different index: a forward walk
    // would overwrite the index at position 1 before reading it, and everything
    // past the first byte would come out wrong.
    static uint8_t buf[AV_STRIPE_PIXELS*2];
    for(unsigned i=0;i<AV_STRIPE_PIXELS;i++) buf[i]=(uint8_t)((i*37+11)&0xff);
    static uint16_t expected[AV_STRIPE_PIXELS];
    for(unsigned i=0;i<AV_STRIPE_PIXELS;i++) expected[i]=table[(uint8_t)((i*37+11)&0xff)];

    av_expand_indexed(buf,AV_STRIPE_PIXELS,table);
    for(unsigned i=0;i<AV_STRIPE_PIXELS;i++) {
        uint16_t got=(uint16_t)((buf[2*i]<<8)|buf[2*i+1]);
        if(got!=expected[i]) printf("  pixel %u: got %04x want %04x\n",i,got,expected[i]);
        assert(got==expected[i]);
    }
    // Big-endian, matching what the panel already receives.
    assert(buf[0]==(uint8_t)(expected[0]>>8));
    assert(buf[1]==(uint8_t)(expected[0]&0xff));

    // One pixel is the smallest case and the one where an off-by-one in the
    // loop bound would read past the front of the buffer.
    uint8_t one[2]={200,0};
    av_expand_indexed(one,1,table);
    assert((((uint16_t)one[0]<<8)|one[1])==table[200]);

    // Zero pixels writes nothing at all.
    uint8_t none[2]={0xaa,0xbb};
    av_expand_indexed(none,0,table);
    assert(none[0]==0xaa && none[1]==0xbb);
}
static void video_payload_tests(void) {
    // A well-formed packet: three stripes whose lengths account for exactly the
    // bytes that follow them.
    uint8_t payload[2+2*3+10]={0,3, 0,2, 0,3, 0,5, 1,2,3,4,5,6,7,8,9,10};
    av_video_t v;
    assert(av_video_decode(payload,sizeof(payload),&v));
    assert(v.first==0 && v.count==3 && v.data==payload+8);
    const uint8_t *p; size_t n;
    assert(av_video_stripe(&v,0,&p,&n) && n==2 && p==v.data);
    assert(av_video_stripe(&v,1,&p,&n) && n==3 && p==v.data+2);
    assert(av_video_stripe(&v,2,&p,&n) && n==5 && p==v.data+5);
    assert(!av_video_stripe(&v,3,&p,&n));
    // A run that starts part way down the frame is how the later packets of a
    // frame arrive, and the first stripe has to be reported where it is.
    uint8_t later[2+2+3]={9,1, 0,3, 7,7,7};
    assert(av_video_decode(later,sizeof(later),&v) && v.first==9 && v.count==1);

    // A count of zero says nothing; it is not an empty packet.
    uint8_t empty[4]={0,0,0,0};
    assert(!av_video_decode(empty,sizeof(empty),&v));
    // More stripes than a packet is allowed to carry.
    uint8_t too_many[4]={0,AV_STRIPES_PER_PACKET+1,0,0};
    assert(!av_video_decode(too_many,sizeof(too_many),&v));
    // A run that leaves the frame, at the start and at the end.
    uint8_t over_start[4]={AV_STRIPES,1,0,0};
    assert(!av_video_decode(over_start,sizeof(over_start),&v));
    uint8_t over_end[4]={AV_STRIPES-1,2,0,0};
    assert(!av_video_decode(over_end,sizeof(over_end),&v));
    // The lengths must account for the payload exactly. Too few would leave
    // bytes nobody reads; too many would run off the end. Either way what the
    // device would draw is not what the server sent.
    uint8_t short_table[5]={0,1,0,9,0};
    assert(!av_video_decode(short_table,sizeof(short_table),&v));
    uint8_t short_data[6]={0,1,0,9,1,2};
    assert(!av_video_decode(short_data,sizeof(short_data),&v));
    uint8_t long_data[6]={0,1,0,1,1,2};
    assert(!av_video_decode(long_data,sizeof(long_data),&v));
    // A table that does not fit inside the payload at all.
    uint8_t truncated[3]={0,2,0};
    assert(!av_video_decode(truncated,sizeof(truncated),&v));
    assert(!av_video_decode(NULL,10,&v));
    assert(!av_video_decode(payload,sizeof(payload),NULL));

    // The longest run the format allows: the whole frame in one packet.
    //
    // This used to be checked against a worst case -- every stripe stored
    // rather than compressed, which is what deflate does to incompressible
    // input -- on the reasoning that the largest run the decoder accepts must
    // also be one a sender can produce. That is no longer the shape of the
    // format. A packet may now carry a whole frame, which is what keeps the
    // picture's cost to the device in frames rather than in packets, and a
    // frame of genuinely incompressible noise is about 77 KB against the
    // 24576 ceiling -- so such a frame is split by the sender rather than
    // refused by the receiver.
    //
    // What has to hold is therefore not "the worst case fits" but the two
    // things that make a large packet safe: the decoder accepts the run when
    // the packet is within the ceiling, and the ceiling is enforced before a
    // byte of payload is read. The buffers are AV_VIDEO_MAX long, so that
    // check on the header is what bounds what the read can write.
    enum { STORED_BYTES=5131 };
    static uint8_t big[2+2*AV_STRIPES_PER_PACKET+STORED_BYTES*AV_STRIPES_PER_PACKET];
    big[0]=0; big[1]=AV_STRIPES_PER_PACKET;
    for(unsigned i=0;i<AV_STRIPES_PER_PACKET;i++) {
        big[2+2*i]=(uint8_t)(STORED_BYTES>>8); big[3+2*i]=(uint8_t)(STORED_BYTES&0xff);
    }
    assert(av_video_decode(big,sizeof(big),&v));
    assert(v.first==0 && v.count==AV_STRIPES);

    uint8_t header[AV_HEADER_BYTES];
    av_header_t h={0};
    h.type=AV_VIDEO; h.length=AV_VIDEO_MAX+1;
    av_header_encode(header,&h);
    assert(!av_header_decode(header,&h));
    h.length=AV_VIDEO_MAX;
    av_header_encode(header,&h);
    assert(av_header_decode(header,&h));
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
int main(void) {
    header_tests();
    stream_tests();
    palette_tests();
    expansion_tests();

    video_payload_tests();
    channel_tests();
    json_tests();
    puts("FAV1 parser/state/indexed picture tests: PASS");
    return 0;
}
