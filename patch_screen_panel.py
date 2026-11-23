# -*- coding: utf-8 -*-
"""
给小智 v2.6.0 注入「可配置屏幕面板」补丁（立创实战派 default/SpiLcdDisplay）。

替代旧的 patch_screen_info.py（二选一，不能同时装）。

功能：
  1) 三个待机页面：① 时钟  ② 状态(实用信息)  ③ 动图(表情轮播 + 自定义标语)
  2) 触摸屏右下角两个按钮：「下一页」「主题」，点一下即切换
  3) 明暗主题即时切换（只改本面板 + 屏幕底色，不碰官方 SetTheme，避免加锁冲突）
  4) NVS 断电记忆：页面、主题重启后保持
  5) 配置来自 screen_config.txt：标语、表情序列、切换速度、默认页、默认主题、信息项开关

实现说明（只改 lcd_display.cc，不碰别的补丁文件）：
  - 锚点与已编译成功的 patch_screen_info.py 完全一致，降低风险
  - 不调用官方 SetTheme（它在 LVGL 事件回调里可能造成锁重入），改为自行设置颜色
  - 所有 LVGL 操作都在事件回调 / lv_timer 上下文内（已持锁），不额外加锁

用法（云端工作流 xiaozhi/ 目录，在 patch_silence / patch_ptt 之后）：
    python patch_screen_panel.py
"""

import os
import sys

LCD = "main/display/lcd_display.cc"
CFG = "screen_config.txt"
MARK = "PC_SCREEN_PANEL_PATCH"


# ---------------- 配置读取 ----------------
DEFAULTS = {
    "theme": "dark",
    "default_page": "1",
    "show_seconds": "1",
    "show_ip": "1",
    "show_heap": "1",
    "show_uptime": "1",
    "show_state": "1",
    "show_signal": "1",
    "slogan": "你好，我是小智",
    "emoji_interval": "900",
    "emojis": "😀,😊,😄,🤖,👀,🌟,🌈",
}


def load_config():
    cfg = dict(DEFAULTS)
    if os.path.exists(CFG):
        with open(CFG, encoding="utf-8-sig") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                cfg[k.strip()] = v.strip()
    return cfg


def cstr(s):
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


def as_bool(s):
    return "true" if str(s).strip() in ("1", "true", "True", "yes", "on") else "false"


def as_int(s, lo, hi, default):
    try:
        v = int(str(s).strip())
    except Exception:
        return default
    return max(lo, min(hi, v))


# ---------------- 补丁主流程 ----------------
def must_replace(src, old, new, label):
    if old not in src:
        print(f"[错误] 找不到锚点：{label}")
        sys.exit(1)
    return src.replace(old, new, 1)


