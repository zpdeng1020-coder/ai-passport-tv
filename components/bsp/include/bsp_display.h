// components/bsp/include/bsp_display.h
// ST7789P3 240x320 显示:SPI 面板初始化 + 厂商专属寄存器 + LEDC 背光调光。
#pragma once

#include "esp_err.h"
#include "esp_lcd_types.h"
#include <stdbool.h>
#include <stdint.h>

// 初始化 SPI 总线、面板、厂商寄存器、背光 LEDC。成功后屏幕已上电但内容未定。
esp_err_t bsp_display_init(void);

// 取底层面板句柄。想直接 esp_lcd_panel_draw_bitmap 画,或接 LVGL 以外的 GUI 时用。
// 未初始化返回 NULL。
esp_lcd_panel_handle_t bsp_display_panel(void);

// 取底层 panel io 句柄(LVGL 接入需要)。未初始化返回 NULL。
esp_lcd_panel_io_handle_t bsp_display_io(void);

// Exclusive raw landscape owner. Call from one task, before LVGL initialization.
// claim() installs the only DMA callback; LVGL init refuses while raw is claimed.
// submit() waits for any previous transfer before submitting; the caller must
// retain pixels until wait() succeeds. A timeout never grants buffer ownership.
// release() requires all DMA completed, restores portrait and removes callback.
esp_err_t bsp_display_raw_claim(void);
esp_err_t bsp_display_raw_submit(int y, int rows, const void *rgb565_be, uint32_t timeout_ms);
esp_err_t bsp_display_raw_wait(uint32_t timeout_ms);
esp_err_t bsp_display_raw_release(void);
bool bsp_display_raw_active(void);

// Start a transfer without waiting for the one before it, and wait for several
// at once. Only the measurement build uses these; submit()/wait() above are the
// product's pair and are unchanged.
//
// They exist because submit() waits for any outstanding transfer first, which
// makes the panel strictly serial with whatever the caller does between
// stripes -- and the bus is not the reason. `trans_queue_depth` is 10, so the
// controller will hold ten transfers; the serialisation is this driver
// tracking exactly one (`s_raw_pending`, one binary semaphore). Until that is
// measured, "a second stripe buffer did not help" cannot be told apart from
// "a second stripe buffer could not help here", and the two want opposite
// decisions.
//
// The caller owns the ordering and must not overwrite a buffer until drain()
// has accounted for the transfer that read it.
esp_err_t bsp_display_raw_submit_nowait(int y, int rows, const void *rgb565_be);
esp_err_t bsp_display_raw_drain(unsigned transfers, uint32_t timeout_ms);
// Internal LVGL ownership guard (initialization only; not concurrent calls).
bool bsp_display_lvgl_claim(void);
void bsp_display_lvgl_unclaim(void);

// 背光亮度 0..100(%)。LEDC PWM,0=全灭。
void bsp_display_backlight(uint8_t percent);

// ---------------------------------------------------------------------------
// LVGL 接入(可选层)。必须先 bsp_display_init() 成功后再调。
// 不想用 LVGL 的开发者可忽略本段,直接用 bsp_display_panel() 自己画。
// ---------------------------------------------------------------------------
// 前向声明:LVGL 里 lv_display_t 是 `typedef struct _lv_display_t lv_display_t;`,
// 故此处用 struct 形式即可,避免本头文件强行 include lvgl.h。
struct _lv_display_t;

// 启动 LVGL 与其渲染任务,返回 lv_display_t*。失败返回 NULL。
struct _lv_display_t *bsp_lvgl_init(void);

// LVGL 非线程安全:在【非 LVGL 任务】里操作任何 lv_* 对象前后必须加解锁。
bool bsp_lvgl_lock(int timeout_ms);
void bsp_lvgl_unlock(void);
