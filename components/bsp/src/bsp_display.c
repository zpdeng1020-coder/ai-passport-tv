// components/bsp/src/bsp_display.c
// 移植自 trae_card/components/platform/platform_esp32/src/disp_st7789.c
#include "bsp_display.h"
#include "bsp_pins.h"
#include "driver/spi_master.h"
#include "driver/ledc.h"
#include "esp_lcd_panel_io.h"
#include "esp_lcd_panel_ops.h"
#include "esp_lcd_panel_vendor.h"
#include "esp_log.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "freertos/semphr.h"

static SemaphoreHandle_t s_raw_done;
// `s_raw_outstanding` counts transfers queued but not yet drained. It is a
// count rather than a flag because the controller's queue holds ten and the
// measurement build uses more than one; the product's submit()/wait() pair
// keeps it at zero or one at all times, so the two behave identically there.
static bool s_raw, s_lvgl_owned;
static unsigned s_raw_outstanding;
static bool raw_done(esp_lcd_panel_io_handle_t io, esp_lcd_panel_io_event_data_t *event, void *ctx) {
    (void)io; (void)event;
    BaseType_t wake = pdFALSE;
    xSemaphoreGiveFromISR((SemaphoreHandle_t)ctx, &wake);
    return wake == pdTRUE;
}

static const char *TAG = "bsp_disp";

static esp_lcd_panel_handle_t    s_panel;
static esp_lcd_panel_io_handle_t s_io;
static bool                      s_bl_ready;

// ---------------------------------------------------------------------------
// ST7789P3 厂商专属初始化序列(porch / power / gamma)。
// 这些是【面板厂给的参考例程 TFT_init() 里的值】,不是 ST7789 通用默认值 ——
// 换面板必须找对应厂商要新的一份,照抄这份大概率显示异常。
//
// 以下四条由 esp_lcd 内置驱动完成,故此处不重复:
//   0x11 SLPOUT / 0x3A COLMOD → esp_lcd_panel_init()
//   0x21 INVON                → esp_lcd_panel_invert_color()
//   0x29 DISPON               → esp_lcd_panel_disp_on_off()
//   0x36 MADCTL               → esp_lcd_panel_mirror()(⚠ 别再手动写 0x36,会被它覆盖)
// ---------------------------------------------------------------------------
typedef struct {
    uint8_t  cmd;
    uint8_t  data[16];
    uint8_t  len;
    uint16_t delay_ms;
} st_init_cmd_t;

static const st_init_cmd_t ST7789P3_CMDS[] = {
    {0xB2, {0x05, 0x05, 0x00, 0x33, 0x33}, 5, 0},   // PORCTRL 帧率 porch
    {0xB7, {0x35}, 1, 0},                            // GCTRL 栅极
    {0xBB, {0x21}, 1, 0},                            // VCOMS
    {0xC0, {0x2C}, 1, 0},                            // LCMCTRL
    {0xC2, {0x01}, 1, 0},                            // VDVVRHEN
    {0xC3, {0x0B}, 1, 0},                            // VRHS
    {0xC4, {0x20}, 1, 0},                            // VDVSET
    {0xC6, {0x0F}, 1, 0},                            // FRCTRL2 60Hz 点反转
    {0xD0, {0xA7, 0xA1}, 2, 0},                      // PWCTRL1
    {0xD0, {0xA4, 0xA1}, 2, 0},                      // PWCTRL1(参考例程重发,覆盖上一条)
    {0xD6, {0xA1}, 1, 0},
    {0xE0, {0xD0, 0x04, 0x08, 0x0A, 0x09, 0x05, 0x2D, 0x43,
            0x49, 0x09, 0x16, 0x15, 0x26, 0x2B}, 14, 0},   // PVGAMCTRL 正伽马
    {0xE1, {0xD0, 0x03, 0x09, 0x0A, 0x0A, 0x06, 0x2E, 0x44,
            0x40, 0x3A, 0x15, 0x15, 0x26, 0x2A}, 14, 10},  // NVGAMCTRL 负伽马
};

static void backlight_init(void) {
    if (BSP_LCD_BL < 0) { ESP_LOGW(TAG, "背光引脚未接 MCU,亮度不可调"); return; }
    ledc_timer_config_t t = {
        .speed_mode      = BSP_BL_LEDC_MODE,
        .timer_num       = BSP_BL_LEDC_TIMER,
        .duty_resolution = BSP_BL_LEDC_RES,
        .freq_hz         = BSP_BL_LEDC_FREQ_HZ,
        .clk_cfg         = LEDC_AUTO_CLK,
    };
    esp_err_t e = ledc_timer_config(&t);
    if (e != ESP_OK) { ESP_LOGE(TAG, "ledc_timer_config 失败: %s", esp_err_to_name(e)); return; }

    ledc_channel_config_t ch = {
        .gpio_num   = BSP_LCD_BL,
        .speed_mode = BSP_BL_LEDC_MODE,
        .channel    = BSP_BL_LEDC_CHANNEL,
        .timer_sel  = BSP_BL_LEDC_TIMER,
        .duty       = 0,
        .hpoint     = 0,
    };
    e = ledc_channel_config(&ch);
    if (e != ESP_OK) { ESP_LOGE(TAG, "ledc_channel_config 失败: %s", esp_err_to_name(e)); return; }

    s_bl_ready = true;
    ESP_LOGI(TAG, "背光 LEDC 就绪 gpio=%d", BSP_LCD_BL);
}

