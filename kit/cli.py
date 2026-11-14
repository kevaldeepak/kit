"""kit: install, remove and update modules from the kit repo."""
import argparse
import datetime
import importlib.util
import os
import socket
import subprocess
import sys
import sysconfig

from . import VERSION, notify, source, system, ui

HELP = """\
kit: modules for this device, from github.com/kevaldeepak/kit

  kit available              every module in the repo (refreshes the list)
  kit list                   what's installed here
  kit info <module>          details, dependencies, config location
  kit install [module ...]   install (no name: pick from a list)
  kit configure <module>     change a module's settings
  kit remove <module>        uninstall (keeps config; --purge deletes it)
  kit update [module]        update kit and installed modules
  kit doctor [module]        check everything is working
  kit self-uninstall         remove kit itself (--purge: config too)

Options: -y/--yes accept defaults, --set key=value answer a prompt up front,
--force skip dependency checks, --offline use the cached module list.
"""


# ---------- state ----------

def load_state():
    st = system.read_json(system.STATE_FILE)
    st.setdefault("modules", {})
    return st


def save_state(st):
    system.write_json(system.STATE_FILE, st)


def installed():
    return load_state()["modules"]


def get_source():
    return os.environ.get("KIT_SOURCE") or load_state().get("source") or source.default_source()


def refresh(offline=False):
    if offline:
        return source.manifests()
    src = get_source()
    try:
        source.refresh(src)
    except Exception as e:  # noqa: BLE001
        if source.manifests():
            ui.warn(f"Couldn't refresh from {source.describe(src)} ({e}); using the cached list.")
        else:
            raise ui.Abort(f"Couldn't download the module list from {source.describe(src)}: {e}")
    return source.manifests()


# ---------- module plumbing ----------

def load_hooks(name, mod_dir):
    path = os.path.join(mod_dir, "module.py")
    if not os.path.exists(path):
        return None
    if mod_dir not in sys.path:
        sys.path.insert(0, mod_dir)
    spec = importlib.util.spec_from_file_location(f"kitmod_{name}", path)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def hook(hooks, name, *args):
    fn = getattr(hooks, name, None) if hooks else None
    return fn(*args) if fn else None


class Ctx:
    """What a module's module.py hooks get."""

    def __init__(self, manifest, opts=None, yes=False):
        self.name = manifest["name"]
        self.manifest = manifest
        self.module_dir = os.path.join(system.MODULES_DIR, self.name)
        self.config_dir = os.path.join(system.CONFIG_DIR, self.name)
        self.opts = opts or {}
        self.yes = yes
        self.system = system
        self.notify = notify
        self.hostname = socket.gethostname()

    def ask(self, label, key=None, default=None, **kw):
        return ui.ask(label, default=default, preset=self.opts.get(key) if key else None,
                      key=key, assume=self.yes, **kw)

    def confirm(self, label, key=None, default=False):
        return ui.confirm(label, default=default, preset=self.opts.get(key) if key else None, assume=self.yes)

    def say(self, msg=""):
        ui.say(msg)

    def warn(self, msg):
        ui.warn(msg)

    def path(self, *parts):
        os.makedirs(self.config_dir, exist_ok=True)
        return os.path.join(self.config_dir, *parts)

    def load(self, fname="config.json"):
        return system.read_json(self.path(fname))

    def save(self, data, fname="config.json", secret=False):
        system.write_json(self.path(fname), data, secret=secret)


def module_commands(m):
    return (m.get("commands") or {}).items()


def apply_runtime(m):
    """Shims + schedule for an installed module. Returns schedule message."""
    mod_dir = os.path.join(system.MODULES_DIR, m["name"])
    for cmd, script in module_commands(m):
        system.write_shim(cmd, [os.path.join(mod_dir, script)])
    sch = m.get("schedule")
    if not sch:
        return None
    script = (m.get("commands") or {}).get(sch.get("command")) or sch.get("script")
    argv = system.schedule_argv(os.path.join(mod_dir, script), sch.get("args") or [])
    ok, msg = system.schedule_add(m["name"], argv, int(sch.get("every_min", 5)))
    if not ok:
        ui.warn(f"Schedule: {msg}")
    return msg


