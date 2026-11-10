"""Install libraries the bot imports when they are missing from this Python.

The wallet store needs the cryptography package. A machine that only installed
part of the stack can still start: this checks once, then pip-installs the gaps.
"""
import importlib
import subprocess
import sys

# (module import name, pip requirement)
REQUIRED = (
    ("telegram", "python-telegram-bot>=21"),
    ("cryptography", "cryptography>=41"),
    ("eth_account", "eth-account>=0.13,<1"),
    ("eth_utils", "eth-utils>=5"),
    ("eth_abi", "eth-abi>=5"),
    ("web3", "web3>=7,<9"),
    ("httpx", "httpx>=0.27"),
    ("requests", "requests>=2.32"),
    ("websockets", "websockets>=13"),
    ("solana", "solana>=0.36.6,<0.37"),
    ("solders", "solders>=0.26,<0.27"),
    ("certifi", "certifi>=2024.2.2"),
)


def missing(modules=REQUIRED):
    """Pip requirements whose import currently fails."""
    needed = []
    for module, spec in modules:
        try:
            importlib.import_module(module)
        except ImportError:
            needed.append(spec)
    return needed


def _pip_install(specs, extra=()):
    cmd = [sys.executable, "-m", "pip", "install", "--disable-pip-version-check", *extra, *specs]
    return subprocess.run(cmd, text=True, capture_output=True)


def ensure(modules=REQUIRED, runner=_pip_install):
    """Install missing libraries into the Python that is running the bot.

    Returns the requirements that were installed. Raises SystemExit when pip
    cannot install them, with the command to run by hand.
    """
    needed = missing(modules)
    if not needed:
        return []
    print("installing missing libraries: " + ", ".join(needed), flush=True)
    result = runner(needed)
    stderr = (getattr(result, "stderr", None) or "")
    if getattr(result, "returncode", 1) != 0 and "externally-managed-environment" in stderr:
        print("this Python blocks normal pip installs; retrying into the same environment", flush=True)
        result = runner(needed, ("--break-system-packages",))
        stderr = (getattr(result, "stderr", None) or "")
    if getattr(result, "returncode", 1) != 0:
        detail = stderr.strip().splitlines()
        tail = detail[-1] if detail else "pip install failed"
        raise SystemExit(
            "could not install " + ", ".join(needed) + ".\n"
            + tail + "\n"
            + f"run: {sys.executable} -m pip install -r requirements.txt"
        )
    still = missing(modules)
    if still:
        raise SystemExit("still missing after install: " + ", ".join(still))
    return needed
