# -*- coding: utf-8 -*-
"""字体子集化：思源宋体 SemiBold -> GB2312 全汉字 + ASCII + 常用标点（~2.6MB）。
用法: py tools/subset_font.py"""
import io
from pathlib import Path
from fontTools import subset

FONT = Path(__file__).resolve().parent.parent / "asset" / "font" / "SourceHanSerifCN-SemiBold-7.ttf"
OUT = FONT.with_name("SourceHanSerifCN-SemiBold-subset.ttf")


def main():
    chars = set()
    for hi in range(0xB0, 0xF8):          # GB2312 汉字区 6763 字
        for lo in range(0xA1, 0xFF):
            try:
                chars.add(bytes([hi, lo]).decode("gb2312"))
            except UnicodeDecodeError:
                pass
    for hi in range(0xA1, 0xA8):          # GB2312 符号区
        for lo in range(0xA1, 0xFF):
            try:
                chars.add(bytes([hi, lo]).decode("gb2312"))
            except UnicodeDecodeError:
                pass
    chars |= {chr(c) for c in range(0x20, 0x7F)}
    # GB2312 收录不到的常用口语字（如「欸」连 GBK 都没有），卡面文案高频出现，手动补齐
    chars |= set("欸诶")
    chars |= set("，。！？、；：…～—「」『』（）〔〕【】《》〈〉·ー～ｰ✦★☆♪")
    print(f"charset: {len(chars)} 字")

    opts = subset.Options()
    opts.layout_features = ["*"]
    opts.name_IDs = [1, 2]
    opts.notdef_outline = True
    font = subset.load_font(str(FONT), opts)
    s = subset.Subsetter(opts)
    s.populate(text="".join(chars))
    s.subset(font)
    buf = io.BytesIO()
    font.save(buf)
    # 直接 'wb' 会撞上运行中 bot 进程的字体句柄（无写共享，Errno 22）；
    # 句柄允许删除共享，先写临时文件再原子替换即可
    tmp = OUT.with_suffix(".ttf.new")
    tmp.write_bytes(buf.getvalue())
    tmp.replace(OUT)
    print(f"-> {OUT} {OUT.stat().st_size / 1048576:.2f} MB")


if __name__ == "__main__":
    main()
