# MinIO Alternatifleri Benchmark Harness — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Dört S3-uyumlu obje deposunu (MinIO baseline, Silo, RustFS, SeaweedFS) tek node'da, eşitlenmiş hata toleransı altında ölçen ve S3 API uyumluluğunu matrisleyen, yeniden üretilebilir açık kaynak bir benchmark harness'ı inşa etmek.

**Architecture:** Sistem başına bir Docker Compose dosyası, iki compose profili (`c1` redundancy yok, `c2` 1 cihaz kaybına dayanır). Bir orkestratör shell script'i sistemleri round-robin sırayla ayağa kaldırır, `warp` container'ıyla workload matrisini koşturur, `docker stats` telemetrisini toplar ve damgalı JSON üretir. Ayrı bir pytest suite'i S3 conformance matrisini doldurur. Bir analiz script'i ham JSON'ları markdown tablolara çevirir.

**Tech Stack:** Docker Compose, bash, Python 3 (pytest + boto3), warp v1.6.1, jq

**Spec:** `docs/superpowers/specs/2026-08-19-minio-alternatives-benchmark-design.md`

## Global Constraints

Bu bölümdeki her kural, aşağıdaki **tüm** task'ların gereksinimlerine örtük olarak dahildir.

**Pinlenmiş image'lar (arm64 digest'leri, 2026-08-19'da çözüldü):**

| Sistem | Referans | arm64 digest |
|---|---|---|
| MinIO baseline | `alpine/minio:RELEASE.2025-10-15T17-29-55Z` | `sha256:3804726ba5769adfbe099766e70477003ae0bfd1e1a3ea12268ef830f3ffdf39` |
| Silo | `pgsty/silo:latest` | `sha256:2c00469c7b3b9537115727c059873869ec6d82b15fcabf8c75ff9961ae89161f` |
| RustFS | `rustfs/rustfs:rc` | `sha256:821d0244341a9c679b36f87d7b863e8e77cab86f712c26c6ba9ad5669f222e72` |
| SeaweedFS | `chrislusf/seaweedfs:v3.33` | `sha256:52ec3c86db6e726a6cfa74031051ce3fab28fdef1dcebec0574ddeb14670b453` |

**warp:** v1.6.1. Docker image'ı YOKTUR (`minio/warp` Docker Hub'da v1.3.1'de durmuştur). GitHub release asset'inden kurulur:
- URL: `https://github.com/minio/warp/releases/download/v1.6.1/warp-rdma_linux_arm64.tar.gz`
- SHA256: `7ce9f0061fe5d0d0ff809384234ddbab9f9b66a6b15e7fa0b3e0819f63ce0cfc`
- RDMA build'idir; RDMA opt-in'dir, TCP üzerinden normal çalışır. RDMA bayrakları KULLANILMAZ.

**Kaynak bütçesi — sistem başına, container başına DEĞİL:** Bir sistemin tüm container'ları toplamda `6 CPU` ve `2048m` bellek paylaşır. SeaweedFS `c2`'de 5 container'a bölünür; MinIO tek container'da kullanır. Bu kasıtlıdır: çok-process mimarisinin overhead'i o mimarinin gerçek bir özelliğidir.

**Dayanıklılık ekseni:** Hata toleransı eşitlenir (hepsi 1 cihaz/sunucu kaybına dayanır). Depolama verimi eşitlenmez, **sonuç olarak raporlanır**.

**Depolama düzeni:** `c1` = 1 volume, `c2` = 4 volume. Hepsi aynı VM diskinde, aynı dosya sisteminde.

**Ağ:** warp, servislerle aynı user-defined bridge network'te container olarak koşar. Host'tan koşturmak YASAK (gvisor userspace ağ yığını sonucu bozar).

**Ölçüm protokolü:** cold start → healthcheck → 30 sn warmup (atılır) → 60 sn ölçüm. Round-robin 3 tur. Rapor **medyan + p95/p99**, ortalama değil.

**Disk tavanı:** Host'ta ~21 GB boş. Orkestratör her sistem geçişinde volume temizliği + `docker image prune` yapar ve host boş alanı 5 GB'ın altına düşerse ABORT eder.

**Lisans:** Apache-2.0. Harness kodu bizimdir; MinIO/Silo'nun AGPL'i S3 API üzerinden çağrı yapıldığı için bulaşmaz.

**Damgalama:** Üretilen her sonuç dosyası `hardware-profile.json` içeriğini gömer. Bağlamsız sayı üretilmez.

**Dil:** Kod, yorumlar, commit mesajları ve public dokümanlar İngilizce. Blog taslakları Türkçe.

---

## File Structure

| Dosya | Sorumluluk |
|---|---|
| `LICENSE` | Apache-2.0 |
| `.gitignore` | `results/**/raw/`, `.venv/`, `__pycache__/` hariç tutulmaz — raw sonuçlar commit edilir; sadece geçici dosyalar ignore edilir |
| `images.lock` | Pinlenmiş digest'ler, tek gerçek kaynağı |
| `requirements-dev.txt` | pytest, boto3, botocore pinleri |
| `bench/hwprofile.sh` | Donanım/runtime profili → JSON |
| `bench/lock-images.sh` | Digest'leri registry'den yeniden çözer, `images.lock`'u doğrular |
| `bench/stack.sh` | Bir sistemi bir konfigde ayağa kaldırır/indirir/healthcheck eder |
| `bench/warp/Dockerfile` | warp binary'sini checksum doğrulayarak kuran minimal image |
| `bench/workloads.yaml` | Workload matrisi tanımı |
| `bench/run-workload.sh` | Tek bir (sistem, konfig, profil, concurrency) koşumu |
| `bench/telemetry.sh` | `docker stats` akışı → CSV |
| `bench/run.sh` | Orkestratör: round-robin, disk guard, prune |
| `bench/analyze.py` | Ham JSON → medyan/p95/p99 → markdown tablo |
| `compose/_common.yaml` | Ortak YAML anchor'ları (limitler, network, healthcheck) |
| `compose/{minio,silo,rustfs,seaweedfs}.yaml` | Sistem başına `c1` ve `c2` profilleri |
| `conformance/conftest.py` | S3 client fixture'ları, sonuç toplayıcı hook |
| `conformance/test_s3_conformance.py` | ~25 hedefli assertion |
| `tests/test_hwprofile.py` | hwprofile JSON şema testi |
| `tests/test_images_lock.py` | images.lock bütünlük testi |
| `tests/test_smoke_roundtrip.py` | Her sistem için put/get/delete roundtrip |
| `tests/test_durability.py` | Fault injection: 1 cihaz kaybında okuma başarılı mı |
| `README.md` / `METHODOLOGY.md` / `CONTRIBUTING.md` | Public dokümanlar |
| `docs/RESULTS.md` | Analiz çıktısı |
| `docs/blog-1-conformance.md` / `docs/blog-2-methodology.md` | Blog taslakları |

---

## Task 1: Repo İskeleti, Image Lock ve Donanım Profili

**Files:**
- Create: `LICENSE`, `.gitignore`, `requirements-dev.txt`, `images.lock`
- Create: `bench/hwprofile.sh`, `bench/lock-images.sh`
- Test: `tests/test_hwprofile.py`, `tests/test_images_lock.py`

**Interfaces:**
- Produces: `bench/hwprofile.sh` → stdout'a `hardware-profile.json` şeması (schema v1). `profile_id` alanı diğer tüm task'lar tarafından `results/<profile_id>/` dizin adı olarak kullanılır.
- Produces: `images.lock` → JSON, `.images.<name>.digest` yolu ile okunur. `<name>` ∈ {minio, silo, rustfs, seaweedfs}.

- [ ] **Step 1: Python ortamı ve bağımlılıklar**

`requirements-dev.txt`:
```
pytest==8.3.4
boto3==1.36.2
botocore==1.36.2
pyyaml==6.0.2
```

`botocore>=1.36` gereklidir: `put_object(IfNoneMatch=...)` parametresi bu sürümlerde mevcuttur. Daha eski bir botocore ile conditional write testleri parametreyi sessizce düşürür ve YANLIŞ "destekleniyor" sonucu üretir.

Çalıştır:
```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements-dev.txt
```

- [ ] **Step 2: images.lock oluştur**

`images.lock`:
```json
{
  "schema": 1,
  "resolved_at": "2026-08-19",
  "platform": "linux/arm64",
  "images": {
    "minio": {
      "ref": "alpine/minio:RELEASE.2025-10-15T17-29-55Z",
      "digest": "sha256:3804726ba5769adfbe099766e70477003ae0bfd1e1a3ea12268ef830f3ffdf39",
      "note": "Last MinIO community release. Third-party image; upstream stopped publishing on this release."
    },
    "silo": {
      "ref": "pgsty/silo:latest",
      "digest": "sha256:2c00469c7b3b9537115727c059873869ec6d82b15fcabf8c75ff9961ae89161f",
      "note": "PGSTY fork of MinIO. Drop-in; full console restored."
    },
    "rustfs": {
      "ref": "rustfs/rustfs:rc",
      "digest": "sha256:821d0244341a9c679b36f87d7b863e8e77cab86f712c26c6ba9ad5669f222e72",
      "note": "Release candidate. Distributed mode marked under testing upstream."
    },
    "seaweedfs": {
      "ref": "chrislusf/seaweedfs:v3.33",
      "digest": "sha256:52ec3c86db6e726a6cfa74031051ce3fab28fdef1dcebec0574ddeb14670b453",
      "note": "Stable release line."
    }
  },
  "warp": {
    "version": "v1.6.1",
    "asset": "warp-rdma_linux_arm64.tar.gz",
    "url": "https://github.com/minio/warp/releases/download/v1.6.1/warp-rdma_linux_arm64.tar.gz",
    "sha256": "7ce9f0061fe5d0d0ff809384234ddbab9f9b66a6b15e7fa0b3e0819f63ce0cfc",
    "note": "No Docker image published for this version; built locally from the release asset."
  }
}
```

- [ ] **Step 3: images.lock testini yaz (başarısız olacak)**

`tests/test_images_lock.py`:
```python
import json
import re
from pathlib import Path

LOCK = Path(__file__).resolve().parents[1] / "images.lock"
EXPECTED_SYSTEMS = {"minio", "silo", "rustfs", "seaweedfs"}
DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")


def load():
    return json.loads(LOCK.read_text())


def test_lock_file_exists():
    assert LOCK.is_file(), f"{LOCK} missing"


def test_all_systems_present():
    assert set(load()["images"]) == EXPECTED_SYSTEMS


def test_every_image_pinned_by_digest():
    for name, entry in load()["images"].items():
        assert DIGEST_RE.match(entry["digest"]), f"{name} digest malformed: {entry['digest']}"


def test_every_image_has_a_note():
    for name, entry in load()["images"].items():
        assert entry.get("note"), f"{name} missing provenance note"


def test_warp_pinned_by_checksum():
    warp = load()["warp"]
    assert re.match(r"^[0-9a-f]{64}$", warp["sha256"])
    assert warp["url"].endswith(warp["asset"])


def test_platform_is_arm64():
    assert load()["platform"] == "linux/arm64"
```

- [ ] **Step 4: Testi koştur, geçmesini doğrula**

Run: `.venv/bin/pytest tests/test_images_lock.py -v`
Expected: 6 passed

- [ ] **Step 5: lock-images.sh doğrulayıcısını yaz**

`bench/lock-images.sh` — digest'leri registry'den yeniden çözer ve `images.lock` ile karşılaştırır. Drift tespiti içindir; CI'da ve yayından önce koşturulur.

```bash
#!/usr/bin/env bash
# Re-resolve arm64 digests from the registry and diff against images.lock.
# Exit 1 on any drift. This is how we detect a tag being re-pointed upstream.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LOCK="$ROOT/images.lock"
drift=0

resolve_arm64_digest() {
  local repo="$1" tag="$2" token manifest
  token=$(curl -fsS "https://auth.docker.io/token?service=registry.docker.io&scope=repository:${repo}:pull" \
    | python3 -c 'import sys,json; print(json.load(sys.stdin)["token"])')
  manifest=$(curl -fsS \
    -H "Authorization: Bearer ${token}" \
    -H "Accept: application/vnd.oci.image.index.v1+json, application/vnd.docker.distribution.manifest.list.v2+json" \
    "https://registry-1.docker.io/v2/${repo}/manifests/${tag}")
  printf '%s' "$manifest" | python3 -c '
import sys, json
index = json.load(sys.stdin)
for m in index.get("manifests", []):
    p = m.get("platform", {})
    if p.get("os") == "linux" and p.get("architecture") == "arm64":
        print(m["digest"]); break
else:
    sys.exit("no linux/arm64 manifest in index")
'
}

while IFS=$'\t' read -r name ref locked; do
  repo="${ref%:*}"; tag="${ref##*:}"
  actual=$(resolve_arm64_digest "$repo" "$tag")
  if [[ "$actual" == "$locked" ]]; then
    printf 'ok    %-10s %s\n' "$name" "$locked"
  else
    printf 'DRIFT %-10s locked=%s actual=%s\n' "$name" "$locked" "$actual"
    drift=1
  fi
done < <(python3 -c '
import json, sys
lock = json.load(open(sys.argv[1]))
for name, e in lock["images"].items():
    print(f"{name}\t{e[\"ref\"]}\t{e[\"digest\"]}")
' "$LOCK")

exit "$drift"
```