def remove_runtime(m):
    for cmd, _ in module_commands(m):
        system.remove_shim(cmd)
    if m.get("schedule"):
        system.schedule_remove(m["name"])


def installed_manifest(name):
    mf = os.path.join(system.MODULES_DIR, name, "module.json")
    if not os.path.exists(mf):
        return None
    m = system.read_json(mf)
    m.setdefault("name", name)
    m.setdefault("requires", [])
    return m


def dependents_of(name, among=None):
    inst = installed() if among is None else among
    out = []
    for n in inst:
        m = installed_manifest(n) or {}
        if name in (m.get("requires") or []):
            out.append(n)
    return out


def supports(m):
    return system.OS in (m.get("platforms") or [])


def parse_sets(pairs):
    out = {}
    for p in pairs or []:
        if "=" not in p:
            raise ui.Abort(f"--set needs key=value, got: {p}")
        k, v = p.split("=", 1)
        out[k.strip()] = v
    return out


# ---------- commands ----------

def cmd_available(a):
    avail = refresh(a.offline)
    inst = installed()
    if not avail:
        ui.say("No modules in the repo yet.")
        return 0
    ui.say(f"Modules in {source.describe(get_source())}:\n")
    for i, (n, m) in enumerate(avail.items(), 1):
        notes = []
        if not supports(m):
            notes.append(f"not available on {system.OS}")
        if m["requires"]:
            notes.append("needs " + ", ".join(m["requires"]))
        if n in inst:
            iv = inst[n]["version"]
            if system.version_tuple(m["version"]) > system.version_tuple(iv):
                notes.append(f"installed {iv}, update available")
            else:
                notes.append("installed")
        tail = f"   ({'; '.join(notes)})" if notes else ""
        ui.say(f"  [{i}] {n:<8} v{m['version']:<7} {m['description']}{tail}")
    ui.say("\nInstall with: kit install <module>")
    return 0


def cmd_list(a):
    inst = installed()
    if not inst:
        ui.say("Nothing installed yet. See what's available: kit available")
        return 0
    for n, rec in inst.items():
        m = installed_manifest(n) or {}
        extra = " (installed as a dependency)" if rec.get("auto") else ""
        ui.say(f"{n:<8} v{rec['version']:<7} {m.get('description', '')}{extra}")
    return 0


def cmd_info(a):
    avail = refresh(a.offline)
    n = a.module
    m = installed_manifest(n) or avail.get(n)
    if not m:
        raise ui.Abort(f'No module called "{n}". See: kit available')
    rec = installed().get(n)
    latest = avail.get(n, {}).get("version")
    ui.say(f"{n}: {m.get('description', '')}")
    ui.say(f"  installed   {('v' + rec['version']) if rec else 'no'}" + (f" (repo has v{latest})" if latest and rec and latest != rec["version"] else ""))
    if not rec and latest:
        ui.say(f"  version     v{latest}")
    ui.say(f"  platforms   {', '.join(m.get('platforms') or [])}")
    ui.say(f"  needs       {', '.join(m.get('requires') or []) or 'nothing'}")
    deps = dependents_of(n)
    ui.say(f"  needed by   {', '.join(deps) or 'nothing installed'}")
    cmds = [c for c, _ in module_commands(m)]
    if cmds:
        ui.say(f"  commands    {', '.join(cmds)}")
    if rec:
        ui.say(f"  config      {os.path.join(system.CONFIG_DIR, n)}")
        if m.get("schedule"):
            ui.say(f"  schedule    {system.schedule_info(n) or 'MISSING (run kit doctor)'}")
    return 0


