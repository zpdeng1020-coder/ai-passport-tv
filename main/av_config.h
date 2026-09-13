#pragma once
// Never put real credentials here. The ignored private header is local only.
#if __has_include("av_private_config.h") && !defined(AV_PUBLIC_BUILD)
#include "av_private_config.h"
#define AV_CONFIG_PRESENT 1
#else
#define AV_CONFIG_PRESENT 0
#define AV_WIFI_SSID ""
#define AV_WIFI_PASSWORD ""
#define AV_SERVER_IPV4 "0.0.0.0"
#define AV_SERVER_PORT 8096
#define AV_PAIRING_TOKEN ""
#endif
