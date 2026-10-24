"""Android Studio AVD backend. scrcpy windows are reused, not piled up."""
from __future__ import annotations

import os
import shutil
import signal
import subprocess
import time
from pathlib import Path

from core.errors import DCGenError, FailureCode
from core.log import log
from device import identity as ident_mod
from .base import Instance

ADB_TIMEOUT = 30


class AvdBackend:
    def __init__(self, cfg, headless: bool = True):
        self.cfg = cfg
        self.headless = headless
        self.emulator = self._which("emulator")
        self.adb = self._which("adb")
        self.avdmanager = self._which("avdmanager")
        self.scrcpy = shutil.which("scrcpy")
        self.avd_home = Path(os.environ.get("ANDROID_AVD_HOME",
                                            Path.home() / ".android" / "avd"))
        self._procs: dict[int, subprocess.Popen] = {}
        self._windows: dict[int, subprocess.Popen] = {}
        self._proxies: dict[int, str] = {}
        self._props_ok: dict[int, bool] = {}

    @staticmethod
    def _which(name: str) -> str:
        path = shutil.which(name)
        if not path:
            raise DCGenError(FailureCode.E_UNKNOWN,
                             f"{name} not on PATH - install android-platform-tools / "
                             "android-commandlinetools and set ANDROID_HOME")
        return path

    def instance(self, idx: int) -> Instance:
        e = self.cfg.emulator
        return Instance(idx=idx, name=f"{e.prefix}{idx}",
                        console_port=e.console_port_base + 2 * idx,
                        adb_port=e.adb_port_base + 2 * idx,
                        proxy_port=e.proxy_port_base + 2 * idx)

    def list_instances(self) -> list[Instance]:
        if not self.avd_home.exists():
            return []
        out = []
        for path in sorted(self.avd_home.glob(f"{self.cfg.emulator.prefix}*.avd")):
            try:
                idx = int(path.stem.replace(self.cfg.emulator.prefix, ""))
            except ValueError:
                continue
            out.append(self.instance(idx))
        return out

    def create(self, name: str, device: str | None = None) -> None:
        profile = device or self.cfg.emulator.device_profile
        r = subprocess.run(
            [self.avdmanager, "create", "avd", "-n", name, "-k",
             self.cfg.emulator.image, "-d", profile, "--force"],
            input=b"no\n", capture_output=True)
        if r.returncode != 0:
            raise DCGenError(FailureCode.E_UNKNOWN,
                             f"avdmanager create failed: {r.stderr.decode()[:300]}")
        self._tune_config(name)
        log(f"created AVD {name} ({profile})", "ok")

    def _tune_config(self, name: str) -> None:
        ini = self.avd_home / f"{name}.avd" / "config.ini"
        if not ini.exists():
            return
        want = {
            "hw.ramSize": str(self.cfg.emulator.mem_mb),
            "hw.cpu.ncore": str(self.cfg.emulator.cores),
            "disk.dataPartition.size": self.cfg.emulator.data_partition,
            "vm.heapSize": str(self.cfg.emulator.heap_mb),
            "hw.gpu.enabled": "yes",
            "hw.gpu.mode": self.cfg.emulator.gpu_headless if self.headless else "host",
            "hw.keyboard": "yes",
            "hw.lcd.density": str(self.cfg.device.dpi),
            "hw.lcd.width": str(self.cfg.device.width),
            "hw.lcd.height": str(self.cfg.device.height),
            "hw.audioInput": "no",
            "hw.audioOutput": "no",
            "showDeviceFrame": "no",
        }
        lines = ini.read_text().splitlines()
        seen = set()
        for i, line in enumerate(lines):
            key = line.split("=", 1)[0].strip()
            if key in want:
                lines[i] = f"{key}={want[key]}"
                seen.add(key)
        for key, val in want.items():
            if key not in seen:
                lines.append(f"{key}={val}")
        ini.write_text("\n".join(lines) + "\n")

    def clone(self, src: int, dst: int) -> None:
        s, d = self.instance(src), self.instance(dst)
        subprocess.run([self.avdmanager, "create", "avd", "-n", d.name, "-k",
                        self.cfg.emulator.image, "-d", self.cfg.emulator.device_profile,
                        "--force"], input=b"no\n", capture_output=True)
        self._tune_config(d.name)
        src_img = self.avd_home / f"{s.name}.avd" / "userdata-qemu.img"
        dst_img = self.avd_home / f"{d.name}.avd" / "userdata-qemu.img"
        if src_img.exists():
            shutil.copy2(src_img, dst_img)
        log(f"cloned {s.name} -> {d.name}", "ok")

    def start(self, idx: int, headless: bool | None = None) -> None:
        inst = self.instance(idx)
        if self.is_booted(idx):
            log(f"{inst.name} already booted", "ok")
            return
        headless = self.headless if headless is None else headless
        e = self.cfg.emulator
        cmd = [self.emulator, "-avd", inst.name, "-port", str(inst.console_port),
               "-memory", str(e.mem_mb), "-cores", str(e.cores),
               "-no-audio", "-no-boot-anim", "-no-metrics",
               "-netdelay", "none", "-netspeed", "full", "-no-snapshot-save"]
        cmd += (["-no-window", "-gpu", e.gpu_headless] if headless
                else ["-gpu", e.gpu_windowed])
        if (self.avd_home / f"{inst.name}.avd" / "snapshots" / e.snapshot).exists():
            cmd += ["-snapshot", e.snapshot]
        if e.writable_system:
            cmd += ["-writable-system"]
        proxy_port = self._proxies.get(idx)
        if proxy_port:
            cmd += ["-http-proxy", f"http://10.0.2.2:{proxy_port}"]
        if self._props_ok.get(idx) is not False:
            for k, v in ident_mod.for_instance(idx).boot_props().items():
                cmd += ["-prop", f"{k}={v}"]

        logfile = self.cfg.paths.logs / f"emulator-{idx}.log"
        logfile.parent.mkdir(parents=True, exist_ok=True)
        self._procs[idx] = subprocess.Popen(
            cmd, stdout=open(logfile, "ab"), stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL, start_new_session=True)
        log(f"{inst.name} launching (headless={headless}, "
            f"proxy={'10.0.2.2:' + proxy_port if proxy_port else 'none'})")

    def wait_booted(self, idx: int) -> None:
        inst = self.instance(idx)
        deadline = time.time() + self.cfg.emulator.boot_timeout
        while time.time() < deadline:
            if (self._getprop(idx, "sys.boot_completed") == "1"
                    and self._getprop(idx, "init.svc.bootanim") == "stopped"):
                self._unlock(idx)
                log(f"{inst.name} booted", "ok")
                return
            time.sleep(2)
        raise DCGenError(FailureCode.E_BOOT_TIMEOUT,
                         f"{inst.name} did not boot in {self.cfg.emulator.boot_timeout}s")

    def is_booted(self, idx: int) -> bool:
        return self._serial_present(idx) and self._getprop(idx, "sys.boot_completed") == "1"

    def stop(self, idx: int) -> None:
        try:
            self.adb_run(idx, "emu", "kill", timeout=10)
        except DCGenError:
            proc = self._procs.get(idx)
            if proc and proc.poll() is None:
                proc.send_signal(signal.SIGTERM)
        self._procs.pop(idx, None)
        self._windows.pop(idx, None)
        log(f"{self.instance(idx).name} stopped")

    def stop_all(self) -> None:
        for inst in self.list_instances():
            if self._serial_present(inst.idx):
                try:
                    self.stop(inst.idx)
                except Exception:
                    pass

    def _unlock(self, idx: int) -> None:
        self.adb_run(idx, "shell", "input", "keyevent", "82", timeout=10)
        self.adb_run(idx, "shell", "wm", "dismiss-keyguard", timeout=10)

    def adb_serial(self, idx: int) -> str:
        return self.instance(idx).serial

    def _serial_present(self, idx: int) -> bool:
        r = subprocess.run([self.adb, "devices"], capture_output=True, text=True)
        return self.adb_serial(idx) in r.stdout

    def adb_run(self, idx: int, *args: str, timeout: int = ADB_TIMEOUT,
                check: bool = True, binary: bool = False):
        cmd = [self.adb, "-s", self.adb_serial(idx), *args]
        r = subprocess.run(cmd, capture_output=True, timeout=timeout)
        if check and r.returncode != 0:
            err = r.stderr.decode(errors="replace")[:300]
            if "unauthorized" in err:
                raise DCGenError(FailureCode.E_ADB_UNAUTHORIZED,
                                 f"adb unauthorized on {self.adb_serial(idx)}")
            if "device offline" in err or "not found" in err:
                raise DCGenError(FailureCode.E_DEVICE_OFFLINE, err)
            raise DCGenError(FailureCode.E_UNKNOWN, f"adb {' '.join(args)}: {err}")
        return r.stdout if binary else r.stdout.decode(errors="replace")

    def _getprop(self, idx: int, key: str) -> str:
        try:
            return self.adb_run(idx, "shell", "getprop", key, timeout=10,
                                check=False).strip()
        except Exception:
            return ""

    def set_proxy(self, idx: int, host_port: str | None) -> None:
        if host_port is None:
            self._proxies.pop(idx, None)
        else:
            self._proxies[idx] = host_port.rsplit(":", 1)[-1]

    def set_proxy_runtime(self, idx: int, host_port: str | None) -> None:
        port = host_port.rsplit(":", 1)[-1] if host_port else None
        val = f"10.0.2.2:{port}" if port else ":0"
        self.adb_run(idx, "shell", "settings", "put", "global", "http_proxy", val)

    def randomize_identity(self, idx: int) -> None:
        ident = ident_mod.for_instance(idx)
        self.adb_run(idx, "root", check=False)
        time.sleep(1)
        self.adb_run(idx, "shell", "settings", "put", "secure",
                     "android_id", ident.android_id, check=False)

        if self._props_ok.get(idx) is False:
            # build.prop is read by init AT BOOT, so the rewrite is inert until
            # we reboot. Without this the whole flow runs on the image's props
            # while the account claims to be a Pixel 7.
            self._rewrite_build_prop(idx, ident)
            self.reboot(idx)
            bad = self.verify_props(idx, ident)
            if bad:
                log(f"{self.instance(idx).name} prop spoof incomplete: {bad}", "warn")
            else:
                self._props_ok[idx] = True
        log(f"identity set: {ident.short()}")

    def set_locale_tz(self, idx: int) -> None:
        self.adb_run(idx, "shell", "setprop", "persist.sys.timezone",
                     self.cfg.device.timezone, check=False)
        self.adb_run(idx, "shell", "setprop", "persist.sys.locale",
                     self.cfg.device.locale, check=False)

    def locale_tz_applied(self, idx: int) -> bool:
        return (self._getprop(idx, "persist.sys.timezone") == self.cfg.device.timezone
                and self._getprop(idx, "persist.sys.locale") == self.cfg.device.locale)

    def probe_props(self, idx: int) -> bool:
        ok = self._getprop(idx, "ro.product.model") == \
            ident_mod.for_instance(idx).model
        self._props_ok[idx] = ok
        return ok
    #: Most-authoritative first. init resolves ro.product.model from the FIRST
    #: partition in ro.product.property_source_order (default odm,product,vendor,
    #: system) that defines it. /system is LAST, so writing only /system/build.prop
    #: is a silent no-op - which is exactly what bit us.
    PROP_PATHS = (
        "/odm/build.prop",
        "/product/build.prop",
        "/vendor/build.prop",
        "/system_ext/build.prop",
        "/system/build.prop",
    )

    def _prop_map(self, ident) -> dict[str, str]:
        """Bare key plus every per-partition variant, so it lands under whichever
        resolution model this init uses."""
        m: dict[str, str] = {}
        for suf in ("", ".odm", ".product", ".vendor", ".system_ext", ".system"):
            p = f"ro.product{suf}."
            m[p + "model"] = ident.model
            m[p + "brand"] = ident.brand
            m[p + "manufacturer"] = ident.manufacturer
            m[p + "device"] = ident.device
            m[p + "name"] = ident.product
            m[p + "board"] = ident.device
        try:
            a, b, _ = ident.fingerprint.split(":")
            ver, build_id, incr = b.split("/")
            desc = f"{a.split('/')[-1]}-user {ver} {build_id} {incr} release-keys"
        except ValueError:
            desc = ident.fingerprint
        for suf in ("", ".odm", ".product", ".vendor", ".system_ext", ".system"):
            b_ = f"ro{suf}.build."
            m[b_ + "fingerprint"] = ident.fingerprint
            m[b_ + "description"] = desc
            m[b_ + "product"] = ident.product
        m["ro.product.model.arm64"] = ident.model
        return m

    def _rewrite_build_prop(self, idx: int, ident) -> None:
        """Write the identity into every partition that can win the lookup."""
        self.adb_run(idx, "root", check=False)
        self.adb_run(idx, "remount", check=False)

        body = ['for f in ' + " ".join(self.PROP_PATHS) + "; do",
                '  [ -f "$f" ] || continue']
        for key, val in self._prop_map(ident).items():
            body.append(
                f'  if grep -q "^{key}=" "$f"; then '
                f'sed -i "s|^{key}=.*|{key}={val}|" "$f"; '
                f'else echo "{key}={val}" >> "$f"; fi')
        body += ['  echo "wrote $f"', 'done']
        script = "#!/system/bin/sh\n" + "\n".join(body) + "\n"

        local = self.cfg.paths.logs / f"buildprop-{idx}.sh"
        local.parent.mkdir(parents=True, exist_ok=True)
        local.write_text(script)
        self.adb_run(idx, "push", str(local), "/data/local/tmp/spoof.sh", check=False)
        out = self.adb_run(idx, "shell", "sh", "/data/local/tmp/spoof.sh", check=False)
        for line in out.splitlines():
            if line.startswith("wrote "):
                log(f"{self.instance(idx).name} {line}", "ok")

    def reboot(self, idx: int) -> None:
        """build.prop is read by init at boot - a rewrite is inert until then."""
        log(f"{self.instance(idx).name} rebooting to apply build.prop")
        self.adb_run(idx, "reboot", check=False)
        deadline = time.time() + 90
        while time.time() < deadline:          # wait for it to actually go down
            if not self._serial_present(idx):
                break
            if self._getprop(idx, "sys.boot_completed") != "1":
                break
            time.sleep(1)
        self.wait_booted(idx)

    def verify_props(self, idx: int, ident) -> list[str]:
        """Empty list == the spoof is live in the property area, not just on disk."""
        want = {
            "ro.product.model": ident.model,
            "ro.product.brand": ident.brand,
            "ro.product.device": ident.device,
            "ro.build.fingerprint": ident.fingerprint,
        }
        bad = []
        for key, val in want.items():
            got = self._getprop(idx, key)
            if got != val:
                bad.append(f"{key}: want {val!r} got {got!r}")
        return bad

    def snapshot(self, idx: int, name: str) -> None:
        self.adb_run(idx, "emu", "avd", "snapshot", "save", name, timeout=180)
        log(f"{self.instance(idx).name} snapshot '{name}' saved", "ok")

    def restore(self, idx: int, name: str) -> None:
        self.adb_run(idx, "emu", "avd", "snapshot", "load", name, timeout=180)

    def window(self, idx: int) -> None:
        proc = self._windows.get(idx)
        if proc is not None and proc.poll() is None:
            return
        if not self.scrcpy:
            log("scrcpy not installed - brew install scrcpy", "warn")
            return
        self._windows[idx] = subprocess.Popen(
            [self.scrcpy, "-s", self.adb_serial(idx), "--no-audio",
             "--max-size", "720", "--window-title", f"inst-{idx}"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            start_new_session=True)
        log(f"scrcpy window opened for inst-{idx}")

    def app_clear(self, idx: int) -> None:
        self.adb_run(idx, "shell", "pm", "clear", self.cfg.app.package)

    def install_apk(self, idx: int, path: Path) -> None:
    if path.suffix.lower() in (".xapk", ".apks"):
        with tempfile.TemporaryDirectory() as td:
            with zipfile.ZipFile(path) as z:
                z.extractall(td)
            apks = sorted(Path(td).rglob("*.apk"))
            abi = self.abi                      # e.g. "arm64-v8a"
            keep = [a for a in apks
                    if a.name in ("base.apk", "com.discord.apk")
                    or abi in a.name
                    or a.name.startswith(("split_config.", "config."))]
            self.adb_run(idx, "install-multiple", "-r", "-g",
                         *[str(a) for a in (keep or apks)])
            return
    self.adb_run(idx, "install", "-r", "-g", str(path))

    def launch_app(self, idx: int) -> None:
        self.adb_run(idx, "shell", "am", "start", "-n", self.cfg.app.activity)
