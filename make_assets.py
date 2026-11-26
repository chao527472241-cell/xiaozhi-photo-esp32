# -*- coding: utf-8 -*-
"""
生成小智屏幕用的图片资源（全部编译进固件，不依赖字体，绝不会踩字体坑）
================================================================
产出 pc_assets.h，里面是纯字节数组：
  1) 壁纸 320x240  RGB565        （有 wallpaper.jpg 就用它，没有就用内置画的渐变山景）
  2) 5 个磁贴图标 48x48 RGB565A8 （麦克风/时钟/状态条/齿轮/显示器）
  3) 11 个字符 30x46 RGB565A8    （0-9 加冒号，用来拼大号时钟）

用法：
    python make_assets.py            # 生成 pc_assets.h
    python make_assets.py --preview  # 额外生成一张界面模拟图 _mock_home.png，方便预览

换壁纸：把任意图片改名成 wallpaper.jpg 放在本目录，重新编译即可。
"""

import os
import sys
import math
import random

from PIL import Image, ImageDraw, ImageFilter, ImageFont

HERE = os.path.dirname(os.path.abspath(__file__))
OUT_H = os.path.join(HERE, "pc_assets.h")
WALLPAPER = os.path.join(HERE, "wallpaper.jpg")
MOCK = os.path.join(HERE, "_mock_home.png")

W, H = 320, 240          # 屏幕分辨率
ICON_W = ICON_H = 48     # 图标绘制坐标系（先按 48 画，超采样，边缘才干净）
ICON_OUT = 34            # 实际输出尺寸：磁贴只有 60 高，图标必须缩小
DIGIT_W, DIGIT_H = 30, 46  # 单个字符格子
SS = 4                   # 画图标/文字时的超采样倍数（先画大再缩小，边缘才干净）

# ---------------------------------------------------------------- 壁纸

def _gradient(w, h, stops):
    """竖直渐变。stops = [(位置0~1, (r,g,b)), ...]"""
    img = Image.new("RGB", (w, h))
    d = ImageDraw.Draw(img)
    for y in range(h):
        t = y / max(1, h - 1)
        # 找到所在区间
        a, b = stops[0], stops[-1]
        for i in range(len(stops) - 1):
            if stops[i][0] <= t <= stops[i + 1][0]:
                a, b = stops[i], stops[i + 1]
                break
        span = max(1e-6, b[0] - a[0])
        k = (t - a[0]) / span
        k = 0.0 if k < 0 else (1.0 if k > 1 else k)
        c = tuple(int(a[1][j] + (b[1][j] - a[1][j]) * k) for j in range(3))
        d.line([(0, y), (w, y)], fill=c)
    return img


def _starfield(w, h, top_ratio=0.62, count=90, seed=7):
    rnd = random.Random(seed)
    layer = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    for _ in range(count):
        x = rnd.uniform(0, w)
        y = rnd.uniform(0, h * top_ratio)
        r = rnd.choice([1, 1, 1, 1.5, 2])
        a = rnd.randint(40, 170)
        d.ellipse([x - r, y - r, x + r, y + r], fill=(255, 255, 255, a))
    return layer


