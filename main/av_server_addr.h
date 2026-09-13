// Where the streaming server is, as configured rather than compiled in.
//
// The address used to be a build-time constant. That works for the one device
// that was built with it and breaks the moment anything moves: a home router
// hands out a different lease, or a second person wants to point their device at
// their own server, and the only cure is a rebuild and a reflash.
//
// So the address is stored instead, entered on the device's setup page. This
// module turns what was stored into something connectable, and is kept apart
// from the network code so the parsing can be tested without a device.
#pragma once
#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

// Longest stored address accepted, including the terminating byte. Long enough
// for an IPv4 literal with a port, and for a hostname.
#define AV_SERVER_ADDR_MAX 64u

// The port used when the stored address does not name one. Matches the server's
// own default so the common case needs no port typed at all.
#define AV_SERVER_PORT_DEFAULT 8096u

typedef struct {
    // The host exactly as it was stored: an IPv4 literal or a name to resolve.
    char host[AV_SERVER_ADDR_MAX];
    uint16_t port;
} av_server_addr_t;

// Parse "host", "host:port" or "host/anything".
//
// Accepts both an IPv4 literal and a hostname; deciding which it is happens when
// the address is used, not here, so this stays free of the network stack.
// Surrounding whitespace is ignored, because the value arrives from a text field
// someone typed into. A path or query is accepted and dropped: people paste a
// whole URL, and the host and port are what matter.
//
// Returns false when there is nothing usable, and leaves *out untouched, so a
// caller can keep a previous good value rather than losing it to a bad edit.
bool av_server_addr_parse(const char *text, av_server_addr_t *out);

// Whether the stored value names something that can be dialled. An empty string,
// whitespace, or a placeholder such as "0.0.0.0" is not usable, and the caller
// shows the setup page instead of trying to connect.
bool av_server_addr_usable(const av_server_addr_t *addr);