def build_helper(cfg):
    emojis = [e.strip() for e in cfg["emojis"].split(",") if e.strip()]
    if not emojis:
        emojis = ["😀", "🤖", "🌟"]
    emoji_arr = "{" + ", ".join(cstr(e) for e in emojis) + "}"

    # default_page 配置是 1..3，代码里用 0..2
    page = as_int(cfg["default_page"], 1, 3, 1) - 1
    dark = "true" if cfg["theme"].strip().lower() == "dark" else "false"
    interval = as_int(cfg["emoji_interval"], 200, 5000, 900)

    return r'''

// === PC_SCREEN_PANEL_PATCH：可配置待机面板 ===
#include <time.h>
#include <stdio.h>
#include <string>
#include "application.h"
#include "esp_timer.h"
#include "esp_system.h"
#include "esp_netif.h"
#include "esp_wifi.h"

// --- 由电脑端 screen_config.txt 生成的配置常量 ---
static const bool  PC_SHOW_SECONDS = __SHOW_SECONDS__;
static const bool  PC_SHOW_IP      = __SHOW_IP__;
static const bool  PC_SHOW_HEAP    = __SHOW_HEAP__;
static const bool  PC_SHOW_UPTIME  = __SHOW_UPTIME__;
static const bool  PC_SHOW_STATE   = __SHOW_STATE__;
static const bool  PC_SHOW_SIGNAL  = __SHOW_SIGNAL__;
static const char* PC_SLOGAN       = __SLOGAN__;
static const uint32_t PC_EMOJI_INTERVAL = __INTERVAL__;
static const int   PC_DEFAULT_PAGE = __DEFAULT_PAGE__;   // 0=时钟 1=状态 2=动图
static const bool  PC_DEFAULT_DARK = __DEFAULT_DARK__;
static const char* PC_EMOJIS[]     = __EMOJI_ARR__;
static const int   PC_EMOJI_COUNT  = (int)(sizeof(PC_EMOJIS) / sizeof(PC_EMOJIS[0]));

// --- 运行时对象 ---
static lv_obj_t* s_pc_clock = nullptr;
static lv_obj_t* s_pc_date = nullptr;
static lv_obj_t* s_pc_status = nullptr;
static lv_obj_t* s_pc_emoji = nullptr;
static lv_obj_t* s_pc_slogan = nullptr;
static lv_obj_t* s_pc_btn_page = nullptr;
static lv_obj_t* s_pc_btn_theme = nullptr;
static lv_obj_t* s_pc_btn_page_txt = nullptr;
static lv_obj_t* s_pc_btn_theme_txt = nullptr;
static lv_obj_t* s_pc_screen = nullptr;
static lv_obj_t* s_pc_container = nullptr;
static int  s_pc_page = PC_DEFAULT_PAGE;
static bool s_pc_dark = PC_DEFAULT_DARK;
static int  s_pc_emoji_idx = 0;
static uint32_t s_pc_last_emoji_ms = 0;

static void pc_set_visible(lv_obj_t* o, bool show) {
    if (o == nullptr) return;
    if (show) lv_obj_remove_flag(o, LV_OBJ_FLAG_HIDDEN);
    else      lv_obj_add_flag(o, LV_OBJ_FLAG_HIDDEN);
}

static void pc_apply_theme() {
    if (s_pc_screen == nullptr) return;
    lv_color_t bg   = s_pc_dark ? lv_color_hex(0x121212) : lv_color_hex(0xFFFFFF);
    lv_color_t txt  = s_pc_dark ? lv_color_hex(0xFFFFFF) : lv_color_hex(0x1A1A1A);
    lv_color_t sub  = s_pc_dark ? lv_color_hex(0xBDBDBD) : lv_color_hex(0x606060);
    lv_color_t btn  = s_pc_dark ? lv_color_hex(0x3A3A3A) : lv_color_hex(0xE3E3E3);
    lv_obj_set_style_bg_color(s_pc_screen, bg, 0);
    if (s_pc_container != nullptr) lv_obj_set_style_bg_color(s_pc_container, bg, 0);
    if (s_pc_clock)  lv_obj_set_style_text_color(s_pc_clock, txt, 0);
    if (s_pc_date)   lv_obj_set_style_text_color(s_pc_date, sub, 0);
    if (s_pc_status) lv_obj_set_style_text_color(s_pc_status, txt, 0);
    if (s_pc_slogan) lv_obj_set_style_text_color(s_pc_slogan, sub, 0);
    if (s_pc_btn_page)  lv_obj_set_style_bg_color(s_pc_btn_page, btn, 0);
    if (s_pc_btn_theme) lv_obj_set_style_bg_color(s_pc_btn_theme, btn, 0);
    if (s_pc_btn_page_txt)  lv_obj_set_style_text_color(s_pc_btn_page_txt, txt, 0);
    if (s_pc_btn_theme_txt) lv_obj_set_style_text_color(s_pc_btn_theme_txt, txt, 0);
}

static void pc_btn_event_cb(lv_event_t* e) {
    if (lv_event_get_code(e) != LV_EVENT_CLICKED) return;
    lv_obj_t* tgt = lv_event_get_target(e);
    if (tgt == s_pc_btn_page) {
        s_pc_page = (s_pc_page + 1) % 3;
        s_pc_last_emoji_ms = 0;
        Settings st("screen_panel", true);
        st.SetInt("page", s_pc_page);
    } else if (tgt == s_pc_btn_theme) {
        s_pc_dark = !s_pc_dark;
        Settings st("screen_panel", true);
        st.SetString("theme", s_pc_dark ? "dark" : "light");
        pc_apply_theme();
    }
}

static void pc_timer_cb(lv_timer_t* /*timer*/) {
    if (s_pc_clock == nullptr) return;
    bool idle = (Application::GetInstance().GetDeviceState() == kDeviceStateIdle);

    // 非待机（说话/聆听/回复）时全部隐藏，不遮挡对话
    pc_set_visible(s_pc_clock,  idle && s_pc_page == 0);
    pc_set_visible(s_pc_date,   idle && s_pc_page == 0);
    pc_set_visible(s_pc_status, idle && s_pc_page == 1);
    pc_set_visible(s_pc_emoji,  idle && s_pc_page == 2);
    pc_set_visible(s_pc_slogan, idle && s_pc_page == 2);
    pc_set_visible(s_pc_btn_page, idle);
    pc_set_visible(s_pc_btn_theme, idle);
    if (!idle) return;

    uint32_t now = lv_tick_get();

    if (s_pc_page == 0) {
        // ---- 时钟页 ----
        time_t tnow = time(NULL);
        struct tm* t = localtime(&tnow);
        if (t != nullptr && t->tm_year >= 125) {  // 2025-1900，防未对时
            char b[16];
            if (PC_SHOW_SECONDS) strftime(b, sizeof(b), "%H:%M:%S", t);
            else                 strftime(b, sizeof(b), "%H:%M", t);
            lv_label_set_text(s_pc_clock, b);
            static const char* wd[7] = {"周日", "周一", "周二", "周三", "周四", "周五", "周六"};
            char d[32];
            snprintf(d, sizeof(d), "%d月%d日 %s", t->tm_mon + 1, t->tm_mday, wd[t->tm_wday]);
            lv_label_set_text(s_pc_date, d);
        }
    } else if (s_pc_page == 1) {
        // ---- 状态页（实用信息，按配置开关）----
        // 注意：main 组件开启 -Werror=frame-larger-than=1536，缓冲区务必精简
        char buf[320];
        size_t n = 0;
        if (PC_SHOW_IP) {
            char ip[16] = "未连接";
            esp_netif_t* h = esp_netif_get_handle_from_ifkey("WIFI_STA_DEF");
            esp_netif_ip_info_t ii;
            if (h != nullptr && esp_netif_get_ip_info(h, &ii) == ESP_OK && ii.ip.addr != 0) {
                snprintf(ip, sizeof(ip), IPSTR, IP2STR(&ii.ip));
            }
            n += snprintf(buf + n, sizeof(buf) - n, "IP     %s\n", ip);
        }
        if (PC_SHOW_SIGNAL) {
            wifi_ap_record_t ap;
            int rssi = -999;
            if (esp_wifi_sta_get_ap_info(&ap) == ESP_OK) rssi = ap.rssi;
            const char* sig = "未知";
            if      (rssi >= -55) sig = "强";
            else if (rssi >= -70) sig = "中";
            else if (rssi > -999) sig = "弱";
            n += snprintf(buf + n, sizeof(buf) - n, "信号   %s\n", sig);
        }
        if (PC_SHOW_HEAP) {
            uint32_t free_kb = esp_get_free_heap_size() / 1024;
            n += snprintf(buf + n, sizeof(buf) - n, "内存   %u KB 空闲\n", (unsigned)free_kb);
        }
        if (PC_SHOW_UPTIME) {
            uint32_t sec = (uint32_t)(esp_timer_get_time() / 1000000);
            n += snprintf(buf + n, sizeof(buf) - n, "运行   %02u:%02u:%02u\n",
                          sec / 3600, (sec % 3600) / 60, sec % 60);
        }
        if (PC_SHOW_STATE) {
            auto st = Application::GetInstance().GetDeviceState();
            const char* s = "连接中";
            if      (st == kDeviceStateIdle)      s = "待机";
            else if (st == kDeviceStateListening) s = "聆听中";
            else if (st == kDeviceStateSpeaking)  s = "说话中";
            n += snprintf(buf + n, sizeof(buf) - n, "状态   %s\n", s);
        }
        n += snprintf(buf + n, sizeof(buf) - n, "\n点右下方按钮切页 / 换主题");
        lv_label_set_text(s_pc_status, buf);
    } else {
        // ---- 动图页：表情轮播 + 呼吸 ----
        if (PC_EMOJI_COUNT > 0 && (s_pc_last_emoji_ms == 0 || now - s_pc_last_emoji_ms >= PC_EMOJI_INTERVAL)) {
            s_pc_last_emoji_ms = now;
            s_pc_emoji_idx = (s_pc_emoji_idx + 1) % PC_EMOJI_COUNT;
            lv_label_set_text(s_pc_emoji, PC_EMOJIS[s_pc_emoji_idx]);
        }
        // 呼吸透明度（约 1.2 秒一个周期）
        uint32_t ph = now % 1200;
        lv_opa_t opa;
        if (ph < 600) opa = (lv_opa_t)(150 + ph * 105 / 600);
        else          opa = (lv_opa_t)(255 - (ph - 600) * 105 / 600);
        lv_obj_set_style_text_opa(s_pc_emoji, opa, 0);
    }
}
// === PC_SCREEN_PANEL_PATCH END ===
'''.replace("__SHOW_SECONDS__", as_bool(cfg["show_seconds"])) \
    .replace("__SHOW_IP__", as_bool(cfg["show_ip"])) \
    .replace("__SHOW_HEAP__", as_bool(cfg["show_heap"])) \
    .replace("__SHOW_UPTIME__", as_bool(cfg["show_uptime"])) \
    .replace("__SHOW_STATE__", as_bool(cfg["show_state"])) \
    .replace("__SHOW_SIGNAL__", as_bool(cfg["show_signal"])) \
    .replace("__SLOGAN__", cstr(cfg["slogan"])) \
    .replace("__INTERVAL__", str(interval)) \
    .replace("__DEFAULT_PAGE__", str(page)) \
    .replace("__DEFAULT_DARK__", dark) \
    .replace("__EMOJI_ARR__", emoji_arr)