Run: `chmod +x bench/lock-images.sh && ./bench/lock-images.sh`
Expected: dört satır `ok`, exit 0. Herhangi bir `DRIFT` satırı, upstream'in tag'i yeniden işaretlediği anlamına gelir — `images.lock` bilinçli olarak güncellenmeli ve sonuçlar yeniden koşulmalıdır.

- [ ] **Step 6: hwprofile testini yaz (başarısız olacak)**

`tests/test_hwprofile.py`:
```python
import json
import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "bench" / "hwprofile.sh"


def profile():
    out = subprocess.run([str(SCRIPT)], capture_output=True, text=True, check=True).stdout
    return json.loads(out)


def test_script_is_executable():
    assert SCRIPT.is_file() and SCRIPT.stat().st_mode & 0o111


def test_emits_valid_json_with_schema_version():
    assert profile()["schema"] == 1


def test_profile_id_is_a_slug():
    pid = profile()["profile_id"]
    assert re.match(r"^[a-z0-9]+(-[a-z0-9]+)*$", pid), f"not a slug: {pid}"


def test_host_block_is_populated():
    host = profile()["host"]
    assert host["arch"] in {"arm64", "aarch64", "x86_64", "amd64"}
    assert host["cpu_cores"] >= 1
    assert host["ram_bytes"] > 1_000_000_000
    assert host["cpu_model"]


def test_runtime_block_reports_vm_resources():
    rt = profile()["container_runtime"]
    assert rt["server_version"]
    assert rt["vm_cpus"] >= 1
    assert rt["vm_ram_bytes"] > 0


def test_disk_block_reports_host_free_space():
    assert profile()["disk"]["host_free_bytes"] > 0


def test_images_are_embedded_from_lock():
    assert set(profile()["images"]) == {"minio", "silo", "rustfs", "seaweedfs"}


def test_captured_at_is_iso8601_utc():
    assert re.match(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$", profile()["captured_at"])
```

- [ ] **Step 7: Testi koştur, başarısız olduğunu doğrula**

Run: `.venv/bin/pytest tests/test_hwprofile.py -v`
Expected: FAIL — `bench/hwprofile.sh` yok

- [ ] **Step 8: hwprofile.sh'i yaz**

Linux'ta da çalışmalıdır — katkıda bulunanların çoğu Linux'ta koşturacak.

```bash
#!/usr/bin/env bash
# Emit a hardware/runtime profile as JSON on stdout.
# Every result file embeds this so no number ever travels without its context.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

case "$(uname -s)" in
  Darwin)
    os="darwin"
    os_version="$(uname -r)"
    cpu_model="$(sysctl -n machdep.cpu.brand_string)"
    cpu_cores="$(sysctl -n hw.ncpu)"
    ram_bytes="$(sysctl -n hw.memsize)"
    host_free_bytes=$(( $(df -k /System/Volumes/Data | awk 'NR==2 {print $4}') * 1024 ))
    ;;
  Linux)
    os="linux"
    os_version="$(uname -r)"
    cpu_model="$(awk -F': ' '/model name|Model/ {print $2; exit}' /proc/cpuinfo)"
    [ -n "$cpu_model" ] || cpu_model="unknown"
    cpu_cores="$(nproc)"
    ram_bytes=$(( $(awk '/MemTotal/ {print $2}' /proc/meminfo) * 1024 ))
    host_free_bytes=$(( $(df -k / | awk 'NR==2 {print $4}') * 1024 ))
    ;;
  *) echo "unsupported OS: $(uname -s)" >&2; exit 1 ;;
esac

arch="$(uname -m)"

# profile_id names the results directory. Slug form: <cpu>-<ram>gb-<os>
ram_gb=$(( ram_bytes / 1024 / 1024 / 1024 ))
cpu_slug="$(printf '%s' "$cpu_model" \
  | tr '[:upper:]' '[:lower:]' \
  | sed -E 's/\(r\)|\(tm\)//g; s/[^a-z0-9]+/-/g; s/^-+|-+$//g' \
  | cut -c1-24 | sed -E 's/-+$//')"
profile_id="${PROFILE_ID:-${cpu_slug}-${ram_gb}gb-${os}}"

docker_server="$(docker info --format '{{.ServerVersion}}')"
docker_client="$(docker version --format '{{.Client.Version}}')"
vm_cpus="$(docker info --format '{{.NCPU}}')"
vm_ram_bytes="$(docker info --format '{{.MemTotal}}')"
vm_arch="$(docker info --format '{{.Architecture}}')"
storage_driver="$(docker info --format '{{.Driver}}')"

python3 - "$ROOT/images.lock" <<PYEOF
import json, sys
lock = json.load(open(sys.argv[1]))
print(json.dumps({
    "schema": 1,
    "profile_id": "${profile_id}",
    "captured_at": "$(date -u +%Y-%m-%dT%H:%M:%SZ)",
    "host": {
        "os": "${os}",
        "os_version": "${os_version}",
        "arch": "${arch}",
        "cpu_model": "${cpu_model}",
        "cpu_cores": ${cpu_cores},
        "ram_bytes": ${ram_bytes},
    },
    "container_runtime": {
        "kind": "docker",
        "client_version": "${docker_client}",
        "server_version": "${docker_server}",
        "vm_cpus": ${vm_cpus},
        "vm_ram_bytes": ${vm_ram_bytes},
        "vm_arch": "${vm_arch}",
        "storage_driver": "${storage_driver}",
    },
    "disk": {"host_free_bytes": ${host_free_bytes}},
    "images": {k: v["digest"] for k, v in lock["images"].items()},
    "warp": lock["warp"]["version"],
}, indent=2))
PYEOF
```

- [ ] **Step 9: Testi koştur, geçmesini doğrula**

Run: `chmod +x bench/hwprofile.sh && .venv/bin/pytest tests/test_hwprofile.py -v`
Expected: 8 passed

Bu makinede beklenen `profile_id`: `apple-m1-8gb-darwin`

- [ ] **Step 10: LICENSE ve .gitignore**

`LICENSE` — Apache License 2.0 tam metni. Kaynak: https://www.apache.org/licenses/LICENSE-2.0.txt
Telif satırı: `Copyright 2026 Mertcan Sağlam`

`.gitignore`:
```
.venv/
__pycache__/
*.pyc
.pytest_cache/
bench/warp/warp-rdma_linux_arm64.tar.gz
results/**/tmp/
```

Dikkat: `results/**/raw/` ignore EDİLMEZ. Ham sonuçlar bilerek commit edilir — reprodüksiyon iddiasının kanıtı odur.

- [ ] **Step 11: Commit**

```bash
git add LICENSE .gitignore requirements-dev.txt images.lock bench/ tests/
git commit -m "feat: repo skeleton, digest-pinned image lock, hardware profiler

Images pinned by arm64 digest so results stay reproducible after upstream
re-points a tag. lock-images.sh detects drift. Every result file will embed
hardware-profile.json output."
```

---

## Task 2: Konfig 1 Compose Stack'leri (redundancy yok) ve Smoke Test

**Files:**
- Create: `compose/_common.yaml`, `compose/minio.yaml`, `compose/silo.yaml`, `compose/rustfs.yaml`, `compose/seaweedfs.yaml`
- Create: `bench/stack.sh`
- Test: `tests/test_smoke_roundtrip.py`

**Interfaces:**
- Consumes: `images.lock` digest'leri (Task 1)
- Produces: `bench/stack.sh up <system> <config>` / `down <system>` / `endpoint <system>` API'si. `<system>` ∈ {minio, silo, rustfs, seaweedfs}, `<config>` ∈ {c1, c2}. Tüm sistemler S3'ü container içinde `9000` portunda sunar ve host'a `19000` olarak map edilir. Erişim anahtarları her sistemde `benchuser` / `benchsecret0`.
- Produces: Ortak network adı `s3bench` (external, orkestratör tarafından oluşturulur).

- [ ] **Step 1: Ortak anchor dosyasını yaz**

`compose/_common.yaml` — YAML anchor'ları. Compose `extends` yerine anchor kullanıyoruz çünkü kaynak limitlerinin tek yerde durması gerekiyor.

```yaml
# Shared anchors. Resource budget is PER SYSTEM, not per container:
# every system's containers together get 6 CPUs and 2048m.
x-net: &net
  networks: [s3bench]

x-s3-healthcheck: &s3-healthcheck
  interval: 2s
  timeout: 3s
  retries: 45
  start_period: 3s

networks:
  s3bench:
    external: true
    name: s3bench
```

- [ ] **Step 2: MinIO compose'unu yaz**

`compose/minio.yaml`:
```yaml
include: [_common.yaml]

services:
  minio:
    profiles: ["c1"]
    image: alpine/minio@sha256:3804726ba5769adfbe099766e70477003ae0bfd1e1a3ea12268ef830f3ffdf39
    container_name: bench-minio
    <<: *net
    command: server /data --address :9000 --console-address :9001
    environment:
      MINIO_ROOT_USER: benchuser
      MINIO_ROOT_PASSWORD: benchsecret0
    ports: ["19000:9000"]
    volumes: ["minio-d0:/data"]
    cpus: 6.0
    mem_limit: 2048m
    healthcheck:
      <<: *s3-healthcheck
      test: ["CMD", "mc", "ready", "local"]

  minio-c2:
    profiles: ["c2"]
    image: alpine/minio@sha256:3804726ba5769adfbe099766e70477003ae0bfd1e1a3ea12268ef830f3ffdf39
    container_name: bench-minio
    <<: *net
    command: server /data{0...3} --address :9000 --console-address :9001
    environment:
      MINIO_ROOT_USER: benchuser
      MINIO_ROOT_PASSWORD: benchsecret0
      # EC:1 over 4 drives = 3 data + 1 parity -> survives 1 drive loss, 75% usable.
      MINIO_STORAGE_CLASS_STANDARD: "EC:1"
    ports: ["19000:9000"]
    volumes:
      - "minio-d0:/data0"
      - "minio-d1:/data1"
      - "minio-d2:/data2"
      - "minio-d3:/data3"
    cpus: 6.0
    mem_limit: 2048m
    healthcheck:
      <<: *s3-healthcheck
      test: ["CMD", "mc", "ready", "local"]

volumes:
  minio-d0: {}
  minio-d1: {}
  minio-d2: {}
  minio-d3: {}
```

- [ ] **Step 3: Silo compose'unu yaz**

`compose/silo.yaml` — MinIO ile aynı yapı; sadece image ve container adı değişir. Silo drop-in olduğu için komut ve env değişkenleri birebir aynıdır; bu dosyanın MinIO'nunkine bu kadar benzemesi bulgunun kendisidir.

```yaml
include: [_common.yaml]

services:
  silo:
    profiles: ["c1"]
    image: pgsty/silo@sha256:2c00469c7b3b9537115727c059873869ec6d82b15fcabf8c75ff9961ae89161f
    container_name: bench-silo
    <<: *net
    command: server /data --address :9000 --console-address :9001
    environment:
      MINIO_ROOT_USER: benchuser
      MINIO_ROOT_PASSWORD: benchsecret0
    ports: ["19000:9000"]
    volumes: ["silo-d0:/data"]
    cpus: 6.0
    mem_limit: 2048m
    healthcheck:
      <<: *s3-healthcheck
      test: ["CMD-SHELL", "curl -fsS http://localhost:9000/minio/health/live || exit 1"]

  silo-c2:
    profiles: ["c2"]
    image: pgsty/silo@sha256:2c00469c7b3b9537115727c059873869ec6d82b15fcabf8c75ff9961ae89161f
    container_name: bench-silo
    <<: *net
    command: server /data{0...3} --address :9000 --console-address :9001
    environment:
      MINIO_ROOT_USER: benchuser
      MINIO_ROOT_PASSWORD: benchsecret0
      MINIO_STORAGE_CLASS_STANDARD: "EC:1"
    ports: ["19000:9000"]
    volumes:
      - "silo-d0:/data0"
      - "silo-d1:/data1"
      - "silo-d2:/data2"
      - "silo-d3:/data3"
    cpus: 6.0
    mem_limit: 2048m
    healthcheck:
      <<: *s3-healthcheck
      test: ["CMD-SHELL", "curl -fsS http://localhost:9000/minio/health/live || exit 1"]

volumes:
  silo-d0: {}
  silo-d1: {}
  silo-d2: {}
  silo-d3: {}
```

Not: Silo image'ı distroless olabilir ve `mc` içermeyebilir; bu yüzden healthcheck `curl` ile HTTP health endpoint'ini kullanır. `curl` de yoksa Step 8'de düzeltilir.

- [ ] **Step 4: SeaweedFS compose'unu yaz**

`compose/seaweedfs.yaml` — `c1` tek container (`weed server` master+volume+filer+s3'ü tek process'te koşturur). `c2` replication `001` için ayrı volume server'lar gerektirir; 6 CPU / 2048m bütçesi 5 container'a bölünür.