def pick_modules(avail):
    inst = installed()
    names = [n for n, m in avail.items() if supports(m)]
    if not names:
        raise ui.Abort("No modules available for this platform.")
    if not ui.interactive():
        raise ui.Abort("Name the module(s) to install, e.g. kit install rss")
    ui.say("Available modules:")
    for i, n in enumerate(names, 1):
        m = avail[n]
        tail = []
        if m["requires"]:
            tail.append("needs " + ", ".join(m["requires"]))
        if n in inst:
            tail.append("installed")
        ui.say(f"  [{i}] {n:<8} v{m['version']:<7} {m['description']}" + (f"  ({'; '.join(tail)})" if tail else ""))
    while True:
        raw = input("Pick modules (e.g. 2, or 1 2): ").replace(",", " ").split()
        try:
            picked = [names[int(x) - 1] for x in raw if 1 <= int(x) <= len(names)]
        except ValueError:
            picked = [x for x in raw if x in names]
        if picked:
            return picked
        if not raw:
            raise ui.Abort("Nothing picked.")


def resolve(names, avail, inst):
    """Install order (dependencies first) and the deps that were not asked for."""
    order, extra = [], []

    def visit(n, stack):
        if n in stack:
            raise ui.Abort("Circular dependency: " + " -> ".join(stack + [n]))
        if n in order or n in inst:
            return
        if n not in avail:
            raise ui.Abort(f'"{stack[-1] if stack else n}" needs "{n}", which isn\'t in the repo.' if stack else f'No module called "{n}". See: kit available')
        for d in avail[n]["requires"]:
            visit(d, stack + [n])
        order.append(n)
        if n not in names:
            extra.append(n)

    for n in names:
        visit(n, [])
    return order, extra


def install_one(m, opts, yes, auto=False):
    name = m["name"]
    ui.say(f"\n== {name} ==")
    dest = os.path.join(system.MODULES_DIR, name)
    system.copy_tree(m["_dir"], dest)
    m = installed_manifest(name)
    ctx = Ctx(m, opts, yes)
    hooks = load_hooks(name, dest)
    try:
        hook(hooks, "configure", ctx)
        sched = apply_runtime(m)
        if sched:
            ui.say(f"Schedule: {sched}")
        hook(hooks, "install", ctx)
    except BaseException:
        remove_runtime(m)
        system.rmtree(dest)
        raise
    st = load_state()
    st["modules"][name] = {"version": m.get("version", "0.0.0"), "auto": auto,
                           "installed_at": datetime.datetime.now().isoformat(timespec="seconds")}
    save_state(st)
    problems = [msg for ok, msg in (hook(hooks, "doctor", ctx) or []) if not ok]
    if problems:
        ui.warn(f"{name} doctor: " + "; ".join(problems))
    elif getattr(hooks, "doctor", None):
        ui.say(f"{name} doctor ... all good")


def cmd_install(a):
    avail = refresh(a.offline)
    opts = parse_sets(a.set)
    names = a.modules or pick_modules(avail)
    inst = installed()
    for n in names:
        if n in avail and not supports(avail[n]):
            raise ui.Abort(f"{n} isn't available on {system.OS} (supports: {', '.join(avail[n]['platforms'])}).")
    already = [n for n in names if n in inst]
    for n in already:
        ui.say(f"{n} is already installed (v{inst[n]['version']}). Change settings: kit configure {n}")
    todo = [n for n in names if n not in inst]
    if not todo:
        return 0
    order, extra = resolve(todo, avail, inst) if not a.force else (todo, [])
    for n in extra:
        needers = [x for x in order if n in avail[x]["requires"]]
        if not ui.confirm(f"{', '.join(needers)} needs {n}, which isn't installed. Install it too?", True, assume=a.yes):
            raise ui.Abort("Cancelled, nothing installed.")
    for n in order:
        if not supports(avail[n]):
            raise ui.Abort(f"{n} (needed) isn't available on {system.OS}.")
    done = []
    for n in order:
        install_one(avail[n], opts, a.yes, auto=n in extra)
        done.append(n)
    st = installed()
    ui.say("\nDone. Installed: " + ", ".join(f"{n} {st[n]['version']}" for n in done))
    return 0


