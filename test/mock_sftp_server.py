#!/usr/bin/env python3
"""Scriptable SFTP v3 server for sshfs tests.

sshfs runs this in place of ssh (-o ssh_command=<python> <this file>) and
talks SFTP over stdin/stdout; the ssh arguments it appends are ignored. It
serves the local filesystem below SSHFS_MOCK_ROOT like OpenSSH's sftp-server
(paths are checked lexically, a symlink out of the root is not caught) and
injects behaviour OpenSSH never shows, as a JSON object in SSHFS_MOCK_CONFIG.
All keys are optional:

  read_max        cap on the payload of every DATA reply (short reads)
  read_error      {"name": <basename>, "offset": N, and "status": <code> or
                  "reply": "long" | "wrong_type"}: READs of that file from
                  offset N get that STATUS, a DATA reply longer than asked,
                  or a HANDLE reply
  write_reply     "wrong_type" | "truncated": do the WRITE, then answer with
                  an ATTRS reply, or a STATUS without its status code
  fail            {"<request>": <status code>}: answer every such request
                  with that STATUS, e.g. {"mkdir": 4}
  readdir_batch   names per NAME reply (default 100)
  extensions      extensions advertised in VERSION (default: OpenSSH's)
  attr_extended   [[type, data], ...] extended attrs added to every ATTRS
  attr_extended_bad
                  {"name": <basename>, "kind": "count" | "length"}: ATTRS for
                  that file announce more pairs than follow, or a string
                  longer than the packet
  drop_on         exit without a reply when a STAT/LSTAT names this basename,
                  on the first connection only (needs SSHFS_MOCK_STATE)
  reorder         send each burst of replies newest first
  stats           file for a JSON object of counters (the last connection to
                  count something wins): reordered_read and reordered_write
                  (replies sent ahead of an older one), short_replies,
                  injected (replies altered by read_error, write_reply or
                  fail), setstat, ext_<name> (EXTENDED requests),
                  scripted_disconnect (drop_on fired) and unknown_handle

It writes to stderr only if the client breaks the protocol (bad packet
length, no INIT first), and exits with status 1. The tests reject words like
"error" on stderr, so that and any Python traceback show up.
"""

import errno
import json
import os
import select
import struct
import sys

SSH_FXP_INIT = 1
SSH_FXP_VERSION = 2
SSH_FXP_OPEN = 3
SSH_FXP_CLOSE = 4
SSH_FXP_READ = 5
SSH_FXP_WRITE = 6
SSH_FXP_LSTAT = 7
SSH_FXP_FSTAT = 8
SSH_FXP_SETSTAT = 9
SSH_FXP_FSETSTAT = 10
SSH_FXP_OPENDIR = 11
SSH_FXP_READDIR = 12
SSH_FXP_REMOVE = 13
SSH_FXP_MKDIR = 14
SSH_FXP_RMDIR = 15
SSH_FXP_REALPATH = 16
SSH_FXP_STAT = 17
SSH_FXP_RENAME = 18
SSH_FXP_READLINK = 19
SSH_FXP_SYMLINK = 20
SSH_FXP_STATUS = 101
SSH_FXP_HANDLE = 102
SSH_FXP_DATA = 103
SSH_FXP_NAME = 104
SSH_FXP_ATTRS = 105
SSH_FXP_EXTENDED = 200
SSH_FXP_EXTENDED_REPLY = 201

ATTR_SIZE = 0x1
ATTR_UIDGID = 0x2
ATTR_PERMISSIONS = 0x4
ATTR_ACMODTIME = 0x8
ATTR_EXTENDED = 0x80000000

FX_OK = 0
FX_EOF = 1
FX_NO_SUCH_FILE = 2
FX_PERMISSION_DENIED = 3
FX_FAILURE = 4
FX_BAD_MESSAGE = 5
FX_OP_UNSUPPORTED = 8

FXF_READ = 0x1
FXF_WRITE = 0x2
FXF_APPEND = 0x4
FXF_CREAT = 0x8
FXF_TRUNC = 0x10
FXF_EXCL = 0x20

