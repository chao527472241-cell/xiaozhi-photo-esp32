# -*- coding: utf-8 -*-
"""
小智 v2.6.0 · 触摸界面补丁（磁贴桌面 / 大号时钟 / 状态 / 设置）
==================================================================
只往 lcd_display.cc 注入两段：
  A. 文件作用域：include + 静态对象 + 回调 + 绘制函数
  B. SetupUI 结尾：调用 pc_build_ui(screen) 建界面

设计要点（都是踩坑换来的）：
  * 图标、数字、壁纸全部是编译进固件的**位图**（pc_assets.h），
    完全不使用自定义字体 —— 之前崩溃就是字体指针/占位字体导致的。
  * 文字标签不单独设字体，继承 screen 字体，主题刷新也不会失效。
  * 只有待机才显示面板；说话/聆听/回复时自动隐藏，不挡对话。

用法：在 xiaozhi/ 目录执行  python patch_screen_panel.py
依赖：脚本同目录要有 pc_assets.h（由 make_assets.py 生成）
"""

import os
import sys

LCD = "main/display/lcd_display.cc"
BOARD_CC = "main/boards/lckfb/szpi-esp32s3/lichuang_dev_board.cc"
ASSETS_CANDIDATES = ["main/display/pc_assets.h", "pc_assets.h"]
CONFIG = "screen_config.txt"
MARK = "PC_SCREEN_PANEL_PATCH"
IMU_MARK = "QMI8658_IMU_PATCH"   # 板载六轴采集任务（注入板级 .cc）


def load_config():
    cfg = {
        "slogan": "嗨，我是小智",
        "theme": "dark",
        "boot": "home",
        "show_ip": "1",
        "show_signal": "1",
        "show_heap": "1",
        "show_uptime": "1",
        "show_state": "1",
        "wallpaper": "1",
        "imu": "1",
        "dim": "30",
    }
    if not os.path.exists(CONFIG):
        print("[提示] 没找到 %s，使用默认设置" % CONFIG)
        return cfg
    with open(CONFIG, encoding="utf-8-sig") as f:
        for raw in f:
            line = raw.strip()
            if not line or line.startswith("#") or line.startswith("//"):
                continue
            if "=" not in line:
                continue
            k, v = line.split("=", 1)
            k, v = k.strip().lower(), v.strip()
            if k:
                cfg[k] = v
    return cfg


def cstr(s):
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


def flag(v):
    v = (v or "1").strip().lower()
    return "true" if v in ("1", "on", "true", "yes", "y", "开", "是") else "false"


# ======================================================================
# 板载六轴（QMI8658）采集任务 —— 注入到板级文件 lichuang_dev_board.cc
# ======================================================================
# 为什么不在界面代码里直接开 I2C：
#   I2C 口（SDA=GPIO1 / SCL=GPIO2）已经被板级对象用 i2c_master 占用了（音频/触摸/
#   IO 扩展/摄像头都在上面）。同一端口重复 i2c_new_master_bus 会失败，所以采集放
#   在板级文件里复用现成句柄 i2c_bus_，结果用全局变量抛给界面层。
IMU_CODE = r'''
// === QMI8658_IMU_PATCH ===
// 板载六轴 QMI8658：与音频/触摸/IO 扩展共用同一条 I2C（SDA=GPIO1, SCL=GPIO2，
// 地址 0x6A 或 0x6B）。采集放这里，界面层只读全局变量：
//   g_qmi_acc       最近一次加速度（g）
//   g_qmi_ok        1 = 传感器正常
//   g_qmi_pickup    拿起次数（先安静 1 秒，再倾斜 18° 以上才算）
//   g_qmi_shake     摇一摇次数（600ms 内 4 次大抖动）
//   g_qmi_face_down 1 = 屏幕朝下（按上电姿态判方向，不写死 +Z，兼容模组倒装）
#include <math.h>
#include <string.h>
#include <freertos/FreeRTOS.h>
#include <freertos/task.h>
#include "esp_log.h"
#include "esp_timer.h"
#include "driver/i2c_master.h"

extern "C" {
volatile float g_qmi_acc[3] = {0.0f, 0.0f, 1.0f};
volatile int   g_qmi_ok = 0;
volatile int   g_qmi_pickup = 0;
volatile int   g_qmi_shake = 0;
volatile int   g_qmi_face_down = 0;
}

namespace {

constexpr const char* QMI_TAG = "QMI8658";
constexpr uint8_t kRegWhoAmI = 0x00;
constexpr uint8_t kRegCtrl1  = 0x02;
constexpr uint8_t kRegCtrl2  = 0x03;
constexpr uint8_t kRegCtrl7  = 0x08;
constexpr uint8_t kRegAccX   = 0x35;
constexpr float   kLsbPerG   = 8192.0f;   // ±4g 档：32768 / 4

i2c_master_dev_handle_t s_qmi_dev = nullptr;

bool QmiWrite(uint8_t reg, uint8_t val) {
    uint8_t buf[2] = {reg, val};
    return i2c_master_transmit(s_qmi_dev, buf, 2, 100) == ESP_OK;
}

bool QmiRead(uint8_t reg, uint8_t* out, size_t len) {
    return i2c_master_transmit_receive(s_qmi_dev, &reg, 1, out, len, 100) == ESP_OK;
}

void QmiTask(void*) {
    uint8_t buf[6];
    float px = 0, py = 0, pz = 0;
    float rest[3] = {0, 0, 1};    // 静止参考姿态：用它算「倾斜了多少度」
    bool  have_rest = false;
    bool  was_quiet = false;      // 动之前的 1 秒内是否安静过
    int   still_ctr = 0;
    int   fail_ctr = 0;
    int64_t last_pickup = 0, last_shake = 0, shake_win = 0;
    int   shake_hits = 0;
    int   face_ctr = 0;
    float az_ref = 0.0f;          // 上电时「朝上」的 Z 分量（兼容模组倒装）

    for (;;) {
        vTaskDelay(pdMS_TO_TICKS(20));
        if (s_qmi_dev == nullptr) continue;
        if (!QmiRead(kRegAccX, buf, 6)) {
            if (++fail_ctr == 20) ESP_LOGW(QMI_TAG, "acc read failing, keep retrying");
            continue;
        }
        fail_ctr = 0;
        float ax = (float)(int16_t)((buf[1] << 8) | buf[0]) / kLsbPerG;
        float ay = (float)(int16_t)((buf[3] << 8) | buf[2]) / kLsbPerG;
        float az = (float)(int16_t)((buf[5] << 8) | buf[4]) / kLsbPerG;
        g_qmi_acc[0] = ax; g_qmi_acc[1] = ay; g_qmi_acc[2] = az;

        float dx = ax - px, dy = ay - py, dz = az - pz;
        px = ax; py = ay; pz = az;
        float dm  = sqrtf(dx * dx + dy * dy + dz * dz);   // 20ms 内的变化量
        float mag = sqrtf(ax * ax + ay * ay + az * az);
        float dev = fabsf(mag - 1.0f);
        bool  quiet = (dm < 0.03f && dev < 0.12f);
        int64_t now = esp_timer_get_time();

        if (az_ref == 0.0f && quiet && fabsf(az) > 0.6f) az_ref = az;
        if (!have_rest) { rest[0] = ax; rest[1] = ay; rest[2] = az; have_rest = true; }

        if (quiet) {
            if (++still_ctr > 50) {           // 连续安静 1 秒
                was_quiet = true;
                // 静止时把参考姿态慢慢挪过来：换个地方摆、慢慢漂移都能自适应
                rest[0] = rest[0] * 0.97f + ax * 0.03f;
                rest[1] = rest[1] * 0.97f + ay * 0.03f;
                rest[2] = rest[2] * 0.97f + az * 0.03f;
            }
        } else {
            // 用「姿态倾斜角」判断拿起，而不是用加速度大小：
            // 慢慢拿起来时合加速度一直≈1g，靠大小根本测不到，倾斜 20 度就很明显。
            float dot = rest[0] * ax + rest[1] * ay + rest[2] * az;
            float nl  = sqrtf(ax * ax + ay * ay + az * az);
            float rl  = sqrtf(rest[0] * rest[0] + rest[1] * rest[1] + rest[2] * rest[2]);
            float tilt = 0.0f;
            if (nl > 0.1f && rl > 0.1f) {
                float c = dot / (nl * rl);
                if (c >  1.0f) c =  1.0f;
                if (c < -1.0f) c = -1.0f;
                tilt = acosf(c) * 57.2958f;
            }
            if (was_quiet && tilt > 18.0f && now - last_pickup > 2000000) {
                g_qmi_pickup = (int)g_qmi_pickup + 1;   // volatile 不能用 ++（C++20 已弃用）
                last_pickup = now;
            }
            still_ctr = 0;
            was_quiet = false;
        }

        // 摇一摇：600ms 内 4 次以上剧烈抖动
        if (dm > 0.35f) {
            if (now - shake_win > 600000) { shake_win = now; shake_hits = 0; }
            if (++shake_hits >= 4 && now - last_shake > 1500000) {
                g_qmi_shake = (int)g_qmi_shake + 1;
                last_shake = now;
                shake_hits = 0;
            }
        }

        bool face_down = (az_ref != 0.0f) && ((az * az_ref) < -0.60f * fabsf(az_ref));
        if (face_down) { if (face_ctr < 60) face_ctr++; }
        else           { face_ctr = 0; }
        g_qmi_face_down = (face_ctr > 40) ? 1 : 0;
    }
}

}  // namespace

void qmi8658_start(i2c_master_bus_handle_t bus) {
    if (bus == nullptr || s_qmi_dev != nullptr) return;

    uint8_t addr = 0x6A;
    if (i2c_master_probe(bus, 0x6A, 100) != ESP_OK) {
        if (i2c_master_probe(bus, 0x6B, 100) != ESP_OK) {
            ESP_LOGW(QMI_TAG, "qmi8658 not found on 0x6a/0x6b, imu disabled");
            return;
        }
        addr = 0x6B;
    }

    i2c_device_config_t cfg = {};
    cfg.dev_addr_length = I2C_ADDR_BIT_LEN_7;
    cfg.device_address  = addr;
    cfg.scl_speed_hz    = 400 * 1000;
    if (i2c_master_bus_add_device(bus, &cfg, &s_qmi_dev) != ESP_OK) {
        ESP_LOGW(QMI_TAG, "add_device failed, imu disabled");
        return;
    }

    uint8_t who = 0;
    QmiRead(kRegWhoAmI, &who, 1);
    QmiWrite(kRegCtrl1, 0x40);   // 寄存器地址自增 + 小端
    QmiWrite(kRegCtrl2, 0x14);   // ±4g / 250Hz
    QmiWrite(kRegCtrl7, 0x01);   // 只开加速度计（陀螺仪不启用，省电）
    vTaskDelay(pdMS_TO_TICKS(30));

    uint8_t chk[6] = {0};
    if (!QmiRead(kRegAccX, chk, 6)) {
        ESP_LOGW(QMI_TAG, "probe ok but acc read failed, imu disabled");
        return;
    }
    float cx = (float)(int16_t)((chk[1] << 8) | chk[0]) / kLsbPerG;
    float cy = (float)(int16_t)((chk[3] << 8) | chk[2]) / kLsbPerG;
    float cz = (float)(int16_t)((chk[5] << 8) | chk[4]) / kLsbPerG;

    g_qmi_ok = 1;
    BaseType_t ok = xTaskCreate(QmiTask, "qmi8658", 3072, nullptr, 3, nullptr);
    ESP_LOGI(QMI_TAG, "imu %s addr=0x%02x whoami=0x%02x acc=%.2f/%.2f/%.2f",
             ok == pdPASS ? "ready" : "task failed", addr, who, cx, cy, cz);
}
// === QMI8658_IMU_PATCH END ===
'''