esp_err_t bsp_display_init(void) {
    if (s_panel) return ESP_OK;

    spi_bus_config_t bus = {
        .mosi_io_num = BSP_LCD_MOSI,
        .sclk_io_num = BSP_LCD_SCLK,
        .miso_io_num = -1, .quadwp_io_num = -1, .quadhd_io_num = -1,
        .max_transfer_sz = BSP_LCD_W * 80 * 2,
    };
    esp_err_t e = spi_bus_initialize(BSP_LCD_SPI_HOST, &bus, SPI_DMA_CH_AUTO);
    if (e != ESP_OK) {
        ESP_LOGE(TAG, "SPI 总线初始化失败 (%s) —— 检查 MOSI=GPIO%d / SCLK=GPIO%d 是否冲突",
                 esp_err_to_name(e), BSP_LCD_MOSI, BSP_LCD_SCLK);
        return e;
    }

    esp_lcd_panel_io_spi_config_t io_cfg = {
        .cs_gpio_num = BSP_LCD_CS,
        .dc_gpio_num = BSP_LCD_DC,
        .pclk_hz = BSP_LCD_PCLK_HZ,
        .spi_mode = BSP_LCD_SPI_MODE,
        .lcd_cmd_bits = 8, .lcd_param_bits = 8,
        .trans_queue_depth = 10,
    };
    e = esp_lcd_new_panel_io_spi((esp_lcd_spi_bus_handle_t)BSP_LCD_SPI_HOST, &io_cfg, &s_io);
    if (e != ESP_OK) { ESP_LOGE(TAG, "panel_io 创建失败: %s", esp_err_to_name(e)); return e; }

    esp_lcd_panel_dev_config_t dev = {
        .reset_gpio_num = BSP_LCD_RST,          // -1 → SWRESET 软复位
        .rgb_ele_order  = LCD_RGB_ELEMENT_ORDER_RGB,
        .bits_per_pixel = 16,
    };
    e = esp_lcd_new_panel_st7789(s_io, &dev, &s_panel);
    if (e != ESP_OK) { ESP_LOGE(TAG, "面板创建失败: %s", esp_err_to_name(e)); return e; }

    esp_lcd_panel_reset(s_panel);   // rst=-1 时走 SWRESET
    esp_lcd_panel_init(s_panel);    // SLPOUT / COLMOD / RAMCTRL

    for (size_t i = 0; i < sizeof(ST7789P3_CMDS) / sizeof(ST7789P3_CMDS[0]); i++) {
        const st_init_cmd_t *c = &ST7789P3_CMDS[i];
        esp_err_t r = esp_lcd_panel_io_tx_param(s_io, c->cmd, c->data, c->len);
        if (r != ESP_OK) ESP_LOGE(TAG, "厂商初始化命令 0x%02X 失败: %s", c->cmd, esp_err_to_name(r));
        if (c->delay_ms) vTaskDelay(pdMS_TO_TICKS(c->delay_ms));
    }

    esp_lcd_panel_invert_color(s_panel, BSP_LCD_INVERT_COLOR);   // 0x21 / 0x20
    esp_lcd_panel_mirror(s_panel, false, false);                 // 0x36 MADCTL:本板不需镜像(XY 双镜像 = 画面 180°)
    esp_lcd_panel_set_gap(s_panel, 0, 0);
    esp_lcd_panel_disp_on_off(s_panel, true);                    // 0x29 DISPON

    backlight_init();
    ESP_LOGI(TAG, "显示就绪 %dx%d", BSP_LCD_W, BSP_LCD_H);
    return ESP_OK;
}