def build_create():
    return r'''    // === PC_SCREEN_PANEL_PATCH：创建可配置面板 ===
    s_pc_screen = screen;
    s_pc_container = container_;

    // 读取断电记忆（NVS）
    {
        Settings st("screen_panel", false);
        s_pc_page = st.GetInt("page", PC_DEFAULT_PAGE);
        std::string th = st.GetString("theme", PC_DEFAULT_DARK ? "dark" : "light");
        s_pc_dark = (th == "dark");
    }
    if (s_pc_page < 0 || s_pc_page > 2) s_pc_page = 0;
    ESP_LOGI("ScreenPanel", "page=%d theme=%s emojis=%d", s_pc_page, s_pc_dark ? "dark" : "light", PC_EMOJI_COUNT);

    // 时钟页
    s_pc_clock = lv_label_create(screen);
    lv_obj_set_style_text_font(s_pc_clock, &BUILTIN_TEXT_FONT, 0);
    lv_label_set_text(s_pc_clock, "--:--");
    lv_obj_align(s_pc_clock, LV_ALIGN_CENTER, 0, 18);

    s_pc_date = lv_label_create(screen);
    lv_obj_set_style_text_font(s_pc_date, text_font, 0);
    lv_label_set_text(s_pc_date, "");
    lv_obj_align(s_pc_date, LV_ALIGN_CENTER, 0, 54);

    // 状态页
    s_pc_status = lv_label_create(screen);
    lv_obj_set_style_text_font(s_pc_status, text_font, 0);
    lv_obj_set_width(s_pc_status, LV_HOR_RES * 0.86);
    lv_obj_set_style_text_align(s_pc_status, LV_TEXT_ALIGN_LEFT, 0);
    lv_label_set_long_mode(s_pc_status, LV_LABEL_LONG_WRAP);
    lv_label_set_text(s_pc_status, "");
    lv_obj_align(s_pc_status, LV_ALIGN_CENTER, 0, 6);

    // 动图页
    s_pc_emoji = lv_label_create(screen);
    lv_obj_set_style_text_font(s_pc_emoji, &font_noto_emoji_30_4, 0);
    lv_label_set_text(s_pc_emoji, PC_EMOJI_COUNT > 0 ? PC_EMOJIS[0] : "😀");
    lv_obj_align(s_pc_emoji, LV_ALIGN_CENTER, 0, -16);

    s_pc_slogan = lv_label_create(screen);
    lv_obj_set_style_text_font(s_pc_slogan, text_font, 0);
    lv_label_set_text(s_pc_slogan, PC_SLOGAN);
    lv_obj_align(s_pc_slogan, LV_ALIGN_CENTER, 0, 46);

    // 按钮：下一页
    s_pc_btn_page = lv_obj_create(screen);
    lv_obj_set_size(s_pc_btn_page, 60, 30);
    lv_obj_align(s_pc_btn_page, LV_ALIGN_BOTTOM_RIGHT, -10, -8);
    lv_obj_set_style_radius(s_pc_btn_page, 14, 0);
    lv_obj_set_style_pad_all(s_pc_btn_page, 0, 0);
    lv_obj_add_flag(s_pc_btn_page, LV_OBJ_FLAG_CLICKABLE);
    lv_obj_remove_flag(s_pc_btn_page, LV_OBJ_FLAG_SCROLLABLE);
    s_pc_btn_page_txt = lv_label_create(s_pc_btn_page);
    lv_label_set_text(s_pc_btn_page_txt, "下一页");
    lv_obj_set_style_text_font(s_pc_btn_page_txt, text_font, 0);
    lv_obj_center(s_pc_btn_page_txt);
    lv_obj_add_event_cb(s_pc_btn_page, pc_btn_event_cb, LV_EVENT_CLICKED, nullptr);

    // 按钮：主题
    s_pc_btn_theme = lv_obj_create(screen);
    lv_obj_set_size(s_pc_btn_theme, 52, 30);
    lv_obj_align(s_pc_btn_theme, LV_ALIGN_BOTTOM_RIGHT, -78, -8);
    lv_obj_set_style_radius(s_pc_btn_theme, 14, 0);
    lv_obj_set_style_pad_all(s_pc_btn_theme, 0, 0);
    lv_obj_add_flag(s_pc_btn_theme, LV_OBJ_FLAG_CLICKABLE);
    lv_obj_remove_flag(s_pc_btn_theme, LV_OBJ_FLAG_SCROLLABLE);
    s_pc_btn_theme_txt = lv_label_create(s_pc_btn_theme);
    lv_label_set_text(s_pc_btn_theme_txt, "主题");
    lv_obj_set_style_text_font(s_pc_btn_theme_txt, text_font, 0);
    lv_obj_center(s_pc_btn_theme_txt);
    lv_obj_add_event_cb(s_pc_btn_theme, pc_btn_event_cb, LV_EVENT_CLICKED, nullptr);

    // 应用配色 + 启动每秒刷新
    pc_apply_theme();
    lv_timer_create(pc_timer_cb, 400, nullptr);
    // === PC_SCREEN_PANEL_PATCH END ===
}'''


