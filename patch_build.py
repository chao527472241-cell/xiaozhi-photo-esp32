# -*- coding: utf-8 -*-
"""
生成用于云端编译的 build.py 补丁（运行时注入到官方 build.py）。
作用：给 build.py 的 _wake_word_sdkconfig_options 增加一个 'xiaozhi' 分支，
使其使用 Multinet 自定义唤醒词（拼音可配），唤醒显示词可配。
不改动官方小智源码，仅在 GitHub Actions 云端临时打补丁。
"""
import sys

# 从命令行读取：拼音唤醒词、显示文字、阈值
pinyin = sys.argv[1] if len(sys.argv) > 1 else "xiao zhi"
display = sys.argv[2] if len(sys.argv) > 2 else "小智"
threshold = sys.argv[3] if len(sys.argv) > 3 else "20"

PATCH = f'''
# ==== 自定义唤醒词补丁（xiaozhi） ====
    if normalized == "xiaozhi":
        options = [
            "CONFIG_WAKE_WORD_DISABLED=n",
            "CONFIG_USE_ESP_WAKE_WORD=n",
            "CONFIG_USE_AFE_WAKE_WORD=y",
            "CONFIG_USE_CUSTOM_WAKE_WORD=y",
            "CONFIG_SR_MN_CN_MULTINET6_QUANT=y",
            'CONFIG_CUSTOM_WAKE_WORD="{pinyin}"',
            'CONFIG_CUSTOM_WAKE_WORD_DISPLAY="{display}"',
            "CONFIG_CUSTOM_WAKE_WORD_THRESHOLD={threshold}",
        ]
        return normalized, options, ["CONFIG_USE_CUSTOM_WAKE_WORD"]
'''

# 定位插入点：在 normalized 赋值之后、第一个 if 之前
needle = '''    normalized = wake_word.strip().casefold().replace("-", "_")
'''
with open("scripts/build.py", "r", encoding="utf-8") as f:
    src = f.read()

if needle not in src:
    raise SystemExit("补丁锚点未找到，官方 build.py 结构可能变化")

src = src.replace(needle, needle + PATCH, 1)

# build.py 里 Kconfig 别名校验：--wake-word 值会先经 _WAKE_WORD_MODEL_PATTERN 校验，
# 但 xiaozhi 分支在上面提前 return，不会被校验，OK。
# 另外 CLI 帮助里 --wake-word 的说明无需改，run 时传 xiaozhi 即可。

with open("scripts/build.py", "w", encoding="utf-8") as f:
    f.write(src)

print(f"[补丁] 已注入自定义唤醒词：拼音={pinyin!r} 显示={display!r} 阈值={threshold}")
