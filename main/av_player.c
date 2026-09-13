// Standalone FAV1 LAN prototype. Session workers own socket, audio and raw LCD.
#include "av_player.h"
#include "av_protocol.h"
#include "av_config.h"
#include "bsp_audio.h"
#include "bsp_display.h"
#include "bsp_button.h"
#include "bsp_pins.h"
#include "bsp_battery.h"
#include "ui_menu.h"
#include "av_provision.h"
#include "av_provision_policy.h"
#include "av_settings.h"
#include "av_store.h"
#include "ui_text.h"
#include "esp32c3/rom/tjpgd.h"
#include "esp_heap_caps.h"
#include "esp_system.h"
#include "esp_log.h"
#include "esp_timer.h"
#include "esp_wifi.h"
#include "esp_event.h"
#include "esp_netif.h"
#include "nvs_flash.h"
#include "cJSON.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "freertos/queue.h"
#include "freertos/event_groups.h"
#include "lwip/sockets.h"
// Separate from sockets.h on purpose: name resolution is its own header in
// lwIP, and without it the address is dialled by number only.
#include "lwip/netdb.h"
#include <errno.h>
#include <fcntl.h>
#include <stdio.h>
#include <inttypes.h>
#include <stdlib.h>
#include <stdatomic.h>
#include <string.h>

#define STOP BIT0
#define RX_DONE BIT1
#define AUDIO_DONE BIT2
#define VIDEO_DONE BIT3
#define CONFIG_READY BIT4
#define CLOCK_READY BIT5
#define WIFI_READY BIT6
#define PCM_QUEUE 20
// Sessions without a picture before the channel is treated as dead and skipped.
// A channel the viewer chose gets more attempts: stepping away from a deliberate
// choice reads as the button having done nothing.
#define DEAD_CHANNEL_TRIES 2u
#define DEAD_CHANNEL_TRIES_USER 4u
// How long a channel change waits for the three workers to retire before giving
// up on them. Every worker sleeps in 5 ms steps and checks STOP, so an ordinary
// exit is one or two ticks; the video worker can be inside an 89 ms JPEG decode
// and the audio worker inside a DMA write, which is what this covers. It is a
// ceiling on a wedged worker, not a schedule.
#define SESSION_DRAIN_MS 1000u
// A second wait, used only when the first expires. SESSION_DRAIN_MS covers every
// ordinary exit with room to spare (measured at 10-37 ms); this covers an
// unlucky overlap with a codec open or a DMA write. See session_drain().
#define SESSION_DRAIN_GRACE_MS 4000u
#define STRIPE_BYTES (AV_WIDTH * AV_STRIPE_ROWS * 2)
// Output level used for the colour-bar screens, where the codec is open with no
// audio fed to it and the only sound is its own noise floor.
//
// Higher than a typical listening level on purpose: it is the noise floor that
// has to be audible here, not a signal, and that floor sits well below the
// signal. Whether this dial moves it at all depends on where the chip generates
// it -- a level control before the noise source scales it, one after does not --
// which cannot be settled by reading the datasheet. It is a named constant so
// the value can be changed after listening; if raising it does nothing, the
// noise is downstream of the control and the hiss has to be generated instead.
#define SETUP_HISS_VOLUME 100u
_Static_assert(AV_VIDEO_WIDTH * 2 == AV_WIDTH && AV_VIDEO_HEIGHT * 2 == AV_HEIGHT,
               "Nearest-neighbor x2 geometry");
#define DMA_ESTIMATE_MS 90 // Six 240-frame descriptors / 16 kHz; NOT DMA measurement.
_Static_assert(AV_WIDTH == BSP_LCD_H && AV_HEIGHT == BSP_LCD_W, "Landscape geometry");
_Static_assert(JD_FORMAT == 0, "C3 ROM outputs RGB888");
static const char *TAG = "av_raw";
// What a session knows about reaching the server.
//
// Three states rather than two. "Not yet known" and "failed" are different
// facts, and the screen has to separate them: a device that started dialling a
// moment ago is not a device that cannot connect, and saying the second while
// the first is true sends the viewer to change a setting that was never wrong.
// The connection attempt itself takes up to three seconds, so that window is
// long enough to read.
typedef enum {
    // The session is dialling; nothing has been concluded.
    LINK_TRYING = 0,
    // The socket came up, so the server answered.
    LINK_UP,
    // The attempt is over and the server was not reached. Everything that can
    // go wrong on the way -- no address, a name that will not resolve, a
    // refused or timed-out connection -- ends here, because what the viewer
    // does about all of them is the same: go to the setup page.
    LINK_DOWN,
} link_state_t;

typedef struct { uint32_t pts; uint8_t pcm[AV_AUDIO_BYTES]; } audio_t;
typedef struct { uint8_t *jpeg; uint32_t length, pts; } video_t;
// A queued button gesture. Carrying the event type (not just the key) is what
// lets a click mean "next channel" while a long press means "volume".
typedef struct { bsp_btn_t key; bsp_btn_ev_t ev; } key_ev_t;
// Channel switching reconnects with a new id, so a switch is just a session
// restart. One writer (the key path) sets a pending flag; the session owner
// applies it, which avoids touching the string while a connection is being made.
// The id length lives in av_protocol.h so the parser and this struct agree.
// The display name is kept as well: the overlay shows it, and the server already
// sends it in CONFIG, so no extra request is needed to name a channel.
typedef struct { char id[AV_CHANNEL_ID_MAX]; char name[UI_MENU_NAME_MAX]; } channel_t;
typedef struct {
    EventGroupHandle_t events;
    QueueHandle_t audio, video, free_video, keys;
    uint8_t *jpeg[2], *stripe[2], *work;
    // clock_us is the audio playback origin, set when feeding actually starts.
    int64_t clock_us;
    atomic_uint_least32_t submitted_samples, decoded, decode_max_ms, feed_gap_max_ms;
    atomic_uint_least32_t dropped, last_packet_ms;
    atomic_uint_least32_t audio_high, video_high;
    atomic_uchar volume;
    // The server sends the channel list in CONFIG; the device only stores ids
    // and never hardcodes them, so adding a channel needs no reflash.
    channel_t list[AV_CHANNEL_MAX];
    // Unsigned, not uchar: AV_CHANNEL_MAX is above 255, and an 8-bit count
    // silently wrapped, which would have made a 300-channel list look like 44.
    atomic_uint count, index;
    atomic_bool switch_pending;
    // Set once the first PCM/JPEG arrives, which switches the idle watchdog
    // from the startup allowance to the normal inter-packet gap.
    atomic_bool media_started;
    // s.channel is what hello() sends; s.pending is what a key press asks for.
    // s.channel has two writers, never at the same time: the owner task copies
    // pending -> channel between sessions, and the receive task overwrites it
    // with the channel the server confirms while that session is running. Both
    // sit on opposite sides of a session boundary, and the owner only reads the
    // string while building a connection, so no lock is needed.
    char channel[AV_CHANNEL_ID_MAX], pending[AV_CHANNEL_ID_MAX];
    // The overlay. Only the session owner and the video task touch it, and they
    // are the same thread of control: the owner changes the view between frames,
    // the video task reads it while drawing. No separate lock is needed because
    // the video task never blocks on the owner for longer than one frame.
    ui_menu_t menu;
    // A CONFIG arrives on the receive task but the menu is read by the video
    // task while it draws. The two must not touch the same strings at once, so
    // the receive task only raises a flag; the session owner builds the menu
    // from s.list between frames, where nothing is drawing from it.
    atomic_bool list_dirty;
    // Battery percentage, refreshed on its own slow schedule: the fuel gauge is
    // read over I2C on the shared bus, which is far too slow for the frame path.
    atomic_int battery_soc;
    // Consecutive sessions that ended without ever showing a picture. A source
    // that has gone dead fails instantly, so without this the device reconnects
    // to the same dead channel forever and the screen never leaves the test
    // pattern. Counting lets it step past a channel that does not work.
    unsigned dead_streak;
    // What this session knows about reaching the server.
    //
    // Written by the receive task as it dials, and read by the video task while
    // it draws and by the session owner between sessions.
    atomic_int link;
    // Set when the viewer picked the channel themselves. A channel they asked for
    // gets more attempts before being skipped, because jumping away from a
    // deliberate choice looks like the button did nothing.
    atomic_bool chosen_by_user;
    // Raised when the volume or the backlight changed. The key path only sets
    // it: writing to NVS on the button task would delay the next press, so the
    // session loop does the write when it next comes round.
    atomic_bool settings_dirty;
} player_t;
static player_t s;
// The channel whose waiting screen is currently on the panel. A reconnect to the
// same channel leaves the picture alone instead of blanking it.
static char shown_channel[AV_CHANNEL_ID_MAX];
// Whether the panel has ever been painted. Everything before the first frame
// would otherwise show uninitialised display memory, so the first session must
// draw the waiting screen even though its channel id is still empty and equals
// shown_channel. See the use site.
static bool waiting_drawn;
// Which overlay the waiting screen was last painted with, and when.
//
// The waiting screen is a still image, so it is painted when something it shows
// changes rather than on every pass: a full repaint pushes 150 KB over the
// panel's bus, which is not something to do dozens of times a second for a
// picture that has not moved. The overlay is what changes -- the status page's
// readings, the highlight in the list -- which is why it is repainted while one
// is open.
static int waiting_view=-1;
static int waiting_link=-1;
// The address the last connection attempt used, as "host:port".
//
// Kept so that dialling again is not mistaken for news. Every session begins by
// connecting, so a device that cannot reach its server would otherwise announce
// "connecting" once per attempt -- at about a second apart, that is a message
// flashing on and off for ever, and it tells the viewer nothing they were not
// already told by the failure underneath it. Dialling is worth showing only when
// there is something new to say it about: the first attempt, or a different
// address.
static char last_attempt_addr[AV_SERVER_ADDR_MAX+8];
static int64_t waiting_painted_us;
// How often the waiting screen is refreshed while an overlay is up. Fast enough
// that a key press looks immediate, slow enough that the bus is left alone
// between presses.
#define WAITING_REFRESH_MS 200
// The setup page takes over the whole device, so every key means something
// different while it is up. It is set before the panel is claimed and cleared
// before playback, and it is read by the key handler on the button task.
static atomic_bool s_setup_active;
// The viewer asked to leave setup. Read by the setup loop, which owns the
// decision to go back to playback.
static atomic_bool s_setup_leave;
// The viewer asked to open the setup screen, set from the status page and
// consumed at a session boundary -- the same shape as switch_pending, and for
// the same reason: the request arrives while a session is running, and the
// session has to be over before its memory can be given to the access point.
static atomic_bool s_setup_request;
static portMUX_TYPE clock_lock = portMUX_INITIALIZER_UNLOCKED;
// Guards the channel id table, which two tasks touch: the receive task rebuilds
// it from CONFIG while the owner task snapshots it on a key press. Publishing
// count=0 first is not enough on its own, because a reader can pass the old
// count check just before the writer starts overwriting the ids.
static portMUX_TYPE list_lock = portMUX_INITIALIZER_UNLOCKED;
static bool stopping(void) { return xEventGroupGetBits(s.events) & STOP; }
static bool rx_done(void) { return xEventGroupGetBits(s.events) & RX_DONE; }
static void fail(const char *reason) {
    ESP_LOGW(TAG, "Session reset: %s", reason); // Never log remote JSON/secrets.
    xEventGroupSetBits(s.events, STOP);
}
static bool delay_until(int64_t us) {
    while (!stopping() && esp_timer_get_time()<us) vTaskDelay(pdMS_TO_TICKS(5));
    return !stopping();
}
static int64_t estimated_pts(void) {
    taskENTER_CRITICAL(&clock_lock);
    int64_t origin=s.clock_us;
    uint32_t samples=s.submitted_samples;
    taskEXIT_CRITICAL(&clock_lock);
    int64_t wall=(esp_timer_get_time()-origin)/1000;
    int64_t submitted=(int64_t)samples*1000/16000;
    return wall<submitted ? wall : submitted;
}
static void on_key(bsp_btn_t key, bsp_btn_ev_t ev, void *ctx) {
    (void)ctx;
    // Every gesture this firmware uses must be forwarded: a click steps a
    // channel, a long press is volume or the menu, a double click is the status
    // page. Callbacks run in the button task, so only enqueue and drop when
    // full; never block or do work here.
    if (ev!=BSP_BTN_CLICK && ev!=BSP_BTN_LONG && ev!=BSP_BTN_DOUBLE) return;
    key_ev_t item={.key=key,.ev=ev};
    xQueueSend(s.keys,&item,0);
}
// WiFi connection state arrives through the provisioning bridge, which owns the
// WiFi stack. Both callbacks run on the WiFi event task, so they only touch the
// event group: no locking, no work.
//
// "Connected" means the station has an address on the network, not merely that
// it associated with the access point. The player needs the stronger statement,
// because it is about to open a socket to the server.
static void on_wifi_up(void) {
    xEventGroupSetBits(s.events,WIFI_READY);
}
static void on_wifi_down(void) {
    // A network that went away ends the session. The bridge deliberately does not
    // report this when the station is stopped to raise the setup access point, so
    // entering provisioning does not look like a connection failure.
    xEventGroupClearBits(s.events,WIFI_READY);
    xEventGroupSetBits(s.events,STOP);
}
// Bring up the WiFi stack and join the stored network.
//
// This no longer initializes anything itself. The provisioning bridge does it,
// because esp_netif_init, esp_event_loop_create_default and esp_wifi_init must
// each happen exactly once for the whole firmware and the bridge's component
// needs the same three. Calling them here as well would fail and leave the radio
// unusable, so the one owner is the bridge.
//
// `has_network` reports whether a network is known: one saved by an earlier
// provisioning run, or the credentials compiled into this build. When it is
// false the device has nothing to join and must raise the setup access point.
static esp_err_t wifi_init(bool *has_network) {
    if (!av_provision_init(has_network)) {
        ESP_LOGE(TAG,"WiFi stack unavailable");
        return ESP_FAIL;
    }
    av_provision_on_connected(on_wifi_up);
    av_provision_on_disconnected(on_wifi_down);
    if (*has_network) {
        av_provision_join_stored();
        av_provision_keep_radio_awake();
    }
    return ESP_OK;
}
// Receiver-owned diagnostics: metadata only, never addresses/secrets/payload.
static const char *s_rx_stage;
// Socket owner only. An absolute deadline also covers all discard fragments.
static bool io_until(int fd, void *buf, size_t n, bool sending, int64_t deadline) {
    size_t off=0;
    int64_t started=esp_timer_get_time();
    const char *reason="deadline";
    int error=0;
    while (off<n && !stopping() && esp_timer_get_time()<deadline) {
        ssize_t r=sending ? send(fd,(uint8_t *)buf+off,n-off,0) : recv(fd,(uint8_t *)buf+off,n-off,0);
        if (r>0) off+=(size_t)r;
        else if (r==0) { reason="EOF"; break; }
        else if (errno!=EAGAIN && errno!=EWOULDBLOCK && errno!=EINTR) {
            error=errno; reason="errno"; break;
        } else vTaskDelay(pdMS_TO_TICKS(5));
    }
    if(off==n) return true;
    if(stopping()) reason="cancelled";
    ESP_LOGW(TAG,"RX_IO stage=%s reason=%s errno=%d bytes=%u/%u elapsed_ms=%"PRId64,
        s_rx_stage,reason,error,(unsigned)off,(unsigned)n,(esp_timer_get_time()-started)/1000);
    return false;
}
static bool io_all(int fd, void *buf, size_t n, bool sending, unsigned deadline_ms) {
    return io_until(fd,buf,n,sending,esp_timer_get_time()+(int64_t)deadline_ms*1000);
}
// Where to connect, as the setup page stored it, falling back to what this build
// has compiled in.
//
// Read afresh for each session rather than cached once: the address can be
// changed on the setup page while the device is running, and a cached copy would
// keep dialling the old one until the next reflash. The read is a few
// milliseconds against a connection that already allows three seconds.
static bool server_address(av_server_addr_t *out) {
    if (av_store_server_addr_load(out)) return true;
    // A published build has none of these compiled in, which is the point: the
    // store is then the only source, and an unset address is an ordinary state
    // that the setup page exists to fix rather than an error.
    if (AV_SERVER_IPV4[0] && strcmp(AV_SERVER_IPV4,"0.0.0.0")) {
        // Composed and then parsed by the same reader the page's text goes
        // through, so a compiled-in address and a typed one cannot come out
        // meaning different things.
        char text[AV_SERVER_ADDR_MAX];
        int n=snprintf(text,sizeof(text),"%s:%u",AV_SERVER_IPV4,(unsigned)AV_SERVER_PORT);
        if (n>0 && (size_t)n<sizeof(text) && av_server_addr_parse(text,out)) return true;
    }
    return false;
}
// Say where the device will connect, but only when the answer changes.
//
// The address is read once per session so that one stored on the setup page
// takes effect without a restart. Announcing it each time would print the same
// line every few seconds on a device that is retrying, which buries everything
// else in the log; saying it when it changes keeps the useful fact without the
// repetition.
static void report_server_address(const av_server_addr_t *addr, bool configured)
{
    static char last[AV_SERVER_ADDR_MAX + 8];
    char now[AV_SERVER_ADDR_MAX + 8];
    if (configured) {
        snprintf(now,sizeof(now),"%s:%u",addr->host,(unsigned)addr->port);
    } else {
        snprintf(now,sizeof(now),"(not set)");
    }
    if (strcmp(last,now)==0) return;
    snprintf(last,sizeof(last),"%s",now);
    if (configured) {
        ESP_LOGI(TAG,"Server address: %s",now);
    } else {
        ESP_LOGW(TAG,"No server address stored; it is entered on the setup page");
    }
}

