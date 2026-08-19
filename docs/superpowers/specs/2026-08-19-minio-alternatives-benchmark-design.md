# MinIO Alternatifleri: Benchmark ve Uyumluluk Araştırması — Tasarım

**Tarih:** 2026-08-19
**Durum:** Tasarım onayı bekliyor
**Çıktılar:** Açık kaynak benchmark harness (GitHub) + iki teknik blog yazısı

---

## 1. Bağlam ve Motivasyon

MinIO Community Edition yaşam döngüsünü tamamladı:

| Tarih | Olay |
|---|---|
| 2025-10-15 | Son community release (`RELEASE.2025-10-15T17-29-55Z`), bir privilege-escalation CVE fix'i. Aynı gün Docker Hub/quay.io image yayını durdu. |
| 2025-12 | README'ye "maintenance mode" commit'i. |
| 2026-04-25 | `minio/minio` repo'su arşivlendi. Read-only, AGPLv3 kaynak duruyor, patch yok. |

Big data ekipleri ve self-host kullanıcıları alternatif arıyor. Dolaşımdaki karşılaştırmaların çoğu metodolojisiz: benchmark aracı, obje boyutu dağılımı, node sayısı ve dayanıklılık konfigürasyonu belirtilmeden throughput rakamı veriliyor. Bu çalışmanın amacı, **yeniden üretilebilir** bir ölçüm ortaya koymak.

## 2. Kapsam

### 2.1 Ölçüm kapsamındaki sistemler

| Sistem | Image | Lisans | Konum |
|---|---|---|---|
| MinIO (baseline) | `alpine/minio:RELEASE.2025-10-15T17-29-55Z` | AGPL-3.0 | Son community stable. Referans noktası. |
| Silo (PGSTY) | `pgsty/silo:latest-arm64` | AGPL-3.0 | MinIO fork'u. Drop-in; console ve CVE fix'leri sürüyor. |
| RustFS | `rustfs/rustfs:rc` | Apache-2.0 | Rust, 31k★. En agresif yeni rakip. |
| SeaweedFS | `chrislusf/seaweedfs:v3.33` | Apache-2.0 | Go, 34k★, 2012'den beri. Olgun default. |

Dördünün de native `linux/arm64` image'ı doğrulandı — emülasyon yok.

### 2.2 Ölçüm kapsamı DIŞI (kağıt üzerinde değerlendirilir)

