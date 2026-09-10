"""Native launcher presentation using the Web workbench's light-theme tokens.

No server, file access or task execution here; lifecycle remains in launcher.py.
"""
import tkinter as tk
from tkinter import ttk

COLORS = {
    "bg": "#f5f5f7", "surface": "#ffffff", "fg": "#333338",
    "strong": "#1d1d1f", "muted": "#616169", "line": "#d6d6dc",
    "soft": "#efeff2", "accent": "#c71f46", "hover": "#c3163e",
    "success": "#07733f", "success_bg": "#edf7f1",
    "warning": "#925600", "warning_bg": "#fff5e7",
}


def brand_image(size=64):
    """Rasterize the existing magnifier mark for native titlebar/tray/EXE icons."""
    from PIL import Image, ImageDraw
    image = Image.new("RGBA", (256, 256), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle((2, 2, 254, 254), radius=54, fill="#ffffff")
    draw.ellipse((38, 34, 190, 186), outline="#25c9ce", width=14)
    draw.ellipse((54, 34, 206, 186), outline=COLORS["accent"], width=14)
    draw.ellipse((102, 82, 157, 137), outline=COLORS["accent"], width=12)
    draw.line((183, 174, 223, 220), fill=COLORS["accent"], width=16)
    return image.resize((size, size), Image.Resampling.LANCZOS)


class Card(tk.Frame):
    """Rounded decorative background; real native widgets retain keyboard focus."""
    def __init__(self, parent, padding=22):
        super().__init__(parent, bg=COLORS["bg"])
        self.canvas = tk.Canvas(self, highlightthickness=0, bd=0, bg=COLORS["bg"])
        self.canvas.place(x=0, y=0, relwidth=1, relheight=1)
        self.body = ttk.Frame(self, style="Card.TFrame")
        self.body.pack(fill="both", expand=True, padx=padding, pady=padding)
        self.bind("<Configure>", self.redraw)

    def redraw(self, event):
        w, h, r = event.width - 1, event.height - 1, 14
        self.canvas.delete("all")
        self.canvas.create_polygon(r, 1, w-r, 1, w, 1, w, r, w, h-r,
            w, h, w-r, h, r, h, 1, h, 1, h-r, 1, r, 1, 1,
            smooth=True, splinesteps=24, fill=COLORS["surface"], outline=COLORS["line"])


def configure_styles(root):
    from PIL import Image, ImageDraw, ImageTk
    root.configure(bg=COLORS["bg"])
    root.option_add("*Font", ("Microsoft YaHei UI", 10))
    style = ttk.Style(root)
    # The Windows native theme ignores button colors; clam honors Web tokens.
    style.theme_use("clam")
    root._button_images = []
    style.configure("Page.TFrame", background=COLORS["bg"])
    style.configure("Card.TFrame", background=COLORS["surface"])
    for name, size, weight, fg, bg in (
        ("Title", 22, "bold", "strong", "bg"),
        ("Subtitle", 10, "normal", "muted", "bg"),
        ("Caption", 10, "normal", "muted", "surface"),
        ("Heading", 12, "bold", "strong", "surface"),
        ("Status", 18, "bold", "strong", "surface"),
        ("Body", 10, "normal", "fg", "surface"),
        ("Footer", 9, "normal", "muted", "bg"),
        ("Address", 11, "normal", "fg", "soft"),
    ):
        style.configure(f"{name}.TLabel", font=("Microsoft YaHei UI", size, weight),
                        foreground=COLORS[fg], background=COLORS[bg])
    for name, fg, bg in (("Idle", "muted", "soft"), ("Ready", "success", "success_bg"),
                         ("Busy", "warning", "warning_bg"), ("Error", "accent", "warning_bg")):
        style.configure(f"{name}.TLabel", foreground=COLORS[fg], background=COLORS[bg],
                        padding=(12, 5), font=("Microsoft YaHei UI", 10, "bold"))
    for name in ("Primary", "Secondary", "Quiet", "Danger"):
        primary = name == "Primary"
        bg = COLORS["accent"] if primary else COLORS["surface"]
        fg = "#ffffff" if primary else COLORS["accent"] if name == "Danger" else COLORS["fg"]
        style.configure(f"{name}.TButton", font=("Microsoft YaHei UI", 10, "bold" if primary else "normal"),
                        padding=(18, 11), background=bg, foreground=fg,
                        bordercolor=bg if primary or name == "Quiet" else COLORS["line"],
                        lightcolor=bg, darkcolor=bg, focuscolor=COLORS["accent"],
                        borderwidth=1, relief="flat", focusthickness=2)
        style.map(f"{name}.TButton", background=[("disabled", COLORS["soft"]),
                  ("pressed", COLORS["hover"] if primary else "#e7e7eb"),
                  ("active", COLORS["hover"] if primary else COLORS["soft"])],
                  foreground=[("disabled", "#898992")],
                  bordercolor=[("focus", COLORS["accent"]), ("disabled", COLORS["line"])])
        # Nine-slice image skin gives native ttk buttons the Web's 8px radius,
        # while preserving native commands, disabled state and keyboard focus.
        images = {}
        for state in ("normal", "active", "pressed", "disabled", "focus"):
            fill = (COLORS["soft"] if state == "disabled" else
                    COLORS["hover"] if primary and state in {"active", "pressed"} else
                    COLORS["soft"] if not primary and state in {"active", "pressed"} else bg)
            border = COLORS["accent"] if state == "focus" else fill if primary or name == "Quiet" else COLORS["line"]
            image = Image.new("RGBA", (160, 160), (0, 0, 0, 0))
            draw = ImageDraw.Draw(image)
            draw.rounded_rectangle((3, 3, 157, 157), radius=32, fill=fill,
                                   outline=border, width=8 if state == "focus" else 4)
            photo = ImageTk.PhotoImage(image.resize((40, 40), Image.Resampling.LANCZOS), master=root)
            root._button_images.append(photo)
            images[state] = photo
        element = f"{name}.rounded"
        style.element_create(element, "image", images["normal"],
            ("disabled", images["disabled"]), ("pressed", images["pressed"]),
            ("focus", images["focus"]), ("active", images["active"]), border=10, sticky="nsew")
        style.layout(f"{name}.TButton", [(element, {"sticky": "nsew", "children": [
            ("Button.padding", {"sticky": "nsew", "children": [("Button.label", {"sticky": "nsew"})]})]})])
    style.configure("Launch.Horizontal.TProgressbar", background=COLORS["accent"],
                    troughcolor=COLORS["soft"], borderwidth=0, thickness=4)


class LauncherView:
    def __init__(self, owner, version):
        self.owner = owner
        root = owner.root
        root.title("CreatorHub · 启动管理器")
        root.geometry("840x670")
        root.minsize(780, 650)
        configure_styles(root)
        from PIL import ImageTk
        self.window_icon = ImageTk.PhotoImage(brand_image(), master=root)
        root.iconphoto(True, self.window_icon)
        page = ttk.Frame(root, style="Page.TFrame", padding=(28, 24))
        page.pack(fill="both", expand=True)
        page.columnconfigure(0, weight=1)

        header = ttk.Frame(page, style="Page.TFrame")
        header.grid(row=0, column=0, sticky="ew", pady=(0, 24))
        header.columnconfigure(1, weight=1)
        mark = tk.Canvas(header, width=48, height=48, bg=COLORS["bg"], highlightthickness=0)
        mark.grid(row=0, column=0, rowspan=2, padx=(0, 14))
        # Same magnifier mark as frontend/brand.svg, with Web brand accents.
        mark.create_oval(5, 5, 35, 35, outline="#25c9ce", width=3)
        mark.create_oval(9, 5, 39, 35, outline=COLORS["accent"], width=3)
        mark.create_oval(18, 15, 29, 26, outline=COLORS["accent"], width=2)
        mark.create_line(34, 32, 44, 43, fill=COLORS["accent"], width=3)
        ttk.Label(header, text="CreatorHub", style="Title.TLabel").grid(row=0, column=1, sticky="w")
        ttk.Label(header, text="启动管理器  /  你的本地内容工作台", style="Subtitle.TLabel").grid(row=1, column=1, sticky="w")
        ttk.Label(header, text=f"桌面版 {version}", style="Idle.TLabel").grid(row=0, column=2, rowspan=2)

        service = Card(page)
        service.grid(row=1, column=0, sticky="ew")
        body = service.body
        body.columnconfigure(0, weight=1)
        ttk.Label(body, text="本地服务", style="Heading.TLabel").grid(row=0, column=0, sticky="w")
        self.badge = ttk.Label(body, text="●  待启动", style="Idle.TLabel")
        self.badge.grid(row=0, column=1, sticky="e")
        owner.status = tk.StringVar(root, "准备启动工作台")
        self.status_label = ttk.Label(body, textvariable=owner.status, style="Status.TLabel", wraplength=670)
        self.status_label.grid(row=1, column=0, columnspan=2, sticky="w", pady=(18, 8))
        self.address = tk.StringVar(root, "本机访问地址将在启动后显示")
        ttk.Label(body, textvariable=self.address, style="Address.TLabel", padding=(12, 8)).grid(row=2, column=0, columnspan=2, sticky="ew")
        self.progress_row = ttk.Frame(body, style="Card.TFrame", height=12)
        self.progress_row.grid(row=3, column=0, columnspan=2, sticky="ew", pady=(10, 0))
        owner.progress = ttk.Progressbar(self.progress_row, mode="indeterminate", style="Launch.Horizontal.TProgressbar")
        owner.progress.pack(fill="x")
        self.progress_row.grid_remove()
        owner.detail = tk.StringVar(root, "首次启动需要联网准备浏览器组件，之后会自动打开面板。")
        self.detail_label = ttk.Label(body, textvariable=owner.detail, style="Caption.TLabel", wraplength=680)
        self.detail_label.grid(row=4, column=0, columnspan=2, sticky="w", pady=(12, 18))
        actions = ttk.Frame(body, style="Card.TFrame")
        actions.grid(row=5, column=0, columnspan=2, sticky="ew")
        owner.open_button = ttk.Button(actions, text="打开工作台  →", command=owner.open_panel, state="disabled", style="Primary.TButton", cursor="hand2")
        owner.open_button.pack(side="left", padx=(0, 10))
        owner.start_button = ttk.Button(actions, text="启动 / 重试", command=owner.start, style="Secondary.TButton", cursor="hand2")
        owner.start_button.pack(side="left", padx=(0, 10))
        ttk.Button(actions, text="最小化到托盘", command=owner.hide, style="Quiet.TButton", cursor="hand2").pack(side="right")

        utilities = Card(page, padding=20)
        utilities.grid(row=2, column=0, sticky="ew", pady=(16, 0))
        tools = utilities.body
        for column, (title, subtitle, action) in enumerate((
            ("使用指南", "按平台查看图文教程", owner.open_guide),
            ("数据目录", "配置、下载与账号资料", owner.open_data),
            ("诊断摘要", "导出不含账号的状态信息", owner.diagnostics),
        )):
            tools.columnconfigure(column, weight=1, uniform="tool")
            box = ttk.Frame(tools, style="Card.TFrame", padding=(8, 0))
            box.grid(row=0, column=column, sticky="nsew")
            ttk.Button(box, text=f"{title}  ↗", style="Quiet.TButton", command=action, cursor="hand2").pack(anchor="w")
            ttk.Label(box, text=subtitle, style="Caption.TLabel").pack(anchor="w", padx=18, pady=(2, 0))

        footer = ttk.Frame(page, style="Page.TFrame")
        footer.grid(row=3, column=0, sticky="ew", pady=(20, 0))
        footer.columnconfigure(0, weight=1)
        ttk.Label(footer, text="数据保存在本机 · 关闭网页不会停止任务", style="Footer.TLabel").grid(row=0, column=0, sticky="w")
        self.exit_button = ttk.Button(footer, text="停止并退出", command=owner.close, style="Danger.TButton", cursor="hand2")
        self.exit_button.grid(row=0, column=1, sticky="e")
        body.bind("<Configure>", lambda e: self.resize_text(e.width))

    def resize_text(self, width):
        width = max(350, width - 12)
        self.detail_label.configure(wraplength=width)
        self.status_label.configure(wraplength=width)

    def phase(self, phase, url=None):
        text, style = {
            "starting": ("●  启动中", "Busy"), "ready": ("●  运行中", "Ready"),
            "error": ("●  需要处理", "Error"), "stopping": ("●  停止中", "Busy"),
        }[phase]
        self.badge.configure(text=text, style=f"{style}.TLabel")
        if phase in {"starting", "stopping"}:
            self.progress_row.grid()
        else:
            self.progress_row.grid_remove()
        self.address.set(url or ("正在安全退出本地服务…" if phase == "stopping" else "本机访问地址将在启动后显示"))