static int connect_server(void) {
    av_server_addr_t target;
    if (!server_address(&target)) {
        ESP_LOGW(TAG,"No server address; enter one on the setup page");
        return -1;
    }
    // Resolved rather than converted: an address typed into the page may be a
    // name, and a name is what survives the server moving to a different lease.
    // A literal costs nothing extra -- lwIP recognises one and skips the lookup.
    struct addrinfo hints={0}, *found=NULL;
    hints.ai_family=AF_INET;
    hints.ai_socktype=SOCK_STREAM;
    char service[6];
    snprintf(service,sizeof(service),"%u",(unsigned)target.port);
    int gai=getaddrinfo(target.host,service,&hints,&found);
    if (gai!=0 || !found) {
        ESP_LOGW(TAG,"Cannot resolve %s: %d",target.host,gai);
        if (found) freeaddrinfo(found);
        return -1;
    }
    int fd=socket(found->ai_family,found->ai_socktype,found->ai_protocol);
    if (fd<0) { freeaddrinfo(found); return -1; }
    // The address is used only to dial, so the list is released before the wait:
    // holding it across the three seconds below would keep DNS memory occupied
    // for no reason.
    struct sockaddr_storage peer;
    memcpy(&peer,found->ai_addr,found->ai_addrlen);
    socklen_t peer_len=found->ai_addrlen;
    freeaddrinfo(found);
    if (fcntl(fd,F_SETFL,O_NONBLOCK)<0) { close(fd); return -1; }
    if (connect(fd,(struct sockaddr *)&peer,peer_len)<0 && errno!=EINPROGRESS) {
        close(fd); return -1;
    }
    int64_t end=esp_timer_get_time()+3000000;
    while (!stopping() && esp_timer_get_time()<end) {
        fd_set wr; FD_ZERO(&wr); FD_SET(fd,&wr);
        struct timeval tv={.tv_usec=50000};
        int r=select(fd+1,NULL,&wr,NULL,&tv);
        if (r<0 && errno!=EINTR) break;
        if (r>0) {
            int error=0; socklen_t len=sizeof(error);
            if (getsockopt(fd,SOL_SOCKET,SO_ERROR,&error,&len)==0 && !error) return fd;
            break;
        }
    }
    close(fd); return -1;
}
static bool json_number(const cJSON *j, const char *key, uint32_t expected) {
    const cJSON *v=cJSON_GetObjectItemCaseSensitive(j,key);
    return cJSON_IsNumber(v) && v->valuedouble==(double)expected;
}
static bool config_valid(char *buf, size_t n, uint32_t session) {
    if (!av_json_depth_safe(buf,n,4)) return false;
    buf[n]=0;
    const char *end=NULL;
    cJSON *j=cJSON_ParseWithLengthOpts(buf,n+1,&end,true);
    bool ok=cJSON_IsObject(j) && json_number(j,"width",AV_VIDEO_WIDTH) && json_number(j,"height",AV_VIDEO_HEIGHT) &&
        json_number(j,"fps",AV_FPS) && json_number(j,"sample_rate",16000) && json_number(j,"channels",1) &&
        json_number(j,"sample_bits",16) && json_number(j,"audio_chunk_ms",20) &&
        json_number(j,"video_max_bytes",AV_VIDEO_MAX) && json_number(j,"session",session);
    const cJSON *delay=cJSON_GetObjectItemCaseSensitive(j,"start_delay_ms");
    if (delay && !json_number(j,"start_delay_ms",200)) ok=false;
    if (ok) {
        // "channel_list" is distinct from "channels", which is the audio channel
        // count checked above. Reusing one key for both made every CONFIG fail
        // validation, so keep them separate.
        const cJSON *list=cJSON_GetObjectItemCaseSensitive(j,"channel_list");
        if (cJSON_IsArray(list)) {
            // Adopt the channel the server says it is actually serving. The
            // device asks for a name it may not know (empty on the first
            // connection, or an id the server no longer carries) and the server
            // answers with what it chose. Without this the device's idea of "the
            // channel I am on" stayed empty, so its index lookup failed and the
            // very first press jumped to the top of the list rather than the
            // next entry. Written before the table swap so the index computed
            // below already uses the confirmed name.
            const cJSON *current=cJSON_GetObjectItemCaseSensitive(j,"channel");
            if (cJSON_IsString(current) && current->valuestring) {
                size_t len=strlen(current->valuestring);
                if (len>0 && len<AV_CHANNEL_ID_MAX) {
                    bool printable=true;
                    for (size_t i=0;i<len;i++) {
                        char c=current->valuestring[i];
                        if (c<'!' || c>'~') { printable=false; break; }
                    }
                    if (printable) memcpy(s.channel,current->valuestring,len+1);
                }
            }
            // Parse and validate into a private table first, then publish it in
            // one short locked step. Walking cJSON is far more work than the
            // copy, and the lock disables interrupts, so it must not be held
            // across the walk.
            // The table is heap, not stack: at 40 bytes an entry and up to
            // AV_CHANNEL_MAX entries it is far larger than this task's 5 KB
            // stack, and putting it here crashed the device on every CONFIG the
            // moment the channel limit was raised above a handful.
            channel_t *staged=heap_caps_malloc(sizeof(channel_t)*AV_CHANNEL_MAX,MALLOC_CAP_8BIT);
            if (!staged) {
                ESP_LOGW(TAG,"No memory to parse the channel list");
                cJSON_Delete(j);
                return false;
            }
            memset(staged,0,sizeof(channel_t)*AV_CHANNEL_MAX); // full-width copy below
            unsigned n=0;
            const cJSON *entry=NULL;
            cJSON_ArrayForEach(entry,list) {
                const cJSON *id=entry ? cJSON_GetObjectItemCaseSensitive(entry,"id") : NULL;
                if (!cJSON_IsString(id) || !id->valuestring) continue;
                size_t len=strlen(id->valuestring);
                // Bounded like every other control input: an oversized or
                // non-printable id is skipped, never truncated into the table.
                if (n>=AV_CHANNEL_MAX || len==0 || len>=AV_CHANNEL_ID_MAX) continue;
                bool printable=true;
                for (size_t i=0;i<len;i++) {
                    char c=id->valuestring[i];
                    if (c<'!' || c>'~') { printable=false; break; }
                }
                if (!printable) continue;
                memcpy(staged[n].id,id->valuestring,len+1);
                // The display name is optional: an older server may not send it,
                // and the overlay falls back to the id when it is absent. It is
                // free-form UTF-8, so only the length is bounded here.
                const cJSON *name=entry ? cJSON_GetObjectItemCaseSensitive(entry,"name") : NULL;
                if (cJSON_IsString(name) && name->valuestring) {
                    strncpy(staged[n].name,name->valuestring,sizeof(staged[n].name)-1);
                }
                n++;
            }
            if (n) {
                unsigned index;
                {
                    // Pointers, not copies: also off the stack for the same
                    // reason as the table above.
                    const char **ids=heap_caps_malloc(sizeof(char *)*n,MALLOC_CAP_8BIT);
                    if (!ids) {
                        free(staged);
                        ESP_LOGW(TAG,"No memory to index the channel list");
                        cJSON_Delete(j);
                        return false;
                    }
                    for (unsigned i=0;i<n;i++) ids[i]=staged[i].id;
                    index=av_channel_index_of(ids,n,s.channel);
                    free(ids);
                    if (index>=n) index=0; // current channel was removed from the list
                }
                // Publish index before count: a reader that sees the new count
                // must already see the index that belongs with it, otherwise it
                // can step from a stale position. count=0 is published while the
                // table is replaced so the key path, which reads it from another
                // task, never copies a half-written id.
                taskENTER_CRITICAL(&list_lock);
                atomic_store(&s.count,0);
                memcpy(s.list,staged,n*sizeof(staged[0]));
                atomic_store(&s.index,(unsigned char)index);
                atomic_store(&s.count,(unsigned int)n);
                taskEXIT_CRITICAL(&list_lock);
                free(staged);
                staged=NULL;

                // The overlay is installed by the session owner, not here: the
                // menu is read by the video task while it draws, and a partial
                // copy would be drawn from.
                atomic_store(&s.list_dirty,true);
            }
        }
    }
    cJSON_Delete(j);
    return ok;
}
static bool hello(int fd) {
    cJSON *j=cJSON_CreateObject();
    if (!j) return false;
    // The requested channel travels in the handshake, so switching channels is
    // simply a new connection. The server falls back to its default if the id
    // is unknown, which keeps an older device working against a newer server.
    //
    // The token is sent only when this build has one. Omitting it and sending an
    // empty string look the same on the wire only if the server treats them
    // alike, and it does not: the field's presence is what says "here is my
    // token", so an empty one would be a claim rather than an absence.
    bool ok=cJSON_AddNumberToObject(j,"version",1) &&
        cJSON_AddStringToObject(j,"channel",s.channel) &&
        (AV_PAIRING_TOKEN[0]=='\0' ||
         cJSON_AddStringToObject(j,"token",AV_PAIRING_TOKEN)!=NULL);
    char *body=ok ? cJSON_PrintUnformatted(j) : NULL;
    cJSON_Delete(j);
    if (!body) return false;
    size_t n=strlen(body);
    uint8_t wire[AV_HEADER_BYTES];
    av_header_t h={.type=AV_HELLO,.length=n}; av_header_encode(wire,&h);
    ok=n<=AV_CONTROL_MAX && io_all(fd,wire,sizeof(wire),true,1000) && io_all(fd,body,n,true,1000);
    // Best effort: do not leave token JSON in a reusable heap block.
    memset(body,0,n); cJSON_free(body);
    return ok;
}
static void receive_task(void *arg) {
    (void)arg;
    s_rx_stage="connect";
    av_header_t h={0};
    av_stream_t stream={0};
    // Heap, not stack: the control payload has to hold the whole channel list
    // (about 12 KB of JSON for a few hundred channels), which will not fit in
    // this task's stack. Allocated before the first goto so the cleanup path
    // can always free it, whether or not a connection was ever made.
    char *control=heap_caps_malloc(AV_CONTROL_MAX+1,MALLOC_CAP_8BIT);
    int fd=-1;
    bool clean=false;
    if(!control) {
        ESP_LOGW(TAG,"No memory for the control buffer");
        atomic_store(&s.link,(int)LINK_DOWN);
        goto done;
    }
    fd=connect_server();
    if(fd<0) {
        // The error number is captured here rather than printed from the call,
        // where anything called in between is free to overwrite it and the
        // number reported would be some later failure's.
        int connect_errno=errno;
        ESP_LOGW(TAG,"RX connect failed errno=%d",connect_errno);
        // Conclusive: the dial is over and the server did not answer.
        atomic_store(&s.link,(int)LINK_DOWN);
        goto done;
    }
    // Reached only once the socket is up: from here the failure is the channel's,
    // not the server's.
    atomic_store(&s.link,(int)LINK_UP);
    s_rx_stage="hello";
    if (!hello(fd)) goto done;
    uint8_t wire[AV_HEADER_BYTES];
    unsigned header_deadline_ms=2000;
    while (!stopping()) {
        s_rx_stage="header-read";
        memset(&h,0,sizeof(h));
        if (!io_all(fd,wire,sizeof(wire),false,header_deadline_ms)) break;
        s_rx_stage="header-decode";
        if(!av_header_decode(wire,&h)) break;
        s_rx_stage="stream-state";
        if(!av_stream_accept(&stream,&h)) break;
        s.last_packet_ms=(uint32_t)(esp_timer_get_time()/1000);
        if (h.type==AV_CONFIG || h.type==AV_ERROR) {
            s_rx_stage="control-read";
            if (!io_all(fd,control,h.length,false,1500)) break;
            s_rx_stage=h.type==AV_ERROR ? "server-error" : "config-validation";
            if (h.type==AV_ERROR || !config_valid(control,h.length,h.session)) break;
            // Do NOT fix the playback origin here. A live server answers CONFIG
            // immediately and only then fills its transcode buffer, so anchoring
            // at CONFIG time would start the audio clock seconds before the
            // first sample arrives, and the queue would look starved on the
            // first read. The audio task sets the origin when it starts feeding.
            xEventGroupSetBits(s.events,CONFIG_READY);
            // The server answers the handshake immediately and only then fills
            // its transcode buffer, so the first media packet can be seconds
            // away. Relax the per-header deadline once, here. It is never
            // restored: this is a per-session local that a new session resets,
            // and a stalled stream is caught by the inter-packet watchdog in the
            // session loop, which is tighter than this deadline.
            // Do not "fix" it back to 2000 ms without rechecking that watchdog.
            header_deadline_ms=AV_FIRST_MEDIA_TIMEOUT_MS;
        } else if (h.type==AV_AUDIO) {
            audio_t a={.pts=h.pts_ms};
            atomic_store(&s.media_started,true);
            // Flow control, applied only to audio and only now that the header
            // is already read. The host sends audio at its own clock rate while
            // the device consumes at the I2S rate, and the two drift apart by a
            // fraction of a percent, so on a 20-chunk queue the drift eventually
            // overflows it whatever buffer the host keeps. Holding this packet
            // back stops us reading the socket and lets TCP push the host down
            // to the device's real rate.
            // It must not sit before the header read: the audio queue settles at
            // its ceiling, so a check there blocked almost every read and starved
            // video, which then arrived stale and was dropped.
            // Only STOP ends the wait: every audio-task exit path sets it.
            while (!stopping() && uxQueueMessagesWaiting(s.audio)>PCM_QUEUE-4) {
                s_rx_stage="audio-flow-control";
                vTaskDelay(pdMS_TO_TICKS(10));
            }
            if (stopping()) break;
            s_rx_stage="audio-read";
            if (!io_all(fd,a.pcm,sizeof(a.pcm),false,1500)) break;
            if (!xQueueSend(s.audio,&a,0)) { fail("bounded PCM queue full"); break; }
            unsigned q=uxQueueMessagesWaiting(s.audio); if(q>s.audio_high) s.audio_high=q;
        } else if (h.type==AV_VIDEO) {
            video_t v={.length=h.length,.pts=h.pts_ms};
            atomic_store(&s.media_started,true);
            if (!xQueueReceive(s.free_video,&v.jpeg,0)) {
                // Never block audio behind rendering. Discard entire bounded payload.
                uint8_t discard[256]; uint32_t left=h.length;
                int64_t deadline=esp_timer_get_time()+1500000;
                s_rx_stage="video-discard";
                while (left && !stopping()) {
                    unsigned n=left<sizeof(discard)?left:sizeof(discard);
                    if (!io_until(fd,discard,n,false,deadline)) break;
                    left-=n;
                }
                if (left) break;
                s.dropped++; continue;
            }
            s_rx_stage="video-read";
            if (!io_all(fd,v.jpeg,v.length,false,1500)) {
                xQueueSend(s.free_video,&v.jpeg,0); break;
            }
            if (!xQueueSend(s.video,&v,0)) {
                xQueueSend(s.free_video,&v.jpeg,0); s.dropped++;
            }
            unsigned q=uxQueueMessagesWaiting(s.video); if(q>s.video_high) s.video_high=q;
        } else if (h.type==AV_END) { clean=true; break; }
    }
done:
    free(control);
    if (fd>=0) { shutdown(fd,SHUT_RDWR); close(fd); }
    if (!clean) {
        ESP_LOGW(TAG,"RX_EXIT stage=%s cancelled=%d type=%u seq=%"PRIu32" expected_seq=%"PRIu32
            " pts=%"PRIu32" length=%"PRIu32" audio_next_pts=%"PRIu32" video_pts=%"PRIu32,
            s_rx_stage,stopping(),h.type,h.seq,stream.next_seq,h.pts_ms,h.length,
            stream.audio_next_pts,stream.video_pts);
        if(!stopping()) fail(s_rx_stage);
    }
    xEventGroupSetBits(s.events,RX_DONE);
    vTaskDelete(NULL);
}
static void audio_task(void *arg) {
    (void)arg;
    bool opened=false;
    while (!stopping() && !(xEventGroupGetBits(s.events)&CONFIG_READY)) vTaskDelay(pdMS_TO_TICKS(10));
    if (stopping()) goto done;
    if (bsp_audio_set_format(16000,16,1)!=ESP_OK) { fail("codec open"); goto done; }
    opened=true; bsp_audio_set_mute(true); bsp_audio_set_volume(s.volume);
    // Clear any prior-session DMA audio while muted. >90ms, finite writes.
    audio_t a={0};
    for (int i=0;i<6 && !stopping();i++) {
        size_t written=0;
        if (bsp_audio_write_timeout(a.pcm,sizeof(a.pcm),&written,100)!=ESP_OK || written!=sizeof(a.pcm)) {
            fail("I2S reset write"); goto done;
        }
    }
    // Allow the final silence to drain, then wait for a reserve of audio. This
    // also covers the gap between CONFIG and the server's first media packet,
    // which can be seconds on a live channel.
    if (!delay_until(esp_timer_get_time()+100000)) goto done;
    int64_t prebuffer_deadline=esp_timer_get_time()+AV_FIRST_MEDIA_TIMEOUT_MS*1000;
    while (!stopping() && uxQueueMessagesWaiting(s.audio)<5 && !rx_done()
           && esp_timer_get_time()<prebuffer_deadline) vTaskDelay(pdMS_TO_TICKS(5));
    if (stopping()) goto done;
    if (uxQueueMessagesWaiting(s.audio)<5) { fail("no audio after CONFIG"); goto done; }
    // The origin is anchored here, at the first real feed, not at CONFIG.
    if (!delay_until(esp_timer_get_time()+DMA_ESTIMATE_MS*1000)) goto done;
    // The codec stays muted until there is real audio in hand.
    //
    // Previously this unmuted before the loop below, on the strength of the
    // queue check a few lines up. That check is a floor, not a guarantee: the
    // first receive can still miss and fall into the empty-queue wait, leaving
    // the output enabled for up to AUDIO_UNDERRUN_MS with nothing to play. An
    // enabled output with a silent input is audible as hiss, so opening it at the
    // moment the first block is actually written keeps it on only while there is
    // something to put through it.
    bool unmuted=false;
    int64_t last_feed=0;
    uint8_t volume=s.volume;
    while (!stopping()) {
        if (!xQueueReceive(s.audio,&a,pdMS_TO_TICKS(20))) {
            // An empty queue does NOT mean silence yet: the I2S DMA still holds
            // up to ~90 ms, and Wi-Fi delivers in bursts. Treating the first
            // miss as a fatal underrun tore down sessions on a brief hiccup.
            // Only give up once the gap exceeds what the DMA can cover.
            // Receive exactly once per attempt: probing the queue in a loop
            // condition would consume the item and leave nothing to feed.
            int64_t empty_since=esp_timer_get_time();
            bool got=false;
            while (!stopping() && !rx_done()) {
                if (xQueueReceive(s.audio,&a,0)) { got=true; break; }
                if ((esp_timer_get_time()-empty_since)/1000 >= (int64_t)AUDIO_UNDERRUN_MS) break;
                vTaskDelay(pdMS_TO_TICKS(5));
            }
            if (!got) {
                // A cooperative stop empties the queue by design: a channel
                // switch or the OK key clears the flag, the inner loop exits
                // without receiving, and reporting that as an underrun put a
                // spurious reset warning in every switch. Only rx_done() means
                // the peer ended the stream.
                if (stopping() || rx_done()) break;
                ESP_LOGW(TAG,"AUDIO_EMPTY gap_ms=%"PRId64" queue=%u submitted=%"PRIu32,
                    (esp_timer_get_time()-empty_since)/1000,
                    (unsigned)uxQueueMessagesWaiting(s.audio),(uint32_t)s.submitted_samples);
                fail("audio underrun; reconnect/rebuffer"); break;
            }
        }
        if (!unmuted) {
            unmuted=true;
            bsp_audio_set_mute(false);
        }
        if (volume!=s.volume) { volume=s.volume; bsp_audio_set_volume(volume); }
        int64_t before=esp_timer_get_time();
        if (!last_feed) {
            taskENTER_CRITICAL(&clock_lock);
            s.clock_us=before+DMA_ESTIMATE_MS*1000;
            taskEXIT_CRITICAL(&clock_lock);
            xEventGroupSetBits(s.events,CLOCK_READY);
        } else {
            unsigned gap=(before-last_feed)/1000;
            if (gap>s.feed_gap_max_ms) s.feed_gap_max_ms=gap;
            // One chunk more than the queue-empty check above, because this
            // interval covers one extra 20 ms write. The previous 80 ms limit
            // was below the DMA's own ~90 ms of cover, so a routine Wi-Fi gap
            // failed a session that was still playing audio.
            if (gap>AUDIO_FEED_GAP_MS) { fail("audio feed gap exceeds DMA budget"); break; }
        }
        size_t written=0;
        esp_err_t e=bsp_audio_write_timeout(a.pcm,sizeof(a.pcm),&written,100);
        taskENTER_CRITICAL(&clock_lock);
        s.submitted_samples+=written/2;
        taskEXIT_CRITICAL(&clock_lock);
        last_feed=esp_timer_get_time();
        if (e!=ESP_OK || written!=sizeof(a.pcm)) { fail("I2S timeout/partial write"); break; }
    }
    if (!stopping()) delay_until(esp_timer_get_time()+100000); // Estimated final drain.
done:
    if (opened) {
        bsp_audio_set_mute(true);
        if (bsp_audio_stream_close()!=ESP_OK) fail("codec close");
    }
    xEventGroupSetBits(s.events,AUDIO_DONE);
    vTaskDelete(NULL);
}