DEFAULT_EXTENSIONS = {
    "posix-rename@openssh.com": "1",
    "statvfs@openssh.com": "2",
    "hardlink@openssh.com": "1",
    "fsync@openssh.com": "1",
}

REQUEST_NAMES = {
    SSH_FXP_OPEN: "open", SSH_FXP_CLOSE: "close", SSH_FXP_READ: "read",
    SSH_FXP_WRITE: "write", SSH_FXP_LSTAT: "lstat", SSH_FXP_FSTAT: "fstat",
    SSH_FXP_SETSTAT: "setstat", SSH_FXP_FSETSTAT: "fsetstat",
    SSH_FXP_OPENDIR: "opendir", SSH_FXP_READDIR: "readdir",
    SSH_FXP_REMOVE: "remove", SSH_FXP_MKDIR: "mkdir", SSH_FXP_RMDIR: "rmdir",
    SSH_FXP_REALPATH: "realpath", SSH_FXP_STAT: "stat",
    SSH_FXP_RENAME: "rename", SSH_FXP_READLINK: "readlink",
    SSH_FXP_SYMLINK: "symlink", SSH_FXP_EXTENDED: "extended",
}

# Same mapping as OpenSSH's errno_to_portable(); everything else is FAILURE.
ERRNO_TO_FX = {
    errno.ENOENT: FX_NO_SUCH_FILE,
    errno.ENOTDIR: FX_NO_SUCH_FILE,
    errno.ELOOP: FX_NO_SUCH_FILE,
    errno.EACCES: FX_PERMISSION_DENIED,
    errno.EPERM: FX_PERMISSION_DENIED,
    errno.EINVAL: FX_BAD_MESSAGE,
    errno.ENAMETOOLONG: FX_BAD_MESSAGE,
    errno.ENOSYS: FX_OP_UNSUPPORTED,
}

# Replies are held while requests keep arriving, then sent newest first once
# the client has been quiet for this long or this many are waiting.
REORDER_IDLE = 0.02
REORDER_MAX = 8

MAX_PACKET = 1 << 20


class ScriptedDisconnect(Exception):
    """The test asked for the connection to be dropped."""


class ProtocolError(Exception):
    """The client sent something that is not SFTP v3."""


class Reader:
    def __init__(self, data):
        self.data = data
        self.pos = 0

    def take(self, n):
        if self.pos + n > len(self.data):
            raise ValueError("short packet")
        chunk = self.data[self.pos:self.pos + n]
        self.pos += n
        return chunk

    def u32(self):
        return struct.unpack(">I", self.take(4))[0]

    def u64(self):
        return struct.unpack(">Q", self.take(8))[0]

    def string(self):
        return self.take(self.u32())

    def attrs(self):
        flags = self.u32()
        a = {}
        if flags & ATTR_SIZE:
            a["size"] = self.u64()
        if flags & ATTR_UIDGID:
            a["uid"], a["gid"] = self.u32(), self.u32()
        if flags & ATTR_PERMISSIONS:
            a["mode"] = self.u32()
        if flags & ATTR_ACMODTIME:
            a["atime"], a["mtime"] = self.u32(), self.u32()
        if flags & ATTR_EXTENDED:
            for _ in range(self.u32()):
                self.string()
                self.string()
        return a


def p_u32(v):
    return struct.pack(">I", v)


def p_u64(v):
    return struct.pack(">Q", v)


def p_str(b):
    if isinstance(b, str):
        b = b.encode()
    return p_u32(len(b)) + b


def chown_id(v):
    # (uid_t)-1 means "leave alone"; Python spells that -1
    return -1 if v == 0xFFFFFFFF else v


