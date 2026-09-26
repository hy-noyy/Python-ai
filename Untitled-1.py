# -*- coding: utf-8 -*-
"""
批量图片处理工具
功能：
  1. 批量压缩（按质量 / 按长边尺寸 / 按文件大小）
  2. 批量裁剪（固定尺寸 / 按比例 + 九宫格裁剪位置）
  3. 批量加水印（文字 / 图片、九宫格定位 + 平铺、边距、透明度）

依赖：pip install Pillow
运行：python batch_image_tool.py
"""

import io
import os
import time
import queue
import threading
import traceback
import tkinter as tk
from tkinter import ttk, filedialog, messagebox, colorchooser

from PIL import Image, ImageDraw, ImageFont, ImageOps, ImageTk

# --------------------------------------------------------------------------
# 常量与工具函数
# --------------------------------------------------------------------------

SUPPORTED_EXT = {'.jpg', '.jpeg', '.png', '.webp', '.bmp', '.tif', '.tiff'}
WRITE_FORMATS = {'JPEG': '.jpg', 'PNG': '.png', 'WEBP': '.webp'}


def collect_images(folder, recursive=True):
    """收集文件夹下的所有图片文件"""
    result = []
    if not folder or not os.path.isdir(folder):
        return result
    if recursive:
        for root, _dirs, files in os.walk(folder):
            for f in files:
                if os.path.splitext(f)[1].lower() in SUPPORTED_EXT:
                    result.append(os.path.join(root, f))
    else:
        for f in os.listdir(folder):
            p = os.path.join(folder, f)
            if os.path.isfile(p) and os.path.splitext(f)[1].lower() in SUPPORTED_EXT:
                result.append(p)
    result.sort()
    return result


def flatten_alpha(im, bg=(255, 255, 255)):
    """把带透明通道的图合成到纯色背景上（JPEG 不支持透明）"""
    if im.mode in ('RGBA', 'LA') or (im.mode == 'P' and 'transparency' in im.info):
        im = im.convert('RGBA')
        bgim = Image.new('RGB', im.size, bg)
        bgim.paste(im, mask=im.split()[-1])
        return bgim
    if im.mode != 'RGB':
        return im.convert('RGB')
    return im


def encode_image(im, fmt, quality=90, png_colors=None):
    """把 PIL 图像编码成指定格式的字节流"""
    buf = io.BytesIO()
    if fmt == 'JPEG':
        im = flatten_alpha(im)
        im.save(buf, 'JPEG', quality=int(quality), optimize=True, progressive=True)
    elif fmt == 'WEBP':
        if im.mode not in ('RGB', 'RGBA'):
            im = im.convert('RGBA' if im.mode in ('LA', 'P', 'PA') else 'RGB')
        im.save(buf, 'WEBP', quality=int(quality), method=4)
    elif fmt == 'PNG':
        if png_colors:
            if im.mode not in ('RGB', 'RGBA', 'L'):
                im = im.convert('RGBA')
            if im.mode == 'RGBA':
                im = flatten_alpha(im)   # 量化会丢透明，先合到白底
            im = im.convert('P', palette=Image.ADAPTIVE, colors=int(png_colors))
        im.save(buf, 'PNG', optimize=True, compress_level=9)
    else:
        raise ValueError('不支持的格式: %s' % fmt)
    return buf.getvalue()


def resize_long_edge(im, max_long):
    """按长边限制等比缩放（不放大）"""
    w, h = im.size
    if max(w, h) <= max_long:
        return im
    scale = float(max_long) / max(w, h)
    nw = max(1, int(round(w * scale)))
    nh = max(1, int(round(h * scale)))
    return im.resize((nw, nh), Image.LANCZOS)


def _encode_under_once(im, fmt, target, min_quality):
    """尝试一次编码，成功返回 bytes，失败返回 None"""
    if fmt == 'PNG':
        data = encode_image(im, 'PNG')
        if len(data) <= target:
            return data
        for colors in (256, 192, 128, 96, 64, 48, 32, 24, 16, 8, 4, 2):
            d = encode_image(im, 'PNG', png_colors=colors)
            if len(d) <= target:
                return d
        return None

    lo, hi = max(1, int(min_quality)), 95
    if len(encode_image(im, fmt, quality=hi)) <= target:
        return encode_image(im, fmt, quality=hi)
    if len(encode_image(im, fmt, quality=lo)) > target:
        return None
    best = lo
    while lo <= hi:
        mid = (lo + hi) // 2
        if len(encode_image(im, fmt, quality=mid)) <= target:
            best = mid
            lo = mid + 1
        else:
            hi = mid - 1
    return encode_image(im, fmt, quality=best)


def encode_under_target(im, fmt, target_bytes, min_quality=15):
    """
    把图片压到不超过 target_bytes。
    返回 (最终使用的图像, bytes)。优先降质量，不行再逐步缩小尺寸。
    """
    work = im
    for _ in range(14):
        data = _encode_under_once(work, fmt, target_bytes, min_quality)
        if data is not None:
            return work, data
        nw, nh = int(work.width * 0.85), int(work.height * 0.85)
        if nw < 24 or nh < 24:
            break
        if (nw, nh) == work.size:
            break
        work = work.resize((nw, nh), Image.LANCZOS)
    return work, encode_image(work, fmt, quality=min_quality, png_colors=4)


def crop_to_ratio(im, ratio, pos_h, pos_v):
    """按目标宽高比裁剪出最大区域，pos_h/pos_v 决定裁剪位置"""
    w, h = im.size
    if w <= 0 or h <= 0:
        return im
    cur = float(w) / h
    if cur > ratio:
        ch = h
        cw = int(round(h * ratio))
    else:
        cw = w
        ch = int(round(w / ratio))
    cw = max(1, min(cw, w))
    ch = max(1, min(ch, h))

    if pos_h == 'left':
        x = 0
    elif pos_h == 'center':
        x = (w - cw) // 2
    else:
        x = w - cw

    if pos_v == 'top':
        y = 0
    elif pos_v == 'middle':
        y = (h - ch) // 2
    else:
        y = h - ch

    return im.crop((x, y, x + cw, y + ch))