def _sun_glow(w, h, cx, cy, radius):
    """柔和的光晕：一圈圈叠加，越外越淡"""
    layer = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    steps = 26
    for i in range(steps, 0, -1):
        t = i / steps
        r = radius * (1 + 2.6 * t)
        a = int(52 * (1 - t) ** 1.9)
        d.ellipse([cx - r, cy - r, cx + r, cy + r], fill=(255, 214, 160, a))
    layer = layer.filter(ImageFilter.GaussianBlur(w // 90))
    return layer


def _ridge(w, h, base_y, amp, freq, phase, color, seed=1):
    """一条山脊（用正弦叠加做出起伏），返回 RGBA 图层"""
    layer = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    rnd = random.Random(seed)
    pts = []
    for x in range(0, w + 1, 4):
        t = x / w
        y = (base_y
             + amp * math.sin(t * math.pi * freq + phase)
             + amp * 0.42 * math.sin(t * math.pi * freq * 2.7 + phase * 1.7)
             + amp * 0.18 * math.sin(t * math.pi * freq * 6.1 + rnd.random()))
        pts.append((x, y))
    pts += [(w, h), (0, h)]
    d.polygon(pts, fill=color)
    return layer


def make_wallpaper():
    """有 wallpaper.jpg 就裁切缩放；没有就用程序画一张黄昏山景"""
    if os.path.exists(WALLPAPER):
        try:
            src = Image.open(WALLPAPER).convert("RGB")
            src_ratio = src.width / src.height
            target = W / H
            if src_ratio > target:            # 太宽，左右裁
                nw = int(src.height * target)
                x0 = (src.width - nw) // 2
                src = src.crop((x0, 0, x0 + nw, src.height))
            else:                              # 太高，上下裁
                nh = int(src.width / target)
                y0 = (src.height - nh) // 2
                src = src.crop((0, y0, src.width, y0 + nh))
            return src.resize((W, H), Image.LANCZOS)
        except Exception as e:
            print("[警告] wallpaper.jpg 读不了（%s），改用内置图案" % e)

    w, h = W * 2, H * 2      # 超采样画，最后缩小，边缘更顺滑
    img = _gradient(w, h, [
        (0.00, (18, 16, 58)),
        (0.22, (48, 30, 96)),
        (0.42, (112, 52, 118)),
        (0.58, (196, 88, 120)),
        (0.70, (248, 146, 118)),
        (0.80, (255, 196, 146)),
        (1.00, (255, 224, 182)),
    ]).convert("RGBA")

    img = Image.alpha_composite(img, _starfield(w, h, 0.55, 110, seed=11))
    img = Image.alpha_composite(img, _sun_glow(w, h, w * 0.855, h * 0.235, w * 0.05))

    # 太阳本体
    sun = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    ds = ImageDraw.Draw(sun)
    r = w * 0.052
    cx, cy = w * 0.855, h * 0.235
    ds.ellipse([cx - r, cy - r, cx + r, cy + r], fill=(255, 242, 214, 255))
    img = Image.alpha_composite(img, sun)

    # 三层山，越远越淡、越近越黑
    img = Image.alpha_composite(img, _ridge(w, h, h * 0.76, h * 0.040, 2.2, 0.6, (150, 74, 116, 220), seed=21))
    img = Image.alpha_composite(img, _ridge(w, h, h * 0.84, h * 0.050, 1.6, 2.1, (84, 42, 84, 240), seed=22))
    img = Image.alpha_composite(img, _ridge(w, h, h * 0.93, h * 0.045, 1.1, 4.0, (32, 18, 44, 255), seed=23))

    return img.convert("RGB").resize((W, H), Image.LANCZOS)


# ---------------------------------------------------------------- 图标

def _canvas():
    return Image.new("RGBA", (ICON_W * SS, ICON_H * SS), (0, 0, 0, 0))


def _S(v):
    return int(round(v * SS))


def icon_mic():
    img = _canvas(); d = ImageDraw.Draw(img)
    c = (89, 167, 255, 255)
    d.rounded_rectangle([_S(18), _S(7), _S(30), _S(28)], radius=_S(6), fill=c)
    d.arc([_S(12), _S(13), _S(36), _S(35)], start=20, end=160, fill=c, width=_S(2.4))
    d.line([_S(24), _S(33), _S(24), _S(39)], fill=c, width=_S(2.4))
    d.line([_S(17), _S(41), _S(31), _S(41)], fill=c, width=_S(2.4))
    return img


def icon_clock():
    img = _canvas(); d = ImageDraw.Draw(img)
    c = (56, 214, 224, 255)
    d.ellipse([_S(8), _S(8), _S(40), _S(40)], outline=c, width=_S(3))
    d.line([_S(24), _S(24), _S(24), _S(14)], fill=c, width=_S(3))
    d.line([_S(24), _S(24), _S(32), _S(28)], fill=c, width=_S(3))
    d.ellipse([_S(22), _S(22), _S(26), _S(26)], fill=c)
    return img


def icon_bars():
    img = _canvas(); d = ImageDraw.Draw(img)
    c = (74, 222, 128, 255)
    d.rounded_rectangle([_S(10), _S(25), _S(17), _S(40)], radius=_S(2), fill=c)
    d.rounded_rectangle([_S(21), _S(17), _S(28), _S(40)], radius=_S(2), fill=c)
    d.rounded_rectangle([_S(32), _S(8), _S(39), _S(40)], radius=_S(2), fill=c)
    return img


def icon_gear():
    img = _canvas(); d = ImageDraw.Draw(img)
    c = (255, 176, 32, 255)
    cx = cy = 24.0
    d.ellipse([_S(11), _S(11), _S(37), _S(37)], outline=c, width=_S(4))
    for i in range(8):
        a = math.radians(i * 45)
        ha = math.radians(10)
        r1, r2 = 15.0, 20.5
        pts = [(cx + r1 * math.cos(a - ha), cy + r1 * math.sin(a - ha)),
               (cx + r2 * math.cos(a - ha * 0.65), cy + r2 * math.sin(a - ha * 0.65)),
               (cx + r2 * math.cos(a + ha * 0.65), cy + r2 * math.sin(a + ha * 0.65)),
               (cx + r1 * math.cos(a + ha), cy + r1 * math.sin(a + ha))]
        d.polygon([(_S(x), _S(y)) for x, y in pts], fill=c)
    d.ellipse([_S(19), _S(19), _S(29), _S(29)], fill=(0, 0, 0, 0))
    return img


def icon_pc():
    """显示器 + 两条数据线，代表「电脑运行信息」"""
    img = _canvas(); d = ImageDraw.Draw(img)
    c = (199, 146, 234, 255)
    # 屏幕外框
    d.rounded_rectangle([_S(6), _S(9), _S(42), _S(31)], radius=_S(3),
                        outline=c, width=_S(2.6))
    # 屏幕里的两行「数据」
    d.line([_S(13), _S(17), _S(23), _S(17)], fill=c, width=_S(2.2))
    d.line([_S(13), _S(23), _S(35), _S(23)], fill=c, width=_S(2.2))
    # 支架
    d.line([_S(24), _S(31), _S(24), _S(37)], fill=c, width=_S(2.6))
    d.line([_S(15), _S(39), _S(33), _S(39)], fill=c, width=_S(2.6))
    return img


def icon_alarm():
    """闹钟：圆钟面 + 顶上两个铃铛 + 指针 + 两只脚"""
    img = _canvas(); d = ImageDraw.Draw(img)
    c = (255, 107, 107, 255)
    # 钟面（稍微下移，给顶部铃铛让位）
    d.ellipse([_S(9), _S(15), _S(39), _S(45)], outline=c, width=_S(3))
    # 顶部两个实心铃铛，压在钟面左上/右上
    d.ellipse([_S(3), _S(5), _S(17), _S(19)], fill=c)
    d.ellipse([_S(31), _S(5), _S(45), _S(19)], fill=c)
    # 指针：时针朝上、分针朝右下
    d.line([_S(24), _S(30), _S(24), _S(21)], fill=c, width=_S(3))
    d.line([_S(24), _S(30), _S(31), _S(34)], fill=c, width=_S(3))
    # 两只脚
    d.line([_S(14), _S(43), _S(9), _S(47)], fill=c, width=_S(3))
    d.line([_S(34), _S(43), _S(39), _S(47)], fill=c, width=_S(3))
    return img


# ---------------------------------------------------------------- 数字

FONT_CANDIDATES = [
    "C:/Windows/Fonts/seguisb.ttf",
    "C:/Windows/Fonts/arialbd.ttf",
    "C:/Windows/Fonts/segoeuib.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
]


def load_font(px, cjk=False):
    paths = FONT_CANDIDATES
    if cjk:
        paths = ["C:/Windows/Fonts/msyh.ttc", "C:/Windows/Fonts/msyhbd.ttc",
                 "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc"] + FONT_CANDIDATES
    for p in paths:
        if os.path.exists(p):
            try:
                return ImageFont.truetype(p, px)
            except Exception:
                pass
    try:
        return ImageFont.load_default(px)
    except Exception:
        return ImageFont.load_default()


def _glyph(ch, color=(255, 255, 255, 255)):
    img = Image.new("RGBA", (DIGIT_W * SS, DIGIT_H * SS), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    font = load_font(int(DIGIT_H * SS * 0.92))
    box = d.textbbox((0, 0), ch, font=font)
    tw, th = box[2] - box[0], box[3] - box[1]
    x = (DIGIT_W * SS - tw) / 2 - box[0]
    y = (DIGIT_H * SS - th) / 2 - box[1]
    d.text((x, y), ch, font=font, fill=color)
    return img


# ---------------------------------------------------------------- 编码

def _rgb565_bytes(img):
    """RGB565 小端序字节流"""
    out = bytearray()
    px = img.convert("RGB").load()
    for y in range(img.height):
        for x in range(img.width):
            r, g, b = px[x, y]
            v = ((r >> 3) << 11) | ((g >> 2) << 5) | (b >> 3)
            out.append(v & 0xFF)
            out.append((v >> 8) & 0xFF)
    return bytes(out)


def _rgb565a8_bytes(img):
    """RGB565A8：先 RGB565 平面，再 8 位 alpha 平面（LVGL 的标准布局）"""
    rgb = _rgb565_bytes(img)
    rgba = img.convert("RGBA")
    px = rgba.load()
    alpha = bytearray()
    for y in range(rgba.height):
        for x in range(rgba.width):
            alpha.append(px[x, y][3])
    return rgb + bytes(alpha)


def _emit_array(f, name, data, per_line=16):
    f.write("static const uint8_t %s[%d] = {\n" % (name, len(data)))
    for i in range(0, len(data), per_line):
        chunk = data[i:i + per_line]
        f.write("    " + ",".join("0x%02X" % b for b in chunk) + ",\n")
    f.write("};\n\n")


# ---------------------------------------------------------------- 预览图

def build_mock(wall, icons, glyphs):
    """按固件里的实际布局画一张模拟图，用来确认好不好看"""
    m = wall.copy().convert("RGBA")
    scrim = Image.new("RGBA", (W, H), (6, 8, 14, 118))
    m = Image.alpha_composite(m, scrim)
    d = ImageDraw.Draw(m)

    # 品牌字
    d.text((10, 4), "小智", font=load_font(15, cjk=True), fill=(255, 255, 255, 235))

    # 6 个磁贴：横屏 3 列 × 2 行
    # 横坐标 6 + 104*i（宽 100，列间 4 像素），纵坐标 28 + 108*r（高 98，行间 10 像素）
    tiles = [
        ("小智AI", 0, (89, 167, 255), 6, 28),
        ("时钟",   1, (56, 214, 224), 110, 28),
        ("状态",   2, (74, 222, 128), 214, 28),
        ("电脑",   4, (199, 146, 234), 6, 136),
        ("设置",   3, (255, 176, 32), 110, 136),
        ("闹钟",   5, (255, 107, 107), 214, 136),
    ]
    for label, ii, accent, x, y in tiles:
        tw, th = 100, 98
        d.rounded_rectangle([x, y, x + tw, y + th], radius=13,
                            fill=(20, 26, 38, 235), outline=accent + (255,), width=2)
        ic = icons[ii].resize((34, 34), Image.LANCZOS)
        m.alpha_composite(ic, (x + (tw - 34) // 2, y + 20))
        f = load_font(15, cjk=True)
        bb = d.textbbox((0, 0), label, font=f)
        d.text((x + (tw - (bb[2] - bb[0])) / 2, y + 60), label, font=f,
               fill=(236, 240, 248, 255))

    m.convert("RGB").save(MOCK)
    print("[预览] 已生成界面模拟图：" + MOCK)

    # ---- 时钟页样张 ----
    c = wall.copy().convert("RGBA")
    c = Image.alpha_composite(c, Image.new("RGBA", (W, H), (4, 6, 12, 96)))
    dc = ImageDraw.Draw(c)
    txt = "23:05"
    total = sum(DIGIT_W + 2 for _ in txt) - 2
    x = (W - total) // 2
    for ch in txt:
        idx = int(ch) if ch.isdigit() else 10
        g = glyphs[idx]
        c.alpha_composite(g, (x, 78))
        x += DIGIT_W + 2
    f = load_font(15, cjk=True)
    s = "11月23日 周一"
    bb = dc.textbbox((0, 0), s, font=f)
    dc.text(((W - (bb[2] - bb[0])) // 2, 140), s, font=f, fill=(255, 255, 255, 235))
    f2 = load_font(13, cjk=True)
    s2 = "嗨，我是小智"
    bb2 = dc.textbbox((0, 0), s2, font=f2)
    dc.text(((W - (bb2[2] - bb2[0])) // 2, 166), s2, font=f2, fill=(255, 255, 255, 190))
    c.convert("RGB").save(os.path.join(HERE, "_mock_clock.png"))

    # ---- 设置页样张 ----
    st = wall.copy().convert("RGBA")
    st = Image.alpha_composite(st, Image.new("RGBA", (W, H), (4, 6, 12, 150)))
    dst = ImageDraw.Draw(st)
    dst.text((14, 10), "‹  设置", font=load_font(17, cjk=True), fill=(255, 255, 255, 245))
    rows = [("启动界面", "主界面"), ("配色", "暗色"), ("屏幕亮度", "75%"), ("设备信息", "›")]
    y = 44
    for name, val in rows:
        dst.rounded_rectangle([10, y, W - 10, y + 40], radius=10,
                              fill=(22, 28, 40, 235), outline=(90, 110, 140, 120), width=1)
        dst.text((24, y + 11), name, font=load_font(15, cjk=True), fill=(236, 240, 248, 255))
        bb = dst.textbbox((0, 0), val, font=load_font(15, cjk=True))
        dst.text((W - 24 - (bb[2] - bb[0]), y + 11), val,
                 font=load_font(15, cjk=True), fill=(120, 200, 255, 255))
        y += 46
    st.convert("RGB").save(os.path.join(HERE, "_mock_settings.png"))

    # ---- 电脑页样张 ----
    p = wall.copy().convert("RGBA")
    p = Image.alpha_composite(p, Image.new("RGBA", (W, H), (4, 6, 12, 150)))
    dp = ImageDraw.Draw(p)
    dp.text((14, 8), "‹  电脑", font=load_font(17, cjk=True), fill=(255, 255, 255, 245))
    rows = [("CPU", "23%  3.6GHz"), ("内存", "61%  9.8/16G"),
            ("磁盘", "C 45%  D 72%"), ("网络", "↓1.2M ↑0.3M"),
            ("CPU 温度", "56°C"), ("芯片温度", "42°C")]
    y = 36
    for name, val in rows:
        dp.rounded_rectangle([10, y, W - 10, y + 30], radius=8, fill=(22, 28, 40, 225),
                             outline=(90, 110, 140, 100), width=1)
        dp.text((20, y + 6), name, font=load_font(14, cjk=True), fill=(200, 210, 225, 255))
        fv = load_font(15, cjk=True)
        bb = dp.textbbox((0, 0), val, font=fv)
        dp.text((W - 20 - (bb[2] - bb[0]), y + 5), val, font=fv, fill=(120, 200, 255, 255))
        y += 32
    p.convert("RGB").save(os.path.join(HERE, "_mock_pc.png"))
    print("[预览] 另存时钟页/设置页/电脑页样张")


# ---------------------------------------------------------------- 主流程

def main():
    wallpaper = make_wallpaper()
    # 超采样画完再缩回目标尺寸（边缘才干净）。
    # 图标按 48 画、输出 34：磁贴只有 60 高，放不下 48 的图。
    icons = [i.resize((ICON_OUT, ICON_OUT), Image.LANCZOS)
             for i in (icon_mic(), icon_clock(), icon_bars(), icon_gear(),
                       icon_pc(), icon_alarm())]
    glyphs = [g.resize((DIGIT_W, DIGIT_H), Image.LANCZOS)
              for g in ([_glyph(c) for c in "0123456789"] + [_glyph(":")])]

    lines = []
    lines.append("// 由 make_assets.py 自动生成，不要手改")
    lines.append("#pragma once")
    lines.append("#include <lvgl.h>")
    lines.append("")
    lines.append("#define PC_ASSETS_OK 1")
    lines.append("#define PC_WALLPAPER_W %d" % W)
    lines.append("#define PC_WALLPAPER_H %d" % H)
    lines.append("#define PC_ICON_W %d" % ICON_OUT)
    lines.append("#define PC_ICON_H %d" % ICON_OUT)
    lines.append("#define PC_DIGIT_W %d" % DIGIT_W)
    lines.append("#define PC_DIGIT_H %d" % DIGIT_H)
    lines.append("#define PC_DIGIT_COUNT %d" % len(glyphs))
    lines.append("")

    body = []
    import io
    buf = io.StringIO()

    wall_bytes = _rgb565_bytes(wallpaper)
    _emit_array(buf, "pc_wallpaper_map", wall_bytes)

    icon_bytes = b"".join(_rgb565a8_bytes(i) for i in icons)
    _emit_array(buf, "pc_icons_map", icon_bytes)

    glyph_bytes = b"".join(_rgb565a8_bytes(g) for g in glyphs)
    _emit_array(buf, "pc_digits_map", glyph_bytes)

    with open(OUT_H, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
        f.write(buf.getvalue())

    print("[完成] 已生成 %s" % OUT_H)
    print("       壁纸 %d 字节，图标 %d 字节，字符 %d 字节，合计 %.0f KB"
          % (len(wall_bytes), len(icon_bytes), len(glyph_bytes),
             (len(wall_bytes) + len(icon_bytes) + len(glyph_bytes)) / 1024))

    if "--preview" in sys.argv:
        build_mock(wallpaper, icons, glyphs)


if __name__ == "__main__":
    main()
