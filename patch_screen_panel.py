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
ASSETS_CANDIDATES = ["main/display/pc_assets.h", "pc_assets.h"]
CONFIG = "screen_config.txt"
MARK = "PC_SCREEN_PANEL_PATCH"


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


TEMPLATE = r'''
// === __MARK__：触摸界面（磁贴桌面/时钟/状态/设置）===
#include "pc_assets.h"
#include <stdio.h>
#include <string.h>
#include <stdint.h>
#include <time.h>
#include "application.h"
#include "settings.h"
#include "esp_timer.h"
#include "esp_system.h"
#include "esp_netif.h"
#include "esp_wifi.h"

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

// --- 对象 ---
static lv_obj_t* s_pc_root = nullptr;
static lv_obj_t* s_pc_wall = nullptr;
static lv_obj_t* s_pc_scrim = nullptr;
static lv_obj_t* s_pc_page[4] = {nullptr, nullptr, nullptr, nullptr};
static lv_obj_t* s_pc_dot[3] = {nullptr, nullptr, nullptr};
static lv_obj_t* s_pc_tile[4] = {nullptr, nullptr, nullptr, nullptr};
static lv_obj_t* s_pc_tile_txt[4] = {nullptr, nullptr, nullptr, nullptr};
static lv_obj_t* s_pc_row[3] = {nullptr, nullptr, nullptr};
static lv_obj_t* s_pc_row_txt[3] = {nullptr, nullptr, nullptr};
static lv_obj_t* s_pc_row_val[3] = {nullptr, nullptr, nullptr};
static lv_obj_t* s_pc_title = nullptr;
static lv_obj_t* s_pc_clock_img[5] = {nullptr, nullptr, nullptr, nullptr, nullptr};
static lv_obj_t* s_pc_date = nullptr;
static lv_obj_t* s_pc_slogan_lbl = nullptr;
static lv_obj_t* s_pc_info = nullptr;

static lv_image_dsc_t s_pc_wall_dsc;
static lv_image_dsc_t s_pc_icon_dsc[4];
static lv_image_dsc_t s_pc_digit_dsc[PC_DIGIT_COUNT];

static int  s_pc_page_idx = 0;
static bool s_pc_dark = PC_DEFAULT_DARK;
static int  s_pc_boot = PC_BOOT_PAGE;
static bool s_pc_manual_hide = false;
static int  s_pc_last_digit[5] = {-1, -1, -1, -1, -1};
static bool s_pc_assets_ready = false;

static const uint32_t PC_ACCENT[4] = {0x59A7FF, 0x38D6E0, 0x4ADE80, 0xFFB020};
static const char* PC_TILE_NAME[4] = {"小智AI", "时钟", "状态", "设置"};
static const int PC_TILE_ICON[4] = {0, 1, 2, 3};

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
    for (int i = 0; i < 4; i++) {
        uint32_t one = (uint32_t)(PC_ICON_W * PC_ICON_H * 3);
        pc_make_dsc(&s_pc_icon_dsc[i], pc_icons_map + one * (uint32_t)i, one,
                    PC_ICON_W, PC_ICON_H, PC_ICON_W * 2, LV_COLOR_FORMAT_RGB565A8);
    }
    for (int i = 0; i < PC_DIGIT_COUNT; i++) {
        uint32_t one = (uint32_t)(PC_DIGIT_W * PC_DIGIT_H * 3);
        pc_make_dsc(&s_pc_digit_dsc[i], pc_digits_map + one * (uint32_t)i, one,
                    PC_DIGIT_W, PC_DIGIT_H, PC_DIGIT_W * 2, LV_COLOR_FORMAT_RGB565A8);
    }
}

static void pc_show_page(int p) {
    if (p < 0) p = 0;
    if (p > 3) p = 3;
    s_pc_page_idx = p;
    for (int i = 0; i < 4; i++) {
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

    if (s_pc_scrim) {
        lv_obj_set_style_bg_color(s_pc_scrim, scr, 0);
        lv_obj_set_style_bg_opa(s_pc_scrim, s_pc_dark ? 120 : 90, 0);
    }
    for (int i = 0; i < 4; i++) {
        if (s_pc_tile[i])     lv_obj_set_style_bg_color(s_pc_tile[i], tile, 0);
        if (s_pc_tile_txt[i]) lv_obj_set_style_text_color(s_pc_tile_txt[i], txt, 0);
    }
    for (int i = 0; i < 3; i++) {
        if (s_pc_row[i])     lv_obj_set_style_bg_color(s_pc_row[i], row, 0);
        if (s_pc_row_txt[i]) lv_obj_set_style_text_color(s_pc_row_txt[i], txt, 0);
        if (s_pc_row_val[i]) lv_obj_set_style_text_color(s_pc_row_val[i], blue, 0);
    }
    if (s_pc_title)      lv_obj_set_style_text_color(s_pc_title, txt, 0);
    if (s_pc_date)       lv_obj_set_style_text_color(s_pc_date, txt, 0);
    if (s_pc_slogan_lbl) lv_obj_set_style_text_color(s_pc_slogan_lbl, sub, 0);
    if (s_pc_info)       lv_obj_set_style_text_color(s_pc_info, txt, 0);
    for (int i = 0; i < 3; i++) {
        if (s_pc_dot[i]) lv_obj_set_style_bg_color(s_pc_dot[i], txt, 0);
    }
}

static void pc_action_cb(lv_event_t* e) {
    if (lv_event_get_code(e) != LV_EVENT_CLICKED) return;
    int code = (int)(intptr_t)lv_event_get_user_data(e);
    Settings st("screen_panel", true);
    switch (code) {
        case 1: pc_show_page(1); break;
        case 2: pc_show_page(2); break;
        case 3: pc_show_page(3); break;
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
    int p = s_pc_page_idx;
    if (p == 0)      pc_show_page(1);
    else if (p == 1) pc_show_page(2);
    else if (p == 2) pc_show_page(0);
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

static void pc_timer_cb(lv_timer_t* /*t*/) {
    if (s_pc_root == nullptr) return;

    bool idle = (Application::GetInstance().GetDeviceState() == kDeviceStateIdle);
    if (!idle) s_pc_manual_hide = false;
    bool show = idle && !s_pc_manual_hide;
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

    for (int i = 0; i < 4; i++) {
        s_pc_page[i] = lv_obj_create(s_pc_root);
        lv_obj_set_size(s_pc_page[i], LV_HOR_RES, LV_VER_RES);
        lv_obj_set_pos(s_pc_page[i], 0, 0);
        lv_obj_set_style_bg_opa(s_pc_page[i], LV_OPA_TRANSP, 0);
        lv_obj_set_style_border_width(s_pc_page[i], 0, 0);
        lv_obj_set_style_pad_all(s_pc_page[i], 0, 0);
        lv_obj_set_style_radius(s_pc_page[i], 0, 0);
        lv_obj_remove_flag(s_pc_page[i], LV_OBJ_FLAG_SCROLLABLE);
        lv_obj_add_flag(s_pc_page[i], LV_OBJ_FLAG_CLICKABLE);
        if (i < 3) lv_obj_add_event_cb(s_pc_page[i], pc_page_click_cb, LV_EVENT_CLICKED, nullptr);
    }

    // ---- 页0：磁贴桌面 ----
    lv_obj_t* brand = lv_label_create(s_pc_page[0]);
    lv_label_set_text(brand, "小智");
    lv_obj_set_style_text_color(brand, lv_color_hex(0xFFFFFF), 0);
    lv_obj_set_pos(brand, 12, 4);

    for (int i = 0; i < 4; i++) {
        int col = i % 2, row = i / 2;
        s_pc_tile[i] = lv_obj_create(s_pc_page[0]);
        lv_obj_set_size(s_pc_tile[i], 144, 84);
        lv_obj_set_pos(s_pc_tile[i], 8 + col * 152, 30 + row * 92);
        lv_obj_set_style_radius(s_pc_tile[i], 14, 0);
        lv_obj_set_style_border_width(s_pc_tile[i], 2, 0);
        lv_obj_set_style_border_color(s_pc_tile[i], lv_color_hex(PC_ACCENT[i]), 0);
        lv_obj_set_style_pad_all(s_pc_tile[i], 4, 0);
        lv_obj_set_style_pad_row(s_pc_tile[i], 2, 0);
        lv_obj_remove_flag(s_pc_tile[i], LV_OBJ_FLAG_SCROLLABLE);
        lv_obj_add_flag(s_pc_tile[i], LV_OBJ_FLAG_CLICKABLE);
        lv_obj_set_flex_flow(s_pc_tile[i], LV_FLEX_FLOW_COLUMN);
        lv_obj_set_flex_align(s_pc_tile[i], LV_FLEX_ALIGN_CENTER,
                             LV_FLEX_ALIGN_CENTER, LV_FLEX_ALIGN_CENTER);

        lv_obj_t* ic = lv_image_create(s_pc_tile[i]);
        lv_image_set_src(ic, &s_pc_icon_dsc[PC_TILE_ICON[i]]);
        lv_obj_remove_flag(ic, LV_OBJ_FLAG_CLICKABLE);

        s_pc_tile_txt[i] = lv_label_create(s_pc_tile[i]);
        lv_label_set_text(s_pc_tile_txt[i], PC_TILE_NAME[i]);

        int code = (i == 0) ? 4 : i;
        lv_obj_add_event_cb(s_pc_tile[i], pc_action_cb, LV_EVENT_CLICKED, (void*)(intptr_t)code);
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

    // ---- 页面小圆点 ----
    for (int i = 0; i < 3; i++) {
        s_pc_dot[i] = lv_obj_create(s_pc_page[0]);
        lv_obj_set_size(s_pc_dot[i], i == 0 ? 12 : 6, 6);
        lv_obj_set_pos(s_pc_dot[i], LV_HOR_RES / 2 - 18 + i * 14, 226);
        lv_obj_set_style_radius(s_pc_dot[i], LV_RADIUS_CIRCLE, 0);
        lv_obj_set_style_border_width(s_pc_dot[i], 0, 0);
        lv_obj_set_style_bg_opa(s_pc_dot[i], i == 0 ? LV_OPA_COVER : LV_OPA_40, 0);
        lv_obj_remove_flag(s_pc_dot[i], LV_OBJ_FLAG_SCROLLABLE);
        lv_obj_remove_flag(s_pc_dot[i], LV_OBJ_FLAG_CLICKABLE);
    }

    pc_apply_palette();
    pc_refresh_settings();
    pc_show_page(s_pc_boot == 1 ? 1 : 0);
    lv_timer_create(pc_timer_cb, 500, nullptr);
}
// === __MARK__ END ===
'''


def main():
    cfg = load_config()
    print("[补丁] 界面配置: theme=%s boot=%s wallpaper=%s slogan=%s" %
          (cfg["theme"], cfg["boot"], cfg["wallpaper"], cfg["slogan"]))

    dark = "true" if cfg["theme"].strip().lower() != "light" else "false"
    boot = 1 if cfg["boot"].strip().lower() in ("clock", "时钟", "1") else 0

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
