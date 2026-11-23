# -*- coding: utf-8 -*-
"""
给小智 v2.6.0 注入「BOOT 按键 PTT 开关」补丁（配合 patch_silence.py 使用，在其之后运行）。

功能：
  1) 板子端：BOOT 单击 = 按一下开始听、再按一下停止（用官方现成的
     StartListening/StopListening，ManualStop 模式，不会自动结束）。
  2) 通过串口打印标记 PC_PTT_BEGIN / PC_PTT_END，电脑端据此收集并贴入文字。
  3) 定义全局 volatile bool g_ptt_active，patch_silence 的静默逻辑读取它：
     PTT 会话期间，识别到的文字一律静默（喇叭不出声、不接话）。

与现有功能的关系：
  - 不改唤醒词（嗨小欧仍可用，作为跟小智聊天的入口）。
  - 不改拍照静默（esp32_camera.cc 的 g_pc_mute 逻辑保持）。
  - 触发词「小K」转发仍然保留，PTT 是新增的第二种输入方式。

用法（云端工作流里，在 xiaozhi/ 目录）：python patch_ptt.py
"""

import os
import sys

APP = "main/application.cc"
BOARD = "main/boards/lckfb/szpi-esp32s3/lichuang_dev_board.cc"

# 电脑端识别的标记（与 serial_speech.py 保持一致）
BEGIN = "PC_PTT_BEGIN"
END = "PC_PTT_END"


def must_replace(src, old, new, label):
    if old not in src:
        print(f"[错误] 找不到锚点：{label}")
        sys.exit(1)
    return src.replace(old, new, 1)