def cmd_configure(a):
    n = a.module
    m = installed_manifest(n)
    if not m or n not in installed():
        raise ui.Abort(f"{n} isn't installed. Install it: kit install {n}")
    hooks = load_hooks(n, os.path.join(system.MODULES_DIR, n))
    if not getattr(hooks, "configure", None):
        ui.say(f"{n} has no settings.")
        return 0
    ctx = Ctx(m, parse_sets(a.set), a.yes)
    hook(hooks, "configure", ctx)
    hook(hooks, "reconfigured", ctx)
    ui.say(f"Saved {n} settings.")
    return 0


def remove_one(n, purge):
    m = installed_manifest(n) or {"name": n}
    mod_dir = os.path.join(system.MODULES_DIR, n)
    hooks = load_hooks(n, mod_dir) if os.path.isdir(mod_dir) else None
    ctx = Ctx(m)
    try:
        hook(hooks, "uninstall", ctx, purge)
    except Exception as e:  # noqa: BLE001
        ui.warn(f"{n} uninstall hook failed: {e}")
    remove_runtime(m)
    system.rmtree(mod_dir)
    cfg = os.path.join(system.CONFIG_DIR, n)
    if purge:
        system.rmtree(cfg)
    st = load_state()
    st["modules"].pop(n, None)
    save_state(st)
    kept = "" if purge or not os.path.isdir(cfg) else f" Config kept in {cfg} (reinstall picks it up; --purge deletes it)."
    ui.say(f"Removed {n}.{kept}")


def cmd_remove(a):
    inst = installed()
    targets = []
    for n in a.modules:
        if n not in inst:
            raise ui.Abort(f"{n} isn't installed.")
        targets.append(n)
    # everything that depends on a target, recursively
    dependents = []
    stack = list(targets)
    while stack:
        cur = stack.pop()
        for d in dependents_of(cur):
            if d not in targets and d not in dependents:
                dependents.append(d)
                stack.append(d)
    if dependents and not a.force:
        for t in targets:
            ds = [d for d in dependents if t in (installed_manifest(d) or {}).get("requires", [])]
            if ds:
                ui.warn(f"{', '.join(ds)} {'depends' if len(ds) == 1 else 'depend'} on {t}. Removing {t} will break {'it' if len(ds) == 1 else 'them'}.")
        if a.with_dependents:
            choice = 1
        elif not ui.interactive():
            raise ui.Abort("Cancelled. Use --with-dependents to remove them too, or --force to remove anyway.")
        else:
            choice = ui.choose("What now?", ["Cancel", f"Remove {', '.join(dependents + targets)}",
                                             f"Remove only {', '.join(targets)} anyway"], 0)
        if choice == 0:
            raise ui.Abort("Cancelled, nothing removed.")
        if choice == 1:
            targets = dependents + targets
    elif not a.yes and ui.interactive():
        if not ui.confirm(f"Remove {', '.join(targets)}?", True):
            raise ui.Abort("Cancelled.")
    # dependencies that would be left unused afterwards
    leftover = []
    for t in targets:
        for d in (installed_manifest(t) or {}).get("requires", []):
            if d in inst and d not in targets and d not in leftover:
                leftover.append(d)
    for t in targets:
        remove_one(t, a.purge)
    for d in leftover:
        if not dependents_of(d) and ui.confirm(
                f"Nothing else uses {d} now. Remove {d} too?", False, assume=a.yes):
            remove_one(d, a.purge)
    return 0


def update_core(avail_root, quiet=False):
    new = source.core_version(avail_root)
    st = load_state()
    cur = st.get("core", VERSION)
    if system.version_tuple(new) > system.version_tuple(cur):
        system.copy_tree(os.path.join(avail_root, "kit"), os.path.join(system.CORE_DIR, "kit"))
        st["core"] = new
        save_state(st)
        ui.say(f"kit updated {cur} -> {new}")
    elif not quiet:
        ui.say(f"kit is up to date ({cur})")


