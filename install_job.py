"""Create or update the MCPServer job in a NIS-Elements JOBS database.

    python install_job.py --db D:\\Embryos\\Embryos.dat

The job's Python task is a short loader that runs NISMCPServer.py from this
folder, so after this the server is upgraded by editing that file.

How: NIS keeps jobs in SQLite. A job is a jobdef row plus jobprogram rows
(one per save; the newest is used). A jobprogram's wszJsonTaskList is a
JSON list of tasks; a task's "data" is base64 of NIS's binary record format
(decoded by parse/serialize below). The new job is a copy of an existing
one-Python-task job with the script and names replaced; an existing
MCPServer job gets a new jobprogram row, as when NIS saves. The database is
backed up first. The format is undocumented: inferred from this PC's
NIS 6.20 database, and every task blob in it round-trips exactly.
"""
import argparse
import base64
import json
import sqlite3
import struct
import time
import uuid
from pathlib import Path

LOADER = '''# IMPORTANT: 'limjob' must be imported like this (not from nor as)
try:
    import limjob
except ImportError:
    limjob = None

# Written by install_job.py on {date}. This job only loads and runs the
# MCP server file below: to upgrade the server, edit that file and run
# this job again. To stop it, POST http://127.0.0.1:8767/shutdown.
# Never press JOBS Abort.
import importlib.util

SERVER_FILE = r"{server}"


def run(imgs: "tuple[limjob.Image]", Job: "limjob.JobParam", macro: "limjob.MacroParam",
        ctx: "limjob.RunContext"):
    spec = importlib.util.spec_from_file_location("NISMCPServer", SERVER_FILE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)      # fresh code on every run
    module.run(imgs, Job, macro, ctx)
'''

# --- NIS record format -------------------------------------------------
# Record: type u8, name length u8 (UTF-16 chars incl. NUL), name, value.
# Type 11 is a level: count u32, length u64 (record start to end of
# children), children, then a table of count u64 child offsets from the
# record start, not always in child order (so the order is kept).
FIXED = {1: "<B", 2: "<i", 3: "<I", 4: "<q", 5: "<Q", 6: "<d", 7: "<Q"}


def parse(buf: bytes, pos: int = 0, end: "int | None" = None, count: "int | None" = None):
    items, end = [], len(buf) if end is None else end
    while pos < end and (count is None or len(items) < count):
        start, typ, nlen = pos, buf[pos], buf[pos + 1]
        name = buf[pos + 2:pos + 2 + 2 * nlen].decode("utf-16-le")
        pos += 2 + 2 * nlen
        if typ in FIXED:
            val = struct.unpack_from(FIXED[typ], buf, pos)[0]
            pos += struct.calcsize(FIXED[typ])
        elif typ == 8:                                  # NUL-terminated UTF-16
            j = pos
            while buf[j:j + 2] != b"\0\0":
                j += 2
            val, pos = buf[pos:j].decode("utf-16-le"), j + 2
        elif typ == 9:                                  # byte array
            n = struct.unpack_from("<Q", buf, pos)[0]
            val, pos = bytes(buf[pos + 8:pos + 8 + n]), pos + 8 + n
        elif typ == 11:
            n, length = struct.unpack_from("<IQ", buf, pos)
            pos += 12
            children, starts = [], []
            for _ in range(n):
                starts.append(pos - start)
                child, pos = parse(buf, pos, start + length, 1)
                children += child
            offsets = struct.unpack_from(f"<{n}Q", buf, pos)
            pos += 8 * n
            val = (children, [starts.index(o) for o in offsets])
        else:
            raise ValueError(f"unknown record type {typ} at byte {start}")
        items.append((typ, name, val))
    return items, pos


def serialize(items) -> bytes:
    out = bytearray()
    for typ, name, val in items:
        out += bytes([typ, len(name)]) + name.encode("utf-16-le")
        if typ in FIXED:
            out += struct.pack(FIXED[typ], val)
        elif typ == 8:
            out += val.encode("utf-16-le") + b"\0\0"
        elif typ == 9:
            out += struct.pack("<Q", len(val)) + val
        elif typ == 11:
            children, order = val
            header = 2 + 2 * len(name) + 12
            body, offsets = bytearray(), []
            for child in children:
                offsets.append(header + len(body))
                body += serialize([child])
            out += struct.pack("<IQ", len(children), header + len(body)) + body
            out += struct.pack(f"<{len(children)}Q", *(offsets[i] for i in order))
    return bytes(out)