// ---------------------------------------------------------------------------
// Overlay. The panel is owned through the raw path and the BSP refuses to start
// LVGL once raw has claimed it, so the overlay is drawn straight into the stripe
// buffers on their way to the panel. That also means it costs no extra memory:
// the stripes already exist and are already the right size.
// ---------------------------------------------------------------------------

// Menu geometry. Both the drawing code and the scroll logic need the same
// numbers: computing the row count only where it was drawn left the menu's own
// idea of how many rows fit stuck at zero, so the list never scrolled.
#define MENU_HEADER_H 26
#define MENU_ROW_H    24

// How many list rows fit under the header.
static unsigned menu_rows(void) {
    return ui_menu_visible_rows((int)AV_HEIGHT-MENU_HEADER_H,MENU_ROW_H);
}

// Rows of the panel a stripe covers, as a drawing surface.
static ui_surface_t stripe_surface(unsigned stripe_y, uint8_t *pixels) {
    ui_surface_t surface = { pixels, (int)AV_WIDTH, (int)stripe_y, (int)AV_STRIPE_ROWS };
    return surface;
}

// Copy the display name for a channel id, or the id itself when there is no
// name, into the caller's buffer. Used for the "connecting to" banner, which
// names a channel that is not the one playing yet, so it cannot go through
// current_channel_name().
//
// The name is copied out under the lock, never returned as a pointer into the
// table: the receive task replaces that table from CONFIG, and a pointer handed
// back here was dereferenced later by the caller, on another task, with no lock
// held -- a read of ids and names that could be rewritten mid-use.
static void channel_name_of(const char *id, char *out, size_t size) {
    if (!out || !size) return;
    out[0]='\0';
    if (!id || !id[0]) return;
    bool found=false;
    taskENTER_CRITICAL(&list_lock);
    unsigned count=atomic_load(&s.count);
    for (unsigned i=0;i<count;i++) {
        if (!strcmp(s.list[i].id,id)) {
            snprintf(out,size,"%s",s.list[i].name[0] ? s.list[i].name : s.list[i].id);
            found=true;
            break;
        }
    }
    taskEXIT_CRITICAL(&list_lock);
    // Not in the list: the id is still the most useful thing to show, and it is
    // the caller's own string rather than anything this task is holding.
    if (!found) snprintf(out,size,"%s",id);
}

// The name to show for the channel now playing, copied out under the lock for
// the same reason as above. Falls back to the id when the server sent no
// display name, so the banner is never blank.
static void current_channel_name(char *out, size_t size) {
    if (!out || !size) return;
    out[0]='\0';
    bool found=false;
    taskENTER_CRITICAL(&list_lock);
    unsigned count=atomic_load(&s.count);
    for (unsigned i=0;i<count;i++) {
        if (!strcmp(s.list[i].id,s.channel)) {
            snprintf(out,size,"%s",s.list[i].name[0] ? s.list[i].name : s.list[i].id);
            found=true;
            break;
        }
    }
    taskEXIT_CRITICAL(&list_lock);
    if (!found) snprintf(out,size,"%s",s.channel[0] ? s.channel : "?");
}

// Signal bars from the connected access point's RSSI. Returns 0..4; 0 means the
// reading failed, which the status page shows as "no signal" rather than as a
// weak one, because an unreadable value is not the same as a bad one.
static unsigned wifi_bars(void) {
    wifi_ap_record_t ap;
    if (esp_wifi_sta_get_ap_info(&ap)!=ESP_OK) return 0;
    int rssi=ap.rssi;
    if (rssi>=-55) return 4;
    if (rssi>=-65) return 3;
    if (rssi>=-75) return 2;
    return 1;
}

