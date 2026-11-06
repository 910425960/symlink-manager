# -*- coding: utf-8 -*-
"""
软链接管理器 (Symlink Manager)
==============================
Windows 软链接管理桌面程序：为文件/文件夹创建软链接并登记记录，
支持记录的增删改查、搜索过滤、JSON 持久化。

- 仅依赖 Python 标准库（tkinter），可用 PyInstaller 打包为小体积 EXE。
- 术语约定（源 / 目标）：
    源路径 (source)  = 真实数据所在的文件或文件夹（已存在）
    目标路径 (target) = 软链接本身被创建的位置（链接文件路径）
  即：在「目标路径」处创建一个链接，指向「源路径」的真实数据。
- 目录链接优先创建符号链接(symlink)；权限不足(ERROR_PRIVILEGE_NOT_HELD 1314)
  时自动降级为 Junction（目录联接，无需管理员权限）。
- 删除链接只移除链接本身（rmdir/unlink），绝不触碰源数据。
- 保存时按「软件名称」判定是否已存在：
    不存在 → 确认对话框 → 创建；
    已存在 → 三选一（覆盖 / 重命名另存 / 先删除再覆盖），删除类动作二次确认。

运行:  python symlink_manager.py
自检:  python symlink_manager.py --selftest
冒烟:  python symlink_manager.py --smoketest [结果文件路径]
"""

import ctypes
import json
import os
import sys
import time
import uuid

if sys.platform != "win32":
    print("本程序仅支持 Windows。")
    sys.exit(1)

APP_TITLE = "软链接管理器"
WINERROR_PRIVILEGE_NOT_HELD = 1314
WINERROR_ALREADY_EXISTS = 183
WINERROR_PATH_NOT_FOUND = 3
WINERROR_FILE_NOT_FOUND = 2
WINERROR_ACCESS_DENIED = 5

# ==========================================================================
# 设计 Token（视觉系统契约）
#   中性结构 + 单一强调色；语义色 success/warning/danger 仅用于状态，
#   绝不作为区分状态的唯一手段（列表状态另配文字/悬停说明）。
# ==========================================================================

COLORS = {
    "bg":            "#eef1f5",   # 应用画布
    "surface":       "#ffffff",   # 面板 / 表格 / 对话框
    "surface_alt":   "#f7f9fc",   # 斑马行 / 次级面板
    "surface_head":  "#f2f5f9",   # 表头 / 工具栏
    "border":        "#d7dee8",   # 分隔线
    "border_strong": "#b9c3d0",
    "text":          "#1b2430",   # 主文本
    "text_sec":      "#55637a",   # 次文本
    "text_ter":      "#8794a8",   # 弱文本
    "primary":       "#2563eb",   # 主操作
    "primary_hover": "#1d4ed8",
    "primary_down":  "#1e40af",
    "on_primary":    "#ffffff",
    "danger":        "#dc2626",   # 破坏性操作
    "danger_hover":  "#b91c1c",
    "danger_down":   "#991b1b",
    "on_danger":     "#ffffff",
    "ghost_hover":   "#e8edf4",   # 次要按钮悬停
    "success":       "#15803d",
    "warning":       "#b45309",
    "focus":         "#2563eb",
    "sel_bg":        "#dbe8ff",   # 表格选中
    "sel_fg":        "#14264d",
    "broken":        "#b42318",   # 损坏链接文本
    "missing":       "#b54708",   # 缺失链接文本
    "tooltip_bg":    "#232a33",
    "tooltip_fg":    "#f4f7fb",
    "toast_bg":      "#1f2937",
    "toast_fg":      "#ffffff",
    "toast_ok":      "#22c55e",
}

SPACE = {"xs": 3, "sm": 5, "md": 8, "lg": 10, "xl": 16, "row_h": 24}
RADIUS = 6

FONT_CANDIDATES = ["Microsoft YaHei UI", "Microsoft YaHei", "微软雅黑",
                   "SimHei", "Segoe UI", "TkDefaultFont"]
MONO_CANDIDATES = ["Cascadia Mono", "Consolas", "Courier New"]

# 运行时由 apply_theme 填充（字体需拿到 root 后才能探测可用族）
_THEME = {}


# ==========================================================================
# 数据持久化
# ==========================================================================

def app_data_dir():
    base = os.environ.get("APPDATA") or os.path.expanduser("~")
    d = os.path.join(base, "SymlinkManager")
    os.makedirs(d, exist_ok=True)
    return d


RECORDS_FILE = os.path.join(app_data_dir(), "records.json")