```yaml
include: [_common.yaml]

services:
  seaweedfs:
    profiles: ["c1"]
    image: chrislusf/seaweedfs@sha256:52ec3c86db6e726a6cfa74031051ce3fab28fdef1dcebec0574ddeb14670b453
    container_name: bench-seaweedfs
    <<: *net
    command: >
      server -dir=/data -s3 -s3.port=9000
      -master.volumeSizeLimitMB=1024
      -volume.max=0
      -defaultReplication=000
    environment:
      AWS_ACCESS_KEY_ID: benchuser
      AWS_SECRET_ACCESS_KEY: benchsecret0
    ports: ["19000:9000"]
    volumes: ["swfs-d0:/data"]
    cpus: 6.0
    mem_limit: 2048m
    healthcheck:
      <<: *s3-healthcheck
      test: ["CMD-SHELL", "wget -qO- http://localhost:9000 >/dev/null 2>&1 || exit 1"]

  # ---- c2: master+filer+s3 plus four independent volume servers ----
  swfs-master:
    profiles: ["c2"]
    image: chrislusf/seaweedfs@sha256:52ec3c86db6e726a6cfa74031051ce3fab28fdef1dcebec0574ddeb14670b453
    container_name: bench-seaweedfs
    <<: *net
    command: >
      server -dir=/data -s3 -s3.port=9000
      -master.volumeSizeLimitMB=1024
      -volume=false
      -defaultReplication=001
    environment:
      AWS_ACCESS_KEY_ID: benchuser
      AWS_SECRET_ACCESS_KEY: benchsecret0
    ports: ["19000:9000"]
    volumes: ["swfs-meta:/data"]
    cpus: 2.0
    mem_limit: 768m
    healthcheck:
      <<: *s3-healthcheck
      test: ["CMD-SHELL", "wget -qO- http://localhost:9000 >/dev/null 2>&1 || exit 1"]

  swfs-vol0: &swfs-vol
    profiles: ["c2"]
    image: chrislusf/seaweedfs@sha256:52ec3c86db6e726a6cfa74031051ce3fab28fdef1dcebec0574ddeb14670b453
    container_name: bench-swfs-vol0
    <<: *net
    command: volume -dir=/data -mserver=bench-seaweedfs:9333 -port=8080 -ip=bench-swfs-vol0 -max=0
    volumes: ["swfs-d0:/data"]
    cpus: 1.0
    mem_limit: 320m
    depends_on: [swfs-master]

  swfs-vol1:
    <<: *swfs-vol
    container_name: bench-swfs-vol1
    command: volume -dir=/data -mserver=bench-seaweedfs:9333 -port=8080 -ip=bench-swfs-vol1 -max=0
    volumes: ["swfs-d1:/data"]

  swfs-vol2:
    <<: *swfs-vol
    container_name: bench-swfs-vol2
    command: volume -dir=/data -mserver=bench-seaweedfs:9333 -port=8080 -ip=bench-swfs-vol2 -max=0
    volumes: ["swfs-d2:/data"]

  swfs-vol3:
    <<: *swfs-vol
    container_name: bench-swfs-vol3
    command: volume -dir=/data -mserver=bench-seaweedfs:9333 -port=8080 -ip=bench-swfs-vol3 -max=0
    volumes: ["swfs-d3:/data"]

volumes:
  swfs-meta: {}
  swfs-d0: {}
  swfs-d1: {}
  swfs-d2: {}
  swfs-d3: {}
```

Toplam `c2` bütçesi: 2.0 + 4×1.0 = 6.0 CPU, 768m + 4×320m = 2048m. Global constraint'e uyar.

- [ ] **Step 5: RustFS compose'unu yaz (bayraklar Task 3'te doğrulanacak)**

`compose/rustfs.yaml` — `c1` yazılır. `c2`'nin EC bayrakları Task 3 Step 1'de CLI'dan öğrenilecek ve orada eklenecektir. Bu task sadece `c1`'i teslim eder.

```yaml
include: [_common.yaml]

services:
  rustfs:
    profiles: ["c1"]
    image: rustfs/rustfs@sha256:821d0244341a9c679b36f87d7b863e8e77cab86f712c26c6ba9ad5669f222e72
    container_name: bench-rustfs
    <<: *net
    environment:
      RUSTFS_ACCESS_KEY: benchuser
      RUSTFS_SECRET_KEY: benchsecret0
      RUSTFS_ADDRESS: ":9000"
      RUSTFS_VOLUMES: "/data"
    ports: ["19000:9000"]
    volumes: ["rustfs-d0:/data"]
    cpus: 6.0
    mem_limit: 2048m
    healthcheck:
      <<: *s3-healthcheck
      test: ["CMD-SHELL", "curl -fsS http://localhost:9000/ -o /dev/null || exit 1"]

volumes:
  rustfs-d0: {}
  rustfs-d1: {}
  rustfs-d2: {}
  rustfs-d3: {}
```

`RUSTFS_*` env adları RustFS'in MinIO-benzeri konvansiyonundan türetilmiştir. Step 8 bunları ampirik olarak doğrular; container ayağa kalkmazsa `docker run --rm <digest> --help` çıktısına göre düzeltilir.

- [ ] **Step 6: stack.sh'i yaz**

`bench/stack.sh`:
```bash
#!/usr/bin/env bash
# Bring one system up in one config, wait until its S3 endpoint answers, tear it down.
# Usage: stack.sh up <system> <config> | down <system> | endpoint <system>
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
COMPOSE_DIR="$ROOT/compose"
NETWORK="s3bench"
ACCESS_KEY="benchuser"
SECRET_KEY="benchsecret0"

ensure_network() {
  docker network inspect "$NETWORK" >/dev/null 2>&1 || docker network create "$NETWORK" >/dev/null
}

compose() {
  local system="$1"; shift
  docker compose -f "$COMPOSE_DIR/${system}.yaml" -p "bench-${system}" "$@"
}

wait_ready() {
  # Poll the S3 endpoint from inside the network. A container healthcheck is not
  # enough: a system can report healthy before its S3 route is serving.
  local system="$1" deadline=$((SECONDS + 120))
  while (( SECONDS < deadline )); do
    if docker run --rm --network "$NETWORK" \
         curlimages/curl:8.11.1 -fsS -o /dev/null \
         "http://bench-${system}:9000/" 2>/dev/null; then
      return 0
    fi
    # A 403 from the S3 root also means "serving" - anonymous list is denied.
    if docker run --rm --network "$NETWORK" \
         curlimages/curl:8.11.1 -sS -o /dev/null -w '%{http_code}' \
         "http://bench-${system}:9000/" 2>/dev/null | grep -qE '^(200|403)$'; then
      return 0
    fi
    sleep 2
  done
  echo "timeout waiting for ${system} S3 endpoint" >&2
  compose "$system" logs --tail 60 >&2
  return 1
}

case "${1:-}" in
  up)
    system="$2"; config="$3"
    ensure_network
    compose "$system" --profile "$config" up -d
    wait_ready "$system"
    ;;
  down)
    system="$2"
    compose "$system" --profile c1 --profile c2 down -v --remove-orphans
    ;;
  endpoint)
    printf 'http://bench-%s:9000\n' "$2"
    ;;
  *)
    echo "usage: stack.sh {up <system> <config>|down <system>|endpoint <system>}" >&2
    exit 2
    ;;
esac
```

- [ ] **Step 7: Smoke testini yaz (başarısız olacak)**

`tests/test_smoke_roundtrip.py`:
```python
"""Every system must survive a put/get/delete roundtrip before it is benchmarked.

These run against the host-mapped port (19000). The benchmark itself talks over
the bridge network, but for a correctness smoke test the host port is simpler
and the extra latency does not matter.
"""
import subprocess
import time
from pathlib import Path

import boto3
import pytest
from botocore.client import Config
from botocore.exceptions import ClientError

ROOT = Path(__file__).resolve().parents[1]
STACK = str(ROOT / "bench" / "stack.sh")
SYSTEMS = ["minio", "silo", "rustfs", "seaweedfs"]
ENDPOINT = "http://127.0.0.1:19000"


@pytest.fixture(params=SYSTEMS)
def system(request):
    name = request.param
    subprocess.run([STACK, "up", name, "c1"], check=True)
    try:
        yield name
    finally:
        subprocess.run([STACK, "down", name], check=True)


@pytest.fixture
def s3(system):
    client = boto3.client(
        "s3",
        endpoint_url=ENDPOINT,
        aws_access_key_id="benchuser",
        aws_secret_access_key="benchsecret0",
        region_name="us-east-1",
        config=Config(signature_version="s3v4", s3={"addressing_style": "path"}),
    )
    # The endpoint answers before it is fully ready on some systems; retry briefly.
    for _ in range(30):
        try:
            client.list_buckets()
            break
        except Exception:
            time.sleep(1)
    return client


def test_put_get_delete_roundtrip(s3, system):
    bucket = "smoke"
    payload = b"hello from the benchmark harness"
    key = "roundtrip/object.bin"

    s3.create_bucket(Bucket=bucket)
    s3.put_object(Bucket=bucket, Key=key, Body=payload)

    got = s3.get_object(Bucket=bucket, Key=key)["Body"].read()
    assert got == payload, f"{system}: content mismatch"

    listed = s3.list_objects_v2(Bucket=bucket, Prefix="roundtrip/")
    assert [o["Key"] for o in listed.get("Contents", [])] == [key]

    s3.delete_object(Bucket=bucket, Key=key)
    with pytest.raises(ClientError) as err:
        s3.get_object(Bucket=bucket, Key=key)
    assert err.value.response["Error"]["Code"] in ("NoSuchKey", "404")
```

- [ ] **Step 8: Testi koştur, dört sistemi de yeşile çevir**

Run: `.venv/bin/pytest tests/test_smoke_roundtrip.py -v`
Expected: 4 passed (her sistem için bir parametrize)

Bu adımın işi ampiriktir. Başarısız olan her sistem için:
1. `docker compose -f compose/<system>.yaml -p bench-<system> --profile c1 logs` çıktısını oku
2. Env değişkeni adı veya komut sözdizimi yanlışsa, `docker run --rm <digest> --help` ile doğrusunu öğren
3. Compose dosyasını düzelt, tekrar koştur

RustFS ve Silo için düzeltme ihtimali en yüksektir (RustFS env adları tahminidir; Silo distroless olabilir ve healthcheck aracı bulundurmayabilir). Düzeltmeler compose dosyalarına yansıtılır ve gerekçesi yorum olarak yazılır.

- [ ] **Step 9: Commit**

```bash
git add compose/ bench/stack.sh tests/test_smoke_roundtrip.py
git commit -m "feat: config-1 compose stacks for all four systems

Per-system resource budget (6 CPU / 2048m) is enforced at the compose level,
split across containers where a system is multi-process. SeaweedFS c2 divides
the same budget across master+filer+s3 and four volume servers.
Smoke test proves put/get/list/delete on each system before benchmarking."
```

---

## Task 3: Konfig 2 (1 cihaz kaybına dayanır) ve Fault Injection Doğrulaması

Bu task, spec'in açık bıraktığı doğrulama kalemlerini kapatır. Dayanıklılık iddiasını **ampirik olarak kanıtlar** — bir cihazı gerçekten öldürüp okumanın hâlâ çalıştığını gösterir. Benchmark yazılarının neredeyse hiçbiri bunu yapmaz.

**Files:**
- Modify: `compose/rustfs.yaml` (c2 profili eklenir)
- Test: `tests/test_durability.py`

**Interfaces:**
- Consumes: `bench/stack.sh` (Task 2)
- Produces: `results/<profile_id>/durability.json` — sistem başına gözlemlenen `{fault_tolerated: bool, usable_ratio: float, mechanism: str}`. Task 7 bu dosyayı rapor tablosunda kullanır.

- [ ] **Step 1: RustFS'in EC yeteneğini CLI'dan öğren**

Run:
```bash
docker run --rm rustfs/rustfs@sha256:821d0244341a9c679b36f87d7b863e8e77cab86f712c26c6ba9ad5669f222e72 --help 2>&1 | head -60
docker run --rm --entrypoint sh rustfs/rustfs@sha256:821d0244341a9c679b36f87d7b863e8e77cab86f712c26c6ba9ad5669f222e72 -c 'env | grep -i rustfs' 2>&1 | head -30
```

Çıktıya göre karar ağacı — üç olasılık, üçünün de ne yapılacağı tanımlı:

| Bulgu | Yapılacak |
|---|---|
| Parity/EC ayarlanabilir bir bayrak/env var | 4 volume + parity 1 ile `c2` yazılır. MinIO ile birebir eşleşir. |
| Çoklu volume destekleniyor ama parity sabit | 4 volume ile `c2` yazılır, **gözlemlenen** hata toleransı ve depolama verimi Step 5'te ölçülür ve rapora o değerlerle girer. Sapma `METHODOLOGY.md`'de yazılır. |
| Çoklu volume/EC hiç yok (rc aşaması) | RustFS `c2` matrisinden ÇIKARILIR. Raporda "bu sürümde tek-cihaz dayanıklılığı sunmuyor" olarak yer alır — bu, bir eksiklik değil, **bulgunun kendisi**dir ve olgunluk değerlendirmesinin ana kanıtıdır. |

