import datetime, http.client, json, socket, sqlite3, subprocess, time, uuid


class UnixHTTP(http.client.HTTPConnection):
    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(15)
        self.sock.connect("/run/eidolon/system.sock")


def api(method, path, body=None):
    c = UnixHTTP("localhost", timeout=15)
    c.request(
        method,
        path,
        body=json.dumps(body) if body else None,
        headers={"Content-Type": "application/json"},
    )
    r = c.getresponse()
    data = json.loads(r.read())
    c.close()
    return r.status, data


def process(unit="eidolon-livekit"):
    text = subprocess.check_output(
        [
            "systemctl",
            "show",
            unit,
            "-p",
            "MainPID",
            "-p",
            "InvocationID",
            "-p",
            "ActiveState",
            "-p",
            "SubState",
            "-p",
            "Job",
        ],
        text=True,
    )
    return dict(line.split("=", 1) for line in text.splitlines())


def states():
    code, data = api("GET", "/api/system/v1/services")
    assert code == 200
    return [
        {k: s.get(k) for k in ["service_id", "runtime_state", "detail", "network_current"]}
        for s in data["services"]
    ]


started = time.time()
before = process()
manager_before = process("eidolond")
code, state = api("GET", "/api/system/v1/services/livekit")
assert code == 200, state
request = {
    "operation": "system.service.restart",
    "request_id": "lifecycle-hil-" + uuid.uuid4().hex,
    "expected_revision": state["desired"]["revision"],
}
t = time.monotonic()
code, receipt = api("POST", "/api/system/v1/services/livekit/restart", request)
elapsed = time.monotonic() - t
assert code == 200, (code, receipt)
post_submit = process()
during = states()
code, replay = api("POST", "/api/system/v1/services/livekit/restart", request)
assert code == 200 and replay["replayed"]
# An actual slow host job must outlive a restarted eidolond, not just a fake.
before_manager_restart = process()
restarted_during_job = bool(before_manager_restart["Job"]) or before_manager_restart[
    "ActiveState"
] in ["activating", "deactivating"]
if restarted_during_job:
    subprocess.run(["systemctl", "restart", "eidolond"], check=True, timeout=35)
samples = []
for i in range(46):
    try:
        snapshot = {
            "elapsed_s": round(time.time() - started, 2),
            "process": process(),
            "services": states(),
        }
    except (OSError, http.client.HTTPException, ValueError) as exc:
        snapshot = {
            "elapsed_s": round(time.time() - started, 2),
            "temporary_manager_unavailable": type(exc).__name__,
        }
    samples.append(snapshot)
    if time.time() - started >= 90:
        break
    time.sleep(2)
code, replay_after = api("POST", "/api/system/v1/services/livekit/restart", request)
assert code == 200 and replay_after["replayed"]
c = sqlite3.connect("file:/var/lib/eidolon/eidolond.sqlite3?mode=ro", uri=True)
pending = c.execute(
    "SELECT count(*) FROM system_requests WHERE json_type(outcome_json,'$.runtime_intent')='object'"
).fetchone()[0]
lines = subprocess.check_output(
    [
        "journalctl",
        "-u",
        "eidolon-unit-applier",
        "--since",
        "@" + str(int(started)),
        "-o",
        "json",
        "--no-pager",
    ],
    text=True,
).splitlines()
messages = [json.loads(line).get("MESSAGE", "") for line in lines]
accepted = [m for m in messages if "accepted restart eidolon-livekit.service" in m]
observed_ids = {
    s["process"]["InvocationID"] for s in samples if "process" in s and s["process"]["InvocationID"]
}
final = states()
after = process()
result = {
    "started_at_utc": datetime.datetime.fromtimestamp(started, datetime.timezone.utc).isoformat(),
    "duration_s": round(time.time() - started, 2),
    "before": before,
    "after": after,
    "request_elapsed_s": round(elapsed, 3),
    "post_submit": post_submit,
    "before_manager_restart": before_manager_restart,
    "during_submit_services": during,
    "manager_restarted_during_host_job": restarted_during_job,
    "manager_before": manager_before,
    "manager_after": process("eidolond"),
    "receipt": receipt,
    "replay": replay,
    "replay_after_recovery": replay_after,
    "samples": samples,
    "accepted_restart_log": accepted,
    "pending_intents_after": pending,
    "checks": {
        "manager_restarted_while_job_pending": restarted_during_job
        and manager_before["InvocationID"] != process("eidolond")["InvocationID"],
        "command_returned_before_completion": bool(post_submit["Job"]),
        "only_one_restart_submitted": len(accepted) == 1,
        "one_replacement_observed": observed_ids <= {before["InvocationID"], after["InvocationID"]}
        and before["InvocationID"] != after["InvocationID"],
        "all_services_ready": all(s["runtime_state"] == "ready" for s in final),
        "pending_intents_empty": pending == 0,
        "hub_ready_while_media_restarting": next(s for s in during if s["service_id"] == "hub")[
            "runtime_state"
        ]
        == "ready",
    },
}
print(json.dumps(result, indent=2))
assert all(result["checks"].values()), result["checks"]