class Server:
    def __init__(self, cfg, root, rfd, wfd):
        self.cfg = cfg
        self.roots = {os.path.abspath(root), os.path.realpath(root)}
        self.root = os.path.abspath(root)
        self.rfd = rfd
        self.wfd = wfd
        self.handles = {}
        self.next_handle = 0
        self.held = []
        self.request_type = None
        self.stats = {}
        self.drop_on = None
        if "drop_on" in cfg and self.first_connection():
            self.drop_on = cfg["drop_on"]

    def first_connection(self):
        # Count connections in a state file so that a reconnecting sshfs
        # gets a well-behaved server the second time round.
        state = os.environ.get("SSHFS_MOCK_STATE")
        if not state:
            return True
        n = 0
        try:
            with open(state) as fh:
                n = int(fh.read() or 0)
        except FileNotFoundError:
            pass
        with open(state, "w") as fh:
            fh.write(str(n + 1))
        return n == 0

    # -- I/O ------------------------------------------------------------

    def read_exact(self, n, eof_ok=False):
        buf = b""
        while len(buf) < n:
            chunk = os.read(self.rfd, n - len(buf))
            if not chunk:
                if eof_ok and not buf:
                    raise EOFError
                raise ProtocolError("truncated packet")
            buf += chunk
        return buf

    def read_packet(self):
        (length,) = struct.unpack(">I", self.read_exact(4, eof_ok=True))
        if length < 1 or length > MAX_PACKET:
            raise ProtocolError(f"bad packet length {length}")
        body = self.read_exact(length)
        return body[0], Reader(body[1:])

    def write_all(self, data):
        while data:
            n = os.write(self.wfd, data)
            data = data[n:]

    def send(self, ptype, payload):
        pkt = p_u32(len(payload) + 1) + bytes([ptype]) + payload
        if self.cfg.get("reorder"):
            self.held.append((self.request_type, pkt))
        else:
            self.write_all(pkt)

    def flush_held(self):
        # Newest first: SFTP allows any order, sshfs matches replies by id
        for name, op in (("reordered_read", SSH_FXP_READ), ("reordered_write", SSH_FXP_WRITE)):
            n = sum(req == op for req, _ in self.held)
            if n > 1:
                self.count(name, n - 1)
        while self.held:
            self.write_all(self.held.pop()[1])

    def status(self, rid, code, msg=""):
        self.send(SSH_FXP_STATUS, p_u32(rid) + p_u32(code) + p_str(msg) + p_str(""))

    def count(self, key, n=1):
        self.stats[key] = self.stats.get(key, 0) + n
        path = self.cfg.get("stats")
        if path:
            # Replace atomically: the test reads this while we keep running
            with open(path + ".tmp", "w") as fh:
                json.dump(self.stats, fh)
            os.replace(path + ".tmp", path)

    # -- helpers --------------------------------------------------------

    def inside(self, path):
        return any(path == r or path.startswith(r.rstrip(os.sep) + os.sep)
                   for r in self.roots)

    def resolve(self, raw):
        """Map a client path onto the served tree, refusing escapes."""
        path = raw.decode("utf-8", "surrogateescape")
        if not path.startswith("/"):
            path = os.path.join(self.root, path)
        path = os.path.normpath(path)
        if not self.inside(path):
            raise PermissionError(errno.EACCES, "outside root")
        return path

    def canonical(self, raw):
        """REALPATH like OpenSSH's (BSD) realpath(3): symlinks are resolved,
        and only the last component may be missing."""
        real = os.path.realpath(self.resolve(raw))
        if not os.path.lexists(real) and not os.path.isdir(os.path.dirname(real)):
            raise FileNotFoundError(errno.ENOENT, "No such file or directory")
        if not self.inside(real):
            raise PermissionError(errno.EACCES, "outside root")
        return real

    def encode_attrs(self, st, name=None):
        flags = ATTR_SIZE | ATTR_UIDGID | ATTR_PERMISSIONS | ATTR_ACMODTIME
        ext = self.cfg.get("attr_extended") or []
        if ext:
            flags |= ATTR_EXTENDED
        out = p_u32(flags)
        out += p_u64(st.st_size)
        out += p_u32(st.st_uid) + p_u32(st.st_gid)
        out += p_u32(st.st_mode)
        out += p_u32(int(st.st_atime)) + p_u32(int(st.st_mtime))
        if ext:
            bad = self.cfg.get("attr_extended_bad") or {}
            kind = bad.get("kind") if bad.get("name") == name else None
            extra = {"count": 2, "length": 1}.get(kind, 0)
            out += p_u32(len(ext) + extra)
            for k, v in ext:
                out += p_str(k) + p_str(v)
            if kind == "length":
                out += p_u32(1 << 20) + b"x"
        return out

    def apply_attrs(self, path, fd, a):
        if "size" in a:
            if fd is not None:
                os.ftruncate(fd, a["size"])
            else:
                os.truncate(path, a["size"])
        if "mode" in a:
            os.chmod(fd if fd is not None else path, a["mode"] & 0o7777)
        if "uid" in a:
            uid, gid = chown_id(a["uid"]), chown_id(a["gid"])
            if fd is not None:
                os.fchown(fd, uid, gid)
            else:
                # Follows symlinks, like OpenSSH's SETSTAT (upstream issue 105).
                os.chown(path, uid, gid)
        if "atime" in a:
            os.utime(fd if fd is not None else path, (a["atime"], a["mtime"]))

    def new_handle(self, obj):
        self.next_handle += 1
        h = b"h%d" % self.next_handle
        self.handles[h] = obj
        return h

    def handle(self, raw, kind):
        obj = self.handles.get(raw)
        if obj is None or obj[0] != kind:
            self.count("unknown_handle")
            raise KeyError(raw)
        return obj[1], obj[2]

    def file_handle(self, raw):
        return self.handle(raw, "file")

    def dir_handle(self, raw):
        return self.handle(raw, "dir")

    def name_entry(self, name, st):
        return p_str(name) + p_str(name) + self.encode_attrs(st, name)

    # -- request dispatch -----------------------------------------------

    def serve(self):
        ptype, r = self.read_packet()
        if ptype != SSH_FXP_INIT:
            raise ProtocolError(f"first packet is type {ptype}, not INIT")
        exts = self.cfg.get("extensions", DEFAULT_EXTENSIONS)
        payload = p_u32(3)
        for k, v in exts.items():
            payload += p_str(k) + p_str(v)
        self.write_all(p_u32(len(payload) + 1) + bytes([SSH_FXP_VERSION]) + payload)

        while True:
            if self.held:
                ready, _, _ = select.select([self.rfd], [], [], REORDER_IDLE)
                if not ready or len(self.held) >= REORDER_MAX:
                    self.flush_held()
                    continue
            ptype, r = self.read_packet()
            self.request_type = ptype
            rid = r.u32()
            try:
                self.dispatch(ptype, rid, r)
            except OSError as e:
                self.status(rid, ERRNO_TO_FX.get(e.errno, FX_FAILURE), e.strerror or "")
            except KeyError:
                self.status(rid, FX_FAILURE, "bad handle")
            except ValueError:
                self.status(rid, FX_BAD_MESSAGE, "bad message")

    def dispatch(self, ptype, rid, r):
        injected = self.cfg.get("fail", {}).get(REQUEST_NAMES.get(ptype))
        if injected is not None:
            self.count("injected")
            self.status(rid, injected, "injected")
            return

        if ptype in (SSH_FXP_LSTAT, SSH_FXP_STAT):
            path = self.resolve(r.string())
            if self.drop_on and os.path.basename(path) == self.drop_on:
                self.count("scripted_disconnect")
                raise ScriptedDisconnect
            st = os.lstat(path) if ptype == SSH_FXP_LSTAT else os.stat(path)
            self.send(SSH_FXP_ATTRS, p_u32(rid) +
                      self.encode_attrs(st, os.path.basename(path)))

        elif ptype == SSH_FXP_FSTAT:
            fd, _ = self.file_handle(r.string())
            self.send(SSH_FXP_ATTRS, p_u32(rid) + self.encode_attrs(os.fstat(fd)))

        elif ptype == SSH_FXP_OPEN:
            path = self.resolve(r.string())
            pflags = r.u32()
            a = r.attrs()
            flags = {FXF_READ: os.O_RDONLY, FXF_WRITE: os.O_WRONLY,
                     FXF_READ | FXF_WRITE: os.O_RDWR}.get(pflags & 3, os.O_RDONLY)
            if pflags & FXF_APPEND:
                flags |= os.O_APPEND
            if pflags & FXF_CREAT:
                flags |= os.O_CREAT
            if pflags & FXF_TRUNC:
                flags |= os.O_TRUNC
            if pflags & FXF_EXCL:
                flags |= os.O_EXCL
            fd = os.open(path, flags, a.get("mode", 0o666) & 0o7777)
            h = self.new_handle(("file", fd, os.path.basename(path)))
            self.send(SSH_FXP_HANDLE, p_u32(rid) + p_str(h))

        elif ptype == SSH_FXP_READ:
            fd, name = self.file_handle(r.string())
            offset, length = r.u64(), r.u32()
            err = self.cfg.get("read_error")
            if err and err["name"] == name and offset >= err.get("offset", 0):
                self.count("injected")
                if "status" in err:
                    self.status(rid, err["status"], "injected")
                elif err.get("reply") == "long":
                    self.send(SSH_FXP_DATA, p_u32(rid) + p_str(b"x" * (length + 1)))
                else:
                    self.send(SSH_FXP_HANDLE, p_u32(rid) + p_str(b"h0"))
                return
            cap = self.cfg.get("read_max")
            data = os.pread(fd, min(length, cap or length), offset)
            if not data:
                self.status(rid, FX_EOF, "EOF")
                return
            if cap and len(data) == cap < length:
                self.count("short_replies")
            self.send(SSH_FXP_DATA, p_u32(rid) + p_str(data))

        elif ptype == SSH_FXP_WRITE:
            fd, _ = self.file_handle(r.string())
            offset = r.u64()
            data = memoryview(r.string())
            done = 0
            while done < len(data):
                n = os.pwrite(fd, data[done:], offset + done)
                if n <= 0:
                    raise OSError(errno.EIO, "short write")
                done += n
            bad = self.cfg.get("write_reply")
            if bad == "wrong_type":
                self.count("injected")
                self.send(SSH_FXP_ATTRS, p_u32(rid) + self.encode_attrs(os.fstat(fd)))
            elif bad == "truncated":
                self.count("injected")
                self.send(SSH_FXP_STATUS, p_u32(rid))
            else:
                self.status(rid, FX_OK)

        elif ptype == SSH_FXP_CLOSE:
            obj = self.handles.pop(r.string(), None)
            if obj is None:
                # OpenSSH: close(2) of a bad descriptor, EBADF
                self.count("unknown_handle")
                self.status(rid, FX_NO_SUCH_FILE, "bad handle")
                return
            if obj[0] == "file":
                os.close(obj[1])
            self.status(rid, FX_OK)

        elif ptype == SSH_FXP_OPENDIR:
            path = self.resolve(r.string())
            entries = [".", ".."] + sorted(os.listdir(path))
            h = self.new_handle(("dir", path, entries))
            self.send(SSH_FXP_HANDLE, p_u32(rid) + p_str(h))

        elif ptype == SSH_FXP_READDIR:
            path, entries = self.dir_handle(r.string())
            batch = self.cfg.get("readdir_batch", 100)
            out = b""
            count = 0
            while entries and count < batch:
                name = entries.pop(0)
                try:
                    st = os.lstat(os.path.join(path, name))
                except FileNotFoundError:
                    continue  # removed since OPENDIR
                out += self.name_entry(name, st)
                count += 1
            if count == 0:
                self.status(rid, FX_EOF, "EOF")
            else:
                self.send(SSH_FXP_NAME, p_u32(rid) + p_u32(count) + out)

        elif ptype in (SSH_FXP_SETSTAT, SSH_FXP_FSETSTAT):
            if ptype == SSH_FXP_SETSTAT:
                path, fd = self.resolve(r.string()), None
            else:
                path, fd = None, self.file_handle(r.string())[0]
            self.count("setstat")
            self.apply_attrs(path, fd, r.attrs())
            self.status(rid, FX_OK)

        elif ptype == SSH_FXP_REMOVE:
            os.unlink(self.resolve(r.string()))
            self.status(rid, FX_OK)

        elif ptype == SSH_FXP_MKDIR:
            path = self.resolve(r.string())
            os.mkdir(path, r.attrs().get("mode", 0o777) & 0o7777)
            self.status(rid, FX_OK)

        elif ptype == SSH_FXP_RMDIR:
            os.rmdir(self.resolve(r.string()))
            self.status(rid, FX_OK)

        elif ptype == SSH_FXP_REALPATH:
            path = self.canonical(r.string())
            # OpenSSH sends the name twice and no attributes
            self.send(SSH_FXP_NAME, p_u32(rid) + p_u32(1) +
                      p_str(path) + p_str(path) + p_u32(0))

        elif ptype == SSH_FXP_RENAME:
            old, new = self.resolve(r.string()), self.resolve(r.string())
            if os.path.lexists(new):
                # Plain SFTP v3 rename refuses to overwrite, like OpenSSH.
                self.status(rid, FX_FAILURE, "target exists")
                return
            os.rename(old, new)
            self.status(rid, FX_OK)

        elif ptype == SSH_FXP_READLINK:
            path = self.resolve(r.string())
            target = os.readlink(path)
            self.send(SSH_FXP_NAME, p_u32(rid) + p_u32(1) +
                      self.name_entry(target, os.lstat(path)))

        elif ptype == SSH_FXP_SYMLINK:
            # OpenSSH swaps the arguments relative to the draft: the first
            # string is the link target, the second the new link. sshfs
            # follows OpenSSH.
            target = r.string().decode("utf-8", "surrogateescape")
            link = self.resolve(r.string())
            os.symlink(target, link)
            self.status(rid, FX_OK)

        elif ptype == SSH_FXP_EXTENDED:
            self.extended(rid, r.string().decode(errors="replace"), r)

        else:
            self.status(rid, FX_OP_UNSUPPORTED, "unsupported")

    def extended(self, rid, name, r):
        exts = self.cfg.get("extensions", DEFAULT_EXTENSIONS)
        if name in exts:
            self.count("ext_" + name)
        if name not in exts:
            self.status(rid, FX_OP_UNSUPPORTED, "unsupported")
        elif name == "posix-rename@openssh.com":
            os.rename(self.resolve(r.string()), self.resolve(r.string()))
            self.status(rid, FX_OK)
        elif name == "hardlink@openssh.com":
            os.link(self.resolve(r.string()), self.resolve(r.string()))
            self.status(rid, FX_OK)
        elif name == "fsync@openssh.com":
            os.fsync(self.file_handle(r.string())[0])
            self.status(rid, FX_OK)
        elif name == "statvfs@openssh.com":
            v = os.statvfs(self.resolve(r.string()))
            vals = (v.f_bsize, v.f_frsize, v.f_blocks, v.f_bfree, v.f_bavail,
                    v.f_files, v.f_ffree, v.f_favail, 0, 0, v.f_namemax)
            self.send(SSH_FXP_EXTENDED_REPLY,
                      p_u32(rid) + b"".join(p_u64(x) for x in vals))
        else:
            self.status(rid, FX_OP_UNSUPPORTED, "unsupported")


def main():
    cfg = json.loads(os.environ.get("SSHFS_MOCK_CONFIG") or "{}")
    root = os.environ.get("SSHFS_MOCK_ROOT", "/")
    server = Server(cfg, root, sys.stdin.fileno(), sys.stdout.fileno())
    status = 0
    try:
        server.serve()
    except (EOFError, ScriptedDisconnect, BrokenPipeError, ConnectionResetError):
        pass
    except ProtocolError as exc:
        sys.stderr.write(f"mock sftp server: protocol error: {exc}\n")
        status = 1
    finally:
        try:
            server.flush_held()
        except OSError:
            pass
    sys.exit(status)


if __name__ == "__main__":
    main()