def cmd_update(a):
    avail = refresh(a.offline)
    inst = installed()
    if not a.modules:
        update_core(source.REPO_DIR)
    targets = a.modules or list(inst)
    for n in targets:
        if n not in inst:
            raise ui.Abort(f"{n} isn't installed.")
    for n in targets:
        if n not in avail:
            ui.warn(f"{n} is no longer in the repo, leaving it as is.")
            continue
        m = avail[n]
        old = inst[n]["version"]
        if system.version_tuple(m["version"]) <= system.version_tuple(old) and not a.force:
            ui.say(f"{n} is up to date ({old})")
            continue
        missing = [d for d in m["requires"] if d not in installed()]
        if missing:
            if not ui.confirm(f"{n} {m['version']} now needs {', '.join(missing)}. Install {'it' if len(missing) == 1 else 'them'}?", True, assume=a.yes):
                ui.warn(f"Skipped {n}.")
                continue
            order, _ = resolve(missing, avail, installed())
            for d in order:
                install_one(avail[d], {}, a.yes, auto=True)
        dest = os.path.join(system.MODULES_DIR, n)
        system.copy_tree(m["_dir"], dest)
        im = installed_manifest(n)
        hooks = load_hooks(n, dest)
        hook(hooks, "upgrade", Ctx(im, {}, a.yes), old)
        apply_runtime(im)
        st = load_state()
        st["modules"][n]["version"] = im.get("version", old)
        save_state(st)
        ui.say(f"{n} updated {old} -> {im.get('version')}")
    return 0


def cmd_doctor(a):
    inst = installed()
    targets = a.modules or list(inst)
    bad = 0
    ui.say(f"kit {load_state().get('core', VERSION)} on {system.OS}, source {source.describe(get_source())}")
    if not system.bin_on_path():
        ui.warn(f"{system.BIN_DIR} isn't on PATH in this shell")
    if not inst:
        ui.say("Nothing installed.")
    for n in targets:
        if n not in inst:
            ui.warn(f"{n} isn't installed")
            bad += 1
            continue
        m = installed_manifest(n)
        if not m:
            ui.warn(f"{n}: files missing, reinstall with kit update {n} --force")
            bad += 1
            continue
        hooks = load_hooks(n, os.path.join(system.MODULES_DIR, n))
        checks = list(hook(hooks, "doctor", Ctx(m)) or [])
        for d in m.get("requires") or []:
            checks.insert(0, (d in inst, f"needs {d}" + ("" if d in inst else " (NOT installed)")))
        for cmd, _ in module_commands(m):
            checks.append((os.path.exists(system.shim_path(cmd)), f"command {cmd}"))
        if m.get("schedule") and not any(msg.startswith("schedule:") for _, msg in checks):
            info = system.schedule_info(n)
            checks.append((bool(info), f"schedule: {info or 'missing'}"))
        ui.say(f"\n{n} v{inst[n]['version']}")
        for ok, msg in checks:
            ui.say(f"  {'ok ' if ok else 'BAD'}  {msg}")
            bad += 0 if ok else 1
    ui.say("\nAll good." if not bad else f"\n{bad} problem(s).")
    return 0 if not bad else 1


def write_pth():
    purelib = sysconfig.get_paths()["purelib"]
    os.makedirs(purelib, exist_ok=True)
    system.write_text(os.path.join(purelib, "kit.pth"), system.CORE_DIR + "\n")


def cmd_setup(a):
    """Called by bootstrap.sh / bootstrap.ps1."""
    src = a.source or source.default_source()
    root = os.path.abspath(a.from_dir) if a.from_dir else None
    if root is None:
        source.refresh(src)
        root = source.REPO_DIR
    elif os.path.normcase(root) != os.path.normcase(source.REPO_DIR):
        system.copy_tree(root, source.REPO_DIR)
    system.copy_tree(os.path.join(source.REPO_DIR, "kit"), os.path.join(system.CORE_DIR, "kit"))
    write_pth()
    system.write_shim("kit", ["-m", "kit.cli"])
    st = load_state()
    st["source"] = src
    st["core"] = source.core_version()
    save_state(st)
    # modules already installed (re-running bootstrap) get fresh shims
    for n in st["modules"]:
        m = installed_manifest(n)
        if m:
            apply_runtime(m)
    hint = system.ensure_path()
    ui.say(f"kit {st['core']} ready ({system.OS}).")
    if hint:
        ui.say(hint)
    if a.no_picker or not ui.interactive():
        ui.say("Install modules with: kit install <module>   (list: kit available)")
        return 0
    if ui.confirm("Open the module picker now?", True):
        return cmd_install(argparse.Namespace(modules=[], set=[], yes=False, force=False, offline=True))
    return 0


