"""Small Windows ownership primitives; no service registration or elevation."""

from __future__ import annotations

import ctypes
import hashlib
import os
import subprocess
from pathlib import Path


def allow_current_user(directory: Path) -> None:
    """Let the restricted database token use a newly created private directory."""
    if os.name != "nt":
        return
    import win32api
    import win32con
    import win32security

    with win32security.OpenProcessToken(
        win32api.GetCurrentProcess(), win32con.TOKEN_QUERY
    ) as token:
        user = win32security.GetTokenInformation(token, win32security.TokenUser)[0]
    descriptor = win32security.GetNamedSecurityInfo(
        str(directory), win32security.SE_FILE_OBJECT, win32security.DACL_SECURITY_INFORMATION
    )
    acl = descriptor.GetSecurityDescriptorDacl()
    if acl is None:
        return
    for index in range(acl.GetAceCount()):
        (kind, _), mask, sid = acl.GetAce(index)
        if (
            kind == win32security.ACCESS_ALLOWED_ACE_TYPE
            and sid == user
            and mask & win32con.FILE_ALL_ACCESS == win32con.FILE_ALL_ACCESS
        ):
            return
    acl.AddAccessAllowedAceEx(
        win32security.ACL_REVISION,
        win32con.OBJECT_INHERIT_ACE | win32con.CONTAINER_INHERIT_ACE,
        win32con.FILE_ALL_ACCESS,
        user,
    )
    win32security.SetNamedSecurityInfo(
        str(directory),
        win32security.SE_FILE_OBJECT,
        win32security.DACL_SECURITY_INFORMATION,
        None,
        None,
        acl,
        None,
    )


