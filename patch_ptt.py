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
            "                    // 只静默，绝不再 AbortSpeaking。\n"
            "                    // AbortSpeaking 会向服务器发 abort，把本轮 LLM 生成连同\n"
            "                    // 拍照 tool call 一起掐掉 —— 实测导致说「拍张照」不出图。\n"
            "                    // 静默由音频回调丢包实现，本来就足够不出声。\n"
            "                    pc_mute_hold(9000);\n"
            '                    ESP_LOGI(TAG, ">> PC_PTT_SPEECH");\n'
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
            + "extern void pc_mute_hold(uint32_t ms);  // 只延长不缩短，application.cc 定义\n"
            + "extern void pc_mute_set(uint32_t ms);   // 强制设定，可缩短\n"
            + "static bool s_boot_ptt_on = false;\n"
            + "\n"
            + "// 第二下 BOOT 之后只负责撤掉 g_ptt_active（让后续对话恢复正常判断）。\n"
            + "// 静默本身交给 pc_mute_hold() 的定时器，因为云端 STT、LLM 回复、\n"
            + "// 拍照点评都要晚 1~3 秒才回来，这里提前清掉就会让小智开口。\n"
            + "static esp_timer_handle_t s_ptt_grace = nullptr;\n"
            + "static void pc_ptt_grace_cb(void*) {\n"
            + "    g_ptt_active = false;\n"
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
                // 用长时限兜底（120 秒），避免一直说下去静默中途失效；
                // 按停止时会立刻改成一个更短的时限。
                pc_ptt_grace_disarm();   // 连按两次时，取消上一轮残留的宽限期
                g_ptt_active = true;
                pc_mute_set(120000);
                ESP_LOGI("PTT", "PC_PTT_BEGIN");
                app.StartListening();
            } else {
                // 停止拾音：发结束标记，电脑端据此合并贴入。
                // 静默继续保持 9 秒（覆盖 STT + LLM 回复 + 拍照点评），
                // 由 pc_mute_hold 的定时器到点自动恢复，避免一直哑着。
                app.StopListening();
                ESP_LOGI("PTT", "PC_PTT_END");
                pc_ptt_grace_arm(6000);   // 6 秒后撤掉 g_ptt_active
                pc_mute_set(9000);        // 9 秒静默（强制缩短，覆盖上面的 120 秒）
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
