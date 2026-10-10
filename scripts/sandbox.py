"""
Sandbox for LaTeX runs (imported by build.py and accel.py, standard library only).

Off unless LATEX_SANDBOX=bwrap (build.py --sandbox and serve.py --sandbox set it; hosted
workers always do). Then every TeX process runs under bubblewrap (Linux): no network, no
other processes, the system and the TeX installation read-only, the document's directory
read-only, only its build directory writable, /tmp private, a minimal environment and
resource limits. If bubblewrap is missing or does not work, builds are refused, never run
unsandboxed. See README, "Sandboxed builds".
"""

from __future__ import annotations

import functools
import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path

try:
    import resource  # POSIX only; the sandbox needs Linux anyway.
except ImportError:
    resource = None

VARIABLE = "LATEX_SANDBOX"

# Read-only inside: programs, libraries, perl (latexmk), fonts and fontconfig, TeX's configuration.
# Not /etc as a whole: it holds secrets (/etc/shadow, keys) and host details.
SYSTEM_DIRS = (
    "/usr", "/bin", "/sbin", "/lib", "/lib32", "/lib64", "/libx32",
    "/etc/alternatives", "/etc/fonts", "/etc/texmf", "/etc/perl", "/etc/LatexMk", "/etc/latexmkrc",
    "/etc/ld.so.cache", "/etc/ld.so.conf", "/etc/ld.so.conf.d", "/etc/localtime", "/etc/papersize",
    "/etc/libpaper.d", "/var/cache/fontconfig",
)
# kpsewhich variables naming the TeX installation (formats, ls-R, texmf.cnf), bound read-only.
TEX_VARIABLES = ("TEXMFROOT", "TEXMFDIST", "TEXMFLOCAL", "TEXMFSYSVAR", "TEXMFSYSCONFIG")
# The only variables a sandboxed run inherits (lowercase ones are kpathsea's: log width, openin_any, ...).
KEEP_ENV = {
    "PATH", "LANG", "LANGUAGE", "TZ", "SOURCE_DATE_EPOCH", "FORCE_SOURCE_DATE",
    "max_print_line", "error_line", "half_error_line", "shell_escape", "openin_any", "openout_any",
}
# ponytail: no RLIMIT_NPROC, it counts every process of the user (the server too); a hosted
# operator caps tasks per worker with systemd TasksMax= instead.
MEMORY = 2 << 30  # RLIMIT_AS
FILE_SIZE = 512 << 20  # RLIMIT_FSIZE
OPEN_FILES = 1024  # RLIMIT_NOFILE


class SandboxError(OSError):
    """The sandbox is required but cannot be used. An OSError, so callers report a failed build."""


def enabled() -> bool:
    value = os.environ.get(VARIABLE, "").strip().lower()
    if value in ("", "0", "off", "none"):
        return False
    if value != "bwrap":
        raise SandboxError(f"{VARIABLE}={value!r}: the only sandbox is 'bwrap' (or leave it unset).")
    return True


