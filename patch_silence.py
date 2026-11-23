# -*- coding: utf-8 -*-
"""
给小智 v2.6.0 的 application.cc 注入「静默转发」补丁（v2，音频级静默）：
1. 顶部定义全局 bool g_pc_mute（喇叭静默开关）
2. 音频回调：g_pc_mute 为真时直接丢弃音频包 → 喇叭绝对不出声
3. stt 行：命中触发词 → g_pc_mute=true 且 abort；普通对话 → g_pc_mute=false（恢复声音）

拍照静默由 esp32_camera.cc 在发图时设 g_pc_mute=true（extern 引用本全局变量）。
触发词从 pc_prefixes.txt 读取（每行一个，UTF-8）。

用法（云端工作流里，在 xiaozhi/ 目录）：python patch_silence.py
"""

import os
import sys

APP = "main/application.cc"
CFG = "pc_prefixes.txt"

DEFAULT_PREFIXES = ["小K", "小k", "发给电脑", "工作台", "输入到电脑", "写到电脑", "发送给电脑"]


def load_prefixes():
    prefixes = list(DEFAULT_PREFIXES)
    if os.path.exists(CFG):
        with open(CFG, encoding="utf-8-sig") as f:
            lines = [l.strip() for l in f if l.strip() and not l.lstrip().startswith("#")]
        if lines:
            prefixes = lines
    return prefixes


def cstr(s):
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


def main():
    prefixes = load_prefixes()
    print("[补丁] 静默触发词:", ", ".join(prefixes))

    if not os.path.exists(APP):
        print("[错误] 找不到", APP)
        sys.exit(1)

    with open(APP, encoding="utf-8") as f:
        src = f.read()

    if "g_pc_mute" in src:
        print("[跳过] 已包含静默补丁")
        return

    # --- 1) 顶部定义全局静默开关 ---
    inc_anchor = '#define TAG "Application"'
    if inc_anchor not in src:
        print("[错误] 找不到 TAG 锚点")
        sys.exit(1)
    mute_block = '''

// === PC 静默转发：全局喇叭静默开关（esp32_camera.cc 用 extern 引用）===
volatile bool g_pc_mute = false;

// 静默保持：所有打开静默的地方都必须走 pc_mute_hold()，由定时器兜底关闭。
// 原因：拍照点评、云端回复都比触发点晚很多秒，写死成"永不恢复"会一直哑着；
// 而当场清掉又会让小智把要打给电脑的话念出来。定时器是唯一能两头兼顾的做法。
// esp32_camera.cc 与板型 BOOT 回调通过 extern void pc_mute_hold(uint32_t) 调用。
static esp_timer_handle_t s_pc_mute_timer = nullptr;
static void pc_mute_timer_cb(void*) {
    g_pc_mute = false;
    ESP_LOGI(TAG, "PC_MUTE_HOLD_END");
}
void pc_mute_hold(uint32_t ms) {
    g_pc_mute = true;
    if (s_pc_mute_timer == nullptr) {
        esp_timer_create_args_t mute_args = {};
        mute_args.callback = pc_mute_timer_cb;
        mute_args.arg = nullptr;
        mute_args.dispatch_method = ESP_TIMER_TASK;
        mute_args.name = "pc_mute_hold";
        if (esp_timer_create(&mute_args, &s_pc_mute_timer) != ESP_OK) {
            s_pc_mute_timer = nullptr;
            return;
        }
    } else {
        esp_timer_stop(s_pc_mute_timer);
    }
    esp_timer_start_once(s_pc_mute_timer, (uint64_t)ms * 1000ULL);
}
// === 补丁 ==='''
    src = src.replace(inc_anchor,
                      '#include <esp_timer.h>\n' + inc_anchor + mute_block, 1)

    # --- 2) 音频回调：静默时丢弃音频 ---
    audio_anchor = '''    protocol_->OnIncomingAudio([this](std::unique_ptr<AudioStreamPacket> packet) {
        if (GetDeviceState() == kDeviceStateSpeaking) {'''
    audio_new = '''    protocol_->OnIncomingAudio([this](std::unique_ptr<AudioStreamPacket> packet) {
        if (g_pc_mute) {
            // 静默窗口：丢弃小智的语音包，喇叭不出声（拍照评论 / 小K回复）
            return;
        }
        if (GetDeviceState() == kDeviceStateSpeaking) {'''
    if audio_anchor not in src:
        print("[错误] 找不到音频回调锚点")
        sys.exit(1)
    src = src.replace(audio_anchor, audio_new, 1)

    # --- 3) stt 行：触发词静默，普通对话恢复 ---
    stt_anchor = '                ESP_LOGI(TAG, ">> %s", text->valuestring);'
    if stt_anchor not in src:
        print("[错误] 找不到 stt 锚点")
        sys.exit(1)
    arr = ", ".join(cstr(p) for p in prefixes)
    stt_new = stt_anchor + f'''\n                // === PC 静默转发：判断是否给电脑的句子 ===
                {{
                    static const char* SILENT_PREFIXES[] = {{{arr}}};
                    const char* pc_said = text->valuestring;
                    bool pc_match = false;
                    for (size_t pc_i = 0; pc_i < sizeof(SILENT_PREFIXES)/sizeof(SILENT_PREFIXES[0]); ++pc_i) {{
                        const char* pc_p = SILENT_PREFIXES[pc_i];
                        if (strncmp(pc_said, pc_p, strlen(pc_p)) == 0) {{
                            pc_match = true;
                            break;
                        }}
                    }}
                    if (pc_match) {{
                        // 给电脑的句子：静默喇叭 + 打断回复；文字已通过上面的 >> 行发到串口
                        pc_mute_hold(9000);
                        ESP_LOGI(TAG, ">> PC_SILENCE_ABORT");
                        Schedule([this]() {{ AbortSpeaking(kAbortReasonNone); }});
                    }} else {{
                        // 普通对话：恢复声音
                        g_pc_mute = false;
                    }}
                }}
                // === 补丁结束 ==='''
    src = src.replace(stt_anchor, stt_new, 1)

    with open(APP, "w", encoding="utf-8") as f:
        f.write(src)
    print("[完成] 已注入静默补丁到", APP)


if __name__ == "__main__":
    main()