def main():
    for p in (APP, BOARD):
        if not os.path.exists(p):
            print(f"[错误] 找不到 {p}")
            sys.exit(1)

    # ========== 1) application.cc：定义 g_ptt_active ==========
    with open(APP, encoding="utf-8") as f:
        app = f.read()

    if "g_ptt_active" in app:
        print("[跳过] application.cc 已含 g_ptt_active")
    else:
        anchor = "volatile bool g_pc_mute = false;"
        if anchor not in app:
            print("[错误] 找不到 g_pc_mute 定义（请先运行 patch_silence.py）")
            sys.exit(1)
        app = app.replace(
            anchor,
            anchor + "\n\n// === PTT 按键会话标志（lichuang_dev_board.cc 读写）===\n"
            "volatile bool g_ptt_active = false;\n// === PTT 补丁 ===",
            1,
        )
        # 静默判断：PTT 会话期间强制静默（在原有触发词判断之前）
        stt_anchor = "                // === PC 静默转发：判断是否给电脑的句子 ==="
        stt_new = (
            "                // === PTT 补丁：按键会话期间一律静默转发 ===\n"
            "                if (g_ptt_active) {\n"
            '                    g_pc_mute = true;\n'
            '                    ESP_LOGI(TAG, ">> PC_PTT_SPEECH");\n'
            "                    Schedule([this]() { AbortSpeaking(kAbortReasonNone); });\n"
            "                } else\n"
        )
        if stt_anchor not in app:
            print("[错误] 找不到静默补丁锚点（请先运行 patch_silence.py）")
            sys.exit(1)
        app = app.replace(stt_anchor, stt_new + stt_anchor, 1)

        with open(APP, "w", encoding="utf-8") as f:
            f.write(app)
        print("[完成] application.cc 已注入 g_ptt_active 与 PTT 静默判断")

    # ========== 2) 板子文件：BOOT 单击改为 PTT 开关 ==========
    with open(BOARD, encoding="utf-8") as f:
        board = f.read()

    if "g_ptt_active" in board:
        print("[跳过] 板子文件已含 PTT 补丁")
    else:
        # 2a) 顶部加 extern 声明（放在 include 区之后、namespace 之前）
        inc_anchor = '#include "press_to_talk_mcp_tool.h"'
        if inc_anchor not in board:
            print("[错误] 找不到板子文件 include 锚点")
            sys.exit(1)
        board = board.replace(
            inc_anchor,
            inc_anchor
            + "\n\n// === PTT 补丁：引用 application.cc 的全局标志 ===\n"
            + "#include <esp_timer.h>\n"
            + "extern volatile bool g_ptt_active;\nextern volatile bool g_pc_mute;\n"
            + "static bool s_boot_ptt_on = false;\n"
            + "\n"
            + "// 第二下 BOOT 之后的静默宽限期。\n"
            + "// 云端的最终 STT 结果、LLM 回复、拍照点评都比 PC_PTT_END 晚 1~3 秒才回来。\n"
            + "// 若在按键那一刻就清 g_pc_mute / g_ptt_active，小智会把\"给电脑\"的话\n"
            + "// 当成普通聊天大声回答，还会点评照片（实测踩过）。\n"
            + "// 所以改成：按下停止键后先保持静默，由一次性定时器兜底清理。\n"
            + "static esp_timer_handle_t s_ptt_grace = nullptr;\n"
            + "static void pc_ptt_grace_cb(void*) {\n"
            + "    g_ptt_active = false;\n"
            + "    g_pc_mute = false;\n"
            + "    ESP_LOGI(\"PTT\", \"PC_PTT_GRACE_END\");\n"
            + "}\n"
            + "static void pc_ptt_grace_arm(uint32_t ms) {\n"
            + "    if (s_ptt_grace == nullptr) {\n"
            + "        esp_timer_create_args_t a = {};\n"
            + "        a.callback = pc_ptt_grace_cb;\n"
            + "        a.arg = nullptr;\n"
            + "        a.dispatch_method = ESP_TIMER_TASK;\n"
            + "        a.name = \"ptt_grace\";\n"
            + "        esp_timer_create(&a, &s_ptt_grace);\n"
            + "    } else {\n"
            + "        esp_timer_stop(s_ptt_grace);\n"
            + "    }\n"
            + "    esp_timer_start_once(s_ptt_grace, (uint64_t)ms * 1000ULL);\n"
            + "}\n"
            + "static void pc_ptt_grace_disarm() {\n"
            + "    if (s_ptt_grace != nullptr) esp_timer_stop(s_ptt_grace);\n"
            + "}\n"
            + "// === PTT 补丁结束 ===",
            1,
        )

        # 2b) 替换 BOOT OnClick 回调体
        old_click = '''        boot_button_.OnClick([this]() {
            auto& app = Application::GetInstance();
            // During startup (before connected), pressing BOOT button enters Wi-Fi config mode without reboot
            if (app.GetDeviceState() == kDeviceStateStarting) {
                EnterWifiConfigMode();
                return;
            }
            // In press-to-talk mode the click event is handled by press down/up
            if (!press_to_talk_tool_ || !press_to_talk_tool_->IsPressToTalkEnabled()) {
                app.ToggleChatState();
            }
        });'''
        new_click = '''        boot_button_.OnClick([this]() {
            auto& app = Application::GetInstance();
            // During startup (before connected), pressing BOOT button enters Wi-Fi config mode without reboot
            if (app.GetDeviceState() == kDeviceStateStarting) {
                EnterWifiConfigMode();
                return;
            }
            // === PTT 补丁：按一下开始听，再按一下停止 ===
            // 只在可对话状态下切换，避免配网/升级等状态被打扰
            auto st = app.GetDeviceState();
            if (st != kDeviceStateIdle && st != kDeviceStateListening &&
                st != kDeviceStateSpeaking) {
                return;
            }
            s_boot_ptt_on = !s_boot_ptt_on;
            if (s_boot_ptt_on) {
                // 开始拾音：ManualStop 模式，不会自动结束；静默喇叭
                pc_ptt_grace_disarm();   // 连按两次时，取消上一轮残留的宽限期
                g_ptt_active = true;
                g_pc_mute = true;
                ESP_LOGI("PTT", "PC_PTT_BEGIN");
                app.StartListening();
            } else {
                // 停止拾音：发结束标记，电脑端据此合并贴入。
                // 这里【绝不能】直接清 g_ptt_active / g_pc_mute：
                // 云端识别结果和回复要晚 1~3 秒才回来，提前清掉就会变成
                // "小智把要打给电脑的话当成聊天大声回答 + 顺便点评照片"。
                app.StopListening();
                ESP_LOGI("PTT", "PC_PTT_END");
                pc_ptt_grace_arm(5000);  // 保持静默 5 秒，期间照收文字、不出声
            }
            // === PTT 补丁：结束（原 ToggleChatState 逻辑已被本补丁取代）===
        });'''
        board = must_replace(board, old_click, new_click, "BOOT OnClick 回调")

        with open(BOARD, "w", encoding="utf-8") as f:
            f.write(board)
        print("[完成] 板子文件 BOOT 单击已改为 PTT 开关")

    print("[全部完成] PTT 按键补丁已注入")


if __name__ == "__main__":
    main()