def find_default_font():
    """在常见路径里找一个可用的中文字体"""
    candidates = [
        'C:/Windows/Fonts/msyh.ttc',
        'C:/Windows/Fonts/msyhbd.ttc',
        'C:/Windows/Fonts/simhei.ttf',
        'C:/Windows/Fonts/simsun.ttc',
        '/System/Library/Fonts/PingFang.ttc',
        '/System/Library/Fonts/STHeiti Medium.ttc',
        '/Library/Fonts/Arial Unicode.ttf',
        '/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc',
        '/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc',
        '/usr/share/fonts/truetype/wqy/wqy-microhei.ttc',
        '/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc',
        '/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf',
    ]
    for p in candidates:
        if os.path.exists(p):
            return p
    return ''


def load_font(path, size):
    """加载字体，失败时退回默认字体"""
    try:
        if path and os.path.exists(path):
            return ImageFont.truetype(path, int(size))
    except Exception:
        pass
    try:
        return ImageFont.load_default(size)      # Pillow >= 10.1
    except TypeError:
        return ImageFont.load_default()


def render_text_image(text, font, color, opacity=1.0):
    """把文字渲染成一张带透明背景的 RGBA 图片"""
    tmp = Image.new('RGBA', (1, 1))
    d = ImageDraw.Draw(tmp)
    try:
        bbox = d.textbbox((0, 0), text, font=font)
    except Exception:
        bbox = (0, 0, len(text) * 10, 20)
    tw = max(1, bbox[2] - bbox[0])
    th = max(1, bbox[3] - bbox[1])
    pad = max(2, int(th * 0.15))

    img = Image.new('RGBA', (tw + pad * 2, th + pad * 2), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    r, g, b = color
    a = int(255 * max(0.0, min(1.0, opacity)))
    d.text((pad - bbox[0], pad - bbox[1]), text, font=font, fill=(r, g, b, a))
    return img


def apply_opacity(img, opacity):
    """调整 RGBA 图片的整体透明度（0~1）"""
    if opacity >= 0.999:
        return img
    img = img.convert('RGBA')
    r, g, b, a = img.split()
    a = a.point(lambda v: int(v * opacity))
    return Image.merge('RGBA', (r, g, b, a))


def hex_to_rgb(s):
    s = s.strip().lstrip('#')
    if len(s) == 3:
        s = ''.join(c * 2 for c in s)
    try:
        return tuple(int(s[i:i + 2], 16) for i in (0, 2, 4))
    except Exception:
        return (255, 255, 255)


def calc_pos(canvas_size, wm_size, pos, margin_x, margin_y):
    """根据九宫格位置计算水印左上角坐标"""
    W, H = canvas_size
    w, h = wm_size
    ph, pv = pos.split('-')
    if ph == 'left':
        x = margin_x
    elif ph == 'center':
        x = (W - w) // 2
    else:
        x = W - w - margin_x
    if pv == 'top':
        y = margin_y
    elif pv == 'middle':
        y = (H - h) // 2
    else:
        y = H - h - margin_y
    return int(x), int(y)


def make_pos_grid(parent, var):
    """生成 3x3 九宫格单选按钮"""
    f = ttk.Frame(parent)
    labels = [['左上', '上', '右上'],
              ['左', '居中', '右'],
              ['左下', '下', '右下']]
    values = [['left-top', 'center-top', 'right-top'],
              ['left-middle', 'center-middle', 'right-middle'],
              ['left-bottom', 'center-bottom', 'right-bottom']]
    for r in range(3):
        for c in range(3):
            ttk.Radiobutton(f, text=labels[r][c], value=values[r][c],
                            variable=var, width=6).grid(row=r, column=c, padx=2, pady=2)
    return f


# --------------------------------------------------------------------------
# 基类：源文件夹 / 输出设置 / 日志 / 进度
# --------------------------------------------------------------------------

class BaseTab(ttk.Frame):
    def __init__(self, master):
        super().__init__(master, padding=10)
        self.msg_q = queue.Queue()
        self._running = False
        self._cancel = threading.Event()
        self._alive = True

        self.src_dir = tk.StringVar()
        self.recursive = tk.BooleanVar(value=True)
        self.out_mode = tk.StringVar(value='folder')     # overwrite / folder / suffix
        self.out_dir = tk.StringVar()
        self.suffix = tk.StringVar(value='_out')
        self.fmt_var = tk.StringVar(value='keep')        # keep / JPEG / PNG / WEBP

        self._build()
        self.after(80, self._poll)

    # ---------------- UI ----------------
    def _build(self):
        self._build_source()

        self.opt_frame = ttk.LabelFrame(self, text='处理选项', padding=10)
        self.opt_frame.pack(fill='x', pady=(8, 0))
        self.build_options(self.opt_frame)

        self._build_output()
        self._build_action()

    def _build_source(self):
        f = ttk.LabelFrame(self, text='源文件夹', padding=8)
        f.pack(fill='x')
        ttk.Entry(f, textvariable=self.src_dir).pack(side='left', fill='x', expand=True)
        ttk.Button(f, text='选择文件夹…', command=self._choose_src).pack(side='left', padx=4)
        ttk.Checkbutton(f, text='含子文件夹', variable=self.recursive,
                        command=self._refresh_count).pack(side='left', padx=4)
        self.count_lbl = ttk.Label(f, text='未选择', foreground='#666')
        self.count_lbl.pack(side='left', padx=6)

    def _build_output(self):
        f = ttk.LabelFrame(self, text='输出设置', padding=8)
        f.pack(fill='x', pady=(8, 0))

        row = ttk.Frame(f)
        row.pack(fill='x')
        ttk.Radiobutton(row, text='覆盖原图', variable=self.out_mode,
                        value='overwrite').pack(side='left')
        ttk.Radiobutton(row, text='保存到新文件夹', variable=self.out_mode,
                        value='folder').pack(side='left', padx=12)
        ttk.Radiobutton(row, text='原目录 + 文件名后缀', variable=self.out_mode,
                        value='suffix').pack(side='left')

        row2 = ttk.Frame(f)
        row2.pack(fill='x', pady=(6, 0))
        ttk.Label(row2, text='新文件夹：').pack(side='left')
        ttk.Entry(row2, textvariable=self.out_dir).pack(side='left', fill='x', expand=True)
        ttk.Button(row2, text='选择…', command=self._choose_out).pack(side='left', padx=4)
        ttk.Label(row2, text='后缀：').pack(side='left', padx=(10, 0))
        ttk.Entry(row2, textvariable=self.suffix, width=10).pack(side='left')

        row3 = ttk.Frame(f)
        row3.pack(fill='x', pady=(6, 0))
        ttk.Label(row3, text='输出格式：').pack(side='left')
        for txt, val in [('保持原格式', 'keep'), ('JPEG', 'JPEG'),
                         ('PNG', 'PNG'), ('WebP', 'WEBP')]:
            ttk.Radiobutton(row3, text=txt, variable=self.fmt_var,
                            value=val).pack(side='left', padx=4)

    def _build_action(self):
        f = ttk.Frame(self)
        f.pack(fill='x', pady=(10, 0))
        self.btn_start = ttk.Button(f, text='开始处理', command=self.start)
        self.btn_start.pack(side='left')
        self.btn_cancel = ttk.Button(f, text='取消', command=self._cancel_all,
                                     state='disabled')
        self.btn_cancel.pack(side='left', padx=6)
        self.progress = ttk.Progressbar(f, mode='determinate', length=380)
        self.progress.pack(side='left', padx=10, fill='x', expand=True)
        self.status_lbl = ttk.Label(f, text='就绪', width=14)
        self.status_lbl.pack(side='left')

        lf = ttk.LabelFrame(self, text='日志', padding=4)
        lf.pack(fill='both', expand=True, pady=(8, 0))
        self.log_txt = tk.Text(lf, height=8, wrap='word', state='disabled',
                               font=('Consolas', 9))
        sb = ttk.Scrollbar(lf, command=self.log_txt.yview)
        self.log_txt.configure(yscrollcommand=sb.set)
        sb.pack(side='right', fill='y')
        self.log_txt.pack(side='left', fill='both', expand=True)

    # ---------------- 供子类重写 ----------------
    def build_options(self, parent):
        raise NotImplementedError

    def process_file(self, src):
        raise NotImplementedError

    def on_source_changed(self):
        pass

    # ---------------- 事件 ----------------
    def _choose_src(self):
        d = filedialog.askdirectory(title='选择图片文件夹')
        if d:
            self.src_dir.set(d)
            self._refresh_count()
            self.on_source_changed()

    def _choose_out(self):
        d = filedialog.askdirectory(title='选择输出文件夹')
        if d:
            self.out_dir.set(d)

    def _refresh_count(self):
        n = len(collect_images(self.src_dir.get().strip(), self.recursive.get()))
        self.count_lbl.config(text='共 %d 张图片' % n)

    def _cancel_all(self):
        self._cancel.set()
        self.status_lbl.config(text='正在取消…')

    # ---------------- 输出路径 ----------------
    def target_format(self, src):
        f = self.fmt_var.get()
        if f != 'keep':
            return f
        ext = os.path.splitext(src)[1].lower()
        if ext in ('.jpg', '.jpeg'):
            return 'JPEG'
        if ext == '.png':
            return 'PNG'
        if ext == '.webp':
            return 'WEBP'
        return 'PNG'      # bmp / tiff 等统一转 PNG

    def make_out_path(self, src, fmt):
        """返回 (输出路径, 需要删除的旧文件或 None)"""
        out_ext = WRITE_FORMATS[fmt]
        d, name = os.path.split(src)
        stem, old_ext = os.path.splitext(name)
        mode = self.out_mode.get()

        if mode == 'overwrite':
            out = os.path.join(d, stem + out_ext)
            dup = os.path.join(d, name)
            need_del = dup if os.path.abspath(dup) != os.path.abspath(out) else None
            return out, need_del

        if mode == 'suffix':
            return os.path.join(d, stem + self.suffix.get() + out_ext), None

        base = self.out_dir.get().strip() or d
        if self.recursive.get():
            root = self.src_dir.get().strip()
            if root:
                rel = os.path.relpath(d, root)
                if rel not in ('.', ''):
                    base = os.path.join(base, rel)
        return os.path.join(base, stem + out_ext), None

    # ---------------- 运行 ----------------
    def start(self):
        if self._running:
            return
        src = self.src_dir.get().strip()
        if not src or not os.path.isdir(src):
            messagebox.showwarning('提示', '请先选择有效的源文件夹')
            return
        if self.out_mode.get() == 'folder':
            od = self.out_dir.get().strip()
            if not od:
                messagebox.showwarning('提示', '请选择输出文件夹')
                return
            if os.path.abspath(od) == os.path.abspath(src):
                if not messagebox.askyesno('确认', '输出文件夹与源文件夹相同，'
                                                   '同名文件会被覆盖，是否继续？'):
                    return
        files = collect_images(src, self.recursive.get())
        if not files:
            messagebox.showinfo('提示', '该文件夹内没有找到支持的图片')
            return

        self._running = True
        self._cancel.clear()
        self.btn_start.config(state='disabled')
        self.btn_cancel.config(state='normal')
        self.progress['value'] = 0
        self.progress['maximum'] = len(files)
        self.log_txt.config(state='normal')
        self.log_txt.delete('1.0', 'end')
        self.log_txt.config(state='disabled')
        self.append_log('开始处理，共 %d 张图片…' % len(files))

        t = threading.Thread(target=self._worker, args=(files,), daemon=True)
        t.start()

    def _worker(self, files):
        total = len(files)
        ok = fail = 0
        t0 = time.time()
        for i, src in enumerate(files, 1):
            if self._cancel.is_set():
                self.msg_q.put(('log', '>> 用户取消，已处理 %d/%d' % (i - 1, total)))
                break
            try:
                self.process_file(src)
                ok += 1
                self.msg_q.put(('log', '[%d/%d] ✓ %s' % (i, total, os.path.basename(src))))
            except Exception as e:
                fail += 1
                self.msg_q.put(('log', '[%d/%d] ✗ %s  —— %s'
                                % (i, total, os.path.basename(src), e)))
                self.msg_q.put(('log', '      ' + traceback.format_exc().splitlines()[-1]))
            self.msg_q.put(('progress', i, total))
        self.msg_q.put(('done', ok, fail, time.time() - t0))

    # ---------------- 主线程轮询 ----------------
    def _poll(self):
        if not self._alive:
            return
        try:
            while True:
                msg = self.msg_q.get_nowait()
                kind = msg[0]
                if kind == 'log':
                    self.append_log(msg[1])
                elif kind == 'progress':
                    self._set_progress(msg[1], msg[2])
                elif kind == 'done':
                    self._on_finished(msg[1], msg[2], msg[3])
        except queue.Empty:
            pass
        try:
            self.after(80, self._poll)
        except Exception:
            pass

    def append_log(self, text):
        self.log_txt.config(state='normal')
        self.log_txt.insert('end', text + '\n')
        if int(self.log_txt.index('end-1c').split('.')[0]) > 800:
            self.log_txt.delete('1.0', '200.0')
        self.log_txt.see('end')
        self.log_txt.config(state='disabled')

    def _set_progress(self, cur, total):
        self.progress['maximum'] = max(1, total)
        self.progress['value'] = cur
        self.status_lbl.config(text='%d / %d' % (cur, total))

    def _on_finished(self, ok, fail, elapsed):
        self._running = False
        self.btn_start.config(state='normal')
        self.btn_cancel.config(state='disabled')
        self.status_lbl.config(text='完成')
        self.append_log('—' * 46)
        self.append_log('完成：成功 %d 张，失败 %d 张，耗时 %.1f 秒' % (ok, fail, elapsed))
        if fail == 0 and ok > 0:
            messagebox.showinfo('完成', '全部处理完成！\n成功 %d 张，耗时 %.1f 秒' % (ok, elapsed))


# --------------------------------------------------------------------------
# 1. 批量压缩
# --------------------------------------------------------------------------

class CompressTab(BaseTab):
    def __init__(self, master):
        self.mode = tk.StringVar(value='quality')          # quality / size / filesize
        self.q_var = tk.DoubleVar(value=80)                # 质量模式
        self.maxlong_var = tk.StringVar(value='1920')      # 尺寸模式
        self.q2_var = tk.DoubleVar(value=85)               # 尺寸模式下的质量
        self.target_kb_var = tk.StringVar(value='500')     # 大小模式
        super().__init__(master)

    def build_options(self, parent):
        row = ttk.Frame(parent)
        row.pack(fill='x')
        ttk.Label(row, text='压缩模式：').pack(side='left')
        for txt, val in [('按质量', 'quality'), ('按尺寸', 'size'), ('按大小', 'filesize')]:
            ttk.Radiobutton(row, text=txt, variable=self.mode, value=val,
                            command=self._switch).pack(side='left', padx=6)

        box = ttk.Frame(parent)
        box.pack(fill='x', pady=(10, 0))

        # --- 按质量 ---
        self.p_quality = ttk.Frame(box)
        ttk.Label(self.p_quality, text='图片质量：').pack(side='left')
        self.q_scale = ttk.Scale(self.p_quality, from_=1, to=100, orient='horizontal',
                                 variable=self.q_var, length=340, command=self._on_q)
        self.q_scale.pack(side='left', padx=4)
        self.q_lbl = ttk.Label(self.p_quality, text='80 %', width=6)
        self.q_lbl.pack(side='left')
        ttk.Label(self.p_quality, text='（数值越小体积越小）',
                  foreground='#888').pack(side='left', padx=8)

        # --- 按尺寸 ---
        self.p_size = ttk.Frame(box)
        ttk.Label(self.p_size, text='长边最大像素：').pack(side='left')
        ttk.Entry(self.p_size, textvariable=self.maxlong_var, width=8).pack(side='left')
        ttk.Label(self.p_size, text='   输出质量：').pack(side='left')
        ttk.Scale(self.p_size, from_=1, to=100, orient='horizontal',
                  variable=self.q2_var, length=200,
                  command=lambda v: self.q2_lbl.config(text='%d %%' % int(float(v)))
                  ).pack(side='left', padx=4)
        self.q2_lbl = ttk.Label(self.p_size, text='85 %', width=6)
        self.q2_lbl.pack(side='left')
        ttk.Label(self.p_size, text='（短边自动等比缩放，不放大）',
                  foreground='#888').pack(side='left', padx=8)

        # --- 按大小 ---
        self.p_bytes = ttk.Frame(box)
        ttk.Label(self.p_bytes, text='单张不超过：').pack(side='left')
        ttk.Entry(self.p_bytes, textvariable=self.target_kb_var, width=8).pack(side='left')
        ttk.Label(self.p_bytes, text='KB').pack(side='left', padx=4)
        ttk.Label(self.p_bytes, text='（自动搜索最佳质量，必要时缩小尺寸）',
                  foreground='#888').pack(side='left', padx=8)

        self._switch()

    def _switch(self):
        for f in (self.p_quality, self.p_size, self.p_bytes):
            f.pack_forget()
        m = self.mode.get()
        if m == 'quality':
            self.p_quality.pack(fill='x')
        elif m == 'size':
            self.p_size.pack(fill='x')
        else:
            self.p_bytes.pack(fill='x')

    def _on_q(self, v):
        try:
            self.q_lbl.config(text='%d %%' % int(float(v)))
        except Exception:
            pass

    # ---------------- 处理单张 ----------------
    def process_file(self, src):
        fmt = self.target_format(src)
        out_path, to_delete = self.make_out_path(src, fmt)
        d = os.path.dirname(os.path.abspath(out_path))
        os.makedirs(d, exist_ok=True)

        with Image.open(src) as im:
            im = ImageOps.exif_transpose(im)
            im.load()

            mode = self.mode.get()
            if mode == 'quality':
                data = encode_image(im, fmt, quality=int(float(self.q_var.get())))
            elif mode == 'size':
                maxlong = int(float(self.maxlong_var.get() or 1920))
                if maxlong < 8:
                    raise ValueError('长边像素过小')
                res = resize_long_edge(im, maxlong)
                data = encode_image(res, fmt, quality=int(float(self.q2_var.get())))
            else:
                kb = float(self.target_kb_var.get() or 500)
                if kb <= 0:
                    raise ValueError('目标大小必须大于 0')
                _res, data = encode_under_target(im, fmt, int(kb * 1024))

            with open(out_path, 'wb') as f:
                f.write(data)

        if to_delete and os.path.exists(to_delete):
            try:
                os.remove(to_delete)
            except OSError:
                pass


# --------------------------------------------------------------------------
# 2. 批量裁剪
# --------------------------------------------------------------------------

class CropTab(BaseTab):
    def __init__(self, master):
        self.mode = tk.StringVar(value='fixed')            # fixed / ratio
        self.w_var = tk.StringVar(value='800')
        self.h_var = tk.StringVar(value='600')
        self.ratio_var = tk.StringVar(value='16:9')
        self.custom_ratio = tk.StringVar(value='16:9')
        self.pos_var = tk.StringVar(value='center-middle')
        self.q_var = tk.DoubleVar(value=92)
        super().__init__(master)

    def build_options(self, parent):
        row = ttk.Frame(parent)
        row.pack(fill='x')
        ttk.Label(row, text='裁剪模式：').pack(side='left')
        ttk.Radiobutton(row, text='固定尺寸', variable=self.mode, value='fixed',
                        command=self._switch).pack(side='left', padx=6)
        ttk.Radiobutton(row, text='按比例', variable=self.mode, value='ratio',
                        command=self._switch).pack(side='left', padx=6)

        box = ttk.Frame(parent)
        box.pack(fill='x', pady=(10, 0))

        # 固定尺寸
        self.p_fixed = ttk.Frame(box)
        ttk.Label(self.p_fixed, text='宽：').pack(side='left')
        ttk.Entry(self.p_fixed, textvariable=self.w_var, width=8).pack(side='left')
        ttk.Label(self.p_fixed, text=' px    高：').pack(side='left')
        ttk.Entry(self.p_fixed, textvariable=self.h_var, width=8).pack(side='left')
        ttk.Label(self.p_fixed, text=' px').pack(side='left')
        ttk.Label(self.p_fixed, text='（先按此比例裁剪，再缩放到该尺寸）',
                  foreground='#888').pack(side='left', padx=10)

        # 按比例
        self.p_ratio = ttk.Frame(box)
        ttk.Label(self.p_ratio, text='目标比例：').pack(side='left')
        cb = ttk.Combobox(self.p_ratio, textvariable=self.ratio_var, width=10,
                          state='readonly',
                          values=['1:1', '4:3', '3:4', '16:9', '9:16',
                                  '3:2', '2:3', 'custom'])
        cb.pack(side='left')
        cb.bind('<<ComboboxSelected>>', lambda e: self._switch())
        self.custom_entry = ttk.Entry(self.p_ratio, textvariable=self.custom_ratio, width=10)
        self.custom_lbl = ttk.Label(self.p_ratio, text='自定义(如 5:4)：')
        ttk.Label(self.p_ratio, text='（只裁剪，不缩放）',
                  foreground='#888').pack(side='left', padx=10)

        self._switch()

        # 裁剪位置
        posf = ttk.LabelFrame(parent, text='裁剪位置（智能居中请选“居中”）', padding=6)
        posf.pack(fill='x', pady=(10, 0))
        make_pos_grid(posf, self.pos_var).pack(side='left')

        qf = ttk.Frame(parent)
        qf.pack(fill='x', pady=(10, 0))
        ttk.Label(qf, text='输出质量：').pack(side='left')
        ttk.Scale(qf, from_=1, to=100, orient='horizontal', variable=self.q_var,
                  length=260, command=lambda v: self.q_lbl.config(text='%d %%' % int(float(v)))
                  ).pack(side='left', padx=4)
        self.q_lbl = ttk.Label(qf, text='92 %', width=6)
        self.q_lbl.pack(side='left')

    def _switch(self):
        self.p_fixed.pack_forget()
        self.p_ratio.pack_forget()
        if self.mode.get() == 'fixed':
            self.p_fixed.pack(fill='x')
        else:
            self.p_ratio.pack(fill='x')
            if self.ratio_var.get() == 'custom':
                self.custom_lbl.pack(side='left')
                self.custom_entry.pack(side='left')
            else:
                self.custom_lbl.pack_forget()
                self.custom_entry.pack_forget()

    def get_ratio(self):
        if self.mode.get() == 'fixed':
            w = float(self.w_var.get() or 800)
            h = float(self.h_var.get() or 600)
            if w <= 0 or h <= 0:
                raise ValueError('宽高必须大于 0')
            return w / h
        s = self.ratio_var.get()
        if s == 'custom':
            s = self.custom_ratio.get()
        if ':' not in s:
            raise ValueError('比例格式应为 16:9')
        a, b = s.split(':')
        a, b = float(a), float(b)
        if a <= 0 or b <= 0:
            raise ValueError('比例必须大于 0')
        return a / b

    def process_file(self, src):
        fmt = self.target_format(src)
        out_path, to_delete = self.make_out_path(src, fmt)
        os.makedirs(os.path.dirname(os.path.abspath(out_path)), exist_ok=True)

        ratio = self.get_ratio()
        ph, pv = self.pos_var.get().split('-')

        with Image.open(src) as im:
            im = ImageOps.exif_transpose(im)
            im.load()
            res = crop_to_ratio(im, ratio, ph, pv)
            if self.mode.get() == 'fixed':
                tw = int(float(self.w_var.get()))
                th = int(float(self.h_var.get()))
                res = res.resize((tw, th), Image.LANCZOS)
            data = encode_image(res, fmt, quality=int(float(self.q_var.get())))
            with open(out_path, 'wb') as f:
                f.write(data)

        if to_delete and os.path.exists(to_delete):
            try:
                os.remove(to_delete)
            except OSError:
                pass


# --------------------------------------------------------------------------
# 3. 批量加水印
# --------------------------------------------------------------------------

class WatermarkTab(BaseTab):
    def __init__(self, master):
        self.wm_type = tk.StringVar(value='text')        # text / image
        self.text_var = tk.StringVar(value='@我的品牌')
        self.font_path = tk.StringVar(value=find_default_font())
        self.color_var = tk.StringVar(value='#FFFFFF')
        self.opacity_var = tk.DoubleVar(value=60)        # 0-100
        self.scale_var = tk.DoubleVar(value=15)          # 水印宽度占图片宽度 %
        self.img_path = tk.StringVar()
        self.pos_var = tk.StringVar(value='right-bottom')
        self.margin_x_var = tk.DoubleVar(value=30)
        self.margin_y_var = tk.DoubleVar(value=30)
        self.tile_var = tk.BooleanVar(value=False)
        self.angle_var = tk.DoubleVar(value=30)
        self.q_var = tk.DoubleVar(value=92)
        self._photo = None
        self._preview_job = None
        super().__init__(master)

    def build_options(self, parent):
        # 1. 创建左右主容器
        main_paned = ttk.Frame(parent)
        main_paned.pack(fill='both', expand=True)

        left_panel = ttk.Frame(main_paned)
        left_panel.pack(side='left', fill='both', expand=True, padx=(0, 10))

        right_panel = ttk.Frame(main_paned)
        right_panel.pack(side='right', fill='y', padx=(10, 0))

        # --- 左侧所有设置项 ---
        top = ttk.Frame(left_panel)
        top.pack(fill='x')
        ttk.Label(top, text='水印类型：').pack(side='left')
        ttk.Radiobutton(top, text='文字水印', variable=self.wm_type, value='text',
                        command=self._switch).pack(side='left', padx=4)
        ttk.Radiobutton(top, text='图片水印 (PNG Logo)', variable=self.wm_type,
                        value='image', command=self._switch).pack(side='left', padx=4)

        box = ttk.Frame(left_panel)
        box.pack(fill='x', pady=(10, 0))

        # ---- 文字面板 ----
        self.p_text = ttk.LabelFrame(box, text='文字设置', padding=8)
        r1 = ttk.Frame(self.p_text)
        r1.pack(fill='x')
        ttk.Label(r1, text='水印文字：').pack(side='left')
        ttk.Entry(r1, textvariable=self.text_var, width=20).pack(side='left')
        ttk.Label(r1, text='  颜色：').pack(side='left')
        self.color_btn = tk.Button(r1, text='  ', bg=self.color_var.get(),
                                   width=4, relief='groove', command=self._pick_color)
        self.color_btn.pack(side='left', padx=4)
        ttk.Label(r1, text='（点击选色）', foreground='#888').pack(side='left')

        r2 = ttk.Frame(self.p_text)
        r2.pack(fill='x', pady=(6, 0))
        ttk.Label(r2, text='字体文件：').pack(side='left')
        ttk.Entry(r2, textvariable=self.font_path).pack(side='left', fill='x', expand=True)
        ttk.Button(r2, text='浏览…', command=self._pick_font).pack(side='left', padx=4)

        # ---- 图片面板 ----
        self.p_img = ttk.LabelFrame(box, text='图片设置', padding=8)
        ri = ttk.Frame(self.p_img)
        ri.pack(fill='x')
        ttk.Label(ri, text='Logo 文件：').pack(side='left')
        ttk.Entry(ri, textvariable=self.img_path).pack(side='left', fill='x', expand=True)
        ttk.Button(ri, text='浏览…', command=self._pick_logo).pack(side='left', padx=4)

        # ---- 通用参数 ----
        self.p_common = ttk.LabelFrame(box, text='大小 / 透明度', padding=8)
        c1 = ttk.Frame(self.p_common)
        c1.pack(fill='x')
        ttk.Label(c1, text='水印宽度占图片宽度：').pack(side='left')
        ttk.Scale(c1, from_=1, to=80, orient='horizontal', variable=self.scale_var,
                  length=200,
                  command=lambda v: (self.scale_lbl.config(text='%d %%' % int(float(v))),
                                     self.schedule_preview())
                  ).pack(side='left', padx=4)
        self.scale_lbl = ttk.Label(c1, text='15 %', width=6)
        self.scale_lbl.pack(side='left')

        c2 = ttk.Frame(self.p_common)
        c2.pack(fill='x', pady=(6, 0))
        ttk.Label(c2, text='透明度：').pack(side='left')
        ttk.Scale(c2, from_=0, to=100, orient='horizontal', variable=self.opacity_var,
                  length=200,
                  command=lambda v: (self.opacity_lbl.config(text='%d %%' % int(float(v))),
                                     self.schedule_preview())
                  ).pack(side='left', padx=4)
        self.opacity_lbl = ttk.Label(c2, text='60 %', width=6)
        self.opacity_lbl.pack(side='left')

        # ---- 位置 / 平铺 ----
        pf = ttk.LabelFrame(left_panel, text='摆放位置', padding=6)
        pf.pack(fill='x', pady=(10, 0))
        make_pos_grid(pf, self.pos_var).pack(side='left', padx=(0, 20))

        right = ttk.Frame(pf)
        right.pack(side='left', fill='x', expand=True)

        ttk.Checkbutton(right, text='平铺满屏（倾斜重复铺满整张图）',
                        variable=self.tile_var,
                        command=self.schedule_preview).pack(anchor='w')
        ang = ttk.Frame(right)
        ang.pack(fill='x', pady=(4, 0))
        ttk.Label(ang, text='平铺角度：').pack(side='left')
        ttk.Scale(ang, from_=-90, to=90, orient='horizontal', variable=self.angle_var,
                  length=150,
                  command=lambda v: (self.angle_lbl.config(text='%d°' % int(float(v))),
                                     self.schedule_preview())
                  ).pack(side='left', padx=4)
        self.angle_lbl = ttk.Label(ang, text='30°', width=6)
        self.angle_lbl.pack(side='left')

        mg = ttk.Frame(right)
        mg.pack(fill='x', pady=(6, 0))
        ttk.Label(mg, text='边距 X：').pack(side='left')
        ttk.Scale(mg, from_=0, to=400, orient='horizontal', variable=self.margin_x_var,
                  length=80,
                  command=lambda v: (self.mx_lbl.config(text=str(int(float(v)))),
                                     self.schedule_preview())
                  ).pack(side='left')
        self.mx_lbl = ttk.Label(mg, text='30', width=4)
        self.mx_lbl.pack(side='left')
        ttk.Entry(mg, textvariable=self.margin_x_var, width=5).pack(side='left', padx=(2, 10))

        ttk.Label(mg, text='边距 Y：').pack(side='left')
        ttk.Scale(mg, from_=0, to=400, orient='horizontal', variable=self.margin_y_var,
                  length=80,
                  command=lambda v: (self.my_lbl.config(text=str(int(float(v)))),
                                     self.schedule_preview())
                  ).pack(side='left')
        self.my_lbl = ttk.Label(mg, text='30', width=4)
        self.my_lbl.pack(side='left')
        ttk.Entry(mg, textvariable=self.margin_y_var, width=5).pack(side='left', padx=2)

        # ---- 输出质量 (移到左侧底部) ----
        qf = ttk.Frame(left_panel)
        qf.pack(fill='x', pady=(10, 0))
        ttk.Label(qf, text='输出质量：').pack(side='left')
        ttk.Scale(qf, from_=1, to=100, orient='horizontal', variable=self.q_var,
                  length=200, command=lambda v: self.q_lbl.config(text='%d %%' % int(float(v)))
                  ).pack(side='left', padx=4)
        self.q_lbl = ttk.Label(qf, text='92 %', width=6)
        self.q_lbl.pack(side='left')

        # --- 右侧预览面板 ---
        pvf = ttk.LabelFrame(right_panel, text='预览', padding=6)
        pvf.pack(fill='both', expand=True)
        
        # 右侧空间较小，改为 260x260 的画布
        self.canvas = tk.Canvas(pvf, width=260, height=260, bg='#2b2b2b', highlightthickness=0)
        self.canvas.pack(fill='both', expand=True)
        self.canvas.create_text(130, 130, text='（选择源文件夹后显示预览）', fill='#888')
        
        ttk.Button(pvf, text='刷新预览', command=self.update_preview).pack(pady=(6, 0), anchor='center')

        self._switch()
        for v in (self.text_var, self.color_var, self.pos_var, self.img_path,
                  self.font_path, self.tile_var):
            v.trace_add('write', lambda *a: self.schedule_preview())

    # ---------------- 面板切换 ----------------
    def _switch(self):
        self.p_text.pack_forget()
        self.p_img.pack_forget()
        if self.wm_type.get() == 'text':
            self.p_text.pack(fill='x', pady=(8, 0))
        else:
            self.p_img.pack(fill='x', pady=(8, 0))
        self.p_common.pack(fill='x', pady=(8, 0))

    def _pick_color(self):
        c = colorchooser.askcolor(color=self.color_var.get(), title='选择水印颜色')
        if c and c[1]:
            self.color_var.set(c[1])
            self.color_btn.config(bg=c[1])

    def _pick_font(self):
        p = filedialog.askopenfilename(
            title='选择字体文件',
            filetypes=[('字体文件', '*.ttf *.ttc *.otf'), ('所有文件', '*.*')])
        if p:
            self.font_path.set(p)

    def _pick_logo(self):
        p = filedialog.askopenfilename(
            title='选择 Logo 图片',
            filetypes=[('PNG 图片', '*.png'), ('所有图片', '*.png *.webp *.jpg *.jpeg')])
        if p:
            self.img_path.set(p)
            self.wm_type.set('image')
            self._switch()

    # ---------------- 水印生成 ----------------
    def build_watermark(self, base):
        """根据当前设置生成水印图（尺寸相对 base 原图），失败返回 None"""
        ratio = max(0.005, float(self.scale_var.get()) / 100.0)
        target_w = max(8, int(base.width * ratio))
        opacity = max(0.0, min(1.0, float(self.opacity_var.get()) / 100.0))

        if self.wm_type.get() == 'text':
            text = self.text_var.get()
            if not text.strip():
                return None
            f100 = load_font(self.font_path.get(), 100)
            tmp = Image.new('RGBA', (1, 1))
            bb = ImageDraw.Draw(tmp).textbbox((0, 0), text, font=f100)
            w100 = max(1, bb[2] - bb[0])
            size = max(8, int(round(100.0 * target_w / w100)))
            font = load_font(self.font_path.get(), size)
            rgb = hex_to_rgb(self.color_var.get())
            return render_text_image(text, font, rgb, opacity)

        # 图片水印
        p = self.img_path.get().strip()
        if not p or not os.path.exists(p):
            return None
        try:
            with Image.open(p) as logo:
                logo = logo.convert('RGBA')
                r = target_w / float(logo.width)
                nh = max(1, int(logo.height * r))
                logo = logo.resize((target_w, nh), Image.LANCZOS)
        except Exception:
            return None
        return apply_opacity(logo, opacity)

    def compose(self, base, wm, margin_scale=1.0):
        """把水印合成到 base 上，返回 RGBA 图"""
        base = base.convert('RGBA')
        layer = Image.new('RGBA', base.size, (0, 0, 0, 0))
        if wm is not None:
            if self.tile_var.get():
                angle = float(self.angle_var.get())
                rot = wm.rotate(angle, expand=True, resample=Image.BICUBIC)
                gap = int(max(rot.size) * 0.8)
                step_x = rot.width + gap
                step_y = rot.height + gap
                y = -rot.height
                while y < base.height + rot.height:
                    x = -rot.width
                    while x < base.width + rot.width:
                        layer.alpha_composite(rot, (int(x), int(y)))
                        x += step_x
                    y += step_y
            else:
                mx = int(float(self.margin_x_var.get()) * margin_scale)
                my = int(float(self.margin_y_var.get()) * margin_scale)
                x, y = calc_pos(base.size, wm.size, self.pos_var.get(), mx, my)
                layer.alpha_composite(wm, (x, y))
        return Image.alpha_composite(base, layer)

    # ---------------- 预览 ----------------
    def on_source_changed(self):
        self.schedule_preview()

    def schedule_preview(self, *args):
        if self._preview_job:
            try:
                self.after_cancel(self._preview_job)
            except Exception:
                pass
        self._preview_job = self.after(300, self.update_preview)

    def _preview_source(self):
        d = self.src_dir.get().strip()
        if d and os.path.isdir(d):
            files = collect_images(d, self.recursive.get())
            if files:
                return files[0]
        return None

    def update_preview(self):
        self._preview_job = None
        src = self._preview_source()
        
        # 动态获取右侧 Canvas 的当前尺寸
        cw = self.canvas.winfo_width()
        ch = self.canvas.winfo_height()
        if cw <= 1: cw = 260
        if ch <= 1: ch = 260

        if not src:
            self.canvas.delete('all')
            self.canvas.create_text(cw//2, ch//2, text='（选择源文件夹后显示预览）', fill='#888')
            return
        try:
            with Image.open(src) as im:
                im = ImageOps.exif_transpose(im).convert('RGBA')
        except Exception:
            return

        # 按 Canvas 实际尺寸等比缩放
        r = min(cw / float(im.width), ch / float(im.height), 1.0)
        tw = max(1, int(im.width * r))
        th = max(1, int(im.height * r))
        thumb = im.resize((tw, th), Image.LANCZOS)

        wm = self.build_watermark(im)
        wmt = None
        if wm is not None:
            wmt = wm.resize((max(1, int(wm.width * r)), max(1, int(wm.height * r))),
                            Image.LANCZOS)

        try:
            out = self.compose(thumb, wmt, margin_scale=r)
        except Exception:
            out = thumb

        self._photo = ImageTk.PhotoImage(out)
        self.canvas.delete('all')
        # 将图片居中绘制
        x = (cw - tw) // 2
        y = (ch - th) // 2
        self.canvas.create_image(x, y, anchor='nw', image=self._photo)

    # ---------------- 处理单张 ----------------
    def process_file(self, src):
        fmt = self.target_format(src)
        out_path, to_delete = self.make_out_path(src, fmt)
        os.makedirs(os.path.dirname(os.path.abspath(out_path)), exist_ok=True)

        with Image.open(src) as im:
            im = ImageOps.exif_transpose(im)
            im.load()
            wm = self.build_watermark(im)
            if wm is None:
                raise ValueError('水印内容为空或 Logo 文件无效')
            out = self.compose(im, wm, margin_scale=1.0)
            data = encode_image(out, fmt, quality=int(float(self.q_var.get())))
            with open(out_path, 'wb') as f:
                f.write(data)

        if to_delete and os.path.exists(to_delete):
            try:
                os.remove(to_delete)
            except OSError:
                pass


# --------------------------------------------------------------------------
# 主窗口
# --------------------------------------------------------------------------

class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title('批量图片处理工具 —— 压缩 / 裁剪 / 加水印')
        self.geometry('1100x850')  # 稍微加宽，给右侧预览留出空间
        self.minsize(900, 720)

        try:
            style = ttk.Style()
            if 'clam' in style.theme_names():
                style.theme_use('clam')
        except Exception:
            pass

        nb = ttk.Notebook(self)
        nb.pack(fill='both', expand=True, padx=8, pady=8)
        nb.add(CompressTab(nb), text='  批量压缩  ')
        nb.add(CropTab(nb), text='  批量裁剪  ')
        nb.add(WatermarkTab(nb), text='  批量加水印  ')

        self.protocol('WM_DELETE_WINDOW', self._on_close)

    def _on_close(self):
        for child in self.winfo_children():
            for tab in getattr(child, 'tabs', lambda: [])():
                pass
        self.destroy()


def main():
    app = App()
    app.mainloop()


if __name__ == '__main__':
    main()