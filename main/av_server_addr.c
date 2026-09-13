#include "av_server_addr.h"

#include <string.h>

// Whitespace at either end is dropped before anything else: the value comes from
// a text field, and a trailing space after a pasted address is a mistake the
// reader should not have to notice.
static const char *skip_space(const char *p)
{
    while (*p == ' ' || *p == '\t' || *p == '\r' || *p == '\n') {
        p++;
    }
    return p;
}

bool av_server_addr_parse(const char *text, av_server_addr_t *out)
{
    if (!text || !out) {
        return false;
    }

    const char *begin = skip_space(text);
    const char *end = begin + strlen(begin);
    while (end > begin && (end[-1] == ' ' || end[-1] == '\t' ||
                           end[-1] == '\r' || end[-1] == '\n')) {
        end--;
    }
    if (end == begin) {
        return false;   // blank field
    }

    // The scheme is stripped BEFORE anything is cut at a slash.
    //
    // The other order looks equivalent and is not: "http://host:8096/live" would
    // first be cut at the first '/', leaving "http:", and the address would be
    // lost before the scheme was ever recognised. That is the shape a person
    // pastes most often, so the order here is the part that matters.
    const char *host_begin = begin;
    for (const char *p = begin; p + 2 < end; p++) {
        if (p[0] == ':' && p[1] == '/' && p[2] == '/') {
            host_begin = p + 3;
            break;
        }
    }

    // From here on, a '/' or '?' ends the address: what follows is a path that
    // has nothing to do with where to connect.
    const char *stop = host_begin;
    while (stop < end && *stop != '/' && *stop != '?') {
        stop++;
    }
    size_t length = (size_t)(stop - host_begin);
    if (length == 0) {
        return false;
    }

    // A port, when present, is separated by the last ':' of what is left. Only
    // the final one introduces the port; an earlier colon would be part of a
    // scheme, which has already been removed.
    const char *colon = NULL;
    for (const char *p = host_begin; p < host_begin + length; p++) {
        if (*p == ':') {
            colon = p;
        }
    }

    uint16_t port = AV_SERVER_PORT_DEFAULT;
    size_t host_length = length;
    if (colon) {
        host_length = (size_t)(colon - host_begin);
        const char *digits = colon + 1;
        size_t count = (size_t)((host_begin + length) - digits);
        // A colon with nothing usable after it is not a port. Treated as an
        // error rather than ignored, because "host:" is far more likely to be a
        // typo than an address whose port was meant to be omitted.
        if (count == 0 || count > 5) {
            return false;
        }
        unsigned value = 0;
        for (size_t i = 0; i < count; i++) {
            char c = digits[i];
            if (c < '0' || c > '9') {
                return false;
            }
            value = value * 10u + (unsigned)(c - '0');
        }
        // Zero is not a port anything listens on; above 65535 does not fit.
        if (value == 0 || value > 65535u) {
            return false;
        }
        port = (uint16_t)value;
    }

    if (host_length == 0 || host_length >= AV_SERVER_ADDR_MAX) {
        return false;   // nothing left, or too long to store
    }

    av_server_addr_t parsed;
    memcpy(parsed.host, host_begin, host_length);
    parsed.host[host_length] = '\0';
    parsed.port = port;

    // Written only now, so a rejected value leaves the caller's copy alone.
    *out = parsed;
    return true;
}

bool av_server_addr_usable(const av_server_addr_t *addr)
{
    if (!addr || addr->host[0] == '\0') {
        return false;
    }
    // The build-time placeholder means "not configured". Anything else is taken
    // at face value: whether it resolves is the network stack's business, and
    // refusing a name here would defeat the point of accepting one.
    return strcmp(addr->host, "0.0.0.0") != 0;
}
