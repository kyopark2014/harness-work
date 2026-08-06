"""
Helper for running LibreOffice (soffice) in environments where AF_UNIX
sockets may be blocked (e.g., sandboxed VMs).  Detects the restriction
at runtime and applies an LD_PRELOAD shim if needed.

Usage:
    from office.soffice import run_soffice

    result = run_soffice(["--headless", "--convert-to", "pdf", "input.docx"])

Call soffice through run_soffice, not through subprocess with get_soffice_env():
the env dict carries the shim but names no user profile, and a non-root sandbox
cannot bootstrap the default one -- soffice aborts with "User installation could
not be completed" and converts nothing. get_soffice_env() stays public for the
callers that build their own argv (they must pass -env:UserInstallation too).
"""

import contextlib
import os
import socket
import subprocess
import tempfile
from collections.abc import Iterable
from pathlib import Path


def get_soffice_env() -> dict:
    env = os.environ.copy()
    env["SAL_USE_VCLPLUGIN"] = "svp"

    if _needs_shim():
        shim = _ensure_shim()
        env["LD_PRELOAD"] = str(shim)

    return env


def run_soffice(args: Iterable[str], **kwargs) -> subprocess.CompletedProcess:
    args = list(args)
    with contextlib.ExitStack() as stack:
        if not any(str(a).startswith("-env:UserInstallation") for a in args):
            profile = stack.enter_context(
                tempfile.TemporaryDirectory(prefix="lo_profile_", ignore_cleanup_errors=True)
            )
            args = [f"-env:UserInstallation={Path(profile).as_uri()}"] + args
        try:
            # Justification: fixed `soffice` binary + argv list, shell=False;
            # temp profile dir when caller omits -env:UserInstallation.
            # nosemgrep: python.lang.security.audit.dangerous-subprocess-use-audit
            return subprocess.run(  # nosec B603 — fixed `soffice` binary + argv list, shell=False
                ["soffice", *args],
                env=get_soffice_env(),
                **kwargs,
            )
        except FileNotFoundError as error:
            raise RuntimeError("LibreOffice 'soffice' is not available on PATH") from error
        except (OSError, subprocess.SubprocessError) as error:
            raise RuntimeError("Failed to run soffice") from error



_SHIM_SO = Path(tempfile.gettempdir()) / "lo_socket_shim.so"
# FDs >= this limit are passed through unshimmed to bound shim memory.
MAX_SHIMMED_FDS = 1024


def _needs_shim() -> bool:
    try:
        test_socket = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        test_socket.close()
        return False
    except OSError:
        return True


def _ensure_shim() -> Path:
    if _SHIM_SO.exists():
        return _SHIM_SO

    src = Path(tempfile.gettempdir()) / "lo_socket_shim.c"
    src.write_text(_SHIM_SOURCE)
    # Build a shared LD_PRELOAD library: -shared/-fPIC for .so, -ldl for dlsym.
    try:
        # Justification: fixed `gcc` binary + static argv list under tempfile dir, shell=False;
        # compiles LD_PRELOAD shim only.
        # nosemgrep: python.lang.security.audit.dangerous-subprocess-use-audit
        subprocess.run(  # nosec B603 — fixed `gcc` binary + static argv list under tempfile dir, shell=False
            ["gcc", "-shared", "-fPIC", "-o", str(_SHIM_SO), str(src), "-ldl"],
            check=True,
            capture_output=True,
        )
    except FileNotFoundError as error:
        raise RuntimeError(
            "Cannot build the AF_UNIX socket shim: 'gcc' is not available"
        ) from error
    except subprocess.CalledProcessError as error:
        stderr = (error.stderr or b"").decode("utf-8", "replace").strip()
        raise RuntimeError(f"Failed to compile the AF_UNIX socket shim: {stderr}") from error
    finally:
        src.unlink(missing_ok=True)
    return _SHIM_SO