TEMPLATE = r'''
// === __MARK__：触摸界面（磁贴桌面/时钟/状态/电脑/设置）===
#include "pc_assets.h"
#include <stdio.h>
#include <string.h>
#include <stdint.h>
#include <stdlib.h>
#include <time.h>
#include "application.h"
#include "settings.h"
#include "esp_timer.h"
#include "esp_system.h"
#include "esp_netif.h"
#include "esp_wifi.h"
#include "driver/uart.h"
#include "driver/temperature_sensor.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "board.h"
#include "audio_codec.h"
#include "assets/lang_config.h"

// --- 由电脑端 screen_config.txt 生成 ---
static const char* PC_SLOGAN      = __SLOGAN__;
static const bool  PC_SHOW_IP     = __SHOW_IP__;
static const bool  PC_SHOW_SIGNAL = __SHOW_SIGNAL__;
static const bool  PC_SHOW_HEAP   = __SHOW_HEAP__;
static const bool  PC_SHOW_UPTIME = __SHOW_UPTIME__;
static const bool  PC_SHOW_STATE  = __SHOW_STATE__;
static const bool  PC_USE_WALL    = __WALL__;
static const bool  PC_DEFAULT_DARK = __DARK__;
static const int   PC_BOOT_PAGE    = __BOOT__;
static const bool  PC_IMU          = __IMU__;      // 六轴手势开关
static const int   PC_DIM_SEC      = __DIM__;      // 多久没动静熄背光（秒，0=不熄）

// --- 对象 ---
static lv_obj_t* s_pc_root = nullptr;
static lv_obj_t* s_pc_wall = nullptr;
static lv_obj_t* s_pc_scrim = nullptr;
static lv_obj_t* s_pc_page[6] = {nullptr, nullptr, nullptr, nullptr, nullptr, nullptr};
static lv_obj_t* s_pc_tile[6] = {nullptr, nullptr, nullptr, nullptr, nullptr, nullptr};
static lv_obj_t* s_pc_tile_txt[6] = {nullptr, nullptr, nullptr, nullptr, nullptr, nullptr};
static lv_obj_t* s_pc_row[3] = {nullptr, nullptr, nullptr};
static lv_obj_t* s_pc_row_txt[3] = {nullptr, nullptr, nullptr};
static lv_obj_t* s_pc_row_val[3] = {nullptr, nullptr, nullptr};
static lv_obj_t* s_pc_title = nullptr;
static lv_obj_t* s_pc_pctitle = nullptr;
static lv_obj_t* s_pc_clock_img[5] = {nullptr, nullptr, nullptr, nullptr, nullptr};
static lv_obj_t* s_pc_date = nullptr;
static lv_obj_t* s_pc_slogan_lbl = nullptr;
static lv_obj_t* s_pc_info = nullptr;

// 电脑信息页：6 行「名称 + 数值」
#define PC_PC_ROWS 6
static lv_obj_t* s_pc_pcrow[PC_PC_ROWS] = {nullptr};
static lv_obj_t* s_pc_pcname[PC_PC_ROWS] = {nullptr};
static lv_obj_t* s_pc_pcval[PC_PC_ROWS] = {nullptr};
static lv_obj_t* s_pc_pclink = nullptr;   // 右上角「已连接 / 等待电脑」

static lv_image_dsc_t s_pc_wall_dsc;
static lv_image_dsc_t s_pc_icon_dsc[6];
static lv_image_dsc_t s_pc_digit_dsc[PC_DIGIT_COUNT];
// 闹钟页顶部用的小号数字（21x32），让上面那行时间少占点高度，好把下面的行留宽
static lv_image_dsc_t s_pc_digit_sm_dsc[PC_DIGIT_COUNT];

static int  s_pc_page_idx = 0;
static bool s_pc_dark = PC_DEFAULT_DARK;
static int  s_pc_boot = PC_BOOT_PAGE;
static bool s_pc_manual_hide = false;
static int  s_pc_last_digit[5] = {-1, -1, -1, -1, -1};
static bool s_pc_assets_ready = false;
static bool s_pc_io_ready = false;
static int64_t s_pc_last_pc_us = 0;

// 六轴手势：数据由板级 QMI8658 采集任务写入（见 lichuang_dev_board.cc）
extern "C" {
extern volatile float g_qmi_acc[3];
extern volatile int   g_qmi_ok;
extern volatile int   g_qmi_pickup;
extern volatile int   g_qmi_shake;
extern volatile int   g_qmi_face_down;
}

static int  s_pc_imu_pickup_seen = 0;
static int  s_pc_imu_shake_seen = 0;
static bool s_pc_dimmed = false;          // 背光已关
static bool s_pc_muted = false;           // 屏幕朝下静音中
static int  s_pc_vol_saved = 70;
static int  s_pc_bright_saved = 75;       // 熄屏前的背光亮度
static int64_t s_pc_act_us = 0;           // 最后一次用户动作（触摸/拿起/摇）

static void pc_wake();

// 电脑端经串口下发的主机信息
static volatile float s_pc_cpu = -1;
static volatile float s_pc_freq = -1;
static volatile float s_pc_mempct = -1;
static volatile float s_pc_memu = -1;
static volatile float s_pc_memt = -1;
static volatile float s_pc_disk = -1;
static volatile float s_pc_dn = -1;
static volatile float s_pc_up = -1;
static volatile float s_pc_ctemp = -1;
static volatile int64_t s_pc_host_stamp = 0;   // 最近一次收到数据的时间（us）

// 芯片温度（ESP32-S3 内部传感器，不是室温）
static temperature_sensor_handle_t s_pc_tsens = nullptr;
static bool s_pc_tsens_ok = false;
static volatile float s_pc_chip_temp = -999;

static const uint32_t PC_ACCENT[6] = {0x59A7FF, 0x38D6E0, 0x4ADE80, 0xC792EA, 0xFFB020, 0xFF6B6B};
static const char* PC_TILE_NAME[6] = {"小智AI", "时钟", "状态", "电脑", "设置", "闹钟"};
static const int PC_TILE_ICON[6] = {0, 1, 2, 4, 3, 5};
// 磁贴 → action code：4=收起面板，1/2/5/6=切页，3=设置页
static const int PC_TILE_CODE[6] = {4, 1, 2, 5, 3, 6};

static void pc_make_dsc(lv_image_dsc_t* d, const uint8_t* data, uint32_t size,
                        int w, int h, int stride, lv_color_format_t cf) {
    memset(d, 0, sizeof(*d));
    d->data_size = size;
    d->data = data;
    d->header.magic = LV_IMAGE_HEADER_MAGIC;
    d->header.cf = (uint32_t)cf;
    d->header.w = (uint16_t)w;
    d->header.h = (uint16_t)h;
    d->header.stride = (uint16_t)stride;
}

static void pc_init_assets() {
    if (s_pc_assets_ready) return;
    s_pc_assets_ready = true;
    pc_make_dsc(&s_pc_wall_dsc, pc_wallpaper_map, (uint32_t)sizeof(pc_wallpaper_map),
                PC_WALLPAPER_W, PC_WALLPAPER_H, PC_WALLPAPER_W * 2, LV_COLOR_FORMAT_RGB565);
    for (int i = 0; i < 6; i++) {
        uint32_t one = (uint32_t)(PC_ICON_W * PC_ICON_H * 3);
        pc_make_dsc(&s_pc_icon_dsc[i], pc_icons_map + one * (uint32_t)i, one,
                    PC_ICON_W, PC_ICON_H, PC_ICON_W * 2, LV_COLOR_FORMAT_RGB565A8);
    }
    for (int i = 0; i < PC_DIGIT_COUNT; i++) {
        uint32_t one = (uint32_t)(PC_DIGIT_W * PC_DIGIT_H * 3);
        pc_make_dsc(&s_pc_digit_dsc[i], pc_digits_map + one * (uint32_t)i, one,
                    PC_DIGIT_W, PC_DIGIT_H, PC_DIGIT_W * 2, LV_COLOR_FORMAT_RGB565A8);
    }
    for (int i = 0; i < PC_DIGIT_COUNT; i++) {
        uint32_t one = (uint32_t)(PC_DIGIT_SM_W * PC_DIGIT_SM_H * 3);
        pc_make_dsc(&s_pc_digit_sm_dsc[i], pc_digits_sm_map + one * (uint32_t)i, one,
                    PC_DIGIT_SM_W, PC_DIGIT_SM_H, PC_DIGIT_SM_W * 2, LV_COLOR_FORMAT_RGB565A8);
    }
}

static void pc_show_page(int p) {
    if (p < 0) p = 0;
    if (p > 5) p = 5;
    s_pc_page_idx = p;
    for (int i = 0; i < 6; i++) {
        if (s_pc_page[i] == nullptr) continue;
        if (i == p) lv_obj_remove_flag(s_pc_page[i], LV_OBJ_FLAG_HIDDEN);
        else        lv_obj_add_flag(s_pc_page[i], LV_OBJ_FLAG_HIDDEN);
    }
    for (int i = 0; i < 5; i++) s_pc_last_digit[i] = -1;
}

static void pc_refresh_settings() {
    if (s_pc_row_val[0]) lv_label_set_text(s_pc_row_val[0], s_pc_boot == 0 ? "主界面" : "时钟");
    if (s_pc_row_val[1]) lv_label_set_text(s_pc_row_val[1], s_pc_dark ? "暗色" : "亮色");
}

static void pc_apply_palette() {
    lv_color_t tile = lv_color_hex(s_pc_dark ? 0x141A26 : 0xF5F8FE);
    lv_color_t txt  = lv_color_hex(s_pc_dark ? 0xECF0F8 : 0x1A2230);
    lv_color_t sub  = lv_color_hex(s_pc_dark ? 0x9AA6BC : 0x5A6577);
    lv_color_t row  = lv_color_hex(s_pc_dark ? 0x161C28 : 0xFFFFFF);
    lv_color_t scr  = lv_color_hex(s_pc_dark ? 0x04060A : 0xFFFFFF);
    lv_color_t blue = lv_color_hex(0x59A7FF);
    lv_color_t bd   = lv_color_hex(s_pc_dark ? 0x2A3446 : 0xDCE3EE);

    if (s_pc_scrim) {
        lv_obj_set_style_bg_color(s_pc_scrim, scr, 0);
        lv_obj_set_style_bg_opa(s_pc_scrim, s_pc_dark ? 120 : 90, 0);
    }
    for (int i = 0; i < 5; i++) {
        if (s_pc_tile[i])     lv_obj_set_style_bg_color(s_pc_tile[i], tile, 0);
        if (s_pc_tile_txt[i]) lv_obj_set_style_text_color(s_pc_tile_txt[i], txt, 0);
    }
    for (int i = 0; i < 3; i++) {
        if (s_pc_row[i])     lv_obj_set_style_bg_color(s_pc_row[i], row, 0);
        if (s_pc_row_txt[i]) lv_obj_set_style_text_color(s_pc_row_txt[i], txt, 0);
        if (s_pc_row_val[i]) lv_obj_set_style_text_color(s_pc_row_val[i], blue, 0);
    }
    for (int i = 0; i < PC_PC_ROWS; i++) {
        if (s_pc_pcrow[i]) {
            lv_obj_set_style_bg_color(s_pc_pcrow[i], row, 0);
            lv_obj_set_style_border_color(s_pc_pcrow[i], bd, 0);
        }
        if (s_pc_pcname[i]) lv_obj_set_style_text_color(s_pc_pcname[i], sub, 0);
        if (s_pc_pcval[i])  lv_obj_set_style_text_color(s_pc_pcval[i], blue, 0);
    }
    if (s_pc_pctitle)    lv_obj_set_style_text_color(s_pc_pctitle, txt, 0);
    if (s_pc_pclink)     lv_obj_set_style_text_color(s_pc_pclink, sub, 0);
    if (s_pc_title)      lv_obj_set_style_text_color(s_pc_title, txt, 0);
    if (s_pc_date)       lv_obj_set_style_text_color(s_pc_date, txt, 0);
    if (s_pc_slogan_lbl) lv_obj_set_style_text_color(s_pc_slogan_lbl, sub, 0);
    if (s_pc_info)       lv_obj_set_style_text_color(s_pc_info, txt, 0);
}

static void pc_action_cb(lv_event_t* e) {
    if (lv_event_get_code(e) != LV_EVENT_CLICKED) return;
    if (s_pc_dimmed) { pc_wake(); return; }   // 熄屏状态下第一下只负责点亮，不触发动作
    int code = (int)(intptr_t)lv_event_get_user_data(e);
    Settings st("screen_panel", true);
    switch (code) {
        case 1: pc_show_page(1); break;
        case 2: pc_show_page(2); break;
        case 3: pc_show_page(3); break;
        case 5: pc_show_page(4); break;
        case 6: pc_show_page(5); break;
        case 4:
            s_pc_manual_hide = true;
            if (s_pc_root) lv_obj_add_flag(s_pc_root, LV_OBJ_FLAG_HIDDEN);
            break;
        case 10:
            s_pc_boot = (s_pc_boot + 1) % 2;
            st.SetInt("boot", s_pc_boot);
            pc_refresh_settings();
            break;
        case 11:
            s_pc_dark = !s_pc_dark;
            st.SetInt("dark", s_pc_dark ? 1 : 0);
            pc_apply_palette();
            pc_refresh_settings();
            break;
        case 12: pc_show_page(0); break;
        default: break;
    }
}

static void pc_page_click_cb(lv_event_t* e) {
    if (lv_event_get_code(e) != LV_EVENT_CLICKED) return;
    if (s_pc_dimmed) { pc_wake(); return; }
    int p = s_pc_page_idx;
    if (p == 0)      pc_show_page(1);
    else if (p == 1) pc_show_page(2);
    else if (p == 2) pc_show_page(5);
    else if (p == 5) pc_show_page(4);
    else if (p == 4) pc_show_page(0);
}

static const char* pc_week_name(int w) {
    static const char* wd[7] = {"日", "一", "二", "三", "四", "五", "六"};
    if (w < 0 || w > 6) return "?";
    return wd[w];
}

static void pc_refresh_info() {
    // main 组件开了 -Werror=frame-larger-than=1536，缓冲区别贪大
    char buf[300];
    size_t n = 0;
    if (PC_SHOW_IP) {
        char ip[16] = "未连接";
        esp_netif_t* h = esp_netif_get_handle_from_ifkey("WIFI_STA_DEF");
        esp_netif_ip_info_t ii;
        if (h != nullptr && esp_netif_get_ip_info(h, &ii) == ESP_OK && ii.ip.addr != 0) {
            snprintf(ip, sizeof(ip), IPSTR, IP2STR(&ii.ip));
        }
        n += snprintf(buf + n, sizeof(buf) - n, "IP      %s\n", ip);
    }
    if (PC_SHOW_SIGNAL) {
        wifi_ap_record_t ap;
        int rssi = -999;
        if (esp_wifi_sta_get_ap_info(&ap) == ESP_OK) rssi = ap.rssi;
        const char* sig = "未知";
        if (rssi >= -55) sig = "强";
        else if (rssi >= -70) sig = "中";
        else if (rssi > -999) sig = "弱";
        n += snprintf(buf + n, sizeof(buf) - n, "WiFi    %s\n", sig);
    }
    if (PC_SHOW_HEAP) {
        uint32_t free_kb = esp_get_free_heap_size() / 1024;
        n += snprintf(buf + n, sizeof(buf) - n, "内存    %u KB\n", (unsigned)free_kb);
    }
    if (PC_SHOW_UPTIME) {
        uint64_t sec = (uint64_t)(esp_timer_get_time() / 1000000);
        n += snprintf(buf + n, sizeof(buf) - n, "运行    %02u:%02u:%02u\n",
                      (unsigned)(sec / 3600), (unsigned)((sec % 3600) / 60), (unsigned)(sec % 60));
    }
    if (PC_SHOW_STATE) {
        auto st = Application::GetInstance().GetDeviceState();
        const char* s = "连接中";
        if      (st == kDeviceStateIdle)      s = "待机";
        else if (st == kDeviceStateListening) s = "聆听中";
        else if (st == kDeviceStateSpeaking)  s = "说话中";
        n += snprintf(buf + n, sizeof(buf) - n, "状态    %s\n", s);
    }
    if (n == 0) n += snprintf(buf + n, sizeof(buf) - n, "暂无信息");
    if (s_pc_info) lv_label_set_text(s_pc_info, buf);
}

// ---------------- 电脑信息：串口下行接收 ----------------
//
// 电脑端（serial_speech.py）每 2 秒往同一个 COM 口写一行：
//   @PC cpu=23 freq=3.6 mempct=61 memu=9.8 memt=16 disk=45 dn=1220 up=310 ctemp=56
// 板子这边 UART0 是全双工的，日志走 TX 不受影响，这里只读 RX。

static float pc_kv(const char* s, const char* key) {
    const char* p = strstr(s, key);
    if (p == nullptr) return -1;
    p += strlen(key);
    if (*p != '=') return -1;
    return strtof(p + 1, nullptr);
}

static void pc_parse_line(const char* line) {
    if (strncmp(line, "@PC", 3) != 0) return;
    s_pc_cpu    = pc_kv(line, "cpu");
    s_pc_freq   = pc_kv(line, "freq");
    s_pc_mempct = pc_kv(line, "mempct");
    s_pc_memu   = pc_kv(line, "memu");
    s_pc_memt   = pc_kv(line, "memt");
    s_pc_disk   = pc_kv(line, "disk");
    s_pc_dn     = pc_kv(line, "dn");
    s_pc_up     = pc_kv(line, "up");
    s_pc_ctemp  = pc_kv(line, "ctemp");
    s_pc_host_stamp = esp_timer_get_time();
}

static void pc_uart_task(void* /*arg*/) {
    static char line[192];
    int n = 0;
    uint8_t buf[96];
    for (;;) {
        int r = uart_read_bytes(UART_NUM_0, buf, sizeof(buf), pdMS_TO_TICKS(500));
        for (int i = 0; i < r; i++) {
            char ch = (char)buf[i];
            if (ch == '\n' || ch == '\r') {
                if (n > 3) {
                    line[n] = '\0';
                    pc_parse_line(line);
                }
                n = 0;
            } else if (n < (int)sizeof(line) - 1) {
                line[n++] = ch;
            } else {
                n = 0;   // 垃圾数据太长，丢掉重来
            }
        }
    }
}

static void pc_host_rx_start() {
    if (s_pc_io_ready) return;
    s_pc_io_ready = true;

    // 先按 8N1/115200 重配一遍（console 启动时已经配过，这里只是确保一致），
    // 再装 RX 驱动。日志输出走 ROM 的直接写，不经过 driver，所以装驱动不影响打印。
    uart_config_t cfg = {};
    cfg.baud_rate  = 115200;
    cfg.data_bits  = UART_DATA_8_BITS;
    cfg.parity     = UART_PARITY_DISABLE;
    cfg.stop_bits  = UART_STOP_BITS_1;
    cfg.flow_ctrl  = UART_HW_FLOWCTRL_DISABLE;
    cfg.source_clk = UART_SCLK_DEFAULT;
    uart_param_config(UART_NUM_0, &cfg);

    esp_err_t err = uart_driver_install(UART_NUM_0, 1024, 0, 0, nullptr, 0);
    if (err != ESP_OK) {
        ESP_LOGW("ScreenPanel", "uart0 rx install failed(0x%x)，电脑信息不可用", err);
        return;
    }
    BaseType_t ok = xTaskCreate(pc_uart_task, "pc_host_rx", 4096, nullptr, 3, nullptr);
    ESP_LOGI("ScreenPanel", "pc host rx %s (uart0 115200)",
             ok == pdPASS ? "ready" : "task create failed");
}

// ---------------- 芯片温度 ----------------
// ESP32-S3 片内传感器。注意它测的是芯片自身温度，比室温高 5~15 度，
// 而且会随 CPU/WiFi 负载跳动 —— 只能当参考，不是环境温度。

static void pc_chip_temp_start() {
    temperature_sensor_config_t cfg = TEMPERATURE_SENSOR_CONFIG_DEFAULT(-10, 80);
    if (temperature_sensor_install(&cfg, &s_pc_tsens) == ESP_OK &&
        temperature_sensor_enable(s_pc_tsens) == ESP_OK) {
        s_pc_tsens_ok = true;
        ESP_LOGI("ScreenPanel", "chip temp sensor ready");
    } else {
        s_pc_tsens_ok = false;
        ESP_LOGW("ScreenPanel", "chip temp sensor unavailable");
    }
}

// ---------------- 六轴手势：拿起亮屏 / 摇一摇切页 / 翻转静音 ----------------

static void pc_wake() {
    s_pc_act_us = esp_timer_get_time();
    if (!s_pc_dimmed) return;
    s_pc_dimmed = false;
    Backlight* bl = Board::GetInstance().GetBacklight();
    if (bl != nullptr) bl->SetBrightness((uint8_t)(s_pc_bright_saved > 0 ? s_pc_bright_saved : 75));
    ESP_LOGI("ScreenPanel", "wake (brightness %d)", s_pc_bright_saved);
}

static void pc_sleep() {
    if (s_pc_dimmed) return;
    Backlight* bl = Board::GetInstance().GetBacklight();
    if (bl == nullptr) return;
    int cur = bl->brightness();
    if (cur > 0) s_pc_bright_saved = cur;
    s_pc_dimmed = true;
    bl->SetBrightness(0);
    ESP_LOGI("ScreenPanel", "dim");
}

static void pc_next_page() {
    int p = s_pc_page_idx;
    if      (p == 0) pc_show_page(1);
    else if (p == 1) pc_show_page(2);
    else if (p == 2) pc_show_page(5);
    else if (p == 5) pc_show_page(4);
    else             pc_show_page(0);
}

// 设置页没有「点空白翻页」回调，单独挂一个只唤醒的
static void pc_wake_only_cb(lv_event_t* e) {
    if (lv_event_get_code(e) == LV_EVENT_CLICKED) pc_wake();
}

static void pc_imu_poll() {
    if (!PC_IMU) return;
    int64_t now = esp_timer_get_time();

    if (g_qmi_pickup != s_pc_imu_pickup_seen) {
        s_pc_imu_pickup_seen = g_qmi_pickup;
        pc_wake();
    }
    if (g_qmi_shake != s_pc_imu_shake_seen) {
        s_pc_imu_shake_seen = g_qmi_shake;
        pc_wake();
        pc_next_page();
    }

    // 翻转静音：屏幕朝下时说话没意义，先闭嘴（翻回来恢复原音量）
    bool fd = (g_qmi_face_down != 0);
    AudioCodec* codec = Board::GetInstance().GetAudioCodec();
    if (fd && !s_pc_muted) {
        if (codec != nullptr) {
            s_pc_vol_saved = codec->output_volume();
            codec->SetOutputVolume(0);
        }
        s_pc_muted = true;
    } else if (!fd && s_pc_muted) {
        if (codec != nullptr) codec->SetOutputVolume(s_pc_vol_saved > 0 ? s_pc_vol_saved : 70);
        s_pc_muted = false;
    }

    // 安静一段时间就关背光。只在待机时熄，说话/聆听时保持亮着（熄着的话立刻点亮）。
    bool idle = (Application::GetInstance().GetDeviceState() == kDeviceStateIdle);
    if (!idle) {
        if (s_pc_dimmed) pc_wake();
        s_pc_act_us = now;
    } else if (!s_pc_dimmed && PC_DIM_SEC > 0 &&
               now - s_pc_act_us > (int64_t)PC_DIM_SEC * 1000000LL) {
        pc_sleep();
    }
}

// ---------------- 电脑页刷新 ----------------

static void pc_fmt_net(char* out, size_t n, float kb) {
    if (kb < 0)                       snprintf(out, n, "--");
    else if (kb >= 1024.0f)           snprintf(out, n, "%.1fM", kb / 1024.0f);
    else                              snprintf(out, n, "%.0fK", kb);
}

static void pc_refresh_pc_page() {
    int64_t now = esp_timer_get_time();
    if (now - s_pc_last_pc_us < 1500000) return;   // 1.5 秒节流，别每 500ms 都读传感器
    s_pc_last_pc_us = now;

    if (s_pc_tsens_ok) {
        float c = 0;
        if (temperature_sensor_get_celsius(s_pc_tsens, &c) == ESP_OK && c > -20 && c < 120) {
            s_pc_chip_temp = c;
        }
    }

    bool live = (s_pc_host_stamp > 0) && (now - s_pc_host_stamp < 20000000LL);
    if (s_pc_pclink) lv_label_set_text(s_pc_pclink, live ? "USB 已连接" : "等待电脑");

    char v[PC_PC_ROWS][24];
    memset(v, 0, sizeof(v));

    if (s_pc_cpu >= 0) {
        if (s_pc_freq > 0) snprintf(v[0], sizeof(v[0]), "%.0f%% %.1fGHz", s_pc_cpu, s_pc_freq);
        else               snprintf(v[0], sizeof(v[0]), "%.0f%%", s_pc_cpu);
    } else snprintf(v[0], sizeof(v[0]), "--");

    if (s_pc_mempct >= 0) {
        if (s_pc_memt > 0) snprintf(v[1], sizeof(v[1]), "%.0f%% %.1f/%.0fG",
                                    s_pc_mempct, s_pc_memu, s_pc_memt);
        else               snprintf(v[1], sizeof(v[1]), "%.0f%%", s_pc_mempct);
    } else snprintf(v[1], sizeof(v[1]), "--");

    if (s_pc_disk >= 0) snprintf(v[2], sizeof(v[2]), "%.0f%%", s_pc_disk);
    else                snprintf(v[2], sizeof(v[2]), "--");

    {
        char a[10], b[10];
        pc_fmt_net(a, sizeof(a), s_pc_dn);
        pc_fmt_net(b, sizeof(b), s_pc_up);
        snprintf(v[3], sizeof(v[3]), "%s / %s", a, b);
    }

    if (s_pc_ctemp > 0) snprintf(v[4], sizeof(v[4]), "%.0f 度", s_pc_ctemp);
    else                snprintf(v[4], sizeof(v[4]), "未装");

    if (s_pc_chip_temp > -100) snprintf(v[5], sizeof(v[5]), "%.0f 度", s_pc_chip_temp);
    else                       snprintf(v[5], sizeof(v[5]), "--");

    for (int i = 0; i < PC_PC_ROWS; i++) {
        if (s_pc_pcval[i]) lv_label_set_text(s_pc_pcval[i], v[i]);
    }
}

// ---------------- 闹钟 / 定时提醒 ----------------
#define PC_ALM_N 3

typedef struct {
    uint8_t on;
    uint8_t hh;
    uint8_t mm;
    uint8_t rep;      // 0=只响一次  1=每天  2=工作日
} pc_alm_t;

static pc_alm_t  s_pc_alm[PC_ALM_N] = {{1, 7, 0, 2}, {0, 8, 30, 1}, {0, 12, 0, 1}};
static lv_obj_t* s_pc_alm_row[PC_ALM_N] = {nullptr, nullptr, nullptr};
static lv_obj_t* s_pc_alm_t[PC_ALM_N] = {nullptr, nullptr, nullptr};
static lv_obj_t* s_pc_alm_r[PC_ALM_N] = {nullptr, nullptr, nullptr};
static lv_obj_t* s_pc_alm_s[PC_ALM_N] = {nullptr, nullptr, nullptr};
static lv_obj_t* s_pc_alm_big[5] = {nullptr, nullptr, nullptr, nullptr, nullptr};
static lv_obj_t* s_pc_alm_date = nullptr;
static lv_obj_t* s_pc_alm_hint = nullptr;
static lv_obj_t* s_pc_alm_ov = nullptr;
static lv_obj_t* s_pc_alm_ov_t = nullptr;

static int  s_pc_alm_digit[5] = {-1, -1, -1, -1, -1};
static int  s_pc_alm_cache[PC_ALM_N] = {-1, -1, -1};
static int  s_pc_alm_ring = -1;                 // 正在响的槽位，-1=没响
static int64_t s_pc_alm_ring_us = 0;            // 上一次补音的时刻
static int  s_pc_alm_vol_bak = -1;              // 响铃前的音量
static int  s_pc_alm_last_key = -1;             // 上次判过的「日期+时分」
static bool s_pc_alm_snoozed = false;
static int  s_pc_cd_end = 0;                    // 倒计时截止（epoch 秒），0=没开
static int  s_pc_cd_total = 0;

static void pc_alm_save() {
    char buf[64];
    int cap = (int)sizeof(buf);
    int n = 0;
    for (int i = 0; i < PC_ALM_N; i++) {
        if (cap - n < 16) break;
        n += snprintf(buf + n, (size_t)(cap - n), "%s%d,%d,%d,%d", i ? ";" : "",
                      (int)s_pc_alm[i].on, (int)s_pc_alm[i].hh,
                      (int)s_pc_alm[i].mm, (int)s_pc_alm[i].rep);
    }
    Settings st("screen_panel", true);
    st.SetString("alarms", buf);
}

static void pc_alm_load() {
    Settings st("screen_panel", false);
    std::string s = st.GetString("alarms", "1,7,0,2;0,8,30,1;0,12,0,1");
    const char* p = s.c_str();
    for (int i = 0; i < PC_ALM_N && *p; i++) {
        int on = 0, hh = 7, mm = 0, rep = 1;
        if (sscanf(p, "%d,%d,%d,%d", &on, &hh, &mm, &rep) == 4) {
            s_pc_alm[i].on  = on ? 1 : 0;
            s_pc_alm[i].hh  = (uint8_t)((hh % 24 + 24) % 24);
            s_pc_alm[i].mm  = (uint8_t)((mm % 60 + 60) % 60);
            s_pc_alm[i].rep = (uint8_t)((rep % 3 + 3) % 3);
        }
        const char* q = strchr(p, ';');
        if (q == nullptr) break;
        p = q + 1;
    }
}

static void pc_alm_fire(int idx) {
    s_pc_alm_ring = idx;
    s_pc_alm_ring_us = esp_timer_get_time();
    s_pc_alm_snoozed = false;
    s_pc_manual_hide = false;
    pc_wake();                       // 熄着屏也要亮起来
    AudioCodec* ac = Board::GetInstance().GetAudioCodec();
    if (ac != nullptr) {
        if (s_pc_alm_vol_bak < 0) s_pc_alm_vol_bak = ac->output_volume();
        ac->SetOutputVolume(100);
    }
    pc_show_page(5);
    if (s_pc_alm_ov != nullptr) {
        lv_obj_remove_flag(s_pc_alm_ov, LV_OBJ_FLAG_HIDDEN);
    }
    if (s_pc_alm_ov_t != nullptr) {
        char t[16];
        if (idx >= 0 && idx < PC_ALM_N) {
            snprintf(t, sizeof(t), "%02d:%02d", s_pc_alm[idx].hh, s_pc_alm[idx].mm);
        } else {
            snprintf(t, sizeof(t), "倒计时结束");
        }
        lv_label_set_text(s_pc_alm_ov_t, t);
    }
    Application::GetInstance().PlaySound(Lang::Sounds::OGG_SUCCESS);
    ESP_LOGI("ScreenPanel", "alarm fire idx=%d", idx);
}

static void pc_alm_stop(bool snooze) {
    if (s_pc_alm_ov != nullptr) lv_obj_add_flag(s_pc_alm_ov, LV_OBJ_FLAG_HIDDEN);
    AudioCodec* ac = Board::GetInstance().GetAudioCodec();
    if (ac != nullptr && s_pc_alm_vol_bak > 0) ac->SetOutputVolume(s_pc_alm_vol_bak);
    s_pc_alm_vol_bak = -1;
    s_pc_alm_ring = -1;
    s_pc_alm_last_key = -1;
    if (snooze) {
        time_t now = time(nullptr);
        s_pc_cd_total = 300;
        s_pc_cd_end = (int)now + 300;
    } else {
        s_pc_cd_end = 0;
        s_pc_cd_total = 0;
    }
    s_pc_act_us = esp_timer_get_time();
}

static void pc_cd_start(int minutes) {
    time_t now = time(nullptr);
    if (now < 1600000000) {          // 还没对时，终点会算错
        if (s_pc_alm_hint) lv_label_set_text(s_pc_alm_hint, "还没对时，稍等再试");
        return;
    }
    s_pc_cd_total = minutes * 60;
    s_pc_cd_end = (int)now + minutes * 60;
    s_pc_alm_last_key = -1;
    if (s_pc_alm_hint) lv_label_set_text(s_pc_alm_hint, "倒计时中 · 点任意按钮取消");
}

static void pc_alm_tick() {
    if (s_pc_alm_ring >= 0) {
        s_pc_act_us = esp_timer_get_time();      // 响铃期间不许熄屏
        int64_t dt = esp_timer_get_time() - s_pc_alm_ring_us;
        if (dt > 180000000LL) {                  // 3 分钟没人管，自己停
            pc_alm_stop(false);
            return;
        }
        if (dt > 2000000LL) {                    // 每 2 秒补一次提示音
            s_pc_alm_ring_us = esp_timer_get_time();
            Application::GetInstance().PlaySound(Lang::Sounds::OGG_SUCCESS);
        }
        return;
    }

    time_t tnow = time(nullptr);
    struct tm tm_now;
    localtime_r(&tnow, &tm_now);
    if (tm_now.tm_year < 120) return;            // 还没对时

    if (s_pc_cd_end > 0 && (int)tnow >= s_pc_cd_end) {
        s_pc_cd_end = 0;
        s_pc_cd_total = 0;
        pc_alm_fire(-2);
        return;
    }

    int key = (tm_now.tm_yday << 11) | (tm_now.tm_hour << 6) | tm_now.tm_min;
    if (key == s_pc_alm_last_key) return;        // 同一分钟只判一次
    s_pc_alm_last_key = key;
    for (int i = 0; i < PC_ALM_N; i++) {
        if (!s_pc_alm[i].on) continue;
        if (s_pc_alm[i].hh != tm_now.tm_hour || s_pc_alm[i].mm != tm_now.tm_min) continue;
        int wd = tm_now.tm_wday;                 // 0=周日
        if (s_pc_alm[i].rep == 2 && (wd == 0 || wd == 6)) continue;
        if (s_pc_alm[i].rep == 0) {              // 只响一次，响完自动关
            s_pc_alm[i].on = 0;
            s_pc_alm_cache[i] = -1;
            pc_alm_save();
        }
        pc_alm_fire(i);
        break;
    }
}

static void pc_alm_row_cb(lv_event_t* e) {
    if (lv_event_get_code(e) != LV_EVENT_CLICKED) return;
    if (s_pc_dimmed) { pc_wake(); return; }
    int i = (int)(intptr_t)lv_event_get_user_data(e);
    if (i < 0 || i >= PC_ALM_N) return;
    s_pc_alm[i].on = s_pc_alm[i].on ? 0 : 1;
    s_pc_alm_cache[i] = -1;
    s_pc_alm_last_key = -1;
    pc_alm_save();
    s_pc_act_us = esp_timer_get_time();
}

static void pc_alm_time_cb(lv_event_t* e) {
    lv_event_code_t c = lv_event_get_code(e);
    if (c != LV_EVENT_CLICKED && c != LV_EVENT_LONG_PRESSED) return;
    if (s_pc_dimmed) { pc_wake(); return; }
    int i = (int)(intptr_t)lv_event_get_user_data(e);
    if (i < 0 || i >= PC_ALM_N) return;
    int step = (c == LV_EVENT_LONG_PRESSED) ? 60 : 10;
    int tot = (int)s_pc_alm[i].hh * 60 + (int)s_pc_alm[i].mm + step;
    tot %= 1440;
    s_pc_alm[i].hh = (uint8_t)(tot / 60);
    s_pc_alm[i].mm = (uint8_t)(tot % 60);
    s_pc_alm[i].on = 1;                          // 调时间顺手打开
    s_pc_alm_cache[i] = -1;
    s_pc_alm_last_key = -1;
    pc_alm_save();
    s_pc_act_us = esp_timer_get_time();
}

static void pc_cd_btn_cb(lv_event_t* e) {
    if (lv_event_get_code(e) != LV_EVENT_CLICKED) return;
    if (s_pc_dimmed) { pc_wake(); return; }
    int m = (int)(intptr_t)lv_event_get_user_data(e);
    if (s_pc_cd_end > 0) {
        s_pc_cd_end = 0;
        s_pc_cd_total = 0;
        if (s_pc_alm_hint) lv_label_set_text(s_pc_alm_hint, "已取消");
    } else {
        pc_cd_start(m);
    }
    s_pc_act_us = esp_timer_get_time();
}

static void pc_alm_ov_cb(lv_event_t* e) {
    lv_event_code_t c = lv_event_get_code(e);
    if (c == LV_EVENT_LONG_PRESSED) {
        s_pc_alm_snoozed = true;
        pc_alm_stop(true);
    } else if (c == LV_EVENT_CLICKED && !s_pc_alm_snoozed) {
        pc_alm_stop(false);
    }
}

static void pc_alm_refresh() {
    time_t tnow = time(nullptr);
    struct tm tm_now;
    localtime_r(&tnow, &tm_now);
    bool ok = tm_now.tm_year >= 120;

    if (ok) {
        int hh = tm_now.tm_hour, mm = tm_now.tm_min;
        int idx[5] = { hh / 10, hh % 10, 10, mm / 10, mm % 10 };
        for (int i = 0; i < 5; i++) {
            if (s_pc_alm_big[i] == nullptr) continue;
            if (s_pc_alm_digit[i] == idx[i]) continue;
            s_pc_alm_digit[i] = idx[i];
            lv_image_set_src(s_pc_alm_big[i], &s_pc_digit_sm_dsc[idx[i]]);
        }
    }

    if (s_pc_alm_date != nullptr) {
        char d[56];
        if (!ok) {
            lv_label_set_text(s_pc_alm_date, "正在对时…");
        } else if (s_pc_cd_end > 0) {
            int left = s_pc_cd_end - (int)tnow;
            if (left < 0) left = 0;
            snprintf(d, sizeof(d), "%d月%d日 周%s · 剩 %d:%02d",
                     tm_now.tm_mon + 1, tm_now.tm_mday, pc_week_name(tm_now.tm_wday),
                     left / 60, left % 60);
            lv_label_set_text(s_pc_alm_date, d);
        } else {
            snprintf(d, sizeof(d), "%d月%d日 周%s", tm_now.tm_mon + 1, tm_now.tm_mday,
                     pc_week_name(tm_now.tm_wday));
            lv_label_set_text(s_pc_alm_date, d);
        }
    }

    for (int i = 0; i < PC_ALM_N; i++) {
        int sig = (int)s_pc_alm[i].on | ((int)s_pc_alm[i].hh << 1)
                | ((int)s_pc_alm[i].mm << 8) | ((int)s_pc_alm[i].rep << 16);
        if (sig == s_pc_alm_cache[i]) continue;
        s_pc_alm_cache[i] = sig;
        if (s_pc_alm_t[i] != nullptr) {
            char t[8];
            snprintf(t, sizeof(t), "%02d:%02d", s_pc_alm[i].hh, s_pc_alm[i].mm);
            lv_label_set_text(s_pc_alm_t[i], t);
        }
        if (s_pc_alm_r[i] != nullptr) {
            const char* r = (s_pc_alm[i].rep == 0) ? "一次"
                          : ((s_pc_alm[i].rep == 1) ? "每天" : "工作日");
            lv_label_set_text(s_pc_alm_r[i], r);
        }
        if (s_pc_alm_s[i] != nullptr) {
            lv_label_set_text(s_pc_alm_s[i], s_pc_alm[i].on ? "开" : "关");
            lv_obj_set_style_text_color(s_pc_alm_s[i],
                lv_color_hex(s_pc_alm[i].on ? 0x4ADE80 : 0x8892A6), 0);
        }
    }
}

static void pc_timer_cb(lv_timer_t* /*t*/) {
    pc_imu_poll();   // 手势常驻：面板隐藏/熄屏时也要能靠拿起、摇晃叫醒
    if (s_pc_root == nullptr) return;

    pc_alm_tick();   // 闹钟/倒计时：和页面是否可见无关，必须每轮都跑

    bool idle = (Application::GetInstance().GetDeviceState() == kDeviceStateIdle);
    if (!idle) s_pc_manual_hide = false;
    bool show = (s_pc_alm_ring >= 0) || (idle && !s_pc_manual_hide);
    if (show) lv_obj_remove_flag(s_pc_root, LV_OBJ_FLAG_HIDDEN);
    else {
        lv_obj_add_flag(s_pc_root, LV_OBJ_FLAG_HIDDEN);
        return;
    }

    if (s_pc_page_idx == 1) {
        time_t tnow = time(nullptr);
        struct tm tm_now;
        localtime_r(&tnow, &tm_now);
        if (tm_now.tm_year >= 120) {
            int hh = tm_now.tm_hour, mm = tm_now.tm_min;
            int idx[5] = { hh / 10, hh % 10, 10, mm / 10, mm % 10 };
            for (int i = 0; i < 5; i++) {
                if (s_pc_clock_img[i] == nullptr) continue;
                if (s_pc_last_digit[i] == idx[i]) continue;
                s_pc_last_digit[i] = idx[i];
                lv_image_set_src(s_pc_clock_img[i], &s_pc_digit_dsc[idx[i]]);
            }
            char d[40];
            snprintf(d, sizeof(d), "%d月%d日 周%s", tm_now.tm_mon + 1, tm_now.tm_mday,
                     pc_week_name(tm_now.tm_wday));
            if (s_pc_date) lv_label_set_text(s_pc_date, d);
        } else if (s_pc_date) {
            lv_label_set_text(s_pc_date, "正在对时…");
        }
    } else if (s_pc_page_idx == 2) {
        pc_refresh_info();
    } else if (s_pc_page_idx == 4) {
        pc_refresh_pc_page();
    } else if (s_pc_page_idx == 5) {
        pc_alm_refresh();
    }
}

static void pc_build_ui(lv_obj_t* screen) {
    pc_init_assets();

    {
        Settings st("screen_panel", false);
        s_pc_dark = st.GetInt("dark", PC_DEFAULT_DARK ? 1 : 0) != 0;
        s_pc_boot = st.GetInt("boot", PC_BOOT_PAGE);
    }
    if (s_pc_boot < 0 || s_pc_boot > 1) s_pc_boot = 0;
    ESP_LOGI("ScreenPanel", "launcher build dark=%d boot=%d", s_pc_dark ? 1 : 0, s_pc_boot);

    s_pc_root = lv_obj_create(screen);
    lv_obj_set_size(s_pc_root, LV_HOR_RES, LV_VER_RES);
    lv_obj_set_pos(s_pc_root, 0, 0);
    lv_obj_set_style_bg_opa(s_pc_root, LV_OPA_TRANSP, 0);
    lv_obj_set_style_border_width(s_pc_root, 0, 0);
    lv_obj_set_style_pad_all(s_pc_root, 0, 0);
    lv_obj_set_style_radius(s_pc_root, 0, 0);
    lv_obj_remove_flag(s_pc_root, LV_OBJ_FLAG_SCROLLABLE);
    lv_obj_remove_flag(s_pc_root, LV_OBJ_FLAG_CLICKABLE);

    if (PC_USE_WALL) {
        s_pc_wall = lv_image_create(s_pc_root);
        lv_image_set_src(s_pc_wall, &s_pc_wall_dsc);
        lv_obj_set_pos(s_pc_wall, 0, 0);
        lv_obj_remove_flag(s_pc_wall, LV_OBJ_FLAG_CLICKABLE);
    }

    s_pc_scrim = lv_obj_create(s_pc_root);
    lv_obj_set_size(s_pc_scrim, LV_HOR_RES, LV_VER_RES);
    lv_obj_set_pos(s_pc_scrim, 0, 0);
    lv_obj_set_style_radius(s_pc_scrim, 0, 0);
    lv_obj_set_style_border_width(s_pc_scrim, 0, 0);
    lv_obj_remove_flag(s_pc_scrim, LV_OBJ_FLAG_SCROLLABLE);
    lv_obj_remove_flag(s_pc_scrim, LV_OBJ_FLAG_CLICKABLE);

    for (int i = 0; i < 6; i++) {
        s_pc_page[i] = lv_obj_create(s_pc_root);
        lv_obj_set_size(s_pc_page[i], LV_HOR_RES, LV_VER_RES);
        lv_obj_set_pos(s_pc_page[i], 0, 0);
        lv_obj_set_style_bg_opa(s_pc_page[i], LV_OPA_TRANSP, 0);
        lv_obj_set_style_border_width(s_pc_page[i], 0, 0);
        lv_obj_set_style_pad_all(s_pc_page[i], 0, 0);
        lv_obj_set_style_radius(s_pc_page[i], 0, 0);
        lv_obj_remove_flag(s_pc_page[i], LV_OBJ_FLAG_SCROLLABLE);
        lv_obj_add_flag(s_pc_page[i], LV_OBJ_FLAG_CLICKABLE);
        // 设置页有自己的返回按钮，不参与"点空白翻页"
        if (i != 3) lv_obj_add_event_cb(s_pc_page[i], pc_page_click_cb, LV_EVENT_CLICKED, nullptr);
    }
    // 设置页没有翻页回调，但熄屏后点空白也要能亮起来
    lv_obj_add_event_cb(s_pc_page[3], pc_wake_only_cb, LV_EVENT_CLICKED, nullptr);

    // ---- 页0：磁贴桌面 ----
    lv_obj_t* brand = lv_label_create(s_pc_page[0]);
    lv_label_set_text(brand, "小智");
    lv_obj_set_style_text_color(brand, lv_color_hex(0xFFFFFF), 0);
    lv_obj_set_pos(brand, 12, 4);

    for (int i = 0; i < 6; i++) {
        // 横屏 3×2 网格：一排三个、共两排
        // 屏 320 宽：6 + 100 + 4 + 100 + 4 + 100 + 6 = 320
        // 屏 240 高：28(顶栏) + 98 + 10 + 98 + 6 = 240
        int x = 6 + (i % 3) * 104;
        int y = 28 + (i / 3) * 108;
        int w = 100, h = 98;
        s_pc_tile[i] = lv_obj_create(s_pc_page[0]);
        lv_obj_set_size(s_pc_tile[i], w, h);
        lv_obj_set_pos(s_pc_tile[i], x, y);
        lv_obj_set_style_radius(s_pc_tile[i], 13, 0);
        lv_obj_set_style_border_width(s_pc_tile[i], 2, 0);
        lv_obj_set_style_border_color(s_pc_tile[i], lv_color_hex(PC_ACCENT[i]), 0);
        lv_obj_set_style_pad_all(s_pc_tile[i], 0, 0);
        lv_obj_remove_flag(s_pc_tile[i], LV_OBJ_FLAG_SCROLLABLE);
        lv_obj_add_flag(s_pc_tile[i], LV_OBJ_FLAG_CLICKABLE);

        lv_obj_t* ic = lv_image_create(s_pc_tile[i]);
        lv_image_set_src(ic, &s_pc_icon_dsc[PC_TILE_ICON[i]]);
        // 图标资源本身就是 34x34（make_assets.py 里缩好的），这里不做缩放：
        // lv_image_set_scale 依赖软件变换，跨 LVGL 版本行为不一致，能不用就不用。
        lv_obj_align(ic, LV_ALIGN_TOP_MID, 0, 20);
        lv_obj_remove_flag(ic, LV_OBJ_FLAG_CLICKABLE);

        s_pc_tile_txt[i] = lv_label_create(s_pc_tile[i]);
        lv_label_set_text(s_pc_tile_txt[i], PC_TILE_NAME[i]);
        lv_obj_align(s_pc_tile_txt[i], LV_ALIGN_TOP_MID, 0, 60);

        lv_obj_add_event_cb(s_pc_tile[i], pc_action_cb, LV_EVENT_CLICKED,
                            (void*)(intptr_t)PC_TILE_CODE[i]);
    }

    // ---- 页1：大号时钟 ----
    {
        int total = 5 * PC_DIGIT_W + 4 * 2;
        int x0 = (LV_HOR_RES - total) / 2;
        for (int i = 0; i < 5; i++) {
            s_pc_clock_img[i] = lv_image_create(s_pc_page[1]);
            lv_image_set_src(s_pc_clock_img[i], &s_pc_digit_dsc[i == 2 ? 10 : 0]);
            lv_obj_set_pos(s_pc_clock_img[i], x0 + i * (PC_DIGIT_W + 2), 74);
            lv_obj_remove_flag(s_pc_clock_img[i], LV_OBJ_FLAG_CLICKABLE);
        }
        s_pc_date = lv_label_create(s_pc_page[1]);
        lv_label_set_text(s_pc_date, "--月--日");
        lv_obj_align(s_pc_date, LV_ALIGN_TOP_MID, 0, 132);

        s_pc_slogan_lbl = lv_label_create(s_pc_page[1]);
        lv_label_set_text(s_pc_slogan_lbl, PC_SLOGAN);
        lv_obj_align(s_pc_slogan_lbl, LV_ALIGN_TOP_MID, 0, 160);
    }

    // ---- 页2：状态信息 ----
    s_pc_info = lv_label_create(s_pc_page[2]);
    lv_obj_set_width(s_pc_info, LV_HOR_RES - 28);
    lv_obj_set_style_text_align(s_pc_info, LV_TEXT_ALIGN_LEFT, 0);
    lv_label_set_long_mode(s_pc_info, LV_LABEL_LONG_WRAP);
    lv_label_set_text(s_pc_info, "读取中…");
    lv_obj_set_pos(s_pc_info, 14, 40);

    // ---- 页3：设置 ----
    s_pc_title = lv_label_create(s_pc_page[3]);
    lv_label_set_text(s_pc_title, "<  设置");
    lv_obj_set_pos(s_pc_title, 12, 8);
    lv_obj_add_flag(s_pc_title, LV_OBJ_FLAG_CLICKABLE);
    lv_obj_add_event_cb(s_pc_title, pc_action_cb, LV_EVENT_CLICKED, (void*)(intptr_t)12);

    {
        const char* names[3] = {"启动界面", "配色", "设备信息"};
        const char* vals[3] = {"主界面", "暗色", ">"};
        int codes[3] = {10, 11, 2};
        for (int i = 0; i < 3; i++) {
            s_pc_row[i] = lv_obj_create(s_pc_page[3]);
            lv_obj_set_size(s_pc_row[i], LV_HOR_RES - 20, 40);
            lv_obj_set_pos(s_pc_row[i], 10, 44 + i * 46);
            lv_obj_set_style_radius(s_pc_row[i], 10, 0);
            lv_obj_set_style_border_width(s_pc_row[i], 1, 0);
            lv_obj_set_style_border_color(s_pc_row[i], lv_color_hex(0x5A6E8C), 0);
            lv_obj_set_style_pad_all(s_pc_row[i], 0, 0);
            lv_obj_remove_flag(s_pc_row[i], LV_OBJ_FLAG_SCROLLABLE);
            lv_obj_add_flag(s_pc_row[i], LV_OBJ_FLAG_CLICKABLE);

            s_pc_row_txt[i] = lv_label_create(s_pc_row[i]);
            lv_label_set_text(s_pc_row_txt[i], names[i]);
            lv_obj_align(s_pc_row_txt[i], LV_ALIGN_LEFT_MID, 14, 0);

            s_pc_row_val[i] = lv_label_create(s_pc_row[i]);
            lv_label_set_text(s_pc_row_val[i], vals[i]);
            lv_obj_align(s_pc_row_val[i], LV_ALIGN_RIGHT_MID, -14, 0);

            lv_obj_add_event_cb(s_pc_row[i], pc_action_cb, LV_EVENT_CLICKED,
                                (void*)(intptr_t)codes[i]);
        }
    }

    // ---- 页4：电脑运行信息（数据由电脑端经串口下发） ----
    {
        s_pc_pctitle = lv_label_create(s_pc_page[4]);
        lv_label_set_text(s_pc_pctitle, "<  电脑");
        lv_obj_set_pos(s_pc_pctitle, 12, 8);
        lv_obj_add_flag(s_pc_pctitle, LV_OBJ_FLAG_CLICKABLE);
        lv_obj_add_event_cb(s_pc_pctitle, pc_action_cb, LV_EVENT_CLICKED, (void*)(intptr_t)12);

        s_pc_pclink = lv_label_create(s_pc_page[4]);
        lv_label_set_text(s_pc_pclink, "等待电脑");
        lv_obj_align(s_pc_pclink, LV_ALIGN_TOP_RIGHT, -12, 10);

        const char* names[PC_PC_ROWS] = {
            "CPU", "内存", "磁盘", "网络 下/上", "CPU 温度", "芯片温度"
        };
        for (int i = 0; i < PC_PC_ROWS; i++) {
            s_pc_pcrow[i] = lv_obj_create(s_pc_page[4]);
            lv_obj_set_size(s_pc_pcrow[i], LV_HOR_RES - 20, 30);
            lv_obj_set_pos(s_pc_pcrow[i], 10, 36 + i * 32);
            lv_obj_set_style_radius(s_pc_pcrow[i], 8, 0);
            lv_obj_set_style_border_width(s_pc_pcrow[i], 1, 0);
            lv_obj_set_style_pad_all(s_pc_pcrow[i], 0, 0);
            lv_obj_remove_flag(s_pc_pcrow[i], LV_OBJ_FLAG_SCROLLABLE);
            lv_obj_remove_flag(s_pc_pcrow[i], LV_OBJ_FLAG_CLICKABLE);

            s_pc_pcname[i] = lv_label_create(s_pc_pcrow[i]);
            lv_label_set_text(s_pc_pcname[i], names[i]);
            lv_obj_align(s_pc_pcname[i], LV_ALIGN_LEFT_MID, 10, 0);

            s_pc_pcval[i] = lv_label_create(s_pc_pcrow[i]);
            lv_label_set_text(s_pc_pcval[i], "--");
            lv_obj_align(s_pc_pcval[i], LV_ALIGN_RIGHT_MID, -10, 0);
        }
    }

    // ---- 页5：闹钟 / 定时提醒 ----
    {
        // 顶部时间用小号数字（21x32），比大钟页矮 14 像素，
        // 省下来的高度全部给下面三行闹钟。
        int total = 5 * PC_DIGIT_SM_W + 4 * 2;
        int x0 = (LV_HOR_RES - total) / 2;
        for (int i = 0; i < 5; i++) {
            s_pc_alm_big[i] = lv_image_create(s_pc_page[5]);
            lv_image_set_src(s_pc_alm_big[i], &s_pc_digit_sm_dsc[i == 2 ? 10 : 0]);
            lv_obj_set_pos(s_pc_alm_big[i], x0 + i * (PC_DIGIT_SM_W + 2), 2);
            lv_obj_remove_flag(s_pc_alm_big[i], LV_OBJ_FLAG_CLICKABLE);
        }
        s_pc_alm_date = lv_label_create(s_pc_page[5]);
        lv_label_set_text(s_pc_alm_date, "正在对时…");
        lv_obj_align(s_pc_alm_date, LV_ALIGN_TOP_MID, 0, 36);

        for (int i = 0; i < PC_ALM_N; i++) {
            s_pc_alm_row[i] = lv_obj_create(s_pc_page[5]);
            lv_obj_set_size(s_pc_alm_row[i], 300, 40);
            lv_obj_set_pos(s_pc_alm_row[i], 10, 58 + i * 44);
            lv_obj_set_style_radius(s_pc_alm_row[i], 9, 0);
            lv_obj_set_style_border_width(s_pc_alm_row[i], 1, 0);
            lv_obj_set_style_pad_all(s_pc_alm_row[i], 0, 0);
            lv_obj_remove_flag(s_pc_alm_row[i], LV_OBJ_FLAG_SCROLLABLE);
            lv_obj_add_flag(s_pc_alm_row[i], LV_OBJ_FLAG_CLICKABLE);
            lv_obj_add_event_cb(s_pc_alm_row[i], pc_alm_row_cb, LV_EVENT_CLICKED,
                                (void*)(intptr_t)i);

            s_pc_alm_t[i] = lv_label_create(s_pc_alm_row[i]);
            lv_label_set_text(s_pc_alm_t[i], "07:00");
            lv_obj_align(s_pc_alm_t[i], LV_ALIGN_LEFT_MID, 12, 0);
            lv_obj_add_flag(s_pc_alm_t[i], LV_OBJ_FLAG_CLICKABLE);
            lv_obj_add_event_cb(s_pc_alm_t[i], pc_alm_time_cb, LV_EVENT_CLICKED,
                                (void*)(intptr_t)i);
            lv_obj_add_event_cb(s_pc_alm_t[i], pc_alm_time_cb, LV_EVENT_LONG_PRESSED,
                                (void*)(intptr_t)i);

            s_pc_alm_r[i] = lv_label_create(s_pc_alm_row[i]);
            lv_label_set_text(s_pc_alm_r[i], "工作日");
            lv_obj_align(s_pc_alm_r[i], LV_ALIGN_LEFT_MID, 80, 0);

            s_pc_alm_s[i] = lv_label_create(s_pc_alm_row[i]);
            lv_label_set_text(s_pc_alm_s[i], "关");
            lv_obj_align(s_pc_alm_s[i], LV_ALIGN_RIGHT_MID, -14, 0);
        }

        {
            const char* cn[3] = {"25 分", "10 分", "5 分"};
            int cm[3] = {25, 10, 5};
            for (int i = 0; i < 3; i++) {
                lv_obj_t* b = lv_obj_create(s_pc_page[5]);
                lv_obj_set_size(b, 96, 30);
                lv_obj_set_pos(b, 10 + i * 100, 190);
                lv_obj_set_style_radius(b, 9, 0);
                lv_obj_set_style_border_width(b, 1, 0);
                lv_obj_set_style_border_color(b, lv_color_hex(0xFF6B6B), 0);
                lv_obj_set_style_pad_all(b, 0, 0);
                lv_obj_remove_flag(b, LV_OBJ_FLAG_SCROLLABLE);
                lv_obj_add_flag(b, LV_OBJ_FLAG_CLICKABLE);
                lv_obj_add_event_cb(b, pc_cd_btn_cb, LV_EVENT_CLICKED,
                                    (void*)(intptr_t)cm[i]);
                lv_obj_t* lb = lv_label_create(b);
                lv_label_set_text(lb, cn[i]);
                lv_obj_align(lb, LV_ALIGN_CENTER, 0, 0);
            }
        }

        s_pc_alm_hint = lv_label_create(s_pc_page[5]);
        lv_label_set_text(s_pc_alm_hint, "点时间 +10 分 · 长按 +60 分");
        lv_obj_align(s_pc_alm_hint, LV_ALIGN_BOTTOM_MID, 0, -4);

        // 响铃遮罩：轻触关闭，长按贪睡
        s_pc_alm_ov = lv_obj_create(s_pc_page[5]);
        lv_obj_set_size(s_pc_alm_ov, LV_HOR_RES, LV_VER_RES);
        lv_obj_set_pos(s_pc_alm_ov, 0, 0);
        lv_obj_set_style_bg_color(s_pc_alm_ov, lv_color_hex(0x1A1114), 0);
        lv_obj_set_style_bg_opa(s_pc_alm_ov, LV_OPA_COVER, 0);
        lv_obj_set_style_border_width(s_pc_alm_ov, 0, 0);
        lv_obj_set_style_radius(s_pc_alm_ov, 0, 0);
        lv_obj_set_style_pad_all(s_pc_alm_ov, 0, 0);
        lv_obj_remove_flag(s_pc_alm_ov, LV_OBJ_FLAG_SCROLLABLE);
        lv_obj_add_flag(s_pc_alm_ov, LV_OBJ_FLAG_CLICKABLE);
        lv_obj_add_flag(s_pc_alm_ov, LV_OBJ_FLAG_HIDDEN);
        lv_obj_add_event_cb(s_pc_alm_ov, pc_alm_ov_cb, LV_EVENT_CLICKED, nullptr);
        lv_obj_add_event_cb(s_pc_alm_ov, pc_alm_ov_cb, LV_EVENT_LONG_PRESSED, nullptr);

        s_pc_alm_ov_t = lv_label_create(s_pc_alm_ov);
        lv_label_set_text(s_pc_alm_ov_t, "07:00");
        lv_obj_align(s_pc_alm_ov_t, LV_ALIGN_CENTER, 0, -22);

        lv_obj_t* ovh = lv_label_create(s_pc_alm_ov);
        lv_label_set_text(ovh, "时间到");
        lv_obj_align(ovh, LV_ALIGN_CENTER, 0, 10);

        lv_obj_t* ovtip = lv_label_create(s_pc_alm_ov);
        lv_label_set_text(ovtip, "轻触关闭 · 长按贪睡 5 分钟");
        lv_obj_align(ovtip, LV_ALIGN_CENTER, 0, 44);
    }

    pc_alm_load();
    pc_apply_palette();
    pc_refresh_settings();
    pc_show_page(s_pc_boot == 1 ? 1 : 0);
    pc_host_rx_start();
    pc_chip_temp_start();
    s_pc_act_us = esp_timer_get_time();
    ESP_LOGI("ScreenPanel", "gesture %s dim=%ds", PC_IMU ? "on" : "off", PC_DIM_SEC);
    lv_timer_create(pc_timer_cb, 500, nullptr);
}
// === __MARK__ END ===
'''