// Draw one stripe's worth of overlay, if this stripe is one the overlay occupies.
// Everything is positioned from absolute screen rows so a stripe can be drawn in
// isolation without knowing what came before it.
static void overlay_stripe(unsigned stripe_y, uint8_t *pixels) {
    ui_surface_t surface=stripe_surface(stripe_y,pixels);
    unsigned first=stripe_y, last=stripe_y+AV_STRIPE_ROWS;   // [first,last)

    // A helper for anchored drawing: place a line whose text top is `y`, only
    // when this stripe overlaps the rows the text and its box occupy.
    #define LINE_TOP_PAD UI_TEXT_PAD_Y
    #define LINE_HEIGHT ((int)UI_TEXT_LINE_H + 2*UI_TEXT_PAD_Y)

    if (s.menu.view==UI_VIEW_VIDEO) {
        // The volume indicator, at the top of the screen while it is up.
        //
        // The top, not the bottom, because the channel banner already lives at
        // the bottom and the two can be on screen together -- change channel,
        // then reach for the volume before the banner has expired. Putting this
        // above it would have hidden one of them, and neither is worth losing.
        //
        // It shows a number and a filled bar together: the number is the exact
        // level, the bar is how much of the range is left, and a glance usually
        // wants the second.
        if (ui_menu_volume_visible(&s.menu)) {
            // Laid out downwards from the text, not from a guessed band height:
            // ui_text_draw paints a box of LINE_HEIGHT rows, and the bar has to
            // start below that box's bottom edge or it draws over the glyphs.
            // The band is then whatever that comes to.
            const int box_top=0;
            const int text_bottom=box_top+LINE_TOP_PAD+LINE_HEIGHT;
            const int bar_y=text_bottom+4, bar_h=4;
            const int box_h=(bar_y+bar_h)-box_top;
            if ((int)first<(unsigned)(box_top+box_h) && (int)last>(unsigned)box_top) {
                char text[20];
                snprintf(text,sizeof(text),"Volume %u%%",s.menu.volume);
                // One block first, so the text and the bar sit on the same
                // known colour rather than directly on the picture.
                ui_text_fill(surface,0,box_top,(int)AV_WIDTH,box_h,UI_COLOR_BOX);
                ui_text_draw(surface,12,box_top+LINE_TOP_PAD,text,UI_COLOR_TEXT,UI_COLOR_BOX);
                const int bar_x=12;
                const int bar_w=(int)AV_WIDTH-2*bar_x;
                ui_text_fill(surface,bar_x,bar_y,bar_w,4,UI_COLOR_RULE);
                const int filled=(int)((unsigned)bar_w*s.menu.volume/100u);
                if (filled>0) ui_text_fill(surface,bar_x,bar_y,filled,4,UI_COLOR_SELECT);
            }
        }
        // The banner sits at the bottom, just above the edge: it is the least
        // disruptive place for something that appears on its own.
        if (s.menu.banner_ms>0 && s.menu.banner[0]) {
            int height=LINE_HEIGHT, top=(int)AV_HEIGHT-height;
            if ((int)first<(int)AV_HEIGHT && (int)last>top) {
                // A full-width bar reads as a caption rather than a stray box.
                ui_text_fill(surface,0,top,(int)AV_WIDTH,height,UI_COLOR_BOX);
                int width=ui_text_width(s.menu.banner);
                int x=(width>=(int)AV_WIDTH)?0:((int)AV_WIDTH-width)/2;
                ui_text_draw(surface,x,top+LINE_TOP_PAD,s.menu.banner,
                             UI_COLOR_TEXT,UI_COLOR_BOX);
            }
        }
    } else if (s.menu.view==UI_VIEW_MENU) {
        // Header band, then one row per channel.
        const int header=MENU_HEADER_H, row=MENU_ROW_H;
        if (first<(unsigned)header) {
            ui_text_fill(surface,0,0,(int)AV_WIDTH,header,UI_COLOR_BOX);
        }
        if (first<(unsigned)header+1u && last>(unsigned)header) {
            ui_text_fill(surface,0,header,(int)AV_WIDTH,1,UI_COLOR_RULE);
        }
        unsigned rows=menu_rows();
        for (unsigned i=0;i<rows;i++) {
            unsigned index=s.menu.scroll+i;
            if (index>=s.menu.count) break;
            int y=header+(int)i*row+4;
            if ((int)last<=y || (int)first>=y+row-2) continue;
            bool chosen=index==s.menu.selected;
            if (chosen) ui_text_fill(surface,0,y-4,(int)AV_WIDTH,row,UI_COLOR_SELECT);
            const char *label=s.menu.entries[index].name[0]
                ? s.menu.entries[index].name : s.menu.entries[index].id;
            ui_text_draw(surface,14,y,label,
                         chosen?UI_COLOR_SELECT_TEXT:UI_COLOR_TEXT,
                         chosen?UI_COLOR_SELECT:UI_COLOR_BOX);
        }
    } else if (s.menu.view==UI_VIEW_BRIGHTNESS) {
        // The backlight page. Deliberately the same shape as the volume
        // indicator: a labelled level over a filled bar, because they are the
        // same kind of thing and should not need to be learned twice.
        //
        // It is drawn on the video rather than as a list row, so it does not
        // depend on a channel list having arrived: the backlight has to be
        // reachable on a device that has not connected to a server yet.
        const int header=MENU_HEADER_H;
        if (first<(unsigned)header) {
            ui_text_fill(surface,0,0,(int)AV_WIDTH,header,UI_COLOR_BOX);
        }
        if (first<(unsigned)header+1u && last>(unsigned)header) {
            ui_text_fill(surface,0,header,(int)AV_WIDTH,1,UI_COLOR_RULE);
        }
        const int level_y=header+16;
        if ((int)last>level_y-UI_TEXT_PAD_Y && (int)first<level_y+LINE_HEIGHT) {
            char text[32];
            snprintf(text,sizeof(text),"Brightness  %u%%",
                     av_brightness_percent(s.menu.brightness));
            ui_text_draw(surface,14,level_y,text,UI_COLOR_TEXT,UI_COLOR_BOX);
            // The bar sits below the label, laid out from the label's own box so
            // it cannot be drawn over the glyphs.
            const int bar_x=14, bar_y=level_y+LINE_HEIGHT, bar_h=6;
            const int bar_w=(int)AV_WIDTH-2*bar_x;
            ui_text_fill(surface,bar_x,bar_y,bar_w,bar_h,UI_COLOR_RULE);
            // Filled from the level itself, so the bar shows where this step
            // sits in the range rather than which step number it is.
            const unsigned percent=av_brightness_percent(s.menu.brightness);
            const int filled=(int)((unsigned)bar_w*percent/100u);
            if (filled>0) ui_text_fill(surface,bar_x,bar_y,filled,bar_h,UI_COLOR_SELECT);
            // The hint, dimmed, one line under the bar.
            const int hint_y=bar_y+bar_h+10;
            if ((int)last>hint_y-UI_TEXT_PAD_Y && (int)first<hint_y+LINE_HEIGHT) {
                ui_text_draw(surface,14,hint_y,"UP/DOWN to change, OK to finish",
                             UI_COLOR_DIM,UI_COLOR_BACKDROP);
            }
        }
    } else {
        // Status page: three labelled lines, drawn straight onto the backdrop.
        const int header=MENU_HEADER_H, line=MENU_ROW_H;
        if (first<(unsigned)header) {
            ui_text_fill(surface,0,0,(int)AV_WIDTH,header,UI_COLOR_BOX);
        }
        if (first<(unsigned)header+1u && last>(unsigned)header) {
            ui_text_fill(surface,0,header,(int)AV_WIDTH,1,UI_COLOR_RULE);
        }
        // Sized for the label plus a full-length channel name (UI_MENU_NAME_MAX).
        // The name is bounded by the table, not by this line, so a buffer that
        // holds only the label is a truncation warning the compiler is right to
        // raise.
        char text[UI_MENU_NAME_MAX+24];
        // Four lines: three readings and the gesture that leaves for setup. The
        // fourth is not decoration -- the setup gesture is a long press on an
        // arrow, which nothing else on this page uses and which nobody would
        // guess, so the page has to say it.
        for (unsigned i=0;i<4;i++) {
            int y=header+8+(int)i*line;
            if ((int)last<=y-UI_TEXT_PAD_Y || (int)first>=y+(UI_TEXT_LINE_H+UI_TEXT_PAD_Y)) continue;
            uint16_t colour=UI_COLOR_TEXT;
            if (i==0) {
                // Copied out under the table lock: this runs on the video task
                // while the receive task may be replacing the table.
                char name[UI_MENU_NAME_MAX];
                current_channel_name(name,sizeof(name));
                snprintf(text,sizeof(text),"Channel  %s",name);
            } else if (i==1) {
                unsigned bars=wifi_bars();
                if (bars) snprintf(text,sizeof(text),"WiFi     %u/4",bars);
                else snprintf(text,sizeof(text),"WiFi     --");
            } else if (i==2) {
                int soc=atomic_load(&s.battery_soc);
                if (soc>=0) snprintf(text,sizeof(text),"Battery  %d%%",soc);
                else snprintf(text,sizeof(text),"Battery  --");
            } else {
                // Dimmed, because it is an instruction rather than a reading.
                //
                // It says "WiFi" rather than "network" because that is the word
                // on the setup page the viewer will land on, and it says which
                // arrow because the other one does nothing here.
                colour=UI_COLOR_DIM;
                snprintf(text,sizeof(text),"Hold UP: set up WiFi");
            }
            ui_text_draw(surface,14,y,text,colour,UI_COLOR_BACKDROP);
        }
    }
    #undef LINE_HEIGHT
    #undef LINE_TOP_PAD
}

typedef struct { video_t frame; size_t pos; unsigned stripe_y, next_x; } decode_t;
static UINT jpeg_input(JDEC *jd, BYTE *buf, UINT n) {
    decode_t *d=jd->device;
    size_t left=d->frame.length-d->pos;
    if(n>left) n=left;
    if(buf) memcpy(buf,d->frame.jpeg+d->pos,n);
    d->pos+=n; return n;
}
static UINT jpeg_output(JDEC *jd, void *bitmap, JRECT *r) {
    decode_t *d=jd->device;
    if(stopping()) return 0;
    unsigned source_y=d->stripe_y/2;
    unsigned source_rows=AV_VIDEO_HEIGHT-source_y;
    if(source_rows>AV_MCU_ROWS) source_rows=AV_MCU_ROWS;
    // ROM 4:2:0, scale=0: raster-ordered 16x16 RGB888 MCUs, last row 16x8.
    // Validate order as well as bounds so stale/unfilled stripe bytes cannot ship.
    if(d->stripe_y>=AV_HEIGHT || r->left!=d->next_x ||
       r->right!=r->left+15 || r->top!=source_y ||
       r->bottom!=source_y+source_rows-1) return 0;
    unsigned stripes=source_rows*2/AV_STRIPE_ROWS;
    for(unsigned i=0;i<stripes;i++) {
        if(!av_pack_rgb888_x2(s.stripe[i],d->stripe_y+i*AV_STRIPE_ROWS,
                             r->left,r->top,r->right,r->bottom,bitmap)) return 0;
    }
    d->next_x=r->right+1;
    if(d->next_x==AV_VIDEO_WIDTH) {
        // Both buffers hold one complete MCU row. Drain each DMA before reuse;
        // no extra 32-row buffers and no claimed decode/transfer overlap.
        for(unsigned i=0;i<stripes;i++) {
            // The overlay is painted into the stripe on its way to the panel:
            // the buffers already exist, so text costs no extra memory, and a
            // stripe that the overlay does not touch is passed through untouched.
            overlay_stripe(d->stripe_y,s.stripe[i]);
            if(bsp_display_raw_submit(d->stripe_y,AV_STRIPE_ROWS,s.stripe[i],200)!=ESP_OK ||
               bsp_display_raw_wait(200)!=ESP_OK) return 0;
            d->stripe_y+=AV_STRIPE_ROWS;
        }
        d->next_x=0;
        vTaskDelay(1); // C3 single core: do not starve networking/buttons.
    }
    return 1;
}
static void drain_display(void) {
    for(unsigned i=0;i<10;i++) {
        esp_err_t e=bsp_display_raw_wait(200);
        if(e==ESP_OK) return;
        fail("LCD DMA drain failed; buffers retained");
    }
    // Fault-only recovery, never free live DMA memory or force-delete a worker.
    ESP_LOGE(TAG,"LCD DMA unresponsive for 2s; fault reboot without freeing buffers");
    esp_restart();
}
// The SMPTE-style bars, in the order they are conventionally drawn: white,
// yellow, cyan, green, magenta, red, blue. Seven equal vertical bars fill the
// width exactly because 320/7 is not a whole number of pixels -- each bar takes
// the pixels its share covers, so the last one absorbs the remainder rather than
// leaving a strip of untouched memory at the right edge.
static void draw_color_bars(ui_surface_t surface) {
    static const uint16_t bars[7] = {
        0xFFFFu, // white
        0xFFE0u, // yellow
        0x07FFu, // cyan
        0x07E0u, // green
        0xF81Fu, // magenta
        0xF800u, // red
        0x001Fu, // blue
    };
    // Filled from the surface's own origin: ui_text_fill clips to the rows this
    // stripe covers, so a y of 0 would draw nothing on every stripe but the
    // first.
    for (unsigned i=0;i<7;i++) {
        int left=(int)((unsigned)AV_WIDTH*i/7u);
        int right=(int)((unsigned)AV_WIDTH*(i+1u)/7u);
        ui_text_fill(surface,left,surface.origin_y,right-left,surface.rows,bars[i]);
    }
}
// Whether the waiting screen needs painting again.
//
// Asked from the video task main loop rather than decided once when the session
// begins. The link state is reset to "dialling" at the start of every session,
// so a decision taken at that instant can only ever see "dialling" -- it paints
// "connecting" and never sees the failure that follows, and the screen then sits
// on "connecting" for as long as the device keeps retrying. Asking repeatedly
// makes the screen follow the state instead of sampling it once at the worst
// possible moment.
static bool waiting_screen_stale(void)
{
    // Nothing owns the panel until the first frame arrives. After that the
    // frames do, and repainting the bars underneath them would flash.
    if (atomic_load(&s.media_started)) return false;
    if (!waiting_drawn) return true;
    if (strcmp(s.channel,shown_channel)) return true;
    if ((int)s.menu.view!=waiting_view) return true;
    if ((int)atomic_load(&s.link)!=waiting_link) return true;
    // While an overlay is up it is repainted on a slow timer: its readings
    // change and a key press has to show. The bars beneath it do not, which is
    // why this is not simply every pass.
    if (s.menu.view!=UI_VIEW_VIDEO &&
        esp_timer_get_time()-waiting_painted_us>=(int64_t)WAITING_REFRESH_MS*1000) return true;
    return false;
}

