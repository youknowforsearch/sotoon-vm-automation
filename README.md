# sotoon_vm

Create Sotoon compute VMs from a declarative inventory.
One VM = one ExternalIP (optional) + one Link + one Instance.

Requires `python3` and `PyYAML`.

## Commands

```bash
./sotoon_vm.py inventory.yaml --dry-run          # print request bodies, send nothing
./sotoon_vm.py inventory.yaml                    # create everything, wait for ready
./sotoon_vm.py inventory.yaml kafka              # only that group
./sotoon_vm.py inventory.yaml haproxy bastion    # only these groups
./sotoon_vm.py inventory.yaml --no-wait          # create, skip the ready poll
./sotoon_vm.py inventory.yaml --delete           # instance -> link -> external IP
./sotoon_vm.py inventory.yaml kafka --delete
```

Exit code is 0 only if every VM was created and became ready.

## Inventory

```yaml
workspace: "00000000-0000-0000-0000-000000000000"
token: "service-user:<token>"

vars:                        # inherited by every group
  region: thr1               # thr1 | thr3 | thr4
  image: ubuntu-22.04
  iam_ssh: true              # SSH via Sotoon IAM (default)
  network:
    vpc: default
    subnet: default
    public_ip: false
  user:
    name: ubuntu             # default OS user
  labels:
    env: prod

groups:

  service:
    name: random-service     # group name is the VM name prefix
    count: 40                # -> random-service-01 .. random-service-40
    type:
      group: memory          # eco | general | compute | memory
      cores: 4
      os_disk: 120Gi         # total OS disk, must divide by cores -> m1d30-4
    labels:
      app: my-service        # merges with the inherited labels

  haproxy:
    name: lb                 # -> lb-1, lb-2
    count: 2
    type: g1d60-1            # a literal instance-type name also works
    network:
      public_ip: true        # merges over the inherited network block
      reserve_ip: true       # keep the IP after the VM is deleted
    labels:
      app: haproxy

  bastion:
    hosts: [bastion]         # explicit names instead of a count
    type:
      group: eco
      cores: 1
      os_disk: 30Gi
    network:
      public_ip: true
      ip: 10.0.0.15          # pin the private IP (single VM only)
```

Group values override `vars`; nested maps (`network`, `user`, `labels`) merge key
by key rather than replacing the whole block.

### Keys

| Key | Meaning |
|---|---|
| `count` | fan out to numbered VMs, zero-padded to fit |
| `start` | first index, for growing a fleet (`start: 41` -> `-41` onward) |
| `hosts` | explicit VM names, instead of `count` |
| `name` | name prefix, if not the group name |
| `region` | `thr1`, `thr3`, `thr4`; per group |
| `image` | exact image name, e.g. `ubuntu-22.04` |
| `type` | `{group, cores, os_disk}` or a literal name |
| `network.vpc` / `.subnet` | required |
| `network.public_ip` | allocate an ExternalIP and bind it to the link |
| `network.reserve_ip` | keep the ExternalIP after teardown |
| `network.ip` | pin the private IP |
| `user.name` | default OS user; the password is generated into `<vm>-login` |
| `iam_ssh` | SSH auth through Sotoon IAM, default `true` |
| `powered_on` | create stopped with `false` |
| `cloud_init_base64` | base64 cloud-init, sent as `userData` |
| `labels` | key/value, applied to all three resources |

### Instance types

`{family}{gen}d{GiB per core}-{cores}` — `m1d30-4` is Memory Optimized, 4 cores,
30 GiB of OS disk per core. Families: `e`, `g`, `c`, `m`.

You give `os_disk` as the **total**; it is divided by `cores` to build the name, so
it has to divide evenly. `os_disk: 700Gi` on 24 cores fails with the nearest valid
sizes (696Gi, 720Gi). A literal `type: g1d30-24` skips the arithmetic.

## Not supported

Network-attached disks. The API can reference an existing claim but cannot create
a volume or choose a tier, so disks are left to the console.

## Permissions

The service user needs create/get/list/delete on `workspaces/links` and
`workspaces/externalips` (`networking.cafebazaar.cloud`) and on
`workspaces/instances` (`compute.cafebazaar.cloud`), plus get/list on
`workspaces/secrets` (`core/v1`). A 403 naming your user means the token is valid
but no rules are bound to it.
