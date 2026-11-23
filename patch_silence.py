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

// 最近一次按下 BOOT 的时间（微秒；0 = 从没按过）。
// esp32_camera.cc 用它区分「按键拍照」和「唤醒词拍照」：
//   按键拍照 → 静默掉点评；唤醒词拍照 → 正常点评。
volatile int64_t g_pc_ptt_last_us = 0;

// 静默保持：所有打开静默的地方都必须走 pc_mute_hold / pc_mute_set，
// 由定时器兜底关闭。原因：拍照点评、云端回复都比触发点晚很多秒，
// 写死成"永不恢复"会一直哑着；而当场清掉又会让小智把要打给电脑的话念出来。
//
// pc_mute_hold()：只延长不缩短。云端常把一句话拆成两条「迟到」的 stt，
//   每条都会走一次 hold(9000)；若不比较截止时间，就会把拍照刚设的 25 秒
//   压回 9 秒，点评正好在剩余时间里漏出来（实测踩过）。
// pc_mute_set()：强制设定，可缩短。BOOT 停止时用它把"开始听"的 120 秒改成 9 秒。
static esp_timer_handle_t s_pc_mute_timer = nullptr;
static int64_t s_pc_mute_deadline = 0;

static void pc_mute_timer_cb(void*) {
    g_pc_mute = false;
    s_pc_mute_deadline = 0;
    ESP_LOGI(TAG, "PC_MUTE_HOLD_END");
}

static bool pc_mute_ensure_timer() {
    if (s_pc_mute_timer != nullptr) {
        return true;
    }
    esp_timer_create_args_t mute_args = {};
    mute_args.callback = pc_mute_timer_cb;
    mute_args.arg = nullptr;
    mute_args.dispatch_method = ESP_TIMER_TASK;
    mute_args.name = "pc_mute_hold";
    if (esp_timer_create(&mute_args, &s_pc_mute_timer) != ESP_OK) {
        // 定时器建不起来就别静默，否则会永久哑掉
        s_pc_mute_timer = nullptr;
        g_pc_mute = false;
        return false;
    }
    return true;
}

void pc_mute_hold(uint32_t ms) {
    int64_t want = esp_timer_get_time() + (int64_t)ms * 1000;
    if (g_pc_mute && s_pc_mute_timer != nullptr && want <= s_pc_mute_deadline) {
        return;  // 已有更长的静默在生效，不要缩短
    }
    if (!pc_mute_ensure_timer()) {
        return;
    }
    g_pc_mute = true;
    esp_timer_stop(s_pc_mute_timer);
    esp_timer_start_once(s_pc_mute_timer, (uint64_t)ms * 1000ULL);
    s_pc_mute_deadline = want;
}

void pc_mute_set(uint32_t ms) {
    if (!pc_mute_ensure_timer()) {
        return;
    }
    g_pc_mute = true;
    esp_timer_stop(s_pc_mute_timer);
    esp_timer_start_once(s_pc_mute_timer, (uint64_t)ms * 1000ULL);
    s_pc_mute_deadline = esp_timer_get_time() + (int64_t)ms * 1000;
}

// 立即取消静默。双击 BOOT 直拍时用：第一下已经把静默开到 120 秒，
// 第二下确认是拍照就必须立刻解掉，否则拍完 2 分钟内喊不应。
void pc_mute_off() {
    g_pc_mute = false;
    s_pc_mute_deadline = 0;
    if (s_pc_mute_timer != nullptr) {
        esp_timer_stop(s_pc_mute_timer);
    }
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
                        // 普通对话：这里绝对不能把静默关掉。
                        // 云端常把一句话拆成两条「迟到」的 stt 下发；若在此处无条件
                        // g_pc_mute = false，拍照那 20 多秒静默会被它中途关掉，
                        // 结果就是照片传出来了、小智紧接着开始点评照片（实测踩过）。
                        // 恢复声音统一交给 pc_mute_hold() 的定时器到期。
                    }}
                }}
                // === 补丁结束 ==='''
    src = src.replace(stt_anchor, stt_new, 1)

    with open(APP, "w", encoding="utf-8") as f:
        f.write(src)
    print("[完成] 已注入静默补丁到", APP)


if __name__ == "__main__":
    main()