bool bsp_display_raw_active(void) { return s_raw; }
bool bsp_display_lvgl_claim(void) {
    if (s_raw || s_lvgl_owned) return false;
    s_lvgl_owned = true;
    return true;
}
void bsp_display_lvgl_unclaim(void) { s_lvgl_owned = false; }
esp_err_t bsp_display_raw_claim(void) {
    if (!s_panel || s_raw || s_lvgl_owned) return ESP_ERR_INVALID_STATE;
    // Counting, not binary, so that several transfers can be outstanding at
    // once. It behaves exactly as a binary semaphore does while the caller
    // keeps to one at a time, which is what submit()/wait() do; the depth only
    // matters to submit_nowait()/drain(), which exist to measure whether the
    // panel can be fed without waiting for each stripe in turn.
    //
    // The depth is well above a frame's stripe count: a give on a full
    // counting semaphore is dropped, and a dropped give is a drain that never
    // returns.
    s_raw_done = xSemaphoreCreateCounting(32, 0);
    if (!s_raw_done) return ESP_ERR_NO_MEM;
    esp_lcd_panel_io_callbacks_t cb = { .on_color_trans_done = raw_done };
    esp_err_t e = esp_lcd_panel_io_register_event_callbacks(s_io, &cb, s_raw_done);
    if (e == ESP_OK) e = esp_lcd_panel_swap_xy(s_panel, true);
    if (e == ESP_OK) e = esp_lcd_panel_mirror(s_panel, true, false);
    if (e != ESP_OK) {
        cb.on_color_trans_done = NULL;
        esp_lcd_panel_io_register_event_callbacks(s_io, &cb, NULL);
        vSemaphoreDelete(s_raw_done); s_raw_done = NULL;
        return e;
    }
    s_raw = true;
    return ESP_OK;
}
esp_err_t bsp_display_raw_wait(uint32_t timeout_ms) {
    if (!s_raw) return ESP_ERR_INVALID_STATE;
    if (!s_raw_outstanding) return ESP_OK;
    if (!xSemaphoreTake(s_raw_done, pdMS_TO_TICKS(timeout_ms))) return ESP_ERR_TIMEOUT;
    s_raw_outstanding--;
    return ESP_OK;
}
esp_err_t bsp_display_raw_submit(int y, int rows, const void *pixels, uint32_t timeout_ms) {
    if (!s_raw) return ESP_ERR_INVALID_STATE;
    if (!pixels || y < 0 || rows <= 0 || y + rows > BSP_LCD_W) return ESP_ERR_INVALID_ARG;
    esp_err_t e = bsp_display_raw_wait(timeout_ms);
    if (e != ESP_OK) return e;
    e = esp_lcd_panel_draw_bitmap(s_panel, 0, y, BSP_LCD_H, y + rows, pixels);
    if (e == ESP_OK) s_raw_outstanding++;
    return e;
}
esp_err_t bsp_display_raw_submit_nowait(int y, int rows, const void *pixels) {
    if (!s_raw) return ESP_ERR_INVALID_STATE;
    if (!pixels || y < 0 || rows <= 0 || y + rows > BSP_LCD_W) return ESP_ERR_INVALID_ARG;
    // No wait here, and that is the entire difference from submit(). The
    // controller's own queue holds ten, so this returns once the transfer is
    // queued rather than once the bus is free.
    esp_err_t e = esp_lcd_panel_draw_bitmap(s_panel, 0, y, BSP_LCD_H, y + rows, pixels);
    if (e == ESP_OK) s_raw_outstanding++;
    return e;
}
esp_err_t bsp_display_raw_drain(unsigned transfers, uint32_t timeout_ms) {
    if (!s_raw) return ESP_ERR_INVALID_STATE;
    for (unsigned i = 0; i < transfers; i++) {
        if (!s_raw_outstanding) break;
        if (!xSemaphoreTake(s_raw_done, pdMS_TO_TICKS(timeout_ms))) return ESP_ERR_TIMEOUT;
        s_raw_outstanding--;
    }
    return ESP_OK;
}

esp_err_t bsp_display_raw_release(void) {
    if (!s_raw || s_raw_outstanding) return ESP_ERR_INVALID_STATE;
    esp_lcd_panel_io_callbacks_t cb = {0};
    esp_err_t e = esp_lcd_panel_io_register_event_callbacks(s_io, &cb, NULL);
    if (e != ESP_OK) return e;
    esp_lcd_panel_swap_xy(s_panel, false);
    esp_lcd_panel_mirror(s_panel, false, false);
    vSemaphoreDelete(s_raw_done); s_raw_done = NULL; s_raw = false;
    s_raw_outstanding = 0;
    return ESP_OK;
}

esp_lcd_panel_handle_t bsp_display_panel(void) { return s_panel; }

esp_lcd_panel_io_handle_t bsp_display_io(void) { return s_io; }

void bsp_display_backlight(uint8_t percent) {
    if (!s_bl_ready) return;
    if (percent > 100) percent = 100;
    uint32_t max_duty = (1u << BSP_BL_LEDC_RES) - 1u;
    uint32_t duty = (max_duty * percent) / 100u;
    ledc_set_duty(BSP_BL_LEDC_MODE, BSP_BL_LEDC_CHANNEL, duty);
    ledc_update_duty(BSP_BL_LEDC_MODE, BSP_BL_LEDC_CHANNEL);
}