def set_script(data_b64: str, script: str) -> str:
    """Replace PythonScript/Script in a task's data."""
    items, _ = parse(base64.b64decode(data_b64))
    typ, name, (children, order) = items[0]
    assert name.rstrip("\0") == "PythonScript", name
    names = [n.rstrip("\0") for _, n, _ in children]
    i = names.index("Script")
    children[i] = (8, children[i][1], script)
    return base64.b64encode(serialize(items)).decode("ascii")


def get_script(data_b64: str) -> str:
    items, _ = parse(base64.b64decode(data_b64))
    return next(v for t, n, v in items[0][2][0] if n.rstrip("\0") == "Script")


# --- the database ------------------------------------------------------
def julian_now() -> float:
    return time.time() / 86400.0 + 2440587.5            # NIS stores times as Julian days


def latest_program(db, jobdef: int):
    return db.execute("SELECT iKey, wszJsonTaskList FROM jobprogram WHERE iJobdefKeyRef=?"
                      " ORDER BY dMTime DESC, iKey DESC LIMIT 1", (jobdef,)).fetchone()


def python_jobs(db):
    """(key, name) of jobs whose newest program is exactly one Python task, newest first."""
    for key, name in db.execute("SELECT iKey, wszName FROM jobdef ORDER BY dMTime DESC").fetchall():
        prog = latest_program(db, key)
        tasks = json.loads(prog[1] or "[]") if prog else []
        if len(tasks) == 1 and tasks[0].get("class") == "PythonScript":
            yield key, name


def install(db_path: Path, name: str, server: Path, backup: bool = True) -> str:
    db = sqlite3.connect(db_path, timeout=10)
    try:
        jobs = list(python_jobs(db))
        existing = [k for k, n in jobs if n == name]
        if not jobs:
            raise SystemExit("no job with a single Python task to copy: create one in NIS JOBS first")
        template = existing[0] if existing else jobs[0][0]
        tasks = json.loads(latest_program(db, template)[1])
        script = LOADER.format(date=time.strftime("%Y-%m-%d %H:%M"), server=server.resolve())
        tasks[0]["data"] = set_script(tasks[0]["data"], script)
        tasks[0]["name"] = name
        task_list = json.dumps(tasks, separators=(",", ":"), ensure_ascii=False)

        if backup:
            bak = db_path.with_name(f"{db_path.stem}.before-{name}-{time.strftime('%Y%m%d-%H%M%S')}{db_path.suffix}")
            with sqlite3.connect(bak) as dst:
                db.backup(dst)
            print(f"backup: {bak}")

        now = julian_now()
        with db:                                        # one transaction
            if existing:
                jobdef = existing[0]
                db.execute("UPDATE jobdef SET dMTime=? WHERE iKey=?", (now, jobdef))
                action = "updated"
            else:
                cols = [r[1] for r in db.execute("PRAGMA table_info(jobdef)") if r[1] != "iKey"]
                row = dict(zip(cols, db.execute(f"SELECT {', '.join(cols)} FROM jobdef WHERE iKey=?",
                                                (template,)).fetchone()))
                row.update(wszName=name, wszDesc="MCP server (install_job.py)", wszUuid=uuid.uuid4().hex,
                           dCTime=now, dMTime=now)
                jobdef = db.execute(f"INSERT INTO jobdef ({', '.join(row)}) VALUES ({', '.join('?' * len(row))})",
                                    list(row.values())).lastrowid
                action = "created"
            db.execute("INSERT INTO jobprogram (iJobdefKeyRef, wszSummary, pProperties, wszUuid, dMTime,"
                       " wszJsonTaskList) VALUES (?, '', NULL, ?, ?, ?)",
                       (jobdef, uuid.uuid4().hex, now, task_list))
        return f"{action} job {name!r} (jobdef {jobdef}), loading {server.resolve()}"
    finally:
        db.close()


def main(argv=None) -> None:
    here = Path(__file__).resolve().parent
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--db", type=Path, required=True,
                    help="the JOBS database NIS uses, e.g. D:\\Embryos\\Embryos.dat")
    ap.add_argument("--name", default="MCPServer")
    ap.add_argument("--server", type=Path, default=here / "NISMCPServer.py")
    ap.add_argument("--no-backup", action="store_true")
    a = ap.parse_args(argv)
    if not a.server.is_file():
        raise SystemExit(f"no such file: {a.server}")
    print(install(a.db, a.name, a.server, backup=not a.no_backup))
    print("In NIS: reopen JOBS (or restart NIS) to see the job, then run it.")


if __name__ == "__main__":
    main()