_SHIM_SOURCE = rf"""
#define _GNU_SOURCE
#include <dlfcn.h>
#include <errno.h>
#include <signal.h>
#include <stdio.h>
#include <stdlib.h>
#include <sys/socket.h>
#include <unistd.h>

/* FDs >= MAX_SHIMMED_FDS are passed through unshimmed. */
#define MAX_SHIMMED_FDS {MAX_SHIMMED_FDS}

static int (*real_socket)(int, int, int);
static int (*real_socketpair)(int, int, int, int[2]);
static int (*real_listen)(int, int);
static int (*real_accept)(int, struct sockaddr *, socklen_t *);
static int (*real_close)(int);
static int (*real_read)(int, void *, size_t);

/* Per-FD bookkeeping (FDs >= MAX_SHIMMED_FDS are passed through unshimmed). */
static int is_shimmed[MAX_SHIMMED_FDS];
static int peer_of[MAX_SHIMMED_FDS];
static int wake_r[MAX_SHIMMED_FDS];            /* accept() blocks reading this */
static int wake_w[MAX_SHIMMED_FDS];            /* close()  writes to this      */
static int listener_fd = -1;        /* FD that received listen()    */

__attribute__((constructor))
static void init(void) {{
    real_socket     = dlsym(RTLD_NEXT, "socket");
    real_socketpair = dlsym(RTLD_NEXT, "socketpair");
    real_listen     = dlsym(RTLD_NEXT, "listen");
    real_accept     = dlsym(RTLD_NEXT, "accept");
    real_close      = dlsym(RTLD_NEXT, "close");
    real_read       = dlsym(RTLD_NEXT, "read");
    for (int i = 0; i < MAX_SHIMMED_FDS; i++) {{
        peer_of[i] = -1;
        wake_r[i]  = -1;
        wake_w[i]  = -1;
    }}
}}

/* ---- socket ---------------------------------------------------------- */
int socket(int domain, int type, int protocol) {{
    if (domain == AF_UNIX) {{
        int fd = real_socket(domain, type, protocol);
        if (fd >= 0) return fd;
        /* socket(AF_UNIX) blocked – fall back to socketpair(). */
        int sv[2];
        if (real_socketpair(domain, type, protocol, sv) == 0) {{
            if (sv[0] >= 0 && sv[0] < MAX_SHIMMED_FDS) {{
                is_shimmed[sv[0]] = 1;
                peer_of[sv[0]]    = sv[1];
                int wp[2];
                if (pipe(wp) == 0) {{
                    wake_r[sv[0]] = wp[0];
                    wake_w[sv[0]] = wp[1];
                }}
            }}
            return sv[0];
        }}
        errno = EPERM;
        return -1;
    }}
    return real_socket(domain, type, protocol);
}}

/* ---- listen ---------------------------------------------------------- */
int listen(int sockfd, int backlog) {{
    if (sockfd >= 0 && sockfd < MAX_SHIMMED_FDS && is_shimmed[sockfd]) {{
        listener_fd = sockfd;
        return 0;
    }}
    return real_listen(sockfd, backlog);
}}

/* ---- accept ---------------------------------------------------------- */
int accept(int sockfd, struct sockaddr *addr, socklen_t *addrlen) {{
    if (sockfd >= 0 && sockfd < MAX_SHIMMED_FDS && is_shimmed[sockfd]) {{
        /* Block until close() writes to the wake pipe. */
        if (wake_r[sockfd] >= 0) {{
            char buf;
            real_read(wake_r[sockfd], &buf, 1);
        }}
        errno = ECONNABORTED;
        return -1;
    }}
    return real_accept(sockfd, addr, addrlen);
}}

/* ---- close ----------------------------------------------------------- */
int close(int fd) {{
    if (fd >= 0 && fd < MAX_SHIMMED_FDS && is_shimmed[fd]) {{
        int was_listener = (fd == listener_fd);
        is_shimmed[fd] = 0;

        if (wake_w[fd] >= 0) {{              /* unblock accept() */
            char c = 0;
            write(wake_w[fd], &c, 1);
            real_close(wake_w[fd]);
            wake_w[fd] = -1;
        }}
        if (wake_r[fd] >= 0) {{ real_close(wake_r[fd]); wake_r[fd]  = -1; }}
        if (peer_of[fd] >= 0) {{ real_close(peer_of[fd]); peer_of[fd] = -1; }}

        if (was_listener)
            _exit(0);                        /* conversion done – exit */
    }}
    return real_close(fd);
}}
"""



if __name__ == "__main__":
    import sys
    result = run_soffice(sys.argv[1:])
    sys.exit(result.returncode)