@functools.cache
def problem() -> str | None:
    """Why bubblewrap cannot be used here, or None if it works (checked once per process)."""
    if not sys.platform.startswith("linux"):
        return "the LaTeX sandbox (bubblewrap) needs Linux."
    bwrap = shutil.which("bwrap")
    if bwrap is None:
        return "bwrap (bubblewrap) was not found on PATH; install it (apt-get install bubblewrap)."
    try:
        result = subprocess.run(
            [bwrap, "--unshare-all", "--die-with-parent", "--ro-bind", "/", "/", "true"],
            stdin=subprocess.DEVNULL, capture_output=True, text=True, errors="replace", timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return f"bwrap does not run: {exc}"
    if result.returncode != 0:
        return f"bwrap does not work here (user namespaces disabled?): {result.stderr.strip()}"
    return None


def check() -> None:
    """Raise SandboxError if the sandbox is on but unusable."""
    if enabled() and problem():
        raise SandboxError(f"{VARIABLE}=bwrap, but {problem()} Refusing to build unsandboxed.")


@functools.cache
def tex_dirs() -> tuple[str, ...]:
    found = []
    for variable in TEX_VARIABLES:
        try:
            value = subprocess.run(
                ["kpsewhich", f"-var-value={variable}"], capture_output=True, text=True, timeout=30,
            ).stdout.strip()
        except (OSError, subprocess.TimeoutExpired):
            continue
        for part in value.split(os.pathsep):
            part = part.lstrip("!")
            if part.startswith("/") and "{" not in part and os.path.realpath(part) != "/":
                found.append(os.path.realpath(part))
    return tuple(found)


def readable_dirs(program: str) -> list[str]:
    """System directories to bind read-only, without ones inside another."""
    dirs = [*SYSTEM_DIRS, *tex_dirs()]
    found = shutil.which(program)
    if found:  # A TeX Live from tug.org: its bin/ directory (often covered by TEXMFROOT).
        dirs.append(os.path.dirname(os.path.realpath(found)))
    kept: list[str] = []
    for path in dirs:
        if not any(path == other or path.startswith(other.rstrip("/") + "/") for other in kept):
            kept.append(path)
    return kept


def wrap(cmd: list[str], cwd: Path, writable: list[Path], readable: list[Path] = ()) -> list[str]:
    """The bwrap command line that runs cmd in cwd with only `writable` writable; cwd and `readable` read-only."""
    args = [
        shutil.which("bwrap") or "bwrap", "--unshare-all", "--die-with-parent", "--new-session",
        "--proc", "/proc", "--dev", "/dev", "--tmpfs", "/tmp",
    ]
    for path in readable_dirs(cmd[0]):
        args += ["--ro-bind-try", path, path]
    for path in (cwd, *readable):
        args += ["--ro-bind", str(path), str(path)]
    for path in writable:  # After the read-only binds, so a build directory inside the source stays writable.
        args += ["--bind", str(path), str(path)]
    return [*args, "--chdir", str(cwd), "--", *cmd]


def environment(env: dict[str, str] | None, var: Path) -> dict[str, str]:
    """A minimal environment: no tokens or secrets of the parent; caches in the build directory."""
    env = os.environ if env is None else env
    kept = {key: value for key, value in env.items() if key in KEEP_ENV or key.startswith("LC_")}
    return {
        **kept, "HOME": "/tmp", "TMPDIR": "/tmp",
        # luaotfload's and fontconfig's caches persist per document; TEXMFSYSVAR (read-only) is read first.
        "TEXMFVAR": str(var), "TEXMFCONFIG": str(var), "XDG_CACHE_HOME": str(var / "cache"),
        "PAR_GLOBAL_TMPDIR": str(var / "par"),  # TeX Live's biber unpacks itself there once, not on every run.
    }


def _limits(values: list[tuple[int, int]]) -> None:  # In the child between fork and exec: no imports.
    for kind, value in values:
        resource.setrlimit(kind, (value, value))


def spawn(
    cmd: list[str], cwd: Path, build_dir: Path, env: dict[str, str] | None = None, cpu: int = 1800,
    work: Path | None = None,
) -> dict:
    """
    Keyword arguments for subprocess.Popen / subprocess.run that run a TeX command: the one place
    every LaTeX process of build.py and accel.py starts. Unsandboxed unless LATEX_SANDBOX=bwrap.
    The sandbox may write build_dir, or (a figure job) only read it and write work and the caches.
    cpu: the CPU-seconds limit, the run's timeout. Call scrub(build_dir) once the run has ended.
    """
    if not enabled():
        return {"args": cmd, "cwd": cwd, "env": env}
    check()
    var = build_dir / "texmf-var"
    (var / "par").mkdir(parents=True, exist_ok=True)
    values = []
    for kind, value in ((resource.RLIMIT_CPU, cpu), (resource.RLIMIT_AS, MEMORY),
                        (resource.RLIMIT_FSIZE, FILE_SIZE), (resource.RLIMIT_NOFILE, OPEN_FILES)):
        hard = resource.getrlimit(kind)[1]
        values.append((kind, value if hard == resource.RLIM_INFINITY else min(value, hard)))
    return {
        "args": wrap(cmd, cwd, [build_dir], []) if work is None else wrap(cmd, cwd, [work, var], [build_dir]),
        "cwd": cwd, "env": environment(env, var), "preexec_fn": functools.partial(_limits, values),
    }


def scrub(directory: Path) -> None:
    """
    Delete symlinks, FIFOs, sockets and devices a sandboxed run left in its writable directory.
    Python outside the sandbox reads and writes there (the PDF, logs, figure copies); a symlink
    to /etc/shadow or ~/.ssh would make it read or write the host's file. Runs once no sandboxed
    process is left (bwrap kills the whole PID namespace when the command ends).
    """
    if not enabled():
        return
    for root, dirs, files in os.walk(directory):
        for name in dirs + files:
            path = os.path.join(root, name)
            try:
                mode = os.lstat(path).st_mode
                if not (stat.S_ISREG(mode) or stat.S_ISDIR(mode)):
                    os.unlink(path)
            except OSError:
                pass