// Paint the colour-bar waiting screen, with whatever overlay is up.
//
// Returns false when the panel stopped accepting data, which the caller treats
// as a display fault.
//
// The overlay is painted here too, and that is what makes the status page
// reachable at all. That page is drawn by overlay_stripe(), whose only other
// caller is the JPEG output path -- so an overlay could otherwise appear only
// over a moving picture. A device with no picture would then have no status
// page, and the status page is the only way to the setup screen: the state that
// needs setup most was the one state that could not reach it.
static bool paint_waiting_screen(void)
{
    waiting_drawn=true;
    waiting_view=(int)s.menu.view;
    waiting_link=(int)atomic_load(&s.link);
    waiting_painted_us=esp_timer_get_time();
    snprintf(shown_channel,sizeof(shown_channel),"%s",s.channel);
for(unsigned y=0;y<AV_HEIGHT && !stopping();y+=AV_STRIPE_ROWS) {
    ui_surface_t strip=stripe_surface(y,s.stripe[0]);
    draw_color_bars(strip);
    // One panel holding both lines, drawn as a unit: the name, and below it
    // the reason there is no picture when there is one worth giving.
    //
    // Laid out together rather than positioned one at a time, so the panel
    // is centred on what it holds and both lines share a left edge. Sized to
    // the wider line, and kept a margin from the screen edge so it reads as
    // a card over the pattern rather than a band across it.
    //
    // The whole panel is drawn by every strip its box touches, not just the
    // strip the first line starts in. Overlap is the test, and it is taken
    // against the box each line occupies rather than its glyphs alone: a
    // line begins above its glyphs (UI_TEXT_PAD_Y) and ends below them, so
    // measuring glyphs leaves the strip holding the padding to skip the
    // line, and testing where a line *starts* cuts it at the boundary. Lines
    // straddle strips by construction -- strips are 16 rows and a line is 24
    // -- so either mistake shows as text sliced through the middle.

    // What the viewer is waiting for, and why.
    //
    // The connection has three states and the screen shows a different
    // thing for each. The one that matters here is the middle: while the
    // dial is in progress nothing has been concluded, and saying "cannot
    // reach the server" during those seconds is not a premature verdict so
    // much as a false one -- it sends the viewer to change a setting that
    // was never the problem, and the connection usually succeeds a moment
    // later. Failure is reported only once the attempt is over.
    const link_state_t link=(link_state_t)atomic_load(&s.link);
    const bool dialling = link==LINK_TRYING;
    const bool failed   = link==LINK_DOWN && !atomic_load(&s.media_started);

    // Keyed on whether the server answered rather than on whether an address is
    // stored, because a stored address that is wrong -- a stale lease, a
    // mistyped port -- leaves the device just as stuck and is the case most
    // worth naming.
    //
    // There is deliberately no "no address set" wording. The setup page requires
    // that field, so a device which got through setup always has one; a device
    // that never went through it is in setup rather than here. The line would
    // name a state that cannot be reached, and a message that cannot be true is
    // worse than none: it sends the viewer to look for a setting they already
    // have.
    char heading[UI_MENU_NAME_MAX+16];
    const char *reason=NULL;
    if (failed) {
        reason="连不上服务器";
    }
    bool has_heading=s.channel[0]!=0;
    if (has_heading) {
        current_channel_name(heading,sizeof(heading));
    } else {
        // No channel named yet, so the line carries the state instead, which
        // is worth saying only because the alternative is an empty panel.
        //
        // Both remaining cases are covered: still dialling, and connected
        // but not yet sent a channel. They read differently because they are
        // different -- the first is waiting on the network, the second on the
        // server's transcoder, which takes seconds on a live channel -- and a
        // viewer who sees the same words for both cannot tell whether the
        // wait is normal.
        snprintf(heading,sizeof(heading),
                 dialling ? "正在连接" : "正在打开频道");
        has_heading=true;
    }

    // How to get out of this state, shown whenever the device cannot reach
    // its server.
    //
    // Without it the screen is a dead end: the way to the setup page is a
    // long press on an arrow *on the status page*, and the status page is
    // opened by double-clicking that same arrow. Nothing about the device
    // suggests either gesture, so a viewer who does not already know them
    // has no move to make -- and this is the screen where they need one. The
    // other half of the fix is that the status page now draws over this
    // screen; telling the viewer about a screen that cannot appear would be
    // worse than saying nothing.
    const char *hint=NULL;
    if (reason) {
        hint="双击↑ 长按↑ 改地址";
    }

    // Measured with the padding each line is drawn with, not the glyphs
    // alone: ui_text_draw paints its own box around the text, so a panel
    // sized to the glyph run alone is a few pixels narrower than the text
    // inside it and the two edges disagree.
    //
    // Bounded so a long channel name shrinks the panel rather than running
    // off the screen; the lines themselves are clipped by the surface.
    const int PANEL_MAX=(int)AV_WIDTH-24;
    int text_w=0;
    if (has_heading) text_w=ui_text_width(heading);
    if (reason) {
        int rw=ui_text_width(reason);
        if (rw>text_w) text_w=rw;
    }
    if (hint) {
        int hw=ui_text_width(hint);
        if (hw>text_w) text_w=hw;
    }
    // The lines are stacked; the count is what the height follows.
    int lines=0;
    if (has_heading) lines++;
    if (reason) lines++;
    if (hint) lines++;
    if (lines==0) lines=1;

    int panel_w=text_w+2*UI_TEXT_PAD_X;
    if (panel_w>PANEL_MAX) panel_w=PANEL_MAX;
    int panel_x=((int)AV_WIDTH-panel_w)/2;
    const int line_step=(int)UI_TEXT_LINE_H+2*(int)UI_TEXT_PAD_Y;
    int panel_h=lines*line_step;
    int panel_y=(int)AV_HEIGHT/2-panel_h/2;

    if ((int)y<panel_y+panel_h && panel_y<(int)(y+AV_STRIPE_ROWS)) {
        // The panel behind the lines. One rectangle, not one per line, so
        // they read as a single object with a shared background, and every
        // line starts at the same left edge -- centring each on its own made
        // them disagree whenever their widths did.
        ui_text_fill(strip,panel_x,panel_y,panel_w,panel_h,UI_COLOR_BOX);
        int row=0;
        if (has_heading) {
            ui_text_draw(strip,panel_x+UI_TEXT_PAD_X,panel_y+row*line_step+UI_TEXT_PAD_Y,
                         heading,UI_COLOR_TEXT,UI_COLOR_BOX);
            row++;
        }
        if (reason) {
            // Dimmer than a name, because it explains rather than
            // identifies: the name is what the viewer is looking for, this
            // is why it has not appeared.
            ui_text_draw(strip,panel_x+UI_TEXT_PAD_X,panel_y+row*line_step+UI_TEXT_PAD_Y,
                         reason,UI_COLOR_DIM,UI_COLOR_BOX);
            row++;
        }
        if (hint) {
            ui_text_draw(strip,panel_x+UI_TEXT_PAD_X,panel_y+row*line_step+UI_TEXT_PAD_Y,
                         hint,UI_COLOR_DIM,UI_COLOR_BOX);
        }
    }
    // On top of the panel, so an overlay covers it rather than competing
    // with it: the status page and the channel list paint their own
    // backgrounds across the full width for exactly this reason.
    overlay_stripe(y,s.stripe[0]);
    if(bsp_display_raw_submit(y,AV_STRIPE_ROWS,s.stripe[0],200)!=ESP_OK ||
       bsp_display_raw_wait(200)!=ESP_OK) {
        fail("waiting screen DMA"); break;
    }
}
    return true;
}

static void video_task(void *arg) {
    (void)arg;
    bool claimed=bsp_display_raw_claim()==ESP_OK;
    if(!claimed) { fail("raw display claim"); goto done; }
    // Painted before the first frame so the panel never shows uninitialised
    // memory, and repainted from the main loop below whenever what it shows
    // changes -- the connection state moves on its own, and the screen has to
    // move with it.
    if (!paint_waiting_screen()) goto done;
    while(!stopping()) {
        // The connection state moves without a frame ever arriving: the dial
        // gives up after a few seconds and the screen has to say so. This is the
        // only place its owner runs while it waits, so this is where the check
        // belongs.
        if (waiting_screen_stale() && !paint_waiting_screen()) break;
        video_t v;
        if(!xQueueReceive(s.video,&v,pdMS_TO_TICKS(20))) { if(rx_done()) break; continue; }
        while(!stopping() && !(xEventGroupGetBits(s.events)&CLOCK_READY)) {
            if(xEventGroupGetBits(s.events)&AUDIO_DONE) { fail("no audio clock"); break; }
            vTaskDelay(pdMS_TO_TICKS(5));
        }
        while(!stopping() && estimated_pts()<(int64_t)v.pts) {
            if(xEventGroupGetBits(s.events)&AUDIO_DONE) break;
            vTaskDelay(pdMS_TO_TICKS(5));
        }
        if(!stopping() && estimated_pts()-(int64_t)v.pts<=100) {
            decode_t d={.frame=v}; JDEC jd;
            int64_t start=esp_timer_get_time();
            JRESULT r=jd_prepare(&jd,jpeg_input,s.work,4096,&d);
            if(r==JDR_OK && jd.width==AV_VIDEO_WIDTH && jd.height==AV_VIDEO_HEIGHT && jd.msx==2 && jd.msy==2) {
                r=jd_decomp(&jd,jpeg_output,0); // Decode 160x120 RGB888, then nearest-neighbor x2.
            } else r=JDR_FMT3;
            // Every frame boundary drains DMA before stripe[0] is reused.
            drain_display();
            unsigned elapsed=(esp_timer_get_time()-start)/1000;
            if(elapsed>s.decode_max_ms) s.decode_max_ms=elapsed;
            if(r==JDR_OK && d.stripe_y==AV_HEIGHT) s.decoded++;
            else if(!stopping()) fail("JPEG decode/geometry/DMA error");
        } else s.dropped++;
        xQueueSend(s.free_video,&v.jpeg,0);
    }
    // Fault recovery preserves ownership until completion or a hardware reboot.
    drain_display();
    if(bsp_display_raw_release()!=ESP_OK) fail("raw release");
done:
    xEventGroupSetBits(s.events,VIDEO_DONE);
    vTaskDelete(NULL);
}
static bool allocate_session(void) {
    s.audio=xQueueCreate(PCM_QUEUE,sizeof(audio_t)); s.video=xQueueCreate(2,sizeof(video_t));
    s.free_video=xQueueCreate(2,sizeof(uint8_t *));
    for(int i=0;i<2;i++) {
        s.jpeg[i]=heap_caps_malloc(AV_VIDEO_MAX,MALLOC_CAP_INTERNAL|MALLOC_CAP_8BIT);
        s.stripe[i]=heap_caps_malloc(STRIPE_BYTES,MALLOC_CAP_INTERNAL|MALLOC_CAP_DMA);
    }
    s.work=malloc(4096);
    if(!s.audio || !s.video || !s.free_video || !s.jpeg[0] || !s.jpeg[1] ||
       !s.stripe[0] || !s.stripe[1] || !s.work) return false;
    for(int i=0;i<2;i++) xQueueSend(s.free_video,&s.jpeg[i],0);
    return true;
}
static void free_session(void) {
    if(s.audio) vQueueDelete(s.audio);
    if(s.video) vQueueDelete(s.video);
    if(s.free_video) vQueueDelete(s.free_video);
    s.audio=s.video=s.free_video=NULL;
    for(int i=0;i<2;i++) { free(s.jpeg[i]); free(s.stripe[i]); s.jpeg[i]=s.stripe[i]=NULL; }
    free(s.work); s.work=NULL;
}
// Ask the session loop to reconnect on the id the menu selected. Returns false
// when there is nothing to do, e.g. the chosen channel is the one already on.
static bool request_switch(const char *id) {
    if (!id || !id[0]) return false;
    if (!strcmp(id,s.channel)) return false;   // already there; reconnecting would flicker
    unsigned count=atomic_load(&s.count);
    unsigned index=0;
    bool found=false;
    taskENTER_CRITICAL(&list_lock);
    for (unsigned i=0;i<count;i++) {
        if (!strcmp(s.list[i].id,id)) { index=i; found=true; break; }
    }
    if (found) atomic_store(&s.index,index);
    taskEXIT_CRITICAL(&list_lock);
    // Copy only an id that actually fits, so pending is always terminated. The
    // old bound could truncate a long id and leave the string unterminated.
    size_t length=strnlen(id,AV_CHANNEL_ID_MAX);
    if (!length || length>=AV_CHANNEL_ID_MAX) return false;
    memcpy(s.pending,id,length+1);
    atomic_store(&s.chosen_by_user,true);
    atomic_store(&s.switch_pending,true);
    xEventGroupSetBits(s.events,STOP);          // end this session; the loop reconnects
    return true;
}

