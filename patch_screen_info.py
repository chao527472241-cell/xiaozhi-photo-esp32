# -*- coding: utf-8 -*-
"""
给小智 v2.6.0 注入「待机屏幕实用信息」补丁（立创实战派 default 显示风格）。

效果：
  待机（idle）时，在机器人图标下方显示大字时钟 HH:MM 和日期星期，
  例如：
        09:41
     11月23日 周一
  说话/聆听/回复时不显示，避免挡住对话气泡；回到待机自动恢复。
  时间来自固件已有的服务器对时（连上小智即准确），无需 NTP 配置。

实现（只改 lcd_display.cc，不碰头文件、不碰静默/PTT/拍照补丁）：
  1) 顶部 include application.h（读设备状态）
  2) 顶部加文件级静态 label 指针 + LVGL timer 回调：
     - 每秒刷新：idle 才显示，其它状态隐藏
     - strftime 取 HH:MM 和 月/日/周X
  3) 在 LcdDisplay::SetupUI() 结尾创建两个 label + 启动 timer

用法（云端工作流 xiaozhi/ 目录，在 patch_silence / patch_ptt 之后）：
    python patch_screen_info.py
"""

import os
import sys

LCD = "main/display/lcd_display.cc"

MARK = "PC_SCREEN_INFO_PATCH"


def must_replace(src, old, new, label):
    if old not in src:
        print(f"[错误] 找不到锚点：{label}")
        sys.exit(1)
    return src.replace(old, new, 1)


def main():
    if not os.path.exists(LCD):
        print(f"[错误] 找不到 {LCD}")
        sys.exit(1)

    with open(LCD, encoding="utf-8") as f:
        src = f.read()

    if MARK in src:
        print("[跳过] lcd_display.cc 已含屏幕信息补丁")
        return

    # ---------- 1) include application.h ----------
    inc_anchor = '#include "board.h"'
    if inc_anchor not in src:
        print("[错误] 找不到 board.h include 锚点")
        sys.exit(1)
    src = src.replace(
        inc_anchor,
        inc_anchor + '\n#include "application.h"  // === PC_SCREEN_INFO_PATCH ===',
        1,
    )

    # ---------- 2) 顶部加时钟回调 ----------
    decl_anchor = "LV_FONT_DECLARE(font_noto_emoji_30_4);"
    if decl_anchor not in src:
        print("[错误] 找不到字体声明锚点")
        sys.exit(1)
    helper = r'''

// === PC_SCREEN_INFO_PATCH：待机实用信息（时间/日期/星期）===
#include <time.h>
#include <stdio.h>
static lv_obj_t* s_pc_clock_label = nullptr;   // HH:MM
static lv_obj_t* s_pc_date_label = nullptr;    // 11月23日 周一
static void pc_screen_info_timer_cb(lv_timer_t* /*timer*/) {
    if (s_pc_clock_label == nullptr || s_pc_date_label == nullptr) return;
    // 只在待机显示，说话/聆听/回复时隐藏，避免遮挡对话气泡
    bool idle = (Application::GetInstance().GetDeviceState() == kDeviceStateIdle);
    lv_obj_set_style_opa(s_pc_clock_label, idle ? LV_OPA_COVER : LV_OPA_TRANSP, 0);
    lv_obj_set_style_opa(s_pc_date_label, idle ? LV_OPA_COVER : LV_OPA_TRANSP, 0);
    if (!idle) return;

    time_t now = time(NULL);
    struct tm* t = localtime(&now);
    // 系统时间未同步（年份不对）时不显示，避免显示 00:00 误导
    if (t == nullptr || t->tm_year < 2025 - 1900) return;

    char clock_buf[16];
    strftime(clock_buf, sizeof(clock_buf), "%H:%M", t);
    lv_label_set_text(s_pc_clock_label, clock_buf);

    static const char* wd[7] = {"周日", "周一", "周二", "周三", "周四", "周五", "周六"};
    char date_buf[32];
    snprintf(date_buf, sizeof(date_buf), "%d月%d日 %s",
             t->tm_mon + 1, t->tm_mday, wd[t->tm_wday]);
    lv_label_set_text(s_pc_date_label, date_buf);
}
// === PC_SCREEN_INFO_PATCH END ===
'''
    src = src.replace(decl_anchor, decl_anchor + helper, 1)

    # ---------- 3) SetupUI 结尾创建 label + timer ----------
    end_anchor = '''    lv_obj_center(low_battery_label_);
    lv_obj_add_flag(low_battery_popup_, LV_OBJ_FLAG_HIDDEN);
}'''
    if end_anchor not in src:
        print("[错误] 找不到 SetupUI 结尾锚点")
        sys.exit(1)
    create_code = r'''    lv_obj_center(low_battery_label_);
    lv_obj_add_flag(low_battery_popup_, LV_OBJ_FLAG_HIDDEN);

    // === PC_SCREEN_INFO_PATCH：待机时钟 + 日期 ===
    // 放在机器人图标下方；用 BUILTIN_TEXT_FONT 保证中文/数字可显示
    s_pc_clock_label = lv_label_create(screen);
    lv_obj_set_style_text_font(s_pc_clock_label, &BUILTIN_TEXT_FONT, 0);
    lv_obj_set_style_text_color(s_pc_clock_label, lvgl_theme->text_color(), 0);
    lv_label_set_text(s_pc_clock_label, "--:--");
    lv_obj_align(s_pc_clock_label, LV_ALIGN_BOTTOM_MID, 0, -lvgl_theme->spacing(10) - 28);
    lv_obj_set_style_opa(s_pc_clock_label, LV_OPA_TRANSP, 0);  // 默认隐藏

    s_pc_date_label = lv_label_create(screen);
    lv_obj_set_style_text_font(s_pc_date_label, text_font, 0);
    lv_obj_set_style_text_color(s_pc_date_label, lvgl_theme->text_color(), 0);
    lv_label_set_text(s_pc_date_label, "");
    lv_obj_align(s_pc_date_label, LV_ALIGN_BOTTOM_MID, 0, -lvgl_theme->spacing(10));
    lv_obj_set_style_opa(s_pc_date_label, LV_OPA_TRANSP, 0);

    // 每秒刷新一次（LVGL 内置 timer，随 lv_timer_handler 驱动）
    lv_timer_create(pc_screen_info_timer_cb, 1000, nullptr);
    // === PC_SCREEN_INFO_PATCH END ===
}'''
    src = src.replace(end_anchor, create_code, 1)

    with open(LCD, "w", encoding="utf-8") as f:
        f.write(src)
    print("[完成] 屏幕信息补丁已注入 lcd_display.cc")


if __name__ == "__main__":
    main()