class InstanceLock:
    def __init__(self, root: Path):
        self.root = root
        self.handle = None
        self.file = None

    def acquire(self) -> bool:
        if os.name == "nt":
            from ctypes import wintypes

            kernel = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
            kernel.CreateMutexW.restype = wintypes.HANDLE
            name = (
                "Local\\Wenyi-"
                + hashlib.sha256(str(self.root.resolve()).casefold().encode()).hexdigest()
            )
            self.handle = kernel.CreateMutexW(None, False, name)
            if not self.handle:
                raise ctypes.WinError(ctypes.get_last_error())
            return ctypes.get_last_error() != 183
        import fcntl

        self.file = (self.root / "instance.lock").open("a+b")
        try:
            fcntl.flock(self.file, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except BlockingIOError:
            return False

    def close(self) -> None:
        if self.file:
            self.file.close()
        if self.handle:
            from ctypes import wintypes

            kernel = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel.CloseHandle.argtypes = [wintypes.HANDLE]
            kernel.CloseHandle(self.handle)
            self.handle = None


class ProcessOwner:
    """Windows Job Object closes only this launcher's descendants on a crash."""

    def __init__(self):
        self.handle = None
        if os.name != "nt":
            return
        from ctypes import wintypes

        class BasicLimits(ctypes.Structure):
            _fields_ = [
                ("PerProcessUserTimeLimit", ctypes.c_longlong),
                ("PerJobUserTimeLimit", ctypes.c_longlong),
                ("LimitFlags", wintypes.DWORD),
                ("MinimumWorkingSetSize", ctypes.c_size_t),
                ("MaximumWorkingSetSize", ctypes.c_size_t),
                ("ActiveProcessLimit", wintypes.DWORD),
                ("Affinity", ctypes.c_size_t),
                ("PriorityClass", wintypes.DWORD),
                ("SchedulingClass", wintypes.DWORD),
            ]

        class IoCounters(ctypes.Structure):
            _fields_ = [
                (name, ctypes.c_ulonglong)
                for name in (
                    "ReadOperationCount",
                    "WriteOperationCount",
                    "OtherOperationCount",
                    "ReadTransferCount",
                    "WriteTransferCount",
                    "OtherTransferCount",
                )
            ]

        class ExtendedLimits(ctypes.Structure):
            _fields_ = [("BasicLimitInformation", BasicLimits), ("IoInfo", IoCounters)] + [
                (name, ctypes.c_size_t)
                for name in (
                    "ProcessMemoryLimit",
                    "JobMemoryLimit",
                    "PeakProcessMemoryUsed",
                    "PeakJobMemoryUsed",
                )
            ]

        self.kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        self.kernel.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
        self.kernel.CreateJobObjectW.restype = wintypes.HANDLE
        self.kernel.SetInformationJobObject.argtypes = [
            wintypes.HANDLE,
            ctypes.c_int,
            ctypes.c_void_p,
            wintypes.DWORD,
        ]
        self.kernel.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        self.kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        self.handle = self.kernel.CreateJobObjectW(None, None)
        limits = ExtendedLimits()
        limits.BasicLimitInformation.LimitFlags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not self.handle or not self.kernel.SetInformationJobObject(
            self.handle, 9, ctypes.byref(limits), ctypes.sizeof(limits)
        ):
            raise ctypes.WinError(ctypes.get_last_error())

    def add(self, process) -> None:
        if self.handle and not self.kernel.AssignProcessToJobObject(
            self.handle, int(process._handle)
        ):
            raise ctypes.WinError(ctypes.get_last_error())

    def spawn(self, args, *, output, env, cwd, restrict_admin=False):
        if os.name != "nt":
            return subprocess.Popen(
                args,
                cwd=cwd,
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=output,
                stderr=subprocess.STDOUT,
            )
        import msvcrt

        import win32api
        import win32con
        import win32process
        import win32security

        current = win32api.GetCurrentProcess()
        inherited = []
        token = restricted = None
        try:
            with open(os.devnull, "rb") as null:
                for source in (null, output):
                    inherited.append(
                        win32api.DuplicateHandle(
                            current,
                            msvcrt.get_osfhandle(source.fileno()),
                            current,
                            0,
                            True,
                            win32con.DUPLICATE_SAME_ACCESS,
                        )
                    )
            startup = win32process.STARTUPINFO()
            startup.dwFlags = win32con.STARTF_USESTDHANDLES
            startup.hStdInput, startup.hStdOutput = inherited
            startup.hStdError = inherited[1]
            flags = (
                win32con.CREATE_SUSPENDED
                | win32con.CREATE_NO_WINDOW
                | win32con.CREATE_UNICODE_ENVIRONMENT
            )
            parameters = (
                args[0],
                subprocess.list2cmdline(args),
                None,
                None,
                True,
                flags,
                env,
                str(cwd),
                startup,
            )
            if restrict_admin:
                # Match PostgreSQL's own frontend launch policy: retain this user,
                # but mark Administrators and Power Users SIDs as deny-only.
                token = win32security.OpenProcessToken(current, win32con.TOKEN_ALL_ACCESS)
                disabled = [
                    (win32security.CreateWellKnownSid(kind, None), 0)
                    for kind in (
                        win32security.WinBuiltinAdministratorsSid,
                        win32security.WinBuiltinPowerUsersSid,
                    )
                ]
                restricted = win32security.CreateRestrictedToken(token, 1, disabled, [], [])
                user = win32security.GetTokenInformation(token, win32security.TokenUser)[0]
                acl = win32security.GetTokenInformation(restricted, win32security.TokenDefaultDacl)
                if acl is not None:
                    acl.AddAccessAllowedAceEx(
                        win32security.ACL_REVISION,
                        win32con.OBJECT_INHERIT_ACE,
                        win32con.GENERIC_ALL,
                        user,
                    )
                    win32security.SetTokenInformation(
                        restricted, win32security.TokenDefaultDacl, acl
                    )
                handles = win32process.CreateProcessAsUser(restricted, *parameters)
            else:
                handles = win32process.CreateProcess(*parameters)
            process_handle, thread, pid, _ = handles
            process = WindowsProcess(args, process_handle, pid)
            try:
                # Own the process before it can create any descendants.
                self.add(process)
                win32process.ResumeThread(thread)
            except BaseException:
                process.terminate()
                process.wait(timeout=10)
                raise
            finally:
                thread.Close()
            return process
        finally:
            for handle in inherited:
                handle.Close()
            if restricted is not None:
                restricted.Close()
            if token is not None:
                token.Close()

    def close(self) -> None:
        if self.handle:
            self.kernel.CloseHandle(self.handle)
            self.handle = None


class WindowsProcess:
    """The small Popen interface used by the supervisor, backed by owned handles."""

    def __init__(self, args, handle, pid):
        self.args, self._handle, self.pid = args, handle, pid
        self.returncode = None

    def poll(self):
        import win32event
        import win32process

        if win32event.WaitForSingleObject(self._handle, 0) == win32event.WAIT_OBJECT_0:
            self.returncode = win32process.GetExitCodeProcess(self._handle)
        return self.returncode

    def wait(self, timeout=None):
        import win32event

        milliseconds = win32event.INFINITE if timeout is None else max(0, int(timeout * 1000))
        if win32event.WaitForSingleObject(self._handle, milliseconds) == win32event.WAIT_TIMEOUT:
            raise subprocess.TimeoutExpired(self.args, timeout)
        return self.poll()

    def terminate(self):
        import win32api

        if self.poll() is None:
            win32api.TerminateProcess(self._handle, 1)
