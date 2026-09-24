#!/usr/bin/env python3
"""Create Sotoon compute VMs from a declarative inventory.

    ./sotoon_vm.py inventory.yaml [group ...] [--dry-run] [--delete] [--no-wait]

The inventory has top-level `vars:` inherited by every group and a `groups:` map
that overrides them, ansible-style. Each group fans out to `count:` numbered VMs
or to an explicit `hosts:` list, each with its own link and external IP.Naming a
group on the command line limits the run to it. One VM failing does not stop the 
rest.

Credentials (`workspace:` and `token:`) live at the top of the inventory.
API: https://api.sotoon.ir/redoc#tag/Compute
"""
import argparse, json, pathlib, re, sys, time, urllib.error, urllib.request
import yaml

BASE = "https://api.sotoon.ir/compute/v2"
FAMILY = {"eco": "e", "economy": "e", "general": "g", "general-purpose": "g",
          "compute": "c", "compute-optimized": "c", "memory": "m", "memory-optimized": "m"}


class APIError(Exception):
    pass


def instance_type(want):
    """`{family}{gen}d{GiB per core}-{cores}` -- e.g. c1d30-1, m1d30-4.

    `os_disk` is the total, which the name encodes per core, so it has to divide
    evenly. A plain string passes through for a name that breaks the pattern.
    """
    if isinstance(want, str):
        return want
    letter = FAMILY.get(str(want["group"]).lower().replace("_", "-"))
    if not letter:
        raise SystemExit(f"unknown group {want['group']!r}; use one of {sorted(set(FAMILY))}")
    cores = int(want["cores"])
    total = int(re.sub(r"[^0-9]", "", str(want["os_disk"])))
    per_core, remainder = divmod(total, cores)
    if remainder or not per_core:
        raise SystemExit(
            f"os_disk {total}Gi does not divide into {cores} cores; "
            f"nearest are {per_core * cores}Gi and {(per_core + 1) * cores}Gi")
    return f"{letter}{want.get('gen', 1)}d{per_core}-{cores}"


def merge(base, over):
    """Group vars win over inventory vars; nested dicts merge rather than replace."""
    out = dict(base)
    for k, v in (over or {}).items():
        out[k] = merge(out[k], v) if isinstance(v, dict) and isinstance(out.get(k), dict) else v
    return out


def load_inventory(path, only=()):
    """-> (workspace, token, [(group name, resolved config)]) in file order."""
    doc = yaml.safe_load(path.read_text())
    missing = [k for k in ("workspace", "token", "groups") if k not in doc]
    if missing:
        raise SystemExit(f"{path}: missing {', '.join(missing)} (see README.md)")
    unknown = set(only) - set(doc["groups"])
    if unknown:
        raise SystemExit(f"no such group: {', '.join(sorted(unknown))}")
    out = []
    for name, group in doc["groups"].items():
        if only and name not in only:
            continue
        cfg = merge(doc.get("vars", {}), group)
        cfg.setdefault("name", name)
        out.append((name, cfg))
    return doc["workspace"], doc["token"], out


def vm_names(cfg):
    """Explicit `hosts:`, else `count: 40` on `name: es` -> es-01 .. es-40."""
    if cfg.get("hosts"):
        return list(cfg["hosts"])
    count = int(cfg.get("count", 0))
    if not count:
        return [cfg["name"]]
    start = int(cfg.get("start", 1))
    width = len(str(start + count - 1))
    return [f"{cfg['name']}-{i:0{width}d}" for i in range(start, start + count)]