def patch_imu():
    """往板级文件注入 QMI8658 采集任务，并在构造函数里启动它。"""
    if not os.path.exists(BOARD_CC):
        print("[警告] 找不到 %s，跳过六轴补丁" % BOARD_CC)
        return

    with open(BOARD_CC, encoding="utf-8") as f:
        src = f.read()

    if IMU_MARK in src:
        print("[跳过] 板级文件已含六轴补丁")
        return

    anchor = "class LichuangDevBoard : public WifiBoard {"
    if anchor not in src:
        print("[警告] 找不到板级类定义锚点，跳过六轴补丁")
        return
    src = src.replace(anchor, IMU_CODE + "\n" + anchor, 1)

    # 构造函数末尾启动：这时候 I2C 总线、摄像头（也用这条总线）都初始化完了
    ctor_anchor = "        InitializeTools();\n"
    if ctor_anchor not in src:
        print("[警告] 找不到构造函数锚点，六轴采集不会启动")
        return
    src = src.replace(ctor_anchor, ctor_anchor + "        qmi8658_start(i2c_bus_);\n", 1)

    with open(BOARD_CC, "w", encoding="utf-8") as f:
        f.write(src)
    print("[完成] 六轴补丁已注入板级文件（拿起亮屏/摇一摇切页/翻转静音）")


