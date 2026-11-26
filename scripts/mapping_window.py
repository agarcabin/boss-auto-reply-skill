"""Boss 回复自动化 daily cloud mapping window."""
import argparse
import ctypes
from pathlib import Path
import threading
import time
import tkinter as tk
from tkinter import messagebox, ttk

from cloud import Cloud
from daily import fingerprint, normalize_inventory, today, validate_mapping
from preferences import DEFAULT_ROOT, Preferences
import reply


BG = '#f3f6f8'
INK = '#173044'
MUTED = '#667a89'
NAVY = '#123d5a'
TEAL = '#0b7f8f'
BORDER = '#d8e2e8'
HEADER_BG = '#eaf1f5'
MAX_VISIBLE_ROWS = 5.5
TABLE_COLUMNS = (
    ('开启', 0, 72),
    ('BOSS 岗位名称 / 状态', 3, 300),
    ('云端企业', 2, 210),
    ('云端岗位', 2, 210),
)


def visible_row_count(job_count):
    return min(float(max(int(job_count), 1)), MAX_VISIBLE_ROWS)


class MappingWindow:
    def __init__(self, root, inventory_path, state=None):
        self.root = root
        self.state = Path(state or reply.STATE)
        self.inventory = reply.load(inventory_path)
        if not self.inventory.get('inventory_hash'):
            self.inventory = normalize_inventory(self.inventory)
        self.cfg = reply.config()
        self.config_root = Path(self.cfg.get('job_config_root') or DEFAULT_ROOT).expanduser()
        self.preferences = Preferences(self.config_root)
        self.cloud_catalog = {}
        self.projects = []
        self.rows = []
        self.cloud_ready = False
        self.auto_started = False
        self.auto_start_deadline = time.monotonic() + 60
        self._configure_window()
        self._build()
        self.root.after_idle(self._fit_window_height)
        threading.Thread(target=self.fetch_cloud, daemon=True).start()
        self.root.after(1000, self.poll_startup)
        self.root.after(500, self.poll_auto_start)

    def _configure_window(self):
        self.root.title('Boss 回复自动化 · 今日岗位对应')
        self.root.configure(bg=BG)
        self.root.geometry('1120x1')
        self.root.minsize(940, 1)
        style = ttk.Style(self.root)
        if 'vista' in style.theme_names():
            style.theme_use('vista')
        style.configure('Mapping.TCombobox', padding=5)
        style.configure('Mapping.TCheckbutton', background='#ffffff')

    def _build(self):
        header = tk.Frame(self.root, bg=NAVY, height=126)
        header.pack(fill='x')
        header.pack_propagate(False)
        header.grid_columnconfigure(0, weight=1)
        tk.Label(header, text='Boss 回复自动化', bg=NAVY, fg='white',
                 font=('Microsoft YaHei UI', 20, 'bold')).grid(
                     row=0, column=0, sticky='w', padx=28, pady=(18, 3))
        tk.Label(header, text='确认今日 BOSS 岗位对应的云端企业及岗位',
                 bg=NAVY, fg='#c9dbe7', font=('Microsoft YaHei UI', 10)).grid(
                     row=1, column=0, sticky='w', padx=28, pady=(0, 14))

        content = tk.Frame(self.root, bg=BG)
        content.pack(fill='both', expand=True, padx=24, pady=18)

        meta = tk.Frame(content, bg='#ffffff', highlightbackground=BORDER, highlightthickness=1)
        meta.pack(fill='x')
        tk.Label(meta, text=f"{self.inventory['date']}   BOSS账号：{self.inventory['account']}",
                 bg='#ffffff', fg=INK, font=('Microsoft YaHei UI', 10, 'bold')).pack(side='left', padx=16, pady=13)
        tk.Label(meta, text=f'共 {len(self.inventory["jobs"])} 个在招或待核实岗位',
                 bg='#ffffff', fg=MUTED, font=('Microsoft YaHei UI', 9)).pack(side='right', padx=16)

        path_card = tk.Frame(content, bg='#eaf2f6')
        path_card.pack(fill='x', pady=(10, 0))
        tk.Label(path_card, text='岗位配置目录', bg='#eaf2f6', fg=MUTED,
                 font=('Microsoft YaHei UI', 9, 'bold')).pack(side='left', padx=(14, 8), pady=9)
        tk.Label(path_card, text=str(self.config_root), bg='#eaf2f6', fg=INK,
                 font=('Consolas', 9)).pack(side='left', pady=9)
        tk.Label(path_card, text='已自动创建', bg='#d8eee9', fg='#146b5b',
                 font=('Microsoft YaHei UI', 8, 'bold'), padx=8, pady=2).pack(side='right', padx=14)

        self.message = tk.StringVar(value='正在连接云端知识库…')
        self.message_label = tk.Label(content, textvariable=self.message, bg=BG, fg=MUTED,
                                      anchor='w', font=('Microsoft YaHei UI', 9))
        self.message_label.pack(fill='x', pady=(10, 8))

        table_card = tk.Frame(content, bg='#ffffff', highlightbackground=BORDER, highlightthickness=1)
        head = tk.Frame(table_card, bg=HEADER_BG)
        head.pack(fill='x')
        for column, (text, weight, minsize) in enumerate(TABLE_COLUMNS):
            head.grid_columnconfigure(column, weight=weight, minsize=minsize)
            tk.Label(head, text=text, bg=HEADER_BG, fg=INK,
                     anchor='w', font=('Microsoft YaHei UI', 9, 'bold')).grid(
                         row=0, column=column, sticky='ew',
                         padx=(14 if column == 0 else (0 if column == 1 else 8), 8), pady=10)

        canvas = tk.Canvas(table_card, bg='#ffffff', highlightthickness=0)
        scrollbar = ttk.Scrollbar(table_card, orient='vertical', command=canvas.yview)
        canvas.configure(yscrollcommand=scrollbar.set)
        self.canvas = canvas
        self.scrollbar = scrollbar
        if len(self.inventory['jobs']) > 5:
            scrollbar.pack(side='right', fill='y')
        canvas.pack(side='left', fill='both', expand=True)
        self.body = tk.Frame(canvas, bg='#ffffff')
        window_id = canvas.create_window((0, 0), window=self.body, anchor='nw')
        self.body.bind('<Configure>', lambda event: canvas.configure(scrollregion=canvas.bbox('all')))
        canvas.bind('<Configure>', lambda event: canvas.itemconfigure(window_id, width=event.width))
        for column, (_, weight, minsize) in enumerate(TABLE_COLUMNS):
            self.body.grid_columnconfigure(column, weight=weight, minsize=minsize)

        for index, boss in enumerate(self.inventory['jobs']):
            self._add_row(index, boss)

        footer = tk.Frame(content, bg=BG)
        footer.pack(side='bottom', fill='x', pady=(12, 0))
        tk.Label(footer, text='未开启的岗位不会自动回复。保存后仍需通过原生页面就绪检查。',
                 bg=BG, fg=MUTED, font=('Microsoft YaHei UI', 9)).pack(side='left')
        tk.Button(footer, text='取消', command=self.root.destroy, bg='#ffffff', fg=INK,
                  activebackground='#edf2f5', relief='solid', bd=1, padx=18, pady=7).pack(side='right')
        self.save_button = tk.Button(footer, text='开始挂机', command=self.save,
                                     bg=TEAL, fg='white', activebackground='#086c79',
                                     activeforeground='white', disabledforeground='#d9f0f3',
                                     relief='flat', padx=20, pady=8,
                                     state='disabled', font=('Microsoft YaHei UI', 9, 'bold'))
        self.save_button.pack(side='right', padx=(0, 10))
        table_card.pack(fill='both', expand=True)

    def _fit_window_height(self):
        self.canvas.configure(height=1)
        self.root.update_idletasks()
        base_height = max(1, self.root.winfo_reqheight() - self.canvas.winfo_reqheight())
        row_height = max(1, round(self.body.winfo_reqheight() / max(len(self.rows), 1)))
        visible = visible_row_count(len(self.rows))
        body_height = max(1, round(row_height * visible))
        self.canvas.configure(height=body_height)
        self.root.update_idletasks()
        height = max(1, base_height + body_height)
        self.root.minsize(940, height)
        x = max(0, (self.root.winfo_screenwidth() - 1120) // 2)
        y = max(0, (self.root.winfo_screenheight() - height) // 2)
        self.root.geometry(f'1120x{height}+{x}+{y}')

    def _add_row(self, index, boss):
        background = '#ffffff' if index % 2 == 0 else '#f8fafb'
        grid_row = index * 2

        enabled = tk.BooleanVar(value=False)
        cloud = tk.StringVar()
        position = tk.StringVar()
        check = tk.Checkbutton(self.body, variable=enabled, bg=background, activebackground=background,
                               command=lambda: self.persist_row(self.rows[-1]))
        check.grid(row=grid_row, column=0, padx=(16, 8), pady=16, sticky='w')
        if boss['status'] != 'active':
            check.configure(state='disabled')

        status = '开放中' if boss['status'] == 'active' else '上架状态待核实'
        if boss.get('visibility_warning'):
            status += ' · 曝光受限'
        title_box = tk.Frame(self.body, bg=background)
        title_box.grid(row=grid_row, column=1, sticky='ew', padx=(0, 8), pady=10)
        tk.Label(title_box, text=str(boss['title']).strip(), bg=background, fg=INK, anchor='w',
                 font=('Microsoft YaHei UI', 10, 'bold')).pack(anchor='w')
        tk.Label(title_box, text=status, bg=background, fg='#a05b10' if boss.get('visibility_warning') else MUTED,
                 anchor='w', font=('Microsoft YaHei UI', 8)).pack(anchor='w', pady=(3, 0))

        company_box = ttk.Combobox(self.body, textvariable=cloud, state='disabled',
                                   style='Mapping.TCombobox')
        company_box.grid(row=grid_row, column=2, sticky='ew', padx=8, pady=16)
        position_box = ttk.Combobox(self.body, textvariable=position, state='disabled',
                                    style='Mapping.TCombobox')
        position_box.grid(row=grid_row, column=3, sticky='ew', padx=(8, 16), pady=16)
        tk.Frame(self.body, bg=BORDER, height=1).grid(
            row=grid_row + 1, column=0, columnspan=4, sticky='ew')
        record = {'boss': boss, 'enabled': enabled, 'cloud': cloud, 'position': position,
                  'check': check, 'company_box': company_box, 'position_box': position_box}
        company_box.bind('<<ComboboxSelected>>', lambda event, row=record: self.change_company(row))
        position_box.bind('<<ComboboxSelected>>', lambda event, row=record: self.persist_row(row))
        self.rows.append(record)

    def fetch_cloud(self):
        try:
            cloud = Cloud(self.cfg)
            cloud.health()
            catalog = cloud.catalog()
            self.cloud_catalog = {
                item['name']: [position['name'] for position in item['positions']]
                for item in catalog['companies']
            }
            self.projects = sorted(self.cloud_catalog)
            self.cloud_ready = cloud.capabilities().get('stateless_hr_reply') == 1
            self.root.after(0, self.cloud_loaded)
        except Exception:
            self.root.after(0, lambda: self.message.set('云端知识库连接失败，暂不能启动自动化。'))

    def cloud_loaded(self):
        for row in self.rows:
            row['company_box']['values'] = self.projects
            row['company_box'].configure(state='readonly')
            saved = self.saved_choice(row['boss'])
            if saved.get('cloud_company') in self.cloud_catalog:
                row['cloud'].set(saved['cloud_company'])
                self.change_company(row, preferred=saved.get('cloud_position', ''))
                row['enabled'].set(bool(saved.get('enabled')))
        self.update_ready()

    def saved_choice(self, boss):
        saved = self.preferences.get(self.inventory['account'], boss)
        if saved:
            return saved
        previous = self.state / 'last-mapping.json'
        if not previous.exists():
            return {}
        data = reply.load(previous)
        if data.get('account') != self.inventory['account']:
            return {}
        matches = [record for record in data.get('mappings', []) if record.get('title') == boss['title']]
        if len(matches) != 1:
            return {}
        return {'cloud_company': matches[0].get('cloud_project'),
                'cloud_position': matches[0].get('cloud_position'),
                'enabled': matches[0].get('enabled', False)}

    def change_company(self, row, preferred=''):
        company = row['cloud'].get()
        positions = self.cloud_catalog.get(company, [])
        row['position_box']['values'] = positions
        row['position_box'].configure(state='readonly' if positions else 'disabled')
        selected = preferred if preferred in positions else (positions[0] if positions else '')
        row['position'].set(selected)
        self.persist_row(row)

    def persist_row(self, row):
        self.preferences.update(
            self.inventory['account'], row['boss'],
            cloud_company=row['cloud'].get(), cloud_position=row['position'].get(),
            enabled=row['enabled'].get(), confirmed=False)
        self.update_ready()

    def update_ready(self):
        if not self.cloud_ready:
            self.save_button.configure(state='disabled')
            return
        valid = any(row['enabled'].get() and row['cloud'].get() in self.cloud_catalog
                    and row['position'].get() in self.cloud_catalog[row['cloud'].get()]
                    for row in self.rows)
        self.save_button.configure(state='normal' if valid else 'disabled')
        if valid:
            self.message.set(f'云端知识库已连接，共 {len(self.projects)} 家企业。确认后保存并开始。')
            self.message_label.configure(fg='#146b5b')
        else:
            self.message.set('请开启至少一个岗位，并选择云端企业和云端岗位。')
            self.message_label.configure(fg=MUTED)

    def save(self, automatic=False):
        try:
            self.auto_started = True
            choices = [{
                'boss_key': row['boss']['key'],
                'enabled': row['enabled'].get(),
                'cloud_project': row['cloud'].get(),
                'cloud_position': row['position'].get(),
            } for row in self.rows]
            mapping = validate_mapping(self.inventory, self.cloud_catalog, choices, self.config_root)
            path = self.state / 'daily' / (today() + '-' + fingerprint(mapping['account'])[:12] + '.json')
            reply.save(path, mapping)
            reply.save(self.state / 'last-mapping.json', mapping)
            for row in self.rows:
                self.preferences.update(
                    self.inventory['account'], row['boss'],
                    cloud_company=row['cloud'].get(), cloud_position=row['position'].get(),
                    enabled=row['enabled'].get(), confirmed=True)
            cfg = reply.config()
            cfg['job_config_root'] = str(self.config_root)
            cfg['cloud_mapping_preferences'] = str(self.preferences.path)
            cfg['daily_mapping_file'] = str(path)
            cfg['allowed_jobs'] = sorted({row['cloud_project'] for row in mapping['mappings'] if row.get('enabled')})
            cfg['job_aliases'] = {row['title']: row['cloud_project'] for row in mapping['mappings'] if row.get('enabled')}
            cfg['mode'] = 'cloud'
            cfg['max_rounds'] = 0
            cfg['account'] = mapping['account']
            cfg['state_dir'] = str(self.state)
            reply.save(self.state / 'config.json', cfg)
            reply.save(self.state / 'start-request.json', {
                'date': today(), 'account': mapping['account'], 'mapping_file': str(path),
                'inventory_hash': mapping['inventory_hash'], 'send_authorized': True, 'state': 'requested'})
            self.message.set(('超过 1 分钟未操作，已按当前岗位对应自动开始。'
                              if automatic else '已保存。正在检查原生页面，就绪后开始回复自动化。'))
            self.message_label.configure(fg='#146b5b')
            self.save_button.configure(state='disabled')
        except (OSError, ValueError, KeyError) as exc:
            self.auto_started = False
            messagebox.showerror('尚不能开始', str(exc), parent=self.root)

    def poll_startup(self):
        status = self.state / 'startup-status.json'
        try:
            if status.exists():
                data = reply.load(status)
                if (data.get('account') == self.inventory['account']
                        and data.get('message') and data.get('state') != 'mapping'):
                    self.message.set(data['message'])
                    self.message_label.configure(
                        fg='#a05b10' if data.get('state') in ('blocked', 'waiting_for_boss') else '#146b5b')
                    if data.get('state') in ('starting', 'started'):
                        self.root.after(500, self.root.destroy)
                        return
        except (OSError, ValueError):
            pass
        self.root.after(1000, self.poll_startup)

    def poll_auto_start(self):
        if self.auto_started:
            return
        remaining = max(0, int(self.auto_start_deadline - time.monotonic() + .999))
        valid = self.cloud_ready and any(
            row['enabled'].get() and row['cloud'].get() in self.cloud_catalog
            and row['position'].get() in self.cloud_catalog[row['cloud'].get()]
            for row in self.rows)
        if valid:
            if remaining:
                self.message.set(f'已恢复上次选择；{remaining} 秒内可手动调整，超时后自动开始。')
            elif not self.auto_started:
                self.message.set('超过 1 分钟未操作，正在按当前岗位对应自动开始。')
                self.message_label.configure(fg='#146b5b')
                self.root.after(300, lambda: self.save(automatic=True))
        elif remaining:
            self.message.set(f'请在 {remaining} 秒内开启至少一个岗位；没有完整映射时不会自动发送。')
        self.root.after(500, self.poll_auto_start)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--inventory', required=True)
    args = parser.parse_args()
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except (AttributeError, OSError):
        pass
    root = tk.Tk()
    try:
        MappingWindow(root, args.inventory)
    except Exception as exc:
        root.withdraw()
        messagebox.showerror('Boss 回复自动化', f'岗位对应窗口无法启动：{exc}')
        return
    root.mainloop()


if __name__ == '__main__':
    main()
