// Server address parsing, tested without a device.
//
// This runs on text someone typed or pasted into a web form, so the cases that
// matter are the messy real ones: a trailing space, a whole URL pasted in, a
// colon with nothing after it. Each has a wrong answer that reads as "the device
// silently will not connect", which is the hardest kind of fault to diagnose
// from the outside.
#include "av_server_addr.h"

#include <stdio.h>
#include <string.h>

static int failures;

static void check(int condition, const char *what)
{
    if (!condition) {
        printf("FAIL: %s\n", what);
        failures++;
    }
}

static void test_plain_address_gets_the_default_port(void)
{
    av_server_addr_t a;
    check(av_server_addr_parse("192.168.1.20", &a), "a bare address is accepted");
    check(strcmp(a.host, "192.168.1.20") == 0, "the host is kept");
    check(a.port == AV_SERVER_PORT_DEFAULT, "the default port is filled in");
}

static void test_explicit_port_is_used(void)
{
    av_server_addr_t a;
    check(av_server_addr_parse("192.168.1.20:9000", &a), "an explicit port is accepted");
    check(strcmp(a.host, "192.168.1.20") == 0, "the host excludes the port");
    check(a.port == 9000, "the port is taken from the text");
}

static void test_a_hostname_is_accepted(void)
{
    // Accepting a name is the point of the exercise: an address that has to be a
    // literal is an address that breaks when the network renumbers it.
    av_server_addr_t a;
    check(av_server_addr_parse("myserver.example.org", &a), "a hostname is accepted");
    check(strcmp(a.host, "myserver.example.org") == 0, "the name is kept whole");
    check(a.port == AV_SERVER_PORT_DEFAULT, "a hostname gets the default port");

    check(av_server_addr_parse("myserver.example.org:7000", &a), "hostname with a port");
    check(a.port == 7000, "the hostname's port is taken");
}

static void test_a_pasted_url_keeps_only_the_address(void)
{
    // People paste what they have. The host and port are the useful part.
    av_server_addr_t a;
    check(av_server_addr_parse("http://192.168.1.20:8096/live", &a),
          "a full URL is accepted");
    // Both the host and the port have to be checked. Checking the port alone
    // lets the host silently become "http:" and the case still passes, which is
    // exactly the failure this parser had before the scheme was stripped first.
    check(strcmp(a.host, "192.168.1.20") == 0, "the scheme is stripped");
    check(a.port == 8096, "the port survives a pasted URL");

    // A scheme with no port: the default has to come back, not a stray colon.
    check(av_server_addr_parse("http://192.168.1.20/live", &a), "a URL without a port");
    check(strcmp(a.host, "192.168.1.20") == 0, "the host survives a portless URL");
    check(a.port == AV_SERVER_PORT_DEFAULT, "a portless URL falls back to the default");

    check(av_server_addr_parse("192.168.1.20/watch", &a), "a path is dropped");
    check(strcmp(a.host, "192.168.1.20") == 0, "the host is what remains");
    check(a.port == AV_SERVER_PORT_DEFAULT, "dropping a path keeps the default port");
}

static void test_surrounding_whitespace_is_ignored(void)
{
    av_server_addr_t a;
    check(av_server_addr_parse("  192.168.1.20  ", &a), "padded text is accepted");
    check(strcmp(a.host, "192.168.1.20") == 0, "the padding is gone");
    check(av_server_addr_parse("\t192.168.1.20:8080\n", &a), "tabs and newlines too");
    check(strcmp(a.host, "192.168.1.20") == 0 && a.port == 8080,
          "tabs and newlines are stripped with the port intact");
}

static void test_blank_input_is_rejected(void)
{
    av_server_addr_t a;
    check(!av_server_addr_parse("", &a), "an empty string is rejected");
    check(!av_server_addr_parse("   ", &a), "whitespace alone is rejected");
    check(!av_server_addr_parse(NULL, &a), "a null pointer is rejected");
    check(!av_server_addr_parse("192.168.1.20", NULL), "a null output is rejected");
}

static void test_bad_ports_are_rejected(void)
{
    av_server_addr_t a;
    check(!av_server_addr_parse("192.168.1.20:", &a), "a colon with no port is rejected");
    check(!av_server_addr_parse("192.168.1.20:abc", &a), "a non-numeric port is rejected");
    check(!av_server_addr_parse("192.168.1.20:0", &a), "port zero is rejected");
    check(!av_server_addr_parse("192.168.1.20:65536", &a), "a port above 65535 is rejected");
    check(!av_server_addr_parse("192.168.1.20:999999", &a), "an absurd port is rejected");
    // 65535 is the largest port that fits; it must not be rejected with the rest.
    check(av_server_addr_parse("192.168.1.20:65535", &a), "port 65535 is accepted");
    check(a.port == 65535, "port 65535 is stored");
    check(av_server_addr_parse("192.168.1.20:1", &a), "port 1 is accepted");
}

static void test_a_rejected_value_leaves_the_output_untouched(void)
{
    // Callers keep their last good address across a bad edit. That only works if
    // a failed parse does not write to the output.
    av_server_addr_t a;
    check(av_server_addr_parse("192.168.1.20:8096", &a), "a good value first");
    check(!av_server_addr_parse("192.168.1.20:", &a), "then a bad one");
    check(strcmp(a.host, "192.168.1.20") == 0 && a.port == 8096,
          "the good value survived the bad edit");
}

static void test_an_overlong_address_is_rejected_not_truncated(void)
{
    // Truncating would produce a different host that might belong to someone
    // else. Refusing is the safe answer.
    char long_text[AV_SERVER_ADDR_MAX + 32];
    memset(long_text, 'a', sizeof(long_text) - 1);
    long_text[sizeof(long_text) - 1] = '\0';
    av_server_addr_t a;
    check(!av_server_addr_parse(long_text, &a), "an overlong host is rejected");

    // One character shorter than the limit is fine: the bound must not be off by
    // one in the direction that refuses a legal value.
    char fits[AV_SERVER_ADDR_MAX];
    memset(fits, 'a', AV_SERVER_ADDR_MAX - 1);
    fits[AV_SERVER_ADDR_MAX - 1] = '\0';
    check(av_server_addr_parse(fits, &a), "a host that exactly fits is accepted");
}

static void test_usability(void)
{
    av_server_addr_t a;
    check(av_server_addr_parse("192.168.1.20", &a), "parse a real address");
    check(av_server_addr_usable(&a), "a real address is usable");
    a.host[0] = '\0';
    check(!av_server_addr_usable(&a), "an empty host is not usable");
    check(!av_server_addr_usable(NULL), "a null address is not usable");
    check(av_server_addr_parse("0.0.0.0", &a), "the placeholder parses");
    check(!av_server_addr_usable(&a), "the placeholder is not usable");
}

int main(void)
{
    test_plain_address_gets_the_default_port();
    test_explicit_port_is_used();
    test_a_hostname_is_accepted();
    test_a_pasted_url_keeps_only_the_address();
    test_surrounding_whitespace_is_ignored();
    test_blank_input_is_rejected();
    test_bad_ports_are_rejected();
    test_a_rejected_value_leaves_the_output_untouched();
    test_an_overlong_address_is_rejected_not_truncated();
    test_usability();
    if (failures) {
        printf("test_av_server_addr: %d failure(s)\n", failures);
        return 1;
    }
    printf("test_av_server_addr: all cases passed\n");
    return 0;
}