// Applies one gesture. Called only from the session owner (main loop), so it can
// touch s.channel while no connection is being built.
static void process_key(const key_ev_t *item) {
    bool up = item->key==BSP_BTN_UP;

    // The setup page owns the device while it is up. A channel must not change
    // and a session must not start behind it, so nothing else is reachable and
    // the only gesture that does anything is the one that leaves.
    if (atomic_load(&s_setup_active)) {
        if (item->key==BSP_BTN_OK && item->ev==BSP_BTN_LONG) {
            atomic_store(&s_setup_leave,true);
        }
        return;
    }

    // While the list, the status page or the backlight page is open the keys
    // belong to the overlay: a gesture must not both adjust something here and
    // change the channel behind it.
    if (s.menu.view!=UI_VIEW_VIDEO) {
        if (item->key==BSP_BTN_OK) {
            if (item->ev==BSP_BTN_CLICK) {
                if (s.menu.view==UI_VIEW_MENU) {
                    ui_menu_confirm(&s.menu);
                } else {
                    ui_menu_close(&s.menu);
                }
            } else if (item->ev==BSP_BTN_LONG) {
                ui_menu_close(&s.menu);          // long press is the way out
            }
            return;
        }
        // The arrows adjust the backlight while that page is up, and put it away
        // on any other page. This is the one view where a click does not leave:
        // the whole point of opening it is to press up and down a few times, and
        // closing on the first press would make it useless.
        if (s.menu.view==UI_VIEW_BRIGHTNESS && item->ev==BSP_BTN_CLICK) {
            s.menu.brightness=av_brightness_step(s.menu.brightness,up);
            bsp_display_backlight(av_brightness_percent(s.menu.brightness));
            // Marked dirty rather than saved here: this runs on the button task,
            // and writing to flash takes long enough to delay the next press.
            // The session loop picks the flag up when it is otherwise idle.
            atomic_store(&s.settings_dirty,true);
            // Keep the page up while the viewer is still adjusting, the same way
            // the list refreshes its own timeout on every scroll.
            ui_menu_keep_open(&s.menu);
            return;
        }
        if (item->ev==BSP_BTN_CLICK) {
            if (s.menu.view==UI_VIEW_MENU) {
                ui_menu_scroll_by(&s.menu,up?-1:1,menu_rows());
            }
            else ui_menu_close(&s.menu);         // any key leaves the other pages
            return;
        }
        // Leaving the status page for the setup screen. The arrows' long press is
        // the one gesture the status page does not already use -- OK's long press
        // closes the page, and both clicks scroll or close -- so the setup screen
        // is reachable without taking a control away from anything else.
        if (s.menu.view==UI_VIEW_STATUS && item->ev==BSP_BTN_LONG && up) {
            atomic_store(&s_setup_request,true);
            // The session has to actually stop, and the workers only notice when
            // this bit is set: they run `while (!stopping())`, so raising a
            // request flag alone leaves all three spinning and the session never
            // ends. Without this the drain waits out its full timeout and the
            // device restarts instead of showing the setup screen. Channel
            // switching sets the same bit for the same reason (request_switch).
            xEventGroupSetBits(s.events,STOP);
        }
        return;
    }

    if (item->key==BSP_BTN_OK) {
        if (item->ev==BSP_BTN_CLICK) {
            // The channel list. A click rather than a long press because the
            // list is what the viewer reaches for while watching; the cost is
            // that the button has to wait out the long-press window before the
            // driver can call it a click.
            ui_menu_set_rows(&s.menu,menu_rows());
            ui_menu_open(&s.menu);
            return;
        }
        if (item->ev==BSP_BTN_LONG) {
            // The backlight. It is set once and then left alone, so it takes the
            // gesture that responds immediately and is harder to reach by
            // accident -- holding a button for a second and a half is not
            // something a stray touch does.
            ui_menu_open_brightness(&s.menu);
            return;
        }
        return;
    }
    if (item->ev==BSP_BTN_LONG) {
        // Long press steps the volume, ten percent at a time.
        //
        // It was five, and that read as "the button does nothing": the codec
        // maps the percentage linearly onto -50..0 dB, so 5% is 2.5 dB, which is
        // about five steps of the ES8311's own register and very hard to hear.
        // Ten percent is 5 dB, which is a change the ear picks up. The register
        // was verified to move on every press, so this was always a step-size
        // problem rather than a broken control.
        //
        // The floor is low enough to be quiet in a small room and the ceiling
        // full scale, because the useful setting depends on the speaker and the
        // room and the viewer is the one who can hear it.
        if (up && s.volume<100) {
            s.volume = (uint8_t)(s.volume>90u ? 100u : s.volume+10u);
        }
        if (!up && s.volume>0) {
            s.volume = (uint8_t)(s.volume<10u ? 0u : s.volume-10u);
        }
        // Show the level. The audio worker picks the value up on its next chunk;
        // this only reports it. It runs even at the stop of the range, so the
        // indicator still appears and the button never looks dead.
        ui_menu_show_volume(&s.menu,s.volume);
        // Remember it, but not from here: see settings_dirty.
        atomic_store(&s.settings_dirty,true);
        return;
    }
    if (item->ev==BSP_BTN_DOUBLE) {
        // The status page needs a gesture of its own: a long press on either
        // arrow already means volume, and a long press on OK opens the list.
        ui_menu_open_status(&s.menu);
        return;
    }
    if (item->ev!=BSP_BTN_CLICK) return;
    // A click steps through the server-provided list. With no list yet (the
    // first CONFIG has not arrived) the gesture is ignored rather than guessed.
    // Work out the neighbouring channel and copy its id, all inside the lock the
    // receive task rewrites the table with. No pointer array is built: at
    // AV_CHANNEL_MAX entries that is a kilobyte on a 3.5 KB stack, and the
    // arithmetic it would feed is one line.
    char next[AV_CHANNEL_ID_MAX]={0};
    bool stepped=false;
    taskENTER_CRITICAL(&list_lock);
    unsigned count=atomic_load(&s.count);
    if (count) {
        unsigned index=atomic_load(&s.index) % count;
        unsigned target=(unsigned)(((int)index+(up?1:-1)+(int)count)%(int)count);
        const char *id=s.list[target].id;
        size_t n=id ? strnlen(id,AV_CHANNEL_ID_MAX) : 0;
        if (n>0 && n<AV_CHANNEL_ID_MAX) {
            memcpy(next,id,n+1);
            atomic_store(&s.index,target);
            stepped=true;
        }
    }
    taskEXIT_CRITICAL(&list_lock);
    if (!stepped) return;
    request_switch(next);
}
// Drain the key queue and carry out whatever the overlay asked for.
//
// Every loop that reads keys goes through here. The menu sets its action when a
// gesture is processed, and it must be collected in the same pass: doing that
// only in the session loop meant a choice made just as a session was ending was
// never picked up, so pressing OK appeared to do nothing at all.
// Returns true when the session must end so the new channel can be connected.
// `wait_ms` is how long to block when there is nothing to do. It must not be
// zero on the session path: that loop has no other blocking call, so a
// non-blocking read turns it into a spin that starves every other task on this
// single-core chip -- the watchdog then fires against whichever task happened to
// be running, and no data ever reaches the video and audio queues.
// Stop the current session and wait for its three workers to retire before the
// caller gives the session's buffers back. Returns false when a worker did not
// finish in time, in which case nothing may be freed.
//
// This is the whole channel-change contract in one place, because the failure it
// prevents is invisible when it is inline: the loop's exit condition is "all
// three workers signalled DONE", and each signals only after its last touch of a
// queue or a DMA buffer. free_session() destroys both, so ending the loop
// without this wait tore them down under a worker still inside an I2S write or a
// decode, and the device panicked and rebooted. Automatic channel changes never
// hit it because they let the session end on its own.
static void session_drain(void) {
    const EventBits_t want=RX_DONE|AUDIO_DONE|VIDEO_DONE;
    int64_t began=esp_timer_get_time();
    EventBits_t bits=xEventGroupWaitBits(s.events,want,pdFALSE,pdTRUE,
                                         pdMS_TO_TICKS(SESSION_DRAIN_MS));
    // The wait is normally over in well under SESSION_DRAIN_MS. A second, much
    // longer wait covers an unlucky overlap with a codec or DMA call; it is
    // logged because a channel change taking seconds is worth knowing about.
    if ((bits&want)!=want) {
        ESP_LOGW(TAG,"Workers still running after %u ms (missing=0x%02x); waiting up to %u ms more",
                 (unsigned)SESSION_DRAIN_MS,(unsigned)(want&~bits),
                 (unsigned)SESSION_DRAIN_GRACE_MS);
        bits=xEventGroupWaitBits(s.events,want,pdFALSE,pdTRUE,
                                 pdMS_TO_TICKS(SESSION_DRAIN_GRACE_MS));
    }
    if ((bits&want)==want) return;
    // Out of options. This session's queues and DMA buffers cannot be given back
    // while a worker is inside them, and there is no safe way to take a worker
    // out of a codec or DMA call from here: freeing them anyway is what crashed
    // the device, and leaving them allocated strands the memory, which starves
    // every later session until the screen is stuck blank. Restarting is the
    // only outcome that both keeps the memory consistent and leaves the device
    // usable -- it reconnects on its own, and the protected card identity is in
    // flash, untouched by a reset.
    //
    // Reached only if a worker outlives SESSION_DRAIN_MS+SESSION_DRAIN_GRACE_MS,
    // which no normal exit does.
    ESP_LOGE(TAG,"Workers did not retire after %u ms (missing=0x%02x); restarting",
             (unsigned)((esp_timer_get_time()-began)/1000),(unsigned)(want&~bits));
    vTaskDelay(pdMS_TO_TICKS(50));   // let the log reach the port
    esp_restart();
}
// Defined below, next to the loop that uses it. The setup screen drains keys
// through the same function, so that a gesture means the same thing whichever
// mode is up and there is only one place that reads the key queue.
static bool pump_keys(uint32_t wait_ms);

// Paint the setup screen once, top to bottom.
//
// The panel is written in stripes and each stripe is submitted on its own, so
// this is a whole-frame paint rather than an overlay on a moving picture: there
// is no video behind it and nothing else is drawing.
//
// `buffer` is the caller's, not s.stripe[]. The session's buffers are freed
// before setup starts -- handing the access point that memory is what makes it
// fit -- so there is nothing of the session's left to draw into. It must be
// STRIPE_BYTES long: this writes a whole stripe into it, and a shorter buffer is
// a silent overwrite of whatever follows it in memory.
static bool paint_setup_screen(uint8_t *buffer, bool client_ready)
{
    char ssid[64] = {0};
    char url[64] = {0};
    av_provision_ap_name(ssid,sizeof(ssid));
    av_provision_ap_url(url,sizeof(url));

    // The text on this screen, in reading order. Laid out as data because the
    // stripe loop below has to know where every line is before it starts: a
    // stripe can only draw the lines that intersect it.
    typedef struct { int y; const char *text; uint16_t colour; } line_t;
    char state[48];
    snprintf(state,sizeof(state),"%s",client_ready ? "已连接设备" : "等待设备连接");
    // Shown because setup opens for either of two reasons now: no network, or
    // no server address. Without this line the second case looks identical to
    // the first, and the viewer is left re-entering a password that was never
    // the problem.
    //
    // Sized for the longest address that can be stored: the label, the host's
    // full AV_SERVER_ADDR_MAX, and the widest port. A smaller buffer would
    // truncate the address, and a truncated address on screen is worse than
    // none -- it looks correct and sends the viewer to check the wrong thing.
    // The label is three glyphs and a space, eight bytes in UTF-8.
    char address[AV_SERVER_ADDR_MAX+8];
    char fitted[AV_SERVER_ADDR_MAX+8];
    char server[sizeof(fitted)+16] = {0};
    av_server_addr_t stored;
    if (server_address(&stored)) {
        snprintf(address,sizeof(address),"%s:%u",stored.host,(unsigned)stored.port);
        // Trimmed to the width the row can show, so a long host is cut to
        // something that still reads as an address rather than running off the
        // panel. The stored value itself is untouched.
        if (ui_text_truncate(fitted,sizeof(fitted),address,AV_WIDTH-24)) {
            snprintf(server,sizeof(server),"服务器 %s",fitted);
        } else {
            snprintf(server,sizeof(server),"服务器 已设置");
        }
    } else {
        snprintf(server,sizeof(server),"服务器 未设置");
    }
    // Positions are 24 apart, the height a line actually occupies
    // (UI_TEXT_LINE_H plus a pad top and bottom). Closer than that and the
    // opaque boxes behind two labels overlap.
    const line_t lines[] = {
        { 12, "配网模式",      UI_COLOR_TEXT },
        { 40, "请连接此网络：", UI_COLOR_DIM  },
        { 64, ssid,            UI_COLOR_TEXT },
        {100, "打开此地址：",   UI_COLOR_DIM  },
        {124, url,             UI_COLOR_TEXT },
        {158, server,          UI_COLOR_DIM  },
        {190, state,           UI_COLOR_DIM  },
        {216, "长按 OK 退出",  UI_COLOR_DIM  },
    };
    const unsigned line_count=sizeof(lines)/sizeof(lines[0]);

    for(unsigned y=0;y<AV_HEIGHT && !stopping();y+=AV_STRIPE_ROWS) {
        ui_surface_t strip=stripe_surface(y,buffer);
        // The same colour bars as the waiting screen, so the two screens that
        // both mean "not playing yet" look like each other.
        //
        // There is no legibility problem to solve here: ui_text_draw paints an
        // opaque box behind every label, so the bars only show in the gaps. An
        // earlier attempt to fix unreadable text by removing the bars was
        // treating the wrong cause -- the text was being drawn into one stripe
        // out of the several it spans, which is fixed below.
        draw_color_bars(strip);
        for (unsigned i=0;i<line_count;i++) {
            const int line_top=lines[i].y;
            const int line_bottom=line_top+(int)UI_TEXT_LINE_H+2*UI_TEXT_PAD_Y;
            // Overlap, not containment. ui_text_draw paints a background box and
            // then 16 rows of glyph, and a line usually straddles two stripes;
            // testing whether the line *starts* inside this stripe drew only the
            // rows above the boundary and left the rest to the stripe that never
            // drew them at all, so every line came out cut in half. Every stripe
            // that the line touches must draw it, which is what ui_text_fill's
            // clipping makes safe.
            if (line_bottom<=(int)y || line_top>=(int)(y+AV_STRIPE_ROWS)) continue;
            int width=ui_text_width(lines[i].text);
            int x=(width>=(int)AV_WIDTH-24)?12:((int)AV_WIDTH-width)/2;
            ui_text_draw(strip,x,line_top,lines[i].text,lines[i].colour,UI_COLOR_BACKDROP);
        }
        if(bsp_display_raw_submit(y,AV_STRIPE_ROWS,buffer,200)!=ESP_OK ||
           bsp_display_raw_wait(200)!=ESP_OK) {
            fail("setup screen DMA");
            return false;
        }
    }
    return true;
}