Bulguyu `compose/rustfs.yaml` içine yorum olarak, gerekçesiyle yaz.

- [ ] **Step 2: RustFS c2 profilini ekle (Step 1'in sonucuna göre)**

Step 1 birinci veya ikinci sonucu verdiyse, `compose/rustfs.yaml`'a ekle:

```yaml
  rustfs-c2:
    profiles: ["c2"]
    image: rustfs/rustfs@sha256:821d0244341a9c679b36f87d7b863e8e77cab86f712c26c6ba9ad5669f222e72
    container_name: bench-rustfs
    <<: *net
    environment:
      RUSTFS_ACCESS_KEY: benchuser
      RUSTFS_SECRET_KEY: benchsecret0
      RUSTFS_ADDRESS: ":9000"
      RUSTFS_VOLUMES: "/data0,/data1,/data2,/data3"
      # Parity setting goes here once Step 1 identifies the correct knob.
    ports: ["19000:9000"]
    volumes:
      - "rustfs-d0:/data0"
      - "rustfs-d1:/data1"
      - "rustfs-d2:/data2"
      - "rustfs-d3:/data3"
    cpus: 6.0
    mem_limit: 2048m
    healthcheck:
      <<: *s3-healthcheck
      test: ["CMD-SHELL", "curl -fsS http://localhost:9000/ -o /dev/null || exit 1"]
```

Üçüncü sonuç çıktıysa bu adım atlanır ve `tests/test_durability.py` içindeki `C2_SYSTEMS` listesinden `rustfs` çıkarılır.

- [ ] **Step 3: MinIO'nun EC:1'i kabul ettiğini doğrula**

Run:
```bash
./bench/stack.sh up minio c2
docker exec bench-minio mc alias set local http://localhost:9000 benchuser benchsecret0
docker exec bench-minio mc admin info local --json | python3 -m json.tool | head -40
./bench/stack.sh down minio
```
Expected: dört drive `online`, pool bilgisi görünür. `EC:1` reddedilirse MinIO loglarında `invalid storage class` görünür; o durumda `EC:2`'ye düşülür ve **her iki sistemde de** (MinIO ve Silo) EC:2 kullanılır, hata toleransı 2 disk olarak rapora yazılır ve SeaweedFS `001`'in 1 sunucu toleransıyla arasındaki fark `METHODOLOGY.md`'de açıkça belirtilir.

- [ ] **Step 4: Dayanıklılık testini yaz (başarısız olacak)**

`tests/test_durability.py`:
```python
"""Prove the durability claim instead of asserting it.

Each system is brought up in c2, an object is written, one device is destroyed,
and the object must still read back byte-identical. The measured outcome is
written to results/<profile_id>/durability.json and feeds the report.
"""
import json
import subprocess
import time
from pathlib import Path

import boto3
import pytest
from botocore.client import Config

ROOT = Path(__file__).resolve().parents[1]
STACK = str(ROOT / "bench" / "stack.sh")
ENDPOINT = "http://127.0.0.1:19000"
PAYLOAD = b"durability probe " * 4096  # 64 KiB, spans erasure stripes

# Systems that offer a single-device-loss config. Task 3 Step 1 may remove
# rustfs from this list if the rc build has no multi-device mode.
C2_SYSTEMS = ["minio", "silo", "rustfs", "seaweedfs"]

# How a device is destroyed differs by architecture: MinIO/Silo/RustFS spread
# one process over four mounted volumes, SeaweedFS runs four volume servers.
DEVICE_KILLERS = {
    "minio": ("wipe-volume", "bench-minio", "/data3"),
    "silo": ("wipe-volume", "bench-silo", "/data3"),
    "rustfs": ("wipe-volume", "bench-rustfs", "/data3"),
    "seaweedfs": ("stop-container", "bench-swfs-vol3", None),
}


def s3_client():
    return boto3.client(
        "s3",
        endpoint_url=ENDPOINT,
        aws_access_key_id="benchuser",
        aws_secret_access_key="benchsecret0",
        region_name="us-east-1",
        config=Config(
            signature_version="s3v4",
            s3={"addressing_style": "path"},
            retries={"max_attempts": 1},
        ),
    )


def kill_one_device(system):
    kind, target, path = DEVICE_KILLERS[system]
    if kind == "wipe-volume":
        subprocess.run(
            ["docker", "exec", target, "sh", "-c", f"rm -rf {path}/* {path}/.minio.sys 2>/dev/null || true"],
            check=False,
        )
    else:
        subprocess.run(["docker", "stop", target], check=True, capture_output=True)
    time.sleep(5)  # let the system notice


@pytest.mark.parametrize("system", C2_SYSTEMS)
def test_survives_single_device_loss(system, durability_results):
    subprocess.run([STACK, "up", system, "c2"], check=True)
    try:
        s3 = s3_client()
        for _ in range(30):
            try:
                s3.list_buckets()
                break
            except Exception:
                time.sleep(1)

        bucket, key = "durability", "probe.bin"
        s3.create_bucket(Bucket=bucket)
        s3.put_object(Bucket=bucket, Key=key, Body=PAYLOAD)
        assert s3.get_object(Bucket=bucket, Key=key)["Body"].read() == PAYLOAD

        kill_one_device(system)

        recovered = s3.get_object(Bucket=bucket, Key=key)["Body"].read()
        durability_results[system] = {
            "fault_tolerated": recovered == PAYLOAD,
            "device_killed": DEVICE_KILLERS[system][0],
        }
        assert recovered == PAYLOAD, f"{system}: lost data after one device loss"
    finally:
        subprocess.run([STACK, "down", system], check=True)
```

`tests/conftest.py` (yeni dosya) — sonuçları toplayan fixture:
```python
import json
import os
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="session")
def profile_id():
    out = subprocess.run(
        [str(ROOT / "bench" / "hwprofile.sh")], capture_output=True, text=True, check=True
    ).stdout
    return json.loads(out)["profile_id"]


@pytest.fixture(scope="session")
def durability_results(profile_id):
    collected = {}
    yield collected
    out_dir = ROOT / "results" / profile_id
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "durability.json").write_text(json.dumps(collected, indent=2) + "\n")
```

- [ ] **Step 5: Testi koştur ve gözlemlenen sonucu kaydet**

Run: `.venv/bin/pytest tests/test_durability.py -v`
Expected: `C2_SYSTEMS` içindeki her sistem için bir sonuç. **Bir sistemin başarısız olması kabul edilebilir bir bulgudur** — testi yeşile zorlamak için konfigürasyonu gevşetme. Başarısızlık gerçekse:
1. Loglardan sebebi doğrula (yetersiz parity mi, healing gecikmesi mi)
2. Healing gecikmesiyse `time.sleep` süresini artır ve tekrar koştur
3. Gerçekten dayanıksızsa `durability.json`'a `fault_tolerated: false` olarak geçir ve raporda öyle yaz

- [ ] **Step 6: Depolama verimini ölç ve kaydet**

Her sistem için `c2`'de bilinen boyutta veri yazıp diskte kapladığı yeri ölç:

```bash
#!/usr/bin/env bash
# bench/measure-usable-ratio.sh <system>
# Writes 256 MiB of incompressible data and reports logical/physical ratio.
set -euo pipefail
system="$1"
./bench/stack.sh up "$system" c2

docker run --rm --network s3bench -e AWS_ACCESS_KEY_ID=benchuser \
  -e AWS_SECRET_ACCESS_KEY=benchsecret0 \
  --entrypoint sh amazon/aws-cli:2.22.19 -c "
    dd if=/dev/urandom of=/tmp/blob bs=1M count=256 2>/dev/null
    aws --endpoint-url http://bench-${system}:9000 s3 mb s3://ratio 2>/dev/null || true
    aws --endpoint-url http://bench-${system}:9000 s3 cp /tmp/blob s3://ratio/blob
  "

physical=$(docker system df -v --format '{{json .}}' \
  | python3 -c "
import sys, json
d = json.load(sys.stdin)
total = sum(int(v.get('Size','0').rstrip('B') or 0) for v in d.get('Volumes', []) if '${system}' in v.get('Name',''))
print(total)
" 2>/dev/null || echo 0)

python3 -c "print(f'logical=268435456 physical=${physical} usable_ratio={268435456/max(int('${physical}'),1):.3f}')"
./bench/stack.sh down "$system"
```

`docker system df -v` volume boyutlarını her sürücüde raporlamayabilir. Vermezse alternatif: `docker run --rm -v <volume>:/v alpine du -sb /v` ile her volume'u tek tek ölç. Ölçülen değerleri `durability.json`'a `usable_ratio` alanı olarak ekle.

Beklenen: MinIO/Silo EC:1 → ~0.75; SeaweedFS `001` → ~0.50. **Bu fark raporun en değerli tek tablosudur**: aynı hata toleransı, farklı depolama maliyeti.

- [ ] **Step 7: Commit**

```bash
git add compose/rustfs.yaml tests/test_durability.py tests/conftest.py bench/measure-usable-ratio.sh results/
git commit -m "feat: config-2 stacks with empirical durability verification

Durability is proven by fault injection, not asserted: one device is destroyed
and the object must still read back byte-identical. Storage efficiency is
measured rather than assumed, which is what makes the EC-vs-replication
comparison meaningful at equal fault tolerance."
```

---
## Task 4: S3 Conformance Suite

Bu çalışmanın en uzun ömürlü çıktısı. Donanımdan bağımsız, yeniden koşulduğunda aynı sonucu verir, blog yazısı 1'in tamamı buradan çıkar.

**Files:**
- Create: `conformance/conftest.py`, `conformance/test_s3_conformance.py`
- Modify: `tests/conftest.py` (paylaşılan `profile_id` fixture'ı zaten var)

**Interfaces:**
- Consumes: `bench/stack.sh up <system> c1` (Task 2)
- Produces: `results/<profile_id>/conformance.json` — `{<system>: {<test_id>: {"status": "supported"|"unsupported"|"error", "detail": str}}}`. Task 7 bunu matris tablosuna çevirir.

- [ ] **Step 1: Conformance fixture'larını ve sonuç toplayıcıyı yaz**

`conformance/conftest.py`:
```python
"""Fixtures and an outcome collector for the S3 conformance matrix.

A conformance run is not pass/fail in the usual sense: an unsupported feature is
a finding, not a broken test. Every test therefore records a status rather than
only raising, and the session writes the whole matrix to JSON at the end.
"""
import json
import os
import subprocess
import time
import uuid
from pathlib import Path

import boto3
import pytest
from botocore.client import Config

ROOT = Path(__file__).resolve().parents[1]
STACK = str(ROOT / "bench" / "stack.sh")
ENDPOINT = "http://127.0.0.1:19000"
SYSTEMS = os.environ.get("CONFORMANCE_SYSTEMS", "minio,silo,rustfs,seaweedfs").split(",")

_MATRIX = {}


def pytest_addoption(parser):
    parser.addoption("--system", action="store", default=None,
                     help="run the matrix against a single system")


@pytest.fixture(scope="session")
def system(request):
    """One system per pytest session. run.sh invokes pytest once per system."""
    name = request.config.getoption("--system") or SYSTEMS[0]
    subprocess.run([STACK, "up", name, "c1"], check=True)
    try:
        yield name
    finally:
        subprocess.run([STACK, "down", name], check=True)


@pytest.fixture(scope="session")
def s3(system):
    client = boto3.client(
        "s3",
        endpoint_url=ENDPOINT,
        aws_access_key_id="benchuser",
        aws_secret_access_key="benchsecret0",
        region_name="us-east-1",
        config=Config(signature_version="s3v4", s3={"addressing_style": "path"},
                      retries={"max_attempts": 2}),
    )
    for _ in range(30):
        try:
            client.list_buckets()
            break
        except Exception:
            time.sleep(1)
    return client


@pytest.fixture
def bucket(s3):
    name = f"conf-{uuid.uuid4().hex[:12]}"
    s3.create_bucket(Bucket=name)
    yield name
    try:
        objs = s3.list_objects_v2(Bucket=name).get("Contents", [])
        if objs:
            s3.delete_objects(Bucket=name, Delete={"Objects": [{"Key": o["Key"]} for o in objs]})
        s3.delete_bucket(Bucket=name)
    except Exception:
        pass  # teardown failures must not mask findings


@pytest.fixture
def record(request, system):
    """Record a status for this test. Default status is derived from the outcome."""
    def _record(status, detail=""):
        _MATRIX.setdefault(system, {})[request.node.name] = {
            "status": status, "detail": detail,
        }
    return _record


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item, call):
    outcome = yield
    report = outcome.get_result()
    if report.when != "call":
        return
    sys_name = item.funcargs.get("system")
    if not sys_name:
        return
    entry = _MATRIX.setdefault(sys_name, {})
    if item.name in entry:
        return  # the test recorded its own status explicitly
    entry[item.name] = {
        "status": "supported" if report.passed else "unsupported",
        "detail": "" if report.passed else str(report.longrepr).splitlines()[-1][:300],
    }


def pytest_sessionfinish(session, exitstatus):
    if not _MATRIX:
        return
    profile = json.loads(subprocess.run(
        [str(ROOT / "bench" / "hwprofile.sh")], capture_output=True, text=True, check=True
    ).stdout)
    out_dir = ROOT / "results" / profile["profile_id"]
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / "conformance.json"
    existing = json.loads(path.read_text()) if path.exists() else {}
    existing.update(_MATRIX)
    path.write_text(json.dumps({"hardware_profile": profile, "matrix": existing}, indent=2) + "\n")
```

- [ ] **Step 2: Conditional write testlerini yaz (en kritik grup)**

`conformance/test_s3_conformance.py` — ilk grup. Iceberg ve Delta Lake commit protokolleri `If-None-Match: *` üzerine kuruludur; bu iki test tek başına "bu depoya lakehouse koyabilir miyim" sorusunu cevaplar.

```python
"""S3 conformance matrix.

Each test asserts AWS-documented behaviour. A failure means the system does not
implement that behaviour - which is a finding, not a bug in this suite.
"""
import io
import time

import pytest
from botocore.exceptions import ClientError

BODY = b"conformance payload"


def _code(err):
    return err.response.get("Error", {}).get("Code", "")


# --------------------------------------------------------------------------
# Conditional writes - Iceberg and Delta Lake commit protocols depend on these
# --------------------------------------------------------------------------

def test_put_if_none_match_rejects_overwrite(s3, bucket, record):
    """PutObject with If-None-Match: * must fail once the key exists."""
    key = "cond/create-once"
    s3.put_object(Bucket=bucket, Key=key, Body=BODY, IfNoneMatch="*")
    try:
        s3.put_object(Bucket=bucket, Key=key, Body=b"second write", IfNoneMatch="*")
    except ClientError as err:
        assert _code(err) in ("PreconditionFailed", "412"), f"wrong error: {_code(err)}"
        record("supported", "412 PreconditionFailed on existing key")
        return
    # No error raised: the system silently ignored the precondition and overwrote.
    # That is the dangerous failure mode - a lakehouse would lose commits here.
    body = s3.get_object(Bucket=bucket, Key=key)["Body"].read()
    record("unsupported", f"precondition ignored, object now {body!r}")
    pytest.fail("If-None-Match ignored: overwrite succeeded")


def test_put_if_none_match_allows_first_write(s3, bucket, record):
    key = "cond/first-write"
    s3.put_object(Bucket=bucket, Key=key, Body=BODY, IfNoneMatch="*")
    assert s3.get_object(Bucket=bucket, Key=key)["Body"].read() == BODY
    record("supported", "first write accepted")


def test_put_if_match_on_stale_etag_is_rejected(s3, bucket, record):
    key = "cond/if-match"
    s3.put_object(Bucket=bucket, Key=key, Body=BODY)
    s3.put_object(Bucket=bucket, Key=key, Body=b"updated")
    try:
        s3.put_object(Bucket=bucket, Key=key, Body=b"third", IfMatch='"deadbeef00000000000000000000dead"')
    except ClientError as err:
        assert _code(err) in ("PreconditionFailed", "412")
        record("supported", "stale If-Match rejected")
        return
    record("unsupported", "stale If-Match accepted")
    pytest.fail("If-Match ignored")


def test_get_if_none_match_returns_304(s3, bucket, record):
    key = "cond/get-304"
    etag = s3.put_object(Bucket=bucket, Key=key, Body=BODY)["ETag"]
    try:
        s3.get_object(Bucket=bucket, Key=key, IfNoneMatch=etag)
    except ClientError as err:
        assert err.response["ResponseMetadata"]["HTTPStatusCode"] == 304
        record("supported", "304 Not Modified")
        return
    record("unsupported", "conditional GET returned body")
    pytest.fail("If-None-Match on GET ignored")
```

- [ ] **Step 3: Versioning, Object Lock ve multipart testlerini yaz**

Aynı dosyaya ekle:

```python
# --------------------------------------------------------------------------
# Versioning
# --------------------------------------------------------------------------

def test_versioning_can_be_enabled(s3, bucket, record):
    s3.put_bucket_versioning(Bucket=bucket, VersioningConfiguration={"Status": "Enabled"})
    status = s3.get_bucket_versioning(Bucket=bucket).get("Status")
    assert status == "Enabled", f"status came back as {status!r}"
    record("supported", "bucket versioning enabled")


def test_versioning_keeps_prior_versions(s3, bucket, record):
    s3.put_bucket_versioning(Bucket=bucket, VersioningConfiguration={"Status": "Enabled"})
    key = "ver/object"
    s3.put_object(Bucket=bucket, Key=key, Body=b"v1")
    s3.put_object(Bucket=bucket, Key=key, Body=b"v2")
    versions = s3.list_object_versions(Bucket=bucket, Prefix=key).get("Versions", [])
    assert len(versions) >= 2, f"only {len(versions)} version(s) retained"
    record("supported", f"{len(versions)} versions retained")


def test_delete_creates_a_delete_marker(s3, bucket, record):
    s3.put_bucket_versioning(Bucket=bucket, VersioningConfiguration={"Status": "Enabled"})
    key = "ver/marker"
    s3.put_object(Bucket=bucket, Key=key, Body=b"v1")
    s3.delete_object(Bucket=bucket, Key=key)
    markers = s3.list_object_versions(Bucket=bucket, Prefix=key).get("DeleteMarkers", [])
    assert markers, "no delete marker created"
    record("supported", "delete marker created")


# --------------------------------------------------------------------------
# Object Lock / WORM
# --------------------------------------------------------------------------

def test_object_lock_bucket_can_be_created(s3, record):
    import uuid
    name = f"lock-{uuid.uuid4().hex[:12]}"
    try:
        s3.create_bucket(Bucket=name, ObjectLockEnabledForBucket=True)
    except ClientError as err:
        record("unsupported", f"{_code(err)}")
        pytest.fail(f"object-lock bucket rejected: {_code(err)}")
    cfg = s3.get_object_lock_configuration(Bucket=name)
    assert cfg["ObjectLockConfiguration"]["ObjectLockEnabled"] == "Enabled"
    record("supported", "object lock enabled at bucket creation")
    s3.delete_bucket(Bucket=name)


def test_retention_blocks_deletion(s3, record):
    import uuid
    from datetime import datetime, timedelta, timezone
    name = f"lock-{uuid.uuid4().hex[:12]}"
    s3.create_bucket(Bucket=name, ObjectLockEnabledForBucket=True)
    key = "worm/locked"
    until = datetime.now(timezone.utc) + timedelta(minutes=5)
    s3.put_object(Bucket=name, Key=key, Body=BODY,
                  ObjectLockMode="COMPLIANCE", ObjectLockRetainUntilDate=until)
    version = s3.list_object_versions(Bucket=name, Prefix=key)["Versions"][0]["VersionId"]
    try:
        s3.delete_object(Bucket=name, Key=key, VersionId=version)
    except ClientError as err:
        assert _code(err) in ("AccessDenied", "InvalidRequest")
        record("supported", "retained version could not be deleted")
        return
    record("unsupported", "retained version was deletable")
    pytest.fail("COMPLIANCE retention not enforced")


# --------------------------------------------------------------------------
# Multipart
# --------------------------------------------------------------------------

PART = b"x" * (5 * 1024 * 1024)  # 5 MiB, the S3 minimum for non-final parts


def test_multipart_upload_completes(s3, bucket, record):
    key = "mpu/basic"
    up = s3.create_multipart_upload(Bucket=bucket, Key=key)["UploadId"]
    parts = []
    for n in (1, 2):
        etag = s3.upload_part(Bucket=bucket, Key=key, UploadId=up, PartNumber=n, Body=PART)["ETag"]
        parts.append({"ETag": etag, "PartNumber": n})
    s3.complete_multipart_upload(Bucket=bucket, Key=key, UploadId=up,
                                 MultipartUpload={"Parts": parts})
    assert s3.head_object(Bucket=bucket, Key=key)["ContentLength"] == 2 * len(PART)
    record("supported", "2-part upload completed")


def test_multipart_accepts_out_of_order_parts(s3, bucket, record):
    key = "mpu/out-of-order"
    up = s3.create_multipart_upload(Bucket=bucket, Key=key)["UploadId"]
    etag2 = s3.upload_part(Bucket=bucket, Key=key, UploadId=up, PartNumber=2, Body=PART)["ETag"]
    etag1 = s3.upload_part(Bucket=bucket, Key=key, UploadId=up, PartNumber=1, Body=PART)["ETag"]
    s3.complete_multipart_upload(
        Bucket=bucket, Key=key, UploadId=up,
        MultipartUpload={"Parts": [{"ETag": etag1, "PartNumber": 1},
                                   {"ETag": etag2, "PartNumber": 2}]})
    assert s3.head_object(Bucket=bucket, Key=key)["ContentLength"] == 2 * len(PART)
    record("supported", "parts uploaded out of order")


def test_multipart_abort_releases_the_upload(s3, bucket, record):
    key = "mpu/abort"
    up = s3.create_multipart_upload(Bucket=bucket, Key=key)["UploadId"]
    s3.upload_part(Bucket=bucket, Key=key, UploadId=up, PartNumber=1, Body=PART)
    s3.abort_multipart_upload(Bucket=bucket, Key=key, UploadId=up)
    active = s3.list_multipart_uploads(Bucket=bucket).get("Uploads", [])
    assert up not in [u["UploadId"] for u in active]
    record("supported", "aborted upload no longer listed")
```

- [ ] **Step 4: Listeleme, presigned URL ve kalan testleri yaz**

Aynı dosyaya ekle:

```python
# --------------------------------------------------------------------------
# Listing - the lakehouse pain point
# --------------------------------------------------------------------------

def test_list_objects_v2_paginates_with_continuation_token(s3, bucket, record):
    for i in range(120):
        s3.put_object(Bucket=bucket, Key=f"page/{i:04d}", Body=b"x")
    first = s3.list_objects_v2(Bucket=bucket, Prefix="page/", MaxKeys=50)
    assert first["IsTruncated"] is True
    assert len(first["Contents"]) == 50
    token = first["NextContinuationToken"]
    second = s3.list_objects_v2(Bucket=bucket, Prefix="page/", MaxKeys=50,
                                ContinuationToken=token)
    assert len(second["Contents"]) == 50
    assert second["Contents"][0]["Key"] > first["Contents"][-1]["Key"]
    record("supported", "continuation token honoured, keys ordered")


def test_list_objects_v2_delimiter_yields_common_prefixes(s3, bucket, record):
    for key in ("a/1", "a/2", "b/1", "top"):
        s3.put_object(Bucket=bucket, Key=key, Body=b"x")
    listing = s3.list_objects_v2(Bucket=bucket, Delimiter="/")
    prefixes = sorted(p["Prefix"] for p in listing.get("CommonPrefixes", []))
    assert prefixes == ["a/", "b/"], f"got {prefixes}"
    assert [o["Key"] for o in listing.get("Contents", [])] == ["top"]
    record("supported", "delimiter rollup correct")


# --------------------------------------------------------------------------
# Presigned URLs, copy, range, tagging, encryption
# --------------------------------------------------------------------------

def test_presigned_get_url_works(s3, bucket, record):
    import urllib.request
    key = "presign/object"
    s3.put_object(Bucket=bucket, Key=key, Body=BODY)
    url = s3.generate_presigned_url("get_object",
                                    Params={"Bucket": bucket, "Key": key}, ExpiresIn=300)
    with urllib.request.urlopen(url, timeout=15) as resp:
        assert resp.read() == BODY
    record("supported", "presigned GET honoured")


def test_presigned_put_url_works(s3, bucket, record):
    import urllib.request
    key = "presign/upload"
    url = s3.generate_presigned_url("put_object",
                                    Params={"Bucket": bucket, "Key": key}, ExpiresIn=300)
    req = urllib.request.Request(url, data=BODY, method="PUT")
    with urllib.request.urlopen(req, timeout=15) as resp:
        assert resp.status in (200, 204)
    assert s3.get_object(Bucket=bucket, Key=key)["Body"].read() == BODY
    record("supported", "presigned PUT honoured")


def test_copy_object_preserves_content(s3, bucket, record):
    s3.put_object(Bucket=bucket, Key="copy/src", Body=BODY)
    s3.copy_object(Bucket=bucket, Key="copy/dst",
                   CopySource={"Bucket": bucket, "Key": "copy/src"})
    assert s3.get_object(Bucket=bucket, Key="copy/dst")["Body"].read() == BODY
    record("supported", "server-side copy")


def test_range_get_returns_the_requested_slice(s3, bucket, record):
    s3.put_object(Bucket=bucket, Key="range/object", Body=b"0123456789")
    got = s3.get_object(Bucket=bucket, Key="range/object", Range="bytes=2-5")["Body"].read()
    assert got == b"2345", f"got {got!r}"
    record("supported", "byte range honoured")


def test_object_tagging_roundtrip(s3, bucket, record):
    key = "tag/object"
    s3.put_object(Bucket=bucket, Key=key, Body=BODY)
    s3.put_object_tagging(Bucket=bucket, Key=key,
                          Tagging={"TagSet": [{"Key": "layer", "Value": "bronze"}]})
    tags = s3.get_object_tagging(Bucket=bucket, Key=key)["TagSet"]
    assert tags == [{"Key": "layer", "Value": "bronze"}]
    record("supported", "tag set roundtrip")


def test_sse_s3_encryption_is_accepted(s3, bucket, record):
    key = "sse/object"
    s3.put_object(Bucket=bucket, Key=key, Body=BODY, ServerSideEncryption="AES256")
    head = s3.head_object(Bucket=bucket, Key=key)
    assert head.get("ServerSideEncryption") == "AES256"
    record("supported", "SSE-S3 applied and reported")


def test_sse_c_encryption_roundtrip(s3, bucket, record):
    import base64, hashlib, os as _os
    key_bytes = _os.urandom(32)
    b64 = base64.b64encode(key_bytes).decode()
    md5 = base64.b64encode(hashlib.md5(key_bytes).digest()).decode()
    okey = "ssec/object"
    s3.put_object(Bucket=bucket, Key=okey, Body=BODY,
                  SSECustomerAlgorithm="AES256", SSECustomerKey=b64, SSECustomerKeyMD5=md5)
    got = s3.get_object(Bucket=bucket, Key=okey, SSECustomerAlgorithm="AES256",
                        SSECustomerKey=b64, SSECustomerKeyMD5=md5)["Body"].read()
    assert got == BODY
    record("supported", "SSE-C roundtrip")


def test_bucket_policy_can_be_set_and_read(s3, bucket, record):
    import json as _json
    policy = _json.dumps({
        "Version": "2012-10-17",
        "Statement": [{"Effect": "Allow", "Principal": {"AWS": ["*"]},
                       "Action": ["s3:GetObject"],
                       "Resource": [f"arn:aws:s3:::{bucket}/public/*"]}],
    })
    s3.put_bucket_policy(Bucket=bucket, Policy=policy)
    back = _json.loads(s3.get_bucket_policy(Bucket=bucket)["Policy"])
    assert back["Statement"][0]["Action"] == ["s3:GetObject"]
    record("supported", "bucket policy stored and returned")
```

- [ ] **Step 5: Suite'i MinIO'ya karşı koştur (referans doğrulaması)**

Run: `.venv/bin/pytest conformance/ --system minio -v`
Expected: Büyük çoğunluğu `supported`. MinIO S3 uyumluluğu en geniş olan sistemdir; burada beklenmedik bir `unsupported` çıkarsa **testin kendisi hatalıdır**, sistemin değil. Testi düzelt.

Bu adım, suite'i kalibre eder. Referans olmadan diğer üç sistemin sonucuna güvenilemez.

- [ ] **Step 6: Kalan üç sisteme karşı koştur**

Run:
```bash
for sys in silo rustfs seaweedfs; do
  .venv/bin/pytest conformance/ --system "$sys" -v || true
done
```
`|| true` gereklidir: `unsupported` bulgular pytest'i non-zero döndürür, ama bunlar başarısızlık değil bulgudur.

Expected: `results/<profile_id>/conformance.json` dört sistemin de matrisini içerir.

**Öngörülen bulgu:** SeaweedFS'in conditional write desteklemediğine dair upstream kaydı var. Bu doğrulanırsa, Iceberg/Delta için SeaweedFS'in doğrudan uygun olmadığı sonucu çıkar ve blog yazısı 1'in ana bulgusu olur. Doğrulanmazsa (sürüm 3.33'te eklenmiş olabilir) bu da kayda değer bir bulgudur — eski kaynaklara dayanan herkes yanılıyor demektir.

- [ ] **Step 7: Commit**

```bash
git add conformance/ results/
git commit -m "feat: S3 conformance matrix across four object stores

Conditional writes (If-None-Match) are tested first: Iceberg and Delta commit
protocols depend on them, and a store that silently ignores the precondition
loses commits rather than erroring. Suite is calibrated against MinIO before
the other three are trusted."
```

---

## Task 5: warp Container, Workload Tanımları ve Telemetri

**Files:**
- Create: `bench/warp/Dockerfile`, `bench/workloads.yaml`, `bench/run-workload.sh`, `bench/telemetry.sh`

**Interfaces:**
- Consumes: `bench/stack.sh` (Task 2), `images.lock` warp bloğu (Task 1)
- Produces: `bench/run-workload.sh <system> <config> <profile> <concurrency>` → `results/<profile_id>/raw/<system>__<config>__<profile>__c<concurrency>__r<round>.json` ve eşlik eden `.telemetry.csv`. `ROUND` env değişkeni ile tur numarası verilir.

- [ ] **Step 1: warp image'ını kur**

`bench/warp/Dockerfile`:
```dockerfile
# warp v1.6.1 has no published Docker image - minio/warp on Docker Hub stopped
# at v1.3.1 when MinIO ended community distribution. We build from the official
# release asset and verify its checksum, which pins us harder than a tag would.
FROM alpine:3.21

ARG WARP_VERSION=v1.6.1
ARG WARP_ASSET=warp-rdma_linux_arm64.tar.gz
ARG WARP_SHA256=7ce9f0061fe5d0d0ff809384234ddbab9f9b66a6b15e7fa0b3e0819f63ce0cfc

RUN apk add --no-cache ca-certificates curl \
 && curl -fsSL -o /tmp/warp.tgz \
      "https://github.com/minio/warp/releases/download/${WARP_VERSION}/${WARP_ASSET}" \
 && echo "${WARP_SHA256}  /tmp/warp.tgz" | sha256sum -c - \
 && tar -xzf /tmp/warp.tgz -C /tmp \
 && mv /tmp/warp /usr/local/bin/warp \
 && chmod +x /usr/local/bin/warp \
 && rm -rf /tmp/warp.tgz /tmp/* \
 && apk del curl

WORKDIR /results
ENTRYPOINT ["warp"]
```

Run:
```bash
docker build -t bench-warp:1.6.1 bench/warp
docker run --rm bench-warp:1.6.1 --version
```
Expected: `warp version 1.6.1` benzeri çıktı. Checksum uyuşmazsa build başarısız olur — istenen davranış budur.

Tarball içindeki binary yolu `warp` değilse (`tar -tzf` ile kontrol et) `mv` satırını gerçek yola göre düzelt.

- [ ] **Step 2: Workload matrisini tanımla**

`bench/workloads.yaml`:
```yaml
# Workload matrix. Object sizes are chosen to separate the claims under test:
#   small     - RustFS advertises 2.3x MinIO at 4 KiB
#   bigdata   - independent reports put MinIO ahead on ~20 MiB sequential reads
#   list      - the operation lakehouse catalogs hammer
defaults:
  duration: 45s
  warmup: 15s
  concurrency: 32
  bucket: warp-bench

profiles:
  - id: small
    op: mixed
    args: ["--obj.size=4KiB", "--objects=20000"]
  - id: medium
    op: mixed
    args: ["--obj.size=1MiB", "--objects=2000"]
  - id: bigdata-get
    op: get
    args: ["--obj.size=20MiB", "--objects=200"]
  - id: bigdata-put
    op: put
    args: ["--obj.size=20MiB"]
  - id: multipart
    op: multipart
    args: ["--obj.size=128MiB", "--parts=16"]
  - id: list
    op: list
    args: ["--objects=100000", "--obj.size=1KiB"]

# The concurrency sweep runs on one profile only. Sweeping every profile would
# push the full matrix past 10 hours on this hardware for little extra signal.
sweep:
  profile: medium
  concurrency: [8, 64]

rounds: 3
```

**Zaman bütçesi** (planlayıcı ve koşturan için):
- Çekirdek matris: 4 sistem × 2 konfig × 6 profil × 3 tur = 144 koşum
- Sweep: 4 × 2 × 1 profil × 2 concurrency × 3 tur = 48 koşum
- Toplam 192 koşum × 60 sn = 3 sa 12 dk, artı stack up/down geçiş maliyeti (~30 sn × ~64 geçiş = ~32 dk)
- **Beklenen toplam: ~4 saat.** Gece koşumu olarak planla. `--quick` modu (1 tur, sweep yok, 3 profil) geliştirme için ~25 dakikadır.

- [ ] **Step 3: Telemetri toplayıcısını yaz**

`bench/telemetry.sh`:
```bash
#!/usr/bin/env bash
# Sample docker stats once a second for the containers of one system.
# Usage: telemetry.sh <system> <output.csv>  (run in background; kill to stop)
set -euo pipefail

system="$1"
out="$2"

echo "ts,container,cpu_pct,mem_bytes,mem_limit_bytes,net_rx_bytes,net_tx_bytes,blk_read_bytes,blk_write_bytes" > "$out"

to_bytes() { # "1.5GiB" -> bytes
  python3 -c '
import re, sys
s = sys.argv[1].strip()
m = re.match(r"^([0-9.]+)\s*([A-Za-z]*)$", s)
if not m: print(0); raise SystemExit
val, unit = float(m.group(1)), m.group(2).upper()
mult = {"":1,"B":1,"KB":1e3,"KIB":1024,"MB":1e6,"MIB":1024**2,
        "GB":1e9,"GIB":1024**3,"TB":1e12,"TIB":1024**4}
print(int(val * mult.get(unit, 1)))' "$1"
}

while true; do
  ts="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  while IFS='|' read -r name cpu mem net blk; do
    [[ "$name" == *"$system"* || "$name" == bench-swfs-* ]] || continue
    memused="${mem%% /*}"; memlimit="${mem##*/ }"
    rx="${net%% /*}"; tx="${net##*/ }"
    br="${blk%% /*}"; bw="${blk##*/ }"
    printf '%s,%s,%s,%s,%s,%s,%s,%s,%s\n' \
      "$ts" "$name" "${cpu%\%}" \
      "$(to_bytes "$memused")" "$(to_bytes "$memlimit")" \
      "$(to_bytes "$rx")" "$(to_bytes "$tx")" \
      "$(to_bytes "$br")" "$(to_bytes "$bw")" >> "$out"
  done < <(docker stats --no-stream --format '{{.Name}}|{{.CPUPerc}}|{{.MemUsage}}|{{.NetIO}}|{{.BlockIO}}')
  sleep 1
done
```

- [ ] **Step 4: Tek koşum script'ini yaz**

`bench/run-workload.sh`:
```bash
#!/usr/bin/env bash
# Run one (system, config, profile, concurrency) measurement and emit JSON.
# Assumes the system is already up via stack.sh. Usage:
#   ROUND=1 run-workload.sh <system> <config> <profile-id> <concurrency>
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
system="$1"; config="$2"; profile_id="$3"; concurrency="$4"
round="${ROUND:-1}"

hw="$("$ROOT/bench/hwprofile.sh")"
pid="$(printf '%s' "$hw" | python3 -c 'import sys,json; print(json.load(sys.stdin)["profile_id"])')"
raw="$ROOT/results/$pid/raw"
mkdir -p "$raw"

stem="${system}__${config}__${profile_id}__c${concurrency}__r${round}"
telemetry="$raw/$stem.telemetry.csv"
benchdata="/results/$stem"

# Read the profile definition out of workloads.yaml.
read -r op args duration warmup bucket < <(python3 - "$ROOT/bench/workloads.yaml" "$profile_id" <<'PY'
import sys, yaml
cfg = yaml.safe_load(open(sys.argv[1]))
d = cfg["defaults"]
p = next(p for p in cfg["profiles"] if p["id"] == sys.argv[2])
print(p["op"], "\x1f".join(p["args"]), d["duration"], d["warmup"], d["bucket"])
PY
)
IFS=$'\x1f' read -r -a warp_args <<< "$args"

"$ROOT/bench/telemetry.sh" "$system" "$telemetry" &
telemetry_pid=$!
trap 'kill "$telemetry_pid" 2>/dev/null || true' EXIT

docker run --rm --network s3bench \
  -v "$raw:/results" \
  bench-warp:1.6.1 \
  "$op" \
  --host="bench-${system}:9000" \
  --access-key=benchuser \
  --secret-key=benchsecret0 \
  --bucket="$bucket" \
  --concurrent="$concurrency" \
  --duration="$duration" \
  --warp-client= \
  --benchdata="$benchdata" \
  --noclear=false \
  "${warp_args[@]}"

kill "$telemetry_pid" 2>/dev/null || true
trap - EXIT

# warp writes <benchdata>.csv.zst; analyze it into JSON.
docker run --rm -v "$raw:/results" bench-warp:1.6.1 \
  analyze "/results/$stem.csv.zst" --json > "$raw/$stem.warp.json"

python3 - "$raw/$stem.warp.json" "$raw/$stem.json" "$telemetry" <<PY
import json, sys, statistics, csv
warp_json, out_path, telemetry_path = sys.argv[1], sys.argv[2], sys.argv[3]
warp = json.load(open(warp_json))

cpu, mem = [], []
with open(telemetry_path) as fh:
    for row in csv.DictReader(fh):
        try:
            cpu.append(float(row["cpu_pct"])); mem.append(int(row["mem_bytes"]))
        except (ValueError, KeyError):
            pass

json.dump({
    "hardware_profile": json.loads('''$hw'''),
    "run": {
        "system": "$system", "config": "$config", "profile": "$profile_id",
        "concurrency": $concurrency, "round": $round,
    },
    "warp": warp,
    "telemetry": {
        "samples": len(cpu),
        "cpu_pct_mean": statistics.mean(cpu) if cpu else None,
        "cpu_pct_max": max(cpu) if cpu else None,
        "mem_bytes_mean": statistics.mean(mem) if mem else None,
        "mem_bytes_peak": max(mem) if mem else None,
    },
}, open(out_path, "w"), indent=2)
print(out_path)
PY
```

- [ ] **Step 5: Tek koşumu uçtan uca doğrula**

Run:
```bash
chmod +x bench/telemetry.sh bench/run-workload.sh
./bench/stack.sh up minio c1
ROUND=1 ./bench/run-workload.sh minio c1 medium 32
./bench/stack.sh down minio
```
Expected: `results/apple-m1-8gb-darwin/raw/minio__c1__medium__c32__r1.json` oluşur; içinde `warp` analiz bloğu, sıfır olmayan `telemetry.samples`, ve gömülü `hardware_profile` bulunur.

warp bayrakları uyuşmazsa (`--warp-client=` veya `--noclear` sözdizimi sürümle değişmiş olabilir) `docker run --rm bench-warp:1.6.1 <op> --help` çıktısına göre düzelt ve `bench/run-workload.sh`'i güncelle.

- [ ] **Step 6: Commit**

```bash
git add bench/warp/ bench/workloads.yaml bench/run-workload.sh bench/telemetry.sh results/
git commit -m "feat: warp container, workload matrix, per-run telemetry

warp is built from the v1.6.1 release asset with checksum verification because
no Docker image exists past v1.3.1. Each run emits warp analysis plus CPU/memory
telemetry with the hardware profile embedded."
```

---

## Task 6: Orkestratör

**Files:**
- Create: `bench/run.sh`

**Interfaces:**
- Consumes: `bench/stack.sh`, `bench/run-workload.sh`, `bench/workloads.yaml`
- Produces: `results/<profile_id>/raw/` altında tam matris; `results/<profile_id>/run.log`

- [ ] **Step 1: Orkestratörü yaz**

`bench/run.sh`:
```bash
#!/usr/bin/env bash
# Full benchmark matrix, round-robin across systems.
#
# Round-robin matters: running one system's whole matrix before the next lets
# thermal throttling accumulate against whoever runs last. Cycling systems each
# round spreads that penalty evenly, and taking the median across rounds removes
# most of what is left.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SYSTEMS=(minio silo rustfs seaweedfs)
CONFIGS=(c1 c2)
MIN_FREE_GB=5

QUICK=0
[[ "${1:-}" == "--quick" ]] && QUICK=1

hw="$("$ROOT/bench/hwprofile.sh")"
pid="$(printf '%s' "$hw" | python3 -c 'import sys,json; print(json.load(sys.stdin)["profile_id"])')"
mkdir -p "$ROOT/results/$pid"
log="$ROOT/results/$pid/run.log"
exec > >(tee -a "$log") 2>&1

echo "=== benchmark run: profile=$pid quick=$QUICK started=$(date -u +%FT%TZ) ==="

read -r rounds profiles sweep_profile sweep_concurrency default_concurrency \
  < <(python3 - "$ROOT/bench/workloads.yaml" "$QUICK" <<'PY'
import sys, yaml
cfg = yaml.safe_load(open(sys.argv[1]))
quick = sys.argv[2] == "1"
ids = [p["id"] for p in cfg["profiles"]]
if quick:
    ids = ids[:3]
print(1 if quick else cfg["rounds"],
      ",".join(ids),
      "" if quick else cfg["sweep"]["profile"],
      "" if quick else ",".join(str(c) for c in cfg["sweep"]["concurrency"]),
      cfg["defaults"]["concurrency"])
PY
)

free_gb() {
  case "$(uname -s)" in
    Darwin) echo $(( $(df -k /System/Volumes/Data | awk 'NR==2 {print $4}') / 1024 / 1024 )) ;;
    *)      echo $(( $(df -k / | awk 'NR==2 {print $4}') / 1024 / 1024 )) ;;
  esac
}

guard_disk() {
  local free; free="$(free_gb)"
  if (( free < MIN_FREE_GB )); then
    echo "ABORT: only ${free}GB free on host, need ${MIN_FREE_GB}GB" >&2
    exit 1
  fi
  echo "-- disk ok: ${free}GB free"
}

reclaim() {
  docker image prune -f >/dev/null 2>&1 || true
  docker builder prune -f >/dev/null 2>&1 || true
}

for round in $(seq 1 "$rounds"); do
  for system in "${SYSTEMS[@]}"; do
    for config in "${CONFIGS[@]}"; do
      guard_disk
      echo "== round=$round system=$system config=$config =="

      if ! "$ROOT/bench/stack.sh" up "$system" "$config"; then
        echo "!! $system/$config failed to start - skipping (recorded as unsupported config)"
        "$ROOT/bench/stack.sh" down "$system" || true
        continue
      fi

      IFS=',' read -r -a profile_ids <<< "$profiles"
      for profile in "${profile_ids[@]}"; do
        echo "-- $system/$config/$profile @c${default_concurrency} r${round}"
        ROUND="$round" "$ROOT/bench/run-workload.sh" \
          "$system" "$config" "$profile" "$default_concurrency" || \
          echo "!! workload failed: $system/$config/$profile"
      done

      if [[ -n "$sweep_profile" ]]; then
        IFS=',' read -r -a sweep_c <<< "$sweep_concurrency"
        for c in "${sweep_c[@]}"; do
          echo "-- sweep $system/$config/$sweep_profile @c${c} r${round}"
          ROUND="$round" "$ROOT/bench/run-workload.sh" \
            "$system" "$config" "$sweep_profile" "$c" || \
            echo "!! sweep failed: $system/$config@c$c"
        done
      fi

      "$ROOT/bench/stack.sh" down "$system"
      reclaim
    done
  done
done

echo "=== finished $(date -u +%FT%TZ) ==="
echo "raw results: results/$pid/raw/"
```

- [ ] **Step 2: Quick modda uçtan uca doğrula**

Run: `chmod +x bench/run.sh && ./bench/run.sh --quick`
Expected: ~25 dakika. `results/<profile_id>/raw/` altında 4 sistem × 2 konfig × 3 profil = 24 JSON dosyası (bir sistem `c2`'yi desteklemiyorsa daha az). `run.log` her adımı kaydeder.

Kısmi başarısızlıklar tolere edilir ve loglanır — bir sistemin bir konfigde ayağa kalkamaması koşumu durdurmaz, rapora bulgu olarak girer.

- [ ] **Step 3: Tam matrisi koştur**

Run: `./bench/run.sh`
Expected: ~4 saat. Gece koşumu. Bitiminde 192'ye yakın JSON dosyası.

Koşum sırasında makineyi başka iş için kullanma — arka plan yükü tüm ölçümleri kirletir. Bu, `METHODOLOGY.md`'de yazılacak bir gerekliliktir.

- [ ] **Step 4: Commit**

```bash
git add bench/run.sh results/
git commit -m "feat: benchmark orchestrator with round-robin scheduling

Systems cycle each round rather than running their full matrix back to back,
so thermal throttling is distributed instead of penalising whoever runs last.
Aborts when host free space drops under 5GB - the Docker.raw sparse file will
otherwise fill the disk silently."
```

---

## Task 7: Analiz ve RESULTS.md

**Files:**
- Create: `bench/analyze.py`
- Create: `docs/RESULTS.md` (üretilir)

**Interfaces:**
- Consumes: `results/<profile_id>/raw/*.json`, `results/<profile_id>/conformance.json`, `results/<profile_id>/durability.json`
- Produces: `docs/RESULTS.md`, `results/<profile_id>/summary.json`

- [ ] **Step 1: Analiz script'ini yaz**

`bench/analyze.py`:
```python
#!/usr/bin/env python3
"""Turn raw run JSON into medians, percentiles and markdown tables.

Aggregation rule: take the median across rounds, never the mean. One slow round
from a background process on the host would drag a mean; the median ignores it.
"""
import argparse
import json
import statistics
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SYSTEM_LABEL = {
    "minio": "MinIO (baseline)",
    "silo": "Silo",
    "rustfs": "RustFS",
    "seaweedfs": "SeaweedFS",
}


def load_runs(raw_dir):
    runs = []
    for path in sorted(raw_dir.glob("*.json")):
        if path.name.endswith(".warp.json"):
            continue
        try:
            runs.append(json.loads(path.read_text()))
        except json.JSONDecodeError:
            print(f"skipping unreadable {path.name}")
    return runs


def extract_throughput(warp_block):
    """Pull MiB/s and ops/s out of warp's analysis JSON.

    warp nests its numbers under an operations list; shapes differ slightly
    between operation types, so read defensively and return None when absent.
    """
    ops = warp_block.get("operations") or []
    if not ops:
        return None, None
    op = ops[0]
    throughput = op.get("throughput", {})
    mib = throughput.get("average_bps")
    mib = mib / (1024 * 1024) if mib else None
    ops_per_s = throughput.get("average_ops")
    return mib, ops_per_s


def extract_ttfb(warp_block):
    ops = warp_block.get("operations") or []
    if not ops:
        return {}
    ttfb = ops[0].get("throughput", {}).get("ttfb") or ops[0].get("ttfb") or {}
    return {
        "p50": ttfb.get("median_millis"),
        "p95": ttfb.get("p95_millis"),
        "p99": ttfb.get("p99_millis"),
    }


def aggregate(runs):
    buckets = defaultdict(list)
    for r in runs:
        meta = r["run"]
        key = (meta["system"], meta["config"], meta["profile"], meta["concurrency"])
        mib, ops = extract_throughput(r.get("warp", {}))
        buckets[key].append({
            "mib_s": mib,
            "ops_s": ops,
            "ttfb": extract_ttfb(r.get("warp", {})),
            "cpu_pct_mean": r.get("telemetry", {}).get("cpu_pct_mean"),
            "mem_bytes_peak": r.get("telemetry", {}).get("mem_bytes_peak"),
        })

    summary = {}
    for key, samples in buckets.items():
        def med(field):
            vals = [s[field] for s in samples if s.get(field) is not None]
            return statistics.median(vals) if vals else None

        def med_ttfb(p):
            vals = [s["ttfb"].get(p) for s in samples if s["ttfb"].get(p) is not None]
            return statistics.median(vals) if vals else None

        mib = med("mib_s")
        cpu = med("cpu_pct_mean")
        summary["|".join(str(k) for k in key)] = {
            "rounds": len(samples),
            "mib_s_median": mib,
            "ops_s_median": med("ops_s"),
            "ttfb_p50_ms": med_ttfb("p50"),
            "ttfb_p95_ms": med_ttfb("p95"),
            "ttfb_p99_ms": med_ttfb("p99"),
            "cpu_pct_mean": cpu,
            "mem_bytes_peak": med("mem_bytes_peak"),
            # Efficiency is the metric that survives a constrained box: raw
            # throughput is capped by the VM, throughput per unit CPU is not.
            "mib_s_per_cpu_pct": (mib / cpu) if (mib and cpu) else None,
        }
    return summary


def md_table(rows, headers):
    out = ["| " + " | ".join(headers) + " |",
           "|" + "|".join(["---"] * len(headers)) + "|"]
    for row in rows:
        out.append("| " + " | ".join("-" if c is None else str(c) for c in row) + " |")
    return "\n".join(out)


def fmt(value, digits=1):
    return "-" if value is None else f"{value:,.{digits}f}"


def render(summary, conformance, durability, hw):
    parts = [
        "# Results",
        "",
        "> **Read these numbers as comparative, not absolute.** They come from a "
        "single node inside a macOS VM. Absolute throughput does not transfer to "
        "other hardware; the ranking and the efficiency ratios are what carry.",
        "",
        "## Hardware profile",
        "",
        "```json",
        json.dumps(hw, indent=2),
        "```",
        "",
        "## Durability configuration, as measured",
        "",
    ]

    dur_rows = []
    for system, entry in sorted(durability.items()):
        dur_rows.append([
            SYSTEM_LABEL.get(system, system),
            "yes" if entry.get("fault_tolerated") else "NO",
            fmt(entry.get("usable_ratio"), 2),
            entry.get("mechanism", "-"),
        ])
    parts += [
        md_table(dur_rows, ["System", "Survived 1 device loss", "Usable ratio", "Mechanism"]),
        "",
        "Fault tolerance is the axis held equal across systems; storage efficiency "
        "is reported as a result. That is the honest way to compare erasure coding "
        "against replication - you cannot hold both constant at once.",
        "",
        "## Throughput by profile (median of rounds, concurrency 32)",
        "",
    ]

    profiles = sorted({k.split("|")[2] for k in summary})
    for config in ("c1", "c2"):
        rows = []
        for profile in profiles:
            for system in ("minio", "silo", "rustfs", "seaweedfs"):
                key = f"{system}|{config}|{profile}|32"
                if key not in summary:
                    continue
                s = summary[key]
                rows.append([
                    profile, SYSTEM_LABEL.get(system, system),
                    fmt(s["mib_s_median"]), fmt(s["ops_s_median"], 0),
                    fmt(s["ttfb_p50_ms"], 2), fmt(s["ttfb_p95_ms"], 2),
                    fmt(s["ttfb_p99_ms"], 2),
                    fmt(s["mib_s_per_cpu_pct"], 3),
                ])
        if rows:
            label = "no redundancy" if config == "c1" else "survives 1 device loss"
            parts += [f"### Config {config} ({label})", "",
                      md_table(rows, ["Profile", "System", "MiB/s", "ops/s",
                                      "TTFB p50 ms", "p95", "p99", "MiB/s per CPU%"]),
                      ""]

    parts += ["## S3 conformance matrix", ""]
    matrix = conformance.get("matrix", {})
    test_ids = sorted({t for sys_tests in matrix.values() for t in sys_tests})
    systems = [s for s in ("minio", "silo", "rustfs", "seaweedfs") if s in matrix]
    glyph = {"supported": "yes", "unsupported": "NO", "error": "err"}
    rows = []
    for test_id in test_ids:
        row = [test_id.replace("test_", "").replace("_", " ")]
        for system in systems:
            row.append(glyph.get(matrix.get(system, {}).get(test_id, {}).get("status"), "-"))
        rows.append(row)
    parts += [md_table(rows, ["Behaviour"] + [SYSTEM_LABEL.get(s, s) for s in systems]), ""]

    return "\n".join(parts) + "\n"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--profile-id", required=True)
    args = ap.parse_args()

    base = ROOT / "results" / args.profile_id
    runs = load_runs(base / "raw")
    if not runs:
        raise SystemExit(f"no runs found under {base / 'raw'}")

    summary = aggregate(runs)
    (base / "summary.json").write_text(json.dumps(summary, indent=2) + "\n")

    conformance_path = base / "conformance.json"
    conformance = json.loads(conformance_path.read_text()) if conformance_path.exists() else {}
    durability_path = base / "durability.json"
    durability = json.loads(durability_path.read_text()) if durability_path.exists() else {}

    out = ROOT / "docs" / "RESULTS.md"
    out.write_text(render(summary, conformance, durability, runs[0]["hardware_profile"]))
    print(f"wrote {out} from {len(runs)} runs")


if __name__ == "__main__":
    main()
```

- [ ] **Step 2: Analizi koştur ve warp JSON şemasını doğrula**

Run: `.venv/bin/python bench/analyze.py --profile-id apple-m1-8gb-darwin`
Expected: `docs/RESULTS.md` üretilir, tablolarda `-` değil gerçek sayılar bulunur.

Throughput sütunları `-` çıkarsa `extract_throughput` warp'ın gerçek JSON şemasıyla uyuşmuyor demektir. Şemayı gör:
```bash
python3 -m json.tool "results/apple-m1-8gb-darwin/raw/minio__c1__medium__c32__r1.warp.json" | head -60
```
`extract_throughput` ve `extract_ttfb`'yi gördüğün gerçek anahtarlara göre düzelt. Bu, warp sürümüne bağlı tek kırılgan noktadır.

- [ ] **Step 3: Commit**

```bash
git add bench/analyze.py docs/RESULTS.md results/
git commit -m "feat: aggregate raw runs into medians and markdown tables

Median across rounds, never mean: one round disturbed by host background work
would drag a mean. Reports MiB/s per CPU% alongside raw throughput, because on
a VM-capped box efficiency carries signal that absolute throughput does not."
```

---

## Task 8: Public Dokümanlar

**Files:**
- Create: `README.md`, `METHODOLOGY.md`, `CONTRIBUTING.md`

**Interfaces:**
- Consumes: Task 1-7 boyunca öğrenilen gerçek davranışlar. Bu task **koşum tamamlandıktan sonra** yazılır — varsayımlar değil, gözlemler belgelenir.

- [ ] **Step 1: README.md yaz**

İlk ekranda bulunması zorunlu olanlar, bu sırayla:
1. Bir cümlelik ne olduğu
2. **Ne ölçmediği** — tek node, VM içi, mutlak throughput taşınmaz; Ceph RGW ve Ozone kapsam dışı ve neden
3. Test edilen sistemler ve pinlenmiş digest'leri
4. Tek komutluk reprodüksiyon: `./bench/run.sh`
5. Sonuçlara ve `METHODOLOGY.md`'ye link
6. Lisans notu: harness Apache-2.0; MinIO/Silo AGPL'i S3 API üzerinden çağrı yapıldığı için bulaşmaz

Kısıtlar dipnota konmaz. Public bir benchmark'ta metodoloji şeffaflığı, sonuçların kendisinden daha değerlidir.

- [ ] **Step 2: METHODOLOGY.md yaz**

Bölümler:
- **Adalet kontrolleri** — altı maddenin her biri ve *neden* gerekli olduğu (sistem başına bütçe, round-robin termal dağıtım, warp'ın VM içinde koşması ve gvisor tuzağı, warmup atma, medyan tercihi, digest pinleme)
- **Dayanıklılık ekseni kararı** — hata toleransı eşitlenir, depolama verimi raporlanır; neden ikisi birden eşitlenemez
- **Ölçülen dayanıklılık** — fault injection prosedürü ve gözlemlenen sonuçlar
- **Bilinen limitler** — 3.9 GB VM RAM tavanı, tek node, macOS VM disk katmanı, gvisor, termal throttling
- **warp'ın tarafsızlığı** — MinIO kökenli olduğu açıkça yazılır; saf S3 API konuştuğu ve sonuçların spot doğrulandığı belirtilir
- **Task 2 Step 8 ve Task 3 Step 1'de yapılan sapmalar** — hangi sistemde hangi konfigürasyon düzeltmesi yapıldı ve neden

- [ ] **Step 3: CONTRIBUTING.md yaz**

Ana mekanizma: başkalarının kendi donanımında koşup sonuç PR'lamasi.

```markdown
## Submitting results from your hardware

1. `./bench/lock-images.sh` - confirms upstream has not re-pointed a pinned tag
2. `./bench/run.sh` - about four hours; do not use the machine while it runs
3. `.venv/bin/python bench/analyze.py --profile-id <your-profile-id>`
4. Open a PR containing only `results/<your-profile-id>/`

Your profile id is derived automatically from your CPU, RAM and OS. Override it
with `PROFILE_ID=...` if the derived one collides with an existing directory.

We accept results that disagree with ours. A number that contradicts the
published table is more useful than one that confirms it - open the PR and say
what differed.
```

Son paragraf bilinçlidir: public bir benchmark'ın güvenilirliği, çelişen veriyi kabul edip etmediğiyle ölçülür.

- [ ] **Step 4: Commit**

```bash
git add README.md METHODOLOGY.md CONTRIBUTING.md
git commit -m "docs: public README, methodology and contribution guide

Limitations sit on the first screen rather than in a footnote. CONTRIBUTING
turns a single-laptop measurement into a multi-hardware dataset by inviting
result PRs, including contradicting ones."
```

---

## Task 9: Blog Taslakları

**Files:**
- Create: `docs/blog-1-conformance.md`, `docs/blog-2-methodology.md`

**Interfaces:**
- Consumes: `docs/RESULTS.md`, `results/<profile_id>/conformance.json`, `results/<profile_id>/durability.json`
- Bu task, gerçek sonuçlar üretildikten SONRA yapılır. Sayı uydurulmaz.

- [ ] **Step 1: Blog 1 — conformance yazısını yaz (Türkçe)**

`docs/blog-1-conformance.md`. Başlık yönü: "Hangi S3 alternatifine Iceberg koyabilirsin".

İskelet:
1. **Giriş** — MinIO arşivlendi, ekipler taşınıyor, ama herkes throughput'a bakıyor; oysa lakehouse için belirleyici olan API bütünlüğü
2. **Neden conditional write** — Iceberg ve Delta commit protokolünün `If-None-Match: *`'a nasıl dayandığı; desteklenmediğinde ne olduğu (sessizce üzerine yazma = kayıp commit, hata bile almıyorsun)
3. **Matris** — `RESULTS.md`'den conformance tablosu, gözlemlenen davranış notlarıyla
4. **Bulgular** — sistem başına ne çalışıyor ne çalışmıyor, karar için ne anlama geliyor
5. **Nasıl test ettim** — repo linki, tek komut, "kendi sürümünde koştur"
6. **Kısıtlar** — hangi sürümler, hangi tarih, neyin test edilmediği

Kural: bu yazıdaki her iddia `conformance.json`'daki bir satıra dayanır. Test edilmemiş hiçbir şey iddia edilmez.

- [ ] **Step 2: Blog 2 — metodoloji ve performans yazısını yaz (Türkçe)**

`docs/blog-2-methodology.md`. Başlık yönü: "8 GB'lık bir MacBook'ta dört S3 deposu: neyi ölçebilirsin, neyi ölçemezsin".

İskelet:
1. **Giriş** — dolaşımdaki karşılaştırmaların ortak sorunu: tool, obje boyutu, node sayısı ve dayanıklılık konfigürasyonu yazılmadan verilen rakamlar
2. **Dayanıklılık tuzağı** — EC'yi replication'a karşı kıyaslarken iki ekseni birden eşitleyemezsin; hata toleransını eşitleyip depolama verimini raporlama tercihi ve ölçülen sonuçlar
3. **Kısıtlı donanımda üç tuzak** — gvisor ağ katmanı (warp'ı host'tan koşturursan Docker Desktop'ı ölçersin), termal throttling (round-robin çözümü), container başına vs sistem başına kaynak bütçesi (SeaweedFS 5 container'a bölünüyor)
4. **Sonuçlar** — `RESULTS.md` tabloları, MiB/s per CPU% dahil
5. **MinIO vs Silo differential** — aynı kod tabanı, fork drift performans bedeli getirdi mi
6. **Neyi ölçemedim** — Ceph RGW ve Ozone neden kapsam dışı, çok node'da ne değişir
7. **Repo ve davet** — kendi donanımında koştur, çelişen sonuç da kabul

- [ ] **Step 3: Commit**

```bash
git add docs/blog-1-conformance.md docs/blog-2-methodology.md
git commit -m "docs: two blog drafts - conformance matrix and benchmark methodology

Every claim traces to a row in conformance.json or RESULTS.md; nothing is
asserted that was not measured."
```

- [ ] **Step 4: Yayın öncesi son kontrol**

```bash
./bench/lock-images.sh          # digest drift yok mu
.venv/bin/pytest tests/ -v      # harness testleri geçiyor mu
git log --oneline               # commit geçmişi temiz mi
```

**Uzak repo oluşturma ve push, kullanıcının açık onayı olmadan YAPILMAZ.** Yayın kararı ve zamanlaması kullanıcıya aittir.

---

## Self-Review Notları

**Spec kapsam kontrolü** — spec'in her bölümü bir task'a bağlanıyor:

| Spec bölümü | Task |
|---|---|
| §2.1 Ölçülen sistemler | Task 1 (`images.lock`), Task 2 (compose) |
| §2.2 Ceph/Ozone kapsam dışı | Task 8 (README), Task 9 (blog 2 §6) |
| §2.3 Silo differential | Task 2, Task 9 (blog 2 §5) |
| §3 Test ortamı | Task 1 (`hwprofile.sh`) |
| §4 Adalet kontrolleri (8 madde) | Task 2 (1,2), Task 5 (3), Task 6 (4,6), Task 5 (5), Task 7 (7), Task 1 (8) |
| §5 Dayanıklılık matrisi | Task 3 |
| §6 Workload matrisi | Task 5 (`workloads.yaml`), Task 6 |
| §7 Telemetri | Task 5 (`telemetry.sh`), Task 7 (türetilen metrikler) |
| §8 Conformance suite | Task 4 |
| §9 Repo yapısı | Task 1, Task 8 |
| §10 Blog yazıları | Task 9 |
| §11 Kısıtlar | Task 7 (RESULTS.md başlığı), Task 8 (README, METHODOLOGY) |
| §12 Yayın akışı | Task 9 Step 4 |
| §13 Başarı kriterleri | Task 6 Step 3, Task 4 Step 6, Task 8 Step 3 |

Boşluk yok.

**Bilinen kırılgan noktalar** — bunlar placeholder değil, ampirik doğrulama gerektiren ve karar ağacı tanımlı adımlardır:
- Task 2 Step 8: RustFS env adları ve Silo healthcheck aracı ampirik düzeltme gerektirebilir
- Task 3 Step 1: RustFS EC yeteneği üç olası sonuçtan biriyle çözülür, üçünün de eylemi tanımlı
- Task 3 Step 3: MinIO `EC:1` reddederse `EC:2` fallback'i ve raporlama sonucu tanımlı
- Task 5 Step 5: warp bayrak sözdizimi sürüme bağlı
- Task 7 Step 2: warp analiz JSON şeması `extract_throughput` ile uyuşmalı

**İsim tutarlılığı** — task'lar arası kullanılan sabitler:
`bench-<system>` container adları · `s3bench` network · `benchuser`/`benchsecret0` kimlik bilgileri · `9000` container içi S3 portu · `19000` host portu · `c1`/`c2` konfig adları · `results/<profile_id>/raw/<system>__<config>__<profile>__c<concurrency>__r<round>.json` dosya adı şeması. Tüm task'larda aynı.