def cmd_self_uninstall(a):
    inst = installed()
    if not a.yes and ui.interactive():
        what = f" and {', '.join(inst)}" if inst else ""
        if not ui.confirm(f"Remove kit{what} from this device?", False):
            raise ui.Abort("Cancelled.")
    for n in list(inst):
        remove_one(n, a.purge)
    system.remove_shim("kit")
    system.remove_path_entry()
    if a.purge:
        system.rmtree(system.CONFIG_DIR)
    else:
        st = load_state()
        st.pop("core", None)
        save_state(st)
    if system.IS_WIN:
        # can't delete the running python.exe; finish after we exit
        subprocess.Popen(f'cmd /c timeout /t 3 >nul & rmdir /s /q "{system.KIT_HOME}"',
                         shell=True, creationflags=0x08000000)
    else:
        system.rmtree(system.KIT_HOME)
    ui.say("kit removed." + ("" if a.purge else f" Config kept in {system.CONFIG_DIR}."))
    return 0


def build_parser():
    p = argparse.ArgumentParser(prog="kit", add_help=False)
    sub = p.add_subparsers(dest="cmd")

    def add(name, *aliases):
        sp = sub.add_parser(name, aliases=list(aliases), add_help=False)
        sp.add_argument("--offline", action="store_true")
        sp.add_argument("-y", "--yes", action="store_true")
        return sp

    add("available", "avail", "search")
    add("list", "ls")
    sp = add("info", "show")
    sp.add_argument("module")
    sp = add("install", "add")
    sp.add_argument("modules", nargs="*")
    sp.add_argument("--set", action="append", default=[])
    sp.add_argument("--force", action="store_true")
    sp = add("configure", "config")
    sp.add_argument("module")
    sp.add_argument("--set", action="append", default=[])
    sp = add("remove", "rm", "uninstall")
    sp.add_argument("modules", nargs="+")
    sp.add_argument("--purge", action="store_true")
    sp.add_argument("--force", action="store_true")
    sp.add_argument("--with-dependents", action="store_true")
    sp = add("update", "upgrade")
    sp.add_argument("modules", nargs="*")
    sp.add_argument("--force", action="store_true")
    sp = add("doctor")
    sp.add_argument("modules", nargs="*")
    sp = add("setup")
    sp.add_argument("--source")
    sp.add_argument("--from", dest="from_dir")
    sp.add_argument("--no-picker", action="store_true")
    sp = add("self-uninstall")
    sp.add_argument("--purge", action="store_true")

    def err(msg):
        print(f"kit: {msg}\n")
        print(HELP)
        sys.exit(2)

    p.error = err
    for sp in sub.choices.values():
        sp.error = err
    return p


COMMANDS = {"available": cmd_available, "avail": cmd_available, "search": cmd_available,
            "list": cmd_list, "ls": cmd_list, "info": cmd_info, "show": cmd_info,
            "install": cmd_install, "add": cmd_install, "configure": cmd_configure, "config": cmd_configure,
            "remove": cmd_remove, "rm": cmd_remove, "uninstall": cmd_remove,
            "update": cmd_update, "upgrade": cmd_update, "doctor": cmd_doctor,
            "setup": cmd_setup, "self-uninstall": cmd_self_uninstall}


def main(argv=None):
    system.fix_console()
    argv = sys.argv[1:] if argv is None else argv
    if not argv or argv[0] in ("-h", "--help", "help"):
        print(HELP)
        return 0
    if argv[0] in ("-V", "--version", "version"):
        print(f"kit {load_state().get('core', VERSION)}")
        return 0
    a = build_parser().parse_args(argv)
    try:
        return COMMANDS[a.cmd](a) or 0
    except ui.Abort as e:
        if e.code:
            print(e.code)
        return 1
    except KeyboardInterrupt:
        print("\nCancelled.")
        return 130


if __name__ == "__main__":
    sys.exit(main())