class API:
    def __init__(self, token, workspace, region, dry_run=False):
        self.token, self.ws, self.region, self.dry_run = token, workspace, region, dry_run

    def _call(self, method, path, body=None):
        url = f"{BASE}/{self.region}{path}"
        req = urllib.request.Request(
            url, method=method,
            data=json.dumps(body).encode() if body is not None else None,
            headers={"Authorization": f"Bearer {self.token}",
                     "Content-Type": "application/json", "Accept": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                return json.loads(r.read() or b"null")
        except urllib.error.HTTPError as e:
            raise APIError(f"{method} {url} -> {e.code}\n"
                           f"{e.read().decode(errors='replace')[:600]}") from None

    def get(self, path):
        return self._call("GET", path)

    def post(self, kind, plural, name, spec, labels=None):
        body = {"apiVersion": "compute/v2", "kind": kind,
                "metadata": {"name": name, "workspace": self.ws, "labels": labels or {}},
                "spec": spec}
        if self.dry_run:
            print(f"# POST /workspaces/{self.ws}/{plural}\n{json.dumps(body, indent=2)}")
            return name
        return self._call("POST", f"/workspaces/{self.ws}/{plural}", body)["metadata"]["name"]

    def delete(self, plural, name):
        self._call("DELETE", f"/workspaces/{self.ws}/{plural}/{name}")


def build(api, cfg, vm, itype):
    labels, net = dict(cfg.get("labels", {})), cfg["network"]

    external_ip = None
    if net.get("public_ip"):
        external_ip = api.post("ExternalIP", "external-ips", f"{vm}-eip",
                               {"reserved": bool(net.get("reserve_ip", False))}, labels)

    link_spec = {"vpcName": net["vpc"], "subnetName": net["subnet"]}
    if net.get("ip"):
        link_spec["ip"] = net["ip"]  # only sane for a single VM
    if external_ip:
        link_spec["externalIPRef"] = {"name": external_ip}
    link = api.post("Link", "links", f"{vm}-nic0", link_spec, labels)

    # No volumes: the instance type's d<n> fixes the local OS disk, and network
    # disks are out of scope -- this API cannot create them, only reference claims.
    spec = {
        "imageSource": {"image": cfg["image"]},
        "type": itype,
        "poweredOn": cfg.get("powered_on", True),
        "interfaces": [{"name": "ens3", "link": link}],
    }
    # SSH is authenticated through Sotoon IAM; set `iam_ssh: false` to turn it off.
    spec["iamEnabled"] = cfg.get("iam_ssh", True)
    if user := cfg.get("user"):
        # password is a secret reference, never a literal; optional:true has the
        # platform generate it into <instance>-login, readable via GET /secrets.
        spec["initialUser"] = {"username": user["name"],
                               "password": {"fromSecret": {"optional": True}}}
    if cfg.get("cloud_init_base64"):
        spec["userData"] = cfg["cloud_init_base64"]

    return api.post("Instance", "instances", vm, spec, labels)


def wait_ready(api, names, timeout=900):
    """Poll until every instance reports status.ready. Returns the stragglers."""
    pending, deadline = list(names), time.time() + timeout
    while pending and time.time() < deadline:
        still = []
        for n in pending:
            try:
                if (api.get(f"/workspaces/{api.ws}/instances/{n}").get("status") or {}).get("ready"):
                    print(f"  ready: {n}")
                    continue
            except APIError as e:
                print(f"  poll failed: {e.args[0].splitlines()[0]}")
            still.append(n)
        pending = still
        if pending:
            print(f"  waiting on {len(pending)}: {', '.join(pending[:5])}"
                  f"{'...' if len(pending) > 5 else ''}")
            time.sleep(10)
    return pending


def teardown(api, cfg):
    """Instance, then its link, then its external IP -- reverse of create."""
    failed = []
    for vm in vm_names(cfg):
        for plural, res in (("instances", vm), ("links", f"{vm}-nic0"),
                            ("external-ips", f"{vm}-eip")):
            try:
                api.delete(plural, res)
                print(f"deleted {plural}/{res}")
            except APIError as e:
                print(f"skip {plural}/{res}: {e.args[0].splitlines()[0]}")
                failed.append(f"{plural}/{res}")
    return failed


def create_group(api, cfg, names):
    """-> (created names, failed names). Errors are per VM, never fatal."""
    itype = instance_type(cfg["type"])
    print(f"{len(names)} x {itype} / {cfg['image']}: {names[0]} .. {names[-1]}")
    made, failed = [], []
    for vm in names:
        try:
            made.append(build(api, cfg, vm, itype))
        except APIError as e:
            print(f"  FAILED {vm}: {e.args[0]}")
            failed.append(vm)
    print(f"  created {len(made)}/{len(names)}")
    return made, failed


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("inventory", type=pathlib.Path)
    ap.add_argument("groups", nargs="*", help="limit the run to these groups")
    ap.add_argument("--dry-run", action="store_true", help="print request bodies, send nothing")
    ap.add_argument("--delete", action="store_true", help="tear the VMs and their networks down")
    ap.add_argument("--no-wait", action="store_true")
    args = ap.parse_args()

    workspace, token, groups = load_inventory(args.inventory, args.groups)

    failed, waiting, wants_secret = [], [], False
    for name, cfg in groups:
        api = API(token, workspace, cfg.get("region", "thr1"), args.dry_run)
        print(f"[{name}]")
        if args.delete:
            failed += teardown(api, cfg)
            continue
        made, bad = create_group(api, cfg, vm_names(cfg))
        failed += bad
        wants_secret |= bool(cfg.get("user")) and bool(made)
        if made:
            waiting.append((api, made))

    if not (args.dry_run or args.no_wait):
        for api, made in waiting:
            if stuck := wait_ready(api, made):
                print(f"not ready in time: {', '.join(stuck)}")
                failed += stuck
    if wants_secret and not args.dry_run:
        print("OS passwords: GET /workspaces/<ws>/secrets/<vm>-login (base64)")
    if failed:
        print(f"PROBLEMS with {len(failed)}: {', '.join(failed)}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