def main():
    cfg = load_config()
    print("[补丁] 界面配置: theme=%s boot=%s wallpaper=%s slogan=%s imu=%s dim=%s" %
          (cfg["theme"], cfg["boot"], cfg["wallpaper"], cfg["slogan"], cfg["imu"], cfg["dim"]))

    dark = "true" if cfg["theme"].strip().lower() != "light" else "false"
    boot = 1 if cfg["boot"].strip().lower() in ("clock", "时钟", "1") else 0
    try:
        dim_sec = int(cfg["dim"])
    except (TypeError, ValueError):
        dim_sec = 20
    if dim_sec < 0:
        dim_sec = 0
    if dim_sec > 600:
        dim_sec = 600

    patch_imu()

    code = (TEMPLATE
            .replace("__MARK__", MARK)
            .replace("__SLOGAN__", cstr(cfg["slogan"]))
            .replace("__SHOW_IP__", flag(cfg["show_ip"]))
            .replace("__SHOW_SIGNAL__", flag(cfg["show_signal"]))
            .replace("__SHOW_HEAP__", flag(cfg["show_heap"]))
            .replace("__SHOW_UPTIME__", flag(cfg["show_uptime"]))
            .replace("__SHOW_STATE__", flag(cfg["show_state"]))
            .replace("__WALL__", flag(cfg["wallpaper"]))
            .replace("__DARK__", dark)
            .replace("__IMU__", flag(cfg["imu"]))
            .replace("__DIM__", str(dim_sec))
            .replace("__BOOT__", str(boot)))

    assets = None
    for cand in ASSETS_CANDIDATES:
        if os.path.exists(cand):
            assets = cand
            break
    if assets is None:
        print("[错误] 找不到 pc_assets.h（先运行 make_assets.py 生成）")
        sys.exit(1)
    print("[补丁] 使用图片资源: " + assets)

    if not os.path.exists(LCD):
        print("[错误] 找不到 " + LCD)
        sys.exit(1)

    with open(LCD, encoding="utf-8") as f:
        src = f.read()

    if MARK in src:
        print("[跳过] lcd_display.cc 已含界面补丁")
        return

    decl_anchor = "LV_FONT_DECLARE(font_noto_emoji_30_4);"
    if decl_anchor not in src:
        print("[错误] 找不到字体声明锚点")
        sys.exit(1)
    src = src.replace(decl_anchor, decl_anchor + code, 1)

    # 注意：lcd_display.cc 里的 SetupUI 有两份（#if CONFIG_USE_WECHAT_MESSAGE_STYLE 的两支），
    # 只有一支会被真正编译。所以两个分支都要插入调用，否则可能插进没启用的那份里，
    # 结果就是"补丁注入了、编译也过了，但界面根本没出现"（踩过）。
    end_anchor = "    lv_obj_center(low_battery_label_);"
    n_hit = src.count(end_anchor)
    if n_hit == 0:
        print("[错误] 找不到 SetupUI 结尾锚点")
        sys.exit(1)
    src = src.replace(end_anchor, "    pc_build_ui(screen);\n" + end_anchor)
    print("[补丁] SetupUI 分支插入调用 %d 处（只有启用的那支会生效）" % n_hit)

    with open(LCD, "w", encoding="utf-8") as f:
        f.write(src)
    print("[完成] 触摸界面补丁已注入（%d 行）" % len(code.split("\n")))


if __name__ == "__main__":
    main()