def main():
    cfg = load_config()
    print("[补丁] 屏幕配置: theme=%s default_page=%s slogan=%s" %
          (cfg["theme"], cfg["default_page"], cfg["slogan"]))

    if not os.path.exists(LCD):
        print("[错误] 找不到", LCD)
        sys.exit(1)

    with open(LCD, encoding="utf-8") as f:
        src = f.read()

    if MARK in src:
        print("[跳过] lcd_display.cc 已含屏幕面板补丁")
        return

    # 1) 字体声明后插入 helper（含所有 include 与配置常量）
    decl_anchor = "LV_FONT_DECLARE(font_noto_emoji_30_4);"
    if decl_anchor not in src:
        print("[错误] 找不到字体声明锚点")
        sys.exit(1)
    src = src.replace(decl_anchor, decl_anchor + build_helper(cfg), 1)

    # 2) SetupUI 结尾创建面板
    end_anchor = '''    lv_obj_center(low_battery_label_);
    lv_obj_add_flag(low_battery_popup_, LV_OBJ_FLAG_HIDDEN);
}'''
    if end_anchor not in src:
        print("[错误] 找不到 SetupUI 结尾锚点")
        sys.exit(1)
    src = src.replace(end_anchor, '''    lv_obj_center(low_battery_label_);
    lv_obj_add_flag(low_battery_popup_, LV_OBJ_FLAG_HIDDEN);

''' + build_create(), 1)

    with open(LCD, "w", encoding="utf-8") as f:
        f.write(src)
    print("[完成] 可配置屏幕面板补丁已注入 lcd_display.cc")


if __name__ == "__main__":
    main()