// Run the setup screen until it is finished or the viewer leaves.
//
// This owns the panel for its whole duration and is the only thing drawing, so
// it repaints on every state change rather than on a timer: nothing changes
// while it waits, and a timer would only redraw the same pixels.
//
// Returns true when the viewer left deliberately and playback should resume;
// false when setup ran out of time, which the caller answers with a restart
// because the radio has to come back up in station mode.
static bool run_setup_mode(void)
{
    // Start from a clean stop flag.
    //
    // STOP is how a session is told to end, and it is raised by whoever
    // requested setup as well as by failures like a lost network. The session
    // loop clears it at the top of every pass, so one left over from the session
    // that just ended can still be set when this runs -- and everything below
    // reads it: the paint loop treats a set STOP as a dead display and leaves
    // immediately, which looked exactly like the setup screen failing to draw.
    // Clearing it here makes the flag mean "during this setup session" instead of
    // "at some point recently".
    xEventGroupClearBits(s.events,STOP);

    bool claimed=bsp_display_raw_claim()==ESP_OK;
    if(!claimed) {
        ESP_LOGE(TAG,"Cannot show setup: the display is still owned by playback");
        return false;
    }
    atomic_store(&s_setup_active,true);
    atomic_store(&s_setup_leave,false);

    // Bring the audio output up and leave it running with nothing fed to it.
    //
    // This is the colour-bar screen, and a colour bar screen that is silent
    // reads as a device that has locked up rather than one that is waiting. The
    // hiss the codec makes with its output enabled and its input silent is the
    // sound an unused television makes, which is the point: it says the device
    // is on and doing something.
    //
    // Opening the codec here rather than at the top of the program is
    // deliberate. Playback does not run in this mode, so the memory is free, and
    // the alternative -- initializing audio unconditionally at start-up -- would
    // take that memory away from the access point for every device whether or
    // not it ever shows this screen.
    bool audio_ready=false;
    if (bsp_audio_init()==ESP_OK && bsp_audio_set_format(16000,16,1)==ESP_OK) {
        audio_ready=true;
        bsp_audio_set_volume(SETUP_HISS_VOLUME);
        bsp_audio_set_mute(false);
    } else {
        // Not fatal: the screen is still worth showing without the hiss.
        ESP_LOGW(TAG,"Setup screen running without audio");
    }

    // Its own stripe buffer, allocated before the access point takes the rest of
    // the heap and freed on every way out. The session's are gone by now, and
    // s.work is far too small: it is the JPEG decoder's scratch.
    uint8_t *buffer=heap_caps_malloc(STRIPE_BYTES,MALLOC_CAP_INTERNAL|MALLOC_CAP_DMA);
    if(!buffer) {
        ESP_LOGE(TAG,"No memory for the setup screen");
        atomic_store(&s_setup_active,false);
        bsp_display_raw_release();
        return false;
    }

    if(!av_provision_start_ap()) {
        ESP_LOGE(TAG,"Setup access point failed to start");
        free(buffer);
        atomic_store(&s_setup_active,false);
        bsp_display_raw_release();
        return false;
    }

    // Bounded, because the access point is open: anyone in range can join it and
    // change this device's network. The window is long enough to find the
    // network, open the page and type a password, and it does not last forever.
    const uint32_t started=(uint32_t)(esp_timer_get_time()/1000);
    bool client_seen=false;
    bool left=false;
    // Repaint when the answer changes, and once at the start, so the screen
    // reflects the state without redrawing identical pixels every 200 ms.
    bool painted=false;
    bool painted_ready=false;

    bool paint_failed=false;
    for(;;) {
        pump_keys(200);
        bool ready=av_provision_client_ready();
        if(!painted || ready!=painted_ready) {
            painted=true; painted_ready=ready;
            // A failed paint stops the loop. Retrying it would fail the same way
            // every time -- the panel or its DMA is gone -- and each attempt
            // routes through fail(), which logs and raises STOP, so the loop
            // would spin printing while nothing on screen changed. Leaving is
            // what gets the device back to a state it can recover from.
            if(!paint_setup_screen(buffer,ready)) {
                ESP_LOGE(TAG,"Setup screen could not be drawn; leaving setup");
                paint_failed=true;
                break;
            }
        }
        // The panel or the DMA can fail underneath this loop, and fail() marks
        // that by raising STOP. Nothing else here reads it, so without this
        // check the loop would run on with a dead display.
        if(stopping()) {
            ESP_LOGW(TAG,"Setup stopped by a display or worker fault");
            paint_failed=true;
            break;
        }
        if(ready && !client_seen) {
            client_seen=true;
            ESP_LOGI(TAG,"Setup: a device is on the page; heap=%u largest=%u",
                     (unsigned)esp_get_free_heap_size(),
                     (unsigned)heap_caps_get_largest_free_block(MALLOC_CAP_INTERNAL));
        }
        if(atomic_load(&s_setup_leave)) { left=true; break; }
        if(av_setup_expired(started,(uint32_t)(esp_timer_get_time()/1000),
                            AV_SETUP_WINDOW_MS)) {
            ESP_LOGW(TAG,"Setup was not completed within %u minutes",
                     (unsigned)(AV_SETUP_WINDOW_MS/60000u));
            break;
        }
        // The setup screen is finished the moment a network is stored and
        // verified. Restarting is deliberate: the access point, its web server
        // and its DNS responder were all built out of the same stack, and
        // bringing the device back up in station mode is simpler and more
        // reliable than unwinding them and re-initializing in place.
        if(av_provision_has_network()) {
            ESP_LOGI(TAG,"Setup stored a network; restarting to join it");
            if (audio_ready) {
                // Muted first: cutting power to an enabled output is audible as
                // a click, and this is the one moment the device is quiet.
                bsp_audio_set_mute(true);
            }
            av_provision_stop_ap();
            atomic_store(&s_setup_active,false);
            drain_display();
            bsp_display_raw_release();
            free(buffer);
            vTaskDelay(pdMS_TO_TICKS(200));
            esp_restart();
        }
    }

    if (audio_ready) {
        // Muted before the codec is closed, so leaving does not pop.
        bsp_audio_set_mute(true);
        bsp_audio_stream_close();
    }
    av_provision_stop_ap();
    // Bring the connection back before returning.
    //
    // Raising the access point stopped the station, and taking the access point
    // down does not restart it. Without this the viewer leaves the setup page
    // onto a device whose radio is idle: the hotspot disappears as they expect,
    // but playback never resumes and the screen stays where it was. Rejoining
    // here is what makes "leave setup" actually mean "go back to watching".
    av_provision_join_stored();
    av_provision_keep_radio_awake();
    atomic_store(&s_setup_active,false);
    // The display is waited on but not released when a paint failed: the DMA may
    // be wedged, and releasing while a transfer is outstanding is worse than
    // leaving it claimed. The caller restarts, which resets the panel anyway.
    if(!paint_failed) {
        drain_display();
        bsp_display_raw_release();
    }
    free(buffer);
    // A fault raised STOP to mark a dead session; clearing it here stops the
    // next session from starting already told to stop.
    xEventGroupClearBits(s.events,STOP);
    return left && !paint_failed;
}

static bool pump_keys(uint32_t wait_ms) {
    key_ev_t item;
    bool first=true;
    while (xQueueReceive(s.keys,&item,first?pdMS_TO_TICKS(wait_ms):0)) {
        first=false;
        process_key(&item);
    }
    char chosen[AV_CHANNEL_ID_MAX];
    ui_action_t action=ui_menu_take_action(&s.menu,chosen,sizeof(chosen));
    if (action==UI_ACTION_SWITCH) {
        if (request_switch(chosen)) {
            // Close the list first: the banner is drawn in the video view, and
            // leaving the menu up would hide the confirmation behind it.
            ui_menu_close(&s.menu);
            char name[UI_MENU_NAME_MAX];
            channel_name_of(chosen,name,sizeof(name));
            ui_menu_banner(&s.menu,name,NULL);
            ESP_LOGI(TAG,"Switching to %s",chosen);
            return true;
        }
        // The chosen channel is the one already playing: leave the list as it is
        // rather than appearing to do nothing.
    }
    return false;
}