**Ceph RGW** ve **Apache Ozone**. Her ikisi de çok node'lu çalışmak için tasarlandı; 3.9 GB RAM'li tek node'da alacakları sonuçlar mimarilerini değil kısıtı ölçer. Raporda niteliksel bölüm olarak yer alırlar; sayısal karşılaştırmaya sokulmazlar. Gerçek ölçümleri için 3-5 node'luk bir cloud fazı gerekir (Faz 2, bu spec'in dışında).

### 2.3 Neden Silo ölçülüyor

Silo MinIO'nun fork'u, aynı kod tabanı — performansları eşit çıkmalı. Ölçümün amacı bir yarış değil bir **differential test**: fork drift'i performans bedeli getiriyor mu? Drop-in olduğu için ölçüm maliyeti sıfıra yakın, bulgunun değeri yüksek.

## 3. Test Ortamı

```
Host:            Apple M1, 8 çekirdek, 8 GB RAM, macOS (darwin 25.4.0)
Container:       Docker Desktop 29.1.3
Docker VM:       8 CPU, 3919 MiB RAM (~3.4 GB kullanılabilir), aarch64
Ağ:              gvisor (host↔VM). Container↔container VM kernel'inde kalır.
Host boş disk:   ~21 GB  ← gerçek tavan. Docker.raw sparse; VM'in gördüğü 193 GB yanıltıcı.
```

**Disk yönetimi zorunlu.** Koşum öncesi `docker system prune` (şu an ~27 GB geri kazanılabilir), koşumlar arası volume temizliği. Toplam yazılan veri konfig başına ≤ 6 GB tutulur.

## 4. Adalet Kontrolleri

Bu benchmark'ın değeri buradan gelir. Her biri sonucu doğrudan etkiler:

1. **Eşit kaynak limiti — sistem başına, container başına değil.** Her sistemin tüm container seti toplamda `cpus: 6` ve `mem_limit: 2g` bütçesini paylaşır. Bu ayrım kritik: SeaweedFS Konfig 2'de master + filer + 4 volume server olarak koşar; her container'a ayrı ayrı 2 GB vermek ona 8+ GB verip MinIO'ya 2 GB vermek olurdu. Bütçe compose seviyesinde sistem toplamına uygulanır. Kalan RAM warp client ve VM overhead'i için.
2. **Eşit depolama düzeni.** Konfig 1'de tek volume, Konfig 2'de 4 ayrı volume — her sistem için aynı sayıda. Hepsi aynı VM diskinde, aynı dosya sisteminde.
3. **warp VM içinde container olarak koşar**, servisle aynı user-defined bridge network'te. Host'tan koşturmak gvisor userspace ağ yığınını yola sokar ve motorları değil Docker Desktop'ı ölçer.
4. **Tek seferde tek sistem ayakta.** 3.9 GB RAM zorunluluğu.
5. **Cold start → healthcheck → 30 sn warmup (atılır) → ölçüm.**
6. **Round-robin tekrarlar.** Her turda 4 sistem sırayla koşar, 3 tur. Sistemleri arka arkaya koşturmak, laptop'ta termal throttling nedeniyle son koşanı cezalandırır. Round-robin bunu dört sisteme eşit dağıtır.
7. **Medyan + p95/p99 raporlanır**, ortalama değil.
8. **Image'lar digest ile pinlenir** (`images.lock`). Kayan tag public benchmark'ta reprodüksiyonu imkânsız kılar.

## 5. Dayanıklılık Matrisi

### 5.1 Metodolojik karar: hangi eksen eşitlenir

EC ve replication aynı anda iki eksende eşitlenemez:

- MinIO 4 disk EC:2 → 2 disk kaybına dayanır, %50 kullanılabilir
- SeaweedFS `001` replication → 1 sunucu kaybına dayanır, %50 kullanılabilir
- MinIO 4 disk EC:1 → 1 disk kaybına dayanır, %75 kullanılabilir

Depolama verimini eşitlersen hata toleransı kayar; tersi de doğru.

**Karar: hata toleransı eşitlenir (hepsi 1 cihaz/sunucu kaybına dayanır), depolama verimi sonuç olarak raporlanır.** Gerekçe: hata toleransı operasyonel gereksinimdir, depolama verimi mimarinin sonucudur. EC'nin replication'a karşı avantajı tam olarak bu tabloda görünür hale gelir.

### 5.2 Konfigürasyonlar

| Sistem | Konfig 1 — redundancy yok | Konfig 2 — 1 cihaz kaybına dayanır | Beklenen kullanılabilir alan |
|---|---|---|---|
| MinIO | tek disk, standalone | 4 disk, EC parity 1 | %75 |
| Silo | tek disk, standalone | 4 disk, EC parity 1 | %75 |
| RustFS | tek disk | 4 disk, parity 1 eşdeğeri | %75 (doğrulanacak) |
| SeaweedFS | 1 volume server, `-defaultReplication=000` | 4 volume server, `-defaultReplication=001` | %50 |

**Implementation-time doğrulama gerektiren kalemler** (spec bunları varsaymaz, kurulum sırasında CLI'dan teyit edilir):
- RustFS'in EC parity flag sözdizimi ve tek-disk modunun varlığı
- MinIO/Silo'da 4 diskte `EC:1` ayarının kabul edilip edilmediği (`MINIO_STORAGE_CLASS_STANDARD=EC:1`)
- SeaweedFS `001` replication'ın 4 volume server ile beklendiği gibi yerleştiği

SeaweedFS'in `volume.ec.encode` erasure coding'i arşiv/write-once odaklıdır ve sonradan uygulanır; sıcak veri yolunda kullanılmaz. Performans matrisine sokulmaz, conformance ve nitel değerlendirmede dipnot olarak yer alır.

## 6. Performans Workload Matrisi

Araç: `warp` v1.6.1 (arm64), container olarak.

| Profil | Obje boyutu | Operasyon | Neyi test eder |
|---|---|---|---|
| small | 4 KiB | mixed, put, get | RustFS'in "MinIO'nun 2.3x'i" iddiası |
| medium | 1 MiB | mixed | Genel amaçlı |
| bigdata | 20 MiB | get, put | Parquet/sequential; MinIO'nun önde olduğu iddia edilen bölge |
| multipart | 128 MiB | multipart | Yedekleme, büyük obje |
| list | 100k obje | list | Lakehouse'un acı noktası |
| delete | — | delete | Temizlik maliyeti |

- Concurrency sweep: 8 / 32 / 64 (8 CPU'da üstü anlamsız)
- Aynı workload dosyası tüm sistemlere uygulanır; sisteme özgü ayar yapılmaz
- Profil başına 60 sn ölçüm + 30 sn warmup
- Çapraz doğrulama: seçili profillerde `s3tester` ile spot kontrol (warp MinIO kökenli olduğu için tarafsızlık teyidi)

## 7. Telemetri

`docker stats` saniyelik akış → CPU%, RAM, net I/O, block I/O.

Türetilen metrikler:
- **Peak RAM ve idle RAM** — 3.9 GB'lık kutuda kimin şişman olduğu
- **MB/s başına CPU%** — verimlilik; kısıtlı donanımda throughput kadar belirleyici
- Image boyutu, cold start süresi, healthy olana kadar geçen süre

## 8. S3 Conformance Suite

pytest + boto3, ~25 hedefli assertion. Donanımdan bağımsız — bu çalışmanın en uzun ömürlü ve en taşınabilir çıktısı.

| Grup | Test edilen |
|---|---|
| **Conditional write** | `If-None-Match: *`, `If-Match`. **Iceberg/Delta commit protokolü buna dayanır.** SeaweedFS'te desteklenmediğine dair kayıt var, doğrulanacak. |
| Versioning | Enable, list versions, delete marker davranışı |
| Object Lock / WORM | Retention modları, legal hold |
| Multipart | init/upload/complete/abort, part boyut sınırları, out-of-order part |
| Presigned URL | GET/PUT, expiry davranışı |
| ListObjectsV2 | Continuation token pagination, delimiter/prefix, 1000+ obje |
| Diğer | CopyObject, range GET, tagging, bucket policy, SSE-S3, SSE-C |

Çıktı: `destekleniyor / kısmi / yok` matrisi, her hücrede gözlemlenen davranış notu.

## 9. Repo Yapısı (public)

```
minio-alternatives-benchmark/
  README.md              # ne ölçüyor, ne ölçmüyor, nasıl koşulur — kısıtlar ilk ekranda
  METHODOLOGY.md         # adalet kontrolleri, dayanıklılık kararı, bilinen limitler
  CONTRIBUTING.md        # kendi donanımında koştur, results/ altına PR aç
  LICENSE                # Apache-2.0
  images.lock            # pinlenmiş sha256 digest'leri
  compose/
    _common.yaml  minio.yaml  silo.yaml  rustfs.yaml  seaweedfs.yaml
  bench/
    run.sh  workloads.yaml  telemetry.sh  hwprofile.sh
  conformance/
    conftest.py  test_s3_conformance.py
  results/
    m1-8gb-macos/raw/*.json        # bu çalışmanın koşumu
    <community-profiles>/          # gelen PR'lar
  docs/
    RESULTS.md  blog-1-conformance.md  blog-2-methodology.md
```

### 9.1 Donanım havuzunu genişleten mekanizma

Tek laptop'tan çıkan sayılar dar kalır. `CONTRIBUTING.md` + `hwprofile.sh` bunu yapısal olarak çözer: her sonuç dosyası otomatik toplanan bir `hardware-profile.json` ile damgalanır (CPU modeli, çekirdek sayısı, RAM, disk tipi, kernel, container runtime, image digest'leri). Hiçbir sayı bağlamsız dolaşmaz ve zamanla tek-donanımlı ölçüm çok-donanımlı bir veri setine dönüşür.

### 9.2 Lisans notu

Harness Apache-2.0. MinIO ve Silo'nun AGPL-3.0'ı bulaşmaz: container'lar ağ üzerinden S3 API ile çağrılıyor, kod linklenmiyor veya türetilmiyor. README'de açıkça belirtilir — sorulacak bir soru.

## 10. Blog Yazıları

**Yazı 1 — Conformance öncelikli.** "Hangi S3 alternatifine Iceberg/Delta koyabilirsin". Conditional write, Object Lock, versioning matrisi merkezde. Donanım kısıtından tamamen bağımsız, uzun ömürlü, alan boş. Big data ekiplerinin doğrudan ihtiyacı olan tablo.

**Yazı 2 — Metodoloji + performans bulguları.** "8 GB'lık bir MacBook'ta dört S3 deposu: neyi ölçebilirsin, neyi ölçemezsin". Adalet kontrolleri, dayanıklılık ekseni kararı, termal round-robin, gvisor tuzağı. Performans tabloları burada, kısıtlarıyla birlikte.

Platform-nötr markdown yazılır.

## 11. Kısıtlar ve Dürüstlük Notları

Rapor ve README'nin **ilk ekranında**, dipnotta değil:

1. Sonuçlar tek node + macOS VM ortamından. **Mutlak throughput donanıma taşınmaz; karşılaştırmalı okunur.**
2. 3.9 GB RAM tavanı yüksek concurrency'de dört sistemi de sınırlar. Sonuçlar bu tavan altındaki davranışı yansıtır.
3. Ceph RGW ve Apache Ozone ölçülmedi; tek node'da alacakları sonuç mimarilerini temsil etmez.
4. `warp` MinIO kökenlidir. Saf S3 API konuşur, ancak tarafsızlık için `s3tester` ile spot doğrulama yapılır.
5. RustFS `rc` aşamasındadır ve distributed mode'u "under testing" olarak işaretlidir. Bulgular sürüme özgüdür, tarih ve digest damgalıdır.
6. Performansta "kazanan" başlığı atılmaz. Çıktı bir karar matrisidir: hangi iş yükünde, hangi kısıt altında, hangisi.

## 12. Yayın Akışı

`git init` yapıldı, spec commit'lendi. **Uzak repo oluşturma ve push, kullanıcının açık onayı olmadan yapılmaz.** Harness ve sonuçlar yerelde hazırlanır; yayın kararı ve zamanlaması kullanıcıya aittir.

## 13. Başarı Kriterleri

- Dört sistem de iki dayanıklılık konfigünde ayağa kalkar ve workload matrisini tamamlar
- Conformance matrisi 4 sistem × ~25 assertion için doldurulur
- `bench/run.sh` tek komutla baştan sona koşar ve `results/` altına damgalı JSON üretir
- Üçüncü bir kişi repo'yu klonlayıp kendi donanımında koşturabilir ve sonucunu PR'layabilir
- İki blog yazısı taslağı `docs/` altında hazır
