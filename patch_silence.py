# -*- coding: utf-8 -*-
"""
给小智 v2.6.0 的 application.cc 注入「静默转发」补丁：
识别到以触发词开头的句子时，打印标记并发 abort 打断小智回复，
让这句话只发电脑（通过串口），小智不接话不出声。

用法（云端工作流里）：
    python patch_silence.py
在 xiaozhi/ 目录下运行，会修改 main/application.cc。
触发词从同目录 pc_prefixes.txt 读取（每行一个，UTF-8）。
"""

import os
import sys

APP = "main/application.cc"
CFG = "pc_prefixes.txt"

# 默认触发词（当配置文件不存在时）
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
    # 转成 C 字符串字面量，转义引号和反斜杠
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


def main():
    prefixes = load_prefixes()
    print("[补丁] 静默触发词:", ", ".join(prefixes))

    if not os.path.exists(APP):
        print("[错误] 找不到", APP)
        sys.exit(1)

    with open(APP, encoding="utf-8") as f:
        src = f.read()

    # 已经打过补丁就跳过
    if "PC_SILENCE_ABORT" in src:
        print("[跳过] 已包含静默转发补丁")
        return

    anchor = '                ESP_LOGI(TAG, ">> %s", text->valuestring);'
    if anchor not in src:
        print("[错误] 找不到注入锚点（固件版本不匹配？）")
        sys.exit(1)

    arr = ", ".join(cstr(p) for p in prefixes)
    patch = anchor + f'''\n                // === PC 静默转发补丁（自动生成，勿手改）===
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
                        ESP_LOGI(TAG, ">> PC_SILENCE_ABORT");
                        // 打断大模型回复，让小智不接话；文字已通过上面的 >> 行发到串口给电脑
                        Schedule([this]() {{ AbortSpeaking(kAbortReasonNone); }});
                    }}
                }}
                // === 补丁结束 ==='''

    src = src.replace(anchor, patch, 1)
    with open(APP, "w", encoding="utf-8") as f:
        f.write(src)
    print("[完成] 已注入静默转发补丁到", APP)


if __name__ == "__main__":
    main()