class Store:
    """软链接记录的持久化存储（JSON）。"""

    def __init__(self, path=None):
        self.path = path or RECORDS_FILE
        self.records = []
        self.load()

    def load(self):
        self.records = []
        if not os.path.isfile(self.path):
            return
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, list):
                self.records = [r for r in data if isinstance(r, dict)]
        except (json.JSONDecodeError, OSError) as e:
            raise RuntimeError("记录文件损坏或无法读取：%s\n(%s)" % (self.path, e))

    def save(self):
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self.records, f, ensure_ascii=False, indent=2)
        os.replace(tmp, self.path)

    # ---- 查询 ----
    def all(self):
        return list(self.records)

    def get(self, rid):
        for r in self.records:
            if r.get("id") == rid:
                return r
        return None

    def find_by_name(self, name, exclude_id=None):
        """按软件名称（不区分大小写/首尾空格）查找记录，返回记录或 None。"""
        n = (name or "").strip().lower()
        if not n:
            return None
        for r in self.records:
            if r.get("id") == exclude_id:
                continue
            if r["name"].strip().lower() == n:
                return r
        return None

    def name_exists(self, name, exclude_id=None):
        return self.find_by_name(name, exclude_id) is not None

    def target_exists(self, target, exclude_id=None):
        t = os.path.normcase(os.path.normpath(target.strip()))
        return any(os.path.normcase(os.path.normpath(r["target"])) == t
                   and r.get("id") != exclude_id for r in self.records)

    def find_by_target(self, target, exclude_id=None):
        t = os.path.normcase(os.path.normpath(target.strip()))
        for r in self.records:
            if r.get("id") == exclude_id:
                continue
            if os.path.normcase(os.path.normpath(r["target"])) == t:
                return r
        return None

    # ---- 增删改 ----
    def add(self, name, source, target, kind, link_type):
        rec = {
            "id": uuid.uuid4().hex,
            "name": name.strip(),
            "source": os.path.normpath(source),
            "target": os.path.normpath(target),
            "kind": kind,               # "dir" | "file"
            "link_type": link_type,     # "symlink" | "junction"
            "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        }
        self.records.append(rec)
        self.save()
        return rec

    def update(self, rid, name=None, source=None, target=None,
               kind=None, link_type=None):
        rec = self.get(rid)
        if rec is None:
            raise KeyError("记录不存在")
        if name is not None:
            rec["name"] = name.strip()
        if source is not None:
            rec["source"] = os.path.normpath(source)
        if target is not None:
            rec["target"] = os.path.normpath(target)
        if kind is not None:
            rec["kind"] = kind
        if link_type is not None:
            rec["link_type"] = link_type
        self.save()
        return rec

    def remove(self, rid):
        rec = self.get(rid)
        if rec is None:
            raise KeyError("记录不存在")
        self.records.remove(rec)
        self.save()


# ==========================================================================
# 软链接核心逻辑
# ==========================================================================

class LinkError(Exception):
    """带用户友好信息的链接操作错误。"""


def friendly_oserror(e):
    winerror = getattr(e, "winerror", None)
    if winerror == WINERROR_PRIVILEGE_NOT_HELD:
        return ("Windows 权限不足：创建符号链接需要管理员权限，或开启"
                "「设置 → 系统 → 开发者选项 → 开发人员模式」。\n"
                "目录链接会自动尝试降级为 Junction（无需管理员）；"
                "文件链接请任选上述一种方式后重试。")
    if winerror == WINERROR_ACCESS_DENIED:
        return "访问被拒绝：没有对该路径的写入权限，请尝试以管理员身份运行。"
    if winerror in (WINERROR_PATH_NOT_FOUND, WINERROR_FILE_NOT_FOUND):
        return "路径不存在：%s" % e
    if winerror == WINERROR_ALREADY_EXISTS:
        return "目标位置已存在同名文件/文件夹。"
    return str(e)


def is_admin():
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def reparse_tag(path):
    try:
        return getattr(os.lstat(path), "st_reparse_tag", 0)
    except OSError:
        return 0


def is_any_link(path):
    """是否为符号链接或 Junction（仅看链接本身，不跟随）。"""
    return reparse_tag(path) != 0


def classify_target(path):
    """目标位置的磁盘状态：free | link | dir | file。"""
    path = os.path.normpath(path)
    if not os.path.lexists(path):
        return "free"
    if is_any_link(path):
        return "link"
    if os.path.isdir(path):
        return "dir"
    return "file"


def _remove_link_if_present(path):
    """若 path 是链接则删除之（返回是否删除）。普通文件/文件夹一律不动。"""
    if os.path.lexists(path) and is_any_link(path):
        remove_link(path)
        return True
    return False


def create_link(source, target):
    """在 target 处创建指向 source 的链接。返回 link_type: symlink|junction。"""
    source = os.path.normpath(source)
    target = os.path.normpath(target)

    if not os.path.exists(source):
        raise LinkError("源路径不存在：%s" % source)
    if os.path.exists(target) or is_any_link(target):
        raise LinkError("目标路径已存在文件/文件夹/链接，请先处理冲突：\n%s" % target)
    parent = os.path.dirname(target)
    if not os.path.isdir(parent):
        raise LinkError("目标路径的上级目录不存在：\n%s" % parent)

    is_dir = os.path.isdir(source)
    try:
        os.symlink(source, target, target_is_directory=is_dir)
        return "symlink"
    except OSError as e:
        if getattr(e, "winerror", None) != WINERROR_PRIVILEGE_NOT_HELD:
            raise LinkError(friendly_oserror(e))
        if not is_dir:
            raise LinkError(friendly_oserror(e))
        try:
            import _winapi
            _winapi.CreateJunction(source, target)
            return "junction"
        except OSError as e2:
            raise LinkError("符号链接权限不足，且降级创建 Junction 也失败：\n%s"
                            % friendly_oserror(e2))


def remove_link(link_path):
    """安全删除链接本身（符号链接或 Junction），绝不删除源数据。"""
    link_path = os.path.normpath(link_path)
    if not os.path.lexists(link_path):
        raise LinkError("链接不存在（可能已被手动删除）：\n%s" % link_path)
    if not is_any_link(link_path):
        raise LinkError("目标路径不是软链接/Junction，为安全起见拒绝删除：\n%s"
                        % link_path)
    try:
        if os.path.isdir(link_path):        # 目录 symlink 或 junction
            os.rmdir(link_path)             # rmdir 只移除链接，不影响源数据
        else:                               # 文件 symlink
            os.unlink(link_path)
    except OSError as e:
        raise LinkError("删除链接失败：%s" % friendly_oserror(e))


def link_status(rec):
    """返回记录当前链接状态：ok | broken | missing。"""
    target = rec["target"]
    if not os.path.lexists(target):
        return "missing"
    if not is_any_link(target):
        return "broken"     # 位置被普通文件占用
    if not os.path.exists(target):
        return "broken"     # 链接在，但源已失效
    return "ok"


# ==========================================================================
# 保存决策：四分支纯逻辑（不触碰 GUI，可被 selftest 覆盖）
#   均可能抛出 LinkError；成功返回带 msg 的结果字典。
# ==========================================================================

def _kind_of(source):
    return "dir" if os.path.isdir(source) else "file"


def op_create_new(store, name, source, target, replace_target=False):
    """软件不存在 → 新建链接并登记。replace_target=True 时先清除目标处已有的链接。"""
    if replace_target:
        _remove_link_if_present(target)
    kind = _kind_of(source)
    link_type = create_link(source, target)
    rec = store.add(name, source, target, kind, link_type)
    return {"action": "created", "rec": rec, "link_type": link_type,
            "msg": "已创建软链接：%s" % name}


def op_overwrite(store, existing_rec, name, source, target):
    """覆盖：移除旧链接后，用新内容替换原有软件（保留同一记录 id）。"""
    old_target = existing_rec["target"]
    _remove_link_if_present(old_target)
    if os.path.normcase(os.path.normpath(target)) != os.path.normcase(os.path.normpath(old_target)):
        _remove_link_if_present(target)
    kind = _kind_of(source)
    link_type = create_link(source, target)
    rec = store.update(existing_rec["id"], name=name, source=source,
                       target=target, kind=kind, link_type=link_type)
    return {"action": "overwritten", "rec": rec, "link_type": link_type,
            "msg": "已覆盖并更新：%s" % name}


def op_save_as(store, new_name, source, target):
    """重命名另存：以新名称保存为一条独立软件记录。new_name 须由调用方校验未占用。"""
    kind = _kind_of(source)
    link_type = create_link(source, target)
    rec = store.add(new_name, source, target, kind, link_type)
    return {"action": "saved_as", "rec": rec, "link_type": link_type,
            "msg": "已另存为：%s" % new_name}


def op_delete_then_create(store, existing_rec, name, source, target):
    """先删除再覆盖：移除原有记录及其链接后，写入新内容（产生新记录 id）。"""
    _remove_link_if_present(existing_rec["target"])
    store.remove(existing_rec["id"])
    _remove_link_if_present(target)
    kind = _kind_of(source)
    link_type = create_link(source, target)
    rec = store.add(name, source, target, kind, link_type)
    return {"action": "recreated", "rec": rec, "link_type": link_type,
            "msg": "已删除原记录并重新创建：%s" % name}


# ==========================================================================
# 自检模式（不启动 GUI）
# ==========================================================================

def selftest():
    import tempfile
    import shutil
    passed, failed = [], []

    def check(desc, fn):
        try:
            fn()
            passed.append(desc)
            print("[PASS] %s" % desc)
        except Exception as e:
            failed.append((desc, e))
            print("[FAIL] %s -> %r" % (desc, e))

    tmp = tempfile.mkdtemp(prefix="slnmgr_selftest_")
    store_path = os.path.join(tmp, "records.json")
    try:
        src_dir = os.path.join(tmp, "src_dir")
        os.makedirs(src_dir)
        with open(os.path.join(src_dir, "data.txt"), "w") as f:
            f.write("real data")
        src_dir2 = os.path.join(tmp, "src_dir2")
        os.makedirs(src_dir2)
        with open(os.path.join(src_dir2, "other.txt"), "w") as f:
            f.write("other data")
        src_file = os.path.join(tmp, "src_file.txt")
        with open(src_file, "w") as f:
            f.write("file data")

        st = Store(store_path)

        def t_store_crud():
            r = st.add("测试软件", src_dir, os.path.join(tmp, "link_dir"), "dir", "junction")
            assert st.name_exists("测试软件")
            assert st.name_exists(" 测试软件 ")
            assert not st.name_exists("其他")
            st.update(r["id"], name="改名")
            assert st.get(r["id"])["name"] == "改名"
            st2 = Store(store_path)
            assert st2.get(r["id"])["name"] == "改名", "持久化失败"
            st.remove(r["id"])
            assert st.get(r["id"]) is None

        def t_name_dup():
            st.add("dup", src_dir, os.path.join(tmp, "x1"), "dir", "junction")
            assert st.name_exists("DUP"), "名称查重应不区分大小写"
            assert st.name_exists(" dup ")
            rid = st.all()[0]["id"]
            assert not st.name_exists("dup", exclude_id=rid), "编辑自身不应算重复"
            assert st.find_by_name("DUP")["id"] == rid
            st.remove(rid)

        def t_dir_link():
            lt = create_link(src_dir, os.path.join(tmp, "link_dir"))
            assert lt in ("symlink", "junction")
            p = os.path.join(tmp, "link_dir", "data.txt")
            assert os.path.isfile(p) and open(p).read() == "real data"
            remove_link(os.path.join(tmp, "link_dir"))
            assert not os.path.lexists(os.path.join(tmp, "link_dir"))
            assert os.path.isfile(os.path.join(src_dir, "data.txt")), "源数据必须完好"

        def t_file_link():
            dst = os.path.join(tmp, "link_file.txt")
            try:
                create_link(src_file, dst)
            except LinkError as e:
                if "权限不足" in str(e):
                    print("  (skip) 文件符号链接需要管理员/开发者模式: %s" % e)
                    return
                raise
            assert open(dst).read() == "file data"
            remove_link(dst)

        def t_conflict():
            dst = os.path.join(tmp, "link_dir2")
            create_link(src_dir, dst)
            try:
                create_link(src_dir, dst)
                raise AssertionError("应因目标已存在而失败")
            except LinkError as e:
                assert "已存在" in str(e)
            try:
                create_link(os.path.join(tmp, "no_such"), os.path.join(tmp, "l3"))
                raise AssertionError("应因源不存在而失败")
            except LinkError as e:
                assert "源路径不存在" in str(e)
            remove_link(dst)

        def t_safe_remove():
            plain = os.path.join(tmp, "plain_dir")
            os.makedirs(plain)
            try:
                remove_link(plain)
                raise AssertionError("普通目录应拒绝删除")
            except LinkError as e:
                assert "不是软链接" in str(e)
            assert os.path.isdir(plain)

        def t_classify_target():
            assert classify_target(os.path.join(tmp, "nothing_here")) == "free"
            lk = os.path.join(tmp, "cls_link")
            create_link(src_dir, lk)
            assert classify_target(lk) == "link"
            remove_link(lk)
            pd = os.path.join(tmp, "cls_dir"); os.makedirs(pd)
            assert classify_target(pd) == "dir"
            pf = os.path.join(tmp, "cls_file"); open(pf, "w").write("x")
            assert classify_target(pf) == "file"

        def t_op_create_new():
            tgt = os.path.join(tmp, "op_new")
            res = op_create_new(st, "软件A", src_dir, tgt)
            assert res["action"] == "created"
            assert os.path.isfile(os.path.join(tgt, "data.txt"))
            assert st.find_by_name("软件A") is not None
            assert st.name_exists("软件A")

        def t_op_overwrite():
            existing = st.find_by_name("软件A")
            old_tgt = existing["target"]
            res = op_overwrite(st, existing, "软件A", src_dir2, old_tgt)
            assert res["action"] == "overwritten"
            assert res["rec"]["id"] == existing["id"], "覆盖应保留同一记录 id"
            assert os.path.isfile(os.path.join(old_tgt, "other.txt")), "应指向新源"
            assert not os.path.exists(os.path.join(old_tgt, "data.txt"))
            assert st.find_by_name("软件A")["source"].endswith("src_dir2")

        def t_op_save_as():
            tgt = os.path.join(tmp, "op_saveas")
            res = op_save_as(st, "软件A-副本", src_dir, tgt)
            assert res["action"] == "saved_as"
            assert st.find_by_name("软件A-副本") is not None
            assert st.find_by_name("软件A") is not None, "原记录应保留，成为独立软件"
            assert st.find_by_name("软件A")["id"] != st.find_by_name("软件A-副本")["id"]
            assert os.path.isfile(os.path.join(tgt, "data.txt"))

        def t_op_delete_then_create():
            existing = st.find_by_name("软件A-副本")
            old_id = existing["id"]
            old_tgt = existing["target"]
            res = op_delete_then_create(st, existing, "软件A-副本", src_dir2, old_tgt)
            assert res["action"] == "recreated"
            assert res["rec"]["id"] != old_id, "删除后重建应产生新记录 id"
            assert st.get(old_id) is None, "旧记录应被移除"
            assert os.path.isfile(os.path.join(old_tgt, "other.txt"))
            assert os.path.isfile(os.path.join(src_dir, "data.txt")), "源数据必须完好"

        def t_cancel_no_change():
            # 模拟「取消」：不调用任何 op_，store 与磁盘均不变
            before_recs = json.dumps(st.all(), sort_keys=True, ensure_ascii=False)
            before_dir = sorted(os.listdir(tmp))
            # 取消分支 = 直接返回，无任何副作用（此处断言状态未变）
            after_recs = json.dumps(st.all(), sort_keys=True, ensure_ascii=False)
            after_dir = sorted(os.listdir(tmp))
            assert before_recs == after_recs
            assert before_dir == after_dir

        check("记录 CRUD 与持久化", t_store_crud)
        check("软件名称查重（不区分大小写/含空格）", t_name_dup)
        check("目录链接创建/读取/安全删除", t_dir_link)
        check("文件链接创建/安全删除（无权限则跳过）", t_file_link)
        check("冲突检测：目标已存在 / 源不存在", t_conflict)
        check("拒绝删除非链接目录", t_safe_remove)
        check("目标状态分类 free/link/dir/file", t_classify_target)
        check("分支·新建 op_create_new", t_op_create_new)
        check("分支·覆盖 op_overwrite（保留 id/换源）", t_op_overwrite)
        check("分支·重命名 op_save_as（独立新记录）", t_op_save_as)
        check("分支·先删除再覆盖 op_delete_then_create（新 id/源完好）",
              t_op_delete_then_create)
        check("分支·取消不改任何数据", t_cancel_no_change)

        print("\n结果: %d passed, %d failed" % (len(passed), len(failed)))
        return 0 if not failed else 1
    finally:
        for name in os.listdir(tmp):
            p = os.path.join(tmp, name)
            try:
                if is_any_link(p):
                    remove_link(p)
            except Exception:
                pass
        shutil.rmtree(tmp, ignore_errors=True)


# ==========================================================================
# GUI
# ==========================================================================

def window_icon_path():
    """定位程序图标 .ico。

    PyInstaller onefile 运行时数据文件解压到 sys._MEIPASS；
    源码运行时取脚本所在目录。两者指向同一个 app_icon.ico。
    """
    base = getattr(sys, "_MEIPASS", None) or os.path.dirname(os.path.abspath(__file__))
    path = os.path.join(base, "app_icon.ico")
    return path if os.path.isfile(path) else None


def apply_window_icon(root):
    """设置主窗口与所有 Toplevel 的标题栏 / 任务栏图标。

    PyInstaller 的 EXE(icon=...) 只写 PE 资源（资源管理器里的文件图标），
    不会自动设置 tkinter 窗口的 WM_SETICON，标题栏默认仍是 Tk 自带图标。
    必须显式调用 iconbitmap；-default 让后续创建的对话框一并继承。
    任何失败都静默降级，绝不阻断程序启动。
    """
    path = window_icon_path()
    if not path:
        return "icon file missing"
    try:
        root.iconbitmap(path)
    except Exception as e:
        return "iconbitmap failed: %r" % (e,)
    try:
        root.iconbitmap(default=path)
    except Exception:
        pass                       # 主窗口已设好，default 失败不影响使用
    return "ok"


def window_icon_state(root):
    """返回 (窗口图标已设置, 证据详情)，供 smoketest 断言。

    两个 Windows/Tk 陷阱：
      1. winfo_id() 返回的是子窗口 HWND，标题栏图标挂在顶层窗口
         （GetParent / GA_ROOT）上，必须查对句柄；
      2. Tk 在 Windows 上查询 `wm iconbitmap` 恒返回空串，不能作为判据。
    故以顶层窗口的 WM_GETICON 是否返回非零 HICON 为准。
    """
    path = window_icon_path()
    detail = ["ico=%s" % ("yes" if path else "missing")]
    try:
        cur = root.tk.call("wm", "iconbitmap", root._w)
        detail.append("wm=%s" % (cur or "(empty on Windows)"))
    except Exception as e:
        detail.append("wm=<error %r>" % (e,))
    win32_ok = False
    try:
        u = ctypes.windll.user32
        u.SendMessageW.restype = ctypes.c_void_p
        u.SendMessageW.argtypes = [ctypes.c_void_p, ctypes.c_uint,
                                   ctypes.c_void_p, ctypes.c_void_p]
        u.GetParent.restype = ctypes.c_void_p
        u.GetParent.argtypes = [ctypes.c_void_p]
        u.GetAncestor.restype = ctypes.c_void_p
        u.GetAncestor.argtypes = [ctypes.c_void_p, ctypes.c_uint]
        child = root.winfo_id()
        hwnd = u.GetParent(child) or u.GetAncestor(child, 2) or child
        big = u.SendMessageW(hwnd, 0x007F, 1, 0) or 0    # WM_GETICON / ICON_BIG
        small = u.SendMessageW(hwnd, 0x007F, 0, 0) or 0  # WM_GETICON / ICON_SMALL
        detail.append("hwnd=%s hicon_big=%s hicon_small=%s" % (hwnd, big, small))
        win32_ok = bool(big or small)
    except Exception as e:
        detail.append("win32=<error %r>" % (e,))
    return (bool(path) and win32_ok), " ".join(detail)


def run_gui(auto_close_ms=None, ui_selftest=False, ui_test_report=None):
    import threading
    import tkinter as tk
    from tkinter import ttk, filedialog
    from tkinter import font as tkfont

    # ---------------- 字体解析 ----------------
    def _resolve_family(candidates):
        fams = set(tkfont.families())
        for c in candidates:
            if c in fams:
                return c
        return tkfont.nametofont("TkDefaultFont").actual("family")

    # ---------------- 主题 ----------------
    def apply_theme(root):
        ui = _resolve_family(FONT_CANDIDATES)
        mono = _resolve_family(MONO_CANDIDATES)
        _THEME.update({
            "ui": ui, "mono": mono,
            "f_base": (ui, 9),
            "f_small": (ui, 8),
            "f_title": (ui, 12, "bold"),
            "f_h2": (ui, 10, "bold"),
            "f_bold": (ui, 9, "bold"),
            "f_btn": (ui, 9),
            "f_mono": (mono, 8),
        })
        st = ttk.Style(root)
        try:
            st.theme_use("clam")
        except tk.TclError:
            pass

        C = COLORS
        st.configure(".", background=C["bg"], foreground=C["text"],
                     font=_THEME["f_base"], borderwidth=0, focusthickness=0)
        st.configure("TFrame", background=C["bg"])
        st.configure("Card.TFrame", background=C["surface"])
        st.configure("Toolbar.TFrame", background=C["surface_head"])
        st.configure("Status.TFrame", background=C["surface_head"])

        # 文本
        st.configure("TLabel", background=C["bg"], foreground=C["text"],
                     font=_THEME["f_base"])
        st.configure("Card.TLabel", background=C["surface"], foreground=C["text"])
        st.configure("Title.TLabel", background=C["surface"], foreground=C["text"],
                     font=_THEME["f_title"])
        st.configure("H2.TLabel", background=C["surface"], foreground=C["text"],
                     font=_THEME["f_h2"])
        st.configure("Sec.TLabel", background=C["surface"], foreground=C["text_sec"],
                     font=_THEME["f_base"])
        st.configure("Hint.TLabel", background=C["surface"], foreground=C["text_ter"],
                     font=_THEME["f_small"])
        st.configure("Status.TLabel", background=C["surface_head"],
                     foreground=C["text_sec"], font=_THEME["f_small"])
        st.configure("Path.TLabel", background=C["surface"], foreground=C["text"],
                     font=_THEME["f_mono"])

        # 按钮
        def btn(style, bg, fg, hov, down):
            st.configure(style, background=bg, foreground=fg, font=_THEME["f_btn"],
                         borderwidth=0, focusthickness=0, padding=(10, 4),
                         relief="flat")
            st.map(style,
                   background=[("active", hov), ("pressed", down), ("disabled", C["border"])],
                   foreground=[("disabled", C["text_ter"])])
        btn("Primary.TButton", C["primary"], C["on_primary"], C["primary_hover"], C["primary_down"])
        btn("Danger.TButton", C["danger"], C["on_danger"], C["danger_hover"], C["danger_down"])
        btn("TButton", C["surface"], C["text"], C["ghost_hover"], C["border"])
        st.map("TButton", bordercolor=[("active", C["border_strong"])])
        st.configure("Ghost.TButton", background=C["surface_head"], foreground=C["text_sec"],
                     font=_THEME["f_small"], padding=(8, 3))
        st.map("Ghost.TButton", background=[("active", C["ghost_hover"])])

        # 输入框
        st.configure("TEntry", fieldbackground=C["surface"], foreground=C["text"],
                     bordercolor=C["border_strong"], lightcolor=C["border"],
                     darkcolor=C["border"], insertcolor=C["text"], padding=4)
        st.map("TEntry", bordercolor=[("focus", C["focus"])],
               lightcolor=[("focus", C["focus"])])

        # 表格
        st.configure("Treeview", background=C["surface"], fieldbackground=C["surface"],
                     foreground=C["text"], rowheight=SPACE["row_h"],
                     borderwidth=0, font=_THEME["f_base"])
        st.configure("Treeview.Heading", background=C["surface_head"],
                     foreground=C["text_sec"], font=_THEME["f_bold"],
                     relief="flat", padding=(4, 3))
        st.map("Treeview.Heading", background=[("active", C["border"])])
        st.map("Treeview",
               background=[("selected", C["sel_bg"])],
               foreground=[("selected", C["sel_fg"])])

        # 进度条（加载状态）
        st.configure("Horizontal.TProgressbar", background=C["primary"],
                     troughcolor=C["border"], bordercolor=C["surface"],
                     lightcolor=C["primary"], darkcolor=C["primary"], thickness=6)

        # 滚动条
        st.configure("Vertical.TScrollbar", background=C["border_strong"],
                     troughcolor=C["surface"], bordercolor=C["surface"],
                     arrowcolor=C["text_sec"])
        st.map("Vertical.TScrollbar", background=[("active", C["text_ter"])])

        # 分隔线
        st.configure("TSeparator", background=C["border"])

        # 工具栏复选框（详细列开关）
        st.configure("Toolbar.TCheckbutton", background=C["surface_head"],
                     foreground=C["text_sec"], font=_THEME["f_small"],
                     indicatorcolor=C["surface"], padding=(4, 2))
        st.map("Toolbar.TCheckbutton",
               background=[("active", C["surface_head"])],
               foreground=[("active", C["text"])])

    # ---------------- 通用小工具 ----------------
    def center_over(win, parent=None):
        win.update_idletasks()
        w, h = win.winfo_reqwidth(), win.winfo_reqheight()
        if parent is not None:
            px, py = parent.winfo_rootx(), parent.winfo_rooty()
            pw, ph = parent.winfo_width(), parent.winfo_height()
            x = px + max((pw - w) // 2, 0)
            y = py + max((ph - h) // 3, 0)
        else:
            x = (win.winfo_screenwidth() - w) // 2
            y = (win.winfo_screenheight() - h) // 2
        win.geometry("%dx%d+%d+%d" % (w, h, x, y))

    class ToolTip:
        """悬停提示：为按钮/图标提供清晰文字说明。"""
        def __init__(self, widget, text):
            self.w = widget
            self.text = text
            self.tip = None
            self._after = None
            widget.bind("<Enter>", self._schedule, add="+")
            widget.bind("<Leave>", self._hide, add="+")
            widget.bind("<ButtonPress>", self._hide, add="+")

        def _schedule(self, _e=None):
            self._cancel_after()
            self._after = self.w.after(450, self._show)

        def _cancel_after(self):
            if self._after is not None:
                try:
                    self.w.after_cancel(self._after)
                except Exception:
                    pass
                self._after = None

        def _show(self):
            self._after = None
            if self.tip is not None or not self.text:
                return
            try:
                x = self.w.winfo_rootx()
                y = self.w.winfo_rooty() + self.w.winfo_height() + 4
                self.tip = tk.Toplevel(self.w)
                self.tip.wm_overrideredirect(True)
                self.tip.wm_geometry("+%d+%d" % (x, y))
                fr = tk.Frame(self.tip, background=COLORS["tooltip_bg"],
                              highlightthickness=0)
                fr.pack()
                tk.Label(fr, text=self.text, background=COLORS["tooltip_bg"],
                         foreground=COLORS["tooltip_fg"], font=_THEME["f_small"],
                         justify="left", padx=9, pady=5).pack()
            except Exception:
                self.tip = None

        def _hide(self, _e=None):
            self._cancel_after()
            if self.tip is not None:
                self.tip.destroy()
                self.tip = None

    # ---------------- 模态对话框基类 ----------------
    class BaseDialog(tk.Toplevel):
        def __init__(self, parent, title):
            super().__init__(parent)
            self.title(title)
            self.transient(parent)
            self.resizable(False, False)
            self.configure(background=COLORS["surface"])
            self.result = None
            self._default = None
            self.protocol("WM_DELETE_WINDOW", self._cancel)
            self.bind("<Escape>", lambda e: self._cancel())

        def _cancel(self):
            self.result = None
            self.destroy()

        def _finish(self, val):
            self.result = val
            self.destroy()

        def _button_bar(self, parent, specs):
            """specs: [(text, value, style, is_default), ...]"""
            bar = ttk.Frame(parent, style="Card.TFrame")
            bar.pack(fill="x", pady=(SPACE["lg"], 0))
            inner = ttk.Frame(bar, style="Card.TFrame")
            inner.pack(side="right")
            first = None
            for text, value, style, is_default in specs:
                b = ttk.Button(inner, text=text, style=style,
                               command=lambda v=value: self._finish(v))
                b.pack(side="left", padx=(SPACE["sm"], 0))
                if is_default:
                    self._default = b
                if first is None:
                    first = b
            self.bind("<Return>", lambda e: self._default.invoke()
                      if self._default else None)
            return first

        def open(self):
            self.grab_set()
            center_over(self, self.master)
            if self._default is not None:
                self._default.focus_set()
            else:
                self.focus_set()
            self.wait_window(self)
            return self.result

    class MessageDialog(BaseDialog):
        """带语义色标条的模态消息框，替代原生 messagebox。"""
        SEV = {"info": COLORS["primary"], "success": COLORS["success"],
               "warning": COLORS["warning"], "error": COLORS["danger"],
               "question": COLORS["primary"]}

        def __init__(self, parent, title, message, severity="info",
                     buttons=None, default=None):
            super().__init__(parent, title)
            # buttons: [(text, value, style), ...]；default=None 表示默认高亮最后一个
            buttons = buttons or [("确定", "ok", "Primary.TButton")]
            accent = self.SEV.get(severity, COLORS["primary"])

            wrap = ttk.Frame(self, style="Card.TFrame")
            wrap.pack(fill="both", expand=True)
            tk.Frame(wrap, background=accent, width=4).pack(side="left", fill="y")

            body = ttk.Frame(wrap, style="Card.TFrame",
                             padding=(SPACE["lg"], SPACE["lg"], SPACE["xl"], SPACE["md"]))
            body.pack(side="left", fill="both", expand=True)
            ttk.Label(body, text=title, style="H2.TLabel").pack(anchor="w")
            ttk.Label(body, text=message, style="Sec.TLabel", justify="left",
                      wraplength=420).pack(anchor="w", pady=(SPACE["sm"], 0))
            n = len(buttons)
            self._button_bar(body, [
                (t, v, s, ((v == default) if default is not None else (i == n - 1)))
                for i, (t, v, s) in enumerate(buttons)
            ])

    class ConfirmDialog(MessageDialog):
        def __init__(self, parent, title, message, ok_text="确定",
                     cancel_text="取消", severity="question", ok_style="Primary.TButton"):
            super().__init__(parent, title, message, severity=severity,
                             buttons=[(cancel_text, None, "TButton"),
                                      (ok_text, True, ok_style)],
                             default=True)

    class PromptDialog(BaseDialog):
        """带输入框的模态对话框（用于「重命名」输入新名称）。"""
        def __init__(self, parent, title, message, initial="",
                     ok_text="确定", validate=None):
            super().__init__(parent, title)
            self.validate = validate
            wrap = ttk.Frame(self, style="Card.TFrame",
                             padding=(SPACE["xl"], SPACE["lg"]))
            wrap.pack(fill="both", expand=True)
            ttk.Label(wrap, text=title, style="H2.TLabel").pack(anchor="w")
            ttk.Label(wrap, text=message, style="Sec.TLabel", justify="left",
                      wraplength=420).pack(anchor="w", pady=(SPACE["sm"], SPACE["md"]))
            self.var = tk.StringVar(value=initial)
            ent = ttk.Entry(wrap, textvariable=self.var, width=48)
            ent.pack(fill="x")
            ent.focus_set()
            self._err = ttk.Label(wrap, text="", style="Hint.TLabel",
                                  foreground=COLORS["danger"])
            self._err.pack(anchor="w", pady=(SPACE["xs"], 0))
            self._button_bar(wrap, [(ok_text, "__ok__", "Primary.TButton", True),
                                    ("取消", None, "TButton", False)])
            ent.bind("<Return>", lambda e: self._on_ok())
            self._default.configure(command=self._on_ok)
            self._ent = ent

        def _on_ok(self):
            val = self.var.get().strip()
            if self.validate:
                err = self.validate(val)
                if err:
                    self._err.configure(text=err)
                    self._ent.focus_set()
                    return
            self._finish(val)

    class ConflictDialog(BaseDialog):
        """软件已存在时的三选一：覆盖 / 重命名另存 / 先删除再覆盖 / 取消。"""
        def __init__(self, parent, name, existing_rec, source, target):
            super().__init__(parent, "名称已存在")
            wrap = ttk.Frame(self, style="Card.TFrame",
                             padding=(SPACE["xl"], SPACE["lg"]))
            wrap.pack(fill="both", expand=True)
            ttk.Label(wrap, text="软件「%s」已存在" % name,
                      style="H2.TLabel").pack(anchor="w")
            ttk.Label(wrap, style="Sec.TLabel", justify="left", wraplength=440,
                      text="管理列表中已有同名软件记录。请选择如何处理本次保存：").pack(
                          anchor="w", pady=(SPACE["sm"], SPACE["md"]))

            info = ttk.Frame(wrap, style="Card.TFrame")
            info.pack(fill="x", pady=(0, SPACE["md"]))
            self._kv(info, "已有源路径", existing_rec["source"])
            self._kv(info, "已有目标", existing_rec["target"])
            self._kv(info, "本次源路径", source)
            self._kv(info, "本次目标", target)

            opts = [
                ("覆盖", "overwrite",
                 "直接用本次内容替换原有软件（更新同一条记录）。"),
                ("重命名另存", "save_as",
                 "输入新名称，把本次内容保存为一条独立的软件记录。"),
                ("先删除再覆盖", "delete_create",
                 "移除原有记录及其软链接后，再写入本次内容（生成新记录）。"),
            ]
            for label, value, desc in opts:
                self._option(wrap, label, desc, value,
                             danger=(value == "delete_create"))
            bar = ttk.Frame(wrap, style="Card.TFrame")
            bar.pack(fill="x", pady=(SPACE["lg"], 0))
            cancel = ttk.Button(bar, text="取消", style="TButton",
                                command=self._cancel)
            cancel.pack(side="right")
            self._default = None

        def _kv(self, parent, k, v):
            row = ttk.Frame(parent, style="Card.TFrame")
            row.pack(fill="x", pady=1)
            ttk.Label(row, text=k, style="Hint.TLabel", width=10).pack(side="left")
            ttk.Label(row, text=v, style="Path.TLabel",
                      foreground=COLORS["text_sec"]).pack(side="left")

        def _option(self, parent, label, desc, value, danger=False):
            style = "Danger.TButton" if danger else "Primary.TButton" if value == "overwrite" else "TButton"
            row = ttk.Frame(parent, style="Card.TFrame")
            row.pack(fill="x", pady=(0, SPACE["sm"]))
            b = ttk.Button(row, text=label, style=style,
                           command=lambda v=value: self._finish(v))
            b.pack(side="left")
            d = ttk.Label(row, text=desc, style="Hint.TLabel",
                          wraplength=300, justify="left")
            d.pack(side="left", padx=(SPACE["md"], 0), fill="y")

    class Toast:
        """非阻塞成功反馈，自动消失。"""
        def __init__(self, root, text, kind="ok", ms=2400):
            self.win = tk.Toplevel(root)
            self.win.wm_overrideredirect(True)
            self.win.attributes("-topmost", True)
            accent = COLORS["toast_ok"] if kind == "ok" else COLORS["danger"]
            fr = tk.Frame(self.win, background=COLORS["toast_bg"])
            fr.pack()
            tk.Frame(fr, background=accent, width=4).pack(side="left", fill="y")
            tk.Label(fr, text=text, background=COLORS["toast_bg"],
                     foreground=COLORS["toast_fg"], font=_THEME["f_base"],
                     padx=16, pady=11).pack(side="left")
            self.win.update_idletasks()
            w = self.win.winfo_reqwidth()
            rx, ry = root.winfo_rootx(), root.winfo_rooty()
            rw, rh = root.winfo_width(), root.winfo_height()
            x = rx + (rw - w) // 2
            y = ry + rh - 84
            self.win.geometry("+%d+%d" % (max(x, 0), max(y, 0)))
            self.win.after(ms, self._close)

        def _close(self):
            try:
                self.win.destroy()
            except Exception:
                pass

        def destroy(self):
            self._close()

        def update_idletasks(self):
            self.win.update_idletasks()

    def run_async(root, work, on_done, message="正在处理，请稍候…"):
        """后台线程执行耗时 I/O，主线程回调；>120ms 才显示加载遮罩，避免闪烁。"""
        state = {"done": False, "overlay": None}
        result = {}

        def make_overlay():
            ov = tk.Toplevel(root)
            ov.transient(root)
            ov.resizable(False, False)
            ov.configure(background=COLORS["surface"])
            ov.protocol("WM_DELETE_WINDOW", lambda: None)
            fr = ttk.Frame(ov, style="Card.TFrame", padding=(SPACE["xl"], SPACE["lg"]))
            fr.pack()
            pb = ttk.Progressbar(fr, mode="indeterminate", length=240,
                                 style="Horizontal.TProgressbar")
            pb.pack(pady=(0, SPACE["md"]))
            pb.start(12)
            ttk.Label(fr, text=message, style="Sec.TLabel").pack()
            center_over(ov, root)
            try:
                ov.grab_set()
            except tk.TclError:
                pass
            state["overlay"] = ov
            state["pb"] = pb

        def maybe_show():
            if not state["done"] and state["overlay"] is None:
                make_overlay()

        def finish():
            state["done"] = True
            ov = state["overlay"]
            if ov is not None:
                try:
                    state["pb"].stop()
                    ov.grab_release()
                except Exception:
                    pass
                ov.destroy()
            on_done(result.get("ok"), result.get("err"))

        def worker():
            try:
                result["ok"] = work()
            except LinkError as e:
                result["err"] = str(e)
            except Exception as e:
                result["err"] = "操作失败：%r" % e
            finally:
                root.after(0, finish)

        threading.Thread(target=worker, daemon=True).start()
        root.after(120, maybe_show)

    # ---------------- 列定义 ----------------
    # 前四列顺序固定（序号|软件名称|源文件夹路径|目标文件夹路径），
    # 其后为详细信息列（紧凑详细型布局），可通过工具栏开关显隐。
    COLUMNS = [("#", "序号", 44, True),
               ("name", "软件名称", 150, True),
               ("source", "源文件夹路径", 280, True),
               ("target", "目标文件夹路径", 280, True),
               ("status", "状态", 64, False),
               ("link_type", "类型", 72, False),
               ("kind", "源类型", 56, False),
               ("created_at", "创建时间", 128, False)]
    STATUS_TEXT = {"ok": "● 正常", "broken": "▲ 损坏", "missing": "○ 缺失"}

    class App(tk.Tk):
        def __init__(self):
            super().__init__()
            apply_window_icon(self)   # 标题栏/任务栏图标（EXE 资源图标不含此项）
            apply_theme(self)
            self.title(APP_TITLE)
            self.geometry("1180x640")
            self.minsize(880, 480)
            self.configure(background=COLORS["bg"])
            try:
                self.store = Store()
            except RuntimeError as e:
                MessageDialog(self, "无法启动", str(e), severity="error").open()
                raise SystemExit(1)
            self.detail_var = tk.BooleanVar(value=True)   # 详细信息列开关
            self._sort_state = {"col": None, "desc": False}
            self._build_ui()
            self._bind_shortcuts()
            self.refresh_table()

        # ---------- UI ----------
        def _build_ui(self):
            # 顶部标题条
            head = ttk.Frame(self, style="Toolbar.TFrame",
                             padding=(SPACE["lg"], SPACE["md"]))
            head.pack(fill="x")
            ttk.Label(head, text=APP_TITLE, style="H2.TLabel").pack(side="left")
            self.admin_var = tk.StringVar(value="")
            ttk.Label(head, textvariable=self.admin_var, style="Status.TLabel").pack(
                side="left", padx=(SPACE["md"], 0))
            if not is_admin():
                self.admin_var.set("· 非管理员：目录链接自动用 Junction")
                ToolTip(head, "当前以普通权限运行。\n目录链接会自动降级为 Junction（无需管理员）；\n"
                              "文件链接需要管理员权限或开启开发者模式。")

            # 工具栏
            tb = ttk.Frame(self, style="Toolbar.TFrame",
                           padding=(SPACE["lg"], SPACE["xs"]))
            tb.pack(fill="x")
            self._tb_button(tb, "＋ 新建", "Primary.TButton", self.on_add,
                            "创建软链接并登记记录 (Ctrl+N)")
            self._tb_button(tb, "编辑", "TButton", self.on_edit,
                            "编辑选中记录 (Ctrl+E / 双击)")
            self._tb_button(tb, "删除", "Danger.TButton", self.on_delete,
                            "删除选中记录 (Delete)")
            self._sep(tb)
            self._tb_button(tb, "刷新", "TButton", self.refresh_table,
                            "重新读取记录与链接状态 (F5)")
            self._tb_button(tb, "打开目标位置", "TButton", self.on_open_target,
                            "在资源管理器中打开选中记录的目标目录")
            self._tb_button(tb, "校验选中", "TButton", self.on_verify,
                            "检查选中记录的链接是否正常")
            self._sep(tb)
            det = ttk.Checkbutton(tb, text="详细列", variable=self.detail_var,
                                  command=self._toggle_detail,
                                  style="Toolbar.TCheckbutton")
            det.pack(side="left", padx=(0, SPACE["sm"]))
            ToolTip(det, "显示/隐藏 状态、类型、源类型、创建时间 列 (Ctrl+D)")

            # 搜索（右侧）
            right = ttk.Frame(tb, style="Toolbar.TFrame")
            right.pack(side="right")
            ttk.Label(right, text="搜索", style="Status.TLabel").pack(side="left")
            self.search_var = tk.StringVar()
            ent = ttk.Entry(right, textvariable=self.search_var, width=22)
            ent.pack(side="left", padx=(SPACE["xs"], SPACE["xs"]))
            ent.bind("<KeyRelease>", lambda e: self.refresh_table())
            self.search_entry = ent
            clear = ttk.Button(right, text="✕", style="Ghost.TButton", width=3,
                               command=self._clear_search)
            clear.pack(side="left")
            ToolTip(clear, "清除搜索")
            ToolTip(ent, "按软件名称 / 源路径 / 目标路径实时过滤 (Ctrl+F)")

            # 表格 + 底部详情面板
            mid = ttk.Frame(self, style="Card.TFrame",
                            padding=(SPACE["lg"], SPACE["xs"], SPACE["lg"], 0))
            mid.pack(fill="both", expand=True)
            card = ttk.Frame(mid, style="Card.TFrame")
            card.pack(fill="both", expand=True)
            self.tree = ttk.Treeview(card, columns=[c[0] for c in COLUMNS],
                                     show="headings", selectmode="browse")
            for key, title, width, _req in COLUMNS:
                self.tree.heading(key, text=title,
                                  command=lambda k=key: self._sort_by(k))
                anchor = "center" if key in ("#", "status", "link_type", "kind") else "w"
                self.tree.column(key, width=width, anchor=anchor,
                                 stretch=(key not in ("#", "status", "link_type", "kind")),
                                 minwidth=40 if key == "#" else 50)
            vsb = ttk.Scrollbar(card, orient="vertical", command=self.tree.yview,
                                style="Vertical.TScrollbar")
            hsb = ttk.Scrollbar(card, orient="horizontal", command=self.tree.xview)
            self.tree.configure(yscrollcommand=vsb.set, xscrollcommand=hsb.set)
            self.tree.grid(row=0, column=0, sticky="nsew")
            vsb.grid(row=0, column=1, sticky="ns")
            hsb.grid(row=1, column=0, sticky="ew")
            card.rowconfigure(0, weight=1)
            card.columnconfigure(0, weight=1)
            self.tree.bind("<Double-1>", lambda e: self.on_edit())
            self.tree.bind("<Button-3>", self._popup_menu)
            self.tree.bind("<<TreeviewSelect>>", self._on_select)
            self.tree.tag_configure("zebra", background=COLORS["surface_alt"])
            self.tree.tag_configure("broken", foreground=COLORS["broken"])
            self.tree.tag_configure("missing", foreground=COLORS["missing"])

            # 空状态覆盖层
            self.empty_lbl = ttk.Label(mid, style="Sec.TLabel", justify="center",
                                       text="暂无软链接记录\n\n点击左上角「＋ 新建」创建第一条软链接")
            self.empty_lbl.place(relx=0.5, rely=0.42, anchor="center")
            self.empty_lbl.lower()

            # 底部详情面板：选中记录的完整字段（紧凑详细型的核心信息区）
            dp = ttk.Frame(mid, style="Card.TFrame")
            dp.pack(fill="x", pady=(SPACE["xs"], SPACE["xs"]))
            ttk.Label(dp, text="详情", style="H2.TLabel").grid(
                row=0, column=0, sticky="w", padx=(0, SPACE["md"]))
            self.detail_var_text = tk.StringVar(
                value="选中一行查看完整信息（含状态、链接类型、创建时间等）")
            ttk.Label(dp, textvariable=self.detail_var_text, style="Sec.TLabel",
                      justify="left", wraplength=1020).grid(
                          row=0, column=1, sticky="w")
            dp.columnconfigure(1, weight=1)

            # 状态栏
            sb = ttk.Frame(self, style="Status.TFrame",
                           padding=(SPACE["lg"], 3))
            sb.pack(fill="x", side="bottom")
            self.status_var = tk.StringVar(value="就绪")
            ttk.Label(sb, textvariable=self.status_var,
                      style="Status.TLabel").pack(side="left")
            self.sort_var = tk.StringVar(value="")
            ttk.Label(sb, textvariable=self.sort_var,
                      style="Status.TLabel").pack(side="right")

        def _tb_button(self, parent, text, style, cmd, tip=None):
            b = ttk.Button(parent, text=text, style=style, command=cmd)
            b.pack(side="left", padx=(0, SPACE["xs"]))
            if tip:
                ToolTip(b, tip)
            return b

        def _sep(self, parent):
            ttk.Frame(parent, style="Toolbar.TFrame", width=1).pack(
                side="left", fill="y", padx=SPACE["xs"], pady=2)

        # ---------- 详细列显隐 / 排序 / 详情面板 ----------
        def _visible_cols(self):
            return [c[0] for c in COLUMNS
                    if c[3] or self.detail_var.get()]

        def _toggle_detail(self):
            vis = self._visible_cols()
            self.tree.configure(displaycolumns=vis)
            self.refresh_table()

        def _sort_by(self, col):
            if col == "#":
                key = lambda r: 0
            elif col == "status":
                order = {"ok": 0, "broken": 1, "missing": 2}
                key = lambda r: order.get(link_status(r), 3)
            elif col == "kind":
                key = lambda r: r.get("kind", "")
            elif col == "link_type":
                key = lambda r: r.get("link_type", "")
            elif col == "created_at":
                key = lambda r: r.get("created_at", "")
            else:
                key = lambda r: str(r.get(col, "")).lower()
            if self._sort_state["col"] == col:
                self._sort_state["desc"] = not self._sort_state["desc"]
            else:
                self._sort_state = {"col": col, "desc": False}
            self._sort_key = key
            title = dict((c[0], c[1]) for c in COLUMNS)[col]
            self.sort_var.set("排序：%s %s（点表头切换）"
                              % (title, "↓" if self._sort_state["desc"] else "↑"))
            self.refresh_table()

        def _on_select(self, _e=None):
            sel = self.tree.selection()
            if not sel:
                self.detail_var_text.set(
                    "选中一行查看完整信息（含状态、链接类型、创建时间等）")
                return
            rec = self.store.get(sel[0])
            if rec is None:
                return
            status = link_status(rec)
            stext = STATUS_TEXT[status]
            lt = rec.get("link_type", "—")
            lt_text = {"symlink": "符号链接 symlink", "junction": "目录联接 junction"}.get(lt, lt)
            kind_text = "文件夹" if rec.get("kind") == "dir" else "文件"
            self.detail_var_text.set(
                "%s　|　%s　|　类型：%s　|　源类型：%s　|　创建于：%s\n源：%s\n目标：%s"
                % (rec["name"], stext, lt_text, kind_text,
                   rec.get("created_at", "—"), rec["source"], rec["target"]))

        def _clear_search(self):
            self.search_var.set("")
            self.refresh_table()

        def _bind_shortcuts(self):
            self.bind("<Control-n>", lambda e: self.on_add())
            self.bind("<Control-N>", lambda e: self.on_add())
            self.bind("<Control-e>", lambda e: self.on_edit())
            self.bind("<Control-E>", lambda e: self.on_edit())
            self.bind("<Control-f>", lambda e: self.search_entry.focus_set())
            self.bind("<Control-F>", lambda e: self.search_entry.focus_set())
            self.bind("<F5>", lambda e: self.refresh_table())
            self.bind("<Delete>", lambda e: self.on_delete())
            self.bind("<Control-d>", lambda e: self._toggle_detail_hotkey())
            self.bind("<Control-D>", lambda e: self._toggle_detail_hotkey())

        def _toggle_detail_hotkey(self):
            self.detail_var.set(not self.detail_var.get())
            self._toggle_detail()

        def _popup_menu(self, event):
            iid = self.tree.identify_row(event.y)
            if not iid:
                return
            self.tree.selection_set(iid)
            rec = self.store.get(iid)
            if rec is None:
                return
            menu = tk.Menu(self, tearoff=0)
            menu.configure(foreground=COLORS["text"],
                           activebackground=COLORS["primary"],
                           activeforeground=COLORS["on_primary"],
                           font=_THEME["f_base"])
            menu.add_command(label="编辑…", command=self.on_edit)
            menu.add_command(label="打开目标位置", command=self.on_open_target)
            menu.add_command(label="校验链接", command=self.on_verify)
            menu.add_command(label="复制源路径",
                             command=lambda: self._copy(rec["source"]))
            menu.add_command(label="复制目标路径",
                             command=lambda: self._copy(rec["target"]))
            menu.add_separator()
            menu.add_command(label="删除…", command=self.on_delete)
            menu.tk_popup(event.x_root, event.y_root)

        def _copy(self, text):
            self.clipboard_clear()
            self.clipboard_append(text)
            self.toast("已复制到剪贴板")

        # ---------- 表格 ----------
        def _filtered(self):
            kw = self.search_var.get().strip().lower()
            recs = self.store.all()
            if kw:
                recs = [r for r in recs
                        if kw in " ".join([r["name"], r["source"], r["target"]]).lower()]
            if self._sort_state["col"]:
                key = getattr(self, "_sort_key", None)
                if key:
                    recs = sorted(recs, key=key, reverse=self._sort_state["desc"])
            return recs

        def refresh_table(self, keep_id=None):
            self.tree.configure(displaycolumns=self._visible_cols())
            self.tree.delete(*self.tree.get_children())
            recs = self._filtered()
            for i, r in enumerate(recs, 1):
                status = link_status(r)
                tags = ["zebra"] if i % 2 == 0 else []
                if status != "ok":
                    tags.append(status)
                lt = {"symlink": "符号链接", "junction": "联接"}.get(
                    r.get("link_type"), r.get("link_type", "—"))
                kind_text = "文件夹" if r.get("kind") == "dir" else "文件"
                self.tree.insert("", "end", iid=r["id"], tags=tuple(tags),
                                 values=(i, r["name"], r["source"], r["target"],
                                         STATUS_TEXT[status], lt, kind_text,
                                         r.get("created_at", "—")))
            if keep_id and self.tree.exists(keep_id):
                self.tree.selection_set(keep_id)
                self.tree.see(keep_id)
                self._on_select()
            # 空状态
            if not recs:
                self.empty_lbl.configure(
                    text="没有匹配的记录\n\n调整搜索关键词，或点击「＋ 新建」创建软链接"
                    if self.search_var.get().strip()
                    else "暂无软链接记录\n\n点击左上角「＋ 新建」创建第一条软链接")
                self.empty_lbl.lift()
            else:
                self.empty_lbl.lower()
            total = len(self.store.all())
            shown = len(recs)
            msg = "共 %d 条记录" % total
            if shown != total:
                msg += "（筛选显示 %d 条）" % shown
            self.status_var.set(msg)

        def selected_record(self, quiet=False):
            sel = self.tree.selection()
            if not sel:
                if not quiet:
                    MessageDialog(self, "未选择", "请先在列表中选择一条记录。",
                                  severity="info").open()
                return None
            rec = self.store.get(sel[0])
            if rec is None:
                MessageDialog(self, "记录失效", "该记录已失效，请刷新列表。",
                              severity="warning").open()
                self.refresh_table()
            return rec

        def set_status(self, text):
            self.status_var.set(text)

        def toast(self, text, kind="ok"):
            Toast(self, text, kind=kind)

        # ---------- 新建 ----------
        def on_add(self):
            dlg = EditDialog(self, "新建软链接", None,
                             validate=self._validate_create)
            data = dlg.open()
            if data is None:
                self.set_status("已取消新建。")
                return
            name, source, target = data
            existing = self.store.find_by_name(name)
            if existing is None:
                self._create_when_absent(name, source, target)
            else:
                self._create_when_present(name, source, target, existing)

        def _validate_create(self, name, source, target):
            """新建框内的即时校验：拦截输入级错误与目标被真实文件占用。"""
            err = self._validate_input(name, source, target)
            if err:
                return err
            tstate = classify_target(target)
            if tstate in ("dir", "file"):
                what = "文件夹" if tstate == "dir" else "文件"
                return ("目标路径已被真实%s占用：\n%s\n请更换目标路径。"
                        % (what, target))
            return None

        def _create_when_absent(self, name, source, target):
            """软件不存在：确认对话框 → 创建。"""
            tstate = classify_target(target)
            replace = False
            if tstate == "link":
                ok = ConfirmDialog(
                    self, "目标已存在链接",
                    "目标位置已存在一个软链接（不在管理列表中）：\n%s\n\n"
                    "继续将覆盖该链接（不影响其指向的真实数据）。是否继续？" % target,
                    ok_text="覆盖并创建", severity="warning").open()
                if not ok:
                    self.set_status("已取消，未做任何修改。")
                    return
                replace = True
            kind_text = "文件夹" if os.path.isdir(source) else "文件"
            ok = ConfirmDialog(
                self, "确认创建",
                "软件「%s」尚不存在，是否创建软链接？\n\n"
                "源（真实数据·%s）：%s\n目标（链接位置）：%s"
                % (name, kind_text, source, target),
                ok_text="创建").open()
            if not ok:
                self.set_status("已取消，未做任何修改。")
                return
            self._run(lambda: op_create_new(self.store, name, source, target,
                                            replace_target=replace), keep_id=None)

        def _create_when_present(self, name, source, target, existing):
            """软件已存在：三选一（覆盖 / 重命名另存 / 先删除再覆盖）。"""
            choice = ConflictDialog(self, name, existing, source, target).open()
            if choice is None:
                self.set_status("已取消，未做任何修改。")
                return
            if choice == "overwrite":
                self._run(lambda: op_overwrite(self.store, existing, name,
                                               source, target),
                          keep_id=existing["id"])
            elif choice == "save_as":
                self._prompt_save_as(name, source, target)
            elif choice == "delete_create":
                ok = ConfirmDialog(
                    self, "二次确认 · 先删除再覆盖",
                    "将移除原有软件记录「%s」及其软链接，然后写入本次内容。\n\n"
                    "原目标链接：%s\n\n此操作会删除原有链接（不影响源真实数据），"
                    "且不可撤销。是否继续？" % (name, existing["target"]),
                    ok_text="删除并重建", ok_style="Danger.TButton",
                    severity="warning").open()
                if not ok:
                    self.set_status("已取消，未做任何修改。")
                    return
                self._run(lambda: op_delete_then_create(
                    self.store, existing, name, source, target), keep_id=None)

        def _prompt_save_as(self, name, source, target):
            def validate(new_name):
                if not new_name:
                    return "名称不能为空。"
                if new_name.strip().lower() == name.strip().lower():
                    return "新名称不能与原名称相同。"
                if self.store.find_by_name(new_name):
                    return "名称「%s」已被占用，请换一个。" % new_name
                if self.store.find_by_target(target):
                    return "目标路径已被其他记录占用。"
                return None
            new_name = PromptDialog(
                self, "重命名另存",
                "为本次内容输入一个新的软件名称，将保存为独立记录：",
                initial=name + " - 副本", validate=validate).open()
            if not new_name:
                self.set_status("已取消，未做任何修改。")
                return
            self._run(lambda: op_save_as(self.store, new_name, source, target),
                      keep_id=None)

        def _run(self, work, keep_id=None):
            """在后台线程执行 op_，完成后刷新并反馈（对话框此时已关闭）。"""
            def on_done(res, err):
                if err:
                    MessageDialog(self, "操作失败", err, severity="error").open()
                    return
                self.refresh_table(keep_id=keep_id or res.get("rec", {}).get("id"))
                lt = res.get("link_type")
                note = "（已降级为 Junction）" if lt == "junction" else ""
                self.set_status(res.get("msg", "完成") + " " + note)
                self.toast(res.get("msg", "完成"))
            run_async(self, work, on_done)

        def _validate_input(self, name, source, target):
            if not name:
                return "软件名称不能为空。"
            if not source:
                return "源路径不能为空。"
            if not target:
                return "目标路径不能为空。"
            if not os.path.exists(source):
                return "源路径不存在：\n%s" % source
            if not os.path.isdir(os.path.dirname(target)):
                return "目标路径的上级目录不存在：\n%s" % os.path.dirname(target)
            return None


        # ---------- 编辑 ----------
        def on_edit(self):
            rec = self.selected_record()
            if rec is None:
                return
            dlg = EditDialog(self, "编辑记录", rec,
                             validate=lambda n, s, t: self._validate_edit(rec, n, s, t))
            data = dlg.open()
            if data is None:
                self.set_status("已取消编辑。")
                return
            name, source, target = data
            self._apply_edit(rec, name, source, target)

        def _validate_edit(self, rec, name, source, target):
            err = self._validate_input(name, source, target)
            if err:
                return err
            other = self.store.find_by_name(name, exclude_id=rec["id"])
            if other is not None:
                return "名称「%s」已被另一条记录占用，请换一个名称。" % name
            src_changed = os.path.normcase(os.path.normpath(source)) != \
                os.path.normcase(os.path.normpath(rec["source"]))
            tgt_changed = os.path.normcase(os.path.normpath(target)) != \
                os.path.normcase(os.path.normpath(rec["target"]))
            if tgt_changed and classify_target(target) in ("dir", "file"):
                return "新目标路径已被真实文件/文件夹占用：\n%s" % target
            if src_changed and classify_target(target) == "link":
                # 目标仍是本记录自己的链接则允许重建，其它链接需提示
                if os.path.normcase(os.path.normpath(target)) != \
                   os.path.normcase(os.path.normpath(rec["target"])):
                    return "新目标路径已存在其他软链接：\n%s" % target
            return None

        def _apply_edit(self, rec, name, source, target):
            src_changed = os.path.normcase(os.path.normpath(source)) != \
                os.path.normcase(os.path.normpath(rec["source"]))
            tgt_changed = os.path.normcase(os.path.normpath(target)) != \
                os.path.normcase(os.path.normpath(rec["target"]))

            if not src_changed and not tgt_changed:
                def work():
                    self.store.update(rec["id"], name=name)
                    return {"action": "renamed", "rec": rec,
                            "msg": "已更新软件名称：%s" % name}
                self._run(work, keep_id=rec["id"])
                return

            ok = ConfirmDialog(
                self, "确认修改路径映射",
                "源/目标路径已修改，需要删除旧链接并在新位置重建。\n\n"
                "旧链接：%s\n新链接：%s → %s\n\n是否继续？"
                % (rec["target"], target, source), ok_text="重建链接").open()
            if not ok:
                self.set_status("已取消，未做任何修改。")
                return

            def work():
                old_target, old_source = rec["target"], rec["source"]
                had_old = os.path.lexists(old_target) and is_any_link(old_target)
                if had_old:
                    remove_link(old_target)
                try:
                    kind = _kind_of(source)
                    link_type = create_link(source, target)
                except LinkError:
                    if had_old and os.path.exists(old_source):
                        try:
                            create_link(old_source, old_target)
                            raise LinkError("重建新链接失败，已回滚恢复旧链接。")
                        except LinkError as e2:
                            raise LinkError("重建失败且回滚也失败，请手动检查：%s\n旧链接位置：%s"
                                            % (e2, old_target))
                    raise
                self.store.update(rec["id"], name=name, source=source,
                                  target=target, kind=kind, link_type=link_type)
                return {"action": "edited", "rec": rec,
                        "msg": "已更新记录并重建链接：%s" % name}
            self._run(work, keep_id=rec["id"])

        # ---------- 删除 ----------
        def on_delete(self):
            rec = self.selected_record()
            if rec is None:
                return
            choice = DeleteDialog(self, rec).open()
            if choice is None:
                self.set_status("已取消，未做任何修改。")
                return
            if choice == "both":
                ok = ConfirmDialog(
                    self, "二次确认 · 删除软链接",
                    "将删除目标位置的软链接本身，并移除管理记录。\n\n"
                    "链接：%s\n\n（不会删除源路径中的真实数据）此操作不可撤销，是否继续？"
                    % rec["target"], ok_text="确认删除", ok_style="Danger.TButton",
                    severity="warning").open()
                if not ok:
                    self.set_status("已取消，未做任何修改。")
                    return
                self._run(lambda: self._do_delete_both(rec), keep_id=None)
            else:
                self._run(lambda: self._do_delete_record(rec), keep_id=None)


        def _do_delete_record(self, rec):
            self.store.remove(rec["id"])
            return {"msg": "已移除记录：%s（磁盘链接保留）" % rec["name"]}

        def _do_delete_both(self, rec):
            t = rec["target"]
            if not os.path.lexists(t):
                self.store.remove(rec["id"])
                return {"msg": "链接已不存在，仅移除记录：%s" % rec["name"]}
            if not is_any_link(t):
                self.store.remove(rec["id"])
                return {"msg": "目标非链接，为安全仅移除记录：%s" % rec["name"]}
            remove_link(t)
            self.store.remove(rec["id"])
            return {"msg": "已删除软链接并移除记录：%s" % rec["name"]}

        # ---------- 其他 ----------
        def on_open_target(self):
            rec = self.selected_record()
            if rec is None:
                return
            folder = os.path.dirname(rec["target"])
            if os.path.isdir(folder):
                os.startfile(folder)
            else:
                MessageDialog(self, "无法打开", "目标所在目录不存在：\n%s" % folder,
                              severity="warning").open()

        def on_verify(self):
            rec = self.selected_record()
            if rec is None:
                return
            status = link_status(rec)
            text = {
                "ok": "链接正常：目标链接存在且指向有效的源。",
                "broken": "链接异常：目标位置存在，但源已失效或被普通文件占用。",
                "missing": "链接缺失：目标位置的链接已不存在（可能被手动删除）。",
            }[status]
            sev = {"ok": "success", "broken": "warning", "missing": "warning"}[status]
            detail = "源路径：%s\n目标路径：%s\n链接类型：%s" % (
                rec["source"], rec["target"], rec.get("link_type", "—"))
            MessageDialog(self, "校验结果 · %s" % rec["name"],
                          text + "\n\n" + detail, severity=sev).open()
            self.refresh_table(keep_id=rec["id"])

    # ---------------- 新建/编辑对话框 ----------------
    class EditDialog(BaseDialog):
        """收集软件名称/源/目标并做基础校验；保存后返回 (name, source, target)。

        确认/冲突等后续对话框由主窗口在本框关闭后弹出，避免嵌套模态。
        """
        def __init__(self, parent, title, record, validate=None):
            super().__init__(parent, title)
            self.record = record
            self.validate = validate

            wrap = ttk.Frame(self, style="Card.TFrame",
                             padding=(SPACE["xl"], SPACE["lg"]))
            wrap.pack(fill="both", expand=True)
            ttk.Label(wrap, text=title, style="Title.TLabel").grid(
                row=0, column=0, columnspan=3, sticky="w")
            ttk.Label(wrap, style="Hint.TLabel", justify="left", text=(
                "源路径 = 真实数据所在的文件/文件夹（必须已存在）\n"
                "目标路径 = 软链接被创建的位置（必须尚不存在）")).grid(
                    row=1, column=0, columnspan=3, sticky="w",
                    pady=(SPACE["xs"], SPACE["md"]))

            self.name_var = tk.StringVar(value=record["name"] if record else "")
            self.src_var = tk.StringVar(value=record["source"] if record else "")
            self.tgt_var = tk.StringVar(value=record["target"] if record else "")

            self._row(wrap, 2, "软件名称", self.name_var,
                      tip="用于标识该软件的名称，保存时按此判断是否已存在")
            self._row(wrap, 3, "源路径", self.src_var, browse=[
                ("选文件夹", self._browse_source_dir, "选择作为源的文件夹"),
                ("选文件", self._browse_source_file, "选择作为源的单个文件"),
            ], tip="真实数据所在位置的文件或文件夹")
            self._row(wrap, 4, "目标路径", self.tgt_var, browse=[
                ("浏览…", self._browse_target, "选择软链接的创建位置"),
            ], tip="软链接将被创建的位置（当前不应存在）")

            self._err = ttk.Label(wrap, text="", style="Hint.TLabel",
                                  foreground=COLORS["danger"], justify="left",
                                  wraplength=460)
            self._err.grid(row=5, column=0, columnspan=3, sticky="w",
                           pady=(SPACE["sm"], 0))

            self._auto_target_state = not record
            self._last_auto = ""
            self.name_var.trace_add("write", self._auto_target)

            bar = ttk.Frame(wrap, style="Card.TFrame")
            bar.grid(row=6, column=0, columnspan=3, sticky="e",
                     pady=(SPACE["lg"], 0))
            self._default = None
            ttk.Button(bar, text="取消", style="TButton",
                       command=self._cancel).pack(side="right",
                                                  padx=(SPACE["sm"], 0))
            save = ttk.Button(bar, text="保存", style="Primary.TButton",
                              command=self._save)
            save.pack(side="right")
            self._default = save
            self.bind("<Return>", lambda e: self._save())
            save.focus_set()

        def _row(self, parent, r, label, var, browse=None, tip=None):
            ttk.Label(parent, text=label, style="Card.TLabel").grid(
                row=r, column=0, sticky="nw", pady=SPACE["sm"], padx=(0, SPACE["md"]))
            ent = ttk.Entry(parent, textvariable=var, width=54)
            ent.grid(row=r, column=1, sticky="w", pady=SPACE["sm"])
            if tip:
                ToolTip(ent, tip)
            if browse:
                cell = ttk.Frame(parent, style="Card.TFrame")
                cell.grid(row=r, column=2, sticky="w", padx=(SPACE["sm"], 0),
                          pady=SPACE["sm"])
                for btext, bcmd, btip in browse:
                    b = ttk.Button(cell, text=btext, style="TButton", command=bcmd)
                    b.pack(side="left", padx=(0, SPACE["xs"]))
                    if btip:
                        ToolTip(b, btip)


        def _auto_target(self, *_):
            if not self._auto_target_state:
                return
            cur = self.tgt_var.get().strip()
            name = self.name_var.get().strip()
            if name and (not cur or cur == self._last_auto):
                parent = os.path.dirname(cur) if cur else ""
                if parent:
                    newt = os.path.join(parent, name)
                    self.tgt_var.set(newt)
                    self._last_auto = newt

        def _browse_source_dir(self):
            init = self.src_var.get() or os.path.expanduser("~")
            p = filedialog.askdirectory(
                parent=self, title="选择源文件夹",
                initialdir=init if os.path.isdir(init) else None)
            if p:
                self.src_var.set(os.path.normpath(p))

        def _browse_source_file(self):
            init = self.src_var.get() or os.path.expanduser("~")
            initdir = os.path.dirname(init) if os.path.isfile(init) else None
            p = filedialog.askopenfilename(parent=self, title="选择源文件",
                                           initialdir=initdir)
            if p:
                self.src_var.set(os.path.normpath(p))

        def _browse_target(self):
            init_dir = self.tgt_var.get()
            init_dir = os.path.dirname(init_dir) if init_dir else os.path.expanduser("~")
            p = filedialog.asksaveasfilename(
                parent=self, title="选择软链接的创建位置（输入链接名称）",
                initialdir=init_dir if os.path.isdir(init_dir) else None,
                confirmoverwrite=False)
            if p:
                self.tgt_var.set(os.path.normpath(p))
                self._auto_target_state = False

        def _save(self):
            name = self.name_var.get().strip()
            source = self.src_var.get().strip()
            target = self.tgt_var.get().strip()
            self._err.configure(text="")
            if self.validate is not None:
                try:
                    err = self.validate(name, source, target)
                except Exception as e:
                    err = "校验失败：%r" % e
                if err:
                    self._err.configure(text=err)
                    return
            self._finish((name, source, target))


    # ---------------- 删除对话框 ----------------
    class DeleteDialog(BaseDialog):
        def __init__(self, parent, rec):
            super().__init__(parent, "删除记录")
            status = link_status(rec)
            status_text = {"ok": "链接正常", "broken": "链接已损坏（源失效）",
                           "missing": "链接已不存在"}[status]
            sev_color = {"ok": COLORS["success"], "broken": COLORS["warning"],
                         "missing": COLORS["warning"]}[status]

            wrap = ttk.Frame(self, style="Card.TFrame",
                             padding=(SPACE["xl"], SPACE["lg"]))
            wrap.pack(fill="both", expand=True)
            ttk.Label(wrap, text="删除「%s」" % rec["name"],
                      style="H2.TLabel").pack(anchor="w")
            dot = ttk.Label(wrap, text="● " + status_text, style="Hint.TLabel",
                            foreground=sev_color)
            dot.pack(anchor="w", pady=(2, SPACE["md"]))
            self._kv(wrap, "源路径", rec["source"])
            self._kv(wrap, "目标链接", rec["target"])

            ttk.Label(wrap, style="Sec.TLabel", justify="left", wraplength=440, text=(
                "· 仅移除记录：只从列表删除，磁盘上的软链接保持不变。\n"
                "· 同时删除软链接：移除记录，并删除目标位置的链接本身\n"
                "  （不会删除源路径中的真实数据）。")).pack(
                    anchor="w", pady=(SPACE["md"], SPACE["md"]))

            bar = ttk.Frame(wrap, style="Card.TFrame")
            bar.pack(fill="x")
            ttk.Button(bar, text="取消", style="TButton",
                       command=self._cancel).pack(side="right",
                                                  padx=(SPACE["sm"], 0))
            b2 = ttk.Button(bar, text="同时删除软链接", style="Danger.TButton",
                            command=lambda: self._finish("both"))
            b2.pack(side="right", padx=(SPACE["sm"], 0))
            if status == "missing":
                b2.state(["disabled"])
                ToolTip(b2, "链接已不存在，无需删除")
            else:
                ToolTip(b2, "删除链接本身并移除记录（不动源数据）")
            b1 = ttk.Button(bar, text="仅移除记录", style="TButton",
                           command=lambda: self._finish("record"))
            b1.pack(side="right")
            ToolTip(b1, "只从管理列表移除，保留磁盘上的链接")
            self._default = b1

        def _kv(self, parent, k, v):
            row = ttk.Frame(parent, style="Card.TFrame")
            row.pack(fill="x", pady=1)
            ttk.Label(row, text=k, style="Hint.TLabel", width=10).pack(side="left")
            ttk.Label(row, text=v, style="Path.TLabel",
                      foreground=COLORS["text_sec"]).pack(side="left")

    app = App()
    if ui_selftest:
        # 逐个构建所有对话框/组件，验证样式与布局不抛异常（构建即销毁，不进入模态）
        report = {}
        here = os.path.dirname(os.path.abspath(__file__))
        rec = {"id": "_uitest_", "name": "测试软件", "source": here,
               "target": os.path.join(here, "_uitest_link_"), "kind": "dir",
               "link_type": "junction",
               "created_at": time.strftime("%Y-%m-%d %H:%M:%S")}

        def _run_tests():
            app.withdraw()
            specs = [
                ("MessageDialog", lambda: MessageDialog(app, "标题", "测试消息")),
                ("ConfirmDialog", lambda: ConfirmDialog(app, "标题", "确认吗？")),
                ("PromptDialog", lambda: PromptDialog(app, "重命名", "输入新名称：",
                                                      initial="副本")),
                ("ConflictDialog", lambda: ConflictDialog(app, "测试软件", rec,
                                                          rec["source"], rec["target"])),
                ("DeleteDialog", lambda: DeleteDialog(app, rec)),
                ("EditDialog_new", lambda: EditDialog(app, "新建软链接", None,
                                                      validate=lambda *a: None)),
                ("EditDialog_edit", lambda: EditDialog(app, "编辑记录", rec,
                                                       validate=lambda *a: None)),
                ("Toast", lambda: Toast(app, "操作成功")),
            ]
            for name, factory in specs:
                obj = None
                try:
                    obj = factory()
                    obj.update_idletasks()
                    report[name] = (True, "")
                except Exception as e:
                    report[name] = (False, repr(e))
                finally:
                    try:
                        if obj is not None:
                            obj.destroy()
                    except Exception:
                        pass

            # ---- 行为级测试：排序 / 详细列显隐 / 详情面板 ----
            # 依赖 smoketest 种入的 3 条记录 smoke0/1/2（name 冒烟样例0/1/2，
            # created_at 递增）。空库下这些测试自动跳过。
            def _behavior(name, fn):
                try:
                    fn()
                    report[name] = (True, "")
                except AssertionError as e:
                    report[name] = (False, "assert: %s" % e)
                except Exception as e:
                    report[name] = (False, repr(e))

            # ---- 窗口标题栏图标（与 EXE 的 PE 资源图标是两回事）----
            # 证据写进 msg，成功时 smoketest 也会输出，便于确认实际生效
            try:
                wm_ok, evidence = window_icon_state(app)
                report["window_icon"] = (bool(wm_ok), evidence)
            except Exception as e:
                report["window_icon"] = (False, repr(e))

            ids = list(app.tree.get_children())
            if ids == ["smoke0", "smoke1", "smoke2"]:
                def t_sort():
                    # created_at 降序 -> 2,1,0；再切 name 升序 -> 应回到 0,1,2
                    app._sort_by("created_at")
                    app._sort_by("created_at")   # 第二次点击 -> 降序
                    mid = list(app.tree.get_children())
                    assert mid == ["smoke2", "smoke1", "smoke0"], \
                        "created_at 降序应得 2,1,0，实际 %s" % mid
                    app._sort_by("name")         # 换列 -> 升序
                    fin = list(app.tree.get_children())
                    assert fin == ["smoke0", "smoke1", "smoke2"], \
                        "name 升序应得 0,1,2，实际 %s" % fin
                _behavior("behavior_sort", t_sort)

                def t_detail_toggle():
                    app.detail_var.set(True)
                    app._toggle_detail()
                    full = list(app.tree.cget("displaycolumns"))
                    app.detail_var.set(False)
                    app._toggle_detail()
                    mini = list(app.tree.cget("displaycolumns"))
                    app.detail_var.set(True)      # 复原
                    app._toggle_detail()
                    assert len(full) == 8, "详细列开启应显示 8 列，实际 %d" % len(full)
                    assert len(mini) == 4, "详细列关闭应显示 4 列，实际 %d" % len(mini)
                    assert mini == ["#", "name", "source", "target"], \
                        "关闭详细列应只保留固定四列，实际 %s" % mini
                _behavior("behavior_detail_toggle", t_detail_toggle)

                def t_detail_panel():
                    app.tree.selection_set("smoke1")
                    app._on_select()
                    txt = app.detail_var_text.get()
                    assert "冒烟样例1" in txt, "详情面板应显示选中记录名称，实际：%s" % txt
                    assert "源：" in txt and "目标：" in txt, "详情面板应含源/目标路径"
                _behavior("behavior_detail_panel", t_detail_panel)
            else:
                report["behavior_sort"] = (True, "(skipped: no seed data)")
                report["behavior_detail_toggle"] = (True, "(skipped)")
                report["behavior_detail_panel"] = (True, "(skipped)")

            app.destroy()

        app.after(150, _run_tests)
        app.mainloop()
        return report

    if auto_close_ms:
        app.after(auto_close_ms, app.destroy)
    app.mainloop()
    return app


# ==========================================================================

def smoketest(result_path=None):
    """确定性验证 GUI 运行时栈与完整界面构建（用于校验打包后的 EXE）。

    覆盖两级：
      1. 主窗口构建（apply_theme / 工具栏 / 表格 / 状态栏）；
      2. 全部对话框与组件构建（Message/Confirm/Prompt/Conflict/Delete/Edit/Toast）。
    任一构建抛异常即视为失败（退出码非 0，结果文件标记 FAIL）。
    windowed（无控制台）EXE 下无法看 stdout，故以文件 + 退出码为准。
    使用临时记录文件，不污染用户真实数据。
    """
    import ctypes
    import tempfile
    import shutil

    global RECORDS_FILE
    old_records = RECORDS_FILE
    tmpdir = tempfile.mkdtemp(prefix="slnmgr_smoke_")
    RECORDS_FILE = os.path.join(tmpdir, "records.json")
    # 种入样例记录：强制 refresh_table 走行插入路径
    # （空库不会执行插入循环，无法暴露该类 AttributeError）
    seed = []
    for i in range(3):
        seed.append({
            "id": "smoke%d" % i, "name": "冒烟样例%d" % i,
            "source": tmpdir, "target": os.path.join(tmpdir, "ghost_link%d" % i),
            "kind": "dir", "link_type": "junction" if i % 2 else "symlink",
            "created_at": "2026-01-0%d 10:00:00" % (i + 1),
        })
    with open(RECORDS_FILE, "w", encoding="utf-8") as f:
        json.dump(seed, f, ensure_ascii=False)
    lines = []
    ok = True
    try:
        # 1) 主窗口构建，1.2s 后自动销毁
        run_gui(auto_close_ms=1200)
        lines.append("main_window OK")

        # 2) 全部对话框/组件构建自检
        report = run_gui(ui_selftest=True)
        for name, (passed, err) in sorted(report.items()):
            lines.append("%s %s%s" % (name, "OK" if passed else "FAIL",
                                      (" -> " + err if err else "")))
            if not passed:
                ok = False

        # Tcl 版本探测，确认 Tk 运行时就绪
        import tkinter as tk
        probe = tk.Tk()
        probe.withdraw()
        tv = probe.tk.call("info", "patchlevel")
        probe.destroy()
        lines.append("tcl=%s ctypes=%s" % (tv, bool(ctypes.windll.shell32)))
    finally:
        RECORDS_FILE = old_records
        shutil.rmtree(tmpdir, ignore_errors=True)

    header = "OK" if ok else "FAIL"
    msg = header + "\n" + "\n".join(lines)
    if result_path:
        with open(result_path, "w", encoding="utf-8") as f:
            f.write(msg)
    else:
        print(msg)
    return 0 if ok else 1


def main():
    if "--selftest" in sys.argv:
        sys.exit(selftest())
    if "--smoketest" in sys.argv:
        i = sys.argv.index("--smoketest")
        rp = sys.argv[i + 1] if i + 1 < len(sys.argv) else None
        sys.exit(smoketest(rp))
    run_gui()


if __name__ == "__main__":
    main()