void av_player_main(void) {
    ESP_LOGW(TAG,"RAW LAN prototype; audio clock ESTIMATED (+90ms DMA budget), not DMA/physical output");
    // Nothing is required at build time any more. The network, its password and
    // the address of the streaming server are all entered on the setup page, so
    // a build with none of them compiled in is a working one: it opens that page
    // on first boot instead of refusing to start. The pairing token is optional
    // too, and is sent only when one was compiled in.
    // Start from the server default. An empty id makes the server pick, which
    // avoids the device and server disagreeing about the default channel name.
    memset(s.channel,0,sizeof(s.channel));
    memset(s.pending,0,sizeof(s.pending));
    // Volume and backlight come from the store, falling back to the values this
    // firmware used before either could be changed, so a device that has never
    // been adjusted behaves exactly as it did.
    s.volume=(uint8_t)AV_VOLUME_DEFAULT_PERCENT;
    uint8_t stored_brightness=(uint8_t)AV_BRIGHTNESS_DEFAULT_PERCENT;
    // Filled by wifi_init(): whether a network is known, from the setup page's
    // store or from this build's compiled-in credentials.
    bool has_network=false;
    s.events=xEventGroupCreate(); s.keys=xQueueCreate(8,sizeof(key_ev_t));
    if(!s.events || !s.keys) { ESP_LOGE(TAG,"Control allocation failed"); return; }
    // The display and the WiFi stack are required; audio is not started here,
    // because a device with no network yet has nothing to play and giving the
    // setup page the codec's memory is what makes the access point fit.
    if(bsp_display_init()!=ESP_OK || wifi_init(&has_network)!=ESP_OK) {
        ESP_LOGE(TAG,"Hardware/network initialization failed; no NVS erase attempted"); return;
    }
    // Buttons come up before the mode decision, because leaving setup needs one.
    if(bsp_button_init(on_key,NULL)!=ESP_OK) ESP_LOGW(TAG,"Buttons unavailable");
    ui_menu_init(&s.menu);

    // Now that the menu exists, restore what the viewer last chose. NVS is
    // already up by this point: wifi_init() above initializes it for the
    // provisioning component's credential store.
    // Through locals: s.volume is atomic, and the store deals in plain bytes.
    uint8_t stored_volume=(uint8_t)AV_VOLUME_DEFAULT_PERCENT;
    av_store_load(&stored_volume,&stored_brightness);
    s.volume=stored_volume;
    s.menu.brightness=av_brightness_nearest_index(stored_brightness);
    bsp_display_backlight(av_brightness_percent(s.menu.brightness));

    // Both facts the setup page can supply, read the same way and for the same
    // reason: either may be missing on a device that has never been set up, and
    // the page is the only way to provide one. Resolved eagerly so the decision
    // below and the connection later cannot disagree.
    av_server_addr_t target;
    bool server_configured=server_address(&target);

    // The setup screen is a mode of its own, not a page: it takes the panel and
    // runs until it is done or the viewer leaves. Arriving here without a network
    // is the case a published build is in, since it has nothing compiled in and
    // nothing stored yet.
    //
    // Setup runs at most once here, and leaving it leads on to playback even
    // when nothing was supplied. That is not a dead end: with no network the
    // session loop waits for one and keeps handling keys while it waits, so the
    // status page still opens and its long press still comes back here. Going
    // round this loop instead would take that away -- a viewer who left would be
    // returned immediately, and the access point would be up almost
    // continuously, which is precisely what its time limit exists to avoid.
    if(av_boot_mode(has_network,server_configured)==AV_BOOT_SETUP) {
        ESP_LOGW(TAG,"Setup needed: network=%d server=%d",(int)has_network,(int)server_configured);
        if(!run_setup_mode()) {
            // Setup timed out. Restarting is how the radio comes back up in
            // station mode on the next pass, and how a device left alone in an
            // open access point gets out of it.
            ESP_LOGW(TAG,"Restarting after an unfinished setup");
            vTaskDelay(pdMS_TO_TICKS(200));
            esp_restart();
        }
        // Re-read both rather than assuming the page supplied them. Leaving
        // setup deliberately can happen before anything is filled in, and
        // carrying on with a stale answer would have the device act on a network
        // or an address it does not have.
        vTaskDelay(pdMS_TO_TICKS(300));
        has_network=av_provision_has_network();
        server_configured=server_address(&target);
        ESP_LOGI(TAG,"Left setup: network=%d server=%d",(int)has_network,(int)server_configured);
    }

    // Audio is started only once there is a network to play from: on a device
    // that has just been set up it was never initialized, and the memory this
    // keeps from the setup screen is what let the access point fit in the first
    // place.
    if(bsp_audio_init()!=ESP_OK) {
        ESP_LOGE(TAG,"Audio initialization failed"); return;
    }
    // The battery gauge shares the I2C bus with the codec, so it is brought up
    // once here and only read on a slow schedule from the session loop.
    if (bsp_battery_init()!=ESP_OK) {
        ESP_LOGW(TAG,"Fuel gauge unavailable; the status page will show --");
    }
    atomic_store(&s.battery_soc,-1);
    char last_announced[UI_MENU_NAME_MAX];
    memset(last_announced,0,sizeof(last_announced));
    while(true) {
        pump_keys(50);
        // Apply a requested channel before opening the next session, so the
        // handshake names the new channel. Clearing the flag here (not in the
        // key handler) keeps exactly one writer of s.channel.
        if (atomic_exchange(&s.switch_pending,false)) {
            ESP_LOGI(TAG,"Channel %s -> %s",s.channel,s.pending);
            memcpy(s.channel,s.pending,sizeof(s.channel));
        }
        xEventGroupClearBits(s.events,STOP|RX_DONE|AUDIO_DONE|VIDEO_DONE|CONFIG_READY|CLOCK_READY);
        // Rousing the connection is the WiFi stack's job now, not this loop's.
        // The component retries a dropped association a few times and then
        // rescans on a backoff, so calling esp_wifi_connect() here as well would
        // race it. This waits for the address instead, and keeps the keys alive
        // while it does so the viewer is never locked out of the device.
        if(!(xEventGroupGetBits(s.events)&WIFI_READY)) {
            int64_t deadline=esp_timer_get_time()+15000000;
            while(!(xEventGroupGetBits(s.events)&WIFI_READY) && esp_timer_get_time()<deadline && !stopping()) {
                pump_keys(50);
            }
            if(!(xEventGroupGetBits(s.events)&WIFI_READY) || stopping()) { vTaskDelay(pdMS_TO_TICKS(500)); continue; }
        }
        s.submitted_samples=s.decoded=s.dropped=s.decode_max_ms=s.feed_gap_max_ms=0;
        s.audio_high=s.video_high=0; s.clock_us=0;
        atomic_store(&s.media_started,false);
        // Back to "dialling", but only when that is worth saying.
        //
        // Unconditionally would be wrong twice over. It would make the screen
        // claim a fresh attempt every second on a device that is failing, and --
        // because the state is reset the instant a session starts and the
        // failure is only concluded once the dial gives up -- the screen would
        // never get to show the failure at all. It would settle on whichever
        // value happened to be set when it last painted, which is "connecting",
        // for as long as the device kept retrying.
        //
        // So the state returns to "dialling" only when the address is not the one
        // just tried: the first attempt, or a change the viewer made. A retry of
        // the same address leaves the concluded failure on screen, where it
        // belongs, until something is actually different.
        {
            av_server_addr_t probe;
            char text[AV_SERVER_ADDR_MAX+8]={0};
            if (server_address(&probe)) {
                snprintf(text,sizeof(text),"%s:%u",probe.host,(unsigned)probe.port);
            }
            if (strcmp(text,last_attempt_addr)) {
                snprintf(last_attempt_addr,sizeof(last_attempt_addr),"%s",text);
                atomic_store(&s.link,(int)LINK_TRYING);
            }
        }
        s.last_packet_ms=(uint32_t)(esp_timer_get_time()/1000);
        if(!allocate_session()) {
            ESP_LOGE(TAG,"Session allocation failed free=%u largest=%u",(unsigned)esp_get_free_heap_size(),
                (unsigned)heap_caps_get_largest_free_block(MALLOC_CAP_INTERNAL));
            free_session(); vTaskDelay(pdMS_TO_TICKS(2000)); continue;
        }
        ESP_LOGI(TAG,"Allocated JPEG=49152 PCM=12800 stripes=20480; heap=%u largest=%u",
            (unsigned)esp_get_free_heap_size(),(unsigned)heap_caps_get_largest_free_block(MALLOC_CAP_INTERNAL));
        if(xTaskCreate(video_task,"av_video",4096,NULL,4,NULL)!=pdPASS) { fail("video task allocation"); xEventGroupSetBits(s.events,VIDEO_DONE); }
        if(xTaskCreate(audio_task,"av_audio",4096,NULL,6,NULL)!=pdPASS) { fail("audio task allocation"); xEventGroupSetBits(s.events,AUDIO_DONE); }
        if(xTaskCreate(receive_task,"av_rx",5120,NULL,5,NULL)!=pdPASS) { fail("receive task allocation"); xEventGroupSetBits(s.events,RX_DONE); }
        int64_t session_start=esp_timer_get_time();
        int64_t next_metrics=session_start+10000000;
        int64_t next_battery=session_start;
        int64_t last_tick=session_start;
        // The banner fires once per session, at the moment the server confirms
        // the channel; last_announced carries across sessions so reconnecting to
        // the same channel does not raise it again.
        bool announced=false;
        uint32_t last_decoded=0;
        while((xEventGroupGetBits(s.events)&(RX_DONE|AUDIO_DONE|VIDEO_DONE))!=(RX_DONE|AUDIO_DONE|VIDEO_DONE)) {
            // A channel was chosen: end this session and reconnect on the new
            // one. request_switch() has already set STOP, so the workers are on
            // their way out; session_drain() waits for them before the buffers
            // are reclaimed below.
            bool switching=pump_keys(50);
            // A setup request ends the session the same way a channel change
            // does, and for a stronger reason: the access point needs the memory
            // this session is holding.
            if(switching || atomic_load(&s_setup_request)) {
                session_drain();
                break;
            }

            // Install the channel list the receiver just parsed, here, where no
            // frame is being drawn from it. Built straight from s.list so the
            // table is not held in a third copy: on this chip the per-entry cost
            // is what decides how many channels fit at all.
            // Rebuilding the list resets its highlight and scroll, so it must not
            // happen while the viewer is looking at it: a CONFIG arrives on every
            // reconnect, and one landing mid-browse would yank the list back to
            // the top. Deferred until the overlay is closed.
            if (atomic_load(&s.list_dirty) && s.menu.view==UI_VIEW_VIDEO) {
                // The staging copy is heap, not stack: at a few hundred channels
                // it is far larger than this task's stack, and it is needed for
                // only the instant the overlay is rebuilt.
                ui_menu_entry_t *entries=heap_caps_malloc(sizeof(ui_menu_entry_t)*AV_CHANNEL_MAX,
                                                          MALLOC_CAP_8BIT);
                if (entries) {
                    atomic_store(&s.list_dirty,false);
                    unsigned n;
                    taskENTER_CRITICAL(&list_lock);
                    n=atomic_load(&s.count);
                    for (unsigned i=0;i<n;i++) {
                        memset(&entries[i],0,sizeof(entries[i]));
                        memcpy(entries[i].id,s.list[i].id,sizeof(entries[i].id));
                        memcpy(entries[i].name,s.list[i].name,sizeof(entries[i].name));
                    }
                    taskEXIT_CRITICAL(&list_lock);
                    if (n) ui_menu_set_rows(&s.menu,menu_rows());
                if (n && !ui_menu_load(&s.menu,entries,n,s.channel)) {
                        ESP_LOGW(TAG,"channel list rejected by the overlay; keeping the previous one");
                    }
                    free(entries);
                } else {
                    ESP_LOGW(TAG,"No memory to rebuild the channel overlay");
                }
            }

            // Drive the overlay clock from the session loop, which is the only
            // place that sees every tick even while the video task is busy.
            int64_t tick_now=esp_timer_get_time();
            if (ui_menu_tick(&s.menu,(tick_now-last_tick)/1000)) {
                // The banner or the page expired; nothing else needed here --
                // the next frame simply stops drawing it.
            }
            last_tick=tick_now;
            // The gauge is on the shared I2C bus; read it rarely and never in
            // the frame path.
            if(tick_now>=next_battery) {
                int soc=bsp_battery_soc();
                atomic_store(&s.battery_soc,soc);   // -1 when unreadable
                next_battery=tick_now+30000000;     // every 30 s
            }
            // Write back a setting that changed. Done here rather than in the key
            // handler because a flash write takes long enough to be felt as a
            // laggy button, and this loop is the one place with time to spare.
            // The volume is read again at the moment of writing, so a burst of
            // presses settles on the value the viewer stopped at instead of
            // writing each intermediate one.
            if (atomic_exchange(&s.settings_dirty,false)) {
                av_store_save(s.volume,av_brightness_percent(s.menu.brightness));
            }
            // Announce the channel once the server has confirmed which one it is
            // serving, not when the key was pressed: the banner then names what
            // is actually on screen, which is what the viewer wants to check.
            if(!announced && (xEventGroupGetBits(s.events)&CONFIG_READY)) {
                announced=true;
                char name[UI_MENU_NAME_MAX];
                current_channel_name(name,sizeof(name));
                if (strcmp(name,last_announced)) {
                    ui_menu_banner(&s.menu,name,last_announced);
                    strncpy(last_announced,name,sizeof(last_announced)-1);
                    last_announced[sizeof(last_announced)-1]='\0';
                }
            }
            // Read producer timestamp BEFORE now: the receiver may preempt this
            // lower-priority task; unsigned now-old subtraction otherwise wraps
            // when a freshly updated timestamp is newer than the sampled now.
            uint32_t last_packet=atomic_load(&s.last_packet_ms);
            int64_t now=esp_timer_get_time();
            // Before the first media packet the server is still filling its
            // transcode buffer, which takes seconds on a live channel. Only
            // after media starts flowing does a 5 s gap mean a stalled stream.
            uint32_t idle_limit=atomic_load(&s.media_started)
                ? 5000u : AV_FIRST_MEDIA_TIMEOUT_MS;
            if(!stopping() && !rx_done() && av_elapsed_ms((uint32_t)(now/1000),last_packet)>idle_limit) {
                ESP_LOGW(TAG,"RX_INACTIVE age_ms=%"PRIu32,av_elapsed_ms((uint32_t)(now/1000),last_packet));
                fail("receive inactivity; cooperative stop");
            }
            if(now>=next_metrics) {
                uint32_t frames=s.decoded;
                ESP_LOGI(TAG,"CLOCK_ESTIMATED interval_frames=%"PRIu32" interval_ms=%"PRIu32
                    " dropped=%"PRIu32" queue_high=%"PRIu32"/%"PRIu32" heap=%u largest=%u decode_max_ms=%"PRIu32,
                    frames-last_decoded,(uint32_t)((now-(next_metrics-10000000))/1000),
                    (uint32_t)s.dropped,(uint32_t)s.audio_high,(uint32_t)s.video_high,
                    (unsigned)esp_get_free_heap_size(),(unsigned)heap_caps_get_largest_free_block(MALLOC_CAP_INTERNAL),
                    (uint32_t)s.decode_max_ms);
                last_decoded=frames; next_metrics=now+10000000;
            }
        }
        // Workers signal only after their final resource access; no forced deletion.
        uint32_t session_ms=(esp_timer_get_time()-session_start)/1000;
        uint32_t fps_x1000=session_ms ? (uint64_t)s.decoded*1000000/session_ms : 0;
        ESP_LOGI(TAG,"CLOCK_ESTIMATED rendered_fps_x1000=%"PRIu32" session_ms=%"PRIu32
            " heap=%u largest=%u",fps_x1000,session_ms,(unsigned)esp_get_free_heap_size(),
            (unsigned)heap_caps_get_largest_free_block(MALLOC_CAP_INTERNAL));
        ESP_LOGI(TAG,"ESTIMATED clock: submitted_samples=%"PRIu32" DMA_completed=unmeasured acoustic=unmeasured decoded=%"PRIu32
            " dropped=%"PRIu32" decode_max_ms=%"PRIu32" feed_gap_max_ms=%"PRIu32" queue_high=%"PRIu32"/%"PRIu32" min_heap=%u",
            s.submitted_samples,s.decoded,s.dropped,s.decode_max_ms,s.feed_gap_max_ms,s.audio_high,s.video_high,
            (unsigned)esp_get_minimum_free_heap_size());
        // Safe only because the workers were waited for above: they signal DONE
        // after their last touch of a queue or a DMA buffer, and this destroys
        // both.
        free_session();

        // Answered here rather than inside the session loop because this is the
        // point where the session's memory is free again: the access point, its
        // web server and its DNS responder are built out of exactly that memory,
        // and starting them while it was still held is what would make them fail.
        if(atomic_exchange(&s_setup_request,false)) {
            // The channel table belongs to a session. Setup does not use it, and
            // a stale one would be drawn on the status page after setup.
            ESP_LOGW(TAG,"Entering setup mode on request; heap=%u largest=%u",
                     (unsigned)esp_get_free_heap_size(),
                     (unsigned)heap_caps_get_largest_free_block(MALLOC_CAP_INTERNAL));
            if(run_setup_mode()) {
                // The viewer cancelled. The stored network is unchanged, so this
                // rejoins whatever was already working.
                ESP_LOGI(TAG,"Left setup; resuming playback");
                has_network=true;
                continue;
            }
            // Setup timed out, or could not take the panel: restart so the radio
            // comes back up in station mode. Reached only when something is
            // wrong, since a completed setup restarts from inside run_setup_mode.
            ESP_LOGW(TAG,"Restarting after an unfinished setup");
            vTaskDelay(pdMS_TO_TICKS(200));
            esp_restart();
        }

        // A session that never produced a picture means the source is broken, not
        // that the network hiccuped: live sources fail fast with a 403 or a dead
        // host. Step to the next channel after a couple of tries so a dead entry
        // in the list cannot trap the viewer on the test pattern.
        //
        // Not when the server was never reached, though. That is not a fault of
        // the channel: every channel fails identically, so stepping would cycle
        // the whole table for nothing while the viewer watches the name change
        // and never sees a picture. Staying put keeps the waiting screen up --
        // which is the screen the status page is drawn over, and that page is
        // the only way from here to the setup screen where the address is fixed.
        //
        // Read fresh rather than from the copy taken at start-up, so an address
        // stored on the setup page is picked up on the next pass without a
        // restart.
        server_configured=server_address(&target);
        report_server_address(&target,server_configured);
        // Only a session that got through to the server and then saw nothing
        // counts as a bad channel. A session that never reached the server says
        // nothing about the channel at all.
        const bool server_answered = atomic_load(&s.link)==(int)LINK_UP;
        unsigned limit = atomic_exchange(&s.chosen_by_user,false)
            ? DEAD_CHANNEL_TRIES_USER : DEAD_CHANNEL_TRIES;
        if (atomic_load(&s.media_started) || !server_answered) {
            s.dead_streak=0;
        } else if (s.count>1 && ++s.dead_streak>=limit) {
            s.dead_streak=0;
            unsigned count=atomic_load(&s.count);
            unsigned index=(atomic_load(&s.index)+1)%count;
            char next[AV_CHANNEL_ID_MAX]={0};
            bool ok=false;
            taskENTER_CRITICAL(&list_lock);
            const char *id=s.list[index].id;
            size_t length=id ? strnlen(id,AV_CHANNEL_ID_MAX) : 0;
            if (length>0 && length<AV_CHANNEL_ID_MAX) {
                memcpy(next,id,length+1);
                atomic_store(&s.index,index);
                ok=true;
            }
            taskEXIT_CRITICAL(&list_lock);
            if (ok) {
                ESP_LOGW(TAG,"No picture on %s; trying %s",s.channel,next);
                request_switch(next);
                atomic_store(&s.chosen_by_user,false);   // this one is automatic
            }
        }
        vTaskDelay(pdMS_TO_TICKS(1000)); // Bounded reconnect, fresh session and PTS.
    }
}
